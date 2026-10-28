from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Mapping, TYPE_CHECKING

from dotman import cli_style
from dotman.atomic_files import write_text_atomic
from dotman.errors import UsageError
from dotman.printable import terminal_safe_line
from dotman.toml_utils import TomlLoadError, dump_toml_text, load_toml_file
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
    UnresolvedSelectorError,
    parse_full_spec_selector_text,
    resolve_package_ids,
)
from dotman.repository import MissingExtendsParentError, Repository

if TYPE_CHECKING:
    from dotman.collisions import TrackedTargetConflictError
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
        raise UsageError(f"unknown repo '{repo_name}'") from exc


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
    package_ids: tuple[str, ...] = ()
    issue: TrackedPackageEntryIssue | None = None


@dataclass(frozen=True)
class TrackedStateSummary:
    packages: list[TrackedPackageSummary]
    invalid_package_entries: list[TrackedPackageEntryIssue]


def iter_trackable_catalog_entries(context: TrackedStateContext):
    for repo in candidate_repositories(context):
        for package_id in sorted(repo.packages):
            # Inherited metadata such as binding_mode only exists on the resolved package.
            try:
                package = repo.resolve_package(package_id)
            except MissingExtendsParentError as exc:
                # A dangling parent makes the package stale, not the repo broken (#239), so
                # browsing skips it; other manifest errors such as a cycle still propagate.
                _record_skipped_entry(
                    (repo.config.name, package_id, None),
                    SkippedCatalogPackage(repo=repo.config.name, package_id=package_id, message=str(exc)),
                )
                continue
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
    try:
        payload = load_toml_file(state_path, context="tracked packages file")
    except TomlLoadError as exc:
        # dotman wrote this file, so corruption is a runtime failure (exit 1), not
        # the user's TOML to fix like a manifest or config (TomlLoadError, exit 2).
        raise TrackedPackagesFileError(state_path, f"must be valid TOML: {exc.detail}") from exc
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



def read_persisted_tracked_package_entries(repo: Repository) -> list[FullSpecSelector]:
    return persisted_tracked_package_entries_for_repo(repo, read_tracked_package_entries(repo))



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
    if binding.selector in repo.packages and _is_multi_instance(repo, binding.selector):
        return (binding.repo, binding.selector, binding.profile)
    return (binding.repo, binding.selector, None)


