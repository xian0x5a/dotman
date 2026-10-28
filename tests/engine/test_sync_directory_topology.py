import pytest

from dotman.sync_session import SetApproval, SetResolutionIntent, CommandRejected
from tests.engine.test_sync_convergence import command
from tests.engine.test_sync_directory_observation import directory_engine, open_directory, put


@pytest.mark.parametrize("direction", ["push", "pull"])
def test_deletion_prunes_intermediate_parents_but_retains_root(tmp_path, monkeypatch, direction):
    engine = directory_engine(tmp_path, monkeypatch)
    repo, live = tmp_path / "repo/packages/app/tree", tmp_path / "live/tree"
    destination = live if direction == "push" else repo
    put(destination, "nested/deep/child")
    with open_directory(engine, preview=False) as session:
        row, = session.view.rows
        command(session, SetResolutionIntent, row.row_id, "use-repository" if direction == "push" else "use-live")
        command(session, SetApproval, row.row_id, True)
        assert session.execute().result.units[0].status == "converged"
    assert destination.is_dir()
    assert list(destination.iterdir()) == []


@pytest.mark.parametrize("direction", ["push", "pull"])
@pytest.mark.parametrize("ancestor", [True, False])
def test_managed_topology_requires_all_deletion_approvals(tmp_path, monkeypatch, direction, ancestor):
    engine = directory_engine(tmp_path, monkeypatch)
    repo, live = tmp_path / "repo/packages/app/tree", tmp_path / "live/tree"
    source, destination = (repo, live) if direction == "push" else (live, repo)
    writer, blocker = ("node/child", "node") if ancestor else ("node", "node/child")
    put(source, writer, b"new")
    put(destination, blocker, b"old")
    with open_directory(engine, preview=False) as session:
        assert all(unit.state == "drifted" for unit in session.view.observations)
        intent = "use-repository" if direction == "push" else "use-live"
        for row in session.view.rows:
            command(session, SetResolutionIntent, row.row_id, intent)
        command(session, SetApproval, f"main:app.tree/{writer}", True)
        assert isinstance(session.execute(), CommandRejected)
        assert (destination / blocker).read_bytes() == b"old"
        assert next(row for row in session.view.rows if row.row_id.endswith("/" + writer)).approved
        command(session, SetApproval, f"main:app.tree/{blocker}", True)
        result = session.execute().result
        assert all(unit.status == "converged" for unit in result.units), str(result)
        mutations = [step.scope_identity for step in result.steps if step.kind == "target"]
        assert mutations.index(f"main:app.tree/{blocker}") < mutations.index(f"main:app.tree/{writer}")
    assert (destination / writer).read_bytes() == b"new"


@pytest.mark.parametrize("exists", [True, False])
def test_root_mode_is_selectable_only_when_existing(tmp_path, monkeypatch, exists):
    from dotman.sync_session import SetIncluded
    engine = directory_engine(tmp_path, monkeypatch, policy="push-only")
    manifest = tmp_path / "repo/packages/app/package.toml"
    manifest.write_text(manifest.read_text().replace('type = "directory"', 'type = "directory"\nchmod = "0700"'))
    from dotman.engine import DotmanEngine
    engine = DotmanEngine.from_config_path(tmp_path / "config.toml")
    root = tmp_path / "live/tree"
    if exists:
        root.mkdir(parents=True); root.chmod(0o755)
    with open_directory(engine, preview=False) as session:
        roots = [row for row in session.view.rows if row.kind == "directory-root"]
        assert len(roots) == int(exists)
        if exists:
            assert not roots[0].included
            command(session, SetIncluded, roots[0].row_id, True)
        result = session.execute().result
        assert result.units == ()
    assert root.exists() == exists
    if exists:
        assert root.stat().st_mode & 0o777 == 0o700


def test_selected_child_creates_missing_root_with_target_mode(tmp_path, monkeypatch):
    engine = directory_engine(tmp_path, monkeypatch, policy="push-only")
    manifest = tmp_path / "repo/packages/app/package.toml"
    manifest.write_text(manifest.read_text().replace('type = "directory"', 'type = "directory"\nchmod = "0700"'))
    from dotman.engine import DotmanEngine
    engine = DotmanEngine.from_config_path(tmp_path / "config.toml")
    put(tmp_path / "repo/packages/app/tree", "nested/child")
    with open_directory(engine, preview=False) as session:
        row, = session.view.rows
        command(session, SetApproval, row.row_id, True)
        assert session.execute().result.units[0].status == "converged"
    assert (tmp_path / "live/tree").stat().st_mode & 0o777 == 0o700


