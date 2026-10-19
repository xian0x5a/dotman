"""Publish approved, frozen file effects without projecting or observing sources."""

from __future__ import annotations

import stat
import heapq
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Literal, Protocol, Sequence

from dotman.sync_path_policy import SyncPathError
from dotman import file_access
from dotman.command_runtime import INTERRUPTED_EXIT_CODE, CommandRuntime, command_runtime_session, current_command_runtime
from dotman.interaction_policy import interaction_scope
from dotman.execution import ExecutionObserver, ExecutionStep, ExecutionStepResult, StepReporter, _execute_step
from dotman.models import HookPlan, PackagePlan, ResolvedSyncTarget, SnapshotConfig, TargetPlan, package_ref_text
from dotman.planning import PackagePlanningInput, plan_hooks, plan_repo_hooks
from dotman.snapshot import SnapshotRecord, create_push_snapshot, mark_snapshot_status, prune_snapshots


class Effect(Protocol):
    @property
    def kind(self) -> Literal["write", "delete", "chmod", "reapply"]: ...
    @property
    def path(self) -> Path: ...
    @property
    def content(self) -> bytes | None: ...
    @property
    def mode(self) -> int | None: ...


@dataclass(frozen=True)
class PublicationUnit:
    row_id: str
    identity: ResolvedSyncTarget
    effects: tuple[Effect, ...]
    auxiliary: bool = False
    symlink_authorized: bool = False


@dataclass(frozen=True)
class PublicationMetadata:
    packages: tuple[PackagePlan, ...]
    repo_hooks: tuple[tuple[str, tuple[HookPlan, ...]], ...]
    children: tuple[tuple[ResolvedSyncTarget, tuple[TargetPlan, ...]], ...] = ()


@dataclass(frozen=True)
class PublicationUnitResult:
    row_id: str
    status: Literal["ok", "failed", "skipped"]
    error: str | None = None
    diagnostic_code: str | None = None


@dataclass(frozen=True)
class PublicationResult:
    units: tuple[PublicationUnitResult, ...]
    error: str | None = None
    steps: tuple[ExecutionStepResult, ...] = ()
    snapshot: SnapshotRecord | None = None
    interrupted: bool = False


def prepare_publication(
    inputs: Sequence[PackagePlanningInput], *, file_symlink_mode: str = "prompt", dir_symlink_mode: str = "fail",
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
                action="noop", target_kind="probe" if target.probe_command is not None else target.target.target_type, chmod=target.chmod, projection_kind="raw",
                command_cwd=target.command_cwd,
                # One static target may supply metadata to both Sync stages.
                command_env={**target.command_env, "DOTMAN_OPERATION": "push"},
                file_symlink_mode=file_symlink_mode, dir_symlink_mode=dir_symlink_mode,
            )
            for target in item.target_metadata
        ]
        hooks = plan_hooks(
            item.repo, context.resolved_packages, context.context,
            selection=item.selection, operation="push", inferred_os=context.inferred_os,
            variables=context.variables, target_plans=targets,
            declaration_package_ids={item.selection.identity.package_id},
        )
        # Guards already narrowed capability during Observation and never rerun.
        hooks = {name: values for name, values in hooks.items() if not name.startswith("guard_")}
        packages.append(PackagePlan(
            operation="push", selection=item.selection,
            hooks=hooks, target_plans=targets, repo_root=item.repo.root,
        ))
        if item.repo.config.name not in repos:
            repos[item.repo.config.name] = tuple(
                hook for hooks in plan_repo_hooks(item.repo, operation="push").values()
                for hook in hooks
            )
    return PublicationMetadata(tuple(packages), tuple(repos.items()))


@dataclass(frozen=True)
class HookActivation:
    """Retained hook scope; a Probe activates normal hooks without a payload."""

    scope: str
    target: ResolvedSyncTarget | None = None


def _package_scope(package: PackagePlan) -> str:
    return f"{package.repo_name}:{package_ref_text(package_id=package.package_id, bound_profile=package.bound_profile)}"


def _target_identity(package: PackagePlan, target: TargetPlan) -> ResolvedSyncTarget:
    return ResolvedSyncTarget(
        repo=package.repo_name, package_id=target.package_id,
        bound_profile=package.bound_profile, target_name=target.target_name,
        child_path=target.child_path,
    )