def _is_multi_instance(repo: Repository, package_id: str) -> bool:
    try:
        return repo.package_binding_mode(package_id) == "multi_instance"
    except MissingExtendsParentError:
        # A stale row kept in state has no resolvable binding mode; scope it like a singleton
        # so tracking another package in the same repo does not trip over it.
        return False



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
    """Expand a binding the user named to track into its package bindings, or raise why it cannot be tracked.

    This is the gate for user input; persisted rows go through resolve_persisted_tracked_package_entry
    instead. It raises usage errors naming what does not resolve, so every binding it returns
    resolves, and the stale-row filtering that later runs over the would-be state never skips
    (and warns about) a binding that was never tracked.
    """
    if package_entry.profile not in repo.profiles:
        raise UsageError(f"unknown profile '{package_entry.profile}' in repo '{repo.config.name}'")
    package_match = package_entry.selector in repo.packages
    group_match = package_entry.selector in repo.groups
    if package_match and group_match:
        raise UsageError(
            f"selector '{package_entry.selector}' is ambiguous between package and group in repo '{repo.config.name}'"
        )
    if not package_match and not group_match:
        raise UsageError(f"selector '{package_entry.selector}' did not match any package or group")
    if package_match:
        # Raises the missing dependency, group member or `extends` parent by name.
        resolve_package_ids(repo, package_entry.selector, "package")
        package_ids = [package_entry.selector]
    else:
        package_ids = repo.expand_group(package_entry.selector)
        for package_id in package_ids:
            try:
                resolve_package_ids(repo, package_id, "package")
            except (MissingExtendsParentError, UnresolvedSelectorError) as exc:
                raise UsageError(
                    f"group '{package_entry.selector}' package '{package_id}' cannot be tracked: {exc}"
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



@dataclass(frozen=True)
class SkippedTrackedRow:
    binding: FullSpecSelector
    message: str
    # The repo state dir holding the row, to check at report time that it is still tracked.
    state_dir: Path


@dataclass(frozen=True)
class SkippedCatalogPackage:
    """A package a catalog walk could not resolve because its `extends` parent is missing."""

    repo: str
    package_id: str
    message: str


SkippedEntry = SkippedTrackedRow | SkippedCatalogPackage

SkippedEntryKey = tuple[str, str, str | None]


@dataclass
class _SkippedEntryLog:
    entries: dict[SkippedEntryKey, SkippedEntry] = field(default_factory=dict)
    # Keys some output already showed, so the end-of-command warning skips them
    # even when a later walk records the same entry again.
    reported: set[SkippedEntryKey] = field(default_factory=set)
    # Rows this command last wrote per repo state dir, so a stale row it removed or
    # replaced is not reported; dirs it never wrote keep every row it read.
    written_rows: dict[Path, list[FullSpecSelector]] = field(default_factory=dict)


# Entries skipped as stale during the running command, keyed by identity so repeated
# walks report each one once. Scoped to one command by `collecting_skipped_entries`.
# Tracked rows are keyed (repo, package, profile); catalog packages (repo, package, None).
_SKIPPED_ENTRIES: ContextVar[_SkippedEntryLog | None] = ContextVar(
    "dotman_skipped_entries", default=None,
)


@contextmanager
def collecting_skipped_entries() -> Iterator[None]:
    """Collect the stale entries that tracked-state and catalog walks skip while a command runs."""
    token = _SKIPPED_ENTRIES.set(_SkippedEntryLog())
    try:
        yield
    finally:
        _SKIPPED_ENTRIES.reset(token)


@contextmanager
def silencing_skipped_entries() -> Iterator[None]:
    """Stop collecting skipped entries, for commands that list every invalid row themselves."""
    token = _SKIPPED_ENTRIES.set(None)
    try:
        yield
    finally:
        _SKIPPED_ENTRIES.reset(token)


def take_unreported_skipped_entries(
    entry_type: type | None = None,
) -> list[SkippedEntry]:
    """Return the skipped entries (of `entry_type`, if given) no output has shown yet, marking them shown.

    A stale row the command itself has since removed or replaced is left out: there is
    nothing left to untrack, however many reads of the old state skipped it.
    """
    log = _SKIPPED_ENTRIES.get()
    if log is None:
        return []
    taken = {
        key: entry for key, entry in log.entries.items()
        if key not in log.reported and (entry_type is None or isinstance(entry, entry_type))
    }
    log.reported.update(taken)

    def still_tracked(entry: SkippedEntry) -> bool:
        if not isinstance(entry, SkippedTrackedRow) or entry.state_dir not in log.written_rows:
            return True
        return entry.binding in log.written_rows[entry.state_dir]

    return [entry for entry in taken.values() if still_tracked(entry)]


def _record_skipped_entry(key: SkippedEntryKey, entry: SkippedEntry) -> None:
    log = _SKIPPED_ENTRIES.get()
    if log is not None:
        log.entries.setdefault(key, entry)


def _record_skipped_tracked_row(binding: FullSpecSelector, message: str, state_dir: Path) -> None:
    _record_skipped_entry(
        (binding.repo, binding.selector, binding.profile),
        SkippedTrackedRow(binding=binding, message=message, state_dir=state_dir),
    )


def stale_tracked_row_rejection(
    context: TrackedStateContext,
    *,
    subject_text: str,
    repo_name: str | None,
    package_id: str,
    profile: str | None = None,
) -> str | None:
    """The error for a name that matched nothing because its tracked row is stale, or None if no stale row is named.

    It reads the persisted rows rather than this command's skip log: a command can fail
    on the name before any walk of the tracked state skipped the row. The row is marked
    shown, since the error explains it, so the end-of-command warning leaves it out.
    """
    # Filtered rather than candidate_repositories(repo_name): an unknown repo has no rows, not an error to raise here.
    repos = [repo for repo in candidate_repositories(context) if not repo_name or repo.config.name == repo_name]
    for repo in repos:
        for binding in read_tracked_package_entries(repo):
            if binding.selector != package_id or profile not in (None, binding.profile):
                continue
            try:
                resolve_persisted_tracked_package_entry(repo, binding)
            except PersistedTrackedPackageEntryResolutionError as exc:
                _mark_skipped_entry_shown((binding.repo, binding.selector, binding.profile))
                return cli_style.stale_tracked_row_rejection_text(subject_text, binding, exc.message)
    return None


def _mark_skipped_entry_shown(key: SkippedEntryKey) -> None:
    log = _SKIPPED_ENTRIES.get()
    if log is not None:
        log.reported.add(key)


def effective_tracked_package_entries_for_repo(
    repo: Repository,
    raw_bindings: list[FullSpecSelector],
) -> list[FullSpecSelector]:
    effective_bindings: list[FullSpecSelector] = []
    for binding in raw_bindings:
        try:
            resolve_persisted_tracked_package_entry(repo, binding)
        except PersistedTrackedPackageEntryResolutionError as exc:
            _record_skipped_tracked_row(binding, exc.message, repo.config.state_path)
            continue
        effective_bindings = _normalize_tracked_package_entries_in_repo(repo, effective_bindings, binding)
    return effective_bindings



def persisted_tracked_package_entries_for_repo(
    repo: Repository,
    raw_bindings: list[FullSpecSelector],
) -> list[FullSpecSelector]:
    # Rows that do not resolve right now (package on another branch, profile removed)
    # are kept verbatim so they stay listed and removable after unrelated writes.
    unresolved_bindings = [
        binding for binding in raw_bindings if not is_resolvable_persisted_tracked_package_entry(repo, binding)
    ]
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
    if validate:
        validate_tracked_package_entries(
            context,
            effective_tracked_package_entries_by_repo(state, raw_entries_by_repo),
        )
    write_tracked_package_entries(repo, normalized)



def validate_tracked_package_entry_additions(
    context: "PlanningContext",
    bindings: list[FullSpecSelector],
) -> None:
    """Validate the state that tracking all of `bindings` together would produce."""
    validate_tracked_package_entries(context, _tracked_package_entries_with_additions(context, bindings))


def tracked_package_entry_addition_conflicts(
    context: "PlanningContext",
    bindings: list[FullSpecSelector],
) -> list["TrackedTargetConflictError"]:
    """Every target conflict in the state that tracking all of `bindings` together would produce."""
    from dotman import planning

    return planning.tracked_package_ownership_conflicts(
        context,
        entries_by_repo=tracked_entries_by_repo_from_bindings(
            _tracked_package_entries_with_additions(context, bindings)
        ),
    )


def _tracked_package_entries_with_additions(
    context: "PlanningContext",
    bindings: list[FullSpecSelector],
) -> dict[str, list[FullSpecSelector]]:
    state = context.tracked_state
    raw_entries_by_repo = raw_tracked_package_entries_by_repo(state)
    for binding in bindings:
        repo = get_repository(state, binding.repo)
        raw_entries_by_repo[repo.config.name] = normalize_tracked_package_entry_set(
            state,
            persisted_tracked_package_entries_for_repo(repo, raw_entries_by_repo.get(repo.config.name, [])),
            expand_tracked_package_entry_in_repo(repo, binding),
        )
    return effective_tracked_package_entries_by_repo(state, raw_entries_by_repo)



def _is_named_by(row_text: str, query: str) -> bool:
    """A stored row's name is matched by what was stored or by the escaped text dotman shows for it.

    Rows an older dotman wrote can hold unprintable characters, which every
    listing shows escaped (#295). Accepting that text back lets the untrack hint
    in the stale-row warning name the row without printing it raw.
    """
    return query in (row_text, terminal_safe_line(row_text))


def find_persisted_tracked_package_entry_matches(
    context: TrackedStateContext,
    binding_text: str,
) -> tuple[str, str | None, list[PersistedTrackedPackageEntryRecord], list[PersistedTrackedPackageEntryRecord]]:
    explicit_repo, selector, profile = parse_full_spec_selector_text(binding_text)
    tracked_records = all_persisted_tracked_package_entry_records(context)
    if explicit_repo is not None:
        tracked_records = [record for record in tracked_records if record.package_entry.repo == explicit_repo]
    if profile is not None:
        tracked_records = [record for record in tracked_records if _is_named_by(record.package_entry.profile, profile)]
    exact_matches = [record for record in tracked_records if _is_named_by(record.package_entry.selector, selector)]
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
    write_text_atomic(state_path, dump_toml_text(state))
    log = _SKIPPED_ENTRIES.get()
    if log is not None:
        log.written_rows[state_dir] = list(bindings)



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
        try:
            validate_tracked_package_entries(
                context,
                effective_tracked_package_entries_by_repo(state, raw_entries_by_repo),
            )
        except TrackedTargetConflictError as exc:
            binding_label = f"{record.package_entry.repo}:{record.package_entry.selector}@{record.package_entry.profile}"
            raise UsageError(
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

    try:
        validate_tracked_package_entries(
            context,
            effective_tracked_package_entries_by_repo(state, raw_entries_by_repo),
        )
    except TrackedTargetConflictError as exc:
        label = operation_label or ", ".join(f"{binding.repo}:{binding.selector}@{binding.profile}" for binding in removed)
        raise UsageError(
            f"cannot {operation} '{label}': removing these bindings would expose {exc}"
        ) from None

    for repo_name, remaining in remaining_by_repo.items():
        repo = get_repository(state, repo_name)
        write_tracked_package_entries_file(repo.config.state_path, remaining)

    return removed



def remove_tracked_package_entry_record(bindings: list[FullSpecSelector], target: FullSpecSelector) -> list[FullSpecSelector]:
    removed = False
    remaining: list[FullSpecSelector] = []
    for binding in bindings:
        if not removed and binding == target:
            removed = True
            continue
        remaining.append(binding)
    return remaining



def iter_tracked_package_entries(
    context: TrackedStateContext,
) -> list[tuple[Repository, FullSpecSelector, SelectorKind, list[str]]]:
    valid_records, invalid_records = configured_persisted_tracked_package_entry_records(context)
    for record in invalid_records:
        # package_invalid rows are malformed manifests, not stale state; they stay silent here as before.
        if record.issue is not None and record.issue.reason != "package_invalid":
            _record_skipped_tracked_row(record.package_entry, record.issue.message, record.state_dir)
    return [
        # Persisted rows are always package entries; groups are flattened when recorded.
        (record.repo, record.package_entry, "package", list(record.package_ids))
        for record in valid_records
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
                package_ids = resolve_persisted_tracked_package_entry(repo, binding)
            except PersistedTrackedPackageEntryResolutionError as exc:
                invalid_records.append(_invalid_persisted_record(repo, binding, reason=exc.reason, message=exc.message))
                continue
            except ValueError as exc:
                # Planning fails on a broken manifest; list and doctor still report the row with the cause.
                invalid_records.append(_invalid_persisted_record(repo, binding, reason="package_invalid", message=str(exc)))
                continue
            valid_records.append(
                PersistedTrackedPackageEntryRecord(
                    state_key=repo.config.state_key,
                    state_dir=repo.config.state_path,
                    package_entry=binding,
                    repo=repo,
                    package_ids=package_ids,
                )
            )
    return valid_records, invalid_records



def _invalid_persisted_record(
    repo: Repository,
    binding: FullSpecSelector,
    *,
    reason: str,
    message: str,
) -> PersistedTrackedPackageEntryRecord:
    return PersistedTrackedPackageEntryRecord(
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
            reason=reason,
            message=message,
        ),
    )


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
) -> tuple[str, ...]:
    """Return the package IDs a persisted row tracks, or raise why it does not resolve.

    The one validity rule for persisted rows: list, doctor, untrack, planning and
    writes all use it, so they agree on which rows are invalid. Only stale state
    (a profile, package, dependency, or `extends` parent that no longer exists) makes a
    row invalid; malformed package content (bad TOML, an extends cycle) is a repo error
    and propagates as a plain ValueError.
    """
    if binding.profile not in repo.profiles:
        raise PersistedTrackedPackageEntryResolutionError(reason="unknown_profile", message="unknown profile")
    if binding.selector not in repo.packages:
        if binding.selector in repo.groups:
            raise PersistedTrackedPackageEntryResolutionError(
                reason="selector_kind_invalid",
                message="tracked packages file rows must reference packages, not groups",
            )
        raise PersistedTrackedPackageEntryResolutionError(reason="unknown_selector", message="unknown selector")
    try:
        return tuple(resolve_package_ids(repo, binding.selector, "package"))
    except MissingExtendsParentError as exc:
        # Same kind of dangling reference as a missing dependency: stale, not a broken manifest.
        raise PersistedTrackedPackageEntryResolutionError(reason="extends_parent_missing", message=str(exc)) from exc
    except UnresolvedSelectorError as exc:
        # The message names the selector that does not resolve, so the warning points at the fix.
        raise PersistedTrackedPackageEntryResolutionError(reason="dependency_resolution_failed", message=str(exc)) from exc


def is_resolvable_persisted_tracked_package_entry(repo: Repository, binding: FullSpecSelector) -> bool:
    try:
        resolve_persisted_tracked_package_entry(repo, binding)
    except PersistedTrackedPackageEntryResolutionError:
        return False
    return True



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
