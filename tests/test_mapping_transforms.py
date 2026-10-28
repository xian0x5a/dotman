"""Behavior shared by the JSON, YAML, and plist mapping transforms."""

from __future__ import annotations

import json
import math
from pathlib import Path
import plistlib

import pytest

from dotman import cli
from tests.helpers import dump_yaml, read_yaml

MAPPING_FORMATS = {
    "json": (lambda data: json.dumps(data).encode(), lambda content: json.loads(content)),
    # Keep insertion order so tests can pin key order.
    "yaml": (
        lambda data: dump_yaml(data).encode(),
        read_yaml,
    ),
    "plist": (lambda data: plistlib.dumps(data, sort_keys=False), plistlib.loads),
}


def write_mapping(path: Path, transform_format: str, data: dict) -> Path:
    serialize, _ = MAPPING_FORMATS[transform_format]
    path.write_bytes(serialize(data))
    return path


def run_transform(transform_format: str, tmp_path: Path, base: dict, *arguments: str) -> object:
    base_path = write_mapping(tmp_path / f"base.{transform_format}", transform_format, base)
    output_path = tmp_path / f"output.{transform_format}"
    assert cli.main(["transform", transform_format, str(base_path), str(output_path), *arguments]) == 0
    _, deserialize = MAPPING_FORMATS[transform_format]
    return deserialize(output_path.read_bytes())


@pytest.mark.parametrize("transform_format", MAPPING_FORMATS)
def test_retain_regex_matching_nothing_retains_nothing(transform_format, tmp_path) -> None:
    base = {"live": 1, "nested": {"value": 2}}

    assert run_transform(
        transform_format, tmp_path, base, "--mode", "cleanup", "--selectors", "re:^absent"
    ) == {}


@pytest.mark.parametrize("transform_format", MAPPING_FORMATS)
def test_merge_with_unmatched_retain_regex_keeps_only_overlay(transform_format, tmp_path) -> None:
    overlay_path = write_mapping(tmp_path / f"overlay.{transform_format}", transform_format, {"managed": 3})

    assert run_transform(
        transform_format,
        tmp_path,
        {"live": 1},
        "--mode",
        "merge",
        "--overlay-file",
        str(overlay_path),
        "--selectors",
        "re:^absent",
    ) == {"managed": 3}


@pytest.mark.parametrize("transform_format", MAPPING_FORMATS)
@pytest.mark.parametrize("repo_settings", [{"theme": "light"}, {}])
def test_merge_remove_keeps_live_keys_of_a_mapping_no_selector_reaches(
    transform_format, repo_settings, tmp_path
) -> None:
    # Live `settings` holds only the key the regex excludes, so no selector
    # reaches `settings`. The merge must still descend into it rather than let
    # the repo's copy replace the live-only `windowBounds`.
    overlay_path = write_mapping(
        tmp_path / f"overlay.{transform_format}", transform_format, {"settings": repo_settings}
    )

    assert run_transform(
        transform_format,
        tmp_path,
        {"settings": {"windowBounds": [1, 2]}, "state": 1},
        "--mode",
        "merge",
        "--overlay-file",
        str(overlay_path),
        "--selector-type",
        "remove",
        "--selectors",
        r"re:^settings\.(?!windowBounds$)[^.]+$",
    ) == {"settings": {"windowBounds": [1, 2], **repo_settings}, "state": 1}


@pytest.mark.parametrize("transform_format", MAPPING_FORMATS)
def test_not_selector_keeps_a_key_at_any_depth_out_of_the_synced_region(transform_format, tmp_path) -> None:
    # A regex cannot do this alone: selecting `settings.a` takes `a.cache` too.
    selectors = ("--selectors", "settings", r"not:re:(^|\.)cache$")
    live = {"state": 1, "settings": {"theme": "dark", "cache": "L0", "a": {"x": 1, "cache": "L1"}}}

    captured = run_transform(transform_format, tmp_path, live, "--mode", "cleanup", *selectors)
    assert captured == {"settings": {"theme": "dark", "a": {"x": 1}}}

    repo = {"settings": {"theme": "light", "a": {"x": 9}}}
    overlay_path = write_mapping(tmp_path / f"repo.{transform_format}", transform_format, repo)
    rendered = run_transform(
        transform_format,
        tmp_path,
        live,
        "--mode", "merge", "--overlay-file", str(overlay_path), "--selector-type", "remove", *selectors,
    )
    assert rendered == {
        "state": 1,
        "settings": {"theme": "light", "cache": "L0", "a": {"x": 9, "cache": "L1"}},
    }


@pytest.mark.parametrize("transform_format", MAPPING_FORMATS)
@pytest.mark.parametrize(
    "selectors",
    [("settings.a.x", "not:settings.a"), ("not:settings.a", "settings.a.x")],
)
def test_exclusion_wins_over_an_include_inside_it(transform_format, selectors, tmp_path) -> None:
    live = {"settings": {"a": {"x": 1}, "b": 2}}

    assert run_transform(
        transform_format, tmp_path, live, "--mode", "cleanup", "--selectors", *selectors
    ) == {}


