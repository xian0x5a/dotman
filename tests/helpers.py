from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from dotman.cli import build_parser
from dotman.command_runtime import ArgvCommand, CommandRequest, current_command_runtime
from dotman.models import PackagePlan, ResolvedPackageIdentity, ResolvedPackageSelection


def write_example_local_override(tmp_path: Path, *, repo_name: str, repo_path: Path) -> None:
    example_local_path = repo_path / "local.example.toml"
    if not example_local_path.exists():
        return
    local_path = tmp_path / "xdg-config" / "dotman" / "repos" / repo_name / "local.toml"
    local_path.parent.mkdir(parents=True, exist_ok=True)
    local_path.write_text(example_local_path.read_text(encoding="utf-8"), encoding="utf-8")


# Shared repo builders live here so split test modules reuse identical setup code.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_REPO = PROJECT_ROOT / "examples" / "repo"
REFERENCE_REPO = PROJECT_ROOT / "tests" / "fixtures" / "reference_repo"


def write_tracked_packages_state(
    state_root: Path,
    *,
    repo_name: str,
    entries: list[tuple[str, str]],
) -> None:
    state_dir = state_root / "dotman" / "repos" / repo_name
    state_dir.mkdir(parents=True, exist_ok=True)
    lines = ["schema_version = 1", ""]
    for package_id, profile in entries:
        lines.extend(
            [
                "[[packages]]",
                f'repo = "{repo_name}"',
                f'package_id = "{package_id}"',
                f'profile = "{profile}"',
                "",
            ]
        )
    (state_dir / "tracked-packages.toml").write_text("\n".join(lines), encoding="utf-8")


def make_resolved_package_selection(
    *,
    repo_name: str,
    package_id: str,
    requested_profile: str,
    bound_profile: str | None = None,
    explicit: bool = True,
    source_kind: str = "selector_query",
    source_selector: str | None = None,
    owner_identity: ResolvedPackageIdentity | None = None,
) -> ResolvedPackageSelection:
    return ResolvedPackageSelection(
        identity=ResolvedPackageIdentity(
            repo=repo_name,
            package_id=package_id,
            bound_profile=bound_profile,
        ),
        requested_profile=requested_profile,
        explicit=explicit,
        source_kind=source_kind,
        source_selector=source_selector or package_id,
        owner_identity=owner_identity,
    )


def make_package_plan(
    *,
    operation: str,
    repo_name: str,
    package_id: str,
    requested_profile: str,
    bound_profile: str | None = None,
    explicit: bool = True,
    source_kind: str = "selector_query",
    source_selector: str | None = None,
    owner_identity: ResolvedPackageIdentity | None = None,
    hooks: dict | None = None,
    target_plans: list | None = None,
    repo_root: Path | None = None,
) -> PackagePlan:
    return PackagePlan(
        operation=operation,
        selection=make_resolved_package_selection(
            repo_name=repo_name,
            package_id=package_id,
            requested_profile=requested_profile,
            bound_profile=bound_profile,
            explicit=explicit,
            source_kind=source_kind,
            source_selector=source_selector,
            owner_identity=owner_identity,
        ),
        hooks=hooks or {},
        target_plans=target_plans or [],
        repo_root=repo_root,
    )


def capture_parser_help(capsys: pytest.CaptureFixture[str], *args: str) -> str:
    parser = build_parser()

    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args([*args, "--help"])

    assert exc_info.value.code == 0
    return capsys.readouterr().out


def write_named_manager_config(tmp_path: Path, repos: dict[str, Path]) -> Path:
    config_path = tmp_path / "config.toml"
    lines: list[str] = []
    for index, (repo_name, repo_path) in enumerate(repos.items(), start=1):
        lines.extend(
            [
                f"[repos.{repo_name}]",
                f'path = "{repo_path}"',
                f"order = {index * 10}",
                "",
            ]
        )
        write_example_local_override(tmp_path, repo_name=repo_name, repo_path=repo_path)
    config_path.write_text("\n".join(lines), encoding="utf-8")
    return config_path


SyncTarget = tuple[str, str, bytes | None, bytes | None, str]
"""(name, sync_policy, repository bytes or None, live bytes or None, extra target TOML)."""


