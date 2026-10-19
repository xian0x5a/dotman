from __future__ import annotations

import asyncio

import pytest
from textual import events

from textual.widgets import DataTable, Static

from dotman import sync_deck, sync_observation
from dotman.sync_deck import CommandDeck, SyncDeckApp, WorksetTable
from dotman.sync_path_policy import SyncPathError

SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
from tests.engine.test_sync_session import make_engine
from tests.helpers import wait_until_idle


def post_cell_click(app, offset):
    # Pilot.click bypasses App.on_event. Inject the terminal event seam so mouse
    # and keyboard ordering exercises the same boundary as a real terminal.
    table = app.query_one(DataTable)
    x, y = table.region.x + offset[0], table.region.y + offset[1]
    for event_type in (events.MouseDown, events.MouseUp):
        app.post_message(event_type(
            app.screen, x, y, 0, 0, 1, False, False, False,
            screen_x=x, screen_y=y,
        ))


# Short enough to keep the transient-notice tests fast.
TRANSIENT_NOTICE_SECONDS = 0.5


def run(coroutine):
    return asyncio.run(asyncio.wait_for(coroutine, timeout=5))


def detail_strips(app):
    body = app.query_one("#detail-body")
    return [body.render_line(y) for y in range(body.size.height)]


def detail_lines(app):
    """All detail rows as rendered, without the ring or scroll clipping."""
    return [strip.text.rstrip() for strip in detail_strips(app)]


def review_strips(app):
    body = app.query_one("#review-body")
    return [body.render_line(y) for y in range(body.size.height)]


def review_text(app):
    return "\n".join(strip.text.rstrip() for strip in review_strips(app))


def title_text(app):
    return str(app.query_one("#title", Static).render())


def detail_facts(app):
    """Whitespace-normalized rows, so grid column padding does not matter."""
    return [" ".join(line.split()) for line in detail_lines(app)]


def test_rendered_columns_align_across_variable_identities_and_resize(tmp_path, monkeypatch):
    engine = make_engine(tmp_path, monkeypatch, [
        ("a", "push-only", b"r", b"l", ""),
        ("longer_target", "push-only", b"r", b"l", ""),
    ])
    with engine.open_sync_session(engine.resolve_sync_scope([]), preview=True) as session:
        app = SyncDeckApp(CommandDeck(session, use_color=False))

        async def interact():
            async with app.run_test(size=(100, 24)) as pilot:
                table = app.query_one(DataTable)
                lines = [table.render_line(y).text for y in range(3)]
                assert lines[1].index("push-only") == lines[0].index("Policy")
                assert lines[2].index("push-only") == lines[0].index("Policy")
                assert lines[1].index("main:app.a") == lines[0].index("Target")
                assert lines[2].index("main:app.longer_target") == lines[0].index("Target")
                await pilot.resize_terminal(42, 16)
                assert table.size.width == 42
                await pilot.press("end")
                await pilot.pause()
                assert table.scroll_x > 0
                assert "Use repository" in table.render_line(1).text
                await pilot.resize_terminal(100, 24)
                await pilot.press("home")
                assert not any(row.approved for row in session.view.rows)
        run(interact())


def test_focused_row_highlight_spans_the_whole_row(tmp_path, monkeypatch):
    engine = make_engine(tmp_path, monkeypatch, [
        ("a", "push-only", b"r", b"l", ""),
        ("b", "push-only", b"r", b"l", ""),
    ])
    with engine.open_sync_session(engine.resolve_sync_scope([]), preview=True) as session:
        app = SyncDeckApp(CommandDeck(session, use_color=False))

        async def interact():
            async with app.run_test(size=(100, 24)) as pilot:
                await pilot.press("down")
                table = app.query_one(DataTable)
                cursor_background = table.get_component_rich_style("datatable--cursor").bgcolor
                focused_line = table.render_line(2)
                assert "main:app.b" in focused_line.text
                assert {segment.style.bgcolor for segment in focused_line} == {cursor_background}
        run(interact())


def test_long_target_identities_shrink_so_all_columns_fit_the_terminal(tmp_path, monkeypatch):
    long_name = "very_long_target_name_that_would_push_policy_and_resolution_off_screen"
    engine = make_engine(tmp_path, monkeypatch, [
        ("a", "push-only", b"r", b"l", ""),
        (long_name, "push-only", b"r", b"l", ""),
    ])
    with engine.open_sync_session(engine.resolve_sync_scope([]), preview=True) as session:
        app = SyncDeckApp(CommandDeck(session, use_color=False))

        async def interact():
            async with app.run_test(size=(80, 24)) as pilot:
                await pilot.pause()
                table = app.query_one(DataTable)
                assert table.max_scroll_x == 0
                header, short_row, long_row = (table.render_line(y).text for y in range(3))
                assert "Resolution" in header
                # The Selection header is no wider than its "[ ]" marker cells.
                assert header.index("Target") <= len(" [ ]  ")
                assert "main:app.a " in short_row
                # Middle elision keeps both the repo prefix and the target name tail.
                assert "main:app.very" in long_row and "…" in long_row and "off_screen" in long_row
                assert "Use repository" in long_row
                # The focused detail still names the full canonical identity.
                await pilot.press("down")
                assert f"main:app.{long_name}" in "".join(detail_lines(app))
                await pilot.resize_terminal(200, 24)
                await pilot.pause()
                assert f"main:app.{long_name}" in table.render_line(2).text
        run(interact())


def test_keyboard_review_scroll_return_approval_and_confirmation(tmp_path, monkeypatch):
    engine = make_engine(tmp_path, monkeypatch, [
        ("one", "push-only", b"repo", b"live", ""),
        ("two", "push-only", b"repo\n" * 100, b"live\n" * 100, ""),
    ])
    with engine.open_sync_session(engine.resolve_sync_scope([]), preview=True) as session:
        app = SyncDeckApp(CommandDeck(session, use_color=False))

        async def interact():
            async with app.run_test() as pilot:
                await pilot.press("down", "enter", "down", "down")
                log = app.query_one("#review")
                await pilot.pause()
                assert log.scroll_y > 0
                position = log.scroll_y
                assert not any(row.approved for row in session.view.rows)
                await pilot.press("escape", "enter")
                await pilot.pause()
                assert log.scroll_y == position
                # x toggles Selection like Space; c opens the confirmation.
                await pilot.press("escape", "x", "c")
                frozen = session.view
                await pilot.press("space", "a", "u", "down")
                assert session.view == frozen
                await pilot.press("escape", "C", "X")
                assert not app.deck.confirming and session.view == frozen
                await pilot.press("c", "enter")
                assert app.return_value is True
        run(interact())
        assert [row.approved for row in session.view.rows] == [False, True]


def test_copy_key_copies_full_target_identity_and_review_text(tmp_path, monkeypatch):
    monkeypatch.setattr("dotman.sync_deck.TRANSIENT_NOTICE_SECONDS", TRANSIENT_NOTICE_SECONDS)
    engine = make_engine(tmp_path, monkeypatch, [
        ("one", "push-only", b"repo", b"live", ""),
    ])
    with engine.open_sync_session(engine.resolve_sync_scope([]), preview=True) as session:
        app = SyncDeckApp(CommandDeck(session, use_color=False))

        async def interact():
            async with app.run_test(size=(40, 24)) as pilot:
                await pilot.press("y")
                # The narrow Target cell is elided; the copy keeps the full identity.
                assert app.clipboard == "main:app.one"
                await pilot.press("enter", "y")
                assert app.clipboard.startswith(":: Proposal Review — main:app.one")
                assert "\x1b[" not in app.clipboard
                assert "Copied" in str(app.query_one("#notice", Static).render())
                await asyncio.sleep(TRANSIENT_NOTICE_SECONDS)
                await pilot.pause()
                assert str(app.query_one("#notice", Static).render()) == ""
        run(interact())


def test_review_title_shows_approval_after_toggle(tmp_path, monkeypatch):
    engine = make_engine(tmp_path, monkeypatch, [
        ("one", "push-only", b"repo", b"live", ""),
    ])
    with engine.open_sync_session(engine.resolve_sync_scope([]), preview=True) as session:
        app = SyncDeckApp(CommandDeck(session, use_color=False))

        async def interact():
            async with app.run_test() as pilot:
                await pilot.press("enter")
                await pilot.pause()
                assert title_text(app) == ":: Proposal Review (unapproved)"
                # Review names the toggle like the workset; the title shows what Selection means here.
                assert "x select" in help_text(app) and "Space" not in help_text(app)
                # The Decision section scrolls away; the title keeps Approval in view.
                await pilot.press("space")
                await pilot.pause()
                assert title_text(app) == ":: Proposal Review (approved)"
                await pilot.press("space")
                await pilot.pause()
                assert title_text(app) == ":: Proposal Review (unapproved)"
        run(interact())


def test_mouse_click_focuses_identity_and_toggles_only_approval(tmp_path, monkeypatch):
    engine = make_engine(tmp_path, monkeypatch, [
        ("one", "push-only", b"repo", b"live", ""),
        ("two", "push-only", b"repo", b"live", ""),
    ])
    with engine.open_sync_session(engine.resolve_sync_scope([]), preview=True) as session:
        app = SyncDeckApp(CommandDeck(session, use_color=False))

        async def interact():
            async with app.run_test() as pilot:
                post_cell_click(app, (15, 2))
                await pilot.pause()
                assert app.deck.focused_row.row_id == "main:app.two"
                assert not any(row.approved for row in session.view.rows)
                post_cell_click(app, (2, 2))
                await pilot.pause()
                assert [row.approved for row in session.view.rows] == [False, True]
                await pilot.press("a")
                assert all(row.approved for row in session.view.rows)
                await pilot.press("u")
                assert not any(row.approved for row in session.view.rows)
                # Esc only steps back, so an extra press at the workset must not abort.
                await pilot.press("escape")
                assert app.is_running
                await pilot.press("q")
                assert app.return_value is False
        run(interact())


