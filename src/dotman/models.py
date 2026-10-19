from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from dotman.planning import PackagePlanningInput


def _serialized_projection(value: str) -> str | dict[str, str]:
    prefix = "__dotman_command__:"
    if value.startswith(prefix):
        return {"run": value[len(prefix):]}
    return value


def projection_display_text(value: str) -> str:
    """Render a stored projection the way the manifest spells it, e.g. ``{ run = "render" }``."""
    serialized = _serialized_projection(value)
    # Only builtin names are forced, so the command never needs TOML escaping.
    return serialized if isinstance(serialized, str) else f'{{ run = "{serialized["run"]}" }}'


def package_ref_text(*, package_id: str, bound_profile: str | None = None) -> str:
    if bound_profile is None:
        return package_id
    return f"{package_id}<{bound_profile}>"


def target_ref_text(*, package_id: str, target_name: str, bound_profile: str | None = None) -> str:
    return f"{package_ref_text(package_id=package_id, bound_profile=bound_profile)}.{target_name}"


def target_separator_index(package_text: str, text: str) -> int:
    """Return the index of the package/target ``.``, or -1 when absent."""
    # A profile instance may contain dots (for example ``app<work.v2>``).
    # The target separator is therefore the first dot after the profile's
    # closing angle bracket, when an instance is present.
    separator = package_text.find(".")
    profile_start = package_text.find("<")
    # Child paths may contain angle brackets; only treat ``<`` as an
    # instance opener when it occurs before the package-target separator.
    if profile_start >= 0 and (separator < 0 or profile_start < separator):
        profile_end = package_text.find(">", profile_start + 1)
        if profile_end < 0:
            raise ValueError(f"sync scope '{text}' is not canonical")
        separator = package_text.find(".", profile_end + 1)
    return separator


def sync_unit_target_identity(canonical: str) -> str:
    """Return the target part of a canonical file or directory-child identity."""
    repo, colon, remainder = canonical.partition(":")
    separator = target_separator_index(remainder, canonical) if colon else -1
    if not repo or separator < 0:
        raise ValueError(f"'{canonical}' is not a canonical Sync Unit identity")
    # Target names never contain `/`, so the first one after the separator starts the child path.
    child = remainder.find("/", separator)
    return canonical if child < 0 else f"{repo}:{remainder[:child]}"


def repo_qualified_target_text(*, repo_name: str, package_id: str, target_name: str, bound_profile: str | None = None) -> str:
    return f"{repo_name}:{target_ref_text(package_id=package_id, target_name=target_name, bound_profile=bound_profile)}"


@dataclass(frozen=True)
class RepoConfig:
    name: str
    path: Path
    order: int
    state_key: str
    state_path: Path
    local_override_path: Path


@dataclass(frozen=True)
class SnapshotConfig:
    enabled: bool
    path: Path
    max_generations: int


@dataclass(frozen=True)
class UiMenusConfig:
    bottom_up: bool = True


@dataclass(frozen=True)
class UiConfig:
    full_paths: bool = False
    compact_path_tail_segments: int = 2
    menus: UiMenusConfig = field(default_factory=UiMenusConfig)


DEFAULT_COMMIT_MESSAGE = "chore(dotman): {summary}\n\n{packages}"


@dataclass(frozen=True)
class GitConfig:
    commit_message: str = DEFAULT_COMMIT_MESSAGE


@dataclass(frozen=True)
class ManagerConfig:
    config_path: Path
    repos: dict[str, RepoConfig]
    snapshots: SnapshotConfig
    ui: UiConfig = field(default_factory=UiConfig)
    git: GitConfig = field(default_factory=GitConfig)
    file_symlink_mode: str = "prompt"
    dir_symlink_mode: str = "fail"

    @property
    def ordered_repos(self) -> list[RepoConfig]:
        return sorted(self.repos.values(), key=lambda repo: repo.order)


@dataclass(frozen=True)
class AdditionalSource:
    """An editor input path and the package root that declared it."""

    path: str
    root: Path | None = None

    def to_dict(self) -> str:
        # Configuration and plan output intentionally expose user paths, not
        # internal absolute provenance roots.
        return self.path


