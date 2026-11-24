"""Freeze non-payload work and retain only Guard-admitted hook families."""

from __future__ import annotations

from dataclasses import dataclass, replace
from functools import partial
from typing import Literal
import stat

from dotman.command_runtime import CommandRuntime, run_ordered
from dotman.models import GuardSkip, ResolvedPackageIdentity, ResolvedSyncTarget
from dotman.planning import PackagePlanningInput
from dotman.projection import TargetMetadata, run_probe_command
from dotman.progress import ProgressSink
from dotman.sync_observation import Diagnostic, _ResolvedInputs, _identity
from dotman.sync_publication import PublicationMetadata, stage_target_order


@dataclass(frozen=True)
class AuxiliaryRow:
    row_id: str
    kind: Literal["probe", "hook", "directory-root", "guard-skip"]
    included: bool
    scope: str
    directions: tuple[str, ...]
    allowed_commands: tuple[Literal["set-included"], ...] = ("set-included",)
    diagnostics: tuple[Diagnostic, ...] = ()
    guard_skip: GuardSkip | None = None
    # Package inputs whose dependency closure brought this scope in; empty when requested.
    included_via: tuple[ResolvedPackageIdentity, ...] = ()
    # Probe Work only: the additional_triggers entries with work in this run, as
    # (repo, package, target or None); empty when the probe alone activated it.
    triggered_by: tuple[tuple[str, str, str | None], ...] = ()


def guard_skip_rows(skips: tuple[tuple[str, GuardSkip], ...]) -> tuple[AuxiliaryRow, ...]:
    """Explain omitted work in the workset; it can never be selected."""
    return tuple(
        AuxiliaryRow(
            f"{skip.scope_label} (guard_{direction})"
            + (f" (path rule: {skip.path_rule_pattern})" if skip.path_rule_pattern is not None else ""),
            "guard-skip", False, skip.scope_label, (direction,), allowed_commands=(), guard_skip=skip,
        )
        for direction, skip in skips
    )


def _package_scope(repo: str, package_id: str, profile: str | None) -> str:
    return ResolvedPackageIdentity(repo, package_id, profile).canonical


def retain_directional_hooks(
    metadata: PublicationMetadata, scopes: frozenset[str],
) -> PublicationMetadata:
    """Keep payload metadata for completion, but never restore a removed hook route."""
    return replace(
        metadata,
        repo_hooks=tuple((repo, hooks if repo in scopes else ()) for repo, hooks in metadata.repo_hooks),
        packages=tuple(replace(
            package,
            hooks={
                name: [hook for hook in hooks if
                    _package_scope(package.repo_name, package.package_id, package.bound_profile) in scopes
                    and (hook.scope_kind != "target" or ResolvedSyncTarget(
                        package.repo_name, package.package_id, hook.target_name,
                        bound_profile=package.bound_profile,
                    ).canonical in scopes)
                ]
                for name, hooks in package.hooks.items()
            },
        ) for package in metadata.packages),
    )


# Work identity for trigger matching: repo, package and target (None for package scope).
_WorkKey = tuple[str, str, str | None]


def _work_key(identity: ResolvedSyncTarget) -> _WorkKey:
    return identity.repo, identity.package_id, identity.target_name


def _triggers_with_work(target: TargetMetadata, work: set[_WorkKey]) -> tuple[_WorkKey, ...]:
    """The target's additional_triggers entries that have work in this run.

    Package instances are not told apart: any instance's work counts, which at
    worst runs an idempotent hook that had nothing to do.
    """
    entries = []
    for entry in target.target.additional_triggers or ():
        package_id, _, target_name = entry.partition(".")
        if any(key[0] == target.repo_name and key[1] == package_id and (not target_name or key[2] == target_name)
               for key in work):
            entries.append((target.repo_name, package_id, target_name or None))
    return tuple(entries)


def plan_auxiliary(
    inputs: _ResolvedInputs,
    directional: dict[str, list[PackagePlanningInput]],
    metadata: dict[str, PublicationMetadata],
    *,
    drifted: tuple[ResolvedSyncTarget, ...] = (),
    command_runtime: CommandRuntime,
    run_noop: bool,
    dir_symlink_mode: str = "fail",
    sink: ProgressSink | None = None,
) -> tuple[AuxiliaryRow, ...]:
    admitted = {
        direction: {_identity(target) for item in survivors for target in item.target_metadata}
        for direction, survivors in directional.items()
    }
    directions = {
        identity: tuple(direction for direction in ("push", "pull") if identity in admitted[direction])
        for identity in inputs
    }
    probe_targets = [
        (identity, target) for identity, (_item, target) in inputs.items() if target.probe_command is not None
    ]
    # A probe with no surviving direction never runs, yet still counts as progress.
    probe_tasks = [
        partial(run_probe_command, command_runtime, target) if directions[identity] else (lambda: False)
        for identity, target in probe_targets
    ]
    probed_present = set()
    for (identity, _target), present in zip(probe_targets, run_ordered(probe_tasks), strict=True):
        if present:
            probed_present.add(identity)
        if sink is not None:
            sink.update(1)

    root_work = {
        identity for identity, (_item, target) in inputs.items()
        if (target.target.target_type == "directory" and "push" in directions[identity] and target.chmod is not None
            and target.live_path.is_dir() and (not target.live_path.is_symlink() or dir_symlink_mode == "follow")
            and stat.S_IMODE(target.live_path.stat().st_mode) != int(target.chmod, 8))
    }

    hook_rows = []
    hook_work: set[_WorkKey] = set()
    for direction, frozen in metadata.items():
        scopes = {}
        for repo, hooks in frozen.repo_hooks:
            if any(run_noop or hook.run_noop for hook in hooks):
                scopes[repo] = None
        for package in frozen.packages:
            package_scope = _package_scope(package.repo_name, package.package_id, package.bound_profile)
            for hooks in package.hooks.values():
                for hook in hooks:
                    if not (run_noop or hook.run_noop):
                        continue
                    hook_work.add((package.repo_name, package.package_id,
                                   hook.target_name if hook.scope_kind == "target" else None))
                    scope = (
                        ResolvedSyncTarget(package.repo_name, package.package_id, hook.target_name,
                                           bound_profile=package.bound_profile).canonical
                        if hook.scope_kind == "target" else package_scope
                    )
                    scopes[scope] = None
        hook_rows.extend(
            AuxiliaryRow(f"{scope} ({direction}-hooks)", "hook", False, scope, (direction,))
            for scope in scopes
        )

    # Triggers name direct dependencies, which run first, so deciding in stage order
    # lets a target activated by its triggers count as work for a later one.
    work = hook_work | {_work_key(identity) for identity in (*drifted, *probed_present, *root_work)}
    stage_position = {identity: index for index, identity in enumerate(stage_target_order(metadata["push"]))}
    triggered_by = {}
    for identity, target in sorted(probe_targets, key=lambda pair: stage_position[pair[0]]):
        entries = _triggers_with_work(target, work) if directions[identity] else ()
        if entries:
            triggered_by[identity] = entries
            probed_present.add(identity)
            work.add(_work_key(identity))

    rows = []
    for identity in inputs:
        if identity in root_work:
            rows.append(AuxiliaryRow(identity.canonical, "directory-root", False, identity.canonical, ("push",)))
        if identity in probed_present:
            rows.append(AuxiliaryRow(identity.canonical, "probe", False, identity.canonical, directions[identity],
                                     triggered_by=triggered_by.get(identity, ())))
    return tuple(rows) + tuple(hook_rows)
