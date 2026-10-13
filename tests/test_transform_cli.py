from __future__ import annotations

import io
from pathlib import Path
import json
import plistlib

import pytest

import dotman.transforms.framework as MODULE


class DummyEngine(MODULE.BaseTransformEngine):
    name = "dummy"
    SELECTOR_SPECS = (
        MODULE.SelectorSpec(
            name="key",
            prefix="exact",
            is_default=True,
            description="Exact key selector",
        ),
    )

    def transform(self, request: MODULE.TransformRequest) -> MODULE.TransformOutput:
        self.validate_request(request)
        return MODULE.TransformOutput(content="ok\n", mode_reference_path=request.base_path)


def test_selector_spec_records_prefix_and_default_status() -> None:
    spec = MODULE.SelectorSpec(
        name="table_regex",
        prefix="re",
        description="Regex table selector",
    )

    assert spec.prefix == "re"
    assert spec.is_default is False


def test_compile_selector_regexes_reports_invalid_pattern() -> None:
    with pytest.raises(ValueError, match="invalid test selector regex"):
        MODULE.compile_selector_regexes(["["], "test selector")


def test_transform_request_requires_overlay_in_merge_mode(tmp_path: Path) -> None:
    request = MODULE.TransformRequest(
        base_path=tmp_path / "base",
        output_path=tmp_path / "output",
        mode=MODULE.TransformMode.MERGE,
        selector_action=MODULE.SelectorAction.RETAIN,
        selectors_by_type={"key": ("model",)},
    )

    with pytest.raises(ValueError, match="overlay_path is required"):
        request.validate_basic()


def test_transform_request_rejects_overlay_in_cleanup_mode(tmp_path: Path) -> None:
    request = MODULE.TransformRequest(
        base_path=tmp_path / "base",
        output_path=tmp_path / "output",
        mode=MODULE.TransformMode.CLEANUP,
        selector_action=MODULE.SelectorAction.REMOVE,
        selectors_by_type={"key": ("model",)},
        overlay_path=tmp_path / "live",
    )

    with pytest.raises(ValueError, match="only valid when mode=merge"):
        request.validate_basic()


def test_transform_request_requires_output_path_without_stdout(tmp_path: Path) -> None:
    request = MODULE.TransformRequest(
        base_path=tmp_path / "base",
        output_path=None,
        mode=MODULE.TransformMode.CLEANUP,
        selector_action=MODULE.SelectorAction.RETAIN,
        selectors_by_type={"key": ("model",)},
    )

    with pytest.raises(ValueError, match="output_path is required unless stdout output is enabled"):
        request.validate_basic()


def test_transform_request_allows_stdout_without_output_path(tmp_path: Path) -> None:
    request = MODULE.TransformRequest(
        base_path=tmp_path / "base",
        output_path=None,
        mode=MODULE.TransformMode.CLEANUP,
        selector_action=MODULE.SelectorAction.RETAIN,
        selectors_by_type={"key": ("model",)},
        engine_options={"stdout": True},
    )

    request.validate_basic()


def test_base_engine_rejects_unknown_selector_types(tmp_path: Path) -> None:
    engine = DummyEngine()
    request = MODULE.TransformRequest(
        base_path=tmp_path / "base",
        output_path=tmp_path / "output",
        mode=MODULE.TransformMode.CLEANUP,
        selector_action=MODULE.SelectorAction.RETAIN,
        selectors_by_type={"table_regex": (r"^projects\.",)},
    )

    with pytest.raises(ValueError, match="does not support selector types"):
        engine.validate_request(request)


def test_emit_transform_output_decodes_binary_when_stdout_is_text_only(
    tmp_path: Path,
    monkeypatch,
) -> None:
    base_path = tmp_path / "base"
    base_path.write_text("ref\n", encoding="utf-8")

    fake_stdout = io.StringIO()
    monkeypatch.setattr(MODULE.sys, "stdout", fake_stdout)

    MODULE.emit_transform_output(
        None,
        MODULE.TransformOutput(content="snowman ☃".encode("utf-8"), mode_reference_path=base_path),
        stdout=True,
    )

    assert fake_stdout.getvalue() == "snowman ☃"


