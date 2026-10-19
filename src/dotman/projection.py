from __future__ import annotations

import os
import stat
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from dotman.capture import BUILTIN_PATCH_CAPTURE
from dotman.command_runtime import (
    CommandRequest,
    CommandRuntime,
    ShellCommand,
    first_output_line,
    raise_for_command_interruption,
)
from dotman.collisions import TargetClaim, validate_reserved_path_conflicts, validate_target_collisions
from dotman.config import expand_path
from dotman.file_access import needs_sudo_for_read, read_bytes
from dotman.ignore import GitIgnoreChain, collect_gitignore_chain
from dotman.manifest import (
    FORCED_COMMAND_PREFIX,
    flatten_vars,
    merge_ignore_patterns,
    resolve_sync_policy,
    sync_policy_allows_operation,
)
from dotman.models import (
    AdditionalSource,
    EditorSpec,
    ManagerConfig,
    PackageSpec,
    ResolvedPackageSelection,
    TargetPathRule,
    TargetSpec,
    repo_qualified_target_text,
    target_path_rule_matches,
)
from dotman.repository import Repository
from dotman.templates import render_template_file, render_template_string


@dataclass(frozen=True)
class ProjectionContext:
    config: ManagerConfig
    command_runtime: CommandRuntime


@dataclass(frozen=True)
class TargetMetadata:
    repo_name: str
    package_id: str
    bound_profile: str | None
    requested_profile: str
    target_name: str
    repo_path: Path
    live_path: Path
    probe_command: str | None
    render_command: str | None
    capture_command: str | None
    compare_repo: str
    compare_live: str
    ignore_patterns: tuple[str, ...]
    gitignore_enabled: bool
    skip_markers: tuple[str, ...]
    chmod: str | None
    path_rules: tuple[TargetPathRule, ...]
    command_cwd: Path
    command_env: dict[str, str]
    package: PackageSpec
    target: TargetSpec
    gitignore: GitIgnoreChain | None = None
    editor: Any = None
    additional_sources: tuple[str, ...] = ()
    additional_source_entries: tuple[AdditionalSource, ...] = ()
    additional_sources_root: Path | None = None
    live_path_is_symlink: bool = False
    live_path_symlink_target: str | None = None
    # Rendered but unresolved; observation runs it once per session.
    ignore_command: str | None = None

    @property
    def canonical(self) -> str:
        return repo_qualified_target_text(
            repo_name=self.repo_name,
            package_id=self.package_id,
            target_name=self.target_name,
            bound_profile=self.bound_profile,
        )


def target_claim(metadata: TargetMetadata) -> TargetClaim:
    return TargetClaim(
        package_id=metadata.package_id,
        label=metadata.canonical,
        repo_path=metadata.repo_path,
        live_path=metadata.live_path,
        ignore_patterns=metadata.ignore_patterns,
    )


def target_claims_path(target: TargetSpec) -> bool:
    return target.probe is None


def validate_probe_target_config(*, repo_name: str, package: PackageSpec, target: TargetSpec) -> None:
    if target.probe is None:
        return
    target_label = repo_qualified_target_text(repo_name=repo_name, package_id=package.id, target_name=target.name)
    if resolve_sync_policy(package=package, target=target) == "push-only-delete":
        raise ValueError(f"target '{target_label}' uses probe and cannot use push-only-delete; use push-only")
    forbidden_probe_fields = {
        "source": target.source,
        "path": target.path,
        "type": target.target_type,
        "chmod": target.chmod,
        "render": target.render if target.render_explicit else None,
        "capture": target.capture if target.capture_explicit else None,
        "editor": target.editor if target.editor_explicit else None,
        "compare": (
            {"repo": target.compare_repo, "live": target.compare_live}
            if target.compare_repo_explicit or target.compare_live_explicit
            else None
        ),
        "ignore": target.ignore_patterns if target.ignore_patterns is not None else target.ignore_command,
        "path_rules": target.path_rules or None,
    }
    forbidden = sorted(name for name, value in forbidden_probe_fields.items() if value is not None)
    if forbidden:
        raise ValueError(
            f"target '{target_label}' uses probe and must not define: "
            + ", ".join(forbidden)
        )


def _projection_command(value: str) -> str:
    return value[len(FORCED_COMMAND_PREFIX):] if value.startswith(FORCED_COMMAND_PREFIX) else value


