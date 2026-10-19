from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping, TYPE_CHECKING

import tomlkit

from dotman.atomic_files import write_text_atomic
from dotman.toml_utils import load_toml_file
from dotman.models import (
    FullSpecSelector,
    TrackableCatalogEntry,
    TrackableGroupDetail,
    TrackableGroupMemberDetail,
    TrackablePackageDetail,
    TrackableTargetDetail,
    TrackedPackageEntry,
    TrackedPackageEntrySummary,
    TrackedPackageSummary,
    TrackedPackageEntryIssue,
    ManagerConfig,
    SelectorKind,
)
from dotman.package_resolution import (
    bound_profile_for_package,
    parse_full_spec_selector_text,
    resolve_package_ids,
)
from dotman.repository import Repository

if TYPE_CHECKING:
    from dotman.planning import PlanningContext

TRACKED_PACKAGES_FILE_NAME = "tracked-packages.toml"
TRACKED_PACKAGES_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class TrackedStateContext:
    config: ManagerConfig
    repositories: Mapping[str, Repository]
    state_root: Path

    def __post_init__(self) -> None:
        object.__setattr__(self, "repositories", MappingProxyType(dict(self.repositories)))


def get_repository(context: TrackedStateContext, repo_name: str) -> Repository:
    try:
        return context.repositories[repo_name]
    except KeyError as exc:
        raise ValueError(f"unknown repo '{repo_name}'") from exc


def candidate_repositories(
    context: TrackedStateContext,
    repo_name: str | None = None,
) -> list[Repository]:
    if repo_name is not None:
        return [get_repository(context, repo_name)]
    return [context.repositories[repo.name] for repo in context.config.ordered_repos]


class TrackedPackagesFileError(ValueError):
    def __init__(self, path: Path, detail: str) -> None:
        self.path = path
        self.detail = detail
        super().__init__(detail)

    def __str__(self) -> str:
        return f"tracked packages file '{self.path}' {self.detail}"


class PersistedTrackedPackageEntryResolutionError(ValueError):
    def __init__(self, *, reason: str, message: str) -> None:
        self.reason = reason
        self.message = message
        super().__init__(message)


@dataclass(frozen=True)
class PersistedTrackedPackageEntryRecord:
    state_key: str
    state_dir: Path
    package_entry: FullSpecSelector
    repo: Repository | None = None
    selector_kind: SelectorKind | None = None
    package_ids: tuple[str, ...] = ()
    issue: TrackedPackageEntryIssue | None = None


@dataclass(frozen=True)
class TrackedStateSummary:
    packages: list[TrackedPackageSummary]
    invalid_package_entries: list[TrackedPackageEntryIssue]


def iter_trackable_catalog_entries(context: TrackedStateContext):
    for repo in candidate_repositories(context):
        for package_id, package in sorted(repo.packages.items()):
            yield repo, TrackableCatalogEntry(
                kind="package",
                repo=repo.config.name,
                selector=package_id,
                description=package.description,
                binding_mode=package.binding_mode,
            )
        for group_id, group in sorted(repo.groups.items()):
            yield repo, TrackableCatalogEntry(
                kind="group",
                repo=repo.config.name,
                selector=group_id,
                description=group.description,
                member_count=len(group.members),
            )


def list_trackables(context: TrackedStateContext) -> list[TrackableCatalogEntry]:
    return [trackable for _repo, trackable in iter_trackable_catalog_entries(context)]



def list_tracked_state(context: TrackedStateContext) -> TrackedStateSummary:
    return TrackedStateSummary(
        packages=list_tracked_packages(context),
        invalid_package_entries=sorted_tracked_package_entry_issues(
            [
                *list_invalid_explicit_package_entries(context),
                *list_orphan_explicit_package_entries(context),
            ]
        ),
    )