def test_emit_transform_output_skips_rewrite_when_reusing_same_compare_path(
    tmp_path: Path,
) -> None:
    reference_path = tmp_path / "reference"
    output_path = tmp_path / "output"
    reference_path.write_text("ref\n", encoding="utf-8")
    output_path.write_text("keep\n", encoding="utf-8")
    output_path.chmod(0o600)
    output_path.touch()
    original_mtime = output_path.stat().st_mtime_ns

    MODULE.emit_transform_output(
        output_path,
        MODULE.TransformOutput(
            content="keep\n",
            mode_reference_path=reference_path,
            reused_compare_path=output_path,
        ),
    )

    assert output_path.read_text(encoding="utf-8") == "keep\n"
    assert output_path.stat().st_mtime_ns == original_mtime
    assert output_path.stat().st_mode & 0o777 == reference_path.stat().st_mode & 0o777


@pytest.mark.parametrize("content", ["after\n", b"after\n"])
def test_transform_file_output_failure_preserves_existing_destination(
    tmp_path: Path,
    monkeypatch,
    content: str | bytes,
) -> None:
    output_path = tmp_path / "output"
    output_path.write_bytes(b"before\n")

    def fail_replacement(temp_path: Path, target_path: Path) -> Path:
        raise RuntimeError("replacement failed")

    monkeypatch.setattr(Path, "replace", fail_replacement)

    with pytest.raises(RuntimeError, match="replacement failed"):
        MODULE.write_output_to_path(
            output_path,
            MODULE.TransformOutput(content=content, mode_reference_path=None),
        )

    assert output_path.read_bytes() == b"before\n"
    assert list(tmp_path.glob(".dotman-*.tmp")) == []


def test_root_cli_supports_stdin_and_output_dash(monkeypatch, capsys) -> None:
    from dotman import cli

    monkeypatch.setattr("sys.stdin", io.StringIO('{"a": 1}\n'))
    assert cli.main(["transform", "json", "-", "-", "--mode", "cleanup"]) == 0
    assert json.loads(capsys.readouterr().out) == {"a": 1}


def test_root_cli_rejects_two_stdin_inputs(monkeypatch, capsys) -> None:
    from dotman import cli

    monkeypatch.setattr("sys.stdin", io.StringIO("must not be read"))
    assert cli.main(["transform", "json", "-", "--mode", "merge", "--overlay-file", "-", "--stdout"]) == 2
    assert "at most one" in capsys.readouterr().err


def test_stdout_takes_precedence_over_output_operand(tmp_path, capsys) -> None:
    from dotman import cli

    base = tmp_path / "base.json"
    output = tmp_path / "unused.json"
    base.write_text('{"a": 1}\n', encoding="utf-8")
    assert cli.main(["transform", "json", str(base), str(output), "--mode", "cleanup", "--stdout"]) == 0
    assert json.loads(capsys.readouterr().out) == {"a": 1}
    assert not output.exists()


def test_root_help_documents_json_selector_contract(capsys) -> None:
    from dotman import cli

    with pytest.raises(SystemExit) as exit_info:
        cli.main(["transform", "json", "--help"])
    assert exit_info.value.code == 0
    help_text = capsys.readouterr().out
    assert "Unprefixed selectors use exact:" in help_text
    assert "exact:" in help_text
    assert "re:" in help_text
    assert "dotted or quoted nested JSON object key path" in help_text
    assert "full JSON object key paths" in " ".join(help_text.split())


def test_root_merge_reads_overlay_from_stdin(tmp_path, monkeypatch) -> None:
    from dotman import cli

    base = tmp_path / "base.json"
    output = tmp_path / "output.json"
    base.write_text('{"managed": {"old": 1}, "local": 2}\n', encoding="utf-8")
    monkeypatch.setattr("sys.stdin", io.StringIO('{"managed": {"new": 3}}\n'))

    assert cli.main(["transform", "json", str(base), str(output), "--mode", "merge", "--overlay-file", "-", "--selectors", "managed"]) == 0
    assert json.loads(output.read_text()) == {"managed": {"new": 3}}


