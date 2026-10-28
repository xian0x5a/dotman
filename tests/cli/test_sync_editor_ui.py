"""Editor behavior at the CLI presentation and terminal input seams."""

from dotman.cli_style import render_sync_term


def test_edited_uses_shared_resolution_style():
    assert render_sync_term("Edited", use_color=True) == render_sync_term(
        "Use repository", use_color=True
    ).replace("Use repository", "Edited")


def test_edited_push_review_shows_deliberate_repository_change_and_live_effect(tmp_path, monkeypatch):
    from dataclasses import replace
    from types import SimpleNamespace

    from dotman.sync_base_store import FilePresent
    from dotman.sync_deck import CommandDeck, row_resolution
    from dotman.sync_deck_command import sync_document
    from dotman.sync_session import Proposal, PublicationEffect
    from tests.engine.test_sync_session import make_engine

    engine = make_engine(tmp_path, monkeypatch, [
        ("unit", "push-only", b"repository-before\n", b"live-before\n", ""),
    ])
    with engine.open_sync_session(engine.resolve_sync_scope([]), preview=True) as opened:
        row = opened.view.rows[0]
        proposal = Proposal(
            repository=FilePresent(b"edited-source\n"),
            live=FilePresent(b"rendered-edit\n"),
            primary_source_change=FilePresent(b"edited-source\n"),
            publication_effects=(PublicationEffect(
                "write", row.observation.live_path, b"rendered-edit\n",
            ),),
            intent="editor", reconciliation="edited repository outcome",
        )
        row = replace(row, proposal=proposal, approved=True)
        session = SimpleNamespace(view=replace(opened.view, rows=(row,)))
        deck = CommandDeck(session, use_color=False)
        assert row_resolution(row) == "Edited"
        text = deck.review_text()
        assert "Resolution: Edited" in text
        assert "Repository effect preview" in text
        assert "-repository-before" in text and "+edited-source" in text
        assert "Live effect preview" in text
        assert "-live-before" in text and "+rendered-edit" in text
        unit = sync_document(SimpleNamespace(dry_run=True, no_confirm=False, scopes=[]), session, None)["sync_units"][0]
        assert unit["resolution_intent"] == "use-repository"
        assert unit["resolution"] == "editor"
        assert "edited-source" not in str(unit)


def test_review_shows_existence_changes_even_without_content_difference(tmp_path, monkeypatch):
    from dataclasses import replace
    from types import SimpleNamespace

    from dotman.sync_base_store import FilePresent
    from dotman.sync_deck import CommandDeck
    from dotman.sync_session import Proposal, PublicationEffect
    from tests.engine.test_sync_session import make_engine

    engine = make_engine(tmp_path, monkeypatch, [("unit", "push-only", b"", None, "")])
    with engine.open_sync_session(engine.resolve_sync_scope([]), preview=True) as opened:
        row = opened.view.rows[0]
        proposal = Proposal(
            repository=FilePresent(b""), live=FilePresent(b""),
            primary_source_change=None,
            publication_effects=(PublicationEffect("write", row.observation.live_path, b""),),
            intent="editor", reconciliation="edited repository outcome",
        )
        row = replace(row, proposal=proposal, approved=True)
        deck = CommandDeck(SimpleNamespace(view=replace(opened.view, rows=(row,))), use_color=False)
        text = deck.review_text()
        repository, live = text.split("Live effect preview", 1)
        assert "No content difference" in repository
        assert "new file" in live and "No content difference" not in live
        assert "frozen" not in text.lower()


