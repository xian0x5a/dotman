"""Tracked state rows that do not resolve survive unrelated track and untrack writes."""
from __future__ import annotations

import json
import re
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
        (repo_root / "packages" / package_id / "package.toml").write_text('', encoding="utf-8")


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

    assert main(["--config", str(config_path), "--no-confirm", *command]) == 2

    assert _listed_package_entries(config_path, capsys) == before


def _write_stale_row_repo(repo_root: Path, stale_kind: str) -> tuple[str, str]:
    """Write packages `a` and `b` (b owns one target) plus what `stale_kind` needs; return the stale row."""
    _write_plain_packages(repo_root, ["a", "b"])
    (repo_root / "packages" / "b" / "files").mkdir()
    (repo_root / "packages" / "b" / "files" / "b.conf").write_text("b\n", encoding="utf-8")
    (repo_root / "packages" / "b" / "package.toml").write_text(
        f'[targets.conf]\nsource = "files/b.conf"\npath = "{repo_root.parent / "live" / "b.conf"}"\n',
        encoding="utf-8",
    )
    if stale_kind == "profile-missing":
        return ("b", "removed-profile")
    if stale_kind == "dependency-broken":
        (repo_root / "packages" / "broken").mkdir(parents=True)
        (repo_root / "packages" / "broken" / "package.toml").write_text('depends = ["nope"]\n', encoding="utf-8")
        return ("broken", "default")
    if stale_kind == "group-row":
        (repo_root / "groups").mkdir(parents=True)
        (repo_root / "groups" / "stack.toml").write_text('members = ["b"]\n', encoding="utf-8")
        return ("stack", "default")
    raise AssertionError(stale_kind)


