"""Text dotman did not validate at load never reaches the terminal raw (issues #295, #304).

Two kinds of stored text can hold characters a terminal misreads:

- Tracked rows an older dotman wrote, before profile IDs were checked for
  printability. A row's profile may hold NBSP or ESC, and the rows must stay
  visible, escaped, and removable.
- Package descriptions, which are free text rather than names.

Escaping is `\\uNNNN` per unprintable character, so a profile `work<NBSP>laptop`
shows as `work\\u00a0laptop`.
"""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

import pytest

from dotman.cli import main
from tests.helpers import write_multi_instance_repo, write_named_manager_config

REPO_NAME = "main"
LEGACY_PACKAGE = "profiled"  # multi_instance, so its profile is part of how a row is labelled
OTHER_PACKAGE = "other"
OTHER_PROFILE = "basic"

NBSP_PROFILE = "work\xa0laptop"
ESCAPE_PROFILE = "work\x1b[31mred"

# (profile an older dotman stored, the raw character in it, how it must be shown)
LEGACY_PROFILES = [
    pytest.param(NBSP_PROFILE, "\xa0", "work\\u00a0laptop", id="nbsp"),
    pytest.param(ESCAPE_PROFILE, "\x1b", "work\\u001b[31mred", id="esc"),
]


def write_repo_with_legacy_row(tmp_path: Path, legacy_profile: str) -> Path:
    """Write a repo and state where `main:profiled` is tracked under a profile that no longer loads.

    Returns the config path. The repo has two profiles, `basic` and `work`; the
    stored profile is neither, so the row is stale ("unknown profile").
    """
    repo_root = tmp_path / "repo"
    write_multi_instance_repo(repo_root)
    other_package = repo_root / "packages" / OTHER_PACKAGE
    (other_package / "files").mkdir(parents=True)
    (other_package / "files" / "other.conf").write_text("other\n", encoding="utf-8")
    (other_package / "package.toml").write_text(
        '[targets.conf]\nsource = "files/other.conf"\npath = "~/.config/other.conf"\n', encoding="utf-8"
    )
    config_path = write_named_manager_config(tmp_path, {REPO_NAME: repo_root})
    write_tracked_rows(tmp_path, [(LEGACY_PACKAGE, legacy_profile), (OTHER_PACKAGE, OTHER_PROFILE)])
    return config_path


def tracked_state_path(tmp_path: Path) -> Path:
    return tmp_path / "state" / "dotman" / "repos" / REPO_NAME / "tracked-packages.toml"


def write_tracked_rows(tmp_path: Path, rows: list[tuple[str, str]]) -> None:
    """Write `tracked-packages.toml` the way an older dotman did, whatever the profile holds.

    `json.dumps` quotes with `\\uNNNN` escapes, which TOML 1.0 reads back, so the
    file is valid even when a profile holds a control character.
    """
    lines = ["schema_version = 1", ""]
    for package_id, profile in rows:
        lines += [
            "[[packages]]",
            f"repo = {json.dumps(REPO_NAME)}",
            f"package_id = {json.dumps(package_id)}",
            f"profile = {json.dumps(profile)}",
            "",
        ]
    state_path = tracked_state_path(tmp_path)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text("\n".join(lines), encoding="utf-8")


def read_tracked_rows(tmp_path: Path) -> list[tuple[str, str]]:
    packages = tomllib.loads(tracked_state_path(tmp_path).read_text(encoding="utf-8")).get("packages", [])
    return [(row["package_id"], row["profile"]) for row in packages]


