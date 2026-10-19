from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from dotman.config import expand_path
from dotman.ignore import GitIgnoreChain, IgnoreMatcher, collect_gitignore_chain
from dotman.models import PackageSpec, ResolvedPackageSelection, TrackedTargetSummary
from dotman.templates import render_template_string


class TrackedTargetConflictError(ValueError):
    def __init__(
        self,
        *,
        live_path: Path,
        precedence: str,
        contenders: list[str],
        candidates: list[TrackedTargetCandidate],
    ) -> None:
        self.live_path = live_path
        self.precedence = precedence
        self.contenders = tuple(contenders)
        self.candidates = tuple(candidates)
        conflict_text = ", ".join(contenders)
        super().__init__(
            f"conflicting {precedence} tracked targets for {live_path}: {conflict_text}"
        )


@dataclass(frozen=True)
class TrackedTargetCandidate:
    plan_index: int
    target_index: int
    live_path: Path
    precedence: int
    precedence_name: str
    selection: ResolvedPackageSelection
    selection_label: str
    package_id: str
    target_name: str
    target_label: str
    target_summary: TrackedTargetSummary | None = None


@dataclass(frozen=True)
class TrackedTargetOverride:
    winner: TrackedTargetCandidate
    overridden: tuple[TrackedTargetCandidate, ...]

def resolve_tracked_target_winners(
    candidates_by_live_path: dict[Path, list[TrackedTargetCandidate]],
) -> set[tuple[int, int]]:
    winner_indexes: set[tuple[int, int]] = set()
    for live_path, candidates in candidates_by_live_path.items():
        candidates_by_instance_target: dict[tuple[str, str, str | None, str], TrackedTargetCandidate] = {}
        for candidate in candidates:
            instance_target_key = (
                candidate.selection.identity.repo,
                candidate.selection.identity.package_id,
                candidate.selection.identity.bound_profile,
                candidate.target_name,
            )
            existing = candidates_by_instance_target.get(instance_target_key)
            if existing is None or (candidate.precedence, -candidate.plan_index, -candidate.target_index) > (
                existing.precedence,
                -existing.plan_index,
                -existing.target_index,
            ):
                candidates_by_instance_target[instance_target_key] = candidate

        deduped_candidates = list(candidates_by_instance_target.values())
        highest_precedence = max(candidate.precedence for candidate in deduped_candidates)
        contenders = [candidate for candidate in deduped_candidates if candidate.precedence == highest_precedence]
        first = contenders[0]
        if len(contenders) > 1:
            raise TrackedTargetConflictError(
                live_path=live_path,
                precedence=first.precedence_name,
                contenders=[
                    f"{candidate.selection_label} -> {candidate.target_label}"
                    for candidate in sorted(contenders, key=lambda item: (item.selection_label, item.target_label))
                ],
                candidates=sorted(
                    contenders,
                    key=lambda item: (
                        item.selection_label,
                        item.target_label,
                    ),
                ),
            )
        winner_indexes.add((first.plan_index, first.target_index))
    return winner_indexes



@dataclass(frozen=True)
class TargetClaim:
    """One path-claiming target, as ownership checks see it."""

    # Package instance (`package` or `package<profile>`) that owns the claim.
    # Only claims from the same instance skip each other's reserved paths.
    owner: str
    # Canonical `repo:package.target` identity, for messages.
    label: str
    repo_path: Path
    live_path: Path
    ignore_patterns: tuple[str, ...]


