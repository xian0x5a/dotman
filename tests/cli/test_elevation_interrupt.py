"""Cancellation of a broker-backed sudo password prompt through the public CLI."""

from __future__ import annotations

import errno
import fcntl
import json
import os
import pty
import select
import signal
import subprocess
import sys
import termios
import time
from pathlib import Path

import pytest

from tests.engine.test_sync_session import make_engine

# These PTY lifecycle tests rely on Linux /proc, AF_UNIX path lengths, and
# pty/process-group semantics that macOS does not share.
pytestmark = pytest.mark.skipif(sys.platform == "darwin", reason="Linux PTY lifecycle semantics")

WAIT_SECONDS = 8


def _controlling_terminal(slave: int) -> None:
    # setsid alone leaves the PTY slave *without* a controlling terminal. The
    # foreground group's Ctrl-C delivery is the contract under test here.
    os.setsid()
    fcntl.ioctl(slave, termios.TIOCSCTTY, 0)
    assert os.tcgetpgrp(slave) == os.getpgrp()


def _read_available(master: int, output: bytearray) -> None:
    if select.select([master], [], [], 0.01)[0]:
        try:
            output.extend(os.read(master, 65536))
        except OSError as exc:
            if exc.errno != errno.EIO:  # Linux PTYs report EIO after slave closure.
                raise


def _wait_for(predicate, process: subprocess.Popen, master: int, output: bytearray) -> None:
    deadline = time.monotonic() + WAIT_SECONDS
    while not predicate():
        _read_available(master, output)
        assert time.monotonic() < deadline, output[-3000:].decode(errors="replace")
        assert process.poll() is None or predicate(), output[-3000:].decode(errors="replace")


