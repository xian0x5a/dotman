"""`/` is only a package namespace separator; `repo:` is the only repo qualifier.

Fixture: repos `main` and `work`. `main` owns the namespaced package `work/git` and group
`work/team`, while repo `work` owns an unrelated package `git` and group `team`. The text
`work/git` must always mean `main:work/git`, never repo `work` + package `git`.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest
from dotman.cli import main

from tests.helpers import write_named_manager_config, write_tracked_packages_state

LIVE_FILE_NAME = ".added-by-test"


def _write_package(repo_root: Path, package_id: str, live_path: str) -> None:
    package_root = repo_root / "packages" / Path(*package_id.split("/"))
    (package_root / "files").mkdir(parents=True)
    (package_root / "files" / "payload").write_text("payload\n", encoding="utf-8")
    (package_root / "package.toml").write_text(
        f'[targets.payload]\nsource = "files/payload"\npath = "{live_path}"\n',
        encoding="utf-8",
    )


def _write_group(repo_root: Path, group_id: str, members: list[str]) -> None:
    group_path = repo_root / "groups" / Path(*group_id.split("/"))
    group_path = group_path.with_suffix(".toml")
    group_path.parent.mkdir(parents=True, exist_ok=True)
    members_text = ", ".join(f'"{member}"' for member in members)
    group_path.write_text(f"members = [{members_text}]\n", encoding="utf-8")


@pytest.fixture
def collision_config(tmp_path: Path, monkeypatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    (home / LIVE_FILE_NAME).write_text("live\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))

    repos: dict[str, Path] = {}
    for repo_name in ("main", "work"):
        repo_root = tmp_path / repo_name
        (repo_root / "profiles").mkdir(parents=True)
        (repo_root / "profiles" / "basic.toml").write_text("", encoding="utf-8")
        repos[repo_name] = repo_root
    # Distinct live paths so both packages can be tracked together without a target conflict.
    _write_package(repos["main"], "work/git", "~/.config/main-work-git")
    _write_package(repos["work"], "git", "~/.config/work-git")
    _write_group(repos["main"], "work/team", ["work/git"])
    _write_group(repos["work"], "team", ["git"])
    return write_named_manager_config(tmp_path, repos)


def _track_both_repos_state(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    write_tracked_packages_state(
        state_root, repo_name="main", entries=[("work/git", "basic")]
    )
    write_tracked_packages_state(
        state_root, repo_name="work", entries=[("git", "basic")]
    )


# (argv after `--config ... --json`, expected selector, extractor of the (repo, selector) acted on)
Extractor = Callable[[dict], tuple[str, str]]
SLASH_SELECTOR_COMMANDS: dict[str, tuple[list[str], str, Extractor]] = {
    "track-package": (
        ["track", "work/git@basic"],
        "work/git",
        lambda payload: (payload["package_entry"]["repo"], payload["package_entry"]["package_id"]),
    ),
    "track-group": (
        ["track", "work/team@basic"],
        "work/team",
        lambda payload: (payload["package_entry"]["repo"], payload["package_entry"]["package_id"]),
    ),
    "untrack-group": (
        ["untrack", "work/team"],
        "work/git",  # the group's member entry removed from state
        lambda payload: (
            payload["package_entries"][0]["repo"],
            payload["package_entries"][0]["package_id"],
        ),
    ),
    "info-tracked": (
        ["info", "tracked", "work/git"],
        "work/git",
        lambda payload: (payload["package"]["repo"], payload["package"]["package_id"]),
    ),
    "info-trackable": (
        ["info", "trackable", "work/git"],
        "work/git",
        lambda payload: (payload["trackable"]["repo"], payload["trackable"]["selector"]),
    ),
    "add": (
        ["--no-confirm", "add", LIVE_FILE_NAME, "work/git"],
        "work/git",
        lambda payload: (payload["repo"], payload["package_id"]),
    ),
}


@pytest.mark.parametrize("command_id", SLASH_SELECTOR_COMMANDS)
def test_slash_selector_resolves_to_namespaced_package_not_repo_prefix(
    command_id: str,
    collision_config: Path,
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    argv, expected_selector, extract_resolved_identity = SLASH_SELECTOR_COMMANDS[command_id]
    monkeypatch.chdir(tmp_path / "home")
    if not command_id.startswith("track"):
        _track_both_repos_state(tmp_path)

    exit_code = main(["--config", str(collision_config), "--json", *argv])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    repo, selector = extract_resolved_identity(payload)
    assert (repo, selector) == ("main", expected_selector)


def test_edit_slash_selector_opens_namespaced_package_not_repo_prefix(
    collision_config: Path,
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.delenv("VISUAL", raising=False)
    monkeypatch.delenv("EDITOR", raising=False)
    _track_both_repos_state(tmp_path)

    exit_code = main(["--config", str(collision_config), "edit", "package", "work/git"])

    assert exit_code == 0
    assert str(tmp_path / "main" / "packages" / "work" / "git") in capsys.readouterr().out


def test_slash_after_repo_name_is_not_a_repo_qualifier(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    repo_root = tmp_path / "beta"
    (repo_root / "profiles").mkdir(parents=True)
    (repo_root / "profiles" / "p.toml").write_text("", encoding="utf-8")
    _write_package(repo_root, "sunshine", "~/.config/sunshine")
    config_path = write_named_manager_config(tmp_path, {"beta": repo_root})

    assert main(["--config", str(config_path), "--json", "track", "beta:sunshine@p"]) == 0
    capsys.readouterr()

    exit_code = main(["--config", str(config_path), "--json", "track", "beta/sunshine@p"])

    assert exit_code == 2
    assert "selector 'beta/sunshine' did not match any package or group" in capsys.readouterr().err
