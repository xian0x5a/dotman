from __future__ import annotations

import atexit
import os
import shlex
import sys
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from dotman.atomic_files import create_directories_durably
from dotman.atomic_files import write_bytes_atomic as atomic_write_bytes_atomic
from dotman.atomic_files import write_symlink_atomic as atomic_write_symlink_atomic
from dotman.command_runtime import (
    ArgvCommand,
    CommandRequest,
    CommandResult,
    CommandRuntime,
    current_command_runtime,
    raise_for_command_interruption,
)

from dotman.interaction_policy import no_confirm_enabled
from dotman.terminal import colors_enabled

_SUDO_KEEPALIVE_INTERVAL_SECONDS = 30
_PRIVILEGED_HELPER_MODULE = "dotman.privileged_ops"


def _sudo_request(*sudo_arguments: str, **request_options) -> CommandRequest:
    # sudo's default tty-scoped ticket is invisible to a child started in a new
    # session, so every sudo call stays in the invoking terminal session.
    return CommandRequest(
        command=ArgvCommand(("sudo", *sudo_arguments)),
        isolate_process_group=False,
        **request_options,
    )


def _askpass_arguments() -> tuple[str, ...]:
    # Without a terminal, sudo only falls back to SUDO_ASKPASS when a display
    # is set (#114). `-A` makes it use the helper anyway, so a helper that needs
    # no display also works in headless runs such as cron.
    if os.environ.get("SUDO_ASKPASS") and not _has_controlling_terminal():
        return ("-A",)
    return ()


def _has_controlling_terminal() -> bool:
    # The same check sudo makes before it prompts on a terminal.
    try:
        os.close(os.open("/dev/tty", os.O_RDWR))
    except OSError:
        return False
    return True


class _SudoLease:
    def __init__(self, runtime: CommandRuntime | None = None) -> None:
        self._runtime = runtime if runtime is not None else current_command_runtime()
        self._stop_event = threading.Event()
        self._keepalive_thread: threading.Thread | None = None
        self._acquired = False

    def request(self, reason: str | None = None) -> None:
        if os.geteuid() == 0:
            return
        # `-n -v` never prompts: it succeeds only while sudo's ticket (or a
        # NOPASSWD rule) already grants root, so no password notice is due.
        probe = self._runtime.run(_sudo_request("-n", "-v"))
        raise_for_command_interruption(probe)
        ticket_valid = probe.exit_code == 0
        if ticket_valid and self._acquired:
            self._ensure_keepalive()
            return
        self.close()
        if not ticket_valid:
            # No-confirm suppresses dotman's decisions, not sudo authentication:
            # sudo authenticates however it is configured (terminal, askpass).
            _emit_sudo_notice(reason, password_required=True)
            result = self._runtime.run(_sudo_request(*_askpass_arguments(), "-v", io="tty"))
            raise_for_command_interruption(result)
            if result.exit_code != 0:
                raise PermissionError("sudo authentication failed")
        elif not no_confirm_enabled():
            _emit_sudo_notice(reason, password_required=False)
        self._acquired = True
        self._ensure_keepalive()

    def _ensure_keepalive(self) -> None:
        if self._keepalive_thread is None or not self._keepalive_thread.is_alive():
            # The thread keeps its own stop event: close() waits only briefly and
            # then installs a fresh one, which a slow tick must not pick up.
            self._keepalive_thread = threading.Thread(
                target=self._keepalive_loop, args=(self._stop_event,), daemon=True,
            )
            self._keepalive_thread.start()

    def _keepalive_loop(self, stop_event: threading.Event) -> None:
        while not stop_event.wait(_SUDO_KEEPALIVE_INTERVAL_SECONDS):
            self._runtime.run(_sudo_request("-n", "true"))

    def close(self) -> None:
        if not self._acquired:
            return
        self._stop_event.set()
        if self._keepalive_thread is not None:
            self._keepalive_thread.join(timeout=1)
        self._acquired = False
        self._keepalive_thread = None
        self._stop_event = threading.Event()


_active_sudo_lease: _SudoLease | None = None
# Planning reads protected endpoints from worker threads. Without one owner,
# each would find no ticket and prompt for a password on the same terminal.
_sudo_request_lock = threading.Lock()
_sudo_lease_depth = 0
_sudo_atexit_registered = False



def _current_sudo_lease() -> _SudoLease:
    global _active_sudo_lease, _sudo_atexit_registered
    if _active_sudo_lease is None:
        _active_sudo_lease = _SudoLease()
        if not _sudo_atexit_registered:
            atexit.register(_cleanup_active_sudo_lease)
            _sudo_atexit_registered = True
    return _active_sudo_lease



def _cleanup_active_sudo_lease() -> None:
    global _active_sudo_lease
    if _active_sudo_lease is None:
        return
    _active_sudo_lease.close()
    _active_sudo_lease = None


@contextmanager
def sudo_session() -> Iterator[None]:
    global _sudo_lease_depth
    _sudo_lease_depth += 1
    try:
        yield
    finally:
        _sudo_lease_depth -= 1
        if _sudo_lease_depth == 0:
            _cleanup_active_sudo_lease()