def list_invalid_explicit_package_entries(
    context: TrackedStateContext,
    *,
    bindings_by_repo: dict[str, list[FullSpecSelector]] | None = None,
) -> list[TrackedPackageEntryIssue]:
    _valid_records, invalid_records = configured_persisted_tracked_package_entry_records(
        context,
        bindings_by_repo=bindings_by_repo,
    )
    return sorted_tracked_package_entry_issues(
        [record.issue for record in invalid_records if record.issue is not None]
    )



def list_orphan_explicit_package_entries(context: TrackedStateContext) -> list[TrackedPackageEntryIssue]:
    return sorted_tracked_package_entry_issues(
        [
            record.issue
            for record in orphan_persisted_tracked_package_entry_records(context)
            if record.issue is not None
        ]
    )



def list_tracked_packages(context: TrackedStateContext) -> list[TrackedPackageSummary]:
    tracked_packages: dict[tuple[str, str, str | None], TrackedPackageSummary] = {}
    package_states: dict[tuple[str, str, str | None], str] = {}
    for repo, binding, selector_kind, package_ids in iter_tracked_package_entries(context):
        binding_summary = TrackedPackageEntrySummary(
            repo=repo.config.name,
            selector=binding.selector,
            profile=binding.profile,
            selector_kind=selector_kind,
        )
        for package_id in package_ids:
            package = repo.resolve_package(package_id)
            bound_profile = bound_profile_for_package(repo, package_id, binding.profile)
            key = (repo.config.name, package_id, bound_profile)
            package_state = "explicit" if selector_kind == "package" and binding.selector == package_id else "implicit"
            existing = tracked_packages.get(key)
            if existing is None:
                tracked_packages[key] = TrackedPackageSummary(
                    repo=repo.config.name,
                    package_id=package_id,
                    description=package.description,
                    package_entries=[binding_summary],
                    state=package_state,
                    bound_profile=bound_profile,
                )
                package_states[key] = package_state
                continue
            if binding_summary not in existing.package_entries:
                existing.package_entries.append(binding_summary)
            if package_state == "explicit":
                package_states[key] = "explicit"

    return [
        TrackedPackageSummary(
            repo=summary.repo,
            package_id=summary.package_id,
            description=summary.description,
            package_entries=sorted(summary.package_entries, key=lambda item: (item.selector, item.profile, item.repo)),
            state=package_states[key],
            bound_profile=summary.bound_profile,
        )
        for key, summary in sorted(
            tracked_packages.items(),
            key=lambda item: (
                0 if package_states[item[0]] == "explicit" else 1,
                item[0][0],
                item[0][1],
                "" if item[0][2] is None else item[0][2],
            ),
        )
    ]


def _tracked_instances_for_package(
    context: TrackedStateContext,
    *,
    repo_name: str,
    package_id: str,
) -> list[TrackedPackageSummary]:
    return [
        package
        for package in list_tracked_packages(context)
        if package.repo == repo_name and package.package_id == package_id
    ]


def describe_trackable(
    context: TrackedStateContext,
    *,
    repo_name: str,
    selector: str,
    selector_kind: SelectorKind,
) -> TrackablePackageDetail | TrackableGroupDetail:
    repo = get_repository(context, repo_name)
    if selector_kind == "package":
        return describe_trackable_package(context, repo=repo, package_id=selector)
    return describe_trackable_group(context, repo=repo, group_id=selector)


def describe_trackable_package(
    context: TrackedStateContext,
    *,
    repo: Repository,
    package_id: str,
) -> TrackablePackageDetail:
    package = repo.resolve_package(package_id)
    targets = [
        TrackableTargetDetail(
            target_name=target_name,
            source=target.source,
            path=target.path,
            target_type=target.target_type,
            probe_command=target.probe,
            render=target.render or "raw",
            capture=target.capture or "raw",
            editor=target.editor,
            compare_repo=target.compare_repo,
            compare_live=target.compare_live,
            additional_sources=target.additional_sources,
            chmod=target.chmod,
        )
        for target_name, target in sorted((package.targets or {}).items())
        if not target.disabled
    ]
    return TrackablePackageDetail(
        repo=repo.config.name,
        selector=package_id,
        description=package.description,
        binding_mode=package.binding_mode,
        tracked_instances=_tracked_instances_for_package(
            context,
            repo_name=repo.config.name,
            package_id=package_id,
        ),
        targets=targets,
    )


