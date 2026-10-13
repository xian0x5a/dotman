from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar, copy_context
from dataclasses import dataclass, field
from pathlib import Path
from threading import Thread
from types import MappingProxyType
from typing import TYPE_CHECKING, Callable, Iterable, Iterator, Literal, Mapping, Protocol, Sequence, TextIO, TypeAlias, TypeVar

from dotman.terminal import preserve_terminal_state

if TYPE_CHECKING:
    # Annotation-only: loading models costs every standalone helper call ~16ms.
    from dotman.models import ElevationMode


INTERRUPTED_EXIT_CODE = 130
_INTERRUPT_GRACE_SECONDS = 0.5
_CANCELLATION_POLL_SECONDS = 0.05
_EXIT_POLL_SECONDS = 0.005
# Planning children are mostly CPU-bound interpreter startups (`uv run`, nested
# `dotman transform`), so at most one worker per core avoids oversubscribing.
# Eight captures most of the overlap: a 24-core plan measured ~940ms at eight
# workers versus ~850ms at 24, and the cap keeps big machines from bursting wide.
PLANNING_COMMAND_CONCURRENCY = min(8, os.cpu_count() or 1)

_T = TypeVar("_T")


@dataclass(frozen=True)
class ShellCommand:
    source: str

    def __post_init__(self) -> None:
        if not self.source:
            raise ValueError("shell command must not be empty")


@dataclass(frozen=True)
class ArgvCommand:
    arguments: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.arguments:
            raise ValueError("argument command must not be empty")


Command: TypeAlias = ShellCommand | ArgvCommand
CommandIO: TypeAlias = Literal["pipe", "tty"]


@dataclass(frozen=True)
class CommandRequest:
    command: Command
    cwd: Path | None = None
    env: Mapping[str, str] = field(default_factory=dict)
    excluded_env_keys: frozenset[str] = frozenset()
    io: CommandIO = "pipe"
    input: bytes | None = None
    stream_output: bool = False
    stdout_sink: TextIO | None = field(default=None, compare=False, repr=False)
    stderr_sink: TextIO | None = field(default=None, compare=False, repr=False)
    elevation: ElevationMode = "none"
    isolate_process_group: bool = True

    def __post_init__(self) -> None:
        if self.io not in {"pipe", "tty"}:
            raise ValueError(f"unsupported command I/O mode '{self.io}'")
        if self.io == "tty" and self.input is not None:
            raise ValueError("tty command input must come from the terminal")
        if self.io == "tty" and self.stream_output:
            raise ValueError("tty command output belongs to the terminal")
        object.__setattr__(self, "env", MappingProxyType(dict(self.env)))


@dataclass(frozen=True)
class CommandResult:
    exit_code: int
    stdout: bytes = b""
    stderr: bytes = b""

    @property
    def stdout_text(self) -> str:
        return self.stdout.decode("utf-8")

    @property
    def stderr_text(self) -> str:
        return self.stderr.decode("utf-8")


def first_output_line(stderr: str, stdout: str) -> str | None:
    """Summarize a failed user command by its first non-empty stderr line, else stdout."""
    for output in (stderr, stdout):
        for line in output.splitlines():
            stripped = line.strip()
            if stripped:
                return stripped
    return None


def raise_for_command_interruption(result: CommandResult) -> None:
    if result.exit_code == INTERRUPTED_EXIT_CODE:
        raise KeyboardInterrupt


class CommandRuntime(Protocol):
    def request_cancel(self) -> None: ...

    def check_cancelled(self) -> None: ...

    def run(self, request: CommandRequest) -> CommandResult: ...


class CommandElevation(Protocol):
    def prepare(
        self,
        command: Command,
        env: Mapping[str, str],
        mode: ElevationMode,
    ) -> tuple[Command, dict[str, str]]: ...


@dataclass(frozen=True)
class SystemCommandElevation:
    def prepare(
        self,
        command: Command,
        env: Mapping[str, str],
        mode: ElevationMode,
    ) -> tuple[Command, dict[str, str]]:
        prepared_env = dict(env)
        if mode == "none":
            return command, prepared_env

        # Imports stay local because the privileged file helpers also execute
        # argv commands through this module. Elevation itself remains owned here.
        from dotman.file_access import request_sudo, sudo_prefix_command

        if mode == "root":
            if os.geteuid() != 0:
                request_sudo("run privileged command")
                if isinstance(command, ShellCommand):
                    command = ShellCommand(sudo_prefix_command(command.source))
                else:
                    command = ArgvCommand(("sudo", "-n", "-E", *command.arguments))
            return command, prepared_env
        if mode == "intercept":
            from dotman.elevation import current_elevation_broker

            broker_env = current_elevation_broker().env(reason="run privileged command")
            if "PATH" in prepared_env:
                # Command-specific PATH remains authoritative after the injected shim.
                shim_dir = broker_env["PATH"].split(os.pathsep, 1)[0]
                broker_env["PATH"] = f"{shim_dir}{os.pathsep}{prepared_env['PATH']}"
            return command, {**prepared_env, **broker_env}
        raise ValueError(f"unsupported elevation mode '{mode}'")


