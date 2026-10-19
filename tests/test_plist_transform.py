from __future__ import annotations

from pathlib import Path
import plistlib

from dotman.transforms import plist as MODULE


def load_plist(path: Path):
    with path.open("rb") as handle:
        return plistlib.load(handle)


def write_plist(path: Path, data: dict, fmt=plistlib.FMT_XML) -> None:
    with path.open("wb") as handle:
        plistlib.dump(data, handle, fmt=fmt, sort_keys=True)


def test_plist_engine_declares_typed_selectors() -> None:
    selector_specs = {spec.name: spec for spec in MODULE.PlistTransformEngine.selector_specs()}

    assert selector_specs["key"].prefix == "exact"
    assert selector_specs["key_regex"].prefix == "re"


def test_compare_file_preserves_existing_bytes(tmp_path: Path) -> None:
    input_path = tmp_path / "input.plist"
    compare_path = tmp_path / "compare.plist"
    output_path = tmp_path / "output.plist"

    write_plist(input_path, {"Alpha": 1}, fmt=plistlib.FMT_BINARY)
    compare_path.write_bytes(input_path.read_bytes())

    exit_code = MODULE.main(
        [
            str(input_path),
            str(output_path),
            "--mode",
            "cleanup",
            "--compare-file",
            str(compare_path),
            "--output-format",
            "xml",
        ]
    )

    assert exit_code == 0
    assert output_path.read_bytes() == compare_path.read_bytes()


def test_strip_mode_without_compare_file_reserializes_requested_format(
    tmp_path: Path,
 ) -> None:
    input_path = tmp_path / "input.plist"
    output_path = tmp_path / "output.plist"

    write_plist(input_path, {"Alpha": 1}, fmt=plistlib.FMT_BINARY)

    exit_code = MODULE.main(
        [
            str(input_path),
            str(output_path),
            "--mode",
            "cleanup",
            "--output-format",
            "xml",
        ]
    )

    assert exit_code == 0
    assert load_plist(output_path) == {"Alpha": 1}
    assert output_path.read_bytes().startswith(b"<?xml")


def test_merge_mode_without_compare_file_reserializes_requested_format(
    tmp_path: Path,
 ) -> None:
    live_path = tmp_path / "live.plist"
    repo_path = tmp_path / "repo.plist"
    output_path = tmp_path / "output.plist"

    write_plist(live_path, {"Alpha": 1}, fmt=plistlib.FMT_BINARY)
    write_plist(repo_path, {"Alpha": 1})

    exit_code = MODULE.main(
        [
            str(live_path),
            str(output_path),
            "--mode",
            "merge",
            "--overlay-file",
            str(repo_path),
            "--output-format",
            "xml",
        ]
    )

    assert exit_code == 0
    assert load_plist(output_path) == {"Alpha": 1}
    assert output_path.read_bytes().startswith(b"<?xml")


def test_strip_mode_retain_key_keeps_only_selected_keys(tmp_path: Path) -> None:
    input_path = tmp_path / "input.plist"
    output_path = tmp_path / "output.plist"

    write_plist(
        input_path,
        {
            "bypassEventsFromOtherApplications": True,
            "SULastCheckTime": "noise",
        },
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
            "bypassEventsFromOtherApplications",
        ]
    )

    assert exit_code == 0
    assert load_plist(output_path) == {"bypassEventsFromOtherApplications": True}


def test_strip_mode_remove_key_regex_strips_matching_keys(tmp_path: Path) -> None:
    input_path = tmp_path / "input.plist"
    output_path = tmp_path / "output.plist"

    write_plist(
        input_path,
        {
            "NSWindow Frame Main": "noise",
            "SULastCheckTime": "noise",
            "KeepLocal": True,
        },
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
            r"re:^(NSWindow|SU)",
        ]
    )

    assert exit_code == 0
    assert load_plist(output_path) == {"KeepLocal": True}