@dataclass(frozen=True)
class EditorSpec:
    """Resolved Proposal Editor provider configuration."""
    type: str | None = "default"
    run: str | None = None
    io: HookCommandIO = "tty"
    elevation: ElevationMode = "none"
    additional_sources: tuple[str, ...] = ()
    # Per-entry provenance survives inheritance and append operations.
    additional_source_entries: tuple[AdditionalSource, ...] = field(default=(), repr=False, compare=False)
    # Kept only for callers constructing an EditorSpec directly. Parsed entries
    # always carry their own root.
    additional_sources_root: Path | None = field(default=None, repr=False, compare=False)
    additional_sources_explicit: bool = field(default=False, repr=False, compare=False)

    def source_entries(self) -> tuple[AdditionalSource, ...]:
        if self.additional_source_entries:
            return self.additional_source_entries
        return tuple(AdditionalSource(path, self.additional_sources_root) for path in self.additional_sources)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = ({"type": self.type} if self.type is not None else {"run": self.run})
        payload.update({"io": self.io, "elevation": self.elevation})
        if self.additional_sources:
            payload["additional_sources"] = list(self.additional_sources)
        return payload


@dataclass(frozen=True)
class TargetPathRule:
    pattern: str
    name: str = ""
    priority: int = 0
    chmod: str | None = None
    render: str = "raw"
    capture: str = "raw"
    compare_repo: str = "raw"
    compare_live: str = "capture"
    editor: EditorSpec = field(default_factory=EditorSpec)
    sync_policy: str | None = None
    additional_sources: tuple[str, ...] = ()
    additional_source_entries: tuple[AdditionalSource, ...] = field(default=(), repr=False, compare=False)
    render_explicit: bool = field(default=False, repr=False, compare=False)
    capture_explicit: bool = field(default=False, repr=False, compare=False)
    compare_repo_explicit: bool = field(default=False, repr=False, compare=False)
    compare_live_explicit: bool = field(default=False, repr=False, compare=False)
    editor_explicit: bool = field(default=False, repr=False, compare=False)
    priority_explicit: bool = field(default=False, repr=False, compare=False)
    pattern_explicit: bool = field(default=False, repr=False, compare=False)
    sync_policy_explicit: bool = field(default=False, repr=False, compare=False)
    hooks: dict[str, "HookSpec"] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name, "pattern": self.pattern, "priority": self.priority,
            "chmod": self.chmod, "render": _serialized_projection(self.render), "capture": _serialized_projection(self.capture),
            "compare": {"repo": _serialized_projection(self.compare_repo), "live": _serialized_projection(self.compare_live)},
            "editor": self.editor.to_dict(), "sync_policy": self.sync_policy,
            "additional_sources": list(self.additional_sources),
        }


def target_path_rule_matches(relative_path: str, pattern: str) -> bool:
    return PurePosixPath(relative_path).match(pattern)


@dataclass(frozen=True)
class TargetSpec:
    name: str
    declared_in: Path
    source: str | None = None
    path: str | None = None
    target_type: str | None = None
    sync_policy: str | None = None
    chmod: str | None = None
    render: str | None = None
    capture: str | None = None
    # Resolved flat projection contract.
    editor: EditorSpec = field(default_factory=EditorSpec)
    compare_repo: str = "raw"
    compare_live: str = "capture"
    additional_sources: tuple[str, ...] = ()
    additional_source_entries: tuple[AdditionalSource, ...] = field(default=(), repr=False, compare=False)
    # Compatibility fallback for manually constructed specs.
    additional_sources_root: Path | None = field(default=None, repr=False, compare=False)
    render_explicit: bool = field(default=False, repr=False, compare=False)
    capture_explicit: bool = field(default=False, repr=False, compare=False)
    compare_repo_explicit: bool = field(default=False, repr=False, compare=False)
    compare_live_explicit: bool = field(default=False, repr=False, compare=False)
    editor_explicit: bool = field(default=False, repr=False, compare=False)
    ignore_patterns: tuple[str, ...] | None = None
    ignore_command: str | None = None
    path_rules: tuple[TargetPathRule, ...] = ()
    hooks: dict[str, "HookSpec"] | None = None
    disabled: bool = False
    # Planned only when selected by exact target selector.
    on_demand: bool = False
    probe: str | None = None


