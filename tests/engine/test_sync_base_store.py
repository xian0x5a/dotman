from __future__ import annotations

import json
import os
import stat

import pytest

from dotman.sync_base_store import (
    DirectoryChildPresent,
    FilePresent,
    Missing,
    SyncBaseRecord,
    SyncBaseRecordCorruptionError,
    SyncBaseStore,
    SyncBaseStoreDurabilityError,
    SyncBaseStoreError,
    SyncBaseStoreLockedError,
    SyncBaseStoreSecurityError,
)


def record(identity=b"main:app.unit", payload=None):
    return SyncBaseRecord(identity, payload if payload is not None else FilePresent(b"data"))


def record_path(store):
    return next(store.repo_state_directory.glob("bases/*/*.json"))


@pytest.mark.parametrize(
    "payload",
    [
        Missing(),
        FilePresent(b""),
        FilePresent(b"\x00\xff"),
        DirectoryChildPresent(b"script", True),
        DirectoryChildPresent(b"script", False),
    ],
)
def test_typed_payload_roundtrip(tmp_path, payload):
    root = tmp_path / "manager"
    with SyncBaseStore.open(root, "repo") as store:
        store.replace(record(payload=payload))
        assert store.record_path(b"main:app.unit").is_file()
        assert store.record_path(b"main:app.unit").parent.parent == store.repo_state_directory / "bases"
    with SyncBaseStore.open(root, "repo", read_only=True) as store:
        assert store.read(b"main:app.unit") == record(payload=payload)
        assert store.read(b"main:app.absent") is None
        assert store.identities() == (b"main:app.unit",)


def test_private_self_contained_records(tmp_path):
    with SyncBaseStore.open(tmp_path / "manager", "repo") as store:
        store.replace(record(b"main:app.a"))
        store.replace(record(b"main:app.b"))
        files = list(store.repo_state_directory.glob("bases/*/*.json"))
        assert len(files) == 2
        for path in (tmp_path / "manager").rglob("*"):
            assert path.stat().st_mode & 0o777 == (0o700 if path.is_dir() else 0o600)
        files[0].unlink()
        assert len(store.identities()) == 1


def test_corruption_is_per_unit_and_explicitly_discardable(tmp_path):
    with SyncBaseStore.open(tmp_path / "manager", "repo") as store:
        store.replace(record())
        path = record_path(store)
        store.replace(record(b"main:app.healthy"))
        path.write_bytes(b"main:app.broken")
        with pytest.raises(SyncBaseRecordCorruptionError) as error:
            store.read(b"main:app.unit")
        assert error.value.affected_identities == (b"main:app.unit",)
        assert store.read(b"main:app.healthy") == record(b"main:app.healthy")
        assert store.delete(b"main:app.unit")
        assert store.read(b"main:app.healthy") == record(b"main:app.healthy")
        assert store.read(b"main:app.unit") is None


def test_scan_isolates_corruption_without_cleanup(tmp_path):
    with SyncBaseStore.open(tmp_path / "manager", "repo") as store:
        store.replace(record(b"main:app.healthy"))
        store.replace(record(b"main:app.broken"))
        broken_path = store.record_path(b"main:app.broken")
        broken_path.write_bytes(b"main:app.broken")
        before = broken_path.read_bytes()
        with store.read_transaction():
            scanned = store.scan()
            assert scanned.records == (record(b"main:app.healthy"),)
            assert scanned.corrupt_count == 1
            assert store.identities() == (b"main:app.healthy",)
            assert store.read(b"main:app.healthy") == record(b"main:app.healthy")
            with pytest.raises(SyncBaseRecordCorruptionError):
                store.read(b"main:app.broken")
        assert broken_path.read_bytes() == before


