from __future__ import annotations

import io
from pathlib import Path

import pytest

from dotman import cli
from tests.helpers import read_yaml


def test_root_yaml_stdin_stdout_selection(monkeypatch, capsys) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO('keep: "yes"\ndrop: "no"\n'))

    assert cli.main(["transform", "yaml", "-", "-", "--mode", "cleanup", "--selectors", "keep"]) == 0

    assert read_yaml(capsys.readouterr().out) == {"keep": "yes"}


def test_root_yaml_cleanup_without_selectors_is_identity(tmp_path: Path, capsys) -> None:
    base = tmp_path / "base.yaml"
    base.write_text("a: 1\nb: 2\n", encoding="utf-8")

    assert cli.main(["transform", "yaml", str(base), "--mode", "cleanup", "--stdout"]) == 0

    assert read_yaml(capsys.readouterr().out) == {"a": 1, "b": 2}


def test_root_yaml_merge_compare_text_and_permissions(tmp_path: Path) -> None:
    base = tmp_path / "base.yaml"
    overlay = tmp_path / "overlay.yaml"
    compare = tmp_path / "compare.yaml"
    output = tmp_path / "output.yaml"
    base.write_text("local: yes\nmanaged: old\n", encoding="utf-8")
    overlay.write_text("managed: repo\n", encoding="utf-8")
    expected = "managed: repo\nlocal: yes\n"
    compare.write_text(expected, encoding="utf-8")
    base.chmod(0o640)

    assert cli.main(["transform", "yaml", str(base), str(output), "--mode", "merge", "--overlay-file", str(overlay), "--selector-type", "retain", "--selectors", "local", "--compare-file", str(compare)]) == 0

    assert output.read_text(encoding="utf-8") == expected
    assert output.stat().st_mode & 0o777 == 0o640


def test_root_yaml_help_documents_selector_contract(capsys) -> None:
    with pytest.raises(SystemExit) as result:
        cli.main(["transform", "yaml", "--help"])
    assert result.value.code == 0
    help_text = capsys.readouterr().out
    assert "Unprefixed selectors use exact:" in help_text
    assert "exact:" in help_text
    assert "re:" in help_text
    assert "dotted or quoted nested YAML mapping key path" in help_text
    assert "full YAML mapping key paths" in " ".join(help_text.split())
    assert "--compare-file" in help_text


def test_root_yaml_compare_reuses_crlf_bytes_for_file_and_stdout(
    tmp_path: Path, capsysbinary
) -> None:
    base = tmp_path / "base.yaml"
    compare = tmp_path / "compare.yaml"
    output = tmp_path / "output.yaml"
    base.write_bytes(b"items: [a]\n")
    expected = b"items: [a]\r\n"
    compare.write_bytes(expected)
    args = ["transform", "yaml", str(base), str(output), "--mode", "cleanup", "--selectors", "items", "--compare-file", str(compare)]
    assert cli.main(args) == 0
    assert output.read_bytes() == expected

    args[3] = "-"
    assert cli.main(args) == 0
    assert capsysbinary.readouterr().out == expected


def test_root_yaml_overlay_stdin_and_multiple_stdin_rejection(
    tmp_path: Path, monkeypatch
) -> None:
    base = tmp_path / "base.yaml"
    output = tmp_path / "output.yaml"
    base.write_text('local: "yes"\nmanaged: "old"\n', encoding="utf-8")
    monkeypatch.setattr("sys.stdin", io.StringIO('managed: "stdin"\n'))
    assert cli.main(["transform", "yaml", str(base), str(output), "--mode", "merge", "--overlay-file", "-", "--selectors", "local"]) == 0
    assert read_yaml(output.read_text()) == {"local": "yes", "managed": "stdin"}

    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    assert cli.main(["transform", "yaml", "-", str(output), "--mode", "merge", "--overlay-file", "-", "--selectors", "local"]) == 2