@pytest.mark.parametrize("ancestor", [True, False])
@pytest.mark.parametrize("child", ["child", "deep/nested/child"])
def test_topology_snapshot_restores_file_and_directory_preimages(tmp_path, monkeypatch, ancestor, child):
    from dotman.snapshot import list_snapshots, build_restore_actions, execute_restore_action
    engine = directory_engine(tmp_path, monkeypatch, policy="push-only")
    source, destination = tmp_path / "repo/packages/app/tree", tmp_path / "live/tree"
    writer, blocker = ("node/" + child, "node") if ancestor else ("node", "node/" + child)
    put(source, writer, b"new"); put(destination, blocker, b"old")
    with open_directory(engine, preview=False) as session:
        for row in session.view.rows:
            command(session, SetApproval, row.row_id, True)
        assert session.execute().result.status == "completed"
        snapshots = list_snapshots(session._context.config.snapshots.path)
    actions = build_restore_actions(snapshots[0])
    for action in actions:
        if action.action != "noop":
            result = execute_restore_action(action)
            assert result.status == "ok", result
    assert (destination / blocker).read_bytes() == b"old"


def test_obsolete_base_requires_a_later_complete_successful_real_census(tmp_path, monkeypatch):
    from dotman.sync_base_store import SyncBaseStore
    engine = directory_engine(tmp_path, monkeypatch)
    repo, live = tmp_path / "repo/packages/app/tree", tmp_path / "live/tree"
    put(repo, "gone"); put(live, "gone")
    with open_directory(engine, preview=False) as session:
        state_root = session._context.tracked_state.state_root
        key = next(iter(session._root_inputs.values()))[0].repo.config.state_key
    def stored():
        with SyncBaseStore.open(state_root, key, read_only=True) as store:
            return store.read(b"main:app.tree/gone")
    assert stored() is not None
    (live / "gone").unlink()
    with open_directory(engine, preview=False) as session:
        row, = session.view.rows
        command(session, SetResolutionIntent, row.row_id, "use-live")
        command(session, SetApproval, row.row_id, True)
        assert session.execute().result.units[0].status == "converged"
    assert stored() is not None
    with open_directory(engine) as session:
        assert session.view.rows == ()
    assert stored() is not None
    with open_directory(engine, ["main:app.tree/gone"], preview=False) as session:
        session.execute()
    assert stored() is not None
    with open_directory(engine, preview=False) as session:
        assert stored() is not None
        session.execute()
    assert stored() is None


@pytest.mark.parametrize("blocker", ["ignored/child", "node/child"])
def test_unmanaged_blockers_are_typed_and_never_mutated(tmp_path, monkeypatch, blocker):
    engine = directory_engine(tmp_path, monkeypatch, policy="push-only", extra='[targets.tree.ignore]\npatterns = ["' + blocker + '"]')
    writer = blocker.split("/")[0]
    put(tmp_path / "repo/packages/app/tree", writer, b"new")
    blocked = put(tmp_path / "live/tree", blocker, b"private")
    with open_directory(engine, preview=False) as session:
        row, = session.view.rows
        command(session, SetApproval, row.row_id, True)
        rejected = session.execute()
        assert isinstance(rejected, CommandRejected)
        assert rejected.diagnostics[0].code == "structural-conflict"
        assert session.view.rows[0].approved
    assert blocked.read_bytes() == b"private"


def test_completed_topology_prerequisite_survives_dependent_failure(tmp_path, monkeypatch):
    from dotman import file_access
    engine = directory_engine(tmp_path, monkeypatch)
    put(tmp_path / "repo/packages/app/tree", "node", b"new")
    live = tmp_path / "live/tree"
    put(live, "node/child", b"old")
    write = file_access.write_bytes_atomic
    def fail_writer(path, content, **kwargs):
        if path == live / "node":
            raise OSError("dependent writer failed")
        return write(path, content, **kwargs)
    monkeypatch.setattr(file_access, "write_bytes_atomic", fail_writer)
    with open_directory(engine, preview=False) as session:
        for row in session.view.rows:
            command(session, SetResolutionIntent, row.row_id, "use-repository")
            command(session, SetApproval, row.row_id, True)
        result = session.execute().result
        assert {unit.identity: unit.status for unit in result.units} == {
            "main:app.tree/node": "execution-failed", "main:app.tree/node/child": "converged",
        }
    assert not (live / "node/child").exists()


