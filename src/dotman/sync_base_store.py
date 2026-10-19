from __future__ import annotations

import base64
import binascii
import errno
import fcntl
import hashlib
import json
import os
import re
import stat
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Final, Self, TypeAlias

from dotman.config import validate_state_key
from dotman.models import sync_unit_target_identity

STORE_EPOCH: Final = 2
# Where records live, versioned separately from the record format (STORE_EPOCH):
# a layout change moves records without changing their bytes.
STORE_LAYOUT: Final = 2
LAYOUT_FILE_NAME: Final = "sync-bases.layout"
LOCK_FILE_NAME: Final = "sync-bases.lock"
BASES_DIRECTORY_NAME: Final = "bases"
RECORD_FILE_SUFFIX: Final = ".json"
_PRIVATE_DIRECTORY_MODE: Final = 0o700
_PRIVATE_FILE_MODE: Final = 0o600


class SyncBaseStoreError(RuntimeError):
    """A failure that prevents a Sync Base store operation from being trusted."""


class SyncBaseStoreDurabilityError(SyncBaseStoreError):
    """The new record committed, but its survival across a crash is uncertain."""

    committed: Final = True
    durability_uncertain: Final = True


class SyncBaseStoreUnsupportedRuntimeError(SyncBaseStoreError):
    """The runtime lacks required secure filesystem capabilities."""


class SyncBaseStoreSecurityError(SyncBaseStoreError):
    """The store's filesystem layout is not private and trustworthy."""


class SyncBaseStoreLockedError(SyncBaseStoreError):
    """Another store operation owns a conflicting lock."""


class SyncBaseStoreEpochError(SyncBaseStoreError):
    """A record uses an unsupported format epoch."""


class SyncBaseStoreLayoutError(SyncBaseStoreError):
    """The store uses a layout this version cannot read."""


class SyncBaseRecordCorruptionError(SyncBaseStoreError):
    """One self-contained record failed integrity validation."""

    def __init__(
        self,
        detail: str,
        *,
        affected_identities: tuple[bytes, ...],
        reason: str = "record_corrupt",
    ) -> None:
        self.reason = reason
        self.detail = detail
        self.affected_identities = affected_identities
        super().__init__(detail)


def _require_bytes(value: object, *, field_name: str, allow_empty: bool) -> bytes:
    if type(value) is not bytes:
        raise TypeError(f"{field_name} must be bytes")
    if not allow_empty and not value:
        raise ValueError(f"{field_name} must not be empty")
    return value


@dataclass(frozen=True)
class Missing:
    """The acknowledged repository representation is absent."""


@dataclass(frozen=True)
class FilePresent:
    content: bytes

    def __post_init__(self) -> None:
        _require_bytes(self.content, field_name="file content", allow_empty=True)


@dataclass(frozen=True)
class DirectoryChildPresent:
    content: bytes
    executable: bool

    def __post_init__(self) -> None:
        _require_bytes(
            self.content, field_name="directory-child content", allow_empty=True
        )
        if type(self.executable) is not bool:
            raise TypeError("directory-child executable must be bool")


SyncBasePayload: TypeAlias = Missing | FilePresent | DirectoryChildPresent


@dataclass(frozen=True)
class SyncBaseRecord:
    identity: bytes
    payload: SyncBasePayload

    def __post_init__(self) -> None:
        _require_bytes(
            self.identity, field_name="canonical identity", allow_empty=False
        )
        if type(self.payload) not in (Missing, FilePresent, DirectoryChildPresent):
            raise TypeError("Sync Base payload must have an exact supported type")
        if type(self.payload) is FilePresent:
            FilePresent(self.payload.content)
        elif type(self.payload) is DirectoryChildPresent:
            DirectoryChildPresent(self.payload.content, self.payload.executable)


@dataclass(frozen=True)
class SyncBaseScan:
    """Validated records and isolated corruption, without exposing unusable metadata."""

    records: tuple[SyncBaseRecord, ...]
    corrupt_files: tuple[RecordLocation, ...]

    @property
    def corrupt_count(self) -> int:
        return len(self.corrupt_files)


def _identity(status: os.stat_result) -> tuple[int, int]:
    return status.st_dev, status.st_ino


