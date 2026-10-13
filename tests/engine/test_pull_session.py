from __future__ import annotations

import pytest

from dotman.sync_session import CommandRejected, SetResolutionIntent
from tests.engine.test_sync_session import make_engine


@pytest.mark.parametrize("source,live", [(b"repo", b"live"), (None, b"new"), (b"old", None)])
def test_pull_fixed_opt_out_frozen_repository_effect(tmp_path, monkeypatch, source, live):
    engine = make_engine(tmp_path, monkeypatch, [("unit", "both", source, live, "")])
    with engine.open_pull_session(engine.resolve_sync_scope()) as session:
        row = session.view.rows[0]
        assert row.approved
        assert row.intent is None
        assert row.allowed_intents == ()
        assert "set-resolution-intent" not in row.allowed_commands
        view = session.view
        assert isinstance(session.dispatch(SetResolutionIntent(
            view.session_id, view.revision, row.row_id, "merge")), CommandRejected)
        live_path = tmp_path / "live/unit"
        live_path.parent.mkdir(exist_ok=True)
        live_path.write_bytes(b"external")
        result = session.execute().result
        assert result.status == "completed"
        assert result.units[0].status == "applied"
        source_path = tmp_path / "repo/packages/app/unit"
        assert (source_path.read_bytes() if source_path.exists() else None) == live
        assert live_path.read_bytes() == b"external"


def test_pull_ignores_push_only_and_push_guard(tmp_path, monkeypatch):
    engine = make_engine(tmp_path, monkeypatch, [
        ("unit", "both", b"repo", b"live", '[targets.unit.hooks]\nguard_push = "exit 9"'),
        ("push", "push-only", b"repo", b"live", ""),
    ])
    with engine.open_pull_session(engine.resolve_sync_scope()) as session:
        assert [unit.identity.target_name for unit in session.view.observations] == ["unit"]
        assert session.execute().result.status == "completed"
    assert (tmp_path / "repo/packages/app/push").read_bytes() == b"repo"


def test_pull_interactive_subset_after_materialization_failure(tmp_path, monkeypatch):
    engine = make_engine(tmp_path, monkeypatch, [
        ("bad", "both", b"repo", b"live", 'capture = "exit 9"\n[targets.bad.compare]\nlive = "raw"'),
        ("good", "both", b"repo", b"live", ""),
    ])
    with engine.open_pull_session(engine.resolve_sync_scope()) as session:
        assert [row.approved for row in session.view.rows] == [False, True]
        result = session.execute().result
        assert result.status == "failed"
        assert (tmp_path / "repo/packages/app/good").read_bytes() == b"live"
        assert (tmp_path / "repo/packages/app/bad").read_bytes() == b"repo"


def test_pull_guard_omits_work_before_observation(tmp_path, monkeypatch):
    engine = make_engine(tmp_path, monkeypatch, [
        ("unit", "both", b"repo", b"live",
         'capture = "exit 9"\n[targets.unit.hooks]\nguard_pull = "exit 100"'),
    ])
    with engine.open_pull_session(engine.resolve_sync_scope()) as session:
        assert session.view.observations == ()
        assert session.execute().result.status == "completed"


def test_pull_direct_agreement_and_changed_pull_advance_checkpoint(tmp_path, monkeypatch):
    from dotman.sync_base_store import SyncBaseStore
    from dotman.sync_scope import sync_unit_identity_bytes
    engine = make_engine(tmp_path, monkeypatch, [("unit", "both", b"repo", b"repo", "")])
    with engine.open_pull_session(engine.resolve_sync_scope()) as session:
        assert session.view.observations[0].base.acknowledged
        identity = sync_unit_identity_bytes(session.view.observations[0].identity)
    state = tmp_path / "state/dotman"
    with SyncBaseStore.open(state, "main", read_only=True) as store:
        before = store.read(identity)
    (tmp_path / "live/unit").write_bytes(b"changed")
    with engine.open_pull_session(engine.resolve_sync_scope()) as session:
        assert session.view.observations[0].base.record == before
        result = session.execute().result
        assert result.status == "completed"
        assert result.units[0].acknowledged
    with SyncBaseStore.open(state, "main", read_only=True) as store:
        from dotman.sync_base_store import FilePresent
        assert store.read(identity).payload == FilePresent(b"changed")

