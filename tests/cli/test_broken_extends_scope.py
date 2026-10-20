"""A tracked row whose package extends a missing parent is stale: commands skip it and say so.

Push, pull and sync list it as a `[skipped]` entry in their human output; every other
command, and JSON output, reports it as a stderr warning.
"""
from __future__ import annotations

import json
import shutil
import tomllib
from pathlib import Path

import pytest
from dotman.cli import main
from dotman.command_runtime import MemoryCommandRuntime

from tests.helpers import write_named_manager_config, write_tracked_packages_state

STALE_REPO = "other"
STALE_PACKAGE = "child"
STALE_ROW = f"{STALE_REPO}:{STALE_PACKAGE}@default"
MISSING_PARENT = "gone"
STALE_WARNING = f"warning: skipped stale tracked row {STALE_ROW}"
STALE_ENTRY_PREFIX = f"  [skipped] {STALE_ROW} ("
STALE_ENTRY_UNTRACK_LINE = f"      stale tracked row; remove it with: dotman untrack {STALE_ROW}"


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


def _run_split(config_path: Path, capsys, *argv: str, no_confirm: bool = True) -> tuple[int, str, str]:
    capsys.readouterr()
    code = main(["--config", str(config_path), *(["--no-confirm"] if no_confirm else []), *argv])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _run(config_path: Path, capsys, *argv: str) -> tuple[int, str]:
    code, stdout, stderr = _run_split(config_path, capsys, *argv)
    return code, stdout + stderr


def _is_stale_entry_line(line: str) -> bool:
    return line.startswith(STALE_ENTRY_PREFIX) or line == STALE_ENTRY_UNTRACK_LINE


def _stale_entry_index(stdout_lines: list[str]) -> int:
    """Index of the stale row's `[skipped]` entry, asserting it is shown exactly once with its untrack line."""
    entry_indexes = [index for index, line in enumerate(stdout_lines) if line.startswith(STALE_ENTRY_PREFIX)]
    assert len(entry_indexes) == 1, stdout_lines
    index = entry_indexes[0]
    assert stdout_lines[index + 1] == STALE_ENTRY_UNTRACK_LINE, stdout_lines
    return index


@pytest.mark.parametrize(
    "argv",
    [
        pytest.param(["track", "main:a@default"], id="track"),
        pytest.param(["untrack", "main:a@default"], id="untrack"),
        # push, pull and sync human output show the row as an entry instead (see below).
        # These two read the tracked rows through a second skip point.
        pytest.param(["info", "trackable", "main:a"], id="info-trackable"),
        pytest.param(["list", "vars"], id="list-vars"),
    ],
)
def test_command_skips_the_stale_row_and_warns(two_repo_config: Path, capsys, argv: list[str]) -> None:
    code, output = _run(two_repo_config, capsys, *argv)

    assert code == 0, output
    assert STALE_WARNING in output


def _open_deck_approving_everything(monkeypatch) -> list[bool]:
    """Make the run interactive with a Deck that approves at once; returns a list that records each opening."""
    openings: list[bool] = []
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    monkeypatch.setattr("dotman.sync_deck.run_command_deck", lambda session, *, use_color: openings.append(True) or True)
    return openings


