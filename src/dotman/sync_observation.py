"""Frozen independent Sync Unit Observation; no review state, Approval or execution plans."""

from __future__ import annotations

import stat
from contextlib import ExitStack
from functools import partial
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal

from dotman.sync_path_policy import SyncPathError
from dotman import planning, projection
from dotman.command_runtime import run_ordered
from dotman.file_access import read_bytes
from dotman.manifest import resolve_sync_policy, sync_policy_allows_operation
from dotman.models import GuardSkip, ResolvedSyncScope, ResolvedSyncTarget, resolved_package_selection_key
from dotman.planning_guards import evaluate_directional_guards, evaluate_directory_path_rule_guards
from dotman.progress import ProgressSink
from dotman.sync_directory import census_directory, child_metadata, planned_child_metadata
from dotman.sync_base_lifecycle import (
    BaseUnit,
    FrozenBaseUnit,
    SyncBaseLifecycle,
)
from dotman.sync_base_store import (
    FilePresent,
    DirectoryChildPresent,
    SyncBasePayload,
    Missing,
    SyncBaseRecord,
    SyncBaseStore,
    SyncBaseStoreError,
)

FileState = SyncBasePayload
ObservationState = Literal["directly-in-sync", "drifted", "observation-failed"]


@dataclass(frozen=True)
class Diagnostic:
    code: str
    message: str
    severity: Literal["error", "warning"] = "error"


@dataclass(frozen=True)
class BaseEvidence:
    status: Literal["usable", "unavailable", "not-applicable"]
    reason: str | None = None
    record: SyncBaseRecord | None = None
    acknowledged: bool = False
    deleted: bool = False


@dataclass(frozen=True)
class Observation:
    identity: ResolvedSyncTarget
    state: ObservationState
    configured_policy: str
    effective_policy: str
    file_symlink_mode: Literal["prompt", "follow"]
    compare_repo: str
    compare_live: str
    base: BaseEvidence
    repository: FileState | None = None
    live: FileState | None = None
    comparison_repository: FileState | None = None
    comparison_live: FileState | None = None
    chmod: str | None = None
    live_is_symlink: bool = False
    live_mode: int | None = None
    diagnostics: tuple[Diagnostic, ...] = ()
    repository_path: Path | None = None
    live_path: Path | None = None
    repository_blockers: tuple[str, ...] = ()
    live_blockers: tuple[str, ...] = ()
    managed_children: tuple[str, ...] = ()


def _identity(metadata: projection.TargetMetadata) -> ResolvedSyncTarget:
    return ResolvedSyncTarget(
        repo=metadata.repo_name,
        package_id=metadata.package_id,
        target_name=metadata.target_name,
        bound_profile=metadata.bound_profile,
    )


def _read_endpoint(
    path: Path, *, repository: bool, follow_missing: bool = False, directory_child: bool = False, repository_root: Path | None = None
) -> tuple[FileState, bool, int | None]:
    if repository and repository_root is not None:
        if not path.is_relative_to(repository_root):
            raise SyncPathError("repository-confinement", "repository endpoint is outside its repository")
        for parent in (path, *path.parents):
            if parent.is_symlink():
                raise SyncPathError("repository-symlink", f"repository endpoint must not traverse a symlink: {parent}")
            if parent == repository_root:
                break
    try:
        shape = path.lstat()
    except (FileNotFoundError, NotADirectoryError):
        return Missing(), False, None
    is_symlink = stat.S_ISLNK(shape.st_mode)
    if is_symlink:
        if repository:
            raise SyncPathError("repository-symlink", "repository endpoint must not be a symlink")
        try:
            shape = path.stat()
        except FileNotFoundError as exc:
            if follow_missing:
                return Missing(), True, None
            raise SyncPathError(
                "symlink-referent", "prompt-mode live symlink requires a regular-file referent"
            ) from exc
    if directory_child and stat.S_ISDIR(shape.st_mode) and not is_symlink:
        return Missing(), False, None
    if not stat.S_ISREG(shape.st_mode):
        raise SyncPathError("unsupported-entry", "endpoint must be a regular file")
    return FilePresent(read_bytes(path)), is_symlink, stat.S_IMODE(shape.st_mode)


_ResolvedInputs = dict[
    ResolvedSyncTarget, tuple[planning.PackagePlanningInput, projection.TargetMetadata]
]


