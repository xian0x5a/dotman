from __future__ import annotations

import json
import math
from pathlib import Path
import re
from typing import Any

from dotman.transforms.framework import (
    BaseTransformEngine,
    SelectorSpec,
    TransformMode,
    TransformOutput,
    TransformRequest,
    compile_selector_regexes,
    iter_key_paths,
    merge_overlay_data,
    select_data,
    select_key_paths,
    identity_key_text,
    subtract_excluded_key_paths,
    split_quoted_key_path,
    values_strictly_equal,
    read_input_text,
    decode_reference_text,
    read_reference_bytes,
    read_reference_text,
    without_emptied_mappings,
)


JsonDict = dict[str, Any]
JsonKeyPath = tuple[str, ...]
# The empty key path selects the whole document.
DOCUMENT_ROOT_KEY_PATH: JsonKeyPath = ()
KeyRegex = re.Pattern[str]
DEFAULT_JSON_INDENT = "  "
_JSON_INDENT_RE = re.compile(r"^([ \t]+)\S")
_LONE_SURROGATE_RE = re.compile("[\ud800-\udfff]")


def parse_finite_json_float(number_text: str) -> float:
    # json.loads turns an out-of-range number such as 1e400 into inf, which
    # would be written back as the invalid JSON token Infinity.
    value = float(number_text)
    if math.isinf(value):
        raise ValueError(f"JSON number {number_text} is out of float range")
    return value


def reject_non_json_constant(token: str) -> float:
    # json.loads accepts NaN and Infinity as an extension, but they would be
    # written back as the same invalid JSON tokens.
    raise ValueError(f"{token} is not a valid JSON number")


def load_json(path: Path, *, stdin_bytes: bytes | None = None) -> JsonDict:
    source_text = read_input_text(path, stdin_bytes=stdin_bytes)
    if source_text is None:
        return {}

    loaded = json.loads(
        source_text,
        parse_float=parse_finite_json_float,
        parse_constant=reject_non_json_constant,
    )
    if not isinstance(loaded, dict):
        raise ValueError(f"Expected top-level JSON object in {path}")
    return loaded



def compile_key_regexes(raw_key_regexes: tuple[str, ...]) -> tuple[KeyRegex, ...]:
    return compile_selector_regexes(raw_key_regexes, "JSON key selector")



def parse_json_key_path(raw_key: str) -> JsonKeyPath:
    return split_quoted_key_path(raw_key, "JSON")


def parse_json_key_paths(raw_key_paths: tuple[str, ...]) -> tuple[JsonKeyPath, ...]:
    return tuple(parse_json_key_path(raw_key) for raw_key in raw_key_paths)



def detect_json_indent(text: str) -> str | None:
    for line in text.splitlines():
        match = _JSON_INDENT_RE.match(line)
        if match:
            return match.group(1)
    return None



def detect_json_indent_from_text(text: str | None) -> str | None:
    if text is None:
        return None
    try:
        json.loads(text)
    except ValueError:
        return None

    return detect_json_indent(text)



def select_json_indent(*reference_paths: Path | None, stdin_bytes: bytes | None = None) -> str:
    for reference_path in reference_paths:
        indent = detect_json_indent_from_text(
            read_reference_text(reference_path, stdin_bytes=stdin_bytes)
        )
        if indent is not None:
            return indent
    return DEFAULT_JSON_INDENT



def json_text(data: JsonDict, indent: str = DEFAULT_JSON_INDENT) -> str:
    text = json.dumps(data, indent=indent, ensure_ascii=False)
    # JSON permits lone surrogate escapes such as "\ud800", but UTF-8 cannot
    # encode them raw, so keep those (and only those) escaped.
    return _LONE_SURROGATE_RE.sub(lambda match: f"\\u{ord(match.group()):04x}", text) + "\n"



def get_existing_bytes_if_semantically_unchanged(path: Path, data: JsonDict) -> bytes | None:
    existing_bytes = read_reference_bytes(path)
    existing_text = decode_reference_text(existing_bytes)
    if existing_text is None:
        return None
    try:
        existing_data = json.loads(existing_text)
    except Exception:
        return None

    if not values_strictly_equal(existing_data, data):
        return None

    return existing_bytes



def build_json_output(
    data: JsonDict,
    *,
    mode_reference_path: Path | None,
    compare_path: Path | None = None,
    indent_reference_paths: tuple[Path | None, ...] = (),
    stdin_bytes: bytes | None = None,
) -> TransformOutput:
    if compare_path is not None:
        existing_bytes = get_existing_bytes_if_semantically_unchanged(compare_path, data)
        if existing_bytes is not None:
            return TransformOutput(
                content=existing_bytes,
                mode_reference_path=mode_reference_path,
                reused_compare_path=compare_path,
            )

    indent = select_json_indent(compare_path, *indent_reference_paths, stdin_bytes=stdin_bytes)
    return TransformOutput(
        content=json_text(data, indent=indent),
        mode_reference_path=mode_reference_path,
    )



class JsonTransformEngine(BaseTransformEngine):
    name = "json"
    SUPPORTS_EXCLUDED_SELECTORS = True
    SELECTOR_SPECS = (
        SelectorSpec(
            name="key",
            prefix="exact",
            is_default=True,
            description="exact dotted or quoted nested JSON object key path",
        ),
        SelectorSpec(
            name="key_regex",
            prefix="re",
            description="regex matching full JSON object key paths",
        ),
    )

    def requires_selectors(self) -> bool:
        return False

    def configure_parser(self, parser) -> None:
        parser.add_argument(
            "--compare-file",
            type=Path,
            help="Optional JSON file to compare against for semantic no-op text reuse",
        )

    def build_engine_options(self, parsed_args) -> dict[str, Any]:
        return {
            "compare_path": parsed_args.compare_file,
            "stdout": parsed_args.stdout,
            "stdin_bytes": parsed_args.stdin_bytes,
        }

    def validate_request(self, request: TransformRequest) -> None:
        super().validate_request(request)
        parse_json_key_paths(request.selector_values("key"))
        compile_key_regexes(request.selector_values("key_regex"))
        parse_json_key_paths(request.excluded_selector_values("key"))
        compile_key_regexes(request.excluded_selector_values("key_regex"))

    def transform(self, request: TransformRequest) -> TransformOutput:
        self.validate_request(request)
        exact_key_paths = parse_json_key_paths(request.selector_values("key"))
        selected_key_regexes = compile_key_regexes(request.selector_values("key_regex"))

        base_data = load_json(
            request.base_path,
            stdin_bytes=request.engine_option("stdin_bytes"),
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
                    parse_json_key_paths(request.excluded_selector_values("key")),
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
            overlay_data = load_json(
                request.overlay_path,
                stdin_bytes=request.engine_option("stdin_bytes"),
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

        return build_json_output(
            transformed_data,
            mode_reference_path=request.base_path,
            compare_path=request.engine_option("compare_path"),
            indent_reference_paths=(request.base_path, request.overlay_path),
            stdin_bytes=request.engine_option("stdin_bytes"),
        )