# Where the entry goes: right under the `:: <command>` header in a real run, so above
# the timeline and any work entry; before the `:: <status> —` summary in a preview or report.
@pytest.mark.parametrize(
    ("argv", "deck_opens", "real_run"),
    [
        pytest.param(["push", "main:a"], False, True, id="push"),
        pytest.param(["sync", "main:a"], False, True, id="sync"),
        # Stale rows never reach the Deck, so the run still lists them itself.
        pytest.param(["sync", "main:a"], True, True, id="sync-deck-opened"),
        pytest.param(["pull", "--dry-run", "main:a"], False, False, id="pull-preview"),
        pytest.param(["sync", "--report", "main:a"], False, False, id="sync-report"),
    ],
)
def test_sync_family_lists_the_stale_row_as_one_skipped_entry_instead_of_a_warning(
    two_repo_config: Path, capsys, monkeypatch, argv: list[str], deck_opens: bool, real_run: bool
) -> None:
    deck_openings = _open_deck_approving_everything(monkeypatch) if deck_opens else []

    code, stdout, stderr = _run_split(two_repo_config, capsys, *argv, no_confirm=not deck_opens)

    assert code == 0, stdout + stderr
    assert deck_openings == ([True] if deck_opens else [])
    lines = stdout.splitlines()
    entry_index = _stale_entry_index(lines)
    assert MISSING_PARENT in lines[entry_index], "the entry names the reason"
    assert lines[0].startswith(f":: {argv[0].capitalize()}"), lines
    summary_index = next(index for index, line in enumerate(lines) if line.startswith(":: ") and " — " in line)
    if real_run:
        assert entry_index == 1, lines
    else:
        assert 0 < entry_index < summary_index, lines
    # Shown once: not repeated as a stderr warning.
    assert "stale tracked row" not in stderr, stderr


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
    # The row made the command fail, so it is not also listed as a skipped entry.
    assert not any(_is_stale_entry_line(line) for line in output.splitlines()), output
    # The error explains the named row, so it is not also warned about.
    assert not any(
        line.startswith("warning:") and (STALE_ROW in line or "stale tracked row" in line) for line in output.splitlines()
    ), output
    # The error itself names the cause and carries the fix, since no warning does.
    for name in (STALE_REPO, STALE_PACKAGE, MISSING_PARENT):
        assert name in output
    assert f"dotman untrack {STALE_ROW}" in output, output


@pytest.mark.parametrize(
    "argv",
    [
        pytest.param(["track", f"{STALE_REPO}:{STALE_PACKAGE}@work"], id="track"),
        pytest.param(["push", "--dry-run", f"{STALE_REPO}:{STALE_PACKAGE}<work>"], id="push-instance"),
    ],
)
def test_naming_one_stale_instance_errors_on_that_row_only(
    tmp_path: Path, capsys, argv: list[str]
) -> None:
    config_path = _write_two_repo_config(
        tmp_path, f'binding_mode = "multi_instance"\nextends = ["{MISSING_PARENT}"]\n'
    )
    (tmp_path / "other-repo" / "profiles" / "work.toml").write_text("", encoding="utf-8")
    write_tracked_packages_state(
        tmp_path / "state", repo_name=STALE_REPO, entries=[(STALE_PACKAGE, "default"), (STALE_PACKAGE, "work")]
    )
    named_row = f"{STALE_REPO}:{STALE_PACKAGE}@work"

    code, output = _run(config_path, capsys, *argv)

    assert code != 0
    assert f"dotman untrack {named_row}" in output, output
    lines = output.splitlines()
    warnings = [line for line in lines if line.startswith("warning:")]
    error_lines = [line for line in lines if "dotman untrack" in line and line not in warnings]
    assert error_lines and all(STALE_ROW not in line for line in error_lines), "the error names only the named row"
    assert not any(named_row in line for line in warnings), output
    # Naming one row does not hide the other: push reads the tracked state before it
    # fails, so it warns about that row once; track rejects the name before reading it.
    expected_other_row_warnings = 0 if argv[0] == "track" else 1
    assert len([line for line in warnings if STALE_ROW in line]) == expected_other_row_warnings, output


def _stale_row_with_package_deleted(tmp_path: Path) -> Path:
    """Row `other:child@default` stays tracked after `child` is removed from the repo."""
    config_path = _write_two_repo_config(tmp_path, "")
    shutil.rmtree(tmp_path / "other-repo" / "packages" / STALE_PACKAGE)
    return config_path


def _stale_row_with_profile_deleted(tmp_path: Path) -> Path:
    """Row `other:child@gone` stays tracked after profile `gone` is removed; `child` itself resolves."""
    config_path = _write_two_repo_config(tmp_path, "")
    write_tracked_packages_state(tmp_path / "state", repo_name=STALE_REPO, entries=[(STALE_PACKAGE, "gone")])
    return config_path


