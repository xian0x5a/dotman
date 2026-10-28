from __future__ import annotations

import io
from pathlib import Path
import xml.etree.ElementTree as ET

import pytest

from dotman import cli
from dotman.transforms import xml as MODULE
from dotman.transforms.framework import emit_transform_output


def transform_xml(base_path: str | Path, output_path: str | Path | None, *, stdout: bool = False, **options) -> None:
    # File-level driver: the engine's XML output builder plus the shared emitter.
    emit_transform_output(
        Path(output_path) if output_path is not None else None,
        MODULE.render_xml_output(base_path, **options),
        stdout=stdout,
    )


def parse_xml(path: Path) -> ET.Element:
    return ET.parse(path).getroot()


def write_semantically_equal_overlay_xml(repo_path: Path, live_path: Path) -> str:
    repo_path.write_text(
        """<config>
  <keep id="1"></keep>
  <tail></tail>
</config>
""",
        encoding="utf-8",
    )
    live_text = """<?xml version="1.0"?>
<config>
 <keep id="1"/>
 <WindowGeometry y="200" x="100"/>
 <tail/>
</config>"""
    live_path.write_text(live_text, encoding="utf-8")
    return live_text


def test_xml_engine_declares_typed_selectors() -> None:
    selector_specs = {spec.name: spec for spec in MODULE.XmlTransformEngine.selector_specs()}

    assert selector_specs["node_matcher"].prefix == "exact"
    assert selector_specs["node_regex"].prefix == "re"


def test_main_accepts_typed_selector_flags(tmp_path: Path) -> None:
    repo_path = tmp_path / "repo.xml"
    live_path = tmp_path / "live.xml"
    output_path = tmp_path / "output.xml"

    repo_path.write_text(
        """<config>
  <keep>repo</keep>
</config>
""",
        encoding="utf-8",
    )
    live_path.write_text(
        """<config>
  <keep>live</keep>
  <WindowGeometry y="200" x="100" />
</config>
""",
        encoding="utf-8",
    )

    exit_code = cli.main(
        [
            "transform",
            "xml",
            str(live_path),
            str(output_path),
            "--mode",
            "merge",
            "--overlay-file",
            str(repo_path),
            "--selector-type",
            "retain",
            "--selectors",
            "config/WindowGeometry",
            "--sort-attributes",
        ]
    )

    assert exit_code == 0
    root = parse_xml(output_path)
    assert root.findtext("keep") == "repo"
    assert root.find("WindowGeometry") is not None
    assert root.find("WindowGeometry").attrib == {"x": "100", "y": "200"}


def test_main_accepts_regex_node_selector_flags(tmp_path: Path) -> None:
    repo_path = tmp_path / "repo.xml"
    live_path = tmp_path / "live.xml"
    output_path = tmp_path / "output.xml"

    repo_path.write_text(
        """<config>
  <keep>repo</keep>
</config>
""",
        encoding="utf-8",
    )
    live_path.write_text(
        """<config>
  <keep>live</keep>
  <WindowGeometry y="200" x="100" />
  <WindowState fullscreen="true" />
</config>
""",
        encoding="utf-8",
    )

    exit_code = cli.main(
        [
            "transform",
            "xml",
            str(live_path),
            str(output_path),
            "--mode",
            "merge",
            "--overlay-file",
            str(repo_path),
            "--selector-type",
            "retain",
            "--selectors",
            r"re:^config/Window",
        ]
    )

    assert exit_code == 0
    root = parse_xml(output_path)
    assert root.findtext("keep") == "repo"
    assert root.find("WindowGeometry") is not None
    assert root.find("WindowState") is not None


def test_main_accepts_sort_children_flags(tmp_path: Path) -> None:
    input_path = tmp_path / "input.xml"
    output_path = tmp_path / "output.xml"

    input_path.write_text(
        """<config>
  <mutedDictionaries>
    <mutedDictionary>b</mutedDictionary>
    <mutedDictionary>a</mutedDictionary>
  </mutedDictionaries>
</config>
""",
        encoding="utf-8",
    )

    exit_code = cli.main(
        [
            "transform",
            "xml",
            str(input_path),
            str(output_path),
            "--mode",
            "cleanup",
            "--sort-children",
            "config/mutedDictionaries",
            "--selector-type",
            "remove",
            "--selectors",
            "config/WindowGeometry",
        ]
    )

    assert exit_code == 0
    root = parse_xml(output_path)
    assert [
        child.text for child in root.findall("mutedDictionaries/mutedDictionary")
    ] == ["a", "b"]


