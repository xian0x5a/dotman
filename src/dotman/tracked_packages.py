from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, TYPE_CHECKING

from dotman.collisions import resolve_tracked_target_winners
from dotman.config import expand_path
from dotman.manifest import deep_merge, infer_profile_os
from dotman.projection import default_compare_live, resolve_target_kind, validate_probe_target_config
from dotman.models import (
    FullSpecSelector,
    SelectorKind,
    TrackedPackageEntrySummary,
    TrackedOwnedTargetDetail,
    TrackedPackageEntryDetail,
    TrackedPackageDetail,
    TrackedTargetSummary,
    PackageSpec,
    package_ref_text,
    repo_qualified_target_text,
)
from dotman.repository import Repository
from dotman.resolver import build_target_match_fields
from dotman.package_resolution import (
    bound_profile_for_package,
    parse_full_spec_selector_text,
    parse_package_ref_text,
    resolved_package_selection,
    selected_package_ids,
)
from dotman.templates import build_template_context, render_template_string
from dotman.tracking import (
    TrackedStateContext,
    candidate_repositories,
    get_repository,
    iter_tracked_package_entries,
    read_effective_tracked_package_entries,
    tracked_entries_by_repo_from_bindings,
)

if TYPE_CHECKING:
    from dotman.planning import PlanningContext


@dataclass(frozen=True)
class TrackedTargetMatch:
    repo_name: str
    package_id: str
    target_name: str
    # Probe targets have no repository path; their kind is "probe".
    repo_path: Path | None
    target_kind: str
    bound_profile: str | None = None


@dataclass(frozen=True)
class _TrackedPackageOwnershipDetail:
    effective_binding_keys: set[tuple[str, str, str]]
    owned_targets: list[TrackedOwnedTargetDetail]


def resolve_tracked_package(
    context: TrackedStateContext,
    package_text: str,
) -> tuple[Repository, str, str | None]:
    selector, bound_profile, exact_matches, partial_matches = find_tracked_package_matches(
        context,
        package_text,
    )
    if len(exact_matches) == 1:
        return exact_matches[0]
    if len(exact_matches) > 1:
        candidates = ", ".join(
            f"{repo.config.name}:{package_ref_text(package_id=package_id, bound_profile=match_bound_profile)}"
            for repo, package_id, match_bound_profile in exact_matches
        )
        if len({repo.config.name for repo, _package_id, _match_bound_profile in exact_matches}) > 1:
            raise ValueError(f"tracked package '{selector}' is defined in multiple repos: {candidates}")
        raise ValueError(f"tracked package '{selector}' is ambiguous: {candidates}")

    if len(partial_matches) == 1:
        repo, package_id, match_bound_profile = partial_matches[0]
        raise ValueError(
            f"no exact match for '{selector}'; use exact name '"
            f"{repo.config.name}:{package_ref_text(package_id=package_id, bound_profile=match_bound_profile)}'"
        )
    if len(partial_matches) > 1:
        candidates = ", ".join(
            f"{repo.config.name}:{package_ref_text(package_id=package_id, bound_profile=match_bound_profile)}"
            for repo, package_id, match_bound_profile in partial_matches
        )
        raise ValueError(f"tracked package '{selector}' is ambiguous: {candidates}")
    raise ValueError(f"tracked package '{selector}' did not match any tracked package")


def describe_tracked_package(context: "PlanningContext", package_text: str) -> TrackedPackageDetail:
    repo, package_id, bound_profile = resolve_tracked_package(
        context.tracked_state,
        package_text,
    )
    ownership = describe_tracked_package_target_ownership(
        context,
        repo.config.name,
        package_id,
        bound_profile,
    )
    details: list[TrackedPackageEntryDetail] = []
    description = repo.resolve_package(package_id).description

    for candidate_repo, binding, selector_kind, package_ids in iter_tracked_package_entries(
        context.tracked_state
    ):
        if candidate_repo.config.name != repo.config.name or package_id not in package_ids:
            continue
        if bound_profile_for_package(candidate_repo, package_id, binding.profile) != bound_profile:
            continue
        details.append(
            describe_tracked_package_entry(
                context,
                candidate_repo,
                binding,
                selector_kind,
                package_id,
                package_ids,
                executable=(binding.repo, binding.selector, binding.profile) in ownership.effective_binding_keys,
            )
        )

    if not details:
        package_ref = package_ref_text(package_id=package_id, bound_profile=bound_profile)
        raise ValueError(f"package '{repo.config.name}:{package_ref}' is not currently tracked")

    return TrackedPackageDetail(
        repo=repo.config.name,
        package_id=package_id,
        description=description,
        package_entries=sorted(
            details,
            key=lambda item: (
                item.package_entry.selector,
                item.package_entry.profile,
                item.package_entry.repo,
            ),
        ),
        owned_targets=ownership.owned_targets,
        bound_profile=bound_profile,
    )