def test_root_compare_reuses_raw_bytes_to_stdout(tmp_path, capfdbinary) -> None:
    from dotman import cli

    base = tmp_path / "base.json"
    compare = tmp_path / "compare.json"
    base.write_text('{"value": 1}\n', encoding="utf-8")
    expected = b'{\r\n  "value": 1\r\n}\r\n'
    compare.write_bytes(expected)

    assert cli.main(["transform", "json", str(base), "--mode", "cleanup", "--compare-file", str(compare), "--stdout"]) == 0
    assert capfdbinary.readouterr().out == expected


def test_root_compare_reuses_raw_bytes_at_different_output_path(tmp_path) -> None:
    from dotman import cli

    base = tmp_path / "base.json"
    compare = tmp_path / "compare.json"
    output = tmp_path / "output.json"
    base.write_text('{"value": 1}\n', encoding="utf-8")
    expected = b'{\r\n\t"value": 1\r\n}\r\n'
    compare.write_bytes(expected)

    assert cli.main(["transform", "json", str(base), str(output), "--mode", "cleanup", "--compare-file", str(compare)]) == 0
    assert output.read_bytes() == expected


def test_root_file_output_inherits_base_permissions(tmp_path) -> None:
    from dotman import cli

    base = tmp_path / "base.json"
    output = tmp_path / "output.json"
    base.write_text('{"value": 1}\n', encoding="utf-8")
    base.chmod(0o640)

    assert cli.main(["transform", "json", str(base), str(output), "--mode", "cleanup"]) == 0
    assert output.stat().st_mode & 0o777 == 0o640


def test_root_stdin_base_does_not_sync_permissions(tmp_path, monkeypatch) -> None:
    from dotman import cli

    output = tmp_path / "output.json"
    monkeypatch.setattr("sys.stdin", io.StringIO('{"value": 1}\n'))
    chmod_calls = []
    original_chmod = Path.chmod
    monkeypatch.setattr(Path, "chmod", lambda self, mode: chmod_calls.append((self, mode)))

    assert cli.main(["transform", "json", "-", str(output), "--mode", "cleanup"]) == 0
    assert json.loads(output.read_text()) == {"value": 1}
    assert chmod_calls == []
    monkeypatch.setattr(Path, "chmod", original_chmod)


def test_root_nested_regex_removal_and_mode_validation(tmp_path, capsys) -> None:
    from dotman import cli

    base = tmp_path / "base.json"
    overlay = tmp_path / "overlay.json"
    output = tmp_path / "output.json"
    base.write_text('{"settings": {"secret": 1, "keep": 2}, "other": 3}\n', encoding="utf-8")
    overlay.write_text('{}\n', encoding="utf-8")

    assert cli.main(["transform", "json", str(base), str(output), "--mode", "cleanup", "--selector-type", "remove", "--selectors", "re:^settings\\.secret$"]) == 0
    assert json.loads(output.read_text()) == {"settings": {"keep": 2}, "other": 3}
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["transform", "json", str(base), str(output), "--mode", "cleanup", "--overlay-file", str(overlay)])
    assert exit_info.value.code == 2
    assert "only valid when mode=merge" in capsys.readouterr().err


def test_file_output_has_base_permissions_before_it_becomes_visible(tmp_path, monkeypatch) -> None:
    from dotman import cli

    base = tmp_path / "secret.json"
    output = tmp_path / "output.json"
    base.write_text('{"token": "x"}\n', encoding="utf-8")
    base.chmod(0o600)
    modes_at_replace = []
    original_replace = Path.replace

    def spy_replace(self: Path, target: Path) -> Path:
        modes_at_replace.append(self.stat().st_mode & 0o777)
        return original_replace(self, target)

    monkeypatch.setattr(Path, "replace", spy_replace)

    assert cli.main(["transform", "json", str(base), str(output), "--mode", "cleanup"]) == 0
    assert modes_at_replace == [0o600]