def _validate_inode(path: Path, status: os.stat_result, *, directory: bool) -> None:
    if stat.S_ISLNK(status.st_mode):
        raise SyncBaseStoreSecurityError(
            f"Sync Base store path must not be a symlink: {path}"
        )
    expected_type = stat.S_ISDIR if directory else stat.S_ISREG
    kind = "directory" if directory else "regular file"
    if not expected_type(status.st_mode):
        raise SyncBaseStoreSecurityError(
            f"Sync Base store path must be a {kind}: {path}"
        )
    if status.st_uid != os.geteuid():
        raise SyncBaseStoreSecurityError(
            f"Sync Base store path has wrong owner: {path}"
        )
    if not directory and status.st_nlink != 1:
        raise SyncBaseStoreSecurityError(
            f"Sync Base store file must not have hard links: {path}"
        )


def _validate_status(path: Path, status: os.stat_result, *, directory: bool) -> None:
    _validate_inode(path, status, directory=directory)
    expected_mode = _PRIVATE_DIRECTORY_MODE if directory else _PRIVATE_FILE_MODE
    if stat.S_IMODE(status.st_mode) != expected_mode:
        raise SyncBaseStoreSecurityError(
            f"Sync Base store path mode must be {expected_mode:#05o}: {path}"
        )


def _repair_mode(path: Path, descriptor: int, *, directory: bool) -> None:
    """Tighten only mode bits of a pinned inode; every other defect still fails."""
    status = os.fstat(descriptor)
    _validate_inode(path, status, directory=directory)
    expected_mode = _PRIVATE_DIRECTORY_MODE if directory else _PRIVATE_FILE_MODE
    if stat.S_IMODE(status.st_mode) != expected_mode:
        os.fchmod(descriptor, expected_mode)


Directory: TypeAlias = tuple[Path, int]