def describe_trackable_group(
    context: TrackedStateContext,
    *,
    repo: Repository,
    group_id: str,
) -> TrackableGroupDetail:
    return TrackableGroupDetail(
        repo=repo.config.name,
        selector=group_id,
        members=[
            TrackableGroupMemberDetail(
                package_id=package_id,
                tracked_instances=_tracked_instances_for_package(
                    context,
                    repo_name=repo.config.name,
                    package_id=package_id,
                ),
            )
            for package_id in repo.expand_group(group_id)
        ],
    )


def tracked_packages_file_path(state_dir: Path) -> Path:
    return state_dir / TRACKED_PACKAGES_FILE_NAME

def read_tracked_packages_file(state_path: Path) -> list[TrackedPackageEntry]:
    if not state_path.exists():
        return []
    payload = load_toml_file(state_path, context="tracked packages file")
    schema_version = payload.get("schema_version")
    if schema_version != TRACKED_PACKAGES_SCHEMA_VERSION:
        raise TrackedPackagesFileError(state_path, f"must declare schema_version = {TRACKED_PACKAGES_SCHEMA_VERSION}")
    packages_payload = payload.get("packages", [])
    if not isinstance(packages_payload, list):
        raise TrackedPackagesFileError(state_path, "must declare packages as an array of tables")
    # The file is a set of entries. A repeated row (hand edit, merge) would otherwise
    # surface as an ambiguous duplicate and survive a single-row untrack.
    return list(
        dict.fromkeys(
            _tracked_package_entry_from_row(state_path, row_number, package_payload)
            for row_number, package_payload in enumerate(packages_payload, start=1)
        )
    )


def _tracked_package_entry_from_row(state_path: Path, row_number: int, package_payload: object) -> TrackedPackageEntry:
    if not isinstance(package_payload, dict):
        raise TrackedPackagesFileError(state_path, f"must declare packages row {row_number} as a table")
    for key in ("repo", "package_id", "profile"):
        if not isinstance(package_payload.get(key), str):
            raise TrackedPackagesFileError(state_path, f"must declare string '{key}' in packages row {row_number}")
    return TrackedPackageEntry(
        repo=package_payload["repo"],
        package_id=package_payload["package_id"],
        profile=package_payload["profile"],
    )


def read_tracked_package_entries_file(state_path: Path) -> list[FullSpecSelector]:
    return [
        FullSpecSelector(repo=entry.repo, selector=entry.package_id, selector_kind="package", profile=entry.profile)
        for entry in read_tracked_packages_file(state_path)
    ]



def read_tracked_package_entries(repo: Repository) -> list[FullSpecSelector]:
    return read_tracked_package_entries_file(tracked_packages_file_path(repo.config.state_path))



def read_effective_tracked_package_entries(repo: Repository) -> list[FullSpecSelector]:
    return effective_tracked_package_entries_for_repo(repo, read_tracked_package_entries(repo))



def expand_tracked_package_entry(
    context: TrackedStateContext,
    binding: FullSpecSelector,
) -> list[FullSpecSelector]:
    repo = get_repository(context, binding.repo)
    return expand_tracked_package_entry_in_repo(repo, binding)



def raw_tracked_package_entries_by_repo(
    context: TrackedStateContext,
) -> dict[str, list[FullSpecSelector]]:
    return {
        repo_config.name: read_tracked_package_entries(
            get_repository(context, repo_config.name)
        )
        for repo_config in context.config.ordered_repos
    }



