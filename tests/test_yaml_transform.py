from __future__ import annotations

from pathlib import Path

import pytest
import yaml

import dotman.transforms.yaml as MODULE


def load_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_yaml_engine_declares_typed_selectors() -> None:
    selector_specs = {spec.name: spec for spec in MODULE.YamlTransformEngine.selector_specs()}

    assert selector_specs["key"].prefix == "exact"
    assert selector_specs["key_regex"].prefix == "re"


def test_yaml_11_boolean_words_load_as_strings_and_keep_plain_spelling(tmp_path: Path) -> None:
    input_path = tmp_path / "input.yaml"
    output_path = tmp_path / "output.yaml"
    input_path.write_text(
        "reasoningEfforts:\n"
        "  off: none\n"
        "  on: max\n"
        "  enabled: true\n",
        encoding="utf-8",
    )

    exit_code = MODULE.main(
        [str(input_path), str(output_path), "--mode", "cleanup"]
    )

    assert exit_code == 0
    loaded_efforts = MODULE.load_yaml(output_path)["reasoningEfforts"]
    assert {str(key): value for key, value in loaded_efforts.items()} == {
        "off": "none", "on": "max", "enabled": True
    }
    assert output_path.read_text(encoding="utf-8") == (
        "reasoningEfforts:\n"
        "  off: none\n"
        "  on: max\n"
        "  enabled: true\n"
    )


def test_serialized_yaml_uses_two_space_fallback_indent(tmp_path: Path) -> None:
    input_path = tmp_path / "input.yaml"
    output_path = tmp_path / "output.yaml"

    input_path.write_text("alpha: 1\nbeta: {nested: true}\n", encoding="utf-8")

    exit_code = MODULE.main(
        [
            str(input_path),
            str(output_path),
            "--mode",
            "cleanup",
        ]
    )

    assert exit_code == 0
    assert output_path.read_text(encoding="utf-8") == (
        "alpha: 1\n"
        "beta:\n"
        "  nested: true\n"
    )


def test_serialized_yaml_preserves_compare_file_indent(tmp_path: Path) -> None:
    input_path = tmp_path / "input.yaml"
    compare_path = tmp_path / "compare.yaml"
    output_path = tmp_path / "output.yaml"

    input_path.write_text("settings: {alpha: 1, beta: true}\n", encoding="utf-8")
    compare_path.write_text(
        "settings:\n"
        "    alpha: 1\n"
        "    beta: false\n",
        encoding="utf-8",
    )

    exit_code = MODULE.main(
        [
            str(input_path),
            str(output_path),
            "--mode",
            "cleanup",
            "--compare-file",
            str(compare_path),
        ]
    )

    assert exit_code == 0
    assert output_path.read_text(encoding="utf-8") == (
        "settings:\n"
        "    alpha: 1\n"
        "    beta: true\n"
    )


def test_merge_with_missing_base_preserves_overlay_content(tmp_path: Path) -> None:
    live_path = tmp_path / "missing-live.yaml"
    repo_path = tmp_path / "repo.yaml"
    output_path = tmp_path / "output.yaml"

    repo_path.write_text(
        "settings:\n"
        "    nested: true\n"
        "    other: false\n",
        encoding="utf-8",
    )

    exit_code = MODULE.main(
        [
            str(live_path),
            str(output_path),
            "--mode",
            "merge",
            "--overlay-file",
            str(repo_path),
        ]
    )

    assert exit_code == 0
    assert output_path.read_text(encoding="utf-8") == repo_path.read_text(
        encoding="utf-8"
    )


def test_merge_without_selectors_replaces_unselected_mappings_wholesale(
    tmp_path: Path,
) -> None:
    live_path = tmp_path / "live.yaml"
    repo_path = tmp_path / "repo.yaml"
    output_path = tmp_path / "output.yaml"

    live_path.write_text(
        "settings:\n"
        "  local: keep\n"
        "  managed: old\n"
        "other: keep\n",
        encoding="utf-8",
    )
    repo_path.write_text(
        "settings:\n"
        "  managed: new\n",
        encoding="utf-8",
    )

    exit_code = MODULE.main(
        [
            str(live_path),
            str(output_path),
            "--mode",
            "merge",
            "--overlay-file",
            str(repo_path),
        ]
    )

    assert exit_code == 0
    assert load_yaml(output_path) == {
        "settings": {"managed": "new"},
        "other": "keep",
    }


