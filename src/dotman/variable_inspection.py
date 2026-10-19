from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotman.errors import UsageError
from dotman.manifest import deep_merge, infer_profile_os
from dotman.models import FullSpecSelector
from dotman.repository import Repository
from dotman.templates import build_template_context
from dotman.tracking import TrackedStateContext, iter_tracked_package_entries


@dataclass(frozen=True)
class VariableProvenance:
    source_kind: str
    source_label: str
    source_path: Path

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_kind": self.source_kind,
            "source_label": self.source_label,
            "source_path": str(self.source_path),
        }


@dataclass(frozen=True)
class ResolvedVariableOccurrence:
    repo: str
    selector: str
    profile: str
    selector_kind: str
    variable: str
    value: Any
    provenance: VariableProvenance

    def to_dict(self) -> dict[str, Any]:
        return {
            "repo": self.repo,
            "selector": self.selector,
            "profile": self.profile,
            "selector_kind": self.selector_kind,
            "variable": self.variable,
            "value": self.value,
            "provenance": self.provenance.to_dict(),
        }


@dataclass(frozen=True)
class ResolvedVariableDetail:
    variable: str
    occurrences: list[ResolvedVariableOccurrence]

    def to_dict(self) -> dict[str, Any]:
        return {
            "variable": self.variable,
            "occurrences": [occurrence.to_dict() for occurrence in self.occurrences],
        }



def _flatten_mapping(data: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    flattened: dict[str, Any] = {}
    for key, value in data.items():
        flat_key = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            flattened.update(_flatten_mapping(value, flat_key))
        else:
            flattened[flat_key] = value
    return flattened



def _normalize_variable_query(variable_text: str) -> str:
    query = variable_text.strip()
    if query.startswith("vars."):
        return query.removeprefix("vars.")
    return query



def _binding_variable_layers(
    *,
    repo: Repository,
    package_ids: list[str],
    binding_profile: str,
) -> tuple[list[tuple[str, str, Path, dict[str, Any]]], dict[str, Any], list[str]]:
    layers: list[tuple[str, str, Path, dict[str, Any]]] = []
    merged_variables: dict[str, Any] = {}

    for package_id in package_ids:
        package = repo.resolve_package(package_id)
        package_vars = package.vars or {}
        layers.append(("package", package.id, package.package_root / "package.toml", _flatten_mapping(package_vars)))
        merged_variables = deep_merge(merged_variables, package_vars)

    profile_vars, lineage = repo.compose_profile(binding_profile)
    for profile_id in lineage:
        profile = repo.profiles[profile_id]
        layers.append(("profile", profile.id, profile.path, _flatten_mapping(profile.vars)))
    merged_variables = deep_merge(merged_variables, profile_vars)

    layers.append(("local", "repo local override", repo.config.local_override_path, _flatten_mapping(repo.local_vars)))
    merged_variables = deep_merge(merged_variables, repo.local_vars)
    return layers, merged_variables, lineage



def _build_resolved_variable_occurrences_for_binding(
    repo: Repository,
    binding: FullSpecSelector,
    selector_kind: str,
    package_ids: list[str],
) -> list[ResolvedVariableOccurrence]:
    layers, merged_variables, lineage = _binding_variable_layers(
        repo=repo,
        package_ids=package_ids,
        binding_profile=binding.profile,
    )
    inferred_os = infer_profile_os(binding.profile, lineage, merged_variables)
    resolved_context = build_template_context(merged_variables, profile=binding.profile, inferred_os=inferred_os)
    resolved_vars = resolved_context["vars"]
    resolved_flat = _flatten_mapping(resolved_vars)

    occurrences: list[ResolvedVariableOccurrence] = []
    for variable_name, resolved_value in sorted(resolved_flat.items()):
        provenance_layer = next((layer for layer in reversed(layers) if variable_name in layer[3]), None)
        if provenance_layer is None:
            continue
        source_kind, source_name, source_path, _layer_vars = provenance_layer
        occurrences.append(
            ResolvedVariableOccurrence(
                repo=repo.config.name,
                selector=binding.selector,
                profile=binding.profile,
                selector_kind=selector_kind,
                variable=variable_name,
                value=resolved_value,
                provenance=VariableProvenance(
                    source_kind=source_kind,
                    source_label=source_name,
                    source_path=source_path,
                ),
            )
        )
    return occurrences



def list_resolved_variables(context: TrackedStateContext) -> list[ResolvedVariableOccurrence]:
    occurrences: list[ResolvedVariableOccurrence] = []
    for repo, binding, selector_kind, package_ids in iter_tracked_package_entries(context):
        occurrences.extend(
            _build_resolved_variable_occurrences_for_binding(
                repo,
                binding,
                selector_kind,
                package_ids,
            )
        )
    return sorted(occurrences, key=lambda item: (item.variable, item.repo, item.selector, item.profile))


def list_winning_variables(context: TrackedStateContext) -> list[ResolvedVariableOccurrence]:
    winning_by_key: dict[tuple[str, str], ResolvedVariableOccurrence] = {}
    seen_keys: set[tuple[str, str]] = set()
    for repo, binding, selector_kind, package_ids in iter_tracked_package_entries(context):
        for occurrence in _build_resolved_variable_occurrences_for_binding(
            repo,
            binding,
            selector_kind,
            package_ids,
        ):
            key = (occurrence.repo, occurrence.variable)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            winning_by_key[key] = occurrence
    return sorted(winning_by_key.values(), key=lambda item: (item.variable, item.repo, item.selector, item.profile))



def find_variable_matches(context: TrackedStateContext, variable_text: str) -> tuple[list[str], list[str]]:
    query = _normalize_variable_query(variable_text)
    variable_names = sorted({occurrence.variable for occurrence in list_resolved_variables(context)})
    exact_matches = [name for name in variable_names if name == query]
    partial_matches = [name for name in variable_names if query in name and name not in exact_matches]
    return exact_matches, partial_matches



def describe_resolved_variable(context: TrackedStateContext, variable_text: str) -> ResolvedVariableDetail:
    query = _normalize_variable_query(variable_text)
    occurrences = [occurrence for occurrence in list_resolved_variables(context) if occurrence.variable == query]
    if not occurrences:
        raise UsageError(f"variable '{query}' did not match any resolved variable")
    return ResolvedVariableDetail(variable=query, occurrences=occurrences)
