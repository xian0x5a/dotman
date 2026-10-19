from __future__ import annotations

import re
import sys
from collections.abc import Collection
from dataclasses import replace
from pathlib import Path
from typing import Any

from dotman.models import AdditionalSource, DefaultCommandElevationMode, EditorSpec, HookCommandSpec, HookSpec, PackageSpec, TargetPathRule, TargetSpec, repo_qualified_target_text
from dotman.presets import BUILTIN_TARGET_PRESETS, get_builtin_target_preset


VALID_COMMAND_IO_VALUES = ("pipe", "tty")
VALID_HOOK_IO_VALUES = VALID_COMMAND_IO_VALUES
VALID_ELEVATION_VALUES = ("none", "root", "intercept")
VALID_DEFAULT_COMMAND_ELEVATION_VALUES = ("none", "intercept")
VALID_SYNC_POLICY_VALUES = ("push-only", "pull-only", "both", "push-only-delete")
VALID_TARGET_TYPE_VALUES = ("file", "directory")
FORCED_COMMAND_PREFIX = "__dotman_command__:"
PACKAGE_MANIFEST_FILE_NAME = "package.toml"
TARGET_MANIFEST_KEYS = frozenset(
    {"capture", "chmod", "compare", "disabled", "editor", "hooks", "ignore", "on_demand",
     "path", "path_rules", "preset", "probe", "render", "source", "sync_policy", "type"}
)

TARGET_PATH_RULE_KEYS = frozenset(
    {"capture", "chmod", "compare", "editor", "hooks", "pattern", "priority",
     "preset", "render", "sync_policy"}
)

TARGET_IGNORE_KEYS = frozenset({"command", "patterns"})


def validate_supported_keys(
    payload: dict[str, Any],
    *,
    supported_keys: Collection[str],
    context: str,
) -> None:
    unsupported_keys = sorted(key for key in payload if key not in supported_keys)
    if unsupported_keys:
        unsupported_text = ", ".join(unsupported_keys)
        raise ValueError(f"{context} has unsupported keys: {unsupported_text}")


def _validate_namespaced_id(selector_id: str, *, kind: str) -> None:
    if not selector_id.strip():
        raise ValueError(f"{kind} id must not be empty")
    if any(character in selector_id for character in SELECTOR_ID_RESERVED_CHARACTERS):
        raise ValueError(f"invalid {kind} id '{selector_id}'")
    parts = selector_id.split("/")
    if any(not part or part in {".", ".."} or any(character.isspace() for character in part) for part in parts):
        raise ValueError(f"invalid {kind} id '{selector_id}'")


# Package and group IDs are selectors in `repo:selector@profile` and
# `repo:package<profile>.target`, so they can't hold any of that syntax.
SELECTOR_ID_RESERVED_CHARACTERS = ("\\", ":", "@", "<", ">", ".")
# Profile IDs sit inside `@profile` and `<profile>`, where dots, spaces and
# other punctuation are allowed; only the characters that delimit them are reserved.
PROFILE_ID_RESERVED_CHARACTERS = ("@", "<", ">")


def validate_package_id(package_id: str) -> None:
    _validate_namespaced_id(package_id, kind="package")


def validate_group_id(group_id: str) -> None:
    _validate_namespaced_id(group_id, kind="group")


def validate_profile_id(profile_id: str) -> None:
    if any(character in profile_id for character in PROFILE_ID_RESERVED_CHARACTERS):
        raise ValueError(f"invalid profile id '{profile_id}'")


def discover_package_manifests(packages_root: Path) -> list[Path]:
    """Find package manifests, treating everything inside a package folder as package content.

    A folder holding `package.toml` is a package, so the walk stops descending there:
    packages cannot nest, and a dotfile source that is itself named `package.toml`
    (for example `packages/app/files/package.toml`) is not mistaken for a manifest.
    Folders without a manifest are namespaces, e.g. `packages/work/git/package.toml`.
    """
    manifest_paths: list[Path] = []
    for directory, child_directories, file_names in packages_root.walk():
        if PACKAGE_MANIFEST_FILE_NAME in file_names:
            manifest_paths.append(directory / PACKAGE_MANIFEST_FILE_NAME)
            child_directories.clear()
    return sorted(manifest_paths)


def validate_target_name(target_name: str) -> None:
    if (
        not target_name.strip()
        or "." in target_name
        # `/` separates directory-child paths in canonical sync identities.
        or "/" in target_name
        or any(character.isspace() for character in target_name)
    ):
        raise ValueError(f"invalid target name '{target_name}'")


def _copy_map(value: dict[str, Any] | None) -> dict[str, Any]:
    if value is None:
        return {}
    result: dict[str, Any] = {}
    for key, item in value.items():
        if isinstance(item, dict):
            result[key] = _copy_map(item)
        elif isinstance(item, list):
            result[key] = list(item)
        else:
            result[key] = item
    return result


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = _copy_map(base)
    for key, override_value in override.items():
        base_value = merged.get(key)
        if isinstance(base_value, dict) and isinstance(override_value, dict):
            merged[key] = deep_merge(base_value, override_value)
        elif isinstance(override_value, list):
            merged[key] = list(override_value)
        elif isinstance(override_value, dict):
            merged[key] = _copy_map(override_value)
        else:
            merged[key] = override_value
    return merged


# Shared field validators take the owner label (`context`, e.g. "package
# manifest <path> target 'a'") and the manifest key, so every error names the
# file and field that are wrong.
def normalize_string_list(value: Any, *, context: str, key: str) -> tuple[str, ...] | None:
    if value is None:
        return None
    if isinstance(value, str):
        return (value,)
    if not isinstance(value, list):
        raise ValueError(f"{context} {key} must be a string or list of strings, got {type(value).__name__}")
    for index, item in enumerate(value):
        if not isinstance(item, str):
            raise ValueError(f"{context} {key}[{index}] must be a string, got {type(item).__name__}")
    return tuple(value)


def require_optional_string(value: Any, *, context: str, key: str) -> str | None:
    if value is not None and not isinstance(value, str):
        raise ValueError(f"{context} {key} must be a string, got {type(value).__name__}")
    return value


