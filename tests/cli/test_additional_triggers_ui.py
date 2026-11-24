import asyncio

from dotman.engine import DotmanEngine
from dotman.sync_deck import CommandDeck, SyncDeckApp, WorksetTable
from tests.cli.test_sync_deck_textual import detail_facts
from tests.engine.test_additional_triggers import build, consumer


def test_detail_panel_names_the_entries_that_triggered_the_target(tmp_path, monkeypatch):
    config = build(tmp_path, monkeypatch, consumer(tmp_path))
    engine = DotmanEngine.from_config_path(config)

    async def interact():
        with engine.open_sync_session(engine.resolve_sync_scope(), preview=True) as session:
            app = SyncDeckApp(CommandDeck(session, use_color=False))
            async with app.run_test() as pilot:
                table = app.query_one(WorksetTable)
                rows = [[str(cell) for cell in table.get_row_at(index)] for index in range(table.row_count)]
                index = next(i for i, row in enumerate(rows) if "main:app.check" in row[1])
                assert "Probe Work" in rows[index]
                while app.deck.focus != index:
                    await pilot.press("down")
                await pilot.pause()
                assert any(fact.startswith("Triggered by") and "main:base.conf" in fact
                           for fact in detail_facts(app))
    asyncio.run(asyncio.wait_for(interact(), timeout=5))