def build_target_metadata(
    *,
    repo: Repository,
    packages: list[PackageSpec],
    context: dict[str, Any],
    selection: ResolvedPackageSelection,
    operation: str,
    inferred_os: str,
    declaration_package_ids: set[str],
    target_names: set[str] | None = None,
    inspect_live_symlinks: bool = True,
    inspect_gitignore_patterns: bool = True,
    validate_declaration_conflicts: bool = True,
) -> list[TargetMetadata]:
    metadata_targets: list[TargetMetadata] = []

    for package in packages:
        if package.id not in declaration_package_ids:
            continue
        for target in (package.targets or {}).values():
            if target_names is not None and target.name not in target_names:
                continue
            if target.disabled:
                continue
            sync_policy = resolve_sync_policy(package=package, target=target)
            if not sync_policy_allows_operation(sync_policy, operation=operation):
                continue
            if target.probe is not None:
                validate_probe_target_config(repo_name=repo.config.name, package=package, target=target)
                probe_command = render_template_string(
                    target.probe,
                    context,
                    base_dir=target.declared_in,
                    source_path=target.declared_in,
                )
                placeholder_path = expand_path(str(target.declared_in), dereference=False)
                metadata_targets.append(
                    TargetMetadata(
                        repo_name=repo.config.name,
                        package_id=package.id,
                        bound_profile=selection.bound_profile,
                        requested_profile=selection.requested_profile,
                        target_name=target.name,
                        repo_path=placeholder_path,
                        live_path=placeholder_path,
                        probe_command=probe_command,
                        render_command=None,
                        capture_command=None,
                        editor=target.editor,
                        additional_sources=target.additional_sources,
                        additional_source_entries=target.additional_source_entries,
                        additional_sources_root=target.additional_sources_root,
                        compare_repo="raw",
                        compare_live="raw",
                        ignore_patterns=(),
                        gitignore_enabled=False,
                        skip_markers=(),
                        chmod=None,
                        path_rules=(),
                        command_cwd=target.declared_in,
                        command_env=build_target_command_env(
                            repo=repo,
                            package=package,
                            target=target,
                            repo_path=placeholder_path,
                            live_path=placeholder_path,
                            selection=selection,
                            operation=operation,
                            inferred_os=inferred_os,
                            context=context,
                        ),
                        package=package,
                        target=target,
                    )
                )
                continue
            if target.source is None or target.path is None:
                target_label = repo_qualified_target_text(repo_name=repo.config.name, package_id=package.id, target_name=target.name)
                raise ValueError(f"target '{target_label}' must define source and path")
            rendered_source = render_template_string(
                target.source,
                context,
                base_dir=target.declared_in,
                source_path=target.declared_in,
            )
            rendered_path = render_template_string(
                target.path,
                context,
                base_dir=target.declared_in,
                source_path=target.declared_in,
            )
            # Target identity must stay configuration-derived; following a source
            # symlink here would make ownership depend on volatile host state.
            repo_path = expand_path(str(target.declared_in / rendered_source), dereference=False)
            live_path = expand_path(rendered_path, dereference=False)
            live_path_is_symlink = inspect_live_symlinks and operation == "push" and live_path.is_symlink()
            live_path_symlink_target = os.readlink(live_path) if live_path_is_symlink else None
            render_command = (
                render_template_string(target.render, context, base_dir=target.declared_in, source_path=target.declared_in)
                if target.render != "raw"
                else None
            )
            capture_command = (
                render_template_string(target.capture, context, base_dir=target.declared_in, source_path=target.declared_in)
                if target.capture != "raw"
                else None
            )
            gitignore_enabled = package.gitignore_enabled if package.gitignore_enabled is not None else repo.ignore_defaults.gitignore
            pattern_layers: list[tuple[str, ...]] = [repo.ignore_defaults.patterns]
            gitignore = (
                collect_gitignore_chain(repo_path, repo.root, nested=inspect_gitignore_patterns)
                if gitignore_enabled else None
            )
            if package.ignore_patterns is not None:
                pattern_layers.append(package.ignore_patterns)
            if target.ignore_patterns is not None:
                pattern_layers.append(target.ignore_patterns)
            ignore_patterns = merge_ignore_patterns(*pattern_layers)
            skip_markers = repo.ignore_defaults.skip_markers
            path_rules = render_target_path_rules(target.path_rules, context=context, base_dir=target.declared_in)
            metadata_targets.append(
                TargetMetadata(
                    repo_name=repo.config.name,
                    package_id=package.id,
                    bound_profile=selection.bound_profile,
                    requested_profile=selection.requested_profile,
                    target_name=target.name,
                    repo_path=repo_path,
                    live_path=live_path,
                    probe_command=None,
                    render_command=render_command,
                    capture_command=capture_command,
                    compare_repo=target.compare_repo if target.compare_repo != "raw" else "raw",
                    compare_live=target.compare_live,
                    ignore_patterns=ignore_patterns,
                    gitignore_enabled=gitignore_enabled,
                    gitignore=gitignore,
                    skip_markers=skip_markers,
                    chmod=target.chmod,
                    path_rules=path_rules,
                    command_cwd=target.declared_in,
                    command_env=build_target_command_env(
                        repo=repo,
                        package=package,
                        target=target,
                        repo_path=repo_path,
                        live_path=live_path,
                        selection=selection,
                        operation=operation,
                        inferred_os=inferred_os,
                        context=context,
                    ),
                    package=package,
                    target=target,
                    editor=target.editor,
                    additional_sources=target.additional_sources,
                    additional_source_entries=target.additional_source_entries,
                    additional_sources_root=target.additional_sources_root,
                    live_path_is_symlink=live_path_is_symlink,
                    live_path_symlink_target=live_path_symlink_target,
                    ignore_command=(
                        render_template_string(target.ignore_command, context, base_dir=target.declared_in, source_path=target.declared_in)
                        if target.ignore_command is not None
                        else None
                    ),
                )
            )

    if validate_declaration_conflicts:
        claims = [target_claim(metadata) for metadata in metadata_targets if target_claims_path(metadata.target)]
        validate_target_collisions(claims, operation=operation, gitignore_chains={
            (metadata.repo_path, metadata.live_path): metadata.gitignore for metadata in metadata_targets
        })
        if operation == "push":
            validate_reserved_path_conflicts(repo.config.name, packages, claims, context)
    return metadata_targets



