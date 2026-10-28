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
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedKeyMap, CommentedKeySeq, CommentedMap, CommentedSeq, CommentedSet, TaggedScalar, merge_attrib
from ruamel.yaml.error import CommentMark, YAMLError
from ruamel.yaml.nodes import MappingNode, Node, ScalarNode, SequenceNode
from ruamel.yaml.representer import RoundTripRepresenter
from ruamel.yaml.scalarbool import ScalarBoolean
from ruamel.yaml.scalarstring import FoldedScalarString, LiteralScalarString
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

    representing_set = False

    def represent_none(self, data: None) -> ScalarNode:
        # ruamel leaves null values empty (`key:`, a bare `-`); write `null`
        # as dotman's earlier YAML output did, so files it wrote do not churn.
        # A `!!set` member's null value must stay empty, or it joins the member.
        return self.represent_scalar("tag:yaml.org,2002:null", "" if self.representing_set else "null")

    def represent_set(self, setting: CommentedSet) -> Node:
        self.representing_set = True
        try:
            return super().represent_set(setting)
        finally:
            self.representing_set = False


YamlRepresenter.add_representer(type(None), YamlRepresenter.represent_none)
YamlRepresenter.add_representer(CommentedSet, YamlRepresenter.represent_set)


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
    """The value with ruamel's spelling-preserving types replaced by plain Python ones.

    A custom tag on a scalar is part of its value. Tags on mappings and
    sequences are not compared: selection rebuilds mappings without them, and
    the edited document keeps them.
    """
    if isinstance(value, TaggedScalar):
        tag = value.tag.value
        return str(value.value) if tag is None or tag.startswith(_STANDARD_TAG_PREFIX) else TaggedValue(tag, str(value.value))
    if isinstance(value, (CommentedKeyMap, CommentedKeySeq)):
        # Mappings and sequences used as keys must stay hashable.
        return tuple(plain_value(item) for item in (value.items() if isinstance(value, Mapping) else value))
    if isinstance(value, Mapping):
        return {plain_value(key): plain_value(child) for key, child in effective_items(value)}
    if isinstance(value, list):
        return [plain_value(child) for child in value]
    if isinstance(value, tuple):
        return tuple(plain_value(child) for child in value)
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
    # ruamel raises bare KeyError or ValueError for a malformed value under an
    # explicit standard tag, such as `!!bool maybe` or `!!int abc`.
    except (YAMLError, KeyError, ValueError) as error:
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
    if mapping_indent is not None and mapping_indent < 1:
        mapping_indent = None
    sequence_layout = nested_sequence_layout(root_node)
    if mapping_indent is None and sequence_layout is None:
        return None
    default = YamlLayout()
    sequence, offset = sequence_layout or (default.sequence, default.offset)
    return YamlLayout(mapping=mapping_indent or default.mapping, sequence=sequence, offset=offset)


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
        if (
            key_node is not None
            and isinstance(value_node, SequenceNode)
            and not value_node.flow_style
            and value_node.value
            # An anchor or tag moves the sequence start off its first dash.
            and value_node.start_mark.line == value_node.value[0].start_mark.line
        ):
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


def effective_items(mapping: Mapping[Any, Any]) -> list[tuple[Any, Any]]:
    """The mapping's items as written now: its own keys, then keys from its `<<` sources in order.

    ruamel copies merged keys into the merging mapping when loading and does
    not refresh them after an edit to the merged mapping.
    """
    if not isinstance(mapping, CommentedMap):
        return list(mapping.items())
    items = dict(mapping.non_merged_items())
    for source in merge_sources(mapping):
        for key, value in effective_items(source):
            items.setdefault(key, value)
    return list(items.items())


def own_keys(mapping: CommentedMap) -> list[Any]:
    """Keys written in the mapping itself, without those a `<<` merge brings in."""
    return [key for key, _ in mapping.non_merged_items()]


def merge_sources(mapping: CommentedMap) -> list[CommentedMap]:
    return list(getattr(mapping, merge_attrib, []))


