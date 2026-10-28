import subprocess
from contextlib import contextmanager

import pytest

from dotman.sync_base_store import DirectoryChildPresent, Missing
from dotman.sync_session import SetApproval, SetResolutionIntent
from tests.engine.test_sync_convergence import command
from tests.engine.test_sync_directory_observation import directory_engine, open_directory, put


def commit(tmp_path):
    subprocess.run(["git", "add", "."], cwd=tmp_path / "repo", check=True)
    subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.test",
                    "commit", "-qm", "children"], cwd=tmp_path / "repo", check=True)


@pytest.mark.parametrize("policy,intent,expected_repo,expected_live", [
    ("push-only", "use-repository", b"repo", b"repo"),
    ("pull-only", "use-live", b"live", b"live"),
    ("both", "use-repository", b"repo", b"repo"),
    ("both", "use-live", b"live", b"live"),
    ("push-only-delete", "use-repository", b"repo", None),
])
def test_child_policy_outcomes_are_independently_approved(
    tmp_path, monkeypatch, policy, intent, expected_repo, expected_live,
):
    engine = directory_engine(tmp_path, monkeypatch, policy=policy)
    repo, live = tmp_path / "repo/packages/app/tree", tmp_path / "live/tree"
    for name in ("nested/a", "b"):
        put(repo, name, b"repo")
        put(live, name, b"live")
    commit(tmp_path)
    with open_directory(engine, preview=False) as session:
        identity = "main:app.tree/nested/a"
        assert all(not row.approved for row in session.view.rows)
        command(session, SetResolutionIntent, identity, intent)
        command(session, SetApproval, identity, True)
        selected = next(row for row in session.view.rows if row.row_id == identity)
        assert selected.approved
        result = session.execute().result
        assert {unit.identity: unit.status for unit in result.units} == {
            "main:app.tree/b": "pending", identity: "converged",
        }
        assert all(step.scope_identity == identity for step in result.steps)
    assert (repo / "nested/a").read_bytes() == expected_repo
    assert ((live / "nested/a").read_bytes() if (live / "nested/a").exists() else None) == expected_live
    assert (repo / "b").read_bytes() == b"repo"
    assert (live / "b").read_bytes() == b"live"


@pytest.mark.parametrize("base_exec,repo_exec,live_exec,expected_exec", [
    (False, False, True, True), (False, True, False, True),
    (True, False, True, False), (True, True, False, False),
    (False, True, True, True), (True, False, False, False),
])
def test_merge_reconciles_child_bytes_and_executable_independently(
    tmp_path, monkeypatch, base_exec, repo_exec, live_exec, expected_exec,
):
    engine = directory_engine(tmp_path, monkeypatch)
    repo = put(tmp_path / "repo/packages/app/tree", "child", b"first\nmiddle\nlast\n")
    live = put(tmp_path / "live/tree", "child", repo.read_bytes())
    repo.chmod(0o755 if base_exec else 0o644)
    live.chmod(repo.stat().st_mode)
    commit(tmp_path)
    with open_directory(engine, preview=False):
        pass
    repo.write_bytes(b"repository\nmiddle\nlast\n")
    live.write_bytes(b"first\nmiddle\nlive\n")
    repo.chmod(0o755 if repo_exec else 0o644)
    live.chmod(0o700 if live_exec else 0o600)
    with open_directory(engine, preview=False) as session:
        row, = session.view.rows
        assert row.intent == "merge"
        command(session, SetApproval, row.row_id, True)
        row, = session.view.rows
        assert row.proposal.repository == DirectoryChildPresent(b"repository\nmiddle\nlive\n", expected_exec)
        assert row.proposal.capture.executable == live_exec
        assert session.execute().result.units[0].status == "converged"
    assert bool(repo.stat().st_mode & 0o111) == expected_exec
    assert bool(live.stat().st_mode & 0o111) == expected_exec
    assert live.stat().st_mode & 0o666 == 0o600
    with open_directory(engine) as session:
        assert session.view.observations[0].base.record.payload == DirectoryChildPresent(
            b"repository\nmiddle\nlive\n", expected_exec,
        )


