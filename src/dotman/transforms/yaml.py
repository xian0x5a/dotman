"""YAML transform: shared selection and merge, written back by splicing the target text.

Selection and merge run on the loaded document like every mapping transform.
Output then edits the target document's text: a key whose data is unchanged
keeps its bytes, comments, anchors and merge keys, and only changed keys are
cut, replaced or added, with values rendered by ruamel.yaml. Values follow
YAML 1.2.
"""
from __future__ import annotations

import copy
import datetime
import functools
import io
import warnings
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from enum import Enum, auto
from pathlib import Path
from typing import Any, Mapping

from ruamel.yaml import YAML
from ruamel.yaml.comments import (
    Comment,
    CommentedKeyMap,
    CommentedKeySeq,
    CommentedMap,
    CommentedSeq,
    CommentedSet,
    Format,
    TaggedScalar,
)
from ruamel.yaml.error import ReusedAnchorWarning, YAMLError
from ruamel.yaml.events import (
    AliasEvent,
    CollectionEndEvent,
    Event,
    MappingStartEvent,
    ScalarEvent,
    SequenceStartEvent,
)
from ruamel.yaml.nodes import MappingNode, Node, ScalarNode, SequenceNode
from ruamel.yaml.representer import RoundTripRepresenter
from ruamel.yaml.scalarbool import ScalarBoolean
from ruamel.yaml.scalarstring import FoldedScalarString, LiteralScalarString
from ruamel.yaml.tag import Tag

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
# a long value that only moved.
_NO_WRAP_WIDTH = 1 << 20
# Explicit tags from the YAML type repository (`!!str 12`, and the `=` that
# ruamel resolves to the YAML 1.1 value type) still spell plain strings.
_STANDARD_TAG_PREFIX = "tag:yaml.org,2002:"
# Each splice round fixes the places an earlier round changed through an
# anchor or merge; nesting of shared values bounds how many rounds that takes.
_MAX_SPLICE_ROUNDS = 16
# Values are rendered as this key's value, then cut after its colon.
_RENDERED_VALUE_KEY = "dotman_rendered_value"


@dataclass(frozen=True)
class YamlLayout:
    """Block indentation in ruamel's terms: mapping indent, sequence content indent, dash offset."""

    mapping: int = DEFAULT_YAML_INDENT
    sequence: int = DEFAULT_YAML_INDENT
    offset: int = 0


@dataclass(frozen=True, eq=False)
class TaggedValue:
    """A value under a custom tag such as `!Ref`, which differs from the same untagged value."""

    tag: str
    value: Any

    # Compared like other plain values, so `!foo [1]` and `!foo [true]` differ.
    def __eq__(self, other: object) -> bool:
        return isinstance(other, TaggedValue) and self.tag == other.tag and values_strictly_equal(self.value, other.value)

    def __hash__(self) -> int:
        return hash((self.tag, repr(self.value)))


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


@contextmanager
def reused_anchors_allowed() -> Iterator[None]:
    with warnings.catch_warnings():
        # Redefining an anchor is valid YAML; later aliases use the latest one.
        warnings.simplefilter("ignore", ReusedAnchorWarning)
        yield


def parse_yaml_text(text: str) -> Any:
    with reused_anchors_allowed():
        return round_trip_yaml().load(text)


# Documents are shared by text: transform loads the base, then splicing loads
# the same text again, and ruamel's `<<` merge handling makes loading a
# document with many merges slow. Nothing mutates a loaded document.
@functools.lru_cache(maxsize=4)
def parsed_document(text: str) -> Any:
    return parse_yaml_text(text)


def collection_tag(value: Any) -> str | None:
    tag = getattr(value, "tag", None) if isinstance(value, (CommentedMap, CommentedSeq)) else None
    return None if tag is None else tag.value


def plain_value(value: Any) -> Any:
    """The value with ruamel's spelling-preserving types replaced by plain Python ones.

    A custom tag on a scalar or sequence is part of its value. Mapping tags
    are compared by `mapping_tags_match`, since selection and merge rebuild
    mappings as plain dicts that inherit the written tag.
    """
    if isinstance(value, TaggedScalar):
        tag = value.tag.value
        return str(value.value) if tag is None or tag.startswith(_STANDARD_TAG_PREFIX) else TaggedValue(tag, str(value.value))
    if isinstance(value, (CommentedKeyMap, CommentedKeySeq)):
        # Mappings and sequences used as keys must stay hashable.
        return tuple(plain_value(item) for item in (value.items() if isinstance(value, Mapping) else value))
    if isinstance(value, CommentedSet):
        # Typed members, so `{1}` and `{true}` differ.
        return frozenset(strict_key(member) for member in value)
    if isinstance(value, Mapping):
        return {plain_value(key): plain_value(child) for key, child in value.items()}
    if isinstance(value, list):
        items = [plain_value(child) for child in value]
        tag = collection_tag(value)
        return items if tag is None or tag.startswith(_STANDARD_TAG_PREFIX) else TaggedValue(tag, items)
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