def find_tracked_target_matches(
    context: "PlanningContext",
    target_text: str,
    *,
    include_probes: bool = False,
) -> tuple[str, list[TrackedTargetMatch], list[TrackedTargetMatch]]:
    """Match tracked targets; probes only when the caller selects identities, not paths."""
    _explicit_repo, selector, profile = parse_full_spec_selector_text(target_text)
    if profile is not None:
        raise ValueError("tracked target lookup expects a target selector, not a binding")
    if "." in selector:
        package_query, separator, target_name = selector.partition(".")
        if not separator or not package_query or not target_name:
            raise ValueError(
                f"invalid tracked target selector '{target_text}'; expected [<repo>:]<package>.<target>"
            )
        parse_package_ref_text(package_query)

    tracked_targets = list_tracked_targets(context)
    if include_probes:
        tracked_targets += list_tracked_probe_targets(context)
    exact_matches: list[TrackedTargetMatch] = []
    partial_matches: list[TrackedTargetMatch] = []
    for candidate in tracked_targets:
        match_fields = build_target_match_fields(
            repo_name=candidate.repo_name,
            package_id=candidate.package_id,
            target_name=candidate.target_name,
            bound_profile=candidate.bound_profile,
        )
        if candidate.target_kind == "probe":
            # Probes join sync input matching only as [repo:]package.target; a
            # bare probe or profile name would make a same-named package input ambiguous.
            match_fields = tuple(field for field in match_fields if field.endswith(f".{candidate.target_name}"))
        if any(field == target_text for field in match_fields):
            exact_matches.append(candidate)
            continue
        if any(target_text in field for field in match_fields):
            partial_matches.append(candidate)
    return target_text, exact_matches, partial_matches


def list_tracked_targets(context: "PlanningContext") -> list[TrackedTargetMatch]:
    from dotman import planning

    tracked_targets: dict[tuple[str, str, str | None, str], TrackedTargetMatch] = {}
    candidates_by_live_path = planning.collect_tracked_ownership_candidates(
        context,
        include_target_summary=True,
    )
    winner_indexes = resolve_tracked_target_winners(candidates_by_live_path)
    for candidates in candidates_by_live_path.values():
        for candidate in candidates:
            if (candidate.plan_index, candidate.target_index) not in winner_indexes:
                continue
            if candidate.target_summary is None:
                continue
            if candidate.target_summary.repo_path is None:
                raise RuntimeError("tracked ownership target unexpectedly lacks a repository path")
            key = (
                candidate.selection.identity.repo,
                candidate.package_id,
                candidate.selection.identity.bound_profile,
                candidate.target_name,
            )
            tracked_targets.setdefault(
                key,
                TrackedTargetMatch(
                    repo_name=candidate.selection.identity.repo,
                    package_id=candidate.package_id,
                    target_name=candidate.target_name,
                    repo_path=candidate.target_summary.repo_path,
                    target_kind=candidate.target_summary.target_kind,
                    bound_profile=candidate.selection.identity.bound_profile,
                ),
            )
    return sorted(
        tracked_targets.values(),
        key=lambda item: (
            item.target_name,
            item.repo_name,
            item.package_id,
            "" if item.bound_profile is None else item.bound_profile,
        ),
    )


def list_tracked_probe_targets(context: "PlanningContext") -> list[TrackedTargetMatch]:
    """Probes claim no path, so path ownership never lists them."""
    from dotman import planning

    return [
        TrackedTargetMatch(
            repo_name=selection.identity.repo,
            package_id=selection.identity.package_id,
            target_name=target_name,
            repo_path=None,
            target_kind="probe",
            bound_profile=selection.identity.bound_profile,
        )
        for selection in planning.resolve_tracked_package_selections(context)
        for target_name, target in (
            context.repositories[selection.identity.repo].resolve_package(selection.identity.package_id).targets or {}
        ).items()
        if target.probe is not None and not target.disabled
    ]


