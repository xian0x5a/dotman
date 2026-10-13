"""One symmetric directory census and identity-derived child interpretation."""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
import stat

from dotman import projection
from dotman.file_access import read_bytes
from dotman.ignore import IgnoreMatcher, GitIgnoreChain
from dotman.manifest import resolve_sync_policy, validate_patch_capture
from dotman.models import repo_qualified_target_text


@dataclass(frozen=True)
class CensusFailure:
    code: str
    message: str


@dataclass(frozen=True)
class DirectoryCensus:
    # None denotes a payload candidate; failures retain their lexical path.
    entries: tuple[tuple[str, tuple[CensusFailure, ...]], ...]
    repository_leaves: tuple[str, ...] = ()
    live_leaves: tuple[str, ...] = ()
    unrestricted: bool = False

    def blockers(self, relative: str, *, repository: bool) -> tuple[str, ...]:
        leaves = self.repository_leaves if repository else self.live_leaves
        return tuple(path for path in leaves if path != relative and (
            relative.startswith(path + "/") or path.startswith(relative + "/")
        ))



def census_directory(
    metadata: projection.TargetMetadata, *, follow_live_directories: bool,
    selected_paths: tuple[str, ...] = (),
) -> DirectoryCensus:
    """Inspect both trees once, then apply their combined controls to both sides.

    Directory nodes are only traversal/diagnostic scopes, never payloads. Keep
    discovery failures local so a bad sibling cannot erase healthy evidence.
    """
    entries: dict[str, list[CensusFailure]] = {}
    markers: set[str] = set()
    directories: set[str] = set()
    git_controls = list(metadata.gitignore.controls) if metadata.gitignore else []
    leaves = {True: set(), False: set()}
    exclusions = IgnoreMatcher.from_patterns(metadata.ignore_patterns)

    def failure(relative: str, code: str, message: str) -> None:
        entries.setdefault(relative, []).append(CensusFailure(code, f"{relative or '.'}: {message}"))

    def excluded(relative: str, matcher: IgnoreMatcher, *, directory: bool = False) -> bool:
        path = Path(relative)
        return ((matcher.matches_directory(relative) if directory else matcher.matches(relative))
                or any(matcher.matches_directory(parent.as_posix()) for parent in path.parents if parent != Path('.')))

    def scan(root: Path, *, repository: bool) -> None:
        active: set[tuple[int, int]] = set()

        def visit(path: Path, relative: str, *, root_node: bool = False) -> None:
            # An excluded subtree is opaque, and therefore an unmanaged blocker.
            if relative and excluded(relative, exclusions):
                leaves[repository].add(relative)
                return
            try:
                shape = path.lstat()
                link = stat.S_ISLNK(shape.st_mode)
                if not stat.S_ISDIR(shape.st_mode):
                    leaves[repository].add(relative)
                if link:
                    if repository:
                        # Classify link shape for directory-only exclusions, but
                        # never descend or read its referent as repository payload.
                        failure(relative, 'repository-symlink', 'repository entry must not be a symlink')
                        if path.is_dir():
                            directories.add(relative)
                        return
                    try:
                        shape = path.stat()
                    except FileNotFoundError:
                        entries.setdefault(relative, [])
                        return
                directory = stat.S_ISDIR(shape.st_mode)
                if relative and excluded(relative, exclusions, directory=directory):
                    leaves[repository].add(relative)
                    return
                if directory:
                    # Followed directory links remain traversal scopes, not
                    # payload blockers; their interpretation belongs to link policy.
                    if link and not repository and follow_live_directories:
                        leaves[repository].discard(relative)
                    directories.add(relative)
                    if link and not follow_live_directories:
                        failure(relative, 'directory-symlink', 'live directory symlink requires follow mode')
                        return
                    key = (shape.st_dev, shape.st_ino)
                    if key in active:
                        failure(relative, 'directory-symlink-loop', 'live directory symlink loop')
                        return
                    active.add(key)
                    try:
                        # Parent controls must precede nested controls even when a
                        # hidden directory sorts lexically before .gitignore.
                        children = sorted(path.iterdir(), key=lambda entry: (entry.name != ".gitignore", entry.name))
                        if any(child.name in metadata.skip_markers for child in children):
                            markers.add(relative)
                            leaves[repository].add(relative)
                            return
                        for child in children:
                            child_relative = f'{relative}/{child.name}' if relative else child.name
                            if child.name == '.gitignore':
                                leaves[repository].add(child_relative)
                                if repository and metadata.gitignore_enabled:
                                    # Controls must be regular files, never links or FIFOs.
                                    if stat.S_ISREG(child.lstat().st_mode):
                                        content = read_bytes(child).decode('utf-8', errors='replace')
                                        target = metadata.gitignore.target if metadata.gitignore else ''
                                        scope = '/'.join(part for part in (target, relative) if part)
                                        git_controls.append((scope, tuple(content.splitlines())))
                                continue
                            visit(child, child_relative)
                    finally:
                        active.remove(key)
                elif root_node or not (stat.S_ISREG(shape.st_mode) or link):
                    failure(relative, 'unsupported-entry', 'endpoint must be a regular file' if not root_node else 'target root must be a directory')
                else:
                    entries.setdefault(relative, [])
            except FileNotFoundError:
                # A missing endpoint contributes no path; the opposite tree may.
                return
            except OSError as exc:
                failure(relative, 'census-failed', str(exc))

        visit(root, '', root_node=True)

    scan(metadata.repo_path, repository=True)
    scan(metadata.live_path, repository=False)
    # A failure to discover an ancestor means every descendant is unknown,
    # including candidates found on the opposite side. Inherit the original
    # diagnostics before exclusions, without opening either payload endpoint.
    failures_by_scope = {relative: tuple(failures) for relative, failures in entries.items() if failures}
    for relative in entries.keys() | set(selected_paths):
        inherited = [failure for prefix, failures in failures_by_scope.items()
                     if relative != prefix and (not prefix or relative.startswith(prefix + "/"))
                     for failure in failures]
        if inherited:
            entries.setdefault(relative, []).extend(inherited)
    git = IgnoreMatcher.from_patterns(metadata.ignore_patterns, gitignore=GitIgnoreChain(
        metadata.gitignore.target if metadata.gitignore else "", tuple(git_controls)))
    return DirectoryCensus(tuple(
        (relative, tuple(failures)) for relative, failures in sorted(entries.items())
        if not any(not prefix or relative == prefix or relative.startswith(prefix + '/') for prefix in markers)
        and not excluded(relative, git)
        # Exact-child diagnostics can be synthesized after traversal pruning.
        and not excluded(relative, exclusions)
        # A directory exclusion must also hide diagnostics on a directory link.
        and not (relative in directories and (git.matches_directory(relative) or exclusions.matches_directory(relative)))
    ), tuple(sorted(leaves[True])), tuple(sorted(leaves[False])),
       # An unresolved ignore command (inspection) may exclude anything.
       not metadata.ignore_patterns and metadata.ignore_command is None and not markers and not git_controls
       and not any(failures for failures in entries.values()))


