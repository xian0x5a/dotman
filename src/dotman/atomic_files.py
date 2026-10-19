from __future__ import annotations

import os
import re
import stat
from pathlib import Path
from uuid import uuid4

_TEMP_FILE_PREFIX = ".dotman-"
_TEMP_FILE_SUFFIX = ".tmp"
_TEMP_FILE_TOKEN_HEX_LENGTH = 32
_TEMP_FILE_NAME_PATTERN = re.compile(
    rf"{re.escape(_TEMP_FILE_PREFIX)}"
    rf"(?P<pid>[1-9][0-9]*)-"
    rf"(?P<token>[0-9a-f]{{{_TEMP_FILE_TOKEN_HEX_LENGTH}}})"
    rf"{re.escape(_TEMP_FILE_SUFFIX)}"
)
_DEFAULT_FILE_CREATION_MODE = 0o666
_PRIVATE_FILE_CREATION_MODE = 0o600


def _atomic_temp_file_prefix() -> str:
    return f"{_TEMP_FILE_PREFIX}{os.getpid()}-"


def _new_atomic_temp_path(directory: Path) -> Path:
    return directory / f"{_atomic_temp_file_prefix()}{uuid4().hex}{_TEMP_FILE_SUFFIX}"


def write_bytes_atomic(path: Path, content: bytes, *, mode: int | None = None) -> None:
    create_directories_durably(path.parent)
    cleanup_stale_atomic_temp_files(path.parent)
    replacement_mode = mode if mode is not None else target_replacement_mode(path)
    replaced_owner = _replaced_regular_file_owner(path)
    candidate_temp_path = _new_atomic_temp_path(path.parent)
    temp_path: Path | None = None
    # With a known replacement mode the temp file starts owner-only, so its
    # content is never readable by others before the final mode applies. That
    # mode is set after the content is flushed, because a write clears
    # setuid/setgid bits. Otherwise exclusive creation applies the process
    # umask, which is the final mode of a new file.
    creation_mode = _PRIVATE_FILE_CREATION_MODE if replacement_mode is not None else _DEFAULT_FILE_CREATION_MODE
    try:
        file_descriptor = os.open(candidate_temp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, creation_mode)
        temp_path = candidate_temp_path
        with os.fdopen(file_descriptor, "wb") as temp_file:
            temp_file.write(content)
            temp_file.flush()
            if replaced_owner is not None:
                # Before fchmod: a chown clears setuid/setgid bits.
                _keep_owner_and_group(temp_file.fileno(), replaced_owner)
            if replacement_mode is not None:
                os.fchmod(temp_file.fileno(), replacement_mode)
            os.fsync(temp_file.fileno())
        temp_path.replace(path)
        _fsync_directory(path.parent)
    finally:
        cleanup_atomic_temp_file(temp_path)



def write_text_atomic(
    path: Path,
    content: str,
    *,
    encoding: str = "utf-8",
    mode: int | None = None,
) -> None:
    write_bytes_atomic(path, content.encode(encoding), mode=mode)



def write_symlink_atomic(path: Path, target: str | Path) -> None:
    create_directories_durably(path.parent)
    cleanup_stale_atomic_temp_files(path.parent)
    candidate_temp_path = _new_atomic_temp_path(path.parent)
    temp_path: Path | None = None
    try:
        candidate_temp_path.symlink_to(target)
        temp_path = candidate_temp_path
        temp_path.replace(path)
        _fsync_directory(path.parent)
    finally:
        cleanup_atomic_temp_file(temp_path)



def target_replacement_mode(path: Path) -> int | None:
    try:
        return stat.S_IMODE(path.stat().st_mode)
    except FileNotFoundError:
        # Exclusive creation already applies the process umask. Leaving that
        # mode intact avoids mutating the process-global umask during writes.
        return None



def _replaced_regular_file_owner(path: Path) -> os.stat_result | None:
    # lstat: the rename replaces a symlink itself, so the owner of its
    # destination (a root-owned file in /nix/store or /etc) must not carry over.
    try:
        replaced_entry = path.lstat()
    except FileNotFoundError:
        return None
    return replaced_entry if stat.S_ISREG(replaced_entry.st_mode) else None



def _keep_owner_and_group(file_descriptor: int, replaced_status: os.stat_result) -> None:
    # The rename installs a new inode owned by the writer. A service that reads
    # its config through the old owner or group (root:grafana 0640) would lose
    # access. An unprivileged writer gets PermissionError here, which callers
    # in file_access answer by retrying through sudo.
    created_status = os.fstat(file_descriptor)
    uid = replaced_status.st_uid if replaced_status.st_uid != created_status.st_uid else -1
    gid = replaced_status.st_gid if replaced_status.st_gid != created_status.st_gid else -1
    if uid != -1 or gid != -1:
        os.fchown(file_descriptor, uid, gid)



def create_directories_durably(directory: Path) -> None:
    missing_directories: list[Path] = []
    while not directory.exists():
        missing_directories.append(directory)
        directory = directory.parent
    for directory in reversed(missing_directories):
        directory.mkdir(exist_ok=True)
        # A new entry is durable only if the directories leading to it are.
        _fsync_directory(directory.parent)



def _fsync_directory(directory: Path) -> None:
    # The rename is durable only once its directory entry is on disk; Sync Base
    # records and snapshot manifests acknowledge content written here.
    directory_descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory_descriptor)
    finally:
        os.close(directory_descriptor)



def default_created_file_mode() -> int:
    current_umask = os.umask(0)
    os.umask(current_umask)
    return _DEFAULT_FILE_CREATION_MODE & ~current_umask



def cleanup_atomic_temp_file(temp_path: Path | None) -> None:
    if temp_path is None:
        return
    try:
        temp_path.unlink()
    except FileNotFoundError:
        pass



def cleanup_stale_atomic_temp_files(directory: Path) -> None:
    for temp_path in directory.glob(f"{_TEMP_FILE_PREFIX}*{_TEMP_FILE_SUFFIX}"):
        if atomic_temp_file_owner_is_definitely_absent(temp_path):
            cleanup_atomic_temp_file(temp_path)



def atomic_temp_file_owner_is_definitely_absent(temp_path: Path) -> bool:
    name_match = _TEMP_FILE_NAME_PATTERN.fullmatch(temp_path.name)
    if name_match is None:
        # Without a verified creator PID, the sweeper cannot prove that removing
        # this path is safe. Only the writer that created it may clean it up.
        return False
    pid = int(name_match.group("pid"))
    if pid == os.getpid():
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except (OSError, OverflowError):
        return False
    return False


__all__ = [
    "create_directories_durably",
    "atomic_temp_file_owner_is_definitely_absent",
    "cleanup_atomic_temp_file",
    "cleanup_stale_atomic_temp_files",
    "default_created_file_mode",
    "target_replacement_mode",
    "write_bytes_atomic",
    "write_symlink_atomic",
    "write_text_atomic",
]