HookCommandIO = Literal["pipe", "tty"]
ElevationMode = Literal["none", "root", "intercept"]
DefaultCommandElevationMode = Literal["none", "intercept"]


@dataclass(frozen=True)
class HookCommandSpec:
    run: str
    io: HookCommandIO = "pipe"
    elevation: ElevationMode = "none"
    run_noop: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "run": self.run,
            "io": self.io,
            "elevation": self.elevation,
            "run_noop": self.run_noop,
        }


@dataclass(frozen=True)
class HookSpec:
    name: str
    commands: tuple[HookCommandSpec, ...]
    declared_in: Path
    run_noop: bool = False


@dataclass(frozen=True)
class PackageSpec:
    id: str
    package_root: Path
    description: str | None = None
    binding_mode: str = "singleton"
    sync_policy: str | None = None
    depends: tuple[str, ...] | None = None
    extends: tuple[str, ...] | None = None
    reserved_paths: tuple[str, ...] | None = None
    ignore_patterns: tuple[str, ...] | None = None
    gitignore_enabled: bool | None = None
    vars: dict[str, Any] | None = None
    targets: dict[str, TargetSpec] | None = None
    hooks: dict[str, HookSpec] | None = None
    remove: tuple[str, ...] | None = None
    append: dict[str, Any] | None = None
    # Lets extends tell an omitted binding_mode from an explicit "singleton".
    binding_mode_explicit: bool = False


@dataclass(frozen=True)
class GroupSpec:
    id: str
    members: tuple[str, ...]
    path: Path
    description: str | None = None


@dataclass(frozen=True)
class ProfileSpec:
    id: str
    includes: tuple[str, ...]
    vars: dict[str, Any]
    path: Path


@dataclass(frozen=True)
class RepoIgnoreDefaults:
    patterns: tuple[str, ...] = ()
    skip_markers: tuple[str, ...] = ()
    gitignore: bool = False


SelectorKind = Literal["package", "group"]


@dataclass(frozen=True)
class ResolvedSelector:
    repo: str
    selector: str
    selector_kind: SelectorKind

    def with_profile(self, profile: str) -> FullSpecSelector:
        return FullSpecSelector(
            repo=self.repo,
            selector=self.selector,
            selector_kind=self.selector_kind,
            profile=profile,
        )


@dataclass(frozen=True)
class FullSpecSelector(ResolvedSelector):
    profile: str


@dataclass(frozen=True)
class TrackedPackageEntry:
    repo: str
    package_id: str
    profile: str


@dataclass(frozen=True)
class ResolvedPackageIdentity:
    repo: str
    package_id: str
    bound_profile: str | None

    @property
    def canonical(self) -> str:
        return f"{self.repo}:{package_ref_text(package_id=self.package_id, bound_profile=self.bound_profile)}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "repo": self.repo,
            "package_id": self.package_id,
            "bound_profile": self.bound_profile,
        }


PackageSelectionSourceKind = Literal["selector_query", "tracked_entry", "dependency"]


@dataclass(frozen=True)
class ResolvedPackageSelection:
    identity: ResolvedPackageIdentity
    requested_profile: str
    explicit: bool
    source_kind: PackageSelectionSourceKind
    source_selector: str | None = None
    owner_identity: ResolvedPackageIdentity | None = None
    owner_selection_label: str | None = None

    @property
    def repo_name(self) -> str:
        return self.identity.repo

    @property
    def package_id(self) -> str:
        return self.identity.package_id

    @property
    def bound_profile(self) -> str | None:
        return self.identity.bound_profile

    @property
    def selection_label(self) -> str:
        selector = self.source_selector or self.identity.package_id
        return f"{self.identity.repo}:{selector}@{self.requested_profile}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "identity": self.identity.to_dict(),
            "requested_profile": self.requested_profile,
            "explicit": self.explicit,
            "source_kind": self.source_kind,
            "source_selector": self.source_selector,
            "owner_identity": None if self.owner_identity is None else self.owner_identity.to_dict(),
            "owner_selection_label": self.owner_selection_label,
            "selection_label": self.selection_label,
        }