def test_failed_replacement_preserves_previous_record(tmp_path, monkeypatch):
    with SyncBaseStore.open(tmp_path / "manager", "repo") as store:
        store.replace(record())

        def fail(*args, **kwargs):
            raise OSError("injected replacement failure")

        monkeypatch.setattr(os, "replace", fail)
        with pytest.raises(SyncBaseStoreError):
            store.replace(record(payload=FilePresent(b"new")))
        assert store.read(b"main:app.unit") == record()
        assert len(list(store.repo_state_directory.glob("bases/*/*.json"))) == 1


def test_identical_replacement_skips_rewrite(tmp_path):
    with SyncBaseStore.open(tmp_path / "manager", "repo") as store:
        store.replace(record())
        path = record_path(store)
        before = path.stat()
        store.replace(record())
        after = path.stat()
        assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns)
        store.replace(record(payload=FilePresent(b"new")))
        assert path.stat().st_ino != before.st_ino
        assert store.read(b"main:app.unit") == record(payload=FilePresent(b"new"))


def test_read_transaction_excludes_writer_and_refreshes(tmp_path):
    root = tmp_path / "manager"
    with (
        SyncBaseStore.open(root, "repo") as first,
        SyncBaseStore.open(root, "repo") as second,
    ):
        first.replace(record())
        with first.read_transaction():
            assert first.read(b"main:app.unit") == record()
            with pytest.raises(SyncBaseStoreLockedError):
                second.replace(record(payload=Missing()))
            with second.read_transaction():
                assert second.read(b"main:app.unit") == record()
            with pytest.raises(SyncBaseStoreError):
                first.delete(b"main:app.unit")
        second.replace(record(payload=Missing()))
        assert first.read(b"main:app.unit") == record(payload=Missing())


def _loosen_modes(root):
    for path in [root, *root.rglob("*")]:
        path.chmod(0o755 if path.is_dir() else 0o644)


def _modes(root):
    return {path: path.stat().st_mode & 0o777 for path in [root, *root.rglob("*")]}


def test_writable_open_repairs_owned_nonprivate_modes(tmp_path):
    root = tmp_path / "manager"
    with SyncBaseStore.open(root, "repo") as store:
        store.replace(record())
    _loosen_modes(root)
    with SyncBaseStore.open(root, "repo") as store:
        assert store.read(b"main:app.unit") == record()
    assert all(
        mode == (0o700 if path.is_dir() else 0o600)
        for path, mode in _modes(root).items()
    )


def test_read_only_open_rejects_nonprivate_modes_without_repair(tmp_path):
    root = tmp_path / "manager"
    with SyncBaseStore.open(root, "repo") as store:
        store.replace(record())
    _loosen_modes(root)
    before = _modes(root)
    with pytest.raises(SyncBaseStoreSecurityError):
        SyncBaseStore.open(root, "repo", read_only=True)
    assert _modes(root) == before


def test_writable_open_still_rejects_hardlinked_record(tmp_path):
    root = tmp_path / "manager"
    with SyncBaseStore.open(root, "repo") as store:
        store.replace(record())
        path = record_path(store)
    path.chmod(0o644)
    os.link(path, tmp_path / "alias")
    with pytest.raises(SyncBaseStoreSecurityError):
        SyncBaseStore.open(root, "repo")
    assert path.stat().st_mode & 0o777 == 0o644


@pytest.mark.parametrize("mode", [0o644, 0o666])
def test_rejects_record_mode_changed_while_open(tmp_path, mode):
    with SyncBaseStore.open(tmp_path / "manager", "repo") as store:
        store.replace(record())
        path = record_path(store)
        path.chmod(mode)
        with pytest.raises(SyncBaseStoreSecurityError):
            store.read(b"main:app.unit")
        with pytest.raises(SyncBaseStoreSecurityError):
            store.scan()
        with pytest.raises(SyncBaseStoreSecurityError):
            store.replace(record(payload=Missing()))
        assert path.stat().st_mode & 0o777 == mode


