"""The printable rule: what authored identity may contain, and how outside text is shown.

`str.isprintable` is the one predicate. Text the user authors as identity (names,
target `source` and `path`, a repo `path`) must pass it at load, so it is always
printed as written. Text from outside dotman (file names found on disk, OS and
exception messages, rows an older dotman stored) is escaped by it where it enters output.
"""

from __future__ import annotations

from dotman.errors import UsageError

# Package, group and repo names are selectors in `repo:selector@profile` and
# `repo:package<profile>.target`, so they can't hold any of that syntax.
SELECTOR_ID_RESERVED_CHARACTERS = ("\\", ":", "@", "<", ">", ".")

# Profile IDs sit inside `@profile` and `<profile>`, where dots, spaces and
# other punctuation are allowed; only the characters that delimit them are reserved.
PROFILE_ID_RESERVED_CHARACTERS = ("@", "<", ">")

KEPT_LAYOUT_CHARACTERS = "\n\t"


def _shown(character: str) -> str:
    """One character as itself when printable, else its escape: `\\xNN` for an undecodable byte, `\\uNNNN` otherwise."""
    code = ord(character)
    if 0xDC80 <= code <= 0xDCFF:  # os.fsdecode keeps an undecodable byte as a lone surrogate
        return f"\\x{code - 0xDC00:02x}"
    if not character.isprintable():
        return f"\\u{code:04x}" if code <= 0xFFFF else f"\\U{code:08x}"
    return character


def printable_text(text: str) -> str:
    """Printable, injective form of a file name or message for terminal output.

    Backslashes are doubled on top of `_shown`'s escapes. Used as a stand-in
    identity for a child path that has no canonical one, the result stays
    non-canonical and never collides with another child or with a canonical identity.
    """
    return "".join("\\\\" if character == "\\" else _shown(character) for character in text)


def terminal_safe_line(text: str) -> str:
    """Outside text for a person to read inside one line: every unprintable character is escaped, tab and newline included.

    Use it where text dotman does not control enters a row or a labelled line
    (a live file name, an environment value, a row an older dotman stored), so
    it cannot start a new line or shift a column. Not injective: a backslash
    stays a backslash. Outside text that may span lines uses `terminal_safe_text`;
    Sync identities use `printable_text`. Authored identity is validated at load
    and printed as written.
    """
    return "".join(_shown(character) for character in text)


def terminal_safe_text(text: str) -> str:
    """Outside text that may span lines: unprintable characters are escaped, but newline and tab stay.

    Only a message that may run over several lines keeps its layout: an error's
    detail, a Sync diagnostic or a failure message. Anything inside a one-line row
    or field uses `terminal_safe_line`.
    """
    return "".join(
        character if character in KEPT_LAYOUT_CHARACTERS else _shown(character) for character in text
    )


class UnprintableTextError(UsageError):
    """Authored text failed the printable rule; `detail` and `hint` are what the CLI prints as labelled lines."""

    def __init__(self, detail: str, hint: str | None) -> None:
        self.detail = detail
        self.hint = hint
        super().__init__(detail)


def validate_printable(text: str, *, kind: str, context: str | None = None, hint: str | None = None) -> None:
    """Reject authored identity (a name or a path) that a terminal could show as something else.

    Control characters, bidi overrides, zero-width joiners, non-ASCII spaces and
    private-use glyphs (such as Nerd Font icons) pass for ordinary text, so they
    are refused up front. The error echoes the value escaped on one line and,
    given a `context` such as the file or entry being loaded, starts with it.
    A `hint` tells the user how to get past the refusal.
    """
    if not text.isprintable():
        prefix = "" if context is None else f"{context}: "
        raise UnprintableTextError(
            f"{prefix}invalid {kind} '{terminal_safe_line(text)}': must contain only printable characters", hint
        )
