#!/usr/bin/env python3

from __future__ import annotations

import copy
import fnmatch
from pathlib import Path
import re

from lxml import etree

from dotman.transforms.cli import run_engine_cli
from dotman.transforms.framework import (
    STDIN_PATH,
    BaseTransformEngine,
    SelectorAction,
    SelectorSpec,
    TransformOutput,
    TransformRequest,
    compile_selector_regexes,
    read_reference_bytes,
)


NodeRegex = re.Pattern[str]
# Elements, comments, processing instructions, and entity references alike.
XmlNode = etree._Element

# Entities stay unexpanded references and nothing is loaded or fetched, so an
# input cannot pull local files or network content into the output (XXE) or
# expand entity bombs. huge_tree stays off to keep libxml2's size limits.
XML_PARSER = etree.XMLParser(
    resolve_entities=False,
    no_network=True,
    load_dtd=False,
    huge_tree=False,
)


def compile_node_regexes(raw_node_regexes: tuple[str, ...]) -> tuple[NodeRegex, ...]:
    return compile_selector_regexes(raw_node_regexes, "XML node selector")


def matches_node_path(
    node_path: str,
    node_matchers: list[str],
    node_regexes: tuple[NodeRegex, ...] = (),
) -> bool:
    return any(fnmatch.fnmatch(node_path, node_matcher) for node_matcher in node_matchers) or any(
        node_regex.search(node_path) for node_regex in node_regexes
    )


def is_element(node: XmlNode) -> bool:
    # Comments, processing instructions, and entity references are children
    # too, but their tag is a factory function rather than a name.
    return isinstance(node.tag, str)


def child_elements(parent: XmlNode) -> list[XmlNode]:
    return [child for child in parent if is_element(child)]


def element_with_leading_nodes(element: XmlNode) -> list[XmlNode]:
    """Return element preceded by the comments and processing instructions it owns.

    Those directly before an element, with only whitespace between, usually
    describe it, so they are selected, sorted, and merged together with it.
    """
    owned_nodes = [element]
    previous = element.getprevious()
    while (
        previous is not None
        and previous.tag in (etree.Comment, etree.PI)
        and (previous.tail is None or not previous.tail.strip())
    ):
        owned_nodes.insert(0, previous)
        previous = previous.getprevious()
    return owned_nodes


def replace_children(parent: XmlNode, children: list[XmlNode]) -> None:
    # lxml keeps each node's tail on the node, so tails move with it.
    for child in list(parent):
        parent.remove(child)
    parent.extend(children)


def copy_document(root: XmlNode) -> XmlNode:
    # Copying the whole document keeps the doctype and the comments and
    # processing instructions around the root, which an element copy drops.
    return copy.deepcopy(root.getroottree()).getroot()


def element_attribute_identity(element: XmlNode) -> tuple[tuple[str, str], ...]:
    return tuple(
        (attribute_name, element.attrib[attribute_name])
        for attribute_name in ("id", "name", "key", "uuid")
        if attribute_name in element.attrib
    )


def element_identity_key(element: XmlNode) -> tuple[tuple[str, str], ...] | None:
    identity_parts = list(element_attribute_identity(element))

    text_value = (element.text or "").strip()
    if text_value:
        identity_parts.append(("text", text_value))

    if not identity_parts:
        return None

    return tuple(identity_parts)


def pop_matching_child(
    target: XmlNode,
    candidates: list[XmlNode],
) -> XmlNode | None:
    identity_key = element_identity_key(target)
    if identity_key is not None:
        for index, child in enumerate(candidates):
            if child.tag == target.tag and element_identity_key(child) == identity_key:
                return candidates.pop(index)

    # Text may differ between live and repo copies of the same element, and one
    # copy may carry identity attributes the other lacks. Siblings differ when a
    # shared identity attribute differs, or when both are identified but share
    # no attribute to compare; among the rest, prefer the most agreement.
    target_identity = dict(element_attribute_identity(target))
    best_index: int | None = None
    best_agreement = -1
    for index, child in enumerate(candidates):
        if child.tag != target.tag:
            continue
        child_identity = dict(element_attribute_identity(child))
        shared_names = target_identity.keys() & child_identity.keys()
        if any(child_identity[name] != target_identity[name] for name in shared_names):
            continue
        if target_identity and child_identity and not shared_names:
            continue
        if len(shared_names) > best_agreement:
            best_index, best_agreement = index, len(shared_names)

    return None if best_index is None else candidates.pop(best_index)


