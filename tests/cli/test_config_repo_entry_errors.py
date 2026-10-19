"""A bad `[repos.<name>]` entry is reported against config.toml and the repo it came from (issue #295).

Every check in the manager config's repo loop is a usage error (exit 2) that
names the config file, so the user knows which file to open, and the repo
involved. Bad repo names and unprintable state keys are covered in
test_repo_names.py and test_unprintable_names.py.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dotman.cli import main

REPO_PATH = "/nonexistent/repo"  # loading the config only expands the path, it never reads it


def repo_table(name: str, **fields: object) -> str:
    """One `[repos.<name>]` table; `json.dumps` writes values as TOML strings and integers."""
    lines = [f"[repos.{name}]", *(f"{key} = {json.dumps(value)}" for key, value in fields.items())]
    return "\n".join(lines) + "\n"


# (config text, the repos the error is about)
BAD_REPO_ENTRIES = [
    pytest.param("[repos]\nmain = 5\n", ("main",), id="entry-is-not-a-table"),
    pytest.param(repo_table("main", order=10), ("main",), id="path-missing"),
    pytest.param(repo_table("main", path=REPO_PATH), ("main",), id="order-missing"),
    pytest.param(
        repo_table("one", path=REPO_PATH, order=10) + repo_table("two", path=REPO_PATH, order=10),
        ("one", "two"),
        id="duplicate-order",
    ),
    pytest.param(repo_table("main", path=REPO_PATH, order=10, state_key=5), ("main",), id="state-key-is-not-a-string"),
    pytest.param(
        repo_table("one", path=REPO_PATH, order=10, state_key="shared")
        + repo_table("two", path=REPO_PATH, order=20, state_key="shared"),
        ("one", "two"),
        id="duplicate-state-key",
    ),
    pytest.param(
        repo_table("main", path=REPO_PATH, order=10, state_path="/elsewhere"), ("main",), id="unsupported-state-path"
    ),
]


@pytest.mark.parametrize(("config_text", "repo_names"), BAD_REPO_ENTRIES)
def test_a_bad_repo_entry_is_reported_against_config_toml_and_its_repo(
    config_text: str, repo_names: tuple[str, ...], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(config_text, encoding="utf-8")

    exit_code = main(["--config", str(config_path), "--no-confirm", "list", "repo"])

    captured = capsys.readouterr()
    output = captured.out + captured.err
    assert exit_code == 2, output
    assert "config.toml" in output
    for repo_name in repo_names:
        assert repo_name in output
