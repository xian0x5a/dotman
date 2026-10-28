"""YAML transform: shared selection and merge, written back as a minimal round-trip patch.

Selection and merge run on the loaded document like every mapping transform.
Output then edits the target document in place through ruamel.yaml's
round-trip mode, so anything the transform leaves unchanged keeps its comments,
spelling, anchors and merge keys. Values follow YAML 1.2.
"""
from __future__ import annotations

import copy
import datetime
import io
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq, TaggedScalar, merge_attrib
from ruamel.yaml.error import CommentMark, YAMLError
from ruamel.yaml.nodes import MappingNode, Node, ScalarNode, SequenceNode
from ruamel.yaml.representer import RoundTripRepresenter
from ruamel.yaml.scalarbool import ScalarBoolean
from ruamel.yaml.tokens import CommentToken

from dotman.transforms.framework import (
    MappingTransformEngine,
    SelectorSpec,
    TransformMode,
    TransformOutput,
    TransformRequest,
    read_input_text,
    read_reference_text,
    semantically_unchanged_compare_output,
    values_strictly_equal,
)


YamlDict = dict[Any, Any]
DEFAULT_YAML_INDENT = 2
# ruamel re-wraps long scalars at 80 columns by default, which would rewrite
# lines the transform never touched.
_NO_WRAP_WIDTH = 1 << 20
# Applying the desired data converges in one pass unless an edit to a mapping
# that `<<` merges elsewhere changes a mapping already applied; the next pass
# then overrides the merged value there.
_MAX_APPLY_PASSES = 3
# ruamel keeps the comment after a mapping value in slot 2 of its key's entry,
# and the comment after a sequence item in slot 0 of its index's entry.
_MAPPING_COMMENT_SLOT = 2
_SEQUENCE_COMMENT_SLOT = 0
# Explicit tags from the YAML type repository (`!!str 12`, and the `=` that
# ruamel resolves to the YAML 1.1 value type) still spell plain strings.
_STANDARD_TAG_PREFIX = "tag:yaml.org,2002:"


@dataclass(frozen=True)
class YamlLayout:
    """Block indentation in ruamel's terms: mapping indent, sequence content indent, dash offset."""

    mapping: int = DEFAULT_YAML_INDENT
    sequence: int = DEFAULT_YAML_INDENT
    offset: int = 0


@dataclass(frozen=True)
class TaggedValue:
    """A value under a custom tag such as `!Ref`, which differs from the same untagged value."""

    tag: str
    value: Any


class YamlRepresenter(RoundTripRepresenter):
    def insert_underscore(self, prefix: str, digits: str, underscore: Any, anchor: Any = None) -> ScalarNode:
        # ruamel writes a negative hex, octal or binary int after its prefix
        # (`0x-1F`), which no longer reads as an int; keep the sign in front.
        if prefix and digits.startswith("-"):
            return super().insert_underscore("-" + prefix, digits[1:], underscore, anchor=anchor)
        return super().insert_underscore(prefix, digits, underscore, anchor=anchor)

    def represent_none(self, data: None) -> ScalarNode:
        # ruamel leaves null values empty (`key:`, a bare `-`); write `null`
        # as dotman's earlier YAML output did, so files it wrote do not churn.
        return self.represent_scalar("tag:yaml.org,2002:null", "null")


YamlRepresenter.add_representer(type(None), YamlRepresenter.represent_none)


def round_trip_yaml(layout: YamlLayout = YamlLayout()) -> YAML:
    yaml = YAML()
    yaml.Representer = YamlRepresenter
    yaml.preserve_quotes = True
    yaml.width = _NO_WRAP_WIDTH
    yaml.indent(mapping=layout.mapping, sequence=layout.sequence, offset=layout.offset)
    return yaml


def parse_yaml_text(text: str) -> Any:
    return round_trip_yaml().load(text)


def plain_value(value: Any) -> Any:
    """The value with ruamel's spelling-preserving types replaced by plain Python ones."""
    tag = custom_tag(value)
    if tag is not None:
        return TaggedValue(tag, plain_untagged_value(value))
    return plain_untagged_value(value)


def custom_tag(value: Any) -> str | None:
    tag = getattr(getattr(value, "tag", None), "value", None)
    if tag is None or tag.startswith(_STANDARD_TAG_PREFIX):
        return None
    return tag


def plain_untagged_value(value: Any) -> Any:
    if isinstance(value, TaggedScalar):
        return str(value.value)
    if isinstance(value, Mapping):
        return {plain_value(key): plain_value(child) for key, child in value.items()}
    if isinstance(value, list):
        return [plain_value(child) for child in value]
    if isinstance(value, ScalarBoolean):
        return bool(value)
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        return float(value)
    if isinstance(value, str):
        return str(value)
    return value


