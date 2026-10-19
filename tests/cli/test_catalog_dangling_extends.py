"""Browse commands skip an untracked package whose `extends` parent is missing; a cycle stays a repo error."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from dotman.cli import main

from tests.helpers import write_named_manager_config

BROKEN_REPO = "other"
BROKEN_PACKAGE = "child"
MISSING_PARENT = "gone"
# Every package carries this description so one search query matches all of them.
SEARCH_QUERY = "catalog probe"

BROWSE_COMMANDS = [
    pytest.param(["list", "trackables"], id="list-trackables"),
    pytest.param(["search", SEARCH_QUERY], id="search"),
]


def _write_package(repo_root: Path, package_id: str, extends: list[str] | None = None) -> None:
    package_dir = repo_root / "packages" / package_id
    package_dir.mkdir(parents=True)
    lines = [f'description = "{SEARCH_QUERY}"']
    if extends is not None:
        lines.append(f"extends = {json.dumps(extends)}")
    (package_dir / "package.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_config(tmp_path: Path, other_packages: dict[str, list[str] | None]) -> Path:
    """`main` and `other` each have a healthy package; nothing is tracked."""
    roots = {"main": tmp_path / "main-repo", BROKEN_REPO: tmp_path / "other-repo"}
    for root in roots.values():
        (root / "profiles").mkdir(parents=True)
        (root / "profiles" / "default.toml").write_text("", encoding="utf-8")
    _write_package(roots["main"], "a")
    _write_package(roots[BROKEN_REPO], "ok")
    for package_id, extends in other_packages.items():
        _write_package(roots[BROKEN_REPO], package_id, extends)
    return write_named_manager_config(tmp_path, roots)


@pytest.mark.parametrize("json_output", [True, False], ids=["json", "text"])
@pytest.mark.parametrize("argv", BROWSE_COMMANDS)
def test_browse_skips_the_package_with_a_dangling_extends_and_warns(
    tmp_path: Path, capsys, argv: list[str], json_output: bool
) -> None:
    config_path = _write_config(tmp_path, {BROKEN_PACKAGE: [MISSING_PARENT]})

    capsys.readouterr()
    code = main(["--config", str(config_path), *(["--json"] if json_output else []), *argv])
    captured = capsys.readouterr()

    assert code == 0, captured.err
    if json_output:
        json.loads(captured.out)
    assert "main:a" in captured.out
    assert f"{BROKEN_REPO}:ok" in captured.out
    assert f"{BROKEN_REPO}:{BROKEN_PACKAGE}" not in captured.out

    warnings = [line for line in captured.err.splitlines() if line.startswith("warning:")]
    assert len(warnings) == 1, captured.err
    for name in (BROKEN_REPO, BROKEN_PACKAGE, MISSING_PARENT):
        assert name in warnings[0]


def test_browse_still_fails_on_an_extends_cycle(tmp_path: Path, capsys) -> None:
    config_path = _write_config(tmp_path, {"x": ["y"], "y": ["x"]})

    for argv in (["list", "trackables"], ["search", SEARCH_QUERY]):
        capsys.readouterr()
        code = main(["--config", str(config_path), *argv])
        captured = capsys.readouterr()

        assert code != 0, f"{argv}: {captured.out}"
        assert BROKEN_REPO in captured.err
