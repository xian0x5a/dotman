from __future__ import annotations

from pathlib import Path
from threading import Thread
from types import SimpleNamespace

import pytest

import dotman.execution as execution
from dotman import command_runtime as command_runtime_module, file_access
from dotman.command_runtime import CommandResult, MemoryCommandRuntime, ShellCommand, command_runtime_session
from dotman.execution import ExecutionStep, ExecutionStepResult
from dotman.models import HookPlan
from tests.helpers import make_package_plan


def _hook_step(hook_plan: HookPlan, **plan_fields) -> ExecutionStep:
    plan = make_package_plan(
        operation="push", repo_name="fixture", package_id="app", requested_profile="default", **plan_fields,
    )
    return ExecutionStep(
        repo_name="fixture", package_id=hook_plan.package_id, package_plan=plan, kind="hook",
        action=hook_plan.hook_name, scope_kind=hook_plan.scope_kind, hook_plan=hook_plan,
    )


def _run_hook_step(
    step: ExecutionStep, runtime: MemoryCommandRuntime, *, stream_output: bool = False, no_confirm: bool = False,
) -> ExecutionStepResult:
    with command_runtime_session(runtime):
        return execution._execute_step(step, stream_output=stream_output, no_confirm=no_confirm)


@pytest.mark.parametrize(("no_confirm", "expected_value"), [(False, "0"), (True, "1")])
def test_hook_step_env_is_planned_env_plus_no_confirm_policy(no_confirm: bool, expected_value: str) -> None:
    planned_env = {"DOTMAN_REPO_NAME": "fixture", "DOTMAN_OS": "linux", "DOTMAN_VAR_feature__flag": "on"}
    step = _hook_step(HookPlan(
        package_id="app", hook_name="pre_push", command="echo package", cwd=Path("/repo/app"), env=planned_env,
    ))
    runtime = MemoryCommandRuntime([CommandResult(exit_code=0)])

    assert _run_hook_step(step, runtime, no_confirm=no_confirm).status == "ok"

    request, = runtime.requests
    assert dict(request.env) == {**planned_env, "DOTMAN_NO_CONFIRM": expected_value}


@pytest.mark.parametrize("interactive", [False, True])
def test_hook_step_translates_hook_options_to_runtime_request(interactive: bool, monkeypatch) -> None:
    monkeypatch.setattr(execution, "_require_interactive_terminal_for_hook", lambda: None)
    runtime = MemoryCommandRuntime([CommandResult(exit_code=7, stdout=b"out", stderr=b"err")])
    step = _hook_step(HookPlan(
        package_id="app", hook_name="pre_push", command="printf result", cwd=Path("/command-cwd"),
        env={"X": "1"}, io="tty" if interactive else "pipe", elevation="intercept",
    ))

    result = _run_hook_step(step, runtime, stream_output=True)

    assert (result.status, result.stdout, result.stderr) == ("failed", "out", "err")
    assert result.error == "command exited with status 7"
    request, = runtime.requests
    assert request.command == ShellCommand("printf result")
    assert request.cwd == Path("/command-cwd")
    assert request.env["X"] == "1"
    assert request.io == ("tty" if interactive else "pipe")
    # TTY hooks own the terminal, so their output is never streamed through dotman.
    assert request.stream_output is (not interactive)
    assert request.elevation == "intercept"


def test_tty_hook_step_fails_without_terminal(monkeypatch) -> None:
    for stream in ("stdin", "stdout", "stderr"):
        monkeypatch.setattr(f"sys.{stream}.isatty", lambda: False)
    runtime = MemoryCommandRuntime()
    step = _hook_step(HookPlan(env={}, package_id="app", hook_name="pre_push", command="echo tty", cwd=Path("/repo/app"), io="tty"))

    result = _run_hook_step(step, runtime)

    assert result.status == "failed"
    assert result.error == "hook command io 'tty' requires an interactive terminal"
    assert runtime.requests == []


def test_hook_step_marks_command_exit_130_as_interrupted() -> None:
    runtime = MemoryCommandRuntime([CommandResult(exit_code=130)])
    step = _hook_step(HookPlan(env={}, package_id="app", hook_name="pre_push", command="python hook.py", cwd=Path("/repo/app")))

    result = _run_hook_step(step, runtime)

    assert (result.status, result.exit_code) == ("interrupted", 130)
    assert result.error == "command interrupted with status 130"


def test_write_bytes_atomic_cleans_up_temp_file_after_failed_replace(
    tmp_path: Path,
    monkeypatch,
) -> None:
    target_path = tmp_path / "config.txt"
    temp_name_prefix = ".dotman-"
    temp_name_suffix = ".tmp"

    def failing_replace(self: Path, target: Path) -> Path:
        raise RuntimeError("boom")

    monkeypatch.setattr(Path, "replace", failing_replace)

    with pytest.raises(RuntimeError, match="boom"):
        execution.write_bytes_atomic(target_path, b"payload\n")

    leftover_temp_files = list(tmp_path.glob(f"{temp_name_prefix}*{temp_name_suffix}"))
    assert leftover_temp_files == []