def yaml_key_text(key: Any) -> str:
    """Spell a mapping key for selector matching the way the document writes it."""
    if isinstance(key, str):
        return str(key)
    if key is None:
        return "null"
    if isinstance(key, (int, float, datetime.date, TaggedScalar)):
        # A fresh representer: it remembers represented objects for aliasing.
        return YamlRepresenter(None, None, None).represent_data(key).value
    return str(key)


def load_yaml_document(path: Path, *, stdin_bytes: bytes | None = None) -> CommentedMap | None:
    """The document's top-level mapping, or None for a missing, empty or comment-only document."""
    source_text = read_input_text(path, stdin_bytes=stdin_bytes)
    if source_text is None:
        return None
    try:
        loaded = parse_yaml_text(source_text)
    except YAMLError as error:
        raise ValueError(f"invalid YAML in {path}: {error}") from error
    if loaded is None:
        return None
    if not isinstance(loaded, CommentedMap):
        raise ValueError(f"Expected top-level YAML mapping in {path}")
    return loaded


def load_yaml(path: Path, *, stdin_bytes: bytes | None = None) -> YamlDict:
    document = load_yaml_document(path, stdin_bytes=stdin_bytes)
    return {} if document is None else document


def detect_yaml_layout(text: str | None) -> YamlLayout | None:
    if text is None:
        return None
    try:
        root_node = YAML().compose(text)
    except YAMLError:
        return None
    mapping_indent = nested_mapping_indent(root_node)
    if mapping_indent is None or mapping_indent < 1:
        return None
    sequence_layout = nested_sequence_layout(root_node)
    if sequence_layout is None:
        return YamlLayout(mapping=mapping_indent)
    sequence, offset = sequence_layout
    return YamlLayout(mapping=mapping_indent, sequence=sequence, offset=offset)


def block_children(node: Node | None) -> list[tuple[Node | None, Node]]:
    """(key node, value node) pairs of a mapping, or (None, item) pairs of a sequence."""
    if isinstance(node, MappingNode):
        return list(node.value)
    if isinstance(node, SequenceNode):
        return [(None, item) for item in node.value]
    return []


def nested_mapping_indent(node: Node | None) -> int | None:
    # Measure the first block mapping nested under a key, so literal block
    # scalar content never counts as structural indentation.
    for key_node, value_node in block_children(node):
        if key_node is not None and isinstance(value_node, MappingNode) and not value_node.flow_style and value_node.value:
            return value_node.value[0][0].start_mark.column - key_node.start_mark.column
        indent = nested_mapping_indent(value_node)
        if indent is not None:
            return indent
    return None


def nested_sequence_layout(node: Node | None) -> tuple[int, int] | None:
    """(content indent, dash offset) of the first block sequence nested under a key."""
    for key_node, value_node in block_children(node):
        if key_node is not None and isinstance(value_node, SequenceNode) and not value_node.flow_style and value_node.value:
            key_column = key_node.start_mark.column
            dash_offset = value_node.start_mark.column - key_column
            content_indent = value_node.value[0].start_mark.column - key_column
            if dash_offset >= 0 and content_indent >= dash_offset + 2:
                return content_indent, dash_offset
        layout = nested_sequence_layout(value_node)
        if layout is not None:
            return layout
    return None


def select_yaml_layout(*reference_paths: Path | None, stdin_bytes: bytes | None = None) -> YamlLayout:
    for reference_path in reference_paths:
        layout = detect_yaml_layout(read_reference_text(reference_path, stdin_bytes=stdin_bytes))
        if layout is not None:
            return layout
    return YamlLayout()


def own_keys(mapping: CommentedMap) -> list[Any]:
    """Keys written in the mapping itself, without those a `<<` merge brings in."""
    return [key for key, _ in mapping.non_merged_items()]


def flatten_merge(mapping: CommentedMap) -> None:
    """Write every merged-in key into the mapping and drop its `<<`.

    ruamel silently keeps a merged-in key that is deleted, so a mapping that
    must lose one stops merging and spells its keys out instead.
    """
    for key, value in list(mapping.items()):
        mapping[key] = value
    if hasattr(mapping, merge_attrib):
        delattr(mapping, merge_attrib)


def trailing_comment_slot(container: CommentedMap | CommentedSeq, key: Any) -> tuple[list[Any], int]:
    """Where ruamel keeps the comment text that follows the value under key.

    A block mapping or sequence value carries it on its own last entry, so
    this descends to the deepest last entry. Missing entries are created.
    """
    value = container[key]
    if isinstance(value, CommentedMap) and own_keys(value) and not value.fa.flow_style():
        return trailing_comment_slot(value, own_keys(value)[-1])
    if isinstance(value, CommentedSeq) and value and not value.fa.flow_style():
        return trailing_comment_slot(value, len(value) - 1)
    slot = _MAPPING_COMMENT_SLOT if isinstance(container, CommentedMap) else _SEQUENCE_COMMENT_SLOT
    return container.ca.items.setdefault(key, [None, None, None, None]), slot


