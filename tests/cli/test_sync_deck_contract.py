import re
from dataclasses import replace
from types import SimpleNamespace

from dotman.cli_style import render_sync_term
from dotman.sync_base_store import LAYOUT_FILE_NAME, FilePresent, SyncBaseRecord, SyncBaseStore
from dotman.sync_deck import CommandDeck, row_resolution
from dotman.sync_deck_command import sync_document
from dotman.sync_session import AuxiliaryRow, SetApproval, SyncSession
from tests.engine.test_sync_convergence import command
from tests.engine.test_sync_session import make_engine


def test_directory_root_is_auxiliary_selection_not_proposal_approval():
    root = AuxiliaryRow("root:r:p.tree", "directory-root", False, "r:p.tree", ("push",))
    with SyncSession((), preview=True, auxiliary=(root,)) as session:
        deck = CommandDeck(session, use_color=False)
        assert row_resolution(root) == "Directory Root Work"
        assert render_sync_term("Directory Root Work", use_color=True) == render_sync_term(
            "Probe Work", use_color=True).replace("Probe Work", "Directory Root Work")
        deck.select_all(True)
        assert session.view.rows[0].included
        deck.open_review()
        assert not deck.reviewing
        document = sync_document(SimpleNamespace(dry_run=True, no_confirm=False, scopes=[]), session, None)
        assert document["directory_root_work"] == [{
            "identity": "r:p.tree", "selected": True, "directions": ["push"], "diagnostics": [],
        }]
        assert document["sync_units"] == []
        assert "approved: 0" in deck.confirmation_text()
        deck.select_all(False)
        assert not session.view.rows[0].included


def test_review_keeps_base_only_as_facts(tmp_path, monkeypatch):
    engine = make_engine(tmp_path, monkeypatch, [("unit", "both", b"repo", b"live", "")])
    with engine.open_sync_session(engine.resolve_sync_scope([]), preview=True) as opened:
        original = opened.view.rows[0]
        record = SyncBaseRecord(b"unit", FilePresent(b"ancestor"))
        observation = replace(original.observation,
                              base=replace(original.observation.base, status="usable", record=record))
        row = replace(original, observation=observation, intent="use-repository")
        session = SimpleNamespace(view=replace(opened.view, rows=(row,)))
        text = CommandDeck(session, use_color=False).review_text()
        # Sync Base stays summarized as facts; its payload gets no diff of its own.
        assert "Base payload: " in text
        assert "ancestor" not in text
        assert "Capture: not required" in text


def test_confirmation_cancel_preserves_reviewed_selection(tmp_path, monkeypatch):
    engine = make_engine(tmp_path, monkeypatch, [("unit", "push-only", b"repo", b"live", "")])
    with engine.open_sync_session(engine.resolve_sync_scope([]), preview=True) as session:
        deck = CommandDeck(session, use_color=False)
        deck.select()
        reviewed = session.view
        deck.confirm()
        assert deck.confirming
        deck.select(False)
        deck.select_all(False)
        deck.edit()
        deck.open_review()
        assert session.view == reviewed
        assert deck.back()
        assert not deck.confirming
        assert session.view == reviewed


def test_empty_confirmation_warns_that_drift_stays_pending(tmp_path, monkeypatch):
    engine = make_engine(tmp_path, monkeypatch, [
        ("one", "push-only", b"repo", b"live", ""),
        ("two", "push-only", b"repo", b"live", ""),
    ])
    with engine.open_sync_session(engine.resolve_sync_scope([]), preview=True) as session:
        deck = CommandDeck(session, use_color=False)
        # "Nothing to do" is a legitimate decision, so an empty set still confirms.
        deck.confirm()
        assert deck.confirming
        assert "Nothing selected; 2 drifted Sync Units stay pending." in deck.confirmation_text()
        deck.back()
        deck.focus = 0
        deck.select()
        assert "Nothing selected" not in deck.confirmation_text()

def test_single_resolution_is_static_and_blocked_rows_remain_visible(tmp_path, monkeypatch):
    import asyncio
    from textual.widgets import OptionList
    from dotman.sync_deck import SyncDeckApp, WorksetTable

    engine = make_engine(tmp_path, monkeypatch, [
        ("unit", "push-only", b"repo", b"live", ""),
        ("blocked", "push-only", b"repo", b"live", '[targets.blocked.hooks]\nguard_push = "exit 100"'),
    ])
    with engine.open_sync_session(engine.resolve_sync_scope([]), preview=True) as session:
        app = SyncDeckApp(CommandDeck(session, use_color=False))

        async def interact():
            async with app.run_test() as pilot:
                table = app.query_one(WorksetTable)
                for index, row in enumerate(session.view.rows):
                    if row.kind == "guard-skip":
                        continue
                    table.move_cursor(row=index)
                    app.action_resolution()
                    assert not app.query_one("#resolution", OptionList).display
                    if not row.allowed_intents:
                        assert str(table.get_row_at(index)[0]) == "[-]"
                        assert row.observation.diagnostics or row.diagnostics
                app.deck.select_all(True)
                assert sum(getattr(row, "approved", False) for row in session.view.rows) == 1
                await pilot.pause()

        asyncio.run(asyncio.wait_for(interact(), timeout=5))


