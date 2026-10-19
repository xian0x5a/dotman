from __future__ import annotations

import unicodedata
from dataclasses import dataclass, replace
from pathlib import PurePosixPath
from typing import Sequence

from dotman import planning, tracking
from dotman.models import (
    package_ref_text,
    ResolvedPackageIdentity,
    ResolvedPackageSelection,
    ResolvedSyncScope,
    ResolvedSyncTarget,
    target_separator_index,
)
from dotman.package_resolution import parse_package_ref_text
from dotman.collisions import (
    resolve_tracked_target_winners,
    validate_reserved_path_claims,
    validate_target_collisions,
)
from dotman.config import expand_path
from dotman.projection import rendered_target_is_directory, target_claim
from dotman.templates import render_template_string


def _instance_ref(item: planning.PackagePlanningInput) -> str:
    identity = item.selection.identity
    return package_ref_text(package_id=identity.package_id, bound_profile=identity.bound_profile)


def _validate_reserved_paths(
    planning_inputs: list[planning.PackagePlanningInput],
    winning_target_keys: set[tuple[str, str, str | None, str]],
) -> None:
    """Reject winning Push targets or reservations inside another package instance's reserved paths."""
    for repo_name in dict.fromkeys(item.repo.config.name for item in planning_inputs):
        repo_inputs = [item for item in planning_inputs if item.repo.config.name == repo_name]
        # Claims are owned by package instances, so two instances of one
        # `multi_instance` package are checked against each other.
        target_claims = [
            target_claim(metadata)
            for item in repo_inputs
            for metadata in item.target_metadata
            if planning.target_claims_path(metadata.target)
            and (repo_name, item.selection.identity.package_id, item.selection.identity.bound_profile,
                 metadata.target_name) in winning_target_keys
        ]
        reserved_claims = []
        for item in repo_inputs:
            package = item.repo.resolve_package(item.selection.identity.package_id)
            for reserved_path in package.reserved_paths or ():
                rendered_path = render_template_string(
                    reserved_path, item.package_context.context,
                    base_dir=package.package_root, source_path=package.package_root,
                )
                reserved_claims.append((_instance_ref(item), expand_path(rendered_path, dereference=False)))
        validate_reserved_path_claims(repo_name=repo_name, target_claims=target_claims, reserved_claims=reserved_claims)


@dataclass(frozen=True)
class _ScopeSelector:
    text: str
    repo: str
    package_id: str
    bound_profile: str | None
    target_name: str | None
    child_path: str | None


def split_scope_child_path(text: str) -> tuple[str, str | None]:
    """Split ``[repo:]package.target/child`` into its target part and child path.

    Package IDs may contain ``/``, so only a slash after the target separator
    starts a directory-child path.
    """
    package_text = text.partition(":")[2] if ":" in text else text
    separator = target_separator_index(package_text, text)
    child_start = package_text.find("/", separator) if separator >= 0 else -1
    if child_start < 0:
        return text, None
    split_index = len(text) - len(package_text) + child_start
    return text[:split_index], text[split_index + 1 :]


def _parse_scope_selector(text: str) -> _ScopeSelector:
    if not isinstance(text, str) or not text.strip():
        raise ValueError("sync scope selector must not be empty")
    if text != text.strip():
        raise ValueError(f"sync scope '{text}' is not canonical")
    if ":" not in text:
        raise ValueError(f"sync scope '{text}' is not canonical; expected repo:package")
    repo, remainder = text.split(":", 1)
    if not repo or not remainder or ":" in repo:
        raise ValueError(f"sync scope '{text}' is not canonical; expected repo:package")
    target_name: str | None = None
    child_path: str | None = None
    package_ref = remainder
    if "." in remainder:
        separator = target_separator_index(remainder, text)
        if separator >= 0:
            if separator == len(remainder) - 1:
                raise ValueError(f"sync scope '{text}' is not canonical")
            package_ref = remainder[:separator]
            target_name = remainder[separator + 1 :]
        if target_name is not None:
            if "/" in target_name:
                target_name, child_path = target_name.split("/", 1)
                if not target_name or not child_path or "." in target_name:
                    raise ValueError(f"sync scope '{text}' is not canonical")
                path = PurePosixPath(child_path)
                if (
                    path.is_absolute()
                    or path.as_posix() != child_path
                    or any(part in {"", ".", ".."} for part in path.parts)
                    or "\\" in child_path
                ):
                    raise ValueError(f"sync scope '{text}' child path must be normalized POSIX")
            elif "." in target_name:
                raise ValueError(f"sync scope '{text}' is not canonical")
    try:
        parsed_repo, package_id, bound_profile = parse_package_ref_text(f"{repo}:{package_ref}")
    except ValueError as exc:
        raise ValueError(f"sync scope '{text}' is not canonical") from exc
    if parsed_repo != repo:
        raise ValueError(f"sync scope '{text}' is not canonical")
    return _ScopeSelector(text, repo, package_id, bound_profile, target_name, child_path)


