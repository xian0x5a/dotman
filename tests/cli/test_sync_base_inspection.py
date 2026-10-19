import json


import pytest

from dotman.cli import main
from dotman.operation_lock import OperationLock, OperationBusy
from dotman.sync_base_store import LAYOUT_FILE_NAME, SyncBaseRecord, SyncBaseStore, DirectoryChildPresent, FilePresent, Missing
from tests.engine.test_sync_session import make_engine


def fixture_engine(tmp_path, monkeypatch, policy="both", extra=""):
    return make_engine(tmp_path, monkeypatch, [("unit", policy, b"secret-base-payload", b"live", extra)])


def store_record(engine, *, payload=None, identity="main:app.unit"):
    with SyncBaseStore.open(engine._tracked_state_context.state_root, "main") as store:
        store.replace(SyncBaseRecord(identity=identity.encode(), payload=payload if payload is not None else FilePresent(b"secret-base-payload")))
        return store.record_path(identity.encode())


def call(engine, capsys, *args):
    code = main(["--config", str(engine.config.config_path), "--json", *args])
    output = capsys.readouterr()
    return code, json.loads(output.out) if output.out else output.err


@pytest.mark.parametrize("payload", [Missing(), FilePresent(b"secret-base-payload")])
def test_usable_details_list_and_reset_are_metadata_only(tmp_path, monkeypatch, capsys, payload):
    engine = fixture_engine(tmp_path, monkeypatch)
    database = store_record(engine, payload=payload)
    before = database.read_bytes()
    code, info = call(engine, capsys, "info", "sync-base", "main:app.unit")
    assert code == 0
    assert info["status"] == "usable"
    assert info["reason"] is None
    assert info["policy"] == "both" and info["eligibility"] is True
    assert "commit" not in info and "provenance" not in info
    assert info["checks"] == {"integrity": True}
    assert all(info["checks"].values())
    assert "secret-base-payload" not in json.dumps(info)
    assert database.read_bytes() == before
    code, listing = call(engine, capsys, "list", "sync-bases")
    assert code == 0
    assert listing["sync_bases"] == [{key: value for key, value in info.items() if key != "operation"}]
    assert call(engine, capsys, "reset", "sync-base", "main:app.unit")[1]["status"] == "reset"
    assert call(engine, capsys, "reset", "sync-base", "main:app.unit")[1]["status"] == "already_absent"
    assert engine.info_sync_base("main:app.unit")["reason"] == "absent"
    with SyncBaseStore.open(engine._tracked_state_context.state_root, "main", read_only=True) as store:
        assert store.identities() == ()


@pytest.mark.parametrize("policy,status,reason", [("both", "unavailable", "absent"), ("push-only", "not-applicable", "ineligible"), ("push-only-delete", "not-applicable", "ineligible")])
def test_absent_and_ineligible_succeed_without_creating_store(tmp_path, monkeypatch, policy, status, reason):
    engine = fixture_engine(tmp_path, monkeypatch, policy=policy)
    state = engine._tracked_state_context.state_root
    before = sorted(str(path.relative_to(state)) for path in state.rglob("*"))
    info = engine.info_sync_base("main:app.unit")
    assert (info["status"], info["reason"]) == (status, reason)
    assert info["payload"] is None
    assert engine.list_sync_bases() == []
    assert sorted(str(path.relative_to(state)) for path in state.rglob("*")) == before


@pytest.mark.parametrize("field,value,reason", [
    ("shape", "wrong", "record_corrupt"),
    ("content", "YmFk", "payload_corrupt"),
])
def test_corruption_is_distinguished_without_cleanup_and_doctor_is_aggregate(tmp_path, monkeypatch, field, value, reason):
    engine = fixture_engine(tmp_path, monkeypatch)
    database = store_record(engine)
    encoded = json.loads(database.read_bytes())
    encoded["record"][field] = value
    database.write_text(json.dumps(encoded, sort_keys=True, separators=(",", ":")))
    before = database.read_bytes()
    assert engine.info_sync_base("main:app.unit")["reason"] == reason
    assert engine.list_sync_bases() == []
    checks = [check for check in engine.doctor().checks if check.key.startswith("sync_bases")]
    warning = next(check for check in checks if check.key == "sync_bases_corrupt")
    assert warning.status == "warn" and warning.detail == "1 corrupt Sync Bases"
    assert warning.to_dict()["count"] == 1
    assert "main:app.unit" not in json.dumps([check.to_dict() for check in checks])
    assert database.read_bytes() == before


