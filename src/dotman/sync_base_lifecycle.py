"""Shared Base decisions; callers own selection, Observation, effects and ordering."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from dotman.models import ResolvedSyncTarget
from dotman.sync_base_store import (
    DirectoryChildPresent,
    FilePresent,
    SyncBasePayload,
    SyncBaseRecord,
    SyncBaseRecordCorruptionError,
    SyncBaseStore,
    SyncBaseStoreError,
    SyncBaseStoreDurabilityError,
)
from dotman.sync_scope import sync_unit_identity_bytes


@dataclass(frozen=True)
class BaseUnit:
    """One successfully statically resolved, selected file or directory child."""

    identity: ResolvedSyncTarget
    configured_policy: str

    def __post_init__(self) -> None:
        sync_unit_identity_bytes(self.identity)
        if self.configured_policy not in (
            "push-only",
            "pull-only",
            "both",
            "push-only-delete",
        ):
            raise ValueError("invalid configured Sync Policy")

    @property
    def identity_bytes(self) -> bytes:
        return sync_unit_identity_bytes(self.identity)

    @property
    def eligible(self) -> bool:
        return self.configured_policy in ("pull-only", "both")


@dataclass(frozen=True)
class FrozenBaseUnit:
    """The final repository outcome, frozen alongside its qualifying evidence."""

    unit: BaseUnit
    payload: SyncBasePayload

    def record(self) -> SyncBaseRecord:
        return SyncBaseRecord(self.unit.identity_bytes, self.payload)


@dataclass(frozen=True)
class BaseInspection:
    status: Literal["usable", "unavailable", "not-applicable"]
    reason: str | None = None
    record: SyncBaseRecord | None = None
    failure: SyncBaseStoreError | None = None


@dataclass(frozen=True)
class BaseLifecycleResult:
    acknowledged: bool = False
    failure: SyncBaseStoreError | None = None

    @property
    def warning_code(self) -> str | None:
        if self.failure is None:
            return None
        return ("base-durability-uncertain" if isinstance(self.failure, SyncBaseStoreDurabilityError)
                else "base-acknowledgment-failed")


def record_matches_identity(
    record: SyncBaseRecord, identity: ResolvedSyncTarget
) -> bool:
    """Validate record shape against its canonical Sync Unit identity."""
    return (
        record.identity == sync_unit_identity_bytes(identity)
        and not (
            isinstance(record.payload, FilePresent)
            and identity.child_path is not None
        )
        and not (
            isinstance(record.payload, DirectoryChildPresent)
            and identity.child_path is None
        )
    )


class SyncBaseLifecycle:
    """No session orchestration: invoke each method at its documented boundary."""

    def __init__(self, store: SyncBaseStore, *, preview: bool = False) -> None:
        self.store = store
        self.preview = preview

    def inspect(self, unit: BaseUnit) -> BaseInspection:
        """Read only; no projections, live reads or cleanup.

        A corrupt record is an unavailable Base; any other store failure raises.
        """
        if not unit.eligible:
            return BaseInspection("not-applicable", "ineligible")
        try:
            record = self.store.read(unit.identity_bytes)
        except SyncBaseRecordCorruptionError as exc:
            return BaseInspection("unavailable", exc.reason, failure=exc)
        if record is None:
            return BaseInspection("unavailable", "absent")
        if not record_matches_identity(record, unit.identity):
            return BaseInspection(
                "unavailable", "record_corrupt",
                failure=SyncBaseStoreError("checkpoint does not match Sync Unit identity"),
            )
        return BaseInspection("usable", record=record)

    def acknowledge(self, frozen: FrozenBaseUnit) -> BaseLifecycleResult:
        """Save the frozen outcome as the Base: at direct Observation (before review or hooks)
        or at the unit's earliest ordered completion point (before its target post-hook)."""
        if self.preview or not frozen.unit.eligible:
            return BaseLifecycleResult()
        try:
            self.store.replace(frozen.record())
        except SyncBaseStoreDurabilityError as exc:
            return BaseLifecycleResult(acknowledged=True, failure=exc)
        except SyncBaseStoreError as exc:
            return BaseLifecycleResult(failure=exc)
        return BaseLifecycleResult(acknowledged=True)
