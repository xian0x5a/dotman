"""Canonical Additional Source presentation and command routing."""

import asyncio
from pathlib import Path
from types import SimpleNamespace

from dotman.cli_style import render_sync_term
from dotman.sync_deck import CommandDeck, SyncDeckApp, WorksetTable
from dotman.sync_deck_command import sync_document
from dotman.sync_editor import AdditionalEdit
from dotman.sync_session import AdditionalRow, BatchSetApproval, PrepareSourceReview


def additional_row():
    return AdditionalRow(
        row_id="additional:r:shared", repo="r", path=Path("shared"),
        change=AdditionalEdit(Path("/repository/shared"), b"before\n", b"after\n"),
        approved=True, references=("r:app.one", "r:app.two"),
    )


def test_additional_shared_style():
    assert render_sync_term("Additional Source Change", use_color=True) == render_sync_term(
        "Probe Work", use_color=True
    ).replace("Probe Work", "Additional Source Change")


def test_batch_selection_and_source_review_use_session_commands():
    commands = []
    row = additional_row()
    session = SimpleNamespace(
        view=SimpleNamespace(session_id="session", revision=7, rows=(row,)),
        dispatch=lambda command: commands.append(command),
    )
    deck = CommandDeck(session, use_color=False)
    deck.select_all(True)
    deck.open_review()
    assert commands == [
        BatchSetApproval("session", 7, True, (row.row_id,)),
        PrepareSourceReview("session", 7, row.row_id),
    ]


def test_canonical_json_reports_one_shared_source_and_execution_failure():
    row = additional_row()
    session = SimpleNamespace(view=SimpleNamespace(rows=(row,), observations=(), commit_options=()))
    result = SimpleNamespace(
        units=(), status="execution-failed", diagnostics=(), steps=(), commits=(),
        additional_changes=(SimpleNamespace(
            row_id=row.row_id, status="execution-failed",
            diagnostics=(SimpleNamespace(code="source-changed", message="Source changed"),),
        ),),
    )
    document = sync_document(SimpleNamespace(dry_run=False, scopes=[]), session, result)
    assert document["additional_source_changes"] == [{
        "row_id": row.row_id, "repo": "r", "path": "shared", "approved": True,
        "references": ["r:app.one", "r:app.two"], "kind": "write", "bytes": 6,
        "result": "execution-failed",
        "diagnostics": [{"code": "source-changed", "message": "Source changed"}],
    }]
    assert document["summary"]["repository_changes"] == 1
    assert "after" not in str(document)


def test_table_tracks_canonical_source_rows_added_and_removed():
    async def interact():
        session = SimpleNamespace(view=SimpleNamespace(rows=(), operation="sync"))
        app = SyncDeckApp(CommandDeck(session, use_color=False))
        async with app.run_test():
            table = app.query_one(WorksetTable)
            assert table.row_count == 0
            row = additional_row()
            session.view.rows = (row,)
            app.update_workset()
            assert table.row_count == 1
            assert str(table.get_row_at(0)[1]) == "r:shared"
            assert "Additional Source Change" in str(table.get_row_at(0)[3])
            session.view.rows = ()
            app.update_workset()
            assert table.row_count == 0
    asyncio.run(asyncio.wait_for(interact(), timeout=5))


def test_plain_output_reports_source_approval_and_result(capsys):
    from dotman.sync_deck_command import SyncDeckCommandRunner

    row = additional_row()
    session = SimpleNamespace(view=SimpleNamespace(rows=(row,), observations=(), commit_options=()))
    result = SimpleNamespace(
        units=(), status="previewed", diagnostics=(), steps=(), commits=(),
        additional_changes=(SimpleNamespace(row_id=row.row_id, status="would-apply", diagnostics=()),),
    )
    runner = SyncDeckCommandRunner(engine_factory=None, use_color=False)
    runner._emit(SimpleNamespace(dry_run=True, scopes=[], json_output=False), session, result)
    output = capsys.readouterr().out
    assert "[would-apply] r:shared" in output
    assert "Additional Source Change: write" in output
    assert "before" not in output and "after" not in output


def test_unapproved_source_review_labels_bytes_as_candidate():
    from dataclasses import replace
    row = replace(additional_row(), approved=False)
    session = SimpleNamespace(view=SimpleNamespace(rows=(row,)))
    text = CommandDeck(session, use_color=False).review_text()
    assert "Approval: unapproved" in text
    assert "candidate Additional Source" in text
    assert "approved Additional Source" not in text