def split_trailing_comment(entry: list[Any], slot: int) -> tuple[str, str]:
    """(rest of the value's own line, comment lines after it) of one comment slot.

    The lines after belong to whatever follows: the next key's leading comment.
    """
    token = entry[slot]
    if token is None:
        return "", ""
    own_line, newline, following = token.value.partition("\n")
    return own_line + newline, following


def set_trailing_comment(entry: list[Any], slot: int, own_line: str, following: str) -> None:
    text = (own_line or "\n") + following if following else own_line
    if not text:
        entry[slot] = None
    elif entry[slot] is None:
        entry[slot] = CommentToken(text, CommentMark(0), None)
    else:
        entry[slot].value = text


def delete_key(mapping: CommentedMap, key: Any) -> None:
    """Delete key with its own comments, keeping the comment that introduces the next key.

    ruamel stores the comment lines between two keys after the first key's
    value, so a plain delete would take the next key's comment and leave the
    deleted key's behind.
    """
    if key not in own_keys(mapping):
        flatten_merge(mapping)
    keys = own_keys(mapping)
    position = keys.index(key)
    _, following = split_trailing_comment(*trailing_comment_slot(mapping, key))
    if position > 0:
        # The previous key's trailing lines were the deleted key's leading comment.
        previous_entry, previous_slot = trailing_comment_slot(mapping, keys[position - 1])
        previous_own_line, _ = split_trailing_comment(previous_entry, previous_slot)
        set_trailing_comment(previous_entry, previous_slot, previous_own_line, following)
    elif following and position + 1 < len(keys):
        mapping.yaml_set_comment_before_after_key(keys[position + 1], before=None)
        before_entry = mapping.ca.items.setdefault(keys[position + 1], [None, None, None, None])
        before_entry[1] = [CommentToken(following, CommentMark(0), None)]
    del mapping[key]


def shared_containers(value: Any, seen: set[int] | None = None, shared: set[int] | None = None) -> set[int]:
    """ids of mappings and sequences reachable from more than one place, as aliases make them."""
    seen = set() if seen is None else seen
    shared = set() if shared is None else shared
    if not isinstance(value, (CommentedMap, CommentedSeq)):
        return shared
    if id(value) in seen:
        shared.add(id(value))
        return shared
    seen.add(id(value))
    children = [child for _, child in value.non_merged_items()] if isinstance(value, CommentedMap) else list(value)
    for child in children:
        shared_containers(child, seen, shared)
    return shared


def unanchored_copy(mapping: CommentedMap) -> CommentedMap:
    copied = copy.deepcopy(mapping)
    copied.anchor.value = None
    return copied


def apply_desired_mapping(target: CommentedMap, desired: Mapping[Any, Any], shared: set[int]) -> None:
    """Edit target in place until it holds desired, touching only what differs."""
    for key in [key for key in target if key not in desired]:
        delete_key(target, key)
    for key, value in desired.items():
        if key in target:
            current = target[key]
            if values_strictly_equal(plain_value(current), plain_value(value)):
                continue
            if isinstance(current, CommentedMap) and isinstance(value, Mapping):
                # An aliased or merged-in mapping is shared with other places,
                # which must keep their value.
                if id(current) in shared or key not in own_keys(target):
                    current = unanchored_copy(current)
                    target[key] = current
                apply_desired_mapping(current, value, shared)
                continue
        target[key] = value


def patched_document(target: CommentedMap, desired: YamlDict) -> CommentedMap:
    expected = plain_value(desired)
    for _ in range(_MAX_APPLY_PASSES):
        apply_desired_mapping(target, desired, shared_containers(target))
        if values_strictly_equal(plain_value(target), expected):
            return target
    raise RuntimeError("YAML round-trip patch did not converge to the transformed data")


def yaml_text(document: CommentedMap, layout: YamlLayout) -> str:
    stream = io.StringIO()
    round_trip_yaml(layout).dump(document, stream)
    return stream.getvalue()


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
        stdin_bytes = request.engine_option("stdin_bytes")
        reused_output = semantically_unchanged_compare_output(
            compare_path,
            plain_value(data),
            lambda content: plain_value(parse_yaml_text(content.decode("utf-8-sig"))),
            mode_reference_path=request.base_path,
        )
        if reused_output is not None:
            return reused_output

        # Patch the base document; with no base, merge output is the overlay's
        # data, so patching the overlay document keeps its text.
        target = load_yaml_document(request.base_path, stdin_bytes=stdin_bytes)
        if target is None and request.mode == TransformMode.MERGE:
            target = load_yaml_document(request.overlay_path, stdin_bytes=stdin_bytes)
        document = patched_document(CommentedMap() if target is None else target, data)
        layout = select_yaml_layout(compare_path, request.base_path, request.overlay_path, stdin_bytes=stdin_bytes)
        return TransformOutput(content=yaml_text(document, layout), mode_reference_path=request.base_path)
