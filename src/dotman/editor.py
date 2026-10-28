"""The one rule dotman uses to find and run the user's editor.

It follows Git's own lookup so `edit`, `add`, `doctor` and Sync Editors agree:
an explicit command, `VISUAL`, `EDITOR`, `GIT_EDITOR`, Git's `core.editor`,
then the first common editor found on PATH. It also runs the editor the way Git
does: through the shell when the value uses shell syntax, otherwise directly.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
from typing import Callable, Sequence

from dotman.command_runtime import ArgvCommand, CommandRequest, current_command_runtime


EDITOR_ENVIRONMENT_VARIABLES = ("VISUAL", "EDITOR", "GIT_EDITOR")
GIT_CORE_EDITOR_QUERY = ("git", "config", "--get", "core.editor")
FALLBACK_EDITORS = ("sensible-editor", "editor", "nvim", "vim", "vi", "nano")
# Diff flags such as `nvim -d` would turn dotman's file arguments into a diff.
STRIPPED_EDITOR_ARGUMENTS = frozenset({"-d"})
# Git's run-command hands a command to the shell when it contains one of these.
# Spaces, quotes and backslashes are left out: shlex splits those as sh would.
SHELL_METACHARACTERS = frozenset("|&;<>()$`*?[#~=%")
# One shell word: unquoted characters other than operators, quoted strings and
# escapes. The first simple command runs up to the first operator or newline.
_SHELL_WORD = re.compile(r"""(?:[^\s|&;<>()'"\\]|'[^']*'|"(?:[^"\\]|\\.)*"|\\.)+""")
_FIRST_SIMPLE_COMMAND = re.compile(rf"[ \t]*(?:{_SHELL_WORD.pattern}[ \t]*)*")

# PATH lookup for the fallback editors. Tests replace it so a developer's real
# editors never satisfy the chain.
find_executable: Callable[[str], str | None] = shutil.which


def editor_command(paths: Sequence[str], *, explicit: str | None = None) -> ArgvCommand | None:
    """The command that opens `paths` in the editor, or None when the chain is empty.

    Raises ValueError when the configured command cannot be used.
    """
    value = _resolve_editor_value(explicit)
    if value is None:
        return None
    if SHELL_METACHARACTERS.isdisjoint(value):
        return ArgvCommand((*_parse_editor_command(value), *paths))
    script = _drop_stripped_arguments_from_first_command(value)
    if "$" not in value:
        # Git's form: the shell syntax cannot name the files, so they follow it.
        script = f'{script} "$@"'
    # A value that uses `$` places the files itself, through "$@" or "$1".
    # sh -c reserves its first argument for $0; the files start at $1.
    return ArgvCommand(("/bin/sh", "-c", script, "dotman-editor", *paths))


def _resolve_editor_value(explicit: str | None = None) -> str | None:
    """The editor command text as configured, or None when the chain is empty."""
    if explicit:
        return explicit
    for variable in EDITOR_ENVIRONMENT_VARIABLES:
        value = os.environ.get(variable)
        if value:
            return value
    git_editor = _git_core_editor()
    if git_editor:
        return git_editor
    return next((candidate for candidate in FALLBACK_EDITORS if find_executable(candidate) is not None), None)


def resolve_editor_command(explicit: str | None = None) -> list[str] | None:
    """The editor argv, or None when the chain is empty.

    Raises ValueError when the configured command cannot be used.
    """
    value = _resolve_editor_value(explicit)
    return None if value is None else _parse_editor_command(value)


def _parse_editor_command(value: str) -> list[str]:
    try:
        arguments = shlex.split(value)
    except ValueError as exc:
        raise ValueError("editor command could not be parsed") from exc
    editor_command = [argument for argument in arguments if argument not in STRIPPED_EDITOR_ARGUMENTS]
    if not editor_command:
        raise ValueError("editor command is empty after parsing")
    return editor_command


def _drop_stripped_arguments_from_first_command(script: str) -> str:
    # Only the first simple command runs the editor; a `-d` after `;` or `|`
    # belongs to another command.
    command_end = _FIRST_SIMPLE_COMMAND.match(script).end()
    command = script[:command_end]
    # Delete each word with the blanks before it, so the rest keeps its spacing.
    for word in reversed(list(_SHELL_WORD.finditer(command))):
        if word.group() in STRIPPED_EDITOR_ARGUMENTS:
            command = command[:word.start()].rstrip(" \t") + command[word.end():]
    return command + script[command_end:]


def _git_core_editor() -> str | None:
    # A missing git, or one that cannot run, means Git has no editor to offer.
    try:
        result = current_command_runtime().run(
            CommandRequest(command=ArgvCommand(GIT_CORE_EDITOR_QUERY), io="pipe", elevation="none")
        )
    except (OSError, ValueError):
        return None
    if result.exit_code != 0:
        return None
    return result.stdout_text.strip() or None