def test_compare_file_preserves_existing_text(tmp_path: Path) -> None:
    input_path = tmp_path / "input.yaml"
    compare_path = tmp_path / "compare.yaml"
    output_path = tmp_path / "output.yaml"

    input_path.write_text("alpha: 1\nbeta: true\n", encoding="utf-8")
    compare_path.write_text("alpha: 1\nbeta: true\n", encoding="utf-8")

    exit_code = MODULE.main(
        [
            str(input_path),
            str(output_path),
            "--mode",
            "cleanup",
            "--compare-file",
            str(compare_path),
        ]
    )

    assert exit_code == 0
    assert output_path.read_text(encoding="utf-8") == compare_path.read_text(encoding="utf-8")


def test_cleanup_retain_key_path_keeps_selected_nested_object_key(tmp_path: Path) -> None:
    input_path = tmp_path / "input.yaml"
    output_path = tmp_path / "output.yaml"

    input_path.write_text(
        "settings:\n"
        "  window:\n"
        "    width: 1200\n"
        "    height: 800\n"
        "  theme: dark\n"
        "other: true\n",
        encoding="utf-8",
    )

    exit_code = MODULE.main(
        [
            str(input_path),
            str(output_path),
            "--mode",
            "cleanup",
            "--selector-type",
            "retain",
            "--selectors",
            "settings.window.width",
        ]
    )

    assert exit_code == 0
    assert load_yaml(output_path) == {"settings": {"window": {"width": 1200}}}


def test_cleanup_remove_key_path_strips_selected_nested_object_key(tmp_path: Path) -> None:
    input_path = tmp_path / "input.yaml"
    output_path = tmp_path / "output.yaml"

    input_path.write_text(
        "settings:\n"
        "  window:\n"
        "    width: 1200\n"
        "    height: 800\n"
        "  theme: dark\n"
        "other: true\n",
        encoding="utf-8",
    )

    exit_code = MODULE.main(
        [
            str(input_path),
            str(output_path),
            "--mode",
            "cleanup",
            "--selector-type",
            "remove",
            "--selectors",
            "settings.window.width",
        ]
    )

    assert exit_code == 0
    assert load_yaml(output_path) == {
        "settings": {
            "window": {"height": 800},
            "theme": "dark",
        },
        "other": True,
    }


def test_cleanup_key_path_accepts_quoted_dotted_key_parts(tmp_path: Path) -> None:
    input_path = tmp_path / "input.yaml"
    output_path = tmp_path / "output.yaml"

    input_path.write_text(
        "settings.window:\n"
        "  width: 1200\n"
        "  height: 800\n",
        encoding="utf-8",
    )

    exit_code = MODULE.main(
        [
            str(input_path),
            str(output_path),
            "--mode",
            "cleanup",
            "--selector-type",
            "retain",
            "--selectors",
            '"settings.window".width',
        ]
    )

    assert exit_code == 0
    assert load_yaml(output_path) == {"settings.window": {"width": 1200}}


def test_cleanup_remove_key_strips_selected_top_level_keys(tmp_path: Path) -> None:
    input_path = tmp_path / "input.yaml"
    output_path = tmp_path / "output.yaml"

    input_path.write_text(
        "aururl: https://aur.archlinux.org\n"
        "buildDir: /tmp/yay\n"
        "version: 12.5.7\n"
        "bottomup: true\n",
        encoding="utf-8",
    )

    exit_code = MODULE.main(
        [
            str(input_path),
            str(output_path),
            "--mode",
            "cleanup",
            "--selector-type",
            "remove",
            "--selectors",
            "buildDir",
        ]
    )

    assert exit_code == 0
    assert load_yaml(output_path) == {
        "aururl": "https://aur.archlinux.org",
        "version": "12.5.7",
        "bottomup": True,
    }


def test_cleanup_retain_key_regex_keeps_matching_nested_key_paths(tmp_path: Path) -> None:
    input_path = tmp_path / "input.yaml"
    output_path = tmp_path / "output.yaml"

    input_path.write_text(
        "settings:\n"
        "  managed: keep\n"
        "  local: drop\n"
        "other: true\n",
        encoding="utf-8",
    )

    exit_code = MODULE.main(
        [
            str(input_path),
            str(output_path),
            "--mode",
            "cleanup",
            "--selector-type",
            "retain",
            "--selectors",
            r"re:^settings\.managed$",
        ]
    )

    assert exit_code == 0
    assert load_yaml(output_path) == {"settings": {"managed": "keep"}}