def resolve_target_kind(
    *,
    target_type: str | None,
    repo_path: Path,
    live_path: Path,
    target_label: str = "target",
    file_symlink_mode: str = "prompt",
    dir_symlink_mode: str = "fail",
) -> str:
    if target_type is None:
        return infer_target_kind(repo_path=repo_path, live_path=live_path)
    validate_explicit_target_type(
        target_type=target_type,
        repo_path=repo_path,
        live_path=live_path,
        target_label=target_label,
        file_symlink_mode=file_symlink_mode,
        dir_symlink_mode=dir_symlink_mode,
    )
    return target_type


def validate_explicit_target_type(
    *,
    target_type: str,
    repo_path: Path,
    live_path: Path,
    target_label: str,
    file_symlink_mode: str,
    dir_symlink_mode: str,
) -> None:
    live_follow_symlink = target_type == "file" or (
        target_type == "directory" and dir_symlink_mode == "follow"
    )
    path_roles = (
        ("repo source", repo_path, True),
        ("live", live_path, live_follow_symlink),
    )
    for role, path, follow_symlink in path_roles:
        existing_kind = existing_target_path_kind(path, follow_symlink=follow_symlink)
        if existing_kind is not None and existing_kind != target_type:
            raise ValueError(
                f"target '{target_label}' declares type = \"{target_type}\" but {role} path is {existing_kind}: {path}"
            )


def existing_target_path_kind(path: Path, *, follow_symlink: bool = False) -> str | None:
    if path.is_symlink():
        if not follow_symlink:
            return "file"
        resolved_path = path.resolve(strict=False)
        if resolved_path.is_dir():
            return "directory"
        if resolved_path.exists():
            return "file"
        return None
    if path.is_dir():
        return "directory"
    if path.exists():
        return "file"
    return None


def rendered_target_is_directory(metadata: TargetMetadata) -> bool:
    """The declared type, else whether either rendered side is a directory.

    Scope selection and Observation share this so every child Observation reports
    stays selectable by its own identity.
    """
    if metadata.target.target_type is not None:
        return metadata.target.target_type == "directory"
    return infer_target_kind(repo_path=metadata.repo_path, live_path=metadata.live_path) == "directory"


