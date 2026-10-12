import asyncio
from types import SimpleNamespace

from dotman.sync_deck import CommandDeck, SyncDeckApp, WorksetTable
from dotman.sync_deck_command import sync_document
from dotman.sync_session import AuxiliaryRow, SyncSession


def auxiliary_session():
    return SyncSession((), preview=True, auxiliary=(
        AuxiliaryRow(row_id="probe:r:p.check", kind="probe", included=False, scope="r:p.check", directions=("push",)),
        AuxiliaryRow(row_id="hook:r:p", kind="hook", included=False, scope="r:p", directions=("pull",)),
    ))


def test_auxiliary_selection_and_review():
    with auxiliary_session() as session:
        deck = CommandDeck(session, use_color=False)
        deck.select()
        assert session.view.rows[0].included
        deck.open_review()
        assert not deck.reviewing
        assert deck.review_text() == ""
        deck.select_all(True)
        assert all(row.included for row in session.view.rows)
        assert "auxiliary: 2" in deck.confirmation_text()
        deck.select_all(False)
        assert not any(row.included for row in session.view.rows)


def test_auxiliary_json_has_only_auxiliary_metadata():
    with auxiliary_session() as session:
        CommandDeck(session, use_color=False).select_all(True)
        document = sync_document(SimpleNamespace(dry_run=True, scopes=[]), session, None)
        assert document["sync_units"] == []
        assert document["scope"] == ["r:p.check", "r:p"]
        assert document["summary"]["selected_auxiliary"] == 2
        assert document["probe_work"] == [{
            "identity": "r:p.check", "selected": True,
            "directions": ["push"], "diagnostics": [],
        }]
        assert document["hook_work"] == [{
            "identity": "r:p", "selected": True,
            "directions": ["pull"], "diagnostics": [],
        }]


def test_auxiliary_table_focus_and_actions():
    asyncio.run(asyncio.wait_for(auxiliary_table_interaction(), timeout=5))


async def auxiliary_table_interaction():
    with auxiliary_session() as session:
        app = SyncDeckApp(CommandDeck(session, use_color=False))
        async with app.run_test() as pilot:
            table = app.query_one(WorksetTable)
            assert table.row_count == 2
            assert str(table.get_row_at(0)[1]) == "r:p.check"
            assert str(table.get_row_at(1)[1]) == "r:p (pull-hooks)"
            app.action_resolution()
            await pilot.press("down", "enter")
            await pilot.pause()
            assert app.deck.focus == 1
            assert not app.deck.reviewing
            app.action_retry()


def test_no_confirm_selects_auxiliary_and_forwards_run_noop(capsys):
    from dotman.models import UiConfig
    from dotman.sync_deck_command import SyncDeckCommandRunner
    session = auxiliary_session()
    opened = []
    def open_session(scope, *, preview, run_noop, sink, **_live_options):
        opened.append((scope, preview, run_noop))
        return session
    engine = SimpleNamespace(
        config=SimpleNamespace(ui=UiConfig()),
        resolve_sync_scope=lambda scopes, **_options: scopes,
        open_sync_session=open_session,
    )
    args = SimpleNamespace(config=None, scopes=[], no_deps=False, dry_run=True,
                           no_confirm=True, json_output=False, run_noop=True)
    runner = SyncDeckCommandRunner(engine_factory=lambda _: engine, use_color=False)
    assert runner.run(args) == 0
    assert opened == [([], True, True)]
    output = capsys.readouterr().out
    assert "[would-apply] r:p.check" in output
    assert "Probe Work" in output
    assert "[would-apply] r:p (pull-hooks)" in output
    assert "Hook Work" in output


def test_completed_run_keeps_auxiliary_error_and_unrun_hook_outcomes():
    from dotman.sync_deck_command import auxiliary_outcome
    error = [{"code": "probe-failed", "message": "boom"}]
    # Only a hookless probe finishes during planning; errors and unrun hooks are not done.
    assert auxiliary_outcome("r:p.check", [], kind="probe", preview=False, run_completed=True, diagnostics=error) == "failed"
    assert auxiliary_outcome("r:p", [], kind="hook", preview=False, run_completed=True, diagnostics=[]) == "pending"