def test_parse_node_matchers_accepts_repeated_and_comma_separated_values() -> None:
    assert MODULE.parse_node_matchers(
        ("config/WindowGeometry,config/WindowState", "config/timeForNewReleaseCheck")
    ) == [
        "config/WindowGeometry",
        "config/WindowState",
        "config/timeForNewReleaseCheck",
    ]


def test_strip_nodes_removes_selected_live_only_xml_paths(tmp_path: Path) -> None:
    input_path = tmp_path / "input.xml"
    output_path = tmp_path / "output.xml"

    input_path.write_text(
        """<config>
  <WindowGeometry x="100" y="200" />
  <WindowState fullscreen="true" />
  <timeForNewReleaseCheck>12345</timeForNewReleaseCheck>
  <keep>repo</keep>
</config>
""",
        encoding="utf-8",
    )

    transform_xml(
        str(input_path),
        str(output_path),
        node_matchers=[
            "config/WindowGeometry",
            "config/WindowState",
            "config/timeForNewReleaseCheck",
        ],
        sort_attributes=True,
        selector_action=MODULE.SelectorAction.REMOVE,
    )

    root = parse_xml(output_path)

    assert root.find("keep") is not None
    assert root.find("WindowGeometry") is None
    assert root.find("WindowState") is None
    assert root.find("timeForNewReleaseCheck") is None


def test_merge_mode_retain_preserves_selected_live_paths_and_reapplies_repo_tree(
    tmp_path: Path,
) -> None:
    live_path = tmp_path / "live.xml"
    repo_path = tmp_path / "repo.xml"
    output_path = tmp_path / "output.xml"

    live_path.write_text(
        """<config>
  <keep>live</keep>
  <WindowGeometry x="100" y="200" />
  <WindowState fullscreen="true" />
  <timeForNewReleaseCheck>12345</timeForNewReleaseCheck>
</config>
""",
        encoding="utf-8",
    )
    repo_path.write_text(
        """<config>
  <keep>repo</keep>
</config>
""",
        encoding="utf-8",
    )

    transform_xml(
        str(live_path),
        str(output_path),
        overlay_path=str(repo_path),
        node_matchers=[
            "config/WindowGeometry",
            "config/WindowState",
            "config/timeForNewReleaseCheck",
        ],
        selector_action=MODULE.SelectorAction.RETAIN,
    )

    root = parse_xml(output_path)

    assert root.findtext("keep") == "repo"
    assert root.find("WindowGeometry") is not None
    assert root.find("WindowGeometry").attrib == {"x": "100", "y": "200"}
    assert root.find("WindowState") is not None
    assert root.findtext("timeForNewReleaseCheck") == "12345"


def test_retain_node_matchers_in_strip_mode_keeps_only_selected_paths(tmp_path: Path) -> None:
    input_path = tmp_path / "input.xml"
    output_path = tmp_path / "output.xml"

    input_path.write_text(
        """<config>
  <keep>repo</keep>
  <WindowGeometry x="100" y="200" />
  <Section>
    <KeepNested>value</KeepNested>
    <RetainNested>selected</RetainNested>
  </Section>
</config>
""",
        encoding="utf-8",
    )

    transform_xml(
        input_path,
        output_path,
        node_matchers=["config/WindowGeometry", "config/Section/RetainNested"],
        selector_action=MODULE.SelectorAction.RETAIN,
    )

    root = parse_xml(output_path)

    assert root.find("keep") is None
    assert root.find("WindowGeometry") is not None
    assert root.find("Section") is not None
    assert root.find("Section/RetainNested") is not None
    assert root.find("Section/KeepNested") is None