class _PrivateLayout:
    """Pin the private tree; never follow a replaced directory during Python I/O.

    The repository directory chain stays pinned for the store's lifetime. The
    `bases` directory is pinned on first use, and a target group only while an
    operation uses it, so one read validates a bounded set of paths.
    """

    def __init__(
        self, manager_root: Path, state_key: str, *, create: bool, repair: bool
    ) -> None:
        self.directory = manager_root / "repos" / state_key
        self.bases_directory = self.directory / BASES_DIRECTORY_NAME
        self._repair = repair
        self._directories: list[Directory] = []
        self._bases: Directory | None = None
        self._groups: dict[str, Directory] = {}
        self._files: dict[str, int] = {}
        self._parent_descriptor: int | None = None
        try:
            if create:
                manager_root.parent.mkdir(parents=True, exist_ok=True)
            # The XDG parent is caller-trusted, outside the private-tree contract.
            self._parent_descriptor = os.open(
                manager_root.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
            )
            parent_descriptor = self._parent_descriptor
            for path in (manager_root, manager_root / "repos", self.directory):
                descriptor = self._pin(path, parent_descriptor, create=create)
                if descriptor is None:
                    raise FileNotFoundError(errno.ENOENT, "Sync Base store is absent", str(path))
                self._directories.append((path, descriptor))
                self.check_directories()
                parent_descriptor = descriptor
        except BaseException:
            self.close()
            raise

    def _pin(self, path: Path, parent_descriptor: int, *, create: bool) -> int | None:
        """Open one private directory below a pinned parent; None when absent."""
        if create:
            try:
                os.mkdir(path.name, _PRIVATE_DIRECTORY_MODE, dir_fd=parent_descriptor)
                os.fsync(parent_descriptor)
            except FileExistsError:
                pass
        try:
            before = os.stat(path.name, dir_fd=parent_descriptor, follow_symlinks=False)
        except FileNotFoundError:
            return None
        _validate_inode(path, before, directory=True)
        descriptor = os.open(
            path.name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=parent_descriptor,
        )
        try:
            if _identity(os.fstat(descriptor)) != _identity(before):
                raise SyncBaseStoreSecurityError(
                    f"Sync Base directory changed while opening: {path}"
                )
            if self._repair:
                _repair_mode(path, descriptor, directory=True)
            _validate_status(path, os.fstat(descriptor), directory=True)
        except BaseException:
            os.close(descriptor)
            raise
        return descriptor

    @property
    def descriptor(self) -> int:
        return self._directories[-1][1]

    @property
    def root(self) -> Directory:
        return self._directories[-1]

    def bases(self, *, create: bool) -> Directory | None:
        if self._bases is None:
            descriptor = self._pin(self.bases_directory, self.descriptor, create=create)
            if descriptor is not None:
                self._bases = (self.bases_directory, descriptor)
        return self._bases

    @contextmanager
    def group(self, name: str, *, create: bool = False) -> Iterator[Directory | None]:
        """Pin one target group for the duration of an operation."""
        self.check_directories()
        bases = self.bases(create=create)
        path = self.bases_directory / name
        descriptor = None if bases is None else self._pin(path, bases[1], create=create)
        if descriptor is None:
            yield None
            return
        self._groups[name] = (path, descriptor)
        try:
            self.check_directories()
            yield path, descriptor
        finally:
            entry = self._groups.pop(name, None)
            if entry is not None:
                os.close(entry[1])

    def group_names(self) -> list[str]:
        bases = self.bases(create=False)
        return [] if bases is None else sorted(self.file_names(bases[1]))

    def remove_group_if_empty(self, name: str) -> None:
        path, descriptor = self._groups[name]
        if self.file_names(descriptor):
            return
        bases = self._bases
        assert bases is not None
        del self._groups[name]
        os.close(descriptor)
        os.rmdir(name, dir_fd=bases[1])
        os.fsync(bases[1])

    def check_directories(self) -> None:
        pinned = [*self._directories, *([self._bases] if self._bases else []), *self._groups.values()]
        for path, descriptor in pinned:
            current = path.lstat()
            opened = os.fstat(descriptor)
            _validate_status(path, current, directory=True)
            _validate_status(path, opened, directory=True)
            if _identity(current) != _identity(opened):
                raise SyncBaseStoreSecurityError(
                    f"Sync Base directory was substituted: {path}"
                )

    def open_file(self, name: str, *, create: bool = False) -> int:
        descriptor = self._open_file_descriptor(self.root, name, create=create)
        self._files[name] = descriptor
        return descriptor

    def _open_file_descriptor(self, directory: Directory, name: str, *, create: bool = False) -> int:
        self.check_directories()
        directory_path, directory_descriptor = directory
        path = directory_path / name
        before = (
            None
            if create
            else os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
        )
        if before is not None:
            _validate_inode(path, before, directory=False)
        flags = os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
        flags |= (os.O_RDWR | os.O_CREAT | os.O_EXCL) if create else os.O_RDONLY
        descriptor = os.open(name, flags, _PRIVATE_FILE_MODE, dir_fd=directory_descriptor)
        try:
            opened = os.fstat(descriptor)
            if before is not None and _identity(opened) != _identity(before):
                raise SyncBaseStoreSecurityError(
                    f"Sync Base file changed while opening: {path}"
                )
            _validate_status(path, os.fstat(descriptor), directory=False)
            current = os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
            _validate_status(path, current, directory=False)
            if _identity(current) != _identity(opened):
                raise SyncBaseStoreSecurityError(
                    f"Sync Base file was substituted: {path}"
                )
            self.check_directories()
        except BaseException:
            os.close(descriptor)
            raise
        return descriptor

    @staticmethod
    def file_names(directory_descriptor: int) -> set[str]:
        # Reusing a scanned directory's open file description can miss newly
        # created entries on Btrfs. Open a fresh stream relative to the pinned
        # directory; dup would share the old enumeration state.
        descriptor = os.open(
            ".",
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=directory_descriptor,
        )
        try:
            return set(os.listdir(descriptor))
        finally:
            os.close(descriptor)

    def root_names(self) -> set[str]:
        return self.file_names(self.descriptor) & {
            LAYOUT_FILE_NAME, LOCK_FILE_NAME, BASES_DIRECTORY_NAME,
        }

    def _validate_file(self, directory: Directory, name: str) -> None:
        directory_path, directory_descriptor = directory
        if self._repair:
            # O_NONBLOCK keeps a planted FIFO from hanging before type validation.
            descriptor = os.open(
                name,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
                dir_fd=directory_descriptor,
            )
            try:
                _repair_mode(directory_path / name, descriptor, directory=False)
            finally:
                os.close(descriptor)
        current = os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
        _validate_status(directory_path / name, current, directory=False)

    def validate_tree(self) -> None:
        """Validate (and when writable, repair) every store path once."""
        for name in sorted(self.root_names() - {BASES_DIRECTORY_NAME}):
            self._validate_file(self.root, name)
        for group_name in self.group_names():
            with self.group(group_name) as directory:
                assert directory is not None
                for name in sorted(self.file_names(directory[1])):
                    self._validate_file(directory, name)

    def check(self) -> None:
        """Re-validate pinned directories and held files before each use."""
        self.check_directories()
        for name, descriptor in self._files.items():
            path = self.directory / name
            try:
                current = os.stat(name, dir_fd=self.descriptor, follow_symlinks=False)
            except FileNotFoundError:
                raise SyncBaseStoreSecurityError(
                    f"an opened Sync Base file disappeared: {path}"
                ) from None
            _validate_status(path, current, directory=False)
            if _identity(current) != _identity(os.fstat(descriptor)):
                raise SyncBaseStoreSecurityError(
                    f"Sync Base file was substituted: {path}"
                )

    def close(self) -> None:
        for descriptor in self._files.values():
            os.close(descriptor)
        self._files.clear()
        for _, descriptor in self._groups.values():
            os.close(descriptor)
        self._groups.clear()
        if self._bases is not None:
            os.close(self._bases[1])
            self._bases = None
        for _, descriptor in reversed(self._directories):
            os.close(descriptor)
        self._directories.clear()
        if self._parent_descriptor is not None:
            os.close(self._parent_descriptor)
            self._parent_descriptor = None