def flatten_merge(mapping: CommentedMap) -> None:
    """Write every merged-in key into the mapping and drop its `<<`."""
    for key, value in effective_items(mapping):
        mapping[key] = value
    for source in merge_sources(mapping):
        source._ref.remove(mapping)
    delattr(mapping, merge_attrib)


@dataclass(frozen=True)
class CommentSlot:
    """The ruamel comment token after a value: rest of the value's line, then whole lines.

    The whole lines belong to whatever follows, usually the next key's leading
    comment. After a block scalar the token holds only whole lines.
    """

    entry: list[Any]
    index: int
    after_block_scalar: bool

    def split(self) -> tuple[str, str]:
        token = self.entry[self.index]
        if token is None:
            return "", ""
        if self.after_block_scalar:
            return "", token.value
        own_line, newline, following = token.value.partition("\n")
        return own_line + newline, following

    def set(self, own_line: str, following: str) -> None:
        if self.after_block_scalar or not following:
            text = own_line + following
        else:
            text = (own_line or "\n") + following
        if not text:
            self.entry[self.index] = None
        elif self.entry[self.index] is None:
            self.entry[self.index] = CommentToken(text, CommentMark(0), None)
        else:
            self.entry[self.index].value = text


def trailing_comment_slot(container: CommentedMap | CommentedSeq, key: Any) -> CommentSlot:
    """The slot holding the comment after the value under key.

    A block mapping or sequence value keeps it on its own last entry, so this
    descends to the deepest last entry. Missing entries are created.
    """
    value = container[key]
    if isinstance(value, CommentedMap) and own_keys(value) and not value.fa.flow_style():
        return trailing_comment_slot(value, own_keys(value)[-1])
    if isinstance(value, CommentedSeq) and value and not value.fa.flow_style():
        return trailing_comment_slot(value, len(value) - 1)
    index = _MAPPING_COMMENT_SLOT if isinstance(container, CommentedMap) else _SEQUENCE_COMMENT_SLOT
    entry = container.ca.items.setdefault(key, [None, None, None, None])
    return CommentSlot(entry, index, isinstance(value, (LiteralScalarString, FoldedScalarString)))


def detach_from_merging_mappings(mapping: CommentedMap) -> None:
    # ruamel 0.19.1 crashes refreshing the mappings that merge this one after
    # a delete. Their written `<<` does not depend on that refresh, and the
    # written text is verified afterwards.
    mapping._ref.clear()


def delete_key(mapping: CommentedMap, key: Any) -> None:
    """Delete key with its own comments, keeping the comment that introduces the next key.

    ruamel stores the comment lines between two keys after the first key's
    value, so a plain delete would take the next key's comment and leave the
    deleted key's behind.
    """
    # A merged-in key, or an own key overriding one, cannot be removed while the merge stays.
    if any(key in dict(effective_items(source)) for source in merge_sources(mapping)):
        flatten_merge(mapping)
    detach_from_merging_mappings(mapping)
    keys = own_keys(mapping)
    position = keys.index(key)
    _, following = trailing_comment_slot(mapping, key).split()
    if position > 0:
        # The previous key's trailing lines were the deleted key's leading comment.
        previous_slot = trailing_comment_slot(mapping, keys[position - 1])
        previous_own_line, _ = previous_slot.split()
        previous_slot.set(previous_own_line, following)
    elif following and position + 1 < len(keys):
        before_entry = mapping.ca.items.setdefault(keys[position + 1], [None, None, None, None])
        before_entry[1] = [CommentToken(following, CommentMark(0), None)]
    del mapping[key]


def replace_value(container: CommentedMap, key: Any, value: Any) -> None:
    """Set container[key], keeping the comment lines after the old value in place.

    A value from another document drops the lines after it, which introduced
    its next key there.
    """
    following = ""
    if key in own_keys(container):
        old_slot = trailing_comment_slot(container, key)
        own_line, following = old_slot.split()
        old_slot.set(own_line, "")
    container[key] = value
    new_slot = trailing_comment_slot(container, key)
    new_own_line, _ = new_slot.split()
    new_slot.set(new_own_line, following)