def test_merge_mode_remove_preserves_unselected_live_paths_and_reapplies_repo_tree(
    tmp_path: Path,
) -> None:
    live_path = tmp_path / "live.xml"
    repo_path = tmp_path / "repo.xml"
    output_path = tmp_path / "output.xml"

    live_path.write_text(
        """<config>
  <KeepLocal>noise</KeepLocal>
  <WindowGeometry x="100" y="200" />
  <WindowState fullscreen="true" />
</config>
""",
        encoding="utf-8",
    )
    repo_path.write_text(
        """<config>
  <WindowGeometry x="10" y="10" />
</config>
""",
        encoding="utf-8",
    )

    transform_xml(
        live_path,
        output_path,
        overlay_path=repo_path,
        node_matchers=["config/WindowGeometry"],
        selector_action=MODULE.SelectorAction.REMOVE,
    )

    root = parse_xml(output_path)

    assert root.findtext("KeepLocal") == "noise"
    assert root.find("WindowGeometry").attrib == {"x": "10", "y": "10"}
    assert root.find("WindowState") is not None


def test_merge_mode_remove_reflects_nested_deletions_from_repo(tmp_path: Path) -> None:
    live_path = tmp_path / "live.xml"
    repo_path = tmp_path / "repo.xml"
    output_path = tmp_path / "output.xml"

    live_path.write_text(
        """<config>
  <KeepLocal>noise</KeepLocal>
  <Managed>
    <NestedValue>repo</NestedValue>
    <DeleteMe>stale</DeleteMe>
  </Managed>
</config>
""",
        encoding="utf-8",
    )
    repo_path.write_text(
        """<config>
  <Managed>
    <NestedValue>repo</NestedValue>
  </Managed>
</config>
""",
        encoding="utf-8",
    )

    transform_xml(
        live_path,
        output_path,
        overlay_path=repo_path,
        node_matchers=["config/Managed"],
        selector_action=MODULE.SelectorAction.REMOVE,
    )

    root = parse_xml(output_path)

    assert root.findtext("KeepLocal") == "noise"
    assert root.findtext("Managed/NestedValue") == "repo"
    assert root.find("Managed/DeleteMe") is None


def test_merge_with_compare_file_preserves_live_order_and_bytes_when_semantically_equal(
    tmp_path: Path,
) -> None:
    repo_path = tmp_path / "repo.xml"
    live_path = tmp_path / "live.xml"
    output_path = tmp_path / "output.xml"

    live_text = write_semantically_equal_overlay_xml(repo_path, live_path)

    transform_xml(
        str(live_path),
        str(output_path),
        overlay_path=str(repo_path),
        node_matchers=["config/WindowGeometry"],
        compare_path=str(live_path),
        selector_action=MODULE.SelectorAction.RETAIN,
    )

    assert output_path.read_text(encoding="utf-8") == live_text


def test_merge_without_compare_file_reserializes_semantically_equal_output(
    tmp_path: Path,
) -> None:
    repo_path = tmp_path / "repo.xml"
    live_path = tmp_path / "live.xml"
    output_path = tmp_path / "output.xml"

    live_text = write_semantically_equal_overlay_xml(repo_path, live_path)

    transform_xml(
        str(live_path),
        str(output_path),
        overlay_path=str(repo_path),
        node_matchers=["config/WindowGeometry"],
        selector_action=MODULE.SelectorAction.RETAIN,
    )

    assert output_path.read_text(encoding="utf-8") != live_text
    root = parse_xml(output_path)
    assert root.find("keep") is not None
    assert root.find("keep").attrib == {"id": "1"}
    assert root.find("WindowGeometry") is not None
    assert root.find("WindowGeometry").attrib == {"x": "100", "y": "200"}


def test_sort_children_canonicalizes_only_selected_parent_paths(tmp_path: Path) -> None:
    input_path = tmp_path / "input.xml"
    output_path = tmp_path / "output.xml"

    input_path.write_text(
        """<config>
  <mutedDictionaries>
    <mutedDictionary>b</mutedDictionary>
    <mutedDictionary>a</mutedDictionary>
    <mutedDictionary>c</mutedDictionary>
  </mutedDictionaries>
  <programs>
    <program name="b" />
    <program name="a" />
  </programs>
</config>
""",
        encoding="utf-8",
    )

    transform_xml(
        input_path,
        output_path,
        node_matchers=["config/WindowGeometry"],
        selector_action=MODULE.SelectorAction.REMOVE,
        child_sort_parent_matchers=["config/mutedDictionaries"],
    )

    root = parse_xml(output_path)

    assert [
        child.text for child in root.findall("mutedDictionaries/mutedDictionary")
    ] == ["a", "b", "c"]
    assert [
        child.attrib["name"] for child in root.findall("programs/program")
    ] == ["b", "a"]