def effective_tracked_package_entries_by_repo(
    context: TrackedStateContext,
    raw_entries_by_repo: dict[str, list[FullSpecSelector]] | None = None,
) -> dict[str, list[FullSpecSelector]]:
    current_raw_bindings = raw_tracked_package_entries_by_repo(context) if raw_entries_by_repo is None else raw_entries_by_repo
    return {
        repo_config.name: effective_tracked_package_entries_for_repo(
            get_repository(context, repo_config.name),
            current_raw_bindings.get(repo_config.name, []),
        )
        for repo_config in context.config.ordered_repos
    }



def tracked_package_entry_scope_key(
    repo: Repository,
    binding: FullSpecSelector,
) -> tuple[str, str, str | None]:
    if binding.selector in repo.packages and repo.package_binding_mode(binding.selector) == "multi_instance":
        return (binding.repo, binding.selector, binding.profile)
    return (binding.repo, binding.selector, None)



def normalize_tracked_package_entries(
    context: TrackedStateContext,
    bindings: list[FullSpecSelector],
    package_entry: FullSpecSelector,
) -> list[FullSpecSelector]:
    repo = get_repository(context, package_entry.repo)
    return _normalize_tracked_package_entries_in_repo(repo, bindings, package_entry)


def _normalize_tracked_package_entries_in_repo(
    repo: Repository,
    bindings: list[FullSpecSelector],
    package_entry: FullSpecSelector,
) -> list[FullSpecSelector]:
    target_scope = tracked_package_entry_scope_key(repo, package_entry)
    updated = False
    normalized: list[FullSpecSelector] = []
    for existing in bindings:
        if tracked_package_entry_scope_key(repo, existing) == target_scope:
            if not updated:
                normalized.append(package_entry)
                updated = True
            continue
        normalized.append(existing)
    if not updated:
        normalized.append(package_entry)
    return normalized



def normalize_tracked_package_entry_set(
    context: TrackedStateContext,
    bindings: list[FullSpecSelector],
    additions: list[FullSpecSelector],
) -> list[FullSpecSelector]:
    normalized = list(bindings)
    for binding in additions:
        normalized = normalize_tracked_package_entries(context, normalized, binding)
    return normalized



def expand_tracked_package_entry_in_repo(repo: Repository, package_entry: FullSpecSelector) -> list[FullSpecSelector]:
    if package_entry.profile not in repo.profiles:
        raise PersistedTrackedPackageEntryResolutionError(reason="unknown_profile", message="unknown profile")
    package_match = package_entry.selector in repo.packages
    group_match = package_entry.selector in repo.groups
    if package_match and group_match:
        raise PersistedTrackedPackageEntryResolutionError(reason="selector_kind_invalid", message="selector kind invalid")
    if not package_match and not group_match:
        raise PersistedTrackedPackageEntryResolutionError(reason="unknown_selector", message="unknown selector")
    if package_match:
        return [package_entry]
    try:
        package_ids = repo.expand_group(package_entry.selector)
    except ValueError as exc:
        raise PersistedTrackedPackageEntryResolutionError(
            reason="dependency_resolution_failed",
            message="dependency resolution failed",
        ) from exc
    return [
        FullSpecSelector(
            repo=package_entry.repo,
            selector=package_id,
            selector_kind="package",
            profile=package_entry.profile,
        )
        for package_id in package_ids
    ]



def effective_tracked_package_entries_for_repo(
    repo: Repository,
    raw_bindings: list[FullSpecSelector],
) -> list[FullSpecSelector]:
    effective_bindings: list[FullSpecSelector] = []
    for binding in raw_bindings:
        try:
            expanded_bindings = expand_tracked_package_entry_in_repo(repo, binding)
        except PersistedTrackedPackageEntryResolutionError:
            continue
        for expanded_binding in expanded_bindings:
            effective_bindings = _normalize_tracked_package_entries_in_repo(
                repo,
                effective_bindings,
                expanded_binding,
            )
    return effective_bindings



