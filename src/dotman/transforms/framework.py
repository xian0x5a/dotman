#!/usr/bin/env python3

from __future__ import annotations

import argparse
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, time
from enum import StrEnum
import math
from pathlib import Path
import re
import sys
from typing import Any, ClassVar, Iterable, Protocol, runtime_checkable

from dotman.atomic_files import write_bytes_atomic, write_text_atomic
from dotman.errors import UsageError

STDIN_PATH = Path("-")
# A selector with this prefix removes paths from the selection instead of adding them.
EXCLUSION_PREFIX = "not:"
KeyPath = tuple[str, ...]


class TransformMode(StrEnum):
    CLEANUP = "cleanup"
    MERGE = "merge"


class SelectorAction(StrEnum):
    REMOVE = "remove"
    RETAIN = "retain"


def compile_selector_regexes(
    raw_regexes: Iterable[str],
    selector_description: str,
) -> tuple[re.Pattern[str], ...]:
    compiled_regexes: list[re.Pattern[str]] = []
    for raw_regex in raw_regexes:
        try:
            compiled_regexes.append(re.compile(raw_regex))
        except re.error as error:
            raise UsageError(
                f"invalid {selector_description} regex {raw_regex!r}: {error}"
            ) from error
    return tuple(compiled_regexes)


def has_prefix_in(key_path: KeyPath, key_paths: set[KeyPath]) -> bool:
    """Whether key_path or one of its ancestors is in key_paths."""
    return any(key_path[:length] in key_paths for length in range(1, len(key_path) + 1))


def subtract_excluded_key_paths(
    document_key_paths: tuple[KeyPath, ...],
    selected_key_paths: tuple[KeyPath, ...] | None,
    excluded_key_paths: tuple[KeyPath, ...],
) -> tuple[KeyPath, ...]:
    """Drop excluded paths and their subtrees from a selection.

    document_key_paths lists every key path of the base, and a selection of None
    stands for the whole document. A selected path holds its whole subtree, so
    one that contains an excluded path gives way to its children in the base,
    down to the excluded path.
    """
    if selected_key_paths is None:
        selected_key_paths = tuple(key_path for key_path in document_key_paths if len(key_path) == 1)
    selected = set(selected_key_paths)
    # Exclusions match the base like other selectors, so an exact exclusion the
    # base lacks excludes nothing and splits no selection.
    excluded = set(excluded_key_paths) & set(document_key_paths)
    excluded_ancestors = {
        key_path[:length] for key_path in excluded for length in range(1, len(key_path))
    }
    candidates = dict.fromkeys((*selected_key_paths, *document_key_paths))
    return tuple(
        key_path
        for key_path in candidates
        if has_prefix_in(key_path, selected)
        and not has_prefix_in(key_path, excluded)
        and key_path not in excluded_ancestors
    )


def without_emptied_mappings(selected: Mapping[Any, Any], base: Mapping[Any, Any]) -> dict[Any, Any]:
    """Drop the mappings that selection emptied; one already empty in base stays.

    selected must come from selecting base, so each of its keys is in base.
    Capture then reproduces a repo that deleted such a mapping, and Render
    drops one the repo deleted too.
    """
    kept: dict[Any, Any] = {}
    for key, value in selected.items():
        base_value = base[key]
        if isinstance(value, dict) and isinstance(base_value, dict):
            value = without_emptied_mappings(value, base_value)
            if base_value and not value:
                continue
        kept[key] = value
    return kept


def split_quoted_key_path(raw_key: str, format_name: str) -> tuple[str, ...]:
    """Split a dotted selector path; double quotes protect dots and allow ``""``.

    A quoted segment always produces a key, even when empty. An unquoted empty
    segment, as in ``a..b`` or ``a.``, is almost always a typo and is rejected.
    """
    if not raw_key:
        raise UsageError(f"{format_name} key paths must not be empty")
    parts: list[str] = []
    current: list[str] = []
    in_quotes = False
    segment_was_quoted = False
    escape = False

    def finish_segment() -> None:
        if not current and not segment_was_quoted:
            raise UsageError(
                f'empty segment in {format_name} key path {raw_key!r}; use "" for an empty key'
            )
        parts.append("".join(current))

    for char in raw_key:
        if in_quotes and escape:
            current.append(char)
            escape = False
            continue

        if in_quotes and char == "\\":
            escape = True
            continue

        if char == '"':
            in_quotes = not in_quotes
            segment_was_quoted = True
            continue

        if char == "." and not in_quotes:
            finish_segment()
            current = []
            segment_was_quoted = False
            continue

        current.append(char)

    if escape:
        current.append("\\")
    if in_quotes:
        raise UsageError(f"unterminated quoted {format_name} key path: {raw_key}")

    finish_segment()
    return tuple(parts)