def test_corrupt_records_do_not_hide_healthy_inspection_and_doctor_counts_all(tmp_path, monkeypatch):
    engine = make_engine(tmp_path, monkeypatch, [
        (name, 'both', b'repo', b'live', '') for name in ('one', 'two', 'three')
    ])
    paths = [store_record(engine, identity=f'main:app.{name}') for name in ('one', 'two', 'three')]
    paths[0].write_bytes(b'not JSON')
    encoded = json.loads(paths[1].read_bytes())
    encoded['record']['content'] = 'YmFk'
    paths[1].write_text(json.dumps(encoded, sort_keys=True, separators=(',', ':')))
    before = [path.read_bytes() for path in paths]
    assert engine.info_sync_base('main:app.one')['reason'] == 'record_corrupt'
    assert engine.info_sync_base('main:app.two')['reason'] == 'payload_corrupt'
    assert engine.info_sync_base('main:app.three')['status'] == 'usable'
    assert [entry['identity'] for entry in engine.list_sync_bases()] == ['main:app.three']
    corrupt = next(check for check in engine.doctor().checks if check.key == 'sync_bases_corrupt')
    assert corrupt.count == 2
    assert corrupt.status == 'warn'
    assert [path.read_bytes() for path in paths] == before
    assert engine.reset_sync_base('main:app.one')['status'] == 'reset'
    assert engine.info_sync_base('main:app.three')['status'] == 'usable'


@pytest.mark.parametrize("selector", ["unit", "app.unit", "main:app", "main:", "*", "main:app.*", "main:app.unit/../bad", "main:app.unit/child"])
def test_exact_selectors_only(tmp_path, monkeypatch, selector):
    engine = fixture_engine(tmp_path, monkeypatch)
    for method in (engine.info_sync_base, engine.reset_sync_base):
        with pytest.raises(ValueError):
            method(selector)


def test_reset_lock_and_readers_do_not_take_manager_lock(tmp_path, monkeypatch):
    engine = fixture_engine(tmp_path, monkeypatch)
    store_record(engine)
    with OperationLock.acquire(engine._tracked_state_context.state_root):
        assert engine.info_sync_base("main:app.unit")["status"] == "usable"
        assert len(engine.list_sync_bases()) == 1
        engine.doctor()
        with pytest.raises(OperationBusy):
            engine.reset_sync_base("main:app.unit")


def test_inspection_never_executes_guards_projections_observation_or_verification(tmp_path, monkeypatch):
    engine = fixture_engine(tmp_path, monkeypatch, extra='render = "exit 9"\ncapture = "exit 9"\n[targets.unit.hooks]\nguard_push = "exit 9"\nguard_pull = "exit 9"')
    store_record(engine)
    def forbidden(*args, **kwargs):
        pytest.fail("inspection must not execute payload work")
    monkeypatch.setattr("dotman.sync_observation._observe_file", forbidden)
    assert engine.info_sync_base("main:app.unit")["status"] == "usable"
    assert len(engine.list_sync_bases()) == 1


def test_orphan_record_not_listed_but_counted(tmp_path, monkeypatch):
    engine = fixture_engine(tmp_path, monkeypatch)
    with SyncBaseStore.open(engine._tracked_state_context.state_root, "main") as store:
        store.replace(SyncBaseRecord(b"main:app.gone", FilePresent(b"orphan")))
    assert engine.list_sync_bases() == []
    check = next(check for check in engine.doctor().checks if check.key == "sync_bases_orphaned")
    assert check.detail == "1 orphaned Sync Bases" and check.status == "warn"