def yaml_children(value: Any) -> list[Any]:
    """Keys, values and merge sources of a mapping, or items of a sequence."""
    if isinstance(value, CommentedMap):
        pairs = list(value.non_merged_items())
        return [*(key for key, _ in pairs), *(child for _, child in pairs), *merge_sources(value)]
    if isinstance(value, (CommentedSeq, CommentedSet)):
        return list(value)
    return []


def walk_yaml_values(value: Any) -> Iterator[Any]:
    """Every value reachable from value, once each, including aliased and merged ones."""
    seen: set[int] = set()
    pending = [value]
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        yield current
        pending.extend(yaml_children(current))


def shared_containers(document: CommentedMap) -> set[int]:
    """ids of mappings and sequences that aliases or `<<` merges reach from more than one place."""
    references: Counter[int] = Counter()
    for value in walk_yaml_values(document):
        references.update(id(child) for child in yaml_children(value) if isinstance(child, (CommentedMap, CommentedSeq)))
    return {container_id for container_id, count in references.items() if count > 1}


def foreign_value(value: Any) -> Any:
    """A private copy of a value from another document, without anchor names.

    Its anchor names could repeat the target's and redirect target aliases;
    ruamel names anchors itself where the copy still shares structure.
    """
    copied = copy.deepcopy(value)
    for item in walk_yaml_values(copied):
        anchor = getattr(item, "anchor", None)
        if anchor is not None:
            anchor.value = None
    return copied


def strict_key(key: Any) -> tuple[type, Any]:
    plain_key = plain_value(key)
    return type(plain_key), plain_key


def apply_desired_mapping(target: CommentedMap, desired: Mapping[Any, Any], shared: set[int] | None) -> None:
    """Edit target in place until it holds desired, touching only what differs.

    With shared container ids given, a shared or merged-in mapping is copied
    before an edit, so its other aliases and merges keep the old value.
    """
    desired_keys = {strict_key(key) for key in desired}
    for key in [key for key, _ in effective_items(target) if strict_key(key) not in desired_keys]:
        delete_key(target, key)
    for key, value in desired.items():
        current_items = dict(effective_items(target))
        if key in current_items:
            current = current_items[key]
            if values_strictly_equal(plain_value(current), plain_value(value)):
                continue
            if isinstance(current, CommentedMap) and isinstance(value, Mapping):
                if shared is not None and (id(current) in shared or key not in own_keys(target)):
                    current = unanchored_copy(current)
                    replace_value(target, key, current)
                apply_desired_mapping(current, value, shared)
                continue
        replace_value(target, key, foreign_value(value))


def unanchored_copy(mapping: CommentedMap) -> CommentedMap:
    copied = copy.deepcopy(mapping)
    copied.anchor.value = None
    return copied


def patched_yaml_text(load_target: Callable[[], CommentedMap], desired: YamlDict, layout: YamlLayout) -> str:
    """The target document's text, edited to hold desired.

    Editing an aliased or merged mapping in place keeps anchors, but changes
    every place that shares it. When the written text shows that changed a
    place that must keep its value, the edit is redone on copies.
    """
    expected = plain_value(desired)
    for copy_shared in (False, True):
        target = load_target()
        apply_desired_mapping(target, desired, shared_containers(target) if copy_shared else None)
        text = yaml_text(target, layout)
        if values_strictly_equal(plain_value(parse_yaml_text(text)), expected):
            return text
    raise ValueError("cannot write the transformed YAML without changing its data")


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
        target_path = request.base_path
        if load_yaml_document(target_path, stdin_bytes=stdin_bytes) is None and request.mode == TransformMode.MERGE:
            target_path = request.overlay_path

        def load_target() -> CommentedMap:
            document = load_yaml_document(target_path, stdin_bytes=stdin_bytes)
            return CommentedMap() if document is None else document

        layout = select_yaml_layout(compare_path, request.base_path, request.overlay_path, stdin_bytes=stdin_bytes)
        return TransformOutput(content=patched_yaml_text(load_target, data, layout), mode_reference_path=request.base_path)
