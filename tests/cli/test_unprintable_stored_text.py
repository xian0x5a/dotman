"""Text dotman did not validate at load never reaches the terminal raw (issues #295, #304).

Two kinds of stored text can hold characters a terminal misreads:

- Tracked rows an older dotman wrote, before profile IDs were checked for
  printability. A row's profile may hold NBSP or ESC, and the rows must stay
  visible, escaped, and removable.
- Package descriptions, which are free text rather than names.

Escaping is `\\uNNNN` per unprintable character, so a profile `work<NBSP>laptop`
shows as `work\\u00a0laptop`. Tab and newline are unprintable too: a name never
carries layout, while free text such as a description keeps its line breaks.
"""

from __future__ import annotations

import json
import re
import shlex
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


def advertised_untrack_arguments(warning_text: str) -> list[str]:
    """The arguments of the `dotman untrack ...` command a stale-row warning advertises, split as a shell splits them."""
    stale_row_lines = [line for line in warning_text.splitlines() if "skipped stale tracked row" in line]
    assert len(stale_row_lines) == 1, warning_text
    line = stale_row_lines[0]
    advertised = re.search(r"dotman untrack .*$", line)
    assert advertised, f"the stale-row warning must advertise 'dotman untrack <selector>': {warning_text!r}"
    command = advertised.group(0)
    # The command ends the line. It may sit inside a wrapper that opens just before `dotman`
    # ('dotman untrack ...' or `dotman untrack ...`); that wrapper is not part of the command.
    opener = line[advertised.start() - 1 : advertised.start()]
    if opener in {"'", "`", '"'} and command.endswith(opener):
        command = command[:-1]
    try:
        words = shlex.split(command)
    except ValueError as shell_error:  # e.g. an unbalanced quote
        pytest.fail(f"the advertised command is not valid shell ({shell_error}): {command!r}")
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


DESCRIPTION_WITH_UNPRINTABLE = "safe\xa0text\N{RIGHT-TO-LEFT OVERRIDE}!\x1b[31m"
ESCAPED_DESCRIPTION_PARTS = ("safe\\u00a0text", "\\u202e", "\\u001b[31m")


def write_repo_with_description(tmp_path: Path, description: str) -> Path:
    """Write a repo whose one package `app` carries `description`; return the config path."""
    repo_root = tmp_path / "repo"
    (repo_root / "profiles").mkdir(parents=True)
    (repo_root / "profiles" / "default.toml").write_text("", encoding="utf-8")
    package_root = repo_root / "packages" / "app"
    (package_root / "files").mkdir(parents=True)
    (package_root / "files" / "a.conf").write_text("a\n", encoding="utf-8")
    (package_root / "package.toml").write_text(
        f"description = {json.dumps(description)}\n"
        '[targets.conf]\nsource = "files/a.conf"\npath = "~/a.conf"\n',
        encoding="utf-8",
    )
    return write_named_manager_config(tmp_path, {REPO_NAME: repo_root})


@pytest.mark.parametrize(
    ("setup_argv", "argv"),
    [
        pytest.param(None, ["list", "trackables"], id="list-trackables"),
        pytest.param(None, ["search", "app"], id="search"),
        pytest.param(None, ["info", "trackable", f"{REPO_NAME}:app"], id="info-trackable"),
        # `info tracked` describes a package that is tracked, so track it first.
        pytest.param(["track", f"{REPO_NAME}:app@default"], ["info", "tracked", f"{REPO_NAME}:app"], id="info-tracked"),
    ],
)
def test_package_description_is_shown_escaped(
    setup_argv: list[str] | None, argv: list[str], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config_path = write_repo_with_description(tmp_path, DESCRIPTION_WITH_UNPRINTABLE)
    if setup_argv is not None:
        setup_code, setup_stdout, setup_stderr = run_cli(config_path, capsys, *setup_argv)
        assert setup_code == 0, setup_stdout + setup_stderr

    code, stdout, stderr = run_cli(config_path, capsys, *argv)

    assert code == 0, stdout + stderr
    for escaped_part in ESCAPED_DESCRIPTION_PARTS:
        assert escaped_part in stdout
    for raw_character in ("\xa0", "\N{RIGHT-TO-LEFT OVERRIDE}", "\x1b"):
        assert raw_character not in stdout + stderr, f"raw U+{ord(raw_character):04X} reached the terminal"


def test_package_description_keeps_its_line_breaks_and_tabs(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A description is free text written to be read, so only its unprintable characters are escaped, unlike a name."""
    config_path = write_repo_with_description(tmp_path, "first line\n\tsecond line\x1b[31m")

    code, stdout, stderr = run_cli(config_path, capsys, "info", "trackable", f"{REPO_NAME}:app")

    assert code == 0, stdout + stderr
    assert "first line\n\tsecond line\\u001b[31m" in stdout