def test_history_changed_does_not_invalidate_checkpoint(tmp_path, monkeypatch):
    from dotman.command_runtime import ArgvCommand, CommandRequest
    engine = fixture_engine(tmp_path, monkeypatch)
    store_record(engine)
    runtime = engine._planning_context.command_runtime
    repo = tmp_path / "repo"
    for arguments in [
        ("checkout", "--orphan", "unrelated"),
        ("-c", "user.name=Test", "-c", "user.email=test@example.test", "commit", "-qm", "new history"),
    ]:
        result = runtime.run(CommandRequest(ArgvCommand(("git", *arguments)), cwd=repo))
        assert result.exit_code == 0
    info = engine.info_sync_base("main:app.unit")
    assert info["status"] == "usable"
    assert info["reason"] is None


def test_directory_child_metadata_policy_and_exact_reset(tmp_path, monkeypatch):
    from dotman.sync_base_store import DirectoryChildPresent
    from tests.engine.test_sync_directory_observation import directory_engine
    engine = directory_engine(tmp_path, monkeypatch, extra='[targets.tree.path_rules.only]\npattern = "*.private"\nsync_policy = "push-only"')
    # Missing children remain exact identities; no payload census is required.
    store_record(engine, identity="main:app.tree/nested/file", payload=DirectoryChildPresent(b"private", True))
    info = engine.info_sync_base("main:app.tree/nested/file")
    assert info["status"] == "usable" and info["payload"]["executable"] is True
    assert engine.info_sync_base("main:app.tree/secret.private")["reason"] == "ineligible"
    for method in (engine.info_sync_base, engine.reset_sync_base):
        with pytest.raises(ValueError):
            method("main:app.tree")
    assert engine.reset_sync_base("main:app.tree/nested/file")["status"] == "reset"


def test_store_failure_is_cli_error_and_doctor_failure_without_repair(tmp_path, monkeypatch, capsys):
    engine = fixture_engine(tmp_path, monkeypatch)
    database = store_record(engine)
    database.chmod(0o644)
    before = database.read_bytes()
    code, error = call(engine, capsys, "info", "sync-base", "main:app.unit")
    assert code == 1
    assert str(database) in error and "main" in error
    check = next(check for check in engine.doctor().checks if check.key == "sync_bases_store")
    assert check.status == "failed" and check.path == database.parents[2] and check.repo_name == "main"
    assert database.stat().st_mode & 0o777 == 0o644
    assert database.read_bytes() == before



def test_doctor_warns_that_the_next_real_run_replaces_an_unversioned_store(tmp_path, monkeypatch):
    engine = fixture_engine(tmp_path, monkeypatch)
    directory = store_record(engine).parents[2]
    (directory / LAYOUT_FILE_NAME).unlink()
    check = next(check for check in engine.doctor().checks if check.key == "sync_bases_store")
    assert check.status == "warn" and check.path == directory and check.repo_name == "main"
    assert "unversioned" in check.detail

@pytest.mark.parametrize("reason,expected", [
    ("record_corrupt", "corrupt"), ("payload_corrupt", "corrupt"),
    ("absent", "absent"), ("ineligible", "ineligible"),
])
def test_human_reasons_and_styling(reason, expected, capsys):
    from dotman.cli_emit import emit_sync_base
    emit_sync_base(detail={"identity": "main:app.unit", "status": "unavailable", "reason": reason},
                   operation="info-sync-base", json_output=False, use_color=True)
    output = capsys.readouterr().out
    assert f"Reason: {expected}" in output
    assert "\x1b[" in output


def test_reset_rejects_extra_arguments_and_preview(tmp_path, monkeypatch):
    engine = fixture_engine(tmp_path, monkeypatch)
    for arguments in [
        ["reset", "sync-base"],
        ["reset", "sync-base", "main:app.unit", "main:app.unit"],
        ["reset", "sync-base", "main:app.unit", "--dry-run"],
    ]:
        with pytest.raises(SystemExit) as caught:
            main(["--config", str(engine.config.config_path), *arguments])
        assert caught.value.code == 2