@dataclass
class CommandOperation:
    """Cancellation identity shared by an operation and all copied execution contexts."""

    _cancelled: threading.Event = field(default_factory=threading.Event, init=False, repr=False)

    def request_cancel(self) -> None:
        self._cancelled.set()

    def check_cancelled(self) -> None:
        if self._cancelled.is_set():
            raise InterruptedError("command operation cancelled")


_ACTIVE_OPERATION: ContextVar[CommandOperation | None] = ContextVar(
    "dotman_command_operation", default=None,
)


@contextmanager
def command_operation(operation: CommandOperation | None = None) -> Iterator[CommandOperation]:
    """Activate an operation; nested commands share its never-reset latch.

    Context tokens belong only to this lexical activation, not the session's
    lifetime. A session may dispatch from a copied context or another thread.
    """
    operation = operation or _ACTIVE_OPERATION.get() or CommandOperation()
    token = _ACTIVE_OPERATION.set(operation)
    try:
        yield operation
    finally:
        _ACTIVE_OPERATION.reset(token)


class _CancellationLatch:
    def request_cancel(self) -> None:
        """Cancel the active operation, not future uses of this runtime."""
        operation = _ACTIVE_OPERATION.get()
        if operation is not None:
            operation.request_cancel()

    def check_cancelled(self) -> None:
        operation = _ACTIVE_OPERATION.get()
        if operation is not None:
            operation.check_cancelled()


