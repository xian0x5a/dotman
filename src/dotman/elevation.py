from __future__ import annotations

import json
import os
import shutil
import socket
import stat
import struct
import sys
import tempfile
import threading
from contextlib import contextmanager
from contextvars import ContextVar, copy_context
from pathlib import Path
from typing import Iterator

from dotman.command_runtime import CommandOperation, command_operation
from dotman.file_access import request_sudo


BROKER_ENV = "DOTMAN_ELEVATION_BROKER"
REASON_ENV = "DOTMAN_ELEVATION_REASON"
REAL_SUDO_ENV = "DOTMAN_REAL_SUDO"


class ElevationBroker:
    def __init__(self) -> None:
        self._root = Path(tempfile.mkdtemp(prefix="dotman-elevation-"))
        self.socket_path = self._root / "broker.sock"
        self._shim_dir = self._root / "bin"
        self._server: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._request_lock = threading.Lock()
        self._connections_lock = threading.Lock()
        self._connections: dict[threading.Thread, socket.socket] = {}
        self._authenticating = False
        self._operation: CommandOperation | None = None
        self._expected_uid = os.getuid()
        self._real_sudo_path: str | None = None

    def start(self) -> None:
        if self._server is not None:
            return
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            server.bind(str(self.socket_path))
            server.listen(8)
        except Exception:
            server.close()
            raise
        self._server = server
        # ContextVars do not cross thread boundaries; broker authentication
        # must retain the command runtime active when this broker starts.
        with command_operation() as operation:
            self._operation = operation
            request_context = copy_context()
        self._thread = threading.Thread(
            target=request_context.run,
            args=(self._serve,),
            daemon=True,
        )
        self._thread.start()

    def close(self) -> None:
        if not self._root.exists():
            return
        with self._connections_lock:
            if threading.current_thread() in self._connections or threading.current_thread() is self._thread:
                raise RuntimeError("elevation broker cannot close from its own serving thread")
        self._stop_event.set()
        if self._thread is not None:
            self._wake_accept_loop()
            self._thread.join()
            self._thread = None
        if self._server is not None:
            self._server.close()
        self._server = None
        with self._connections_lock:
            connections = tuple(self._connections.items())
            # A command can exit while its client is still authenticating.
            # Cancel that operation rather than leave an orphan terminal prompt.
            if self._authenticating and self._operation is not None:
                self._operation.request_cancel()
        for _thread, connection in connections:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                # A handler may have closed its connection after the snapshot.
                pass
        for thread, _connection in connections:
            # A timed join would allow late terminal restoration after the
            # Command Deck resumes. Socket shutdown and runtime cancellation
            # unblock reads and authentication before this drain completes.
            thread.join()
        self.socket_path.unlink(missing_ok=True)
        for path in sorted(self._root.rglob("*"), reverse=True):
            if path.is_dir():
                path.rmdir()
            else:
                path.unlink(missing_ok=True)
        self._root.rmdir()

    def _wake_accept_loop(self) -> None:
        # Closing a listening socket does not interrupt a blocked accept() on
        # every platform, and polling it on a timeout made each broker close
        # wait up to a full tick. A throwaway connection returns accept() at
        # once; the serve loop sees the stop flag and drops it.
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as wake:
            wake.connect(str(self.socket_path))

    def env(self, *, reason: str | None = None) -> dict[str, str]:
        self.start()
        real_sudo = self._real_sudo_path or shutil.which("sudo")
        if real_sudo is None:
            raise ValueError("sudo is required for elevation intercept mode but was not found in PATH")
        self._real_sudo_path = real_sudo
        self._shim_dir.mkdir(exist_ok=True)
        shim_path = self._shim_dir / "sudo"
        if not shim_path.exists():
            shim_path.write_text(
                f"#!{sys.executable}\n{_SUDO_SHIM_BODY}",
                encoding="utf-8",
            )
            shim_path.chmod(shim_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        current_path = os.environ.get("PATH", "")
        return {
            BROKER_ENV: str(self.socket_path),
            REASON_ENV: reason or "perform privileged operation",
            REAL_SUDO_ENV: real_sudo,
            "PATH": f"{self._shim_dir}{os.pathsep}{current_path}",
        }

    def _serve(self) -> None:
        server = self._server
        assert server is not None
        while not self._stop_event.is_set():
            try:
                connection, _ = server.accept()
            except OSError:
                break
            # Each connection adds another thread boundary, so propagate the
            # broker thread's captured runtime context into its handler too.
            connection_context = copy_context()
            thread = threading.Thread(
                target=connection_context.run,
                args=(self._handle_connection, connection),
                daemon=True,
            )
            with self._connections_lock:
                if self._stop_event.is_set():
                    connection.close()
                    break
                self._connections[thread] = connection
                thread.start()

    def _handle_connection(self, connection: socket.socket) -> None:
        try:
            with connection:
                try:
                    self._validate_peer(connection)
                    payload = _read_json_payload(connection)
                    reason = payload.get("reason") if isinstance(payload, dict) else None
                    with self._request_lock:
                        with self._connections_lock:
                            if self._stop_event.is_set():
                                raise InterruptedError("elevation broker closed")
                            self._authenticating = True
                        try:
                            request_sudo(reason if isinstance(reason, str) and reason else None)
                        finally:
                            with self._connections_lock:
                                self._authenticating = False
                    response = {"ok": True}
                except (KeyboardInterrupt, InterruptedError):
                    response = {"ok": False, "error": "elevation interrupted", "interrupted": True}
                except Exception as exc:  # noqa: BLE001 - broker protocol must convert errors to structured replies.
                    response = {"ok": False, "error": str(exc)}
                try:
                    _write_json_payload(connection, response)
                except (BrokenPipeError, ConnectionResetError):
                    # Ctrl-C can stop the requester before authentication finishes
                    # unwinding; a disconnected client cannot receive its result.
                    pass
        finally:
            with self._connections_lock:
                self._connections.pop(threading.current_thread(), None)

    def _validate_peer(self, connection: socket.socket) -> None:
        if not hasattr(socket, "SO_PEERCRED"):
            return
        try:
            credentials = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
            _pid, uid, _gid = struct.unpack("3i", credentials)
        except OSError:
            return
        if uid != self._expected_uid:
            raise PermissionError("elevation broker rejected request from unexpected uid")


_SUDO_SHIM_BODY = """from __future__ import annotations

import json
import os
import socket
import sys

from dotman.command_runtime import INTERRUPTED_EXIT_CODE, ArgvCommand, CommandRequest, ProductionCommandRuntime, exit_from_interrupt, sigint_received

broker = os.environ.get("DOTMAN_ELEVATION_BROKER")
real_sudo = os.environ.get("DOTMAN_REAL_SUDO")
reason = os.environ.get("DOTMAN_ELEVATION_REASON") or "run sudo command"

if not broker:
    print("dotman sudo shim: DOTMAN_ELEVATION_BROKER is not set", file=sys.stderr)
    raise SystemExit(1)
if not real_sudo:
    print("dotman sudo shim: DOTMAN_REAL_SUDO is not set", file=sys.stderr)
    raise SystemExit(1)


def has_controlling_terminal() -> bool:
    try:
        os.close(os.open("/dev/tty", os.O_RDWR))
    except OSError:
        return False
    return True


def request_broker_lease() -> None:
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.connect(broker)
            client.sendall((json.dumps({"reason": reason}) + "\\n").encode("utf-8"))
            response = client.makefile("r", encoding="utf-8").readline()
    except KeyboardInterrupt:
        exit_from_interrupt()
    except OSError as exc:
        print(f"dotman sudo shim: elevation broker request failed: {exc}", file=sys.stderr)
        raise SystemExit(1)

    try:
        payload = json.loads(response)
    except json.JSONDecodeError:
        print("dotman sudo shim: elevation broker returned invalid response", file=sys.stderr)
        raise SystemExit(1)

    if payload.get("interrupted"):
        # SIGINT stopped dotman's own prompt, not this process.
        raise SystemExit(INTERRUPTED_EXIT_CODE)
    if not payload.get("ok"):
        print(f"dotman sudo shim: elevation broker denied request: {payload.get('error', 'unknown error')}", file=sys.stderr)
        raise SystemExit(1)


if has_controlling_terminal():
    request_broker_lease()
    sudo_arguments = ("-n", *sys.argv[1:])
else:
    # Without a terminal, sudo ties its ticket to the parent process, so dotman's
    # ticket cannot reach this call (#108). Let sudo authenticate it itself,
    # through SUDO_ASKPASS or a NOPASSWD rule.
    sudo_arguments = tuple(sys.argv[1:])

result = ProductionCommandRuntime().run(
    CommandRequest(
        command=ArgvCommand((real_sudo, *sudo_arguments)),
        io="tty",
    )
)
# sudo dying from SIGINT is passed on as one; a command that chose to exit 130 is not.
if result.exit_code == INTERRUPTED_EXIT_CODE and sigint_received():
    exit_from_interrupt()
raise SystemExit(result.exit_code)
"""


_ACTIVE_BROKER: ContextVar[ElevationBroker | None] = ContextVar(
    "dotman_elevation_broker", default=None,
)


def current_elevation_broker() -> ElevationBroker:
    broker = _ACTIVE_BROKER.get()
    if broker is None:
        raise RuntimeError("elevation broker requires an active command scope")
    return broker


@contextmanager
def elevation_broker_session() -> Iterator[None]:
    """Keep a command's broker in its own runtime and cancellation context."""
    broker = ElevationBroker()
    token = _ACTIVE_BROKER.set(broker)
    try:
        yield
    finally:
        try:
            broker.close()
        finally:
            _ACTIVE_BROKER.reset(token)


def _read_json_payload(connection: socket.socket) -> dict[str, object]:
    line = connection.makefile("r", encoding="utf-8").readline()
    if not line:
        raise ValueError("empty broker request")
    payload = json.loads(line)
    if not isinstance(payload, dict):
        raise ValueError("broker request must be a JSON object")
    return payload


def _write_json_payload(connection: socket.socket, payload: dict[str, object]) -> None:
    connection.sendall((json.dumps(payload) + "\n").encode("utf-8"))