@pytest.mark.parametrize("extra,expected", [
    ("", 1),
    ('[targets.tree.ignore]\npatterns = ["private/"]', 0),
    ('[targets.tree.hooks]\nguard_push = "exit 9"', 0),
    ('[targets.tree.path_rules.guarded]\npattern = "*"\n[targets.tree.path_rules.guarded.hooks]\nguard_pull = "exit 9"', 0),
])
def test_doctor_child_orphans_require_complete_unrestricted_census(tmp_path, monkeypatch, extra, expected):
    from tests.engine.test_sync_directory_observation import directory_engine
    engine = directory_engine(tmp_path, monkeypatch, extra=extra)
    database = store_record(engine, identity="main:app.tree/gone", payload=Missing())
    before = database.read_bytes()
    check = next(check for check in engine.doctor().checks if check.key == "sync_bases_orphaned")
    assert check.detail == f"{expected} orphaned Sync Bases"
    assert database.read_bytes() == before
    assert engine.info_sync_base("main:app.tree/gone")["status"] == "usable"


def test_child_base_stays_listed_when_its_path_rules_break_patch_capture(tmp_path, monkeypatch):
    from tests.engine.test_sync_directory_observation import directory_engine, put
    engine = directory_engine(tmp_path, monkeypatch, extra='[targets.tree.path_rules.docs]\npattern = "*.md"\ncapture = "patch"')
    put(tmp_path / "repo/packages/app/tree", "a.md")
    put(tmp_path / "live/tree", "a.md")
    store_record(engine, identity="main:app.tree/a.md", payload=DirectoryChildPresent(b"bytes", False))
    # Only planning rejects the broken rule; it is no evidence the Base is orphaned.
    check = next(check for check in engine.doctor().checks if check.key == "sync_bases_orphaned")
    assert check.count == 0
    assert [(entry["identity"], entry["status"]) for entry in engine.list_sync_bases()] == [("main:app.tree/a.md", "usable")]


def test_instance_identity_remains_canonical(tmp_path, monkeypatch):
    from dotman.engine import DotmanEngine
    from tests.helpers import write_tracked_packages_state
    engine = fixture_engine(tmp_path, monkeypatch)
    manifest = tmp_path / "repo/packages/app/package.toml"
    manifest.write_text('binding_mode = "multi_instance"\n' + manifest.read_text())
    (tmp_path / "repo/profiles/work.v2.toml").write_text("")
    write_tracked_packages_state(tmp_path / "state", repo_name="main", entries=[("app", "work.v2")])
    engine = DotmanEngine.from_config_path(engine.config.config_path)
    identity = "main:app<work.v2>.unit"
    store_record(engine, identity=identity)
    assert engine.info_sync_base(identity)["identity"] == identity
    assert engine.list_sync_bases()[0]["identity"] == identity
    with pytest.raises(ValueError):
        engine.reset_sync_base("main:app.unit")
    assert engine.reset_sync_base(identity)["status"] == "reset"


def test_absent_record_does_not_require_git_history(tmp_path, monkeypatch):
    engine = fixture_engine(tmp_path, monkeypatch)
    with SyncBaseStore.open(engine._tracked_state_context.state_root, "main"):
        pass
    assert engine.info_sync_base("main:app.unit")["reason"] == "absent"


@pytest.mark.parametrize("scope", ["repo", "package"])
def test_doctor_child_orphan_proof_excludes_ancestor_guards(tmp_path, monkeypatch, scope):
    from dotman.engine import DotmanEngine
    from tests.engine.test_sync_directory_observation import directory_engine
    engine = directory_engine(tmp_path, monkeypatch)
    path = tmp_path / ("repo/repo.toml" if scope == "repo" else "repo/packages/app/package.toml")
    path.write_text(path.read_text() + '\n[hooks]\nguard_pull = "exit 9"\n')
    engine = DotmanEngine.from_config_path(engine.config.config_path)
    store_record(engine, identity="main:app.tree/gone", payload=Missing())
    check = next(check for check in engine.doctor().checks if check.key == "sync_bases_orphaned")
    assert check.count == 0