def _scope_planning_inputs(scope: ResolvedSyncScope, direction: str) -> list[planning.PackagePlanningInput]:
    """The scope's static planning for its own packages, in scope order."""
    planned: dict[tuple[str, str, str | None, str], planning.PackagePlanningInput] = {}
    for item in scope.planning_inputs[direction]:
        # A package pulled in by several tracked roots renders identically.
        planned.setdefault(resolved_package_selection_key(item.selection), item)
    # Sessions carry the scope's public selections, not tracked-graph provenance.
    return [
        replace(planned[resolved_package_selection_key(selection)], selection=selection)
        for selection in scope.package_selections
    ]


def _resolve_inputs(
    scope: ResolvedSyncScope,
    *,
    operation: str = "sync",
) -> tuple[_ResolvedInputs, dict[str, list[planning.PackagePlanningInput]]]:
    selected = {replace(target, child_path=None) for target in scope.targets}
    inputs = {}
    directional = {}
    for direction in ("push", "pull"):
        candidates = _scope_planning_inputs(scope, direction)
        narrowed = []
        for item in candidates:
            metadata = [
                entry for entry in item.target_metadata if _identity(entry) in selected
            ]
            for entry in metadata:
                if entry.target.target_type is None and (entry.repo_path.is_dir() or entry.live_path.is_dir()):
                    entry = replace(entry, target=replace(entry.target, target_type="directory"))
                # Provider identity belongs to the invoking workflow, not the
                # direction that happened to supply this unit's metadata.
                # Keep directional candidates unchanged for Guards and hooks.
                provider = replace(entry, command_env={**entry.command_env, "DOTMAN_OPERATION": operation})
                inputs.setdefault(_identity(entry), (item, provider))
            # Empty selected scopes may retain independently noop-eligible hooks.
            narrowed.append(replace(item, target_metadata=metadata))
        directional[direction] = narrowed
    if inputs.keys() != selected:
        raise ValueError("resolved Sync scope planning does not cover its selected targets")
    # Directional metadata collection must not move pull-only files to the end.
    ordered = {replace(identity, child_path=None): inputs[replace(identity, child_path=None)] for identity in scope.targets}
    # Full targets need every potentially configured child direction. Exact
    # child scopes already know their policies, so unrelated rules must not
    # activate ancestor or target Guards for an unselected direction.
    for identity, (item, metadata) in ordered.items():
        if metadata.target.target_type != "directory":
            continue
        selected_paths = {target.child_path for target in scope.targets if replace(target, child_path=None) == identity}
        if None in selected_paths:
            policies = {resolve_sync_policy(package=metadata.package, target=metadata.target)}
            policies.update(rule.sync_policy for rule in metadata.path_rules if rule.sync_policy is not None)
        else:
            policies = {
                resolve_sync_policy(package=metadata.package, target=child_metadata(metadata, path).target)
                for path in selected_paths
            }
        for direction, candidates in directional.items():
            index = next(index for index, candidate in enumerate(candidates) if candidate.selection == item.selection)
            candidate = candidates[index]
            allowed = any(sync_policy_allows_operation(policy, operation=direction) for policy in policies)
            targets = [entry for entry in candidate.target_metadata if allowed or _identity(entry) != identity]
            if allowed and not any(_identity(entry) == identity for entry in targets):
                targets.append(replace(metadata, command_env={**metadata.command_env, "DOTMAN_OPERATION": direction}))
            candidates[index] = replace(candidate, target_metadata=targets)
    return ordered, directional


def _base_unit(identity: ResolvedSyncTarget, metadata: projection.TargetMetadata) -> BaseUnit:
    return BaseUnit(identity, resolve_sync_policy(package=metadata.package, target=metadata.target))