def test_rejects_symlink_record_and_directory(tmp_path):
    root = tmp_path / "manager"
    with SyncBaseStore.open(root, "repo") as store:
        store.replace(record())
        path = record_path(store)
        outside = tmp_path / "outside"
        path.rename(outside)
        path.symlink_to(outside)
        with pytest.raises(SyncBaseStoreSecurityError):
            store.read(b"main:app.unit")
        with pytest.raises(SyncBaseStoreSecurityError):
            store.delete(b"main:app.unit")
        assert outside.exists()
    root.rename(tmp_path / "moved")
    root.symlink_to(tmp_path / "moved", target_is_directory=True)
    with pytest.raises(SyncBaseStoreSecurityError):
        SyncBaseStore.open(root, "repo")


def test_read_only_never_creates_and_cannot_mutate(tmp_path):
    root = tmp_path / "manager"
    with pytest.raises(SyncBaseStoreError):
        SyncBaseStore.open(root, "repo", read_only=True)
    assert not root.exists()
    with SyncBaseStore.open(root, "repo") as store:
        store.replace(record())
    before = {
        p: (p.read_bytes(), p.stat().st_mtime_ns)
        for p in root.rglob("*")
        if p.is_file()
    }
    with SyncBaseStore.open(root, "repo", read_only=True) as store:
        assert store.read(b"main:app.unit") == record()
        with pytest.raises(SyncBaseStoreError):
            store.delete(b"main:app.unit")
    assert before == {
        p: (p.read_bytes(), p.stat().st_mtime_ns)
        for p in root.rglob("*")
        if p.is_file()
    }


def test_identity_binding_and_integrity(tmp_path):
    with SyncBaseStore.open(tmp_path / "manager", "repo") as store:
        store.replace(record())
        first = record_path(store)
        original = first.read_bytes()
        store.replace(record(b"main:app.other"))
        other = next(
            p for p in store.repo_state_directory.glob("bases/*/*.json") if p != first
        )
        other.write_bytes(original)
        with pytest.raises(SyncBaseRecordCorruptionError):
            store.read(b"main:app.other")
        first.write_bytes(original.replace(b'"shape":"file"', b'"shape":"missing"'))
        with pytest.raises(SyncBaseRecordCorruptionError):
            store.read(b"main:app.unit")


@pytest.mark.parametrize(
    "field,value,reason",
    [
        ("content", "YmFk", "payload_corrupt"),
        ("content", "YmFkIQ==", "payload_corrupt"),
        ("content", "!invalid-base64!", "payload_corrupt"),
        ("content", None, "payload_corrupt"),
        ("shape", "missing", "record_corrupt"),
    ],
)
def test_payload_corruption_is_distinct_from_record_corruption(
    tmp_path, field, value, reason
):
    with SyncBaseStore.open(tmp_path / "manager", "repo") as store:
        store.replace(record())
        path = store.record_path(b"main:app.unit")
        encoded = json.loads(path.read_bytes())
        encoded["record"][field] = value
        path.write_text(json.dumps(encoded, sort_keys=True, separators=(",", ":")))
        before = path.read_bytes()
        with pytest.raises(SyncBaseRecordCorruptionError) as error:
            store.read(b"main:app.unit")
        assert error.value.reason == reason
        assert error.value.affected_identities == (b"main:app.unit",)
        assert store.scan().corrupt_count == 1
        assert path.read_bytes() == before
        assert store.delete(b"main:app.unit")


def test_delete_contract(tmp_path):
    with SyncBaseStore.open(tmp_path / "manager", "repo") as store:
        assert not store.delete(b"main:app.unit")
        store.replace(record())
        assert store.delete(b"main:app.unit")
        assert not store.delete(b"main:app.unit")
        assert store.identities() == ()


def test_failed_file_flush_preserves_previous_record(tmp_path, monkeypatch):
    with SyncBaseStore.open(tmp_path / "manager", "repo") as store:
        store.replace(record())

        def fail(descriptor):
            raise OSError("injected flush failure")

        monkeypatch.setattr(os, "fsync", fail)
        with pytest.raises(SyncBaseStoreError):
            store.replace(record(payload=Missing()))
        assert store.read(b"main:app.unit") == record()
        assert not list(store.repo_state_directory.rglob("*.tmp"))