@pytest.mark.parametrize(
    ("write_config", "stale_row"),
    [
        pytest.param(_stale_row_with_package_deleted, STALE_ROW, id="package-deleted"),
        pytest.param(_stale_row_with_profile_deleted, f"{STALE_REPO}:{STALE_PACKAGE}@gone", id="profile-deleted"),
    ],
)
def test_tracking_a_stale_row_whose_package_or_profile_is_gone_names_the_row_once(
    tmp_path: Path, capsys, write_config, stale_row: str
) -> None:
    config_path = write_config(tmp_path)

    code, output = _run(config_path, capsys, "track", stale_row)

    assert code != 0
    assert f"dotman untrack {stale_row}" in output, output
    assert stale_row in output
    assert not any(line.startswith("warning:") and stale_row in line for line in output.splitlines()), output


def test_push_naming_a_row_whose_package_was_deleted_fails_naming_the_row(tmp_path: Path, capsys) -> None:
    config_path = _stale_row_with_package_deleted(tmp_path)

    code, output = _run(config_path, capsys, "push", "--dry-run", f"{STALE_REPO}:{STALE_PACKAGE}")

    assert code != 0
    assert f"dotman untrack {STALE_ROW}" in output, output
    assert not any(line.startswith("warning:") and STALE_ROW in line for line in output.splitlines()), output


def test_tracking_a_healthy_package_beside_a_stale_row_keeps_the_row(
    two_repo_config: Path, tmp_path: Path, capsys
) -> None:
    new_package = tmp_path / "other-repo" / "packages" / "new"
    new_package.mkdir()
    (new_package / "package.toml").write_text("", encoding="utf-8")

    code, output = _run(two_repo_config, capsys, "track", f"{STALE_REPO}:new@default")

    assert code == 0, output
    state_files = [path.read_text(encoding="utf-8") for path in (tmp_path / "state").rglob("tracked-packages.toml")]
    assert any(f'package_id = "{STALE_PACKAGE}"' in text and 'package_id = "new"' in text for text in state_files)


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
    assert STALE_WARNING in captured.err


def test_stale_missing_depends_row_is_skipped_the_same_way(tmp_path: Path, capsys) -> None:
    config_path = _write_two_repo_config(tmp_path, f'depends = ["{MISSING_PARENT}"]\n')

    code, stdout, stderr = _run_split(config_path, capsys, "push", "--dry-run", "main:a")

    assert code == 0, stdout + stderr
    _stale_entry_index(stdout.splitlines())


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


# --- Rows the command itself removed or replaced are not reported as stale -------------------


def _write_main_repo(
    tmp_path: Path,
    *,
    packages: dict[str, str],
    groups: dict[str, list[str]] | None = None,
    tracked: list[tuple[str, str]],
) -> Path:
    """One repo `main` with only profile `default`; `packages` maps id to manifest text."""
    repo_root = tmp_path / "main-repo"
    (repo_root / "profiles").mkdir(parents=True)
    (repo_root / "profiles" / "default.toml").write_text("", encoding="utf-8")
    for package_id, manifest in packages.items():
        (repo_root / "packages" / package_id).mkdir(parents=True)
        (repo_root / "packages" / package_id / "package.toml").write_text(manifest, encoding="utf-8")
    (repo_root / "groups").mkdir()
    for group_id, members in (groups or {}).items():
        (repo_root / "groups" / f"{group_id}.toml").write_text(f"members = {json.dumps(members)}\n", encoding="utf-8")
    config_path = write_named_manager_config(tmp_path, {"main": repo_root})
    write_tracked_packages_state(tmp_path / "state", repo_name="main", entries=tracked)
    return config_path


def _tracked_main_rows(tmp_path: Path) -> set[tuple[str, str]]:
    state_file = tmp_path / "state" / "dotman" / "repos" / "main" / "tracked-packages.toml"
    return {(row["package_id"], row["profile"]) for row in tomllib.loads(state_file.read_text(encoding="utf-8")).get("packages", [])}