def strict_key(key: Any) -> tuple[type, Any]:
    plain_key = plain_value(key)
    return type(plain_key), plain_key


def mapping_tags_match(current: Any, desired: Any) -> bool:
    """Whether every mapping desired carries from a document has current's tag.

    A plain dict is a mapping that selection or merge rebuilt; it keeps the
    tag written in the target.
    """
    if isinstance(desired, Mapping) and isinstance(current, Mapping):
        if isinstance(desired, CommentedMap) and collection_tag(desired) != collection_tag(current):
            return False
        current_by_key = {strict_key(key): child for key, child in current.items()}
        return all(mapping_tags_match(current_by_key.get(strict_key(key)), child) for key, child in desired.items())
    if isinstance(desired, list) and isinstance(current, list):
        return all(mapping_tags_match(current_item, desired_item) for current_item, desired_item in zip(current, desired))
    return True


def same_value(current: Any, desired: Any) -> bool:
    return values_strictly_equal(plain_value(current), plain_value(desired)) and mapping_tags_match(current, desired)


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


def load_yaml_text(text: str, source: object) -> CommentedMap | None:
    """The text's top-level mapping, or None for an empty or comment-only document."""
    try:
        loaded = parsed_document(text)
    # ruamel raises bare exceptions for some malformed values under an
    # explicit standard tag (`!!bool maybe`, `!!int "0x"`), and TypeError for
    # an unhashable complex key such as `? [a, [b]]`.
    except (YAMLError, KeyError, ValueError, IndexError, TypeError) as error:
        raise ValueError(f"invalid YAML in {source}: {error}") from error
    if loaded is None:
        return None
    if not isinstance(loaded, CommentedMap):
        raise ValueError(f"Expected top-level YAML mapping in {source}")
    return loaded


def load_yaml_document(path: Path, *, stdin_bytes: bytes | None = None) -> CommentedMap | None:
    """The document's top-level mapping, or None for a missing, empty or comment-only document."""
    source_text = read_input_text(path, stdin_bytes=stdin_bytes)
    return None if source_text is None else load_yaml_text(source_text, path)


def load_yaml(path: Path, *, stdin_bytes: bytes | None = None) -> YamlDict:
    document = load_yaml_document(path, stdin_bytes=stdin_bytes)
    return {} if document is None else document


def detect_yaml_layout(text: str | None) -> YamlLayout | None:
    if text is None:
        return None
    try:
        with reused_anchors_allowed():
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


def is_alias_of_earlier_node(child: Node, collection: Node) -> bool:
    # Composing resolves an alias to its anchored node, whose marks point at the anchor.
    return child.start_mark.index < collection.start_mark.index


def nested_mapping_indent(node: Node | None) -> int | None:
    # Measure the first block mapping nested under a key, so literal block
    # scalar content never counts as structural indentation.
    for key_node, value_node in block_children(node):
        if (
            key_node is not None
            and isinstance(value_node, MappingNode)
            and not value_node.flow_style
            and value_node.value
            and not is_alias_of_earlier_node(value_node.value[0][0], value_node)
        ):
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
            and not is_alias_of_earlier_node(value_node.value[0], value_node)
        ):
            key_column = key_node.start_mark.column
            item_column = value_node.value[0].start_mark.column
            # An anchor or tag moves the sequence start off its first dash,
            # which then sits before the item, followed by one space.
            on_dash_line = value_node.start_mark.line == value_node.value[0].start_mark.line
            dash_column = value_node.start_mark.column if on_dash_line else item_column - 2
            dash_offset = dash_column - key_column
            content_indent = item_column - key_column
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


# Text positions of the parsed document.


class NodeKind(Enum):
    SCALAR = auto()
    MAPPING = auto()
    SEQUENCE = auto()
    ALIAS = auto()


@dataclass(eq=False)
class TextNode:
    """Where a parsed node is written: text[start:end], anchor and tag included, no trailing blanks."""

    kind: NodeKind
    start: int
    end: int
    parent: TextNode | None
    flow: bool = False
    # The anchored node an alias refers to.
    target: TextNode | None = None
    is_merge_key: bool = False
    # Set on a mapping value: the key it is written under.
    entry_key: TextNode | None = None
    entries: list[tuple[TextNode, TextNode]] = field(default_factory=list)
    items: list[TextNode] = field(default_factory=list)

    def own_entries(self) -> list[tuple[TextNode, TextNode]]:
        return [(key, value) for key, value in self.entries if not key.is_merge_key]

    def is_within(self, ancestor: TextNode) -> bool:
        node: TextNode | None = self
        while node is not None:
            if node is ancestor:
                return True
            node = node.parent
        return False