def infer_target_kind(*, repo_path: Path, live_path: Path) -> str:
    if repo_path.is_dir():
        return "directory"
    if live_path.is_dir():
        # Directory targets should still be recognized when the repo source tree
        # does not exist yet but the live path clearly shows a directory.
        return "directory"
    if repo_path.exists() or live_path.exists():
        return "file"
    return "unknown"




def default_compare_live(capture_command: str | None) -> str:
    if capture_command == BUILTIN_PATCH_CAPTURE:
        return "raw"
    if capture_command is not None:
        return "capture"
    return "raw"


def render_target_path_rules(
    path_rules: tuple[TargetPathRule, ...],
    *,
    context: dict[str, Any],
    base_dir: Path,
) -> tuple[TargetPathRule, ...]:
    return tuple(
        TargetPathRule(
            name=rule.name,
            pattern=rule.pattern,
            priority=rule.priority,
            chmod=rule.chmod,
            render=render_template_string(rule.render, context, base_dir=base_dir, source_path=base_dir) if rule.render != "raw" else "raw",
            capture=render_template_string(rule.capture, context, base_dir=base_dir, source_path=base_dir) if rule.capture != "raw" else "raw",
            # Compare commands render once, when the view is projected, as at target level.
            compare_repo=rule.compare_repo,
            compare_live=rule.compare_live,
            editor=rule.editor,
            sync_policy=rule.sync_policy,
            additional_sources=rule.additional_sources,
            additional_source_entries=rule.additional_source_entries,
            render_explicit=rule.render_explicit,
            capture_explicit=rule.capture_explicit,
            compare_repo_explicit=rule.compare_repo_explicit,
            compare_live_explicit=rule.compare_live_explicit,
            editor_explicit=rule.editor_explicit,
            priority_explicit=rule.priority_explicit,
            pattern_explicit=rule.pattern_explicit,
            sync_policy_explicit=rule.sync_policy_explicit,
            hooks=rule.hooks,
        )
        for rule in path_rules
    )





def directory_child_pull_views(
    *,
    target: TargetSpec,
    capture_command: str | None,
    target_compare_repo: str,
    target_compare_live: str,
    rule_compare_repo: str | None,
    rule_compare_live: str | None,
) -> tuple[str, str]:
    compare_repo = rule_compare_repo or target_compare_repo or "raw"
    compare_live = rule_compare_live or target_compare_live or default_compare_live(capture_command)
    return compare_repo, compare_live


def directory_child_policy(
    relative_path: str,
    path_rules: tuple[TargetPathRule, ...],
    *,
    default_render: str | None,
    default_capture: str | None,
    default_editor: EditorSpec | None = None,
    default_sync_policy: str | None = None,
) -> tuple[str | None, str | None, str | None, str | None, str | None, EditorSpec, tuple[str, ...], str | None]:
    desired_chmod = None
    render_command = default_render
    capture_command = default_capture
    compare_repo = None
    compare_live = None
    editor = default_editor or EditorSpec()
    sync_policy = default_sync_policy
    for rule in path_rules:
        if not target_path_rule_matches(relative_path, rule.pattern):
            continue
        if rule.chmod is not None:
            desired_chmod = rule.chmod
        if rule.render_explicit:
            render_command = None if rule.render == "raw" else rule.render
        if rule.capture_explicit:
            capture_command = None if rule.capture == "raw" else rule.capture
        if rule.compare_repo_explicit:
            compare_repo = rule.compare_repo
        if rule.compare_live_explicit:
            compare_live = rule.compare_live
        if rule.editor_explicit:
            editor = rule.editor
        if rule.sync_policy_explicit:
            sync_policy = rule.sync_policy
        if compare_live == "capture" and capture_command is None:
            compare_live = "raw"
    return desired_chmod, render_command, capture_command, compare_repo, compare_live, editor, editor.additional_sources, sync_policy



def file_is_executable(mode: int) -> bool:
    return bool(mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH))



class PlanningCommandError(ValueError):
    # Command output is untrusted data, not public diagnostic metadata: the
    # message stays output-free and `output_line` is for human output only.
    def __init__(self, message: str, *, output_line: str | None) -> None:
        super().__init__(message)
        self.output_line = output_line


