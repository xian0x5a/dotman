#!/usr/bin/env python3

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re
from typing import Any

import yaml

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


YamlDict = dict[Any, Any]
YamlKeyPath = tuple[str, ...]
# The empty key path selects the whole document.
DOCUMENT_ROOT_KEY_PATH: YamlKeyPath = ()
KeyRegex = re.Pattern[str]
DEFAULT_YAML_INDENT = 2
_MISSING = object()
_NULL_TAG = "tag:yaml.org,2002:null"
_BOOL_TAG = "tag:yaml.org,2002:bool"
_INT_TAG = "tag:yaml.org,2002:int"
_FLOAT_TAG = "tag:yaml.org,2002:float"
_STR_TAG = "tag:yaml.org,2002:str"
_MERGE_TAG = "tag:yaml.org,2002:merge"
# PyYAML's YAML 1.1 "=" value-key tag has no constructor in SafeLoader, so a
# plain "=" scalar would crash instead of loading as the string it is in 1.2.
_VALUE_TAG = "tag:yaml.org,2002:value"
# Private tag the loader gives plain scalars whose YAML 1.1 and 1.2 readings
# differ; it never appears in emitted YAML.
_AMBIGUOUS_PLAIN_SCALAR_TAG = "tag:dotman,2026:ambiguous-plain-scalar"

