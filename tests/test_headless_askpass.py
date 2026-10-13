"""sudo -A for SUDO_ASKPASS in runs without a terminal (issue #114)."""

from __future__ import annotations

import json
import os
import pty
import subprocess
import sys
from pathlib import Path

import pytest

from tests.cli.test_elevation_interrupt import _controlling_terminal

PROCESS_SECONDS = 30
ASKPASS = "/usr/lib/askpass"


def _recording_sudo(path: Path, argv_log: Path) -> Path:
    # Records its argv; fails the non-interactive ticket probe so dotman logs in.
    path.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\n"
        f"with open({str(argv_log)!r}, 'a') as log:\n"
        "    log.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "sys.exit(1 if '-n' in sys.argv[1:] and '-v' in sys.argv[1:] else 0)\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def _recorded_calls(argv_log: Path) -> list[list[str]]:
    return [json.loads(line) for line in argv_log.read_text().splitlines()] if argv_log.exists() else []


def _environment(askpass: str | None, **overrides: str) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if key != "SUDO_ASKPASS"}
    env.update(overrides)
    if askpass is not None:
        env["SUDO_ASKPASS"] = askpass
    return env


def _run(argv: list[str], env: dict[str, str], *, with_terminal: bool) -> subprocess.CompletedProcess[str]:
    if not with_terminal:
        # A new session has no controlling terminal, like cron or ssh without -t.
        return subprocess.run(
            argv, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True,
            check=False, timeout=PROCESS_SECONDS, start_new_session=True,
        )
    master, slave = pty.openpty()
    try:
        return subprocess.run(
            argv, env=env, stdin=slave, capture_output=True, text=True,
            check=False, timeout=PROCESS_SECONDS, preexec_fn=lambda: _controlling_terminal(slave),
        )
    finally:
        os.close(master)
        os.close(slave)


@pytest.mark.skipif(os.geteuid() == 0, reason="root does not require sudo authentication")
@pytest.mark.parametrize(
    ("with_terminal", "askpass", "expected_login"),
    [
        pytest.param(False, ASKPASS, ["-A", "-v"], id="headless-askpass"),
        pytest.param(False, None, ["-v"], id="headless-no-askpass"),
        pytest.param(False, "", ["-v"], id="headless-empty-askpass"),
        pytest.param(True, ASKPASS, ["-v"], id="terminal-askpass"),
    ],
)
def test_sudo_login_uses_askpass_flag_only_headless(tmp_path, with_terminal, askpass, expected_login) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    argv_log = tmp_path / "sudo-argv.jsonl"
    _recording_sudo(bin_dir / "sudo", argv_log)
    env = _environment(askpass, PATH=f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")

    completed = _run(
        [sys.executable, "-c", "from dotman.file_access import request_sudo; request_sudo('publish files')"],
        env, with_terminal=with_terminal,
    )

    assert completed.returncode == 0, completed.stderr
    # Only the password login may prompt; the `-n` ticket probe never needs the helper.
    assert _recorded_calls(argv_log) == [["-n", "-v"], expected_login]