def test_cleanup_with_compare_file_treats_selected_child_lists_as_semantically_equal(
    tmp_path: Path,
) -> None:
    repo_path = tmp_path / "repo.xml"
    live_path = tmp_path / "live.xml"
    output_path = tmp_path / "output.xml"

    repo_text = """<?xml version="1.0"?>
<config>
  <mutedDictionaries>
    <mutedDictionary>a</mutedDictionary>
    <mutedDictionary>b</mutedDictionary>
  </mutedDictionaries>
</config>"""
    repo_path.write_text(repo_text, encoding="utf-8")
    live_path.write_text(
        """<?xml version="1.0"?>
<config>
  <mutedDictionaries>
    <mutedDictionary>b</mutedDictionary>
    <mutedDictionary>a</mutedDictionary>
  </mutedDictionaries>
</config>""",
        encoding="utf-8",
    )

    transform_xml(
        live_path,
        output_path,
        node_matchers=["config/WindowGeometry"],
        compare_path=repo_path,
        child_sort_parent_matchers=["config/mutedDictionaries"],
        selector_action=MODULE.SelectorAction.REMOVE,
    )

    assert output_path.read_text(encoding="utf-8") == repo_text


def test_merge_keeps_retained_live_child_under_its_identified_sibling(tmp_path: Path) -> None:
    live_path = tmp_path / "live.xml"
    repo_path = tmp_path / "repo.xml"
    output_path = tmp_path / "output.xml"
    live_path.write_text(
        '<config><item id="a"><x>1</x></item><item id="b"><keep>live-b</keep></item></config>',
        encoding="utf-8",
    )
    repo_path.write_text('<config><item id="a"><x>1</x></item><item id="b"/></config>', encoding="utf-8")

    transform_xml(
        live_path,
        output_path,
        overlay_path=repo_path,
        node_matchers=["config/item/keep"],
        selector_action=MODULE.SelectorAction.RETAIN,
    )

    root = parse_xml(output_path)
    assert root.find("item[@id='a']/keep") is None
    assert root.findtext("item[@id='b']/keep") == "live-b"


def test_merge_does_not_fuse_siblings_with_different_identities(tmp_path: Path) -> None:
    live_path = tmp_path / "live.xml"
    repo_path = tmp_path / "repo.xml"
    output_path = tmp_path / "output.xml"
    live_path.write_text('<config><item id="a"><x>live-a</x></item><other/></config>', encoding="utf-8")
    repo_path.write_text('<config><item id="b"><y>repo-b</y></item></config>', encoding="utf-8")

    transform_xml(
        live_path,
        output_path,
        overlay_path=repo_path,
        node_matchers=["config/other"],
        selector_action=MODULE.SelectorAction.REMOVE,
    )

    root = parse_xml(output_path)
    assert [
        (item.get("id"), [(child.tag, child.text) for child in item]) for item in root.findall("item")
    ] == [("a", [("x", "live-a")]), ("b", [("y", "repo-b")])]



@pytest.mark.parametrize(
    ("live_items", "repo_items", "expected_items"),
    [
        # The live copy carries an extra identity attribute the repo lacks.
        (
            '<item name="x" id="7"><keep>live</keep></item>',
            '<item name="x"><v>repo</v></item>',
            [({"name": "x"}, [("keep", "live"), ("v", "repo")])],
        ),
        # A sibling identified only by unrelated attributes is not paired.
        (
            '<item id="1"/><item name="a" id="2"><keep>two</keep></item>',
            '<item name="a"><v>repo</v></item>',
            [({"name": "a"}, [("keep", "two"), ("v", "repo")])],
        ),
    ],
)
def test_merge_pairs_siblings_whose_shared_identity_attributes_agree(
    tmp_path: Path, live_items: str, repo_items: str, expected_items: list
) -> None:
    live_path = tmp_path / "live.xml"
    repo_path = tmp_path / "repo.xml"
    output_path = tmp_path / "output.xml"
    live_path.write_text(f"<config>{live_items}</config>", encoding="utf-8")
    repo_path.write_text(f"<config>{repo_items}</config>", encoding="utf-8")

    transform_xml(
        live_path,
        output_path,
        overlay_path=repo_path,
        node_matchers=["config/item/keep"],
        selector_action=MODULE.SelectorAction.RETAIN,
    )

    root = parse_xml(output_path)
    assert [
        (item.attrib, [(child.tag, child.text) for child in item]) for item in root.findall("item")
    ] == expected_items