@pytest.mark.parametrize("policy", ["both", "pull-only"])
def test_exact_chmod_is_live_policy_not_child_repository_or_base(tmp_path, monkeypatch, policy):
    engine = directory_engine(tmp_path, monkeypatch, policy=policy, extra='''
[targets.tree.path_rules.mode]
pattern = "*"
chmod = "0700"
''')
    repo = put(tmp_path / "repo/packages/app/tree", "child", b"repo")
    live = put(tmp_path / "live/tree", "child", b"live")
    repo.chmod(0o644); live.chmod(0o600)
    commit(tmp_path)
    with open_directory(engine, preview=False) as session:
        row, = session.view.rows
        if policy == "both":
            command(session, SetResolutionIntent, row.row_id, "use-live")
        command(session, SetApproval, row.row_id, True)
        row, = session.view.rows
        assert row.proposal.repository == DirectoryChildPresent(b"live", False)
        assert [effect.kind for effect in row.proposal.publication_effects] == (["chmod"] if policy == "both" else [])
        assert session.execute().result.units[0].status == "converged"
    assert repo.stat().st_mode & 0o111 == 0
    assert live.stat().st_mode & 0o777 == (0o700 if policy == "both" else 0o600)
    with open_directory(engine) as session:
        assert session.view.observations[0].base.record.payload == DirectoryChildPresent(b"live", False)


def test_rename_has_independent_missing_and_present_proposals_and_ancestry(tmp_path, monkeypatch):
    engine = directory_engine(tmp_path, monkeypatch)
    repo, live = tmp_path / "repo/packages/app/tree", tmp_path / "live/tree"
    put(repo, "old"); put(live, "old")
    commit(tmp_path)
    with open_directory(engine, preview=False):
        pass
    (live / "old").rename(live / "new")
    with open_directory(engine, preview=False) as session:
        new, old = session.view.rows
        assert new.observation.base.status == "unavailable"
        assert old.observation.base.status == "usable"
        assert new.intent == "use-live" and old.intent == "merge"
        command(session, SetApproval, old.row_id, True)
        new, old = session.view.rows
        assert old.proposal.repository == Missing() and old.approved and not new.approved
        assert old.proposal.publication_effects == ()
        result = session.execute().result
        assert [unit.status for unit in result.units] == ["pending", "converged"]
    assert not (repo / "old").exists() and not (repo / "new").exists()
    assert (live / "new").exists()


@pytest.mark.parametrize("intent,source_present,expected_present", [
    ("use-repository", True, True), ("use-repository", False, False),
    ("use-live", True, False), ("use-live", False, True),
])
def test_missing_child_endpoints_remain_typed_through_frozen_execution(
    tmp_path, monkeypatch, intent, source_present, expected_present,
):
    engine = directory_engine(tmp_path, monkeypatch)
    repo, live = tmp_path / "repo/packages/app/tree", tmp_path / "live/tree"
    put(repo if source_present else live, "nested/child").chmod(0o700)
    commit(tmp_path)
    with open_directory(engine, preview=False) as session:
        row, = session.view.rows
        command(session, SetResolutionIntent, row.row_id, intent)
        command(session, SetApproval, row.row_id, True)
        row, = session.view.rows
        expected = DirectoryChildPresent(b"bytes", True) if expected_present else Missing()
        assert row.proposal.repository == expected
        assert row.proposal.live == expected
        result = session.execute().result
        assert result.units[0].status == "converged"
    for root in (repo, live):
        assert (root / "nested/child").exists() == expected_present
        if expected_present:
            assert bool((root / "nested/child").stat().st_mode & 0o111)


