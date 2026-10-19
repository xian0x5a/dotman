"""A pipe command must not restore terminal modes across a foreground tty command (#291).

Real child processes run through ``ProductionCommandRuntime`` on a real PTY. The
tty command stands in for ``sudo -v``: it turns ECHO off while it holds the
terminal. Marker files sequence the overlap, so no timing is involved.
"""

from __future__ import annotations

import errno
import os
import pty
import select
import subprocess
import sys
import termios
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

import dotman

# Linux PTY semantics, as in test_elevation_interrupt.py.
pytestmark = pytest.mark.skipif(sys.platform == "darwin", reason="Linux PTY lifecycle semantics")

WAIT_SECONDS = 8
CHILD_PATIENCE_SECONDS = 20  # children give up on their own if the test dies
SRC_DIR = Path(dotman.__file__).resolve().parent.parent

# One child for every command. It applies its terminal action, announces
# `<name>-started`, waits for `<name>-release`, then exits. It never restores the
# modes it changed: a child dying mid-change is when dotman's own restore
# matters, and its cleanup would mask the runtime's behavior.
CHILD = """\
import pathlib, sys, termios, time
markers, name, action = pathlib.Path(sys.argv[1]), sys.argv[2], sys.argv[3]
if action != "none":  # a pipe command's stdin is not the terminal
    attributes = termios.tcgetattr(0)
    if action == "echo-off":  # a password prompt
        attributes[3] &= ~termios.ECHO
    elif action == "raw":  # a raw-mode editor keeps echo, drops line editing
        attributes[3] &= ~termios.ICANON
    termios.tcsetattr(0, termios.TCSANOW, attributes)
(markers / f"{{name}}-started").touch()
deadline = time.monotonic() + {patience}
while not (markers / f"{{name}}-release").exists():
    if time.monotonic() > deadline:
        sys.exit(3)
    time.sleep(0.01)
"""

# Runs the commands named on its command line (`name:io:action`) on separate
# threads, starting each one only once the previous child is underway.
DRIVER = """\
import pathlib, sys, threading
from dotman.command_runtime import ArgvCommand, CommandRequest, ProductionCommandRuntime

markers, child = pathlib.Path(sys.argv[1]), sys.argv[2]
runtime = ProductionCommandRuntime()

def run(name, io, action):
    request = CommandRequest(
        ArgvCommand((sys.executable, child, str(markers), name, action)), io=io,
    )
    result = runtime.run(request)
    assert result.exit_code == 0, (name, result.exit_code)
    (markers / f"{{name}}-returned").touch()

threads = []
for spec in sys.argv[3:]:
    name, io, action = spec.split(":")
    thread = threading.Thread(target=run, args=(name, io, action))
    threads.append(thread)
    thread.start()
    while not (markers / f"{{name}}-started").exists():
        assert thread.is_alive(), f"{{name}} ended before it was underway"
        threading.Event().wait(0.01)
for thread in threads:
    thread.join()
"""

# Terminal modes as (ECHO, ICANON).
ORIGINAL = (True, True)  # a fresh PTY
ECHO_OFF = (False, True)
RAW = (True, False)
ECHO_OFF_RAW = (False, False)


@dataclass(frozen=True)
class OverlapCase:
    # Commands in start order, as (name, io, action).
    commands: tuple[tuple[str, str, str], ...]
    # Commands in the order they are released; each return must leave these modes.
    releases: tuple[tuple[str, tuple[bool, bool]], ...]