def test_cleanup_remove_key_regex_strips_matching_nested_key_paths(tmp_path: Path) -> None:
    input_path = tmp_path / "input.yaml"
    output_path = tmp_path / "output.yaml"

    input_path.write_text(
        "settings:\n"
        "  managed: drop\n"
        "  local: keep\n"
        "other: true\n",
        encoding="utf-8",
    )

    exit_code = MODULE.main(
        [
            str(input_path),
            str(output_path),
            "--mode",
            "cleanup",
            "--selector-type",
            "remove",
            "--selectors",
            r"re:^settings\.managed$",
        ]
    )

    assert exit_code == 0
    assert load_yaml(output_path) == {
        "settings": {"local": "keep"},
        "other": True,
    }


def test_cleanup_remove_key_regex_strips_matching_top_level_keys(tmp_path: Path) -> None:
    input_path = tmp_path / "input.yaml"
    output_path = tmp_path / "output.yaml"

    input_path.write_text(
        "WindowGeometry: noise\n"
        "WindowState: noise\n"
        "keep: true\n",
        encoding="utf-8",
    )

    exit_code = MODULE.main(
        [
            str(input_path),
            str(output_path),
            "--mode",
            "cleanup",
            "--selector-type",
            "remove",
            "--selectors",
            r"re:^Window",
        ]
    )

    assert exit_code == 0
    assert load_yaml(output_path) == {"keep": True}


def test_merge_retain_key_preserves_selected_live_keys_and_reapplies_repo_content(
    tmp_path: Path,
) -> None:
    live_path = tmp_path / "live.yaml"
    repo_path = tmp_path / "repo.yaml"
    output_path = tmp_path / "output.yaml"

    live_path.write_text(
        "aururl: https://aur.archlinux.org\n"
        "buildDir: /home/test/.cache/yay\n"
        "version: 11.0.0\n"
        "bottomup: false\n",
        encoding="utf-8",
    )
    repo_path.write_text(
        "aururl: https://aur.archlinux.org\n"
        "version: 12.5.7\n"
        "bottomup: true\n"
        "rpc: true\n",
        encoding="utf-8",
    )

    exit_code = MODULE.main(
        [
            str(live_path),
            str(output_path),
            "--mode",
            "merge",
            "--overlay-file",
            str(repo_path),
            "--selector-type",
            "retain",
            "--selectors",
            "buildDir",
        ]
    )

    assert exit_code == 0
    assert load_yaml(output_path) == {
        "aururl": "https://aur.archlinux.org",
        "buildDir": "/home/test/.cache/yay",
        "version": "12.5.7",
        "bottomup": True,
        "rpc": True,
    }


def test_merge_remove_key_regex_preserves_unselected_nested_live_keys(tmp_path: Path) -> None:
    live_path = tmp_path / "live.yaml"
    repo_path = tmp_path / "repo.yaml"
    output_path = tmp_path / "output.yaml"

    live_path.write_text(
        "settings:\n"
        "  local: keep\n"
        "  managed: old\n"
        "  other: keep\n",
        encoding="utf-8",
    )
    repo_path.write_text(
        "settings:\n"
        "  managed: new\n",
        encoding="utf-8",
    )

    exit_code = MODULE.main(
        [
            str(live_path),
            str(output_path),
            "--mode",
            "merge",
            "--overlay-file",
            str(repo_path),
            "--selector-type",
            "remove",
            "--selectors",
            r"re:^settings\.managed$",
        ]
    )

    assert exit_code == 0
    merged_data = load_yaml(output_path)
    assert list(merged_data["settings"]) == ["local", "managed", "other"]
    assert merged_data == {
        "settings": {"local": "keep", "managed": "new", "other": "keep"}
    }


def test_merge_retain_key_regex_preserves_matching_live_keys_and_reapplies_repo_content(
    tmp_path: Path,
) -> None:
    live_path = tmp_path / "live.yaml"
    repo_path = tmp_path / "repo.yaml"
    output_path = tmp_path / "output.yaml"

    live_path.write_text(
        "WindowGeometry: live-geometry\n"
        "WindowState: live-state\n"
        "managed: stale\n",
        encoding="utf-8",
    )
    repo_path.write_text("managed: repo\n", encoding="utf-8")

    exit_code = MODULE.main(
        [
            str(live_path),
            str(output_path),
            "--mode",
            "merge",
            "--overlay-file",
            str(repo_path),
            "--selector-type",
            "retain",
            "--selectors",
            r"re:^Window",
        ]
    )

    assert exit_code == 0
    assert load_yaml(output_path) == {
        "WindowGeometry": "live-geometry",
        "WindowState": "live-state",
        "managed": "repo",
    }


