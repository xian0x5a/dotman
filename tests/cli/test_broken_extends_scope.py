"""A tracked row whose package extends a missing parent is stale: commands skip it with a warning."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from dotman.cli import main

from tests.helpers import write_named_manager_config, write_tracked_packages_state

STALE_REPO = "other"
STALE_PACKAGE = "child"
STALE_ROW = f"{STALE_REPO}:{STALE_PACKAGE}@default"
MISSING_PARENT = "gone"


def _write_two_repo_config(tmp_path: Path, stale_package_manifest: str) -> Path:
    """`main:a` is healthy; `other:child` is tracked but its manifest dangles."""
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
    (other_root / "packages" / STALE_PACKAGE).mkdir(parents=True)
    (other_root / "packages" / STALE_PACKAGE / "package.toml").write_text(stale_package_manifest, encoding="utf-8")

    config_path = write_named_manager_config(tmp_path, {"main": main_root, STALE_REPO: other_root})
    write_tracked_packages_state(tmp_path / "state", repo_name="main", entries=[("a", "default")])
    write_tracked_packages_state(tmp_path / "state", repo_name=STALE_REPO, entries=[(STALE_PACKAGE, "default")])
    return config_path


@pytest.fixture
def two_repo_config(tmp_path: Path) -> Path:
    return _write_two_repo_config(tmp_path, f'extends = ["{MISSING_PARENT}"]\n')


def _run(config_path: Path, capsys, *argv: str) -> tuple[int, str]:
    capsys.readouterr()
    code = main(["--config", str(config_path), "--no-confirm", *argv])
    captured = capsys.readouterr()
    return code, captured.out + captured.err


def _assert_names_stale_row(output: str) -> None:
    for name in (STALE_REPO, STALE_PACKAGE, MISSING_PARENT):
        assert name in output


@pytest.mark.parametrize(
    "argv",
    [
        pytest.param(["track", "main:a@default"], id="track"),
        pytest.param(["untrack", "main:a@default"], id="untrack"),
        pytest.param(["push", "--dry-run", "main:a"], id="push-qualified"),
        pytest.param(["pull", "--dry-run", "main:a"], id="pull-qualified"),
        pytest.param(["push", "--dry-run", "a"], id="push-unqualified"),
        pytest.param(["push", "--dry-run"], id="push-all-tracked"),
        pytest.param(["pull", "--dry-run"], id="pull-all-tracked"),
        pytest.param(["sync", "--dry-run"], id="sync-all-tracked"),
    ],
)
def test_command_skips_the_stale_row_and_warns(two_repo_config: Path, capsys, argv: list[str]) -> None:
    code, output = _run(two_repo_config, capsys, *argv)

    assert code == 0, output
    assert f"warning: skipped stale tracked row {STALE_ROW}" in output
    _assert_names_stale_row(output)


@pytest.mark.parametrize(
    "argv",
    [
        pytest.param(["push", "--dry-run", f"{STALE_REPO}:{STALE_PACKAGE}"], id="push"),
        pytest.param(["pull", "--dry-run", f"{STALE_REPO}:{STALE_PACKAGE}"], id="pull"),
        pytest.param(["track", f"{STALE_ROW}"], id="track"),
    ],
)
def test_explicitly_selecting_the_stale_package_fails_naming_the_cause(
    two_repo_config: Path, capsys, argv: list[str]
) -> None:
    code, output = _run(two_repo_config, capsys, *argv)

    assert code != 0
    _assert_names_stale_row(output)


def test_warning_is_one_line_per_row_and_points_to_the_fix(two_repo_config: Path, capsys) -> None:
    # `track` walks the tracked state several times; the row is still reported once.
    _code, output = _run(two_repo_config, capsys, "track", "main:a@default")

    warnings = [line for line in output.splitlines() if line.startswith("warning:")]
    assert len(warnings) == 1
    assert "dotman list tracked" in warnings[0]
    assert f"dotman untrack {STALE_ROW}" in warnings[0]


def test_json_output_stays_valid_and_the_warning_goes_to_stderr(two_repo_config: Path, capsys) -> None:
    capsys.readouterr()
    main(["--config", str(two_repo_config), "--json", "--no-confirm", "push", "--dry-run", "main:a"])
    captured = capsys.readouterr()

    json.loads(captured.out)
    assert "warning:" not in captured.out
    assert f"warning: skipped stale tracked row {STALE_ROW}" in captured.err


def test_stale_missing_depends_row_warns_the_same_way(tmp_path: Path, capsys) -> None:
    config_path = _write_two_repo_config(tmp_path, f'depends = ["{MISSING_PARENT}"]\n')

    code, output = _run(config_path, capsys, "push", "--dry-run", "main:a")

    assert code == 0, output
    assert f"warning: skipped stale tracked row {STALE_ROW}" in output


def test_list_tracked_and_doctor_still_report_the_stale_row(two_repo_config: Path, capsys) -> None:
    for command in (["list", "tracked"], ["doctor"]):
        capsys.readouterr()
        main(["--config", str(two_repo_config), "--json", *command])
        captured = capsys.readouterr()
        payload = json.loads(captured.out)

        invalid = payload["invalid_package_entries"]
        assert [(entry["repo"], entry["package_id"]) for entry in invalid] == [(STALE_REPO, STALE_PACKAGE)]
        assert invalid[0]["reason"] == "extends_parent_missing"
        assert MISSING_PARENT in invalid[0]["message"]
        assert "warning:" not in captured.err