def persisted_tracked_package_entries_for_repo(
    repo: Repository,
    raw_bindings: list[FullSpecSelector],
) -> list[FullSpecSelector]:
    # Rows that do not resolve right now (package on another branch, profile removed)
    # are kept verbatim so they stay listed and removable after unrelated writes.
    unresolved_bindings: list[FullSpecSelector] = []
    for binding in raw_bindings:
        try:
            expand_tracked_package_entry_in_repo(repo, binding)
        except PersistedTrackedPackageEntryResolutionError:
            unresolved_bindings.append(binding)
    return [*effective_tracked_package_entries_for_repo(repo, raw_bindings), *unresolved_bindings]


def tracked_entries_by_repo_from_bindings(
    bindings_by_repo: dict[str, list[FullSpecSelector]] | None,
) -> dict[str, list[TrackedPackageEntry]] | None:
    if bindings_by_repo is None:
        return None
    return {
        repo_name: [
            TrackedPackageEntry(
                repo=binding.repo,
                package_id=binding.selector,
                profile=binding.profile,
            )
            for binding in bindings
        ]
        for repo_name, bindings in bindings_by_repo.items()
    }


def validate_tracked_package_entries(
    context: "PlanningContext",
    bindings_by_repo: dict[str, list[FullSpecSelector]],
) -> None:
    from dotman import planning

    planning.validate_tracked_package_ownership(
        context,
        entries_by_repo=tracked_entries_by_repo_from_bindings(bindings_by_repo),
    )



def _skips_conflict_validation(
    context: TrackedStateContext,
    raw_entries_by_repo: dict[str, list[FullSpecSelector]],
    rewritten_repo_names: set[str],
) -> bool:
    # Validation is still skipped while rows are invalid (#183), but the guard sees
    # rewritten repos without the unresolved rows they keep, as when those rows were
    # dropped; keeping them must not switch validation off.
    guard_bindings = {
        repo_name: effective_tracked_package_entries_for_repo(get_repository(context, repo_name), rows)
        if repo_name in rewritten_repo_names else rows
        for repo_name, rows in raw_entries_by_repo.items()
    }
    return bool(list_invalid_explicit_package_entries(context, bindings_by_repo=guard_bindings))



def record_tracked_package_entry(
    context: "PlanningContext",
    binding: FullSpecSelector,
    *,
    validate: bool = True,
) -> None:
    state = context.tracked_state
    repo = get_repository(state, binding.repo)
    raw_entries_by_repo = raw_tracked_package_entries_by_repo(state)
    normalized = normalize_tracked_package_entry_set(
        state,
        persisted_tracked_package_entries_for_repo(repo, raw_entries_by_repo.get(repo.config.name, [])),
        expand_tracked_package_entry_in_repo(repo, binding),
    )
    raw_entries_by_repo[repo.config.name] = normalized
    if validate and not _skips_conflict_validation(state, raw_entries_by_repo, {repo.config.name}):
        validate_tracked_package_entries(
            context,
            effective_tracked_package_entries_by_repo(state, raw_entries_by_repo),
        )
    write_tracked_package_entries(repo, normalized)



def validate_tracked_package_entry(
    context: "PlanningContext",
    binding: FullSpecSelector,
) -> None:
    state = context.tracked_state
    repo = get_repository(state, binding.repo)
    raw_entries_by_repo = raw_tracked_package_entries_by_repo(state)
    raw_entries_by_repo[repo.config.name] = normalize_tracked_package_entry_set(
        state,
        persisted_tracked_package_entries_for_repo(repo, raw_entries_by_repo.get(repo.config.name, [])),
        expand_tracked_package_entry_in_repo(repo, binding),
    )
    if not _skips_conflict_validation(state, raw_entries_by_repo, {repo.config.name}):
        validate_tracked_package_entries(
            context,
            effective_tracked_package_entries_by_repo(state, raw_entries_by_repo),
        )