def require_optional_table(value: Any, *, context: str, key: str) -> dict[str, Any] | None:
    if value is not None and not isinstance(value, dict):
        raise ValueError(f"{context} {key} must be a table, got {type(value).__name__}")
    return value


def normalize_hook_command_specs(
    value: Any,
    *,
    manifest_kind: str,
    manifest_path: Path,
    owner_label: str,
    hook_name: str,
    default_command_elevation: DefaultCommandElevationMode = "none",
) -> tuple[HookCommandSpec, ...]:
    if isinstance(value, str):
        return (HookCommandSpec(run=value, elevation=default_command_elevation),)
    if not isinstance(value, list):
        raise ValueError(
            f"{manifest_kind} {manifest_path} {owner_label} hook '{hook_name}' commands must be a string or list"
        )

    commands: list[HookCommandSpec] = []
    for item in value:
        if isinstance(item, str):
            commands.append(HookCommandSpec(run=item, elevation=default_command_elevation))
            continue
        if isinstance(item, dict):
            commands.append(
                _build_hook_command_spec(
                    command_payload=item,
                    manifest_kind=manifest_kind,
                    manifest_path=manifest_path,
                    owner_label=owner_label,
                    hook_name=hook_name,
                    default_command_elevation=default_command_elevation,
                )
            )
            continue
        raise ValueError(
            f"{manifest_kind} {manifest_path} {owner_label} hook '{hook_name}' commands must contain only strings or command objects"
        )
    return tuple(commands)


def _build_hook_command_spec(
    *,
    command_payload: dict[str, Any],
    manifest_kind: str,
    manifest_path: Path,
    owner_label: str,
    hook_name: str,
    default_command_elevation: DefaultCommandElevationMode = "none",
) -> HookCommandSpec:
    planning_guard = hook_name.startswith("guard_")
    command_spec = _build_command_spec(
        command_payload=command_payload,
        manifest_kind=manifest_kind,
        manifest_path=manifest_path,
        owner_label=owner_label,
        command_label=f"hook '{hook_name}' command object",
        default_command_elevation=default_command_elevation,
        allow_run_noop=not planning_guard,
    )
    if planning_guard and command_spec.io != "pipe":
        raise ValueError(
            f"{manifest_kind} {manifest_path} {owner_label} hook '{hook_name}' command io must be 'pipe'"
        )
    return command_spec


def _build_command_spec(
    *,
    command_payload: dict[str, Any],
    manifest_kind: str,
    manifest_path: Path,
    owner_label: str,
    command_label: str,
    default_command_elevation: DefaultCommandElevationMode = "none",
    allow_run_noop: bool = False,
) -> HookCommandSpec:
    supported_keys = {"run", "io", "elevation"}
    if allow_run_noop:
        supported_keys.add("run_noop")
    validate_supported_keys(
        command_payload,
        supported_keys=supported_keys,
        context=f"{manifest_kind} {manifest_path} {owner_label} {command_label}",
    )
    if "run" not in command_payload:
        raise ValueError(
            f"{manifest_kind} {manifest_path} {owner_label} {command_label} must define 'run'"
        )
    run_value = command_payload.get("run")
    if not isinstance(run_value, str):
        raise ValueError(
            f"{manifest_kind} {manifest_path} {owner_label} {command_label} 'run' must be a string"
        )
    if not run_value.strip():
        raise ValueError(
            f"{manifest_kind} {manifest_path} {owner_label} {command_label} 'run' must not be empty"
        )
    command_context = f"{manifest_kind} {manifest_path} {owner_label} {command_label}"
    io_value = normalize_optional_string_enum(
        command_payload.get("io"), context=command_context, key="io", allowed=VALID_HOOK_IO_VALUES
    ) or "pipe"
    elevation_value = normalize_optional_string_enum(
        command_payload.get("elevation"),
        context=command_context,
        key="elevation",
        allowed=VALID_ELEVATION_VALUES,
    ) or default_command_elevation
    run_noop_value = command_payload.get("run_noop", False) if allow_run_noop else False
    if allow_run_noop and not isinstance(run_noop_value, bool):
        raise ValueError(
            f"{manifest_kind} {manifest_path} {owner_label} {command_label} run_noop must be a boolean"
        )
    return HookCommandSpec(run=run_value, io=io_value, elevation=elevation_value, run_noop=run_noop_value)


def normalize_optional_string_enum(value: Any, *, context: str, key: str, allowed: tuple[str, ...]) -> str | None:
    if require_optional_string(value, context=context, key=key) is None:
        return None
    if value not in allowed:
        allowed_text = ", ".join(allowed)
        raise ValueError(f"{context} has unsupported {key} '{value}'; expected one of: {allowed_text}")
    return value


def normalize_default_command_elevation(value: Any, *, manifest_path: Path) -> DefaultCommandElevationMode:
    normalized = normalize_optional_string_enum(
        value,
        context=f"repo config {manifest_path}",
        key="default_command_elevation",
        allowed=VALID_DEFAULT_COMMAND_ELEVATION_VALUES,
    )
    return normalized or "none"


def normalize_sync_policy(value: Any, *, context: str) -> str | None:
    return normalize_optional_string_enum(value, context=context, key="sync_policy", allowed=VALID_SYNC_POLICY_VALUES)


# int(value, 8) alone also accepts "-755", "0o755" and modes beyond 0o7777,
# none of which a live file can ever report back.
CHMOD_PATTERN = re.compile(r"[0-7]{3,4}")


def _invalid_chmod(value: str) -> bool:
    return CHMOD_PATTERN.fullmatch(value) is None


def normalize_projection(value: Any, *, context: str, key: str, default: str, builtins: tuple[str, ...]) -> str:
    """Resolve a scalar projection or an explicit {run = ...} command."""
    if value is None:
        return default
    if isinstance(value, str):
        if not value.strip():
            raise ValueError(f"{context} {key} must not be empty")
        return value
    if isinstance(value, dict):
        validate_supported_keys(value, supported_keys={"run"}, context=f"{context} {key}")
        run = value.get("run")
        if not isinstance(run, str) or not run.strip():
            raise ValueError(f"{context} {key} command object must define non-empty 'run'")
        return f"{FORCED_COMMAND_PREFIX}{run}" if run in builtins else run
    raise ValueError(f"{context} {key} must be a string or table containing only 'run'")


