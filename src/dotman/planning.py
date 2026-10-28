from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping

from dotman.collisions import (
    TrackedTargetCandidate,
    TrackedTargetOverride,
    TrackedTargetConflictError,
    operation_write_path,
    tracked_target_conflicts,
)
from dotman.command_runtime import CommandRuntime
from dotman.errors import UsageError
from dotman.manifest import deep_merge, infer_profile_os
from dotman.package_resolution import (
    resolve_package_ids,
    resolved_package_selection,
    selected_package_ids,
)
from dotman.models import (
    FullSpecSelector,
    ManagerConfig,
    HookPlan,
    PackageSelectionSourceKind,
    PackageSpec,
    package_ref_text,
    repo_qualified_target_text,
    ResolvedPackageIdentity,
    ResolvedPackageSelection,
    resolved_package_identity_key,
    resolved_package_selection_key,
    TrackedPackageEntry,
    TrackedTargetSummary,
)
from dotman.projection import (
    ProjectionContext,
    TargetMetadata,
    build_target_metadata,
    build_package_hook_env,
    build_repo_hook_env,
    resolve_target_kind,
    target_claims_path,
)
from dotman.repository import Repository, VALID_HOOK_NAMES
from dotman.templates import build_template_context, render_template_string
from dotman import tracking

if TYPE_CHECKING:
    from dotman.tracking import TrackedStateContext

HOOK_NAMES_BY_OPERATION = {
    "push": ("guard_push", "pre_push", "post_push"),
    "pull": ("guard_pull", "pre_pull", "post_pull"),
}


@dataclass(frozen=True)
class PlanningContext:
    config: ManagerConfig
    tracked_state: "TrackedStateContext"
    projection: ProjectionContext

    @property
    def repositories(self) -> Mapping[str, Repository]:
        return self.tracked_state.repositories

    @property
    def command_runtime(self) -> CommandRuntime:
        return self.projection.command_runtime


class TrackedPackageProfileConflictError(UsageError):
    def __init__(
        self,
        *,
        package_identity: ResolvedPackageIdentity,
        conflict_kind: str,
        contenders: tuple[str, ...],
    ) -> None:
        self.package_identity = package_identity
        self.conflict_kind = conflict_kind
        self.contenders = contenders
        package_label = _package_identity_label(package_identity)
        if conflict_kind == "ambiguous_implicit":
            header = f"ambiguous implicit profile contexts for {package_label}:"
        elif conflict_kind == "conflicting_explicit":
            header = f"conflicting explicit profile contexts for {package_label}:"
        else:
            header = f"conflicting profile contexts for {package_label}:"
        super().__init__("\n".join([header, *(f"  {contender}" for contender in contenders)]))


def _package_identity_label(identity: ResolvedPackageIdentity) -> str:
    return f"{identity.repo}:{package_ref_text(package_id=identity.package_id, bound_profile=identity.bound_profile)}"


def get_repository(context: PlanningContext, repo_name: str) -> Repository:
    try:
        return context.repositories[repo_name]
    except KeyError as exc:
        raise UsageError(f"unknown repo '{repo_name}'") from exc


def _merge_selection(
    selections: list[ResolvedPackageSelection],
    selection_indexes: dict[tuple[str, str, str | None, str], int],
    selection: ResolvedPackageSelection,
) -> None:
    key = resolved_package_selection_key(selection)
    existing_index = selection_indexes.get(key)
    if existing_index is None:
        selection_indexes[key] = len(selections)
        selections.append(selection)
        return
    existing = selections[existing_index]
    if selection.explicit and not existing.explicit:
        selections[existing_index] = selection


