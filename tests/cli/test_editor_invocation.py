from __future__ import annotations

import shlex
from pathlib import Path

import pytest

from dotman.cli import main
from dotman.reconcile import run_basic_reconcile


def _write_recording_editor(tmp_path: Path) -> tuple[Path, Path]:
    """An editor that records the arguments it received, one per line."""
    record_path = tmp_path / "editor-arguments"
    editor_path = tmp_path / "recording-editor"
    editor_path.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$@\" > {shlex.quote(str(record_path))}\n")
    editor_path.chmod(0o755)
    return editor_path, record_path


@pytest.mark.parametrize(
    ("setting", "expected_leading_arguments"),
    [
        # Git documents GIT_EDITOR as shell text, so "$@" places the files.
        ('{editor} -t "$@"', ["-t"]),
        ('{editor} -d -t "$@"', ["-t"]),
        ('{editor} "$1"', []),
        # Shell syntax without "$@": Git appends "$@" for the files.
        ("FOO=1 {editor} -d -t", ["-t"]),
        # Plain words run without a shell.
        ("{editor} -d -t", ["-t"]),
    ],
)
def test_edit_config_hands_the_real_path_to_the_configured_editor(
    tmp_path: Path, monkeypatch, setting: str, expected_leading_arguments: list[str]
) -> None:
    editor_path, record_path = _write_recording_editor(tmp_path)
    config_path = tmp_path / "config.toml"
    config_path.write_text("", encoding="utf-8")
    monkeypatch.setenv("GIT_EDITOR", setting.format(editor=shlex.quote(str(editor_path))))

    assert main(["--config", str(config_path), "edit", "config"]) == 0

    assert record_path.read_text().splitlines() == [*expected_leading_arguments, str(config_path.resolve())]


@pytest.mark.parametrize(
    "run_template",
    ['{editor} -d -t "$@"', "{editor} -d -t"],
    ids=["shell", "direct"],
)
def test_reconcile_keeps_diff_flag_of_an_explicit_editor(tmp_path: Path, run_template: str) -> None:
    # A custom Sync Editor's `run` is the user's own command; only an editor
    # found through the lookup chain loses `-d`.
    editor_path, record_path = _write_recording_editor(tmp_path)
    repo_path = tmp_path / "repo-file"
    live_path = tmp_path / "live-file"
    repo_path.write_text("repo\n")
    live_path.write_text("live\n")

    assert run_basic_reconcile(
        repo_path=str(repo_path),
        live_path=str(live_path),
        additional_sources=[],
        editor=run_template.format(editor=shlex.quote(str(editor_path))),
        editor_io="pipe",
        approve_write=True,
    ) == 0

    diff_flag, flag, staged_source = record_path.read_text().splitlines()
    assert (diff_flag, flag) == ("-d", "-t")
    assert Path(staged_source) != repo_path