@dataclass(frozen=True)
class ResolvedSyncTarget:
    """A canonical target or directory-child identity in a resolved sync scope."""

    repo: str
    package_id: str
    target_name: str
    bound_profile: str | None = None
    child_path: str | None = None

    @property
    def canonical(self) -> str:
        identity = repo_qualified_target_text(
            repo_name=self.repo,
            package_id=self.package_id,
            target_name=self.target_name,
            bound_profile=self.bound_profile,
        )
        return identity if self.child_path is None else f"{identity}/{self.child_path}"

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "repo": self.repo,
            "package_id": self.package_id,
            "bound_profile": self.bound_profile,
            "target_name": self.target_name,
        }
        if self.child_path is not None:
            payload["child_path"] = self.child_path
        payload["selector"] = self.canonical
        return payload


@dataclass(frozen=True)
class ResolvedSyncScope:
    """Static, ownership-checked identities supplied to a future SyncSession."""

    selectors: tuple[str, ...]
    package_selections: tuple[ResolvedPackageSelection, ...]
    targets: tuple[ResolvedSyncTarget, ...]
    # Package and target scopes that entered only through a package input's dependency
    # closure, keyed by canonical scope label, valued by the package inputs that pulled them in.
    included_via: dict[str, tuple[ResolvedPackageIdentity, ...]]
    # The full tracked graph's static planning by direction, kept so a session
    # observes exactly what ownership and collisions validated without planning again.
    planning_inputs: dict[str, tuple[PackagePlanningInput, ...]] = field(compare=False, repr=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "selectors": list(self.selectors),
            "packages": [
                {
                    "repo": selection.identity.repo,
                    "package_id": selection.identity.package_id,
                    "bound_profile": selection.identity.bound_profile,
                    "profile": selection.requested_profile,
                }
                for selection in self.package_selections
            ],
            "targets": [target.to_dict() for target in self.targets],
        }


def resolved_package_identity_key(identity: ResolvedPackageIdentity) -> tuple[str, str, str | None]:
    return (identity.repo, identity.package_id, identity.bound_profile)


def resolved_package_selection_key(selection: ResolvedPackageSelection) -> tuple[str, str, str | None, str]:
    return (*resolved_package_identity_key(selection.identity), selection.requested_profile)


@dataclass(frozen=True)
class TrackedPackageEntrySummary:
    repo: str
    selector: str
    profile: str
    selector_kind: SelectorKind

    def to_dict(self) -> dict[str, Any]:
        return {
            "repo": self.repo,
            "package_id": self.selector,
            "profile": self.profile,
        }


@dataclass(frozen=True)
class HookPlan:
    hook_name: str
    command: str
    cwd: Path
    repo_name: str | None = None
    package_id: str | None = None
    target_name: str | None = None
    scope_kind: str = "package"
    io: HookCommandIO = "pipe"
    elevation: ElevationMode = "none"
    run_noop: bool = False
    # Planning owns the full hook env; required so a missing env fails at construction.
    env: dict[str, str] = field(kw_only=True, repr=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "scope_kind": self.scope_kind,
            "repo_name": self.repo_name,
            "package_id": self.package_id,
            "target_name": self.target_name,
            "hook_name": self.hook_name,
            "command": self.command,
            "cwd": str(self.cwd),
            "io": self.io,
            "elevation": self.elevation,
        }