def test_pull_additional_approval_rebuilds_frozen_capture_and_applies_independently(tmp_path, monkeypatch):
    from dotman.sync_session import AdditionalRow, EditProposal, SetApproval
    from dotman.sync_base_store import FilePresent
    editor = 'editor = { run = "printf candidate > \\"$DOTMAN_EDITOR_ADDITIONAL_SOURCE_PATHS\\"", io = "pipe", additional_sources = ["shared"] }'
    engine = make_engine(tmp_path, monkeypatch, [
        ("a", "both", b"repo", b"live", editor),
        ("b", "both", b"repo", b"live",
         'capture = "cat $DOTMAN_PACKAGE_ROOT/shared"\ncompare = {repo = "raw", live = "raw"}\n'
         'editor = {run = "true", io = "pipe", additional_sources = ["shared"]}'),
    ])
    shared = tmp_path / "repo/packages/app/shared"
    shared.write_bytes(b"original")
    with engine.open_pull_session(engine.resolve_sync_scope()) as session:
        def send(kind, row_id, *args):
            return session.dispatch(kind(session.view.session_id, session.view.revision, row_id, *args))
        a, b = session.view.rows
        assert b.proposal.repository == FilePresent(b"original")
        send(EditProposal, a.row_id)
        a, b, additional = session.view.rows
        assert isinstance(additional, AdditionalRow) and additional.approved
        assert b.proposal.repository == FilePresent(b"candidate")
        shared.write_bytes(b"external")
        send(SetApproval, additional.row_id, False)
        assert session.view.rows[1].proposal.repository == FilePresent(b"original")
        send(SetApproval, b.row_id, False)
        send(SetApproval, additional.row_id, True)
        assert session.view.rows[1].proposal is None
        send(SetApproval, a.row_id, False)
        assert session.execute().result.status == "completed"
        assert shared.read_bytes() == b"candidate"
        assert (tmp_path / "repo/packages/app/a").read_bytes() == b"repo"
        assert (tmp_path / "repo/packages/app/b").read_bytes() == b"repo"

def test_pull_patch_capture_uses_frozen_views(tmp_path, monkeypatch):
    from dotman.sync_base_store import FilePresent
    engine = make_engine(tmp_path, monkeypatch, [
        ("unit", "both", b"template\nkeep\nold\n", b"rendered\nkeep\nnew\n",
         'render = "sed s/template/rendered/ $DOTMAN_SOURCE"\ncapture = "patch"\ncompare = {repo = "render", live = "raw"}'),
    ])
    with engine.open_pull_session(engine.resolve_sync_scope()) as session:
        assert session.view.rows[0].proposal.repository == FilePresent(b"template\nkeep\nnew\n")
        (tmp_path / "live/unit").write_bytes(b"external")
        assert session.execute().result.units[0].status == "applied"
    assert (tmp_path / "repo/packages/app/unit").read_bytes() == b"template\nkeep\nnew\n"


def test_pull_editor_recovers_capture_failure_without_automatic_fallback(tmp_path, monkeypatch):
    from dotman.sync_session import EditProposal, SetApproval
    engine = make_engine(tmp_path, monkeypatch, [
        ("unit", "both", b"repo", b"live",
         'capture = "false"\ncompare = {repo = "raw", live = "raw"}\n'
         'editor = {run = "printf edited > \\"$DOTMAN_SOURCE\\"", io = "pipe"}'),
    ])
    with engine.open_pull_session(engine.resolve_sync_scope()) as session:
        row = session.view.rows[0]
        assert row.diagnostics and not row.approved and row.proposal is None
        def send(kind, *args):
            return session.dispatch(kind(session.view.session_id, session.view.revision, row.row_id, *args))
        assert send(EditProposal).result.status == "saved"
        assert not session.view.rows[0].approved
        send(SetApproval, True)
        assert session.execute().result.units[0].status == "applied"
    assert (tmp_path / "repo/packages/app/unit").read_bytes() == b"edited"
    assert (tmp_path / "live/unit").read_bytes() == b"live"


def test_pull_post_hook_failure_keeps_completed_write_and_skips_later_units(tmp_path, monkeypatch):
    engine = make_engine(tmp_path, monkeypatch, [
        ("a", "both", b"repo", b"live", '[targets.a.hooks]\npost_pull = "exit 9"\npre_push = "exit 8"'),
        ("b", "both", b"repo", b"live", ""),
    ])
    with engine.open_pull_session(engine.resolve_sync_scope()) as session:
        result = session.execute().result
        assert result.status == "failed"
        assert [unit.status for unit in result.units] == ["applied", "skipped"]
        assert all(step.stage == "repository-apply" for step in result.steps)
    assert (tmp_path / "repo/packages/app/a").read_bytes() == b"live"
    assert (tmp_path / "repo/packages/app/b").read_bytes() == b"repo"
    assert not (tmp_path / "state/dotman/snapshots").exists()