def find_persisted_tracked_package_entry_matches(
    context: TrackedStateContext,
    binding_text: str,
) -> tuple[str, str | None, list[PersistedTrackedPackageEntryRecord], list[PersistedTrackedPackageEntryRecord]]:
    explicit_repo, selector, profile = parse_full_spec_selector_text(binding_text)
    tracked_records = [
        record for record in all_persisted_tracked_package_entry_records(context)
        if record.issue is None or record.issue.state in {"invalid", "orphan"}
    ]
    if explicit_repo is not None:
        tracked_records = [record for record in tracked_records if record.package_entry.repo == explicit_repo]
    if profile is not None:
        tracked_records = [record for record in tracked_records if record.package_entry.profile == profile]
    exact_matches = [record for record in tracked_records if record.package_entry.selector == selector]
    partial_matches = [record for record in tracked_records if selector in record.package_entry.selector]
    unique_partials = {
        (
            record.state_key,
            record.package_entry.repo,
            record.package_entry.selector,
            record.package_entry.profile,
        ): record
        for record in partial_matches
    }
    return selector, profile, exact_matches, list(unique_partials.values())



def write_tracked_package_entries(
    repo: Repository,
    bindings: list[FullSpecSelector],
) -> None:
    write_tracked_package_entries_file(repo.config.state_path, bindings)



def write_tracked_package_entries_file(state_dir: Path, bindings: list[FullSpecSelector]) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    state_path = tracked_packages_file_path(state_dir)
    tracked_packages = sorted(
        [
            TrackedPackageEntry(repo=binding.repo, package_id=binding.selector, profile=binding.profile)
            for binding in bindings
        ],
        key=lambda entry: (entry.repo, entry.package_id, entry.profile),
    )
    state: dict[str, object] = {"schema_version": TRACKED_PACKAGES_SCHEMA_VERSION}
    # tomlkit renders an empty list as `packages = []`; omit it so an empty state stays bare.
    if tracked_packages:
        state["packages"] = [
            {"repo": entry.repo, "package_id": entry.package_id, "profile": entry.profile}
            for entry in tracked_packages
        ]
    write_text_atomic(state_path, tomlkit.dumps(state))



def remove_persisted_tracked_package_entry(
    context: "PlanningContext",
    record: PersistedTrackedPackageEntryRecord,
    *,
    operation: str = "untrack",
) -> FullSpecSelector:
    from dotman.collisions import TrackedTargetConflictError

    state = context.tracked_state
    persisted_entries = read_tracked_package_entries_file(tracked_packages_file_path(record.state_dir))
    if record.repo is not None and record.issue is None:
        persisted_entries = persisted_tracked_package_entries_for_repo(record.repo, persisted_entries)
    remaining = remove_tracked_package_entry_record(persisted_entries, record.package_entry)
    if record.repo is not None:
        raw_entries_by_repo = raw_tracked_package_entries_by_repo(state)
        raw_entries_by_repo[record.repo.config.name] = remaining
        rewritten_repo_names = {record.repo.config.name} if record.issue is None else set()
        if not _skips_conflict_validation(state, raw_entries_by_repo, rewritten_repo_names):
            try:
                validate_tracked_package_entries(
                    context,
                    effective_tracked_package_entries_by_repo(state, raw_entries_by_repo),
                )
            except TrackedTargetConflictError as exc:
                binding_label = f"{record.package_entry.repo}:{record.package_entry.selector}@{record.package_entry.profile}"
                raise ValueError(
                    f"cannot {operation} '{binding_label}': removing this binding would expose {exc}"
                ) from None
    write_tracked_package_entries_file(record.state_dir, remaining)
    return record.package_entry