def find_tracked_package_matches(
    context: TrackedStateContext,
    package_text: str,
) -> tuple[str, str | None, list[tuple[Repository, str, str | None]], list[tuple[Repository, str, str | None]]]:
    explicit_repo, selector, bound_profile = parse_package_ref_text(package_text)
    candidate_repos = candidate_repositories(context, explicit_repo)
    tracked_package_ids = {
        (
            repo.config.name,
            package_id,
            bound_profile_for_package(repo, package_id, binding.profile),
        ): repo
        for repo, binding, _selector_kind, package_ids in iter_tracked_package_entries(context)
        if repo in candidate_repos
        for package_id in package_ids
    }
    exact_matches = [
        (repo, package_id, match_bound_profile)
        for (repo_name, package_id, match_bound_profile), repo in tracked_package_ids.items()
        if package_id == selector and repo_name == repo.config.name
        and (bound_profile is None or match_bound_profile == bound_profile)
    ]
    partial_matches = [
        (repo, package_id, match_bound_profile)
        for (_repo_name, package_id, match_bound_profile), repo in tracked_package_ids.items()
        if selector in package_ref_text(package_id=package_id, bound_profile=match_bound_profile)
        and (bound_profile is None or match_bound_profile == bound_profile)
    ]
    unique_partials = {
        (repo.config.name, package_id, match_bound_profile): (repo, package_id, match_bound_profile)
        for repo, package_id, match_bound_profile in partial_matches
    }
    return selector, bound_profile, exact_matches, list(unique_partials.values())



def describe_tracked_package_entry(
    planning_context: "PlanningContext",
    repo: Repository,
    binding: FullSpecSelector,
    selector_kind: SelectorKind,
    package_id: str,
    package_ids: list[str],
    *,
    executable: bool,
) -> TrackedPackageEntryDetail:
    context = _tracked_package_entry_template_context(repo, binding, package_ids)
    package = repo.resolve_package(package_id)
    from dotman import planning

    selection = resolved_package_selection(
        repo=repo,
        package_id=package_id,
        requested_profile=binding.profile,
        explicit=package_id in selected_package_ids(repo, binding.selector, selector_kind),
        source_kind="tracked_entry",
        source_selector=binding.selector,
    )
    hooks = (
        planning.plan_hooks(
            repo,
            [package],
            context.context,
            selection=selection,
            operation="push",
            inferred_os=context.inferred_os,
            variables=context.variables,
            target_plans=[],
        )
        if executable
        else {}
    )
    targets = summarize_targets(
        repo,
        package,
        context.context,
        bound_profile=selection.identity.bound_profile,
        file_symlink_mode=planning_context.config.file_symlink_mode,
        dir_symlink_mode=planning_context.config.dir_symlink_mode,
    )
    tracked_reason = "explicit" if selection.explicit else "implicit"

    return TrackedPackageEntryDetail(
        package_entry=TrackedPackageEntrySummary(
            repo=repo.config.name,
            selector=binding.selector,
            profile=binding.profile,
            selector_kind=selector_kind,
        ),
        tracked_reason=tracked_reason,
        targets=targets,
        hooks=hooks,
    )


@dataclass(frozen=True)
class _TrackedPackageEntryTemplateContext:
    context: dict[str, Any]
    variables: dict[str, Any]
    inferred_os: str


def _tracked_package_entry_template_context(
    repo: Repository,
    binding: FullSpecSelector,
    package_ids: list[str],
) -> _TrackedPackageEntryTemplateContext:
    resolved_packages = [repo.resolve_package(candidate_id) for candidate_id in package_ids]
    profile_vars, lineage = repo.compose_profile(binding.profile)
    package_vars: dict[str, Any] = {}
    for package in resolved_packages:
        package_vars = deep_merge(package_vars, package.vars or {})
    variables = deep_merge(deep_merge(package_vars, profile_vars), repo.local_vars)
    inferred_os = infer_profile_os(binding.profile, lineage, variables)
    context = build_template_context(variables, profile=binding.profile, inferred_os=inferred_os)
    return _TrackedPackageEntryTemplateContext(
        context=context,
        variables=variables,
        inferred_os=inferred_os,
    )