def test_merge_retain_key_preserves_live_order_and_drops_deleted_repo_keys(
    tmp_path: Path,
) -> None:
    live_path = tmp_path / "live.yaml"
    repo_path = tmp_path / "repo.yaml"
    output_path = tmp_path / "output.yaml"

    live_path.write_text(
        "aururl: https://aur.archlinux.org\n"
        "aurrpcurl: https://aur.archlinux.org/rpc?\n"
        "buildDir: /home/test/.cache/yay\n"
        "editor: nano\n"
        "useask: false\n",
        encoding="utf-8",
    )
    repo_path.write_text(
        "aururl: https://aur.archlinux.org\n"
        "aurrpcurl: https://aur.archlinux.org/rpc?\n"
        "editor: ''\n"
        "useask: true\n",
        encoding="utf-8",
    )

    exit_code = MODULE.main(
        [
            str(live_path),
            str(output_path),
            "--mode",
            "merge",
            "--overlay-file",
            str(repo_path),
            "--selector-type",
            "retain",
            "--selectors",
            "buildDir",
        ]
    )

    assert exit_code == 0
    merged_data = load_yaml(output_path)
    assert list(merged_data) == ["aururl", "aurrpcurl", "buildDir", "editor", "useask"]
    assert merged_data == {
        "aururl": "https://aur.archlinux.org",
        "aurrpcurl": "https://aur.archlinux.org/rpc?",
        "buildDir": "/home/test/.cache/yay",
        "editor": "",
        "useask": True,
    }


def test_merge_remove_key_path_preserves_unselected_nested_live_keys(tmp_path: Path) -> None:
    live_path = tmp_path / "live.yaml"
    repo_path = tmp_path / "repo.yaml"
    output_path = tmp_path / "output.yaml"

    live_path.write_text(
        "settings:\n"
        "  local: keep\n"
        "  managed: old\n"
        "  other: keep\n",
        encoding="utf-8",
    )
    repo_path.write_text(
        "settings:\n"
        "  managed: new\n",
        encoding="utf-8",
    )

    exit_code = MODULE.main(
        [
            str(live_path),
            str(output_path),
            "--mode",
            "merge",
            "--overlay-file",
            str(repo_path),
            "--selector-type",
            "remove",
            "--selectors",
            "settings.managed",
        ]
    )

    assert exit_code == 0
    merged_data = load_yaml(output_path)
    assert list(merged_data["settings"]) == ["local", "managed", "other"]
    assert merged_data == {
        "settings": {"local": "keep", "managed": "new", "other": "keep"}
    }


def test_merge_remove_key_path_preserves_nested_repo_deletions(tmp_path: Path) -> None:
    live_path = tmp_path / "live.yaml"
    repo_path = tmp_path / "repo.yaml"
    output_path = tmp_path / "output.yaml"

    live_path.write_text(
        "settings:\n"
        "  local: keep\n"
        "  managed: old\n",
        encoding="utf-8",
    )
    repo_path.write_text("{}\n", encoding="utf-8")

    exit_code = MODULE.main(
        [
            str(live_path),
            str(output_path),
            "--mode",
            "merge",
            "--overlay-file",
            str(repo_path),
            "--selector-type",
            "remove",
            "--selectors",
            "settings.managed",
        ]
    )

    assert exit_code == 0
    assert load_yaml(output_path) == {"settings": {"local": "keep"}}


def test_merge_retain_key_path_preserves_selected_nested_live_keys(tmp_path: Path) -> None:
    live_path = tmp_path / "live.yaml"
    repo_path = tmp_path / "repo.yaml"
    output_path = tmp_path / "output.yaml"

    live_path.write_text(
        "settings:\n"
        "  managed: old\n"
        "  noise: keep\n",
        encoding="utf-8",
    )
    repo_path.write_text(
        "settings:\n"
        "  managed: new\n",
        encoding="utf-8",
    )

    exit_code = MODULE.main(
        [
            str(live_path),
            str(output_path),
            "--mode",
            "merge",
            "--overlay-file",
            str(repo_path),
            "--selector-type",
            "retain",
            "--selectors",
            "settings.noise",
        ]
    )

    assert exit_code == 0
    assert load_yaml(output_path) == {"settings": {"managed": "new", "noise": "keep"}}


