"""Apply frozen Primary Source outcomes, completing units before pull post-hooks."""

from __future__ import annotations

import stat
from dataclasses import dataclass
from typing import Callable, Sequence

from dotman import file_access
from dotman.command_runtime import CommandRuntime, command_runtime_session, current_command_runtime
from dotman.interaction_policy import interaction_scope
from dotman.execution import ExecutionObserver, ExecutionStepResult, StepReporter, _execute_step, directory_synced_file_mode
from dotman.atomic_files import default_created_file_mode
from dotman.models import PackagePlan, ResolvedSyncTarget, TargetPlan
from dotman.planning import PackagePlanningInput, plan_hooks, plan_repo_hooks
from dotman.sync_base_store import FilePresent, Missing, DirectoryChildPresent, SyncBasePayload
from dotman.sync_publication import (
    HookActivation, PublicationMetadata, PublicationResult, PublicationUnitResult,
    ordered_stage_steps, stage_target_order, unattempted_steps, _target_identity,
    _PublicationStopped, _failed_step, directory_root,
)


@dataclass(frozen=True)
class RepositoryApplyUnit:
    row_id: str
    identity: ResolvedSyncTarget
    outcome: SyncBasePayload | None
    requires_publication: bool = False


def prepare_repository_apply(
    inputs: Sequence[PackagePlanningInput],
) -> PublicationMetadata:
    """Freeze only execution metadata and hooks at session open, never payloads."""
    packages = []
    repos = {}
    for item in inputs:
        context = item.package_context
        targets = [
            TargetPlan(
                package_id=target.package_id, target_name=target.target_name,
                repo_path=target.repo_path, live_path=target.live_path,
                action="noop", target_kind="probe" if target.probe_command is not None else "file", projection_kind="raw",
                command_cwd=target.command_cwd,
                # One static target may supply metadata to both Sync stages.
                command_env={**target.command_env, "DOTMAN_OPERATION": "pull"},
            )
            for target in item.target_metadata
        ]
        hooks = plan_hooks(
            item.repo, context.resolved_packages, context.context,
            selection=item.selection, operation="pull", inferred_os=context.inferred_os,
            variables=context.variables, target_plans=targets,
            declaration_package_ids={item.selection.identity.package_id},
        )
        # Guards already narrowed capability during Observation and never rerun.
        hooks = {name: values for name, values in hooks.items() if not name.startswith("guard_")}
        packages.append(PackagePlan(
            operation="pull", selection=item.selection,
            hooks=hooks, target_plans=targets, repo_root=item.repo.root,
        ))
        if item.repo.config.name not in repos:
            repos[item.repo.config.name] = tuple(
                hook for hooks in plan_repo_hooks(item.repo, operation="pull").values()
                for hook in hooks
            )
    return PublicationMetadata(tuple(packages), tuple(repos.items()))