def test_file_output_through_symlink_updates_link_target(tmp_path) -> None:
    from dotman import cli

    base = tmp_path / "base.json"
    target = tmp_path / "target.json"
    link = tmp_path / "link.json"
    base.write_text('{"a": 1}\n', encoding="utf-8")
    target.write_text("{}\n", encoding="utf-8")
    link.symlink_to(target)

    assert cli.main(["transform", "json", str(base), str(link), "--mode", "cleanup"]) == 0
    assert link.is_symlink()
    assert json.loads(target.read_text()) == {"a": 1}


def test_compare_reuse_at_same_file_spelled_differently_keeps_mtime(tmp_path, monkeypatch) -> None:
    from dotman import cli

    monkeypatch.chdir(tmp_path)
    base = tmp_path / "base.json"
    compare = tmp_path / "compare.json"
    base.write_text('{"value": 1}\n', encoding="utf-8")
    compare.write_text('{"value":1}\n', encoding="utf-8")
    old_time_ns = 1_600_000_000_000_000_000
    import os

    os.utime(compare, ns=(old_time_ns, old_time_ns))

    assert cli.main(["transform", "json", str(base), str(compare), "--mode", "cleanup", "--compare-file", "compare.json"]) == 0
    assert compare.stat().st_mtime_ns == old_time_ns


def test_compare_file_rejects_stdin(tmp_path, monkeypatch, capsys) -> None:
    from dotman import cli

    base = tmp_path / "base.json"
    base.write_text('{"a": 1}\n', encoding="utf-8")
    monkeypatch.setattr("sys.stdin", io.StringIO("{}\n"))

    with pytest.raises(SystemExit) as exit_info:
        cli.main(["transform", "json", str(base), "--mode", "cleanup", "--compare-file", "-", "--stdout"])
    assert exit_info.value.code == 2
    assert "--compare-file" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("transform_format", "content"),
    [("json", b'{"a": "\xff"}\n'), ("toml", b'a = "\xff"\n'), ("yaml", b'a: "\xff"\n')],
)
def test_stdin_with_invalid_utf8_fails_like_file_input(transform_format, content, monkeypatch, capsys) -> None:
    from dotman import cli

    monkeypatch.setattr("sys.stdin", io.TextIOWrapper(io.BytesIO(content)))

    assert cli.main(["transform", transform_format, "-", "-", "--mode", "cleanup", "--selectors", "zzz"]) == 2
    assert "utf-8" in capsys.readouterr().err.lower()


@pytest.mark.parametrize(
    ("transform_format", "content"),
    [
        ("json", '{"a": 1}\n'),
        ("toml", "a = 1\n"),
        ("yaml", "a: 1\n"),
        ("plist", '<?xml version="1.0" encoding="UTF-8"?>\n<plist version="1.0"><dict><key>a</key><integer>1</integer></dict></plist>\n'),
        ("xml", "<config><a>1</a></config>\n"),
    ],
)
def test_stdin_base_never_inherits_permissions_from_a_file_named_dash(
    transform_format, content, tmp_path, monkeypatch
) -> None:
    from dotman import cli

    monkeypatch.chdir(tmp_path)
    dash_file = tmp_path / "-"
    dash_file.write_text(content, encoding="utf-8")
    dash_file.chmod(0o600)
    output = tmp_path / "output"
    monkeypatch.setattr("sys.stdin", io.TextIOWrapper(io.BytesIO(content.encode())))

    assert cli.main(["transform", transform_format, "-", str(output), "--mode", "cleanup", "--selectors", "a"]) == 0
    assert output.stat().st_mode & 0o777 != 0o600


def test_file_system_errors_are_clean_cli_errors(tmp_path, capsys) -> None:
    from dotman import cli

    base = tmp_path / "base.json"
    base.write_text('{"a": 1}\n', encoding="utf-8")
    output_directory = tmp_path / "directory"
    output_directory.mkdir()

    assert cli.main(["transform", "json", str(base), str(output_directory), "--mode", "cleanup"]) == 2
    assert str(output_directory) in capsys.readouterr().err


