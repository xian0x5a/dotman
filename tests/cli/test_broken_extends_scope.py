"""A tracked package whose `extends` parent is gone fails only commands that touch its repo."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from dotman.cli import main

from tests.helpers import write_named_manager_config, write_tracked_packages_state

BROKEN_REPO = "other"
BROKEN_PACKAGE = "child"
MISSING_PARENT = "gone"


@pytest.fixture
def two_repo_config(tmp_path: Path) -> Path:
    """`main:a` is healthy; `other:child` is tracked but extends the missing package `gone`."""
    main_root = tmp_path / "main-repo"
    (main_root / "profiles").mkdir(parents=True)
    (main_root / "profiles" / "default.toml").write_text("", encoding="utf-8")
    (main_root / "packages" / "a" / "files").mkdir(parents=True)
    (main_root / "packages" / "a" / "files" / "a.conf").write_text("a\n", encoding="utf-8")
    (main_root / "packages" / "a" / "package.toml").write_text(
        f'[targets.conf]\nsource = "files/a.conf"\npath = "{tmp_path / "live" / "a.conf"}"\n',
        encoding="utf-8",
    )

    other_root = tmp_path / "other-repo"
    (other_root / "profiles").mkdir(parents=True)
    (other_root / "profiles" / "default.toml").write_text("", encoding="utf-8")
    (other_root / "packages" / BROKEN_PACKAGE).mkdir(parents=True)
    (other_root / "packages" / BROKEN_PACKAGE / "package.toml").write_text(
        f'extends = ["{MISSING_PARENT}"]\n', encoding="utf-8"
    )

    config_path = write_named_manager_config(tmp_path, {"main": main_root, BROKEN_REPO: other_root})
    write_tracked_packages_state(tmp_path / "state", repo_name="main", entries=[("a", "default")])
    write_tracked_packages_state(tmp_path / "state", repo_name=BROKEN_REPO, entries=[(BROKEN_PACKAGE, "default")])
    return config_path


def _run(config_path: Path, capsys, *argv: str) -> tuple[int, str]:
    capsys.readouterr()
    code = main(["--config", str(config_path), "--no-confirm", *argv])
    captured = capsys.readouterr()
    return code, captured.out + captured.err


def _assert_names_broken_extends(output: str) -> None:
    for name in (BROKEN_REPO, BROKEN_PACKAGE, MISSING_PARENT):
        assert name in output


@pytest.mark.parametrize(
    "argv",
    [
        pytest.param(["track", "main:a@default"], id="track"),
        pytest.param(["untrack", "main:a@default"], id="untrack"),
        pytest.param(["push", "--dry-run", "main:a"], id="push"),
        pytest.param(["pull", "--dry-run", "main:a"], id="pull"),
    ],
)
def test_command_scoped_to_another_repo_succeeds_and_warns_about_broken_extends(
    two_repo_config: Path, capsys, argv: list[str]
) -> None:
    code, output = _run(two_repo_config, capsys, *argv)

    assert code == 0, output
    assert "unknown package" not in output
    _assert_names_broken_extends(output)


@pytest.mark.parametrize(
    "argv",
    [
        pytest.param(["push", "--dry-run", f"{BROKEN_REPO}:{BROKEN_PACKAGE}"], id="push-selecting-broken-package"),
        pytest.param(["pull", "--dry-run", f"{BROKEN_REPO}:{BROKEN_PACKAGE}"], id="pull-selecting-broken-package"),
        pytest.param(["push", "--dry-run"], id="push-all-tracked"),
        pytest.param(["pull", "--dry-run"], id="pull-all-tracked"),
        pytest.param(["sync", "--dry-run"], id="sync-all-tracked"),
    ],
)
def test_command_whose_scope_includes_the_broken_repo_fails_naming_the_cause(
    two_repo_config: Path, capsys, argv: list[str]
) -> None:
    code, output = _run(two_repo_config, capsys, *argv)

    assert code != 0
    _assert_names_broken_extends(output)


def test_track_selecting_the_broken_package_fails_naming_the_cause(two_repo_config: Path, capsys) -> None:
    code, output = _run(two_repo_config, capsys, "track", f"{BROKEN_REPO}:{BROKEN_PACKAGE}@default")

    assert code != 0
    _assert_names_broken_extends(output)


def test_list_tracked_and_doctor_still_report_the_broken_row(two_repo_config: Path, capsys) -> None:
    for command in (["list", "tracked"], ["doctor"]):
        capsys.readouterr()
        main(["--config", str(two_repo_config), "--json", *command])
        payload = json.loads(capsys.readouterr().out)

        invalid = payload["invalid_package_entries"]
        assert [(entry["repo"], entry["package_id"]) for entry in invalid] == [(BROKEN_REPO, BROKEN_PACKAGE)]
        assert MISSING_PARENT in invalid[0]["message"]