def _resolved_package_selections_from_roots(
    repo: Repository,
    *,
    root_package_ids: list[str],
    requested_profile: str,
    source_kind: PackageSelectionSourceKind,
    source_selector: str | None,
) -> list[ResolvedPackageSelection]:
    selections: list[ResolvedPackageSelection] = []
    selection_indexes: dict[tuple[str, str, str | None, str], int] = {}
    for root_package_id in root_package_ids:
        root_selection = resolved_package_selection(
            repo=repo,
            package_id=root_package_id,
            requested_profile=requested_profile,
            explicit=True,
            source_kind=source_kind,
            source_selector=source_selector,
        )
        related_package_ids = resolve_package_ids(repo, root_package_id, "package")
        for related_package_id in related_package_ids:
            if related_package_id == root_package_id:
                _merge_selection(selections, selection_indexes, root_selection)
                continue
            _merge_selection(
                selections,
                selection_indexes,
                resolved_package_selection(
                    repo=repo,
                    package_id=related_package_id,
                    requested_profile=requested_profile,
                    explicit=False,
                    source_kind="dependency",
                    owner_identity=root_selection.identity,
                    owner_selection_label=root_selection.selection_label,
                ),
            )
    return selections


def resolve_tracked_package_selections(
    planning_context: PlanningContext,
    *,
    entries_by_repo: dict[str, list[TrackedPackageEntry]] | None = None,
) -> list[ResolvedPackageSelection]:
    current_entries = (
        entries_by_repo
        if entries_by_repo is not None
        else tracking.tracked_entries_by_repo_from_bindings(
            tracking.effective_tracked_package_entries_by_repo(planning_context.tracked_state)
        )
    )
    selections: list[ResolvedPackageSelection] = []
    for repo_config in planning_context.config.ordered_repos:
        for entry in (current_entries or {}).get(repo_config.name, []):
            selections.extend(resolve_tracked_package_entry(planning_context, entry))
    return _resolve_package_profile_claims(selections)


def _resolve_package_profile_claims(selections: list[ResolvedPackageSelection]) -> list[ResolvedPackageSelection]:
    selections_by_identity: dict[tuple[str, str, str | None], list[ResolvedPackageSelection]] = defaultdict(list)
    for selection in selections:
        selections_by_identity[resolved_package_identity_key(selection.identity)].append(selection)

    retained_identity_keys: set[tuple[str, str, str | None, str]] = set()
    for identity_selections in selections_by_identity.values():
        requested_profiles = {selection.requested_profile for selection in identity_selections}
        if len(requested_profiles) == 1:
            retained_identity_keys.update(resolved_package_selection_key(selection) for selection in identity_selections)
            continue

        explicit_profiles = {selection.requested_profile for selection in identity_selections if selection.explicit}
        if len(explicit_profiles) == 1:
            explicit_profile = next(iter(explicit_profiles))
            retained_identity_keys.update(
                resolved_package_selection_key(selection)
                for selection in identity_selections
                if selection.requested_profile == explicit_profile
            )
            continue

        conflict_kind = "ambiguous_implicit" if not explicit_profiles else "conflicting_explicit"
        raise TrackedPackageProfileConflictError(
            package_identity=identity_selections[0].identity,
            conflict_kind=conflict_kind,
            contenders=tuple(
                _format_profile_conflict_contender(selection)
                for selection in _sort_profile_conflict_selections(identity_selections)
            ),
        )

    resolved: list[ResolvedPackageSelection] = []
    resolved_indexes: dict[tuple[str, str, str | None, str], int] = {}
    for selection in selections:
        if resolved_package_selection_key(selection) in retained_identity_keys:
            _merge_selection(resolved, resolved_indexes, selection)
    return resolved


def _sort_profile_conflict_selections(selections: list[ResolvedPackageSelection]) -> list[ResolvedPackageSelection]:
    return sorted(selections, key=lambda selection: (selection.selection_label, selection.owner_selection_label or ""))


def _format_profile_conflict_contender(selection: ResolvedPackageSelection) -> str:
    if selection.owner_selection_label is None:
        return selection.selection_label
    return f"{selection.selection_label} required by {selection.owner_selection_label}"


@dataclass(frozen=True)
class PackagePlanningContext:
    related_package_ids: list[str]
    resolved_packages: list[PackageSpec]
    variables: dict[str, Any]
    inferred_os: str
    context: dict[str, Any]


