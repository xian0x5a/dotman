"""The one rule dotman uses to find the user's editor.

It follows Git's own lookup so `edit`, `add`, `doctor` and Sync Editors agree:
an explicit command, `VISUAL`, `EDITOR`, `GIT_EDITOR`, Git's `core.editor`,
then the first common editor found on PATH.
"""

from __future__ import annotations

import os
import shlex
import shutil
from typing import Callable

from dotman.command_runtime import ArgvCommand, CommandRequest, current_command_runtime


EDITOR_ENVIRONMENT_VARIABLES = ("VISUAL", "EDITOR", "GIT_EDITOR")
GIT_CORE_EDITOR_QUERY = ("git", "config", "--get", "core.editor")
FALLBACK_EDITORS = ("sensible-editor", "editor", "nvim", "vim", "vi", "nano")
# Diff flags such as `nvim -d` would turn dotman's file arguments into a diff.
STRIPPED_EDITOR_ARGUMENTS = frozenset({"-d"})

# PATH lookup for the fallback editors. Tests replace it so a developer's real
# editors never satisfy the chain.
find_executable: Callable[[str], str | None] = shutil.which


def resolve_editor_value(explicit: str | None = None) -> str | None:
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
    value = resolve_editor_value(explicit)
    return None if value is None else parse_editor_command(value)


def parse_editor_command(value: str) -> list[str]:
    try:
        arguments = shlex.split(value)
    except ValueError as exc:
        raise ValueError("editor command could not be parsed") from exc
    editor_command = [argument for argument in arguments if argument not in STRIPPED_EDITOR_ARGUMENTS]
    if not editor_command:
        raise ValueError("editor command is empty after parsing")
    return editor_command


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
