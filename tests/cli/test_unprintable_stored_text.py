"""Tracked rows an older dotman wrote stay visible, escaped and removable (issues #295, #304).

Before names were checked for printability at load, a tracked row's package ID or
profile could hold NBSP, ESC or a line break. Such a row is stale now, but it is
outside text: it was stored before validation and dotman did not control it, so
every label, warning and menu entry that shows it escapes it. The rows must
stay visible and removable, and the state file dotman rewrites must still load (#304).

Escaping is `\\uNNNN` per unprintable character, so a profile `work<NBSP>laptop`
shows as `work\\u00a0laptop`. Tab and newline are unprintable too: a label never
carries layout.
"""

from __future__ import annotations

import re
import shlex
import tomllib
from pathlib import Path

import pytest

from dotman.cli import main
from tests.helpers import write_multi_instance_repo, write_named_manager_config, write_tracked_packages_state

REPO_NAME = "main"
LEGACY_PACKAGE = "profiled"  # multi_instance, so its profile is part of how a row is labelled
OTHER_PACKAGE = "other"
OTHER_PROFILE = "basic"

NBSP_PROFILE = "work\xa0laptop"
ESCAPE_PROFILE = "work\x1b[31mred"
LAYOUT_PROFILE = "x\nerror: forged line\ty"
ESCAPED_LAYOUT_PROFILE = "x\\u000aerror: forged line\\u0009y"

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
    """Write `tracked-packages.toml` the way an older dotman did, whatever the package ID and profile hold."""
    write_tracked_packages_state(tmp_path / "state", repo_name=REPO_NAME, entries=rows)


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


def advertised_untrack_arguments(warning_text: str) -> list[str]:
    """The arguments of the `dotman untrack ...` command a stale-row warning ends with, split as a shell splits them."""
    stale_row_lines = [line for line in warning_text.splitlines() if "skipped stale tracked row" in line]
    assert len(stale_row_lines) == 1, warning_text
    # The command is the last thing on its line, so a user can copy from `dotman` to the end.
    advertised = re.search(r"dotman untrack .*$", stale_row_lines[0])
    assert advertised, f"the stale-row warning must advertise 'dotman untrack <selector>': {warning_text!r}"
    try:
        words = shlex.split(advertised.group(0))
    except ValueError as shell_error:  # e.g. an unbalanced quote
        pytest.fail(f"the advertised command is not valid shell ({shell_error}): {advertised.group(0)!r}")
    assert words[:2] == ["dotman", "untrack"], words
    return words[2:]


@pytest.mark.parametrize(
    "legacy_profile",
    [
        pytest.param(NBSP_PROFILE, id="nbsp"),
        pytest.param(ESCAPE_PROFILE, id="esc"),
        # Printable, but a shell would split it or strip the quote.
        pytest.param("work laptop", id="space"),
        pytest.param("it's mine", id="apostrophe"),
    ],
)
def test_the_untrack_command_the_warning_advertises_removes_the_legacy_row_when_pasted_into_a_shell(
    legacy_profile: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Escaping the warning must not leave the user a command that matches nothing, or splits into two arguments."""
    config_path = write_repo_with_legacy_row(tmp_path, legacy_profile)
    _code, _stdout, warning_text = run_cli(config_path, capsys, "list", "vars")

    # A shell hands the program what `shlex.split` yields, so the advertised text must survive that.
    code, stdout, stderr = run_cli(config_path, capsys, "untrack", *advertised_untrack_arguments(warning_text))

    assert code == 0, stdout + stderr
    assert read_tracked_rows(tmp_path) == [(OTHER_PACKAGE, OTHER_PROFILE)]


def test_untracking_by_the_escaped_spelling_leaves_look_alike_sibling_rows(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config_path = write_repo_with_legacy_row(tmp_path, NBSP_PROFILE)
    write_tracked_rows(
        tmp_path,
        [(LEGACY_PACKAGE, NBSP_PROFILE), (LEGACY_PACKAGE, "work laptop"), (LEGACY_PACKAGE, OTHER_PROFILE)],
    )

    code, stdout, stderr = run_cli(config_path, capsys, "untrack", f"{REPO_NAME}:{LEGACY_PACKAGE}@work\\u00a0laptop")

    assert code == 0, stdout + stderr
    assert sorted(read_tracked_rows(tmp_path)) == [(LEGACY_PACKAGE, OTHER_PROFILE), (LEGACY_PACKAGE, "work laptop")]


@pytest.mark.parametrize(
    "argv",
    [
        pytest.param(["list", "tracked"], id="list-tracked"),
        # Any command that walks tracked state skips the stale row with a warning; `list vars` only reads.
        pytest.param(["list", "vars"], id="stale-row-warning"),
    ],
)
def test_a_legacy_profile_with_a_line_break_and_a_tab_cannot_forge_an_output_line(
    argv: list[str], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config_path = write_repo_with_legacy_row(tmp_path, LAYOUT_PROFILE)

    code, stdout, stderr = run_cli(config_path, capsys, *argv)

    output = stdout + stderr
    assert code == 0, output
    assert ESCAPED_LAYOUT_PROFILE in output
    assert [line for line in output.splitlines() if line.startswith("error: forged line")] == []


@pytest.mark.parametrize(
    ("legacy_package_id", "escaped_package_id"),
    [
        pytest.param("bad\x1b[31mpkg", "bad\\u001b[31mpkg", id="esc"),
        pytest.param("bad\nforged: row", "bad\\u000aforged: row", id="newline"),
    ],
)
@pytest.mark.parametrize(
    "argv",
    [
        pytest.param(["list", "tracked"], id="list-tracked"),
        # Any command that walks tracked state skips the stale row with a warning; `list vars` only reads.
        pytest.param(["list", "vars"], id="stale-row-warning"),
    ],
)
def test_a_legacy_package_id_is_shown_escaped_in_labels(
    legacy_package_id: str, escaped_package_id: str, argv: list[str], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config_path = write_repo_with_legacy_row(tmp_path, OTHER_PROFILE)
    write_tracked_rows(tmp_path, [(legacy_package_id, OTHER_PROFILE), (OTHER_PACKAGE, OTHER_PROFILE)])

    code, stdout, stderr = run_cli(config_path, capsys, *argv)

    output = stdout + stderr
    assert code == 0, output
    assert f"{REPO_NAME}:{escaped_package_id}" in output
    assert "\x1b" not in output
    assert [line for line in output.splitlines() if line.startswith("forged: row")] == []


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


# tomlkit writes ESC as `\e` (#304) and a literal backslash as `\\`, so `\e` also occurs inside
# `\\e`. A fix that replaces every `\e` in the file with `\u001b` corrupts a profile holding a backslash then `e`.
BACKSLASH_PROFILES = [
    pytest.param("a\\eb", id="backslash-then-e"),
    pytest.param("\\\x1b", id="backslash-then-esc"),
    pytest.param("\\\\e", id="two-backslashes-then-e"),
]


@pytest.mark.parametrize("profile", BACKSLASH_PROFILES)
def test_a_rewritten_tracked_state_reads_back_the_profile_exactly(
    profile: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config_path = write_repo_with_legacy_row(tmp_path, profile)

    # Untracking the other row makes dotman rewrite the whole file, legacy row included.
    code, stdout, stderr = run_cli(config_path, capsys, "untrack", f"{REPO_NAME}:{OTHER_PACKAGE}")

    assert code == 0, stdout + stderr
    assert read_tracked_rows(tmp_path) == [(LEGACY_PACKAGE, profile)]
