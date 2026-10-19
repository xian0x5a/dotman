from __future__ import annotations

import io
from pathlib import Path
from types import SimpleNamespace

from dotman import cli
import pytest


def test_root_rewrite_home_expand_reads_stdin_and_writes_stdout(
    monkeypatch,
    capsysbinary,
) -> None:
    monkeypatch.setenv("HOME", "/home/alice")
    monkeypatch.setattr("sys.stdin", io.TextIOWrapper(io.BytesIO(b"path = ~/project\r\n")))

    assert cli.main(["rewrite", "home", "expand"]) == 0
    assert capsysbinary.readouterr() == (b"path = /home/alice/project\r\n", b"")


def test_root_rewrite_home_collapse_reads_utf8_file(
    tmp_path: Path,
    monkeypatch,
    capsysbinary,
) -> None:
    source = tmp_path / "settings.txt"
    source.write_bytes("é /home/alice/project\n".encode())
    monkeypatch.setenv("HOME", "/home/alice///")

    assert cli.main(["rewrite", "home", "collapse", str(source)]) == 0
    assert capsysbinary.readouterr() == ("é ~/project\n".encode(), b"")


def test_root_rewrite_home_accepts_explicit_stdin_operand(
    monkeypatch,
    capsysbinary,
) -> None:
    monkeypatch.setenv("HOME", "/home/alice")
    monkeypatch.setattr("sys.stdin", io.TextIOWrapper(io.BytesIO(b"~/project")))

    assert cli.main(["rewrite", "home", "expand", "-"]) == 0
    assert capsysbinary.readouterr().out == b"/home/alice/project"


@pytest.mark.parametrize(
    ("arguments", "source", "expected"),
    [
        pytest.param(["expand"], b"proxy: ~\n", b"proxy: ~\n", id="expand-default"),
        pytest.param(
            ["expand", "--bare-tilde", "-"],
            b"proxy: ~\n",
            b"proxy: /home/alice\n",
            id="expand-bare-tilde",
        ),
        pytest.param(
            ["collapse"],
            b"dir: /home/alice\n",
            b"dir: /home/alice\n",
            id="collapse-default",
        ),
        pytest.param(
            ["collapse", "-", "--bare-tilde"],
            b"dir: /home/alice\n",
            b"dir: ~\n",
            id="collapse-bare-tilde",
        ),
    ],
)
def test_root_rewrite_home_bare_tilde_flag_opts_into_bare_home_rewrites(
    arguments: list[str],
    source: bytes,
    expected: bytes,
    monkeypatch,
    capsysbinary,
) -> None:
    monkeypatch.setenv("HOME", "/home/alice")
    monkeypatch.setattr("sys.stdin", io.TextIOWrapper(io.BytesIO(source)))

    assert cli.main(["rewrite", "home", *arguments]) == 0
    assert capsysbinary.readouterr() == (expected, b"")


def test_root_rewrite_home_preserves_all_unmatched_utf8_bytes(
    monkeypatch,
    capsysbinary,
) -> None:
    source = b"\xef\xbb\xbfdecomposed=e\xcc\x81\r\n$HOME ${HOME}\tno-final-newline"
    monkeypatch.setenv("HOME", "/home/alice")
    monkeypatch.setattr("sys.stdin", io.TextIOWrapper(io.BytesIO(source)))

    assert cli.main(["rewrite", "home", "expand"]) == 0
    assert capsysbinary.readouterr().out == source


def test_root_rewrite_home_preserves_byte_fidelity_around_matches(
    monkeypatch,
    capsysbinary,
) -> None:
    source = b"\xef\xbb\xbf first=~/one\r\nsecond=~/two\n  last=~/"
    expected = b"\xef\xbb\xbf first=/home/alice/one\r\nsecond=/home/alice/two\n  last=/home/alice/"
    monkeypatch.setenv("HOME", "/home/alice")
    monkeypatch.setattr("sys.stdin", io.TextIOWrapper(io.BytesIO(source)))

    assert cli.main(["rewrite", "home", "expand"]) == 0
    assert capsysbinary.readouterr().out == expected


@pytest.mark.parametrize("home", [None, "", "relative/home", "/", "///"])
def test_root_rewrite_home_invalid_home_fails_without_stdout(
    home: str | None,
    monkeypatch,
    capsysbinary,
) -> None:
    if home is None:
        monkeypatch.delenv("HOME", raising=False)
    else:
        monkeypatch.setenv("HOME", home)
    monkeypatch.setattr("sys.stdin", io.TextIOWrapper(io.BytesIO(b"~")))

    assert cli.main(["rewrite", "home", "expand"]) == 2
    captured = capsysbinary.readouterr()
    assert captured.out == b""
    assert b"$HOME must be a non-root absolute POSIX path" in captured.err
    assert b"Traceback" not in captured.err


def test_root_rewrite_home_invalid_utf8_is_atomic(
    monkeypatch,
    capsysbinary,
) -> None:
    monkeypatch.setenv("HOME", "/home/alice")
    monkeypatch.setattr("sys.stdin", io.TextIOWrapper(io.BytesIO(b"~/valid\nlate=\xff")))

    assert cli.main(["rewrite", "home", "expand"]) == 1
    captured = capsysbinary.readouterr()
    assert captured.out == b""
    assert b"input is not valid UTF-8" in captured.err
    assert b"Traceback" not in captured.err


def test_root_rewrite_home_missing_file_fails_without_stdout(
    tmp_path: Path,
    monkeypatch,
    capsysbinary,
) -> None:
    missing = tmp_path / "missing.txt"
    monkeypatch.setenv("HOME", "/home/alice")

    assert cli.main(["rewrite", "home", "collapse", str(missing)]) == 2
    captured = capsysbinary.readouterr()
    assert captured.out == b""
    assert str(missing).encode() in captured.err
    assert b"input file does not exist" in captured.err
    assert b"Traceback" not in captured.err


def test_root_rewrite_home_unreadable_file_fails_without_stdout(
    tmp_path: Path,
    monkeypatch,
    capsysbinary,
) -> None:
    source = tmp_path / "private.txt"
    source.write_bytes(b"~")
    monkeypatch.setenv("HOME", "/home/alice")

    def deny_read(path: Path) -> bytes:
        if path == source:
            raise PermissionError
        return b""

    monkeypatch.setattr(Path, "read_bytes", deny_read)

    assert cli.main(["rewrite", "home", "expand", str(source)]) == 1
    captured = capsysbinary.readouterr()
    assert captured.out == b""
    assert b"input file is not readable" in captured.err
    assert b"Traceback" not in captured.err


def test_root_rewrite_home_unreadable_stdin_fails_without_stdout(
    monkeypatch,
    capsysbinary,
) -> None:
    class UnreadableInput:
        def read(self) -> bytes:
            raise OSError("input/output error")

    monkeypatch.setenv("HOME", "/home/alice")
    monkeypatch.setattr("sys.stdin", SimpleNamespace(buffer=UnreadableInput()))

    assert cli.main(["rewrite", "home", "expand"]) == 1
    captured = capsysbinary.readouterr()
    assert captured.out == b""
    assert b"could not read stdin: input/output error" in captured.err
    assert b"Traceback" not in captured.err


@pytest.mark.parametrize("extra_argument", ["output.txt", "--stdout", "--in-place"])
def test_root_rewrite_home_has_no_output_or_in_place_operand(
    extra_argument: str,
    capsys,
) -> None:
    with pytest.raises(SystemExit) as error:
        cli.main(["rewrite", "home", "expand", "input.txt", extra_argument])

    assert error.value.code == 2
    assert "unrecognized arguments" in capsys.readouterr().err