@dataclass(frozen=True)
class TextTree:
    root: TextNode | None
    aliases: list[TextNode]


def after_indicator(text: str, position: int, indicator: str) -> int | None:
    """The position just past the next `indicator`, skipping blanks and comments, or None if absent."""
    scan = position
    while scan < len(text):
        character = text[scan]
        if character == indicator:
            return scan + 1
        if character == "#":
            scan = line_end(text, scan)
        elif character.isspace():
            scan += 1
        else:
            break
    return None


def is_keep_chomped(text: str, event: ScalarEvent) -> bool:
    if event.style not in ("|", ">"):
        return False
    header = text[event.start_mark.index : line_end(text, event.start_mark.index)]
    return "+" in header.partition("#")[0]


def text_tree(text: str) -> TextTree:
    """Build text positions from ruamel's parse events.

    A block collection's end event and an empty scalar's events sit at the
    next token, possibly after comment lines, so those spans come from the
    surrounding tokens instead.
    """
    with reused_anchors_allowed():
        events = iter(list(YAML().parse(text)))
    anchors: dict[str, TextNode] = {}
    aliases: list[TextNode] = []

    def build(event: Event, parent: TextNode | None, cursor: int) -> TextNode:
        start, end = event.start_mark.index, event.end_mark.index
        if isinstance(event, AliasEvent):
            node = TextNode(NodeKind.ALIAS, start, end, parent, target=anchors[event.anchor])
            aliases.append(node)
            return node
        if isinstance(event, ScalarEvent):
            if event.value == "" and event.style is None:
                if event.anchor is None and event.tag is None:
                    start = end = cursor
                else:
                    end = start + len(text[start:line_end(text, start)].partition(" #")[0].rstrip())
            elif is_keep_chomped(text, event):
                # Its trailing blank lines are part of its value.
                end -= text[start:end].endswith("\n")
            else:
                end = start + len(text[start:end].rstrip())
            node = TextNode(NodeKind.SCALAR, start, end, parent)
        else:
            assert isinstance(event, (MappingStartEvent, SequenceStartEvent))
            is_mapping = isinstance(event, MappingStartEvent)
            node = TextNode(NodeKind.MAPPING if is_mapping else NodeKind.SEQUENCE, start, end, parent, flow=bool(event.flow_style))
            if event.anchor is not None:
                anchors[event.anchor] = node
            child_cursor = start if event.anchor is None and event.tag is None else end
            if not is_mapping and text[start:end].endswith("-"):
                # A block sequence's start event ends after its first dash.
                child_cursor = end - 1
            while not isinstance(child_event := next(events), CollectionEndEvent):
                if is_mapping:
                    key = build(child_event, node, child_cursor)
                    key.is_merge_key = (
                        isinstance(child_event, ScalarEvent)
                        and child_event.value == "<<"
                        and child_event.style is None
                        and child_event.tag is None
                    )
                    value = build(next(events), node, after_indicator(text, key.end, ":") or key.end)
                    value.entry_key = key
                    node.entries.append((key, value))
                    child_cursor = max(key.end, value.end)
                else:
                    item = build(child_event, node, after_indicator(text, child_cursor, "-") or child_cursor)
                    node.items.append(item)
                    child_cursor = item.end
            node.end = child_event.end_mark.index if node.flow else child_cursor
        if getattr(event, "anchor", None) is not None and node.kind is NodeKind.SCALAR:
            anchors[event.anchor] = node
        return node

    root: TextNode | None = None
    for event in events:
        if isinstance(event, (MappingStartEvent, SequenceStartEvent, ScalarEvent, AliasEvent)):
            root = build(event, None, event.start_mark.index)
    return TextTree(root, aliases)


def line_start(text: str, position: int) -> int:
    return text.rfind("\n", 0, position) + 1


def line_end(text: str, position: int) -> int:
    end = text.find("\n", position)
    return len(text) if end < 0 else end


def next_line_start(text: str, position: int) -> int:
    return min(line_end(text, position) + 1, len(text))