def test_editor_key_saves_in_place_and_is_disabled_after_confirmation(tmp_path, monkeypatch):
    import asyncio

    from textual.widgets import DataTable, Static

    from dotman.sync_base_store import FilePresent
    from dotman.sync_deck import CommandDeck, SyncDeckApp
    from tests.engine.test_sync_session import make_engine

    editor = tmp_path / "edit-source"
    editor.write_text('#!/bin/sh\nprintf edited > "$DOTMAN_EDITOR_PRIMARY_PATH"\n')
    editor.chmod(0o700)
    engine = make_engine(tmp_path, monkeypatch, [
        ("unit", "push-only", b"repo", b"live",
         f'editor = {{ run = "{editor}", io = "pipe" }}'),
    ])
    with engine.open_sync_session(engine.resolve_sync_scope([]), preview=True) as session:
        app = SyncDeckApp(CommandDeck(session, use_color=False))

        async def interact():
            async with app.run_test(size=(100, 24)) as pilot:
                assert "e edit" in str(app.query_one("#help", Static).render())
                await pilot.press("space", "enter")
                assert session.view.rows[0].approved
                await pilot.press("e")
                # The Editor runs off the UI thread; wait for it, then for the redraw.
                for _ in range(100):
                    if not app.busy:
                        break
                    await pilot.pause(.01)
                await pilot.pause()
                row = session.view.rows[0]
                assert row.proposal.repository == FilePresent(b"edited")
                assert row.approved
                assert app.deck.reviewing
                body = app.query_one("#review-body")
                rendered = " ".join(body.render_line(y).text for y in range(body.size.height))
                # Rendered facts are column-aligned; normalize the padding.
                assert "Resolution: Edited" in " ".join(rendered.split())
                assert (tmp_path / "repo/packages/app/unit").read_bytes() == b"repo"
                assert (tmp_path / "live/unit").read_bytes() == b"live"
                await pilot.press("escape")
                assert "Edited" in app.query_one(DataTable).render_line(1).text
                await pilot.press("c")
                frozen = session.view
                await pilot.press("e")
                assert session.view == frozen
                await pilot.press("escape", "q")

        asyncio.run(asyncio.wait_for(interact(), timeout=5))


def test_no_confirm_sync_never_launches_configured_editor(tmp_path, monkeypatch, capsys):
    from tests.cli.test_sync_deck_command import arguments, runner_for
    from tests.engine.test_sync_session import make_engine

    marker = tmp_path / "editor-ran"
    engine = make_engine(tmp_path, monkeypatch, [
        ("unit", "pull-only", b"repo", b"live",
         f'capture = "exit 23"\ncompare = {{ repo = "raw", live = "raw" }}\n'
         f'editor = {{ run = "touch {marker}", io = "pipe" }}'),
    ])
    assert runner_for(engine).run(arguments(dry_run=True)) == 1
    assert not marker.exists()
    assert (tmp_path / "repo/packages/app/unit").read_bytes() == b"repo"
    assert (tmp_path / "live/unit").read_bytes() == b"live"


def test_tty_editor_returns_terminal_and_cancel_preserves_selected_proposal(tmp_path, monkeypatch):
    import os
    import pty
    import select
    import signal
    import subprocess
    import sys
    import termios

    from tests.cli.test_sync_interrupt import wait_until
    from tests.engine.test_sync_session import make_engine

    ready = tmp_path / "editor-ready"
    editor = tmp_path / "terminal-editor"
    editor.write_text(
        f"#!{sys.executable}\n"
        "import os, pathlib, signal, sys, termios, time\n"
        "signal.signal(signal.SIGINT, lambda *_: sys.exit(130))\n"
        "assert sys.stdin.isatty()\n"
        "assert termios.tcgetattr(0)[3] & termios.ICANON\n"
        "pathlib.Path(os.environ['DOTMAN_EDITOR_PRIMARY_PATH']).write_text('cancelled-edit')\n"
        f"pathlib.Path({str(ready)!r}).touch()\n"
        "while True: time.sleep(0.01)\n"
    )
    editor.chmod(0o700)
    make_engine(tmp_path, monkeypatch, [
        ("unit", "push-only", b"repo", b"live",
         f'editor = {{ run = "{editor}", io = "tty" }}'),
    ])
    master, slave = pty.openpty()
    terminal_before = termios.tcgetattr(slave)
    process = subprocess.Popen(
        [sys.executable, "-m", "dotman.cli", "--config", str(tmp_path / "config.toml"), "sync"],
        stdin=slave, stdout=slave, stderr=slave, start_new_session=True,
        env={**os.environ, "TERM": "xterm-256color"},
    )
    output = bytearray()

    def read_output():
        if select.select([master], [], [], 0.01)[0]:
            output.extend(os.read(master, 65536))

    try:
        wait_until(lambda: b"Use repository" in output, process, read_output)
        os.write(master, b" ")
        wait_until(lambda: b"[x]" in output, process, read_output)
        os.write(master, b"e")
        wait_until(ready.exists, process, read_output)
        process.send_signal(signal.SIGINT)
        wait_until(lambda: b"Editor cancelled" in output, process, read_output)
        assert process.poll() is None
        assert (tmp_path / "repo/packages/app/unit").read_bytes() == b"repo"
        assert (tmp_path / "live/unit").read_bytes() == b"live"
        os.write(master, b"c")
        wait_until(lambda: b"approved:" in output, process, read_output)
        os.write(master, b"\x03")
        wait_until(lambda: process.poll() is not None, process, read_output)
        # The SIGINT only cancelled the Editor; the Deck's key abort ended the run.
        assert process.returncode == 130
        assert termios.tcgetattr(slave) == terminal_before
        assert b"Traceback" not in output
    except AssertionError as exc:
        exc.add_note(output[-6000:].decode(errors="replace"))
        raise
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
        os.close(master)
        os.close(slave)


