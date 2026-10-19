"""Paths dotman did not read from an authored field never reach the terminal raw (issue #295).

An authored target `source` and `path` in package.toml and a repo `path` in config.toml
are checked at load (tests/cli/test_unprintable_names.py), so they print as written.
They are checked as written, so what `~`, `$VAR` and `{{ var }}` expand to is outside
text too. These paths come from outside the manifest and are escaped where they enter output:

- A live file or directory whose name holds a control character, such as a child of
  a directory target.
- A target or repo path that expanded to a value holding one: a Jinja var in a target
  `path`, an environment variable in a repo `path`, and every path under such a repo root.
- The config file's own location, which the user chose.

Escaping is `\\uNNNN` per unprintable character, newline and tab included: a path is
shown inside one-line rows and fields, so it cannot start a new line.

`add` copies a live file's name into the manifest it writes, so it refuses a file
whose path is unprintable instead of writing a manifest that could no longer load.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dotman.cli import main
from tests.helpers import write_named_manager_config

REPO_NAME = "fixture"

# A name whose line breaks fake a finished row and the summary line of a run.
FORGED_ROW_NAME = "evil\n      ok\n:: completed — approved: 0"
SHOWN_FORGED_ROW_NAME = "evil\\u000a      ok\\u000a:: completed — approved: 0"


def run_cli(config_path: Path, capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    """Run one dotman command; return the exit code, stdout and stderr."""
    capsys.readouterr()
    exit_code = main(["--config", str(config_path), "--no-confirm", *argv])
    captured = capsys.readouterr()
    return exit_code, captured.out, captured.err


def write_repo_with_directory_target(tmp_path: Path) -> Path:
    """Write a repo whose package `app` has the directory target `tree` at `~/tree`; return the config path.

    The repo holds `a.conf`, and the live directory holds a different `a.conf`
    plus a child named `FORGED_ROW_NAME` that the repo has never heard of.
    """
    repo_root = tmp_path / "repo"
    (repo_root / "profiles").mkdir(parents=True)
    (repo_root / "profiles" / "default.toml").write_text("", encoding="utf-8")
    package_root = repo_root / "packages" / "app"
    (package_root / "files" / "tree").mkdir(parents=True)
    (package_root / "files" / "tree" / "a.conf").write_text("repo\n", encoding="utf-8")
    (package_root / "package.toml").write_text(
        '[targets.tree]\ntype = "directory"\nsource = "files/tree"\npath = "~/tree"\n', encoding="utf-8"
    )
    live_directory = Path.home() / "tree"
    live_directory.mkdir()
    (live_directory / "a.conf").write_text("live\n", encoding="utf-8")
    (live_directory / FORGED_ROW_NAME).write_text("live-only\n", encoding="utf-8")
    return write_named_manager_config(tmp_path, {REPO_NAME: repo_root})


@pytest.mark.parametrize("command", ["push", "pull"])
def test_a_live_child_name_with_line_breaks_cannot_forge_a_row(
    command: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config_path = write_repo_with_directory_target(tmp_path)
    track_code, track_stdout, track_stderr = run_cli(config_path, capsys, "track", f"{REPO_NAME}:app@default")
    assert track_code == 0, track_stdout + track_stderr

    _code, stdout, stderr = run_cli(config_path, capsys, command, f"{REPO_NAME}:app")

    output = stdout + stderr
    assert SHOWN_FORGED_ROW_NAME in output
    # Only a real run prints the summary line, so a forged one proves the name left its row.
    assert [line for line in output.splitlines() if line.startswith(":: completed — approved: 0")] == []


# A value a template or the environment supplies; its line break fakes the start of a row.
EXPANDED_VALUE = "bad\x1b[31m\nFORGED: expanded"
SHOWN_EXPANDED_VALUE = "bad\\u001b[31m\\u000aFORGED: expanded"


def write_repo_with_templated_target_path(tmp_path: Path) -> Path:
    """Write a repo whose target `path` is `~/{{ vars.x }}` and whose var `x` is `EXPANDED_VALUE`."""
    repo_root = tmp_path / "repo"
    (repo_root / "profiles").mkdir(parents=True)
    (repo_root / "profiles" / "default.toml").write_text("", encoding="utf-8")
    package_root = repo_root / "packages" / "app"
    (package_root / "files").mkdir(parents=True)
    (package_root / "files" / "a.conf").write_text("a\n", encoding="utf-8")
    (package_root / "package.toml").write_text(
        f'[vars]\nx = {json.dumps(EXPANDED_VALUE)}\n[targets.conf]\nsource = "files/a.conf"\npath = "~/{{{{ vars.x }}}}"\n',
        encoding="utf-8",
    )
    return write_named_manager_config(tmp_path, {REPO_NAME: repo_root})


def write_config_with_expanded_repo_path(tmp_path: Path) -> Path:
    """Write a config whose one repo `path` is `$EVIL/repo2`, with `EVIL` set to `EXPANDED_VALUE` by the caller."""
    config_path = tmp_path / "config.toml"
    config_path.write_text(f'[repos.{REPO_NAME}]\npath = "$EVIL/repo2"\norder = 10\n', encoding="utf-8")
    return config_path


def write_repo_under_expanded_root(tmp_path: Path) -> Path:
    """Write a repo at `$EVIL/repo2` (a real directory once `EVIL` is `EXPANDED_VALUE`) with a var `y`, and a live `~/live.conf`.

    Every path inside the repo, the package manifest included, is under that hostile root.
    """
    repo_root = tmp_path / EXPANDED_VALUE / "repo2"  # the directory `write_config_with_expanded_repo_path` points at
    (repo_root / "profiles").mkdir(parents=True)
    (repo_root / "profiles" / "default.toml").write_text("", encoding="utf-8")
    package_root = repo_root / "packages" / "app"
    (package_root / "files").mkdir(parents=True)
    (package_root / "files" / "a.conf").write_text("a\n", encoding="utf-8")
    (package_root / "package.toml").write_text(
        '[vars]\ny = "1"\n[targets.conf]\nsource = "files/a.conf"\npath = "~/a.conf"\n', encoding="utf-8"
    )
    (Path.home() / "live.conf").write_text("x\n", encoding="utf-8")
    return write_config_with_expanded_repo_path(tmp_path)


@pytest.mark.parametrize(
    ("write_config", "setup_argv", "argv"),
    [
        pytest.param(
            write_repo_with_templated_target_path,
            ["track", f"{REPO_NAME}:app@default"],
            ["info", "tracked", f"{REPO_NAME}:app"],
            id="info-tracked-templated-target-path",
        ),
        pytest.param(write_config_with_expanded_repo_path, None, ["list", "repo"], id="list-repo-expanded-repo-path"),
        # The provenance line of a variable names the manifest that set it.
        pytest.param(
            write_repo_under_expanded_root,
            ["track", f"{REPO_NAME}:app@default"],
            ["info", "var", "y"],
            id="info-var-provenance-under-expanded-repo-root",
        ),
        pytest.param(
            write_repo_under_expanded_root,
            None,
            ["add", "~/live.conf", f"{REPO_NAME}:app"],
            id="add-manifest-line-under-expanded-repo-root",
        ),
    ],
)
def test_a_path_that_expanded_to_unprintable_characters_is_shown_escaped_on_one_line(
    write_config, setup_argv: list[str] | None, argv: list[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The authored text is printable, so it loads; the expansion is the user's own environment, and is not."""
    monkeypatch.setenv("EVIL", EXPANDED_VALUE)
    config_path = write_config(tmp_path)
    if setup_argv is not None:
        setup_code, setup_stdout, setup_stderr = run_cli(config_path, capsys, *setup_argv)
        assert setup_code == 0, setup_stdout + setup_stderr

    code, stdout, stderr = run_cli(config_path, capsys, *argv)

    output = stdout + stderr
    assert code == 0, output
    assert SHOWN_EXPANDED_VALUE in output
    assert "\x1b" not in output
    assert [line for line in output.splitlines() if line.startswith("FORGED:")] == []