def values_strictly_equal(left: Any, right: Any) -> bool:
    """Compare parsed values without Python's cross-type numeric equality.

    Plain ``==`` treats ``True == 1 == 1.0`` and ``0.0 == -0.0`` as equal, which
    would let compare-file reuse keep bytes that encode a different value.
    """
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        right_items_by_typed_key = {(type(key), key): value for key, value in right.items()}
        return len(left) == len(right) and all(
            (type(key), key) in right_items_by_typed_key
            and values_strictly_equal(value, right_items_by_typed_key[(type(key), key)])
            for key, value in left.items()
        )
    if isinstance(left, (list, tuple)):
        return len(left) == len(right) and all(
            values_strictly_equal(left_item, right_item)
            for left_item, right_item in zip(left, right, strict=True)
        )
    if isinstance(left, float):
        if math.isnan(left) or math.isnan(right):
            return math.isnan(left) and math.isnan(right)
        return left == right and math.copysign(1.0, left) == math.copysign(1.0, right)
    if isinstance(left, (datetime, time)):
        return left == right and left.utcoffset() == right.utcoffset()
    return left == right


@dataclass(frozen=True)
class SelectorSpec:
    name: str
    description: str
    prefix: str
    is_default: bool = False
    supported_modes: frozenset[TransformMode] = field(
        default_factory=lambda: frozenset({TransformMode.CLEANUP, TransformMode.MERGE})
    )

    def __post_init__(self) -> None:
        if not self.name:
            raise UsageError("selector spec name must not be empty")
        if not self.prefix:
            raise UsageError("selector spec prefix must not be empty")
        if not self.description:
            raise UsageError("selector spec description must not be empty")
        if not self.supported_modes:
            raise UsageError("selector spec supported_modes must not be empty")


@dataclass(frozen=True)
class TransformRequest:
    base_path: Path
    output_path: Path | None
    mode: TransformMode
    selector_action: SelectorAction
    selectors_by_type: Mapping[str, tuple[str, ...]]
    overlay_path: Path | None = None
    engine_options: Mapping[str, Any] = field(default_factory=dict)
    excluded_selectors_by_type: Mapping[str, tuple[str, ...]] = field(default_factory=dict)

    def validate_basic(self) -> None:
        if self.mode == TransformMode.MERGE and self.overlay_path is None:
            raise UsageError("overlay_path is required when mode=merge")
        if self.mode == TransformMode.CLEANUP and self.overlay_path is not None:
            raise UsageError("overlay_path is only valid when mode=merge")
        if self.output_path is None and not self.engine_option("stdout", False):
            raise UsageError("output_path is required unless stdout output is enabled")

    def selector_values(self, selector_type: str) -> tuple[str, ...]:
        return self.selectors_by_type.get(selector_type, ())

    def excluded_selector_values(self, selector_type: str) -> tuple[str, ...]:
        return self.excluded_selectors_by_type.get(selector_type, ())

    def has_included_selectors(self) -> bool:
        return any(self.selectors_by_type.values())

    def has_excluded_selectors(self) -> bool:
        return any(self.excluded_selectors_by_type.values())

    def has_selectors(self) -> bool:
        return self.has_included_selectors() or self.has_excluded_selectors()

    def engine_option(self, option_name: str, default: Any = None) -> Any:
        return self.engine_options.get(option_name, default)


def read_reference_bytes(path: Path | None, *, stdin_bytes: bytes | None = None) -> bytes | None:
    """Read an advisory file used only for formatting hints and compare reuse.

    Unlike engine inputs, a missing reference must not abort the transform:
    the live file a Render compares against may not exist yet. It just
    provides no hint.
    """
    if path is None:
        return None
    if path == STDIN_PATH:
        return stdin_bytes
    if path.is_file():
        return path.read_bytes()
    return None


def decode_reference_text(content: bytes | None) -> str | None:
    """Decode advisory bytes like engine inputs, or return None when they are not UTF-8."""
    if content is None:
        return None
    try:
        return content.decode("utf-8-sig")
    except UnicodeDecodeError:
        return None


def read_reference_text(path: Path | None, *, stdin_bytes: bytes | None = None) -> str | None:
    return decode_reference_text(read_reference_bytes(path, stdin_bytes=stdin_bytes))


@dataclass(frozen=True)
class TransformOutput:
    content: str | bytes
    mode_reference_path: Path | None
    reused_compare_path: Path | None = None

def reference_file_mode(reference_path: Path | None) -> int | None:
    # A stdin base ("-") has no permissions to inherit, even when the working
    # directory happens to contain a file literally named "-".
    if reference_path is None or reference_path == STDIN_PATH or not reference_path.exists():
        return None
    return reference_path.stat().st_mode & 0o777


def sync_output_mode(reference_path: Path | None, output_path: Path) -> None:
    target_mode = reference_file_mode(reference_path)
    if target_mode is None or not output_path.exists():
        return

    current_mode = output_path.stat().st_mode & 0o777
    if current_mode != target_mode:
        output_path.chmod(target_mode)


def decode_utf8_input(content: bytes, source: str) -> str:
    try:
        # utf-8-sig drops a leading byte order mark, which JSON and TOML
        # parsers reject; output is re-serialized without it.
        return content.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise ValueError(f"{source} is not valid UTF-8: {error}") from error