def test_long_tty_editor_session_returns_to_deck(tmp_path, monkeypatch):
    import os
    import pty
    import select
    import subprocess
    import sys
    import termios
    import time

    from tests.cli.test_sync_interrupt import wait_until
    from tests.engine.test_sync_session import make_engine

    ready, release = tmp_path / "editor-ready", tmp_path / "editor-release"
    editor = tmp_path / "terminal-editor"
    editor.write_text(
        f"#!{sys.executable}\n"
        "import os, pathlib, time\n"
        f"pathlib.Path({str(ready)!r}).touch()\n"
        f"while not pathlib.Path({str(release)!r}).exists(): time.sleep(0.01)\n"
        "pathlib.Path(os.environ['DOTMAN_EDITOR_PRIMARY_PATH']).write_text('edited')\n"
    )
    editor.chmod(0o700)
    make_engine(tmp_path, monkeypatch, [
        ("unit", "push-only", b"repo", b"live",
         f'editor = {{ run = "{editor}", io = "tty" }}'),
    ])
    master, slave = pty.openpty()
    terminal_before = termios.tcgetattr(slave)
    process = subprocess.Popen(
        [sys.executable, "-m", "dotman.cli", "--config", str(tmp_path / "config.toml"), "sync"],
        stdin=slave, stdout=slave, stderr=slave, start_new_session=True,
        env={**os.environ, "TERM": "xterm-256color"},
    )
    output = bytearray()

    def read_output():
        if select.select([master], [], [], 0.01)[0]:
            output.extend(os.read(master, 65536))

    try:
        wait_until(lambda: b"Use repository" in output, process, read_output)
        os.write(master, b"e")
        wait_until(ready.exists, process, read_output)
        # Outlast Textual's bounded output queue (30 writes) at the busy
        # spinner's 10 Hz refresh; the deck must not repaint while suspended.
        time.sleep(4)
        resumed_from = len(output)
        release.touch()
        wait_until(lambda: b"Edited" in output[resumed_from:], process, read_output)
        os.write(master, b"\x03")
        wait_until(lambda: process.poll() is not None, process, read_output)
        assert process.returncode == 130
        assert termios.tcgetattr(slave) == terminal_before
        assert b"Traceback" not in output
    except AssertionError as exc:
        exc.add_note(output[-6000:].decode(errors="replace"))
        raise
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
        os.close(master)
        os.close(slave)


def test_edited_pull_review_does_not_claim_capture_is_pending(tmp_path, monkeypatch):
    from dataclasses import replace
    from types import SimpleNamespace

    from dotman.sync_base_store import FilePresent
    from dotman.sync_deck import CommandDeck
    from dotman.sync_session import Proposal
    from tests.engine.test_sync_session import make_engine

    engine = make_engine(tmp_path, monkeypatch, [
        ("unit", "pull-only", b"repo", b"live", ""),
    ])
    with engine.open_sync_session(engine.resolve_sync_scope([]), preview=True) as opened:
        row = opened.view.rows[0]
        proposal = Proposal(
            repository=FilePresent(b"edit"), live=row.observation.live,
            primary_source_change=FilePresent(b"edit"),
            publication_effects=(), intent="editor",
            reconciliation="edited repository outcome",
        )
        row = replace(row, proposal=proposal)
        session = SimpleNamespace(view=replace(opened.view, rows=(row,)))
        text = CommandDeck(session, use_color=False).review_text()
        assert "Capture: not required" in text
        assert "Live remains unchanged" in text
        assert "Drift" not in text


