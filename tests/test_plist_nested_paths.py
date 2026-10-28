from __future__ import annotations

from pathlib import Path
import plistlib

from dotman import cli


def load_plist(path: Path) -> dict:
    with path.open("rb") as handle:
        return plistlib.load(handle)


def write_plist(path: Path, data: dict) -> None:
    with path.open("wb") as handle:
        plistlib.dump(data, handle, fmt=plistlib.FMT_XML, sort_keys=True)


def test_cleanup_retain_quoted_dotted_top_level_key(tmp_path: Path) -> None:
    input_path = tmp_path / "input.plist"
    output_path = tmp_path / "output.plist"
    write_plist(
        input_path,
        {"com.apple.keyboard.fnState": True, "other": False},
    )

    exit_code = cli.main(
        [
            "transform",
            "plist",
            str(input_path),
            str(output_path),
            "--mode",
            "cleanup",
            "--selector-type",
            "retain",
            "--selectors",
            '"com.apple.keyboard.fnState"',
        ]
    )

    assert exit_code == 0
    assert load_plist(output_path) == {"com.apple.keyboard.fnState": True}


def test_cleanup_retain_nested_key_path_keeps_only_selected_key(tmp_path: Path) -> None:
    input_path = tmp_path / "input.plist"
    output_path = tmp_path / "output.plist"
    write_plist(
        input_path,
        {
            "settings": {
                "window": {"width": 1200, "height": 800},
                "theme": "dark",
            },
            "other": True,
        },
    )

    exit_code = cli.main(
        [
            "transform",
            "plist",
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
    assert load_plist(output_path) == {"settings": {"window": {"width": 1200}}}


def test_cleanup_remove_regex_strips_matching_nested_key_paths(tmp_path: Path) -> None:
    input_path = tmp_path / "input.plist"
    output_path = tmp_path / "output.plist"
    write_plist(
        input_path,
        {
            "widget": {
                "media": {"enabled": True, "volume": 75},
                "clock": {"enabled": False, "format": "HH:mm"},
            }
        },
    )

    exit_code = cli.main(
        [
            "transform",
            "plist",
            str(input_path),
            str(output_path),
            "--mode",
            "cleanup",
            "--selector-type",
            "remove",
            "--selectors",
            r"re:^widget\.[^.]+\.enabled$",
        ]
    )

    assert exit_code == 0
    assert load_plist(output_path) == {
        "widget": {
            "media": {"volume": 75},
            "clock": {"format": "HH:mm"},
        }
    }


def test_merge_remove_nested_path_preserves_unselected_live_keys(tmp_path: Path) -> None:
    live_path = tmp_path / "live.plist"
    repo_path = tmp_path / "repo.plist"
    output_path = tmp_path / "output.plist"
    write_plist(
        live_path,
        {"settings": {"local": "keep", "managed": "old", "other": "keep"}},
    )
    write_plist(repo_path, {"settings": {"managed": "new"}})

    exit_code = cli.main(
        [
            "transform",
            "plist",
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
    assert load_plist(output_path) == {
        "settings": {"local": "keep", "managed": "new", "other": "keep"}
    }


def test_merge_retain_nested_path_preserves_selected_live_key(tmp_path: Path) -> None:
    live_path = tmp_path / "live.plist"
    repo_path = tmp_path / "repo.plist"
    output_path = tmp_path / "output.plist"
    write_plist(live_path, {"settings": {"managed": "old", "noise": "keep"}})
    write_plist(repo_path, {"settings": {"managed": "new"}})

    exit_code = cli.main(
        [
            "transform",
            "plist",
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
    assert load_plist(output_path) == {
        "settings": {"managed": "new", "noise": "keep"}
    }
