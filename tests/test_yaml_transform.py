from __future__ import annotations

from pathlib import Path

import pytest

from dotman import cli
import dotman.transforms.yaml as MODULE
from tests.helpers import read_yaml


def load_yaml(path: Path) -> dict:
    return read_yaml(path.read_text(encoding="utf-8"))


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

    exit_code = cli.main(
        ["transform", "yaml", str(input_path), str(output_path), "--mode", "cleanup"]
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


def test_flow_collections_keep_their_style(tmp_path: Path) -> None:
    source = "alpha: 1\nbeta: {nested: true}\nlist: [1, 2]\n"

    assert run_yaml_cleanup(tmp_path, source) == source


def test_serialized_yaml_preserves_compare_file_indent(tmp_path: Path) -> None:
    input_path = tmp_path / "input.yaml"
    compare_path = tmp_path / "compare.yaml"
    output_path = tmp_path / "output.yaml"

    input_path.write_text("settings:\n  alpha: 1\n  beta: true\n", encoding="utf-8")
    compare_path.write_text(
        "settings:\n"
        "    alpha: 1\n"
        "    beta: false\n",
        encoding="utf-8",
    )

    exit_code = cli.main(
        [
            "transform",
            "yaml",
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

    exit_code = cli.main(
        [
            "transform",
            "yaml",
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

    exit_code = cli.main(
        [
            "transform",
            "yaml",
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

    exit_code = cli.main(
        [
            "transform",
            "yaml",
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

    exit_code = cli.main(
        [
            "transform",
            "yaml",
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

    exit_code = cli.main(
        [
            "transform",
            "yaml",
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

    exit_code = cli.main(
        [
            "transform",
            "yaml",
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

    exit_code = cli.main(
        [
            "transform",
            "yaml",
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

    exit_code = cli.main(
        [
            "transform",
            "yaml",
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

    exit_code = cli.main(
        [
            "transform",
            "yaml",
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

    exit_code = cli.main(
        [
            "transform",
            "yaml",
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

    exit_code = cli.main(
        [
            "transform",
            "yaml",
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

    exit_code = cli.main(
        [
            "transform",
            "yaml",
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

    exit_code = cli.main(
        [
            "transform",
            "yaml",
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

    exit_code = cli.main(
        [
            "transform",
            "yaml",
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

    exit_code = cli.main(
        [
            "transform",
            "yaml",
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

    exit_code = cli.main(
        [
            "transform",
            "yaml",
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

    exit_code = cli.main(
        [
            "transform",
            "yaml",
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

    exit_code = cli.main(
        [
            "transform",
            "yaml",
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

    exit_code = cli.main(
        [
            "transform",
            "yaml",
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

    exit_code = cli.main(
        [
            "transform",
            "yaml",
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

    assert cli.main(["transform", "yaml", str(input_path), str(output_path), "--mode", "cleanup", *arguments]) == 0
    return output_path.read_text(encoding="utf-8")


def test_strings_that_yaml_readers_could_resolve_as_non_strings_are_quoted(tmp_path: Path) -> None:
    # 1e3, 0o17, 09, and +.5 are numbers to YAML 1.2 readers; y and N are
    # booleans to YAML 1.1 readers. They must stay strings for both.
    ambiguous_strings = ["1e3", "0o17", "09", "+.5", "0x1F", "y", "N"]
    source = "".join(f"v{index}: '{value}'\n" for index, value in enumerate(ambiguous_strings))

    output = run_yaml_cleanup(tmp_path, source)

    assert output == source


def test_plain_equals_sign_value_is_kept(tmp_path: Path) -> None:
    assert run_yaml_cleanup(tmp_path, "separator: =\n") == "separator: =\n"


def test_comment_only_base_is_an_empty_mapping(tmp_path: Path) -> None:
    input_path = tmp_path / "input.yaml"
    overlay_path = tmp_path / "overlay.yaml"
    output_path = tmp_path / "output.yaml"
    input_path.write_text("# managed: value\n", encoding="utf-8")
    overlay_path.write_text("managed: 1\n", encoding="utf-8")

    assert cli.main(
        ["transform", "yaml", str(input_path), str(output_path), "--mode", "merge", "--overlay-file", str(overlay_path)]
    ) == 0
    assert MODULE.load_yaml(output_path) == {"managed": 1}


def test_invalid_yaml_is_a_clean_cli_error(tmp_path: Path, capsys) -> None:

    # yes and "yes" are the same YAML 1.2 string, so they are duplicate keys.
    for index, source in enumerate(["a: [1\n", "a: 1\n---\nb: 2\n", "a: 1\na: 2\n", 'yes: a\n"yes": b\n']):
        input_path = tmp_path / f"input{index}.yaml"
        input_path.write_text(source, encoding="utf-8")

        assert cli.main(["transform", "yaml", str(input_path), "-", "--mode", "cleanup"]) == 1
        assert str(input_path) in capsys.readouterr().err


def test_indent_detection_ignores_block_scalar_content(tmp_path: Path) -> None:
    source = "script: |\n      echo hi\nsettings:\n  a: 1\n"

    assert run_yaml_cleanup(tmp_path, source).endswith("\nsettings:\n  a: 1\n")


def test_stdin_base_indent_is_preserved(monkeypatch, capsys) -> None:
    import io


    source = "settings:\n    a: 1\n"
    monkeypatch.setattr("sys.stdin", io.StringIO(source))

    assert cli.main(["transform", "yaml", "-", "-", "--mode", "cleanup"]) == 0
    assert capsys.readouterr().out == source


def test_plain_scalars_keep_their_spelling(tmp_path: Path) -> None:
    source = (
        "port: 22:22\n"
        "mode: 0777\n"
        "count: 1_000\n"
        "bits: 0b101\n"
        "hex: 0x1F\n"
        "negative_hex: -0x1F\n"
        "scale: 1e3\n"
        "octal: 0o17\n"
        "leading: 09\n"
        "enabled: yes\n"
        "short: y\n"
        "on: off\n"
        "ref: !Ref foo\n"
        "pair: !GetAtt [a, b]\n"
    )

    assert run_yaml_cleanup(tmp_path, source) == source


def test_plain_scalars_resolve_by_yaml_1_2(tmp_path: Path) -> None:
    source = "a: true\nb: 0777\nc: 1e3\nd: null\ne: 0x1F\nf: -.inf\ng: yes\nh: 22:22\n"

    assert read_yaml(run_yaml_cleanup(tmp_path, source)) == {
        "a": True, "b": 777, "c": 1000.0, "d": None, "e": 31, "f": float("-inf"), "g": "yes", "h": "22:22"
    }


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


def test_plain_number_key_and_quoted_key_of_the_same_spelling_stay_distinct(tmp_path: Path) -> None:
    source = '0777: plain\n"0777": quoted\n'

    assert run_yaml_cleanup(tmp_path, source, "--selector-type", "remove", "--selectors", "absent") == source
    # Selectors match keys by spelling, so both are selected.
    assert run_yaml_cleanup(tmp_path, source, "--selector-type", "remove", "--selectors", "0777") == "{}\n"


# --- Round-trip fidelity (comments, anchors, layout) --------------------------------------------


def run_yaml_merge(tmp_path: Path, base: str | None, overlay: str, *arguments: str) -> str:
    base_path = tmp_path / "base.yaml"
    overlay_path = tmp_path / "overlay.yaml"
    output_path = tmp_path / "merged.yaml"
    if base is not None:
        base_path.write_text(base, encoding="utf-8")
    overlay_path.write_text(overlay, encoding="utf-8")

    assert cli.main([
        "transform", "yaml", str(base_path), str(output_path),
        "--mode", "merge", "--overlay-file", str(overlay_path), *arguments,
    ]) == 0
    return output_path.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "source",
    [
        pytest.param("# c\ndefaults: &d\n  color: red  # pick\nprofile:\n  <<: *d\n", id="issue-167-repro"),
        pytest.param(
            "# header\n\na: 1  # eol\n\n# between\nb:\n  # nested above\n  c: 2  # nested eol\n  d: [1, 2]  # flow\n# trailing\n",
            id="comments-everywhere",
        ),
        pytest.param("base: &b\n  x: 1\nother: *b\nlist:\n  - &i one\n  - *i\n", id="anchors-and-aliases"),
        pytest.param("q1: 'single'\nq2: \"double\"\nplain: text\nflow: {a: 1, b: [x, y]}\n", id="quoting-and-flow"),
        pytest.param("ref: !Ref foo  # tag\nsub: !Sub '${A}-b'\nseq: !Join [',', [a, b]]\n", id="custom-tags"),
    ],
)
def test_identity_cleanup_reproduces_the_input_byte_for_byte(tmp_path: Path, source: str) -> None:
    assert run_yaml_cleanup(tmp_path, source) == source


@pytest.mark.parametrize(
    ("source", "key", "expected"),
    [
        pytest.param(
            "# header\n\n# about a\na: 1  # eol a\n# about b\nb: 2  # eol b\n# about c\nc: 3  # eol c\n",
            "a",
            # Comment lines touching a key belong to it; a blank line keeps the file header.
            "# header\n\n# about b\nb: 2  # eol b\n# about c\nc: 3  # eol c\n",
            id="first-key",
        ),
        pytest.param(
            "# about a\na: 1  # eol a\n# about b\nb: 2  # eol b\n# about c\nc: 3  # eol c\n",
            "b",
            "# about a\na: 1  # eol a\n# about c\nc: 3  # eol c\n",
            id="middle-key",
        ),
        pytest.param(
            "# about a\na: 1  # eol a\n# about b\nb: 2  # eol b\n# about c\nc: 3  # eol c\n",
            "c",
            "# about a\na: 1  # eol a\n# about b\nb: 2  # eol b\n",
            id="last-key",
        ),
        pytest.param(
            "# about a\na: 1  # eol a\n# about block\nblock:  # eol block\n  x: 1  # eol x\n  y: 2\n# about c\nc: 3  # eol c\n",
            "block",
            "# about a\na: 1  # eol a\n# about c\nc: 3  # eol c\n",
            id="nested-block-mapping-value",
        ),
        pytest.param(
            "outer:\n  # about a\n  a: 1  # eol a\n  # about b\n  b: 2  # eol b\n  # about c\n  c: 3  # eol c\n",
            "outer.b",
            "outer:\n  # about a\n  a: 1  # eol a\n  # about c\n  c: 3  # eol c\n",
            id="nested-key",
        ),
        pytest.param(
            "# about a\na: 1  # eol a\n# about seq\nseq:  # eol seq\n  - x  # eol x\n  - y\n# about c\nc: 3  # eol c\n",
            "seq",
            "# about a\na: 1  # eol a\n# about c\nc: 3  # eol c\n",
            id="block-sequence-value",
        ),
    ],
)
def test_removing_a_key_takes_its_comments_and_spares_its_neighbours(
    tmp_path: Path, source: str, key: str, expected: str
) -> None:
    assert run_yaml_cleanup(tmp_path, source, "--selector-type", "remove", "--selectors", key) == expected


def test_removing_a_key_only_a_merge_supplies_flattens_the_mapping(tmp_path: Path) -> None:
    source = "defaults: &d\n  color: red\n  size: 1\nprofile:\n  <<: *d\n  name: p\n"

    output = run_yaml_cleanup(tmp_path, source, "--selector-type", "remove", "--selectors", "profile.size")

    assert read_yaml(output) == {
        "defaults": {"color": "red", "size": 1},
        "profile": {"color": "red", "name": "p"},
    }
    assert "<<" not in output.split("profile:")[1]


def test_changing_an_aliased_mapping_leaves_its_other_uses_alone(tmp_path: Path) -> None:
    source = "base: &b\n  x: 1\n  y: 2\nfirst: *b\nsecond: *b\n"

    output = run_yaml_cleanup(tmp_path, source, "--selector-type", "remove", "--selectors", "first.x")

    assert read_yaml(output) == {
        "base": {"x": 1, "y": 2},
        "first": {"y": 2},
        "second": {"x": 1, "y": 2},
    }


@pytest.mark.parametrize(
    "source",
    [
        pytest.param("settings:\n    a: 1\n    b: 2\nother: 3\n", id="four-space-indent"),
        pytest.param("key:\n- a\n- b\nother: 3\n", id="indentless-sequence"),
        pytest.param("key:\n  - a\n  - b\nother: 3\n", id="indented-sequence"),
    ],
)
def test_layout_survives_an_edit_elsewhere(tmp_path: Path, source: str) -> None:
    output = run_yaml_cleanup(tmp_path, source + "extra: 1\n", "--selector-type", "remove", "--selectors", "extra")

    assert output == source


@pytest.mark.parametrize(
    ("compare", "base", "overlay", "expected_prefix"),
    [
        pytest.param("a:\n    b: 1\n", "a:\n  b: 1\n", "a:\n  b: 1\n", "a:\n    b: 2", id="compare-over-base"),
        pytest.param(None, "a:\n    b: 1\n", "a:\n  b: 1\n", "a:\n    b: 2", id="base-over-overlay"),
    ],
)
def test_indent_source_precedence(
    tmp_path: Path, compare: str | None, base: str, overlay: str, expected_prefix: str
) -> None:
    arguments: list[str] = []
    if compare is not None:
        compare_path = tmp_path / "compare.yaml"
        compare_path.write_text(compare.replace("b: 1", "b: 0"), encoding="utf-8")
        arguments = ["--compare-file", str(compare_path)]
    overlay = overlay.replace("b: 1", "b: 2")

    output = run_yaml_merge(tmp_path, base, overlay, *arguments)

    assert output.startswith(expected_prefix)


def test_indent_falls_back_to_overlay_when_base_is_missing(tmp_path: Path) -> None:
    overlay = "a:\n    b: 1\n"

    assert run_yaml_merge(tmp_path, None, overlay) == overlay


def test_merge_into_missing_base_reproduces_overlay_comments(tmp_path: Path) -> None:
    overlay = "# header\na: 1  # eol\n\n# above b\nb:\n  c: [1, 2]  # nested\n# trailing\n"

    assert run_yaml_merge(tmp_path, None, overlay) == overlay


def test_merge_keeps_output_comments_and_added_keys_bring_inner_comments(tmp_path: Path) -> None:
    base = "a: 1  # base a eol\nsettings:\n  x: 1  # base x\n"
    overlay = "a: 10  # overlay a eol\nsettings:\n  x: 2  # overlay x\nadded:\n  z: 1  # overlay z\n"

    output = run_yaml_merge(tmp_path, base, overlay)

    assert read_yaml(output) == {"a": 10, "settings": {"x": 2}, "added": {"z": 1}}
    assert "# base a eol" in output and "# overlay a eol" not in output
    assert "# base x" in output and "# overlay x" not in output
    assert "# overlay z" in output


def test_compare_file_with_equal_values_is_reused_even_with_different_comments(tmp_path: Path) -> None:
    compare_path = tmp_path / "compare.yaml"
    compare_text = "# other comment\r\nb: 2   # x\r\na: 1\r\n"
    compare_path.write_bytes(compare_text.encode())

    output_path = tmp_path / "out.yaml"
    input_path = tmp_path / "in.yaml"
    input_path.write_text("a: 1  # mine\nb: 2\n", encoding="utf-8")

    assert cli.main([
        "transform", "yaml", str(input_path), str(output_path), "--mode", "cleanup",
        "--compare-file", str(compare_path),
    ]) == 0
    assert output_path.read_bytes() == compare_text.encode()


@pytest.mark.parametrize(
    "source",
    [
        pytest.param("- a\n- b\n", id="top-level-sequence"),
        pytest.param("just a scalar\n", id="top-level-scalar"),
    ],
)
def test_top_level_non_mapping_is_a_clean_cli_error(tmp_path: Path, capsys, source: str) -> None:
    input_path = tmp_path / "input.yaml"
    input_path.write_text(source, encoding="utf-8")

    assert cli.main(["transform", "yaml", str(input_path), "-", "--mode", "cleanup"]) == 1
    assert str(input_path) in capsys.readouterr().err


# --- Round-trip regressions: anchors, merge keys, tags, comment placement ------------------------

COMPOSE_BASE = "x-common: &common\n  restart: always\n  image: app:1\nservices:\n  web:\n    <<: *common\n    ports: [80]\n"


def test_overlay_edit_of_a_merge_source_does_not_leak_into_unselected_users(tmp_path: Path) -> None:
    output = run_yaml_merge(
        tmp_path, COMPOSE_BASE, "x-common: {restart: always, image: app:2}\n",
        "--selector-type", "retain", "--selectors", "services",
    )

    data = read_yaml(output)
    assert data["services"]["web"]["image"] == "app:1"
    assert data["x-common"]["image"] == "app:2"


def test_consistent_anchor_edit_stays_in_place(tmp_path: Path) -> None:
    overlay = COMPOSE_BASE.replace("app:1", "app:2")

    assert run_yaml_merge(tmp_path, COMPOSE_BASE, overlay) == overlay


def test_deleting_a_key_from_a_merge_source_keeps_it_for_unselected_users(tmp_path: Path) -> None:
    output = run_yaml_cleanup(tmp_path, COMPOSE_BASE, "--selector-type", "remove", "--selectors", "x-common.restart")

    data = read_yaml(output)
    assert data["x-common"] == {"image": "app:1"}
    # Only the selected path loses the key; web still reads it through its merge.
    assert data["services"]["web"] == {"restart": "always", "image": "app:1", "ports": [80]}


def test_removing_an_own_key_that_overrides_a_merged_key_uncovers_nothing(tmp_path: Path) -> None:
    source = "x: &x {p: 1, q: 2}\nz:\n  <<: *x\n  p: 7\n"

    output = run_yaml_cleanup(tmp_path, source, "--selector-type", "remove", "--selectors", "z.p")

    assert read_yaml(output)["z"] == {"q": 2}


def test_overlay_anchor_names_cannot_redirect_base_aliases(tmp_path: Path) -> None:
    output = run_yaml_merge(tmp_path, "a: &x {p: 1}\nb: 0\nc: *x\n", "b: &x {q: 9}\nd: *x\n")

    data = read_yaml(output)
    assert data["c"] == {"p": 1}
    assert data["b"] == data["d"] == {"q": 9}


@pytest.mark.parametrize(
    "source",
    [
        pytest.param("m: !Foo {a: 1, b: 2}\nn: 1\n", id="tagged-mapping"),
        pytest.param("k: !!set {a, b}\n", id="set"),
        pytest.param("n: &a\n  - a\n  - b\nm: *a\n", id="anchored-block-sequence"),
    ],
)
def test_unusual_nodes_survive_identity_byte_for_byte(tmp_path: Path, source: str) -> None:
    assert run_yaml_cleanup(tmp_path, source) == source


def test_complex_key_survives_identity(tmp_path: Path) -> None:
    source = "? {a: 1}\n: v\n"

    assert run_yaml_cleanup(tmp_path, source) == source


def test_custom_tag_on_a_mapping_survives_an_edit_elsewhere(tmp_path: Path) -> None:
    output = run_yaml_cleanup(tmp_path, "m: !Foo {a: 1, b: 2}\nn: 1\n", "--selector-type", "remove", "--selectors", "n")

    assert output == "m: !Foo {a: 1, b: 2}\n"


@pytest.mark.parametrize(
    ("removed", "expected"),
    [
        pytest.param("e", "d: 1\n# before f\nf: 6\n", id="remove-block-scalar-keeps-next-comment"),
        pytest.param("f", "d: 1\n# before e\ne: |\n  literal\n", id="remove-key-after-block-scalar-takes-its-comment"),
    ],
)
def test_comments_around_a_block_scalar_follow_their_key(tmp_path: Path, removed: str, expected: str) -> None:
    source = "d: 1\n# before e\ne: |\n  literal\n# before f\nf: 6\n"

    assert run_yaml_cleanup(tmp_path, source, "--selector-type", "remove", "--selectors", removed) == expected


@pytest.mark.parametrize(
    ("base", "overlay", "expected_text"),
    [
        pytest.param(
            "e:\n  - 1\n  - 2\n# before f\nf: 6\n", "e: [1, 3]\n", "\n# before f\nf: 6\n", id="block-list-replaced"
        ),
        pytest.param(
            "e: 1  # eol e\n# before f\nf: 6\n", "e:\n  m: 1\n", "\n# before f\nf: 6\n", id="scalar-replaced-by-block-map"
        ),
    ],
)
def test_replacing_a_value_keeps_the_next_keys_comment_above_that_key(
    tmp_path: Path, base: str, overlay: str, expected_text: str
) -> None:
    output = run_yaml_merge(tmp_path, base, overlay)

    assert read_yaml(output)["f"] == 6
    assert output.endswith(expected_text)


def test_merge_of_float_and_int_spelled_keys_writes_parseable_output(tmp_path: Path) -> None:
    output = run_yaml_merge(tmp_path, "m: {1.0: a}\n", "m: {1: b}\n")

    assert isinstance(read_yaml(output), dict)


# --- Round-trip regressions: shared structure copies, comment hand-over, line endings -----------


def test_removing_a_key_through_a_merge_chain_keeps_every_user_correct(tmp_path: Path) -> None:
    source = "d: &d\n  x: 1\n  y: 2\nm: &m\n  <<: *d\n  z: 3\nm2: *m\n"

    data = read_yaml(run_yaml_cleanup(tmp_path, source, "--selector-type", "remove", "--selectors", "m.x"))

    assert data["m"] == {"y": 2, "z": 3}
    assert data["m2"] == {"x": 1, "y": 2, "z": 3}


def test_removing_keys_from_an_anchor_and_a_mapping_that_merges_it(tmp_path: Path) -> None:
    source = "base: &b {x: 1, y: 2}\nchild: {<<: *b, z: 3}\n"

    data = read_yaml(
        run_yaml_cleanup(tmp_path, source, "--selector-type", "remove", "--selectors", "base.x", "child.y")
    )

    assert data == {"base": {"y": 2}, "child": {"x": 1, "z": 3}}


def test_nested_anchors_in_a_copied_structure_are_defined_once(tmp_path: Path) -> None:
    import re

    base = "use: &b\n  inner: &i {k: 1}\n  again: *i\n  other: 2\nbase: *b\nlater: *i\n"

    output = run_yaml_merge(tmp_path, base, "base: {inner: {k: 5}}\n", "--selector-type", "remove", "--selectors", "zzz")

    data = read_yaml(output)
    # Merge descends into the shared mapping, so only `inner` changes.
    assert data["base"] == {"inner": {"k": 5}, "again": {"k": 1}, "other": 2}
    assert data["use"] == {"inner": {"k": 1}, "again": {"k": 1}, "other": 2}
    assert data["later"] == {"k": 1}
    defined = re.findall(r"&(\w+)", output)
    assert len(defined) == len(set(defined))


def test_overlay_edit_of_a_mapping_merging_an_anchor_leaves_the_anchor_alone(tmp_path: Path) -> None:
    base = "base: &b {x: 1, nested: {k: 1}}\nchild: {<<: *b, z: 3}\n"

    output = run_yaml_merge(
        tmp_path, base, "child: {nested: {k: 2}}\n", "--selector-type", "remove", "--selectors", "zzz"
    )

    assert read_yaml(output)["base"]["nested"]["k"] == 1


@pytest.mark.parametrize(
    ("base", "overlay", "following"),
    [
        pytest.param(
            "servers:\n  - {host: a, port: 1}\n# Logging\nlogging: {level: info}\n",
            "servers: [{host: c, port: 3}]\n",
            "logging",
            id="block-sequence-ending-in-a-flow-item",
        ),
        pytest.param(
            "m:\n  only: 1\n# about n\nn: 2\n",
            "m: {other: 5}\n",
            "n",
            id="appended-key-after-the-old-last-key",
        ),
    ],
)
def test_comment_after_an_edited_value_stays_above_the_next_key(
    tmp_path: Path, base: str, overlay: str, following: str
) -> None:
    output = run_yaml_merge(tmp_path, base, overlay, "--selector-type", "remove", "--selectors", "zzz")

    assert read_yaml(output)
    comment = "# Logging" if following == "logging" else "# about n"
    assert f"{comment}\n{following}:" in output


def test_crlf_document_keeps_crlf_line_endings_after_an_edit(tmp_path: Path) -> None:
    input_path = tmp_path / "input.yaml"
    output_path = tmp_path / "output.yaml"
    input_path.write_bytes(b"a: 1 # c\r\n# about b\r\nb: 2\r\nx: 3\r\n")

    assert cli.main([
        "transform", "yaml", str(input_path), str(output_path), "--mode", "cleanup",
        "--selector-type", "remove", "--selectors", "x",
    ]) == 0

    output = output_path.read_bytes()
    assert read_yaml(output) == {"a": 1, "b": 2}
    assert output.count(b"\n") == output.count(b"\r\n") > 0
