"""Repo names in the manager config must be addressable and stay inside their directories (issue #303).

`[repos.<name>]` keys are validated when the config loads. A name that contains a
selector delimiter cannot be written as `repo:package`, and a name that contains a
path separator or is `.`/`..` points outside `repos/<name>/`. Both are usage errors
(exit 2) that name config.toml and the key. Unprintable characters are a separate
rule (issue #295, tests/cli/test_unprintable_names.py).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dotman.cli import main
from tests.helpers import EXAMPLE_REPO


def write_config(tmp_path: Path, *repo_names: str) -> Path:
    """Write a manager config with one `[repos.<name>]` per name, all backed by the example repo."""
    # `json.dumps` writes a quoted TOML key, so names with `.`, `:` or `/` stay one key.
    tables = [
        f"[repos.{json.dumps(name)}]\npath = {json.dumps(str(EXAMPLE_REPO))}\norder = {index * 10}\n"
        for index, name in enumerate(repo_names, start=1)
    ]
    config_path = tmp_path / "config.toml"
    config_path.write_text("\n".join(tables), encoding="utf-8")
    return config_path


def run_list_repo(config_path: Path, capsys: pytest.CaptureFixture[str]) -> tuple[int, str, str]:
    """Run `dotman list repo`, which loads the config and prints the repos; return the exit code, stdout and stderr."""
    try:
        exit_code = main(["--config", str(config_path), "--no-confirm", "list", "repo"])
    except SystemExit as exit_info:  # argparse exits instead of returning
        exit_code = exit_info.code
    captured = capsys.readouterr()
    return exit_code, captured.out, captured.err


@pytest.mark.parametrize(
    "name",
    [
        pytest.param("a\\b", id="backslash"),
        pytest.param("a:b", id="colon"),
        pytest.param("a@b", id="at"),
        pytest.param("a<b", id="less-than"),
        pytest.param("a>b", id="greater-than"),
        pytest.param("a.b", id="dot"),
        pytest.param("a/b", id="slash"),
        pytest.param("", id="empty"),
        pytest.param("  ", id="whitespace-only"),
        pytest.param(".", id="current-directory"),
        pytest.param("..", id="parent-directory"),
    ],
)
def test_repo_name_with_selector_delimiter_or_path_part_is_rejected(
    name: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config_path = write_config(tmp_path, name)

    exit_code, stdout, stderr = run_list_repo(config_path, capsys)

    output = stdout + stderr
    assert exit_code == 2, output
    assert "config.toml" in output
    # A blank name has nothing visible to look for; stripping it makes that check vacuous.
    # A backslash may be shown doubled, the injective escaped form of an authored name.
    assert name.strip() in output or name.replace("\\", "\\\\") in output


def test_repo_name_that_selectors_cannot_address_rejects_the_whole_config(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`a:b:pkg@p` parses as repo `a`, so a repo named `a:b` would load but never be selectable."""
    config_path = write_config(tmp_path, "main", "a:b")

    exit_code, stdout, stderr = run_list_repo(config_path, capsys)

    assert exit_code == 2, stdout + stderr
    assert stdout == "", "the valid sibling repo must not be listed from a rejected config"
    assert "config.toml" in stderr
    assert "a:b" in stderr


@pytest.mark.parametrize("name", ["main", "work-laptop", "dot_files"])
def test_ordinary_repo_names_load(name: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # Non-ASCII names such as `café` are covered by test_unprintable_names.py.
    config_path = write_config(tmp_path, name)

    exit_code, stdout, stderr = run_list_repo(config_path, capsys)

    assert exit_code == 0, stdout + stderr
    assert name in stdout