def test_dependency_rows_name_the_package_input_that_included_them(tmp_path, monkeypatch):
    engine = make_engine(tmp_path, monkeypatch, [("own", "push-only", b"r", b"l", "")],
                         dependency_targets=[("shared", "push-only", b"r", b"l", "")],
                         dependency_extra='[hooks]\npre_push = [{ run = "true", run_noop = true }]')
    with engine.open_sync_session(engine.resolve_sync_scope(["main:app"]), preview=True) as session:
        app = SyncDeckApp(CommandDeck(session, use_color=True))

        def label_dim(text):
            # A dimmed label renders as one plain segment; a requested one splits into styled parts.
            table = app.query_one(DataTable)
            for y in range(1, len(session.view.rows) + 1):
                for segment in table.render_line(y):
                    if text in segment.text:
                        return bool(segment.style and segment.style.dim)
            raise AssertionError(f"no Target label containing {text!r}")

        async def interact():
            async with app.run_test(size=(110, 24)) as pilot:
                # Only the row a dependency brought in recedes.
                assert label_dim("main:base.shared") and label_dim("main:base (push-hooks)")
                assert not label_dim("app")
                facts_by_row = {}
                for row in session.view.rows:
                    facts_by_row[row.row_id] = detail_facts(app)
                    await pilot.press("down")
                assert "Included via: main:app" in facts_by_row["main:base.shared"]
                # The dependency's hook work is there for the same reason.
                assert "Included via: main:app" in facts_by_row["main:base (push-hooks)"]
                assert not any(fact.startswith("Included via:") for fact in facts_by_row["main:app.own"])
        run(interact())


def test_both_fallback_is_distinct_from_observation_failure(tmp_path, monkeypatch):
    engine = make_engine(tmp_path, monkeypatch, [
        ("both", "both", b"repo", b"live", ""),
        ("bad", "push-only", b"repo", b"live", ""),
    ])
    live = tmp_path / "live/bad"
    live.unlink()
    live.mkdir()
    with engine.open_sync_session(engine.resolve_sync_scope([]), preview=True) as session:
        app = SyncDeckApp(CommandDeck(session, use_color=False))

        async def interact():
            async with app.run_test(size=(110, 24)) as pilot:
                table = app.query_one(DataTable)
                assert "Use repository" in table.render_line(1).text
                assert "Observation failed" in table.render_line(2).text
                # The warning-colored Resolution and the Sync Base line carry the guess; no Fallback fact repeats it.
                assert detail_facts(app)[0] == "main:app.both"
                assert detail_facts(app)[1].startswith("Live path:")
                await pilot.press("space", "a")
                assert [row.approved for row in session.view.rows] == [True, False]
                await pilot.press("down")
                assert detail_facts(app)[:2] == ["main:app.bad", "error: endpoint must be a regular file"]
        run(interact())


def test_guess_color_yields_to_a_choice_and_to_a_failed_proposal(tmp_path, monkeypatch):
    from dataclasses import replace
    from dotman.cli_style import render_sync_term
    from dotman.sync_deck import render_row_resolution
    from dotman.sync_session import Diagnostic

    engine = make_engine(tmp_path, monkeypatch, [("both", "both", b"repo", b"live", "")])
    with engine.open_sync_session(engine.resolve_sync_scope([]), preview=True) as session:
        row = session.view.rows[0]
        plain = render_sync_term("Use repository", use_color=True)
        assert row.resolution_guessed
        assert render_row_resolution(row, use_color=True) != plain
        assert render_row_resolution(replace(row, resolution_chosen=True), use_color=True) == plain
        failed = replace(row, diagnostics=(Diagnostic("materialization-failed", "boom"),))
        assert render_row_resolution(failed, use_color=True) == render_sync_term("Proposal failed", use_color=True)


def test_fixed_resolution_is_recessive_and_offers_no_intent_key(tmp_path, monkeypatch):
    from dotman.cli_style import render_sync_term
    from dotman.sync_deck import render_row_resolution

    engine = make_engine(tmp_path, monkeypatch, [
        ("both", "both", b"repo", b"live", ""),
        ("pull", "pull-only", b"repo", b"live", ""),
        ("push", "push-only", b"repo", b"live", ""),
    ])
    with engine.open_sync_session(engine.resolve_sync_scope([]), preview=True) as session:
        _both, pull, push = session.view.rows
        # Push-only and pull-only follow from Policy; only real choices keep the Resolution emphasis.
        assert render_row_resolution(pull, use_color=True) != render_sync_term("Use live", use_color=True)
        assert render_row_resolution(push, use_color=True) != render_sync_term("Use repository", use_color=True)
        app = SyncDeckApp(CommandDeck(session, use_color=False))

        async def interact():
            async with app.run_test(size=(140, 24)) as pilot:
                assert "r intent" in help_text(app) and "Enter review" in help_text(app)
                await pilot.press("down")
                assert "r intent" not in help_text(app)
                await pilot.press("down")
                assert "r intent" not in help_text(app)
        run(interact())


def test_review_marks_the_resolution_guessed_only_while_guessed(tmp_path, monkeypatch):
    from dotman.sync_session import SetResolutionIntent

    engine = make_engine(tmp_path, monkeypatch, [("both", "both", b"repo", b"live", "")])
    with engine.open_sync_session(engine.resolve_sync_scope([]), preview=True) as session:
        deck = CommandDeck(session, use_color=False)
        deck.open_review()
        assert "Use repository (guessed)" in deck.review_text()
        assert "Fallback:" not in deck.review_text()
        deck.back()
        view = session.view
        session.dispatch(SetResolutionIntent(view.session_id, view.revision, "main:app.both", "use-live"))
        deck.open_review()
        assert "(guessed)" not in deck.review_text()


def test_detail_ring_shows_full_paths_and_state_with_hanging_indent(tmp_path, monkeypatch):
    long_name = "very_long_target_name_that_needs_wrapping_inside_the_detail_ring"
    engine = make_engine(tmp_path, monkeypatch, [(long_name, "push-only", b"repo", b"live", "")])
    with engine.open_sync_session(engine.resolve_sync_scope([]), preview=True) as session:
        app = SyncDeckApp(CommandDeck(session, use_color=False))
        observation = session.view.rows[0].observation

        async def interact():
            async with app.run_test(size=(60, 40)) as pilot:
                await pilot.pause()
                assert app.query_one("#detail").styles.border_top[0] == "round"
                lines = detail_lines(app)
                assert "".join(lines[:2]) == f"main:app.{long_name}"
                live = next(index for index, line in enumerate(lines) if line.startswith("  Live path:"))
                value_column = len(lines[live]) - len(lines[live][len("  Live path:"):].lstrip())
                # Wrapped values continue under their value column, not at the ring edge.
                continuations = []
                for line in lines[live + 1:]:
                    if not line[:value_column].isspace():
                        break
                    continuations.append(line)
                assert continuations and all(not line[value_column].isspace() for line in continuations)
                assert "".join(line[value_column:] for line in [lines[live], *continuations]) == str(observation.live_path)
                facts = detail_facts(app)
                assert any(fact.startswith("Repository path:") for fact in facts)
                assert "Observation: drifted · Sync Base: " + observation.base.status in facts
        run(interact())


def test_tab_focuses_detail_for_keyboard_scrolling_and_esc_returns(tmp_path, monkeypatch):
    engine = make_engine(tmp_path, monkeypatch, [
        (f"target_with_a_long_name_{index}", "push-only", b"repo", b"live", "") for index in range(2)
    ])
    with engine.open_sync_session(engine.resolve_sync_scope([]), preview=True) as session:
        app = SyncDeckApp(CommandDeck(session, use_color=False))

        async def interact():
            async with app.run_test(size=(40, 16)) as pilot:
                await pilot.pause()
                table, detail = app.query_one(WorksetTable), app.query_one("#detail")
                assert detail.max_scroll_y > 0
                assert "Tab detail" in str(app.query_one("#help", Static).render())
                await pilot.press("tab")
                assert app.focused is detail
                await pilot.press("down", "down")
                await pilot.wait_for_animation()
                assert table.cursor_row == 0 and detail.scroll_y > 0
                # Row commands still act on the row the detail describes.
                await pilot.press("space")
                assert [row.approved for row in session.view.rows] == [True, False]
                await pilot.press("escape")
                assert app.is_running and app.focused is table
                await pilot.press("tab")
                await pilot.press("tab")
                assert app.focused is table
                await pilot.press("down")
                await pilot.pause()
                assert table.cursor_row == 1 and detail.scroll_y == 0
        run(interact())