_PLIST_WITH_BAD_DATE = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<plist version="1.0"><dict><key>a</key><date>nope</date></dict></plist>\n'
)


# Each base parses past the syntax layer but fails while building values. The
# expected message is what ordinary invalid input (e.g. `a: [` or `<plist><dict>`)
# already reports for that format; TOML syntax errors carry no format prefix.
@pytest.mark.parametrize(
    ("transform_format", "content", "expected_message"),
    [
        ("toml", "[a]\nb = 1\n[a.b]\n", None),
        ("yaml", "a: !!bool maybe\n", "invalid YAML in {base}"),
        ("yaml", "a: !!timestamp nope\n", "invalid YAML in {base}"),
        ("plist", _PLIST_WITH_BAD_DATE, "invalid plist in {base}"),
    ],
)
def test_malformed_values_are_clean_cli_errors_like_other_invalid_input(
    transform_format, content, expected_message, tmp_path, capsys
) -> None:
    from dotman import cli

    base = tmp_path / f"base.{transform_format}"
    base.write_text(content, encoding="utf-8")

    exit_code = cli.main(["transform", transform_format, str(base), "--stdout", "--mode", "cleanup", "--selectors", "a"])

    error_output = capsys.readouterr().err
    assert exit_code == 2
    assert "detail:" in error_output
    if expected_message is not None:
        assert expected_message.format(base=base) in error_output


@pytest.mark.parametrize(
    ("transform_format", "base_content"),
    [
        ("json", '{"a": 1, "b": 2}\n'),
        ("toml", "a = 1\nb = 2\n"),
        ("yaml", "a: 1\nb: 2\n"),
        ("plist", plistlib.dumps({"a": 1, "b": 2}).decode()),
        ("xml", "<root><a>1</a><b>2</b></root>\n"),
    ],
)
def test_missing_overlay_file_counts_as_empty_like_a_missing_base(
    transform_format, base_content, tmp_path, capsys
) -> None:
    from dotman import cli

    # Either input may be the live file, so a missing overlay is treated the
    # same as a missing base: an empty document.
    base = tmp_path / f"base.{transform_format}"
    base.write_text(base_content, encoding="utf-8")
    selector_args = ["--selectors", "a"]

    assert cli.main(["transform", transform_format, str(base), "--stdout", "--mode", "cleanup", *selector_args]) == 0
    cleanup_output = capsys.readouterr().out

    assert cli.main([
        "transform", transform_format, str(base), "--stdout", "--mode", "merge",
        "--overlay-file", str(tmp_path / f"missing.{transform_format}"), *selector_args,
    ]) == 0
    assert capsys.readouterr().out == cleanup_output


@pytest.mark.parametrize(
    ("transform_format", "content"),
    [("json", '{\n  "a": 1\n}\n'), ("toml", "a = 1\n"), ("yaml", "a: 1\n")],
)
def test_utf8_bom_input_is_read_like_plain_utf8(transform_format, content, tmp_path, capsys) -> None:
    from dotman import cli

    # Some Windows editors save UTF-8 with a byte order mark.
    base = tmp_path / f"base.{transform_format}"
    base.write_bytes(b"\xef\xbb\xbf" + content.encode())

    assert cli.main(["transform", transform_format, str(base), "--stdout", "--mode", "cleanup", "--selectors", "a"]) == 0
    assert capsys.readouterr().out == content


TEXT_TRANSFORM_SAMPLES = {
    "json": '{\n    "value": 1\n}\n',
    "yaml": "value: 1\n",
    "toml": "value = 1\n",
    "xml": "<root><value>1</value></root>\n",
}
TRANSFORM_SAMPLES = {**TEXT_TRANSFORM_SAMPLES, "plist": plistlib.dumps({"value": 1}).decode()}
# XML and TOML cleanup need a selector; one that matches nothing keeps the sample intact.
NO_OP_CLEANUP_SELECTORS = ("--selector-type", "remove", "--selectors", "absent")
CLEANUP_ARGUMENTS = {"xml": NO_OP_CLEANUP_SELECTORS, "toml": NO_OP_CLEANUP_SELECTORS}


