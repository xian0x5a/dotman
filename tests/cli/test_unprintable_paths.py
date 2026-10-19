"""Paths an author or a live file name can make unprintable never reach the terminal raw (issue #295).

Three sources of such paths:

- A target `path` authored in package.toml, shown by `info` and by `push`.
- A live file whose name holds a control character, which `add` copies into a
  manifest it writes and then prints.
- The config file and repo directories, whose names the user chose.

Escaping is `\\uNNNN` per unprintable character, newline and tab included: a path is
shown inside one-line rows and fields, so it cannot start a new line. A manifest
dotman writes must still load: TOML needs those characters escaped, not raw.
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


def write_repo_with_authored_path(tmp_path: Path, authored_path: str = AUTHORED_PATH) -> Path:
    """Write a repo whose package `app` has one target at `authored_path`; return the config path."""
    repo_root = tmp_path / "repo"
    (repo_root / "profiles").mkdir(parents=True)
    (repo_root / "profiles" / "default.toml").write_text("", encoding="utf-8")
    package_root = repo_root / "packages" / "app"
    (package_root / "files").mkdir(parents=True)
    (package_root / "files" / "a.conf").write_text("a\n", encoding="utf-8")
    # `json.dumps` writes the characters as `\uNNNN`, which a TOML basic string reads back.
    (package_root / "package.toml").write_text(
        f'[targets.conf]\nsource = "files/a.conf"\npath = {json.dumps(authored_path)}\n', encoding="utf-8"
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


# A path whose line breaks fake a finished row and the summary line of a run.
FORGED_ROW_PATH = "~/evil\n      ok\n:: completed \u2014 approved: 0"
SHOWN_FORGED_ROW_PATH = "evil\\u000a      ok\\u000a:: completed \u2014 approved: 0"


@pytest.mark.parametrize(
    "argv",
    [
        pytest.param(["push", f"{REPO_NAME}:app"], id="push"),
        pytest.param(["info", "tracked", f"{REPO_NAME}:app"], id="info-tracked"),
        pytest.param(["info", "trackable", f"{REPO_NAME}:app"], id="info-trackable"),
    ],
)
def test_an_authored_path_with_line_breaks_cannot_forge_a_row(
    argv: list[str], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config_path = write_repo_with_authored_path(tmp_path, FORGED_ROW_PATH)
    track_code, track_stdout, track_stderr = run_cli(config_path, capsys, "track", f"{REPO_NAME}:app@default")
    assert track_code == 0, track_stdout + track_stderr

    code, stdout, stderr = run_cli(config_path, capsys, *argv)

    assert code == 0, stdout + stderr
    assert SHOWN_FORGED_ROW_PATH in stdout
    # A real push step prints its own `      ok` status line, so only the forged
    # summary line, which no real run prints, proves the path stayed on its row.
    lines = stdout.splitlines()
    assert [line for line in lines if line.startswith(":: completed \u2014 approved: 0")] == []


def write_config_with_hostile_paths(tmp_path: Path) -> Path:
    """Write a config whose own directory and whose one repo's missing directory have hostile names."""
    config_dir = tmp_path / "cfg\nforged: config\x1b[31m"
    config_dir.mkdir()
    missing_repo = tmp_path / "missing\nforged: repo\x1b[31m"
    config_path = config_dir / "config.toml"
    config_path.write_text(
        f"[repos.{REPO_NAME}]\npath = {json.dumps(str(missing_repo))}\norder = 10\n", encoding="utf-8"
    )
    return config_path


@pytest.mark.parametrize(
    ("argv", "expected_exit_code", "shown_names"),
    [
        pytest.param(["list", "repo"], 0, ["missing\\u000aforged: repo\\u001b[31m"], id="list-repo"),
        # The config path is the `config:` line; the missing repo is a failed check, so the exit code is 1.
        pytest.param(
            ["doctor"], 1, ["cfg\\u000aforged: config\\u001b[31m", "missing\\u000aforged: repo\\u001b[31m"], id="doctor"
        ),
    ],
)
def test_config_and_repo_paths_in_listings_cannot_forge_a_row(
    argv: list[str], expected_exit_code: int, shown_names: list[str], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config_path = write_config_with_hostile_paths(tmp_path)

    code, stdout, stderr = run_cli(config_path, capsys, *argv)

    output = stdout + stderr
    assert code == expected_exit_code, output
    for shown_name in shown_names:
        assert shown_name in output
    assert "\x1b" not in output
    assert [line for line in output.splitlines() if line.startswith("forged:")] == []


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