@dataclass(frozen=True)
class PackagePlanningInput:
    repo: Repository
    selection: ResolvedPackageSelection
    package_context: PackagePlanningContext
    target_metadata: list[TargetMetadata]



def build_package_planning_context(
    repo: Repository,
    selection: ResolvedPackageSelection,
) -> PackagePlanningContext:
    # A package renders from its own dependency closure, never its dependents':
    # `depends` means "needs", so who pulled a package in must not change it.
    related_package_ids = resolve_package_ids(repo, selection.identity.package_id, "package")
    resolved_packages = [repo.resolve_package(package_id) for package_id in related_package_ids]
    profile_vars, lineage = repo.compose_profile(selection.requested_profile)
    package_vars: dict[str, Any] = {}
    for package in resolved_packages:
        package_vars = deep_merge(package_vars, package.vars or {})
    variables = deep_merge(deep_merge(package_vars, profile_vars), repo.local_vars)
    inferred_os = infer_profile_os(selection.requested_profile, lineage, variables)
    context = build_template_context(variables, profile=selection.requested_profile, inferred_os=inferred_os)
    return PackagePlanningContext(
        related_package_ids=related_package_ids,
        resolved_packages=resolved_packages,
        variables=variables,
        inferred_os=inferred_os,
        context=context,
    )


def resolve_full_spec_selector(
    planning_context: PlanningContext,
    query: FullSpecSelector,
) -> list[ResolvedPackageSelection]:
    repo = get_repository(planning_context, query.repo)
    root_package_ids = selected_package_ids(repo, query.selector, query.selector_kind)
    return _resolved_package_selections_from_roots(
        repo,
        root_package_ids=root_package_ids,
        requested_profile=query.profile,
        source_kind="selector_query",
        source_selector=query.selector,
    )


def resolve_tracked_package_entry(
    planning_context: PlanningContext,
    entry: TrackedPackageEntry,
) -> list[ResolvedPackageSelection]:
    repo = get_repository(planning_context, entry.repo)
    if entry.package_id not in repo.packages:
        raise UsageError(f"unknown package '{entry.package_id}' in repo '{repo.config.name}'")
    return _resolved_package_selections_from_roots(
        repo,
        root_package_ids=[entry.package_id],
        requested_profile=entry.profile,
        source_kind="tracked_entry",
        source_selector=entry.package_id,
    )



def _collect_target_candidates(
    planning_context: PlanningContext,
    selections: list[ResolvedPackageSelection],
    *,
    operation: str,
    include_target_summary: bool,
) -> tuple[list[PackagePlanningInput], list[tuple[Path, TrackedTargetCandidate]]]:
    """Plan each selection's targets and list the paths they claim, in order.

    Each candidate's `plan_index` is its selection's index. A target summary
    also inspects gitignore patterns and validates declaration conflicts.
    """
    planning_inputs: list[PackagePlanningInput] = []
    candidates: list[tuple[Path, TrackedTargetCandidate]] = []
    for plan_index, selection in enumerate(selections):
        repo = get_repository(planning_context, selection.identity.repo)
        package_context = build_package_planning_context(repo, selection)
        target_metadata = build_target_metadata(
            repo=repo,
            packages=package_context.resolved_packages,
            context=package_context.context,
            selection=selection,
            operation=operation,
            inferred_os=package_context.inferred_os,
            declaration_package_ids={selection.identity.package_id},
            inspect_live_symlinks=False,
            inspect_gitignore_patterns=include_target_summary,
            validate_declaration_conflicts=include_target_summary,
        )
        planning_inputs.append(
            PackagePlanningInput(
                repo=repo,
                selection=selection,
                package_context=package_context,
                target_metadata=target_metadata,
            )
        )
        for target_index, metadata in enumerate(target_metadata):
            if not target_claims_path(metadata.target):
                continue
            target_summary = _tracked_target_summary(planning_context, metadata) if include_target_summary else None
            candidate_path = operation_write_path(
                repo_path=metadata.repo_path,
                live_path=metadata.live_path,
                operation=operation,
            )
            candidates.append((candidate_path, TrackedTargetCandidate(
                plan_index=plan_index,
                target_index=target_index,
                live_path=metadata.live_path,
                precedence=1 if selection.explicit else 0,
                precedence_name="explicit" if selection.explicit else "implicit",
                selection=selection,
                selection_label=selection.selection_label,
                package_id=metadata.package_id,
                target_name=metadata.target_name,
                target_label=repo_qualified_target_text(
                    repo_name=repo.config.name,
                    package_id=metadata.package_id,
                    target_name=metadata.target_name,
                    bound_profile=selection.bound_profile,
                ),
                target_summary=target_summary,
            )))
    return planning_inputs, candidates