@dataclass
class ProductionCommandRuntime(_CancellationLatch):
    elevation: CommandElevation = field(default_factory=SystemCommandElevation)

    @command_operation()
    def run(self, request: CommandRequest) -> CommandResult:
        self.check_cancelled()
        with ExitStack() as elevation_scope:
            if request.elevation == "intercept":
                from dotman.elevation import elevation_broker_session

                elevation_scope.enter_context(elevation_broker_session())
            try:
                command, request_env = self.elevation.prepare(
                    request.command,
                    request.env,
                    request.elevation,
                )
                environment = {
                    **{
                        key: value
                        for key, value in os.environ.items()
                        if key not in request.excluded_env_keys
                    },
                    **request_env,
                }
                if request.io == "tty":
                    return self._run_with_terminal(request=request, command=command, environment=environment)
                return self._run_with_pipes(request=request, command=command, environment=environment)
            finally:
                # Prompt handlers may still be restoring the terminal after
                # their requesting command exits. Drain them before the caller
                # resumes its UI, even if another Ctrl-C arrives during cleanup.
                with _defer_repeated_interrupts():
                    elevation_scope.close()

    def _run_with_pipes(
        self,
        *,
        request: CommandRequest,
        command: Command,
        environment: dict[str, str],
    ) -> CommandResult:
        stdout_buffer: list[bytes] = []
        stderr_buffer: list[bytes] = []
        stdout_sink = request.stdout_sink or sys.stdout
        stderr_sink = request.stderr_sink or sys.stderr

        def pump(stream, buffer: list[bytes], sink: TextIO) -> None:
            try:
                for chunk in iter(stream.readline, b""):
                    buffer.append(chunk)
                    if request.stream_output:
                        _write_streamed_chunk(chunk, sink)
            finally:
                stream.close()

        def write_input(stream, content: bytes) -> None:
            try:
                stream.write(content)
            except BrokenPipeError:
                pass
            finally:
                stream.close()

        with preserve_terminal_state():
            # Elevated commands stay in the invoking terminal session so sudo's
            # tty-scoped lease remains usable. Ordinary pipe commands own a new
            # process group so interruption can stop the complete shell tree.
            owns_process_group = request.elevation == "none" and request.isolate_process_group
            self.check_cancelled()
            process: subprocess.Popen[bytes] | None = None
            stream_threads: list[tuple[Thread, object]] = []
            try:
                process = subprocess.Popen(
                    _process_arguments(command),
                    **_process_command_options(command),
                    cwd=str(request.cwd) if request.cwd is not None else None,
                    env=environment,
                    stdin=subprocess.PIPE if request.input is not None else subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    start_new_session=owns_process_group,
                )
                stdout_thread = Thread(
                    target=pump,
                    args=(process.stdout, stdout_buffer, stdout_sink),
                    daemon=True,
                )
                stderr_thread = Thread(
                    target=pump,
                    args=(process.stderr, stderr_buffer, stderr_sink),
                    daemon=True,
                )
                stream_threads.extend(((stdout_thread, process.stdout), (stderr_thread, process.stderr)))
                if request.input is not None:
                    if process.stdin is None:
                        raise AssertionError("pipe command stdin was not created")
                    input_thread = Thread(
                        target=write_input,
                        args=(process.stdin, request.input),
                        daemon=True,
                    )
                    stream_threads.append((input_thread, process.stdin))
                for thread, _stream in stream_threads:
                    thread.start()
                return_code = self._wait(process)
                # A shell may exit while its descendants still hold the pipes.
                # Continue observing cancellation until all owned I/O completes.
                for thread, _stream in stream_threads:
                    while thread.is_alive():
                        self.check_cancelled()
                        thread.join(_CANCELLATION_POLL_SECONDS)
                self.check_cancelled()
            except (KeyboardInterrupt, InterruptedError):
                self.request_cancel()
                with _defer_repeated_interrupts():
                    if process is not None:
                        _stop_process(process, owns_process_group=owns_process_group)
                        deadline = time.monotonic() + _INTERRUPT_GRACE_SECONDS
                        for thread, _stream in stream_threads:
                            if thread.ident is not None:
                                thread.join(max(0, deadline - time.monotonic()))
                        # A startup interruption can leave streams without a
                        # pump. Close only those: closing a live pump's buffered
                        # stream can block on its read lock indefinitely.
                        pumped_streams = {
                            stream for thread, stream in stream_threads if thread.ident is not None
                        }
                        for stream in (process.stdin, process.stdout, process.stderr):
                            if stream is not None and stream not in pumped_streams:
                                stream.close()
                raise
        return CommandResult(
            exit_code=_normalize_return_code(return_code),
            stdout=b"".join(stdout_buffer),
            stderr=b"".join(stderr_buffer),
        )

    def _run_with_terminal(
        self,
        *,
        request: CommandRequest,
        command: Command,
        environment: dict[str, str],
    ) -> CommandResult:
        with preserve_terminal_state():
            previous_sigint_handler = signal.getsignal(signal.SIGINT)
            main_thread = threading.current_thread() is threading.main_thread()
            process: subprocess.Popen[bytes] | None = None
            self.check_cancelled()
            try:
                process = subprocess.Popen(
                    _process_arguments(command),
                    **_process_command_options(command),
                    cwd=str(request.cwd) if request.cwd is not None else None,
                    env=environment,
                )
                # The foreground child owns Ctrl-C. Signal handlers are global,
                # so worker-thread commands must leave them untouched.
                if main_thread:
                    signal.signal(signal.SIGINT, signal.SIG_IGN)
                return_code = self._wait(process)
            except (KeyboardInterrupt, InterruptedError):
                self.request_cancel()
                with _defer_repeated_interrupts():
                    if process is not None:
                        _stop_process(process, owns_process_group=False)
                raise
            finally:
                if main_thread:
                    signal.signal(signal.SIGINT, previous_sigint_handler)
        return CommandResult(exit_code=_normalize_return_code(return_code))

    def _wait(self, process: subprocess.Popen[bytes]) -> int:
        # Popen.wait(timeout) restarts its backoff on every call, so a loop of
        # them noticed exit up to ~18ms late on every command. A short fixed
        # poll bounds both exit and cancellation latency.
        while (return_code := process.poll()) is None:
            self.check_cancelled()
            time.sleep(_EXIT_POLL_SECONDS)
        self.check_cancelled()
        return return_code


def run_ordered(tasks: Sequence[Callable[[], _T]]) -> Iterator[_T]:
    """Run independent tasks on a bounded pool; yield their results in task order.

    Tasks share the caller's command operation, so cancelling it stops every
    running command. The first failure in task order propagates; tasks that
    have not started never start.
    """
    workers = min(PLANNING_COMMAND_CONCURRENCY, len(tasks))
    if workers <= 1:
        for task in tasks:
            yield task()
        return
    operation = _ACTIVE_OPERATION.get() or CommandOperation()
    executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="dotman-planning")
    try:
        futures = [executor.submit(_in_operation_context(operation, task)) for task in tasks]
        for future in futures:
            yield future.result()
    except KeyboardInterrupt:
        # Children own their process groups, so the terminal's SIGINT never
        # reaches them. Only the shared latch stops the running siblings.
        operation.request_cancel()
        raise
    finally:
        with _defer_repeated_interrupts():
            executor.shutdown(wait=True, cancel_futures=True)