def test_confirmation_requires_valid_completed_approved_proposals(tmp_path, monkeypatch):
    from dotman.sync_observation import Diagnostic

    engine = make_engine(tmp_path, monkeypatch, [("unit", "push-only", b"repo", b"live", "")])
    with engine.open_sync_session(engine.resolve_sync_scope([]), preview=True) as session:
        deck = CommandDeck(session, use_color=False)
        deck.select()
        ready = session.view.rows[0]
        assert ready.proposal is not None
        for invalid in (
            replace(ready, proposal=None),
            replace(ready, diagnostics=(Diagnostic("failed", "Materialization failed"),)),
            replace(ready, observation=replace(ready.observation, diagnostics=(
                Diagnostic("failed", "Observation failed"),))),
        ):
            for preview in (False, True):
                view = replace(session.view, preview=preview, rows=(invalid,),
                               allowed_commands=("execute", "preview", "abort"))
                reviewed = SimpleNamespace(view=view)
                confirmation = CommandDeck(reviewed, use_color=False)
                confirmation.confirm()
                assert not confirmation.confirming
                assert reviewed.view == view
                if not preview:
                    assert "ready" in confirmation.notice
            # Unapproved work never blocks a separately reviewed executable set.
            view = replace(view, preview=False, rows=(replace(invalid, approved=False),))
            confirmation = CommandDeck(SimpleNamespace(view=view), use_color=False)
            confirmation.confirm()
            assert confirmation.confirming


def test_push_workset_names_fixed_repository_resolution(tmp_path, monkeypatch):
    engine = make_engine(tmp_path, monkeypatch, [("unit", "push-only", b"repo", b"live", "")])
    with engine.open_push_session(engine.resolve_sync_scope([]), preview=True) as session:
        row = session.view.rows[0]
        assert row_resolution(row) == "Use repository"
        CommandDeck(session, use_color=False).open_review()
        assert row_resolution(session.view.rows[0]) == "Use repository"


def test_unavailable_base_store_warns_only_base_eligible_units(tmp_path, monkeypatch):
    engine = make_engine(tmp_path, monkeypatch, [
        ("shared", "both", b"same", b"same", ""),
        ("pushed", "push-only", b"same", b"same", ""),
    ])
    store_directory = tmp_path / "state/dotman/repos/main"
    store_directory.rename(tmp_path / "relocated-store")
    # Opening fails closed on a symlinked store directory.
    store_directory.symlink_to(tmp_path / "relocated-store")
    with engine.open_push_session(engine.resolve_sync_scope([])) as session:
        rows = {row.observation.identity.target_name: row for row in session.view.rows}
        assert set(rows) == {"shared"}
        assert any(item.code == "base-unavailable" for item in rows["shared"].observation.diagnostics)
        assert row_resolution(rows["shared"]) == "In sync"


def test_unversioned_base_store_is_absent_until_a_real_run_replaces_it(tmp_path, monkeypatch):
    engine = make_engine(tmp_path, monkeypatch, [("shared", "both", b"same", b"same", "")])
    manager_root = tmp_path / "state/dotman"
    with SyncBaseStore.open(manager_root, "main") as store:
        store.replace(SyncBaseRecord(b"main:app.stale", FilePresent(b"old")))
        (store.repo_state_directory / LAYOUT_FILE_NAME).unlink()
    for preview in (True, False):
        with engine.open_sync_session(engine.resolve_sync_scope([]), preview=preview) as session:
            # No per-unit store warning resurfaces in-sync rows.
            assert session.view.rows == ()
    with SyncBaseStore.open(manager_root, "main", read_only=True) as store:
        assert store.identities() == (b"main:app.shared",)


def test_review_explains_hook_reapply_with_its_drift(tmp_path, monkeypatch):
    applied = tmp_path / "applied"
    applied.write_bytes(b"user-set\n")
    engine = make_engine(tmp_path, monkeypatch, [("unit", "both", b"reset\n", b"reset\n",
                                                  f'capture = "cat {applied}"\n[targets.unit.hooks]\npost_push = "true"')])
    with engine.open_sync_session(engine.resolve_sync_scope([]), preview=True) as session:
        command(session, SetApproval, session.view.rows[0].row_id, True)
        text = CommandDeck(session, use_color=False).review_text()
        assert re.search(r"reapply \S+/live/unit \(push-hooks\)", text)
        assert "Live file unchanged: Capture differs, so push hooks reapply live state" in text
        # The Pull View diff still explains the drift the unchanged bytes cannot show.
        assert ":: Drift" in text and "-reset" in text and "+user-set" in text
        assert "keeps appearing" not in text and "Nothing will be written" not in text


def test_live_stat_counts_each_changed_target_once_including_modes_and_reapplies(tmp_path, monkeypatch):
    applied = tmp_path / "applied"
    applied.write_bytes(b"user-set\n")
    engine = make_engine(tmp_path, monkeypatch, [
        # A created file with a configured mode has both a write and a chmod effect.
        ("created", "push-only", b"new\n", None, 'chmod = "600"'),
        ("mode", "push-only", b"same\n", b"same\n", 'chmod = "600"'),
        ("applied", "both", b"reset\n", b"reset\n",
         f'capture = "cat {applied}"\n[targets.applied.hooks]\npost_push = "true"'),
    ])
    (tmp_path / "live/mode").chmod(0o644)
    with engine.open_sync_session(engine.resolve_sync_scope([]), preview=True) as session:
        for row in session.view.rows:
            command(session, SetApproval, row.row_id, True)
        text = CommandDeck(session, use_color=False).confirmation_text()
        assert "live: 3 (1 reapplied)" in text and "modes" not in text
        summary = sync_document(SimpleNamespace(dry_run=True, no_confirm=False, scopes=[]), session, None)["summary"]
        assert (summary["live_changes"], summary["live_reapplies"]) == (3, 1)