def run_cli(config_path: Path, capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    """Run one dotman command; return the exit code, stdout and stderr."""
    capsys.readouterr()
    try:
        exit_code = main(["--config", str(config_path), "--no-confirm", *argv])
    except SystemExit as exit_info:  # argparse exits instead of returning
        exit_code = exit_info.code
    captured = capsys.readouterr()
    return exit_code, captured.out, captured.err


def assert_shown_escaped(stdout: str, stderr: str, *, raw_character: str, escaped_profile: str) -> None:
    assert raw_character not in stdout, "raw character on stdout"
    assert raw_character not in stderr, "raw character on stderr"
    assert escaped_profile in stdout + stderr


@pytest.mark.parametrize(("legacy_profile", "raw_character", "escaped_profile"), LEGACY_PROFILES)
def test_list_tracked_shows_a_legacy_row_profile_escaped(
    legacy_profile: str, raw_character: str, escaped_profile: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config_path = write_repo_with_legacy_row(tmp_path, legacy_profile)

    _code, stdout, stderr = run_cli(config_path, capsys, "list", "tracked")

    assert_shown_escaped(stdout, stderr, raw_character=raw_character, escaped_profile=escaped_profile)


@pytest.mark.parametrize(("legacy_profile", "raw_character", "escaped_profile"), LEGACY_PROFILES)
def test_doctor_shows_a_legacy_row_profile_escaped(
    legacy_profile: str, raw_character: str, escaped_profile: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config_path = write_repo_with_legacy_row(tmp_path, legacy_profile)

    _code, stdout, stderr = run_cli(config_path, capsys, "doctor")

    assert_shown_escaped(stdout, stderr, raw_character=raw_character, escaped_profile=escaped_profile)


@pytest.mark.parametrize(("legacy_profile", "raw_character", "escaped_profile"), LEGACY_PROFILES)
def test_stale_row_warning_shows_the_legacy_profile_escaped(
    legacy_profile: str, raw_character: str, escaped_profile: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config_path = write_repo_with_legacy_row(tmp_path, legacy_profile)

    # Any command that walks tracked state skips the stale row with a warning; `list vars` only reads.
    code, stdout, stderr = run_cli(config_path, capsys, "list", "vars")

    assert code == 0, stdout + stderr
    assert "warning: skipped stale tracked row" in stderr
    assert_shown_escaped(stdout, stderr, raw_character=raw_character, escaped_profile=escaped_profile)


@pytest.mark.parametrize(("legacy_profile", "raw_character", "escaped_profile"), LEGACY_PROFILES)
def test_explicitly_selecting_a_legacy_row_package_shows_its_profile_escaped(
    legacy_profile: str, raw_character: str, escaped_profile: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The error that explains why `main:profiled` cannot be selected names the stale row."""
    config_path = write_repo_with_legacy_row(tmp_path, legacy_profile)

    _code, stdout, stderr = run_cli(config_path, capsys, "push", "--dry-run", f"{REPO_NAME}:{LEGACY_PACKAGE}")

    assert_shown_escaped(stdout, stderr, raw_character=raw_character, escaped_profile=escaped_profile)


@pytest.mark.parametrize(("legacy_profile", "raw_character", "escaped_profile"), LEGACY_PROFILES)
def test_untrack_result_line_shows_the_removed_legacy_profile_escaped(
    legacy_profile: str, raw_character: str, escaped_profile: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config_path = write_repo_with_legacy_row(tmp_path, legacy_profile)

    code, stdout, stderr = run_cli(config_path, capsys, "untrack", f"{REPO_NAME}:{LEGACY_PACKAGE}")

    assert code == 0, stdout + stderr
    assert stdout.startswith("untracked "), stdout
    assert_shown_escaped(stdout, stderr, raw_character=raw_character, escaped_profile=escaped_profile)


@pytest.mark.parametrize("legacy_profile", [NBSP_PROFILE, ESCAPE_PROFILE], ids=["nbsp", "esc"])
def test_the_untrack_command_the_warning_advertises_removes_the_legacy_row(
    legacy_profile: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Escaping the warning must not leave the user a command that matches nothing."""
    config_path = write_repo_with_legacy_row(tmp_path, legacy_profile)
    _code, _stdout, warning_text = run_cli(config_path, capsys, "list", "vars")
    advertised = re.search(r"'dotman untrack ([^']+)'", warning_text)
    assert advertised, f"the stale-row warning must advertise 'dotman untrack <selector>': {warning_text!r}"

    # The selector goes in as one argument, exactly as a user's shell would pass the text they copied.
    code, stdout, stderr = run_cli(config_path, capsys, "untrack", advertised.group(1))

    assert code == 0, stdout + stderr
    assert read_tracked_rows(tmp_path) == [(OTHER_PACKAGE, OTHER_PROFILE)]


@pytest.mark.parametrize(
    ("other_rows_before", "argv"),
    [
        pytest.param([], ["track", f"{REPO_NAME}:{OTHER_PACKAGE}@{OTHER_PROFILE}"], id="track"),
        pytest.param([(OTHER_PACKAGE, OTHER_PROFILE)], ["untrack", f"{REPO_NAME}:{OTHER_PACKAGE}"], id="untrack"),
    ],
)
def test_rewriting_tracked_state_keeps_a_legacy_control_character_row_readable(
    other_rows_before: list[tuple[str, str]],
    argv: list[str],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Issue #304: tomlkit writes ESC as `\\e`, TOML 1.1 syntax that dotman's own reader rejects."""
    config_path = write_repo_with_legacy_row(tmp_path, ESCAPE_PROFILE)
    write_tracked_rows(tmp_path, [(LEGACY_PACKAGE, ESCAPE_PROFILE), *other_rows_before])

    code, stdout, stderr = run_cli(config_path, capsys, *argv)
    assert code == 0, stdout + stderr

    state_text = tracked_state_path(tmp_path).read_text(encoding="utf-8")
    assert "\\e" not in state_text, "ESC must be written as \\u001b, which TOML 1.0 readers accept"
    next_code, next_stdout, next_stderr = run_cli(config_path, capsys, "list", "tracked")
    assert next_code == 0, next_stdout + next_stderr
    assert (LEGACY_PACKAGE, ESCAPE_PROFILE) in read_tracked_rows(tmp_path), "the legacy row must survive the rewrite"


DESCRIPTION_WITH_UNPRINTABLE = "safe\xa0text\N{RIGHT-TO-LEFT OVERRIDE}!\x1b[31m"
ESCAPED_DESCRIPTION_PARTS = ("safe\\u00a0text", "\\u202e", "\\u001b[31m")


@pytest.mark.parametrize(
    "argv",
    [
        pytest.param(["list", "trackables"], id="list-trackables"),
        pytest.param(["search", "app"], id="search"),
    ],
)
def test_package_description_is_shown_escaped(argv: list[str], tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo_root = tmp_path / "repo"
    (repo_root / "profiles").mkdir(parents=True)
    (repo_root / "profiles" / "default.toml").write_text("", encoding="utf-8")
    package_root = repo_root / "packages" / "app"
    (package_root / "files").mkdir(parents=True)
    (package_root / "files" / "a.conf").write_text("a\n", encoding="utf-8")
    (package_root / "package.toml").write_text(
        f"description = {json.dumps(DESCRIPTION_WITH_UNPRINTABLE)}\n"
        '[targets.conf]\nsource = "files/a.conf"\npath = "~/a.conf"\n',
        encoding="utf-8",
    )
    config_path = write_named_manager_config(tmp_path, {REPO_NAME: repo_root})

    code, stdout, stderr = run_cli(config_path, capsys, *argv)

    assert code == 0, stdout + stderr
    for escaped_part in ESCAPED_DESCRIPTION_PARTS:
        assert escaped_part in stdout
    for raw_character in ("\xa0", "\N{RIGHT-TO-LEFT OVERRIDE}", "\x1b"):
        assert raw_character not in stdout + stderr, f"raw U+{ord(raw_character):04X} reached the terminal"