def _hook_scopes(
    metadata: PublicationMetadata,
    auxiliary: Sequence[HookActivation],
    active_targets: Sequence[ResolvedSyncTarget],
) -> tuple[set[str], set[str]]:
    ancestors = {name: {name} for name, _ in metadata.repo_hooks}
    identities = set()
    for package in metadata.packages:
        scope = _package_scope(package)
        ancestors[scope] = {scope, package.repo_name}
        for target in package.target_plans:
            identity = _target_identity(package, target)
            identities.add(identity)
            ancestors[identity.canonical] = {identity.canonical, scope, package.repo_name}
    normal = set()
    noop = set()
    for identity in active_targets:
        normal.update(ancestors.get(replace(identity, child_path=None).canonical, ()))
    for activation in auxiliary:
        if activation.scope not in ancestors:
            raise ValueError("Hook activation has no captured metadata")
        if activation.target is not None:
            if activation.target not in identities or activation.target.canonical != activation.scope:
                raise ValueError("Probe activation does not match its captured target")
            normal.update(ancestors[activation.scope])
        else:
            noop.update(ancestors[activation.scope])
    return normal, noop


class _PublicationStopped(Exception):
    def __init__(self, message: str, *, interrupted: bool = False, diagnostic_code: str | None = None):
        super().__init__(message)
        self.interrupted = interrupted
        self.diagnostic_code = diagnostic_code


def _failed_step(step: ExecutionStep, error: BaseException) -> ExecutionStepResult:
    interrupted = isinstance(error, (InterruptedError, KeyboardInterrupt))
    return ExecutionStepResult(
        step, "interrupted" if interrupted else "failed",
        exit_code=INTERRUPTED_EXIT_CODE if interrupted else None,
        error=str(error) or ("Publication interrupted" if interrupted else None),
    )


def _effect_path(effect: Effect, target: TargetPlan, *, symlink_authorized: bool = False) -> Path:
    if effect.path != target.live_path:
        raise ValueError("Publication effect does not match its frozen live endpoint")
    path = effect.path
    directory_scope = target.target_kind == "directory" and target.child_path is None
    follow_link = target.dir_symlink_mode == "follow" if directory_scope else target.file_symlink_mode == "follow"
    followed_leaf = path.is_symlink() and follow_link
    if target.target_kind == "directory":
        root = directory_root(path, target.child_path) if target.child_path else path
        # Check only declared directory scopes: followed referents intentionally
        # need not remain beneath the lexical root.
        ancestors = (root,)
        if target.child_path:
            ancestors += tuple(root / parent for parent in reversed(Path(target.child_path).parents)
                               if parent != Path("."))
        for parent in ancestors:
            if parent.is_symlink() and target.dir_symlink_mode != "follow":
                raise SyncPathError("directory-symlink", f"Live directory symlink requires follow mode: {parent}")
    if path.is_symlink():
        if follow_link:
            # A followed link may retarget after Observation; policy follows its
            # current referent without adopting any newly observed content.
            try:
                path = path.resolve(strict=False)
            except (OSError, RuntimeError) as exc:
                raise SyncPathError("symlink-chain", f"Cannot follow live link: {effect.path}: {exc}") from exc
        elif effect.kind != "delete" and symlink_authorized:
            try:
                referent = path.stat()
            except FileNotFoundError:
                # An authorized write replaces a dangling link itself; Observation
                # admits one only for push-only work.
                referent = None
            except (OSError, RuntimeError) as exc:
                raise SyncPathError("symlink-referent", f"Live link requires a regular-file referent: {path}") from exc
            if referent is not None and not stat.S_ISREG(referent.st_mode):
                raise SyncPathError("symlink-referent", f"Live link requires a regular-file referent: {path}")
        elif effect.kind != "delete" and not symlink_authorized:
            raise SyncPathError("symlink-authorization-required", f"Live symlink replacement is not authorized: {path}")
    try:
        shape = path.lstat()
    except (FileNotFoundError, NotADirectoryError):
        if effect.kind == "chmod":
            raise ValueError(f"Cannot chmod missing live endpoint: {path}") from None
        return path
    if directory_scope:
        if stat.S_ISDIR(shape.st_mode) and effect.kind == "chmod":
            return path
        raise SyncPathError("unsupported-entry", f"Publication expects a directory root: {path}")
    if target.child_path is not None and not followed_leaf and stat.S_ISDIR(shape.st_mode) and effect.kind == "write":
        return path
    if not stat.S_ISREG(shape.st_mode) and not (stat.S_ISLNK(shape.st_mode) and (effect.kind == "delete" or symlink_authorized and effect.kind == "write")):
        raise SyncPathError("unsupported-entry", f"Publication expects a regular file: {path}")
    return path