def run_probe_command(command_runtime: CommandRuntime, metadata: TargetMetadata) -> bool:
    if metadata.probe_command is None:
        raise ValueError(f"missing probe command for {metadata.canonical}")
    result = command_runtime.run(
        CommandRequest(
            command=ShellCommand(metadata.probe_command),
            cwd=metadata.command_cwd,
            env=metadata.command_env,
        )
    )
    raise_for_command_interruption(result)
    if result.exit_code == 0:
        return True
    if result.exit_code == 100:
        return False
    raise PlanningCommandError(
        f"probe failed for {metadata.canonical} "
        f"with status {result.exit_code}",
        output_line=first_output_line(
            result.stderr.decode("utf-8", errors="replace"),
            result.stdout.decode("utf-8", errors="replace"),
        ),
    )



def resolve_ignore_command(command_runtime: CommandRuntime, metadata: TargetMetadata) -> TargetMetadata:
    """Append the command's exclusions after the static layers and clear it."""
    if metadata.ignore_command is None:
        return metadata
    label = repo_qualified_target_text(repo_name=metadata.repo_name, package_id=metadata.package_id,
                                       target_name=metadata.target_name, bound_profile=metadata.bound_profile)
    result = command_runtime.run(
        CommandRequest(command=ShellCommand(metadata.ignore_command), cwd=metadata.command_cwd, env=metadata.command_env)
    )
    raise_for_command_interruption(result)
    if result.exit_code != 0:
        raise PlanningCommandError(
            f"ignore command failed for {label} with status {result.exit_code}",
            output_line=first_output_line(
                result.stderr.decode("utf-8", errors="replace"),
                result.stdout.decode("utf-8", errors="replace"),
            ),
        )
    try:
        lines = tuple(line for line in result.stdout.decode("utf-8").splitlines() if line)
    except UnicodeDecodeError:
        raise PlanningCommandError(f"ignore command for {label} printed non-UTF-8 output", output_line=None) from None
    # Computed lines come last, so a negation could reopen statically excluded
    # paths; keep live state able only to narrow what syncs.
    negation = next((line for line in lines if line.startswith("!")), None)
    if negation is not None:
        raise PlanningCommandError(f"ignore command for {label} printed a negation pattern", output_line=negation)
    return replace(metadata, ignore_patterns=(*metadata.ignore_patterns, *lines), ignore_command=None)


def build_target_command_env(
    *,
    repo: Repository,
    package: PackageSpec,
    target: TargetSpec,
    repo_path: Path,
    live_path: Path,
    selection: ResolvedPackageSelection,
    operation: str,
    inferred_os: str,
    context: dict[str, Any],
) -> dict[str, str]:
    env = {
        "DOTMAN_REPO_NAME": repo.config.name,
        "DOTMAN_REPO_ROOT": str(repo.root),
        "DOTMAN_STATE_PATH": str(repo.config.state_path),
        "DOTMAN_PACKAGE_ID": package.id,
        "DOTMAN_PACKAGE_ROOT": str(package.package_root),
        "DOTMAN_TARGET_NAME": target.name,
        "DOTMAN_TARGET_REPO_PATH": str(repo_path),
        "DOTMAN_TARGET_LIVE_PATH": str(live_path),
        "DOTMAN_REPO_PATH": str(repo_path),
        "DOTMAN_SOURCE": str(repo_path),
        "DOTMAN_LIVE_PATH": str(live_path),
        "DOTMAN_PROFILE": selection.requested_profile,
        "DOTMAN_OPERATION": operation,
        "DOTMAN_OS": inferred_os,
        "DOTMAN_CPU_ARCH": context["cpu_arch"],
    }
    for flat_key, value in flatten_vars(context["vars"]).items():
        env[f"DOTMAN_VAR_{flat_key}"] = value
    return env


def build_package_hook_env(
    *,
    repo: Repository,
    package: PackageSpec,
    selection: ResolvedPackageSelection,
    operation: str,
    inferred_os: str,
    context: dict[str, Any],
) -> dict[str, str]:
    env = {
        "DOTMAN_REPO_NAME": repo.config.name,
        "DOTMAN_REPO_ROOT": str(repo.root),
        "DOTMAN_STATE_PATH": str(repo.config.state_path),
        "DOTMAN_PACKAGE_ID": package.id,
        "DOTMAN_PACKAGE_ROOT": str(package.package_root),
        "DOTMAN_PROFILE": selection.requested_profile,
        "DOTMAN_OPERATION": operation,
        "DOTMAN_OS": inferred_os,
        "DOTMAN_CPU_ARCH": context["cpu_arch"],
    }
    for flat_key, value in flatten_vars(context["vars"]).items():
        env[f"DOTMAN_VAR_{flat_key}"] = value
    return env


