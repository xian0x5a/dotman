"""`dotman reset sync-base --unusable [repo]` drops what `dotman doctor` counts as corrupt or orphaned.

JSON output of the flag: {"status": "reset", "dropped": <int total records removed>}.
"""
import json

import pytest

from dotman.cli import main
from dotman.engine import DotmanEngine
from dotman.sync_base_store import FilePresent, SyncBaseRecord, SyncBaseStore
from tests.cli.test_sync_base_inspection import call, fixture_engine, store_record
from tests.helpers import write_named_manager_config

USABLE = "main:app.unit"
LEGACY_NON_CANONICAL = "main:app.unit\nlegacy"  # control character: _identity() rejects it
ORPHAN_UNTRACKED_TARGET = "main:app.gone"


def stored_identities(engine, repo="main"):
    """Raw identities present in the store, including ones inspection would reject."""
    with SyncBaseStore.open(engine._tracked_state_context.state_root, repo, read_only=True) as store:
        return {identity.decode() for identity in store.identities()}


def doctor_counts(engine):
    checks = {check.key: check.count for check in engine.doctor().checks if check.key.startswith("sync_bases_")}
    return checks["sync_bases_corrupt"], checks["sync_bases_orphaned"]


def with_second_repo(engine, tmp_path):
    """Add a second, empty repo `extra` so every record stored under it is orphaned."""
    from dotman.command_runtime import ArgvCommand, CommandRequest
    from tests.helpers import current_command_runtime

    extra = tmp_path / "extra"
    (extra / "profiles").mkdir(parents=True)
    (extra / "profiles" / "default.toml").write_text("")
    (extra / "packages").mkdir()
    (extra / "packages" / ".gitkeep").write_text("")
    runtime = current_command_runtime()
    for args in [("init", "-q"), ("add", "."), ("-c", "user.name=Test", "-c", "user.email=t@example.test", "commit", "-qm", "x")]:
        assert runtime.run(CommandRequest(ArgvCommand(("git", *args)), cwd=extra)).exit_code == 0
    config_path = write_named_manager_config(tmp_path, {"main": tmp_path / "repo", "extra": extra})
    return DotmanEngine.from_config_path(config_path)


def test_unusable_drops_legacy_non_canonical_record_and_keeps_usable(tmp_path, monkeypatch, capsys):
    engine = fixture_engine(tmp_path, monkeypatch)
    store_record(engine, identity=USABLE)
    store_record(engine, identity=LEGACY_NON_CANONICAL)
    assert doctor_counts(engine) == (1, 0)

    code, result = call(engine, capsys, "reset", "sync-base", "--unusable")

    assert code == 0
    assert result["status"] == "reset" and result["dropped"] == 1
    assert doctor_counts(engine) == (0, 0)
    assert stored_identities(engine) == {USABLE}
    assert engine.info_sync_base(USABLE)["status"] == "usable"


def test_unusable_drops_orphaned_untracked_target_and_vanished_directory_child(tmp_path, monkeypatch, capsys):
    from dotman.sync_base_store import Missing
    from tests.engine.test_sync_directory_observation import directory_engine

    engine = directory_engine(tmp_path, monkeypatch)
    store_record(engine, identity=ORPHAN_UNTRACKED_TARGET)
    store_record(engine, identity="main:app.tree/gone", payload=Missing())
    assert doctor_counts(engine) == (0, 2)

    code, result = call(engine, capsys, "reset", "sync-base", "--unusable", "main")

    assert code == 0
    assert result["dropped"] == 2
    assert doctor_counts(engine) == (0, 0)
    assert stored_identities(engine) == set()


def test_unusable_leaves_a_store_of_only_usable_bases_untouched(tmp_path, monkeypatch, capsys):
    engine = fixture_engine(tmp_path, monkeypatch)
    database = store_record(engine, identity=USABLE)
    before = database.read_bytes()

    code, result = call(engine, capsys, "reset", "sync-base", "--unusable")

    assert code == 0
    assert result["dropped"] == 0
    assert database.read_bytes() == before


def test_unusable_repo_argument_limits_scope_and_no_argument_covers_all_repos(tmp_path, monkeypatch, capsys):
    engine = with_second_repo(fixture_engine(tmp_path, monkeypatch), tmp_path)
    state_root = engine._tracked_state_context.state_root
    for repo in ("main", "extra"):
        with SyncBaseStore.open(state_root, repo) as store:
            store.replace(SyncBaseRecord(f"{repo}:app.gone".encode(), FilePresent(b"orphan")))

    code, result = call(engine, capsys, "reset", "sync-base", "--unusable", "extra")
    assert code == 0 and result["dropped"] == 1
    assert stored_identities(engine, "extra") == set()
    assert stored_identities(engine, "main") == {"main:app.gone"}

    code, result = call(engine, capsys, "reset", "sync-base", "--unusable")
    assert code == 0 and result["dropped"] == 1
    assert stored_identities(engine, "main") == set()


def test_unusable_takes_the_manager_lock(tmp_path, monkeypatch):
    from dotman.operation_lock import OperationBusy, OperationLock

    engine = fixture_engine(tmp_path, monkeypatch)
    store_record(engine, identity=ORPHAN_UNTRACKED_TARGET)
    with OperationLock.acquire(engine._tracked_state_context.state_root):
        # The busy error may surface as an exception or as a nonzero exit; either way nothing is removed.
        try:
            code = main(["--config", str(engine.config.config_path), "reset", "sync-base", "--unusable"])
        except OperationBusy:
            code = None
        assert code != 0
    assert stored_identities(engine) == {ORPHAN_UNTRACKED_TARGET}


def test_identity_together_with_unusable_is_a_usage_error_and_changes_nothing(tmp_path, monkeypatch):
    # Guards against the identity silently winning (or --unusable silently running) when both are given.
    engine = fixture_engine(tmp_path, monkeypatch)
    store_record(engine, identity=USABLE)
    store_record(engine, identity=ORPHAN_UNTRACKED_TARGET)
    with pytest.raises(SystemExit) as caught:
        main(["--config", str(engine.config.config_path), "reset", "sync-base", USABLE, "--unusable"])
    assert caught.value.code == 2
    assert stored_identities(engine) == {USABLE, ORPHAN_UNTRACKED_TARGET}


def test_unusable_drops_record_files_that_fail_validation(tmp_path, monkeypatch, capsys):
    # A corrupt file has no recoverable identity, so only the file location can name it.
    engine = fixture_engine(tmp_path, monkeypatch)
    store_record(engine, identity=USABLE)
    store_record(engine, identity=ORPHAN_UNTRACKED_TARGET).write_bytes(b"not a record")
    assert doctor_counts(engine) == (1, 0)

    code, result = call(engine, capsys, "reset", "sync-base", "--unusable")

    assert code == 0 and result["dropped"] == 1
    assert doctor_counts(engine) == (0, 0)
    assert stored_identities(engine) == {USABLE}


def test_unusable_human_output_reports_only_the_count(tmp_path, monkeypatch, capsys):
    engine = fixture_engine(tmp_path, monkeypatch)
    store_record(engine, identity=LEGACY_NON_CANONICAL)

    code = main(["--config", str(engine.config.config_path), "reset", "sync-base", "--unusable"])

    assert code == 0
    assert capsys.readouterr().out == "1 unusable Sync Base reset\n"