def test_root_work_runs_inside_push_hooks_before_children_without_convergence(tmp_path, monkeypatch):
    from dotman.sync_session import SetIncluded
    from dotman import file_access
    log = tmp_path / "hooks"
    engine = directory_engine(tmp_path, monkeypatch, policy="push-only", extra=f"""
[targets.tree.hooks]
pre_push = "echo pre >> {log}"
post_push = "echo post >> {log}"
""")
    manifest = tmp_path / "repo/packages/app/package.toml"
    manifest.write_text(manifest.read_text().replace('type = "directory"', 'type = "directory"\nchmod = "0700"'))
    from dotman.engine import DotmanEngine
    engine = DotmanEngine.from_config_path(tmp_path / "config.toml")
    put(tmp_path / "repo/packages/app/tree", "child", b"new")
    root = tmp_path / "live/tree"
    put(root, "child", b"old"); root.chmod(0o755)
    write = file_access.write_bytes_atomic
    def check_order(path, content, **kwargs):
        if path == root / "child":
            assert log.read_text().splitlines() == ["pre"]
            assert root.stat().st_mode & 0o777 == 0o700
        return write(path, content, **kwargs)
    monkeypatch.setattr(file_access, "write_bytes_atomic", check_order)
    with open_directory(engine, preview=False) as session:
        for row in session.view.rows:
            command(session, SetIncluded if row.kind == "directory-root" else SetApproval, row.row_id, True)
        result = session.execute().result
        assert [unit.identity for unit in result.units] == ["main:app.tree/child"]
        assert result.units[0].status == "converged"
    assert log.read_text().splitlines() == ["pre", "post"]


@pytest.mark.parametrize("restriction", ["ignore", "marker", "guard", "exclusion", "failure"])
def test_restricted_or_failed_census_never_reclaims_old_child_base(tmp_path, monkeypatch, restriction):
    from dotman.sync_base_store import SyncBaseStore
    from dotman.engine import DotmanEngine
    from dotman.sync_session import SetIncluded
    engine = directory_engine(tmp_path, monkeypatch)
    repo, live = tmp_path / "repo/packages/app/tree", tmp_path / "live/tree"
    put(repo, "gone"); put(live, "gone")
    with open_directory(engine, preview=False) as session:
        state_root = session._context.tracked_state.state_root
        key = next(iter(session._root_inputs.values()))[0].repo.config.state_key
    (repo / "gone").unlink(); (live / "gone").unlink()
    manifest = tmp_path / "repo/packages/app/package.toml"
    if restriction == "ignore":
        manifest.write_text(manifest.read_text() + '\n[targets.tree.ignore]\npatterns = ["gone"]\n')
    elif restriction == "marker":
        put(live, ".dotman-skip")
    elif restriction == "guard":
        manifest.write_text(manifest.read_text() + '\n[targets.tree.hooks]\nguard_pull = "exit 100"\n')
    elif restriction == "failure":
        import os
        os.mkfifo(live / "unsupported")
    else:
        put(repo, "other", b"repo"); put(live, "other", b"live")
    engine = DotmanEngine.from_config_path(tmp_path / "config.toml")
    with open_directory(engine, preview=False) as session:
        if restriction == "exclusion":
            command(session, SetIncluded, "main:app.tree/other", False)
        session.execute()
    with SyncBaseStore.open(state_root, key, read_only=True) as store:
        assert store.read(b"main:app.tree/gone") is not None


def test_exact_writer_selection_identifies_unselected_managed_blocker(tmp_path, monkeypatch):
    engine = directory_engine(tmp_path, monkeypatch, policy="push-only")
    put(tmp_path / "repo/packages/app/tree", "node", b"new")
    put(tmp_path / "live/tree", "node/child", b"old")
    with open_directory(engine, ["main:app.tree/node"], preview=False) as session:
        command(session, SetApproval, "main:app.tree/node", True)
        result = session.execute()
        assert isinstance(result, CommandRejected)
        assert result.diagnostics[0].code == "structural-approval-required"


def test_empty_directory_nodes_are_structural_not_unmanaged_payloads(tmp_path, monkeypatch):
    engine = directory_engine(tmp_path, monkeypatch, policy="push-only")
    put(tmp_path / "repo/packages/app/tree", "node", b"new")
    root = tmp_path / "live/tree"
    (root / "node/empty/nested").mkdir(parents=True)
    with open_directory(engine, preview=False) as session:
        row, = session.view.rows
        command(session, SetApproval, row.row_id, True)
        assert session.execute().result.status == "completed"
    assert (root / "node").read_bytes() == b"new"


def test_writer_requires_every_managed_descendant_deletion(tmp_path, monkeypatch):
    engine = directory_engine(tmp_path, monkeypatch, policy="push-only")
    put(tmp_path / "repo/packages/app/tree", "node", b"new")
    root = tmp_path / "live/tree"
    put(root, "node/a", b"a"); put(root, "node/b", b"b")
    with open_directory(engine, preview=False) as session:
        command(session, SetApproval, "main:app.tree/node", True)
        command(session, SetApproval, "main:app.tree/node/a", True)
        assert isinstance(session.execute(), CommandRejected)
        assert (root / "node/a").read_bytes() == b"a"
        command(session, SetApproval, "main:app.tree/node/b", True)
        assert session.execute().result.status == "completed"
    assert (root / "node").read_bytes() == b"new"