def execute_repository_apply(
    metadata: PublicationMetadata,
    units: Sequence[RepositoryApplyUnit],
    *,
    command_runtime: CommandRuntime | None = None,
    complete: Callable[[RepositoryApplyUnit], None],
    stream_output: bool = False,
    no_confirm: bool = False,
    auxiliary: Sequence[HookActivation] = (),
    run_noop: bool = False,
    check_cancelled: Callable[[], None] | None = None,
    blocked: bool = False,
    observe: ExecutionObserver | None = None,
) -> PublicationResult:
    """Apply repository effects and complete ready units at their ordered positions."""
    units = tuple(units)
    by_identity = {unit.identity: unit for unit in units}
    if len(by_identity) != len(units) or len({unit.row_id for unit in units}) != len(units):
        raise ValueError("Duplicate repository apply unit")
    if any(unit.outcome is not None and not isinstance(unit.outcome, (FilePresent, DirectoryChildPresent, Missing)) for unit in units):
        raise ValueError("Invalid frozen repository outcome")
    planned = ordered_stage_steps(
        metadata, {
            unit.identity: (
                (("target", "delete" if isinstance(unit.outcome, Missing) else "update"),)
                if unit.outcome is not None else ()
            ) + (() if unit.requires_publication else (("unit-completion", "complete"),))
            for unit in units
        },
        direction="pull", active_targets=[unit.identity for unit in units if unit.outcome is not None],
        auxiliary=auxiliary, run_noop=run_noop,
    )
    units = tuple(by_identity[identity] for identity in stage_target_order(metadata) if identity in by_identity)
    results = {unit.row_id: PublicationUnitResult(unit.row_id, "skipped") for unit in units}
    if blocked:
        return PublicationResult(tuple(results.values()), steps=unattempted_steps(planned))
    steps, error, interrupted = [], None, False
    with interaction_scope(no_confirm=no_confirm), command_runtime_session(command_runtime or current_command_runtime()):
        reporter = StepReporter("repository-apply", planned, observe)
        for index, step in enumerate(planned):
            unit = None if step.kind == "hook" else by_identity[_target_identity(step.package_plan, step.target_plan)]
            reported_from = len(steps)
            reporter.started(index)
            try:
                if check_cancelled is not None:
                    check_cancelled()
                if step.kind == "hook":
                    result = _execute_step(step, stream_output=stream_output, no_confirm=no_confirm)
                elif step.kind == "unit-completion":
                    complete(unit)
                    results[unit.row_id] = PublicationUnitResult(unit.row_id, "ok")
                    continue
                else:
                    apply_repository_source(
                        step.target_plan.repo_path, unit.outcome, repo_root=step.package_plan.repo_root,
                        target_root=directory_root(step.target_plan.repo_path, step.target_plan.child_path),
                    )
                    if unit.requires_publication:
                        results[unit.row_id] = PublicationUnitResult(unit.row_id, "ok")
                    result = ExecutionStepResult(step, "ok")
                steps.append(result)
                reporter.finished(index, steps[reported_from:])
                if result.status != "ok":
                    raise _PublicationStopped(
                        result.error or f"{step.action} exited {result.exit_code}",
                        interrupted=result.status == "interrupted",
                    )
            except (_PublicationStopped, OSError, ValueError, RuntimeError, KeyboardInterrupt) as exc:
                if not isinstance(exc, _PublicationStopped):
                    steps.append(_failed_step(step, exc))
                interrupted = isinstance(exc, (InterruptedError, KeyboardInterrupt)) or (
                    isinstance(exc, _PublicationStopped) and exc.interrupted)
                error = str(exc) or "Repository apply interrupted"
                if unit is not None:
                    results[unit.row_id] = PublicationUnitResult(unit.row_id, "failed", error)
                reporter.finished(index, steps[reported_from:])
                steps.extend(unattempted_steps(planned[index + 1:]))
                break
    return PublicationResult(tuple(results.values()), error, tuple(steps), interrupted=interrupted)


def apply_repository_source(path, outcome: SyncBasePayload, *, repo_root, target_root=None) -> None:
    """Apply frozen bytes with the same confinement checks for every Source Change."""
    path.relative_to(repo_root)
    # Atomic replacement protects the leaf, not a retargeted parent link.
    for parent in path.parents:
        if parent.is_symlink():
            raise ValueError(f"Repository source parent is a symlink: {parent}")
        if parent == repo_root:
            break
    try:
        shape = path.lstat()
    except FileNotFoundError:
        shape = None
    if shape is not None and stat.S_ISDIR(shape.st_mode) and isinstance(outcome, DirectoryChildPresent):
        file_access.remove_empty_directory_tree(path)
        shape = None
    if shape is not None and not stat.S_ISREG(shape.st_mode):
        raise ValueError(f"Repository apply expects a regular file: {path}")
    if isinstance(outcome, (FilePresent, DirectoryChildPresent)):
        mode = None
        if isinstance(outcome, DirectoryChildPresent):
            mode = directory_synced_file_mode(
                destination_mode=stat.S_IMODE(shape.st_mode) if shape is not None else default_created_file_mode(),
                source_mode=stat.S_IXUSR if outcome.executable else 0,
            )
        file_access.write_bytes_atomic(path, outcome.content, mode=mode)
    else:
        file_access.delete_path_and_prune_empty_parents(path, root=target_root or path.parent)