def normalize_additional_sources(value: Any, *, manifest_path: Path, target_name: str) -> tuple[str, ...]:
    values = normalize_string_list(
        value, context=f"package manifest {manifest_path} target '{target_name}' editor", key="additional_sources"
    ) or ()
    result: list[str] = []
    for source in values:
        normalized = source.replace("\\", "/")
        path = Path(normalized)
        if path.is_absolute() or any(part in {"", ".", ".."} for part in normalized.split("/")):
            raise ValueError(f"package manifest {manifest_path} target '{target_name}' editor additional_sources must be relative paths within the package")
        if normalized not in result:
            result.append(normalized)
    return tuple(result)


def normalize_compare(value: Any, *, manifest_path: Path, target_name: str) -> tuple[str, str]:
    if value is None:
        return "raw", "capture"
    if not isinstance(value, dict):
        raise ValueError(f"package manifest {manifest_path} target '{target_name}' compare must be a table")
    validate_supported_keys(value, supported_keys={"repo", "live"}, context=f"package manifest {manifest_path} target '{target_name}' compare")
    context = f"package manifest {manifest_path} target '{target_name}'"
    repo = normalize_compare_side(value.get("repo"), side="repo", field_name="compare.repo", default="raw", context=context)
    live = normalize_compare_side(value.get("live"), side="live", field_name="compare.live", default="capture", context=context)
    return repo, live


# Each comparison side's own builtin view; the other is Render or Capture.
COMPARE_SIDE_VIEWS: dict[str, str] = {"repo": "render", "live": "capture"}


def normalize_compare_side(value: Any, *, side: str, field_name: str, default: str, context: str) -> str:
    other_side = "live" if side == "repo" else "repo"
    other_view = COMPARE_SIDE_VIEWS[other_side]
    # The other side's builtin would project the wrong endpoint: live Render
    # yields repository bytes, so drift hides. A command of that name needs
    # run, and is forced so projection never reads it as the builtin.
    if value == other_view:
        raise ValueError(
            f'{context} compare.{side} = "{other_view}" is a compare.{other_side} view; '
            f'use compare = {{ {side} = "raw", {other_side} = "{other_view}" }}, or {{ run = "{other_view}" }} for a command named {other_view}'
        )
    return normalize_projection(value, context=context, key=field_name, default=default,
                                builtins=("raw", COMPARE_SIDE_VIEWS[side], other_view))


def normalize_editor(value: Any, *, manifest_path: Path, target_name: str, default_elevation: DefaultCommandElevationMode = "none") -> EditorSpec:
    if value is None:
        return EditorSpec()
    if isinstance(value, str):
        if not value.strip():
            raise ValueError(f"package manifest {manifest_path} target '{target_name}' editor must not be empty")
        if value in {"default", "jinja"}:
            return EditorSpec(type=value)
        return EditorSpec(type=None, run=value, elevation=default_elevation)
    if not isinstance(value, dict):
        raise ValueError(f"package manifest {manifest_path} target '{target_name}' editor must be a string or table")
    validate_supported_keys(value, supported_keys={"type", "run", "io", "elevation", "additional_sources"},
                           context=f"package manifest {manifest_path} target '{target_name}' editor")
    has_type, has_run = "type" in value, "run" in value
    if has_type == has_run:
        raise ValueError(f"package manifest {manifest_path} target '{target_name}' editor must define exactly one of 'type' or 'run'")
    provider_type = value.get("type") if has_type else None
    if provider_type is not None and (not isinstance(provider_type, str) or provider_type not in {"default", "jinja"}):
        raise ValueError(f"package manifest {manifest_path} target '{target_name}' editor type must be 'default' or 'jinja'")
    run = value.get("run") if has_run else None
    if run is not None and (not isinstance(run, str) or not run.strip()):
        raise ValueError(f"package manifest {manifest_path} target '{target_name}' editor run must be a non-empty string")
    editor_context = f"package manifest {manifest_path} target '{target_name}'"
    io = normalize_optional_string_enum(value.get("io"), context=editor_context, key="editor.io", allowed=VALID_COMMAND_IO_VALUES) or "tty"
    elevation = normalize_optional_string_enum(value.get("elevation"), context=editor_context, key="editor.elevation", allowed=VALID_ELEVATION_VALUES) or (default_elevation if provider_type is None else "none")
    if provider_type is not None and elevation != "none":
        raise ValueError(f"package manifest {manifest_path} target '{target_name}' built-in editor cannot request elevation")
    sources = normalize_additional_sources(value.get("additional_sources"), manifest_path=manifest_path, target_name=target_name)
    source_entries = tuple(AdditionalSource(path, manifest_path.parent) for path in sources)
    return EditorSpec(
        type=provider_type,
        run=run,
        io=io,
        elevation=elevation,
        additional_sources=sources,
        additional_source_entries=source_entries,
        additional_sources_root=manifest_path.parent if sources else None,
        additional_sources_explicit="additional_sources" in value,
    )


def normalize_target_type(value: Any, *, context: str) -> str | None:
    return normalize_optional_string_enum(value, context=context, key="type", allowed=VALID_TARGET_TYPE_VALUES)