def _write_sync_package(
    root: Path, package_id: str, targets: list[SyncTarget] | tuple[SyncTarget, ...], *,
    depends: tuple[str, ...] = (), package_extra: str = "",
) -> None:
    package = root / "repo" / "packages" / package_id
    package.mkdir(parents=True)
    lines = []
    if depends:
        lines.append("depends = [" + ", ".join(f'"{name}"' for name in depends) + "]")
    if package_extra:
        lines.append(package_extra)
    for name, policy, source, live, extra in targets:
        lines += [
            f"[targets.{name}]",
            f'source = "{name}"',
            f'path = "{root / "live" / name}"',
            'type = "file"',
            f'sync_policy = "{policy}"',
            extra,
        ]
        if source is not None:
            (package / name).write_bytes(source)
        if live is not None:
            (root / "live").mkdir(exist_ok=True)
            (root / "live" / name).write_bytes(live)
    (package / "package.toml").write_text("\n".join(lines))


def write_sync_repository(
    root: Path,
    targets: list[SyncTarget] | tuple[SyncTarget, ...],
    *,
    dependency_targets: list[SyncTarget] | tuple[SyncTarget, ...] = (),
    dependency_extra: str = "",
) -> Path:
    """Write a committed one-package repo, live files and tracked state; return the config path.

    Tracked state lands in `root/state`, so callers point XDG_STATE_HOME there.
    `dependency_targets` adds an untracked package `base` that the tracked `app` depends on;
    `dependency_extra` is package-level TOML for `base`, such as hooks.
    """
    repo = root / "repo"
    (repo / "profiles").mkdir(parents=True)
    (repo / "profiles" / "default.toml").write_text("")
    _write_sync_package(root, "app", targets, depends=("base",) if dependency_targets else ())
    if dependency_targets:
        _write_sync_package(root, "base", dependency_targets, package_extra=dependency_extra)
    runtime = current_command_runtime()
    for args in [
        ("init", "-q"),
        ("add", "."),
        ("-c", "user.name=Test", "-c", "user.email=test@example.test", "commit", "-qm", "fixture"),
    ]:
        result = runtime.run(CommandRequest(ArgvCommand(("git", *args)), cwd=repo))
        if result.exit_code != 0:
            raise RuntimeError(f"git {args[0]} failed: {result.stderr}")
    write_tracked_packages_state(root / "state", repo_name="main", entries=[("app", "default")])
    for directory in (root / "state/dotman", root / "state/dotman/repos", root / "state/dotman/repos/main"):
        directory.chmod(0o700)
    return write_named_manager_config(root, {"main": repo})


def write_manager_config(tmp_path: Path) -> Path:
    return write_named_manager_config(
        tmp_path,
        {
            "example": EXAMPLE_REPO,
            "sandbox": REFERENCE_REPO,
        },
    )


def write_named_manager_config_with_state_keys(
    tmp_path: Path,
    repos: dict[str, Path],
    *,
    state_keys: dict[str, str] | None = None,
) -> Path:
    config_path = tmp_path / "config.toml"
    lines: list[str] = []
    for index, (repo_name, repo_path) in enumerate(repos.items(), start=1):
        lines.extend(
            [
                f"[repos.{repo_name}]",
                f'path = "{repo_path}"',
                f"order = {index * 10}",
                (
                    f'state_key = "{state_keys[repo_name]}"'
                    if state_keys is not None and repo_name in state_keys
                    else ""
                ),
                "",
            ]
        )
        write_example_local_override(tmp_path, repo_name=repo_name, repo_path=repo_path)
    config_path.write_text("\n".join(lines), encoding="utf-8")
    return config_path


def write_single_repo_config(tmp_path: Path, *, repo_name: str, repo_path: Path) -> Path:
    return write_named_manager_config(tmp_path, {repo_name: repo_path})


def write_single_repo_config_with_state_key(
    tmp_path: Path,
    *,
    repo_name: str,
    repo_path: Path,
    state_key: str | None = None,
) -> Path:
    return write_named_manager_config_with_state_keys(
        tmp_path,
        {repo_name: repo_path},
        state_keys=None if state_key is None else {repo_name: state_key},
    )