@pytest.mark.parametrize("stale_kind", ["profile-missing", "dependency-broken", "group-row"])
def test_row_that_does_not_resolve_is_reported_invalid_and_skipped_by_planning(
    tmp_path: Path,
    capsys,
    stale_kind: str,
) -> None:
    # list, doctor and planning must agree: a row that does not resolve is reported
    # as invalid, and push/track neither use it silently nor fail because of it.
    repo_root = tmp_path / "repo"
    stale_package, stale_profile = _write_stale_row_repo(repo_root, stale_kind)
    config_path = write_named_manager_config(tmp_path, {"main": repo_root})
    write_tracked_packages_state(tmp_path / "state", repo_name="main", entries=[(stale_package, stale_profile)])

    capsys.readouterr()
    assert main(["--config", str(config_path), "--json", "list", "tracked"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed["packages"] == []
    assert [(entry["package_id"], entry["profile"]) for entry in listed["invalid_package_entries"]] == [
        (stale_package, stale_profile)
    ]

    main(["--config", str(config_path), "--json", "doctor"])
    doctor = json.loads(capsys.readouterr().out)
    assert doctor["ok"] is False
    assert [(entry["package_id"], entry["profile"]) for entry in doctor["invalid_package_entries"]] == [
        (stale_package, stale_profile)
    ]

    assert main(["--config", str(config_path), "--json", "--no-confirm", "push", "--dry-run"]) == 0
    pushed = json.loads(capsys.readouterr().out)
    assert pushed["sync_units"] == []

    assert main(["--config", str(config_path), "--no-confirm", "track", "main:a@default"]) == 0


def test_invalid_row_in_another_repo_leaves_conflict_checks_on(tmp_path: Path, capsys) -> None:
    repo_root = tmp_path / "repo"
    write_implicit_conflict_repo(repo_root)
    other_root = tmp_path / "other"
    _write_plain_packages(other_root, ["a"])
    config_path = write_named_manager_config(tmp_path, {"fixture": repo_root, "other": other_root})
    write_tracked_packages_state(tmp_path / "state", repo_name="fixture", entries=[("alpha", "basic")])
    write_tracked_packages_state(tmp_path / "state", repo_name="other", entries=[("gone", "default")])

    assert main(["--config", str(config_path), "--no-confirm", "track", "fixture:beta@basic"]) == 2
    assert "conflicting explicit tracked targets" in capsys.readouterr().err


def test_untrack_group_removes_member_rows_that_do_not_resolve(tmp_path: Path, capsys) -> None:
    # A member whose dependency is gone is still part of the group the user untracks.
    repo_root = tmp_path / "repo"
    _write_stale_row_repo(repo_root, "dependency-broken")
    (repo_root / "groups").mkdir()
    (repo_root / "groups" / "stack.toml").write_text('members = ["a", "broken"]\n', encoding="utf-8")
    config_path = write_named_manager_config(tmp_path, {"main": repo_root})
    write_tracked_packages_state(tmp_path / "state", repo_name="main", entries=[("a", "default"), ("broken", "default")])

    assert main(["--config", str(config_path), "--no-confirm", "untrack", "main:stack@default"]) == 0

    assert _listed_package_entries(config_path, capsys) == set()


@pytest.mark.parametrize(
    ("groups", "listing_group"),
    [
        pytest.param({"stack": ["a", "missing"]}, "stack", id="listed-directly"),
        pytest.param({"stack": ["inner"], "inner": ["a", "missing"]}, "inner", id="listed-in-nested-group"),
    ],
)
def test_track_group_with_missing_member_is_usage_error_naming_group_and_member(
    tmp_path: Path,
    capsys,
    groups: dict[str, list[str]],
    listing_group: str,
) -> None:
    repo_root = tmp_path / "repo"
    _write_plain_packages(repo_root, ["a"])
    (repo_root / "groups").mkdir()
    for group_id, members in groups.items():
        (repo_root / "groups" / f"{group_id}.toml").write_text(f"members = {json.dumps(members)}\n", encoding="utf-8")
    config_path = write_named_manager_config(tmp_path, {"main": repo_root})

    exit_code = main(["--config", str(config_path), "--no-confirm", "track", "main:stack@default"])

    assert exit_code == 2
    error = capsys.readouterr().err
    assert listing_group in error
    assert "missing" in error
    assert "PersistedTrackedPackageEntryResolutionError" not in error
    assert "dependency resolution failed" not in error


def test_tracked_package_with_broken_manifest_is_reported_and_fails_planning(tmp_path: Path, capsys) -> None:
    # A broken manifest is a repo error, not stale state: list and doctor report the
    # row with the cause, and planning and writes fail instead of skipping it.
    repo_root = tmp_path / "repo"
    _write_plain_packages(repo_root, ["a"])
    for package_id, parent in (("alpha", "beta"), ("beta", "alpha")):
        (repo_root / "packages" / package_id).mkdir()
        (repo_root / "packages" / package_id / "package.toml").write_text(f'extends = ["{parent}"]\n', encoding="utf-8")
    config_path = write_named_manager_config(tmp_path, {"main": repo_root})
    write_tracked_packages_state(tmp_path / "state", repo_name="main", entries=[("alpha", "default")])

    capsys.readouterr()
    for command in (["list", "tracked"], ["doctor"]):
        main(["--config", str(config_path), "--json", *command])
        invalid_entries = json.loads(capsys.readouterr().out)["invalid_package_entries"]
        assert [(entry["package_id"], entry["reason"]) for entry in invalid_entries] == [("alpha", "package_invalid")]
        assert "extends cycle" in invalid_entries[0]["message"]

    assert main(["--config", str(config_path), "--json", "--no-confirm", "push", "--dry-run"]) != 0
    assert "extends cycle" in capsys.readouterr().out
    assert main(["--config", str(config_path), "--no-confirm", "track", "main:a@default"]) != 0
    assert "extends cycle" in capsys.readouterr().err


def _write_broken_manifest_repo(repo_root: Path, broken_kind: str) -> None:
    """Write package `a` plus a package `alpha` whose manifest does not load."""
    _write_plain_packages(repo_root, ["a"])
    (repo_root / "packages" / "alpha").mkdir()
    if broken_kind == "extends-cycle":
        (repo_root / "packages" / "alpha" / "package.toml").write_text('extends = ["beta"]\n', encoding="utf-8")
        (repo_root / "packages" / "beta").mkdir()
        (repo_root / "packages" / "beta" / "package.toml").write_text('extends = ["alpha"]\n', encoding="utf-8")
        return
    if broken_kind == "extends-parent-missing":
        (repo_root / "packages" / "alpha" / "package.toml").write_text('extends = ["gone"]\n', encoding="utf-8")
        return
    raise AssertionError(broken_kind)


@pytest.mark.parametrize("broken_kind", ["extends-cycle", "extends-parent-missing"])
@pytest.mark.parametrize("command", [["list", "tracked"], ["doctor"]], ids=["list-tracked", "doctor"])
def test_human_output_lists_tracked_package_whose_manifest_does_not_load(
    tmp_path: Path,
    capsys,
    broken_kind: str,
    command: list[str],
) -> None:
    repo_root = tmp_path / "repo"
    _write_broken_manifest_repo(repo_root, broken_kind)
    config_path = write_named_manager_config(tmp_path, {"main": repo_root})
    write_tracked_packages_state(tmp_path / "state", repo_name="main", entries=[("alpha", "default")])

    capsys.readouterr()
    main(["--config", str(config_path), *command])

    captured = capsys.readouterr()
    assert "main:alpha" in captured.out
    assert "invalid" in captured.out
    assert "ValueError" not in captured.err


@pytest.mark.parametrize("broken_kind", ["extends-cycle", "extends-parent-missing"])
def test_untrack_of_package_whose_manifest_does_not_load_succeeds(
    tmp_path: Path,
    capsys,
    broken_kind: str,
) -> None:
    repo_root = tmp_path / "repo"
    _write_broken_manifest_repo(repo_root, broken_kind)
    config_path = write_named_manager_config(tmp_path, {"main": repo_root})
    write_tracked_packages_state(tmp_path / "state", repo_name="main", entries=[("alpha", "default")])

    assert main(["--config", str(config_path), "--no-confirm", "untrack", "main:alpha@default"]) == 0

    assert _listed_package_entries(config_path, capsys) == set()


def _write_unresolvable_binding_repo(repo_root: Path, case: str) -> tuple[str, str, str | None]:
    """Write a repo where tracking the returned binding cannot resolve.

    Returns the binding, the missing name and, when a group member is what fails, that member.
    """
    _write_plain_packages(repo_root, ["a"])
    (repo_root / "groups").mkdir()

    def write_package(package_id: str, manifest: str) -> None:
        (repo_root / "packages" / package_id).mkdir()
        (repo_root / "packages" / package_id / "package.toml").write_text(manifest, encoding="utf-8")

    def write_group(group_id: str, members: list[str]) -> None:
        (repo_root / "groups" / f"{group_id}.toml").write_text(f"members = {json.dumps(members)}\n", encoding="utf-8")

    if case == "dependency-missing":
        write_package("app", 'depends = ["nope"]\n')
        return "main:app@default", "nope", None
    if case == "dependency-group-member-missing":
        write_group("stack", ["a", "ghost"])
        write_package("app", 'depends = ["stack"]\n')
        return "main:app@default", "ghost", None
    if case == "group-member-dependency-missing":
        write_package("app", 'depends = ["nope"]\n')
        write_group("stack", ["app"])
        return "main:stack@default", "nope", "app"
    if case == "extends-parent-missing":
        write_package("app", 'extends = ["gone"]\n')
        return "main:app@default", "gone", None
    raise AssertionError(case)


@pytest.mark.parametrize(
    "case",
    ["dependency-missing", "dependency-group-member-missing", "group-member-dependency-missing", "extends-parent-missing"],
)
def test_track_of_binding_that_cannot_resolve_is_usage_error_without_stale_row_warning(
    tmp_path: Path,
    capsys,
    case: str,
) -> None:
    # The binding was never tracked, so it is not a stale row: only the error is shown
    # and nothing is written to tracked state.
    repo_root = tmp_path / "repo"
    binding, missing_name, failing_member = _write_unresolvable_binding_repo(repo_root, case)
    config_path = write_named_manager_config(tmp_path, {"main": repo_root})

    exit_code = main(["--config", str(config_path), "--no-confirm", "track", binding])

    assert exit_code == 2
    error = capsys.readouterr().err
    assert missing_name in error
    if failing_member:
        assert re.search(rf"\b{failing_member}\b", error), error
    assert "warning:" not in error
    assert "stale tracked row" not in error
    assert _listed_package_entries(config_path, capsys) == set()
    state_file = tmp_path / "state" / "dotman" / "repos" / "main" / "tracked-packages.toml"
    assert not state_file.exists() or "[[packages]]" not in state_file.read_text(encoding="utf-8")