def normalize_target_command(value: Any, *, field_name: str, manifest_path: Path, target_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(
            f"package manifest {manifest_path} target '{target_name}' {field_name} must be a string"
        )
    if not value.strip():
        raise ValueError(
            f"package manifest {manifest_path} target '{target_name}' {field_name} must not be empty"
        )
    return value


def resolve_sync_policy(*, package: PackageSpec, target: TargetSpec) -> str:
    return target.sync_policy or package.sync_policy or "both"


def sync_policy_allows_operation(sync_policy: str, *, operation: str) -> bool:
    if sync_policy == "both":
        return True
    if sync_policy in {"push-only", "push-only-delete"}:
        return operation == "push"
    if sync_policy == "pull-only":
        return operation == "pull"
    raise ValueError(f"unsupported sync policy '{sync_policy}'")



def resolve_target_preset(
    *,
    target_payload: dict[str, Any],
    manifest_path: Path,
    target_name: str,
) -> dict[str, Any]:
    preset_name = target_payload.get("preset")
    if preset_name is None:
        return {}
    if not isinstance(preset_name, str):
        raise ValueError(
            f"package manifest {manifest_path} target '{target_name}' preset must be a string"
        )
    preset = get_builtin_target_preset(preset_name)
    if preset is None:
        available = ", ".join(sorted(BUILTIN_TARGET_PRESETS))
        raise ValueError(
            f"package manifest {manifest_path} target '{target_name}' uses unknown preset '{preset_name}'; "
            f"available presets: {available}"
        )
    return preset


def get_target_value(
    *,
    target_payload: dict[str, Any],
    preset_payload: dict[str, Any],
    key: str,
) -> Any:
    if key in target_payload:
        return target_payload[key]
    return preset_payload.get(key)


def normalize_path_rule_hooks(
    value: Any,
    *,
    manifest_path: Path,
    target_name: str,
    rule_index: int,
    default_command_elevation: DefaultCommandElevationMode,
) -> dict[str, HookSpec] | None:
    if value is None:
        return None
    owner_label = f"target '{target_name}' path_rules[{rule_index}]"
    if not isinstance(value, dict):
        raise ValueError(f"package manifest {manifest_path} {owner_label}.hooks must be a table")
    unknown_hook_names = sorted(key for key in value if key not in {"guard_push", "guard_pull"})
    if unknown_hook_names:
        unknown_text = ", ".join(unknown_hook_names)
        raise ValueError(
            f"package manifest {manifest_path} {owner_label} uses unsupported hook names: {unknown_text}"
        )
    return {
        hook_name: build_hook_spec(
            hook_name=hook_name,
            hook_payload=hook_value,
            manifest_path=manifest_path,
            owner_label=owner_label,
            default_command_elevation=default_command_elevation,
        )
        for hook_name, hook_value in value.items()
    }


def normalize_target_path_rules(
    value: Any,
    *,
    manifest_path: Path,
    target_name: str,
    default_command_elevation: DefaultCommandElevationMode = "none",
    inherited_render: str = "raw",
    inherited_capture: str = "raw",
    inherited_compare_repo: str = "raw",
    inherited_compare_live: str = "capture",
    inherited_editor: EditorSpec | None = None,
    inherited_sync_policy: str | None = None,
) -> tuple[TargetPathRule, ...]:
    if value is None:
        return ()
    if not isinstance(value, dict):
        raise ValueError(f"package manifest {manifest_path} target '{target_name}' path_rules must be a table")
    rules: list[TargetPathRule] = []
    for name, raw_payload in value.items():
        if not isinstance(name, str) or not name or any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for ch in name):
            raise ValueError(f"package manifest {manifest_path} target '{target_name}' path rule names must contain only letters, numbers, '_' or '-'")
        if not isinstance(raw_payload, dict):
            raise ValueError(f"package manifest {manifest_path} target '{target_name}' path_rules.{name} must be a table")
        payload = dict(raw_payload)
        validate_supported_keys(payload, supported_keys=TARGET_PATH_RULE_KEYS,
                                context=f"package manifest {manifest_path} target '{target_name}' path_rules.{name}")
        preset_payload: dict[str, Any] = {}
        preset_name = payload.get("preset")
        if preset_name is not None:
            if not isinstance(preset_name, str):
                raise ValueError(f"package manifest {manifest_path} target '{target_name}' path_rules.{name}.preset must be a string")
            preset_payload = get_builtin_target_preset(preset_name) or {}
            if not preset_payload:
                raise ValueError(f"package manifest {manifest_path} target '{target_name}' path_rules.{name} uses unknown preset '{preset_name}'")
        def val(key: str, fallback: Any) -> Any:
            return payload[key] if key in payload else (preset_payload.get(key, fallback))
        pattern = val("pattern", "")
        if not isinstance(pattern, str):
            raise ValueError(f"package manifest {manifest_path} target '{target_name}' path_rules.{name}.pattern must be a string")
        if not pattern.strip() and "pattern" in payload:
            raise ValueError(f"package manifest {manifest_path} target '{target_name}' path_rules.{name}.pattern must be a non-empty string")
        normalized_pattern = pattern.replace("\\", "/")
        if normalized_pattern.startswith("/") or any(part == ".." for part in normalized_pattern.split("/")):
            raise ValueError(f"package manifest {manifest_path} target '{target_name}' path_rules.{name}.pattern must be relative to the target root")
        priority = val("priority", 0)
        if not isinstance(priority, int) or isinstance(priority, bool):
            raise ValueError(f"package manifest {manifest_path} target '{target_name}' path_rules.{name}.priority must be an integer")
        rule_context = f"package manifest {manifest_path} target '{target_name}' path_rules.{name}"
        # Field errors read `target 't' path_rules.r.render ...`, like the
        # pattern/priority/chmod errors above.
        target_context, rule_key = f"package manifest {manifest_path} target '{target_name}'", f"path_rules.{name}"
        render = normalize_projection(val("render", inherited_render), context=target_context, key=f"{rule_key}.render", default=inherited_render, builtins=("raw", "jinja"))
        capture = normalize_projection(val("capture", inherited_capture), context=target_context, key=f"{rule_key}.capture", default=inherited_capture, builtins=("raw", "patch"))
        preset_compare = preset_payload.get("compare")
        rule_compare = payload.get("compare")
        if preset_compare is not None and not isinstance(preset_compare, dict):
            raise ValueError(f"package manifest {manifest_path} target '{target_name}' path_rules.{name}.preset compare must be a table")
        if rule_compare is not None and not isinstance(rule_compare, dict):
            raise ValueError(f"package manifest {manifest_path} target '{target_name}' path_rules.{name}.compare must be a table")
        if preset_compare is not None:
            validate_supported_keys(preset_compare, supported_keys={"repo", "live"},
                                    context=f"package manifest {manifest_path} target '{target_name}' path_rules.{name}.preset compare")
        if rule_compare is not None:
            validate_supported_keys(rule_compare, supported_keys={"repo", "live"},
                                    context=f"package manifest {manifest_path} target '{target_name}' path_rules.{name}.compare")
        compare_payload = dict(preset_compare or {})
        compare_payload.update(rule_compare or {})
        compare_repo = normalize_compare_side(compare_payload.get("repo"), side="repo", field_name="compare.repo",
                                              default=inherited_compare_repo, context=rule_context)
        compare_live = normalize_compare_side(compare_payload.get("live"), side="live", field_name="compare.live",
                                              default=inherited_compare_live, context=rule_context)
        preset_editor_payload = preset_payload.get("editor")
        rule_editor_payload = payload.get("editor")
        editor_label = f"{target_name} path_rules.{name}"
        base_editor = (
            normalize_editor(
                preset_editor_payload,
                manifest_path=manifest_path,
                target_name=editor_label,
                default_elevation=default_command_elevation,
            )
            if preset_editor_payload is not None
            # The target's sources are inherited, not declared by this rule.
            else replace(inherited_editor or EditorSpec(), additional_sources_explicit=False)
        )
        if rule_editor_payload is None:
            editor = base_editor
        else:
            override_editor = normalize_editor(
                rule_editor_payload,
                manifest_path=manifest_path,
                target_name=editor_label,
                default_elevation=default_command_elevation,
            )
            # Provider metadata is replaced atomically, while omitted source
            # declarations retain each inherited entry's original root.
            editor = _merge_editor_specs(
                base_editor,
                override_editor,
                override_explicit=True,
            )
        chmod = val("chmod", None)
        if chmod is not None and (not isinstance(chmod, str) or _invalid_chmod(chmod)):
            raise ValueError(f"package manifest {manifest_path} target '{target_name}' path_rules.{name}.chmod must be 3 or 4 octal digits")
        sync_policy = normalize_optional_string_enum(payload.get("sync_policy", inherited_sync_policy), context=target_context,
                                                     key=f"{rule_key}.sync_policy", allowed=VALID_SYNC_POLICY_VALUES)
        hooks = normalize_path_rule_hooks(payload.get("hooks"), manifest_path=manifest_path, target_name=target_name, rule_index=name,
                                          default_command_elevation=default_command_elevation)
        rules.append(TargetPathRule(name=name, pattern=normalized_pattern, priority=priority, chmod=chmod,
                                    render=render, capture=capture, compare_repo=compare_repo, compare_live=compare_live,
                                    editor=editor, sync_policy=sync_policy,
                                    additional_sources=editor.additional_sources,
                                    additional_source_entries=editor.source_entries(),
                                    render_explicit=("render" in payload or "render" in preset_payload),
                                    capture_explicit=("capture" in payload or "capture" in preset_payload),
                                    compare_repo_explicit=("repo" in compare_payload),
                                    compare_live_explicit=("live" in compare_payload),
                                    editor_explicit=("editor" in payload or "editor" in preset_payload),
                                    priority_explicit=("priority" in payload or "priority" in preset_payload),
                                    pattern_explicit=("pattern" in payload or "pattern" in preset_payload),
                                    sync_policy_explicit=("sync_policy" in payload or "sync_policy" in preset_payload),
                                    hooks=hooks))
    return tuple(sorted(rules, key=lambda rule: (rule.priority, rule.name)))



