"""The one rule dotman uses to find and run the user's editor.

It follows Git's own lookup so `edit`, `add`, `doctor` and Sync Editors agree:
an explicit command, `VISUAL`, `EDITOR`, `GIT_EDITOR`, Git's `core.editor`,
then the first common editor found on PATH. It also runs the editor the way Git
does: through the shell when the value uses shell syntax, otherwise directly.
"""

from __future__ import annotations

import os
import shlex
import shutil
from typing import Callable, Sequence

from dotman.command_runtime import ArgvCommand, CommandRequest, current_command_runtime


EDITOR_ENVIRONMENT_VARIABLES = ("VISUAL", "EDITOR", "GIT_EDITOR")
GIT_CORE_EDITOR_QUERY = ("git", "config", "--get", "core.editor")
FALLBACK_EDITORS = ("sensible-editor", "editor", "nvim", "vim", "vi", "nano")
# Git's run-command hands a command to the shell when it contains one of these.
# Spaces, quotes and backslashes are left out: shlex splits those as sh would.
SHELL_METACHARACTERS = frozenset("|&;<>()$`*?[#~=%")

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
    # Git's form appends "$@": shell syntax without `$` cannot name the files.
    script = value if "$" in value else f'{value} "$@"'
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
    if not arguments:
        raise ValueError("editor command is empty after parsing")
    return arguments



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