def _tracked_target_summary(planning_context: PlanningContext, metadata: TargetMetadata) -> TrackedTargetSummary:
    return TrackedTargetSummary(
        target_name=metadata.target_name,
        repo_path=metadata.repo_path,
        live_path=metadata.live_path,
        target_kind=resolve_target_kind(
            target_type=metadata.target.target_type,
            repo_path=metadata.repo_path,
            live_path=metadata.live_path,
            target_label=metadata.canonical,
            file_symlink_mode=planning_context.config.file_symlink_mode,
            dir_symlink_mode=planning_context.config.dir_symlink_mode,
        ),
        render=metadata.target.render or "raw",
        capture=metadata.target.capture or "raw",
        editor=metadata.target.editor,
        compare_repo=metadata.target.compare_repo or "raw",
        compare_live=metadata.target.compare_live or metadata.compare_live,
        additional_sources=metadata.target.additional_sources,
        additional_source_entries=metadata.target.additional_source_entries,
        chmod=metadata.chmod,
    )


def _group_candidates_by_path(
    candidates: list[tuple[Path, TrackedTargetCandidate]],
) -> dict[Path, list[TrackedTargetCandidate]]:
    candidates_by_path: dict[Path, list[TrackedTargetCandidate]] = defaultdict(list)
    for candidate_path, candidate in candidates:
        candidates_by_path[candidate_path].append(candidate)
    return candidates_by_path


def collect_static_target_candidates(
    planning_context: PlanningContext,
    selections: list[ResolvedPackageSelection],
    *,
    operation: str,
) -> tuple[list[PackagePlanningInput], dict[Path, list[TrackedTargetCandidate]]]:
    planning_inputs, candidates = _collect_target_candidates(
        planning_context, selections, operation=operation, include_target_summary=False,
    )
    return planning_inputs, _group_candidates_by_path(candidates)


def collect_tracked_ownership_candidates(
    planning_context: PlanningContext,
    *,
    entries_by_repo: dict[str, list[TrackedPackageEntry]] | None = None,
    include_target_summary: bool = False,
    selections: list[ResolvedPackageSelection] | None = None,
    operation: str = "push",
) -> dict[Path, list[TrackedTargetCandidate]]:
    resolved_selections = (
        selections
        if selections is not None
        else resolve_tracked_package_selections(planning_context, entries_by_repo=entries_by_repo)
    )
    _, candidates = _collect_target_candidates(
        planning_context, resolved_selections, operation=operation, include_target_summary=include_target_summary,
    )
    # Ownership numbers candidates one by one, not by selection.
    return _group_candidates_by_path(
        [(candidate_path, replace(candidate, plan_index=candidate_index))
         for candidate_index, (candidate_path, candidate) in enumerate(candidates)]
    )



def validate_tracked_package_ownership(
    planning_context: PlanningContext,
    *,
    entries_by_repo: dict[str, list[TrackedPackageEntry]] | None = None,
) -> None:
    conflicts = tracked_package_ownership_conflicts(planning_context, entries_by_repo=entries_by_repo)
    if conflicts:
        raise conflicts[0]


