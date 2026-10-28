from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotman.editor import resolve_editor_command
from dotman.models import FullSpecSelector, TrackedPackageEntryIssue
from dotman.repository import Repository
from dotman.tracking import (
    TrackedPackagesFileError,
    TrackedStateContext,
    get_repository,
    list_invalid_explicit_package_entries,
    read_tracked_package_entries_file,
    sorted_tracked_package_entry_issues,
)


@dataclass(frozen=True)
class DoctorCheck:
    key: str
    status: str
    detail: str
    path: Path | None = None
    repo_name: str | None = None
    hint: str | None = None
    count: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "status": self.status,
            "detail": self.detail,
            "path": None if self.path is None else str(self.path),
            "repo_name": self.repo_name,
            "hint": self.hint,
            **({"count": self.count} if self.count is not None else {}),
        }


@dataclass(frozen=True)
class DoctorSummary:
    config_path: Path
    repo_count: int
    checks: list[DoctorCheck]
    invalid_package_entries: list[TrackedPackageEntryIssue]

    @property
    def failed_checks(self) -> list[DoctorCheck]:
        return [check for check in self.checks if check.status == "failed"]

    @property
    def warning_checks(self) -> list[DoctorCheck]:
        return [check for check in self.checks if check.status == "warn"]

    @property
    def ok(self) -> bool:
        return not self.failed_checks and not self.invalid_package_entries

    def to_dict(self) -> dict[str, Any]:
        return {
            "config_path": str(self.config_path),
            "checks": [check.to_dict() for check in self.checks],
            "invalid_package_entries": [issue.to_dict() for issue in self.invalid_package_entries],
            "ok": self.ok,
            "repo_count": self.repo_count,
        }


def doctor_context(context: TrackedStateContext) -> DoctorSummary:
    checks: list[DoctorCheck] = [*_doctor_dependency_checks()]
    raw_tracked_package_entries_by_repo: dict[str, list[Any]] = {}

    for repo_config in context.config.ordered_repos:
        repo = get_repository(context, repo_config.name)
        checks.extend(_doctor_repo_checks(repo))
        raw_bindings, binding_checks = _read_configured_package_entries_for_doctor(repo)
        checks.extend(binding_checks)
        if raw_bindings is not None:
            raw_tracked_package_entries_by_repo[repo.config.name] = raw_bindings

    invalid_package_entries = list_invalid_explicit_package_entries(
        context,
        bindings_by_repo=raw_tracked_package_entries_by_repo,
    )
    orphan_issues, orphan_checks = _collect_orphan_package_entry_issues(context)
    checks.extend(orphan_checks)

    checks.extend(_doctor_snapshot_checks(context))

    return DoctorSummary(
        config_path=context.config.config_path,
        repo_count=len(context.config.repos),
        checks=checks,
        invalid_package_entries=sorted_tracked_package_entry_issues([*invalid_package_entries, *orphan_issues]),
    )


def _doctor_dependency_checks() -> list[DoctorCheck]:
    return [
        _command_dependency_check(
            command_name="git",
            required=True,
            key="dependency_git",
            ok_detail="git available for diff review",
            missing_detail="git is not installed",
            missing_hint="Install git. Dotman uses git diff for review flows.",
        ),
        _command_dependency_check(
            command_name="fzf",
            required=False,
            key="dependency_fzf",
            ok_detail="fzf available for long interactive selections",
            missing_detail="fzf is not installed",
            missing_hint="Optional: install fzf for long interactive selector lists.",
        ),
        _command_dependency_check(
            command_name="less",
            required=False,
            key="dependency_less",
            ok_detail="less available for paged diff review fallback",
            missing_detail="less is not installed",
            missing_hint="Optional: install less for better paged diff review fallback.",
        ),
        _command_dependency_check(
            command_name="sudo",
            required=False,
            key="dependency_sudo",
            ok_detail="sudo available for privileged operations",
            missing_detail="sudo is not installed",
            missing_hint="Optional but recommended: install sudo if you want dotman to manage protected system paths.",
        ),
        _editor_dependency_check(),
    ]