def copy_with_leading_nodes(element: XmlNode) -> list[XmlNode]:
    return [copy.deepcopy(node) for node in element_with_leading_nodes(element)]


def overlay_with_base_slots(
    original_base_node: XmlNode,
    preserved_base_node: XmlNode | None,
    overlay_node: XmlNode,
) -> XmlNode:
    # The overlay copy supplies attributes, text, namespace declarations, and
    # the order of its children, including its comments and instructions.
    result = copy_document(overlay_node) if overlay_node.getparent() is None else copy.deepcopy(overlay_node)
    if preserved_base_node is None:
        return result

    preserved_children = child_elements(preserved_base_node)
    overlay_children = child_elements(result)
    merged_replacements: dict[XmlNode, XmlNode] = {}
    # Live-only preserved children follow the overlay child that their nearest
    # earlier live sibling paired with (None: before any), so they keep their
    # live neighbours while overlay order decides everything else.
    preserved_after: dict[XmlNode | None, list[XmlNode]] = {None: []}
    anchor: XmlNode | None = None

    for base_child in child_elements(original_base_node):
        preserved_child = pop_matching_child(base_child, preserved_children)
        overlay_child = pop_matching_child(base_child, overlay_children)

        if overlay_child is not None:
            anchor = overlay_child
            preserved_after[anchor] = []
            if preserved_child is not None:
                merged_replacements[overlay_child] = overlay_with_base_slots(
                    base_child, preserved_child, overlay_child
                )
        elif preserved_child is not None:
            preserved_after[anchor].extend(copy_with_leading_nodes(preserved_child))

    merged_children = list(preserved_after[None])
    for child in list(result):
        merged_children.append(merged_replacements.get(child, child))
        merged_children.extend(preserved_after.get(child, ()))
    for preserved_child in preserved_children:
        merged_children.extend(copy_with_leading_nodes(preserved_child))
    replace_children(result, merged_children)
    return result


def drop_indentation_of_emptied_parent(parent: XmlNode) -> None:
    # Call after removing children: whitespace before the first one was
    # indentation, and left on a parent with no children it would read as a
    # whitespace-only value.
    if not len(parent) and parent.text is not None and not parent.text.strip():
        parent.text = None


def retain_nodes(
    root: XmlNode,
    node_matchers: list[str],
    node_regexes: tuple[NodeRegex, ...] = (),
) -> None:
    def retain_matching_descendants(current: XmlNode, cur_path: str) -> bool:
        """Prune current to matched subtrees and their ancestors; report whether any matched."""
        if matches_node_path(cur_path, node_matchers, node_regexes):
            return True

        retained_nodes: set[XmlNode] = set()
        for child in child_elements(current):
            if retain_matching_descendants(child, f"{cur_path}/{child.tag}"):
                retained_nodes.update(element_with_leading_nodes(child))
        removed_nodes = [child for child in current if child not in retained_nodes]
        for child in removed_nodes:
            current.remove(child)
        if removed_nodes:
            drop_indentation_of_emptied_parent(current)
        return bool(retained_nodes)

    retain_matching_descendants(root, root.tag)


def remove_child_keeping_following_text(parent: XmlNode, child: XmlNode) -> None:
    # lxml stores the text after a node as its tail, so a plain remove() would
    # also delete the surrounding document text. Whitespace-only tails are
    # layout, which pretty printing regenerates.
    if child.tail is not None and child.tail.strip():
        previous_sibling = child.getprevious()
        if previous_sibling is None:
            parent.text = (parent.text or "") + child.tail
        else:
            previous_sibling.tail = (previous_sibling.tail or "") + child.tail
    parent.remove(child)


