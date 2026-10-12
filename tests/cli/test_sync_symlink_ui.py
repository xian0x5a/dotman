import asyncio
import json

from dotman.sync_deck import CommandDeck, SyncDeckApp
from tests.cli.test_sync_deck_command import runner_for, arguments
from tests.engine.test_sync_session import open_session
from tests.engine.test_sync_symlinks import linked_file


def test_no_confirm_rejects_prompt_replacement(tmp_path, monkeypatch, capsys):
    engine, path, referent = linked_file(tmp_path, monkeypatch)
    assert runner_for(engine).run(arguments(dry_run=False)) == 1
    payload = json.loads(capsys.readouterr().out)
    assert "symlink-authorization-required" in str(payload)
    assert path.is_symlink()
    assert referent.read_bytes() == b"old"


def test_deck_authorization_is_explicit_and_separate_from_selection(tmp_path, monkeypatch):
    engine, path, referent = linked_file(tmp_path, monkeypatch)
    with open_session(engine) as session:
        app = SyncDeckApp(CommandDeck(session, use_color=False))
        async def interact():
            async with app.run_test(size=(120, 24)) as pilot:
                await pilot.press("space")
                await pilot.pause()
                assert not session.view.rows[0].approved
                # Lowercase l is vim-style navigation; only Shift+L authorizes.
                await pilot.press("l")
                await pilot.pause()
                assert not session.view.rows[0].symlink_authorized
                await pilot.press("L")
                await pilot.pause()
                assert session.view.rows[0].symlink_authorized
                assert not session.view.rows[0].approved
                await pilot.press("space")
                await pilot.pause()
                assert session.view.rows[0].approved
                assert path.is_symlink()
        asyncio.run(asyncio.wait_for(interact(), timeout=5))


def test_workset_hints_list_bulk_selection_and_copy_within_two_rows_at_80_columns(tmp_path, monkeypatch):
    # A link row adds L, the longest workset hint bar.
    engine, _path, _referent = linked_file(tmp_path, monkeypatch)
    with open_session(engine) as session:
        app = SyncDeckApp(CommandDeck(session, use_color=False))
        async def interact():
            async with app.run_test(size=(80, 24)) as pilot:
                await pilot.pause()
                for focus in ("workset", "detail"):
                    help_widget = app.query_one("#help")
                    rendered = " ".join(
                        help_widget.render_line(y).text for y in range(help_widget.size.height)
                    )
                    assert help_widget.size.height <= 2
                    for hint in ("a/u all/none", "y copy", "L link"):
                        assert hint in rendered, (focus, rendered)
                    await pilot.press("tab")
                    await pilot.pause()
        asyncio.run(asyncio.wait_for(interact(), timeout=5))