def _command_dependency_check(
    *,
    command_name: str,
    required: bool,
    key: str,
    ok_detail: str,
    missing_detail: str,
    missing_hint: str,
) -> DoctorCheck:
    command_path = shutil.which(command_name)
    if command_path is None:
        return DoctorCheck(
            key=key,
            status="failed" if required else "warn",
            detail=missing_detail,
            hint=missing_hint,
        )
    return DoctorCheck(
        key=key,
        status="ok",
        detail=ok_detail,
        path=Path(command_path),
    )


def _editor_dependency_check() -> DoctorCheck:
    try:
        editor_command = resolve_editor_command()
    except ValueError as exc:
        return DoctorCheck(
            key="editor",
            status="warn",
            detail=str(exc),
            hint="Fix the first editor setting found among $VISUAL, $EDITOR, $GIT_EDITOR and git's core.editor.",
        )
    if editor_command is None:
        return DoctorCheck(
            key="editor",
            status="warn",
            detail="no editor configured",
            hint="Optional: set $VISUAL or $EDITOR, or install an editor such as nano, for add/edit/reconcile review flows.",
        )
    editor_path = shutil.which(editor_command[0])
    if editor_path is None:
        return DoctorCheck(
            key="editor",
            status="warn",
            detail=f"configured editor '{editor_command[0]}' was not found",
            hint="Install that editor or point $VISUAL or $EDITOR at an installed one.",
        )
    return DoctorCheck(
        key="editor",
        status="ok",
        detail=f"editor configured: {editor_command[0]}",
        path=Path(editor_path),
    )


def _doctor_repo_checks(repo: Any) -> list[DoctorCheck]:
    checks: list[DoctorCheck] = []
    repo_path = repo.config.path
    if not repo_path.exists():
        return [
            DoctorCheck(
                key="repo_path",
                status="failed",
                detail="repo path does not exist",
                path=repo_path,
                repo_name=repo.config.name,
                hint=f"Fix [repos.{repo.config.name}].path or create the repo directory.",
            )
        ]
    if not repo_path.is_dir():
        return [
            DoctorCheck(
                key="repo_path",
                status="failed",
                detail="repo path is not a directory",
                path=repo_path,
                repo_name=repo.config.name,
                hint=f"Fix [repos.{repo.config.name}].path so it points to a repo directory.",
            )
        ]

    checks.append(
        DoctorCheck(
            key="repo_path",
            status="ok",
            detail="repo path is a directory",
            path=repo_path,
            repo_name=repo.config.name,
        )
    )

    profiles_root = repo_path / "profiles"
    if not repo.profiles:
        checks.append(
            DoctorCheck(
                key="profiles",
                status="failed",
                detail="repo defines no profiles",
                path=profiles_root,
                repo_name=repo.config.name,
                hint="Add at least one profile file under profiles/, for example profiles/default.toml.",
            )
        )
    else:
        checks.append(
            DoctorCheck(
                key="profiles",
                status="ok",
                detail=f"found {len(repo.profiles)} profile(s)",
                path=profiles_root,
                repo_name=repo.config.name,
            )
        )

    state_dir = repo.config.state_path
    if state_dir.exists() and not state_dir.is_dir():
        checks.append(
            DoctorCheck(
                key="state_dir",
                status="failed",
                detail="state path exists but is not a directory",
                path=state_dir,
                repo_name=repo.config.name,
                hint=f"Move or remove this path so dotman can create state under {state_dir}.",
            )
        )
    else:
        checks.append(
            DoctorCheck(
                key="state_dir",
                status="ok",
                detail="state path is available",
                path=state_dir,
                repo_name=repo.config.name,
            )
        )

    return checks


