"""Terminal-safe text."""

from __future__ import annotations


def printable_text(text: str) -> str:
    """Printable, injective form of a file name or message for terminal output.

    Backslashes are doubled, undecodable bytes become `\\xNN` and other
    unprintable characters `\\uNNNN`. Used as a stand-in identity for a child
    path that has no canonical one, the result stays non-canonical and never
    collides with another child or with a canonical identity.

    `str.isprintable` is the rule. Names the user writes (package, group, profile,
    target and repo names) must pass it at load, so only text dotman observes or
    echoes (file names, error messages) needs this escaping.
    """
    def escape(character: str) -> str:
        code = ord(character)
        if character == "\\":
            return "\\\\"
        if 0xDC80 <= code <= 0xDCFF:  # os.fsdecode keeps an undecodable byte as a lone surrogate
            return f"\\x{code - 0xDC00:02x}"
        if not character.isprintable():
            return f"\\u{code:04x}" if code <= 0xFFFF else f"\\U{code:08x}"
        return character
    return "".join(escape(character) for character in text)
