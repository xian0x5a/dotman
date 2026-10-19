"""Metadata-only Sync Base workflows; never observe drift or execute projections."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, replace
import hashlib

from dotman.models import ResolvedSyncTarget
from dotman.operation_lock import OperationLock
from dotman.sync_base_lifecycle import (
    BaseInspection, SyncBaseLifecycle, record_matches_identity,
)
from dotman.sync_base_store import (
    Missing, DirectoryChildPresent,
    SyncBaseStore, SyncBaseStoreError,
)
from dotman.sync_directory import census_directory, child_metadata
from dotman.sync_observation import _base_unit, _resolve_inputs
from dotman.sync_scope import _parse_scope_selector, resolve_sync_scope, sync_unit_identity_bytes


UNUSABLE_RESET_HINT = "Run `dotman reset sync-base --unusable` to discard them."


def _identity(text: str) -> ResolvedSyncTarget:
    parsed = _parse_scope_selector(text)
    # Identities never glob: `*?[]` are literal name characters, matched exactly.
    if parsed.target_name is None:
        raise ValueError("Sync Base requires one exact repo-qualified file or directory-child identity")
    identity = ResolvedSyncTarget(
        repo=parsed.repo, package_id=parsed.package_id, target_name=parsed.target_name,
        bound_profile=parsed.bound_profile, child_path=parsed.child_path,
    )
    if identity.canonical != text:
        raise ValueError("Sync Base identity must be canonical")
    sync_unit_identity_bytes(identity)
    return identity


def _unit(inputs, identity):
    entry = inputs.get(replace(identity, child_path=None))
    if entry is None:
        raise ValueError(f"Sync Unit '{identity.canonical}' is not currently tracked")
    _item, metadata = entry
    directory = metadata.target.target_type == "directory"
    if metadata.target.probe is not None:
        raise ValueError("Probe Work is not a Sync Unit")
    if directory != (identity.child_path is not None):
        raise ValueError("Sync Base requires a file target or an exact directory child")
    if directory:
        metadata = child_metadata(metadata, identity.child_path)
    return _base_unit(identity, metadata)


def _store_exists(context, repo) -> bool:
    try:
        return SyncBaseStore.exists(context.tracked_state.state_root, repo.config.state_key)
    except SyncBaseStoreError as exc:
        raise SyncBaseStoreError(f"{repo.config.name}: {exc}") from exc


def _store_unversioned(context, repo) -> bool:
    try:
        return SyncBaseStore.unversioned(context.tracked_state.state_root, repo.config.state_key)
    except SyncBaseStoreError as exc:
        raise SyncBaseStoreError(f"{repo.config.name}: {exc}") from exc


@contextmanager
def _open(context, repo, *, read_only=True):
    try:
        # Inspection and exact reset must not fill gaps in an existing store.
        with SyncBaseStore.open(
            context.tracked_state.state_root, repo.config.state_key,
            read_only=read_only, create=False,
        ) as store:
            yield store
    except SyncBaseStoreError as exc:
        path = context.tracked_state.state_root / "repos" / repo.config.state_key
        raise SyncBaseStoreError(f"{repo.config.name}: {path}: {exc}") from exc


def _detail(unit, inspection):
    result = {
        "identity": unit.identity.canonical, "status": inspection.status,
        "reason": inspection.reason, "policy": unit.configured_policy,
        "eligibility": unit.eligible, "payload": None,
        "checks": {"integrity": None},
    }
    # Do not reveal any authoritative-looking metadata from an unusable record.
    record = inspection.record
    if inspection.status == "usable":
        payload = record.payload
        missing = isinstance(payload, Missing)
        result.update(
            payload={
                "kind": "missing" if missing else "present",
                "size": 0 if missing else len(payload.content),
                "digest": None if missing else hashlib.sha256(payload.content).hexdigest(),
                "executable": payload.executable if isinstance(payload, DirectoryChildPresent) else None,
            },
            checks={"integrity": True},
        )
    elif inspection.reason in ("record_corrupt", "payload_corrupt"):
        result["checks"]["integrity"] = False
    return result


def info_sync_base(context, text: str):
    identity = _identity(text)
    scope = resolve_sync_scope(context, [text])
    inputs, _ = _resolve_inputs(scope)
    unit = _unit(inputs, identity)
    repo = context.repositories[identity.repo]
    if not _store_exists(context, repo):
        return _detail(unit, BaseInspection(
            "unavailable" if unit.eligible else "not-applicable",
            "absent" if unit.eligible else "ineligible",
        ))
    with _open(context, repo) as store, store.read_transaction():
        # Exact inspection must not decode unrelated records.
        return _detail(unit, SyncBaseLifecycle(store, operation="sync", preview=True).inspect(unit))


def list_sync_bases(context):
    scope = resolve_sync_scope(context, include_on_demand=True)
    inputs, _ = _resolve_inputs(scope)
    entries = []
    for repo_config in context.config.ordered_repos:
        repo = context.repositories[repo_config.name]
        if not _store_exists(context, repo):
            continue
        with _open(context, repo) as store, store.read_transaction():
            lifecycle = SyncBaseLifecycle(store, operation="sync", preview=True)
            for key in store.identities():
                try:
                    identity = _identity(key.decode("utf-8"))
                    if identity.repo != repo.config.name:
                        continue
                    unit = _unit(inputs, identity)
                except (ValueError, UnicodeError):
                    continue
                if not unit.eligible:
                    continue
                inspected = lifecycle.inspect(unit)
                if inspected.status == "usable":
                    entries.append(_detail(unit, inspected))
    return sorted(entries, key=lambda entry: entry["identity"])


def reset_sync_base(context, text: str):
    identity = _identity(text)
    with OperationLock.acquire(context.tracked_state.state_root):
        scope = resolve_sync_scope(context, [text])
        inputs, _ = _resolve_inputs(scope)
        _unit(inputs, identity)
        repo = context.repositories[identity.repo]
        deleted = False
        if _store_exists(context, repo):
            with _open(context, repo, read_only=False) as store:
                deleted = store.delete(sync_unit_identity_bytes(identity))
        return {"identity": text, "status": "reset" if deleted else "already_absent"}


def _static_inputs(context):
    # Failed static resolution is not evidence that stored identities are orphaned.
    try:
        scope = resolve_sync_scope(context, include_on_demand=True)
        inputs, _ = _resolve_inputs(scope)
    except ValueError:
        return None
    return inputs


@dataclass(frozen=True)
class _UnusableBases:
    """What doctor counts as corrupt or orphaned in one repo's store."""

    unreadable_files: int  # corrupt record files whose identity cannot be recovered
    corrupt: tuple[bytes, ...]
    orphaned: tuple[bytes, ...]

    @property
    def corrupt_count(self) -> int:
        return self.unreadable_files + len(self.corrupt)