@pytest.mark.parametrize("transform_format", MAPPING_FORMATS)
def test_only_not_selectors_select_the_rest_of_the_document(transform_format, tmp_path) -> None:
    live = {"state": 1, "settings": {"theme": "dark", "cache": "L0"}}

    assert run_transform(
        transform_format, tmp_path, live, "--mode", "cleanup", "--selectors", "not:settings.cache"
    ) == {"state": 1, "settings": {"theme": "dark"}}


@pytest.mark.parametrize("transform_format", MAPPING_FORMATS)
def test_quoted_empty_segment_selects_the_empty_key(transform_format, tmp_path) -> None:
    base = {"a": {"": "empty", "b": "kept"}, "": "top"}

    assert run_transform(
        transform_format,
        tmp_path,
        base,
        "--mode",
        "cleanup",
        "--selector-type",
        "remove",
        "--selectors",
        'a.""',
    ) == {"a": {"b": "kept"}, "": "top"}


@pytest.mark.parametrize("transform_format", MAPPING_FORMATS)
@pytest.mark.parametrize(
    ("selector", "expected"),
    [
        ("a", {"a": {"x": {"p": 1}, "k": 1}, "b": 2}),
        ("re:^a$", {"a": {"x": {"p": 1}, "k": 1}, "b": 2}),
        ("a.x", {"a": {"x": {"p": 1}, "k": 5}, "b": 2}),
        (r"re:^a\.x$", {"a": {"x": {"p": 1}, "k": 5}, "b": 2}),
    ],
)
def test_render_restores_captured_live_values_for_path_and_regex_selectors(
    transform_format, selector, expected, tmp_path
) -> None:
    # The engines keep separate selection code; the same selection spelled as a
    # path or a regex must round-trip alike in each of them.
    live = {"a": {"x": {"p": 1}, "k": 1}, "b": 1}
    repo = run_transform(
        transform_format,
        tmp_path,
        live,
        "--mode", "cleanup", "--selector-type", "remove", "--selectors", selector,
    )
    repo["b"] = 2
    if "a" in repo:
        repo["a"]["k"] = 5
    overlay_path = write_mapping(tmp_path / f"repo.{transform_format}", transform_format, repo)

    rendered = run_transform(
        transform_format,
        tmp_path,
        live,
        "--mode", "merge", "--overlay-file", str(overlay_path), "--selectors", selector,
    )

    assert rendered == expected


@pytest.mark.parametrize("transform_format", MAPPING_FORMATS)
@pytest.mark.parametrize(
    ("result_value", "stale_compare_value"),
    [(True, 1), (0.0, -0.0), (1, 1.0)],
)
def test_compare_file_is_not_reused_for_a_value_of_another_type(
    transform_format, result_value, stale_compare_value, tmp_path
) -> None:
    compare_path = write_mapping(
        tmp_path / f"compare.{transform_format}", transform_format, {"value": stale_compare_value}
    )

    output = run_transform(
        transform_format,
        tmp_path,
        {"value": result_value},
        "--mode",
        "cleanup",
        "--compare-file",
        str(compare_path),
    )

    output_value = output["value"]
    assert type(output_value) is type(result_value)
    if isinstance(result_value, float):
        assert math.copysign(1, output_value) == math.copysign(1, result_value)


@pytest.mark.parametrize("transform_format", MAPPING_FORMATS)
@pytest.mark.parametrize("selector", ["a..b", "a.", ".a"])
def test_empty_unquoted_selector_segment_is_an_error(
    transform_format, selector, tmp_path, capsys
) -> None:
    base_path = write_mapping(tmp_path / f"base.{transform_format}", transform_format, {"a": {"b": 1}})

    # Selector mistakes are usage errors, reported through argparse.
    with pytest.raises(SystemExit) as exit_info:
        cli.main([
            "transform", transform_format, str(base_path), "--stdout",
            "--mode", "cleanup", "--selectors", selector,
        ])
    assert exit_info.value.code == 2
    assert "empty segment" in capsys.readouterr().err


@pytest.mark.parametrize("transform_format", MAPPING_FORMATS)
def test_capture_reproduces_a_repo_that_deleted_an_entry_holding_live_only_keys(
    transform_format, tmp_path
) -> None:
    # Issue #99: the repo deleted entry `b`, whose `hash` is live-only.
    selectors = ("--selectors", r"re:^skills\..+\.hash$")
    live = {"skills": {"a": {"source": "x", "hash": "1"}, "b": {"source": "y", "hash": "2"}}}
    repo = {"skills": {"a": {"source": "x"}}}
    overlay_path = write_mapping(tmp_path / f"repo.{transform_format}", transform_format, repo)

    rendered = run_transform(
        transform_format,
        tmp_path,
        live,
        "--mode", "merge", "--overlay-file", str(overlay_path), "--selector-type", "retain", *selectors,
    )
    # A live-only key survives Render even where the repo lacks its parent.
    assert rendered == {"skills": {"a": {"source": "x", "hash": "1"}, "b": {"hash": "2"}}}

    captured = run_transform(
        transform_format,
        tmp_path,
        rendered,
        "--mode", "cleanup", "--selector-type", "remove", *selectors,
    )
    assert captured == repo