def remove_tracked_package_entries(
    context: "PlanningContext",
    bindings: list[FullSpecSelector],
    *,
    operation: str = "untrack",
    operation_label: str | None = None,
) -> list[FullSpecSelector]:
    from dotman.collisions import TrackedTargetConflictError

    state = context.tracked_state
    unique_bindings = list(dict.fromkeys(bindings))
    raw_entries_by_repo = raw_tracked_package_entries_by_repo(state)
    remaining_by_repo: dict[str, list[FullSpecSelector]] = {}
    removed: list[FullSpecSelector] = []

    for binding in unique_bindings:
        repo = get_repository(state, binding.repo)
        if repo.config.name not in remaining_by_repo:
            remaining_by_repo[repo.config.name] = persisted_tracked_package_entries_for_repo(
                repo,
                raw_entries_by_repo[repo.config.name],
            )
        remaining = remaining_by_repo[repo.config.name]
        updated_remaining = remove_tracked_package_entry_record(remaining, binding)
        if updated_remaining == remaining:
            continue
        remaining_by_repo[repo.config.name] = updated_remaining
        raw_entries_by_repo[repo.config.name] = updated_remaining
        removed.append(binding)

    if not removed:
        return []

    if not _skips_conflict_validation(state, raw_entries_by_repo, set(remaining_by_repo)):
        try:
            validate_tracked_package_entries(
                context,
                effective_tracked_package_entries_by_repo(state, raw_entries_by_repo),
            )
        except TrackedTargetConflictError as exc:
            label = operation_label or ", ".join(f"{binding.repo}:{binding.selector}@{binding.profile}" for binding in removed)
            raise ValueError(
                f"cannot {operation} '{label}': removing these bindings would expose {exc}"
            ) from None

    for repo_name, remaining in remaining_by_repo.items():
        repo = get_repository(state, repo_name)
        write_tracked_package_entries_file(repo.config.state_path, remaining)

    return removed



def remove_tracked_package_entry_record(bindings: list[FullSpecSelector], target: FullSpecSelector) -> list[FullSpecSelector]:
    return [binding for binding in bindings if binding != target]



def iter_tracked_package_entries(
    context: TrackedStateContext,
) -> list[tuple[Repository, FullSpecSelector, SelectorKind, list[str]]]:
    valid_records, _invalid_records = configured_persisted_tracked_package_entry_records(context)
    return [
        (record.repo, record.package_entry, record.selector_kind or "package", list(record.package_ids))
        for record in valid_records
        if record.repo is not None
    ]
def configured_persisted_tracked_package_entry_records(
    context: TrackedStateContext,
    *,
    bindings_by_repo: dict[str, list[FullSpecSelector]] | None = None,
) -> tuple[list[PersistedTrackedPackageEntryRecord], list[PersistedTrackedPackageEntryRecord]]:
    valid_records: list[PersistedTrackedPackageEntryRecord] = []
    invalid_records: list[PersistedTrackedPackageEntryRecord] = []
    # An empty mapping is a real input: doctor passes one when every state file is unreadable.
    current_bindings = raw_tracked_package_entries_by_repo(context) if bindings_by_repo is None else bindings_by_repo
    for repo_config in context.config.ordered_repos:
        repo = get_repository(context, repo_config.name)
        for binding in current_bindings.get(repo_config.name, []):
            try:
                resolved_bindings = resolve_persisted_tracked_package_entry(repo, binding)
            except PersistedTrackedPackageEntryResolutionError as exc:
                invalid_records.append(
                    PersistedTrackedPackageEntryRecord(
                        state_key=repo.config.state_key,
                        state_dir=repo.config.state_path,
                        package_entry=binding,
                        repo=repo,
                        issue=TrackedPackageEntryIssue(
                            state_key=repo.config.state_key,
                            repo=binding.repo,
                            selector=binding.selector,
                            profile=binding.profile,
                            state="invalid",
                            reason=exc.reason,
                            message=exc.message,
                        ),
                    )
                )
                continue
            for resolved_binding in resolved_bindings:
                valid_records.append(
                    PersistedTrackedPackageEntryRecord(
                        state_key=repo.config.state_key,
                        state_dir=repo.config.state_path,
                        package_entry=resolved_binding,
                        repo=repo,
                        selector_kind="package",
                        package_ids=tuple(resolve_package_ids(repo, resolved_binding.selector, "package")),
                    )
                )
    return valid_records, invalid_records