def test_pretty_output_preserves_blank_lines_inside_text(tmp_path: Path) -> None:
    input_path = tmp_path / "input.xml"
    output_path = tmp_path / "output.xml"
    input_path.write_text("<doc><script>line1\n\n    \nline4</script><drop/></doc>", encoding="utf-8")

    transform_xml(input_path, output_path, node_matchers=["doc/drop"], selector_action=MODULE.SelectorAction.REMOVE)

    assert parse_xml(output_path).findtext("script") == "line1\n\n    \nline4"


def test_pretty_output_preserves_mixed_content(tmp_path: Path) -> None:
    input_path = tmp_path / "input.xml"
    output_path = tmp_path / "output.xml"
    input_path.write_text(
        "<doc><p>Hello <b>world</b> again</p><p><b>lead</b> tail</p><p>head <i>end</i></p></doc>",
        encoding="utf-8",
    )

    transform_xml(input_path, output_path, node_matchers=["doc/drop"], selector_action=MODULE.SelectorAction.REMOVE)

    assert [ET.tostring(p, encoding="unicode").strip() for p in parse_xml(output_path)] == [
        "<p>Hello <b>world</b> again</p>",
        "<p><b>lead</b> tail</p>",
        "<p>head <i>end</i></p>",
    ]


def test_pretty_output_indents_element_only_content(tmp_path: Path) -> None:
    input_path = tmp_path / "input.xml"
    output_path = tmp_path / "output.xml"
    input_path.write_text(
        '<config>\n    <group name="g">   <item>1</item><empty/></group>\n<p>Hi <b>x</b></p></config>',
        encoding="utf-8",
    )

    transform_xml(input_path, output_path, node_matchers=["config/drop"], selector_action=MODULE.SelectorAction.REMOVE)

    assert output_path.read_text(encoding="utf-8") == (
        '<?xml version="1.0" ?>\n'
        "<config>\n"
        '  <group name="g">\n'
        "    <item>1</item>\n"
        "    <empty/>\n"
        "  </group>\n"
        "  <p>Hi <b>x</b></p>\n"
        "</config>\n"
    )


def test_removing_an_element_keeps_the_text_that_follows_it(tmp_path: Path) -> None:
    input_path = tmp_path / "input.xml"
    output_path = tmp_path / "output.xml"
    input_path.write_text(
        "<doc><p>Hello <b>bold</b> world</p><p><b>lead</b> tail <i>kept</i></p></doc>",
        encoding="utf-8",
    )

    transform_xml(input_path, output_path, node_matchers=["doc/p/b"], selector_action=MODULE.SelectorAction.REMOVE)

    assert [ET.tostring(p, encoding="unicode").strip() for p in parse_xml(output_path)] == [
        "<p>Hello  world</p>",
        "<p> tail <i>kept</i></p>",
    ]


MALFORMED_XML = "<config><a></config>"


@pytest.mark.parametrize("operand", ["base", "overlay", "stdin"])
def test_malformed_xml_fails_cleanly_naming_its_source(operand, tmp_path, monkeypatch, capsys) -> None:

    good_path = tmp_path / "good.xml"
    good_path.write_text("<config/>", encoding="utf-8")
    bad_path = tmp_path / "bad.xml"
    bad_path.write_text(MALFORMED_XML, encoding="utf-8")
    monkeypatch.setattr("sys.stdin", io.TextIOWrapper(io.BytesIO(MALFORMED_XML.encode())))
    base, overlay, source = {
        "base": (bad_path, good_path, str(bad_path)),
        "overlay": (good_path, bad_path, str(bad_path)),
        "stdin": ("-", good_path, "stdin"),
    }[operand]

    exit_code = cli.main(
        ["transform", "xml", str(base), "-", "--mode", "merge", "--overlay-file", str(overlay), "--selectors", "config/a"]
    )

    assert exit_code == 1
    error = capsys.readouterr().err
    assert source in error
    assert "tag mismatch" in error


