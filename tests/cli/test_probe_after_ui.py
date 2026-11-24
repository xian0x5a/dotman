import asyncio

from dotman.sync_deck import CommandDeck, SyncDeckApp, WorksetTable
from tests.cli.test_sync_deck_textual import detail_facts
from tests.engine.test_probe_after import app_manifest, build


def test_conditional_probe_row_shows_probe_work_question_mark_and_checked_after(tmp_path, monkeypatch):
    from dotman.engine import DotmanEngine
    config = build(tmp_path, monkeypatch, app_manifest(tmp_path))
    engine = DotmanEngine.from_config_path(config)

    async def interact():
        with engine.open_sync_session(engine.resolve_sync_scope(), preview=True) as session:
            app = SyncDeckApp(CommandDeck(session, use_color=False))
            async with app.run_test() as pilot:
                table = app.query_one(WorksetTable)
                rows = [[str(cell) for cell in table.get_row_at(index)] for index in range(table.row_count)]
                index = next(i for i, row in enumerate(rows) if "main:app.check" in row[1])
                assert "Probe Work?" in rows[index]
                while app.deck.focus != index:
                    await pilot.press("down")
                await pilot.pause()
                assert any(fact.startswith("Checked after") and "main:base.conf" in fact
                           for fact in detail_facts(app))
    asyncio.run(asyncio.wait_for(interact(), timeout=5))
