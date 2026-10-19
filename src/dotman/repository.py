from __future__ import annotations

from pathlib import Path
from typing import Any

from dotman.toml_utils import load_toml_file

from dotman.manifest import (
    _copy_map,
    build_hook_spec,
    build_target_spec,
    deep_merge,
    discover_package_manifests,
    merge_package_specs,
    path_rules_following_targets,
    _validate_resolved_package,
    normalize_default_command_elevation,
    normalize_gitignore_enabled,
    normalize_string_list,
    normalize_sync_policy,
    strip_package_extensions,
    require_optional_string,
    require_optional_table,
    validate_group_id,
    validate_profile_id,
    validate_supported_keys,
    validate_package_id,
)
from dotman.package_inheritance import patch_remove_and_append
from dotman.models import (
    DefaultCommandElevationMode,
    GroupSpec,
    HookSpec,
    PackageSpec,
    ProfileSpec,
    RepoConfig,
    RepoIgnoreDefaults,
)


VALID_HOOK_NAMES = (
    "guard_push",
    "pre_push",
    "post_push",
    "guard_pull",
    "pre_pull",
    "post_pull",
)
REPO_CONFIG_KEYS = frozenset({"default_command_elevation", "hooks", "ignore"})
REPO_IGNORE_KEYS = frozenset({"gitignore", "patterns", "skip_markers"})


def normalize_skip_markers(value: Any, *, repo_config_path: Path) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"repo config {repo_config_path} [ignore].skip_markers must be a list[str]")
    markers: list[str] = []
    for marker in value:
        if not marker:
            raise ValueError(f"repo config {repo_config_path} [ignore].skip_markers entries must not be empty")
        if marker in {".", ".."} or "/" in marker or "\\" in marker:
            raise ValueError(
                f"repo config {repo_config_path} [ignore].skip_markers entries must be basenames: {marker!r}"
            )
        markers.append(marker)
    return tuple(markers)


GROUP_FILE_KEYS = frozenset({"members", "description"})
PROFILE_FILE_KEYS = frozenset({"includes", "vars"})

PACKAGE_MANIFEST_TOP_LEVEL_KEYS = {
    "binding_mode",
    "append",
    "depends",
    "description",
    "extends",
    "hooks",
    "ignore",
    "remove",
    "reserved_paths",
    "sync_policy",
    "targets",
    "vars",
}


class MissingExtendsParentError(ValueError):
    """A package extends a parent the repo does not define.

    Distinct from other manifest errors (extends cycle, bad TOML): a dangling
    reference makes a tracked row stale, while malformed content is a repo error.
    """

    def __init__(self, *, repo_name: str, package_id: str, parent_id: str) -> None:
        self.repo_name = repo_name
        self.package_id = package_id
        super().__init__(f"package '{package_id}' extends '{parent_id}', which repo '{repo_name}' does not define")