def _observe_file(
    context: planning.PlanningContext,
    identity: ResolvedSyncTarget,
    item: planning.PackagePlanningInput,
    metadata: projection.TargetMetadata,
    unit: BaseUnit,
    effective: str,
    base: BaseEvidence,
) -> Observation:
    observation = Observation(
        identity,
        "observation-failed",
        unit.configured_policy,
        effective,
        context.config.file_symlink_mode,
        metadata.compare_repo,
        metadata.compare_live,
        base,
        chmod=metadata.chmod,
        repository_path=metadata.repo_path,
        live_path=metadata.live_path,
    )
    if effective == "no-route":
        return replace(
            observation,
            diagnostics=(
                Diagnostic(
                    "no-route", "directional Guards removed every convergence route"
                ),
            ),
        )
    try:
        repository, _link, repository_mode = _read_endpoint(metadata.repo_path, repository=True, directory_child=identity.child_path is not None, repository_root=item.repo.root)
        if identity.child_path is not None and isinstance(repository, FilePresent):
            repository = DirectoryChildPresent(repository.content, projection.file_is_executable(repository_mode))
        observation = replace(observation, repository=repository)
        live, live_is_symlink, live_mode = _read_endpoint(
            metadata.live_path,
            repository=False, directory_child=identity.child_path is not None,
            # Push-only work may replace a dangling link after authorization. Pull
            # capability must not read it as Missing and delete the repository source.
            follow_missing=context.config.file_symlink_mode == "follow" or effective in ("push-only", "push-only-delete"),
        )
        if identity.child_path is not None and isinstance(live, FilePresent):
            live = DirectoryChildPresent(live.content, projection.file_is_executable(live_mode))
        observation = replace(
            observation, live=live, live_is_symlink=live_is_symlink, live_mode=live_mode
        )
        if effective == "push-only-delete":
            compared_repo, compared_live = Missing(), live
        else:

            def projected(view: str, *, repo_side: bool) -> FileState:
                content = projection.project_file_view(
                    context.projection.command_runtime,
                    metadata=metadata,
                    context=item.package_context.context,
                    repository=repository.content
                    if isinstance(repository, (FilePresent, DirectoryChildPresent))
                    else None,
                    live=live.content if isinstance(live, (FilePresent, DirectoryChildPresent)) else None,
                    view=view,
                    repo_side=repo_side,
                    repository_is_proposal=False,
                )
                return Missing() if content is None else (
                    DirectoryChildPresent(content, (repository if repo_side else live).executable)
                    if identity.child_path is not None else FilePresent(content)
                )

            compared_repo = projected(
                "render" if effective == "push-only" else metadata.compare_repo,
                repo_side=True,
            )
            compared_live = (
                live
                if effective == "push-only"
                else projected(
                    metadata.compare_live,
                    repo_side=False,
                )
            )
        if effective == "push-only" and metadata.chmod is not None and isinstance(compared_repo, DirectoryChildPresent):
            compared_repo = replace(compared_repo, executable=projection.file_is_executable(int(metadata.chmod, 8)))
        exact_mode_active = effective in ("push-only", "both")
        mode_agrees = (
            not exact_mode_active
            or isinstance(compared_repo, Missing)
            or metadata.chmod is None
            or live_mode == int(metadata.chmod, 8)
        )
        return replace(
            observation,
            state="directly-in-sync"
            if compared_repo == compared_live and mode_agrees
            else "drifted",
            comparison_repository=compared_repo,
            comparison_live=compared_live,
        )
    except (OSError, ValueError, SyncBaseStoreError) as exc:
        return replace(
            observation, diagnostics=(Diagnostic(exc.code if isinstance(exc, SyncPathError) else "observation-failed", str(exc)),)
        )


def _effective_policy(configured: str, push: bool, pull: bool) -> str:
    if push and pull:
        return "both"
    if push:
        return "push-only-delete" if configured == "push-only-delete" else "push-only"
    return "pull-only" if pull else "no-route"


@dataclass(frozen=True)
class ObservedScope:
    observations: tuple[Observation, ...]
    directional: dict[str, list[planning.PackagePlanningInput]]
    hook_scopes: dict[str, frozenset[str]]
    inputs: _ResolvedInputs
    directory_censuses: tuple = ()
    # Push/Pull omit Guard-skipped work; Sync keeps the unit on its remaining
    # route. Both report the skip so a narrowed policy never goes unexplained.
    guard_skips: tuple[tuple[str, GuardSkip], ...] = ()


def _base_store_exists(context: planning.PlanningContext, item: planning.PackagePlanningInput) -> bool:
    return SyncBaseStore.exists(context.tracked_state.state_root, item.repo.config.state_key)