CASES = {
    # The pipe command captured ECHO on, then the tty prompt took the terminal.
    "pipe_snapshot_before_tty_hold": OverlapCase(
        commands=(("pipe", "pipe", "none"), ("tty", "tty", "echo-off")),
        releases=(("pipe", ECHO_OFF), ("tty", ORIGINAL)),
    ),
    # The pipe command captured ECHO off during the prompt, which ends first.
    "pipe_snapshot_during_tty_hold": OverlapCase(
        commands=(("tty", "tty", "echo-off"), ("pipe", "pipe", "none")),
        releases=(("tty", ORIGINAL), ("pipe", ORIGINAL)),
    ),
    # An editor is killed while a password prompt on another thread still holds
    # the terminal; the prompt must keep its modes, and the terminal must then
    # return to what it was before the editor, not to the editor's modes.
    "tty_commands_first_exits_first": OverlapCase(
        commands=(("editor", "tty", "raw"), ("prompt", "tty", "echo-off")),
        releases=(("editor", ECHO_OFF_RAW), ("prompt", ORIGINAL)),
    ),
    "tty_commands_nested": OverlapCase(
        commands=(("editor", "tty", "raw"), ("prompt", "tty", "echo-off")),
        releases=(("prompt", RAW), ("editor", ORIGINAL)),
    ),
}


class _Pty:
    def __init__(self) -> None:
        self.master, self.slave = pty.openpty()
        self.output = bytearray()

    def drain(self) -> None:
        # Keep the master read so terminal writes and TCSADRAIN never block.
        if select.select([self.master], [], [], 0.01)[0]:
            try:
                self.output.extend(os.read(self.master, 65536))
            except OSError as exc:
                if exc.errno != errno.EIO:
                    raise

    def modes(self) -> tuple[bool, bool]:
        local_flags = termios.tcgetattr(self.slave)[3]
        return bool(local_flags & termios.ECHO), bool(local_flags & termios.ICANON)

    def close(self) -> None:
        os.close(self.master)
        os.close(self.slave)


def _wait_for(marker: Path, terminal: _Pty, driver: subprocess.Popen[bytes]) -> None:
    deadline = time.monotonic() + WAIT_SECONDS
    while not marker.exists():
        terminal.drain()
        assert driver.poll() is None or marker.exists(), (
            f"driver exited ({driver.returncode}) before {marker.name}\n"
            f"{terminal.output[-3000:].decode(errors='replace')}"
        )
        assert time.monotonic() < deadline, (
            f"timed out waiting for {marker.name}\n"
            f"{terminal.output[-3000:].decode(errors='replace')}"
        )


@pytest.mark.parametrize("case_name", CASES)
def test_terminal_modes_follow_foreground_tty_command_across_overlaps(
    case_name: str, tmp_path: Path
) -> None:
    case = CASES[case_name]
    markers = tmp_path / "markers"
    markers.mkdir()
    child_script = tmp_path / "child.py"
    child_script.write_text(CHILD.format(patience=CHILD_PATIENCE_SECONDS))
    driver_script = tmp_path / "driver.py"
    driver_script.write_text(DRIVER.format())

    terminal = _Pty()
    assert terminal.modes() == ORIGINAL, "fresh PTY should start with ECHO and ICANON on"
    driver = subprocess.Popen(
        [
            sys.executable, str(driver_script), str(markers), str(child_script),
            *(":".join(command) for command in case.commands),
        ],
        stdin=terminal.slave, stdout=terminal.slave, stderr=terminal.slave,
        env={**os.environ, "PYTHONPATH": str(SRC_DIR)},
        start_new_session=True,
    )
    try:
        for name, _, _ in case.commands:
            _wait_for(markers / f"{name}-started", terminal, driver)
        for name, expected in case.releases:
            (markers / f"{name}-release").touch()
            _wait_for(markers / f"{name}-returned", terminal, driver)
            assert terminal.modes() == expected, (
                f"after {name} returned, (ECHO, ICANON) should be {expected}"
                f" but are {terminal.modes()}"
            )
        driver.wait(timeout=WAIT_SECONDS)
        assert driver.returncode == 0, terminal.output[-3000:].decode(errors="replace")
    finally:
        # Let any child still waiting finish on its own, then reap the driver.
        for name, _, _ in case.commands:
            (markers / f"{name}-release").touch()
        try:
            driver.wait(timeout=WAIT_SECONDS)
        except subprocess.TimeoutExpired:
            driver.kill()
            driver.wait()
        terminal.close()