@pytest.mark.parametrize("has_previous", [False, True])
def test_post_commit_flush_failure_reports_committed_but_uncertain_durability(
    tmp_path, monkeypatch, has_previous
):
    root = tmp_path / "manager"
    with SyncBaseStore.open(root, "repo") as store:
        if has_previous:
            store.replace(record())
        original_fsync, original_replace = os.fsync, os.replace
        renamed = False

        def rename(*args, **kwargs):
            nonlocal renamed
            original_replace(*args, **kwargs)
            renamed = True

        # Creating a target group flushes directories before the commit too.
        def fail_directory_flush(descriptor):
            if renamed and stat.S_ISDIR(os.fstat(descriptor).st_mode):
                raise OSError("injected post-rename directory flush failure")
            original_fsync(descriptor)

        with monkeypatch.context() as injected:
            injected.setattr(os, "replace", rename)
            injected.setattr(os, "fsync", fail_directory_flush)
            with pytest.raises(SyncBaseStoreDurabilityError) as error:
                store.replace(record(payload=Missing()))
        assert error.value.committed is True
        assert error.value.durability_uncertain is True
        assert store.read(b"main:app.unit") == record(payload=Missing())
        store.replace(record(b"main:app.other"))
        assert store.read(b"main:app.other") == record(b"main:app.other")
    with SyncBaseStore.open(root, "repo", read_only=True) as store:
        assert store.read(b"main:app.unit") == record(payload=Missing())


def test_rejects_hardlinked_record(tmp_path):
    with SyncBaseStore.open(tmp_path / "manager", "repo") as store:
        store.replace(record())
        os.link(record_path(store), tmp_path / "alias")
        with pytest.raises(SyncBaseStoreSecurityError):
            store.read(b"main:app.unit")
        with pytest.raises(SyncBaseStoreSecurityError):
            store.delete(b"main:app.unit")


def test_missing_lock_is_not_recreated_over_existing_records(tmp_path):
    from dotman.sync_base_store import LOCK_FILE_NAME

    root = tmp_path / "manager"
    with SyncBaseStore.open(root, "repo") as store:
        store.replace(record())
        directory = store.repo_state_directory
    (directory / LOCK_FILE_NAME).unlink()
    with pytest.raises(SyncBaseStoreSecurityError):
        SyncBaseStore.open(root, "repo")
    assert not (directory / LOCK_FILE_NAME).exists()


def test_pinned_directory_substitution_is_rejected(tmp_path):
    with SyncBaseStore.open(tmp_path / "manager", "repo") as store:
        store.replace(record())
        directory = store.repo_state_directory
        directory.rename(directory.with_name("moved"))
        directory.mkdir(mode=0o700)
        with pytest.raises(SyncBaseStoreSecurityError):
            store.read(b"main:app.unit")
        with pytest.raises(SyncBaseStoreSecurityError):
            store.replace(record())
        assert list(directory.iterdir()) == []


def test_failed_unit_replacement_does_not_prevent_another_unit(tmp_path, monkeypatch):
    with SyncBaseStore.open(tmp_path / "manager", "repo") as store:
        store.replace(record())
        original = os.replace

        def fail_once(*args, **kwargs):
            monkeypatch.setattr(os, "replace", original)
            raise OSError("injected per-unit failure")

        monkeypatch.setattr(os, "replace", fail_once)
        with pytest.raises(SyncBaseStoreError):
            store.replace(record(payload=Missing()))
        store.replace(record(b"main:app.other"))
        assert store.read(b"main:app.unit") == record()
        assert store.read(b"main:app.other") == record(b"main:app.other")