def tracked_package_ownership_conflicts(
    planning_context: PlanningContext,
    *,
    entries_by_repo: dict[str, list[TrackedPackageEntry]] | None = None,
) -> list[TrackedTargetConflictError]:
    selections = resolve_tracked_package_selections(planning_context, entries_by_repo=entries_by_repo)
    return [
        conflict
        for operation in ("push", "pull")
        for conflict in tracked_target_conflicts(
            collect_tracked_ownership_candidates(
                planning_context,
                entries_by_repo=entries_by_repo,
                selections=selections,
                operation=operation,
            )
        )
    ]


def preview_package_selections_implicit_overrides(
    planning_context: PlanningContext,
    selections: list[ResolvedPackageSelection],
) -> list[TrackedTargetOverride]:
    explicit_selections = [selection for selection in selections if selection.explicit]
    if not explicit_selections:
        return []

    raw_bindings_by_repo = tracking.raw_tracked_package_entries_by_repo(planning_context.tracked_state)
    additions_by_repo: dict[str, list[FullSpecSelector]] = defaultdict(list)
    requested_selection_keys = {
        resolved_package_selection_key(selection)
        for selection in explicit_selections
    }
    for selection in explicit_selections:
        additions_by_repo[selection.identity.repo].append(
            FullSpecSelector(
                repo=selection.identity.repo,
                selector=selection.identity.package_id,
                selector_kind="package",
                profile=selection.requested_profile,
            )
        )

    for repo_name, additions in additions_by_repo.items():
        repo = get_repository(planning_context, repo_name)
        raw_bindings_by_repo[repo.config.name] = tracking.normalize_tracked_package_entry_set(
            planning_context.tracked_state,
            tracking.effective_tracked_package_entries_for_repo(
                repo,
                raw_bindings_by_repo.get(repo.config.name, []),
            ),
            additions,
        )

    candidates_by_live_path = collect_tracked_ownership_candidates(
        planning_context,
        entries_by_repo=tracking.tracked_entries_by_repo_from_bindings(
            tracking.effective_tracked_package_entries_by_repo(
                planning_context.tracked_state,
                raw_bindings_by_repo,
            )
        ),
    )

    overrides_by_package: dict[
        tuple[str, str, str | None, str],
        dict[tuple[str, str, str | None, str], TrackedTargetCandidate],
    ] = {}
    winners_by_package: dict[tuple[str, str, str | None, str], TrackedTargetCandidate] = {}
    for candidates in candidates_by_live_path.values():
        highest_precedence = max(candidate.precedence for candidate in candidates)
        winning_candidates = [candidate for candidate in candidates if candidate.precedence == highest_precedence]
        winner = next(
            (
                candidate
                for candidate in winning_candidates
                if resolved_package_selection_key(candidate.selection) in requested_selection_keys
                and candidate.selection.explicit
            ),
            None,
        )
        if winner is None:
            continue
        overridden = [
            candidate
            for candidate in candidates
            if candidate.selection.identity != winner.selection.identity and candidate.precedence_name == "implicit"
        ]
        if not overridden:
            continue
        winner_key = (
            winner.selection.identity.repo,
            winner.selection.identity.package_id,
            winner.selection.identity.bound_profile,
            winner.package_id,
        )
        winners_by_package[winner_key] = winner
        package_overrides = overrides_by_package.setdefault(winner_key, {})
        for candidate in overridden:
            contender_key = (
                candidate.selection.identity.repo,
                candidate.selection.identity.package_id,
                candidate.selection.identity.bound_profile,
                candidate.package_id,
            )
            package_overrides.setdefault(contender_key, candidate)
    return sorted(
        [
            TrackedTargetOverride(
                winner=winners_by_package[winner_key],
                overridden=tuple(
                    sorted(
                        contenders.values(),
                        key=lambda item: (
                            item.selection.identity.repo,
                            item.selection.identity.package_id,
                            "" if item.selection.identity.bound_profile is None else item.selection.identity.bound_profile,
                            item.package_id,
                        ),
                    )
                ),
            )
            for winner_key, contenders in overrides_by_package.items()
        ],
        key=lambda item: (
            item.winner.package_id,
            item.winner.selection.identity.repo,
            item.winner.selection.identity.package_id,
            "" if item.winner.selection.identity.bound_profile is None else item.winner.selection.identity.bound_profile,
        ),
    )