def strip_nodes(
    root: XmlNode,
    node_matchers: list[str],
    node_regexes: tuple[NodeRegex, ...] = (),
) -> None:
    def strip_matching_descendants(current: XmlNode, cur_path: str) -> None:
        for child in child_elements(current):
            child_path = f"{cur_path}/{child.tag}"
            if matches_node_path(child_path, node_matchers, node_regexes):
                for node in element_with_leading_nodes(child):
                    remove_child_keeping_following_text(current, node)
                drop_indentation_of_emptied_parent(current)
            else:
                strip_matching_descendants(child, child_path)

    if matches_node_path(root.tag, node_matchers, node_regexes):
        root.clear()
    else:
        strip_matching_descendants(root, root.tag)


def build_tree_with_selector_action(
    source_root: XmlNode,
    node_matchers: list[str],
    selector_action: SelectorAction,
    node_regexes: tuple[NodeRegex, ...] = (),
) -> XmlNode:
    root = copy_document(source_root)
    if not node_matchers and not node_regexes:
        return root
    if selector_action == SelectorAction.RETAIN:
        retain_nodes(root, node_matchers, node_regexes)
    else:
        strip_nodes(root, node_matchers, node_regexes)
    return root


def parse_node_matchers(raw_node_matchers: tuple[str, ...] | list[str]) -> list[str]:
    parsed_node_matchers: list[str] = []
    for raw_matcher_group in raw_node_matchers:
        for raw_matcher in raw_matcher_group.split(","):
            node_matcher = raw_matcher.strip()
            if node_matcher:
                parsed_node_matchers.append(node_matcher)
    return parsed_node_matchers


def sort_xml_attributes(root: XmlNode) -> None:
    for elem in root.iter(etree.Element):
        sorted_attributes = dict(sorted(elem.attrib.items()))
        elem.attrib.clear()
        elem.attrib.update(sorted_attributes)


def canonical_xml_sort_key(element: XmlNode) -> str:
    normalized = copy.deepcopy(element)
    # Same layout rule as compare: only whitespace between child elements is layout.
    indent_element_only_content(normalized)
    sort_xml_attributes(normalized)
    return etree.tostring(normalized, encoding="unicode", with_tail=False)


def sort_selected_children(root: XmlNode, parent_matchers: list[str]) -> None:
    def sort_selected_children_recursion(current: XmlNode, cur_path: str) -> None:
        if matches_node_path(cur_path, parent_matchers):
            sorted_groups = sorted(
                (element_with_leading_nodes(child) for child in child_elements(current)),
                key=lambda group: canonical_xml_sort_key(group[-1]),
            )
            grouped_nodes = {node for group in sorted_groups for node in group}
            # Comments and instructions that describe no element stay after the sorted ones.
            unowned_nodes = [node for node in current if node not in grouped_nodes]
            replace_children(current, [*(node for group in sorted_groups for node in group), *unowned_nodes])

        for child in child_elements(current):
            child_path = f"{cur_path}/{child.tag}"
            sort_selected_children_recursion(child, child_path)

    sort_selected_children_recursion(root, root.tag)


def normalized_xml_for_compare(
    root: XmlNode,
    child_sort_parent_matchers: list[str] | None = None,
) -> str:
    normalized = copy_document(root)
    sort_xml_attributes(normalized)
    if child_sort_parent_matchers:
        sort_selected_children(normalized, child_sort_parent_matchers)
    # Re-indent after sorting, since sorted children carry their old tails. The
    # writer keeps leaf and mixed-content text, so only whitespace between child
    # elements is layout; `<sep> </sep>` and `<sep/>` hold different values.
    indent_element_only_content(normalized)
    # The whole document, so the doctype and comments around the root count too.
    return etree.tostring(normalized.getroottree(), encoding="unicode")