def test_target_records_return_only_that_targets_units(tmp_path):
    with SyncBaseStore.open(tmp_path, "main") as store:
        for identity in (b"main:app.tree/a", b"main:app.tree/nested/b", b"main:app.tree2/a", b"main:app.file"):
            store.replace(record(identity))
        assert [item.identity for item in store.target_records(b"main:app.tree")] == [
            b"main:app.tree/a", b"main:app.tree/nested/b",
        ]
        assert [item.identity for item in store.target_records(b"main:app.file")] == [b"main:app.file"]
        assert store.target_records(b"main:app.absent") == ()


def test_records_are_grouped_by_target(tmp_path):
    with SyncBaseStore.open(tmp_path, "main") as store:
        for identity in (b"main:app<work.v2>.tree/a/b", b"main:app<work.v2>.tree/c", b"main:group/app.file"):
            store.replace(record(identity))
        first, second, other = (store.record_path(identity).parent for identity in (
            b"main:app<work.v2>.tree/a/b", b"main:app<work.v2>.tree/c", b"main:group/app.file"))
        assert first == second != other
        assert other.parent == first.parent == store.repo_state_directory / "bases"


def test_deleting_a_targets_last_record_removes_its_group(tmp_path):
    with SyncBaseStore.open(tmp_path, "main") as store:
        store.replace(record(b"main:app.tree/a"))
        store.replace(record(b"main:app.tree/b"))
        group = store.record_path(b"main:app.tree/a").parent
        assert store.delete(b"main:app.tree/a")
        assert group.is_dir()
        assert store.delete(b"main:app.tree/b")
        assert not group.exists()
        assert store.target_records(b"main:app.tree") == ()


def _tree(root):
    return {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}


@pytest.mark.parametrize("read_only", [False, True])
@pytest.mark.parametrize("layout", [b"1\n", b"two"])
def test_unsupported_store_layout_fails_every_open_without_changes(tmp_path, layout, read_only):
    from dotman import sync_base_store

    root = tmp_path / "manager"
    with SyncBaseStore.open(root, "repo") as store:
        store.replace(record())
        marker = store.repo_state_directory / sync_base_store.LAYOUT_FILE_NAME
    marker.write_bytes(layout)
    before = _tree(root)

    with pytest.raises(SyncBaseStoreError, match="layout"):
        SyncBaseStore.open(root, "repo", read_only=read_only)

    assert _tree(root) == before


def test_unversioned_store_is_absent_until_creation_drops_its_records(tmp_path):
    from dotman import sync_base_store

    root = tmp_path / "manager"
    with SyncBaseStore.open(root, "repo") as store:
        store.replace(record(b"main:app.stale"))
        directory = store.repo_state_directory
    (directory / sync_base_store.LAYOUT_FILE_NAME).unlink()
    unrelated = directory / "tracked-packages.toml"
    unrelated.write_bytes(b"kept")
    before = _tree(root)

    assert not SyncBaseStore.exists(root, "repo")
    for options in ({"read_only": True}, {"create": False}):
        with pytest.raises(SyncBaseStoreError, match="layout"):
            SyncBaseStore.open(root, "repo", **options)
    assert _tree(root) == before

    with SyncBaseStore.open(root, "repo") as store:
        assert store.identities() == ()
        store.replace(record())
    assert SyncBaseStore.exists(root, "repo")
    assert unrelated.read_bytes() == b"kept"
    with SyncBaseStore.open(root, "repo", read_only=True) as store:
        assert store.identities() == (b"main:app.unit",)


def test_marker_only_store_completes_on_next_writable_open(tmp_path):
    from dotman import sync_base_store

    root = tmp_path / "manager"
    with SyncBaseStore.open(root, "repo") as store:
        directory = store.repo_state_directory
    # Holds no records, so recreating its lock cannot adopt untrusted data.
    (directory / sync_base_store.LOCK_FILE_NAME).unlink()

    with SyncBaseStore.open(root, "repo") as store:
        store.replace(record())
        assert store.read(b"main:app.unit") == record()
