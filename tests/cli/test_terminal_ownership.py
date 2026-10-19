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
from pathlib import Path

import pytest

import dotman

# Linux PTY semantics, as in test_elevation_interrupt.py.
pytestmark = pytest.mark.skipif(sys.platform == "darwin", reason="Linux PTY lifecycle semantics")

WAIT_SECONDS = 8
CHILD_PATIENCE_SECONDS = 20  # children give up on their own if the test dies
SRC_DIR = Path(dotman.__file__).resolve().parent.parent

# Waits for `<name>-release`, announcing itself with `<name>-started` first.
PIPE_CHILD = """\
import pathlib, sys, time
markers = pathlib.Path(sys.argv[1])
(markers / "pipe-started").touch()
deadline = time.monotonic() + {patience}
while not (markers / "pipe-release").exists():
    if time.monotonic() > deadline:
        sys.exit(3)
    time.sleep(0.01)
"""

# A password prompt: ECHO off on the terminal until released, then put back.
TTY_CHILD = """\
import pathlib, sys, termios, time
markers = pathlib.Path(sys.argv[1])
saved = termios.tcgetattr(0)
held = termios.tcgetattr(0)
held[3] &= ~termios.ECHO
termios.tcsetattr(0, termios.TCSANOW, held)
(markers / "tty-ready").touch()
deadline = time.monotonic() + {patience}
while not (markers / "tty-release").exists():
    if time.monotonic() > deadline:
        sys.exit(3)
    time.sleep(0.01)
termios.tcsetattr(0, termios.TCSANOW, saved)
"""

# Runs one pipe and one tty command on separate threads, starting the one named
# first and waiting for its child to be underway before starting the other.
DRIVER = """\
import pathlib, sys, threading
from dotman.command_runtime import ArgvCommand, CommandRequest, ProductionCommandRuntime

markers = pathlib.Path(sys.argv[1])
first = sys.argv[2]
scripts = {{"pipe": sys.argv[3], "tty": sys.argv[4]}}
underway = {{"pipe": "pipe-started", "tty": "tty-ready"}}
runtime = ProductionCommandRuntime()

def run(kind):
    request = CommandRequest(
        ArgvCommand((sys.executable, scripts[kind], str(markers))), io=kind,
    )
    result = runtime.run(request)
    assert result.exit_code == 0, (kind, result.exit_code)
    (markers / f"{{kind}}-returned").touch()

threads = {{}}
for kind in (first, "tty" if first == "pipe" else "pipe"):
    threads[kind] = threading.Thread(target=run, args=(kind,))
    threads[kind].start()
    while not (markers / underway[kind]).exists():
        assert threads[kind].is_alive(), f"{{kind}} command ended before it was underway"
        threading.Event().wait(0.01)
for thread in threads.values():
    thread.join()
"""


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

    def echo_enabled(self) -> bool:
        return bool(termios.tcgetattr(self.slave)[3] & termios.ECHO)

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


@pytest.mark.parametrize(
    "case",
    ["pipe_snapshot_before_tty_hold", "pipe_snapshot_during_tty_hold"],
)
def test_pipe_command_leaves_terminal_modes_to_overlapping_tty_command(
    case: str, tmp_path: Path
) -> None:
    markers = tmp_path / "markers"
    markers.mkdir()
    pipe_script = tmp_path / "pipe_child.py"
    pipe_script.write_text(PIPE_CHILD.format(patience=CHILD_PATIENCE_SECONDS))
    tty_script = tmp_path / "tty_child.py"
    tty_script.write_text(TTY_CHILD.format(patience=CHILD_PATIENCE_SECONDS))
    driver_script = tmp_path / "driver.py"
    driver_script.write_text(DRIVER.format())

    terminal = _Pty()
    assert terminal.echo_enabled(), "fresh PTY should start with ECHO on"
    driver = subprocess.Popen(
        [
            sys.executable, str(driver_script), str(markers),
            "pipe" if case == "pipe_snapshot_before_tty_hold" else "tty",
            str(pipe_script), str(tty_script),
        ],
        stdin=terminal.slave, stdout=terminal.slave, stderr=terminal.slave,
        env={**os.environ, "PYTHONPATH": str(SRC_DIR)},
        start_new_session=True,
    )
    try:
        if case == "pipe_snapshot_before_tty_hold":
            # The pipe command captured ECHO on, then the tty command took the
            # terminal. The pipe command exits while the prompt is still up.
            _wait_for(markers / "tty-ready", terminal, driver)
            assert not terminal.echo_enabled(), "tty command should hold ECHO off"
            (markers / "pipe-release").touch()
            _wait_for(markers / "pipe-returned", terminal, driver)
            assert not terminal.echo_enabled(), (
                "pipe command restored its stale ECHO-on snapshot under the tty command's prompt"
            )
            (markers / "tty-release").touch()
            _wait_for(markers / "tty-returned", terminal, driver)
        else:
            # The pipe command captured ECHO off while the tty command held the
            # terminal. The tty command finishes first, then the pipe command exits.
            _wait_for(markers / "pipe-started", terminal, driver)
            (markers / "tty-release").touch()
            _wait_for(markers / "tty-returned", terminal, driver)
            assert terminal.echo_enabled(), "tty command should have restored ECHO"
            (markers / "pipe-release").touch()
            _wait_for(markers / "pipe-returned", terminal, driver)
            assert terminal.echo_enabled(), (
                "pipe command put the tty command's ECHO-off modes back after the tty command restored the terminal"
            )
        driver.wait(timeout=WAIT_SECONDS)
        assert driver.returncode == 0, terminal.output[-3000:].decode(errors="replace")
        assert terminal.echo_enabled()
    finally:
        # Let any child still waiting finish on its own, then reap the driver.
        (markers / "pipe-release").touch()
        (markers / "tty-release").touch()
        try:
            driver.wait(timeout=WAIT_SECONDS)
        except subprocess.TimeoutExpired:
            driver.kill()
            driver.wait()
        terminal.close()
