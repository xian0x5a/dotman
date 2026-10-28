from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Iterable

from pathspec import PathSpec
from pathspec.gitignore import GitIgnoreSpec


@dataclass(frozen=True)
class GitIgnoreChain:
    """Control files retain their own coordinate system; Git globs are never rebased."""
    target: str
    controls: tuple[tuple[str, tuple[str, ...]], ...] = ()

    def matcher(self) -> "GitIgnoreMatcher":
        return GitIgnoreMatcher(self.target, tuple(
            (directory, GitIgnoreSpec.from_lines(lines)) for directory, lines in self.controls
        ))


@dataclass(frozen=True)
class GitIgnoreMatcher:
    target: str
    controls: tuple[tuple[str, GitIgnoreSpec], ...]

    def matches(self, relative: str) -> bool:
        path = "/".join(part for part in (self.target, relative.rstrip("/")) if part)
        parts = path.split("/")
        # An excluded directory cannot be reopened by a child's control file.
        for index in range(1, len(parts) + 1):
            candidate = "/".join(parts[:index])
            directory = index < len(parts) or relative.endswith("/")
            ignored = False
            for scope, spec in self.controls:
                prefix = scope + "/" if scope else ""
                if not candidate.startswith(prefix):
                    continue
                local = candidate[len(prefix):] + ("/" if directory else "")
                result = spec.check_file(local)
                if result.include is not None:
                    ignored = result.include
            if ignored:
                return True
        return False

    def matches_directory(self, relative: str) -> bool:
        return self.matches(relative.rstrip("/") + "/")


def collect_gitignore_chain(root: Path, repository_root: Path, *, nested: bool = True) -> GitIgnoreChain:
    from dotman.file_access import read_bytes
    import stat

    relative = root.relative_to(repository_root)
    directories = [repository_root]
    for part in relative.parts:
        directories.append(directories[-1] / part)
    if not nested:
        # Sync reads target and nested controls inside its diagnostic-aware census.
        directories.pop()
    if nested and root.is_dir() and not root.is_symlink():
        for directory, children, _files in os.walk(root, followlinks=False):
            children.sort()
            if Path(directory) != root:
                directories.append(Path(directory))
    controls = []
    for directory in directories:
        control = directory / ".gitignore"
        try:
            shape = control.lstat()
        except (FileNotFoundError, NotADirectoryError):
            continue
        # Ignore controls are data, not symlink or special-file payloads.
        if stat.S_ISREG(shape.st_mode):
            scope = directory.relative_to(repository_root).as_posix()
            controls.append(("" if scope == "." else scope,
                             tuple(read_bytes(control).decode("utf-8", errors="replace").splitlines())))
    return GitIgnoreChain("" if relative == Path(".") else relative.as_posix(), tuple(controls))


def _normalize_relative_path(relative_path: str) -> str:
    normalized = relative_path.replace("\\", "/")
    if normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized.lstrip("/")


@dataclass(frozen=True, slots=True)
class IgnoreMatcher:
    spec: PathSpec | None = None
    gitignore: GitIgnoreMatcher | None = None

    @classmethod
    def from_patterns(cls, patterns: Iterable[str], *, gitignore: GitIgnoreChain | None = None) -> "IgnoreMatcher":
        normalized_patterns = tuple(pattern for pattern in patterns if pattern)
        if not normalized_patterns:
            return cls(gitignore=gitignore.matcher() if gitignore else None)
        return cls(GitIgnoreSpec.from_lines(normalized_patterns), gitignore.matcher() if gitignore else None)

    def matches(self, relative_path: str) -> bool:
        relative = _normalize_relative_path(relative_path)
        result = self.spec.check_file(relative) if self.spec else None
        # Dotman patterns are target-relative overrides, including explicit negations.
        if result is not None and result.include is not None:
            return bool(result.include)
        return bool(self.gitignore and self.gitignore.matches(relative))

    def matches_directory(self, relative_path: str) -> bool:
        return self.matches(f"{_normalize_relative_path(relative_path).rstrip('/')}/")