def _stopped(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    # Adopted children can briefly remain zombies until their new parent reaps.
    try:
        return Path(f"/proc/{pid}/stat").read_text().split(") ", 1)[1].startswith("Z")
    except FileNotFoundError:
        return True


def _fake_sudo(
    bin_dir: Path, ready: Path, *, release: Path | None = None, child_success: Path | None = None,
) -> None:
    bin_dir.mkdir()
    fake_sudo = bin_dir / "sudo"
    fake_sudo.write_text(
        f"#!{sys.executable}\n"
        "import os, pathlib, signal, sys, termios, time\n"
        f"release = {str(release) if release else None!r}\n"
        f"child_success = {str(child_success) if child_success else None!r}\n"
        # The intercept shim prefixes -n even when the hook already supplied it.
        "if sys.argv[1:] in (['-n', 'true'], ['-n', '-n', 'true']):\n"
        "    assert child_success and pathlib.Path(release).exists()\n"
        "    pathlib.Path(child_success).touch()\n"
        "    sys.exit(0)\n"
        "assert sys.argv[1:] == ['-v'], sys.argv\n"
        "assert os.isatty(0) and os.tcgetpgrp(0) == os.getpgrp()\n"
        "signal.signal(signal.SIGINT, lambda *_: sys.exit(130))\n"
        "original = termios.tcgetattr(0)\n"
        "prompt = original.copy()\n"
        "prompt[3] &= ~termios.ECHO\n"
        "termios.tcsetattr(0, termios.TCSANOW, prompt)\n"
        f"pathlib.Path({str(ready)!r}).write_text(str(os.getpid()))\n"
        "sys.stderr.write('Password: ')\n"
        "sys.stderr.flush()\n"
        "while not (release and pathlib.Path(release).exists()): time.sleep(0.01)\n"
        "termios.tcsetattr(0, termios.TCSANOW, original)\n"
    )
    fake_sudo.chmod(0o755)


def _provider(
    executable: Path, provider_pid: Path, client_pid: Path,
    child_command: tuple[str, ...] = ("sudo", "-n", "true"),
) -> None:
    executable.write_text(
        f"#!{sys.executable}\n"
        "import os, pathlib, signal, subprocess, sys\n"
        "signal.signal(signal.SIGINT, lambda *_: sys.exit(130))\n"
        f"pathlib.Path({str(provider_pid)!r}).write_text(str(os.getpid()))\n"
        f"child = subprocess.Popen({child_command!r})\n"
        f"pathlib.Path({str(client_pid)!r}).write_text(str(child.pid))\n"
        "sys.exit(child.wait())\n"
    )
    executable.chmod(0o755)


def _isolated_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bin_dir: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config-home"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ["PATH"])


def _kill_owned(
    process: subprocess.Popen | None, paths: tuple[Path, ...], previous_pids: tuple[int, ...] = (),
) -> None:
    if process is not None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=WAIT_SECONDS)
    owned = list(previous_pids)
    for path in paths:
        if path.exists():
            owned.extend(int(text) for text in path.read_text().split())
    for pid in owned:
        if not _stopped(pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


@pytest.mark.parametrize("action", ["ctrl_c", "sigint", "prompt_sigint", "success"])
def test_cli_broker_password_prompt_result_and_terminal_lifecycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, action: str,
) -> None:
    if os.geteuid() == 0:
        pytest.skip("root does not require sudo authentication")
    ready = tmp_path / "prompt-ready"
    provider_pid = tmp_path / "provider-pid"
    client_pid = tmp_path / "client-pid"
    post_push = tmp_path / "post-push"
    release = tmp_path / "release-password-prompt"
    child_success = tmp_path / "intercept-child-succeeded"
    bin_dir = tmp_path / "bin"
    _fake_sudo(bin_dir, ready, release=release if action == "success" else None,
               child_success=child_success if action == "success" else None)
    _isolated_environment(tmp_path, monkeypatch, bin_dir)
    child_command = ("sudo", "-n", "true")
    provider = tmp_path / "hook-provider"
    _provider(provider, provider_pid, client_pid, child_command)
    engine = make_engine(tmp_path, monkeypatch, [
        ("unit", "push-only", b"repo", b"repo",
         "[targets.unit.hooks]\npre_push = { run = "
         + json.dumps(str(provider)) + ', io = "pipe", elevation = "intercept" }\n'
         + f'post_push = "touch {post_push}"'),
    ])
    source = tmp_path / "repo/packages/app/unit"
    live = tmp_path / "live/unit"
    before = (source.read_bytes(), live.read_bytes())
    master, slave = pty.openpty()
    terminal_before = termios.tcgetattr(slave)
    process: subprocess.Popen | None = None
    output = bytearray()
    try:
        process = subprocess.Popen(
            [sys.executable, "-m", "dotman.cli", "--config", str(engine.config.config_path), "push", "--run-noop"],
            stdin=slave, stdout=slave, stderr=slave,
            preexec_fn=lambda: _controlling_terminal(slave),
            env={**os.environ, "TERM": "xterm-256color"},
        )
        # Keep the initially selected hook-only workset and confirm execution.
        _wait_for(lambda: b"Hook Work" in output, process, master, output)
        os.write(master, b"c")
        _wait_for(lambda: b"Confirmation" in output, process, master, output)
        os.write(master, b"\r")
        _wait_for(
            lambda: all(path.exists() and path.read_text() for path in (ready, client_pid, provider_pid)),
            process, master, output,
        )
        assert not post_push.exists()
        prompt = int(ready.read_text())
        client = int(client_pid.read_text())
        provider_process = int(provider_pid.read_text())
        assert all(not _stopped(pid) for pid in (prompt, client, provider_process))
        assert not (termios.tcgetattr(slave)[3] & termios.ECHO)
        if action == "ctrl_c":
            os.write(master, b"\x03")
        elif action == "sigint":
            process.send_signal(signal.SIGINT)
        elif action == "prompt_sigint":
            # Only sudo receives SIGINT: the broker reply must be interrupted, not auth failure.
            os.kill(prompt, signal.SIGINT)
        else:
            release.touch()
        _wait_for(lambda: process.poll() is not None, process, master, output)
        for _ in range(5):
            _read_available(master, output)
        assert process.returncode == (0 if action == "success" else 130), output.decode(errors="replace")
        assert (source.read_bytes(), live.read_bytes()) == before
        assert post_push.exists() == (action == "success"), output.decode(errors="replace")
        assert child_success.exists() == (action == "success")
        _wait_for(lambda: all(_stopped(pid) for pid in (prompt, client, provider_process)), process, master, output)
        assert termios.tcgetattr(slave) == terminal_before, output.decode(errors="replace")
        assert b"Traceback" not in output, output.decode(errors="replace")
    finally:
        _kill_owned(process, (ready, client_pid, provider_pid))
        os.close(master)
        os.close(slave)


