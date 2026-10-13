from __future__ import annotations

from dotman.cli_emit import render_hook_command_lines


def test_render_hook_command_lines_shows_elevation_badge_without_none() -> None:
    assert render_hook_command_lines("echo hi", command_count=1, index=1, elevation="intercept") == [
        "      [intercept] echo hi"
    ]
    assert render_hook_command_lines("echo hi", command_count=1, index=1, elevation="none") == ["      echo hi"]


def test_render_hook_command_lines_shows_tty_before_elevation_badge() -> None:
    assert render_hook_command_lines("nvim file", command_count=1, index=1, io="tty", elevation="root") == [
        "      [tty] [root] nvim file"
    ]