def get_existing_xml_bytes_if_semantically_unchanged(
    compare_path: Path,
    root: XmlNode,
    child_sort_parent_matchers: list[str] | None = None,
 ) -> bytes | None:
    existing_bytes = read_reference_bytes(compare_path)
    if existing_bytes is None:
        return None
    try:
        existing_root = etree.fromstring(existing_bytes, XML_PARSER)
    except etree.XMLSyntaxError:
        return None

    if normalized_xml_for_compare(
        existing_root,
        child_sort_parent_matchers=child_sort_parent_matchers,
    ) != normalized_xml_for_compare(
        root,
        child_sort_parent_matchers=child_sort_parent_matchers,
    ):
        return None

    return existing_bytes


# Kept byte-identical to the declaration minidom's toprettyxml emitted. Other
# output bytes did change since (`<a/>`, a final newline), so earlier outputs
# are rewritten once unless compare reuse applies.
XML_DECLARATION = '<?xml version="1.0" ?>'
XML_INDENT = "  "


def has_mixed_content(element: XmlNode) -> bool:
    # An entity reference is text the parser left unexpanded.
    return any(child.tag is etree.Entity for child in element) or any(
        text is not None and text.strip()
        for text in (element.text, *(child.tail for child in element))
    )


def indent_element_only_content(element: XmlNode, level: int = 0) -> None:
    # Like etree.indent, but leaves mixed content untouched: etree.indent would
    # still inject newlines where a mixed-content element has no text before
    # its first child or after its last one, changing the document's text.
    if not len(element) or has_mixed_content(element):
        return

    child_indentation = "\n" + XML_INDENT * (level + 1)
    element.text = child_indentation
    for child in element:
        indent_element_only_content(child, level + 1)
        child.tail = child_indentation
    element[-1].tail = "\n" + XML_INDENT * level


def build_pretty_xml_text(root: XmlNode) -> str:
    pretty_root = copy_document(root)
    indent_element_only_content(pretty_root)
    # pretty_print here only breaks lines between the doctype, the root, and
    # the comments and instructions around it: libxml2 leaves an element's
    # content alone once it has text children, and after the indentation above
    # every element with children has some.
    document_text = etree.tostring(pretty_root.getroottree(), encoding="unicode", pretty_print=True)
    return f"{XML_DECLARATION}\n{document_text}"


def ensure_well_formed_output(document_text: str) -> None:
    # A merge writes the overlay's doctype, so kept live nodes can reference
    # entities that only the live doctype defines. Fail instead of writing XML
    # that no reader accepts.
    try:
        etree.fromstring(document_text.encode("utf-8"), XML_PARSER)
    except etree.XMLSyntaxError as error:
        raise ValueError(f"XML output would not be well-formed: {error.msg}") from error


def parse_xml_bytes(content: bytes, source: str) -> XmlNode:
    try:
        return etree.fromstring(content, XML_PARSER)
    except etree.XMLSyntaxError as error:
        raise ValueError(f"{source} is not valid XML: {error.msg}") from error


def read_xml_root(path: Path, stdin_bytes: bytes | None) -> XmlNode | None:
    """Parse XML input, or return None when the input file is missing."""
    if path == STDIN_PATH:
        assert stdin_bytes is not None
        return parse_xml_bytes(stdin_bytes, "stdin")
    if not path.exists():
        return None
    return parse_xml_bytes(path.read_bytes(), str(path))