def _tracked_selections(context) -> list[ResolvedPackageSelection]:
    entries = tracking.effective_tracked_package_entries_by_repo(context.tracked_state)
    tracked_entries = tracking.tracked_entries_by_repo_from_bindings(entries)
    selections = planning.resolve_tracked_package_selections(
        context,
        entries_by_repo=tracked_entries,
    )
    return selections


def _matching_selection(
    selector: _ScopeSelector,
    *,
    repo,
    selections: list[ResolvedPackageSelection],
) -> ResolvedPackageSelection:
    if selector.package_id in repo.groups and selector.package_id not in repo.packages:
        raise ValueError(f"sync scope '{selector.text}' names group '{selector.package_id}'; groups are not tracked identities")
    candidates = [
        selection
        for selection in selections
        if selection.identity.repo == selector.repo
        and selection.identity.package_id == selector.package_id
        and selection.identity.bound_profile == selector.bound_profile
    ]
    if selector.bound_profile is None:
        candidates = [
            selection
            for selection in candidates
            if repo.package_binding_mode(selection.package_id) != "multi_instance"
        ]
    if not candidates:
        raise ValueError(f"sync scope '{selector.text}' did not match an exact tracked package identity")
    if len(candidates) > 1:
        labels = ", ".join(selection.selection_label for selection in candidates)
        raise ValueError(f"sync scope '{selector.text}' is ambiguous: {labels}")
    return candidates[0]


def _scope_closure(
    context,
    selections: list[ResolvedPackageSelection],
    root: ResolvedPackageSelection,
) -> list[ResolvedPackageSelection]:
    """Return root's dependency closure from the already resolved graph.

    Profile resolution has already selected the winning identity for every
    package before this function runs. Traversing manifests here only tells
    us which graph nodes are related; candidates always come from selections
    so an overridden implicit profile can never be resurrected.
    """
    selections_by_package: dict[tuple[str, str], list[ResolvedPackageSelection]] = {}
    for selection in selections:
        selections_by_package.setdefault(
            (selection.identity.repo, selection.identity.package_id), []
        ).append(selection)

    selected_keys: set[tuple[str, str, str | None, str]] = set()
    queue = [root]
    while queue:
        current = queue.pop(0)
        current_key = (
            current.identity.repo,
            current.identity.package_id,
            current.identity.bound_profile,
            current.requested_profile,
        )
        if current_key in selected_keys:
            continue
        selected_keys.add(current_key)
        repo = context.repositories[current.identity.repo]
        related_ids = planning.resolve_package_ids(
            repo, current.identity.package_id, "package"
        )
        for package_id in related_ids:
            if package_id == current.identity.package_id:
                continue
            candidates = selections_by_package.get((current.identity.repo, package_id), ())
            if not candidates:
                continue
            owner_matches = [
                candidate
                for candidate in candidates
                if candidate.owner_identity == current.identity
            ]
            profile_matches = [
                candidate
                for candidate in candidates
                if candidate.requested_profile == current.requested_profile
                or candidate.identity.bound_profile == current.identity.bound_profile
            ]
            choices = owner_matches or profile_matches or list(candidates)
            # A resolved graph has one winner for singleton identities. For
            # multi-instance dependencies, profile/owner matching keeps the
            # closure on the requesting profile rather than broadening it.
            queue.append(next(iter(choices)))

    return [
        selection
        for selection in selections
        if (
            selection.identity.repo,
            selection.identity.package_id,
            selection.identity.bound_profile,
            selection.requested_profile,
        ) in selected_keys
    ]

def _target_exists(context, selection: ResolvedPackageSelection, target_name: str) -> bool:
    repo = context.repositories[selection.identity.repo]
    package = repo.resolve_package(selection.identity.package_id)
    return target_name in (package.targets or {})


def _target_key(target: ResolvedSyncTarget) -> tuple[str, str, str | None, str]:
    return target.repo, target.package_id, target.bound_profile, target.target_name


