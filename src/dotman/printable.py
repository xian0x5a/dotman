"""The printable rule: what a terminal may show raw, and what a name the user writes may contain.

`str.isprintable` is the one predicate. Names must pass it at load, so they are
always shown as written, and any other text is escaped by it before it is shown.
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
    """Text for a person to read inside one line: every unprintable character is escaped, tab and newline included.

    Use it for names, paths and any other field of a row or a labelled line, so
    the text cannot start a new line or shift a column. Not injective: a
    backslash stays a backslash. Free text that may span lines uses
    `terminal_safe_text`; identities use `printable_text`.
    """
    return "".join(_shown(character) for character in text)


def terminal_safe_text(text: str) -> str:
    """Free text for a person to read as a block: unprintable characters are escaped, but newline and tab stay.

    Only a block of free text that may span lines keeps its layout: the `info`
    description, an error's detail, a Sync diagnostic or failure message, and a
    hook command. Anything inside a one-line row or field uses `terminal_safe_line`.
    """
    return "".join(
        character if character in KEPT_LAYOUT_CHARACTERS else _shown(character) for character in text
    )


def validate_printable_name(name: str, *, kind: str) -> None:
    """Reject a name the user wrote that a terminal could show as something else.

    Control characters, bidi overrides, zero-width joiners, non-ASCII spaces and
    private-use glyphs (such as Nerd Font icons) pass for ordinary text, so they
    are refused up front. The name is echoed escaped.
    """
    if not name.isprintable():
        raise UsageError(f"invalid {kind} '{terminal_safe_line(name)}': names must be printable characters")