def test_detail_styles_identity_and_diagnostics_like_the_workset(tmp_path, monkeypatch):
    engine = make_engine(tmp_path, monkeypatch, [("bad", "push-only", b"repo", b"live", "")])
    live = tmp_path / "live/bad"
    live.unlink()
    live.mkdir()
    with engine.open_sync_session(engine.resolve_sync_scope([]), preview=True) as session:
        app = SyncDeckApp(CommandDeck(session, use_color=True))

        async def interact():
            async with app.run_test(size=(110, 24)):
                strips = detail_strips(app)
                plain = list(strips[1])[-1].style
                styled = {segment.text.strip() for strip in strips for segment in strip if segment.style != plain}
                assert {"main", "bad", "error"} <= styled
                hints = app.query_one("#help", Static).render()
                assert hints.plain.startswith("[c confirm] · q abort · x select")
                bold = {hints.plain[span.start:span.end] for span in hints.spans if "bold" in str(span.style)}
                assert {"q", "c", "x"} <= bold and "confirm" not in bold
        run(interact())


def test_pull_review_keeps_frozen_evidence_and_never_approves_on_open(tmp_path, monkeypatch):
    engine = make_engine(tmp_path, monkeypatch, [
        ("unit", "pull-only", b"repo\n", b"live\n", ""),
    ])
    with engine.open_sync_session(engine.resolve_sync_scope([]), preview=True) as session:
        (tmp_path / "live/unit").write_bytes(b"external change\n")
        app = SyncDeckApp(CommandDeck(session, use_color=True))

        async def interact():
            async with app.run_test(size=(100, 24)) as pilot:
                assert "Use live" in app.query_one(DataTable).render_line(1).text
                await pilot.press("enter")
                text = review_text(app)
                assert "+live" in text and "external change" not in text
                assert "Live remains unchanged" in text
                assert not session.view.rows[0].approved
                await pilot.press("space")
                assert session.view.rows[0].approved
                await pilot.press("escape", "c")
                assert "repos: 1 · live: 0" in str(app.query_one("#confirmation", Static).render())
                await pilot.press("ctrl+c")
                assert app.return_value is False
        run(interact())


def test_empty_workset_can_cancel_without_a_cursor_target(tmp_path, monkeypatch):
    engine = make_engine(tmp_path, monkeypatch, [
        ("unit", "push-only", b"same", b"same", ""),
    ])
    with engine.open_sync_session(engine.resolve_sync_scope([]), preview=True) as session:
        app = SyncDeckApp(CommandDeck(session, use_color=False))

        async def interact():
            async with app.run_test() as pilot:
                assert "No drifted work" in detail_lines(app)[0]
                await pilot.press("down", "space", "enter", "a", "u", "q")
                assert app.return_value is False
                assert session.view.rows == ()
        run(interact())


def test_long_workset_scrolls_without_losing_focused_row(tmp_path, monkeypatch):
    engine = make_engine(tmp_path, monkeypatch, [
        (f"unit_{index:02}", "push-only", b"repo", b"live", "")
        for index in range(25)
    ])
    with engine.open_sync_session(engine.resolve_sync_scope([]), preview=True) as session:
        app = SyncDeckApp(CommandDeck(session, use_color=False))

        async def interact():
            async with app.run_test(size=(80, 12)) as pilot:
                await pilot.press(*(["down"] * 24))
                table = app.query_one(DataTable)
                assert table.scroll_y > 0
                assert app.deck.focused_row.row_id == "main:app.unit_24"
                await pilot.press("enter", "escape", "space")
                assert table.cursor_row == 24
                assert [row.row_id for row in session.view.rows if row.approved] == ["main:app.unit_24"]
                assert "Policy" in table.render_line(0).text
                visible_row = next(
                    y for y in range(table.size.height)
                    if "main:app.unit_24" in table.render_line(y).text
                )
                post_cell_click(app, (2, visible_row))
                await pilot.pause()
                assert not any(row.approved for row in session.view.rows)
                assert table.cursor_row == 24
        run(interact())


@pytest.mark.parametrize("navigation,start,expected", [
    ("down", 0, 1),
    ("up", 1, 0),
    ("pagedown", 0, "page"),
    ("pageup", 14, "page"),
    ("ctrl+end", 0, 24),
    ("ctrl+home", 24, 0),
    ("home", 7, 7),
    ("end", 7, 7),
    ("left", 7, 7),
    ("right", 7, 7),
    ("j", 0, 1),
    ("k", 1, 0),
    ("h", 7, 7),
    ("l", 7, 7),
])
@pytest.mark.parametrize("command", ["space", "enter"])
def test_batched_navigation_targets_new_row(tmp_path, monkeypatch, navigation, start, expected, command):
    engine = make_engine(tmp_path, monkeypatch, [
        (f"unit_{index:02}", "push-only", b"repo", b"live", "")
        for index in range(25)
    ])
    with engine.open_sync_session(engine.resolve_sync_scope([]), preview=True) as session:
        app = SyncDeckApp(CommandDeck(session, use_color=False))

        async def interact():
            async with app.run_test(size=(80, 12)) as pilot:
                await pilot.press(*(["down"] * start))
                target = expected
                if expected == "page":
                    # A page is the table's visible row count, which depends on the surrounding layout.
                    table = app.query_one(DataTable)
                    page = table.scrollable_content_region.height - table.header_height
                    target = start + page if navigation == "pagedown" else start - page
                # Unlike Pilot.press, post without yielding between terminal keys.
                app.post_message(events.Key(navigation, None))
                app.post_message(events.Key(command, " " if command == "space" else None))
                await pilot.pause()
                table = app.query_one(DataTable)
                assert table.cursor_row == target
                assert app.deck.focused_row.row_id == f"main:app.unit_{target:02}"
                if command == "space":
                    assert [row.row_id for row in session.view.rows if row.approved] == [
                        f"main:app.unit_{target:02}"
                    ]
                else:
                    assert app.deck.reviewing
                    # Opening review queues its resize after the input batch settles.
                    await pilot.pause()
                    assert review_text(app).splitlines()[0].strip() == f"main:app.unit_{target:02}"
                    assert not any(row.approved for row in session.view.rows)
        run(interact())



@pytest.mark.parametrize("column,keys,focused,approvals", [
    (15, ("space",), 1, [False, True]),
    (15, ("enter",), 1, [False, False]),
    (15, ("up", "space"), 0, [True, False]),
])
def test_batched_mouse_and_keyboard_share_target(tmp_path, monkeypatch, column, keys, focused, approvals):
    engine = make_engine(tmp_path, monkeypatch, [
        ("one", "push-only", b"repo", b"live", ""),
        ("two", "push-only", b"repo", b"live", ""),
    ])
    with engine.open_sync_session(engine.resolve_sync_scope([]), preview=True) as session:
        app = SyncDeckApp(CommandDeck(session, use_color=False))

        async def interact():
            async with app.run_test(size=(80, 12)) as pilot:
                post_cell_click(app, (column, 2))
                for key in keys:
                    app.post_message(events.Key(key, " " if key == "space" else None))
                await pilot.pause()
                assert app.query_one(DataTable).cursor_row == focused
                assert app.deck.focused_row.row_id == session.view.rows[focused].row_id
                assert [row.approved for row in session.view.rows] == approvals
                if keys[-1] == "enter":
                    await pilot.pause()
                    # The title already names the review; the body opens with the target only.
                    assert title_text(app) == ":: Proposal Review (unapproved)"
                    assert review_text(app).splitlines()[0].strip() == "main:app.two"
        run(interact())


def test_help_area_click_cannot_authorize_after_clear_key(tmp_path, monkeypatch):
    engine = make_engine(tmp_path, monkeypatch, [
        ("one", "push-only", b"repo", b"live", ""),
        ("two", "push-only", b"repo", b"live", ""),
    ])
    with engine.open_sync_session(engine.resolve_sync_scope([]), preview=True) as session:
        app = SyncDeckApp(CommandDeck(session, use_color=False))

        async def interact():
            async with app.run_test(size=(80, 12)) as pilot:
                await pilot.pause()
                # Terminal coordinates previously hit the clickable Approve all
                # footer. No yielding before U: a deferred click must not grant
                # Approval after the later explicit clear action.
                for event_type in (events.MouseDown, events.MouseUp):
                    app.post_message(event_type(
                        app.screen, 49, 11, 0, 0, 1, False, False, False,
                        screen_x=49, screen_y=11,
                    ))
                app.post_message(events.Key("u", "u"))
                await pilot.pause()
                assert not any(row.approved for row in session.view.rows)
                await pilot.resize_terminal(40, 12)
                help_widget = app.query_one("#help", Static)
                assert help_widget.size.height == 2
                rendered_help = " ".join(
                    help_widget.render_line(y).text for y in range(help_widget.size.height)
                )
                assert "q abort" in rendered_help and "c confirm" in rendered_help
        run(interact())


def post_help_click(app, label):
    # Through App.on_event like post_cell_click, so a chip click keeps input order.
    help_widget = app.query_one("#help", Static)
    line = help_widget.render_line(0).text
    x = help_widget.region.x + line.index(label) + 1
    y = help_widget.region.y
    for event_type in (events.MouseDown, events.MouseUp):
        app.post_message(event_type(
            app.screen, x, y, 0, 0, 1, False, False, False,
            screen_x=x, screen_y=y,
        ))


