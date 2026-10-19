"""Commit Work: commit the repository sources a run wrote, when pre-approved."""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from string import Formatter
from typing import Iterator, Literal, Sequence

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


def _template_fields(template: str) -> Iterator[str]:
    # A format spec may nest fields, as in {summary:{width}}.
    for _, field, spec, _ in Formatter().parse(template):
        if field is not None:
            yield field
            yield from _template_fields(spec or "")


def validate_commit_message_template(template: object) -> str:
    if not isinstance(template, str) or not template.strip():
        raise ValueError("config git.commit_message must be a non-empty string")
    try:
        fields = list(_template_fields(template))
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
        raise_for_command_interruption(top)
        if top.exit_code != 0:
            return None
        branch = runtime.run(_git_request(path, "branch", "--show-current"))
    except InterruptedError:
        # Cancellation is an OSError too, but it must stop the operation.
        raise
    except OSError:
        # No git executable: there is nothing to offer.
        return None
    raise_for_command_interruption(branch)
    root = Path(top.stdout_text.strip())
    return GitWorkTree(root, branch.stdout_text.strip() or None if branch.exit_code == 0 else None)


def committable_paths(root: Path, paths: Sequence[Path], runtime: CommandRuntime) -> tuple[Path, ...]:
    """The `paths` with changes a commit would record, read without touching the index.

    Like `git add -A`, git status leaves out untracked sources git ignores: they
    are kept out of history on purpose. It also leaves out unchanged sources and
    deleted sources git never tracked, so the message can describe exactly these.
    """
    status = _git(runtime, root, "status", "--porcelain", "-z", "--no-renames", "--untracked-files=all",
                  "--", *map(str, paths))
    # Each -z entry is "XY <path>", relative to the work tree root.
    changed = {root / entry[3:] for entry in status.split("\0") if entry}
    # A directory source covers the changed files below it.
    return tuple(path for path in dict.fromkeys(paths)
                 if path in changed or any(path in name.parents for name in changed))


def commit_sources(root: Path, paths: Sequence[Path], message: str, runtime: CommandRuntime) -> str | None:
    """Commit exactly `paths` from `committable_paths`; unrelated staged or unstaged work stays as it was.

    Returns the short commit hash, or None when the paths match HEAD.
    """
    pathspec = tuple(map(str, paths))
    _git(runtime, root, "add", "-A", "--", *pathspec)
    if runtime.run(_git_request(root, "diff", "--cached", "--quiet", "--", *pathspec)).exit_code == 0:
        return None
    # Pathspec commit (`--only`) leaves other staged changes in the index.
    _git(runtime, root, "commit", "--quiet", "--file=-", "--", *pathspec, message=message)
    return _git(runtime, root, "rev-parse", "--short", "HEAD").strip()


def _git_request(cwd: Path, *args: str, message: str | None = None) -> CommandRequest:
    # Source paths are file names, not patterns: `star*` must not also commit `start`.
    return CommandRequest(ArgvCommand(("git", "--literal-pathspecs", *args)), cwd=cwd,
                          input=message.encode() if message is not None else None)


def _git(runtime: CommandRuntime, cwd: Path, *args: str, message: str | None = None) -> str:
    result = runtime.run(_git_request(cwd, *args, message=message))
    raise_for_command_interruption(result)
    if result.exit_code != 0:
        detail = first_output_line(result.stderr_text, result.stdout_text) or f"exited with status {result.exit_code}"
        raise CommitFailed(f"git {args[0]} failed: {detail}")
    return result.stdout_text