def read_target_ignore_table(
    *,
    target_payload: dict[str, Any],
    preset_payload: dict[str, Any],
    manifest_path: Path,
    target_name: str,
) -> dict[str, Any] | None:
    ignore_payload = get_target_value(target_payload=target_payload, preset_payload=preset_payload, key="ignore")
    if ignore_payload is None:
        return None
    if not isinstance(ignore_payload, dict):
        raise ValueError(f"package manifest {manifest_path} target '{target_name}' ignore must be a table")
    validate_supported_keys(
        ignore_payload,
        supported_keys=TARGET_IGNORE_KEYS,
        context=f"package manifest {manifest_path} target '{target_name}' ignore",
    )
    return ignore_payload


def build_target_spec(
    *,
    target_name: str,
    target_payload: dict[str, Any],
    manifest_path: Path,
    default_command_elevation: DefaultCommandElevationMode = "none",
) -> TargetSpec:
    try:
        validate_target_name(target_name)
    except ValueError as exc:
        raise ValueError(f"package manifest {manifest_path}: {exc}") from None
    target_context = f"package manifest {manifest_path} target '{target_name}'"
    # Check the shape before the keys: `[targets] a = "x"` is a wrong-typed
    # target, not a target with an unsupported key "x".
    if not isinstance(target_payload, dict):
        raise ValueError(f"{target_context} must be a table, got {type(target_payload).__name__}")
    validate_supported_keys(target_payload, supported_keys=TARGET_MANIFEST_KEYS, context=target_context)
    preset_payload = resolve_target_preset(target_payload=target_payload, manifest_path=manifest_path, target_name=target_name)
    def value(key: str, default: Any = None) -> Any:
        return target_payload[key] if key in target_payload else preset_payload.get(key, default)
    source = require_optional_string(value("source"), context=target_context, key="source")
    path = require_optional_string(value("path"), context=target_context, key="path")
    probe = normalize_target_command(value("probe"), field_name="probe", manifest_path=manifest_path, target_name=target_name)
    target_type = normalize_target_type(value("type"), context=target_context)
    sync_policy = normalize_sync_policy(value("sync_policy"), context=target_context)
    def boolean_value(field_name: str) -> bool:
        field_value = value(field_name, False)
        if not isinstance(field_value, bool):
            raise ValueError(f"package manifest {manifest_path} target '{target_name}' {field_name} must be a boolean")
        return field_value

    disabled = boolean_value("disabled")
    on_demand = boolean_value("on_demand")
    chmod = value("chmod")
    if chmod is not None and (not isinstance(chmod, str) or _invalid_chmod(chmod)):
        raise ValueError(f"package manifest {manifest_path} target '{target_name}' chmod must be 3 or 4 octal digits")
    render = normalize_projection(value("render"), context=target_context, key="render", default="raw", builtins=("raw", "jinja"))
    capture = normalize_projection(value("capture"), context=target_context, key="capture", default="raw", builtins=("raw", "patch"))
    # Preset comparison sides are independently inherited: an explicit side
    # replaces only that side while the other side remains from the preset.
    preset_compare = preset_payload.get("compare")
    target_compare = target_payload.get("compare")
    if preset_compare is not None and not isinstance(preset_compare, dict):
        raise ValueError(f"package manifest {manifest_path} target '{target_name}' preset compare must be a table")
    if target_compare is not None and not isinstance(target_compare, dict):
        raise ValueError(f"package manifest {manifest_path} target '{target_name}' compare must be a table")
    compare_payload = dict(preset_compare or {})
    compare_payload.update(target_compare or {})
    compare_payload = compare_payload or None
    compare_repo, compare_live = normalize_compare(compare_payload, manifest_path=manifest_path, target_name=target_name)
    preset_editor_payload = preset_payload.get("editor")
    target_editor_payload = target_payload.get("editor")
    if target_editor_payload is None:
        editor = normalize_editor(
            preset_editor_payload,
            manifest_path=manifest_path,
            target_name=target_name,
            default_elevation=default_command_elevation,
        )
    elif isinstance(target_editor_payload, dict) and isinstance(preset_editor_payload, dict):
        # Provider selection is atomic, while Additional Sources are inherited
        # independently when the override omits them.
        merged_editor_payload = dict(target_editor_payload)
        if (
            "additional_sources" not in merged_editor_payload
            and "additional_sources" in preset_editor_payload
        ):
            merged_editor_payload["additional_sources"] = preset_editor_payload["additional_sources"]
        editor = normalize_editor(
            merged_editor_payload,
            manifest_path=manifest_path,
            target_name=target_name,
            default_elevation=default_command_elevation,
        )
    else:
        editor = normalize_editor(
            target_editor_payload,
            manifest_path=manifest_path,
            target_name=target_name,
            default_elevation=default_command_elevation,
        )
    ignore_payload = read_target_ignore_table(target_payload=target_payload, preset_payload=preset_payload,
                                              manifest_path=manifest_path, target_name=target_name)
    patterns = (
        normalize_string_list(ignore_payload.get("patterns"), context=f"{target_context} ignore", key="patterns")
        if ignore_payload is not None
        else None
    )
    ignore_command = normalize_target_command(
        ignore_payload.get("command") if ignore_payload is not None else None,
        field_name="ignore command", manifest_path=manifest_path, target_name=target_name,
    )
    hooks_payload = target_payload.get("hooks")
    hooks = None
    if hooks_payload is not None:
        if not isinstance(hooks_payload, dict):
            raise ValueError(f"package manifest {manifest_path} target '{target_name}' hooks must be a table")
        unknown = sorted(key for key in hooks_payload if key not in {"guard_push", "pre_push", "post_push", "guard_pull", "pre_pull", "post_pull"})
        if unknown:
            raise ValueError(f"package manifest {manifest_path} target '{target_name}' uses unsupported hook names: {', '.join(unknown)}")
        hooks = {name: build_hook_spec(hook_name=name, hook_payload=payload, manifest_path=manifest_path,
                                       owner_label=f"target '{target_name}'",
                                       default_command_elevation=default_command_elevation)
                 for name, payload in hooks_payload.items()}

    path_rules = normalize_target_path_rules(
        value("path_rules"), manifest_path=manifest_path, target_name=target_name,
        default_command_elevation=default_command_elevation, inherited_render=render,
        inherited_capture=capture, inherited_compare_repo=compare_repo, inherited_compare_live=compare_live,
        inherited_editor=editor, inherited_sync_policy=sync_policy)
    if probe is not None:
        if sync_policy == "push-only-delete":
            raise ValueError(
                f"package manifest {manifest_path} target '{target_name}' uses probe and cannot use push-only-delete; use push-only"
            )
        forbidden = sorted(name for name, item in {
            "source": source, "path": path, "type": target_type, "chmod": chmod,
            "render": None if render == "raw" else render, "capture": None if capture == "raw" else capture,
            "compare": compare_payload, "editor": None if editor == EditorSpec() else editor,
            "ignore": patterns if patterns is not None else ignore_command, "path_rules": path_rules or None,
        }.items() if item is not None)
        if forbidden:
            raise ValueError(f"package manifest {manifest_path} target '{target_name}' uses probe and must not define: {', '.join(forbidden)}")
    return TargetSpec(name=target_name, declared_in=manifest_path.parent, source=source, path=path, probe=probe,
                      target_type=target_type, sync_policy=sync_policy, chmod=chmod, render=render, capture=capture,
                      editor=editor, compare_repo=compare_repo, compare_live=compare_live,
                      additional_sources=editor.additional_sources,
                      additional_source_entries=editor.source_entries(),
                      additional_sources_root=editor.additional_sources_root,
                      render_explicit=("render" in target_payload or "render" in preset_payload),
                      capture_explicit=("capture" in target_payload or "capture" in preset_payload),
                      compare_repo_explicit=(isinstance(compare_payload, dict) and "repo" in compare_payload) or isinstance(preset_payload.get("compare"), dict) and "repo" in preset_payload["compare"],
                      compare_live_explicit=(isinstance(compare_payload, dict) and "live" in compare_payload) or isinstance(preset_payload.get("compare"), dict) and "live" in preset_payload["compare"],
                      editor_explicit=("editor" in target_payload or "editor" in preset_payload),
                      ignore_patterns=patterns, ignore_command=ignore_command,
                      path_rules=path_rules, hooks=hooks,
                      disabled=disabled, on_demand=on_demand)

