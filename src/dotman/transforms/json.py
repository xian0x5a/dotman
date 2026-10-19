#!/usr/bin/env python3

from __future__ import annotations

from dataclasses import dataclass, field
import json
import math
from pathlib import Path
import re
from typing import Any

from dotman.transforms.cli import run_engine_cli
from dotman.transforms.framework import (
    BaseTransformEngine,
    SelectorAction,
    SelectorSpec,
    TransformMode,
    TransformOutput,
    TransformRequest,
    compile_selector_regexes,
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
_MISSING = object()
_LONE_SURROGATE_RE = re.compile("[\ud800-\udfff]")


@dataclass
class JsonPathSelector:
    include_subtree: bool = False
    children: dict[str, "JsonPathSelector"] = field(default_factory=dict)


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



def build_json_path_selector(key_paths: tuple[JsonKeyPath, ...]) -> JsonPathSelector:
    root = JsonPathSelector()
    for key_path in key_paths:
        current = root
        for key_part in key_path:
            if current.include_subtree:
                break
            current = current.children.setdefault(key_part, JsonPathSelector())
        current.include_subtree = True
        current.children.clear()
    return root



def json_key_path_text(key_path: JsonKeyPath) -> str:
    return ".".join(key_path)



def matches_key_regexes(key_path: JsonKeyPath, key_regexes: tuple[KeyRegex, ...]) -> bool:
    path_text = json_key_path_text(key_path)
    return any(key_regex.search(path_text) for key_regex in key_regexes)



def iter_json_key_paths(value: Any, prefix: JsonKeyPath = ()) -> tuple[JsonKeyPath, ...]:
    if not isinstance(value, dict):
        return ()

    key_paths: list[JsonKeyPath] = []
    for key, child_value in value.items():
        key_path = prefix + (key,)
        key_paths.append(key_path)
        key_paths.extend(iter_json_key_paths(child_value, key_path))
    return tuple(key_paths)



def json_key_paths_matching_regexes(
    data: JsonDict,
    key_regexes: tuple[KeyRegex, ...],
) -> tuple[JsonKeyPath, ...]:
    if not key_regexes:
        return ()
    return tuple(
        key_path
        for key_path in iter_json_key_paths(data)
        if matches_key_regexes(key_path, key_regexes)
    )



def selected_json_key_paths(
    data: JsonDict,
    exact_key_paths: tuple[JsonKeyPath, ...],
    key_regexes: tuple[KeyRegex, ...],
) -> tuple[JsonKeyPath, ...]:
    return exact_key_paths + json_key_paths_matching_regexes(data, key_regexes)



def retained_json_value(value: Any, selector: JsonPathSelector) -> Any:
    if selector.include_subtree:
        return value
    if not isinstance(value, dict):
        return _MISSING

    retained_data: JsonDict = {}
    for key, child_value in value.items():
        child_selector = selector.children.get(key)
        if child_selector is None:
            continue
        retained_value = retained_json_value(child_value, child_selector)
        if retained_value is not _MISSING:
            retained_data[key] = retained_value

    if not retained_data:
        return _MISSING
    return retained_data



def stripped_json_value(value: Any, selector: JsonPathSelector) -> Any:
    if selector.include_subtree:
        return _MISSING
    if not isinstance(value, dict):
        return value

    stripped_data: JsonDict = {}
    for key, child_value in value.items():
        child_selector = selector.children.get(key)
        if child_selector is None:
            stripped_data[key] = child_value
            continue

        stripped_value = stripped_json_value(child_value, child_selector)
        if stripped_value is not _MISSING:
            stripped_data[key] = stripped_value

    return stripped_data



def filter_retained_keys(
    data: JsonDict,
    retained_key_paths: tuple[JsonKeyPath, ...],
    retained_key_regexes: tuple[KeyRegex, ...] = (),
) -> JsonDict:
    path_selector = build_json_path_selector(retained_key_paths)
    retained_data: JsonDict = {}
    for key, value in data.items():
        if matches_key_regexes((key,), retained_key_regexes):
            retained_data[key] = value
            continue

        child_selector = path_selector.children.get(key)
        if child_selector is None:
            continue

        retained_value = retained_json_value(value, child_selector)
        if retained_value is not _MISSING:
            retained_data[key] = retained_value

    return retained_data



def filter_stripped_keys(
    data: JsonDict,
    stripped_key_paths: tuple[JsonKeyPath, ...],
    stripped_key_regexes: tuple[KeyRegex, ...] = (),
) -> JsonDict:
    if not stripped_key_paths and not stripped_key_regexes:
        return dict(data)

    path_selector = build_json_path_selector(stripped_key_paths)
    stripped_data: JsonDict = {}
    for key, value in data.items():
        if matches_key_regexes((key,), stripped_key_regexes):
            continue

        child_selector = path_selector.children.get(key)
        if child_selector is None:
            stripped_data[key] = value
            continue

        stripped_value = stripped_json_value(value, child_selector)
        if stripped_value is not _MISSING:
            stripped_data[key] = stripped_value

    return stripped_data



def select_json_data(
    data: JsonDict,
    selector_action: SelectorAction,
    selected_key_paths: tuple[JsonKeyPath, ...],
    selected_key_regexes: tuple[KeyRegex, ...] = (),
) -> JsonDict:
    if selector_action == SelectorAction.REMOVE:
        return filter_stripped_keys(data, selected_key_paths, selected_key_regexes)
    return filter_retained_keys(data, selected_key_paths, selected_key_regexes)



def child_overlay_selector(selector: JsonPathSelector, key: str) -> JsonPathSelector:
    # Everything under a whole selection is whole too. A key no selector
    # reaches gets an empty selector, so merge still descends into it.
    if selector.include_subtree:
        return selector
    return selector.children.get(key) or JsonPathSelector()



def overlay_json_objects(
    original_base_data: JsonDict,
    preserved_base_data: JsonDict,
    overlay_data: JsonDict,
    path_selector: JsonPathSelector,
    whole_key_regexes: tuple[KeyRegex, ...] = (),
) -> JsonDict:
    merged_data: JsonDict = {}

    # Keep surviving keys in live order so repo-managed value changes do not also
    # produce noisy key-movement diffs.
    for key in original_base_data:
        child_selector = child_overlay_selector(path_selector, key)

        if key in overlay_data:
            overlay_value = overlay_data[key]
            base_value = original_base_data[key]
            # A whole selection is one value, so the overlay's copy replaces it. Any
            # other mapping in both base and overlay merges key by key, whether or
            # not selection kept any of it, so its keys keep live order and the live
            # keys selection kept survive.
            if (
                not child_selector.include_subtree
                and not matches_key_regexes((key,), whole_key_regexes)
                and isinstance(base_value, dict)
                and isinstance(overlay_value, dict)
            ):
                merged_data[key] = overlay_json_objects(
                    base_value,
                    preserved_base_data.get(key, {}),
                    overlay_value,
                    child_selector,
                    (),
                )
                continue

            merged_data[key] = overlay_value
            continue

        if key in preserved_base_data:
            merged_data[key] = preserved_base_data[key]

    for source_data in (overlay_data, preserved_base_data):
        for key, value in source_data.items():
            if key in merged_data:
                continue
            merged_data[key] = value

    return merged_data



def overlay_json_data(
    original_base_data: JsonDict,
    preserved_base_data: JsonDict,
    overlay_data: JsonDict,
    selected_key_paths: tuple[JsonKeyPath, ...] = (),
    selected_key_regexes: tuple[KeyRegex, ...] = (),
) -> JsonDict:
    return overlay_json_objects(
        original_base_data,
        preserved_base_data,
        overlay_data,
        build_json_path_selector(selected_key_paths),
        selected_key_regexes,
    )



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
            examples=("buildDir", "settings.window.width", '"key.with.dots".value'),
        ),
        SelectorSpec(
            name="key_regex",
            prefix="re",
            description="regex matching full JSON object key paths",
            examples=(r"^build", r"Dir$"),
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
        selected_key_paths = selected_json_key_paths(
            base_data,
            exact_key_paths,
            selected_key_regexes,
        )
        if request.has_excluded_selectors():
            selected_key_paths = subtract_excluded_key_paths(
                iter_json_key_paths(base_data),
                selected_key_paths if request.has_included_selectors() else None,
                selected_json_key_paths(
                    base_data,
                    parse_json_key_paths(request.excluded_selector_values("key")),
                    compile_key_regexes(request.excluded_selector_values("key_regex")),
                ),
            )
        # Only an absent selector list means identity. Selectors that match
        # nothing must still select nothing.
        transformed_data = (
            select_json_data(base_data, request.selector_action, selected_key_paths)
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
            transformed_data = overlay_json_data(
                base_data,
                transformed_data,
                overlay_data,
                # Without selectors the whole base is one selection, so the overlay
                # replaces each top-level key it shares with the base.
                selected_key_paths if request.has_selectors() else (DOCUMENT_ROOT_KEY_PATH,),
            )

        return build_json_output(
            transformed_data,
            mode_reference_path=request.base_path,
            compare_path=request.engine_option("compare_path"),
            indent_reference_paths=(request.base_path, request.overlay_path),
            stdin_bytes=request.engine_option("stdin_bytes"),
        )



def main(argv: list[str] | None = None) -> int:
    return run_engine_cli(JsonTransformEngine(), argv=argv)


if __name__ == "__main__":
    raise SystemExit(main())