def test_help_chips_click_through_confirmation(tmp_path, monkeypatch):
    engine = make_engine(tmp_path, monkeypatch, [("one", "push-only", b"repo", b"live", "")])
    with engine.open_sync_session(engine.resolve_sync_scope([]), preview=True) as session:
        app = SyncDeckApp(CommandDeck(session, use_color=False))

        async def interact():
            async with app.run_test(size=(100, 24)) as pilot:
                await pilot.press("x")
                post_help_click(app, "[c confirm]")
                await pilot.pause()
                assert app.deck.confirming
                assert help_text(app) == "[Esc return] · [Enter preview] · Ctrl+C abort"
                post_help_click(app, "[Esc return]")
                await pilot.pause()
                assert not app.deck.confirming
                post_help_click(app, "[c confirm]")
                await pilot.pause()
                post_help_click(app, "[Enter preview]")
                await pilot.pause()
                assert app.return_value is True
        run(interact())
        assert [row.approved for row in session.view.rows] == [True]


def test_double_click_on_confirm_chip_cannot_execute(tmp_path, monkeypatch):
    engine = make_engine(tmp_path, monkeypatch, [("one", "push-only", b"repo", b"live", "")])
    with engine.open_sync_session(engine.resolve_sync_scope([]), preview=False) as session:
        app = SyncDeckApp(CommandDeck(session, use_color=False))

        async def interact():
            async with app.run_test(size=(100, 24)) as pilot:
                await pilot.press("x")
                # No pause between clicks: the second lands on whatever chip the
                # first click's view put under the pointer.
                post_help_click(app, "[c confirm]")
                post_help_click(app, "[c confirm]")
                await pilot.pause()
                assert app.return_value is None and not app.deck.confirming
        run(interact())


def test_hovered_help_chip_lights_up(tmp_path, monkeypatch):
    engine = make_engine(tmp_path, monkeypatch, [("one", "push-only", b"repo", b"live", "")])
    with engine.open_sync_session(engine.resolve_sync_scope([]), preview=True) as session:
        app = SyncDeckApp(CommandDeck(session, use_color=True))

        def chip_styles():
            hints = app.query_one("#help", Static).render()
            chip_end = hints.plain.index("]") + 1
            return {(hints.plain[span.start:span.end], str(span.style)) for span in hints.spans if span.end <= chip_end}

        async def interact():
            async with app.run_test(size=(100, 24)) as pilot:
                resting = chip_styles()
                await pilot.hover("#help", offset=(3, 0))
                await pilot.pause()
                hovered = chip_styles()
                assert not any(text == "confirm" and "dim" in style for text, style in hovered)
                assert any(text == "[" and "yellow" in style for text, style in hovered)
                await pilot.hover("#workset")
                await pilot.pause()
                assert chip_styles() == resting
        run(interact())


def test_resolution_key_toggles_between_two_intents_without_menu(tmp_path, monkeypatch):
    from textual.widgets import OptionList

    engine = make_engine(tmp_path, monkeypatch, [
        ('unit', 'both', b'repo', b'live', ''),
    ])
    with engine.open_sync_session(engine.resolve_sync_scope([]), preview=True) as session:
        app = SyncDeckApp(CommandDeck(session, use_color=False))

        async def interact():
            async with app.run_test() as pilot:
                table = app.query_one(DataTable)
                assert session.view.rows[0].intent == 'use-repository'
                # The ? marks the guess without relying on color.
                assert 'Use repository?' in table.render_line(1).text
                await pilot.press('r')
                await pilot.pause()
                assert not app.query_one("#resolution", OptionList).display
                assert session.view.rows[0].intent == 'use-live'
                assert not session.view.rows[0].approved
                # A chosen side is no longer a guess.
                assert 'Use live' in table.render_line(1).text and '?' not in table.render_line(1).text
                await pilot.press('r')
                await pilot.pause()
                assert session.view.rows[0].intent == 'use-repository'
                # Toggling back is an explicit choice, so the guess stays gone.
                assert not session.view.rows[0].resolution_guessed
        run(interact())


def test_resolution_key_on_edited_row_offers_edit_alongside_both_sides(tmp_path, monkeypatch):
    from textual.widgets import OptionList
    from dotman.sync_deck_command import edit_proposal

    engine = make_engine(tmp_path, monkeypatch, [
        ('unit', 'both', b'repo', b'live', 'editor = { run = "printf edited > \\"$DOTMAN_SOURCE\\"", io = "pipe" }'),
    ])
    with engine.open_sync_session(engine.resolve_sync_scope([]), preview=True) as session:
        edit_proposal(session, session.view.rows[0].row_id)
        app = SyncDeckApp(CommandDeck(session, use_color=False))

        async def interact():
            async with app.run_test() as pilot:
                # Edited is a third choice, so R must not silently flip to a side.
                await pilot.press('r')
                menu = app.query_one("#resolution", OptionList)
                assert menu.display and menu.option_count == 3
                assert menu.highlighted == session.view.rows[0].allowed_intents.index('editor')
                await pilot.press('escape')
                assert session.view.rows[0].intent == 'editor'
        run(interact())


def test_resolution_menu_changes_intent_without_approval(tmp_path, monkeypatch):
    from textual.widgets import OptionList
    from tests.engine.test_sync_both_convergence import established

    engine = established(tmp_path, monkeypatch)
    with engine.open_sync_session(engine.resolve_sync_scope([]), preview=True) as session:
        app = SyncDeckApp(CommandDeck(session, use_color=False))

        async def interact():
            async with app.run_test() as pilot:
                assert session.view.rows[0].intent == 'merge'
                await pilot.press('r')
                menu = app.query_one("#resolution", OptionList)
                assert menu.display
                assert menu.option_count == len(session.view.rows[0].allowed_intents)
                await pilot.press('home', 'j')
                assert menu.highlighted == 1
                await pilot.press('k', 'enter')
                assert session.view.rows[0].intent == session.view.rows[0].allowed_intents[0]
                assert not session.view.rows[0].approved
                assert not menu.display
                await pilot.press('r', 'escape')
                assert not menu.display
                assert app.return_value is None
        run(interact())


def test_resolution_cell_and_menu_support_mouse(tmp_path, monkeypatch):
    from textual.widgets import OptionList
    from tests.engine.test_sync_both_convergence import established

    engine = established(tmp_path, monkeypatch)
    with engine.open_sync_session(engine.resolve_sync_scope([]), preview=True) as session:
        app = SyncDeckApp(CommandDeck(session, use_color=False))

        async def interact():
            async with app.run_test(size=(100, 24)) as pilot:
                table = app.query_one(DataTable)
                resolution_x = table.render_line(0).text.index('Resolution')
                post_cell_click(app, (resolution_x, 1))
                await pilot.pause()
                assert app.query_one("#resolution", OptionList).display
                # "select" names only the row toggle; the menu's Enter picks an option.
                assert help_text(app) == "↑/↓/j/k move · Enter choose · Esc dismiss"
                await pilot.click('#resolution', offset=(2, 1))
                assert session.view.rows[0].intent == 'use-repository'
                assert not session.view.rows[0].approved
                assert not app.query_one("#resolution", OptionList).display
        run(interact())


def test_retry_key_rematerializes_failed_review_without_approval(tmp_path, monkeypatch):
    from dotman import sync_session

    engine = make_engine(tmp_path, monkeypatch, [('unit', 'both', b'repo', b'live', '')])
    materialize = sync_session.materialize
    calls = []

    def fail_once(*args, **kwargs):
        calls.append(None)
        if len(calls) == 1:
            raise ValueError('Capture unavailable')
        return materialize(*args, **kwargs)

    monkeypatch.setattr(sync_session, 'materialize', fail_once)
    with engine.open_sync_session(engine.resolve_sync_scope([]), preview=True) as session:
        app = SyncDeckApp(CommandDeck(session, use_color=False))

        async def interact():
            async with app.run_test() as pilot:
                await pilot.press('enter')
                assert session.view.rows[0].diagnostics
                await pilot.press('t')
                assert session.view.rows[0].proposal is not None
                assert not session.view.rows[0].diagnostics
                assert not session.view.rows[0].approved
                assert len(calls) == 2
        run(interact())


def test_materialization_keeps_deck_responsive_and_gates_actions(tmp_path, monkeypatch):
    import threading
    from contextvars import ContextVar
    from dotman.sync_base_store import FilePresent

    engine = make_engine(tmp_path, monkeypatch, [
        ("unit", "pull-only", b"repo", b"live", ""),
    ])
    ready, release = threading.Event(), threading.Event()
    context = ContextVar("materialization-test", default="missing")
    context.set("copied")
    seen = []
    with engine.open_sync_session(engine.resolve_sync_scope([]), preview=True) as session:
        def capture(observation):
            seen.append(context.get())
            ready.set()
            assert release.wait(2), "deck event loop blocked during Capture"
            return FilePresent(b"live")
        monkeypatch.setattr(session, "_capture", capture)
        app = SyncDeckApp(CommandDeck(session, use_color=False))

        async def interact():
            async with app.run_test() as pilot:
                try:
                    table = app.query_one(WorksetTable)
                    help_widget = app.query_one("#help", Static)
                    spinning = lambda: any(frame in str(table.get_cell_at((0, 0))) for frame in SPINNER_FRAMES)
                    app.action_select()
                    # Quick work must not flash progress; it appears only once work lingers,
                    # in the row's Selection cell rather than on an extra line.
                    assert not spinning()
                    for _ in range(100):
                        if ready.is_set():
                            break
                        await asyncio.sleep(.01)
                    assert ready.is_set()
                    for _ in range(100):
                        if spinning():
                            break
                        await asyncio.sleep(.01)
                    assert spinning()
                    assert "Ctrl+C abort" in str(help_widget.render())
                    revision = session.view.revision
                    app.action_clear_selection()
                    app.action_select_all()
                    app.action_review_or_confirm()
                    app.action_retry()
                    app.action_confirm()
                    app.action_resolution()
                    app.choose_resolution(0)
                    post_cell_click(app, (2, 1))
                    await pilot.pause()
                    assert not app.deck.confirming
                    assert session.view.revision == revision
                finally:
                    release.set()
                for _ in range(100):
                    if session.view.rows[0].approved:
                        break
                    await asyncio.sleep(.01)
                await pilot.pause()
                assert session.view.rows[0].approved
                assert seen == ["copied"]
                assert str(table.get_cell_at((0, 0))) == "[x]"
                assert "Ctrl+C abort" not in str(help_widget.render())
        run(interact())