def test_the_config_location_in_doctor_cannot_forge_a_row(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config_dir = tmp_path / "cfg\nforged: config\x1b[31m"
    config_dir.mkdir()
    config_path = config_dir / "config.toml"
    config_path.write_text(
        f"[repos.{REPO_NAME}]\npath = {json.dumps(str(tmp_path / 'missing'))}\norder = 10\n", encoding="utf-8"
    )

    code, stdout, stderr = run_cli(config_path, capsys, "doctor")

    output = stdout + stderr
    assert code == 1, output  # the missing repo is a failed check
    assert "config: " + str(tmp_path) + "/cfg\\u000aforged: config\\u001b[31m/config.toml" in output
    assert "\x1b" not in output
    assert [line for line in output.splitlines() if line.startswith("forged:")] == []


@pytest.mark.parametrize(
    "live_relative_path",
    [
        pytest.param(".config/app/we\x1bird.conf", id="file-name"),
        pytest.param(".config/we\x1bird/app.conf", id="parent-directory-name"),
    ],
)
def test_add_refuses_a_path_with_unprintable_characters_and_writes_nothing(
    live_relative_path: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The manifest `add` would write holds that path, and a path that is unprintable fails to load."""
    live_path = Path.home() / live_relative_path
    live_path.parent.mkdir(parents=True)
    live_path.write_text("x\n", encoding="utf-8")
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    config_path = write_named_manager_config(tmp_path, {REPO_NAME: repo_root})

    code, stdout, stderr = run_cli(config_path, capsys, "add", str(live_path), f"{REPO_NAME}:newpkg")

    assert code == 2, stdout + stderr
    assert list(repo_root.rglob("*")) == []
    assert "we\\u001bird" in stderr, "the error must tell which name is the problem"
    # The user can rename the file or track a directory above it, and the error should say so.
    assert [line for line in stderr.splitlines() if line.strip().startswith("hint:") and "renam" in line.lower()], stderr
    assert "\x1b" not in stdout + stderr, "raw ESC reached the terminal"