@dataclass(frozen=True)
class GuardSkip:
    scope_kind: str
    repo_name: str
    package_id: str | None = None
    bound_profile: str | None = None
    target_name: str | None = None
    path_rule_pattern: str | None = None
    reason: str | None = None

    @property
    def scope_label(self) -> str:
        if self.scope_kind in {"target", "path_rule"} and self.package_id is not None and self.target_name is not None:
            return repo_qualified_target_text(
                repo_name=self.repo_name,
                package_id=self.package_id,
                target_name=self.target_name,
                bound_profile=self.bound_profile,
            )
        if self.scope_kind == "package" and self.package_id is not None:
            package_ref = package_ref_text(
                package_id=self.package_id,
                bound_profile=self.bound_profile,
            )
            return f"{self.repo_name}:{package_ref}"
        return self.repo_name

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "scope_kind": self.scope_kind,
            "repo": self.repo_name,
            "package_id": self.package_id,
            "bound_profile": self.bound_profile,
            "scope": self.scope_label,
            "reason": self.reason,
        }
        if self.target_name is not None:
            payload["target_name"] = self.target_name
        if self.path_rule_pattern is not None:
            payload["path_rule_pattern"] = self.path_rule_pattern
        return payload


@dataclass(frozen=True)
class TrackedTargetSummary:
    target_name: str
    repo_path: Path | None
    live_path: Path | None
    target_kind: str
    render: str = "raw"
    capture: str = "raw"
    editor: EditorSpec = field(default_factory=EditorSpec)
    compare_repo: str = "raw"
    compare_live: str = "capture"
    additional_sources: tuple[str, ...] = ()
    additional_source_entries: tuple[AdditionalSource, ...] = field(default=(), repr=False, compare=False)
    chmod: str | None = None
    probe_command: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "target_name": self.target_name,
            "repo_path": None if self.repo_path is None else str(self.repo_path),
            "live_path": None if self.live_path is None else str(self.live_path),
            "target_kind": self.target_kind,
            "render": _serialized_projection(self.render),
            "capture": _serialized_projection(self.capture),
            "editor": self.editor.to_dict(),
            "compare": {"repo": _serialized_projection(self.compare_repo), "live": _serialized_projection(self.compare_live)},
            "additional_sources": list(self.additional_sources),
            "chmod": self.chmod,
        }
        if self.probe_command is not None:
            payload["probe_command"] = self.probe_command
        return payload


@dataclass(frozen=True)
class TrackedPackageSummary:
    repo: str
    package_id: str
    description: str | None
    package_entries: list[TrackedPackageEntrySummary]
    state: str
    bound_profile: str | None = None

    @property
    def package_ref(self) -> str:
        return package_ref_text(package_id=self.package_id, bound_profile=self.bound_profile)

    def to_dict(self) -> dict[str, Any]:
        return {
            "repo": self.repo,
            "package_id": self.package_id,
            "package_ref": self.package_ref,
            "bound_profile": self.bound_profile,
            "description": self.description,
            "state": self.state,
            "package_entries": [package_entry.to_dict() for package_entry in self.package_entries],
        }


@dataclass(frozen=True)
class TrackableTargetDetail:
    target_name: str
    source: str | None
    path: str | None
    target_type: str | None = None
    render: str = "raw"
    capture: str = "raw"
    editor: EditorSpec = field(default_factory=EditorSpec)
    compare_repo: str = "raw"
    compare_live: str = "capture"
    additional_sources: tuple[str, ...] = ()
    chmod: str | None = None
    probe_command: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "target_name": self.target_name,
            "source": self.source,
            "path": self.path,
            "target_type": self.target_type,
            "render": _serialized_projection(self.render),
            "capture": _serialized_projection(self.capture),
            "editor": self.editor.to_dict(),
            "compare": {"repo": _serialized_projection(self.compare_repo), "live": _serialized_projection(self.compare_live)},
            "additional_sources": list(self.additional_sources),
            "chmod": self.chmod,
        }
        if self.probe_command is not None:
            payload["probe_command"] = self.probe_command
        return payload


@dataclass(frozen=True)
class TrackablePackageDetail:
    repo: str
    selector: str
    description: str | None
    binding_mode: str
    tracked_instances: list[TrackedPackageSummary]
    targets: list[TrackableTargetDetail]
    kind: str = field(init=False, default="package")

    @property
    def tracked(self) -> bool:
        return bool(self.tracked_instances)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "repo": self.repo,
            "selector": self.selector,
            "description": self.description,
            "binding_mode": self.binding_mode,
            "tracked": self.tracked,
            "tracked_instances": [instance.to_dict() for instance in self.tracked_instances],
            "targets": [target.to_dict() for target in self.targets],
        }