def post_cell_mouse(app, event_type, offset):
    table = app.query_one(DataTable)
    x, y = table.region.x + offset[0], table.region.y + offset[1]
    app.post_message(event_type(app.screen, x, y, 0, 0, 1, False, False, False, screen_x=x, screen_y=y))


def test_press_dropped_while_busy_cannot_toggle_on_a_later_release(tmp_path, monkeypatch):
    import threading
    from dotman.sync_base_store import FilePresent

    engine = make_engine(tmp_path, monkeypatch, [("unit", "pull-only", b"repo", b"live", "")])
    ready, release = threading.Event(), threading.Event()
    with engine.open_sync_session(engine.resolve_sync_scope([]), preview=True) as session:
        def capture(observation):
            ready.set()
            assert release.wait(2)
            return FilePresent(b"live")
        monkeypatch.setattr(session, "_capture", capture)
        app = SyncDeckApp(CommandDeck(session, use_color=False))

        async def interact():
            async with app.run_test() as pilot:
                post_cell_mouse(app, events.MouseDown, (2, 1))
                await pilot.pause()
                app.action_select()
                for _ in range(100):
                    if ready.is_set():
                        break
                    await asyncio.sleep(.01)
                # Busy drops this release, leaving the earlier press unanswered.
                post_cell_mouse(app, events.MouseUp, (2, 1))
                await pilot.pause()
                release.set()
                for _ in range(100):
                    if session.view.rows[0].approved and not app.busy:
                        break
                    await asyncio.sleep(.01)
                post_cell_mouse(app, events.MouseUp, (2, 1))
                await pilot.pause()
                assert not app.busy and session.view.rows[0].approved
        run(interact())


def test_abort_waits_for_materialization_before_terminalizing_and_stops_batch(tmp_path, monkeypatch):
    import threading
    from dotman.sync_base_store import FilePresent

    engine = make_engine(tmp_path, monkeypatch, [
        ("one", "pull-only", b"repo", b"live", ""),
        ("two", "pull-only", b"repo", b"live", ""),
    ])
    ready, release = threading.Event(), threading.Event()
    captures = []
    with engine.open_sync_session(engine.resolve_sync_scope([]), preview=True) as session:
        def capture(observation):
            captures.append(observation.identity.canonical)
            ready.set()
            assert release.wait(2)
            return FilePresent(b"live")
        monkeypatch.setattr(session, "_capture", capture)
        app = SyncDeckApp(CommandDeck(session, use_color=False))

        async def interact():
            async with app.run_test() as pilot:
                app.action_select_all()
                try:
                    for _ in range(100):
                        if ready.is_set():
                            break
                        await asyncio.sleep(.01)
                    assert ready.is_set()
                    await pilot.press("ctrl+c", "ctrl+c")
                    assert not session.view.terminal
                    assert app.busy
                    assert app.return_value is None
                finally:
                    release.set()
                for _ in range(100):
                    if app.return_value is False:
                        break
                    await asyncio.sleep(.01)
                assert app.return_value is False
            session.abort()
            assert session.view.terminal
            assert captures == ["main:app.one"]
            assert not session.view.rows[1].approved
            terminal = session.view
            await asyncio.sleep(0)
            assert session.view == terminal
        run(interact())


def test_review_wraps_long_lines_under_their_column_and_reflows(tmp_path, monkeypatch):
    long_line = "word " * 30
    engine = make_engine(tmp_path, monkeypatch, [
        ("one", "push-only", f"{long_line}\n".encode(), b"live\n", ""),
    ])
    with engine.open_sync_session(engine.resolve_sync_scope([]), preview=True) as session:
        app = SyncDeckApp(CommandDeck(session, use_color=False))

        async def interact():
            async with app.run_test(size=(60, 40)) as pilot:
                await pilot.press("enter")
                await pilot.pause()
                review = app.query_one("#review")
                assert review.virtual_size.width <= review.scrollable_content_region.width
                lines = review_text(app).splitlines()
                start = next(i for i, line in enumerate(lines) if line.strip().startswith("+word"))
                marker_column = lines[start].index("+")
                # Continuations hang under the content, right of the +/- marker.
                assert lines[start + 1].startswith(" " * (marker_column + 1) + "word")
                wrapped_rows = sum("word" in line for line in lines)
                await pilot.resize_terminal(120, 40)
                await pilot.pause()
                assert sum("word" in line for line in review_text(app).splitlines()) < wrapped_rows
        run(interact())


def test_review_colors_sections_and_diff_lines(tmp_path, monkeypatch):
    engine = make_engine(tmp_path, monkeypatch, [
        ("one", "push-only", b"repo-line\n", b"live-line\n", ""),
    ])
    with engine.open_sync_session(engine.resolve_sync_scope([]), preview=True) as session:
        app = SyncDeckApp(CommandDeck(session, use_color=True))

        async def interact():
            async with app.run_test(size=(100, 40)) as pilot:
                await pilot.press("enter")
                await pilot.pause()
                styles = {segment.text.strip(): segment.style
                          for strip in review_strips(app) for segment in strip if segment.text.strip()}
                assert styles["Paths"].bold
                assert styles["live-line"].color.number == 1  # ANSI red
                assert styles["repo-line"].color.number == 2  # ANSI green
                assert styles["Approval:"].dim
        run(interact())


def test_copy_key_copies_mouse_selected_review_text(tmp_path, monkeypatch):
    engine = make_engine(tmp_path, monkeypatch, [
        ("one", "push-only", b"repo", b"live", ""),
    ])
    with engine.open_sync_session(engine.resolve_sync_scope([]), preview=True) as session:
        app = SyncDeckApp(CommandDeck(session, use_color=False))

        async def interact():
            async with app.run_test(size=(80, 24)) as pilot:
                await pilot.press("enter")
                await pilot.pause()
                body = app.query_one("#review-body")
                row, start = 0, body.render_line(0).text.index("main:app.one")
                await pilot.mouse_down(body, offset=(start, row))
                await pilot.hover(body, offset=(start + len("main:app.one"), row))
                await pilot.mouse_up(body, offset=(start + len("main:app.one"), row))
                await pilot.press("y")
                assert app.clipboard == "main:app.one"
        run(interact())


def test_review_sections_open_with_full_width_titled_rules(tmp_path, monkeypatch):
    engine = make_engine(tmp_path, monkeypatch, [
        ("one", "push-only", b"repo", b"live", ""),
    ])
    with engine.open_sync_session(engine.resolve_sync_scope([]), preview=True) as session:
        app = SyncDeckApp(CommandDeck(session, use_color=False))

        async def interact():
            async with app.run_test(size=(60, 40)) as pilot:
                await pilot.press("enter")
                await pilot.pause()
                body = app.query_one("#review-body")
                [rule] = [line for line in review_text(app).splitlines() if "Paths" in line]
                assert rule.startswith("── Paths ─")
                assert len(rule) == body.content_size.width and rule.endswith("─")
                # Copied text keeps the compact header form.
                assert "  :: Paths" in app.deck.review_text()
        run(interact())


def visible_review_text(app):
    log = app.query_one("#review")
    lines = review_text(app).splitlines()
    return "\n".join(lines[int(log.scroll_y):int(log.scroll_y) + log.scrollable_content_region.height])


@pytest.mark.parametrize("use_color", [False, True])
def test_full_view_lands_on_off_screen_change_and_steps_between_change_blocks(tmp_path, monkeypatch, use_color):
    repo = b"".join(b"row-%02d\n" % index for index in range(80))
    live = (repo.replace(b"row-20\nrow-21\nrow-22\n", b"live-20\nlive-21\nlive-22\n")
            .replace(b"row-45\n", b"live-45\n"))
    engine = make_engine(tmp_path, monkeypatch, [("one", "push-only", repo, live, "")])
    with engine.open_sync_session(engine.resolve_sync_scope([]), preview=True) as session:
        app = SyncDeckApp(CommandDeck(session, use_color=use_color))

        async def interact():
            async with app.run_test(size=(80, 24)) as pilot:
                await pilot.press("enter", "down", "down")
                await pilot.pause()
                log = app.query_one("#review")
                review_position = log.scroll_y
                assert review_position > 0
                assert "row-00" not in review_text(app)
                # The only diff opens directly, without a menu.
                await pilot.press("v")
                await pilot.pause()
                assert title_text(app) == ":: Full View"
                assert "row-00" in review_text(app) and "row-79" in review_text(app)
                # The first change starts below the screen, so Full View lands on it.
                assert "live-20" in visible_review_text(app) and "row-00" not in visible_review_text(app)
                # A multi-line change block is one stop.
                await pilot.press("n")
                await pilot.pause()
                assert "live-45" in visible_review_text(app) and "live-20" not in visible_review_text(app)
                await pilot.press("N")
                await pilot.pause()
                assert "live-20" in visible_review_text(app)
                # Full View is read-only evidence.
                await pilot.press("space")
                assert not session.view.rows[0].approved
                await pilot.press("y")
                assert "row-00" in app.clipboard
                await pilot.press("escape")
                await pilot.pause()
                assert title_text(app) == ":: Proposal Review (unapproved)"
                assert log.scroll_y == review_position
                assert "row-00" not in review_text(app)
        run(interact())


