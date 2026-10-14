"""Tracked state rows that do not resolve survive unrelated track and untrack writes."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from dotman.cli import main

from tests.helpers import (
    write_implicit_conflict_repo,
    write_named_manager_config,
    write_tracked_packages_state,
    write_untrack_conflict_repo,
)


def _write_plain_packages(repo_root: Path, package_ids: list[str], *, profiles: tuple[str, ...] = ("default",)) -> None:
    (repo_root / "profiles").mkdir(parents=True)
    for profile in profiles:
        (repo_root / "profiles" / f"{profile}.toml").write_text("", encoding="utf-8")
    for package_id in package_ids:
        (repo_root / "packages" / package_id).mkdir(parents=True)
        (repo_root / "packages" / package_id / "package.toml").write_text(f'id = "{package_id}"\n', encoding="utf-8")


def _listed_package_entries(config_path: Path, capsys) -> set[tuple[str, str, str]]:
    """Every persisted entry `list tracked` reports, whether it resolves or is invalid."""
    capsys.readouterr()
    assert main(["--config", str(config_path), "--json", "list", "tracked"]) == 0
    payload = json.loads(capsys.readouterr().out)
    resolved = [entry for package in payload["packages"] for entry in package["package_entries"]]
    return {
        (entry["repo"], entry["package_id"], entry["profile"])
        for entry in [*resolved, *payload["invalid_package_entries"]]
    }


@pytest.mark.parametrize(
    "stale_entry",
    [
        pytest.param(("gone", "default"), id="package-missing"),
        pytest.param(("b", "removed-profile"), id="profile-missing"),
    ],
)
@pytest.mark.parametrize(
    "command",
    [
        pytest.param(["track", "main:a@default"], id="track"),
        pytest.param(["untrack", "main:b@default"], id="untrack"),
    ],
)
def test_track_and_untrack_keep_unrelated_invalid_entries(
    tmp_path: Path,
    capsys,
    stale_entry: tuple[str, str],
    command: list[str],
) -> None:
    # A stale entry (package or profile absent, e.g. another branch is checked
    # out) must stay listed and removable after tracking or untracking something else.
    repo_root = tmp_path / "repo"
    _write_plain_packages(repo_root, ["a", "b"])
    config_path = write_named_manager_config(tmp_path, {"main": repo_root})
    write_tracked_packages_state(tmp_path / "state", repo_name="main", entries=[stale_entry, ("b", "default")])
    stale = ("main", *stale_entry)
    assert stale in _listed_package_entries(config_path, capsys)

    assert main(["--config", str(config_path), "--no-confirm", *command]) == 0

    assert stale in _listed_package_entries(config_path, capsys)


@pytest.mark.parametrize(
    ("write_repo", "entries", "command"),
    [
        pytest.param(write_implicit_conflict_repo, [("alpha", "basic")], ["track", "fixture:beta@basic"], id="track"),
        pytest.param(
            write_untrack_conflict_repo,
            [("shared", "direct"), ("stack-a", "work"), ("stack-b", "personal")],
            ["untrack", "fixture:shared@direct"],
            id="untrack",
        ),
    ],
)
def test_kept_invalid_entry_leaves_conflict_checks_on(
    tmp_path: Path,
    capsys,
    write_repo,
    entries: list[tuple[str, str]],
    command: list[str],
) -> None:
    # A row kept only because it does not resolve must not let through a write
    # that is rejected without it.
    repo_root = tmp_path / "repo"
    write_repo(repo_root)
    config_path = write_named_manager_config(tmp_path, {"fixture": repo_root})
    write_tracked_packages_state(tmp_path / "state", repo_name="fixture", entries=[*entries, ("gone", entries[0][1])])
    before = _listed_package_entries(config_path, capsys)

    assert main(["--config", str(config_path), "--no-confirm", *command]) == 1

    assert _listed_package_entries(config_path, capsys) == before