def test_children_share_canonical_additional_without_sharing_primary_approval(tmp_path, monkeypatch):
    import json
    from dotman.sync_session import EditProposal, AdditionalRow

    editor = 'printf candidate > "$DOTMAN_EDITOR_ADDITIONAL_SOURCE_PATHS"; chmod +x "$DOTMAN_SOURCE"'
    engine = directory_engine(tmp_path, monkeypatch, policy="push-only", extra=f'''
[targets.tree.path_rules.shared]
pattern = "*"
render = "cat $DOTMAN_PACKAGE_ROOT/shared"
editor = {{ run = {json.dumps(editor)}, io = "pipe", additional_sources = ["shared"] }}
''')
    repo, live = tmp_path / "repo/packages/app/tree", tmp_path / "live/tree"
    for name in ("a", "b"):
        put(repo, name, b"repo").chmod(0o644); put(live, name, b"live")
    shared = put(tmp_path / "repo/packages/app", "shared", b"original")
    with open_directory(engine, preview=False) as session:
        a, b = session.view.rows
        command(session, EditProposal, a.row_id)
        a, b, additional = session.view.rows
        assert isinstance(additional, AdditionalRow)
        assert additional.references == (a.row_id, b.row_id)
        assert additional.path.as_posix() == "packages/app/shared"
        command(session, SetApproval, a.row_id, True)
        assert session.view.rows[0].proposal.live.content == b"original"
        command(session, SetApproval, additional.row_id, True)
        assert session.view.rows[0].proposal.live.content == b"candidate"
        command(session, SetApproval, additional.row_id, False)
        assert session.view.rows[0].approved
        assert session.view.rows[0].proposal.live.content == b"original"
        command(session, SetApproval, additional.row_id, True)
        result = session.execute().result
        assert [unit.status for unit in result.units] == ["converged", "pending"]
        assert [item.status for item in result.additional_changes] == ["applied"]
        assert [step.kind for step in result.steps].count("additional-source") == 1
    assert shared.read_bytes() == b"candidate"
    assert (repo / "a").read_bytes() == b"repo" and (repo / "a").stat().st_mode & 0o111
    assert (repo / "b").stat().st_mode & 0o111 == 0
    assert (live / "a").read_bytes() == b"candidate"
    assert (live / "b").read_bytes() == b"live"


def test_child_primary_cannot_be_another_child_additional_source(tmp_path, monkeypatch):
    from dotman.sync_session import EditProposal

    engine = directory_engine(tmp_path, monkeypatch, extra='''
[targets.tree.path_rules.editor]
pattern = "a"
editor = { run = "true", io = "pipe", additional_sources = ["tree/b"] }
''')
    for root in (tmp_path / "repo/packages/app/tree", tmp_path / "live/tree"):
        put(root, "a", str(root).encode())
        put(root, "b", str(root).encode())
    with open_directory(engine) as session:
        row = session.view.rows[0]
        command(session, EditProposal, row.row_id)
        row = session.view.rows[0]
        assert not row.approved
        assert "Primary Source" in row.diagnostics[0].message
        assert len(session.view.rows) == 2


def test_push_exact_mode_comparison_uses_live_policy_not_repository_exec(tmp_path, monkeypatch):
    engine = directory_engine(tmp_path, monkeypatch, policy="push-only", extra='''
[targets.tree.path_rules.mode]
pattern = "*"
chmod = "0600"
''')
    put(tmp_path / "repo/packages/app/tree", "child").chmod(0o755)
    put(tmp_path / "live/tree", "child").chmod(0o600)
    with open_directory(engine) as session:
        assert session.view.observations[0].state == "directly-in-sync"
        assert session.view.rows == ()


def test_child_stages_share_one_target_hook_scope_and_publish_frozen_effects(tmp_path, monkeypatch):
    log = tmp_path / "hooks"
    engine = directory_engine(tmp_path, monkeypatch, extra=f'''
[targets.tree.hooks]
pre_pull = "echo pre-pull >> {log}"
post_pull = "echo post-pull >> {log}"
pre_push = "echo pre-push >> {log}"
post_push = "echo post-push >> {log}"
[targets.tree.path_rules.rendered]
pattern = "*"
render = "tr a-z A-Z < $DOTMAN_SOURCE"
compare = {{ repo = "raw", live = "raw" }}
''')
    repo, live = tmp_path / "repo/packages/app/tree", tmp_path / "live/tree"
    for name in ("a", "b"):
        put(repo, name, b"repo"); put(live, name, b"live")
    with open_directory(engine, preview=False) as session:
        for row in session.view.rows:
            command(session, SetResolutionIntent, row.row_id, "use-live")
            command(session, SetApproval, row.row_id, True)
        for name in ("a", "b"):
            (repo / name).write_bytes(b"external")
            (live / name).write_bytes(b"external")
        result = session.execute().result
        assert [unit.status for unit in result.units] == ["converged", "converged"]
        assert [step.stage for step in result.steps] == ["repository-apply"] * 4 + ["live-publication"] * 4
    assert log.read_text().splitlines() == ["pre-pull", "post-pull", "pre-push", "post-push"]
    for name in ("a", "b"):
        assert (repo / name).read_bytes() == b"live"
        assert (live / name).read_bytes() == b"LIVE"


