"""Intercept hook sudo without a controlling terminal (issue #108)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.cli.test_elevation_interrupt import _isolated_environment, _kill_owned
from tests.engine.test_sync_session import make_engine

pytestmark = pytest.mark.skipif(os.geteuid() == 0, reason="root does not require sudo authentication")

RUN_SECONDS = 60

_NO_TERMINAL_MESSAGE = (
    "sudo: a terminal is required to read the password; either use the -S option "
    "to read from standard input or configure an askpass helper"
)


def _fake_askpass(path: Path, calls: Path) -> None:
    path.write_text(f"#!/bin/sh\necho call >> {calls}\necho fake-password\n")
    path.chmod(0o755)


def _fake_ttyless_sudo(bin_dir: Path, tickets: Path, invocations: Path) -> None:
    # Without a terminal, real sudo's timestamp_type=tty falls back to tickets
    # owned by the parent process: authenticating in one process never helps a
    # sudo started by a different parent. Emulate that, plus askpass prompting.
    bin_dir.mkdir()
    fake_sudo = bin_dir / "sudo"
    fake_sudo.write_text(
        f"#!{sys.executable}\n"
        "import json, os, pathlib, subprocess, sys\n"
        f"tickets = pathlib.Path({str(tickets)!r})\n"
        f"invocations = pathlib.Path({str(invocations)!r})\n"
        f"no_terminal_message = {_NO_TERMINAL_MESSAGE!r}\n"
        "args = sys.argv[1:]\n"
        "flags = set()\n"
        "while args and args[0].startswith('-'):\n"
        "    flags.add(args.pop(0))\n"
        "unsupported = flags - {'-n', '-v', '-E', '-A'}\n"
        "if unsupported:\n"
        "    sys.exit(f'fake sudo: unsupported flags {sorted(unsupported)}')\n"
        "parent = os.getppid()\n"
        "with invocations.open('a') as log:\n"
        "    log.write(json.dumps({'argv': sys.argv[1:], 'ppid': parent}) + '\\n')\n"
        "owners = set(tickets.read_text().split()) if tickets.exists() else set()\n"
        "if '-n' in flags:\n"
        "    if str(parent) not in owners:\n"
        "        sys.stderr.write('sudo: a password is required\\n')\n"
        "        sys.exit(1)\n"
        "else:\n"
        "    askpass = os.environ.get('SUDO_ASKPASS')\n"
        "    if not askpass:\n"
        "        sys.stderr.write(no_terminal_message + '\\n')\n"
        "        sys.exit(1)\n"
        "    if subprocess.run([askpass, '[sudo] password:'], stdout=subprocess.PIPE).returncode != 0:\n"
        "        sys.exit(1)\n"
        "    with tickets.open('a') as ticket_log:\n"
        "        ticket_log.write(f'{parent}\\n')\n"
        "if '-v' in flags:\n"
        "    sys.exit(0)\n"
        "if not args:\n"
        "    sys.exit('fake sudo: no command')\n"
        "sys.exit(subprocess.run(args).returncode)\n"
    )
    fake_sudo.chmod(0o755)


def test_ttyless_intercept_hook_sudo_authenticates_itself_through_askpass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    invocations = tmp_path / "sudo-invocations.jsonl"
    askpass_calls = tmp_path / "askpass-calls"
    askpass = tmp_path / "askpass"
    post_push = tmp_path / "post-push"
    bin_dir = tmp_path / "bin"
    _fake_ttyless_sudo(bin_dir, tmp_path / "sudo-ticket-owners", invocations)
    _fake_askpass(askpass, askpass_calls)
    _isolated_environment(tmp_path, monkeypatch, bin_dir)
    monkeypatch.setenv("SUDO_ASKPASS", str(askpass))
    hook = "sudo sh -c 'echo first-elevated'; sudo sh -c 'echo second-elevated'"
    engine = make_engine(tmp_path, monkeypatch, [
        ("unit", "push-only", b"repo", b"repo",
         "[targets.unit.hooks]\npre_push = { run = " + json.dumps(hook)
         + ', io = "pipe", elevation = "intercept" }\n'
         + f'post_push = "touch {post_push}"'),
    ])

    # A new session with stdin closed has no controlling terminal, like cron or an agent harness.
    process = subprocess.Popen(
        [sys.executable, "-m", "dotman.cli", "--config", str(engine.config.config_path),
         "--no-confirm", "push", "--run-noop"],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=RUN_SECONDS)
    finally:
        _kill_owned(process, ())

    askpass_count = len(askpass_calls.read_text().splitlines()) if askpass_calls.exists() else 0
    diagnostics = (
        f"exit={process.returncode}\nstdout:\n{stdout}\nstderr:\n{stderr}\n"
        f"askpass calls: {askpass_count}\n"
        f"sudo invocations:\n{invocations.read_text() if invocations.exists() else ''}"
    )
    # Compare whole lines: the hook's command text is also echoed in the output.
    lines = [line.strip() for line in stdout.splitlines()]
    assert process.returncode == 0, diagnostics
    assert post_push.exists(), diagnostics
    assert "first-elevated" in lines and "second-elevated" in lines, diagnostics
    # One prompt per hook sudo call: dotman must not prompt on the shim's behalf.
    assert askpass_count == 2, diagnostics
