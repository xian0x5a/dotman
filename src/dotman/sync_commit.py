"""Commit Work: commit the repository sources a run wrote, when pre-approved."""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from string import Formatter
from typing import Literal, Sequence

from dotman.command_runtime import (
    ArgvCommand, CommandRequest, CommandRuntime, first_output_line, raise_for_command_interruption,
)
from dotman.models import ResolvedSyncTarget, package_ref_text, target_ref_text
COMMIT_MESSAGE_FIELDS = ("operation", "count", "summary", "packages", "repo")


class CommitFailed(RuntimeError):
    pass


@dataclass(frozen=True)
class GitWorkTree:
    root: Path
    # None on a detached HEAD.
    branch: str | None


@dataclass(frozen=True)
class RepositoryCommit:
    repo: str
    status: Literal["committed", "no-changes", "skipped", "failed", "interrupted"]
    commit: str | None = None
    message: str | None = None
    error: str | None = None


def validate_commit_message_template(template: object) -> str:
    if not isinstance(template, str) or not template.strip():
        raise ValueError("config git.commit_message must be a non-empty string")
    try:
        fields = [field for _, field, _, _ in Formatter().parse(template) if field is not None]
    except ValueError as exc:
        raise ValueError(f"config git.commit_message is not a valid template: {exc}") from exc
    for field in fields:
        if field not in COMMIT_MESSAGE_FIELDS:
            allowed = ", ".join(f"{{{name}}}" for name in COMMIT_MESSAGE_FIELDS)
            raise ValueError(f"config git.commit_message uses unknown placeholder {{{field}}}; use {allowed}")
    # Names alone miss bad format specs, conversions and nested fields, which
    # would otherwise fail only after a run had written its sources.
    try:
        template.format(operation="sync", count=1, summary="sync main:app.unit", packages="main:app", repo="main")
    except (ValueError, LookupError, AttributeError, TypeError) as exc:
        raise ValueError(f"config git.commit_message is not a valid template: {exc}") from exc
    return template


def render_commit_message(
    template: str, *, operation: str, repo: str, targets: Sequence[ResolvedSyncTarget],
) -> str:
    # Directory children commit under their target; a message counts targets.
    unique = tuple(dict.fromkeys(replace(target, child_path=None) for target in targets))
    packages = sorted({package_ref_text(package_id=target.package_id, bound_profile=target.bound_profile)
                       for target in unique})
    # Name the narrowest scope that covers the change, so one-line logs stay informative.
    if len(unique) == 1:
        target = unique[0]
        scope = target_ref_text(package_id=target.package_id, target_name=target.target_name,
                                bound_profile=target.bound_profile)
    elif len(packages) == 1:
        scope = f"{packages[0]} ({len(unique)} targets)"
    else:
        scope = f"{len(unique)} targets in {len(packages)} packages"
    return template.format(operation=operation, count=len(unique), summary=f"{operation} {scope}",
                           packages="\n".join(packages), repo=repo)


def probe_work_tree(path: Path, runtime: CommandRuntime) -> GitWorkTree | None:
    """The git work tree containing `path`, or None when git cannot commit there."""
    try:
        top = runtime.run(_git_request(path, "rev-parse", "--show-toplevel"))
        if top.exit_code != 0:
            return None
        branch = runtime.run(_git_request(path, "branch", "--show-current"))
    except OSError:
        # No git executable: there is nothing to offer.
        return None
    raise_for_command_interruption(branch)
    root = Path(top.stdout_text.strip())
    return GitWorkTree(root, branch.stdout_text.strip() or None if branch.exit_code == 0 else None)


def commit_sources(root: Path, paths: Sequence[Path], message: str, runtime: CommandRuntime) -> str | None:
    """Commit exactly `paths`; unrelated staged or unstaged work stays as it was.

    Returns the short commit hash, or None when the paths match HEAD.
    """
    paths = tuple(dict.fromkeys(paths))
    tracked = {root / name for name in _git(runtime, root, "ls-files", "-z", "--", *map(str, paths)).split("\0") if name}
    # A deleted source that git never tracked has nothing to stage; naming it would fail.
    pathspec = tuple(str(path) for path in paths if path.exists() or path.is_symlink() or path in tracked)
    if not pathspec:
        return None
    _git(runtime, root, "add", "-A", "--", *pathspec)
    if runtime.run(_git_request(root, "diff", "--cached", "--quiet", "--", *pathspec)).exit_code == 0:
        return None
    # Pathspec commit (`--only`) leaves other staged changes in the index.
    _git(runtime, root, "commit", "--quiet", "--file=-", "--", *pathspec, message=message)
    return _git(runtime, root, "rev-parse", "--short", "HEAD").strip()


def _git_request(cwd: Path, *args: str, message: str | None = None) -> CommandRequest:
    return CommandRequest(ArgvCommand(("git", *args)), cwd=cwd,
                          input=message.encode() if message is not None else None)


def _git(runtime: CommandRuntime, cwd: Path, *args: str, message: str | None = None) -> str:
    result = runtime.run(_git_request(cwd, *args, message=message))
    raise_for_command_interruption(result)
    if result.exit_code != 0:
        detail = first_output_line(result.stderr_text, result.stdout_text) or f"exited with status {result.exit_code}"
        raise CommitFailed(f"git {args[0]} failed: {detail}")
    return result.stdout_text