def indentation(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def is_comment_line(line: str) -> bool:
    return line.lstrip(" ").startswith("#")


@dataclass(frozen=True)
class LinePlace:
    """Where a key or sequence dash starts its line."""

    # The column its block is indented at.
    indent: int
    # Only indentation, or an explicit key's `?`, precedes it, so whole-line
    # edits can remove or insert beside it. A key after a sequence dash
    # (`- key: 1`) shares its line with the item.
    owns_line: bool


def line_place(text: str, position: int) -> LinePlace | None:
    prefix = text[line_start(text, position) : position]
    if prefix.strip() in ("", "?"):
        return LinePlace(indentation(prefix), owns_line=True)
    if not prefix.strip(" -"):
        return LinePlace(len(prefix), owns_line=False)
    return None


def after_deeper_comments(text: str, position: int, indent: int) -> int:
    """The end of the comment lines from position on that are indented deeper than indent.

    Those comments follow a value inside its block, so they belong to it.
    """
    end = scan = position
    while scan < len(text):
        line = text[scan : line_end(text, scan)]
        if line.strip() and not (is_comment_line(line) and indentation(line) > indent):
            break
        scan = next_line_start(text, scan) if line_end(text, scan) < len(text) else len(text)
        if line.strip():
            end = scan
    return end


def head_comment_start(text: str, position: int, indent: int) -> int:
    """The start of the comment lines directly above position, indented no deeper than indent."""
    start = position
    while start > 0:
        previous_start = line_start(text, start - 1)
        line = text[previous_start : start - 1]
        if not is_comment_line(line) or indentation(line) > indent:
            break
        start = previous_start
    return start


def entry_end(text: str, key: TextNode, value: TextNode, indent: int) -> int:
    """The end of the entry's last line, including comments deeper than its key."""
    return after_deeper_comments(text, next_line_start(text, max(key.end, value.end)), indent)


def indented(text: str, indent: int) -> str:
    # A line of only spaces can be literal block content, so only empty lines stay as they are.
    padding = " " * indent
    return "".join(line if line == "\n" else padding + line for line in text.splitlines(keepends=True))


# Rendering changed values.


def copy_presentation(source: Any, copied: CommentedMap | CommentedSeq, *, comments: bool) -> None:
    for attribute in (Comment.attrib, Format.attrib, Tag.attrib):
        if hasattr(source, attribute) and (comments or attribute != Comment.attrib):
            setattr(copied, attribute, copy.deepcopy(getattr(source, attribute)))


def render_copy(value: Any, written: Any = None, *, comments: bool = True, in_flow: bool = False) -> Any:
    """A copy of value to render, sharing nothing: aliases become copies, `<<` merges own keys, anchors go.

    It keeps tags, flow style and comments: value's own, or, for a mapping
    that selection or merge rebuilt as a plain dict, those of written, the
    mapping at the same place in the target. ruamel files comments by key
    and position, so written's comments only carry over when the keys are
    the same. Rendering a copy cannot define an anchor that redirects
    another place's alias.
    """
    if isinstance(value, Mapping) and not isinstance(value, (CommentedKeyMap, CommentedSet)):
        presentation = value if isinstance(value, CommentedMap) else written if isinstance(written, CommentedMap) else None
        # An `!!omap` stays ordered.
        copied_mapping = CommentedMap() if presentation is None else type(presentation)()
        written_children = {strict_key(key): child for key, child in written.items()} if isinstance(written, Mapping) else {}
        child_in_flow = in_flow or (presentation is not None and bool(presentation.fa.flow_style()))
        for key, child in value.items():
            copied_mapping[render_copy(key)] = render_copy(
                child, written_children.get(strict_key(key)), comments=comments, in_flow=child_in_flow
            )
        if presentation is not None:
            same_keys = presentation is value or list(written_children) == [strict_key(key) for key in value]
            copy_presentation(presentation, copied_mapping, comments=comments and same_keys)
        return copied_mapping
    if isinstance(value, list):
        written_items = written if isinstance(written, list) and len(written) == len(value) else [None] * len(value)
        child_in_flow = in_flow or (isinstance(value, CommentedSeq) and bool(value.fa.flow_style()))
        copied_sequence = CommentedSeq(
            render_copy(item, written_item, comments=comments, in_flow=child_in_flow)
            for item, written_item in zip(value, written_items)
        )
        copy_presentation(value, copied_sequence, comments=comments)
        return copied_sequence
    if in_flow and isinstance(value, (LiteralScalarString, FoldedScalarString)):
        # A block scalar cannot sit inside a flow collection.
        return str(value)
    if getattr(getattr(value, "anchor", None), "value", None) is not None:
        copied_scalar = copy.deepcopy(value)
        copied_scalar.anchor.value = None
        return copied_scalar
    return value


def yaml_text(document: Any, layout: YamlLayout) -> str:
    stream = io.StringIO()
    round_trip_yaml(layout).dump(document, stream)
    return stream.getvalue()


def without_trailing_comments(rendered: str) -> str:
    """rendered without its trailing comment lines.

    A rendered value carries the comment lines its source document wrote
    after it, which introduce that document's next key. A block scalar's
    content can look like a comment, so the cut must leave the data alone.
    """
    lines = rendered.splitlines(keepends=True)
    while lines and (not lines[-1].strip() or is_comment_line(lines[-1])):
        lines.pop()
    candidate = "".join(lines)
    if candidate != rendered and reads_as(candidate, rendered):
        return candidate
    return rendered


def reads_as(text: str, expected_text: str) -> bool:
    try:
        return values_strictly_equal(plain_value(parse_yaml_text(text)), plain_value(parse_yaml_text(expected_text)))
    except YAMLError:
        return False


def rendered_fragment(data: Mapping[Any, Any], layout: YamlLayout, written: Any) -> str:
    """data rendered as a document, without trailing comments or a document end marker.

    ruamel's emitter can crash on, or write unreadable YAML for, comments
    copied to a value written at another place or indentation; those are
    dropped then.
    """
    plain_data = plain_value(data)
    for comments in (True, False):
        copied = render_copy(data, written, comments=comments)
        try:
            # ruamel ends a document whose last scalar keeps its trailing
            # newlines (`|+`) with `...`, which would end the spliced document.
            rendered = without_trailing_comments(yaml_text(copied, layout).removesuffix("...\n"))
            if comments and not values_strictly_equal(plain_value(parse_yaml_text(rendered)), plain_data):
                continue
        except Exception:
            if not comments:
                raise
            continue
        return rendered
    raise AssertionError("unreachable")


def rendered_entry(key: Any, value: Any, layout: YamlLayout, indent: int, written: Any = None) -> str:
    """The `key: value` lines, indented, each ending in a newline."""
    entry = CommentedMap()
    entry[key] = value
    written_entry = None if written is None else {key: written}
    return indented(rendered_fragment(entry, layout, written_entry), indent)


def rendered_after_colon(value: Any, layout: YamlLayout, indent: int, written: Any) -> str:
    """The value as written after a key's colon: ` 1`, or a newline and indented block lines."""
    entry = rendered_entry(_RENDERED_VALUE_KEY, value, layout, 0, written)
    first_line, newline, block = entry.removeprefix(_RENDERED_VALUE_KEY + ":").partition("\n")
    return (first_line + newline + indented(block, indent)).removesuffix("\n")


def dedented(text: str, columns: int) -> str:
    return "".join(
        line[min(columns, indentation(line)) :] for line in text.splitlines(keepends=True)
    )


def rendered_items(values: list[Any], layout: YamlLayout, indent: int) -> str:
    """The `- value` lines with their dashes at indent, each ending in a newline."""
    entry = rendered_entry(_RENDERED_VALUE_KEY, values, layout, 0)
    # Under a key, ruamel offsets the dashes by the layout's dash offset.
    return indented(dedented(entry.partition("\n")[2], layout.offset), indent)


def rendered_after_dash(value: Any, layout: YamlLayout, indent: int, written: Any) -> str:
    """The item as written after its dash: ` 1`, or ` a: 1` and indented block lines."""
    entry = rendered_entry(_RENDERED_VALUE_KEY, [value], layout, 0, [written])
    item = indented(dedented(entry.partition("\n")[2], layout.offset), 0)
    first_line, newline, block = item.removeprefix("-").partition("\n")
    return (first_line + newline + indented(block, indent)).removesuffix("\n")


def rendered_document(desired: Mapping[Any, Any], layout: YamlLayout, written: Any) -> str:
    """The desired document, without the comment lines above its first key, which the target text keeps."""
    lines = rendered_fragment(desired, layout, written).splitlines(keepends=True)
    while lines and (not lines[0].strip() or is_comment_line(lines[0])):
        lines.pop(0)
    return "".join(lines)


# Splicing.


@dataclass(frozen=True)
class Edit:
    """Replace text[start:end] with replacement; owner is the node whose data the edit changes."""

    start: int
    end: int
    replacement: str
    owner: TextNode | None

    def removes(self, position: int) -> bool:
        return self.start <= position < self.end


class DocumentSplicer:
    """One round of edits that turn the document text toward the desired data.

    The text ends with a line break, so whole-line edits never need one added.
    """

    def __init__(self, text: str, desired: Mapping[Any, Any], layout: YamlLayout) -> None:
        self.text = text
        self.desired = desired
        self.layout = layout
        self.loaded = load_yaml_text(text, "the spliced YAML")
        self.tree = text_tree(text)

    def edits(self) -> list[Edit]:
        root = self.tree.root
        if root is None:
            if not self.desired:
                return []
            return [Edit(len(self.text), len(self.text), rendered_document(self.desired, self.layout, self.loaded), None)]
        if same_value(self.loaded, self.desired):
            return []
        planned = self.mapping_edits(root, self.loaded, self.desired)
        if planned is None:
            planned = [self.root_replacement()]
        # An edit to an anchored value also changes its aliases and `<<`
        # merges, so those are diffed again only after it lands.
        shared_values = {alias.target for alias in self.tree.aliases if alias.target is not None}
        shared_edits = [
            edit
            for edit in planned
            if edit.owner is not None and any(edit.owner.is_within(shared) for shared in shared_values)
        ]
        return self.with_aliases_written_out(shared_edits or planned, planned)

    def root_replacement(self) -> Edit:
        root = self.tree.root
        assert root is not None
        end = after_deeper_comments(self.text, next_line_start(self.text, root.end), 0)
        rendered = rendered_document(self.desired, self.layout, self.loaded)
        if self.text[line_start(self.text, root.start) : root.start].strip():
            # The root's anchor or tag follows `---` on its line.
            rendered = "\n" + rendered
        return Edit(root.start, end, rendered, root)

    def mapping_edits(self, node: TextNode, loaded: Any, desired: Mapping[Any, Any]) -> list[Edit] | None:
        """Line edits that make a block mapping hold desired, or None when the mapping must be rendered whole."""
        if node.kind is not NodeKind.MAPPING or node.flow or type(loaded) is not CommentedMap:
            return None
        if isinstance(desired, CommentedMap) and collection_tag(desired) != collection_tag(loaded):
            return None
        own_entries = node.own_entries()
        own_items = list(loaded.non_merged_items())
        if len(own_entries) != len(own_items):
            return None
        own_keys = {strict_key(key) for key, _ in own_items}
        merged_values = {strict_key(key): value for key, value in loaded.items() if strict_key(key) not in own_keys}
        desired_items = {strict_key(key): (key, value) for key, value in desired.items()}
        if any(key not in desired_items for key in merged_values):
            # A `<<` merge still brings in the key.
            return None

        edits: list[Edit] = []
        kept_entries = 0
        for (key_node, value_node), (key, current) in zip(own_entries, own_items):
            place = line_place(self.text, key_node.start)
            if place is None:
                return None
            desired_item = desired_items.get(strict_key(key))
            if desired_item is None:
                if not place.owns_line:
                    return None
                start = head_comment_start(self.text, line_start(self.text, key_node.start), place.indent)
                edits.append(Edit(start, entry_end(self.text, key_node, value_node, place.indent), "", value_node))
                continue
            kept_entries += 1
            desired_value = desired_item[1]
            if not same_value(current, desired_value):
                value_edits = self.value_edits(key_node, value_node, current, desired_value, place.indent)
                if value_edits is None:
                    return None
                edits.extend(value_edits)

        appended = [
            (key, value)
            for strict, (key, value) in desired_items.items()
            if strict not in own_keys and not (strict in merged_values and same_value(merged_values[strict], value))
        ]
        if appended:
            last_key, last_value = node.entries[-1]
            first_place = line_place(self.text, node.entries[0][0].start)
            if first_place is None:
                return None
            position = entry_end(self.text, last_key, last_value, first_place.indent)
            rendered = "".join(
                rendered_entry(key, value, self.layout, first_place.indent, merged_values.get(strict_key(key)))
                for key, value in appended
            )
            edits.append(Edit(position, position, rendered, node))
        if not kept_entries and not appended and len(own_entries) == len(node.entries):
            # An emptied mapping is written `{}`.
            return None
        return edits

    def nested_edits(self, node: TextNode, current: Any, desired: Any) -> list[Edit] | None:
        if isinstance(desired, Mapping):
            return self.mapping_edits(node, current, desired)
        if isinstance(desired, list):
            return self.sequence_edits(node, current, desired)
        return None

    def value_edits(self, key: TextNode, value: TextNode, current: Any, desired: Any, indent: int) -> list[Edit] | None:
        nested_edits = self.nested_edits(value, current, desired)
        if nested_edits is not None:
            return nested_edits
        replacement = self.value_replacement(key, value, current, desired, indent)
        return None if replacement is None else [replacement]

    def sequence_edits(self, node: TextNode, loaded: Any, desired: list[Any]) -> list[Edit] | None:
        """Line edits that make a block sequence hold desired, or None when it must be rendered whole.

        Items are matched by value, so an item that stays keeps its text even
        when items before it come or go.
        """
        if node.kind is not NodeKind.SEQUENCE or node.flow or type(loaded) is not CommentedSeq or not desired:
            return None
        if isinstance(desired, CommentedSeq) and collection_tag(desired) != collection_tag(loaded):
            return None
        dashes = [self.item_dash(item) for item in node.items]
        places = [None if dash is None else line_place(self.text, dash) for dash in dashes]
        if len(node.items) != len(loaded) or any(place is None or not place.owns_line for place in places):
            return None
        indent = places[0].indent

        def item_start(index: int) -> int:
            return head_comment_start(self.text, line_start(self.text, dashes[index]), indent)

        def item_end(index: int) -> int:
            return after_deeper_comments(self.text, next_line_start(self.text, node.items[index].end), indent)

        matcher = SequenceMatcher(
            None,
            [repr(plain_value(item)) for item in loaded],
            [repr(plain_value(item)) for item in desired],
            autojunk=False,
        )
        edits: list[Edit] = []
        for operation, old_start, old_end, new_start, new_end in matcher.get_opcodes():
            if operation in ("equal", "replace") and old_end - old_start == new_end - new_start:
                # Matching by plain value misses a changed mapping tag.
                for offset in range(old_end - old_start):
                    index = old_start + offset
                    if not same_value(loaded[index], desired[new_start + offset]):
                        edits.extend(self.item_edits(node.items[index], loaded[index], desired[new_start + offset], indent))
                continue
            edits.extend(Edit(item_start(index), item_end(index), "", node.items[index]) for index in range(old_start, old_end))
            if new_end > new_start:
                position = item_start(old_end) if old_end < len(node.items) else item_end(len(node.items) - 1)
                edits.append(Edit(position, position, rendered_items(desired[new_start:new_end], self.layout, indent), node))
        return edits

    def item_dash(self, item: TextNode) -> int | None:
        dash = item.start - 1
        while dash >= 0 and self.text[dash].isspace():
            dash -= 1
        return dash if dash >= 0 and self.text[dash] == "-" else None

    def item_edits(self, item: TextNode, current: Any, desired: Any, indent: int) -> list[Edit]:
        nested_edits = self.nested_edits(item, current, desired)
        if nested_edits is not None:
            return nested_edits
        dash = self.item_dash(item)
        assert dash is not None
        rendered = rendered_after_dash(desired, self.layout, indent, current)
        return [self.replacement_edit(dash + 1, item, rendered, indent)]

    def value_replacement(self, key: TextNode, value: TextNode, current: Any, desired: Any, indent: int) -> Edit | None:
        """Replace what follows the key's colon, or None for an explicit `?` key written without one."""
        start = after_indicator(self.text, key.end, ":")
        if start is None:
            return None
        return self.replacement_edit(start, value, rendered_after_colon(desired, self.layout, indent, current), indent)

    def replacement_edit(self, start: int, value: TextNode, rendered: str, indent: int) -> Edit:
        """Replace value, written from start on (after its colon or dash), with rendered.

        A replaced multi-line value takes its last line's end-of-line comment
        and the deeper comment lines after it along; the comment on the line
        it starts after stays. A one-line value that becomes a block hands its
        end-of-line comment to that line.
        """
        value_end = max(start, value.end)
        first_line, newline, block = rendered.partition("\n")
        start_line_end = line_end(self.text, start)
        if value_end > start_line_end:
            start_line_rest = self.text[start:start_line_end]
            # The value's anchor or tag may come before the comment.
            comment_start = 0 if start_line_rest.lstrip().startswith("#") else start_line_rest.find(" #")
            if comment_start >= 0:
                rendered = first_line + start_line_rest[comment_start:] + newline + block
            end = after_deeper_comments(self.text, next_line_start(self.text, value_end), indent)
            return Edit(start, end, rendered + "\n", value)
        trailing = self.text[value_end:start_line_end]
        if newline and trailing.lstrip().startswith("#"):
            return Edit(start, start_line_end, first_line + trailing + newline + block, value)
        return Edit(start, value_end, rendered, value)

    def with_aliases_written_out(self, edits: list[Edit], planned: list[Edit]) -> list[Edit]:
        """Add edits so no alias outlives the anchor an edit removes.

        An alias that a planned edit removes anyway gets that edit now.
        Otherwise its mapping entry is rendered whole from the desired data,
        or the document when no block mapping entry holds it.
        """
        while True:
            removing = [edit for edit in edits if edit.start < edit.end]
            stranded = [
                alias
                for alias in self.tree.aliases
                if alias.target is not None
                and any(edit.removes(alias.target.start) for edit in removing)
                and not any(edit.removes(alias.start) for edit in removing)
            ]
            if not stranded:
                return edits
            for alias in stranded:
                if any(edit.removes(alias.start) for edit in edits):
                    continue
                replacement = next((edit for edit in planned if edit.removes(alias.start)), None) or self.entry_replacement(alias)
                owner = replacement.owner
                assert owner is not None
                edits = [edit for edit in edits if edit.owner is None or not edit.owner.is_within(owner)] + [replacement]

    def entry_replacement(self, node: TextNode) -> Edit:
        """Render the nearest mapping entry around node reachable through block mapping entries."""
        steps: list[TextNode] = []
        while node.parent is not None:
            steps.append(node)
            node = node.parent
        entry_values: list[TextNode] = []
        for step in reversed(steps):
            parent = step.parent
            assert parent is not None
            if parent.kind is not NodeKind.MAPPING or parent.flow or step.entry_key is None or step.entry_key.is_merge_key:
                break
            entry_values.append(step)
        loaded: Any = self.loaded
        desired: Any = self.desired
        for step in entry_values:
            assert step.parent is not None and step.entry_key is not None
            if type(loaded) is not CommentedMap or not isinstance(desired, Mapping):
                return self.root_replacement()
            own_values = [value for _, value in step.parent.own_entries()]
            key = list(loaded.non_merged_items())[own_values.index(step)][0]
            desired_by_key = {strict_key(desired_key): child for desired_key, child in desired.items()}
            if strict_key(key) not in desired_by_key:
                return self.root_replacement()
            loaded, desired = loaded[key], desired_by_key[strict_key(key)]
        if not entry_values:
            return self.root_replacement()
        entry_value = entry_values[-1]
        assert entry_value.entry_key is not None
        place = line_place(self.text, entry_value.entry_key.start)
        replacement = None if place is None else self.value_replacement(entry_value.entry_key, entry_value, loaded, desired, place.indent)
        return self.root_replacement() if replacement is None else replacement


def applied_edits(text: str, edits: list[Edit]) -> str:
    pieces: list[str] = []
    position = 0
    for edit in sorted(edits, key=lambda edit: (edit.start, edit.end)):
        if edit.start < position:
            raise AssertionError(f"overlapping YAML edits at {edit.start}")
        pieces.append(text[position : edit.start])
        pieces.append(edit.replacement)
        position = edit.end
    pieces.append(text[position:])
    return "".join(pieces)


def spliced_yaml_text(text: str, desired: Mapping[Any, Any], layout: YamlLayout) -> str:
    """The target text, edited until it holds desired.

    An edit inside an anchored or merged mapping also changes the places that
    share it. The next round sees the ones that must keep their value and
    writes them out, or overrides the merged key.
    """
    expected = plain_value(desired)
    # Splice a text ending in a line break, and give back the one added.
    added_line_break = bool(text) and not text.endswith("\n")
    if added_line_break:
        text += "\n"
    for _ in range(_MAX_SPLICE_ROUNDS):
        try:
            splicer = DocumentSplicer(text, desired, layout)
        except (YAMLError, ValueError) as error:
            raise ValueError(f"cannot write the transformed YAML without changing its data: {error}") from error
        edits = splicer.edits()
        if not edits:
            loaded = {} if splicer.loaded is None else splicer.loaded
            if values_strictly_equal(plain_value(loaded), expected) and mapping_tags_match(loaded, desired):
                if not text.strip() and not desired:
                    return "{}\n"
                return text.removesuffix("\n") if added_line_break else text
            break
        edited_text = applied_edits(text, edits)
        if edited_text == text:
            break
        text = edited_text
    raise ValueError("cannot write the transformed YAML without changing its data")


def target_line_ending(text: str | None) -> str:
    return "\r\n" if text is not None and "\r\n" in text else "\n"


def with_line_ending(text: str, line_ending: str) -> str:
    lf_text = text.replace("\r\n", "\n")
    return lf_text if line_ending == "\n" else lf_text.replace("\n", line_ending)


def holds_yaml_mapping(text: str | None) -> bool:
    try:
        return text is not None and load_yaml_text(text, "reference") is not None
    except ValueError:
        return False


def splice_target_text(request: TransformRequest, compare_path: Path | None, stdin_bytes: bytes | None) -> str:
    """The text output is spliced from: the first document holding a mapping.

    The compare file is the file output replaces, so splicing it changes the
    least; it is advisory and skipped when unreadable. Without one, the base,
    then the overlay when merge has no base, since merge output is then the
    overlay's data.
    """
    base_text = read_input_text(request.base_path, stdin_bytes=stdin_bytes)
    candidates = [read_reference_text(compare_path, stdin_bytes=stdin_bytes), base_text]
    if request.mode == TransformMode.MERGE:
        assert request.overlay_path is not None
        candidates.append(read_input_text(request.overlay_path, stdin_bytes=stdin_bytes))
    return next((text for text in candidates if holds_yaml_mapping(text)), base_text or "")


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

        target_text = splice_target_text(request, compare_path, stdin_bytes)
        line_ending = target_line_ending(target_text)

        layout = select_yaml_layout(compare_path, request.base_path, request.overlay_path, stdin_bytes=stdin_bytes)
        # Splice LF text; the target's line ending is restored afterwards.
        text = spliced_yaml_text(with_line_ending(target_text, "\n"), data, layout)
        return TransformOutput(content=with_line_ending(text, line_ending), mode_reference_path=request.base_path)
