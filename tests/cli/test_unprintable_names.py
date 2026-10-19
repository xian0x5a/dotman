"""Names the user writes must be printable (issue #295, same rule as #272 for live file names).

Package, group, profile and target names, and repo names in the manager config, are
rejected at load when `str.isprintable()` fails. The rejection is a usage error
(exit 2), names the file and field, and shows the name escaped: the raw character
never reaches the terminal. Every unprintable character is escaped, tab and newline
included, so a name never carries layout; a backslash is shown as written.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dotman.cli import main

NBSP = "\xa0"
ZERO_WIDTH_JOINER = "\u200d"
BIDI_OVERRIDE = "\u202e"
ESCAPE_CONTROL = "\x1b"

# Which unprintable characters each kind of name is probed with. NBSP is
# already rejected for package, group and target names (they reject any
# `isspace()` character), but the error echoes it raw; profile and repo names
# accept it silently. ZWJ, bidi and control characters pass every kind today.
REJECTED_CHARACTERS = {
    "package": (NBSP, ZERO_WIDTH_JOINER, ESCAPE_CONTROL),
    "group": (NBSP, BIDI_OVERRIDE, ESCAPE_CONTROL),
    "profile": (NBSP, ZERO_WIDTH_JOINER, BIDI_OVERRIDE, ESCAPE_CONTROL),
    "target": (NBSP, ZERO_WIDTH_JOINER, BIDI_OVERRIDE),
    "repo": (NBSP, ZERO_WIDTH_JOINER, BIDI_OVERRIDE, ESCAPE_CONTROL),
}

# The file and field an error for each kind of name must point at, in the wording
# existing manifest errors already use. Package, group and profile IDs come from
# file names, so their error names the file by kind; target names and repo names
# live inside a file, which is named by its base name.
ERROR_LOCATION = {
    "package": ("package manifest", "package id"),
    "group": ("group file", "group id"),
    "profile": ("profile file", "profile id"),
    "target": ("package.toml", "target"),
    "repo": ("config.toml", "repo"),
}


def write_repo(
    tmp_path: Path,
    *,
    package: str = "app",
    group: str = "stack",
    profile: str = "work",
    target: str = "conf",
    repo_name: str = "fixture",
    state_key: str | None = None,
) -> Path:
    """Write a one-repo fixture whose authored names are chosen by the caller; return the config path."""
    repo_root = tmp_path / "repo"
    # `json.dumps` writes a TOML-compatible quoted key, escaping control characters
    # that TOML forbids raw.
    package_manifest = (
        f"[targets.{json.dumps(target)}]\n"
        'source = "files/a"\n'
        'path = "~/a"\n'
    )
    files = {
        "profiles/default.toml": "",
        f"profiles/{profile}.toml": "",
        f"groups/{group}.toml": f"members = [{json.dumps(package)}]\n",
        f"packages/{package}/package.toml": package_manifest,
    }
    for relative_path, text in files.items():
        path = repo_root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    config_path = tmp_path / "config.toml"
    state_key_line = "" if state_key is None else f"state_key = {json.dumps(state_key)}\n"
    config_path.write_text(
        f"[repos.{json.dumps(repo_name)}]\npath = {json.dumps(str(repo_root))}\norder = 10\n{state_key_line}",
        encoding="utf-8",
    )
    return config_path


def run_list_tracked(config_path: Path, capsys: pytest.CaptureFixture[str]) -> tuple[int, str]:
    """Run `dotman list tracked`, which loads every package, group and profile of every repo."""
    try:
        exit_code = main(["--config", str(config_path), "--no-confirm", "list", "tracked"])
    except SystemExit as exit_info:  # argparse exits instead of returning
        exit_code = exit_info.code
    captured = capsys.readouterr()
    return exit_code, captured.out + captured.err


@pytest.mark.parametrize(
    ("kind", "character"),
    [
        pytest.param(kind, character, id=f"{kind}-U+{ord(character):04X}")
        for kind, characters in REJECTED_CHARACTERS.items()
        for character in characters
    ],
)
def test_unprintable_authored_name_is_rejected_and_shown_escaped(
    kind: str, character: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bad_name = f"work{character}laptop"
    escaped_name = f"work\\u{ord(character):04x}laptop"
    config_path = write_repo(tmp_path, **{("repo_name" if kind == "repo" else kind): bad_name})

    exit_code, output = run_list_tracked(config_path, capsys)

    file_text, field_text = ERROR_LOCATION[kind]
    assert exit_code == 2, output
    assert escaped_name in output
    assert file_text in output
    assert field_text in output
    assert character not in output


def test_printable_non_ascii_names_and_profile_spaces_load(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config_path = write_repo(
        tmp_path,
        package="café",
        group="日本",
        profile="work laptop",
        target="日本",
        repo_name="café",
    )

    exit_code, output = run_list_tracked(config_path, capsys)

    assert exit_code == 0, output


@pytest.mark.parametrize(
    ("kind", "name", "shown"),
    [
        pytest.param("package", "a\\b", "a\\b", id="backslash-package"),
        pytest.param("group", "a\\b", "a\\b", id="backslash-group"),
        pytest.param("package", "a\tb", "a\\u0009b", id="tab"),
        pytest.param("package", "a\nb", "a\\u000ab", id="newline"),
    ],
)
def test_error_text_escapes_every_unprintable_character_in_a_name_but_not_backslashes(
    kind: str, name: str, shown: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A name is never layout: tab and newline are escaped like any control character. A backslash is ordinary text."""
    config_path = write_repo(tmp_path, **{kind: name})

    exit_code, output = run_list_tracked(config_path, capsys)

    assert exit_code == 2, output
    assert f"invalid {kind} id '{shown}'" in output


@pytest.mark.parametrize("character", [NBSP, BIDI_OVERRIDE, ESCAPE_CONTROL], ids=lambda c: f"U+{ord(c):04X}")
def test_unprintable_state_key_is_rejected_at_load(
    character: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Mid-name: a leading or trailing NBSP is stripped before the key is checked.
    config_path = write_repo(tmp_path, state_key=f"work{character}laptop")

    exit_code, output = run_list_tracked(config_path, capsys)

    assert exit_code == 2, output
    assert "config.toml" in output
    assert "state_key" in output
    assert character not in output