def validate_target_collisions(
    claims: list[TargetClaim],
    *,
    operation: str,
    gitignore_chains: dict[tuple[Path, Path], GitIgnoreChain | None] | None = None,
) -> None:
    def parent_matcher(repo_path: Path, live_path: Path, relative: str, patterns: tuple[str, ...]) -> IgnoreMatcher:
        chain = (gitignore_chains or {}).get((repo_path, live_path))
        if chain is not None:
            repository_root = repo_path
            for _part in Path(chain.target).parts:
                repository_root = repository_root.parent
            # Static ownership does not census payloads. Read only the control
            # chain leading to this overlapping claim, not unrelated subtrees.
            controls = collect_gitignore_chain(repo_path / relative, repository_root, nested=False)
            chain = GitIgnoreChain(chain.target, controls.controls)
        return IgnoreMatcher.from_patterns(patterns, gitignore=chain)

    write_paths = [
        operation_write_path(repo_path=claim.repo_path, live_path=claim.live_path, operation=operation)
        for claim in claims
    ]
    write_path_texts = [str(path) for path in write_paths]
    for index, claim in enumerate(claims):
        path, path_text = write_paths[index], write_path_texts[index]
        for other_index in range(index + 1, len(claims)):
            other = claims[other_index]
            other_path, other_path_text = write_paths[other_index], write_path_texts[other_index]
            if path_text == other_path_text:
                raise ValueError(
                    f"conflicting target ownership: {claim.label} and {other.label} both map to {path}"
                )
            if _contains_path_text(path_text, other_path_text):
                relative = other_path.relative_to(path).as_posix()
                parent_ignore = parent_matcher(claim.repo_path, claim.live_path, relative, claim.ignore_patterns)
                if not parent_ignore.matches(relative):
                    raise ValueError(f"incompatible nested targets: {claim.label} contains {other.label}")
            elif _contains_path_text(other_path_text, path_text):
                relative = path.relative_to(other_path).as_posix()
                parent_ignore = parent_matcher(other.repo_path, other.live_path, relative, other.ignore_patterns)
                if not parent_ignore.matches(relative):
                    raise ValueError(f"incompatible nested targets: {other.label} contains {claim.label}")


def operation_write_path(*, repo_path: Path, live_path: Path, operation: str) -> Path:
    if operation == "push":
        return live_path
    if operation == "pull":
        return repo_path
    raise ValueError(f"unsupported operation '{operation}'")



def validate_reserved_path_conflicts(
    repo_name: str,
    packages: list[PackageSpec],
    claims: list[TargetClaim],
    context: dict[str, Any],
    *,
    owner_for_package: Callable[[str], str],
) -> None:
    reserved_claims: list[tuple[str, Path]] = []
    for package in packages:
        for reserved_path in package.reserved_paths or ():
            rendered_path = render_template_string(reserved_path, context, base_dir=package.package_root, source_path=package.package_root)
            reserved_claims.append((owner_for_package(package.id), expand_path(rendered_path, dereference=False)))

    validate_reserved_path_claims(repo_name=repo_name, target_claims=claims, reserved_claims=reserved_claims)


def validate_reserved_path_claims(
    *,
    repo_name: str,
    target_claims: list[TargetClaim],
    reserved_claims: list[tuple[str, Path]],
) -> None:
    """Reject claims inside another package instance's reserved paths, within one repo."""
    for owner, reserved_path in reserved_claims:
        for claim in target_claims:
            if owner == claim.owner:
                continue
            if paths_conflict(reserved_path, claim.live_path):
                raise ValueError(
                    f"reserved path conflict: {repo_name}:{owner} reserves {reserved_path} "
                    f"and {claim.label} maps to {claim.live_path}"
                )

    for index, (owner, reserved_path) in enumerate(reserved_claims):
        for other_owner, other_reserved_path in reserved_claims[index + 1 :]:
            if owner == other_owner:
                continue
            if paths_conflict(reserved_path, other_reserved_path):
                raise ValueError(
                    f"reserved path conflict: {repo_name}:{owner} reserves {reserved_path} "
                    f"and {repo_name}:{other_owner} reserves {other_reserved_path}"
                )



def paths_conflict(left: Path, right: Path) -> bool:
    left_text, right_text = str(left), str(right)
    return left_text == right_text or _contains_path_text(left_text, right_text) or _contains_path_text(right_text, left_text)


def _contains_path_text(ancestor: str, descendant: str) -> bool:
    # Collision checks compare every target pair; `Path.parents` builds a Path
    # per level and dominated sync startup. Rendered paths are absolute and
    # normalized, so ancestry is a separator-bounded string prefix.
    prefix = ancestor if ancestor.endswith(os.sep) else ancestor + os.sep
    return descendant.startswith(prefix)