def validate_planned_child(metadata: projection.TargetMetadata, relative: str, child: projection.TargetMetadata) -> None:
    """Reject a child the run plans whose folded Path Rules break Patch Capture.

    Only planning checks this: a broken rule must not make a stored Base look
    untracked during Base inspection.
    """
    label = repo_qualified_target_text(repo_name=metadata.repo_name, package_id=metadata.package_id,
                                       target_name=metadata.target_name, bound_profile=metadata.bound_profile)
    validate_patch_capture(f'target "{label}/{relative}"', render=child.render_command or "raw",
                           capture=child.capture_command or "raw", compare_repo=child.compare_repo,
                           compare_live=child.compare_live)


def child_metadata(metadata: projection.TargetMetadata, relative: str) -> projection.TargetMetadata:
    policy = projection.directory_child_policy(
        relative, metadata.path_rules, default_render=metadata.render_command,
        default_capture=metadata.capture_command, default_editor=metadata.editor,
        default_sync_policy=resolve_sync_policy(package=metadata.package, target=metadata.target),
    )
    chmod, render, capture, repo_view, live_view, editor, additional, sync_policy = policy
    compare_repo, compare_live = projection.directory_child_pull_views(
        target=metadata.target, capture_command=capture,
        target_compare_repo=metadata.compare_repo, target_compare_live=metadata.compare_live,
        rule_compare_repo=repo_view, rule_compare_live=live_view,
    )
    repo_path, live_path = metadata.repo_path / relative, metadata.live_path / relative
    return replace(
        metadata, repo_path=repo_path, live_path=live_path, render_command=render,
        capture_command=capture, compare_repo=compare_repo, compare_live=compare_live,
        chmod=chmod, editor=editor, additional_sources=additional,
        additional_source_entries=editor.source_entries(),
        target=replace(metadata.target, sync_policy=sync_policy),
        command_env={**metadata.command_env,
                     'DOTMAN_SOURCE': str(repo_path), 'DOTMAN_REPO_PATH': str(repo_path),
                     'DOTMAN_TARGET_REPO_PATH': str(repo_path),
                     'DOTMAN_LIVE_PATH': str(live_path), 'DOTMAN_TARGET_LIVE_PATH': str(live_path)},
    )
