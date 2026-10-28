from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import plistlib
import re
from typing import Any
from xml.parsers.expat import ExpatError

from dotman.transforms.framework import (
    BaseTransformEngine,
    SelectorAction,
    SelectorSpec,
    TransformMode,
    TransformOutput,
    TransformRequest,
    STDIN_PATH,
    compile_selector_regexes,
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
_MISSING = object()


@dataclass
class PlistPathSelector:
    include_subtree: bool = False
    children: dict[str, "PlistPathSelector"] = field(default_factory=dict)


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


def build_plist_path_selector(key_paths: tuple[PlistKeyPath, ...]) -> PlistPathSelector:
    root = PlistPathSelector()
    for key_path in key_paths:
        current = root
        for key_part in key_path:
            if current.include_subtree:
                break
            current = current.children.setdefault(key_part, PlistPathSelector())
        current.include_subtree = True
        current.children.clear()
    return root


def plist_key_path_text(key_path: PlistKeyPath) -> str:
    return ".".join(key_path)


def matches_key_regexes(
    key_path: PlistKeyPath,
    key_regexes: tuple[KeyRegex, ...],
) -> bool:
    path_text = plist_key_path_text(key_path)
    return any(key_regex.search(path_text) for key_regex in key_regexes)


def iter_plist_key_paths(
    value: Any,
    prefix: PlistKeyPath = (),
) -> tuple[PlistKeyPath, ...]:
    if not isinstance(value, dict):
        return ()

    key_paths: list[PlistKeyPath] = []
    for key, child_value in value.items():
        key_path = prefix + (key,)
        key_paths.append(key_path)
        key_paths.extend(iter_plist_key_paths(child_value, key_path))
    return tuple(key_paths)


def plist_key_paths_matching_regexes(
    data: PlistDict,
    key_regexes: tuple[KeyRegex, ...],
) -> tuple[PlistKeyPath, ...]:
    if not key_regexes:
        return ()
    return tuple(
        key_path
        for key_path in iter_plist_key_paths(data)
        if matches_key_regexes(key_path, key_regexes)
    )


def selected_plist_key_paths(
    data: PlistDict,
    exact_key_paths: tuple[PlistKeyPath, ...],
    key_regexes: tuple[KeyRegex, ...],
) -> tuple[PlistKeyPath, ...]:
    return exact_key_paths + plist_key_paths_matching_regexes(data, key_regexes)


def retained_plist_value(value: Any, selector: PlistPathSelector) -> Any:
    if selector.include_subtree:
        return value
    if not isinstance(value, dict):
        return _MISSING

    retained_data: PlistDict = {}
    for key, child_value in value.items():
        child_selector = selector.children.get(key)
        if child_selector is None:
            continue
        retained_value = retained_plist_value(child_value, child_selector)
        if retained_value is not _MISSING:
            retained_data[key] = retained_value

    if not retained_data:
        return _MISSING
    return retained_data


def stripped_plist_value(value: Any, selector: PlistPathSelector) -> Any:
    if selector.include_subtree:
        return _MISSING
    if not isinstance(value, dict):
        return value

    stripped_data: PlistDict = {}
    for key, child_value in value.items():
        child_selector = selector.children.get(key)
        if child_selector is None:
            stripped_data[key] = child_value
            continue
        stripped_value = stripped_plist_value(child_value, child_selector)
        if stripped_value is not _MISSING:
            stripped_data[key] = stripped_value

    return stripped_data


def filter_retained_keys(
    data: PlistDict,
    retained_key_paths: tuple[PlistKeyPath, ...],
) -> PlistDict:
    retained_value = retained_plist_value(
        data,
        build_plist_path_selector(retained_key_paths),
    )
    return {} if retained_value is _MISSING else retained_value


def filter_stripped_keys(
    data: PlistDict,
    stripped_key_paths: tuple[PlistKeyPath, ...],
) -> PlistDict:
    if not stripped_key_paths:
        return dict(data)

    stripped_value = stripped_plist_value(
        data,
        build_plist_path_selector(stripped_key_paths),
    )
    return {} if stripped_value is _MISSING else stripped_value


def select_plist_data(
    data: PlistDict,
    selector_action: SelectorAction,
    selected_key_paths: tuple[PlistKeyPath, ...],
) -> PlistDict:
    if selector_action == SelectorAction.REMOVE:
        return filter_stripped_keys(data, selected_key_paths)
    return filter_retained_keys(data, selected_key_paths)


def child_overlay_selector(selector: PlistPathSelector, key: str) -> PlistPathSelector:
    # Everything under a whole selection is whole too. A key no selector
    # reaches gets an empty selector, so merge still descends into it.
    if selector.include_subtree:
        return selector
    return selector.children.get(key) or PlistPathSelector()


def overlay_plist_dicts(
    original_base_data: PlistDict,
    preserved_base_data: PlistDict,
    overlay_data: PlistDict,
    path_selector: PlistPathSelector,
) -> PlistDict:
    merged_data: PlistDict = {}

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
                and isinstance(base_value, dict)
                and isinstance(overlay_value, dict)
            ):
                merged_data[key] = overlay_plist_dicts(
                    base_value,
                    preserved_base_data.get(key, {}),
                    overlay_value,
                    child_selector,
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


def overlay_plist_data(
    original_base_data: PlistDict,
    preserved_base_data: PlistDict,
    overlay_data: PlistDict,
    selected_key_paths: tuple[PlistKeyPath, ...] = (),
) -> PlistDict:
    return overlay_plist_dicts(
        original_base_data,
        preserved_base_data,
        overlay_data,
        build_plist_path_selector(selected_key_paths),
    )


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
        selected_key_paths = selected_plist_key_paths(
            base_data,
            exact_key_paths,
            selected_key_regexes,
        )
        if request.has_excluded_selectors():
            selected_key_paths = subtract_excluded_key_paths(
                iter_plist_key_paths(base_data),
                selected_key_paths if request.has_included_selectors() else None,
                selected_plist_key_paths(
                    base_data,
                    parse_plist_key_paths(request.excluded_selector_values("key")),
                    compile_key_regexes(request.excluded_selector_values("key_regex")),
                ),
            )
        # Only an absent selector list means identity. Selectors that match
        # nothing must still select nothing.
        transformed_data = (
            select_plist_data(base_data, request.selector_action, selected_key_paths)
            if request.has_selectors()
            else dict(base_data)
        )
        transformed_data = without_emptied_mappings(transformed_data, base_data)

        if request.mode == TransformMode.MERGE:
            assert request.overlay_path is not None
            overlay_data = load_plist(
                request.overlay_path, stdin_bytes=request.engine_option("stdin_bytes")
            )
            transformed_data = overlay_plist_data(
                base_data,
                transformed_data,
                overlay_data,
                # Without selectors the whole base is one selection, so the overlay
                # replaces each top-level key it shares with the base.
                selected_key_paths if request.has_selectors() else (DOCUMENT_ROOT_KEY_PATH,),
            )

        return build_plist_output(
            transformed_data,
            output_format,
            mode_reference_path=request.base_path,
            compare_path=request.engine_option("compare_path"),
        )