def freeze_child_metadata(metadata: PublicationMetadata, observations) -> PublicationMetadata:
    """Retain one target hook scope, with independent frozen child execution paths."""
    children = []
    for package in metadata.packages:
        for target in package.target_plans:
            identity = _target_identity(package, target)
            units = [unit for unit in observations
                     if unit.identity.child_path is not None
                     and replace(unit.identity, child_path=None) == identity]
            if units:
                children.append((identity, tuple(
                    replace(target, child_path=unit.identity.child_path,
                            repo_path=unit.repository_path, live_path=unit.live_path)
                    for unit in units
                )))
    return replace(metadata, children=tuple(children))


def _unit_targets(metadata: PublicationMetadata, package: PackagePlan, target: TargetPlan) -> tuple[TargetPlan, ...]:
    return (target,) + dict(metadata.children).get(_target_identity(package, target), ())


def stage_target_order(metadata: PublicationMetadata) -> tuple[ResolvedSyncTarget, ...]:
    return tuple(
        _target_identity(package, target)
        for repo, _ in metadata.repo_hooks
        for package in metadata.packages if package.repo_name == repo
        for scope_target in package.target_plans
        for target in _unit_targets(metadata, package, scope_target)
    )


def _topology_order(metadata, package, target, target_work) -> tuple[TargetPlan, ...]:
    """Stable topological order: every selected deletion precedes its writer."""
    targets = {unit.child_path or "": unit for unit in _unit_targets(metadata, package, target)}
    work = {path: target_work.get(_target_identity(package, unit), ()) for path, unit in targets.items()}
    deletions = {path for path, actions in work.items() if any(action == "delete" for _, action in actions)}
    dependencies = {
        path: {blocker for blocker in deletions if blocker != path and (
            path.startswith(blocker + "/") or blocker.startswith(path + "/")
        )} if any(action in ("write", "update") for _, action in actions) else set()
        for path, actions in work.items()
    }
    dependents = {path: set() for path in targets}
    for path, blockers in dependencies.items():
        for blocker in blockers:
            dependents[blocker].add(path)
    ready = [path for path, blockers in dependencies.items() if not blockers]
    heapq.heapify(ready)
    ordered = []
    while ready:
        path = heapq.heappop(ready)
        ordered.append(targets[path])
        for dependent in dependents[path]:
            dependencies[dependent].remove(path)
            if not dependencies[dependent]:
                heapq.heappush(ready, dependent)
    if len(ordered) != len(targets):
        raise ValueError("Cyclic directory topology")
    return tuple(ordered)


def ordered_stage_steps(
    metadata: PublicationMetadata,
    target_work: dict[ResolvedSyncTarget, tuple[tuple[str, str], ...]],
    *, direction: str, active_targets: Sequence[ResolvedSyncTarget],
    auxiliary: Sequence[HookActivation] = (), run_noop: bool = False,
) -> tuple[ExecutionStep, ...]:
    """Freeze the nested stage order once; the same sequence drives IO and results."""
    normal, noop = _hook_scopes(metadata, auxiliary, active_targets)
    steps = []
    matched = set()

    def hooks(values, name, scope, package=None, target=None):
        for hook in values:
            if hook.hook_name == name and (
                scope in normal or scope in noop and (hook.run_noop or run_noop)
            ):
                steps.append(ExecutionStep(
                    repo_name=hook.repo_name or "", package_id=hook.package_id,
                    package_plan=package, target_plan=target, kind="hook",
                    action=name, scope_kind=hook.scope_kind, hook_plan=hook,
                ))

    for repo, repo_hooks in metadata.repo_hooks:
        hooks(repo_hooks, f"pre_{direction}", repo)
        for package in metadata.packages:
            if package.repo_name != repo:
                continue
            values = [hook for group in package.hooks.values() for hook in group]
            package_hooks = [hook for hook in values if hook.scope_kind == "package"]
            hooks(package_hooks, f"pre_{direction}", _package_scope(package), package)
            for target in package.target_plans:
                identity = _target_identity(package, target)
                target_hooks = [hook for hook in values if hook.scope_kind == "target"
                                and hook.target_name == target.target_name]
                hooks(target_hooks, f"pre_{direction}", identity.canonical, package, target)
                for unit_target in _topology_order(metadata, package, target, target_work):
                    unit_identity = _target_identity(package, unit_target)
                    if unit_identity in target_work:
                        matched.add(unit_identity)
                        for kind, action in target_work[unit_identity]:
                            steps.append(ExecutionStep(
                                repo_name=repo, package_id=package.package_id,
                                package_plan=package, target_plan=unit_target, kind=kind,
                                action=action, scope_kind="target",
                            ))
                hooks(target_hooks, f"post_{direction}", identity.canonical, package, target)
            hooks(package_hooks, f"post_{direction}", _package_scope(package), package)
        hooks(repo_hooks, f"post_{direction}", repo)
    if matched != set(target_work):
        raise ValueError("Frozen unit has no captured metadata")
    return tuple(steps)