def _read_configured_package_entries_for_doctor(
    repo: Repository,
) -> tuple[list[FullSpecSelector] | None, list[DoctorCheck]]:
    state_path = repo.config.state_path / "tracked-packages.toml"
    try:
        bindings = read_tracked_package_entries_file(state_path)
    except TrackedPackagesFileError as exc:
        return None, [
            DoctorCheck(
                key="tracked_packages_file",
                status="failed",
                detail=f"tracked packages file is malformed: {exc.detail}",
                path=exc.path,
                repo_name=repo.config.name,
                hint="Fix or remove the broken tracked-packages.toml file.",
            )
        ]
    except OSError as exc:
        return None, [
            DoctorCheck(
                key="tracked_packages_file",
                status="failed",
                detail=f"tracked packages file could not be read: {exc.strerror or exc}",
                path=state_path,
                repo_name=repo.config.name,
                hint="Fix the filesystem path or remove the broken tracked-packages.toml file.",
            )
        ]

    return bindings, [
        DoctorCheck(
            key="tracked_packages_file",
            status="ok",
            detail=(
                "tracked packages file is readable"
                if state_path.exists()
                else "tracked packages file not present yet"
            ),
            path=state_path,
            repo_name=repo.config.name,
        )
    ]


def _collect_orphan_package_entry_issues(
    context: TrackedStateContext,
) -> tuple[list[TrackedPackageEntryIssue], list[DoctorCheck]]:
    state_root = context.state_root / "repos"
    if not state_root.exists():
        return [], []

    configured_state_keys = {repo_config.state_key for repo_config in context.config.ordered_repos}
    orphan_issues: list[TrackedPackageEntryIssue] = []
    checks: list[DoctorCheck] = []
    for state_dir in sorted(path for path in state_root.iterdir() if path.is_dir()):
        if state_dir.name in configured_state_keys:
            continue
        state_path = state_dir / "tracked-packages.toml"
        if not state_path.exists():
            continue
        try:
            bindings = read_tracked_package_entries_file(state_path)
        except TrackedPackagesFileError as exc:
            checks.append(
                DoctorCheck(
                    key="orphan_tracked_packages_file",
                    status="failed",
                    detail=f"orphan tracked packages file is malformed: {exc.detail}",
                    path=exc.path,
                    repo_name=None,
                    hint="Fix or remove this orphan tracked-packages.toml file, or add matching repo config.",
                )
            )
            continue
        except OSError as exc:
            checks.append(
                DoctorCheck(
                    key="orphan_tracked_packages_file",
                    status="failed",
                    detail=f"orphan tracked packages file could not be read: {exc.strerror or exc}",
                    path=state_path,
                    repo_name=None,
                    hint="Fix or remove this orphan tracked-packages.toml file, or add matching repo config.",
                )
            )
            continue

        for binding in bindings:
            orphan_issues.append(
                TrackedPackageEntryIssue(
                    state_key=state_dir.name,
                    repo=binding.repo,
                    selector=binding.selector,
                    profile=binding.profile,
                    state="orphan",
                    reason="unknown_repo",
                    message="repo not in config",
                )
            )

    return orphan_issues, checks


def _doctor_snapshot_checks(context: TrackedStateContext) -> list[DoctorCheck]:
    snapshot_path = context.config.snapshots.path
    if not context.config.snapshots.enabled:
        return [
            DoctorCheck(
                key="snapshots",
                status="ok",
                detail="snapshots are disabled",
                path=snapshot_path,
            )
        ]
    if snapshot_path.exists() and not snapshot_path.is_dir():
        return [
            DoctorCheck(
                key="snapshots",
                status="failed",
                detail="snapshot path exists but is not a directory",
                path=snapshot_path,
                hint="Fix snapshots.path so it points to a directory, or remove the blocking file.",
            )
        ]
    return [
        DoctorCheck(
            key="snapshots",
            status="ok",
            detail="snapshot path is available",
            path=snapshot_path,
        )
    ]