class Repository:
    def __init__(self, config: RepoConfig) -> None:
        self.config = config
        self.root = config.path
        self._repo_config_payload = self._load_repo_config_payload()
        self.default_command_elevation = self._load_default_command_elevation()
        self.ignore_defaults = self._load_repo_ignore_defaults()
        self.hooks = self._load_repo_hooks()
        self.packages = self._load_packages()
        self.groups = self._load_groups()
        self.profiles = self._load_profiles()
        self.local_vars = self._load_local_vars()
        self._resolved_packages: dict[str, PackageSpec] = {}

    def _load_repo_config_payload(self) -> dict[str, Any]:
        repo_config_path = self.root / "repo.toml"
        if not repo_config_path.exists():
            return {}
        payload = load_toml_file(repo_config_path, context="repo config")
        validate_supported_keys(
            payload,
            supported_keys=REPO_CONFIG_KEYS,
            context=f"repo config {repo_config_path}",
        )
        return payload

    def _load_default_command_elevation(self) -> DefaultCommandElevationMode:
        return normalize_default_command_elevation(
            self._repo_config_payload.get("default_command_elevation"),
            manifest_path=self.root / "repo.toml",
        )

    def _load_repo_ignore_defaults(self) -> RepoIgnoreDefaults:
        repo_config_path = self.root / "repo.toml"
        if not repo_config_path.exists():
            return RepoIgnoreDefaults()
        payload = self._repo_config_payload
        ignore_payload = payload.get("ignore")
        if ignore_payload is None:
            return RepoIgnoreDefaults()
        if not isinstance(ignore_payload, dict):
            raise ValueError(f"repo config {repo_config_path} [ignore] must be a table")
        validate_supported_keys(
            ignore_payload,
            supported_keys=REPO_IGNORE_KEYS,
            context=f"repo config {repo_config_path} [ignore]",
        )
        ignore_context = f"repo config {repo_config_path} [ignore]"
        patterns = normalize_string_list(ignore_payload.get("patterns"), context=ignore_context, key="patterns") or ()
        gitignore = normalize_gitignore_enabled(ignore_payload.get("gitignore"), context=ignore_context) or False
        return RepoIgnoreDefaults(
            patterns=patterns,
            skip_markers=normalize_skip_markers(ignore_payload.get("skip_markers"), repo_config_path=repo_config_path),
            gitignore=gitignore,
        )

    def _load_repo_hooks(self) -> dict[str, HookSpec] | None:
        repo_config_path = self.root / "repo.toml"
        if not repo_config_path.exists():
            return None
        hooks_payload = self._repo_config_payload.get("hooks")
        if hooks_payload is None:
            return None
        if not isinstance(hooks_payload, dict):
            raise ValueError(f"repo config {repo_config_path} [hooks] must be a table")
        unknown_hook_names = [hook_name for hook_name in hooks_payload if hook_name not in VALID_HOOK_NAMES]
        if unknown_hook_names:
            unknown_text = ", ".join(sorted(unknown_hook_names))
            raise ValueError(
                f"repo config {repo_config_path} uses unsupported hook names: {unknown_text}"
            )
        return {
            hook_name: build_hook_spec(
                hook_name=hook_name,
                hook_payload=hook_value,
                manifest_path=repo_config_path,
                owner_label="repo",
                manifest_kind="repo config",
                default_command_elevation=self.default_command_elevation,
            )
            for hook_name, hook_value in hooks_payload.items()
        }

    def _load_packages(self) -> dict[str, PackageSpec]:
        packages: dict[str, PackageSpec] = {}
        packages_root = self.root / "packages"
        for manifest_path in discover_package_manifests(packages_root):
            package_id = manifest_path.relative_to(packages_root).parent.as_posix()
            payload = load_toml_file(
                manifest_path,
                context="package manifest",
                package_repo=self.config.name,
                package_id=package_id,
            )
            if "id" in payload:
                raise ValueError(
                    f"package manifest {manifest_path} must not define id: "
                    f"the package id '{package_id}' comes from its folder; remove the id line"
                )
            try:
                validate_package_id(package_id)
            except ValueError as exc:
                raise ValueError(f"package manifest {manifest_path}: {exc}") from None
            binding_mode = str(payload.get("binding_mode", "singleton"))
            if binding_mode not in {"singleton", "multi_instance"}:
                raise ValueError(
                    f"package manifest {manifest_path} has unsupported binding_mode '{binding_mode}'"
                )
            unknown_top_level_keys = sorted(key for key in payload if key not in PACKAGE_MANIFEST_TOP_LEVEL_KEYS)
            if unknown_top_level_keys:
                unknown_text = ", ".join(unknown_top_level_keys)
                raise ValueError(f"package manifest {manifest_path} has unknown top-level keys: {unknown_text}")
            context = f"package manifest {manifest_path}"
            targets_payload = require_optional_table(payload.get("targets"), context=context, key="targets")
            hooks_payload = require_optional_table(payload.get("hooks"), context=context, key="hooks")
            vars_payload = require_optional_table(payload.get("vars"), context=context, key="vars")
            append_payload = require_optional_table(payload.get("append"), context=context, key="append")
            targets = (
                {
                    target_name: build_target_spec(
                        target_name=target_name,
                        target_payload=target_payload,
                        manifest_path=manifest_path,
                        default_command_elevation=self.default_command_elevation,
                    )
                    for target_name, target_payload in targets_payload.items()
                }
                if targets_payload is not None
                else None
            )
            hooks = None
            if hooks_payload is not None:
                unknown_hook_names = [hook_name for hook_name in hooks_payload if hook_name not in VALID_HOOK_NAMES]
                if unknown_hook_names:
                    unknown_text = ", ".join(sorted(unknown_hook_names))
                    raise ValueError(
                        f"package manifest {manifest_path} uses unsupported hook names: {unknown_text}"
                    )
                hooks = {
                    hook_name: build_hook_spec(
                        hook_name=hook_name,
                        hook_payload=hook_value,
                        manifest_path=manifest_path,
                        default_command_elevation=self.default_command_elevation,
                    )
                    for hook_name, hook_value in hooks_payload.items()
                }
            package_ignore_payload = payload.get("ignore")
            if package_ignore_payload is not None:
                if not isinstance(package_ignore_payload, dict):
                    raise ValueError(f"package manifest {manifest_path} ignore must be a table")
                validate_supported_keys(package_ignore_payload, supported_keys={"gitignore", "patterns"},
                                        context=f"package manifest {manifest_path} ignore")
                package_ignore_patterns = normalize_string_list(
                    package_ignore_payload.get("patterns"), context=f"{context} ignore", key="patterns"
                )
                package_gitignore_enabled = normalize_gitignore_enabled(
                    package_ignore_payload.get("gitignore"), context=f"{context} ignore"
                )
            else:
                package_ignore_patterns = None
                package_gitignore_enabled = None

            packages[package_id] = PackageSpec(
                id=package_id,
                package_root=manifest_path.parent,
                description=require_optional_string(payload.get("description"), context=context, key="description"),
                binding_mode=binding_mode,
                binding_mode_explicit="binding_mode" in payload,
                sync_policy=normalize_sync_policy(payload.get("sync_policy"), context=context),
                depends=normalize_string_list(payload.get("depends"), context=context, key="depends"),
                extends=normalize_string_list(payload.get("extends"), context=context, key="extends"),
                reserved_paths=normalize_string_list(payload.get("reserved_paths"), context=context, key="reserved_paths"),
                ignore_patterns=package_ignore_patterns,
                gitignore_enabled=package_gitignore_enabled,
                vars=_copy_map(vars_payload) if vars_payload is not None else None,
                targets=targets,
                hooks=hooks,
                remove=normalize_string_list(payload.get("remove"), context=context, key="remove"),
                append=_copy_map(append_payload) if append_payload is not None else None,
            )
        return packages

    def _load_groups(self) -> dict[str, GroupSpec]:
        groups: dict[str, GroupSpec] = {}
        groups_root = self.root / "groups"
        if not groups_root.exists():
            return groups
        for group_path in sorted(groups_root.glob("**/*.toml")):
            group_id = group_path.relative_to(groups_root).with_suffix("").as_posix()
            payload = load_toml_file(group_path, context=f"group file for repo '{self.config.name}' group '{group_id}'")
            context = f"group file {group_path}"
            try:
                validate_group_id(group_id)
            except ValueError as exc:
                raise ValueError(f"{context}: {exc}") from None
            validate_supported_keys(payload, supported_keys=GROUP_FILE_KEYS, context=context)
            groups[group_id] = GroupSpec(
                id=group_id,
                members=normalize_string_list(payload.get("members"), context=context, key="members") or (),
                path=group_path,
                description=require_optional_string(payload.get("description"), context=context, key="description"),
            )
        return groups

    def _load_profiles(self) -> dict[str, ProfileSpec]:
        profiles: dict[str, ProfileSpec] = {}
        profiles_root = self.root / "profiles"
        if not profiles_root.exists():
            return profiles
        for profile_path in sorted(profiles_root.glob("**/*.toml")):
            profile_id = profile_path.relative_to(profiles_root).with_suffix("").as_posix()
            payload = load_toml_file(profile_path, context=f"profile file for repo '{self.config.name}' profile '{profile_id}'")
            context = f"profile file {profile_path}"
            try:
                validate_profile_id(profile_id)
            except ValueError as exc:
                raise ValueError(f"{context}: {exc}") from None
            validate_supported_keys(payload, supported_keys=PROFILE_FILE_KEYS, context=context)
            vars_payload = require_optional_table(payload.get("vars"), context=context, key="vars")
            profiles[profile_id] = ProfileSpec(
                id=profile_id,
                includes=normalize_string_list(payload.get("includes"), context=context, key="includes") or (),
                vars=_copy_map(vars_payload) if vars_payload is not None else {},
                path=profile_path,
            )
        return profiles

    def _load_local_vars(self) -> dict[str, Any]:
        local_path = self.config.local_override_path
        if not local_path.exists():
            return {}
        payload = load_toml_file(local_path, context=f"local override for repo '{self.config.name}'")
        unknown_top_level_keys = sorted(key for key in payload if key != "vars")
        if unknown_top_level_keys:
            unknown_text = ", ".join(unknown_top_level_keys)
            raise ValueError(f"local override {local_path} has unknown top-level keys: {unknown_text}")
        vars_payload = payload.get("vars", {})
        if not isinstance(vars_payload, dict):
            raise ValueError(f"local override {local_path} [vars] must be a table")
        return _copy_map(vars_payload)

    def compose_profile(self, profile_id: str) -> tuple[dict[str, Any], list[str]]:
        if profile_id not in self.profiles:
            raise ValueError(f"unknown profile '{profile_id}' in repo '{self.config.name}'")

        lineage: list[str] = []

        def visit(current_id: str, stack: tuple[str, ...]) -> dict[str, Any]:
            if current_id in stack:
                cycle = " -> ".join([*stack, current_id])
                raise ValueError(f"profile include cycle detected in repo '{self.config.name}': {cycle}")
            if current_id not in self.profiles:
                raise ValueError(
                    f"profile '{stack[-1]}' includes unknown profile '{current_id}' in repo '{self.config.name}'"
                )
            profile = self.profiles[current_id]
            merged: dict[str, Any] = {}
            for include_id in profile.includes:
                merged = deep_merge(merged, visit(include_id, (*stack, current_id)))
            lineage.append(current_id)
            return deep_merge(merged, profile.vars)

        return visit(profile_id, ()), lineage

    def resolve_package(self, package_id: str) -> PackageSpec:
        return self._resolve_package(package_id, validate=True)

    def _resolve_package(self, package_id: str, *, validate: bool, extending: tuple[str, ...] = ()) -> PackageSpec:
        cached = self._resolved_packages.get(package_id)
        if cached is not None:
            return cached
        if package_id in extending:
            cycle = " -> ".join([*extending, package_id])
            raise ValueError(f"package extends cycle detected in repo '{self.config.name}': {cycle}")
        if package_id not in self.packages:
            raise ValueError(f"unknown package '{package_id}' in repo '{self.config.name}'")
        loaded = self.packages[package_id]
        merged: PackageSpec | None = None
        for parent_id in loaded.extends or ():
            if parent_id not in self.packages:
                raise MissingExtendsParentError(
                    repo_name=self.config.name, package_id=package_id, parent_id=parent_id
                )
            # A parent may contribute only part of a keyed path rule. Defer
            # completeness checks until all parents and the child are merged.
            parent_spec = self._resolve_package(parent_id, validate=False, extending=(*extending, package_id))
            merged = parent_spec if merged is None else merge_package_specs(merged, parent_spec)
        current = strip_package_extensions(loaded)
        merged = current if merged is None else merge_package_specs(merged, current)
        # Patches address a path rule's effective values, which follow the merged target.
        merged = path_rules_following_targets(merged)
        merged = patch_remove_and_append(
            merged,
            loaded.remove or (),
            loaded.append or {},
            default_command_elevation=self.default_command_elevation,
        )
        merged = path_rules_following_targets(merged)
        if validate:
            merged = _validate_resolved_package(merged, repo_name=self.config.name)
            self._resolved_packages[package_id] = merged
        return merged

    def package_binding_mode(self, package_id: str) -> str:
        return self.resolve_package(package_id).binding_mode

    def expand_group(self, group_id: str) -> list[str]:
        if group_id not in self.groups:
            raise ValueError(f"unknown group '{group_id}' in repo '{self.config.name}'")
        ordered: list[str] = []
        seen: set[str] = set()

        def visit(current_group_id: str, stack: tuple[str, ...]) -> None:
            if current_group_id in stack:
                cycle = " -> ".join([*stack, current_group_id])
                raise ValueError(f"group membership cycle detected in repo '{self.config.name}': {cycle}")
            current_group = self.groups[current_group_id]
            for member in current_group.members:
                package_exists = member in self.packages
                group_exists = member in self.groups
                if package_exists and group_exists:
                    raise ValueError(
                        f"selector '{member}' is ambiguous between package and group in repo '{self.config.name}'"
                    )
                if group_exists:
                    visit(member, (*stack, current_group_id))
                    continue
                if not package_exists:
                    raise ValueError(f"group member '{member}' does not resolve in repo '{self.config.name}'")
                if member not in seen:
                    seen.add(member)
                    ordered.append(member)

        visit(group_id, ())
        return ordered
