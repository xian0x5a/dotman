import asyncio
import json

from textual.widgets import DataTable

from dotman.cli import main
from dotman.sync_base_store import DirectoryChildPresent
from dotman.sync_session import Proposal
from dotman.sync_deck import CommandDeck, SyncDeckApp, ReviewDiff, _review_difference
from dotman.sync_deck_command import primary_change_summary, selection_uses_inclusion
from tests.engine.test_sync_directory_observation import directory_engine, open_directory, put


def test_no_confirm_directory_child_converges_with_normal_approval(tmp_path, monkeypatch, capsys):
    engine = directory_engine(tmp_path, monkeypatch, policy="push-only")
    repo, live = tmp_path / "repo/packages/app/tree", tmp_path / "live/tree"
    put(repo, "nested/child", b"repo")
    put(live, "nested/child", b"live")

    assert main(["--config", str(engine.config.config_path), "--json", "--no-confirm", "sync"]) == 0

    payload = json.loads(capsys.readouterr().out)
    unit, = payload["sync_units"]
    assert unit["identity"] == "main:app.tree/nested/child"
    assert unit["observation"] == "drifted"
    assert unit["selected"] and unit["approved"]
    assert unit["result"] == "converged"
    assert (live / "nested/child").read_bytes() == b"repo"


def test_deck_renders_child_as_normal_opt_in_approval(tmp_path, monkeypatch):
    engine = directory_engine(tmp_path, monkeypatch, policy="push-only")
    repo, live = tmp_path / "repo/packages/app/tree", tmp_path / "live/tree"
    put(repo, "nested/child", b"repo")
    put(live, "nested/child", b"live")

    with open_directory(engine) as session:
        app = SyncDeckApp(CommandDeck(session, use_color=True))

        async def interact():
            async with app.run_test(size=(110, 24)) as pilot:
                table = app.query_one(DataTable)
                assert "main:app.tree/nested/child" in table.render_line(1).text
                assert "Unsupported" not in table.render_line(1).text
                assert "[ ]" in table.render_line(1).text
                assert not selection_uses_inclusion(session.view.rows[0])

                await pilot.press("space")
                assert session.view.rows[0].approved
                assert "[x]" in table.render_line(1).text

                await pilot.press("space")
                assert not session.view.rows[0].approved
                assert "[ ]" in table.render_line(1).text

        asyncio.run(asyncio.wait_for(interact(), timeout=5))


def test_child_source_summary_preserves_presence_and_executable_state():
    change = DirectoryChildPresent(b"child", executable=True)
    proposal = Proposal(change, change, change, ())
    assert primary_change_summary(proposal, "/repo/child") == {
        "kind": "write",
        "path": "/repo/child",
        "bytes": 5,
        "executable": True,
    }


def test_child_review_diff_shows_bytes_and_executable_only_changes():
    before = DirectoryChildPresent(b"same", executable=False)
    after = DirectoryChildPresent(b"same", executable=True)
    lines = _review_difference(
        before,
        after,
        before_label="frozen child",
        after_label="approved child",
        description="child",
        use_color=False,
    )
    assert lines == [ReviewDiff(("old mode 100644", "new mode 100755"))]

    changed = _review_difference(
        DirectoryChildPresent(b"old", executable=False),
        DirectoryChildPresent(b"new", executable=True),
        before_label="frozen child",
        after_label="approved child",
        description="child",
        use_color=False,
    )
    [diff] = changed
    changed = diff.lines
    assert "-old" in changed and "+new" in changed
    assert "old mode 100644" in changed
    assert "new mode 100755" in changed