def test_write_bytes_atomic_removes_stale_dotman_temp_files_before_write(tmp_path: Path) -> None:
    stale_temp_file = tmp_path / f".dotman-999999-{'deadbeef' * 4}.tmp"
    stale_temp_file.write_text("stale\n", encoding="utf-8")

    target_path = tmp_path / "config.txt"
    execution.write_bytes_atomic(target_path, b"payload\n")

    assert target_path.read_text(encoding="utf-8") == "payload\n"
    assert not stale_temp_file.exists()


def test_read_bytes_uses_sudo_when_direct_read_is_denied(tmp_path: Path, monkeypatch) -> None:
    target_path = tmp_path / "protected.txt"
    target_path.write_text("payload\n", encoding="utf-8")
    target_path.chmod(0o000)

    runtime = MemoryCommandRuntime(
        [CommandResult(exit_code=0), CommandResult(exit_code=0, stdout=b"payload\n")]
    )
    monkeypatch.setattr(file_access, "current_command_runtime", lambda: runtime)

    with file_access.sudo_session():
        assert file_access.read_bytes(target_path) == b"payload\n"


def test_request_sudo_skips_password_notice_while_sudo_ticket_is_valid(monkeypatch, capsys) -> None:
    runtime = MemoryCommandRuntime([CommandResult(exit_code=0), CommandResult(exit_code=0)])
    monkeypatch.setattr(file_access, "current_command_runtime", lambda: runtime)

    with file_access.sudo_session():
        file_access.request_sudo("list protected directory: /etc/sddm.conf.d")
        file_access.request_sudo("write protected path: /etc/sddm.conf")

    captured = capsys.readouterr()
    assert captured.err == "[sudo] list protected directory: /etc/sddm.conf.d\n"
    assert [request.io for request in runtime.requests] == ["pipe", "pipe"]


def test_request_sudo_prompts_with_reason_when_sudo_ticket_is_missing_or_expired(monkeypatch, capsys) -> None:
    runtime = MemoryCommandRuntime(
        [
            CommandResult(exit_code=1), CommandResult(exit_code=0),
            CommandResult(exit_code=1), CommandResult(exit_code=0),
        ]
    )
    monkeypatch.setattr(file_access, "current_command_runtime", lambda: runtime)

    with file_access.sudo_session():
        file_access.request_sudo("list protected directory: /etc/sddm.conf.d")
        file_access.request_sudo("write protected path: /etc/sddm.conf")

    captured = capsys.readouterr()
    assert captured.err == (
        "[sudo] password required to list protected directory: /etc/sddm.conf.d\n"
        "[sudo] password required to write protected path: /etc/sddm.conf\n"
    )
    assert [request.io for request in runtime.requests] == ["pipe", "tty", "pipe", "tty"]


def test_request_sudo_preserves_authentication_interruption(monkeypatch) -> None:
    runtime = MemoryCommandRuntime([CommandResult(exit_code=130)])
    monkeypatch.setattr(file_access, "current_command_runtime", lambda: runtime)

    with file_access.sudo_session(), pytest.raises(KeyboardInterrupt):
        file_access.request_sudo("write protected path")


def test_request_sudo_without_operation_scope_does_not_reuse_a_stale_runtime(monkeypatch) -> None:
    first_runtime = MemoryCommandRuntime([CommandResult(exit_code=0)])
    second_runtime = MemoryCommandRuntime([CommandResult(exit_code=0)])
    active_runtime = [first_runtime]
    monkeypatch.setattr(file_access.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(file_access, "current_command_runtime", lambda: active_runtime[0])

    try:
        file_access.request_sudo("first planning command")
        active_runtime[0] = second_runtime
        file_access.request_sudo("second planning command")
    finally:
        file_access._cleanup_active_sudo_lease()

    assert [request.command.arguments for request in first_runtime.requests] == [("sudo", "-n", "-v")]
    assert [request.command.arguments for request in second_runtime.requests] == [("sudo", "-n", "-v")]


def test_sudo_lease_keepalive_uses_runtime_captured_on_creation(monkeypatch) -> None:
    runtime = MemoryCommandRuntime([CommandResult(exit_code=0)])
    default_runtime_requests = []
    monkeypatch.setattr(
        command_runtime_module.DEFAULT_COMMAND_RUNTIME,
        "run",
        lambda request: default_runtime_requests.append(request) or CommandResult(exit_code=0),
    )

    with command_runtime_session(runtime):
        lease = file_access._SudoLease()

    wait_results = iter((False, True))
    lease._stop_event = SimpleNamespace(wait=lambda timeout: next(wait_results))
    keepalive_thread = Thread(target=lease._keepalive_loop)
    keepalive_thread.start()
    keepalive_thread.join()

    assert [request.command.arguments for request in runtime.requests] == [
        ("sudo", "-n", "true")
    ]
    assert default_runtime_requests == []