def test_merge_top_level_object_key_still_replaces_with_overlay_value(tmp_path: Path) -> None:
    live_path = tmp_path / "live.yaml"
    repo_path = tmp_path / "repo.yaml"
    output_path = tmp_path / "output.yaml"

    live_path.write_text(
        "settings:\n"
        "  local: keep\n"
        "  managed: old\n",
        encoding="utf-8",
    )
    repo_path.write_text(
        "settings:\n"
        "  managed: new\n",
        encoding="utf-8",
    )

    exit_code = MODULE.main(
        [
            str(live_path),
            str(output_path),
            "--mode",
            "merge",
            "--overlay-file",
            str(repo_path),
            "--selector-type",
            "retain",
            "--selectors",
            "settings",
        ]
    )

    assert exit_code == 0
    assert load_yaml(output_path) == {"settings": {"managed": "new"}}


def test_merge_remove_key_preserves_unselected_live_keys(tmp_path: Path) -> None:
    live_path = tmp_path / "live.yaml"
    repo_path = tmp_path / "repo.yaml"
    output_path = tmp_path / "output.yaml"

    live_path.write_text(
        "keepLocal: noise\n"
        "buildDir: /home/test/.cache/yay\n"
        "version: 11.0.0\n",
        encoding="utf-8",
    )
    repo_path.write_text(
        "version: 12.5.7\n"
        "rpc: true\n",
        encoding="utf-8",
    )

    exit_code = MODULE.main(
        [
            str(live_path),
            str(output_path),
            "--mode",
            "merge",
            "--overlay-file",
            str(repo_path),
            "--selector-type",
            "remove",
            "--selectors",
            "buildDir",
        ]
    )

    assert exit_code == 0
    assert load_yaml(output_path) == {
        "keepLocal": "noise",
        "version": "12.5.7",
        "rpc": True,
    }


def test_cleanup_retain_key_matches_non_string_yaml_key(tmp_path: Path) -> None:
    input_path = tmp_path / "input.yaml"
    output_path = tmp_path / "output.yaml"

    input_path.write_text(
        "1: one\n"
        "keep: value\n",
        encoding="utf-8",
    )

    exit_code = MODULE.main(
        [
            str(input_path),
            str(output_path),
            "--mode",
            "cleanup",
            "--selector-type",
            "retain",
            "--selectors",
            "1",
        ]
    )

    assert exit_code == 0
    assert load_yaml(output_path) == {1: "one"}


def run_yaml_cleanup(tmp_path: Path, source: str, *arguments: str) -> str:
    input_path = tmp_path / "input.yaml"
    output_path = tmp_path / "output.yaml"
    input_path.write_text(source, encoding="utf-8")

    assert MODULE.main([str(input_path), str(output_path), "--mode", "cleanup", *arguments]) == 0
    return output_path.read_text(encoding="utf-8")


def test_strings_that_yaml_readers_could_resolve_as_non_strings_are_quoted(tmp_path: Path) -> None:
    # 1e3, 0o17, 09, and +.5 are numbers to YAML 1.2 readers; y and N are
    # booleans to YAML 1.1 readers. They must stay strings for both.
    ambiguous_strings = ["1e3", "0o17", "09", "+.5", "0x1F", "y", "N"]
    source = "".join(f"v{index}: '{value}'\n" for index, value in enumerate(ambiguous_strings))

    output = run_yaml_cleanup(tmp_path, source)

    assert output == source


def test_plain_equals_sign_value_is_a_string(tmp_path: Path) -> None:
    assert MODULE.parse_yaml_text(run_yaml_cleanup(tmp_path, "separator: =\n")) == {"separator": "="}


def test_comment_only_base_is_an_empty_mapping(tmp_path: Path) -> None:
    input_path = tmp_path / "input.yaml"
    overlay_path = tmp_path / "overlay.yaml"
    output_path = tmp_path / "output.yaml"
    input_path.write_text("# managed: value\n", encoding="utf-8")
    overlay_path.write_text("managed: 1\n", encoding="utf-8")

    assert MODULE.main(
        [str(input_path), str(output_path), "--mode", "merge", "--overlay-file", str(overlay_path)]
    ) == 0
    assert MODULE.load_yaml(output_path) == {"managed": 1}