@pytest.mark.parametrize("interruption", ["ctrl_c", "sigint"])
def test_broker_password_prompt_in_tty_editor_cancels_only_editor_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, interruption: str,
) -> None:
    if os.geteuid() == 0:
        pytest.skip("root does not require sudo authentication")
    ready = tmp_path / "prompt-ready"
    provider_pid = tmp_path / "editor-provider-pid"
    shim_pid = tmp_path / "shim-pid"
    bin_dir = tmp_path / "bin"
    _fake_sudo(bin_dir, ready)
    _isolated_environment(tmp_path, monkeypatch, bin_dir)
    editor = tmp_path / "tty-editor"
    _provider(editor, provider_pid, shim_pid)
    engine = make_engine(tmp_path, monkeypatch, [
        ("unit", "push-only", b"repo", b"live",
         "editor = { run = " + json.dumps(str(editor))
         + ', io = "tty", elevation = "intercept" }'),
    ])
    source = tmp_path / "repo/packages/app/unit"
    live = tmp_path / "live/unit"
    before = (source.read_bytes(), live.read_bytes())
    master, slave = pty.openpty()
    terminal_before = termios.tcgetattr(slave)
    process: subprocess.Popen | None = None
    previous_pids: tuple[int, ...] = ()
    output = bytearray()
    try:
        process = subprocess.Popen(
            [sys.executable, "-m", "dotman.cli", "--config", str(engine.config.config_path), "sync"],
            stdin=slave, stdout=slave, stderr=slave,
            preexec_fn=lambda: _controlling_terminal(slave),
            env={**os.environ, "TERM": "xterm-256color"},
        )
        _wait_for(lambda: b"Use repository" in output, process, master, output)
        os.write(master, b" ")
        _wait_for(lambda: b"[x]" in output, process, master, output)
        os.write(master, b"e")
        _wait_for(
            lambda: all(path.exists() and path.read_text() for path in (ready, shim_pid, provider_pid)),
            process, master, output,
        )
        prompt = int(ready.read_text())
        shim = int(shim_pid.read_text())
        editor_process = int(provider_pid.read_text())
        assert all(not _stopped(pid) for pid in (prompt, shim, editor_process))
        assert not (termios.tcgetattr(slave)[3] & termios.ECHO)
        if interruption == "ctrl_c":
            os.write(master, b"\x03")
        else:
            process.send_signal(signal.SIGINT)
        _wait_for(lambda: b"Editor cancelled" in output, process, master, output)
        assert process.poll() is None, output.decode(errors="replace")
        assert b"previous Proposal preserved" in output, output.decode(errors="replace")
        assert (source.read_bytes(), live.read_bytes()) == before
        _wait_for(lambda: all(_stopped(pid) for pid in (prompt, shim, editor_process)), process, master, output)
        assert not (termios.tcgetattr(slave)[3] & termios.ICANON), output.decode(errors="replace")
        # A cancelled Editor attempt must not poison the next attempt's broker.
        previous_pids = (prompt, shim, editor_process)
        for marker in (ready, shim_pid, provider_pid):
            marker.write_text("")
        os.write(master, b"e")
        _wait_for(
            lambda: all(path.read_text() for path in (ready, shim_pid, provider_pid)),
            process, master, output,
        )
        second_pids = (int(ready.read_text()), int(shim_pid.read_text()), int(provider_pid.read_text()))
        assert all(not _stopped(pid) for pid in second_pids)
        assert second_pids[0] != prompt
        assert not (termios.tcgetattr(slave)[3] & termios.ECHO)
        second_cancellation = len(output)
        if interruption == "ctrl_c":
            os.write(master, b"\x03")
        else:
            process.send_signal(signal.SIGINT)
        # The first attempt's notice can be repainted while starting the next
        # Editor. Require a new alternate-screen entry, not that stale notice.
        _wait_for(
            lambda: b"\x1b[?1049h" in output[second_cancellation:]
            and b"Editor cancelled" in output[second_cancellation:],
            process, master, output,
        )
        assert process.poll() is None, output.decode(errors="replace")
        assert (source.read_bytes(), live.read_bytes()) == before
        _wait_for(lambda: all(_stopped(pid) for pid in second_pids), process, master, output)
        assert not (termios.tcgetattr(slave)[3] & termios.ICANON), output.decode(errors="replace")
        os.write(master, b"c")
        _wait_for(lambda: b"approved:" in output, process, master, output)
        os.write(master, b"\x03")
        _wait_for(lambda: process.poll() is not None, process, master, output)
        for _ in range(5):
            _read_available(master, output)
        assert process.returncode == 130, output.decode(errors="replace")
        assert (source.read_bytes(), live.read_bytes()) == before
        assert termios.tcgetattr(slave) == terminal_before, output.decode(errors="replace")
        assert b"Traceback" not in output, output.decode(errors="replace")
    finally:
        _kill_owned(process, (ready, shim_pid, provider_pid), previous_pids)
        os.close(master)
        os.close(slave)