def _emit_sudo_notice(reason: str | None, *, password_required: bool) -> None:
    from dotman import cli_style

    detail = reason or "perform privileged operation"
    use_color = colors_enabled(sys.stderr)
    badge = cli_style.render_sudo_badge(use_color=use_color)
    print(f"{badge} {'password required to ' if password_required else ''}{detail}", file=sys.stderr)



def request_sudo(reason: str | None = None) -> None:
    with _sudo_request_lock:
        try:
            _current_sudo_lease().request(reason)
        finally:
            # A keepalive without an operation scope would retain the runtime that
            # happened to request elevation and leak it into later operations.
            if _sudo_lease_depth == 0:
                _cleanup_active_sudo_lease()



def needs_sudo_for_read(path: Path) -> bool:
    try:
        path.stat()
    except FileNotFoundError:
        return False
    except PermissionError:
        return True
    return not os.access(path, os.R_OK)



def needs_sudo_for_write(path: Path) -> bool:
    parent = path.parent
    while True:
        try:
            parent.stat()
            break
        except FileNotFoundError:
            if parent == parent.parent:
                return False
            parent = parent.parent
        except PermissionError:
            return True
    return not os.access(parent, os.W_OK | os.X_OK)



def sudo_prefix_command(command: str) -> str:
    return f"sudo -n -E /bin/sh -c {shlex.quote(command)}"


def _run_privileged_operation(*args: str, input: bytes | None = None) -> CommandResult:
    result = current_command_runtime().run(
        _sudo_request("-n", sys.executable, "-m", _PRIVILEGED_HELPER_MODULE, *args, input=input)
    )
    raise_for_command_interruption(result)
    return result



def read_bytes(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except PermissionError:
        request_sudo(f"read protected path: {path}")
        result = current_command_runtime().run(_sudo_request("-n", "/bin/cat", str(path)))
        raise_for_command_interruption(result)
        if result.exit_code == 0:
            return result.stdout
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        raise PermissionError(stderr or f"permission denied for {path}")



def write_bytes_atomic(path: Path, content: bytes, *, mode: int | None = None) -> None:
    if not needs_sudo_for_write(path):
        try:
            atomic_write_bytes_atomic(path, content, mode=mode)
            return
        except PermissionError:
            # Fall back to sudo if direct path check was too optimistic.
            pass

    request_sudo(f"write protected path: {path}")
    mode_args = () if mode is None else (str(mode),)
    result = _run_privileged_operation("write-bytes-atomic", str(path), *mode_args, input=content)
    if result.exit_code != 0:
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        raise PermissionError(stderr or f"permission denied for {path}")



def write_symlink_atomic(path: Path, target: str | Path) -> None:
    if not needs_sudo_for_write(path):
        try:
            atomic_write_symlink_atomic(path, target)
            return
        except PermissionError:
            # Fall back to sudo if direct path check was too optimistic.
            pass

    request_sudo(f"write protected path: {path}")
    result = _run_privileged_operation("write-symlink-atomic", str(path), str(target))
    if result.exit_code != 0:
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        raise PermissionError(stderr or f"permission denied for {path}")



def delete_path_and_prune_empty_parents(path: Path, *, root: Path) -> None:
    try:
        if path.exists() or path.is_symlink():
            path.unlink()
    except PermissionError:
        request_sudo(f"delete protected path: {path}")
        result = _run_privileged_operation("delete-path-and-prune-empty-parents", str(path), str(root))
        if result.exit_code != 0:
            stderr = result.stderr.decode("utf-8", errors="replace").strip()
            raise PermissionError(stderr or f"permission denied for {path}")
        return

    prune_root = root if root.is_dir() else root.parent
    current = path.parent
    while current.exists() and current != prune_root and current != current.parent:
        try:
            current.rmdir()
        except OSError:
            break
        current = current.parent



def make_directories(path: Path) -> None:
    try:
        create_directories_durably(path)
    except PermissionError:
        request_sudo(f"create protected directory: {path}")
        result = _run_privileged_operation("make-directories", str(path))
        if result.exit_code != 0:
            stderr = result.stderr.decode("utf-8", errors="replace").strip()
            raise PermissionError(stderr or f"permission denied for {path}")



def chmod(path: Path, mode: int) -> None:
    if not path.exists():
        return
    try:
        os.chmod(path, mode)
    except PermissionError:
        request_sudo(f"change mode on protected path: {path}")
        result = _run_privileged_operation("chmod", str(path), str(mode))
        if result.exit_code != 0:
            stderr = result.stderr.decode("utf-8", errors="replace").strip()
            raise PermissionError(stderr or f"permission denied for {path}")


__all__ = [
    "chmod",
    "delete_path_and_prune_empty_parents",
    "make_directories",
    "needs_sudo_for_read",
    "needs_sudo_for_write",
    "read_bytes",
    "request_sudo",
    "sudo_prefix_command",
    "sudo_session",
    "write_bytes_atomic",
    "write_symlink_atomic",
]


def remove_empty_directory_tree(path: Path) -> None:
    """Remove structural directories only; never recursively remove payloads."""
    if path.is_symlink():
        raise ValueError(f"Cannot prune a directory symlink: {path}")
    for child in sorted(path.iterdir()):
        if child.is_dir() and not child.is_symlink():
            remove_empty_directory_tree(child)
        else:
            raise ValueError(f"Structural directory contains an unmanaged entry: {child}")
    path.rmdir()