def resolve_sync_scope(
    context,
    selectors: Sequence[str] | None = None,
    *,
    include_on_demand: bool = False,
    include_dependencies: bool = True,
) -> ResolvedSyncScope:
    """Resolve selectors to owning tracked targets.

    On-demand targets enter scope only through an exact target selector unless
    ``include_on_demand`` asks for every tracked target, as metadata-only
    inspection does. ``include_dependencies`` expands package inputs to their
    dependency closure.
    """
    raw_selectors = () if selectors is None else ((selectors,) if isinstance(selectors, str) else tuple(selectors))
    parsed = tuple(_parse_scope_selector(text) for text in raw_selectors)
    all_selections = _tracked_selections(context)

    if not parsed:
        selected_selections = list(all_selections)
    else:
        selected_selections = []
        selected_keys: set[tuple[str, str, str | None, str]] = set()
        selected_closures: list[list[ResolvedPackageSelection]] = []
        for item in parsed:
            repo = context.repositories.get(item.repo)
            if repo is None:
                raise ValueError(f"unknown repo '{item.repo}'")
            root = _matching_selection(item, repo=repo, selections=all_selections)
            # Only a package input expands to its dependencies; a target input is the narrowest scope.
            expand = include_dependencies and item.target_name is None
            closure = _scope_closure(context, all_selections, root) if expand else [root]
            selected_closures.append(closure)
            for selection in closure:
                key = (
                    selection.identity.repo,
                    selection.identity.package_id,
                    selection.identity.bound_profile,
                    selection.requested_profile,
                )
                if key not in selected_keys:
                    selected_keys.add(key)
                    selected_selections.append(selection)

    # Static ownership and collision resolution runs against the full tracked graph
    # before narrowing to the requested identities.
    winner_keys_by_operation: dict[str, set[tuple[str, str, str | None, str]]] = {}
    planning_inputs_by_operation: dict[str, tuple[planning.PackagePlanningInput, ...]] = {}
    primary_targets = {}
    primary_metadata = {}
    primary_gitignore = {}
    for operation in ("push", "pull"):
        planning_inputs, candidates = planning.collect_static_target_candidates(
            context, all_selections, operation=operation
        )
        planning_inputs_by_operation[operation] = tuple(planning_inputs)
        winners = resolve_tracked_target_winners(candidates)
        winner_keys_by_operation[operation] = {
            (
                candidate.selection.identity.repo,
                candidate.selection.identity.package_id,
                candidate.selection.identity.bound_profile,
                candidate.target_name,
            )
            for candidate_list in candidates.values()
            for candidate in candidate_list
            if (candidate.plan_index, candidate.target_index) in winners
        }

        # Validate path nesting after ownership has selected the winning target
        # at each live/repository write path.
        winning_target_keys = winner_keys_by_operation[operation]
        claims = []
        for planning_input in planning_inputs:
            for metadata in planning_input.target_metadata:
                if not planning.target_claims_path(metadata.target):
                    winning_target_keys.add((
                        planning_input.selection.identity.repo,
                        metadata.package_id,
                        planning_input.selection.identity.bound_profile,
                        metadata.target_name,
                    ))
                    continue
                metadata_key = (
                    planning_input.selection.identity.repo,
                    planning_input.selection.identity.package_id,
                    planning_input.selection.identity.bound_profile,
                    metadata.target_name,
                )
                if metadata_key not in winning_target_keys:
                    continue
                claims.append(target_claim(metadata))
                primary_targets[metadata_key] = claims[-1]
                primary_metadata[metadata_key] = metadata
                primary_gitignore[(metadata.repo_path, metadata.live_path)] = metadata.gitignore
        validate_target_collisions(claims, operation=operation, gitignore_chains={
            (metadata.repo_path, metadata.live_path): metadata.gitignore
            for planning_input in planning_inputs for metadata in planning_input.target_metadata
        })
        if operation == "push":
            _validate_reserved_paths(planning_inputs, winning_target_keys)

    # Every surviving Sync target can edit its Primary Source, even when policy
    # forbids automatic Pull. Validate their union after directional ownership
    # has removed overridden targets, before narrowing to requested selectors.
    validate_target_collisions(
        list(primary_targets.values()), operation="pull", gitignore_chains=primary_gitignore,
    )

    selected_targets: list[ResolvedSyncTarget] = []
    selected_target_keys: set[tuple[str, str, str | None, str, str | None]] = set()
    # Canonical package and target scope labels: what inputs name, and what only a closure brought in.
    requested_scopes: set[str] = set()
    dependency_roots: dict[str, dict[ResolvedPackageIdentity, None]] = {}
    for item, closure in zip(parsed, selected_closures if parsed else ()):
        repo = context.repositories[item.repo]
        root = _matching_selection(item, repo=repo, selections=all_selections)
        requested_scopes.add(root.identity.canonical)
        if item.target_name is not None:
            if not _target_exists(context, root, item.target_name):
                raise ValueError(f"sync scope '{item.text}' did not match an exact target")
            target = ResolvedSyncTarget(
                repo=item.repo,
                package_id=root.identity.package_id,
                target_name=item.target_name,
                bound_profile=root.identity.bound_profile,
                child_path=item.child_path,
            )
            if not any(_target_key(target) in keys for keys in winner_keys_by_operation.values()):
                raise ValueError(f"sync scope '{item.text}' is not an owning tracked target")
            metadata = primary_metadata.get(_target_key(target))
            if item.child_path is not None and (metadata is None or not rendered_target_is_directory(metadata)):
                raise ValueError(f"sync scope '{item.text}' child identity requires a resolved directory target")
            requested_scopes.add(replace(target, child_path=None).canonical)
            key = (*_target_key(target), target.child_path)
            if key not in selected_target_keys:
                selected_target_keys.add(key)
                selected_targets.append(target)
        else:
            for selection in closure:
                is_dependency = selection.identity != root.identity
                if is_dependency:
                    dependency_roots.setdefault(selection.identity.canonical, {})[root.identity] = None
                package = context.repositories[selection.identity.repo].resolve_package(selection.identity.package_id)
                for target_name, target_spec in (package.targets or {}).items():
                    target = ResolvedSyncTarget(
                        repo=item.repo,
                        package_id=selection.identity.package_id,
                        target_name=target_name,
                        bound_profile=selection.bound_profile,
                    )
                    if not any(_target_key(target) in keys for keys in winner_keys_by_operation.values()):
                        continue
                    if target_spec.on_demand and not include_on_demand:
                        continue
                    if is_dependency:
                        dependency_roots.setdefault(target.canonical, {})[root.identity] = None
                    else:
                        requested_scopes.add(target.canonical)
                    key = (*_target_key(target), None)
                    if key not in selected_target_keys:
                        selected_target_keys.add(key)
                        selected_targets.append(target)

    if not parsed:
        for selection in selected_selections:
            repo = context.repositories[selection.identity.repo]
            package = repo.resolve_package(selection.identity.package_id)
            for target_name, target_spec in (package.targets or {}).items():
                if target_spec.on_demand and not include_on_demand:
                    continue
                target = ResolvedSyncTarget(
                    repo=selection.identity.repo,
                    package_id=selection.identity.package_id,
                    target_name=target_name,
                    bound_profile=selection.identity.bound_profile,
                )
                if not any(_target_key(target) in keys for keys in winner_keys_by_operation.values()):
                    continue
                key = (*_target_key(target), None)
                if key not in selected_target_keys:
                    selected_target_keys.add(key)
                    selected_targets.append(target)

    normalized_selectors = tuple(dict.fromkeys(raw_selectors))
    public_selections = tuple(
        replace(selection, owner_identity=None, owner_selection_label=None)
        for selection in selected_selections
    )
    return ResolvedSyncScope(
        selectors=normalized_selectors,
        package_selections=public_selections,
        targets=tuple(selected_targets),
        # A scope any input names directly is not there because of a dependency.
        included_via={
            scope_label: tuple(roots) for scope_label, roots in dependency_roots.items()
            if scope_label not in requested_scopes
        },
        planning_inputs=planning_inputs_by_operation,
    )


def sync_unit_identity_bytes(identity: ResolvedSyncTarget) -> bytes:
    """Validate the exact file/child identity before using it as a storage key."""
    selector = _parse_scope_selector(identity.canonical)
    if (
        selector.target_name is None
        or (
            selector.repo,
            selector.package_id,
            selector.bound_profile,
            selector.target_name,
            selector.child_path,
        )
        != (
            identity.repo,
            identity.package_id,
            identity.bound_profile,
            identity.target_name,
            identity.child_path,
        )
        # Human output prints identities verbatim, so a control character
        # (NUL, ESC, CR, ...) would reach the terminal raw.
        or any(unicodedata.category(character) == "Cc" for character in identity.canonical)
    ):
        raise ValueError(
            "Sync Base requires a canonical file or directory-child identity"
        )
    return identity.canonical.encode("utf-8")