def test_missing_base_file_fails_cleanly(tmp_path, capsys) -> None:

    missing_path = tmp_path / "missing.xml"

    assert cli.main(["transform", "xml", str(missing_path), "-", "--mode", "cleanup", "--selectors", "config/a"]) == 2
    assert f"base XML file not found: {missing_path}" in capsys.readouterr().err


def test_cleanup_keeps_comments_instructions_doctype_and_namespace_prefixes(tmp_path: Path) -> None:
    input_path = tmp_path / "input.xml"
    output_path = tmp_path / "output.xml"
    input_path.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<!-- header -->
<!DOCTYPE config>
<?app-hint keep?>
<config xmlns:p="urn:p" xmlns:unused="urn:unused">
  <!-- window settings -->
  <p:Window p:x="1"/>
  <?inline pi?>
  <keep>v</keep>
  <!-- noisy state -->
  <drop/>
</config>
<!-- footer -->
""",
        encoding="utf-8",
    )

    transform_xml(input_path, output_path, node_matchers=["config/drop"], selector_action=MODULE.SelectorAction.REMOVE)

    assert output_path.read_text(encoding="utf-8") == (
        '<?xml version="1.0" ?>\n'
        "<!-- header -->\n"
        "<!DOCTYPE config>\n"
        "<?app-hint keep?>\n"
        '<config xmlns:p="urn:p" xmlns:unused="urn:unused">\n'
        "  <!-- window settings -->\n"
        '  <p:Window p:x="1"/>\n'
        "  <?inline pi?>\n"
        "  <keep>v</keep>\n"
        "</config>\n"
        "<!-- footer -->\n"
    )


def test_merge_places_overlay_children_at_their_overlay_position(tmp_path: Path) -> None:
    live_path = tmp_path / "live.xml"
    repo_path = tmp_path / "repo.xml"
    output_path = tmp_path / "output.xml"
    live_path.write_text(
        """<config>
  <!-- live note -->
  <a>live</a>
  <!-- local geometry -->
  <WindowGeometry x="1"/>
  <c>live</c>
</config>
""",
        encoding="utf-8",
    )
    repo_path.write_text(
        """<config>
  <!-- managed -->
  <a>repo</a>
  <b>new</b>
  <c>repo</c>
