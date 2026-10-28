from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Sequence

from dotman.collisions import TrackedTargetConflictError, TrackedTargetOverride
from dotman.command_runtime import CommandRuntime, current_command_runtime
from dotman.config import default_state_root, load_manager_config
from dotman.errors import UsageError
from dotman.models import (
    FullSpecSelector,
    ResolvedSyncScope,
    SearchMatch,
    SelectorKind,
    TrackableCatalogEntry,
    TrackableGroupDetail,
    TrackablePackageDetail,
    TrackedPackageEntrySummary,
    TrackedPackageDetail,
    TrackedPackageSummary,
    ManagerConfig,
)
from dotman.planning import PlanningContext
from dotman.projection import ProjectionContext
from dotman.profiles import rank_profiles
from dotman.repository import Repository
from dotman.sync_scope import resolve_sync_scope
from dotman.tracking import (
    PersistedTrackedPackageEntryRecord,
    TrackedStateContext,
    TrackedStateSummary,
)
from dotman import planning, tracked_packages, tracking, variable_inspection

if TYPE_CHECKING:
    from dotman.progress import ProgressSink
    from dotman.sync_session import SessionEventSink, SessionOpenFailed, SyncSession


def _search_match_reason(
    *,
    query_lower: str,
    selector_lower: str,
    qualified_selector_lower: str,
    description_lower: str | None,
) -> tuple[str, int] | None:
    if query_lower == qualified_selector_lower:
        return "exact_repo_qualified_selector", 0
    if query_lower == selector_lower:
        return "exact_selector", 1
    if selector_lower.startswith(query_lower):
        return "prefix_selector", 2
    if query_lower in selector_lower:
        return "substring_selector", 3
    if description_lower is not None and query_lower in description_lower:
        return "substring_description", 4
    return None


