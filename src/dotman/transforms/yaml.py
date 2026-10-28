from __future__ import annotations

from pathlib import Path
import re
from typing import Any

import yaml

from dotman.transforms.framework import (
    MappingTransformEngine,
    SelectorSpec,
    TransformOutput,
    TransformRequest,
    read_input_text,
    read_reference_text,
    semantically_unchanged_compare_output,
    values_strictly_equal,
)


YamlDict = dict[Any, Any]
DEFAULT_YAML_INDENT = 2
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


class YamlTransformEngine(MappingTransformEngine):
    name = "yaml"
    KEY_PATH_FORMAT_NAME = "YAML"
    KEY_REGEX_DESCRIPTION = "YAML key selector"
    COMPARE_FILE_HELP = "Optional YAML file to compare against for semantic no-op text reuse"
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

    load = staticmethod(load_yaml)
    key_text = staticmethod(yaml_key_text)

    def build_output(self, data: YamlDict, request: TransformRequest) -> TransformOutput:
        compare_path = request.engine_option("compare_path")
        reused_output = semantically_unchanged_compare_output(
            compare_path,
            data,
            lambda content: parse_yaml_text(content.decode("utf-8-sig")),
            mode_reference_path=request.base_path,
        )
        if reused_output is not None:
            return reused_output

        indent = select_yaml_indent(
            compare_path,
            request.base_path,
            request.overlay_path,
            stdin_bytes=request.engine_option("stdin_bytes"),
        )
        return TransformOutput(
            content=yaml_text(data, indent=indent),
            mode_reference_path=request.base_path,
        )