def test_merge_mode_retain_key_merges_selected_overlay_keys(tmp_path: Path) -> None:
    live_path = tmp_path / "live.plist"
    repo_path = tmp_path / "repo.plist"
    output_path = tmp_path / "output.plist"

    write_plist(
        live_path,
        {
            "WindowGeometry": "noise",
            "SULastCheckTime": "noise",
        },
        fmt=plistlib.FMT_BINARY,
    )
    write_plist(repo_path, {"KeepRepo": "repo"})

    exit_code = MODULE.main(
        [
            str(live_path),
            str(output_path),
            "--mode",
            "merge",
            "--overlay-file",
            str(repo_path),
            "--output-format",
            "binary",
            "--selector-type",
            "retain",
            "--selectors",
            "WindowGeometry",
            "SULastCheckTime",
        ]
    )

    assert exit_code == 0
    assert load_plist(output_path) == {
        "KeepRepo": "repo",
        "WindowGeometry": "noise",
        "SULastCheckTime": "noise",
    }


def test_merge_mode_retain_key_regex_preserves_matching_live_keys(tmp_path: Path) -> None:
    live_path = tmp_path / "live.plist"
    repo_path = tmp_path / "repo.plist"
    output_path = tmp_path / "output.plist"

    write_plist(
        live_path,
        {
            "WindowGeometry": "live-geometry",
            "WindowState": "live-state",
            "ManagedKey": "stale",
        },
    )
    write_plist(repo_path, {"ManagedKey": "repo"})

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
    assert load_plist(output_path) == {
        "ManagedKey": "repo",
        "WindowGeometry": "live-geometry",
        "WindowState": "live-state",
    }


def test_merge_mode_remove_key_preserves_unselected_live_keys(tmp_path: Path) -> None:
    live_path = tmp_path / "live.plist"
    repo_path = tmp_path / "repo.plist"
    output_path = tmp_path / "output.plist"

    write_plist(
        live_path,
        {
            "KeepLocal": "noise",
            "WindowGeometry": "noise",
            "WindowState": "fullscreen",
        },
    )
    write_plist(repo_path, {"WindowGeometry": "repo-geometry"})

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
            "WindowGeometry",
        ]
    )

    assert exit_code == 0
    assert load_plist(output_path) == {
        "KeepLocal": "noise",
        "WindowGeometry": "repo-geometry",
        "WindowState": "fullscreen",
    }


def test_merge_mode_remove_key_reapplies_repo_over_unmanaged_live_keys(tmp_path: Path) -> None:
    live_path = tmp_path / "live.plist"
    repo_path = tmp_path / "repo.plist"
    output_path = tmp_path / "output.plist"

    write_plist(
        live_path,
        {
            "KeepLocal": "noise",
            "ManagedKey": "live-value",
            "WindowState": "fullscreen",
        },
    )
    write_plist(repo_path, {"ManagedKey": "repo-value"})

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
            "ManagedKey",
        ]
    )

    assert exit_code == 0
    assert load_plist(output_path) == {
        "KeepLocal": "noise",
        "ManagedKey": "repo-value",
        "WindowState": "fullscreen",
    }


def test_merge_mode_remove_key_reflects_nested_deletions_from_repo(tmp_path: Path) -> None:
    live_path = tmp_path / "live.plist"
    repo_path = tmp_path / "repo.plist"
    output_path = tmp_path / "output.plist"

    write_plist(
        live_path,
        {
            "KeepLocal": "noise",
            "ManagedKey": {
                "NestedValue": "repo",
                "DeleteMe": "stale",
            },
        },
    )
    write_plist(
        repo_path,
        {
            "ManagedKey": {
                "NestedValue": "repo",
            },
        },
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
            "ManagedKey",
        ]
    )

    assert exit_code == 0
    assert load_plist(output_path) == {
        "KeepLocal": "noise",
        "ManagedKey": {
            "NestedValue": "repo",
        },
    }


def test_malformed_xml_plist_is_a_clean_cli_error(tmp_path: Path, capsys) -> None:
    from dotman import cli

    input_path = tmp_path / "input.plist"
    input_path.write_text("<plist><dict><key>a</key>", encoding="utf-8")

    assert cli.main(["transform", "plist", str(input_path), "-", "--mode", "cleanup"]) == 1
    assert str(input_path) in capsys.readouterr().err


def test_value_unsupported_by_output_format_is_a_clean_cli_error(tmp_path: Path, capsys) -> None:
    from dotman import cli

    input_path = tmp_path / "input.plist"
    write_plist(input_path, {"archived": plistlib.UID(1)}, fmt=plistlib.FMT_BINARY)

    assert cli.main(["transform", "plist", str(input_path), "-", "--mode", "cleanup", "--output-format", "xml"]) == 1
    assert "xml" in capsys.readouterr().err