def test_reset_rejects_missing_lock_without_repair(tmp_path, monkeypatch):
    from dotman.sync_base_store import SyncBaseStoreError, LOCK_FILE_NAME

    engine = fixture_engine(tmp_path, monkeypatch)
    database = store_record(engine)
    (database.parents[2] / LOCK_FILE_NAME).unlink()
    def artifacts():
        return {
            entry.name: (entry.stat().st_ino, entry.stat().st_mode, entry.read_bytes())
            for entry in database.parent.iterdir() if entry.is_file()
        }
    before = artifacts()
    with pytest.raises(SyncBaseStoreError):
        engine.reset_sync_base("main:app.unit")
    assert artifacts() == before


@pytest.mark.parametrize("child", [False, True])
def test_doctor_counts_record_shape_identity_mismatch_without_git(
    tmp_path, monkeypatch, child
):
    from tests.engine.test_sync_directory_observation import directory_engine
    from dotman.sync_base_store import DirectoryChildPresent

    engine = directory_engine(tmp_path, monkeypatch) if child else fixture_engine(tmp_path, monkeypatch)
    identity = "main:app.tree/nested/file" if child else "main:app.unit"
    payload = FilePresent(b"secret") if child else DirectoryChildPresent(b"secret", True)
    database = store_record(engine, identity=identity, payload=payload)
    before = database.read_bytes()
    assert engine.info_sync_base(identity)["reason"] == "record_corrupt"

    (tmp_path / "repo/.git").rename(tmp_path / "git-metadata")
    checks = [check for check in engine.doctor().checks if check.key.startswith("sync_bases")]
    corrupt = next(check for check in checks if check.key == "sync_bases_corrupt")
    assert corrupt.count == 1
    assert corrupt.status == "warn"
    assert identity not in json.dumps([check.to_dict() for check in checks])
    assert database.read_bytes() == before


@pytest.mark.parametrize("json_output", [True, False], ids=["json", "human"])
def test_store_read_failure_after_open_is_cli_error(tmp_path, monkeypatch, capsys, json_output):
    from dotman.sync_base_store import SyncBaseStoreError
    engine = fixture_engine(tmp_path, monkeypatch)
    store_record(engine)

    def failing_read(self, identity):
        # Stands in for an I/O failure on a store that opened successfully.
        raise SyncBaseStoreError("simulated read failure")

    monkeypatch.setattr(SyncBaseStore, "read", failing_read)
    mode = ["--json"] if json_output else []
    code = main(["--config", str(engine.config.config_path), *mode, "info", "sync-base", "main:app.unit"])
    output = capsys.readouterr()
    assert code != 0
    assert "simulated read failure" in output.err
    assert "Traceback" not in output.err


def test_child_names_with_glob_characters_are_ordinary_exact_identities(tmp_path, monkeypatch):
    # Selectors never glob, so these characters are literal parts of a name.
    from tests.engine.test_sync_directory_observation import directory_engine, put
    engine = directory_engine(tmp_path, monkeypatch)
    names = ("a[1].txt", "q?.txt", "star*.txt")
    for name in names:
        put(tmp_path / "repo/packages/app/tree", name); put(tmp_path / "live/tree", name)
        store_record(engine, identity=f"main:app.tree/{name}", payload=DirectoryChildPresent(b"bytes", False))
    assert [entry["identity"] for entry in engine.list_sync_bases()] == [f"main:app.tree/{name}" for name in names]
    checks = {check.key: check.detail for check in engine.doctor().checks}
    assert checks["sync_bases_corrupt"] == "0 corrupt Sync Bases"
    assert checks["sync_bases_orphaned"] == "0 orphaned Sync Bases"
    assert engine.info_sync_base("main:app.tree/q?.txt")["status"] == "usable"
    assert engine.reset_sync_base("main:app.tree/star*.txt")["status"] == "reset"