def _in_operation_context(operation: CommandOperation, task: Callable[[], _T]) -> Callable[[], _T]:
    # A Context can be entered by one thread at a time, so each task gets its own copy.
    context = copy_context()
    context.run(_ACTIVE_OPERATION.set, operation)
    return lambda: context.run(task)


MemoryCommandOutcome: TypeAlias = (
    CommandResult | BaseException | Callable[[CommandRequest], CommandResult]
)


class MemoryCommandRuntime(_CancellationLatch):
    def __init__(self, results: Iterable[MemoryCommandOutcome] = ()) -> None:
        self._results = deque(results)
        self.requests: list[CommandRequest] = []

    def queue(self, result: MemoryCommandOutcome) -> None:
        self._results.append(result)

    def run(self, request: CommandRequest) -> CommandResult:
        self.check_cancelled()
        self.requests.append(request)
        if not self._results:
            raise AssertionError("no queued command result")
        result = self._results.popleft()
        if isinstance(result, BaseException):
            raise result
        if callable(result):
            result = result(request)
        self.check_cancelled()
        return result


DEFAULT_COMMAND_RUNTIME = ProductionCommandRuntime()
_ACTIVE_COMMAND_RUNTIME: ContextVar[CommandRuntime] = ContextVar(
    "dotman_command_runtime",
    default=DEFAULT_COMMAND_RUNTIME,
)


def current_command_runtime() -> CommandRuntime:
    return _ACTIVE_COMMAND_RUNTIME.get()


@contextmanager
def command_runtime_session(runtime: CommandRuntime) -> Iterator[None]:
    token = _ACTIVE_COMMAND_RUNTIME.set(runtime)
    try:
        yield
    finally:
        _ACTIVE_COMMAND_RUNTIME.reset(token)


def _process_arguments(command: Command) -> str | tuple[str, ...]:
    if isinstance(command, ShellCommand):
        return command.source
    return command.arguments


def _process_command_options(command: Command) -> dict[str, object]:
    if isinstance(command, ShellCommand):
        return {"shell": True, "executable": "/bin/sh"}
    return {"shell": False}


def _write_streamed_chunk(chunk: bytes, sink: TextIO) -> None:
    text = chunk.decode("utf-8", errors="replace")
    for line in text.splitlines(keepends=True):
        sink.write(f"      {line}")
        sink.flush()


@contextmanager
def _defer_repeated_interrupts() -> Iterator[None]:
    # Once cancellation is accepted, repeated Ctrl-C must not abandon cleanup.
    main_thread = threading.current_thread() is threading.main_thread()
    previous_handler = signal.getsignal(signal.SIGINT)
    if main_thread:
        signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        yield
    finally:
        if main_thread:
            signal.signal(signal.SIGINT, previous_handler)


def _signal_process(process: subprocess.Popen[bytes], sig: int, *, owns_process_group: bool) -> None:
    try:
        if owns_process_group:
            os.killpg(process.pid, sig)
        else:
            process.send_signal(sig)
    except ProcessLookupError:
        pass


def _stop_process(process: subprocess.Popen[bytes], *, owns_process_group: bool) -> None:
    # Escalate even when the group leader has exited: descendants can ignore
    # SIGINT and keep stdout/stdin open after the shell is gone.
    deadline = time.monotonic() + _INTERRUPT_GRACE_SECONDS
    _signal_process(process, signal.SIGINT, owns_process_group=owns_process_group)
    while time.monotonic() < deadline:
        try:
            process.wait(timeout=min(_CANCELLATION_POLL_SECONDS, max(0, deadline - time.monotonic())))
            if not owns_process_group:
                return
            time.sleep(min(_CANCELLATION_POLL_SECONDS, max(0, deadline - time.monotonic())))
        except (subprocess.TimeoutExpired, KeyboardInterrupt):
            continue
    _signal_process(process, signal.SIGKILL, owns_process_group=owns_process_group)
    deadline = time.monotonic() + _INTERRUPT_GRACE_SECONDS
    while True:
        try:
            process.wait(timeout=max(0, deadline - time.monotonic()))
            return
        except KeyboardInterrupt:
            continue


def _normalize_return_code(return_code: int) -> int:
    if return_code in {INTERRUPTED_EXIT_CODE, -signal.SIGINT}:
        return INTERRUPTED_EXIT_CODE
    return return_code