def _warnings(output: str) -> list[str]:
    return [line for line in output.splitlines() if line.startswith("warning:")]


@pytest.mark.parametrize("target", ["main:child@default", "main:stack@default"], ids=["package", "group"])
def test_track_replacing_a_stale_row_in_the_same_scope_does_not_warn_about_it(
    tmp_path: Path, capsys, target: str
) -> None:
    # `child@gone` is replaced by `child@default`; the profile it names no longer exists.
    config_path = _write_main_repo(
        tmp_path, packages={"child": ""}, groups={"stack": ["child"]}, tracked=[("child", "gone")]
    )

    code, output = _run(config_path, capsys, "track", target)

    assert code == 0, output
    assert _tracked_main_rows(tmp_path) == {("child", "default")}
    assert not any("main:child@gone" in line for line in _warnings(output)), output


def test_track_replacing_a_stale_row_still_warns_once_about_an_unrelated_stale_row(tmp_path: Path, capsys) -> None:
    config_path = _write_main_repo(
        tmp_path,
        packages={"child": "", "broken": 'depends = ["nope"]\n'},
        tracked=[("child", "gone"), ("broken", "default")],
    )

    code, output = _run(config_path, capsys, "track", "main:child@default")

    assert code == 0, output
    assert _tracked_main_rows(tmp_path) == {("child", "default"), ("broken", "default")}
    warnings = _warnings(output)
    assert len([line for line in warnings if "main:broken@default" in line]) == 1, output
    assert not any("main:child@gone" in line for line in warnings), output


def test_untrack_of_a_group_does_not_warn_about_the_stale_member_rows_it_removed(tmp_path: Path, capsys) -> None:
    config_path = _write_main_repo(
        tmp_path,
        packages={"app": 'depends = ["nope"]\n', "lib": ""},
        groups={"stack": ["app", "lib"]},
        tracked=[("app", "default"), ("lib", "default")],
    )

    code, output = _run(config_path, capsys, "untrack", "main:stack@default")

    assert code == 0, output
    assert _tracked_main_rows(tmp_path) == set()
    assert not any("main:app@default" in line for line in _warnings(output)), output


def test_untrack_of_a_group_still_warns_once_about_an_unrelated_stale_row(tmp_path: Path, capsys) -> None:
    config_path = _write_main_repo(
        tmp_path,
        packages={"app": 'depends = ["nope"]\n', "lib": "", "broken": 'depends = ["nope"]\n'},
        groups={"stack": ["app", "lib"]},
        tracked=[("app", "default"), ("lib", "default"), ("broken", "default")],
    )

    code, output = _run(config_path, capsys, "untrack", "main:stack@default")

    assert code == 0, output
    assert _tracked_main_rows(tmp_path) == {("broken", "default")}
    warnings = _warnings(output)
    assert len([line for line in warnings if "main:broken@default" in line]) == 1, output
    assert not any("main:app@default" in line for line in warnings), output


def test_untrack_of_a_group_removes_its_rows_whose_profile_was_deleted(tmp_path: Path, capsys) -> None:
    # `untrack` matches persisted rows, so rows of a deleted profile stay removable through their group.
    config_path = _write_main_repo(
        tmp_path,
        packages={"child": "", "lib": ""},
        groups={"stack": ["child", "lib"]},
        tracked=[("child", "gone"), ("lib", "gone")],
    )

    code, output = _run(config_path, capsys, "untrack", "main:stack@gone")

    assert code == 0, output
    assert _tracked_main_rows(tmp_path) == set()
    assert not any("stale tracked row" in line for line in output.splitlines()), output


# --- Naming a stale row in a lookup command ----------------------------------------------------

STALE_TARGET = "conf"
STALE_MANIFEST_WITH_TARGET = (
    f'extends = ["{MISSING_PARENT}"]\n[targets.{STALE_TARGET}]\nsource = "files/c.conf"\npath = "~/c.conf"\n'
)