def build_hook_spec(
    *,
    hook_name: str,
    hook_payload: Any,
    manifest_path: Path,
    owner_label: str = "package",
    manifest_kind: str = "package manifest",
    default_command_elevation: DefaultCommandElevationMode = "none",
) -> HookSpec:
    commands_payload = hook_payload
    run_noop = False
    if isinstance(hook_payload, dict) and "run" in hook_payload:
        commands_payload = [hook_payload]
    elif isinstance(hook_payload, dict):
        planning_guard = hook_name.startswith("guard_")
        supported_keys = {"commands"} if planning_guard else {"commands", "run_noop"}
        unknown_keys = sorted(key for key in hook_payload if key not in supported_keys)
        if unknown_keys:
            unknown_text = ", ".join(unknown_keys)
            raise ValueError(
                f"{manifest_kind} {manifest_path} {owner_label} hook '{hook_name}' has unsupported keys: {unknown_text}"
            )
        if "commands" not in hook_payload:
            raise ValueError(
                f"{manifest_kind} {manifest_path} {owner_label} hook '{hook_name}' must define 'commands'"
            )
        commands_payload = hook_payload.get("commands")
        run_noop_value = hook_payload.get("run_noop", False)
        if not isinstance(run_noop_value, bool):
            raise ValueError(
                f"{manifest_kind} {manifest_path} {owner_label} hook '{hook_name}' run_noop must be a boolean"
            )
        run_noop = run_noop_value
    commands = normalize_hook_command_specs(
        commands_payload,
        manifest_kind=manifest_kind,
        manifest_path=manifest_path,
        owner_label=owner_label,
        hook_name=hook_name,
        default_command_elevation=default_command_elevation,
    )
    return HookSpec(
        name=hook_name,
        commands=commands,
        declared_in=manifest_path.parent,
        run_noop=run_noop,
    )