def test_later_child_chmod_failure_preserves_earlier_convergence_and_base(tmp_path, monkeypatch):
    from dotman import file_access

    engine = directory_engine(tmp_path, monkeypatch)
    repo, live = tmp_path / "repo/packages/app/tree", tmp_path / "live/tree"
    for name in ("a", "b"):
        put(repo, name, b"bytes").chmod(0o644); put(live, name, b"bytes").chmod(0o600)
    commit(tmp_path)
    with open_directory(engine, preview=False):
        pass
    for name in ("a", "b"):
        (repo / name).chmod(0o755)
    chmod = file_access.chmod
    def fail_second(path, mode):
        if path == live / "b":
            raise OSError("second child mode failed")
        return chmod(path, mode)
    monkeypatch.setattr(file_access, "chmod", fail_second)
    with open_directory(engine, preview=False) as session:
        before = [unit.base.record for unit in session.view.observations]
        for row in session.view.rows:
            command(session, SetApproval, row.row_id, True)
        assert all([effect.kind for effect in row.proposal.publication_effects] == ["chmod"]
                   for row in session.view.rows)
        result = session.execute().result
        assert [unit.status for unit in result.units] == ["converged", "execution-failed"]
        assert result.steps[-1].scope_identity == "main:app.tree/b"
        failed, = [step for step in result.steps if step.status == "failed"]
        assert failed.scope_identity == "main:app.tree/b" and failed.kind == "chmod"
    with open_directory(engine) as session:
        after = [unit.base.record for unit in session.view.observations]
        assert after[0].payload == DirectoryChildPresent(b"bytes", True)
        assert before[0].payload == DirectoryChildPresent(b"bytes", False)
        assert after[1] == before[1]


def test_child_no_write_resolution_still_requires_approval_and_acknowledgment(tmp_path, monkeypatch):
    from dotman.sync_session import PrepareProposalReview

    engine = directory_engine(tmp_path, monkeypatch, extra='''
[targets.tree.path_rules.projected]
pattern = "*"
compare = { repo = "raw", live = "raw" }
capture = "printf repo"
render = "printf live"
''')
    put(tmp_path / "repo/packages/app/tree", "child", b"repo")
    put(tmp_path / "live/tree", "child", b"live")
    commit(tmp_path)
    with open_directory(engine, preview=False) as session:
        row, = session.view.rows
        command(session, PrepareProposalReview, row.row_id)
        row, = session.view.rows
        assert row.observation.state == "drifted" and not row.approved
        assert row.proposal.primary_source_change is None
        assert row.proposal.publication_effects == ()
        command(session, SetApproval, row.row_id, True)
        result = session.execute().result
        assert result.units[0].status == "converged"
        assert result.steps == ()
    with open_directory(engine) as session:
        assert session.view.observations[0].base.record.payload == DirectoryChildPresent(b"repo", False)


def test_child_merge_conflict_clears_only_affected_standing_approval(tmp_path, monkeypatch):
    engine = directory_engine(tmp_path, monkeypatch)
    repo, live = tmp_path / "repo/packages/app/tree", tmp_path / "live/tree"
    for name in ("a", "b"):
        put(repo, name, b"base"); put(live, name, b"base")
    commit(tmp_path)
    with open_directory(engine, preview=False):
        pass
    for name in ("a", "b"):
        (repo / name).write_bytes(b"repository")
        (live / name).write_bytes(b"conflict")
    with open_directory(engine) as session:
        for row in session.view.rows:
            command(session, SetResolutionIntent, row.row_id, "use-repository")
            command(session, SetApproval, row.row_id, True)
        command(session, SetResolutionIntent, session.view.rows[0].row_id, "merge")
        a, b = session.view.rows
        assert a.intent == "merge" and not a.approved and a.proposal is None
        assert a.diagnostics[0].code == "reconciliation-conflict"
        # Child conflict evidence keeps the child shape so the Editor restores its mode.
        assert isinstance(a.diagnostics[0].conflict, DirectoryChildPresent)
        assert a.diagnostics[0].conflict.content.startswith(b"<<<<<<< repository\n")
        assert b.approved and b.proposal is not None