def test_invalid_yaml_is_a_clean_cli_error(tmp_path: Path, capsys) -> None:
    from dotman import cli

    for index, source in enumerate(["a: [1\n", "a: 1\n---\nb: 2\n", "r: !Ref foo\n"]):
        input_path = tmp_path / f"input{index}.yaml"
        input_path.write_text(source, encoding="utf-8")

        assert cli.main(["transform", "yaml", str(input_path), "-", "--mode", "cleanup"]) == 1
        assert str(input_path) in capsys.readouterr().err


def test_indent_detection_ignores_block_scalar_content(tmp_path: Path) -> None:
    source = "script: |\n      echo hi\nsettings:\n  a: 1\n"

    assert run_yaml_cleanup(tmp_path, source).endswith("\nsettings:\n  a: 1\n")


def test_stdin_base_indent_is_preserved(monkeypatch, capsys) -> None:
    import io

    from dotman import cli

    source = "settings:\n    a: 1\n"
    monkeypatch.setattr("sys.stdin", io.StringIO(source))

    assert cli.main(["transform", "yaml", "-", "-", "--mode", "cleanup"]) == 0
    assert capsys.readouterr().out == source


def test_plain_scalars_yaml_11_and_12_read_differently_keep_their_spelling(tmp_path: Path) -> None:
    # YAML 1.1 reads these as sexagesimal, octal, underscored, binary, or signed
    # hex ints, booleans, or timestamps; YAML 1.2 reads them as strings or other
    # numbers (0777 is 777, 1e3 is 1000.0). Any rewrite changes one reading.
    source = (
        "port: 22:22\n"
        "mode: 0777\n"
        "count: 1_000\n"
        "bits: 0b101\n"
        "negative_hex: -0x1F\n"
        "scale: 1e3\n"
        "octal: 0o17\n"
        "leading: 09\n"
        "enabled: yes\n"
        "short: y\n"
        "when: 2001-12-14t21:59:43.10-05:00\n"
        "on: off\n"
    )

    assert run_yaml_cleanup(tmp_path, source) == source


def test_plain_scalars_both_yaml_versions_agree_on_keep_their_type(tmp_path: Path) -> None:
    source = "a: true\nb: 12\nc: 1.5\nd: null\ne: 0x1F\nf: -.inf\n"

    assert [type(value) for value in MODULE.parse_yaml_text(source).values()] == [
        bool, int, float, type(None), int, float
    ]
    assert run_yaml_cleanup(tmp_path, source) == "a: true\nb: 12\nc: 1.5\nd: null\ne: 31\nf: -.inf\n"


def test_compare_file_reuse_distinguishes_plain_and_quoted_ambiguous_scalars(tmp_path: Path) -> None:
    compare_path = tmp_path / "compare.yaml"

    compare_path.write_text("mode: 0777  # rwx\n", encoding="utf-8")
    assert run_yaml_cleanup(tmp_path, "mode: 0777\n", "--compare-file", str(compare_path)) == (
        "mode: 0777  # rwx\n"
    )

    # The quoted string is "0777" for every reader, unlike the plain scalar.
    compare_path.write_text("mode: '0777'\n", encoding="utf-8")
    assert run_yaml_cleanup(tmp_path, "mode: 0777\n", "--compare-file", str(compare_path)) == (
        "mode: 0777\n"
    )


def test_selectors_match_keys_by_their_yaml_spelling(tmp_path: Path) -> None:
    source = "true: a\nnull: b\n0777: c\n1.5: d\nyes: e\nkeep: f\n"

    assert run_yaml_cleanup(
        tmp_path,
        source,
        "--selector-type",
        "remove",
        "--selectors",
        "true",
        "null",
        "0777",
        r"re:^1\.5$",
        "yes",
    ) == "keep: f\n"


@pytest.mark.parametrize("spelling", ["0777", "yes", "22:22"])
def test_plain_ambiguous_key_and_quoted_key_of_the_same_spelling_stay_distinct(
    spelling: str, tmp_path: Path
) -> None:
    # Every YAML 1.1 reader sees two keys here (a number or bool, and a string).
    source = f'{spelling}: plain\n"{spelling}": quoted\n'

    assert yaml.safe_load(
        run_yaml_cleanup(tmp_path, source, "--selector-type", "remove", "--selectors", "absent")
    ) == yaml.safe_load(source)
    # Selectors match keys by spelling, so both are selected.
    assert run_yaml_cleanup(tmp_path, source, "--selector-type", "remove", "--selectors", spelling) == "{}\n"