def test_additional_edits_have_independent_canonical_review_and_json(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from dotman.sync_deck import CommandDeck
    from dotman.sync_deck import set_resolution_intent
    from dotman.sync_deck_command import sync_document
    from tests.engine.test_sync_session import make_engine

    editor = tmp_path / "edit-additional"
    editor.write_text(
        '#!/bin/sh\nprintf primary-edit > "$DOTMAN_EDITOR_PRIMARY_PATH"\n'
        'printf additional-candidate > "$DOTMAN_EDITOR_ADDITIONAL_SOURCE_PATHS"\n'
    )
    editor.chmod(0o700)
    engine = make_engine(tmp_path, monkeypatch, [
        ("unit", "both", b"repo", b"live",
         f'editor = {{ run = "{editor}", io = "pipe", additional_sources = ["shared"] }}'),
    ])
    shared = tmp_path / "repo/packages/app/shared"
    shared.write_bytes(b"additional-preimage")
    with engine.open_sync_session(engine.resolve_sync_scope([]), preview=True) as session:
        deck = CommandDeck(session, use_color=False)
        deck.edit()
        # Choosing an automatic intent does not silently discard retained Additional edits.
        set_resolution_intent(session, session.view.rows[0].row_id, "use-repository")
        deck.open_review()
        text = deck.review_text()
        assert "Additional Source Changes" in text
        assert "-additional-preimage" not in text and "+additional-candidate" not in text
        additional = next(row for row in session.view.rows if row.kind == "additional")
        assert str(additional.path) in text
        deck.focus = session.view.rows.index(additional)
        deck.back()
        deck.open_review()
        text = deck.review_text()
        assert "Additional Source Review" in text
        assert "-additional-preimage" in text and "+additional-candidate" in text
        deck.select()
        assert session.view.rows[deck.focus].approved
        assert not session.view.rows[0].approved
        assert shared.read_bytes() == b"additional-preimage"
        document = sync_document(SimpleNamespace(dry_run=True, no_confirm=False, scopes=[]), session, None)
        assert "staged_additional_sources" not in document["sync_units"][0]
        assert document["sync_units"][0]["additional_source_changes"] == [additional.row_id]
        change = document["additional_source_changes"][0]
        assert change["path"] == "packages/app/shared"
        assert change["approved"] is True
        assert change["references"] == [session.view.rows[0].row_id]
        assert "additional-candidate" not in str(document)
        assert "repos: 1" in deck.confirmation_text()
        deck.back()
        deck.select_all(False)
        assert not any(row.approved for row in session.view.rows)
        deck.select_all(True)
        assert all(row.approved for row in session.view.rows)


def test_unchanged_editor_quit_says_resolution_was_kept(tmp_path, monkeypatch):
    from dotman.sync_deck import CommandDeck
    from tests.engine.test_sync_session import make_engine

    engine = make_engine(tmp_path, monkeypatch, [("unit", "push-only", b"repo", b"live", 'editor = { run = "true", io = "pipe" }')])
    with engine.open_sync_session(engine.resolve_sync_scope([]), preview=True) as session:
        deck = CommandDeck(session, use_color=False)
        deck.edit()
        assert deck.notice == "Editor closed without changes; Resolution kept."


def test_rejected_action_on_observation_failed_row_explains_instead_of_showing_code(tmp_path, monkeypatch):
    from dotman.sync_deck import CommandDeck, row_resolution
    from tests.engine.test_sync_session import make_engine

    engine = make_engine(tmp_path, monkeypatch, [("bad", "push-only", b"repo", b"live", "")])
    live = tmp_path / "live/bad"
    live.unlink()
    live.mkdir()
    with engine.open_sync_session(engine.resolve_sync_scope([]), preview=True) as session:
        deck = CommandDeck(session, use_color=False)
        assert row_resolution(deck.focused_row) == "Observation failed"
        for action in (deck.select, deck.open_review):
            deck.notice = ""
            action()
            assert deck.notice != "disallowed"
            assert "observation failed" in deck.notice.lower()
            assert "run sync again" in deck.notice.lower()
