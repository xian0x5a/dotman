from __future__ import annotations

import signal
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

from dotman.atomic_files import write_bytes_atomic as atomic_write_bytes_atomic
from dotman.atomic_files import write_symlink_atomic as atomic_write_symlink_atomic
from dotman.command_runtime import (
    INTERRUPTED_EXIT_CODE,
    CommandRequest,
    ShellCommand,
    current_command_runtime,
)
from dotman.models import HookPlan, PackagePlan, TargetPlan


@dataclass(frozen=True)
class ExecutionStep:
    repo_name: str = ""
    package_id: str | None = None
    package_plan: PackagePlan | None = None
    kind: str = ""
    action: str = ""
    scope_kind: str = "package"
    hook_plan: HookPlan | None = None
    target_plan: TargetPlan | None = None
    # Display text for steps that have neither a hook command nor a target path.
    description: str | None = None

    @property
    def command(self) -> str | None:
        if self.hook_plan is not None:
            return self.hook_plan.command
        return None


@dataclass(frozen=True)
class ExecutionStepResult:
    step: ExecutionStep
    status: str
    skip_reason: str | None = None
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    error: str | None = None


@dataclass(frozen=True)
class StepStarted:
    stage: str
    step: ExecutionStep
    index: int
    total: int


@dataclass(frozen=True)
class StepFinished:
    """A step's attempted result; index/total are None for steps reported without a start."""

    stage: str
    result: ExecutionStepResult
    index: int | None = None
    total: int | None = None


ExecutionEvent = StepStarted | StepFinished
ExecutionObserver = Callable[[ExecutionEvent], None]

# Completion checkpoints are bookkeeping, not work the user selected.
INVISIBLE_STEP_KINDS = frozenset({"unit-completion"})


class StepReporter:
    """Report planned steps numbered within contiguous repo or package groups."""

    def __init__(self, stage: str, planned: Sequence[ExecutionStep], observe: ExecutionObserver | None) -> None:
        self._stage = stage
        self._planned = planned
        self._observe = observe
        self._positions = _group_positions(planned)
        self._finished: set[int] = set()

    def started(self, index: int) -> None:
        if self._observe is not None and index in self._positions:
            self._observe(StepStarted(self._stage, self._planned[index], *self._positions[index]))

    def finished(self, index: int, results: Sequence[ExecutionStepResult]) -> None:
        """Report the first attempted result recorded while running planned step `index`."""
        attempted = next((result for result in results if result.status != "unattempted"), None)
        if self._observe is None or attempted is None or index in self._finished:
            return
        self._finished.add(index)
        position = self._positions.get(index, (None, None))
        self._observe(StepFinished(self._stage, attempted, *position))

    def unplanned(self, result: ExecutionStepResult) -> None:
        if self._observe is not None:
            self._observe(StepFinished(self._stage, result))


def _group_positions(planned: Sequence[ExecutionStep]) -> dict[int, tuple[int, int]]:
    visible = [index for index, step in enumerate(planned) if step.kind not in INVISIBLE_STEP_KINDS]
    groups: list[list[int]] = []
    for index in visible:
        key = step_group_key(planned[index])
        if not groups or step_group_key(planned[groups[-1][-1]]) != key:
            groups.append([])
        groups[-1].append(index)
    return {index: (position, len(group)) for group in groups for position, index in enumerate(group, start=1)}


def step_group_key(step: ExecutionStep) -> tuple[str, str | None, str | None]:
    """Repo hooks group by repo; everything else by package instance."""
    bound_profile = step.package_plan.bound_profile if step.package_plan is not None else None
    return step.repo_name, step.package_id, bound_profile


def _execute_step(step: ExecutionStep, *, stream_output: bool, no_confirm: bool) -> ExecutionStepResult:
    # Publication applies target effects itself; only hook steps run through here.
    if step.kind != "hook":
        raise ValueError(f"unsupported execution step kind '{step.kind}'")
    try:
        if step.hook_plan.io == "tty":
            # Checked before the runtime so a rejected hook never triggers elevation.
            if no_confirm:
                raise ValueError("hook command io 'tty' is unavailable with --no-confirm")
            _require_interactive_terminal_for_hook()
        command_result = current_command_runtime().run(
            CommandRequest(
                command=ShellCommand(step.hook_plan.command),
                cwd=step.hook_plan.cwd,
                env=_build_hook_env(step, no_confirm=no_confirm),
                io=step.hook_plan.io,
                stream_output=stream_output if step.hook_plan.io == "pipe" else False,
                elevation=step.hook_plan.elevation,
            )
        )
        exit_code = command_result.exit_code
        stdout = command_result.stdout_text
        stderr = command_result.stderr_text
        if exit_code == 0:
            return ExecutionStepResult(step=step, status="ok", exit_code=exit_code, stdout=stdout, stderr=stderr)
        if _is_interrupt_exit_code(exit_code):
            return ExecutionStepResult(
                step=step,
                status="interrupted",
                exit_code=INTERRUPTED_EXIT_CODE,
                error=f"command interrupted with status {INTERRUPTED_EXIT_CODE}",
                stdout=stdout,
                stderr=stderr,
            )
        if exit_code == 100 and _is_guard_step(step):
            return ExecutionStepResult(
                step=step,
                status="skipped",
                skip_reason="guard",
                exit_code=exit_code,
                stdout=stdout,
                stderr=stderr,
            )
        return ExecutionStepResult(
            step=step,
            status="failed",
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            error=f"command exited with status {exit_code}",
        )
    except Exception as exc:  # noqa: BLE001 - fail-fast execution should surface the original error text.
        return ExecutionStepResult(step=step, status="failed", error=str(exc))


def _is_guard_step(step: ExecutionStep) -> bool:
    return step.kind == "hook" and step.action.startswith("guard_")


def directory_synced_file_mode(*, destination_mode: int, source_mode: int) -> int:
    exec_bits = stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
    if source_mode & exec_bits:
        return destination_mode | exec_bits
    return destination_mode & ~exec_bits


def write_bytes_atomic(path: Path, content: bytes) -> None:
    atomic_write_bytes_atomic(path, content)


def write_symlink_atomic(path: Path, target: str | Path) -> None:
    atomic_write_symlink_atomic(path, target)


def delete_path_and_prune_empty_parents(path: Path, *, root: Path) -> None:
    if path.exists() or path.is_symlink():
        path.unlink()
    prune_root = root if root.is_dir() else root.parent
    current = path.parent
    while current.exists() and current != prune_root and current != current.parent:
        try:
            current.rmdir()
        except OSError:
            break
        current = current.parent


def _is_interrupt_exit_code(exit_code: int) -> bool:
    return exit_code == INTERRUPTED_EXIT_CODE or exit_code == -signal.SIGINT


def _require_interactive_terminal_for_hook() -> None:
    _require_interactive_terminal(setting_name="hook command io")


def _require_interactive_terminal(*, setting_name: str) -> None:
    if sys.stdin.isatty() and sys.stdout.isatty() and sys.stderr.isatty():
        return
    raise ValueError(f"{setting_name} 'tty' requires an interactive terminal")


def _build_hook_env(step: ExecutionStep, *, no_confirm: bool) -> dict[str, str]:
    return {**step.hook_plan.env, "DOTMAN_NO_CONFIRM": "1" if no_confirm else "0"}


__all__ = [
    "ExecutionStep",
    "ExecutionStepResult",
]