@pytest.mark.parametrize("repo_exec,live_exec", [(True, False), (False, True)])
def test_child_add_add_since_missing_base_keeps_repository_executable(tmp_path, monkeypatch, repo_exec, live_exec):
    engine = directory_engine(tmp_path, monkeypatch)
    repo = put(tmp_path / "repo/packages/app/tree", "child", b"old\n")
    live = put(tmp_path / "live/tree", "child", b"old\n")
    commit(tmp_path)
    with open_directory(engine, preview=False):
        pass
    live.unlink()
    with open_directory(engine, preview=False) as session:
        command(session, SetApproval, "main:app.tree/child", True)
        assert session.execute().result.units[0].status == "converged"
    assert not repo.exists()
    # The child's Base now records Missing; both sides add the same bytes with different executable state.
    put(repo.parent, "child", b"new\n").chmod(0o755 if repo_exec else 0o644)
    put(live.parent, "child", b"new\n").chmod(0o755 if live_exec else 0o644)
    with open_directory(engine, preview=False) as session:
        row, = session.view.rows
        assert row.intent == "merge"
        command(session, SetApproval, row.row_id, True)
        row, = session.view.rows
        assert row.diagnostics == ()
        assert row.proposal.repository == DirectoryChildPresent(b"new\n", repo_exec)


@contextmanager
def changed_children_session(root, monkeypatch, count):
    """A session over `count` children whose repo side changed since their stored Bases, all approved."""
    engine = directory_engine(root, monkeypatch)
    repo, live = root / "repo/packages/app/tree", root / "live/tree"
    names = [f"child{index:02}" for index in range(count)]
    for name in names:
        put(repo, name, b"old"); put(live, name, b"old")
    commit(root)
    with open_directory(engine, preview=False):
        pass
    for name in names:
        put(repo, name, b"new")
    commit(root)
    with open_directory(engine, preview=False) as session:
        for row in session.view.rows:
            command(session, SetApproval, row.row_id, True)
        yield engine, session


def test_execution_validates_each_store_once_whatever_the_child_count(tmp_path, monkeypatch):
    from dotman.sync_base_store import _PrivateLayout

    validations = []
    validate_tree = _PrivateLayout.validate_tree
    monkeypatch.setattr(_PrivateLayout, "validate_tree", lambda self: (validations.append(self), validate_tree(self))[1])
    counts = {}
    for count in (2, 20):
        root = tmp_path / str(count)
        with changed_children_session(root, monkeypatch, count) as (engine, session):
            validations.clear()
            result = session.execute().result
            counts[count] = len(validations)
            assert [unit.status for unit in result.units] == ["converged"] * count
        # Every child checkpointed its new Base, so the acknowledgments really ran.
        with open_directory(engine, preview=False) as session:
            assert {obs.base.record.payload for obs in session.view.observations} == {DirectoryChildPresent(b"new", False)}
    # One writable store (one repo) per execution, not one per acknowledged child.
    assert counts == {2: 1, 20: 1}


def test_interrupt_mid_execution_closes_the_opened_store(tmp_path, monkeypatch):
    from dotman.sync_base_lifecycle import SyncBaseLifecycle
    from dotman.sync_base_store import SyncBaseStore

    opened, closed = [], []
    open_store, close_store, complete = SyncBaseStore.open, SyncBaseStore.close, SyncBaseLifecycle.complete
    monkeypatch.setattr(SyncBaseStore, "open", classmethod(lambda cls, *a, **kw: opened.append(open_store(*a, **kw)) or opened[-1]))
    monkeypatch.setattr(SyncBaseStore, "close", lambda self: (closed.append(self), close_store(self))[1])
    calls = []

    def interrupt_second_completion(self, *args, **kwargs):
        calls.append(args)
        if len(calls) == 2:
            raise KeyboardInterrupt
        return complete(self, *args, **kwargs)

    with changed_children_session(tmp_path, monkeypatch, 3) as (_engine, session):
        opened.clear()
        monkeypatch.setattr(SyncBaseLifecycle, "complete", interrupt_second_completion)
        # The apply loop reports the interrupt as an aborted result rather than propagating it.
        assert session.execute().result.status == "aborted"
    assert len(calls) == 2 and opened
    assert all(any(store is done for done in closed) for store in opened)
