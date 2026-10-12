import asyncio
import pytest
from types import SimpleNamespace

from dotman.sync_deck import CommandDeck, SyncDeckApp, WorksetTable
from dotman.sync_deck_command import PullDeckCommandRunner, sync_document
from tests.engine.test_sync_session import make_engine
from tests.cli.test_sync_deck_textual import detail_facts

GUARD = '[targets.unit.hooks]\nguard_pull = "echo offline >&2; exit 100"'


def guarded_engine(tmp_path, monkeypatch):
    return make_engine(tmp_path, monkeypatch, [("unit", "both", b"repo", b"live", GUARD)])


def test_guard_skip_json_names_scope_direction_and_reason(tmp_path, monkeypatch):
    engine = guarded_engine(tmp_path, monkeypatch)
    with engine.open_pull_session(engine.resolve_sync_scope(), preview=True) as session:
        document = sync_document(SimpleNamespace(dry_run=True, scopes=[]), session, None)
    assert document["guard_skips"] == [{
        "identity": "main:app.unit", "direction": "pull", "scope_kind": "target",
        "path_rule_pattern": None, "reason": "offline",
    }]
    assert document["summary"]["selected_auxiliary"] == 0


def test_guard_skip_human_result_explains_omission(tmp_path, monkeypatch, capsys):
    engine = guarded_engine(tmp_path, monkeypatch)
    args = SimpleNamespace(config=engine.config.config_path, scopes=[], no_deps=False, dry_run=True,
                           no_confirm=True, json_output=False, run_noop=False, command="pull")
    assert PullDeckCommandRunner(engine_factory=lambda _: engine, use_color=False).run(args) == 0
    output = capsys.readouterr().out
    assert "[skipped] main:app.unit (guard_pull)" in output
    assert "Guard skipped: offline" in output


def test_guard_skip_deck_row_is_marked_unselectable_with_reason(tmp_path, monkeypatch):
    engine = guarded_engine(tmp_path, monkeypatch)
    asyncio.run(asyncio.wait_for(guard_skip_deck(engine), timeout=5))


async def guard_skip_deck(engine):
    with engine.open_pull_session(engine.resolve_sync_scope(), preview=True) as session:
        app = SyncDeckApp(CommandDeck(session, use_color=False))
        async with app.run_test() as pilot:
            table = app.query_one(WorksetTable)
            marker, target, _policy, resolution = (str(cell) for cell in table.get_row_at(0))
            assert (marker, target, resolution) == ("[-]", "main:app.unit (guard_pull)", "Guard skipped")
            await pilot.press("space")
            assert not session.view.rows[0].included
            assert any("guard_pull exited 100 (offline)" in fact for fact in detail_facts(app))


def test_guard_skip_deck_row_is_dimmed(tmp_path, monkeypatch):
    engine = guarded_engine(tmp_path, monkeypatch)

    async def interact():
        with engine.open_pull_session(engine.resolve_sync_scope(), preview=True) as session:
            app = SyncDeckApp(CommandDeck(session, use_color=True))
            async with app.run_test():
                target = app.query_one(WorksetTable).get_row_at(0)[1]
                assert target.plain == "main:app.unit (guard_pull)"
                # The whole label is dimmed, not just the Resolution cell.
                assert all(span.style.dim for span in target.spans) and target.spans
    asyncio.run(asyncio.wait_for(interact(), timeout=5))


def test_interactive_execution_log_leaves_guard_skips_to_the_deck(tmp_path, monkeypatch, capsys):
    engine = make_engine(tmp_path, monkeypatch, [
        ("unit", "both", b"repo", b"live", ""),
        ("other", "both", b"repo", b"live", '[targets.other.hooks]\nguard_pull = "echo offline >&2; exit 100"'),
    ])
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    monkeypatch.setattr("dotman.sync_deck.run_command_deck", lambda session, *, use_color: True)
    args = SimpleNamespace(config=engine.config.config_path, scopes=[], no_deps=False, dry_run=False,
                           no_confirm=False, json_output=False, run_noop=False, command="pull")
    assert PullDeckCommandRunner(engine_factory=lambda _: engine, use_color=False).run(args) == 0
    output = capsys.readouterr().out
    assert "[1/1] update" in output
    assert "guard_pull" not in output


def test_sync_guard_narrowing_is_reported_in_human_output(tmp_path, monkeypatch, capsys):
    from dotman.sync_deck_command import SyncDeckCommandRunner
    engine = guarded_engine(tmp_path, monkeypatch)
    args = SimpleNamespace(config=engine.config.config_path, scopes=[], no_deps=False, dry_run=True,
                           no_confirm=True, json_output=False, run_noop=False, command="sync")
    SyncDeckCommandRunner(engine_factory=lambda _: engine, use_color=False).run(args)
    output = capsys.readouterr().out
    assert "[skipped] main:app.unit (guard_pull)" in output
    assert "Guard skipped: offline" in output


@pytest.mark.parametrize("dry_run", [False, True])
def test_guard_skips_alone_log_directly_without_the_deck(tmp_path, monkeypatch, capsys, dry_run):
    engine = guarded_engine(tmp_path, monkeypatch)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)

    def forbidden(session, *, use_color):
        raise AssertionError("Guard skips alone have nothing to decide in the Deck")

    monkeypatch.setattr("dotman.sync_deck.run_command_deck", forbidden)
    args = SimpleNamespace(config=engine.config.config_path, scopes=[], no_deps=False, dry_run=dry_run,
                           no_confirm=False, json_output=False, run_noop=False, command="pull")
    assert PullDeckCommandRunner(engine_factory=lambda _: engine, use_color=False).run(args) == 0
    output = capsys.readouterr().out
    assert "[skipped] main:app.unit (guard_pull)" in output
    assert "Guard skipped: offline" in output
