from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping

from dotman.errors import UsageError


_HOME_SHORTHAND = re.compile(r"~")
_WORD_CHARACTER = re.compile(r"\w")
# Treat path/name punctuation as attachment on the left to protect URLs and
# escapes. Slash stays open on the right so genuine child paths still rewrite.
_LEFT_ATTACHMENT_PUNCTUATION = frozenset(".~+-/\\")
_RIGHT_ATTACHMENT_PUNCTUATION = frozenset(".~+-\\")


def _is_word_or_mark(character: str) -> bool:
    return bool(_WORD_CHARACTER.fullmatch(character)) or unicodedata.category(character).startswith("M")


def _is_standalone_fragment(text: str, *, start: int, end: int) -> bool:
    left = text[start - 1] if start > 0 else ""
    right = text[end] if end < len(text) else ""
    left_attached = bool(left) and (
        _is_word_or_mark(left) or left in _LEFT_ATTACHMENT_PUNCTUATION
    )
    right_attached = bool(right) and (
        _is_word_or_mark(right) or right in _RIGHT_ATTACHMENT_PUNCTUATION
    )
    return not left_attached and not right_attached


def active_home_path(environment: Mapping[str, str]) -> str:
    home = environment.get("HOME", "")
    trimmed_home = home.rstrip("/")
    if not trimmed_home.startswith("/"):
        raise UsageError("$HOME must be a non-root absolute POSIX path")
    return trimmed_home


def _rewrite_standalone_fragments(
    text: str, fragment: re.Pattern[str], replacement: str, *, bare_tilde: bool
) -> str:
    def rewrite(match: re.Match[str]) -> str:
        # A bare `~` is not a home path in every format (YAML reads it as
        # null), so only child paths rewrite unless the caller opts in.
        is_child_path = text.startswith("/", match.end())
        if (bare_tilde or is_child_path) and _is_standalone_fragment(
            text, start=match.start(), end=match.end()
        ):
            return replacement
        return match.group()

    return fragment.sub(rewrite, text)


def expand_home_paths(text: str, *, home: str, bare_tilde: bool = False) -> str:
    """Expand standalone home shorthand fragments without parsing paths."""

    return _rewrite_standalone_fragments(text, _HOME_SHORTHAND, home, bare_tilde=bare_tilde)


def collapse_home_paths(text: str, *, home: str, bare_tilde: bool = False) -> str:
    """Collapse standalone active-home fragments without parsing paths."""

    return _rewrite_standalone_fragments(text, re.compile(re.escape(home)), "~", bare_tilde=bare_tilde)
