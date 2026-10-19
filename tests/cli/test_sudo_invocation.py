"""Running dotman itself under sudo is refused (issue #171)."""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from dotman.cli import main

from tests.helpers import write_manager_config


@pytest.mark.parametrize(
    ("euid", "sudo_user", "refused"),
    [
        pytest.param(0, "alice", True, id="root-started-by-sudo"),
        pytest.param(0, None, False, id="plain-root-login"),
        pytest.param(0, "root", False, id="root-calling-sudo"),
        pytest.param(1000, "alice", False, id="user-started-by-sudo-u"),
    ],
)
def test_repo_commands_refuse_only_root_started_by_sudo(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    euid: int,
    sudo_user: str | None,
    refused: bool,
) -> None:
    config_path = write_manager_config(tmp_path)
    monkeypatch.setattr("os.geteuid", lambda: euid)
    if sudo_user is None:
        monkeypatch.delenv("SUDO_USER", raising=False)
    else:
        monkeypatch.setenv("SUDO_USER", sudo_user)

    exit_code = main(["--config", str(config_path), "--json", "doctor"])

    error_output = capsys.readouterr().err
    if refused:
        assert exit_code == 2
        assert "Traceback" not in error_output
        assert "under sudo" in error_output
        assert "alice" in error_output
    else:
        assert exit_code == 0
        assert "under sudo" not in error_output


def test_standalone_helpers_still_run_under_sudo(
    monkeypatch: pytest.MonkeyPatch,
    capsysbinary: pytest.CaptureFixture[bytes],
) -> None:
    # Hooks with `elevation = "root"` may pipe through standalone helpers such
    # as `dotman rewrite`; they touch no repo or state, so sudo is harmless.
    monkeypatch.setattr("os.geteuid", lambda: 0)
    monkeypatch.setenv("SUDO_USER", "alice")
    monkeypatch.setenv("HOME", "/home/alice")
    monkeypatch.setattr("sys.stdin", io.TextIOWrapper(io.BytesIO(b"~/x")))

    assert main(["rewrite", "home", "expand", "-"]) == 0
    assert capsysbinary.readouterr().out == b"/home/alice/x"