def test_real_sync_over_a_push_only_directory_creates_no_base_store(tmp_path, monkeypatch):
    from dotman.sync_base_store import LAYOUT_FILE_NAME, LOCK_FILE_NAME
    engine = directory_engine(tmp_path, monkeypatch, policy="push-only")
    put(tmp_path / "repo/packages/app/tree", "same"); put(tmp_path / "live/tree", "same")
    with open_directory(engine, preview=False) as session:
        session.execute()
    # Push-only units never record a Base, so nothing may establish storage for them.
    stored = {path.name for path in (tmp_path / "state/dotman/repos/main").iterdir()}
    assert not {LAYOUT_FILE_NAME, LOCK_FILE_NAME} & stored


def test_directory_base_maintenance_failure_flags_only_that_directory(tmp_path, monkeypatch):
    import os
    from dotman.engine import DotmanEngine
    from dotman.sync_base_store import LOCK_FILE_NAME, SyncBaseStore
    from tests.helpers import write_named_manager_config, write_sync_repository, write_tracked_packages_state

    directory_engine(tmp_path, monkeypatch, policy="push-only")
    put(tmp_path / "repo/packages/app/tree", "same"); put(tmp_path / "live/tree", "same")
    # A second repo whose unit agrees on both sides has nothing to report.
    write_sync_repository(tmp_path / "other", [("agree", "both", b"same", b"same", "")])
    write_tracked_packages_state(tmp_path / "state", repo_name="other", entries=[("app", "default")])
    (tmp_path / "state/dotman/repos/other").chmod(0o700)
    engine = DotmanEngine.from_config_path(write_named_manager_config(
        tmp_path, {"main": tmp_path / "repo", "other": tmp_path / "other/repo"}))
    state_root = tmp_path / "state/dotman"
    with SyncBaseStore.open(state_root, "main"):
        pass
    # A hard-linked lock is unrepairable, so main's store cannot be opened.
    os.link(state_root / "repos/main" / LOCK_FILE_NAME, tmp_path / "lock-alias")
    with open_directory(engine, preview=False) as session:
        rows = {row.row_id: row for row in session.view.rows}
        assert "other:app.agree" not in rows
        other, = [unit for unit in session.view.observations if unit.identity.repo == "other"]
        assert other.diagnostics == ()
        # The failing directory still reports it, once per unit.
        child = rows["main:app.tree/same"].observation
        assert [item.code for item in child.diagnostics].count("base-maintenance-failed") == 1


def test_blocker_without_canonical_identity_is_an_unmanaged_conflict(tmp_path, monkeypatch):
    import os
    engine = directory_engine(tmp_path, monkeypatch, policy="push-only")
    put(tmp_path / "repo/packages/app/tree", "node", b"new")
    blocked = put(tmp_path / "live/tree", os.fsdecode(b"node/caf\xe9"), b"private")
    with open_directory(engine, preview=False) as session:
        row = next(row for row in session.view.rows if row.row_id == "main:app.tree/node")
        command(session, SetApproval, row.row_id, True)
        rejected = session.execute()
        assert isinstance(rejected, CommandRejected)
        diagnostic, = rejected.diagnostics
        # Its row can never be approved for deletion, and the message must stay printable.
        assert diagnostic.code == "structural-conflict"
        assert diagnostic.message == "main:app.tree/node: requires deletion of node/caf\\xe9"
    assert blocked.read_bytes() == b"private"


# Siblings such as "a-b", "a.b" and "a0" sort around the "a/" prefix run.
MIXED_LEAVES = ("", "a", "a-b", "a.b", "a/b", "a/b-c", "a/b/c", "a/b/c/d", "a/b0", "a0", "ab", "x/y/z")


@pytest.mark.parametrize("relative", sorted({*MIXED_LEAVES, "a/b/c/d/e", "x", "x/y", "a/q", "missing"} - {""}))
@pytest.mark.parametrize("repository", [True, False])
def test_blockers_match_ancestor_or_descendant_leaves(relative, repository):
    from dotman.sync_directory import DirectoryCensus

    leaves = tuple(sorted(MIXED_LEAVES))
    census = DirectoryCensus((), leaves if repository else (), () if repository else leaves)
    expected = tuple(path for path in leaves if path != relative and (
        relative.startswith(path + "/") or path.startswith(relative + "/")))
    assert census.blockers(relative, repository=repository) == expected
    assert census.blockers(relative, repository=not repository) == ()