class DotmanEngine:
    def __init__(self, config: ManagerConfig, *, command_runtime: CommandRuntime | None = None) -> None:
        self.config = config
        self.command_runtime = command_runtime or current_command_runtime()
        self._tracked_state_context = TrackedStateContext(
            config=config,
            repositories={repo.name: Repository(repo) for repo in config.ordered_repos},
            state_root=default_state_root(),
        )
        self.repos = self._tracked_state_context.repositories
        self._planning_context = PlanningContext(
            config=config,
            tracked_state=self._tracked_state_context,
            projection=ProjectionContext(config=config, command_runtime=self.command_runtime),
        )

    @classmethod
    def from_config_path(
        cls,
        config_path: str | Path | None = None,
        *,
        file_symlink_mode: str | None = None,
        dir_symlink_mode: str | None = None,
        command_runtime: CommandRuntime | None = None,
    ) -> "DotmanEngine":
        config = load_manager_config(config_path)
        if file_symlink_mode is not None or dir_symlink_mode is not None:
            config = replace(
                config,
                file_symlink_mode=file_symlink_mode or config.file_symlink_mode,
                dir_symlink_mode=dir_symlink_mode or config.dir_symlink_mode,
            )
        return cls(config, command_runtime=command_runtime)

    def get_repo(self, repo_name: str) -> Repository:
        try:
            return self.repos[repo_name]
        except KeyError as exc:
            raise UsageError(f"unknown repo '{repo_name}'") from exc

    def candidate_repos(self, repo_name: str | None = None) -> list[Repository]:
        if repo_name:
            return [self.get_repo(repo_name)]
        return [self.repos[repo.name] for repo in self.config.ordered_repos]

    def find_selector_matches(
        self,
        selector: str,
        repo_name: str | None = None,
    ) -> tuple[
        list[tuple[Repository, str, SelectorKind]],
        list[tuple[Repository, str, SelectorKind]],
    ]:
        candidate_repos = self.candidate_repos(repo_name)
        exact_matches: list[tuple[Repository, str, SelectorKind]] = []
        partial_matches: list[tuple[Repository, str, SelectorKind]] = []
        for repo in candidate_repos:
            package_match = selector in repo.packages
            group_match = selector in repo.groups
            if package_match and group_match:
                raise UsageError(f"selector '{selector}' is ambiguous between package and group in repo '{repo.config.name}'")
            if package_match:
                exact_matches.append((repo, selector, "package"))
                continue
            if group_match:
                exact_matches.append((repo, selector, "group"))
                continue
            for package_id in repo.packages:
                if selector in package_id:
                    partial_matches.append((repo, package_id, "package"))
            for group_id in repo.groups:
                if selector in group_id:
                    partial_matches.append((repo, group_id, "group"))
        unique_partials: dict[
            tuple[str, str, SelectorKind],
            tuple[Repository, str, SelectorKind],
        ] = {
            (repo.config.name, match, kind): (repo, match, kind)
            for repo, match, kind in partial_matches
        }
        return exact_matches, list(unique_partials.values())

    def list_profiles(self, repo_name: str) -> list[str]:
        repo = self.get_repo(repo_name)
        return rank_profiles({profile_id: profile.includes for profile_id, profile in repo.profiles.items()})

    def search_selectors(self, query_text: str) -> list[SearchMatch]:
        query = query_text.strip()
        if not query:
            raise UsageError("search query must not be empty")

        query_lower = query.lower()
        ranked_matches: list[tuple[tuple[int, int, int, int, str], SearchMatch]] = []

        for repo, trackable in tracking.iter_trackable_catalog_entries(self._tracked_state_context):
            selector = trackable.selector
            selector_lower = selector.lower()
            qualified_selector = trackable.qualified_selector
            qualified_selector_lower = qualified_selector.lower()
            description = trackable.description
            description_lower = description.lower() if isinstance(description, str) else None

            match = _search_match_reason(
                query_lower=query_lower,
                selector_lower=selector_lower,
                qualified_selector_lower=qualified_selector_lower,
                description_lower=description_lower,
            )
            if match is None:
                continue

            match_reason, tier = match
            ranked_matches.append(
                (
                    (tier, repo.config.order, 0 if trackable.kind == "package" else 1, len(selector), qualified_selector_lower),
                    SearchMatch(
                        kind=trackable.kind,
                        repo=repo.config.name,
                        selector=selector,
                        qualified_selector=qualified_selector,
                        description=description,
                        binding_mode=trackable.binding_mode,
                        member_count=trackable.member_count,
                        match_reason=match_reason,
                        rank=0,
                    ),
                )
            )

        ranked_matches.sort(key=lambda item: item[0])
        return [replace(match, rank=index + 1) for index, (_sort_key, match) in enumerate(ranked_matches)]

    def list_trackables(self) -> list[TrackableCatalogEntry]:
        return tracking.list_trackables(self._tracked_state_context)


    def open_sync_session(
        self, scope: ResolvedSyncScope, *, preview: bool = False, run_noop: bool = False,
        event_sink: SessionEventSink | None = None, sink: "ProgressSink | None" = None,
        stream_output: bool = False, commit: bool = False,
    ) -> SyncSession | SessionOpenFailed:
        """Observe files and auxiliary work through the one-shot Sync boundary."""
        from dotman.sync_session import SyncSession

        return SyncSession.open(
            self._planning_context, scope, preview=preview, run_noop=run_noop, event_sink=event_sink, sink=sink,
            stream_output=stream_output, commit=commit,
        )

    def open_pull_session(self, scope: ResolvedSyncScope, *, preview: bool = False,
                          run_noop: bool = False, event_sink=None, sink: "ProgressSink | None" = None,
                          stream_output: bool = False, commit: bool = False):
        """Open permanent fixed-direction Pull on the shared frozen workset."""
        from dotman.pull_session import PullSession
        return PullSession.open(self._planning_context, scope, preview=preview,
                                run_noop=run_noop, event_sink=event_sink, sink=sink, stream_output=stream_output,
                                commit=commit)

    def open_push_session(self, scope: ResolvedSyncScope, *, preview: bool = False,
                          run_noop: bool = False, event_sink=None, sink: "ProgressSink | None" = None,
                          stream_output: bool = False, commit: bool = False):
        """Open permanent fixed-direction Push on the shared frozen workset."""
        from dotman.push_session import PushSession
        return PushSession.open(self._planning_context, scope, preview=preview,
                                run_noop=run_noop, event_sink=event_sink, sink=sink, stream_output=stream_output,
                                commit=commit)

    def resolve_sync_scope(
        self, selectors: Sequence[str] | None = None, *, include_dependencies: bool = True,
    ) -> ResolvedSyncScope:
        """Resolve exact tracked identities for a SyncSession."""
        return resolve_sync_scope(self._planning_context, selectors, include_dependencies=include_dependencies)



    def list_tracked_state(self) -> TrackedStateSummary:
        # It lists every invalid row itself; a skip warning would only repeat the list.
        with tracking.silencing_skipped_entries():
            return tracking.list_tracked_state(self._tracked_state_context)

    def describe_trackable(self, *, repo_name: str, selector: str, selector_kind: SelectorKind) -> TrackablePackageDetail | TrackableGroupDetail:
        return tracking.describe_trackable(
            self._tracked_state_context,
            repo_name=repo_name,
            selector=selector,
            selector_kind=selector_kind,
        )

    def describe_tracked_package(self, package_text: str) -> TrackedPackageDetail:
        return tracked_packages.describe_tracked_package(self._planning_context, package_text)

    def list_variables(self) -> list[Any]:
        return variable_inspection.list_winning_variables(self._tracked_state_context)

    def describe_variable(self, variable_text: str) -> Any:
        return variable_inspection.describe_resolved_variable(self._tracked_state_context, variable_text)

    def find_variable_matches(self, variable_text: str) -> tuple[list[str], list[str]]:
        return variable_inspection.find_variable_matches(self._tracked_state_context, variable_text)

    def doctor(self) -> Any:
        from dotman.doctor import doctor_context

        from dotman.sync_base_inspection import doctor_sync_bases

        # Like `list tracked`, doctor reports every invalid row, so skip warnings stay silent.
        with tracking.silencing_skipped_entries():
            summary = doctor_context(self._tracked_state_context)
            return replace(summary, checks=[*summary.checks, *doctor_sync_bases(self._planning_context)])

    def list_sync_bases(self):
        from dotman.sync_base_inspection import list_sync_bases
        return list_sync_bases(self._planning_context)

    def info_sync_base(self, identity: str):
        from dotman.sync_base_inspection import info_sync_base
        return info_sync_base(self._planning_context, identity)

    def reset_sync_base(self, identity: str):
        from dotman.sync_base_inspection import reset_sync_base
        return reset_sync_base(self._planning_context, identity)

    def reset_unusable_sync_bases(self, repo_name: str | None):
        from dotman.sync_base_inspection import reset_unusable_sync_bases
        return reset_unusable_sync_bases(self._planning_context, repo_name)

    def read_effective_tracked_package_entries(self, repo: Repository) -> list[FullSpecSelector]:
        return tracking.read_effective_tracked_package_entries(repo)

    def stale_tracked_row_rejection(
        self, *, subject_text: str, repo_name: str | None, package_id: str, profile: str | None = None,
    ) -> str | None:
        return tracking.stale_tracked_row_rejection(
            self._tracked_state_context,
            subject_text=subject_text, repo_name=repo_name, package_id=package_id, profile=profile,
        )

    def read_persisted_tracked_package_entries(self, repo: Repository) -> list[FullSpecSelector]:
        return tracking.read_persisted_tracked_package_entries(repo)

    def expand_tracked_package_entry(self, binding: FullSpecSelector) -> list[FullSpecSelector]:
        return tracking.expand_tracked_package_entry(self._tracked_state_context, binding)

    def record_tracked_package_entry(self, binding: FullSpecSelector, *, validate: bool = True) -> None:
        tracking.record_tracked_package_entry(self._planning_context, binding, validate=validate)

    def validate_tracked_package_entry_additions(self, bindings: list[FullSpecSelector]) -> None:
        tracking.validate_tracked_package_entry_additions(self._planning_context, bindings)

    def tracked_package_entry_addition_conflicts(
        self, bindings: list[FullSpecSelector],
    ) -> list[TrackedTargetConflictError]:
        return tracking.tracked_package_entry_addition_conflicts(self._planning_context, bindings)

    def find_persisted_tracked_package_entry_matches(
        self,
        binding_text: str,
    ) -> tuple[str, str | None, list[PersistedTrackedPackageEntryRecord], list[PersistedTrackedPackageEntryRecord]]:
        return tracking.find_persisted_tracked_package_entry_matches(
            self._tracked_state_context,
            binding_text,
        )

    def find_tracked_package_matches_for_untrack(
        self,
        *,
        selector: str,
        profile: str | None,
        repo_name: str | None,
    ) -> tuple[list[TrackedPackageSummary], list[TrackedPackageEntrySummary]]:
        return tracking.tracked_package_matches_for_untrack(
            self._tracked_state_context,
            selector=selector,
            profile=profile,
            repo_name=repo_name,
        )

    def remove_persisted_tracked_package_entry(
        self,
        record: PersistedTrackedPackageEntryRecord,
        *,
        operation: str = "untrack",
    ) -> FullSpecSelector:
        return tracking.remove_persisted_tracked_package_entry(
            self._planning_context,
            record,
            operation=operation,
        )

    def remove_tracked_package_entries(
        self,
        bindings: list[FullSpecSelector],
        *,
        operation: str = "untrack",
        operation_label: str | None = None,
    ) -> list[FullSpecSelector]:
        return tracking.remove_tracked_package_entries(
            self._planning_context,
            bindings,
            operation=operation,
            operation_label=operation_label,
        )

    def find_tracked_package_matches(
        self,
        package_text: str,
    ) -> tuple[str, str | None, list[tuple[Repository, str, str | None]], list[tuple[Repository, str, str | None]]]:
        return tracked_packages.find_tracked_package_matches(self._tracked_state_context, package_text)

    def find_tracked_target_matches(
        self, target_text: str, *, include_probes: bool = False,
    ) -> tuple[str, list[Any], list[Any]]:
        return tracked_packages.find_tracked_target_matches(
            self._planning_context, target_text, include_probes=include_probes,
        )

    def preview_tracked_package_entry_implicit_overrides(
        self,
        package_entries: list[FullSpecSelector],
    ) -> list[TrackedTargetOverride]:
        """Preview the implicit owners that tracking all of `package_entries` together would override."""
        selections = [
            selection
            for package_entry in package_entries
            for selection in planning.resolve_full_spec_selector(self._planning_context, package_entry)
            if selection.explicit
        ]
        overrides = planning.preview_package_selections_implicit_overrides(
            self._planning_context,
            selections,
        )
        unique: dict[tuple[str, str, str | None, str], TrackedTargetOverride] = {}
        for override in overrides:
            key = (
                override.winner.selection.identity.repo,
                override.winner.package_id,
                override.winner.selection.identity.bound_profile,
                override.winner.selection.requested_profile,
            )
            unique.setdefault(key, override)
        return list(unique.values())


__all__ = [
    "DotmanEngine",
]