def test_full_view_no_newline_marker_keeps_replaced_last_line_one_change_stop(tmp_path, monkeypatch):
    # "\\ No newline at end of file" sits between the removed and added line.
    repo = b"".join(b"row-%02d\n" % index for index in range(10)) + b"old"
    engine = make_engine(tmp_path, monkeypatch, [("one", "push-only", repo, repo.replace(b"old", b"new"), "")])
    with engine.open_sync_session(engine.resolve_sync_scope([]), preview=True) as session:
        app = SyncDeckApp(CommandDeck(session, use_color=False))

        async def interact():
            async with app.run_test(size=(80, 40)) as pilot:
                await pilot.press("enter", "v")
                await pilot.pause()
                assert title_text(app) == ":: Full View"
                assert review_text(app).count("No newline at end of file") == 2
                assert len(app.query_one(sync_deck.ReviewBody).change_rows) == 1
        run(interact())


def test_full_view_menu_offers_each_diff_and_shows_whole_merge_output(tmp_path, monkeypatch):
    from textual.widgets import OptionList
    from tests.engine.test_sync_both_convergence import established

    engine = established(tmp_path, monkeypatch)
    context = b"".join(b"ctx-%d\n" % index for index in range(10))
    (tmp_path / "repo/packages/app/unit").write_bytes(b"repository\n" + context)
    (tmp_path / "live/unit").write_bytes(b"conflict\n" + context)
    with engine.open_sync_session(engine.resolve_sync_scope([]), preview=True) as session:
        app = SyncDeckApp(CommandDeck(session, use_color=False))

        async def interact():
            async with app.run_test(size=(80, 40)) as pilot:
                await pilot.press("enter")
                await pilot.pause()
                assert "ctx-9" not in app.deck.review_text()
                await pilot.press("v")
                menu = app.query_one("#resolution", OptionList)
                assert menu.display
                assert [str(menu.get_option_at_index(index).prompt) for index in range(menu.option_count)] == [
                    "Merge conflicts", "Drift"]
                # Dismissing the menu stays in review.
                await pilot.press("escape")
                assert not menu.display and title_text(app) == ":: Proposal Review (unapproved)"
                await pilot.press("v", "home", "enter")
                await pilot.pause()
                assert title_text(app) == ":: Full View"
                # The first conflict is visible from the top, so nothing scrolls past the heading.
                assert app.query_one("#review").scroll_y == 0
                text = app.deck.review_text()
                assert "<<<<<<< repository" in text and "ctx-9" in text and "⋯" not in text
                # Pager habit: q in Full View or review must not discard the session.
                await pilot.press("q")
                assert app.is_running and title_text(app) == ":: Full View"
                await pilot.press("escape")
                assert title_text(app) == ":: Proposal Review (unapproved)"
                await pilot.press("q")
                assert app.is_running and title_text(app) == ":: Proposal Review (unapproved)"
                await pilot.press("escape")
                assert app.query_one(WorksetTable).display
        run(interact())


def test_menus_open_at_the_bottom_above_help_in_workset_and_review(tmp_path, monkeypatch):
    from textual.widgets import OptionList
    from tests.engine.test_sync_both_convergence import established

    engine = established(tmp_path, monkeypatch)
    (tmp_path / "live/unit").write_bytes(b"conflict\nmiddle\nlast\n")
    with engine.open_sync_session(engine.resolve_sync_scope([]), preview=True) as session:
        app = SyncDeckApp(CommandDeck(session, use_color=False))

        async def interact():
            async with app.run_test(size=(80, 40)) as pilot:
                menu, help_line = app.query_one("#resolution", OptionList), app.query_one("#help")
                await pilot.press("r")
                await pilot.pause()
                assert menu.region.y > app.query_one("#detail").region.y
                assert menu.region.bottom <= help_line.region.y
                await pilot.press("escape", "enter", "v")
                await pilot.pause()
                assert menu.display
                assert menu.region.y > app.query_one("#review").region.y
                assert menu.region.bottom <= help_line.region.y
        run(interact())


def help_text(app):
    return str(app.query_one("#help", Static).render())


def search_repo_and_live():
    repo = b"".join(b"view-%02d\n" % index for index in range(80))
    live = (repo.replace(b"view-20\nview-21\nview-22\n", b"live-20\nlive-21\nlive-22\n")
            .replace(b"view-45\n", b"live-45\n"))
    return repo, live


def test_review_search_box_keeps_deck_keys_out_of_the_query(tmp_path, monkeypatch):
    repo, live = search_repo_and_live()
    engine = make_engine(tmp_path, monkeypatch, [("one", "push-only", repo, live, "")])
    with engine.open_sync_session(engine.resolve_sync_scope([]), preview=True) as session:
        app = SyncDeckApp(CommandDeck(session, use_color=False))

        async def interact():
            async with app.run_test(size=(80, 24)) as pilot:
                await pilot.press("enter")
                await pilot.pause()
                frozen = session.view
                # Every typed key is also a deck key: edit, full view, approve, select all, confirm, quit.
                await pilot.press("slash", "e", "v", "space", "a", "c", "q")
                await pilot.pause()
                assert app.query_one("#search").value == "ev ac" + "q"
                assert help_text(app) == "Enter search · ↑/↓ history · Ctrl+U clear · Esc cancel · Ctrl+C abort"
                assert session.view == frozen
                assert title_text(app) == ":: Proposal Review (unapproved)"
                assert app.is_running
                # Esc while typing cancels the search without leaving the review.
                await pilot.press("escape")
                await pilot.pause()
                assert not app.query_one("#search-bar").display
                assert title_text(app) == ":: Proposal Review (unapproved)"
                assert "/ev acq" not in help_text(app)
        run(interact())


@pytest.mark.parametrize("use_color", [False, True])
def test_full_view_search_highlights_and_steps_matches_before_change_blocks(tmp_path, monkeypatch, use_color):
    repo, live = search_repo_and_live()
    engine = make_engine(tmp_path, monkeypatch, [("one", "push-only", repo, live, "")])
    with engine.open_sync_session(engine.resolve_sync_scope([]), preview=True) as session:
        app = SyncDeckApp(CommandDeck(session, use_color=use_color))

        async def interact():
            async with app.run_test(size=(80, 24)) as pilot:
                await pilot.press("enter", "v")
                await pilot.pause()
                assert "live-20" in visible_review_text(app)
                await pilot.press("slash", "V", "i", "e", "w", "minus", "7", "enter")
                await pilot.pause()
                # Case-insensitive; lands on the first match below the current position.
                assert "view-70" in visible_review_text(app)
                assert "/View-7 1/10" in help_text(app)
                match_line = next(strip for strip in review_strips(app) if "view-70" in strip.text)
                assert any(segment.style and segment.style.reverse and segment.text == "view-7"
                           for segment in match_line)
                await pilot.press("n")
                assert "/View-7 2/10" in help_text(app)
                # Matches wrap around in both directions.
                await pilot.press("N", "N")
                assert "/View-7 10/10" in help_text(app)
                await pilot.press("n")
                assert "/View-7 1/10" in help_text(app)
                # Esc clears the search first; n/N step change blocks again.
                await pilot.press("escape")
                await pilot.pause()
                assert title_text(app) == ":: Full View"
                assert "/View-7" not in help_text(app)
                assert not any(segment.style and segment.style.reverse
                               for strip in review_strips(app) for segment in strip if "view-7" in segment.text)
                await pilot.press("N")
                await pilot.pause()
                assert "live-45" in visible_review_text(app)
                await pilot.press("escape")
                await pilot.pause()
                assert title_text(app) == ":: Proposal Review (unapproved)"
        run(interact())


def test_full_view_search_without_matches_keeps_change_block_navigation(tmp_path, monkeypatch):
    monkeypatch.setattr("dotman.sync_deck.TRANSIENT_NOTICE_SECONDS", TRANSIENT_NOTICE_SECONDS)
    repo, live = search_repo_and_live()
    engine = make_engine(tmp_path, monkeypatch, [("one", "push-only", repo, live, "")])
    with engine.open_sync_session(engine.resolve_sync_scope([]), preview=True) as session:
        app = SyncDeckApp(CommandDeck(session, use_color=False))

        async def interact():
            async with app.run_test(size=(80, 24)) as pilot:
                await pilot.press("enter", "v")
                await pilot.pause()
                await pilot.press("slash", "z", "z", "z", "enter")
                await pilot.pause()
                assert "No match for zzz." in str(app.query_one("#notice", Static).render())
                assert "/zzz" not in help_text(app)
                await asyncio.sleep(TRANSIENT_NOTICE_SECONDS)
                await pilot.pause()
                assert str(app.query_one("#notice", Static).render()) == ""
                await pilot.press("n")
                await pilot.pause()
                assert "live-45" in visible_review_text(app)
        run(interact())


FILTER_UNITS = ("git_config", "nvim_init", "zsh_env", "zsh_rc")


def filter_engine(tmp_path, monkeypatch):
    return make_engine(tmp_path, monkeypatch, [
        (name, "push-only", b"repo", b"live", "") for name in FILTER_UNITS
    ])