def merge_ignore_patterns(*pattern_sets: tuple[str, ...]) -> tuple[str, ...]:
    """Compose ignore layers without changing gitignore rule ordering.

    Repeated patterns are meaningful: an exclusion can be followed by a
    negation and then reinstated by the same exclusion text.
    """
    return tuple(pattern for pattern_set in pattern_sets for pattern in pattern_set)


def normalize_gitignore_enabled(value: Any, *, context: str) -> bool | None:
    """Absent package enablement inherits; an explicit boolean overrides."""
    if value is not None and not isinstance(value, bool):
        raise ValueError(f"{context} gitignore must be a boolean, got {type(value).__name__}")
    return value


def flatten_vars(data: dict[str, Any], prefix: str = "") -> dict[str, str]:
    flattened: dict[str, str] = {}
    for key, value in data.items():
        flat_key = f"{prefix}__{key}" if prefix else key
        if isinstance(value, dict):
            flattened.update(flatten_vars(value, flat_key))
        else:
            flattened[flat_key] = str(value)
    return flattened


PROFILE_NAME_SEGMENT_SEPARATORS = re.compile(r"[/\-_.]")
# Checked in order: a lineage naming both OSes infers darwin, as before.
PROFILE_OS_SEGMENTS = (
    ("darwin", frozenset({"mac", "macos", "darwin"})),
    ("linux", frozenset({"linux", "arch", "archlinux"})),
)


def infer_profile_os(profile_id: str, lineage: list[str], variables: dict[str, Any]) -> str:
    explicit = variables.get("os")
    if isinstance(explicit, str):
        return explicit
    # Match whole name segments so words like "machete" or "research" don't
    # pick an OS; "os/mac", "home-linux" and "arch" still do.
    segments = {
        segment
        for name in (profile_id, *lineage)
        for segment in PROFILE_NAME_SEGMENT_SEPARATORS.split(name.lower())
    }
    for inferred_os, os_segments in PROFILE_OS_SEGMENTS:
        if segments & os_segments:
            return inferred_os
    return sys.platform


def strip_package_extensions(package: PackageSpec) -> PackageSpec:
    return replace(package, extends=None)


def _merge_editor_specs(base: EditorSpec, override: EditorSpec, *, override_explicit: bool) -> EditorSpec:
    # Provider, I/O, and elevation are one atomic Editor selection. Source
    # declarations are independent list configuration: each side is taken from
    # the override only if the override declared it, so a sources-only
    # declaration survives an override that selects no editor.
    selection = override if override_explicit else base
    sources = override if override.additional_sources_explicit else base
    return replace(
        selection.with_sources_of(sources),
        additional_sources_explicit=(
            override.additional_sources_explicit or base.additional_sources_explicit
        ),
    )


def merge_path_rule_specs(base: TargetPathRule, override: TargetPathRule) -> TargetPathRule:
    editor = _merge_editor_specs(base.editor, override.editor, override_explicit=override.editor_explicit)
    hooks = dict(base.hooks or {})
    hooks.update(override.hooks or {})
    return TargetPathRule(
        name=override.name, pattern=override.pattern if override.pattern_explicit else base.pattern,
        priority=override.priority if override.priority_explicit else base.priority,
        chmod=override.chmod if override.chmod is not None else base.chmod,
        render=override.render if override.render_explicit else base.render,
        capture=override.capture if override.capture_explicit else base.capture,
        compare_repo=override.compare_repo if override.compare_repo_explicit else base.compare_repo,
        compare_live=override.compare_live if override.compare_live_explicit else base.compare_live,
        editor=editor, sync_policy=override.sync_policy if override.sync_policy_explicit else base.sync_policy,
        additional_sources=editor.additional_sources,
        additional_source_entries=editor.source_entries(),
        render_explicit=base.render_explicit or override.render_explicit,
        capture_explicit=base.capture_explicit or override.capture_explicit,
        compare_repo_explicit=base.compare_repo_explicit or override.compare_repo_explicit,
        compare_live_explicit=base.compare_live_explicit or override.compare_live_explicit,
        editor_explicit=base.editor_explicit or override.editor_explicit,
        priority_explicit=base.priority_explicit or override.priority_explicit,
        pattern_explicit=base.pattern_explicit or override.pattern_explicit,
        sync_policy_explicit=base.sync_policy_explicit or override.sync_policy_explicit,
        hooks=hooks,
    )


