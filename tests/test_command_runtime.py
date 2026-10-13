from __future__ import annotations

import io
import signal
import sys
from pathlib import Path
from threading import Thread

import pytest

from dotman.command_runtime import (
    ArgvCommand,
    CommandRequest,
    CommandResult,
    MemoryCommandRuntime,
    ProductionCommandRuntime,
    ShellCommand,
    SystemCommandElevation,
    raise_for_command_interruption,
    command_operation,
)


def test_memory_runtime_records_requests_and_returns_results() -> None:
    expected = CommandResult(exit_code=17, stdout=b"out", stderr=b"err")
    runtime = MemoryCommandRuntime([expected])
    request = CommandRequest(command=ShellCommand("exit 17"), env={"MODE": "test"})

    assert runtime.run(request) is expected
    assert runtime.requests == [request]

    with pytest.raises(AssertionError, match="no queued command result"):
        runtime.run(request)


def test_memory_runtime_can_raise_a_queued_interruption() -> None:
    runtime = MemoryCommandRuntime([KeyboardInterrupt()])

    with pytest.raises(KeyboardInterrupt):
        runtime.run(CommandRequest(command=ArgvCommand(("unused",))))


def test_memory_runtime_can_compute_a_result_from_the_recorded_request() -> None:
    runtime = MemoryCommandRuntime(
        [lambda request: CommandResult(exit_code=len(request.command.arguments))]
    )

    result = runtime.run(CommandRequest(command=ArgvCommand(("one", "two"))))

    assert result.exit_code == 2


def test_raise_for_command_interruption_preserves_normalized_sigint() -> None:
    raise_for_command_interruption(CommandResult(exit_code=0))

    with pytest.raises(KeyboardInterrupt):
        raise_for_command_interruption(CommandResult(exit_code=130))


def test_production_runtime_pipe_argv_merges_environment_and_captures_bytes(tmp_path: Path) -> None:
    request = CommandRequest(
        command=ArgvCommand(
            (
                sys.executable,
                "-c",
                (
                    "import os, sys; "
                    "sys.stdout.buffer.write(sys.stdin.buffer.read() + os.environ['DOTMAN_TEST'].encode()); "
                    "sys.stderr.buffer.write(b'err')"
                ),
            )
        ),
        cwd=tmp_path,
        env={"DOTMAN_TEST": "-env"},
        input=b"payload",
    )
    result = ProductionCommandRuntime().run(request)

    assert result == CommandResult(exit_code=0, stdout=b"payload-env", stderr=b"err")


def test_production_runtime_shell_excludes_ambient_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DOTMAN_REMOVED", "ambient")

    result = ProductionCommandRuntime().run(
        CommandRequest(
            command=ShellCommand("printf '%s' \"${DOTMAN_REMOVED-unset}:$DOTMAN_KEPT\""),
            env={"DOTMAN_KEPT": "overlay"},
            excluded_env_keys=frozenset({"DOTMAN_REMOVED"}),
        )
    )

    assert result.stdout == b"unset:overlay"


def test_production_runtime_streams_prefixed_output_while_capturing_it() -> None:
    stdout_sink = io.StringIO()
    stderr_sink = io.StringIO()

    result = ProductionCommandRuntime().run(
        CommandRequest(
            command=ShellCommand("printf 'one\\ntwo\\n'; printf 'bad\\n' >&2"),
            stream_output=True,
            stdout_sink=stdout_sink,
            stderr_sink=stderr_sink,
        )
    )

    assert result.stdout == b"one\ntwo\n"
    assert result.stderr == b"bad\n"
    assert stdout_sink.getvalue() == "      one\n      two\n"
    assert stderr_sink.getvalue() == "      bad\n"


def test_production_runtime_tty_mode_inherits_process_streams(capfd: pytest.CaptureFixture[str]) -> None:
    result = ProductionCommandRuntime().run(
        CommandRequest(
            command=ArgvCommand((sys.executable, "-c", "import sys; print('tty-out'); print('tty-err', file=sys.stderr)")),
            io="tty",
        )
    )

    captured = capfd.readouterr()
    assert result == CommandResult(exit_code=0)
    assert "tty-out" in captured.out
    assert "tty-err" in captured.err