def _stored_children(
    context: planning.PlanningContext,
    directory_inputs: _ResolvedInputs,
    selected_children: dict[ResolvedSyncTarget, set[str | None]],
) -> _ResolvedInputs:
    """Selected directory children that hold a stored Base, from configuration alone."""
    children, stores, unavailable = {}, {}, set()
    with ExitStack() as resources:
        for identity, (item, metadata) in directory_inputs.items():
            if identity.repo in unavailable or not _base_store_exists(context, item):
                continue
            target = identity.canonical.encode()
            try:
                if identity.repo not in stores:
                    stores[identity.repo] = resources.enter_context(SyncBaseStore.open(
                        context.tracked_state.state_root, item.repo.config.state_key, read_only=True,
                    ))
                records = stores[identity.repo].target_records(target)
            except (OSError, ValueError, SyncBaseStoreError):
                # Observation reports the unavailable store on every eligible unit.
                unavailable.add(identity.repo)
                continue
            selected = selected_children[identity]
            for record in records:
                # A record of the bare target identity is not a child.
                if not record.identity.startswith(target + b"/"):
                    continue
                relative = record.identity[len(target) + 1:].decode()
                if None in selected or relative in selected:
                    children[replace(identity, child_path=relative)] = (item, child_metadata(metadata, relative))
    return children


def _discard_ineligible_bases(
    context: planning.PlanningContext,
    inputs: _ResolvedInputs,
    *,
    preview: bool,
) -> tuple[dict[ResolvedSyncTarget, bool], dict[ResolvedSyncTarget, Diagnostic]]:
    """Resolved configured policy is authoritative before volatile Guards."""
    deleted, warnings = {}, {}
    if preview:
        return deleted, warnings
    with ExitStack() as resources:
        stores = {}
        for identity, (item, metadata) in inputs.items():
            unit = _base_unit(identity, metadata)
            # Absent storage holds nothing to discard; do not create it.
            if unit.eligible or identity.repo not in stores and not _base_store_exists(context, item):
                continue
            try:
                if identity.repo not in stores:
                    stores[identity.repo] = resources.enter_context(SyncBaseStore.open(
                        context.tracked_state.state_root, item.repo.config.state_key,
                    ))
                deleted[identity] = stores[identity.repo].delete(unit.identity_bytes)
            except (OSError, ValueError, SyncBaseStoreError) as exc:
                warnings[identity] = Diagnostic("base-maintenance-failed", str(exc), "warning")
    return deleted, warnings


