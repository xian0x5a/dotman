"""Paths an author or a live file name can make unprintable never reach the terminal raw (issue #295).

Two sources of such paths:

- A target `path` authored in package.toml, shown by the `info` commands.
- A live file whose name holds a control character, which `add` copies into a
  manifest it writes and then prints.

Escaping is `\\uNNNN` per unprintable character. A manifest dotman writes must
still load: TOML needs those characters escaped, not raw.
"""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

import pytest

from dotman.cli import main
from tests.helpers import write_named_manager_config

REPO_NAME = "fixture"

AUTHORED_PATH = "~/dir\x1b[31m/a\xa0b\u202e"
SHOWN_AUTHORED_PATH_TAIL = "dir\\u001b[31m/a\\u00a0b\\u202e"
RAW_CHARACTERS = ("\x1b", "\xa0", "\u202e")

ESCAPE_NAMED_FILE = "we\x1bird.conf"
SHOWN_ESCAPE_NAMED_FILE = "we\\u001bird.conf"


def run_cli(config_path: Path, capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    """Run one dotman command; return the exit code, stdout and stderr."""
    capsys.readouterr()
    exit_code = main(["--config", str(config_path), "--no-confirm", *argv])
    captured = capsys.readouterr()
    return exit_code, captured.out, captured.err


def write_repo_with_authored_path(tmp_path: Path) -> Path:
    """Write a repo whose package `app` has one target at `AUTHORED_PATH`; return the config path."""
    repo_root = tmp_path / "repo"
    (repo_root / "profiles").mkdir(parents=True)
    (repo_root / "profiles" / "default.toml").write_text("", encoding="utf-8")
    package_root = repo_root / "packages" / "app"
    (package_root / "files").mkdir(parents=True)
    (package_root / "files" / "a.conf").write_text("a\n", encoding="utf-8")
    # `json.dumps` writes the characters as `\uNNNN`, which a TOML basic string reads back.
    (package_root / "package.toml").write_text(
        f'[targets.conf]\nsource = "files/a.conf"\npath = {json.dumps(AUTHORED_PATH)}\n', encoding="utf-8"
    )
    return write_named_manager_config(tmp_path, {REPO_NAME: repo_root})


@pytest.mark.parametrize("command", ["tracked", "trackable"])
def test_info_shows_an_authored_target_path_escaped(
    command: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config_path = write_repo_with_authored_path(tmp_path)
    track_code, track_stdout, track_stderr = run_cli(config_path, capsys, "track", f"{REPO_NAME}:app@default")
    assert track_code == 0, track_stdout + track_stderr

    code, stdout, stderr = run_cli(config_path, capsys, "info", command, f"{REPO_NAME}:app")

    assert code == 0, stdout + stderr
    assert SHOWN_AUTHORED_PATH_TAIL in stdout
    for raw_character in RAW_CHARACTERS:
        assert raw_character not in stdout + stderr, f"raw U+{ord(raw_character):04X} reached the terminal"


def add_escape_named_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> tuple[Path, int, str, str]:
    """Run `dotman add` for a live file named `ESCAPE_NAMED_FILE` into a new package.

    Returns the config path, exit code, stdout and stderr. The repo is `tmp_path / "repo"`.
    """
    live_path = Path.home() / ".config" / "app" / ESCAPE_NAMED_FILE
    live_path.parent.mkdir(parents=True)
    live_path.write_text("x\n", encoding="utf-8")
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    config_path = write_named_manager_config(tmp_path, {REPO_NAME: repo_root})
    code, stdout, stderr = run_cli(config_path, capsys, "add", str(live_path), f"{REPO_NAME}:newpkg")
    return config_path, code, stdout, stderr


def test_add_of_an_escape_named_file_writes_a_manifest_dotman_loads_again(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config_path, code, stdout, stderr = add_escape_named_file(tmp_path, capsys)
    assert code == 0, stdout + stderr

    manifest = tomllib.loads((tmp_path / "repo" / "packages" / "newpkg" / "package.toml").read_text(encoding="utf-8"))
    (target,) = manifest["targets"].values()
    assert target["path"] == f"~/.config/app/{ESCAPE_NAMED_FILE}"
    assert target["source"] == f"files/config/app/{ESCAPE_NAMED_FILE}"
    # Listing loads every package manifest in the repo.
    list_code, list_stdout, list_stderr = run_cli(config_path, capsys, "list", "trackables")
    assert list_code == 0, list_stdout + list_stderr
    assert f"{REPO_NAME}:newpkg" in list_stdout


def test_add_result_shows_the_target_source_and_path_escaped(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _config_path, code, stdout, stderr = add_escape_named_file(tmp_path, capsys)

    assert code == 0, stdout + stderr
    for label in ("source", "path"):
        assert re.search(rf"^\s*{label}:\s+\S*{re.escape(SHOWN_ESCAPE_NAMED_FILE)}$", stdout, re.MULTILINE), stdout
    assert "\x1b" not in stdout + stderr, "raw ESC reached the terminal"