@pytest.mark.parametrize(
    "argv",
    [
        pytest.param(["info", "tracked", f"{STALE_REPO}:{STALE_PACKAGE}"], id="info-tracked"),
        pytest.param(["edit", "package", f"{STALE_REPO}:{STALE_PACKAGE}"], id="edit-package"),
        pytest.param(["edit", "target", f"{STALE_REPO}:{STALE_PACKAGE}.{STALE_TARGET}"], id="edit-target"),
        pytest.param(["info", "sync-base", f"{STALE_REPO}:{STALE_PACKAGE}.{STALE_TARGET}"], id="info-sync-base"),
    ],
)
def test_lookup_naming_the_stale_row_fails_once_naming_the_row_and_the_fix(
    tmp_path: Path, capsys, monkeypatch, argv: list[str]
) -> None:
    # `edit` would open an editor on success; keep any launch harmless and out of the real runtime.
    monkeypatch.setenv("EDITOR", "true")
    monkeypatch.setattr("dotman.cli_interaction.current_command_runtime", lambda: MemoryCommandRuntime([]))
    config_path = _write_two_repo_config(tmp_path, STALE_MANIFEST_WITH_TARGET)

    # `edit` refuses `--no-confirm`, so no lookup passes it.
    code, stdout, stderr = _run_split(config_path, capsys, *argv, no_confirm=False)
    output = stdout + stderr

    assert code != 0
    assert STALE_ROW in output, output
    assert f"dotman untrack {STALE_ROW}" in output, output
    assert MISSING_PARENT in output, output
    # One error, not a warning plus a generic "did not match": the error itself carries the row and the fix.
    error_text = "\n".join(line for line in output.splitlines() if not line.startswith("warning:"))
    assert STALE_ROW in error_text and f"dotman untrack {STALE_ROW}" in error_text, output
    assert not any(line.startswith("warning:") and STALE_ROW in line for line in output.splitlines()), output


def test_bare_target_query_sharing_a_stale_package_name_fails_as_not_found_and_still_warns(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    monkeypatch.setenv("EDITOR", "true")
    monkeypatch.setattr("dotman.cli_interaction.current_command_runtime", lambda: MemoryCommandRuntime([]))
    # Stale package `conf` shares its name with the bare target query, but no tracked target is named `conf`.
    config_path = _write_main_repo(
        tmp_path, packages={"conf": f'extends = ["{MISSING_PARENT}"]\n'}, tracked=[("conf", "default")]
    )

    code, stdout, stderr = _run_split(config_path, capsys, "edit", "target", "conf", no_confirm=False)
    output = stdout + stderr

    assert code != 0, output
    assert "tracked target 'conf' did not match any tracked target" in output, output
    assert "matches stale tracked row" not in output, output
    [warning] = _warnings(output)
    assert "main:conf@default" in warning, output


# --- The reason names what is missing ------------------------------------------------------------


@pytest.fixture
def missing_dependency_config(tmp_path: Path) -> Path:
    return _write_main_repo(
        tmp_path, packages={"a": "", "app": 'depends = ["nope"]\n'}, tracked=[("a", "default"), ("app", "default")]
    )


def test_stale_row_warning_names_the_missing_dependency(missing_dependency_config: Path, capsys) -> None:
    code, output = _run(missing_dependency_config, capsys, "info", "trackable", "main:a")

    assert code == 0, output
    [warning] = [line for line in _warnings(output) if "main:app@default" in line]
    assert "nope" in warning, warning


def test_list_tracked_reason_names_the_missing_dependency(missing_dependency_config: Path, capsys) -> None:
    capsys.readouterr()
    main(["--config", str(missing_dependency_config), "--json", "list", "tracked"])
    payload = json.loads(capsys.readouterr().out)

    [entry] = payload["invalid_package_entries"]
    assert "nope" in entry["message"], entry
