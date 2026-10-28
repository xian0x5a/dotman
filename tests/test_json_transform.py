from __future__ import annotations

import json
from pathlib import Path

import pytest

from dotman import cli
import dotman.transforms.json as MODULE


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_json_engine_declares_typed_selectors() -> None:
    selector_specs = {spec.name: spec for spec in MODULE.JsonTransformEngine.selector_specs()}

    assert selector_specs["key"].prefix == "exact"
    assert selector_specs["key_regex"].prefix == "re"


def test_serialized_json_uses_two_space_fallback_indent(tmp_path: Path) -> None:
    input_path = tmp_path / "input.json"
    output_path = tmp_path / "output.json"

    input_path.write_text('{"alpha":1,"beta":{"nested":true}}\n', encoding="utf-8")

    exit_code = cli.main(
        [
            "transform",
            "json",
            str(input_path),
            str(output_path),
            "--mode",
            "cleanup",
        ]
    )

    assert exit_code == 0
    assert output_path.read_text(encoding="utf-8") == (
        '{\n'
        '  "alpha": 1,\n'
        '  "beta": {\n'
        '    "nested": true\n'
        '  }\n'
        '}\n'
    )


def test_serialized_json_preserves_compare_file_indent(tmp_path: Path) -> None:
    input_path = tmp_path / "input.json"
    compare_path = tmp_path / "compare.json"
    output_path = tmp_path / "output.json"

    input_path.write_text('{"alpha":1,"beta":true}\n', encoding="utf-8")
    compare_path.write_text(
        '{\n    "alpha": 1,\n    "beta": false\n}\n',
        encoding="utf-8",
    )

    exit_code = cli.main(
        [
            "transform",
            "json",
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
        '{\n    "alpha": 1,\n    "beta": true\n}\n'
    )


def test_merge_with_missing_base_preserves_overlay_file_indent(tmp_path: Path) -> None:
    live_path = tmp_path / "missing-live.json"
    repo_path = tmp_path / "repo.json"
    output_path = tmp_path / "output.json"

    repo_path.write_text(
        '{\n\t"alpha": 1,\n\t"beta": {\n\t\t"nested": true\n\t}\n}\n',
        encoding="utf-8",
    )

    exit_code = cli.main(
        [
            "transform",
            "json",
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


def test_compare_file_preserves_existing_text(tmp_path: Path) -> None:
    input_path = tmp_path / "input.json"
    compare_path = tmp_path / "compare.json"
    output_path = tmp_path / "output.json"

    input_path.write_text('{"alpha":1,"beta":true}\n', encoding="utf-8")
    compare_path.write_text('{\n  "alpha": 1,\n  "beta": true\n}\n', encoding="utf-8")

    exit_code = cli.main(
        [
            "transform",
            "json",
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
    input_path = tmp_path / "input.json"
    output_path = tmp_path / "output.json"

    input_path.write_text(
        json.dumps(
            {
                "settings": {
                    "window": {"width": 1200, "height": 800},
                    "theme": "dark",
                },
                "other": True,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    exit_code = cli.main(
        [
            "transform",
            "json",
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
    assert load_json(output_path) == {"settings": {"window": {"width": 1200}}}



def test_cleanup_remove_key_path_strips_selected_nested_object_key(tmp_path: Path) -> None:
    input_path = tmp_path / "input.json"
    output_path = tmp_path / "output.json"

    input_path.write_text(
        json.dumps(
            {
                "settings": {
                    "window": {"width": 1200, "height": 800},
                    "theme": "dark",
                },
                "other": True,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    exit_code = cli.main(
        [
            "transform",
            "json",
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
    assert load_json(output_path) == {
        "settings": {
            "window": {"height": 800},
            "theme": "dark",
        },
        "other": True,
    }



def test_cleanup_key_path_accepts_quoted_dotted_key_parts(tmp_path: Path) -> None:
    input_path = tmp_path / "input.json"
    output_path = tmp_path / "output.json"

    input_path.write_text(
        json.dumps({"settings.window": {"width": 1200, "height": 800}}, indent=2)
        + "\n",
        encoding="utf-8",
    )

    exit_code = cli.main(
        [
            "transform",
            "json",
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
    assert load_json(output_path) == {"settings.window": {"width": 1200}}



def test_cleanup_remove_key_strips_selected_top_level_keys(tmp_path: Path) -> None:
    input_path = tmp_path / "input.json"
    output_path = tmp_path / "output.json"

    input_path.write_text(
        json.dumps(
            {
                "aururl": "https://aur.archlinux.org",
                "buildDir": "/tmp/yay",
                "version": "12.5.7",
                "bottomup": True,
            },
            indent="\t",
        )
        + "\n",
        encoding="utf-8",
    )

    exit_code = cli.main(
        [
            "transform",
            "json",
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
    assert load_json(output_path) == {
        "aururl": "https://aur.archlinux.org",
        "version": "12.5.7",
        "bottomup": True,
    }


def test_cleanup_retain_key_regex_keeps_matching_nested_key_paths(tmp_path: Path) -> None:
    input_path = tmp_path / "input.json"
    output_path = tmp_path / "output.json"

    input_path.write_text(
        json.dumps(
            {
                "settings": {"managed": "keep", "local": "drop"},
                "other": True,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    exit_code = cli.main(
        [
            "transform",
            "json",
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
    assert load_json(output_path) == {"settings": {"managed": "keep"}}



def test_cleanup_remove_key_regex_strips_matching_nested_key_paths(tmp_path: Path) -> None:
    input_path = tmp_path / "input.json"
    output_path = tmp_path / "output.json"

    input_path.write_text(
        json.dumps(
            {
                "settings": {"managed": "drop", "local": "keep"},
                "other": True,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    exit_code = cli.main(
        [
            "transform",
            "json",
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
    assert load_json(output_path) == {
        "settings": {"local": "keep"},
        "other": True,
    }



def test_cleanup_remove_key_regex_strips_matching_top_level_keys(tmp_path: Path) -> None:
    input_path = tmp_path / "input.json"
    output_path = tmp_path / "output.json"

    input_path.write_text(
        json.dumps(
            {
                "WindowGeometry": "noise",
                "WindowState": "noise",
                "keep": True,
            },
            indent="\t",
        )
        + "\n",
        encoding="utf-8",
    )

    exit_code = cli.main(
        [
            "transform",
            "json",
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
    assert load_json(output_path) == {"keep": True}


def test_merge_retain_key_preserves_selected_live_keys_and_reapplies_repo_content(
    tmp_path: Path,
) -> None:
    live_path = tmp_path / "live.json"
    repo_path = tmp_path / "repo.json"
    output_path = tmp_path / "output.json"

    live_path.write_text(
        json.dumps(
            {
                "aururl": "https://aur.archlinux.org",
                "buildDir": "/home/test/.cache/yay",
                "version": "11.0.0",
                "bottomup": False,
            },
            indent="\t",
        )
        + "\n",
        encoding="utf-8",
    )
    repo_path.write_text(
        json.dumps(
            {
                "aururl": "https://aur.archlinux.org",
                "version": "12.5.7",
                "bottomup": True,
                "rpc": True,
            },
            indent="\t",
        )
        + "\n",
        encoding="utf-8",
    )

    exit_code = cli.main(
        [
            "transform",
            "json",
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
    assert load_json(output_path) == {
        "aururl": "https://aur.archlinux.org",
        "buildDir": "/home/test/.cache/yay",
        "version": "12.5.7",
        "bottomup": True,
        "rpc": True,
    }


def test_merge_remove_key_regex_preserves_unselected_nested_live_keys(tmp_path: Path) -> None:
    live_path = tmp_path / "live.json"
    repo_path = tmp_path / "repo.json"
    output_path = tmp_path / "output.json"

    live_path.write_text(
        json.dumps(
            {"settings": {"local": "keep", "managed": "old", "other": "keep"}},
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    repo_path.write_text(
        json.dumps({"settings": {"managed": "new"}}, indent=2) + "\n",
        encoding="utf-8",
    )

    exit_code = cli.main(
        [
            "transform",
            "json",
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
    merged_data = load_json(output_path)
    assert list(merged_data["settings"]) == ["local", "managed", "other"]
    assert merged_data == {
        "settings": {"local": "keep", "managed": "new", "other": "keep"}
    }



def test_merge_retain_key_regex_preserves_matching_live_keys_and_reapplies_repo_content(
    tmp_path: Path,
) -> None:
    live_path = tmp_path / "live.json"
    repo_path = tmp_path / "repo.json"
    output_path = tmp_path / "output.json"

    live_path.write_text(
        json.dumps(
            {
                "WindowGeometry": "live-geometry",
                "WindowState": "live-state",
                "managed": "stale",
            },
            indent="\t",
        )
        + "\n",
        encoding="utf-8",
    )
    repo_path.write_text(
        json.dumps({"managed": "repo"}, indent="\t") + "\n",
        encoding="utf-8",
    )

    exit_code = cli.main(
        [
            "transform",
            "json",
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
    assert load_json(output_path) == {
        "WindowGeometry": "live-geometry",
        "WindowState": "live-state",
        "managed": "repo",
    }


def test_merge_retain_key_preserves_live_order_and_drops_deleted_repo_keys(
    tmp_path: Path,
) -> None:
    live_path = tmp_path / "live.json"
    repo_path = tmp_path / "repo.json"
    output_path = tmp_path / "output.json"

    live_path.write_text(
        json.dumps(
            {
                "aururl": "https://aur.archlinux.org",
                "aurrpcurl": "https://aur.archlinux.org/rpc?",
                "buildDir": "/home/test/.cache/yay",
                "editor": "nano",
                "useask": False,
            },
            indent="\t",
        )
        + "\n",
        encoding="utf-8",
    )
    repo_path.write_text(
        json.dumps(
            {
                "aururl": "https://aur.archlinux.org",
                "aurrpcurl": "https://aur.archlinux.org/rpc?",
                "editor": "",
                "useask": True,
            },
            indent="\t",
        )
        + "\n",
        encoding="utf-8",
    )

    exit_code = cli.main(
        [
            "transform",
            "json",
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
    merged_data = load_json(output_path)
    assert list(merged_data) == ["aururl", "aurrpcurl", "buildDir", "editor", "useask"]
    assert merged_data == {
        "aururl": "https://aur.archlinux.org",
        "aurrpcurl": "https://aur.archlinux.org/rpc?",
        "buildDir": "/home/test/.cache/yay",
        "editor": "",
        "useask": True,
    }


def test_merge_remove_key_path_preserves_unselected_nested_live_keys(tmp_path: Path) -> None:
    live_path = tmp_path / "live.json"
    repo_path = tmp_path / "repo.json"
    output_path = tmp_path / "output.json"

    live_path.write_text(
        json.dumps(
            {
                "settings": {
                    "local": "keep",
                    "managed": "old",
                    "other": "keep",
                }
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    repo_path.write_text(
        json.dumps({"settings": {"managed": "new"}}, indent=2) + "\n",
        encoding="utf-8",
    )

    exit_code = cli.main(
        [
            "transform",
            "json",
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
    merged_data = load_json(output_path)
    assert list(merged_data["settings"]) == ["local", "managed", "other"]
    assert merged_data == {
        "settings": {"local": "keep", "managed": "new", "other": "keep"}
    }



def test_merge_remove_key_path_preserves_nested_repo_deletions(tmp_path: Path) -> None:
    live_path = tmp_path / "live.json"
    repo_path = tmp_path / "repo.json"
    output_path = tmp_path / "output.json"

    live_path.write_text(
        json.dumps({"settings": {"local": "keep", "managed": "old"}}, indent=2)
        + "\n",
        encoding="utf-8",
    )
    repo_path.write_text("{}\n", encoding="utf-8")

    exit_code = cli.main(
        [
            "transform",
            "json",
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
    assert load_json(output_path) == {"settings": {"local": "keep"}}



def test_merge_retain_key_path_preserves_selected_nested_live_keys(tmp_path: Path) -> None:
    live_path = tmp_path / "live.json"
    repo_path = tmp_path / "repo.json"
    output_path = tmp_path / "output.json"

    live_path.write_text(
        json.dumps({"settings": {"managed": "old", "noise": "keep"}}, indent=2)
        + "\n",
        encoding="utf-8",
    )
    repo_path.write_text(
        json.dumps({"settings": {"managed": "new"}}, indent=2) + "\n",
        encoding="utf-8",
    )

    exit_code = cli.main(
        [
            "transform",
            "json",
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
    assert load_json(output_path) == {"settings": {"managed": "new", "noise": "keep"}}



def test_merge_top_level_object_key_still_replaces_with_overlay_value(tmp_path: Path) -> None:
    live_path = tmp_path / "live.json"
    repo_path = tmp_path / "repo.json"
    output_path = tmp_path / "output.json"

    live_path.write_text(
        json.dumps({"settings": {"local": "keep", "managed": "old"}}, indent=2)
        + "\n",
        encoding="utf-8",
    )
    repo_path.write_text(
        json.dumps({"settings": {"managed": "new"}}, indent=2) + "\n",
        encoding="utf-8",
    )

    exit_code = cli.main(
        [
            "transform",
            "json",
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
    assert load_json(output_path) == {"settings": {"managed": "new"}}



def test_merge_remove_key_preserves_unselected_live_keys(tmp_path: Path) -> None:
    live_path = tmp_path / "live.json"
    repo_path = tmp_path / "repo.json"
    output_path = tmp_path / "output.json"

    live_path.write_text(
        json.dumps(
            {
                "keepLocal": "noise",
                "buildDir": "/home/test/.cache/yay",
                "version": "11.0.0",
            },
            indent="\t",
        )
        + "\n",
        encoding="utf-8",
    )
    repo_path.write_text(
        json.dumps(
            {
                "version": "12.5.7",
                "rpc": True,
            },
            indent="\t",
        )
        + "\n",
        encoding="utf-8",
    )

    exit_code = cli.main(
        [
            "transform",
            "json",
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
    assert load_json(output_path) == {
        "keepLocal": "noise",
        "version": "12.5.7",
        "rpc": True,
    }


def test_stdin_base_indent_is_preserved(monkeypatch, capsys) -> None:
    import io


    source = '{\n\t"settings": {\n\t\t"a": 1\n\t}\n}\n'
    monkeypatch.setattr("sys.stdin", io.StringIO(source))

    assert cli.main(["transform", "json", "-", "-", "--mode", "cleanup"]) == 0
    assert capsys.readouterr().out == source


def test_lone_surrogate_escape_survives_output(tmp_path: Path) -> None:
    input_path = tmp_path / "input.json"
    output_path = tmp_path / "output.json"
    input_path.write_text('{"a": "\\ud800", "b": 1}\n', encoding="utf-8")

    assert cli.main(["transform", "json", str(input_path), str(output_path), "--mode", "cleanup", "--selectors", "a"]) == 0
    assert load_json(output_path) == {"a": "\ud800"}


def test_number_out_of_float_range_is_an_error_not_infinity(tmp_path: Path, capsys) -> None:

    input_path = tmp_path / "input.json"
    input_path.write_text('{"a": 1e400, "b": 1}\n', encoding="utf-8")

    assert cli.main(["transform", "json", str(input_path), "--stdout", "--mode", "cleanup"]) == 1
    captured = capsys.readouterr()
    assert "Infinity" not in captured.out
    assert "1e400" in captured.err


@pytest.mark.parametrize("token", ["NaN", "Infinity", "-Infinity"])
def test_non_json_number_token_is_an_error_not_passed_through(token: str, tmp_path: Path, capsys) -> None:

    input_path = tmp_path / "input.json"
    input_path.write_text(f'{{"a": {token}, "b": 1}}\n', encoding="utf-8")

    assert cli.main(["transform", "json", str(input_path), "--stdout", "--mode", "cleanup"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert token in captured.err