def read_input_text(path: Path, *, stdin_bytes: bytes | None = None) -> str | None:
    """Read strict UTF-8 input text, or None when the input file is missing.

    Bytes are decoded without newline translation so CRLF input is visible to
    engines that preserve line endings.
    """
    if path == STDIN_PATH:
        assert stdin_bytes is not None
        return decode_utf8_input(stdin_bytes, "stdin")
    if not path.exists():
        return None
    return decode_utf8_input(path.read_bytes(), str(path))



def write_output_to_stdout(output: TransformOutput) -> None:
    if isinstance(output.content, str):
        sys.stdout.write(output.content)
        return

    stdout_buffer = getattr(sys.stdout, "buffer", None)
    if stdout_buffer is not None:
        stdout_buffer.write(output.content)
        return

    # Non-interactive runners may replace stdout with a text-only stream like
    # io.StringIO. Decode with surrogateescape so byte-preserving compare reuse
    # still works when stdout is captured as text.
    stdout_encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    sys.stdout.write(output.content.decode(stdout_encoding, errors="surrogateescape"))



def write_output_to_path(output_path: Path, output: TransformOutput) -> None:
    # Resolve so a symlinked output updates its target instead of being
    # replaced by a regular file, and so differently spelled paths to the same
    # file are recognized as the reused compare file.
    output_path = output_path.resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Reusing the existing compare file is intentional. If that same path is also
    # the destination, skip the rewrite to preserve metadata like mtime.
    if (
        output.reused_compare_path is not None
        and output.reused_compare_path.resolve() == output_path
    ):
        sync_output_mode(output.mode_reference_path, output_path)
        return

    output_mode = reference_file_mode(output.mode_reference_path)
    if isinstance(output.content, bytes):
        write_bytes_atomic(output_path, output.content, mode=output_mode)
    else:
        write_text_atomic(output_path, output.content, mode=output_mode)



def emit_transform_output(
    output_path: Path | None,
    output: TransformOutput,
    *,
    stdout: bool = False,
) -> None:
    if stdout:
        write_output_to_stdout(output)
        return

    assert output_path is not None
    write_output_to_path(output_path, output)


@runtime_checkable
class TransformEngine(Protocol):
    name: str
    SUPPORTS_EXCLUDED_SELECTORS: bool

    @classmethod
    def selector_specs(cls) -> tuple[SelectorSpec, ...]:
        ...

    def requires_selectors(self) -> bool:
        ...

    def configure_parser(self, parser: argparse.ArgumentParser) -> None:
        ...

    def build_engine_options(
        self,
        parsed_args: argparse.Namespace,
    ) -> Mapping[str, Any]:
        ...

    def validate_request(self, request: TransformRequest) -> None:
        ...

    def transform(self, request: TransformRequest) -> TransformOutput:
        ...


class BaseTransformEngine(ABC):
    name: ClassVar[str]
    SELECTOR_SPECS: ClassVar[tuple[SelectorSpec, ...]]
    SUPPORTS_EXCLUDED_SELECTORS: ClassVar[bool] = False

    @classmethod
    def selector_specs(cls) -> tuple[SelectorSpec, ...]:
        return cls.SELECTOR_SPECS

    @classmethod
    def selector_spec_map(cls) -> dict[str, SelectorSpec]:
        return {spec.name: spec for spec in cls.selector_specs()}

    def requires_selectors(self) -> bool:
        return True

    def configure_parser(self, parser: argparse.ArgumentParser) -> None:
        del parser

    def build_engine_options(
        self,
        parsed_args: argparse.Namespace,
    ) -> Mapping[str, Any]:
        del parsed_args
        return {}

    def validate_request(self, request: TransformRequest) -> None:
        request.validate_basic()
        if self.requires_selectors() and not request.has_selectors():
            raise UsageError("at least one selector value is required")
        if request.has_excluded_selectors() and not self.SUPPORTS_EXCLUDED_SELECTORS:
            raise UsageError(f"{self.name} does not support {EXCLUSION_PREFIX} selectors")

        supported_specs = self.selector_spec_map()
        selector_types = {
            selector_type: request.selector_values(selector_type)
            + request.excluded_selector_values(selector_type)
            for selector_type in (*request.selectors_by_type, *request.excluded_selectors_by_type)
        }
        unknown_selector_types = sorted(
            selector_type
            for selector_type in selector_types
            if selector_type not in supported_specs
        )
        if unknown_selector_types:
            raise UsageError(
                f"{self.name} does not support selector types: {', '.join(unknown_selector_types)}"
            )

        unsupported_mode_selector_types = sorted(
            selector_type
            for selector_type, values in selector_types.items()
            if selector_type in supported_specs
            and values
            and request.mode not in supported_specs[selector_type].supported_modes
        )
        if unsupported_mode_selector_types:
            raise UsageError(
                f"{self.name} selector types not supported in {request.mode.value} mode: "
                f"{', '.join(unsupported_mode_selector_types)}"
            )

    @abstractmethod
    def transform(self, request: TransformRequest) -> TransformOutput:
        raise NotImplementedError