def summarize_targets(
    repo: Repository,
    package: PackageSpec,
    context: dict[str, Any],
    *,
    bound_profile: str | None = None,
    file_symlink_mode: str = "prompt",
    dir_symlink_mode: str = "fail",
) -> list[TrackedTargetSummary]:
    target_summaries: list[TrackedTargetSummary] = []
    for target in (package.targets or {}).values():
        if target.disabled:
            continue
        target_label = repo_qualified_target_text(
            repo_name=repo.config.name, package_id=package.id, target_name=target.name, bound_profile=bound_profile,
        )
        if target.probe is not None:
            validate_probe_target_config(repo_name=repo.config.name, package=package, target=target)
            target_summaries.append(
                TrackedTargetSummary(
                    target_name=target.name,
                    repo_path=None,
                    live_path=None,
                    target_kind="probe",
                    probe_command=render_template_string(target.probe, context, base_dir=target.declared_in, source_path=target.declared_in),
                )
            )
            continue
        if target.source is None or target.path is None:
            raise ValueError(f"target '{target_label}' must define source and path")
        rendered_source = render_template_string(target.source, context, base_dir=target.declared_in, source_path=target.declared_in)
        rendered_path = render_template_string(target.path, context, base_dir=target.declared_in, source_path=target.declared_in)
        repo_path = (target.declared_in / rendered_source).resolve()
        live_path = expand_path(rendered_path, dereference=False)
        capture_command = (
            render_template_string(target.capture, context, base_dir=target.declared_in, source_path=target.declared_in)
            if target.capture is not None
            else None
        )
        target_summaries.append(
            TrackedTargetSummary(
                target_name=target.name,
                repo_path=repo_path,
                live_path=live_path,
                target_kind=resolve_target_kind(
                    target_type=target.target_type,
                    repo_path=repo_path,
                    live_path=live_path,
                    target_label=target_label,
                    file_symlink_mode=file_symlink_mode,
                    dir_symlink_mode=dir_symlink_mode,
                ),
                render=target.render or "raw",
                capture=target.capture or "raw",
                editor=target.editor,
                compare_repo=target.compare_repo or "raw",
                compare_live=target.compare_live or default_compare_live(capture_command),
                additional_sources=target.additional_sources,
                chmod=target.chmod,
            )
        )
    return target_summaries




def describe_tracked_package_target_ownership(
    context: "PlanningContext",
    repo_name: str,
    package_id: str,
    bound_profile: str | None,
) -> _TrackedPackageOwnershipDetail:
    from dotman import planning

    effective_binding_keys: set[tuple[str, str, str]] = set()
    owned_targets: list[TrackedOwnedTargetDetail] = []
    # `info tracked <repo:pkg>` reports ownership within the selected repo. Full tracked-state
    # validation still checks cross-repo live-path ownership globally.
    repo = get_repository(context.tracked_state, repo_name)
    candidates_by_live_path = planning.collect_tracked_ownership_candidates(
        context,
        entries_by_repo=tracked_entries_by_repo_from_bindings(
            {repo_name: read_effective_tracked_package_entries(repo)}
        ),
        include_target_summary=True,
    )
    winner_indexes = resolve_tracked_target_winners(candidates_by_live_path)
    for candidates in candidates_by_live_path.values():
        for winner in candidates:
            if (winner.plan_index, winner.target_index) not in winner_indexes:
                continue
            if winner.selection.identity.repo != repo_name:
                continue
            if winner.package_id != package_id:
                continue
            if winner.selection.identity.bound_profile != bound_profile:
                continue
            if winner.target_summary is None:
                continue
            # Dependency-owned targets use package id as owner key, matching push planning's
            # dependency selection shape without needing live-file action planning.
            owner_selector = winner.selection.source_selector or winner.package_id
            effective_binding_keys.add((winner.selection.identity.repo, owner_selector, winner.selection.requested_profile))
            owned_targets.append(
                TrackedOwnedTargetDetail(
                    package_entry=TrackedPackageEntrySummary(
                        repo=winner.selection.identity.repo,
                        selector=owner_selector,
                        profile=winner.selection.requested_profile,
                        selector_kind="package",
                    ),
                    target=winner.target_summary,
                )
            )

    return _TrackedPackageOwnershipDetail(
        effective_binding_keys=effective_binding_keys,
        owned_targets=sorted(
            owned_targets,
            key=lambda item: (
                item.target.target_name,
                item.package_entry.profile,
                item.package_entry.selector,
                item.package_entry.repo,
            ),
        ),
    )