@pytest.mark.parametrize("transform_format", MAPPING_FORMATS)
def test_remove_drops_only_mappings_the_removal_empties(transform_format, tmp_path) -> None:
    base = {"empty": {}, "emptied": {"nested": {"cache": 1}}, "kept": {"cache": 1, "x": 2}}

    assert run_transform(
        transform_format,
        tmp_path,
        base,
        "--mode", "cleanup", "--selector-type", "remove", "--selectors", r"re:(^|\.)cache$",
    ) == {"empty": {}, "kept": {"x": 2}}


@pytest.mark.parametrize("transform_format", MAPPING_FORMATS)
def test_merge_remove_keeps_live_key_order_in_a_mapping_the_removal_empties(
    transform_format, tmp_path
) -> None:
    # Render keeps live order, so a repo copy with its keys in another order
    # does not also move them in the live file.
    overlay_path = write_mapping(
        tmp_path / f"overlay.{transform_format}", transform_format, {"a": {"y": 20, "x": 10}}
    )

    rendered = run_transform(
        transform_format,
        tmp_path,
        {"c": 1, "a": {"x": 1, "y": 2}},
        "--mode", "merge", "--overlay-file", str(overlay_path),
        "--selector-type", "remove", "--selectors", "a.x", "a.y",
    )

    assert list(rendered["a"].items()) == [("x", 10), ("y", 20)]


# plist output sorts keys, so only JSON and YAML have a key order to keep.
@pytest.mark.parametrize("transform_format", ["json", "yaml"])
def test_merge_retain_keeps_live_key_order_in_a_mapping_no_selector_reaches(
    transform_format, tmp_path
) -> None:
    # Retain keeps nothing of `a`, yet Render still merges it key by key, so the
    # repo's key order does not move keys in the live file.
    overlay_path = write_mapping(
        tmp_path / f"overlay.{transform_format}", transform_format, {"a": {"y": 20, "x": 10}}
    )

    rendered = run_transform(
        transform_format,
        tmp_path,
        {"live": 1, "a": {"x": 1, "y": 2}},
        "--mode", "merge", "--overlay-file", str(overlay_path),
        "--selector-type", "retain", "--selectors", "live",
    )

    assert list(rendered["a"].items()) == [("x", 10), ("y", 20)]


@pytest.mark.parametrize("transform_format", MAPPING_FORMATS)
def test_merge_remove_drops_a_mapping_the_removal_empties_and_the_repo_deleted(
    transform_format, tmp_path
) -> None:
    # The repo deleted `a`, whose keys are all synced, so Render leaves no `a: {}`.
    overlay_path = write_mapping(tmp_path / f"overlay.{transform_format}", transform_format, {"b": 3})

    assert run_transform(
        transform_format,
        tmp_path,
        {"a": {"x": 1}, "b": 2, "empty": {}},
        "--mode", "merge", "--overlay-file", str(overlay_path),
        "--selector-type", "remove", "--selectors", "a.x", "b",
    ) == {"b": 3, "empty": {}}


# YAML mapping keys need not be strings. Selectors match them by their YAML
# spelling, so `true` selects a boolean key and `1` an integer key.
YAML_NON_STRING_KEYS = pytest.mark.parametrize(("key", "key_text"), [(True, "true"), (1, "1")])


@YAML_NON_STRING_KEYS
@pytest.mark.parametrize(
    ("selector_action", "expected"),
    [
        ("retain", lambda key: {"outer": {key: 1}, key: {"a": 1}}),
        ("remove", lambda key: {"outer": {"x": 2}, "other": 3}),
    ],
)
def test_yaml_selectors_match_non_string_keys_by_their_yaml_spelling(
    key, key_text, selector_action, expected, tmp_path
) -> None:
    base = {"outer": {key: 1, "x": 2}, key: {"a": 1}, "other": 3}

    assert run_transform(
        "yaml",
        tmp_path,
        base,
        "--mode", "cleanup", "--selector-type", selector_action,
        "--selectors", key_text, f"outer.{key_text}",
    ) == expected(key)


@YAML_NON_STRING_KEYS
def test_yaml_merge_replaces_a_wholly_selected_non_string_key(key, key_text, tmp_path) -> None:
    # Selecting the key whole makes its mapping one value, so the repo's copy
    # replaces the live one instead of merging into it key by key.
    overlay_path = write_mapping(tmp_path / "overlay.yaml", "yaml", {key: {"a": 2}, "other": 2})

    assert run_transform(
        "yaml",
        tmp_path,
        {key: {"live": 1, "a": 1}, "other": 1},
        "--mode", "merge", "--overlay-file", str(overlay_path),
        "--selector-type", "retain", "--selectors", key_text,
    ) == {key: {"a": 2}, "other": 2}