@pytest.mark.parametrize("transform_format", TRANSFORM_SAMPLES)
@pytest.mark.parametrize("unreadable_compare", ["directory", "not_utf8"])
def test_unreadable_compare_file_is_not_reused(transform_format, unreadable_compare, tmp_path) -> None:
    from dotman import cli

    base = tmp_path / f"base.{transform_format}"
    base.write_text(TRANSFORM_SAMPLES[transform_format], encoding="utf-8")
    compare = tmp_path / f"compare.{transform_format}"
    if unreadable_compare == "directory":
        compare.mkdir()
    else:
        compare.write_bytes(b"\xff\xfe not utf-8")
    output = tmp_path / f"output.{transform_format}"

    assert cli.main([
        "transform", transform_format, str(base), str(output), "--mode", "cleanup", "--compare-file", str(compare),
        *CLEANUP_ARGUMENTS.get(transform_format, ()),
    ]) == 0
    assert output.exists()


@pytest.mark.parametrize("transform_format", TEXT_TRANSFORM_SAMPLES)
def test_compare_file_with_utf8_bom_is_reused(transform_format, tmp_path) -> None:
    from dotman import cli

    base = tmp_path / f"base.{transform_format}"
    base.write_text(TEXT_TRANSFORM_SAMPLES[transform_format], encoding="utf-8")
    compare = tmp_path / f"compare.{transform_format}"
    compare_bytes = b"\xef\xbb\xbf" + TEXT_TRANSFORM_SAMPLES[transform_format].encode()
    compare.write_bytes(compare_bytes)
    output = tmp_path / f"output.{transform_format}"

    assert cli.main([
        "transform", transform_format, str(base), str(output), "--mode", "cleanup", "--compare-file", str(compare),
        *CLEANUP_ARGUMENTS.get(transform_format, ()),
    ]) == 0
    assert output.read_bytes() == compare_bytes


def test_base_with_utf8_bom_still_sets_json_indent(tmp_path) -> None:
    from dotman import cli

    base = tmp_path / "base.json"
    base.write_bytes(b"\xef\xbb\xbf" + TEXT_TRANSFORM_SAMPLES["json"].encode())
    output = tmp_path / "output.json"

    assert cli.main(["transform", "json", str(base), str(output), "--mode", "cleanup"]) == 0
    assert output.read_text(encoding="utf-8") == TEXT_TRANSFORM_SAMPLES["json"]



@pytest.mark.parametrize("transform_format", TRANSFORM_SAMPLES)
def test_overlay_that_is_a_directory_is_an_error_not_an_empty_file(transform_format, tmp_path, capsys) -> None:
    from dotman import cli

    base = tmp_path / f"base.{transform_format}"
    base.write_text(TRANSFORM_SAMPLES[transform_format], encoding="utf-8")
    overlay = tmp_path / f"overlay.{transform_format}"
    overlay.mkdir()
    output = tmp_path / f"output.{transform_format}"

    assert cli.main([
        "transform", transform_format, str(base), str(output), "--mode", "merge", "--overlay-file", str(overlay),
        *CLEANUP_ARGUMENTS.get(transform_format, ()),
    ]) != 0
    assert "Is a directory" in capsys.readouterr().err
    assert not output.exists()


def test_xml_rejects_not_selectors(tmp_path, capsys) -> None:
    from dotman import cli

    base = tmp_path / "base.xml"
    base.write_text("<config><a/></config>\n", encoding="utf-8")

    with pytest.raises(SystemExit) as exit_info:
        cli.main(["transform", "xml", str(base), "--stdout", "--mode", "cleanup", "--selectors", "not:config/a"])
    assert exit_info.value.code == 2
    assert "not:" in capsys.readouterr().err