def test_production_runtime_tty_mode_from_non_main_thread() -> None:
    # signal.signal() raises ValueError outside the main thread; tty-mode runs
    # happen off the main thread when the elevation broker serves a sudo shim
    # request, so they must not touch process-global signal handlers there.
    results: list[CommandResult] = []
    failures: list[BaseException] = []

    def run_tty_command() -> None:
        try:
            results.append(
                ProductionCommandRuntime().run(
                    CommandRequest(
                        command=ArgvCommand((sys.executable, "-c", "print('thread-tty')")),
                        io="tty",
                    )
                )
            )
        except BaseException as exc:  # noqa: BLE001 - surfaced to the test body.
            failures.append(exc)

    thread = Thread(target=run_tty_command)
    thread.start()
    thread.join(timeout=10)
    assert not thread.is_alive()
    assert failures == []
    assert results == [CommandResult(exit_code=0)]


def test_production_runtime_delegates_elevation_preparation() -> None:
    class RecordingElevation:
        def __init__(self) -> None:
            self.modes: list[str] = []

        def prepare(self, command, env, mode):
            self.modes.append(mode)
            return command, {**env, "ELEVATED": "yes"}

    elevation = RecordingElevation()
    runtime = ProductionCommandRuntime(elevation=elevation)

    result = runtime.run(
        CommandRequest(
            command=ShellCommand("printf %s \"$ELEVATED\""),
            elevation="intercept",
        )
    )

    assert elevation.modes == ["intercept"]
    assert result.stdout == b"yes"


def test_system_elevation_root_requests_sudo_and_wraps_shell(monkeypatch: pytest.MonkeyPatch) -> None:
    reasons: list[str | None] = []
    monkeypatch.setattr("dotman.command_runtime.os.geteuid", lambda: 1000)
    monkeypatch.setattr("dotman.file_access.request_sudo", reasons.append)
    monkeypatch.setattr("dotman.file_access.sudo_prefix_command", lambda command: f"ROOT({command})")

    command, env = SystemCommandElevation().prepare(
        ShellCommand("systemctl restart service"),
        {"X": "1"},
        "root",
    )

    assert reasons == ["run privileged command"]
    assert command == ShellCommand("ROOT(systemctl restart service)")
    assert env == {"X": "1"}


def test_system_elevation_intercept_preserves_command_path_after_shim(monkeypatch: pytest.MonkeyPatch) -> None:
    class Broker:
        def env(self, *, reason):
            assert reason == "run privileged command"
            return {"DOTMAN_ELEVATION_BROKER": "/runtime/broker", "PATH": "/shim:/usr/bin"}

    monkeypatch.setattr("dotman.elevation.current_elevation_broker", lambda: Broker())

    _command, env = SystemCommandElevation().prepare(
        ShellCommand("installer"),
        {"PATH": "/custom/bin", "X": "1"},
        "intercept",
    )

    assert env == {
        "PATH": "/shim:/custom/bin",
        "X": "1",
        "DOTMAN_ELEVATION_BROKER": "/runtime/broker",
    }


def test_production_runtime_normalizes_sigint_exit() -> None:
    result = ProductionCommandRuntime().run(
        CommandRequest(
            command=ArgvCommand(
                (
                    sys.executable,
                    "-c",
                    f"import os, signal; os.kill(os.getpid(), {signal.SIGINT})",
                )
            )
        )
    )

    assert result.exit_code == 130


def test_command_result_text_requires_valid_utf8() -> None:
    result = CommandResult(exit_code=0, stdout="text".encode(), stderr=b"\xff")

    assert result.stdout_text == "text"
    with pytest.raises(UnicodeDecodeError):
        _ = result.stderr_text

@pytest.mark.parametrize("runtime_type", [ProductionCommandRuntime, MemoryCommandRuntime])
def test_cancel_latch_prevents_launch(runtime_type) -> None:
    runtime = runtime_type()
    with command_operation():
        runtime.request_cancel()
        runtime.request_cancel()
        with pytest.raises(InterruptedError):
            runtime.check_cancelled()
        with pytest.raises(InterruptedError):
            runtime.run(CommandRequest(command=ArgvCommand(("must-not-launch",))))


def test_copied_context_cancels_ignored_sigint_and_reaps_group(tmp_path: Path) -> None:
    from contextvars import copy_context
    import os
    import time
    from dotman.command_runtime import command_runtime_session, current_command_runtime

    ready = tmp_path / "ready"
    runtime = ProductionCommandRuntime()
    errors = []
    code = (
        "import os, signal, time; from pathlib import Path; "
        "signal.signal(signal.SIGINT, signal.SIG_IGN); "
        "pid = os.fork(); "
        f"Path({str(ready)!r}).write_text(str(os.getpid())) if pid else None; "
        "time.sleep(30)"
    )
    def run():
        try:
            current_command_runtime().run(CommandRequest(command=ArgvCommand((sys.executable, "-c", code))))
        except BaseException as exc:
            errors.append(exc)
    with command_runtime_session(runtime), command_operation():
        thread = Thread(target=copy_context().run, args=(run,), daemon=True)
        thread.start()
        deadline = time.monotonic() + 3
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(.01)
        try:
            assert ready.exists()
        finally:
            runtime.request_cancel()
            runtime.request_cancel()
            thread.join(3)
    assert not thread.is_alive()
    assert len(errors) == 1 and isinstance(errors[0], InterruptedError)
    with pytest.raises(ProcessLookupError):
        os.kill(int(ready.read_text()), 0)


