"""Real process interruption across the Sync CLI and terminal lifecycle."""

from __future__ import annotations

import json
import os
import pty
import select
import shutil
import signal
import subprocess
import sys
import termios
import time

import pytest

from dotman.operation_lock import OperationLock
from tests.engine.test_sync_both_convergence import established


def wait_until(predicate, process, read_output=lambda: None):
    deadline = time.monotonic() + 5
    while not predicate():
        assert process.poll() is None or predicate(), "Sync exited before reaching the interruption boundary"
        assert time.monotonic() < deadline, "Sync did not reach the interruption boundary"
        read_output()
        time.sleep(0.01)


@pytest.mark.parametrize("keys,title", [
    (b"", b"Merge"),
    (b"\r", b"Proposal Review"),
    (b" c", b"Confirmation"),
], ids=["workset", "review", "confirmation"])
def test_ctrl_c_with_sync_base_exits_cleanly(tmp_path, monkeypatch, keys, title):
    established(tmp_path, monkeypatch)
    source = tmp_path / "repo/packages/app/unit"
    live = tmp_path / "live/unit"
    before = source.read_bytes(), live.read_bytes()
    master, slave = pty.openpty()
    terminal_before = termios.tcgetattr(slave)
    process = subprocess.Popen(
        [sys.executable, "-m", "dotman.cli", "--config", str(tmp_path / "config.toml"), "sync"],
        stdin=slave, stdout=slave, stderr=slave, start_new_session=True,
        env={**os.environ, "TERM": "xterm-256color"},
    )
    output = bytearray()

    def read_output():
        if select.select([master], [], [], 0.01)[0]:
            output.extend(os.read(master, 65536))

    try:
        wait_until(lambda: b"Merge" in output, process, read_output)
        if keys:
            if keys == b" c":
                os.write(master, b" ")
                wait_until(lambda: b"[x]" in output, process, read_output)
                os.write(master, b"c")
            else:
                os.write(master, keys)
            wait_until(lambda: title in output, process, read_output)
        # Textual's raw terminal receives the real Ctrl-C byte as a key event.
        os.write(master, b"\x03")
        wait_until(lambda: process.poll() is not None, process, read_output)
        while select.select([master], [], [], 0.05)[0]:
            read_output()
        assert b"Traceback" not in output, output[-2000:].decode(errors="replace")
        assert b"Task exception" not in output
        assert process.returncode == 130
        assert termios.tcgetattr(slave) == terminal_before
        assert b"\x1b[?1049l" in output  # Leave the alternate screen.
        assert b"\x1b[?25h" in output  # Restore cursor visibility.
        assert (source.read_bytes(), live.read_bytes()) == before
        with OperationLock.acquire(tmp_path / "state/dotman"):
            pass
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
        os.close(master)
        os.close(slave)


def test_sigint_with_sync_base_emits_clean_json_and_stops_execution(tmp_path, monkeypatch):
    ready, later = tmp_path / "ready", tmp_path / "later"
    established(tmp_path, monkeypatch, (
        '[targets.unit.hooks]\n'
        f'pre_pull = "touch {ready}; sleep 2; touch {later}"'
    ))
    source = tmp_path / "repo/packages/app/unit"
    live = tmp_path / "live/unit"
    before = source.read_bytes(), live.read_bytes()
    process = subprocess.Popen(
        [sys.executable, "-m", "dotman.cli", "--config", str(tmp_path / "config.toml"),
         "--json", "--no-confirm", "sync"],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        wait_until(ready.exists, process)
        process.send_signal(signal.SIGINT)
        stdout, stderr = process.communicate(timeout=5)
        assert b"Traceback" not in stderr, stderr.decode()
        assert stderr == b""
        assert process.returncode == 130
        payload = json.loads(stdout)
        assert payload["sync_units"][0]["base"]["status"] == "usable"
        assert (source.read_bytes(), live.read_bytes()) == before
        assert not later.exists()
        with OperationLock.acquire(tmp_path / "state/dotman"):
            pass
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)


@pytest.mark.parametrize("stage", ["capture", "merge", "render"])
@pytest.mark.parametrize("interruption", ["key", "signal"])
@pytest.mark.parametrize("action", [b"\r", b"a"], ids=["review", "batch"])
def test_materialization_remains_animated_and_interruptible(
    tmp_path, monkeypatch, stage, interruption, action,
):
    _assert_materialization_interrupt(tmp_path, monkeypatch, stage, interruption, action)


@pytest.mark.parametrize("interruption", ["key", "signal"])
@pytest.mark.parametrize("release_order", ["before", "after"])
def test_materialization_completion_races_cancellation(
    tmp_path, monkeypatch, interruption, release_order,
):
    _assert_materialization_interrupt(
        tmp_path, monkeypatch, "render", interruption, b"a", release_order,
    )