def table_row_ids(app):
    return [key.value for key in app.query_one(WorksetTable).rows]


def test_workset_filter_narrows_rows_and_scopes_bulk_selection(tmp_path, monkeypatch):
    engine = filter_engine(tmp_path, monkeypatch)
    with engine.open_sync_session(engine.resolve_sync_scope([]), preview=True) as session:
        app = SyncDeckApp(CommandDeck(session, use_color=False))

        async def interact():
            async with app.run_test(size=(100, 20)) as pilot:
                # h is also vim-left: the typed query must not navigate.
                await pilot.press("slash", "Z", "s", "h")
                await pilot.pause()
                # The table narrows while typing.
                assert table_row_ids(app) == ["main:app.zsh_env", "main:app.zsh_rc"]
                assert help_text(app) == "Enter filter · ↑/↓ history · Ctrl+U clear · Esc cancel · Ctrl+C abort"
                await pilot.press("enter")
                await pilot.pause()
                assert "/Zsh 2/4" in help_text(app)
                assert "q abort" not in help_text(app)
                await pilot.press("a")
                assert {row.row_id for row in session.view.rows if row.approved} == {
                    "main:app.zsh_env", "main:app.zsh_rc"}
                # Space and clicks act on the visible row, not the session row at that index.
                post_cell_click(app, (2, 2))
                await pilot.pause()
                assert {row.row_id for row in session.view.rows if row.approved} == {"main:app.zsh_env"}
                await pilot.press("space")
                assert {row.row_id for row in session.view.rows if row.approved} == {
                    "main:app.zsh_env", "main:app.zsh_rc"}
                # The box reopens with the filter for refinement.
                await pilot.press("slash", "backspace")
                await pilot.pause()
                assert app.query_one("#search").value == "Zs"
                # A new filter reports selections it hides.
                await pilot.press("ctrl+u", "g", "i", "t", "enter")
                await pilot.pause()
                assert table_row_ids(app) == ["main:app.git_config"]
                assert "2 selected hidden" in title_text(app)
                await pilot.press("u")
                assert {row.row_id for row in session.view.rows if row.approved} == {
                    "main:app.zsh_env", "main:app.zsh_rc"}
                # q might be a stray query key, so a filtered workset does not abort.
                await pilot.press("q")
                await pilot.pause()
                assert app.is_running
                await pilot.press("escape")
                await pilot.pause()
                assert table_row_ids(app) == [f"main:app.{name}" for name in FILTER_UNITS]
                assert "hidden" not in title_text(app)
                assert "q abort" in help_text(app)
        run(interact())


def test_workset_filter_survives_review_and_cancel_restores_it(tmp_path, monkeypatch):
    monkeypatch.setattr("dotman.sync_deck.TRANSIENT_NOTICE_SECONDS", TRANSIENT_NOTICE_SECONDS)
    engine = filter_engine(tmp_path, monkeypatch)
    with engine.open_sync_session(engine.resolve_sync_scope([]), preview=True) as session:
        app = SyncDeckApp(CommandDeck(session, use_color=False))

        async def interact():
            async with app.run_test(size=(100, 20)) as pilot:
                await pilot.press("slash", "z", "s", "h", "enter", "down", "enter")
                await pilot.pause()
                assert title_text(app) == ":: Proposal Review (unapproved)"
                assert app.deck.focused_row.row_id == "main:app.zsh_rc"
                await pilot.press("escape")
                await pilot.pause()
                assert table_row_ids(app) == ["main:app.zsh_env", "main:app.zsh_rc"]
                assert app.deck.focused_row.row_id == "main:app.zsh_rc"
                # Esc while typing restores the filter from before the search box opened.
                await pilot.press("slash", "ctrl+u", "n", "v", "i")
                await pilot.pause()
                assert table_row_ids(app) == ["main:app.nvim_init"]
                await pilot.press("x")
                await pilot.pause()
                assert table_row_ids(app) == []
                assert detail_lines(app)[0] == "No Target matches the filter."
                await pilot.press("escape")
                await pilot.pause()
                assert table_row_ids(app) == ["main:app.zsh_env", "main:app.zsh_rc"]
                # A filter without matches is reported and dropped.
                await pilot.press("slash", "ctrl+u", "x", "x", "x", "enter")
                await pilot.pause()
                assert "No match for xxx." in str(app.query_one("#notice", Static).render())
                assert table_row_ids(app) == [f"main:app.{name}" for name in FILTER_UNITS]
                await asyncio.sleep(TRANSIENT_NOTICE_SECONDS)
                await pilot.pause()
                assert str(app.query_one("#notice", Static).render()) == ""
        run(interact())


def test_workset_filter_box_recalls_recent_filters_with_up_and_down(tmp_path, monkeypatch):
    monkeypatch.setattr(sync_deck, "SEARCH_HISTORY_LIMIT", 2)
    engine = filter_engine(tmp_path, monkeypatch)
    with engine.open_sync_session(engine.resolve_sync_scope([]), preview=True) as session:
        app = SyncDeckApp(CommandDeck(session, use_color=False))

        async def interact():
            async with app.run_test(size=(100, 20)) as pilot:
                box = app.query_one("#search")
                await pilot.press("slash", "z", "s", "h", "enter", "slash", "ctrl+u", "n", "v", "i", "enter")
                await pilot.pause()
                # The box opens with the current filter, so recall skips that same entry.
                await pilot.press("slash", "up")
                await pilot.pause()
                assert box.value == "zsh"
                assert table_row_ids(app) == ["main:app.zsh_env", "main:app.zsh_rc"]
                await pilot.press("up")
                assert box.value == "zsh"
                # Down past the newest entry returns to the text typed before recall.
                await pilot.press("down")
                await pilot.pause()
                assert box.value == "nvi"
                assert table_row_ids(app) == ["main:app.nvim_init"]
                await pilot.press("escape")
                # Only the most recent filters are kept.
                await pilot.press("slash", "ctrl+u", "g", "i", "t", "enter", "slash", "up", "up")
                assert box.value == "nvi"
        run(interact())


def test_reader_search_box_recalls_only_reader_searches(tmp_path, monkeypatch):
    repo, live = search_repo_and_live()
    engine = make_engine(tmp_path, monkeypatch, [("one", "push-only", repo, live, "")])
    with engine.open_sync_session(engine.resolve_sync_scope([]), preview=True) as session:
        app = SyncDeckApp(CommandDeck(session, use_color=False))

        async def interact():
            async with app.run_test(size=(80, 24)) as pilot:
                box = app.query_one("#search")
                await pilot.press("slash", "o", "n", "e", "enter", "enter")
                await pilot.pause()
                # Workset filters name Targets; they are not offered as file searches.
                await pilot.press("slash", "up")
                assert box.value == ""
                await pilot.press("v", "i", "e", "w", "enter")
                await pilot.pause()
                await pilot.press("slash", "up")
                assert box.value == "view"
        run(interact())


def notice_text(app):
    return str(app.query_one("#notice", Static).render())


def test_notice_clears_on_the_next_key_press_or_click(tmp_path, monkeypatch):
    # Long enough that only input, not the timer, can clear the notice here.
    monkeypatch.setattr("dotman.sync_deck.TRANSIENT_NOTICE_SECONDS", 60)
    engine = filter_engine(tmp_path, monkeypatch)
    with engine.open_sync_session(engine.resolve_sync_scope([]), preview=True) as session:
        app = SyncDeckApp(CommandDeck(session, use_color=False))

        async def interact():
            async with app.run_test(size=(100, 20)) as pilot:
                await pilot.press("slash", "0", "enter")
                await pilot.pause()
                assert notice_text(app) == "No match for 0."
                # A notice answers one key; the next key makes it stale.
                await pilot.press("down")
                await pilot.pause()
                assert notice_text(app) == ""
                # A key that raises a notice still shows its own.
                await pilot.press("slash", "0", "enter")
                await pilot.pause()
                assert notice_text(app) == "No match for 0."
                # Clicking the notice dismisses it without other effects.
                focused = app.deck.focused_row.row_id
                await pilot.click("#notice")
                await pilot.pause()
                assert notice_text(app) == ""
                assert app.deck.focused_row.row_id == focused
                assert not any(row.approved for row in session.view.rows)
        run(interact())


def test_workset_keeps_cursor_visible_when_wrapped_detail_grows(tmp_path, monkeypatch):
    # The last row's long paths wrap, so focusing it grows the detail and shrinks the table.
    long_name = "zz_" + "very_long_segment_" * 4
    engine = make_engine(tmp_path, monkeypatch, [
        *[(f"t{index:02}", "push-only", b"repo", b"live", "") for index in range(30)],
        (long_name, "push-only", b"repo", b"live", ""),
    ])
    with engine.open_sync_session(engine.resolve_sync_scope([]), preview=True) as session:
        app = SyncDeckApp(CommandDeck(session, use_color=False))

        async def interact():
            async with app.run_test(size=(80, 30)) as pilot:
                table = app.query_one(WorksetTable)
                await pilot.pause()
                for _ in range(30):
                    await pilot.press("down")
                await pilot.pause()
                assert table.cursor_row == 30
                visible = [table.render_line(y).text for y in range(table.size.height)]
                assert any("main:app.zz_" in line for line in visible)
        run(interact())