def test_cancel_during_spawn_is_cleaned_up(monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess
    runtime = ProductionCommandRuntime()
    original = subprocess.Popen
    spawned = []
    def spawn(*args, **kwargs):
        process = original(*args, **kwargs)
        spawned.append(process)
        runtime.request_cancel()
        return process
    monkeypatch.setattr("dotman.command_runtime.subprocess.Popen", spawn)
    with pytest.raises(InterruptedError):
        runtime.run(CommandRequest(command=ArgvCommand((sys.executable, "-c", "import time; time.sleep(30)"))))
    assert spawned[0].poll() is not None

def test_cancellation_stops_descendants_after_group_leader_exits(tmp_path: Path) -> None:
    import time
    ready = tmp_path / "child-ready"
    runtime = ProductionCommandRuntime()
    errors = []
    code = (
        "import os, signal, time; from pathlib import Path; "
        "pid = os.fork(); "
        "os._exit(0) if pid else None; "
        "signal.signal(signal.SIGINT, signal.SIG_IGN); "
        f"Path({str(ready)!r}).touch(); "
        "time.sleep(30)"
    )
    def run():
        try:
            runtime.run(CommandRequest(command=ArgvCommand((sys.executable, "-c", code))))
        except BaseException as exc:
            errors.append(exc)
    with command_operation():
        from contextvars import copy_context
        thread = Thread(target=copy_context().run, args=(run,), daemon=True)
        thread.start()
        deadline = time.monotonic() + 3
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(.01)
        try:
            assert ready.exists()
        finally:
            runtime.request_cancel()
            thread.join(3)
    assert not thread.is_alive()
    assert len(errors) == 1 and isinstance(errors[0], InterruptedError)

@pytest.mark.parametrize("io_mode", ["pipe", "tty"])
def test_cancel_during_elevation_prevents_spawn(monkeypatch, io_mode) -> None:
    class CancellingElevation:
        def prepare(self, command, env, mode):
            runtime.request_cancel()
            return command, dict(env)
    runtime = ProductionCommandRuntime(elevation=CancellingElevation())
    def forbidden_spawn(*args, **kwargs):
        pytest.fail("cancelled command was launched")
    monkeypatch.setattr("dotman.command_runtime.subprocess.Popen", forbidden_spawn)
    with pytest.raises(InterruptedError):
        runtime.run(CommandRequest(command=ArgvCommand(("unused",)), io=io_mode))


@pytest.mark.parametrize("started_threads", [0, 1, 2])
def test_interrupt_during_pipe_startup_reaps_process(monkeypatch, started_threads) -> None:
    import subprocess
    runtime = ProductionCommandRuntime()
    original_spawn = subprocess.Popen
    original_start = Thread.start
    spawned = []
    starts = 0
    def spawn(*args, **kwargs):
        process = original_spawn(*args, **kwargs)
        spawned.append(process)
        return process
    def start(thread):
        nonlocal starts
        if starts == started_threads:
            raise KeyboardInterrupt
        starts += 1
        original_start(thread)
    monkeypatch.setattr("dotman.command_runtime.subprocess.Popen", spawn)
    monkeypatch.setattr("dotman.command_runtime.Thread.start", start)
    try:
        with pytest.raises(KeyboardInterrupt):
            runtime.run(CommandRequest(
                command=ArgvCommand((sys.executable, "-c", "import time; time.sleep(30)")),
                input=b"input",
            ))
        assert spawned[0].poll() is not None
        assert all(stream.closed for stream in (spawned[0].stdin, spawned[0].stdout, spawned[0].stderr))
    finally:
        # Keep the regression test leak-free even against the broken runtime.
        for process in spawned:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=1)