</config>
""",
        encoding="utf-8",
    )

    transform_xml(
        live_path,
        output_path,
        overlay_path=repo_path,
        node_matchers=["config/WindowGeometry"],
        selector_action=MODULE.SelectorAction.RETAIN,
    )

    assert output_path.read_text(encoding="utf-8") == (
        '<?xml version="1.0" ?>\n'
        "<config>\n"
        "  <!-- managed -->\n"
        "  <a>repo</a>\n"
        "  <!-- local geometry -->\n"
        '  <WindowGeometry x="1"/>\n'
        "  <b>new</b>\n"
        "  <c>repo</c>\n"
        "</config>\n"
    )


def test_sort_children_moves_leading_comments_with_their_elements(tmp_path: Path) -> None:
    input_path = tmp_path / "input.xml"
    output_path = tmp_path / "output.xml"
    input_path.write_text(
        "<config><list><!-- bee --><item>b</item><!-- ay --><item>a</item><!-- end --></list></config>",
        encoding="utf-8",
    )

    transform_xml(
        input_path,
        output_path,
        node_matchers=["config/drop"],
        child_sort_parent_matchers=["config/list"],
        selector_action=MODULE.SelectorAction.REMOVE,
    )

    assert output_path.read_text(encoding="utf-8") == (
        '<?xml version="1.0" ?>\n'
        "<config>\n"
        "  <list>\n"
        "    <!-- ay -->\n"
        "    <item>a</item>\n"
        "    <!-- bee -->\n"
        "    <item>b</item>\n"
        "    <!-- end -->\n"
        "  </list>\n"
        "</config>\n"
    )


def test_compare_file_differing_only_in_comments_is_not_reused(tmp_path: Path) -> None:
    input_path = tmp_path / "input.xml"
    compare_path = tmp_path / "compare.xml"
    output_path = tmp_path / "output.xml"
    input_path.write_text("<config><!-- note --><a>1</a></config>", encoding="utf-8")
    compare_path.write_text("<config><a>1</a></config>", encoding="utf-8")

    transform_xml(input_path, output_path, node_matchers=["config/drop"], compare_path=compare_path, selector_action=MODULE.SelectorAction.REMOVE)

    assert "<!-- note -->" in output_path.read_text(encoding="utf-8")


def test_external_entities_are_kept_as_references_not_resolved(tmp_path: Path) -> None:
    secret_path = tmp_path / "secret.txt"
    secret_path.write_text("SECRET", encoding="utf-8")
    input_path = tmp_path / "input.xml"
    output_path = tmp_path / "output.xml"
    input_path.write_text(
        f'<!DOCTYPE config [<!ENTITY e SYSTEM "{secret_path.as_uri()}">]>\n<config><v>&e;</v></config>',
        encoding="utf-8",
    )

    transform_xml(input_path, output_path, node_matchers=["config/drop"], selector_action=MODULE.SelectorAction.REMOVE)

    output_text = output_path.read_text(encoding="utf-8")
    assert "SECRET" not in output_text
    assert "<v>&e;</v>" in output_text


@pytest.mark.parametrize("overlay_doctype", ["", "<!DOCTYPE config>\n"], ids=["no-doctype", "doctype-without-entity"])
def test_merge_that_keeps_an_entity_the_output_doctype_lacks_is_an_error(
    overlay_doctype: str, tmp_path: Path, capsys
) -> None:

    live_path = tmp_path / "live.xml"
    repo_path = tmp_path / "repo.xml"
    output_path = tmp_path / "output.xml"
    live_path.write_text(
        '<!DOCTYPE config [<!ENTITY brand "Acme">]>\n<config><title>&brand;</title><managed/></config>\n',
        encoding="utf-8",
    )
    repo_path.write_text(f"{overlay_doctype}<config><managed/></config>\n", encoding="utf-8")

    # The output doctype comes from the overlay, so the kept &brand; would be
    # undefined there; writing it would produce XML no reader accepts.
    assert cli.main([
        "transform", "xml", str(live_path), str(output_path),
        "--mode", "merge", "--overlay-file", str(repo_path), "--selectors", "config/title",
    ]) == 1
    assert "brand" in capsys.readouterr().err
    assert not output_path.exists()


@pytest.mark.parametrize(
    ("input_text", "compare_text", "reused"),
    [
        ("<config>\n    <a>1</a>\n</config>", "<config><a>1</a></config>", True),
        ("<config>\n  <!-- c -->\n  <a/>\n</config>", "<config><!-- c --><a/></config>", True),
        ("<config><sep> </sep></config>", "<config><sep/></config>", False),
        ("<config><sep/></config>", "<config><sep>\n</sep></config>", False),
        ("<config><p>x <b>y</b> </p></config>", "<config><p>x <b>y</b></p></config>", False),
    ],
)
def test_compare_file_ignores_only_layout_between_child_elements(
    tmp_path: Path, input_text: str, compare_text: str, reused: bool
) -> None:
    input_path = tmp_path / "input.xml"
    compare_path = tmp_path / "compare.xml"
    output_path = tmp_path / "output.xml"
    input_path.write_text(input_text, encoding="utf-8")
    compare_path.write_text(compare_text, encoding="utf-8")

    transform_xml(input_path, output_path, node_matchers=["config/drop"], compare_path=compare_path, selector_action=MODULE.SelectorAction.REMOVE)

    assert (output_path.read_text(encoding="utf-8") == compare_text) is reused


@pytest.mark.parametrize(
    ("selector_action", "selectors", "expected_empty_element"),
    [
        (MODULE.SelectorAction.REMOVE, ["config/opts/noise"], "<opts/>"),
        (MODULE.SelectorAction.RETAIN, ["config/missing"], "<config/>"),
    ],
)
def test_emptied_element_drops_its_indentation(
    tmp_path: Path, selector_action: MODULE.SelectorAction, selectors: list[str], expected_empty_element: str
) -> None:
    # Leftover indentation would read as a whitespace-only value, which compare
    # treats as different from the `<opts/>` an app writes for an empty list.
    input_path = tmp_path / "input.xml"
    output_path = tmp_path / "output.xml"
    input_path.write_text("<config>\n  <opts>\n    <noise/>\n  </opts>\n</config>\n", encoding="utf-8")

    transform_xml(input_path, output_path, node_matchers=selectors, selector_action=selector_action)

    assert expected_empty_element in output_path.read_text(encoding="utf-8")