def _assert_materialization_interrupt(
    tmp_path, monkeypatch, stage, interruption, action, release_order=None,
):
    """Gate a real provider, not the UI: progress and cancellation must stay live."""
    ready = tmp_path / "provider-ready"
    release = tmp_path / "release-provider"
    later = tmp_path / "later-provider"
    provider = tmp_path / "provider"
    completed = tmp_path / "completed-provider"
    temporary_source = tmp_path / "temporary-source"
    real_git = shutil.which("git")
    assert real_git
    provider.write_text(
        f"#!{sys.executable}\n"
        "import os, pathlib, subprocess, sys, time\n"
        f"stage = {stage!r}\n"
        "called = 'merge' if pathlib.Path(sys.argv[0]).name == 'git' else sys.argv[1]\n"
        "if called == 'merge' and sys.argv[1:2] != ['merge-file']:\n"
        f"    os.execv({real_git!r}, [{real_git!r}, *sys.argv[1:]])\n"
        "if called == stage:\n"
        "    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "    if called == 'render':\n"
        f"        pathlib.Path({str(temporary_source)!r}).write_text(os.environ['DOTMAN_SOURCE'])\n"
        f"    pathlib.Path({str(ready)!r}).write_text(str(os.getpid()) + ' ' + str(child.pid))\n"
        f"    while not pathlib.Path({str(release)!r}).exists(): time.sleep(0.01)\n"
        "    child.terminate()\n"
        "    child.wait(timeout=2)\n"
        f"    pathlib.Path({str(completed)!r}).touch()\n"
        "elif (stage == 'capture' and called in ('merge', 'render')) or (stage == 'merge' and called == 'render'):\n"
        f"    pathlib.Path({str(later)!r}).touch()\n"
        "if called == 'merge':\n"
        f"    os.execv({real_git!r}, [{real_git!r}, *sys.argv[1:]])\n"
        "source = os.environ['DOTMAN_LIVE_PATH' if called == 'capture' else 'DOTMAN_SOURCE']\n"
        "sys.stdout.buffer.write(pathlib.Path(source).read_bytes())\n"
    )
    provider.chmod(0o755)
    established(
        tmp_path, monkeypatch,
        f'capture = "{provider} capture"\nrender = "{provider} render"\n'
        'compare = { repo = "raw", live = "raw" }',
    )
    # Install the merge wrapper only after fixture repository initialization.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "git").symlink_to(provider)
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ["PATH"])
    if action == b"a":
        package = tmp_path / "repo/packages/app/package.toml"
        with package.open("a") as config:
            config.write(
                '\n[targets.zz_later]\nsource = "zz_later"\n'
                f'path = "{tmp_path / "live/zz_later"}"\ntype = "file"\nsync_policy = "pull-only"\n'
                f'capture = "touch {later}; cat $DOTMAN_LIVE_PATH"\n'
                'compare = { repo = "raw", live = "raw" }\n'
            )
        (tmp_path / "repo/packages/app/zz_later").write_bytes(b"repo")
        (tmp_path / "live/zz_later").write_bytes(b"live")
    endpoints = [
        path for root in (tmp_path / "repo/packages/app", tmp_path / "live")
        for path in root.iterdir() if path.name != "package.toml"
    ]
    before = {path: path.read_bytes() for path in endpoints}
    master, slave = pty.openpty()
    terminal_before = termios.tcgetattr(slave)
    process = subprocess.Popen(
        [sys.executable, "-m", "dotman.cli", "--config", str(tmp_path / "config.toml"), "sync"],
        stdin=slave, stdout=slave, stderr=slave, start_new_session=True,
        env={**os.environ, "TERM": "xterm-256color"},
    )
    output = bytearray()

    def read_output():
        if select.select([master], [], [], 0.01)[0]:
            output.extend(os.read(master, 65536))

    def stopped(pid):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        # A reparented child may await init's reap, but must no longer execute.
        from pathlib import Path
        stat = Path(f"/proc/{pid}/stat")
        return stat.exists() and stat.read_text().split(") ", 1)[1].startswith("Z")

    try:
        wait_until(lambda: b"Merge" in output, process, read_output)
        os.write(master, action)
        wait_until(ready.exists, process, read_output)
        progress_start = len(output)
        # Distinct dots frames prove animation, not merely a static busy label.
        def animated():
            progress = output[progress_start:].decode(errors="replace")
            return (
                "Materializing Proposal" in output.decode(errors="replace")
                and len(set(progress) & set("⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏")) >= 2
            )
        wait_until(animated, process, read_output)
        assert not release.exists() and not later.exists()
        if stage == "render":
            from pathlib import Path
            assert Path(temporary_source.read_text()).is_file()
        # Release adjacent to cancellation, with no readiness wait between them:
        # either the provider completion or cancellation may win this boundary.
        if release_order == "before":
            release.touch()
        if interruption == "key":
            os.write(master, b"\x03\x03")
        else:
            process.send_signal(signal.SIGINT)
        if release_order == "after":
            release.touch()
        wait_until(lambda: process.poll() is not None, process, read_output)
        while select.select([master], [], [], 0.05)[0]:
            read_output()
        assert process.returncode == 130, output[-3000:].decode(errors="replace")
        assert b"Traceback" not in output
        assert b"Task exception" not in output
        assert b"never awaited" not in output
        assert termios.tcgetattr(slave) == terminal_before
        assert b"\x1b[?1049l" in output and b"\x1b[?25h" in output
        assert {path: path.read_bytes() for path in endpoints} == before
        assert not later.exists()
        if release_order is None:
            assert not completed.exists()
        if temporary_source.exists():
            from pathlib import Path
            assert not Path(temporary_source.read_text()).parent.exists()
        assert all(stopped(int(pid)) for pid in ready.read_text().split())
        with OperationLock.acquire(tmp_path / "state/dotman"):
            pass
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
        if ready.exists():
            for pid in ready.read_text().split():
                try:
                    os.kill(int(pid), signal.SIGKILL)
                except ProcessLookupError:
                    pass
        os.close(master)
        os.close(slave)