def orphan_persisted_tracked_package_entry_records(
    context: TrackedStateContext,
) -> list[PersistedTrackedPackageEntryRecord]:
    repos_state_root = context.state_root / "repos"
    if not repos_state_root.exists():
        return []
    configured_state_keys = {repo_config.state_key for repo_config in context.config.ordered_repos}
    orphan_records: list[PersistedTrackedPackageEntryRecord] = []
    for state_dir in sorted(path for path in repos_state_root.iterdir() if path.is_dir()):
        if state_dir.name in configured_state_keys:
            continue
        state_path = tracked_packages_file_path(state_dir)
        if not state_path.exists():
            continue
        for binding in read_tracked_package_entries_file(state_path):
            orphan_records.append(
                PersistedTrackedPackageEntryRecord(
                    state_key=state_dir.name,
                    state_dir=state_dir,
                    package_entry=binding,
                    issue=TrackedPackageEntryIssue(
                        state_key=state_dir.name,
                        repo=binding.repo,
                        selector=binding.selector,
                        profile=binding.profile,
                        state="orphan",
                        reason="unknown_repo",
                        message="repo not in config",
                    ),
                )
            )
    return orphan_records



def all_persisted_tracked_package_entry_records(
    context: TrackedStateContext,
) -> list[PersistedTrackedPackageEntryRecord]:
    valid_records, invalid_records = configured_persisted_tracked_package_entry_records(context)
    return [
        *valid_records,
        *invalid_records,
        *orphan_persisted_tracked_package_entry_records(context),
    ]



def resolve_persisted_tracked_package_entry(
    repo: Repository,
    binding: FullSpecSelector,
) -> list[FullSpecSelector]:
    if binding.selector not in repo.packages:
        if binding.selector in repo.groups:
            raise PersistedTrackedPackageEntryResolutionError(
                reason="selector_kind_invalid",
                message="tracked packages file rows must reference packages, not groups",
            )
        raise PersistedTrackedPackageEntryResolutionError(reason="unknown_selector", message="unknown selector")
    resolved_bindings = [binding]
    try:
        for resolved_binding in resolved_bindings:
            resolve_package_ids(repo, resolved_binding.selector, "package")
    except ValueError as exc:
        raise PersistedTrackedPackageEntryResolutionError(
            reason="dependency_resolution_failed",
            message="dependency resolution failed",
        ) from exc
    return resolved_bindings



def tracked_package_matches_for_untrack(
    context: TrackedStateContext,
    *,
    selector: str,
    profile: str | None,
    repo_name: str | None,
) -> tuple[list[TrackedPackageSummary], list[TrackedPackageEntrySummary]]:
    package_matches: list[TrackedPackageSummary] = []
    owner_package_entries: dict[tuple[str, str, str], TrackedPackageEntrySummary] = {}
    if repo_name is not None and repo_name not in context.repositories:
        return package_matches, []
    candidate_repo_names = set(context.repositories) if repo_name is None else {repo_name}
    for package in list_tracked_packages(context):
        if package.repo not in candidate_repo_names:
            continue
        matching_entries = [package_entry for package_entry in package.package_entries if profile is None or package_entry.profile == profile]
        if not matching_entries:
            continue
        package_ref = package.package_ref
        if package.package_id == selector:
            package_matches.append(package)
        elif selector in package_ref:
            package_matches.append(package)
        else:
            continue
        for package_entry in matching_entries:
            owner_package_entries[(package_entry.repo, package_entry.selector, package_entry.profile)] = package_entry
    sorted_package_matches = sorted(
        package_matches,
        key=lambda item: (item.repo, item.package_id, "" if item.bound_profile is None else item.bound_profile),
    )
    sorted_owner_package_entries = sorted(
        owner_package_entries.values(),
        key=lambda item: (item.repo, item.selector, item.profile),
    )
    return sorted_package_matches, sorted_owner_package_entries



def sorted_tracked_package_entry_issues(issues: list[TrackedPackageEntryIssue]) -> list[TrackedPackageEntryIssue]:
    return sorted(
        issues,
        key=lambda item: (
            0 if item.state == "orphan" else 1,
            item.repo,
            item.selector,
            item.profile,
            item.state_key,
        ),
    )