def render_xml_output(
    base_path: str | Path,
    node_matchers: list[str] | None = None,
    node_regexes: tuple[NodeRegex, ...] = (),
    sort_attributes: bool = False,
    overlay_path: str | Path | None = None,
    selector_action: SelectorAction | None = None,
    compare_path: str | Path | None = None,
    child_sort_parent_matchers: list[str] | None = None,
    stdin_bytes: bytes | None = None,
) -> TransformOutput:
    base_path = Path(base_path)
    overlay_path = Path(overlay_path) if overlay_path is not None else None
    compare_path = Path(compare_path) if compare_path is not None else None
    effective_selector_action = (
        selector_action
        if selector_action is not None
        else SelectorAction.RETAIN
        if overlay_path is not None
        else SelectorAction.REMOVE
    )
    parsed_node_matchers = node_matchers or []
    parsed_child_sort_parent_matchers = child_sort_parent_matchers or []

    base_root = read_xml_root(base_path, stdin_bytes)
    overlay_root = read_xml_root(overlay_path, stdin_bytes) if overlay_path is not None else None

    if base_root is None:
        if overlay_root is None:
            raise ValueError(f"base XML file not found: {base_path}")
        root = etree.Element(overlay_root.tag)
    else:
        root = build_tree_with_selector_action(
            base_root,
            parsed_node_matchers,
            effective_selector_action,
            node_regexes,
        )

    if overlay_root is not None:
        if base_root is None:
            root = copy_document(overlay_root)
        else:
            preserved_root = root
            root = overlay_with_base_slots(base_root, preserved_root, overlay_root)

    if sort_attributes:
        sort_xml_attributes(root)
    if parsed_child_sort_parent_matchers:
        sort_selected_children(root, parsed_child_sort_parent_matchers)

    if compare_path is not None:
        existing_bytes = get_existing_xml_bytes_if_semantically_unchanged(
            compare_path,
            root,
            child_sort_parent_matchers=parsed_child_sort_parent_matchers,
        )
        if existing_bytes is not None:
            return TransformOutput(
                content=existing_bytes,
                mode_reference_path=base_path,
                reused_compare_path=compare_path,
            )

    content = build_pretty_xml_text(root)
    ensure_well_formed_output(content)
    return TransformOutput(
        content=content,
        mode_reference_path=base_path,
    )



class XmlTransformEngine(BaseTransformEngine):
    name = "xml"
    SELECTOR_SPECS = (
        SelectorSpec(
            name="node_matcher",
            prefix="exact",
            is_default=True,
            description="fnmatch-style XML node path matcher",
            examples=("config/WindowGeometry", "config/*WindowState"),
        ),
        SelectorSpec(
            name="node_regex",
            prefix="re",
            description="regex matching XML node paths",
            examples=(r"^config/Window", r"/(Geometry|State)$"),
        ),
    )

    def configure_parser(self, parser) -> None:
        parser.add_argument(
            "--compare-file",
            type=Path,
            help="Optional XML file to compare against for semantic no-op byte reuse",
        )
        parser.add_argument(
            "--sort-attributes",
            action="store_true",
            dest="sort_attributes",
            help="Sort attributes of each element alphabetically",
        )
        parser.add_argument(
            "--sort-children",
            action="append",
            default=[],
            metavar="NODE_PATH",
            help=(
                "Sort immediate children under matching XML node paths; "
                "accepts repeated flags and comma-separated values"
            ),
        )

    def build_engine_options(self, parsed_args) -> dict[str, object]:
        return {
            "compare_path": parsed_args.compare_file,
            "sort_attributes": parsed_args.sort_attributes,
            "child_sort_parent_matchers": tuple(parsed_args.sort_children),
            "stdout": parsed_args.stdout,
            "stdin_bytes": parsed_args.stdin_bytes,
        }

    def validate_request(self, request: TransformRequest) -> None:
        super().validate_request(request)
        compile_node_regexes(request.selector_values("node_regex"))
        if (
            not parse_node_matchers(request.selector_values("node_matcher"))
            and not request.selector_values("node_regex")
        ):
            raise ValueError("node matchers must not be empty")

    def transform(self, request: TransformRequest) -> TransformOutput:
        self.validate_request(request)
        return render_xml_output(
            request.base_path,
            node_matchers=parse_node_matchers(request.selector_values("node_matcher")),
            node_regexes=compile_node_regexes(request.selector_values("node_regex")),
            sort_attributes=bool(request.engine_option("sort_attributes", False)),
            overlay_path=request.overlay_path,
            selector_action=request.selector_action,
            compare_path=request.engine_option("compare_path"),
            child_sort_parent_matchers=parse_node_matchers(
                request.engine_option("child_sort_parent_matchers", ())
            ),
            stdin_bytes=request.engine_option("stdin_bytes"),
        )

def main(argv: list[str] | None = None) -> int:
    return run_engine_cli(XmlTransformEngine(), argv=argv)


if __name__ == "__main__":
    raise SystemExit(main())
