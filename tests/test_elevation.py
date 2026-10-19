from __future__ import annotations

import json
import os
import pty
import select
import signal
import socket
import subprocess
import sys
import termios
import time
from pathlib import Path

import pytest

from dotman import elevation, file_access
from dotman.command_runtime import CommandResult, MemoryCommandRuntime, command_runtime_session
from tests.cli.test_elevation_interrupt import _controlling_terminal


def _request_broker(socket_path: Path, reason: str) -> dict[str, object]:
    # Speaks the same one-line JSON protocol as the intercept sudo shim.
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.connect(str(socket_path))
        client.sendall((json.dumps({"reason": reason}) + "\n").encode("utf-8"))
        return json.loads(client.makefile("r", encoding="utf-8").readline())


def test_elevation_broker_closes_without_waiting_for_a_poll_tick() -> None:
    # Every intercept command opens and closes a broker; guards run
    # several before the sync progress bar can appear.
    cycles = 5
    started = time.perf_counter()
    for _ in range(cycles):
        broker = elevation.ElevationBroker()
        broker.start()
        broker.close()
    elapsed = time.perf_counter() - started

    assert elapsed < 0.02 * cycles


def test_elevation_broker_request_requests_sudo(monkeypatch) -> None:
    broker = elevation.ElevationBroker()
    recorded_reasons: list[str | None] = []
    monkeypatch.setattr(elevation, "request_sudo", lambda reason=None: recorded_reasons.append(reason))

    try:
        broker.start()

        assert _request_broker(broker.socket_path, "install missing Arch packages") == {"ok": True}
        assert recorded_reasons == ["install missing Arch packages"]
    finally:
        broker.close()


def test_elevation_broker_preserves_runtime_in_request_thread(monkeypatch) -> None:
    broker = elevation.ElevationBroker()
    runtime = MemoryCommandRuntime([CommandResult(exit_code=0)])
    monkeypatch.setattr(file_access.os, "geteuid", lambda: 1000)

    try:
        with file_access.sudo_session(), command_runtime_session(runtime):
            broker.start()

            assert _request_broker(broker.socket_path, "install packages") == {"ok": True}

        assert [request.command.arguments for request in runtime.requests] == [
            ("sudo", "-n", "-v")
        ]
    finally:
        broker.close()