@contextmanager
def _locked(descriptor: int, *, write: bool) -> Iterator[None]:
    try:
        fcntl.flock(
            descriptor, (fcntl.LOCK_EX if write else fcntl.LOCK_SH) | fcntl.LOCK_NB
        )
    except OSError as exc:
        if exc.errno in (errno.EACCES, errno.EAGAIN):
            raise SyncBaseStoreLockedError(
                "Sync Base store transaction is locked"
            ) from exc
        raise
    try:
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)


@contextmanager
def _store_errors() -> Iterator[None]:
    try:
        yield
    except OSError as exc:
        raise SyncBaseStoreSecurityError(
            f"Sync Base filesystem operation failed: {exc}"
        ) from exc


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")


RecordLocation: TypeAlias = tuple[str, str]


def _target_group(target: bytes) -> str:
    return hashlib.sha256(target).hexdigest()


def _record_location(identity: bytes) -> RecordLocation:
    """Group a record under its target; hashes avoid length and case-folding limits."""
    target = sync_unit_target_identity(identity.decode("utf-8")).encode("utf-8")
    return _target_group(target), f"{hashlib.sha256(identity).hexdigest()}{RECORD_FILE_SUFFIX}"


def _metadata_digest(body: dict[str, object]) -> str:
    # Authenticate the expected payload digest, not its encoded bytes, so intact
    # metadata can distinguish damaged payload from damaged record structure.
    metadata = {key: value for key, value in body.items() if key != "content"}
    return hashlib.sha256(_canonical_json(metadata)).hexdigest()


def _encode(record: SyncBaseRecord) -> bytes:
    payload = record.payload
    body = {
        "epoch": STORE_EPOCH,
        "identity": base64.b64encode(record.identity).decode("ascii"),
        "shape": "missing"
        if isinstance(payload, Missing)
        else "file"
        if isinstance(payload, FilePresent)
        else "directory-child",
        "content": None
        if isinstance(payload, Missing)
        else base64.b64encode(payload.content).decode("ascii"),
        "executable": payload.executable
        if isinstance(payload, DirectoryChildPresent)
        else None,
        "content_digest": None
        if isinstance(payload, Missing)
        else hashlib.sha256(payload.content).hexdigest(),
        "content_size": None if isinstance(payload, Missing) else len(payload.content),
    }
    return _canonical_json({"record": body, "digest": _metadata_digest(body)})