# The YAML 1.1 spec's single-letter booleans, which PyYAML itself does not resolve.
_YAML11_SINGLE_LETTER_BOOL_RE = re.compile(r"^(?:y|Y|n|N)$")
_YAML12_CORE_NUMBER_RESOLVERS = (
    (_INT_TAG, re.compile(r"^(?:[-+]?[0-9]+|0o[0-7]+|0x[0-9a-fA-F]+)$"), "-+0123456789"),
    (
        _FLOAT_TAG,
        re.compile(
            r"^(?:[-+]?(?:\.[0-9]+|[0-9]+(?:\.[0-9]*)?)(?:[eE][-+]?[0-9]+)?"
            r"|[-+]?\.(?:inf|Inf|INF)|\.(?:nan|NaN|NAN))$"
        ),
        "-+.0123456789",
    ),
)
_YAML12_CORE_RESOLVERS = (
    (_NULL_TAG, re.compile(r"^(?:~|null|Null|NULL|)$")),
    (_BOOL_TAG, re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$")),
    *((tag, pattern) for tag, pattern, _ in _YAML12_CORE_NUMBER_RESOLVERS),
)


class AmbiguousPlainScalar(str):
    """Plain scalar text that YAML 1.1 and YAML 1.2 core readers resolve differently.

    Examples are ``0777`` (511 vs 777), ``22:22`` (1342 vs a string), and
    ``yes`` (true vs a string). It matches selectors by its text and is
    emitted with its original plain spelling so the value each kind of reader
    sees never changes.
    """

    # No reader sees plain 0777 and quoted "0777" as the same mapping key, so
    # they must not collide in a dict either.
    def __eq__(self, other: object) -> bool:
        return isinstance(other, AmbiguousPlainScalar) and str.__eq__(self, other)

    def __ne__(self, other: object) -> bool:
        return not self == other

    def __hash__(self) -> int:
        return hash((AmbiguousPlainScalar, str(self)))


def yaml12_core_tag(text: str) -> str:
    return next((tag for tag, pattern in _YAML12_CORE_RESOLVERS if pattern.match(text)), _STR_TAG)


def yaml12_core_value(tag: str, text: str) -> Any:
    if tag == _NULL_TAG:
        return None
    if tag == _BOOL_TAG:
        return text.lower() == "true"
    if tag == _INT_TAG:
        if text.startswith("0o"):
            return int(text[2:], 8)
        if text.startswith("0x"):
            return int(text[2:], 16)
        return int(text)
    # Python spells YAML's .inf and .nan without the leading dot.
    return float(text.lower().replace(".inf", "inf").replace(".nan", "nan"))


def resolvers_without_tags(
    resolvers: dict[Any, list[tuple[str, re.Pattern[str]]]],
    excluded_tags: frozenset[str],
) -> dict[Any, list[tuple[str, re.Pattern[str]]]]:
    return {
        initial: [resolver for resolver in initial_resolvers if resolver[0] not in excluded_tags]
        for initial, initial_resolvers in resolvers.items()
    }


class VersionNeutralSafeLoader(yaml.SafeLoader):
    """Load plain scalars as typed values only when YAML 1.1 and 1.2 agree.

    Its implicit resolvers are YAML 1.1's. A plain scalar whose YAML 1.1
    reading differs from the YAML 1.2 core schema reading in type or value
    loads as an ``AmbiguousPlainScalar`` holding the original text instead.
    """

    yaml_implicit_resolvers = resolvers_without_tags(
        yaml.SafeLoader.yaml_implicit_resolvers,
        frozenset({_VALUE_TAG}),
    )
    bool_values = {**yaml.SafeLoader.bool_values, "y": True, "n": False}

    def resolve(self, kind: Any, value: Any, implicit: Any) -> str:
        yaml11_tag = super().resolve(kind, value, implicit)
        is_plain_scalar = kind is yaml.ScalarNode and implicit[0]
        if is_plain_scalar and self.yaml_versions_disagree(yaml11_tag, value):
            return _AMBIGUOUS_PLAIN_SCALAR_TAG
        return yaml11_tag

    def yaml_versions_disagree(self, yaml11_tag: str, text: str) -> bool:
        # "<<" is a string in YAML 1.2, but merge keys are widely supported
        # there too; keep applying them at load time.
        if yaml11_tag == _MERGE_TAG:
            return False
        yaml12_tag = yaml12_core_tag(text)
        if yaml11_tag != yaml12_tag:
            return True
        if yaml11_tag == _STR_TAG:
            return False
        yaml11_value = self.yaml_constructors[yaml11_tag](self, yaml.ScalarNode(yaml11_tag, text))
        return not values_strictly_equal(yaml11_value, yaml12_core_value(yaml12_tag, text))


VersionNeutralSafeLoader.add_implicit_resolver(
    _BOOL_TAG, _YAML11_SINGLE_LETTER_BOOL_RE, list("yYnN")
)
VersionNeutralSafeLoader.add_constructor(
    _AMBIGUOUS_PLAIN_SCALAR_TAG,
    lambda loader, node: AmbiguousPlainScalar(loader.construct_scalar(node)),
)


class PortableSafeDumper(yaml.SafeDumper):
    """Quote strings that any common YAML reader would resolve as non-strings.

    SafeDumper already quotes YAML 1.1 lookalikes. These extra resolvers are
    only consulted to decide quoting, covering YAML 1.2 core numbers and the
    single-letter YAML 1.1 booleans that PyYAML itself does not resolve.
    """


PortableSafeDumper.add_implicit_resolver(_BOOL_TAG, _YAML11_SINGLE_LETTER_BOOL_RE, list("yYnN"))
for _tag, _pattern, _first_chars in _YAML12_CORE_NUMBER_RESOLVERS:
    PortableSafeDumper.add_implicit_resolver(_tag, _pattern, list(_first_chars))


def represent_ambiguous_plain_scalar(dumper: yaml.SafeDumper, data: AmbiguousPlainScalar) -> yaml.Node:
    text = str(data)
    # Tagging the node with whatever the dumper resolves this text to makes the
    # serializer mark it implicit, so the emitter writes it plain instead of
    # quoting it like an ordinary string lookalike.
    return dumper.represent_scalar(dumper.resolve(yaml.ScalarNode, text, (True, False)), text)


PortableSafeDumper.add_representer(AmbiguousPlainScalar, represent_ambiguous_plain_scalar)


def parse_yaml_text(text: str) -> Any:
    try:
        return yaml.load(text, Loader=VersionNeutralSafeLoader)
    except (AttributeError, KeyError, TypeError, ValueError) as error:
        # PyYAML's constructors fail with plain Python errors on an invalid
        # explicitly tagged value, such as `!!bool maybe` or `!!timestamp nope`.
        raise yaml.constructor.ConstructorError(problem=f"invalid tagged value ({error!r})") from error


def yaml_key_text(key: Any) -> str:
    """Spell a mapping key for selector matching."""
    if isinstance(key, str):
        return str(key)
    # Match non-string keys by their emitted YAML spelling (true, null, 1.5),
    # not Python's (True, None).
    return yaml.representer.SafeRepresenter().represent_data(key).value


@dataclass
class YamlPathSelector:
    include_subtree: bool = False
    children: dict[str, "YamlPathSelector"] = field(default_factory=dict)


def load_yaml(path: Path, *, stdin_bytes: bytes | None = None) -> YamlDict:
    source_text = read_input_text(path, stdin_bytes=stdin_bytes)
    if source_text is None:
        return {}

    try:
        loaded = parse_yaml_text(source_text)
    except yaml.YAMLError as error:
        raise ValueError(f"invalid YAML in {path}: {error}") from error
    # An empty or comment-only document has no mapping yet, like a missing file.
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise ValueError(f"Expected top-level YAML mapping in {path}")
    return loaded


def compile_key_regexes(raw_key_regexes: tuple[str, ...]) -> tuple[KeyRegex, ...]:
    return compile_selector_regexes(raw_key_regexes, "YAML key selector")


def parse_yaml_key_path(raw_key: str) -> YamlKeyPath:
    return split_quoted_key_path(raw_key, "YAML")


def parse_yaml_key_paths(raw_key_paths: tuple[str, ...]) -> tuple[YamlKeyPath, ...]:
    return tuple(parse_yaml_key_path(raw_key) for raw_key in raw_key_paths)


def build_yaml_path_selector(key_paths: tuple[YamlKeyPath, ...]) -> YamlPathSelector:
    root = YamlPathSelector()
    for key_path in key_paths:
        current = root
        for key_part in key_path:
            if current.include_subtree:
                break
            current = current.children.setdefault(key_part, YamlPathSelector())
        current.include_subtree = True
        current.children.clear()
    return root


def yaml_key_path_text(key_path: YamlKeyPath) -> str:
    return ".".join(key_path)


def matches_key_regexes(key_path: YamlKeyPath, key_regexes: tuple[KeyRegex, ...]) -> bool:
    path_text = yaml_key_path_text(key_path)
    return any(key_regex.search(path_text) for key_regex in key_regexes)


def iter_yaml_key_paths(value: Any, prefix: YamlKeyPath = ()) -> tuple[YamlKeyPath, ...]:
    if not isinstance(value, dict):
        return ()

    key_paths: list[YamlKeyPath] = []
    for key, child_value in value.items():
        key_path = prefix + (yaml_key_text(key),)
        key_paths.append(key_path)
        key_paths.extend(iter_yaml_key_paths(child_value, key_path))
    return tuple(key_paths)


def yaml_key_paths_matching_regexes(
    data: YamlDict,
    key_regexes: tuple[KeyRegex, ...],
) -> tuple[YamlKeyPath, ...]:
    if not key_regexes:
        return ()
    return tuple(
        key_path
        for key_path in iter_yaml_key_paths(data)
        if matches_key_regexes(key_path, key_regexes)
    )


def selected_yaml_key_paths(
    data: YamlDict,
    exact_key_paths: tuple[YamlKeyPath, ...],
    key_regexes: tuple[KeyRegex, ...],
) -> tuple[YamlKeyPath, ...]:
    return exact_key_paths + yaml_key_paths_matching_regexes(data, key_regexes)


def retained_yaml_value(value: Any, selector: YamlPathSelector) -> Any:
    if selector.include_subtree:
        return value
    if not isinstance(value, dict):
        return _MISSING

    retained_data: YamlDict = {}
    for key, child_value in value.items():
        child_selector = selector.children.get(yaml_key_text(key))
        if child_selector is None:
            continue
        retained_value = retained_yaml_value(child_value, child_selector)
        if retained_value is not _MISSING:
            retained_data[key] = retained_value

    if not retained_data:
        return _MISSING
    return retained_data


def stripped_yaml_value(value: Any, selector: YamlPathSelector) -> Any:
    if selector.include_subtree:
        return _MISSING
    if not isinstance(value, dict):
        return value

    stripped_data: YamlDict = {}
    for key, child_value in value.items():
        child_selector = selector.children.get(yaml_key_text(key))
        if child_selector is None:
            stripped_data[key] = child_value
            continue

        stripped_value = stripped_yaml_value(child_value, child_selector)
        if stripped_value is not _MISSING:
            stripped_data[key] = stripped_value

    return stripped_data


def filter_retained_keys(
    data: YamlDict,
    retained_key_paths: tuple[YamlKeyPath, ...],
) -> YamlDict:
    path_selector = build_yaml_path_selector(retained_key_paths)
    retained_data: YamlDict = {}
    for key, value in data.items():
        child_selector = path_selector.children.get(yaml_key_text(key))
        if child_selector is None:
            continue

        retained_value = retained_yaml_value(value, child_selector)
        if retained_value is not _MISSING:
            retained_data[key] = retained_value

    return retained_data


def filter_stripped_keys(
    data: YamlDict,
    stripped_key_paths: tuple[YamlKeyPath, ...],
) -> YamlDict:
    if not stripped_key_paths:
        return dict(data)

    path_selector = build_yaml_path_selector(stripped_key_paths)
    stripped_data: YamlDict = {}
    for key, value in data.items():
        child_selector = path_selector.children.get(yaml_key_text(key))
        if child_selector is None:
            stripped_data[key] = value
            continue

        stripped_value = stripped_yaml_value(value, child_selector)
        if stripped_value is not _MISSING:
            stripped_data[key] = stripped_value

    return stripped_data


def select_yaml_data(
    data: YamlDict,
    selector_action: SelectorAction,
    selected_key_paths: tuple[YamlKeyPath, ...],
) -> YamlDict:
    if selector_action == SelectorAction.REMOVE:
        return filter_stripped_keys(data, selected_key_paths)
    return filter_retained_keys(data, selected_key_paths)


def child_overlay_selector(selector: YamlPathSelector, key: str) -> YamlPathSelector:
    # Everything under a whole selection is whole too. A key no selector
    # reaches gets an empty selector, so merge still descends into it.
    if selector.include_subtree:
        return selector
    return selector.children.get(key) or YamlPathSelector()


def overlay_yaml_objects(
    original_base_data: YamlDict,
    preserved_base_data: YamlDict,
    overlay_data: YamlDict,
    path_selector: YamlPathSelector,
) -> YamlDict:
    merged_data: YamlDict = {}

    # Keep surviving keys in live order so repo-managed value changes do not also
    # produce noisy key-movement diffs.
    for key in original_base_data:
        child_selector = child_overlay_selector(path_selector, yaml_key_text(key))

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
                merged_data[key] = overlay_yaml_objects(
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


def overlay_yaml_data(
    original_base_data: YamlDict,
    preserved_base_data: YamlDict,
    overlay_data: YamlDict,
    selected_key_paths: tuple[YamlKeyPath, ...] = (),
) -> YamlDict:
    return overlay_yaml_objects(
        original_base_data,
        preserved_base_data,
        overlay_data,
        build_yaml_path_selector(selected_key_paths),
    )


def detect_yaml_indent(text: str | None) -> int | None:
    if text is None:
        return None
    try:
        root_node = yaml.compose(text, Loader=VersionNeutralSafeLoader)
    except yaml.YAMLError:
        return None
    indent = nested_mapping_indent(root_node)
    if indent is None or indent < 1:
        return None
    return indent


def nested_mapping_indent(node: yaml.Node | None) -> int | None:
    # Measure the first block mapping nested under a key, so literal block
    # scalar content never counts as structural indentation.
    if isinstance(node, yaml.MappingNode):
        for key_node, value_node in node.value:
            if isinstance(value_node, yaml.MappingNode) and not value_node.flow_style and value_node.value:
                return value_node.value[0][0].start_mark.column - key_node.start_mark.column
            indent = nested_mapping_indent(value_node)
            if indent is not None:
                return indent
    if isinstance(node, yaml.SequenceNode):
        for item_node in node.value:
            indent = nested_mapping_indent(item_node)
            if indent is not None:
                return indent
    return None


def select_yaml_indent(*reference_paths: Path | None, stdin_bytes: bytes | None = None) -> int:
    for reference_path in reference_paths:
        indent = detect_yaml_indent(read_reference_text(reference_path, stdin_bytes=stdin_bytes))
        if indent is not None:
            return indent
    return DEFAULT_YAML_INDENT


def yaml_text(data: YamlDict, indent: int = DEFAULT_YAML_INDENT) -> str:
    return yaml.dump(
        data,
        Dumper=PortableSafeDumper,
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
        indent=indent,
    )


def get_existing_bytes_if_semantically_unchanged(
    path: Path,
    data: YamlDict,
) -> bytes | None:
    existing_bytes = read_reference_bytes(path)
    existing_text = decode_reference_text(existing_bytes)
    if existing_text is None:
        return None
    try:
        existing_data = parse_yaml_text(existing_text)
    except Exception:
        return None

    if not values_strictly_equal(existing_data, data):
        return None

    return existing_bytes


def build_yaml_output(
    data: YamlDict,
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

    indent = select_yaml_indent(
        compare_path,
        *indent_reference_paths,
        stdin_bytes=stdin_bytes,
    )
    return TransformOutput(
        content=yaml_text(data, indent=indent),
        mode_reference_path=mode_reference_path,
    )


class YamlTransformEngine(BaseTransformEngine):
    name = "yaml"
    SUPPORTS_EXCLUDED_SELECTORS = True
    SELECTOR_SPECS = (
        SelectorSpec(
            name="key",
            prefix="exact",
            is_default=True,
            description="exact dotted or quoted nested YAML mapping key path",
        ),
        SelectorSpec(
            name="key_regex",
            prefix="re",
            description="regex matching full YAML mapping key paths",
        ),
    )

    def requires_selectors(self) -> bool:
        return False

    def configure_parser(self, parser) -> None:
        parser.add_argument(
            "--compare-file",
            type=Path,
            help="Optional YAML file to compare against for semantic no-op text reuse",
        )

    def build_engine_options(self, parsed_args) -> dict[str, Any]:
        return {
            "compare_path": parsed_args.compare_file,
            "stdout": parsed_args.stdout,
            "stdin_bytes": parsed_args.stdin_bytes,
        }

    def validate_request(self, request: TransformRequest) -> None:
        super().validate_request(request)
        parse_yaml_key_paths(request.selector_values("key"))
        compile_key_regexes(request.selector_values("key_regex"))
        parse_yaml_key_paths(request.excluded_selector_values("key"))
        compile_key_regexes(request.excluded_selector_values("key_regex"))

    def transform(self, request: TransformRequest) -> TransformOutput:
        self.validate_request(request)
        exact_key_paths = parse_yaml_key_paths(request.selector_values("key"))
        selected_key_regexes = compile_key_regexes(request.selector_values("key_regex"))

        base_data = load_yaml(
            request.base_path,
            stdin_bytes=request.engine_option("stdin_bytes"),
        )
        selected_key_paths = selected_yaml_key_paths(
            base_data,
            exact_key_paths,
            selected_key_regexes,
        )
        if request.has_excluded_selectors():
            selected_key_paths = subtract_excluded_key_paths(
                iter_yaml_key_paths(base_data),
                selected_key_paths if request.has_included_selectors() else None,
                selected_yaml_key_paths(
                    base_data,
                    parse_yaml_key_paths(request.excluded_selector_values("key")),
                    compile_key_regexes(request.excluded_selector_values("key_regex")),
                ),
            )
        # Only an absent selector list means identity. Selectors that match
        # nothing must still select nothing.
        transformed_data = (
            select_yaml_data(base_data, request.selector_action, selected_key_paths)
            if request.has_selectors()
            else dict(base_data)
        )
        transformed_data = without_emptied_mappings(transformed_data, base_data)

        if request.mode == TransformMode.MERGE:
            assert request.overlay_path is not None
            overlay_data = load_yaml(
                request.overlay_path,
                stdin_bytes=request.engine_option("stdin_bytes"),
            )
            transformed_data = overlay_yaml_data(
                base_data,
                transformed_data,
                overlay_data,
                # Without selectors the whole base is one selection, so the overlay
                # replaces each top-level key it shares with the base.
                selected_key_paths if request.has_selectors() else (DOCUMENT_ROOT_KEY_PATH,),
            )

        return build_yaml_output(
            transformed_data,
            mode_reference_path=request.base_path,
            compare_path=request.engine_option("compare_path"),
            indent_reference_paths=(request.base_path, request.overlay_path),
            stdin_bytes=request.engine_option("stdin_bytes"),
        )


def main(argv: list[str] | None = None) -> int:
    return run_engine_cli(YamlTransformEngine(), argv=argv)


if __name__ == "__main__":
    raise SystemExit(main())