def build_repo_hook_env(
    *,
    repo: Repository,
    operation: str,
    context: dict[str, Any],
) -> dict[str, str]:
    env = {
        "DOTMAN_REPO_NAME": repo.config.name,
        "DOTMAN_REPO_ROOT": str(repo.root),
        "DOTMAN_STATE_PATH": str(repo.config.state_path),
        "DOTMAN_OPERATION": operation,
    }
    for flat_key, value in flatten_vars(context.get("vars", {})).items():
        env[f"DOTMAN_VAR_{flat_key}"] = value
    return env


def project_file_view(
    command_runtime: CommandRuntime,
    *,
    metadata: TargetMetadata,
    context: dict[str, Any],
    repository: bytes | None,
    live: bytes | None,
    view: str,
    repo_side: bool,
    repository_is_proposal: bool,
) -> bytes | None:
    """Project one view from frozen evidence; None is Missing, never empty bytes.

    ``repository`` and ``live`` are the Observation-time bytes that decide raw
    views and Missing. Command providers read endpoints at their real paths so
    they can depend on neighbouring files; the returned output is what the
    session freezes. A repository proposal has no endpoint on disk, so it is
    staged in a private file. Providers are trusted side-effect-free stdout
    producers.
    """
    source = repository if repo_side else live
    if source is None or view == "raw":
        return source
    command = view
    if view == "render":
        if metadata.render_command is None:
            return repository
        if metadata.render_command == "jinja":
            if repository is None:
                return None
            return render_template_file(
                metadata.repo_path,
                context,
                source_bytes=repository,
            )[0]
        command = metadata.render_command
    elif view == "capture":
        if metadata.capture_command is None:
            return live
        if metadata.capture_command == BUILTIN_PATCH_CAPTURE:
            raise ValueError("patch Capture is not a comparison view")
        command = metadata.capture_command
    else:
        command = render_template_string(
            _projection_command(command),
            context,
            base_dir=metadata.target.declared_in,
            source_path=metadata.target.declared_in,
        )
    with tempfile.TemporaryDirectory(prefix="dotman-proposal-") as directory:
        repository_path = metadata.repo_path
        if repository_is_proposal and repository is not None:
            repository_path = Path(directory) / "repository"
            repository_path.write_bytes(repository)
            repository_path.chmod(0o400)
        # Projection commands run without elevation. Stage a protected input
        # into a private readable copy so a command Render or Capture can read
        # it without being elevated itself. The staging directory is removed
        # afterwards with the proposal staging above.
        if repository_path.exists() and needs_sudo_for_read(repository_path):
            staged_repo = Path(directory) / f"repo-{repository_path.name}"
            staged_repo.write_bytes(read_bytes(repository_path))
            staged_repo.chmod(0o400)
            repository_path = staged_repo
        live_path = metadata.live_path
        if live_path.exists() and needs_sudo_for_read(live_path):
            staged_live = Path(directory) / f"live-{live_path.name}"
            staged_live.write_bytes(read_bytes(live_path))
            staged_live.chmod(0o400)
            live_path = staged_live
        env = {
            **metadata.command_env,
            "DOTMAN_TARGET_REPO_PATH": str(repository_path),
            "DOTMAN_REPO_PATH": str(repository_path),
            "DOTMAN_SOURCE": str(repository_path),
            "DOTMAN_TARGET_LIVE_PATH": str(live_path),
            "DOTMAN_LIVE_PATH": str(live_path),
        }
        result = command_runtime.run(
            CommandRequest(
                ShellCommand(_projection_command(command)),
                cwd=metadata.command_cwd,
                env=env,
                elevation="none",
            )
        )
        raise_for_command_interruption(result)
        if result.exit_code:
            # Provider output can contain payload bytes and private staging paths;
            # public diagnostics retain only the operation and exit evidence.
            raise ValueError(
                f"comparison projection failed for {metadata.repo_path} with exit {result.exit_code}"
            )
        return result.stdout