def _decode(
    content: bytes,
    name: str,
    expected_identity: bytes | None,
    *,
    location: RecordLocation,
) -> SyncBaseRecord:
    try:
        container = json.loads(content)
        if type(container) is not dict or set(container) != {"record", "digest"}:
            raise ValueError("invalid record container")
        body = container["record"]
        if type(body) is not dict or set(body) != {
            "epoch",
            "identity",
            "shape",
            "content",
            "executable",
            "content_digest",
            "content_size",
        }:
            raise ValueError("invalid record fields")
        if container["digest"] != _metadata_digest(body):
            raise ValueError("record digest mismatch")
        if type(body["epoch"]) is not int or body["epoch"] != STORE_EPOCH:
            raise SyncBaseStoreEpochError(
                f"unsupported Sync Base record epoch {body['epoch']}"
            )
        identity = base64.b64decode(body["identity"], validate=True)
        if (
            not identity
            or _record_location(identity) != location
            or (expected_identity is not None and identity != expected_identity)
        ):
            raise ValueError("record identity mismatch")
        shape = body["shape"]
        if shape == "missing":
            if any(
                body[field] is not None
                for field in ("content", "executable", "content_digest", "content_size")
            ):
                raise ValueError("Missing record contains payload")
            payload: SyncBasePayload = Missing()
        else:
            if not (
                (shape == "file" and body["executable"] is None)
                or (shape == "directory-child" and type(body["executable"]) is bool)
            ):
                raise ValueError("invalid payload shape")
            if (
                type(body["content_size"]) is not int
                or body["content_size"] < 0
                or type(body["content_digest"]) is not str
                or re.fullmatch(r"[0-9a-f]{64}", body["content_digest"]) is None
            ):
                raise ValueError("invalid payload metadata")
            try:
                raw = base64.b64decode(body["content"], validate=True)
                if (
                    len(raw) != body["content_size"]
                    or hashlib.sha256(raw).hexdigest() != body["content_digest"]
                    or base64.b64encode(raw).decode("ascii") != body["content"]
                ):
                    raise ValueError("payload integrity mismatch")
            except (ValueError, TypeError, binascii.Error) as exc:
                raise SyncBaseRecordCorruptionError(
                    f"invalid Sync Base payload {name}: {exc}",
                    affected_identities=(identity,),
                    reason="payload_corrupt",
                ) from exc
            payload = (
                FilePresent(raw)
                if shape == "file"
                else DirectoryChildPresent(raw, body["executable"])
            )
        result = SyncBaseRecord(identity, payload)
        # A single canonical representation rejects duplicate fields and ambiguous encodings.
        if _encode(result) != content:
            raise ValueError("noncanonical record encoding")
        return result
    except (
        ValueError,
        TypeError,
        KeyError,
        UnicodeError,
        binascii.Error,
        RecursionError,
    ) as exc:
        raise SyncBaseRecordCorruptionError(
            f"invalid Sync Base record {name}: {exc}",
            affected_identities=()
            if expected_identity is None
            else (expected_identity,),
        ) from exc