def write_profile_switch_repo(repo_root: Path) -> None:
    (repo_root / "profiles").mkdir(parents=True)
    (repo_root / "packages" / "alpha" / "files").mkdir(parents=True)
    (repo_root / "packages" / "beta" / "files").mkdir(parents=True)
    (repo_root / "profiles" / "basic.toml").write_text("", encoding="utf-8")
    (repo_root / "profiles" / "work.toml").write_text("", encoding="utf-8")
    (repo_root / "packages" / "alpha" / "files" / "alpha.conf").write_text("alpha\n", encoding="utf-8")
    (repo_root / "packages" / "beta" / "files" / "beta.conf").write_text("beta\n", encoding="utf-8")
    (repo_root / "packages" / "alpha" / "package.toml").write_text(
        "\n".join(
            [
                "",
                "[targets.alpha]",
                'source = "files/alpha.conf"',
                'path = "~/.config/{{ profile }}.conf"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    (repo_root / "packages" / "beta" / "package.toml").write_text(
        "\n".join(
            [
                "",
                "[targets.beta]",
                'source = "files/beta.conf"',
                'path = "~/.config/basic.conf"',
                "",
            ]
        ),
        encoding="utf-8",
    )


def write_implicit_conflict_repo(repo_root: Path) -> None:
    (repo_root / "profiles").mkdir(parents=True)
    (repo_root / "groups").mkdir(parents=True)
    (repo_root / "packages" / "alpha" / "files").mkdir(parents=True)
    (repo_root / "packages" / "beta" / "files").mkdir(parents=True)
    (repo_root / "packages" / "alpha-meta").mkdir(parents=True)
    (repo_root / "packages" / "beta-meta").mkdir(parents=True)
    (repo_root / "profiles" / "basic.toml").write_text("", encoding="utf-8")
    (repo_root / "groups" / "alpha-stack.toml").write_text('members = ["alpha-meta"]\n', encoding="utf-8")
    (repo_root / "groups" / "beta-stack.toml").write_text('members = ["beta-meta"]\n', encoding="utf-8")
    (repo_root / "packages" / "alpha" / "files" / "shared.conf").write_text("alpha\n", encoding="utf-8")
    (repo_root / "packages" / "beta" / "files" / "shared.conf").write_text("beta\n", encoding="utf-8")
    (repo_root / "packages" / "alpha" / "package.toml").write_text(
        "\n".join(
            [
                "",
                "[targets.shared]",
                'source = "files/shared.conf"',
                'path = "~/.config/shared.conf"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    (repo_root / "packages" / "beta" / "package.toml").write_text(
        "\n".join(
            [
                "",
                "[targets.shared]",
                'source = "files/shared.conf"',
                'path = "~/.config/shared.conf"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    (repo_root / "packages" / "alpha-meta" / "package.toml").write_text(
        "\n".join(
            [
                'depends = ["alpha"]',
                "",
            ]
        ),
        encoding="utf-8",
    )
    (repo_root / "packages" / "beta-meta" / "package.toml").write_text(
        "\n".join(
            [
                'depends = ["beta"]',
                "",
            ]
        ),
        encoding="utf-8",
    )


def write_untrack_conflict_repo(repo_root: Path) -> None:
    (repo_root / "profiles").mkdir(parents=True)
    (repo_root / "packages" / "shared" / "files").mkdir(parents=True)
    (repo_root / "packages" / "stack-a").mkdir(parents=True)
    (repo_root / "packages" / "stack-b").mkdir(parents=True)

    for profile_name in ("direct", "work", "personal"):
        (repo_root / "profiles" / f"{profile_name}.toml").write_text("", encoding="utf-8")

    (repo_root / "packages" / "shared" / "files" / "shared.conf").write_text(
        "profile={{ profile }}\n",
        encoding="utf-8",
    )
    (repo_root / "packages" / "shared" / "package.toml").write_text(
        "\n".join(
            [
                "",
                "[targets.shared]",
                'source = "files/shared.conf"',
                'path = "~/.config/shared.conf"',
                'render = "jinja"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    (repo_root / "packages" / "stack-a" / "package.toml").write_text(
        "\n".join(
            [
                'depends = ["shared"]',
                "",
            ]
        ),
        encoding="utf-8",
    )
    (repo_root / "packages" / "stack-b" / "package.toml").write_text(
        "\n".join(
            [
                'depends = ["shared"]',
                "",
            ]
        ),
        encoding="utf-8",
    )


def write_profile_ambiguous_dependency_repo(repo_root: Path, *, shared_binding_mode: str = "singleton") -> None:
    (repo_root / "profiles").mkdir(parents=True)
    (repo_root / "packages" / "shared" / "files").mkdir(parents=True)
    (repo_root / "packages" / "meta-a").mkdir(parents=True)
    (repo_root / "packages" / "meta-b").mkdir(parents=True)

    for profile_name in ("basic", "work"):
        (repo_root / "profiles" / f"{profile_name}.toml").write_text("", encoding="utf-8")

    (repo_root / "packages" / "shared" / "files" / "shared.conf").write_text(
        "profile={{ profile }}\n",
        encoding="utf-8",
    )
    shared_lines = []
    if shared_binding_mode != "singleton":
        shared_lines.append(f'binding_mode = "{shared_binding_mode}"')
    shared_lines.extend(
        [
            "",
            "[targets.shared]",
            'source = "files/shared.conf"',
            'path = "~/.config/shared/{{ profile }}.conf"',
            'render = "jinja"',
            # Multi-instance fixture shares one source template; pull would collide on that repo path.
            *(['sync_policy = "push-only"'] if shared_binding_mode != "singleton" else []),
            "",
        ]
    )
    (repo_root / "packages" / "shared" / "package.toml").write_text("\n".join(shared_lines), encoding="utf-8")

    for package_id in ("meta-a", "meta-b"):
        (repo_root / "packages" / package_id / "package.toml").write_text(
            "\n".join(
                [
                    'depends = ["shared"]',
                    "",
                ]
            ),
            encoding="utf-8",
        )


def write_multi_instance_repo(repo_root: Path) -> None:
    (repo_root / "profiles").mkdir(parents=True)
    (repo_root / "packages" / "profiled" / "files").mkdir(parents=True)
    (repo_root / "profiles" / "basic.toml").write_text("", encoding="utf-8")
    (repo_root / "profiles" / "work.toml").write_text("", encoding="utf-8")
    (repo_root / "packages" / "profiled" / "files" / "managed.conf").write_text(
        "profile={{ profile }}\n",
        encoding="utf-8",
    )
    # Shared source template is push-only: pull would make profile instances write
    # back into the same repo file, which strict ownership validation rejects.
    (repo_root / "packages" / "profiled" / "package.toml").write_text(
        "\n".join(
            [
                'binding_mode = "multi_instance"',
                "",
                "[targets.managed]",
                'source = "files/managed.conf"',
                'path = "~/.config/profiled/{{ profile }}.conf"',
                'render = "jinja"',
                'sync_policy = "push-only"',
                "",
            ]
        ),
        encoding="utf-8",
    )


def write_shared_stack_repo(repo_root: Path) -> None:
    (repo_root / "profiles").mkdir(parents=True)
    (repo_root / "packages" / "shared" / "files").mkdir(parents=True)
    (repo_root / "packages" / "shared-stack").mkdir(parents=True)
    (repo_root / "profiles" / "basic.toml").write_text("", encoding="utf-8")
    (repo_root / "packages" / "shared" / "files" / "shared.conf").write_text(
        "shared\n",
        encoding="utf-8",
    )
    (repo_root / "packages" / "shared" / "package.toml").write_text(
        "\n".join(
            [
                "",
                "[targets.shared]",
                'source = "files/shared.conf"',
                'path = "~/.config/shared.conf"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    (repo_root / "packages" / "shared-stack" / "package.toml").write_text(
        "\n".join(
            [
                'depends = ["shared"]',
                "",
            ]
        ),
        encoding="utf-8",
    )


def write_package_override_preview_repo(repo_root: Path) -> None:
    (repo_root / "profiles").mkdir(parents=True)
    (repo_root / "groups").mkdir(parents=True)
    (repo_root / "packages" / "alpha" / "files").mkdir(parents=True)
    (repo_root / "packages" / "beta" / "files").mkdir(parents=True)
    (repo_root / "packages" / "alpha-meta").mkdir(parents=True)
    (repo_root / "packages" / "beta-meta").mkdir(parents=True)

    (repo_root / "profiles" / "basic.toml").write_text("", encoding="utf-8")
    (repo_root / "groups" / "alpha-stack.toml").write_text('members = ["alpha-meta"]\n', encoding="utf-8")
    (repo_root / "groups" / "beta-stack.toml").write_text('members = ["beta-meta"]\n', encoding="utf-8")

    (repo_root / "packages" / "alpha" / "files" / "shared.conf").write_text("alpha shared\n", encoding="utf-8")
    (repo_root / "packages" / "alpha" / "files" / "extra.conf").write_text("alpha extra\n", encoding="utf-8")
    (repo_root / "packages" / "beta" / "files" / "shared.conf").write_text("beta shared\n", encoding="utf-8")
    (repo_root / "packages" / "beta" / "files" / "extra.conf").write_text("beta extra\n", encoding="utf-8")

    (repo_root / "packages" / "alpha" / "package.toml").write_text(
        "\n".join(
            [
                "",
                "[targets.shared]",
                'source = "files/shared.conf"',
                'path = "~/.config/shared.conf"',
                "",
                "[targets.extra]",
                'source = "files/extra.conf"',
                'path = "~/.config/extra.conf"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    (repo_root / "packages" / "beta" / "package.toml").write_text(
        "\n".join(
            [
                "",
                "[targets.shared]",
                'source = "files/shared.conf"',
                'path = "~/.config/shared.conf"',
                "",
                "[targets.extra]",
                'source = "files/extra.conf"',
                'path = "~/.config/extra.conf"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    (repo_root / "packages" / "alpha-meta" / "package.toml").write_text(
        "\n".join(
            [
                'depends = ["alpha"]',
                "",
            ]
        ),
        encoding="utf-8",
    )
    (repo_root / "packages" / "beta-meta" / "package.toml").write_text(
        "\n".join(
            [
                'depends = ["beta"]',
                "",
            ]
        ),
        encoding="utf-8",
    )


def open_tracked_pull_session(
    engine, tmp_path: Path, *, repo_name: str = "fixture",
    entries: list[tuple[str, str]] | None = None, selectors: list[str] | None = None,
    preview: bool = True,
):
    """Open the public Pull seam from real tracked state, never synthetic plans."""
    if entries is not None:
        write_tracked_packages_state(tmp_path / "state", repo_name=repo_name, entries=entries)
    return engine.open_pull_session(engine.resolve_sync_scope(selectors), preview=preview)


def initialize_git_repository(repo_root: Path) -> None:
    from dotman.command_runtime import ArgvCommand, CommandRequest, current_command_runtime

    for arguments in [
        ("init", "-q"),
        ("add", "."),
        ("-c", "user.name=Test", "-c", "user.email=test@example.test",
         "commit", "-qm", "fixture"),
    ]:
        result = current_command_runtime().run(
            CommandRequest(ArgvCommand(("git", *arguments)), cwd=repo_root)
        )
        assert result.exit_code == 0, result.stderr


def resolved_child_metadata(engine, child_path: str):
    """Effective projection settings for one child of the scope's only directory target."""
    from dotman.sync_directory import child_metadata
    from dotman.sync_observation import _resolve_inputs

    inputs, _ = _resolve_inputs(engine.resolve_sync_scope())
    (directory,) = [metadata for _item, metadata in inputs.values() if metadata.target.target_type == "directory"]
    return child_metadata(directory, child_path)


async def wait_until_idle(app, pilot) -> None:
    """Let the Sync Deck finish its background work, then settle the screen."""
    for _ in range(200):
        if not app.busy:
            break
        await asyncio.sleep(.01)
    assert not app.busy
    await pilot.pause()