@dataclass(frozen=True)
class SearchMatch:
    kind: SelectorKind
    repo: str
    selector: str
    qualified_selector: str
    description: str | None
    match_reason: str
    rank: int
    binding_mode: str | None = None
    member_count: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "repo": self.repo,
            "selector": self.selector,
            "qualified_selector": self.qualified_selector,
            "description": self.description,
            "binding_mode": self.binding_mode,
            "member_count": self.member_count,
            "match_reason": self.match_reason,
            "rank": self.rank,
        }


@dataclass(frozen=True)
class TrackableGroupMemberDetail:
    package_id: str
    tracked_instances: list[TrackedPackageSummary]

    @property
    def tracked(self) -> bool:
        return bool(self.tracked_instances)

    def to_dict(self) -> dict[str, Any]:
        return {
            "package_id": self.package_id,
            "tracked": self.tracked,
            "tracked_instances": [instance.to_dict() for instance in self.tracked_instances],
        }


@dataclass(frozen=True)
class TrackableGroupDetail:
    repo: str
    selector: str
    members: list[TrackableGroupMemberDetail]
    kind: str = field(init=False, default="group")

    @property
    def tracked(self) -> bool:
        return any(member.tracked for member in self.members)

    @property
    def tracked_member_count(self) -> int:
        return sum(1 for member in self.members if member.tracked)

    @property
    def tracked_state(self) -> str:
        if not self.members or self.tracked_member_count == 0:
            return "untracked"
        if self.tracked_member_count == len(self.members):
            return "fully_tracked"
        return "partially_tracked"

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "repo": self.repo,
            "selector": self.selector,
            "tracked": self.tracked,
            "tracked_state": self.tracked_state,
            "tracked_member_count": self.tracked_member_count,
            "member_count": len(self.members),
            "members": [member.to_dict() for member in self.members],
        }


@dataclass(frozen=True)
class TrackableCatalogEntry:
    kind: SelectorKind
    repo: str
    selector: str
    description: str | None
    binding_mode: str | None = None
    member_count: int | None = None

    @property
    def qualified_selector(self) -> str:
        return f"{self.repo}:{self.selector}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "repo": self.repo,
            "selector": self.selector,
            "qualified_selector": self.qualified_selector,
            "description": self.description,
            "binding_mode": self.binding_mode,
            "member_count": self.member_count,
        }


@dataclass(frozen=True)
class TrackedPackageEntryIssue:
    state_key: str
    repo: str
    selector: str
    profile: str
    state: str
    reason: str
    message: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "state_key": self.state_key,
            "repo": self.repo,
            "package_id": self.selector,
            "profile": self.profile,
            "state": self.state,
            "reason": self.reason,
            "message": self.message,
        }


@dataclass(frozen=True)
class TrackedPackageEntryDetail:
    package_entry: TrackedPackageEntrySummary
    tracked_reason: str
    targets: list[TrackedTargetSummary]
    hooks: dict[str, list[HookPlan]]

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.package_entry.to_dict(),
            "tracked_reason": self.tracked_reason,
            "targets": [target.to_dict() for target in self.targets],
            "hooks": {name: [item.to_dict() for item in items] for name, items in self.hooks.items()},
        }


@dataclass(frozen=True)
class TrackedOwnedTargetDetail:
    package_entry: TrackedPackageEntrySummary
    target: TrackedTargetSummary

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.package_entry.to_dict(),
            **self.target.to_dict(),
        }