class SyncBaseStore:
    """Private per-unit records with atomic replacement and short nonblocking locks."""

    def __init__(
        self,
        manager_root: Path,
        state_key: str,
        layout: _PrivateLayout,
        lock_descriptor: int,
        *,
        read_only: bool,
    ) -> None:
        self.manager_state_root = manager_root
        self.repo_state_key = state_key
        self.repo_state_directory = layout.directory
        self.read_only = read_only
        self._layout = layout
        self._lock_descriptor = lock_descriptor
        self._reading = False
        self._closed = False

    @staticmethod
    def _store_names(manager_state_root: str | Path, repo_state_key: str) -> set[str]:
        directory = Path(manager_state_root) / "repos" / repo_state_key
        try:
            return set(os.listdir(directory)) & {
                LAYOUT_FILE_NAME, LOCK_FILE_NAME, BASES_DIRECTORY_NAME,
            }
        except FileNotFoundError:
            return set()
        except OSError as exc:
            raise SyncBaseStoreError(f"{directory}: {exc}") from exc

    @classmethod
    def exists(cls, manager_state_root: str | Path, repo_state_key: str) -> bool:
        """Whether a store was established, without opening or creating it.

        Only the layout marker establishes a store; an unversioned directory
        holds no trusted records, so it reads as absent until creation.
        """
        return LAYOUT_FILE_NAME in cls._store_names(manager_state_root, repo_state_key)

    @classmethod
    def unversioned(cls, manager_state_root: str | Path, repo_state_key: str) -> bool:
        """Whether store paths exist without the marker; creation replaces them."""
        names = cls._store_names(manager_state_root, repo_state_key)
        return bool(names) and LAYOUT_FILE_NAME not in names

    @classmethod
    def open(
        cls,
        manager_state_root: str | Path,
        repo_state_key: str,
        *,
        read_only: bool = False,
        create: bool = True,
    ) -> SyncBaseStore:
        """Open private storage; writable opens tighten modes of owned store paths.

        Read-only opens never change storage. Wrong owner, type, symlinks, and
        hard links are always rejected rather than repaired.
        """
        manager_root = Path(manager_state_root)
        if not manager_root.is_absolute():
            raise ValueError("manager state root must be absolute")
        state_key = validate_state_key(repo_state_key, repo_name=repo_state_key)
        create = create and not read_only
        with _store_errors():
            cls._check_runtime()
            layout = _PrivateLayout(
                manager_root, state_key, create=create, repair=not read_only
            )
            try:
                names = layout.root_names()
                if create and LAYOUT_FILE_NAME not in names:
                    lock = cls._establish(layout, lock_exists=LOCK_FILE_NAME in names)
                elif LOCK_FILE_NAME in names:
                    lock = cls._open_lock(layout)
                elif create and names == {LAYOUT_FILE_NAME}:
                    # An interrupted creation that wrote the marker first holds
                    # no records, so recreating its lock cannot adopt any.
                    lock = layout.open_file(LOCK_FILE_NAME, create=True)
                    os.fsync(lock)
                    os.fsync(layout.descriptor)
                else:
                    raise SyncBaseStoreSecurityError("Sync Base store lock is missing")
                with _locked(lock, write=False):
                    layout.validate_tree()
                    cls._require_layout(layout)
                return cls(manager_root, state_key, layout, lock, read_only=read_only)
            except BaseException:
                layout.close()
                raise

    @staticmethod
    def _open_lock(layout: _PrivateLayout) -> int:
        # Repair before opening: opening validates the private mode.
        layout._validate_file(layout.root, LOCK_FILE_NAME)
        return layout.open_file(LOCK_FILE_NAME)

    @classmethod
    def _establish(cls, layout: _PrivateLayout, *, lock_exists: bool) -> int:
        """Create the store; the layout marker is written last.

        Without the marker no store exists, so records under `bases/` from an
        interrupted creation or an older dotman are dropped rather than adopted.
        Other files in the repository state directory are left untouched.
        """
        if lock_exists:
            lock = cls._open_lock(layout)
        else:
            lock = layout.open_file(LOCK_FILE_NAME, create=True)
            os.fsync(lock)
        with _locked(lock, write=True):
            # A concurrent open may have established the store meanwhile.
            if LAYOUT_FILE_NAME not in layout.root_names():
                cls._drop_records(layout)
                cls._create_layout_marker(layout)
                os.fsync(layout.descriptor)
        return lock

    @staticmethod
    def _drop_records(layout: _PrivateLayout) -> None:
        for group in layout.group_names():
            with layout.group(group) as directory:
                assert directory is not None
                for name in layout.file_names(directory[1]):
                    os.unlink(name, dir_fd=directory[1])
                layout.remove_group_if_empty(group)

    @staticmethod
    def _create_layout_marker(layout: _PrivateLayout) -> None:
        temporary = f".{uuid.uuid4().hex}.tmp"
        descriptor = layout._open_file_descriptor(layout.root, temporary, create=True)
        try:
            os.write(descriptor, f"{STORE_LAYOUT}\n".encode("ascii"))
            os.fsync(descriptor)
            os.replace(
                temporary, LAYOUT_FILE_NAME,
                src_dir_fd=layout.descriptor, dst_dir_fd=layout.descriptor,
            )
        finally:
            os.close(descriptor)
            try:
                os.unlink(temporary, dir_fd=layout.descriptor)
            except FileNotFoundError:
                pass

    @staticmethod
    def _require_layout(layout: _PrivateLayout) -> None:
        expected = f"{STORE_LAYOUT}\n".encode("ascii")
        try:
            descriptor = layout._open_file_descriptor(layout.root, LAYOUT_FILE_NAME)
        except FileNotFoundError:
            found = "an unversioned layout"
        else:
            try:
                content = os.pread(descriptor, len(expected) + 1, 0)
            finally:
                os.close(descriptor)
            if content == expected:
                return
            found = f"layout {content[:16]!r}"
        raise SyncBaseStoreLayoutError(
            f"Sync Base store {layout.directory} uses {found}; "
            f"this dotman reads layout {STORE_LAYOUT}"
        )

    @staticmethod
    def _check_runtime() -> None:
        dir_fd_functions = {function.__name__ for function in os.supports_dir_fd}
        if (
            not {"open", "mkdir", "stat", "rename", "unlink", "rmdir"} <= dir_fd_functions
            or "listdir" not in {function.__name__ for function in os.supports_fd}
            or "stat"
            not in {function.__name__ for function in os.supports_follow_symlinks}
            or not all(
                hasattr(os, name)
                for name in (
                    "O_DIRECTORY",
                    "O_NOFOLLOW",
                    "O_CLOEXEC",
                    "O_NONBLOCK",
                    "pread",
                    "geteuid",
                )
            )
            or not callable(getattr(fcntl, "flock", None))
        ):
            raise SyncBaseStoreUnsupportedRuntimeError(
                "Sync Base storage requires POSIX descriptor-relative I/O and flock"
            )

    def _require_open(self) -> None:
        if self._closed:
            raise SyncBaseStoreError("Sync Base store is closed")

    @contextmanager
    def read_transaction(self) -> Iterator[SyncBaseStore]:
        """Hold a coherent view across reads; cooperating writes fail nonblocking."""
        self._require_open()
        if self._reading:
            raise SyncBaseStoreError("a Sync Base read transaction is already active")
        with _store_errors(), _locked(self._lock_descriptor, write=False):
            self._layout.check()
            self._reading = True
            try:
                yield self
            finally:
                self._reading = False

    @contextmanager
    def _write_transaction(self) -> Iterator[None]:
        self._require_open()
        if self.read_only:
            raise SyncBaseStoreError("Sync Base store is read-only")
        if self._reading:
            raise SyncBaseStoreError(
                "cannot mutate inside a Sync Base read transaction"
            )
        with _store_errors(), _locked(self._lock_descriptor, write=True):
            self._layout.check()
            yield

    def _read_file(
        self,
        directory: Directory,
        name: str,
        identity: bytes | None = None,
        *,
        location: RecordLocation,
    ) -> SyncBaseRecord | None:
        directory_path, directory_descriptor = directory
        self._layout.check()
        try:
            descriptor = self._layout._open_file_descriptor(directory, name)
        except FileNotFoundError:
            return None
        try:
            before = os.fstat(descriptor)
            content = bytearray()
            while len(content) < before.st_size:
                chunk = os.pread(
                    descriptor, before.st_size - len(content), len(content)
                )
                if not chunk:
                    raise SyncBaseStoreSecurityError(
                        "Sync Base record changed during read"
                    )
                content.extend(chunk)
            after = os.fstat(descriptor)
            current = os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
            if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            ) or _identity(current) != _identity(after):
                raise SyncBaseStoreSecurityError("Sync Base record changed during read")
            self._layout.check()
            return _decode(
                bytes(content), str(directory_path / name), identity, location=location
            )
        finally:
            os.close(descriptor)

    def _scan_group(self, group: str) -> SyncBaseScan:
        records = []
        corrupt_files = []
        with self._layout.group(group) as directory:
            if directory is None:
                return SyncBaseScan((), ())
            for name in sorted(self._layout.file_names(directory[1])):
                # Interrupted replacements leave hidden temporaries, never records.
                if name.startswith(".") or not name.endswith(RECORD_FILE_SUFFIX):
                    continue
                try:
                    record = self._read_file(directory, name, location=(group, name))
                except SyncBaseRecordCorruptionError:
                    # A broken record may have no recoverable identity. Count the
                    # self-contained file rather than trusting corrupted metadata.
                    corrupt_files.append((group, name))
                    continue
                if record is None:
                    raise SyncBaseStoreSecurityError(
                        "Sync Base record disappeared during enumeration"
                    )
                records.append(record)
        return SyncBaseScan(tuple(records), tuple(corrupt_files))

    def record_path(self, identity: bytes) -> Path:
        """Locate one record for diagnostics without opening or creating storage."""
        identity = _require_bytes(
            identity, field_name="canonical identity", allow_empty=False
        )
        group, name = _record_location(identity)
        return self._layout.bases_directory / group / name

    def read(self, identity: bytes) -> SyncBaseRecord | None:
        identity = _require_bytes(
            identity, field_name="canonical identity", allow_empty=False
        )
        location = _record_location(identity)
        self._require_open()
        if not self._reading:
            with self.read_transaction():
                return self.read(identity)
        with _store_errors(), self._layout.group(location[0]) as directory:
            if directory is None:
                return None
            return self._read_file(directory, location[1], identity, location=location)

    def scan(self) -> SyncBaseScan:
        """Read a coherent inventory; record corruption never hides healthy units."""
        self._require_open()
        if not self._reading:
            with self.read_transaction():
                return self.scan()
        with _store_errors():
            groups = [self._scan_group(group) for group in self._layout.group_names()]
        return SyncBaseScan(
            tuple(sorted(
                (record for group in groups for record in group.records),
                key=lambda record: record.identity,
            )),
            tuple(location for group in groups for location in group.corrupt_files),
        )

    def target_records(self, target: bytes) -> tuple[SyncBaseRecord, ...]:
        """Return healthy records of one file target or one directory target's children."""
        target = _require_bytes(target, field_name="target identity", allow_empty=False)
        if sync_unit_target_identity(target.decode("utf-8")).encode("utf-8") != target:
            raise ValueError(f"{target!r} is not a target identity")
        self._require_open()
        if not self._reading:
            with self.read_transaction():
                return self.target_records(target)
        with _store_errors():
            records = self._scan_group(_target_group(target)).records
        return tuple(sorted(records, key=lambda record: record.identity))

    def identities(self) -> tuple[bytes, ...]:
        """Return only identities whose records passed integrity validation."""
        return tuple(record.identity for record in self.scan().records)

    def replace(self, record: SyncBaseRecord) -> None:
        if type(record) is not SyncBaseRecord:
            raise TypeError("record must be a SyncBaseRecord")
        record = SyncBaseRecord(record.identity, record.payload)
        _record_location(record.identity)
        with self._write_transaction():
            self._replace_locked(record)

    def _replace_locked(self, record: SyncBaseRecord) -> None:
        content = _encode(record)
        location = _record_location(record.identity)
        with self._layout.group(location[0], create=True) as directory:
            assert directory is not None
            directory_path, directory_descriptor = directory
            name = location[1]
            # A corrupt record is unavailable, not permission to overwrite it silently.
            self._read_file(directory, name, record.identity, location=location)
            temporary = f".{uuid.uuid4().hex}.tmp"
            descriptor = self._layout._open_file_descriptor(directory, temporary, create=True)
            try:
                with os.fdopen(descriptor, "wb", closefd=False) as stream:
                    stream.write(content)
                    stream.flush()
                    os.fsync(descriptor)
                self._layout.check_directories()
                current = os.stat(
                    temporary, dir_fd=directory_descriptor, follow_symlinks=False
                )
                _validate_status(directory_path / temporary, current, directory=False)
                if _identity(current) != _identity(os.fstat(descriptor)):
                    raise SyncBaseStoreSecurityError(
                        "Sync Base temporary file was substituted"
                    )
                os.replace(
                    temporary,
                    name,
                    src_dir_fd=directory_descriptor,
                    dst_dir_fd=directory_descriptor,
                )
                try:
                    os.fsync(directory_descriptor)
                except OSError as exc:
                    # Rename is the logical commit: rollback would be another
                    # fallible mutation, not restoration of the pre-commit guarantee.
                    raise SyncBaseStoreDurabilityError(
                        f"Sync Base checkpoint committed at {directory_path / name}; "
                        f"crash durability is uncertain: {exc}"
                    ) from exc
            finally:
                os.close(descriptor)
                try:
                    os.unlink(temporary, dir_fd=directory_descriptor)
                except FileNotFoundError:
                    pass

    def delete(self, identity: bytes) -> bool:
        identity = _require_bytes(
            identity, field_name="canonical identity", allow_empty=False
        )
        with self._write_transaction():
            return self._delete_file(*_record_location(identity))

    def discard_corrupt_files(self) -> int:
        """Remove record files that fail validation; they have no identity to delete by."""
        with self._write_transaction():
            locations = [
                location
                for group in self._layout.group_names()
                for location in self._scan_group(group).corrupt_files
            ]
            for location in locations:
                self._delete_file(*location)
            return len(locations)

    def _delete_file(self, group: str, name: str) -> bool:
        with self._layout.group(group) as directory:
            if directory is None:
                return False
            try:
                descriptor = self._layout._open_file_descriptor(directory, name)
            except FileNotFoundError:
                return False
            os.close(descriptor)
            os.unlink(name, dir_fd=directory[1])
            os.fsync(directory[1])
            # Empty groups would otherwise accumulate for every untracked target.
            self._layout.remove_group_if_empty(group)
            return True

    def close(self) -> None:
        if self._reading:
            raise SyncBaseStoreError("cannot close during a Sync Base read transaction")
        if not self._closed:
            self._closed = True
            self._layout.close()

    def __enter__(self) -> Self:
        self._require_open()
        return self

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()