def activates_hooks(metadata: PublicationMetadata, identity: ResolvedSyncTarget, *, direction: str) -> bool:
    """Whether activating this unit alone runs any normal hook of the direction."""
    return any(step.kind == "hook" for step in ordered_stage_steps(
        metadata, {identity: ()}, direction=direction, active_targets=[identity]))


def unattempted_steps(steps: Sequence[ExecutionStep]) -> tuple[ExecutionStepResult, ...]:
    return tuple(ExecutionStepResult(step, "unattempted", skip_reason="earlier-failure")
                 for step in steps)


def execute_publication(
    metadata: PublicationMetadata,
    units: Sequence[PublicationUnit],
    *,
    snapshot_config: SnapshotConfig,
    complete: Callable[[PublicationUnit], None] | None = None,
    command_runtime: CommandRuntime | None = None,
    stream_output: bool = False,
    no_confirm: bool = False,
    auxiliary: Sequence[HookActivation] = (),
    run_noop: bool = False,
    check_cancelled: Callable[[], None] | None = None,
    blocked: bool = False,
    observe: ExecutionObserver | None = None,
) -> PublicationResult:
    """Consume frozen effects, recording every attempted and unattempted boundary."""
    units = tuple(units)
    by_identity = {unit.identity: unit for unit in units}
    if len(by_identity) != len(units) or len({unit.row_id for unit in units}) != len(units):
        raise ValueError("Duplicate publication unit")
    for unit in units:
        for effect in unit.effects:
            if effect.kind not in {"write", "delete", "chmod", "reapply"}:
                raise ValueError(f"Unknown publication effect: {effect.kind}")
            if effect.kind == "write" and effect.content is None:
                raise ValueError("Frozen write requires content")
            if effect.kind == "chmod" and effect.mode is None:
                raise ValueError("Frozen chmod requires mode")
    planned = ordered_stage_steps(
        metadata, {
            unit.identity: tuple(("chmod" if effect.kind == "chmod" else "target", effect.kind)
                                 for effect in unit.effects) + ((("unit-completion", "complete"),) if unit.effects and not unit.auxiliary else ())
            for unit in units
        },
        direction="push", active_targets=[unit.identity for unit in units if unit.effects],
        auxiliary=auxiliary, run_noop=run_noop,
    )
    units = tuple(by_identity[identity] for identity in stage_target_order(metadata) if identity in by_identity)
    results = {unit.row_id: PublicationUnitResult(
        unit.row_id, "ok" if not unit.effects else "skipped") for unit in units}
    if blocked:
        return PublicationResult(tuple(results.values()), steps=unattempted_steps(planned))
    snapshot_packages, snapshot_endpoints = [], []
    for package in metadata.packages:
        targets = []
        for scope_target in package.target_plans:
            for target in _unit_targets(metadata, package, scope_target):
                unit = by_identity.get(_target_identity(package, target))
                # A reapply touches no file, so it has nothing to snapshot.
                effect = next((item for item in unit.effects if item.kind != "reapply"), None) if unit is not None else None
                if effect is not None:
                    snapshot_endpoints.append((effect, target, unit.symlink_authorized))
                    action = ("delete" if effect.kind == "delete"
                              else "update" if effect.path.exists() or effect.path.is_symlink() else "create")
                    targets.append(replace(target, action=action))
        if targets:
            snapshot_packages.append(replace(package, target_plans=targets))
    steps, snapshot, error, interrupted = [], None, None, False
    snapshot_started = False
    effect_positions = {unit.row_id: 0 for unit in units}
    with interaction_scope(no_confirm=no_confirm), command_runtime_session(command_runtime or current_command_runtime()):
        reporter = StepReporter("live-publication", planned, observe)
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
                    if complete is not None:
                        complete(unit)
                    results[unit.row_id] = PublicationUnitResult(unit.row_id, "ok")
                    # Successful completion is already represented by the unit result.
                    continue
                else:
                    effect = unit.effects[effect_positions[unit.row_id]]
                    effect_positions[unit.row_id] += 1
                    if effect.kind == "reapply":
                        # Activation alone lets the enclosing push hooks reapply live state.
                        steps.append(ExecutionStepResult(step, "ok"))
                        reporter.finished(index, steps[reported_from:])
                        continue
                    if not snapshot_started:
                        try:
                            if snapshot_config.enabled:
                                # Snapshot reads the whole frozen set; reject unsafe
                                # later endpoints before snapshot code can read them.
                                for initial_effect, target, authorized in snapshot_endpoints:
                                    _effect_path(initial_effect, target, symlink_authorized=authorized)
                            snapshot = create_push_snapshot(snapshot_packages, snapshot_config)
                        except (OSError, ValueError, RuntimeError, KeyboardInterrupt) as exc:
                            failure = _failed_step(replace(step, kind="snapshot", action="create"), exc)
                            steps.append(failure)
                            steps.extend(unattempted_steps((step,)))
                            raise _PublicationStopped(failure.error, interrupted=failure.status == "interrupted",
                                                      diagnostic_code=exc.code if isinstance(exc, SyncPathError) else None) from exc
                        snapshot_started = True
                    # Snapshot IO is a separate access; do not retain a referent
                    # resolved before it when applying this frozen effect.
                    path = _effect_path(effect, step.target_plan, symlink_authorized=unit.symlink_authorized)
                    if effect.kind == "write":
                        root = directory_root(effect.path, step.target_plan.child_path)
                        if step.target_plan.child_path is not None and not root.exists():
                            file_access.make_directories(root)
                            if step.target_plan.chmod is not None:
                                file_access.chmod(root, int(step.target_plan.chmod, 8))
                        if step.target_plan.child_path is not None and path.is_dir():
                            file_access.remove_empty_directory_tree(path)
                        file_access.write_bytes_atomic(path, effect.content, mode=effect.mode)
                    elif effect.kind == "delete":
                        # Following a leaf grants endpoint mutation, not ownership
                        # of its external parent directories.
                        prune_root = path.parent if path != effect.path else directory_root(effect.path, step.target_plan.child_path)
                        file_access.delete_path_and_prune_empty_parents(path, root=prune_root)
                    else:
                        file_access.chmod(path, effect.mode)
                    if unit.auxiliary:
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
                error = str(exc) or "Publication interrupted"
                if unit is not None:
                    results[unit.row_id] = PublicationUnitResult(unit.row_id, "failed", error, exc.code if isinstance(exc, SyncPathError) else exc.diagnostic_code if isinstance(exc, _PublicationStopped) else None)
                reporter.finished(index, steps[reported_from:])
                steps.extend(unattempted_steps(planned[index + 1:]))
                break
        if snapshot is not None:
            try:
                snapshot = mark_snapshot_status(snapshot, "failed" if error else "applied")
                # Retention runs only after the final status is durable.
                prune_snapshots(snapshot_config.path, max_generations=snapshot_config.max_generations)
            except (OSError, ValueError, RuntimeError, KeyboardInterrupt) as exc:
                failure = _failed_step(ExecutionStep(kind="snapshot", action="finalize"), exc)
                steps.append(failure)
                reporter.unplanned(failure)
                interrupted = interrupted or failure.status == "interrupted"
                error = error or failure.error
    return PublicationResult(tuple(results.values()), error, tuple(steps), snapshot, interrupted)


def directory_root(path: Path, child_path: str | None) -> Path:
    return path.parents[len(Path(child_path).parts) - 1] if child_path else path.parent
