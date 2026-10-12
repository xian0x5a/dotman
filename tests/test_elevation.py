from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

from dotman import elevation, file_access
from dotman.command_runtime import CommandResult, MemoryCommandRuntime, command_runtime_session


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

        completed = subprocess.run(
            [shim_path, "true"],
            env={**os.environ, **shim_env},
            capture_output=True,
            text=True,
            check=False,
        )

        assert completed.returncode != 0
        assert "elevation broker request failed" in completed.stderr
    finally:
        broker.close()


def test_intercept_sudo_shim_normalizes_real_sudo_interruption(
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

        assert completed.returncode == 130
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