def test_interrupt_during_tty_signal_setup_reaps_process(monkeypatch) -> None:
    import subprocess
    runtime = ProductionCommandRuntime()
    original_spawn = subprocess.Popen
    original_signal = signal.signal
    spawned = []
    interrupted = False
    def spawn(*args, **kwargs):
        process = original_spawn(*args, **kwargs)
        spawned.append(process)
        return process
    def install_signal(sig, handler):
        nonlocal interrupted
        if not interrupted:
            interrupted = True
            raise KeyboardInterrupt
        return original_signal(sig, handler)
    monkeypatch.setattr("dotman.command_runtime.subprocess.Popen", spawn)
    monkeypatch.setattr("dotman.command_runtime.signal.signal", install_signal)
    with pytest.raises(KeyboardInterrupt):
        runtime.run(CommandRequest(
            command=ArgvCommand((sys.executable, "-c", "import time; time.sleep(30)")), io="tty",
        ))
    assert spawned[0].poll() is not None


@pytest.mark.parametrize("runtime_type", [ProductionCommandRuntime, MemoryCommandRuntime])
def test_cancellation_belongs_to_operation_not_shared_runtime(runtime_type):
    from contextvars import copy_context

    runtime = runtime_type()
    request = CommandRequest(command=ArgvCommand(("true",)))
    with command_operation():
        cancelled_context = copy_context()
        runtime.request_cancel()
        with command_operation():
            with pytest.raises(InterruptedError):
                runtime.run(request)
    with command_operation():
        # Stale copied work cannot be revived by a subsequent operation.
        with pytest.raises(InterruptedError):
            cancelled_context.run(runtime.run, request)
        if isinstance(runtime, MemoryCommandRuntime):
            runtime.queue(CommandResult(exit_code=0))
        assert runtime.run(request).exit_code == 0


def test_default_runtime_can_run_a_later_operation_after_cancellation():
    from dotman.command_runtime import DEFAULT_COMMAND_RUNTIME

    with command_operation():
        DEFAULT_COMMAND_RUNTIME.request_cancel()
        with pytest.raises(InterruptedError):
            DEFAULT_COMMAND_RUNTIME.run(CommandRequest(command=ArgvCommand(("must-not-launch",))))
    assert DEFAULT_COMMAND_RUNTIME.run(CommandRequest(command=ArgvCommand(("true",)))).exit_code == 0


def test_production_runtime_notices_exit_promptly() -> None:
    import subprocess
    import time

    # 33ms lands in the widest gap of a restarting Popen.wait(timeout) backoff
    # ladder, where exit used to be noticed ~17ms late. Minimums damp CI noise.
    command = ("sleep", "0.033")
    runtime = ProductionCommandRuntime()

    def fastest(run) -> float:
        durations = []
        for _ in range(5):
            started = time.perf_counter()
            run()
            durations.append(time.perf_counter() - started)
        return min(durations)

    baseline = fastest(lambda: subprocess.run(command, check=True))
    observed = fastest(lambda: runtime.run(CommandRequest(command=ArgvCommand(command))))
    assert observed - baseline < 0.01


def test_run_ordered_reports_the_first_failure_in_task_order() -> None:
    import time
    from dotman.command_runtime import run_ordered

    def slow_failure():
        time.sleep(0.2)
        raise ValueError("first")

    def fast_failure():
        raise ValueError("second")

    with pytest.raises(ValueError, match="first"):
        list(run_ordered([slow_failure, fast_failure]))


def test_interrupting_run_ordered_stops_every_running_command(tmp_path: Path) -> None:
    import os
    import threading
    import time
    from dotman.command_runtime import run_ordered

    runtime = ProductionCommandRuntime()
    pid_files = [tmp_path / f"child-{index}" for index in range(2)]

    def task(pid_file: Path):
        # Children ignore SIGINT and own their process group, as a stubborn
        # projection would; only dotman's cancellation can stop them.
        code = (
            "import os, signal, time; from pathlib import Path; "
            "signal.signal(signal.SIGINT, signal.SIG_IGN); "
            f"Path({str(pid_file)!r}).write_text(str(os.getpid())); "
            "time.sleep(30)"
        )
        return lambda: runtime.run(CommandRequest(command=ArgvCommand((sys.executable, "-c", code))))

    def interrupt_when_both_started():
        deadline = time.monotonic() + 5
        while not all(path.exists() for path in pid_files) and time.monotonic() < deadline:
            time.sleep(0.01)
        signal.pthread_kill(threading.main_thread().ident, signal.SIGINT)

    interrupter = Thread(target=interrupt_when_both_started, daemon=True)
    started = time.monotonic()
    with command_operation():
        interrupter.start()
        with pytest.raises(KeyboardInterrupt):
            list(run_ordered([task(path) for path in pid_files]))
    assert time.monotonic() - started < 10
    for path in pid_files:
        with pytest.raises(ProcessLookupError):
            os.kill(int(path.read_text()), 0)
