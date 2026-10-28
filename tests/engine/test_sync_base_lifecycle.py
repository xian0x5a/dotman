import pytest

from dotman.models import ResolvedSyncTarget
from dotman.sync_base_lifecycle import (
    BaseUnit, FrozenBaseUnit, ProposalCompletion, SyncBaseLifecycle,
)
from dotman.sync_base_store import (
    DirectoryChildPresent, FilePresent, Missing, SyncBaseStore, SyncBaseStoreError,
)


def unit(policy="both", child=None):
    return BaseUnit(ResolvedSyncTarget("main", "app", "config", child_path=child), policy)


@pytest.mark.parametrize("payload,child", [(Missing(), None), (FilePresent(b"dirty"), None),
                                          (DirectoryChildPresent(b"tool", True), "tool")])
def test_completion_saves_frozen_repository_outcome(tmp_path, payload, child):
    selected = unit(child=child)
    with SyncBaseStore.open(tmp_path / "state" / "dotman", "main") as store:
        lifecycle = SyncBaseLifecycle(store)
        result = lifecycle.complete(FrozenBaseUnit(selected, payload),
                                    ProposalCompletion(True, primary_effect="succeeded"))
        assert result.acknowledged
        record = store.read(selected.identity_bytes)
        assert record.payload == payload
        assert lifecycle.inspect(selected).record == record


def test_unapproved_completion_does_not_acknowledge(tmp_path):
    with SyncBaseStore.open(tmp_path / "state" / "dotman", "main") as store:
        result = SyncBaseLifecycle(store).complete(
            FrozenBaseUnit(unit(), Missing()), ProposalCompletion(False))
        assert not result.acknowledged
        assert store.read(unit().identity_bytes) is None


def test_checkpoint_write_failure_does_not_fail_completion():
    failure = SyncBaseStoreError("unavailable")
    class Unavailable:
        def replace(self, record):
            raise failure
    result = SyncBaseLifecycle(Unavailable()).complete(
        FrozenBaseUnit(unit(), Missing()), ProposalCompletion(True))
    assert not result.acknowledged
    assert result.failure is failure


@pytest.mark.parametrize('direct', [False, True])
def test_post_commit_durability_failure_preserves_acknowledgment(tmp_path, monkeypatch, direct):
    from dotman.sync_base_store import SyncBaseStoreDurabilityError

    with SyncBaseStore.open(tmp_path / 'state' / 'dotman', 'main') as store:
        lifecycle = SyncBaseLifecycle(store)
        replace_record = store.replace
        failure = SyncBaseStoreDurabilityError('checkpoint committed; durability uncertain')

        def fail_after_commit(record):
            replace_record(record)
            raise failure

        monkeypatch.setattr(store, 'replace', fail_after_commit)
        frozen = FrozenBaseUnit(unit(), FilePresent(b'committed outcome'))
        result = (lifecycle.direct_agreement(frozen) if direct else
                  lifecycle.complete(frozen, ProposalCompletion(True)))
        assert result.acknowledged
        assert result.failure is failure
        assert store.read(unit().identity_bytes).payload == FilePresent(b'committed outcome')


def test_checkpoint_read_failure_raises_instead_of_reporting_a_base_state():
    failure = SyncBaseStoreError("unavailable")
    class Unavailable:
        def read(self, identity):
            raise failure
    with pytest.raises(SyncBaseStoreError) as raised:
        SyncBaseLifecycle(Unavailable()).inspect(unit())
    assert raised.value is failure


def test_eligible_policy_change_keeps_base(tmp_path):
    with SyncBaseStore.open(tmp_path / "state" / "dotman", "main") as store:
        lifecycle = SyncBaseLifecycle(store)
        lifecycle.direct_agreement(FrozenBaseUnit(unit(), FilePresent(b"outcome")))
        assert lifecycle.inspect(unit("pull-only")).status == "usable"


def test_preview_never_mutates(tmp_path):
    with SyncBaseStore.open(tmp_path / "state" / "dotman", "main") as store:
        SyncBaseLifecycle(store).direct_agreement(FrozenBaseUnit(unit(), Missing()))
        lifecycle = SyncBaseLifecycle(store, preview=True)
        assert not lifecycle.direct_agreement(FrozenBaseUnit(unit(), FilePresent(b"new"))).acknowledged
        assert not lifecycle.complete(FrozenBaseUnit(unit(), FilePresent(b"new")), ProposalCompletion(True)).acknowledged
        assert store.read(unit().identity_bytes).payload == Missing()


@pytest.mark.parametrize("policy", ["push-only", "push-only-delete"])
def test_ineligible_completion_does_not_checkpoint(tmp_path, policy):
    with SyncBaseStore.open(tmp_path / "state" / "dotman", "main") as store:
        result = SyncBaseLifecycle(store).complete(
            FrozenBaseUnit(unit(policy), Missing()), ProposalCompletion(True))
        assert not result.acknowledged
        assert store.read(unit(policy).identity_bytes) is None


def test_corruption_is_unavailable_without_automatic_deletion():
    from dotman.sync_base_store import SyncBaseRecordCorruptionError
    class Corrupt:
        def read(self, identity):
            raise SyncBaseRecordCorruptionError("broken", affected_identities=(identity,))
        def delete(self, identity):
            pytest.fail("must not delete rejected storage automatically")
    result = SyncBaseLifecycle(Corrupt()).inspect(unit())
    assert result.status == "unavailable"
    assert result.failure is not None