@pytest.mark.parametrize("comparison", ["raw", "capture"])
def test_pull_capture_is_reused_across_reviews_and_execution(tmp_path, monkeypatch, comparison):
    from dotman.sync_session import PrepareProposalReview, SetApproval
    from dotman.sync_base_store import FilePresent
    from dotman.command_runtime import CommandResult
    engine = make_engine(tmp_path, monkeypatch, [
        ("unit", "both", b"repo", b"live",
         f'capture = "capture-frozen"\ncompare = {{repo = "raw", live = "{comparison}"}}'),
    ])
    runtime = engine._planning_context.projection.command_runtime
    original = runtime.run
    captures = []

    def run(request):
        if getattr(request.command, "source", None) == "capture-frozen":
            captures.append(request)
            return CommandResult(exit_code=0, stdout=b"captured")
        return original(request)

    monkeypatch.setattr(runtime, "run", run)
    with engine.open_pull_session(engine.resolve_sync_scope()) as session:
        row, = session.view.rows
        assert row.proposal.repository == FilePresent(b"captured")
        for kind, args in [(PrepareProposalReview, ()), (SetApproval, (False,)),
                           (SetApproval, (True,)), (PrepareProposalReview, ())]:
            dispatched = session.dispatch(kind(session.view.session_id, session.view.revision, row.row_id, *args))
            if kind is PrepareProposalReview:
                assert dispatched.result.proposal.repository == FilePresent(b"captured")
        assert len(captures) == 1
        assert session.execute().result.units[0].status == "applied"
        assert len(captures) == 1
    assert (tmp_path / "repo/packages/app/unit").read_bytes() == b"captured"


@pytest.mark.parametrize("probe", [False, True])
@pytest.mark.parametrize("include", [False, True])
def test_pull_auxiliary_work_starts_selected_and_can_be_excluded(tmp_path, monkeypatch, probe, include):
    from dotman.sync_session import AuxiliaryRow, SetIncluded
    engine = make_engine(tmp_path, monkeypatch, [("unit", "both", b"same", b"same", "")])
    marker = tmp_path / "hook"
    manifest = tmp_path / "repo/packages/app/package.toml"
    if probe:
        manifest.write_text(
            'id = "app"\n[targets.probe]\nprobe = "true"\n'
            f'[targets.probe.hooks]\npre_pull = "touch {marker}"\npre_push = "exit 9"\n'
        )
    else:
        manifest.write_text(manifest.read_text() + f'\n[hooks]\npre_pull = "touch {marker}"\npre_push = "exit 9"\n')
    from dotman.engine import DotmanEngine
    engine = DotmanEngine.from_config_path(tmp_path / "config.toml")
    with engine.open_pull_session(engine.resolve_sync_scope(), run_noop=not probe) as session:
        row, = session.view.rows
        assert isinstance(row, AuxiliaryRow) and row.included
        assert row.directions == ("pull",)
        session.dispatch(SetIncluded(session.view.session_id, session.view.revision, row.row_id, include))
        assert session.execute().result.status == "completed"
    assert marker.exists() == include


@pytest.mark.parametrize("unselect_first", [False, True], ids=["approved", "after-batch-unselect"])
def test_pull_editor_starts_from_captured_live_content(tmp_path, monkeypatch, unselect_first):
    """The Pull Editor edits the incoming content even after the row lost its Proposal."""
    from dotman.sync_session import BatchSetApproval, EditProposal, SetApproval
    engine = make_engine(tmp_path, monkeypatch, [
        ("unit", "pull-only", b"old repo\n", b"new live\n",
         'editor = { run = "printf \'tweak\\\\n\' >> \\"$DOTMAN_SOURCE\\"", io = "pipe" }'),
    ])
    with engine.open_pull_session(engine.resolve_sync_scope(), preview=False) as session:
        def view():
            return session.view
        if unselect_first:
            session.dispatch(BatchSetApproval(
                view().session_id, view().revision, False, tuple(row.row_id for row in view().rows)))
        session.dispatch(EditProposal(view().session_id, view().revision, view().rows[0].row_id))
        if not view().rows[0].approved:
            session.dispatch(SetApproval(view().session_id, view().revision, view().rows[0].row_id, True))
        assert session.execute().result.status == "completed"
    assert (tmp_path / "repo/packages/app/unit").read_bytes() == b"new live\ntweak\n"
