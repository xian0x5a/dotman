from __future__ import annotations

from pathlib import Path
import plistlib
import re
from typing import Any
from xml.parsers.expat import ExpatError

from dotman.transforms.framework import (
    BaseTransformEngine,
    SelectorSpec,
    TransformMode,
    TransformOutput,
    TransformRequest,
    STDIN_PATH,
    compile_selector_regexes,
    iter_key_paths,
    merge_overlay_data,
    select_data,
    select_key_paths,
    identity_key_text,
    subtract_excluded_key_paths,
    read_reference_bytes,
    split_quoted_key_path,
    values_strictly_equal,
    without_emptied_mappings,
)


PlistDict = dict[str, Any]
PlistKeyPath = tuple[str, ...]
# The empty key path selects the whole document.
DOCUMENT_ROOT_KEY_PATH: PlistKeyPath = ()
KeyRegex = re.Pattern[str]


def load_plist(path: Path, *, stdin_bytes: bytes | None = None) -> PlistDict:
    if path == STDIN_PATH:
        assert stdin_bytes is not None
        content = stdin_bytes
    elif not path.exists():
        return {}
    else:
        content = path.read_bytes()

    try:
        loaded = plistlib.loads(content)
    except (AttributeError, ExpatError, ValueError) as error:
        # plistlib reports malformed XML as ExpatError and a malformed value such
        # as `<date>nope</date>` as AttributeError, not only as ValueError.
        raise ValueError(f"invalid plist in {path}: {error}") from error

    if not isinstance(loaded, dict):
        raise ValueError(f"Expected plist dictionary in {path}")

    return loaded


def compile_key_regexes(raw_key_regexes: tuple[str, ...]) -> tuple[KeyRegex, ...]:
    return compile_selector_regexes(raw_key_regexes, "plist key path selector")


def parse_plist_key_path(raw_key: str) -> PlistKeyPath:
    return split_quoted_key_path(raw_key, "plist")


def parse_plist_key_paths(raw_key_paths: tuple[str, ...]) -> tuple[PlistKeyPath, ...]:
    return tuple(parse_plist_key_path(raw_key) for raw_key in raw_key_paths)


def plist_format_from_name(format_name: str) -> int:
    return plistlib.FMT_XML if format_name == "xml" else plistlib.FMT_BINARY


def plist_bytes(data: PlistDict, fmt: str) -> bytes:
    try:
        return plistlib.dumps(data, fmt=plist_format_from_name(fmt), sort_keys=True)
    except (TypeError, OverflowError) as error:
        # Some values only fit one format: UID needs binary, and both formats
        # cap integers at 64 bits.
        raise ValueError(f"cannot write {fmt} plist: {error}") from error


def get_existing_bytes_if_semantically_unchanged(
    path: Path,
    data: PlistDict,
) -> bytes | None:
    existing_bytes = read_reference_bytes(path)
    if existing_bytes is None:
        return None
    try:
        existing_data = plistlib.loads(existing_bytes)
    except Exception:
        return None

    if not values_strictly_equal(existing_data, data):
        return None

    return existing_bytes


def build_plist_output(
    data: PlistDict,
    output_format: str,
    *,
    mode_reference_path: Path,
    compare_path: Path | None,
) -> TransformOutput:
    if compare_path is not None:
        existing_bytes = get_existing_bytes_if_semantically_unchanged(compare_path, data)
        if existing_bytes is not None:
            return TransformOutput(
                content=existing_bytes,
                mode_reference_path=mode_reference_path,
                reused_compare_path=compare_path,
            )
    return TransformOutput(
        content=plist_bytes(data, output_format),
        mode_reference_path=mode_reference_path,
    )


class PlistTransformEngine(BaseTransformEngine):
    name = "plist"
    SUPPORTS_EXCLUDED_SELECTORS = True
    SELECTOR_SPECS = (
        SelectorSpec(
            name="key",
            prefix="exact",
            is_default=True,
            description="exact plist dictionary key path",
        ),
        SelectorSpec(
            name="key_regex",
            prefix="re",
            description="regex matching dotted plist dictionary key paths",
        ),
    )

    def requires_selectors(self) -> bool:
        return False

    def configure_parser(self, parser) -> None:
        parser.add_argument(
            "--compare-file",
            type=Path,
            help="Optional plist to compare against for semantic no-op byte reuse",
        )
        parser.add_argument(
            "--output-format",
            choices=("xml", "binary"),
            default="xml",
            help="Serialization format for the output plist",
        )

    def build_engine_options(self, parsed_args) -> dict[str, Any]:
        return {
            "compare_path": parsed_args.compare_file,
            "output_format": parsed_args.output_format,
            "stdout": parsed_args.stdout,
            "stdin_bytes": parsed_args.stdin_bytes,
        }

    def validate_request(self, request: TransformRequest) -> None:
        super().validate_request(request)
        parse_plist_key_paths(request.selector_values("key"))
        compile_key_regexes(request.selector_values("key_regex"))
        parse_plist_key_paths(request.excluded_selector_values("key"))
        compile_key_regexes(request.excluded_selector_values("key_regex"))

    def transform(self, request: TransformRequest) -> TransformOutput:
        self.validate_request(request)
        exact_key_paths = parse_plist_key_paths(request.selector_values("key"))
        selected_key_regexes = compile_key_regexes(request.selector_values("key_regex"))
        output_format = str(request.engine_option("output_format", "xml"))

        base_data = load_plist(
            request.base_path, stdin_bytes=request.engine_option("stdin_bytes")
        )
        selected_key_paths = select_key_paths(
            base_data,
            exact_key_paths,
            selected_key_regexes,
            key_text=identity_key_text,
        )
        if request.has_excluded_selectors():
            selected_key_paths = subtract_excluded_key_paths(
                iter_key_paths(base_data, key_text=identity_key_text),
                selected_key_paths if request.has_included_selectors() else None,
                select_key_paths(
                    base_data,
                    parse_plist_key_paths(request.excluded_selector_values("key")),
                    compile_key_regexes(request.excluded_selector_values("key_regex")),
                    key_text=identity_key_text,
                ),
            )
        # Only an absent selector list means identity. Selectors that match
        # nothing must still select nothing.
        transformed_data = (
            select_data(
                base_data, request.selector_action, selected_key_paths, key_text=identity_key_text
            )
            if request.has_selectors()
            else dict(base_data)
        )
        transformed_data = without_emptied_mappings(transformed_data, base_data)

        if request.mode == TransformMode.MERGE:
            assert request.overlay_path is not None
            overlay_data = load_plist(
                request.overlay_path, stdin_bytes=request.engine_option("stdin_bytes")
            )
            transformed_data = merge_overlay_data(
                base_data,
                transformed_data,
                overlay_data,
                # Without selectors the whole base is one selection, so the overlay
                # replaces each top-level key it shares with the base.
                selected_key_paths if request.has_selectors() else (DOCUMENT_ROOT_KEY_PATH,),
                key_text=identity_key_text,
            )

        return build_plist_output(
            transformed_data,
            output_format,
            mode_reference_path=request.base_path,
            compare_path=request.engine_option("compare_path"),
        )