def plan_hooks(
    repo: Repository,
    packages: list[PackageSpec],
    context: dict[str, Any],
    *,
    selection: ResolvedPackageSelection,
    operation: str,
    inferred_os: str,
    target_plans: list[Any],
    declaration_package_ids: set[str] | None = None,
) -> dict[str, list[HookPlan]]:
    hook_names = HOOK_NAMES_BY_OPERATION.get(operation, VALID_HOOK_NAMES)
    hooks: dict[str, list[HookPlan]] = defaultdict(list)
    targets_by_owner = {
        (target.package_id, target.target_name): target
        for target in target_plans
    }
    for package in packages:
        if declaration_package_ids is not None and package.id not in declaration_package_ids:
            continue
        package_env = build_package_hook_env(
            repo=repo,
            package=package,
            selection=selection,
            operation=operation,
            inferred_os=inferred_os,
            context=context,
        )
        package_hooks = package.hooks or {}
        for hook_name in hook_names:
            hook_spec = package_hooks.get(hook_name)
            if hook_spec is None:
                continue
            for command_spec in hook_spec.commands:
                hooks[hook_name].append(
                    HookPlan(
                        hook_name=hook_name,
                        command=render_template_string(command_spec.run, context, base_dir=hook_spec.declared_in, source_path=hook_spec.declared_in).strip(),
                        cwd=hook_spec.declared_in,
                        repo_name=repo.config.name,
                        package_id=package.id,
                        scope_kind="package",
                        io=command_spec.io,
                        elevation=command_spec.elevation,
                        env=dict(package_env),
                        run_noop=hook_spec.run_noop or command_spec.run_noop,
                    )
                )
        for target_name, target_spec in (package.targets or {}).items():
            target_plan = targets_by_owner.get((package.id, target_name))
            if target_plan is None:
                continue
            target_hooks = target_spec.hooks or {}
            for hook_name in hook_names:
                hook_spec = target_hooks.get(hook_name)
                if hook_spec is None:
                    continue
                target_env = dict(target_plan.command_env or {})
                for command_spec in hook_spec.commands:
                    hooks[hook_name].append(
                        HookPlan(
                            hook_name=hook_name,
                            command=render_template_string(command_spec.run, context, base_dir=hook_spec.declared_in, source_path=hook_spec.declared_in).strip(),
                            cwd=hook_spec.declared_in,
                            repo_name=repo.config.name,
                            package_id=package.id,
                            target_name=target_name,
                            scope_kind="target",
                            io=command_spec.io,
                            elevation=command_spec.elevation,
                            env=target_env,
                            run_noop=hook_spec.run_noop or command_spec.run_noop,
                        )
                    )
    return dict(hooks)


def plan_repo_hooks(
    repo: Repository,
    *,
    operation: str,
) -> dict[str, list[HookPlan]]:
    hook_names = HOOK_NAMES_BY_OPERATION.get(operation, VALID_HOOK_NAMES)
    context = {"vars": repo.local_vars, "repo_name": repo.config.name, "operation": operation}
    env = build_repo_hook_env(repo=repo, operation=operation, context=context)
    hooks: dict[str, list[HookPlan]] = defaultdict(list)
    for hook_name in hook_names:
        if hook_name.startswith("guard_"):
            continue
        hook_spec = (repo.hooks or {}).get(hook_name)
        if hook_spec is None:
            continue
        for command_spec in hook_spec.commands:
            hooks[hook_name].append(
                HookPlan(
                    hook_name=hook_name,
                    command=render_template_string(command_spec.run, context, base_dir=hook_spec.declared_in, source_path=hook_spec.declared_in).strip(),
                    cwd=hook_spec.declared_in,
                    repo_name=repo.config.name,
                    scope_kind="repo",
                    io=command_spec.io,
                    elevation=command_spec.elevation,
                    env=dict(env),
                    run_noop=hook_spec.run_noop or command_spec.run_noop,
                )
            )
    return dict(hooks)