@dataclass(frozen=True)
class TrackedPackageDetail:
    repo: str
    package_id: str
    description: str | None
    package_entries: list[TrackedPackageEntryDetail]
    owned_targets: list[TrackedOwnedTargetDetail]
    bound_profile: str | None = None

    @property
    def package_ref(self) -> str:
        return package_ref_text(package_id=self.package_id, bound_profile=self.bound_profile)

    def to_dict(self) -> dict[str, Any]:
        return {
            "repo": self.repo,
            "package_id": self.package_id,
            "package_ref": self.package_ref,
            "bound_profile": self.bound_profile,
            "description": self.description,
            "package_entries": [package_entry.to_dict() for package_entry in self.package_entries],
            "owned_targets": [target.to_dict() for target in self.owned_targets],
        }


@dataclass(frozen=True)
class TargetPlan:
    package_id: str
    target_name: str
    repo_path: Path
    live_path: Path
    action: str
    target_kind: str
    projection_kind: str
    render: str = "raw"
    capture: str = "raw"
    compare_repo: str = "raw"
    compare_live: str = "capture"
    sync_policy: str = "both"
    editor: EditorSpec = field(default_factory=EditorSpec)
    editor_explicit: bool = field(default=False, repr=False, compare=False)
    additional_sources: tuple[str, ...] = ()
    additional_source_entries: tuple[AdditionalSource, ...] = field(default=(), repr=False, compare=False)
    additional_sources_root: Path | None = field(default=None, repr=False, compare=False)
    render_command: str | None = None
    capture_command: str | None = None
    live_path_is_symlink: bool = field(default=False, repr=False)
    live_path_symlink_target: str | None = field(default=None, repr=False)
    file_symlink_mode: str = field(default="prompt", repr=False)
    dir_symlink_mode: str = field(default="fail", repr=False)
    chmod: str | None = None
    path_rules: tuple[TargetPathRule, ...] = ()
    command_cwd: Path | None = None
    command_env: dict[str, str] | None = field(default=None, repr=False)
    desired_bytes: bytes | None = field(default=None, repr=False)
    child_path: str | None = None
    probe_command: str | None = None

    def to_dict(self) -> dict[str, Any]:
        is_probe = self.target_kind == "probe"
        payload = {
            "package_id": self.package_id,
            "target_name": self.target_name,
            "repo_path": None if is_probe else str(self.repo_path),
            "live_path": None if is_probe else str(self.live_path),
            "action": self.action,
            "target_kind": self.target_kind,
            "projection_kind": self.projection_kind,
            "render": _serialized_projection(self.render),
            "capture": _serialized_projection(self.capture),
            "editor": self.editor.to_dict(),
            "additional_sources": list(self.additional_sources),
            "compare": {"repo": _serialized_projection(self.compare_repo), "live": _serialized_projection(self.compare_live)},
            "sync_policy": self.sync_policy,
            "chmod": self.chmod,
            "path_rules": [rule.to_dict() for rule in self.path_rules],
        }
        if self.probe_command is not None:
            payload["probe_command"] = self.probe_command
        return payload


@dataclass(frozen=True)
class PackagePlan:
    operation: str
    selection: ResolvedPackageSelection
    hooks: dict[str, list[HookPlan]]
    target_plans: list[TargetPlan]
    repo_root: Path | None = None

    @property
    def repo_name(self) -> str:
        return self.selection.identity.repo

    @property
    def package_id(self) -> str:
        return self.selection.identity.package_id

    @property
    def bound_profile(self) -> str | None:
        return self.selection.identity.bound_profile

    @property
    def requested_profile(self) -> str:
        return self.selection.requested_profile

    @property
    def selection_label(self) -> str:
        return self.selection.selection_label

    def to_dict(self) -> dict[str, Any]:
        return {
            "selection": self.selection.to_dict(),
            "selection_label": self.selection.selection_label,
            "repo": self.selection.identity.repo,
            "package_id": self.selection.identity.package_id,
            "bound_profile": self.selection.identity.bound_profile,
            "profile": self.selection.requested_profile,
            "requested_profile": self.selection.requested_profile,
            "explicit": self.selection.explicit,
            "source_kind": self.selection.source_kind,
            "source_selector": self.selection.source_selector,
            "targets": [target.to_dict() for target in self.target_plans],
            "hooks": {name: [item.to_dict() for item in items] for name, items in self.hooks.items()},
        }