def observe_scope(
    context: planning.PlanningContext,
    scope: ResolvedSyncScope,
    *,
    preview: bool,
    run_noop: bool = False,
    resolved_inputs: tuple[_ResolvedInputs, dict[str, list[planning.PackagePlanningInput]]] | None = None,
    directions: tuple[str, ...] = ("push", "pull"),
    read_bases: bool = True,
    operation: str = "sync",
    omit_no_route: bool = False,
    sink: ProgressSink | None = None,
) -> ObservedScope:
    inputs, directional = resolved_inputs if resolved_inputs is not None else _resolve_inputs(scope, operation=operation)
    # Auxiliary planning visits every resolved probe target, even ones this
    # direction filter drops, so progress counts them from the same set.
    probe_target_count = sum(1 for _item, metadata in inputs.values() if metadata.probe_command is not None)
    def participates(metadata):
        policy = resolve_sync_policy(package=metadata.package, target=metadata.target)
        return any(sync_policy_allows_operation(policy, operation=direction) for direction in directions)

    directional = {
        direction: candidates if direction in directions else []
        for direction, candidates in directional.items()
    }
    inputs = {
        identity: value for identity, value in inputs.items()
        if value[1].target.target_type == "directory" or participates(value[1])
    }
    probe_inputs = {identity: value for identity, value in inputs.items() if value[1].probe_command is not None}
    inputs = {identity: value for identity, value in inputs.items() if identity not in probe_inputs}
    directory_inputs = {identity: value for identity, value in inputs.items() if value[1].target.target_type == "directory"}
    ordered_inputs = inputs
    inputs = {identity: value for identity, value in inputs.items() if identity not in directory_inputs}
    selected_children = {
        identity: {target.child_path for target in scope.targets if replace(target, child_path=None) == identity}
        for identity in directory_inputs
    }
    # Configured ineligibility must survive a later Guard failure. It depends
    # only on configuration, so stored child Bases stand in for discovery.
    stored_children = {
        child_identity: (item, child)
        for child_identity, (item, child) in _stored_children(context, directory_inputs, selected_children).items()
        if participates(child)
    } if read_bases and not preview else {}
    maintenance, checkpoint_warnings = _discard_ineligible_bases(
        context, {**inputs, **stored_children}, preview=preview,
    ) if read_bases else ({}, {})
    configured_directional = directional
    eligibility = evaluate_directional_guards(
        directional, command_runtime=context.projection.command_runtime, run_noop=run_noop,
    )
    directional = {direction: value.inputs for direction, value in eligibility.items()}
    guard_skips = [(direction, skip) for direction, value in eligibility.items() for skip in value.guard_skips]
    admitted = {
        direction: {_identity(metadata) for item in survivors for metadata in item.target_metadata}
        for direction, survivors in directional.items()
    }
    # Guards gate host-state work: a target denied in every direction is never
    # scanned and never runs its ignore command.
    directory_inputs = {
        identity: value for identity, value in directory_inputs.items()
        if any(identity in survivors for survivors in admitted.values())
    }
    resolved_directories = {}
    for identity, (item, metadata) in directory_inputs.items():
        selected_paths = selected_children[identity]
        census = census_directory(
            projection.resolve_ignore_command(context.projection.command_runtime, metadata),
            follow_live_directories=context.config.dir_symlink_mode == "follow",
            selected_paths=tuple(sorted(path for path in selected_paths if path is not None)),
        )
        children = {}
        for relative, failures in census.entries:
            if None not in selected_paths and relative not in selected_paths:
                continue
            child = planned_child_metadata(metadata, relative)
            if participates(child):
                children[relative] = (replace(identity, child_path=relative or None), child, failures)
        resolved_directories[identity] = (selected_paths, census, children)

    child_policies, child_failures, child_topology = {}, {}, {}
    directory_censuses = []
    expanded_inputs = {**inputs, **probe_inputs}
    for identity, (item, metadata) in directory_inputs.items():
        selected_paths, census, children = resolved_directories[identity]
        # Any Guard narrowing invalidates a complete participation proof.
        configured_directions = {direction for direction in ("push", "pull")
                                 if any(_identity(target) == identity for candidate in configured_directional[direction]
                                        for target in candidate.target_metadata)}
        complete = (None in selected_paths and census.unrestricted
                    and all(identity in admitted[direction] for direction in configured_directions)
                    and not any((rule.hooks or {}).get("guard_push") or (rule.hooks or {}).get("guard_pull") for rule in metadata.path_rules))
        directory_censuses.append((identity, item, census, complete))
        child_admitted = {}
        for direction in ("push", "pull"):
            candidates = {
                relative for relative, (_child, child, _failures) in children.items()
                if identity in admitted[direction] and sync_policy_allows_operation(
                    resolve_sync_policy(package=child.package, target=child.target), operation=direction)
            }
            child_admitted[direction], child_skips = evaluate_directory_path_rule_guards(
                command_runtime=context.projection.command_runtime, path_rules=metadata.path_rules,
                candidate_paths=candidates, operation=direction, context=item.package_context.context,
                target_env={**metadata.command_env, "DOTMAN_OPERATION": direction},
                repo_name=identity.repo, package_id=identity.package_id,
                bound_profile=identity.bound_profile, target_name=identity.target_name,
            )
            guard_skips.extend((direction, skip) for skip in child_skips)
        for relative, (child_identity, child, failures) in children.items():
            child_topology[child_identity] = (census.blockers(relative, repository=True), census.blockers(relative, repository=False), tuple(path for path, failures in census.entries if not failures))
            expanded_inputs[child_identity] = (item, child)
            unit = _base_unit(child_identity, child)
            child_policies[child_identity] = _effective_policy(unit.configured_policy, relative in child_admitted["push"], relative in child_admitted["pull"])
            child_failures[child_identity] = failures

    inputs = {identity: value for identity, value in expanded_inputs.items() if identity not in probe_inputs}
    if omit_no_route:
        inputs = {
            identity: value for identity, value in inputs.items()
            if child_policies.get(identity, _effective_policy(
                resolve_sync_policy(package=value[1].package, target=value[1].target),
                identity in admitted["push"], identity in admitted["pull"],
            )) != "no-route"
        }
    units = {
        identity: _base_unit(identity, metadata)
        for identity, (item, metadata) in inputs.items()
    }
    if sink is not None:
        # Probe targets are not observed here; auxiliary planning ticks them.
        sink.start(len(inputs) + probe_target_count)
    with ExitStack() as resources:
        lifecycles, store_warnings, attempted = {}, {}, set()
        for identity, (item, _metadata) in inputs.items():
            if not read_bases or identity.repo in attempted:
                continue
            attempted.add(identity.repo)
            try:
                # An established store must be validated, never mistaken for
                # absence; only an unversioned one reads as absent until a real
                # run replaces it.
                # Only eligible units can acknowledge; ineligible-only repos
                # must not create storage they will never use.
                needs_store = not preview and any(
                    unit.eligible for unit_identity, unit in units.items() if unit_identity.repo == identity.repo
                )
                if needs_store or _base_store_exists(context, item):
                    store = resources.enter_context(SyncBaseStore.open(
                        context.tracked_state.state_root, item.repo.config.state_key,
                        read_only=preview,
                    ))
                    lifecycles[identity.repo] = SyncBaseLifecycle(
                        store, operation=operation, preview=preview,
                    )
            except (OSError, ValueError, SyncBaseStoreError) as exc:
                store_warnings[identity.repo] = Diagnostic("base-unavailable", str(exc), "warning")

        # Base inspection and acknowledgment use the store, so they stay on this
        # thread in target order. Only endpoint reads and projections, which run
        # user commands, overlap.
        pending, tasks = [], []
        for identity, (item, metadata) in inputs.items():
            unit = units[identity]
            push, pull = identity in admitted["push"], identity in admitted["pull"]
            effective = child_policies.get(identity, _effective_policy(unit.configured_policy, push, pull))
            lifecycle = lifecycles.get(identity.repo)
            # Store failures only matter to units that keep a Sync Base.
            warning = checkpoint_warnings.get(identity) or (
                store_warnings.get(identity.repo) if unit.eligible else None
            )
            base = BaseEvidence(
                "unavailable" if unit.eligible else "not-applicable",
                "absent" if unit.eligible else "ineligible",
                deleted=maintenance.get(identity, False),
            )
            if lifecycle is not None:
                try:
                    inspection = lifecycle.inspect(unit)
                    base = replace(
                        base, status=inspection.status, reason=inspection.reason,
                        record=inspection.record,
                    )
                    if inspection.failure is not None:
                        warning = Diagnostic("base-unavailable", str(inspection.failure), "warning")
                except (OSError, ValueError, SyncBaseStoreError) as exc:
                    warning = Diagnostic("base-unavailable", str(exc), "warning")
            if child_failures.get(identity):
                failed = Observation(
                    identity, "observation-failed", unit.configured_policy, effective,
                    context.config.file_symlink_mode, metadata.compare_repo, metadata.compare_live, base,
                    chmod=metadata.chmod, repository_path=metadata.repo_path, live_path=metadata.live_path,
                    diagnostics=tuple(Diagnostic(failure.code, failure.message) for failure in child_failures[identity]),
                )
                tasks.append(lambda failed=failed: failed)
            else:
                tasks.append(partial(_observe_file, context, identity, item, metadata, unit, effective, base))
            pending.append((identity, unit, lifecycle, base, warning))

        observations = []
        for (identity, unit, lifecycle, base, warning), observation in zip(pending, run_ordered(tasks), strict=True):
            if warning is None and observation.state == "directly-in-sync" and lifecycle is not None:
                try:
                    result = lifecycle.direct_agreement(FrozenBaseUnit(unit, observation.repository))
                    observation = replace(
                        observation, base=replace(base, acknowledged=result.acknowledged)
                    )
                    if result.failure is not None:
                        warning = Diagnostic(result.warning_code, str(result.failure), "warning")
                except (OSError, ValueError, SyncBaseStoreError) as exc:
                    warning = Diagnostic("base-acknowledgment-failed", str(exc), "warning")
            if warning is not None:
                observation = replace(observation, diagnostics=(*observation.diagnostics, warning))
            if identity in child_topology:
                repository_blockers, live_blockers, managed_children = child_topology[identity]
                observation = replace(observation, repository_blockers=repository_blockers, live_blockers=live_blockers, managed_children=managed_children)
            observations.append(observation)
            if sink is not None:
                sink.update(1)
        order = {identity: index for index, identity in enumerate(ordered_inputs)}
        observations.sort(key=lambda unit: (order[replace(unit.identity, child_path=None)], unit.identity.child_path or ""))
        return ObservedScope(tuple(observations), directional, {
            direction: value.hook_scopes for direction, value in eligibility.items()
        }, expanded_inputs, tuple(directory_censuses), tuple(guard_skips))