def test_privileged_file_access_stays_in_the_terminal_session(monkeypatch, tmp_path) -> None:
    # sudo's default tty-scoped ticket is invisible to a child started in a new
    # session, so a detached `sudo -n` fails with "a password is required".
    protected = tmp_path / "protected.conf"
    protected.write_bytes(b"old")
    protected.chmod(0)
    runtime = MemoryCommandRuntime([CommandResult(exit_code=0)] * 4)
    monkeypatch.setattr(file_access.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(file_access, "needs_sudo_for_write", lambda path: True)

    with file_access.sudo_session(), command_runtime_session(runtime):
        file_access.write_bytes_atomic(protected, b"new")
        file_access.read_bytes(protected)

    sudo_requests = [request for request in runtime.requests if request.command.arguments[0] == "sudo"]
    assert len(sudo_requests) == 4
    assert [request for request in sudo_requests if request.isolate_process_group] == []


def test_intercept_sudo_shim_fails_nonzero_when_broker_is_unreachable(monkeypatch) -> None:
    broker = elevation.ElevationBroker()
    monkeypatch.setattr(elevation.shutil, "which", lambda command: "/bin/true" if command == "sudo" else None)
    try:
        shim_env = broker.env(reason="legacy sudo command")
        shim_path = shim_env["PATH"].split(os.pathsep, 1)[0] + "/sudo"
        shim_env[elevation.BROKER_ENV] = f"{broker.socket_path}.missing"

        # The shim only asks the broker when it has a terminal; give it one so
        # the result does not depend on how the test runner was started.
        master, slave = pty.openpty()
        try:
            completed = subprocess.run(
                [shim_path, "true"],
                env={**os.environ, **shim_env},
                stdin=slave,
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
                preexec_fn=lambda: _controlling_terminal(slave),
            )
        finally:
            os.close(master)
            os.close(slave)

        assert completed.returncode != 0
        assert "elevation broker request failed" in completed.stderr
    finally:
        broker.close()


def test_intercept_sudo_shim_dies_from_sigint_when_real_sudo_is_interrupted(
    monkeypatch,
    tmp_path,
) -> None:
    interrupted_sudo = tmp_path / "interrupted-sudo"
    interrupted_sudo.write_text(
        f"#!{sys.executable}\nimport os\nimport signal\nos.kill(os.getpid(), signal.SIGINT)\n",
        encoding="utf-8",
    )
    interrupted_sudo.chmod(0o755)
    broker = elevation.ElevationBroker()
    monkeypatch.setattr(elevation, "request_sudo", lambda reason=None: None)
    monkeypatch.setattr(
        elevation.shutil,
        "which",
        lambda command: str(interrupted_sudo) if command == "sudo" else None,
    )
    try:
        shim_env = broker.env(reason="interrupted sudo command")
        shim_path = shim_env["PATH"].split(os.pathsep, 1)[0] + "/sudo"
        shim_env["PATH"] = f"{Path(shim_path).parent}{os.pathsep}/usr/bin{os.pathsep}/bin"

        completed = subprocess.run(
            [shim_path, "true"],
            env={**os.environ, **shim_env},
            capture_output=True,
            text=True,
            check=False,
        )

        assert completed.returncode == -signal.SIGINT
    finally:
        broker.close()


def test_hook_script_stops_when_ctrl_c_interrupts_sudo_through_the_intercept_shim(
    monkeypatch,
    tmp_path,
) -> None:
    # Ctrl-C reaches the whole foreground process group: the hook's shell, the
    # shim, and the real sudo. A POSIX shell only stops the script when its
    # child dies from SIGINT, so the shim must not turn that into exit 130.
    started_marker = tmp_path / "sudo-started"
    after_marker = tmp_path / "after"
    real_sudo = tmp_path / "real-sudo"
    real_sudo.write_text(f'#!/bin/sh\n: > "{started_marker}"\nexec "$@"\n', encoding="utf-8")
    real_sudo.chmod(0o755)
    broker = elevation.ElevationBroker()
    monkeypatch.setattr(elevation, "request_sudo", lambda reason=None: None)
    monkeypatch.setattr(
        elevation.shutil,
        "which",
        lambda command: str(real_sudo) if command == "sudo" else None,
    )
    try:
        shim_env = broker.env(reason="interrupted hook")
        # A new session has no controlling terminal, so the shim skips the
        # broker and runs the real sudo directly; it also keeps the SIGINT
        # below away from the test runner's process group.
        hook = subprocess.Popen(
            ["/bin/sh", "-c", f'sudo sleep 30; echo AFTER > "{after_marker}"'],
            env={**os.environ, **shim_env},
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            deadline = time.monotonic() + 30
            while not started_marker.exists():
                assert hook.poll() is None, "hook exited before sudo started"
                assert time.monotonic() < deadline, "sudo never started"
                time.sleep(0.01)
            os.killpg(hook.pid, signal.SIGINT)
            hook.wait(timeout=10)
        finally:
            if hook.poll() is None:
                os.killpg(hook.pid, signal.SIGKILL)
                hook.wait(timeout=10)

        assert not after_marker.exists()
        assert hook.returncode == -signal.SIGINT
    finally:
        broker.close()


def test_concurrent_sudo_requests_prompt_for_a_password_once(monkeypatch) -> None:
    import threading
    from contextvars import copy_context

    ticket = threading.Event()
    prompting = threading.Event()

    def sudo(request):
        if request.io == "tty":
            prompting.set()
            # Hold the prompt open so the second request arrives mid-prompt.
            time.sleep(0.2)
            ticket.set()
            return CommandResult(exit_code=0)
        return CommandResult(exit_code=0 if ticket.is_set() else 1)

    runtime = MemoryCommandRuntime([sudo] * 4)
    monkeypatch.setattr(file_access.os, "geteuid", lambda: 1000)
    with file_access.sudo_session(), command_runtime_session(runtime):
        first = threading.Thread(target=copy_context().run, args=(file_access.request_sudo,))
        first.start()
        assert prompting.wait(5)
        second = threading.Thread(target=copy_context().run, args=(file_access.request_sudo,))
        second.start()
        first.join(5)
        second.join(5)

    assert [request.io for request in runtime.requests].count("tty") == 1


_KEEPALIVE_CHILD_SCRIPT = """
import sys
import time
from pathlib import Path

from dotman import file_access

# Tick right away so one is in flight whenever the test changes the terminal.
file_access._SUDO_KEEPALIVE_INTERVAL_SECONDS = 0.01
finished = Path(sys.argv[1])
with file_access.sudo_session():
    file_access.request_sudo("keepalive test")
    deadline = time.monotonic() + 30
    while not finished.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
"""


@pytest.mark.skipif(os.geteuid() == 0, reason="request_sudo does nothing as root, so no keepalive runs")
def test_sudo_keepalive_tick_does_not_undo_terminal_mode_changes_made_while_it_runs(tmp_path) -> None:
    # A tty hook (password prompt, full-screen tool) owns the terminal during
    # the session; a keepalive tick in flight must not put old modes back (#226).
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    tick_started = tmp_path / "tick-started"
    tick_ended = tmp_path / "tick-ended"
    finished = tmp_path / "finished"
    fake_sudo = fake_bin / "sudo"
    fake_sudo.write_text(
        "#!/bin/sh\n"
        'if [ "$2" = "-v" ]; then exit 0; fi\n'
        f': > "{tick_started}"\n'
        "sleep 0.5\n"
        f': > "{tick_ended}"\n',
        encoding="utf-8",
    )
    fake_sudo.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(tmp_path / "home"),
        "XDG_CONFIG_HOME": str(tmp_path / "xdg-config"),
        "XDG_STATE_HOME": str(tmp_path / "xdg-state"),
        "XDG_DATA_HOME": str(tmp_path / "xdg-data"),
    }

    def wait_for(path: Path, process: subprocess.Popen) -> None:
        deadline = time.monotonic() + 15
        while not path.exists():
            # Keep the pty drained so the child never blocks on its output.
            if select.select([master], [], [], 0.01)[0]:
                os.read(master, 65536)
            assert process.poll() is None, f"child exited before {path.name}"
            assert time.monotonic() < deadline, f"timed out waiting for {path.name}"

    master, slave = pty.openpty()
    process = subprocess.Popen(
        [sys.executable, "-c", _KEEPALIVE_CHILD_SCRIPT, str(finished)],
        stdin=slave, stdout=slave, stderr=slave, env=env,
    )
    try:
        wait_for(tick_started, process)
        # Stand in for the foreground changing modes: turn off echo on the pty.
        modes = termios.tcgetattr(master)
        modes[3] &= ~termios.ECHO
        termios.tcsetattr(master, termios.TCSANOW, modes)
        wait_for(tick_ended, process)
        # The tick restores the terminal after its child exits.
        time.sleep(0.3)
        echo_after_tick = termios.tcgetattr(master)[3] & termios.ECHO
        assert echo_after_tick == 0, "keepalive tick restored the terminal's old ECHO setting"
    finally:
        finished.touch()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        os.close(master)
        os.close(slave)