def _classify_unusable(context, repo, store, inputs, censuses) -> _UnusableBases:
    """Single source of the corrupt/orphaned verdicts shared by doctor and reset --unusable."""
    corrupt = []
    orphaned = []
    scanned = store.scan()
    for record in scanned.records:
        try:
            identity = _identity(record.identity.decode("utf-8"))
            if (
                identity.repo != repo.config.name
                or not record_matches_identity(record, identity)
            ):
                corrupt.append(record.identity)
                continue
        except (ValueError, UnicodeError):
            corrupt.append(record.identity)
            continue
        if inputs is None:
            continue
        try:
            _unit(inputs, identity)
        except ValueError:
            orphaned.append(record.identity)
            continue
        if identity.child_path is not None:
            target = replace(identity, child_path=None)
            item, metadata = inputs[target]
            # Inspection cannot execute Guards to prove participation.
            guarded_scopes = (item.repo, metadata.package, metadata.target, *metadata.path_rules)
            if any(
                (scope.hooks or {}).get(name)
                for scope in guarded_scopes
                for name in ("guard_push", "guard_pull")
            ):
                continue
            if target not in censuses:
                censuses[target] = census_directory(
                    metadata,
                    follow_live_directories=context.config.dir_symlink_mode == "follow",
                )
            census = censuses[target]
            # Exclusions and failed discovery cannot prove absence.
            if census.unrestricted and identity.child_path not in dict(census.entries):
                orphaned.append(record.identity)
    return _UnusableBases(scanned.corrupt_count, tuple(corrupt), tuple(orphaned))


def reset_unusable_sync_bases(context, repo_name: str | None):
    """Discard what doctor flags; those Bases were not usable anyway."""
    repos = [
        context.repositories[repo_config.name] for repo_config in context.config.ordered_repos
        if repo_name in (None, repo_config.name)
    ]
    if repo_name is not None and not repos:
        raise ValueError(f"unknown repo '{repo_name}'")
    with OperationLock.acquire(context.tracked_state.state_root):
        inputs = _static_inputs(context)
        censuses = {}
        dropped = 0
        for repo in repos:
            # Like doctor, leave unversioned stores to the next real operation.
            if _store_unversioned(context, repo) or not _store_exists(context, repo):
                continue
            with _open(context, repo) as store, store.read_transaction():
                unusable = _classify_unusable(context, repo, store, inputs, censuses)
            with _open(context, repo, read_only=False) as store:
                dropped += store.discard_corrupt_files()
                dropped += sum(store.delete(identity) for identity in (*unusable.corrupt, *unusable.orphaned))
        return {"status": "reset", "dropped": dropped}


def doctor_sync_bases(context):
    from dotman.doctor import DoctorCheck

    checks = []
    inputs = _static_inputs(context)
    censuses = {}
    for repo_config in context.config.ordered_repos:
        repo = context.repositories[repo_config.name]
        path = context.tracked_state.state_root / "repos" / repo.config.state_key
        try:
            if _store_unversioned(context, repo):
                checks.append(DoctorCheck(
                    key="sync_bases_store", status="warn",
                    detail="unversioned Sync Base store; the next real Push, Pull, or Sync replaces it",
                    path=path, repo_name=repo.config.name,
                ))
                continue
            if not _store_exists(context, repo):
                continue
            with _open(context, repo) as store, store.read_transaction():
                unusable = _classify_unusable(context, repo, store, inputs, censuses)
            for category, count in (("corrupt", unusable.corrupt_count), ("orphaned", len(unusable.orphaned))):
                checks.append(DoctorCheck(
                    key=f"sync_bases_{category}", status="warn" if count else "ok",
                    detail=f"{count} {category} Sync Bases", path=path, repo_name=repo.config.name, count=count,
                    hint=UNUSABLE_RESET_HINT if count else None,
                ))
        except (SyncBaseStoreError, OSError) as exc:
            checks.append(DoctorCheck(
                key="sync_bases_store", status="failed", detail=str(exc), path=path,
                repo_name=repo.config.name,
            ))
    return checks