@pytest.mark.parametrize("open_menu", [("r",), ("enter", "v")], ids=["resolution-menu", "full-view-menu"])
def test_retry_key_is_refused_while_a_menu_is_open(tmp_path, monkeypatch, open_menu):
    from textual.widgets import OptionList
    from tests.engine.test_sync_both_convergence import established

    engine = established(tmp_path, monkeypatch)
    # A merge conflict gives the review two diffs, so v opens the Full View menu.
    context = b"".join(b"ctx-%d\n" % index for index in range(10))
    (tmp_path / "repo/packages/app/unit").write_bytes(b"repository\n" + context)
    (tmp_path / "live/unit").write_bytes(b"conflict\n" + context)
    with engine.open_sync_session(engine.resolve_sync_scope([]), preview=True) as session:
        app = SyncDeckApp(CommandDeck(session, use_color=False))

        async def interact():
            async with app.run_test(size=(80, 40)) as pilot:
                menu = app.query_one("#resolution", OptionList)
                for key in open_menu:
                    await pilot.press(key)
                    await wait_until_idle(app, pilot)
                assert menu.display
                revision = session.view.revision
                await pilot.press("t")
                await wait_until_idle(app, pilot)
                assert session.view.revision == revision
                assert menu.display
        run(interact())


def test_unexpected_background_error_stops_the_deck(tmp_path, monkeypatch):
    engine = make_engine(tmp_path, monkeypatch, [("unit", "push-only", b"repo", b"live", "")])

    def provider_bug(*args, **kwargs):
        raise RuntimeError("provider bug")

    # Select runs this session command on the materialization lane.
    monkeypatch.setattr(sync_deck, "set_selected", provider_bug)
    with engine.open_sync_session(engine.resolve_sync_scope([]), preview=True) as session:
        app = SyncDeckApp(CommandDeck(session, use_color=False))
        running_after_failure = []

        async def interact():
            async with app.run_test() as pilot:
                await pilot.press("x")
                for _ in range(200):
                    if not app.is_running:
                        break
                    await asyncio.sleep(.01)
                running_after_failure.append(app.is_running)

        # Textual re-raises the app's unexpected exception when the pilot closes.
        with pytest.raises(RuntimeError, match="provider bug"):
            run(interact())
        assert running_after_failure == [False]
        assert app.return_code == 1


def test_review_refresh_keeps_search_hints_true_to_the_search(tmp_path, monkeypatch):
    repo, live = search_repo_and_live()
    engine = make_engine(tmp_path, monkeypatch, [("one", "push-only", repo, live, "")])
    with engine.open_sync_session(engine.resolve_sync_scope([]), preview=True) as session:
        app = SyncDeckApp(CommandDeck(session, use_color=False))

        async def interact():
            async with app.run_test(size=(80, 24)) as pilot:
                await pilot.press("enter")
                await wait_until_idle(app, pilot)
                await pilot.press("slash", "v", "i", "e", "w", "enter")
                await pilot.pause()
                assert "Esc clear" in help_text(app)
                # x refreshes the review after its work.
                await pilot.press("x")
                await wait_until_idle(app, pilot)
                assert session.view.rows[0].approved
                # Whatever the refresh did to the search, Esc must do what the help line says:
                # "Esc clear" stays in the review, "Esc return" leaves it.
                advertises_search = "Esc clear" in help_text(app)
                assert advertises_search or "Esc return" in help_text(app)
                await pilot.press("escape")
                await pilot.pause()
                assert title_text(app).startswith(":: Proposal Review") == advertises_search
        run(interact())


def test_detail_and_review_show_a_diagnostic_from_outside_escaped(tmp_path, monkeypatch):
    """A diagnostic carries OS errors and file names, so a terminal control sequence in one is shown, never sent."""
    engine = make_engine(tmp_path, monkeypatch, [("unit", "both", b"repo", b"live", "")])
    original_read_endpoint = sync_observation._read_endpoint

    def read_endpoint_failing_on_live(path, *, repository, **kwargs):
        if not repository:
            raise SyncPathError("unsupported-entry", "bad\x1b]0;PWN\x1b\\ \x1b[2Jend\nsecond line")
        return original_read_endpoint(path, repository=repository, **kwargs)

    monkeypatch.setattr(sync_observation, "_read_endpoint", read_endpoint_failing_on_live)
    with engine.open_pull_session(engine.resolve_sync_scope(), preview=True) as session:
        app = SyncDeckApp(CommandDeck(session, use_color=False))

        async def interact():
            async with app.run_test(size=(110, 30)):
                detail = "\n".join(detail_lines(app))
                assert "bad\\u001b]0;PWN\\u001b\\ \\u001b[2Jend" in detail
                assert "\x1b" not in detail
                # The Review text lists the same diagnostics as the detail pane.
                review = app.deck.review_text()
                assert "bad\\u001b]0;PWN\\u001b\\ \\u001b[2Jend" in review
                assert "\x1b" not in review
        run(interact())


def test_detail_shows_a_guard_reason_from_the_command_output_escaped(tmp_path, monkeypatch):
    guard = "[targets.unit.hooks]\nguard_pull = \"printf 'off\\\\033[31m' >&2; exit 100\""
    engine = make_engine(tmp_path, monkeypatch, [("unit", "both", b"repo", b"live", guard)])
    with engine.open_pull_session(engine.resolve_sync_scope(), preview=True) as session:
        app = SyncDeckApp(CommandDeck(session, use_color=False))

        async def interact():
            async with app.run_test(size=(110, 24)):
                facts = detail_facts(app)
                assert "Guard skipped: guard_pull exited 100 (off\\u001b[31m)" in facts
                assert all("\x1b" not in fact for fact in facts)
        run(interact())


def assert_shown_escaped(text, expected):
    assert expected in text
    # Tab is layout and newline separates lines; any other unprintable character is raw content.
    assert all(character.isprintable() or character in "\t\n" for character in text), repr(text)


@pytest.mark.parametrize(("content", "shown"), [
    pytest.param(b"pre\x01\x0e\x0fpost\n", "pre\\u0001\\u000e\\u000fpost", id="c0"),
    pytest.param(b"pre\x7fpost\n", "pre\\u007fpost", id="del"),
    pytest.param("pre\u009bpost\n".encode(), "pre\\u009bpost", id="c1"),
    # rich's ANSI decoder would swallow the sequence and show only "xy".
    pytest.param(b"x\x1b[31my\n", "x\\u001b[31my", id="csi-shown-not-swallowed"),
    # rich's ANSI decoder keeps only the text after the last carriage return, blanking the line.
    pytest.param(b"crlf-line\r\n", "crlf-line\\u000d", id="crlf"),
    # Printable text, backslashes and non-ASCII included, shows as written next to an escaped control.
    pytest.param("as\\written café\x01\n".encode(), "as\\written café\\u0001", id="printable-kept"),
])
def test_review_shows_file_content_control_characters_escaped(tmp_path, monkeypatch, content, shown):
    """Diff lines are a tracked file's content, so a control character in it is shown, never sent to the terminal."""
    engine = make_engine(tmp_path, monkeypatch, [("one", "push-only", content, b"live\n", "")])
    with engine.open_sync_session(engine.resolve_sync_scope([]), preview=True) as session:
        app = SyncDeckApp(CommandDeck(session, use_color=False))

        async def interact():
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.press("enter")
                await pilot.pause()
                assert_shown_escaped(review_text(app), f"+{shown}")
                # Copy and search work on the same lines as the pane.
                assert_shown_escaped(app.deck.review_text(), f"+{shown}")
        run(interact())


def test_review_keeps_tab_in_file_content_as_layout(tmp_path, monkeypatch):
    engine = make_engine(tmp_path, monkeypatch, [("one", "push-only", b"tab\there\x01\n", b"live\n", "")])
    with engine.open_sync_session(engine.resolve_sync_scope([]), preview=True) as session:
        app = SyncDeckApp(CommandDeck(session, use_color=False))

        async def interact():
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.press("enter")
                await pilot.pause()
                assert_shown_escaped(app.deck.review_text(), "+tab\there\\u0001")
                pane = review_text(app)
                # Only that the tab is not escaped: rich wraps a grid cell holding a tab early (a separate issue).
                assert "\\u0009" not in pane and "+tab" in pane
                assert_shown_escaped(pane, "here\\u0001")
        run(interact())


def conflict_block(text):
    lines = text.splitlines()
    start = next(index for index, line in enumerate(lines) if "<<<<<<< repository" in line)
    end = next(index for index, line in enumerate(lines) if ">>>>>>> " in line)
    return "\n".join(lines[start:end + 1])


def test_merge_conflict_excerpt_shows_control_characters_escaped(tmp_path, monkeypatch):
    from tests.engine.test_sync_both_convergence import established

    engine = established(tmp_path, monkeypatch)
    (tmp_path / "repo/packages/app/unit").write_bytes(b"repo\x01\x1b[31mside\n")
    (tmp_path / "live/unit").write_bytes("live\x7f\u009bside\n".encode())
    with engine.open_sync_session(engine.resolve_sync_scope([]), preview=True) as session:
        app = SyncDeckApp(CommandDeck(session, use_color=False))

        async def interact():
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.press("enter")
                await pilot.pause()
                # Only the excerpt, since the Drift diff below repeats the same content.
                for block in (conflict_block(review_text(app)), conflict_block(app.deck.review_text())):
                    assert_shown_escaped(block, "repo\\u0001\\u001b[31mside")
                    assert_shown_escaped(block, "live\\u007f\\u009bside")
        run(interact())