def merge_target_specs(base: TargetSpec, override: TargetSpec) -> TargetSpec:
    hooks = dict(base.hooks or {})
    hooks.update(override.hooks or {})
    base_rules = {rule.name: rule for rule in base.path_rules}
    for rule in override.path_rules:
        base_rules[rule.name] = merge_path_rule_specs(base_rules[rule.name], rule) if rule.name in base_rules else rule
    editor = _merge_editor_specs(base.editor, override.editor, override_explicit=override.editor_explicit)
    return TargetSpec(
        name=override.name, declared_in=override.declared_in,
        source=override.source if override.source is not None else base.source,
        path=override.path if override.path is not None else base.path,
        probe=override.probe if override.probe is not None else base.probe,
        target_type=override.target_type if override.target_type is not None else base.target_type,
        sync_policy=override.sync_policy if override.sync_policy is not None else base.sync_policy,
        chmod=override.chmod if override.chmod is not None else base.chmod,
        render=override.render if override.render_explicit else base.render,
        capture=override.capture if override.capture_explicit else base.capture,
        editor=editor,
        compare_repo=override.compare_repo if override.compare_repo_explicit else base.compare_repo,
        compare_live=override.compare_live if override.compare_live_explicit else base.compare_live,
        render_explicit=base.render_explicit or override.render_explicit,
        capture_explicit=base.capture_explicit or override.capture_explicit,
        compare_repo_explicit=base.compare_repo_explicit or override.compare_repo_explicit,
        compare_live_explicit=base.compare_live_explicit or override.compare_live_explicit,
        editor_explicit=base.editor_explicit or override.editor_explicit,
        additional_sources=editor.additional_sources,
        additional_source_entries=editor.source_entries(),
        additional_sources_root=editor.additional_sources_root,
        ignore_patterns=override.ignore_patterns if override.ignore_patterns is not None else base.ignore_patterns,
        ignore_command=override.ignore_command if override.ignore_command is not None else base.ignore_command,
        path_rules=tuple(sorted(base_rules.values(), key=lambda r: (r.priority, r.name))),
        hooks=hooks, disabled=override.disabled or base.disabled,
        on_demand=override.on_demand or base.on_demand,
    )



def _path_rule_following_target(rule: TargetPathRule, target: TargetSpec) -> TargetPathRule:
    # The rule's editor selection and its source list each follow the target
    # unless the rule declared them. The merge ORs in the target's declaration
    # flag; the rule keeps only its own so a later resolution can tell them apart.
    editor = replace(
        _merge_editor_specs(target.editor, rule.editor, override_explicit=rule.editor_explicit),
        additional_sources_explicit=rule.editor.additional_sources_explicit,
    )
    return replace(
        rule,
        render=rule.render if rule.render_explicit else target.render,
        capture=rule.capture if rule.capture_explicit else target.capture,
        compare_repo=rule.compare_repo if rule.compare_repo_explicit else target.compare_repo,
        compare_live=rule.compare_live if rule.compare_live_explicit else target.compare_live,
        editor=editor,
        additional_sources=editor.additional_sources,
        additional_source_entries=editor.source_entries(),
        sync_policy=rule.sync_policy if rule.sync_policy_explicit else target.sync_policy,
    )


def path_rules_following_targets(package: PackageSpec) -> PackageSpec:
    """Re-copy the target fields each path rule leaves unset.

    Loading copies a target's values into the rule fields its manifest omits.
    `extends`, `remove` and `append` can change the target afterwards, so the
    copies are refreshed once the package is resolved; otherwise readers of
    the copies see a parent's stale value.
    """
    if not package.targets:
        return package
    return replace(package, targets={
        name: replace(target, path_rules=tuple(_path_rule_following_target(rule, target) for rule in target.path_rules))
        for name, target in package.targets.items()
    })


def validate_patch_capture(owner: str, *, render: str, capture: str, compare_repo: str, compare_live: str) -> None:
    if capture != "patch":
        return
    if render == "raw":
        raise ValueError(f'{owner} capture = "patch" requires non-raw render')
    if compare_repo != "render" or compare_live != "raw":
        raise ValueError(f'{owner} capture = "patch" requires compare.repo = "render" and compare.live = "raw"')


def _validate_resolved_package(package: PackageSpec, *, repo_name: str) -> PackageSpec:
    for target in (package.targets or {}).values():
        target_label = repo_qualified_target_text(repo_name=repo_name, package_id=package.id, target_name=target.name)
        for rule in target.path_rules:
            if not rule.pattern.strip():
                raise ValueError(f'target "{target_label}" path rule \'{rule.name}\' must define pattern')
        # Extends can split a valid combination across manifests, so only the
        # merged target is checked. Path Rules refine children, so planning
        # checks them per child once overlapping rules are folded together.
        validate_patch_capture(f'target "{target_label}"', render=target.render,
                               capture=target.capture, compare_repo=target.compare_repo,
                               compare_live=target.compare_live)
    return package


def merge_package_specs(base: PackageSpec, override: PackageSpec) -> PackageSpec:
    targets = dict(base.targets or {})
    for name, target in (override.targets or {}).items():
        targets[name] = merge_target_specs(targets[name], target) if name in targets else target

    hooks = dict(base.hooks or {})
    hooks.update(override.hooks or {})

    return PackageSpec(
        id=override.id,
        package_root=override.package_root,
        description=override.description if override.description is not None else base.description,
        binding_mode=override.binding_mode if override.binding_mode_explicit else base.binding_mode,
        sync_policy=override.sync_policy if override.sync_policy is not None else base.sync_policy,
        depends=override.depends if override.depends is not None else base.depends,
        extends=None,
        reserved_paths=override.reserved_paths if override.reserved_paths is not None else base.reserved_paths,
        ignore_patterns=override.ignore_patterns if override.ignore_patterns is not None else base.ignore_patterns,
        gitignore_enabled=override.gitignore_enabled if override.gitignore_enabled is not None else base.gitignore_enabled,
        vars=deep_merge(base.vars or {}, override.vars or {}),
        targets=targets,
        hooks=hooks,
        remove=override.remove if override.remove is not None else base.remove,
        append=deep_merge(base.append or {}, override.append or {}),
        binding_mode_explicit=base.binding_mode_explicit or override.binding_mode_explicit,
    )
