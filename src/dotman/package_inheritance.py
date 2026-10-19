"""Package inheritance `remove` and `append`, addressed by manifest keys.

Paths name what users write in `package.toml` (`targets.t.type`,
`targets.t.compare.live`, `targets.t.path_rules.a`), so this module maps each
manifest key onto the resolved model fields it fills.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal

from dotman.manifest import (
    build_target_spec,
    normalize_additional_sources,
    normalize_hook_command_specs,
    normalize_target_path_rules,
)
from dotman.models import (
    AdditionalSource,
    DefaultCommandElevationMode,
    EditorSpec,
    HookSpec,
    PackageSpec,
    TargetPathRule,
    TargetSpec,
)

AppendKind = Literal["strings", "commands", "sources"]


@dataclass(frozen=True)
class ManifestField:
    """One manifest key: the model fields it fills and how longer paths and append treat it."""

    model_fields: tuple[str, ...]
    # Longer paths walk into this model field (a keyed map, editor, or path rules).
    nested: bool = False
    # Path rules are a keyed table in the manifest but a sorted tuple in the model.
    keyed_rules: bool = False
    append: AppendKind | None = None

    @property
    def model_field(self) -> str:
        return self.model_fields[0]


# A dict value is a manifest sub-table such as `compare` or `ignore`; naming the
# table itself addresses every field below it.
ManifestTable = dict[str, "ManifestField | ManifestTable"]


def _field(*model_fields: str, append: AppendKind | None = None) -> ManifestField:
    return ManifestField(model_fields, append=append)


def _nested(model_field: str, *, keyed_rules: bool = False) -> ManifestField:
    return ManifestField((model_field,), nested=True, keyed_rules=keyed_rules)


# Projection fields carry an explicit flag so extends can tell "unset" from a
# value equal to the default; resetting a field resets its flag too.
_COMPARE_TABLE: ManifestTable = {
    "repo": _field("compare_repo", "compare_repo_explicit"),
    "live": _field("compare_live", "compare_live_explicit"),
}
_EDITOR_FIELD = ManifestField(("editor", "editor_explicit"), nested=True)

MANIFEST_FIELDS: dict[type, ManifestTable] = {
    PackageSpec: {
        "description": _field("description"),
        "binding_mode": _field("binding_mode", "binding_mode_explicit"),
        "sync_policy": _field("sync_policy"),
        "depends": _field("depends", append="strings"),
        "reserved_paths": _field("reserved_paths", append="strings"),
        "ignore": {
            "patterns": _field("ignore_patterns", append="strings"),
            "gitignore": _field("gitignore_enabled"),
        },
        "vars": _nested("vars"),
        "targets": _nested("targets"),
        "hooks": _nested("hooks"),
    },
    TargetSpec: {
        "source": _field("source"),
        "path": _field("path"),
        "probe": _field("probe"),
        "type": _field("target_type"),
        "sync_policy": _field("sync_policy"),
        "chmod": _field("chmod"),
        "render": _field("render", "render_explicit"),
        "capture": _field("capture", "capture_explicit"),
        "compare": _COMPARE_TABLE,
        "editor": _EDITOR_FIELD,
        "ignore": {
            "patterns": _field("ignore_patterns", append="strings"),
            "command": _field("ignore_command"),
        },
        "path_rules": _nested("path_rules", keyed_rules=True),
        "hooks": _nested("hooks"),
        "disabled": _field("disabled"),
        "on_demand": _field("on_demand"),
    },
    TargetPathRule: {
        "pattern": _field("pattern", "pattern_explicit"),
        "priority": _field("priority", "priority_explicit"),
        "chmod": _field("chmod"),
        "render": _field("render", "render_explicit"),
        "capture": _field("capture", "capture_explicit"),
        "compare": _COMPARE_TABLE,
        "editor": _EDITOR_FIELD,
        "sync_policy": _field("sync_policy", "sync_policy_explicit"),
        "hooks": _nested("hooks"),
    },
    # Provider, I/O and elevation form one atomic editor selection, so only the
    # independent source list is addressable on its own.
    EditorSpec: {
        "additional_sources": _field(
            "additional_sources",
            "additional_source_entries",
            "additional_sources_root",
            "additional_sources_explicit",
            append="sources",
        ),
    },
    HookSpec: {
        "commands": _field("commands", append="commands"),
        "run_noop": _field("run_noop"),
    },
}


@dataclass(frozen=True)
class _PatchContext:
    package_root: Path
    default_command_elevation: DefaultCommandElevationMode

    @property
    def manifest_path(self) -> Path:
        return self.package_root / "package.toml"


def _unknown_path(path: str) -> ValueError:
    return ValueError(f"package inheritance path '{path}' does not name a manifest field that remove or append can change")


def _split_path(path: str) -> tuple[str, ...]:
    parts = tuple(path.split("."))
    if any(not part for part in parts):
        raise ValueError(f"invalid package inheritance path '{path}'")
    return parts


def _sync_editor_mirrors(owner: Any) -> Any:
    # Targets and path rules mirror their editor's sources for plan/display
    # consumers; keep those copies in step with the editor.
    editor: EditorSpec = owner.editor
    mirrors: dict[str, Any] = {
        "additional_sources": editor.additional_sources,
        "additional_source_entries": editor.source_entries(),
    }
    if isinstance(owner, TargetSpec):
        mirrors["additional_sources_root"] = editor.additional_sources_root
    return replace(owner, **mirrors)


def _unset_spec(owner: Any, parent: Any, context: _PatchContext) -> Any:
    """The spec the loader builds when a manifest sets nothing on this object."""
    if isinstance(owner, PackageSpec):
        return PackageSpec(id=owner.id, package_root=owner.package_root)
    if isinstance(owner, TargetSpec):
        return build_target_spec(
            target_name=owner.name,
            target_payload={},
            manifest_path=context.manifest_path,
            default_command_elevation=context.default_command_elevation,
        )
    if isinstance(owner, TargetPathRule):
        # An unset rule field falls back to its target's value, as when loading.
        (rule,) = normalize_target_path_rules(
            {owner.name: {}},
            manifest_path=context.manifest_path,
            target_name=parent.name,
            default_command_elevation=context.default_command_elevation,
            inherited_render=parent.render,
            inherited_capture=parent.capture,
            inherited_compare_repo=parent.compare_repo,
            inherited_compare_live=parent.compare_live,
            inherited_editor=parent.editor,
            inherited_sync_policy=parent.sync_policy,
        )
        return rule
    if isinstance(owner, EditorSpec):
        return EditorSpec()
    if isinstance(owner, HookSpec):
        return HookSpec(name=owner.name, commands=(), declared_in=owner.declared_in)
    raise TypeError(f"no manifest defaults for {type(owner).__name__}")


class _Remove:
    def __init__(self, context: _PatchContext) -> None:
        self.context = context

    def field(self, owner: Any, manifest_field: ManifestField, parent: Any, path: str) -> Any:
        unset = _unset_spec(owner, parent, self.context)
        return replace(owner, **{name: getattr(unset, name) for name in manifest_field.model_fields})

    def table(self, owner: Any, table: ManifestTable, parent: Any, path: str) -> Any:
        for entry in table.values():
            owner = self.table(owner, entry, parent, path) if isinstance(entry, dict) else self.field(owner, entry, parent, path)
        return owner

    def entry(self, mapping: dict[str, Any], key: str, path: str) -> dict[str, Any]:
        return {name: value for name, value in mapping.items() if name != key}

    def missing(self, value: Any, path: str) -> Any:
        return value


class _Append:
    def __init__(self, context: _PatchContext, values: Any) -> None:
        self.context = context
        self.values = values

    def field(self, owner: Any, manifest_field: ManifestField, parent: Any, path: str) -> Any:
        current = getattr(owner, manifest_field.model_field)
        if manifest_field.append == "commands":
            return self._append_hook_commands(owner, path)
        if manifest_field.append == "sources":
            return self._append_additional_sources(owner, path)
        if manifest_field.append == "strings" and isinstance(current, tuple):
            values = self._require_list(path)
            if not all(isinstance(item, str) for item in values):
                raise ValueError(f"append target '{path}' must contain only strings")
            return replace(owner, **{manifest_field.model_field: (*current, *values)})
        raise ValueError(f"append target '{path}' is not a list")

    def table(self, owner: Any, table: ManifestTable, parent: Any, path: str) -> Any:
        raise ValueError(f"append target '{path}' is not a list")

    def entry(self, mapping: dict[str, Any], key: str, path: str) -> dict[str, Any]:
        current = mapping[key]
        if isinstance(current, HookSpec):
            return {**mapping, key: self._append_hook_commands(current, path)}
        if isinstance(current, list):
            return {**mapping, key: [*current, *self._require_list(path)]}
        raise ValueError(f"append target '{path}' is not a list")

    def missing(self, value: Any, path: str) -> Any:
        raise ValueError(f"append target '{path}' does not exist")

    def _require_list(self, path: str) -> list[Any]:
        if not isinstance(self.values, list):
            raise ValueError(f"append target '{path}' must receive a list")
        return self.values

    def _append_hook_commands(self, hook: HookSpec, path: str) -> HookSpec:
        commands = normalize_hook_command_specs(
            self.values,
            manifest_kind="package manifest",
            manifest_path=self.context.manifest_path,
            owner_label=f"package hook '{hook.name}'",
            hook_name=hook.name,
            default_command_elevation=self.context.default_command_elevation,
        )
        return replace(hook, commands=(*hook.commands, *commands), declared_in=self.context.package_root)

    def _append_additional_sources(self, editor: EditorSpec, path: str) -> EditorSpec:
        # Additional Sources are the one list whose entries carry declaration
        # provenance: only the appended entries anchor at the child package root.
        appended = normalize_additional_sources(self.values, manifest_path=self.context.manifest_path, target_name=path)
        entries: list[AdditionalSource] = []
        for entry in (*editor.source_entries(), *(AdditionalSource(item, self.context.package_root) for item in appended)):
            if entry not in entries:
                entries.append(entry)
        return replace(
            editor,
            additional_sources=tuple(entry.path for entry in entries),
            additional_source_entries=tuple(entries),
            additional_sources_root=None,
            additional_sources_explicit=True,
        )


_Operation = _Remove | _Append


def _patch_value(value: Any, parts: tuple[str, ...], operation: _Operation, *, parent: Any, path: str) -> Any:
    if type(value) in MANIFEST_FIELDS:
        return _patch_table(value, MANIFEST_FIELDS[type(value)], parts, operation, parent=parent, path=path)
    if not isinstance(value, dict):
        raise _unknown_path(path)
    key, rest = parts[0], parts[1:]
    if key not in value:
        return operation.missing(value, path)
    if not rest:
        return operation.entry(value, key, path)
    return {**value, key: _patch_value(value[key], rest, operation, parent=parent, path=path)}


def _patch_table(
    owner: Any, table: ManifestTable, parts: tuple[str, ...], operation: _Operation, *, parent: Any, path: str
) -> Any:
    key, rest = parts[0], parts[1:]
    entry = table.get(key)
    if entry is None:
        raise _unknown_path(path)
    if isinstance(entry, dict):
        if not rest:
            return operation.table(owner, entry, parent, path)
        return _patch_table(owner, entry, rest, operation, parent=parent, path=path)
    if not rest:
        patched = operation.field(owner, entry, parent, path)
    elif not entry.nested:
        raise _unknown_path(path)
    else:
        child = getattr(owner, entry.model_field)
        if child is None:
            return operation.missing(owner, path)
        if entry.keyed_rules:
            child = {rule.name: rule for rule in child}
        patched_child = _patch_value(child, rest, operation, parent=owner, path=path)
        if entry.keyed_rules:
            patched_child = tuple(sorted(patched_child.values(), key=lambda rule: (rule.priority, rule.name)))
        patched = replace(owner, **{entry.model_field: patched_child})
    return _sync_editor_mirrors(patched) if entry is _EDITOR_FIELD else patched


def _iter_append_paths(payload: dict[str, Any], prefix: tuple[str, ...] = ()):
    for key, value in payload.items():
        current_path = (*prefix, key)
        if isinstance(value, dict):
            yield from _iter_append_paths(value, current_path)
        else:
            yield ".".join(current_path), value


def patch_remove_and_append(
    package: PackageSpec,
    remove_paths: tuple[str, ...],
    append_payload: dict[str, Any],
    *,
    default_command_elevation: DefaultCommandElevationMode = "none",
) -> PackageSpec:
    context = _PatchContext(package_root=package.package_root, default_command_elevation=default_command_elevation)
    patched = package
    for path in remove_paths:
        patched = _patch_value(patched, _split_path(path), _Remove(context), parent=None, path=path)
    for path, values in _iter_append_paths(append_payload):
        patched = _patch_value(patched, _split_path(path), _Append(context, values), parent=None, path=path)
    return patched
