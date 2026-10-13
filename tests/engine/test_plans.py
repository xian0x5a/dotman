from __future__ import annotations

import json
from pathlib import Path

import pytest

from dotman.command_runtime import CommandRequest, CommandResult, ShellCommand
from dotman.engine import DotmanEngine
from dotman.models import HookCommandSpec
from dotman.sync_base_store import FilePresent
from dotman.sync_observation import Observation
from dotman.sync_session import SyncResult
from tests.helpers import (
    initialize_git_repository,
    open_tracked_pull_session,
    write_manager_config,
    write_single_repo_config,
    write_tracked_packages_state,
)


def write_sync_policy_repo(
    tmp_path: Path,
    *,
    package_manifest: list[str],
    target_manifest: list[str] | None = None,
    hook_manifest: list[str] | None = None,
    package_id: str = "app",
) -> Path:
    repo_root = tmp_path / "repo"
    (repo_root / "profiles").mkdir(parents=True)
    (repo_root / "packages" / package_id / "files").mkdir(parents=True)
    (repo_root / "profiles" / "default.toml").write_text("", encoding="utf-8")
    (repo_root / "packages" / package_id / "files" / "config.txt").write_text("config\n", encoding="utf-8")
    (repo_root / "packages" / package_id / "package.toml").write_text(
        "\n".join(
            [
                f'id = "{package_id}"',
                *package_manifest,
                *(hook_manifest or []),
                "",
                "[targets.config]",
                'source = "files/config.txt"',
                'path = "~/.config/app/config.txt"',
                *(target_manifest or []),
                "",
            ]
        ),
        encoding="utf-8",
    )
    return repo_root


def write_sync_policy_repo_with_extends(
    tmp_path: Path,
    *,
    base_manifest: list[str],
    child_manifest: list[str],
) -> Path:
    repo_root = tmp_path / "repo"
    (repo_root / "profiles").mkdir(parents=True)
    (repo_root / "packages" / "base" / "files").mkdir(parents=True)
    (repo_root / "packages" / "child" / "files").mkdir(parents=True)
    (repo_root / "profiles" / "default.toml").write_text("", encoding="utf-8")
    (repo_root / "packages" / "base" / "files" / "config.txt").write_text("base\n", encoding="utf-8")
    (repo_root / "packages" / "child" / "files" / "config.txt").write_text("child\n", encoding="utf-8")
    (repo_root / "packages" / "base" / "package.toml").write_text(
        "\n".join(
            [
                'id = "base"',
                *base_manifest,
                "",
                "[targets.config]",
                'source = "files/config.txt"',
                'path = "~/.config/base/config.txt"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    (repo_root / "packages" / "child" / "package.toml").write_text(
        "\n".join(
            [
                'id = "child"',
                'extends = ["base"]',
                *child_manifest,
                "",
            ]
        ),
        encoding="utf-8",
    )
    return repo_root


def write_hook_metadata_repo(
    tmp_path: Path,
    *,
    package_manifest: list[str],
    package_id: str = "app",
) -> Path:
    repo_root = tmp_path / "repo"
    (repo_root / "profiles").mkdir(parents=True)
    (repo_root / "packages" / package_id).mkdir(parents=True)
    (repo_root / "profiles" / "default.toml").write_text("", encoding="utf-8")
    (repo_root / "packages" / package_id / "package.toml").write_text(
        "\n".join(
            [
                f'id = "{package_id}"',
                *package_manifest,
                "",
            ]
        ),
        encoding="utf-8",
    )
    return repo_root


def write_probe_repo(
    tmp_path: Path,
    *,
    probe_command: str,
    package_manifest: list[str] | None = None,
    target_manifest: list[str] | None = None,
) -> Path:
    repo_root = tmp_path / "repo"
    (repo_root / "profiles").mkdir(parents=True)
    (repo_root / "packages" / "app").mkdir(parents=True)
    (repo_root / "profiles" / "default.toml").write_text("", encoding="utf-8")
    (repo_root / "packages" / "app" / "package.toml").write_text(
        "\n".join(
            [
                'id = "app"',
                *(package_manifest or []),
                "",
                "[targets.version]",
                f"probe = {json.dumps(probe_command)}",
                'sync_policy = "push-only"',
                *(target_manifest or []),
                "",
            ]
        ),
        encoding="utf-8",
    )
    return repo_root


def write_repo_and_target_hook_repo(
    tmp_path: Path,
    *,
    repo_manifest: list[str] | None = None,
    package_manifest: list[str] | None = None,
    target_manifest: list[str] | None = None,
    child_manifest: list[str] | None = None,
) -> Path:
    repo_root = tmp_path / "repo"
    (repo_root / "profiles").mkdir(parents=True)
    (repo_root / "packages" / "app" / "files").mkdir(parents=True)
    (repo_root / "profiles" / "default.toml").write_text("", encoding="utf-8")
    (repo_root / "packages" / "app" / "files" / "config.txt").write_text("config\n", encoding="utf-8")
    if repo_manifest is not None:
        (repo_root / "repo.toml").write_text("\n".join([*repo_manifest, ""]), encoding="utf-8")
    (repo_root / "packages" / "app" / "package.toml").write_text(
        "\n".join(
            [
                'id = "app"',
                *(package_manifest or []),
                "",
                "[targets.config]",
                'source = "files/config.txt"',
                'path = "~/.config/app/config.txt"',
                *(target_manifest or []),
                "",
            ]
        ),
        encoding="utf-8",
    )
    if child_manifest is not None:
        (repo_root / "packages" / "child" / "files").mkdir(parents=True)
        (repo_root / "packages" / "child" / "files" / "config.txt").write_text("child\n", encoding="utf-8")
        (repo_root / "packages" / "child" / "package.toml").write_text(
            "\n".join(
                [
                    'id = "child"',
                    'extends = ["app"]',
                    *child_manifest,
                    "",
                ]
            ),
            encoding="utf-8",
        )
    return repo_root


def write_target_ref_repo(
    tmp_path: Path,
    *,
    alpha_manifest: list[str] | None = None,
    beta_manifest: list[str] | None = None,
    gamma_manifest: list[str] | None = None,
    include_alpha_shared_target: bool = True,
) -> Path:
    repo_root = tmp_path / "repo"
    (repo_root / "profiles").mkdir(parents=True)
    (repo_root / "profiles" / "default.toml").write_text("", encoding="utf-8")

    (repo_root / "packages" / "alpha" / "files").mkdir(parents=True)
    (repo_root / "packages" / "alpha" / "files" / "shared.conf").write_text("shared\n", encoding="utf-8")
    alpha_target_lines = [
        "",
        "[targets.shared]",
        'source = "files/shared.conf"',
        'path = "~/.config/shared.conf"',
    ] if include_alpha_shared_target else []
    (repo_root / "packages" / "alpha" / "package.toml").write_text(
        "\n".join(
            [
                'id = "alpha"',
                *(alpha_manifest or []),
                *alpha_target_lines,
                "",
            ]
        ),
        encoding="utf-8",
    )

    if beta_manifest is not None:
        (repo_root / "packages" / "beta").mkdir(parents=True)
        (repo_root / "packages" / "beta" / "package.toml").write_text(
            "\n".join(
                [
                    'id = "beta"',
                    *beta_manifest,
                    "",
                ]
            ),
            encoding="utf-8",
        )

    if gamma_manifest is not None:
        (repo_root / "packages" / "gamma").mkdir(parents=True)
        (repo_root / "packages" / "gamma" / "package.toml").write_text(
            "\n".join(
                [
                    'id = "gamma"',
                    *gamma_manifest,
                    "",
                ]
            ),
            encoding="utf-8",
        )

    return repo_root


def open_tracked_push_session(
    engine: DotmanEngine,
    tmp_path: Path,
    *,
    entries: list[tuple[str, str]],
    repo_name: str = "fixture",
    preview: bool = True,
):
    """Open the public Push seam from real tracked state."""
    write_tracked_packages_state(tmp_path / "state", repo_name=repo_name, entries=entries)
    return engine.open_push_session(engine.resolve_sync_scope(), preview=preview)


def execute_tracked_push(
    engine: DotmanEngine,
    tmp_path: Path,
    *,
    entries: list[tuple[str, str]],
    repo_name: str = "fixture",
) -> SyncResult:
    with open_tracked_push_session(engine, tmp_path, entries=entries, repo_name=repo_name, preview=False) as session:
        return session.execute().result


def observe_tracked_push(
    engine: DotmanEngine,
    tmp_path: Path,
    *,
    entries: list[tuple[str, str]],
    repo_name: str = "fixture",
) -> dict[str, Observation]:
    with open_tracked_push_session(engine, tmp_path, entries=entries, repo_name=repo_name) as session:
        return {unit.identity.canonical: unit for unit in session.view.observations}


class RecordingCommandRuntime:
    """Record every command request and succeed without running it (root elevation would need sudo)."""

    def __init__(self) -> None:
        self.requests: list[CommandRequest] = []

    def request_cancel(self) -> None:
        pass

    def check_cancelled(self) -> None:
        pass

    def run(self, request: CommandRequest) -> CommandResult:
        self.requests.append(request)
        return CommandResult(exit_code=0)


def write_sample_directory_repo(
    tmp_path: Path,
    target_lines: list[str],
    *,
    repo_children: dict[str, str] | None = None,
    live_children: dict[str, str] | None = None,
    repo_file: bool = False,
    live_directory_link: bool = False,
) -> tuple[Path, Path]:
    """Write fixture:sample with target `config` at ~/.config/sample; return repo root and real live directory."""
    repo_root = tmp_path / "repo"
    (repo_root / "profiles").mkdir(parents=True)
    (repo_root / "profiles" / "default.toml").write_text("", encoding="utf-8")
    package_root = repo_root / "packages" / "sample"
    package_root.mkdir(parents=True)
    (package_root / "package.toml").write_text(
        "\n".join(['id = "sample"', "", "[targets.config]", 'source = "files/config"', 'path = "~/.config/sample"', *target_lines, ""]),
        encoding="utf-8",
    )
    source_path = package_root / "files" / "config"
    if repo_file:
        source_path.parent.mkdir(parents=True)
        source_path.write_text("repo file\n", encoding="utf-8")
    for name, text in (repo_children or {}).items():
        source_path.mkdir(parents=True, exist_ok=True)
        (source_path / name).write_text(text, encoding="utf-8")
    live_path = Path.home() / ".config" / "sample"
    live_root = live_path.parent / "real-sample" if live_directory_link else live_path
    if live_children is not None:
        live_root.mkdir(parents=True)
        for name, text in live_children.items():
            (live_root / name).write_text(text, encoding="utf-8")
    if live_directory_link:
        live_path.symlink_to(live_root, target_is_directory=True)
    return repo_root, live_root


def read_tree(root: Path) -> dict[str, str]:
    return {str(path.relative_to(root)): path.read_text(encoding="utf-8") for path in sorted(root.rglob("*")) if path.is_file()}


def test_example_push_renders_package_defaults_profile_and_local_overrides(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    engine = DotmanEngine.from_config_path(write_manager_config(tmp_path))

    result = execute_tracked_push(engine, tmp_path, entries=[("git", "basic")], repo_name="example")

    assert result.status == "completed"
    assert [(step.scope_identity, step.status) for step in result.steps if step.action == "pre_push"] == [
        ("example:git", "ok"),
        ("example:git", "ok"),
    ]
    gitconfig = (home / ".gitconfig").read_text(encoding="utf-8")
    assert "name = Example User" in gitconfig
    assert "email = local@example.test" in gitconfig
    assert "editor = nvim" in gitconfig
    assert "[include]" not in gitconfig


def test_push_active_probe_runs_repo_package_and_target_hooks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    log = tmp_path / "hooks.log"
    repo_root = write_probe_repo(
        tmp_path,
        probe_command='test "$DOTMAN_PACKAGE_ID:$DOTMAN_TARGET_NAME:$DOTMAN_OPERATION" = "app:version:push"',
        package_manifest=[
            "[hooks]",
            f'pre_push = "echo package >> {log}"',
        ],
        target_manifest=[
            "[targets.version.hooks]",
            f'pre_push = "echo target >> {log}"',
        ],
    )
    (repo_root / "repo.toml").write_text(f'[hooks]\npre_push = "echo repo >> {log}"\n', encoding="utf-8")
    engine = DotmanEngine.from_config_path(write_single_repo_config(tmp_path, repo_name="fixture", repo_path=repo_root))

    with open_tracked_push_session(engine, tmp_path, entries=[("app", "default")], preview=False) as session:
        assert session.view.observations == ()
        assert [(row.kind, row.scope, row.included) for row in session.view.rows] == [
            ("probe", "fixture:app.version", True),
        ]
        assert session.execute().result.status == "completed"

    assert log.read_text(encoding="utf-8").splitlines() == ["repo", "package", "target"]


def test_probe_target_rejects_file_payload_fields(tmp_path: Path) -> None:
    repo_root = write_probe_repo(
        tmp_path,
        probe_command="exit 0",
        target_manifest=[
            'source = "files/config.txt"',
            'path = "~/.config/app/config.txt"',
        ],
    )

    with pytest.raises(ValueError, match="target 'version' uses probe and must not define: path, source"):
        DotmanEngine.from_config_path(write_single_repo_config(tmp_path, repo_name="fixture", repo_path=repo_root))


def test_probe_target_does_not_create_direct_collision_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    repo_root = tmp_path / "repo"
    probe_package_root = repo_root / "packages" / "probe-app"
    file_package_root = repo_root / "packages" / "file-app"
    (repo_root / "profiles").mkdir(parents=True)
    probe_package_root.mkdir(parents=True)
    (file_package_root / "files").mkdir(parents=True)
    (repo_root / "profiles" / "default.toml").write_text("", encoding="utf-8")
    (probe_package_root / "package.toml").write_text('id = "probe-app"\n\n[targets.version]\nprobe = "exit 0"\n', encoding="utf-8")
    (file_package_root / "files" / "config.txt").write_text("config\n", encoding="utf-8")
    # If probe targets entered path collision validation, their internal placeholder path
    # would be the probe package root and would conflict with this nested live path.
    (file_package_root / "package.toml").write_text(
        "\n".join(
            [
                'id = "file-app"',
                "",
                "[targets.config]",
                'source = "files/config.txt"',
                f"path = {json.dumps(str(probe_package_root / 'would-collide-if-probe-claimed-path'))}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    write_tracked_packages_state(tmp_path / "state", repo_name="fixture", entries=[("probe-app", "default"), ("file-app", "default")])
    engine = DotmanEngine.from_config_path(write_single_repo_config(tmp_path, repo_name="fixture", repo_path=repo_root))

    scope = engine.resolve_sync_scope()

    assert sorted(target.canonical for target in scope.targets) == ["fixture:file-app.config", "fixture:probe-app.version"]


def test_default_sync_policy_keeps_targets_in_push_and_pull(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    repo_root = write_sync_policy_repo(tmp_path, package_manifest=[])
    engine = DotmanEngine.from_config_path(write_single_repo_config(tmp_path, repo_name="fixture", repo_path=repo_root))
    initialize_git_repository(repo_root)

    with open_tracked_push_session(engine, tmp_path, entries=[("app", "default")]) as session:
        assert [(unit.identity.package_id, unit.identity.target_name, unit.configured_policy)
                for unit in session.view.observations] == [("app", "config", "both")]
    with open_tracked_pull_session(engine, tmp_path, entries=[("app", "default")]) as session:
        assert [(unit.identity.package_id, unit.identity.target_name, unit.configured_policy)
                for unit in session.view.observations] == [("app", "config", "both")]


def test_target_sync_policy_overrides_package_sync_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    repo_root = write_sync_policy_repo(
        tmp_path,
        package_manifest=['sync_policy = "push-only"'],
        target_manifest=['sync_policy = "pull-only"'],
    )
    engine = DotmanEngine.from_config_path(write_single_repo_config(tmp_path, repo_name="fixture", repo_path=repo_root))
    initialize_git_repository(repo_root)

    assert engine.get_repo("fixture").resolve_package("app").sync_policy == "push-only"
    with open_tracked_push_session(engine, tmp_path, entries=[("app", "default")]) as session:
        assert session.view.observations == ()
        assert session.view.rows == ()
    with open_tracked_pull_session(engine, tmp_path, entries=[("app", "default")]) as session:
        assert [(unit.identity.package_id, unit.identity.target_name, unit.configured_policy)
                for unit in session.view.observations] == [("app", "config", "pull-only")]


def test_package_hook_table_form_parses_run_noop_metadata(
    tmp_path: Path,
) -> None:
    repo_root = write_hook_metadata_repo(
        tmp_path,
        package_manifest=[
            "[hooks.pre_push]",
            'commands = ["echo pre", { run = "echo tty", io = "tty" }]',
            "run_noop = true",
        ],
    )

    engine = DotmanEngine.from_config_path(
        write_single_repo_config(tmp_path, repo_name="fixture", repo_path=repo_root)
    )

    hook = engine.get_repo("fixture").resolve_package("app").hooks["pre_push"]
    assert hook.commands == (
        HookCommandSpec(run="echo pre"),
        HookCommandSpec(run="echo tty", io="tty"),
    )
    assert hook.run_noop is True


def test_package_hook_shorthand_defaults_run_noop_false(
    tmp_path: Path,
) -> None:
    repo_root = write_hook_metadata_repo(
        tmp_path,
        package_manifest=[
            "[hooks]",
            'pre_push = ["echo pre"]',
        ],
    )

    engine = DotmanEngine.from_config_path(
        write_single_repo_config(tmp_path, repo_name="fixture", repo_path=repo_root)
    )

    hook = engine.get_repo("fixture").resolve_package("app").hooks["pre_push"]
    assert hook.commands == (HookCommandSpec(run="echo pre"),)
    assert hook.run_noop is False


def test_package_hook_shorthand_list_accepts_command_objects(
    tmp_path: Path,
) -> None:
    repo_root = write_hook_metadata_repo(
        tmp_path,
        package_manifest=[
            "[hooks]",
            'pre_push = ["echo pre", { run = "echo intercept", elevation = "intercept" }]',
        ],
    )

    engine = DotmanEngine.from_config_path(
        write_single_repo_config(tmp_path, repo_name="fixture", repo_path=repo_root)
    )

    hook = engine.get_repo("fixture").resolve_package("app").hooks["pre_push"]
    assert hook.commands == (
        HookCommandSpec(run="echo pre"),
        HookCommandSpec(run="echo intercept", elevation="intercept"),
    )
    assert hook.run_noop is False


def test_package_hook_command_object_parses_run_noop_metadata(
    tmp_path: Path,
) -> None:
    repo_root = write_hook_metadata_repo(
        tmp_path,
        package_manifest=[
            "[hooks]",
            'pre_push = ["echo normal", { run = "echo noop", run_noop = true }]',
        ],
    )

    engine = DotmanEngine.from_config_path(
        write_single_repo_config(tmp_path, repo_name="fixture", repo_path=repo_root)
    )

    hook = engine.get_repo("fixture").resolve_package("app").hooks["pre_push"]
    assert hook.commands == (
        HookCommandSpec(run="echo normal"),
        HookCommandSpec(run="echo noop", run_noop=True),
    )
    assert hook.run_noop is False


def test_package_hook_command_object_rejects_non_boolean_run_noop(
    tmp_path: Path,
) -> None:
    repo_root = write_hook_metadata_repo(
        tmp_path,
        package_manifest=[
            "[hooks]",
            'pre_push = [{ run = "echo bad", run_noop = "yes" }]',
        ],
    )

    with pytest.raises(ValueError, match="run_noop must be a boolean"):
        DotmanEngine.from_config_path(
            write_single_repo_config(tmp_path, repo_name="fixture", repo_path=repo_root)
        )


def test_push_hook_commands_keep_per_command_io_and_elevation(tmp_path: Path) -> None:
    repo_root = write_repo_and_target_hook_repo(
        tmp_path,
        repo_manifest=[
            'default_command_elevation = "intercept"',
            "[hooks.pre_push]",
            'commands = ["echo repo"]',
        ],
        package_manifest=[
            "[hooks.pre_push]",
            'commands = ["echo package", { run = "echo root", elevation = "root" }, { run = "echo tty", io = "tty" }]',
        ],
    )
    runtime = RecordingCommandRuntime()
    engine = DotmanEngine.from_config_path(
        write_single_repo_config(tmp_path, repo_name="fixture", repo_path=repo_root),
        command_runtime=runtime,
    )

    result = execute_tracked_push(engine, tmp_path, entries=[("app", "default")])

    assert [(request.command.source, request.io, request.elevation)
            for request in runtime.requests if isinstance(request.command, ShellCommand)] == [
        ("echo repo", "pipe", "intercept"),
        ("echo package", "pipe", "intercept"),
        ("echo root", "pipe", "root"),
    ]
    # Tests have no terminal, so a tty command proves its io reached execution by failing before it runs.
    assert [step.error for step in result.steps if step.status == "failed"] == [
        "hook command io 'tty' requires an interactive terminal",
    ]


@pytest.mark.parametrize("scope", ["repo", "package", "target"])
def test_push_noop_target_runs_only_noop_eligible_hook_commands(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scope: str,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    log = tmp_path / "hooks.log"
    hook_lines = [
        f"[{'targets.config.' if scope == 'target' else ''}hooks.pre_push]",
        f'commands = ["echo normal >> {log}", {{ run = "echo noop >> {log}", run_noop = true }}]',
    ]
    repo_root = write_repo_and_target_hook_repo(
        tmp_path,
        repo_manifest=hook_lines if scope == "repo" else None,
        package_manifest=hook_lines if scope != "repo" else None,
    )
    live_path = home / ".config" / "app" / "config.txt"
    live_path.parent.mkdir(parents=True)
    live_path.write_text("config\n", encoding="utf-8")
    engine = DotmanEngine.from_config_path(write_single_repo_config(tmp_path, repo_name="fixture", repo_path=repo_root))

    assert execute_tracked_push(engine, tmp_path, entries=[("app", "default")]).status == "completed"

    assert log.read_text(encoding="utf-8").splitlines() == ["noop"]


@pytest.mark.parametrize("elevation", ["none", "root", "intercept"])
def test_package_hook_command_object_accepts_elevation_modes(
    tmp_path: Path,
    elevation: str,
) -> None:
    repo_root = write_hook_metadata_repo(
        tmp_path,
        package_manifest=[
            "[hooks.pre_push]",
            f'commands = [{{ run = "echo prep", elevation = "{elevation}" }}]',
        ],
    )
    engine = DotmanEngine.from_config_path(write_single_repo_config(tmp_path, repo_name="fixture", repo_path=repo_root))

    hook = engine.get_repo("fixture").resolve_package("app").hooks["pre_push"]
    assert hook.commands == (HookCommandSpec(run="echo prep", elevation=elevation),)


@pytest.mark.parametrize("default_elevation", ["none", "intercept"])
def test_repo_default_command_elevation_applies_to_omitted_command_elevation(
    tmp_path: Path,
    default_elevation: str,
) -> None:
    repo_root = write_repo_and_target_hook_repo(
        tmp_path,
        repo_manifest=[
            f'default_command_elevation = "{default_elevation}"',
            "[hooks.pre_push]",
            'commands = ["echo repo", { run = "echo repo object" }, { run = "echo explicit", elevation = "none" }]',
            "run_noop = true",
        ],
        package_manifest=[
            "[hooks.pre_push]",
            'commands = ["echo package", { run = "echo package root", elevation = "root" }]',
        ],
        target_manifest=[
            'editor = { run = "sh hooks/reconcile.sh" }',
            "[targets.config.hooks.pre_push]",
            'commands = ["echo target"]',
        ],
    )
    engine = DotmanEngine.from_config_path(write_single_repo_config(tmp_path, repo_name="fixture", repo_path=repo_root))

    repo = engine.get_repo("fixture")
    package = repo.resolve_package("app")
    target = package.targets["config"]
    assert [command.elevation for command in repo.hooks["pre_push"].commands] == [default_elevation, default_elevation, "none"]
    assert [command.elevation for command in package.hooks["pre_push"].commands] == [default_elevation, "root"]
    assert [command.elevation for command in target.hooks["pre_push"].commands] == [default_elevation]
    assert target.editor.to_dict() == {
        "run": "sh hooks/reconcile.sh",
        "io": "tty",
        "elevation": default_elevation,
    }


@pytest.mark.parametrize("default_elevation", ["root", "bad"])
def test_repo_default_command_elevation_rejects_unsafe_or_unknown_modes(
    tmp_path: Path,
    default_elevation: str,
) -> None:
    repo_root = write_repo_and_target_hook_repo(
        tmp_path,
        repo_manifest=[f'default_command_elevation = "{default_elevation}"'],
    )

    with pytest.raises(
        ValueError,
        match=r"unsupported default_command_elevation '.+'; expected one of: none, intercept",
    ):
        DotmanEngine.from_config_path(write_single_repo_config(tmp_path, repo_name="fixture", repo_path=repo_root))


@pytest.mark.parametrize(
    ("package_manifest", "error_match"),
    [
        (
            [
                "[hooks.pre_push]",
                'commands = [{ run = "echo pre", nope = true }]',
            ],
            r"command object has unsupported keys: nope",
        ),
        (
            [
                "[hooks.pre_push]",
                'commands = [{ run = "   " }]',
            ],
            r"command object 'run' must not be empty",
        ),
        (
            [
                "[hooks.pre_push]",
                'commands = [{ run = "echo pre", io = "bad" }]',
            ],
            r"unsupported io 'bad'; expected one of: pipe, tty",
        ),
        (
            [
                "[hooks.pre_push]",
                'commands = [{ run = "echo pre", elevation = "bad" }]',
            ],
            r"unsupported elevation 'bad'; expected one of: none, root, intercept",
        ),
        (
            [
                "[hooks]",
                'pre_push = ["echo pre", { run = "echo tty", nope = true }]',
            ],
            r"command object has unsupported keys: nope",
        ),
    ],
)
def test_package_hook_command_objects_fail_fast_on_invalid_shapes(
    tmp_path: Path,
    package_manifest: list[str],
    error_match: str,
) -> None:
    repo_root = write_hook_metadata_repo(tmp_path, package_manifest=package_manifest)

    with pytest.raises(ValueError, match=error_match):
        DotmanEngine.from_config_path(write_single_repo_config(tmp_path, repo_name="fixture", repo_path=repo_root))


def test_package_hook_error_message_does_not_repeat_hook_word(
    tmp_path: Path,
) -> None:
    repo_root = write_hook_metadata_repo(
        tmp_path,
        package_manifest=[
            "[hooks]",
            "pre_push = [42]",
        ],
    )

    with pytest.raises(ValueError) as exc_info:
        DotmanEngine.from_config_path(write_single_repo_config(tmp_path, repo_name="fixture", repo_path=repo_root))

    message = str(exc_info.value)
    assert "hook hook" not in message
    assert "package hook 'pre_push' commands" in message


def test_repo_hook_error_message_names_repo_scope(
    tmp_path: Path,
) -> None:
    repo_root = write_repo_and_target_hook_repo(
        tmp_path,
        repo_manifest=[
            "[hooks]",
            "pre_push = [42]",
        ],
    )

    with pytest.raises(ValueError) as exc_info:
        DotmanEngine.from_config_path(write_single_repo_config(tmp_path, repo_name="fixture", repo_path=repo_root))

    message = str(exc_info.value)
    assert "hook hook" not in message
    assert "repo hook 'pre_push' commands" in message


def test_target_hook_error_message_names_target_scope(
    tmp_path: Path,
) -> None:
    repo_root = write_sync_policy_repo(
        tmp_path,
        package_manifest=[],
        target_manifest=[
            "[targets.config.hooks]",
            "pre_push = [42]",
        ],
    )

    with pytest.raises(ValueError) as exc_info:
        DotmanEngine.from_config_path(write_single_repo_config(tmp_path, repo_name="fixture", repo_path=repo_root))

    message = str(exc_info.value)
    assert "hook hook" not in message
    assert "target 'config' hook 'pre_push' commands" in message


def test_package_hook_table_form_allows_empty_commands_and_skips_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    repo_root = write_sync_policy_repo(
        tmp_path,
        package_manifest=[],
        hook_manifest=[
            "[hooks.post_push]",
            "commands = []",
            "run_noop = true",
        ],
    )
    engine = DotmanEngine.from_config_path(
        write_single_repo_config(tmp_path, repo_name="fixture", repo_path=repo_root)
    )

    hook = engine.get_repo("fixture").resolve_package("app").hooks["post_push"]
    assert hook.commands == ()
    assert hook.run_noop is True

    result = execute_tracked_push(engine, tmp_path, entries=[("app", "default")])
    assert result.status == "completed"
    assert [step.kind for step in result.steps if step.kind == "hook"] == []


def test_package_hook_empty_override_disables_inherited_hook(
    tmp_path: Path,
) -> None:
    repo_root = write_sync_policy_repo_with_extends(
        tmp_path,
        base_manifest=[
            "[hooks.pre_push]",
            'commands = ["echo base"]',
            "run_noop = true",
        ],
        child_manifest=[
            "[hooks.pre_push]",
            "commands = []",
            "run_noop = false",
        ],
    )

    engine = DotmanEngine.from_config_path(
        write_single_repo_config(tmp_path, repo_name="fixture", repo_path=repo_root)
    )

    hook = engine.get_repo("fixture").resolve_package("child").hooks["pre_push"]
    assert hook.commands == ()
    assert hook.run_noop is False


def test_package_inheritance_remove_and_append_patch_full_package_payload(
    tmp_path: Path,
) -> None:
    repo_root = write_sync_policy_repo_with_extends(
        tmp_path,
        base_manifest=[
            'description = "Base package"',
            'depends = ["base-dependency"]',
            'reserved_paths = ["~/.base"]',
            "[vars]",
            'remove_me = "base"',
            'keep_me = "base"',
            'extra = ["base"]',
            "[hooks.pre_push]",
            'commands = ["echo base"]',
        ],
        child_manifest=[
            'remove = ["description", "vars.remove_me", "targets.config"]',
            "[append]",
            'depends = ["child-dependency"]',
            'reserved_paths = ["~/.child"]',
            "[append.vars]",
            'extra = ["child"]',
            "[append.hooks]",
            'pre_push = ["echo child"]',
        ],
    )

    engine = DotmanEngine.from_config_path(
        write_single_repo_config(tmp_path, repo_name="fixture", repo_path=repo_root)
    )

    package = engine.get_repo("fixture").resolve_package("child")

    assert package.description is None
    assert package.depends == ("base-dependency", "child-dependency")
    assert package.reserved_paths == ("~/.base", "~/.child")
    assert package.vars == {"keep_me": "base", "extra": ["base", "child"]}
    assert package.targets == {}
    assert package.hooks["pre_push"].commands == (
        HookCommandSpec(run="echo base"),
        HookCommandSpec(run="echo child"),
    )


def test_package_inheritance_append_rejects_non_list_targets(
    tmp_path: Path,
) -> None:
    repo_root = write_sync_policy_repo_with_extends(
        tmp_path,
        base_manifest=['description = "Base package"'],
        child_manifest=[
            "[append]",
            'description = ["not a list field"]',
        ],
    )

    engine = DotmanEngine.from_config_path(
        write_single_repo_config(tmp_path, repo_name="fixture", repo_path=repo_root)
    )

    with pytest.raises(ValueError, match=r"append target 'description' is not a list"):
        engine.get_repo("fixture").resolve_package("child")


def test_package_hook_override_replaces_metadata_when_merging_extends(
    tmp_path: Path,
) -> None:
    repo_root = write_sync_policy_repo_with_extends(
        tmp_path,
        base_manifest=[
            "[hooks.pre_push]",
            'commands = ["echo base"]',
            "run_noop = false",
        ],
        child_manifest=[
            "[hooks.pre_push]",
            'commands = ["echo child"]',
            "run_noop = true",
        ],
    )

    engine = DotmanEngine.from_config_path(
        write_single_repo_config(tmp_path, repo_name="fixture", repo_path=repo_root)
    )

    hook = engine.get_repo("fixture").resolve_package("child").hooks["pre_push"]
    assert hook.commands == (HookCommandSpec(run="echo child"),)
    assert hook.run_noop is True


def test_repo_hook_table_form_parses_run_noop_metadata(
    tmp_path: Path,
) -> None:
    repo_root = write_repo_and_target_hook_repo(
        tmp_path,
        repo_manifest=[
            "[hooks.pre_push]",
            'commands = [{ run = "echo repo", io = "tty" }]',
            "run_noop = true",
        ],
    )

    engine = DotmanEngine.from_config_path(write_single_repo_config(tmp_path, repo_name="fixture", repo_path=repo_root))

    hook = engine.get_repo("fixture").hooks["pre_push"]
    assert hook.commands == (HookCommandSpec(run="echo repo", io="tty"),)
    assert hook.run_noop is True


def test_target_hook_table_form_parses_run_noop_metadata(
    tmp_path: Path,
) -> None:
    repo_root = write_repo_and_target_hook_repo(
        tmp_path,
        package_manifest=[
            "[targets.config.hooks.pre_push]",
            'commands = [{ run = "echo target", io = "tty" }]',
            "run_noop = true",
        ],
    )

    engine = DotmanEngine.from_config_path(write_single_repo_config(tmp_path, repo_name="fixture", repo_path=repo_root))

    hook = engine.get_repo("fixture").resolve_package("app").targets["config"].hooks["pre_push"]
    assert hook.commands == (HookCommandSpec(run="echo target", io="tty"),)
    assert hook.run_noop is True


def test_target_hook_override_replaces_metadata_when_merging_extends(
    tmp_path: Path,
) -> None:
    repo_root = write_repo_and_target_hook_repo(
        tmp_path,
        package_manifest=[
            "[targets.config.hooks.pre_push]",
            'commands = ["echo base target"]',
            "run_noop = false",
        ],
        child_manifest=[
            "[targets.config]",
            'source = "files/config.txt"',
            'path = "~/.config/child/config.txt"',
            "",
            "[targets.config.hooks.pre_push]",
            'commands = ["echo child target"]',
            "run_noop = true",
        ],
    )

    engine = DotmanEngine.from_config_path(write_single_repo_config(tmp_path, repo_name="fixture", repo_path=repo_root))

    hook = engine.get_repo("fixture").resolve_package("child").targets["config"].hooks["pre_push"]
    assert hook.commands == (HookCommandSpec(run="echo child target"),)
    assert hook.run_noop is True


def test_target_refs_manifest_is_rejected_like_other_unknown_keys(
    tmp_path: Path,
) -> None:
    repo_root = write_target_ref_repo(
        tmp_path,
        beta_manifest=[
            "[target_refs]",
            'shared = "alpha.shared"',
        ],
    )

    with pytest.raises(
        ValueError,
        match=r"package manifest .+ has unknown top-level keys: target_refs",
    ):
        DotmanEngine.from_config_path(write_single_repo_config(tmp_path, repo_name="fixture", repo_path=repo_root))


def test_package_sync_policy_is_inherited_through_extends(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    repo_root = write_sync_policy_repo_with_extends(
        tmp_path,
        base_manifest=['sync_policy = "pull-only"'],
        child_manifest=[],
    )
    engine = DotmanEngine.from_config_path(write_single_repo_config(tmp_path, repo_name="fixture", repo_path=repo_root))
    initialize_git_repository(repo_root)

    assert engine.get_repo("fixture").resolve_package("child").sync_policy == "pull-only"
    with open_tracked_push_session(engine, tmp_path, entries=[("child", "default")]) as session:
        assert session.view.observations == ()
    with open_tracked_pull_session(engine, tmp_path, entries=[("child", "default")]) as session:
        assert [(unit.identity.package_id, unit.identity.target_name, unit.configured_policy)
                for unit in session.view.observations] == [("child", "config", "pull-only")]


def test_push_only_delete_target_is_absent_from_pull(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    repo_root = write_sync_policy_repo(
        tmp_path,
        package_manifest=[],
        target_manifest=['sync_policy = "push-only-delete"'],
    )
    live_path = home / ".config" / "app" / "config.txt"
    live_path.parent.mkdir(parents=True)
    live_path.write_text("live\n", encoding="utf-8")
    engine = DotmanEngine.from_config_path(write_single_repo_config(tmp_path, repo_name="fixture", repo_path=repo_root))
    initialize_git_repository(repo_root)

    with open_tracked_pull_session(engine, tmp_path, entries=[("app", "default")]) as session:
        assert session.view.observations == ()


@pytest.mark.parametrize(
    ("package_manifest", "target_manifest"),
    [
        (['sync_policy = "sideways"'], None),
        ([], ['sync_policy = "sideways"']),
    ],
)
def test_sync_policy_rejects_invalid_values(
    tmp_path: Path,
    package_manifest: list[str],
    target_manifest: list[str] | None,
) -> None:
    repo_root = write_sync_policy_repo(
        tmp_path,
        package_manifest=package_manifest,
        target_manifest=target_manifest,
    )

    with pytest.raises(ValueError, match="sync_policy"):
        DotmanEngine.from_config_path(write_single_repo_config(tmp_path, repo_name="fixture", repo_path=repo_root))


def test_example_meta_package_push_expands_depends_and_renders_command_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    engine = DotmanEngine.from_config_path(write_manager_config(tmp_path))
    write_tracked_packages_state(tmp_path / "state", repo_name="example", entries=[("core-cli-meta", "basic")])

    assert [selection.package_id for selection in engine.resolve_sync_scope().package_selections] == [
        "git",
        "nvim",
        "core-cli-meta",
    ]
    observations = observe_tracked_push(engine, tmp_path, entries=[("core-cli-meta", "basic")], repo_name="example")
    assert sorted(observations) == ["example:git.gitconfig", "example:nvim.init_lua"]
    assert observations["example:nvim.init_lua"].comparison_repository == FilePresent(
        b'vim.g.mapleader = " "\nvim.cmd.colorscheme("industry")\n'
    )


def test_meta_package_depends_on_group_expands_group_members(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    repo_root = tmp_path / "fixture-repo"
    (repo_root / "profiles").mkdir(parents=True)
    (repo_root / "groups").mkdir(parents=True)
    (repo_root / "packages" / "tooling-meta").mkdir(parents=True)
    (repo_root / "packages" / "git" / "files").mkdir(parents=True)
    (repo_root / "packages" / "nvim" / "files").mkdir(parents=True)

    (repo_root / "profiles" / "basic.toml").write_text("", encoding="utf-8")
    (repo_root / "groups" / "tooling.toml").write_text('members = ["git", "nvim"]\n', encoding="utf-8")
    (repo_root / "packages" / "tooling-meta" / "package.toml").write_text(
        '\n'.join([
            'id = "tooling-meta"',
            'depends = ["tooling"]',
            '',
        ]),
        encoding="utf-8",
    )
    (repo_root / "packages" / "git" / "files" / "git.conf").write_text("git\n", encoding="utf-8")
    (repo_root / "packages" / "git" / "package.toml").write_text(
        '\n'.join([
            'id = "git"',
            '',
            '[targets.git]',
            'source = "files/git.conf"',
            'path = "~/.gitconfig"',
            '',
        ]),
        encoding="utf-8",
    )
    (repo_root / "packages" / "nvim" / "files" / "init.lua").write_text("nvim\n", encoding="utf-8")
    (repo_root / "packages" / "nvim" / "package.toml").write_text(
        '\n'.join([
            'id = "nvim"',
            '',
            '[targets.nvim]',
            'source = "files/init.lua"',
            'path = "~/.config/nvim/init.lua"',
            '',
        ]),
        encoding="utf-8",
    )
    write_tracked_packages_state(tmp_path / "state", repo_name="fixture", entries=[("tooling-meta", "basic")])

    engine = DotmanEngine.from_config_path(
        write_single_repo_config(tmp_path, repo_name="fixture", repo_path=repo_root)
    )

    scope = engine.resolve_sync_scope(["fixture:tooling-meta"])

    assert [selection.package_id for selection in scope.package_selections] == ["git", "nvim", "tooling-meta"]
    assert [target.canonical for target in scope.targets] == ["fixture:git.git", "fixture:nvim.nvim"]


def test_dependency_resolution_allows_mixed_package_and_group_cycles_without_revisiting_packages(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "fixture-repo"
    (repo_root / "profiles").mkdir(parents=True)
    (repo_root / "groups").mkdir(parents=True)
    (repo_root / "packages" / "alpha" / "files").mkdir(parents=True)
    (repo_root / "packages" / "beta" / "files").mkdir(parents=True)

    (repo_root / "profiles" / "basic.toml").write_text("", encoding="utf-8")
    (repo_root / "groups" / "bundle.toml").write_text('members = ["beta"]\n', encoding="utf-8")
    (repo_root / "packages" / "alpha" / "files" / "alpha.txt").write_text("alpha\n", encoding="utf-8")
    (repo_root / "packages" / "alpha" / "package.toml").write_text(
        '\n'.join([
            'id = "alpha"',
            'depends = ["bundle"]',
            '',
            '[targets.alpha]',
            'source = "files/alpha.txt"',
            'path = "~/.config/alpha.txt"',
            '',
        ]),
        encoding="utf-8",
    )
    (repo_root / "packages" / "beta" / "files" / "beta.txt").write_text("beta\n", encoding="utf-8")
    (repo_root / "packages" / "beta" / "package.toml").write_text(
        '\n'.join([
            'id = "beta"',
            'depends = ["alpha"]',
            '',
            '[targets.beta]',
            'source = "files/beta.txt"',
            'path = "~/.config/beta.txt"',
            '',
        ]),
        encoding="utf-8",
    )
    write_tracked_packages_state(tmp_path / "state", repo_name="fixture", entries=[("alpha", "basic")])

    engine = DotmanEngine.from_config_path(
        write_single_repo_config(tmp_path, repo_name="fixture", repo_path=repo_root)
    )

    scope = engine.resolve_sync_scope(["fixture:alpha"])

    assert [selection.package_id for selection in scope.package_selections] == ["beta", "alpha"]
    assert [target.canonical for target in scope.targets] == ["fixture:beta.beta", "fixture:alpha.alpha"]


def test_example_extends_preserves_child_values_after_local_merge(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    engine = DotmanEngine.from_config_path(write_manager_config(tmp_path))

    observation, = observe_tracked_push(engine, tmp_path, entries=[("work/git", "work")], repo_name="example").values()

    rendered = observation.comparison_repository.content.decode()
    assert "name = Work User" in rendered
    assert "email = local@example.test" in rendered
    assert "path = ~/.config/git/includes/work.inc" in rendered


def test_push_renders_directory_children_matched_by_path_rule(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    repo_root = tmp_path / "repo"
    package_root = repo_root / "packages" / "shell"
    (package_root / "files" / "profile").mkdir(parents=True)
    (repo_root / "profiles").mkdir()
    (package_root / "package.toml").write_text(
        "\n".join(
            [
                'id = "shell"',
                "",
                '[vars]',
                'greeting = "hello"',
                "",
                "[targets.profile]",
                'source = "files/profile"',
                'path = "~/.profile"',
                "",
                "[targets.profile.path_rules.templates]",
                'pattern = "*.tmpl"',
                'render = "jinja"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    (package_root / "files" / "profile" / "config.tmpl").write_text("greeting = {{ vars.greeting }}\n", encoding="utf-8")
    (repo_root / "profiles" / "default.toml").write_text("", encoding="utf-8")
    live_path = home / ".profile" / "config.tmpl"
    live_path.parent.mkdir(parents=True)
    live_path.write_text("greeting = world\n", encoding="utf-8")

    engine = DotmanEngine.from_config_path(write_single_repo_config(tmp_path, repo_name="fixture", repo_path=repo_root))

    assert execute_tracked_push(engine, tmp_path, entries=[("shell", "default")]).status == "completed"

    assert live_path.read_text(encoding="utf-8") == "greeting = hello\n"


def test_unknown_path_rule_preset_fails_engine_load(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    package_root = repo_root / "packages" / "shell"
    (package_root / "files" / "profile").mkdir(parents=True)
    (repo_root / "profiles").mkdir()
    (package_root / "package.toml").write_text(
        "\n".join(
            [
                'id = "shell"',
                "",
                "[targets.profile]",
                'source = "files/profile"',
                'path = "~/.profile"',
                "",
                "[targets.profile.path_rules.templates]",
                'pattern = "*.tmpl"',
                'preset = "missing"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    (repo_root / "profiles" / "default.toml").write_text("", encoding="utf-8")

    config_path = write_single_repo_config(tmp_path, repo_name="fixture", repo_path=repo_root)

    with pytest.raises(ValueError, match="path_rules\\.templates uses unknown preset 'missing'"):
        DotmanEngine.from_config_path(config_path)


def test_capture_patch_rejects_raw_review_views(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    repo_root = tmp_path / "repo"
    (repo_root / "packages" / "shell" / "files").mkdir(parents=True)
    (repo_root / "profiles").mkdir()
    (repo_root / "packages" / "shell" / "package.toml").write_text(
        "\n".join(
            [
                'id = "shell"',
                "",
                "[targets.profile]",
                'source = "files/profile"',
                'path = "~/.profile"',
                'render = "jinja"',
                'capture = "patch"',
                'compare = { repo = "raw", live = "raw" }',
                "",
            ]
        ),
        encoding="utf-8",
    )
    (repo_root / "packages" / "shell" / "files" / "profile").write_text(
        "{% include 'env.core.sh' %}\n",
        encoding="utf-8",
    )
    (repo_root / "packages" / "shell" / "files" / "env.core.sh").write_text(
        "export XDG_CONFIG_HOME=\"${XDG_CONFIG_HOME:-$HOME/.config}\"\n",
        encoding="utf-8",
    )
    (repo_root / "profiles" / "default.toml").write_text("", encoding="utf-8")
    (home / ".profile").write_text("export XDG_CONFIG_HOME=\"${XDG_CONFIG_HOME:-$HOME/.config}\"\n", encoding="utf-8")

    config_path = write_single_repo_config(tmp_path, repo_name="fixture", repo_path=repo_root)
    write_tracked_packages_state(tmp_path / "state", repo_name="fixture", entries=[("shell", "default")])

    # Patch Capture is checked on the effective target, once extends are merged.
    with pytest.raises(ValueError, match="compare.repo = \"render\" and compare.live = \"raw\""):
        DotmanEngine.from_config_path(config_path).resolve_sync_scope()


def test_capture_patch_requires_non_raw_render(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    repo_root = tmp_path / "repo"
    (repo_root / "packages" / "shell" / "files").mkdir(parents=True)
    (repo_root / "profiles").mkdir()
    (repo_root / "packages" / "shell" / "package.toml").write_text(
        "\n".join(
            [
                'id = "shell"',
                "",
                "[targets.profile]",
                'source = "files/profile"',
                'path = "~/.profile"',
                'capture = "patch"',
                'compare = { repo = "render", live = "raw" }',
                "",
            ]
        ),
        encoding="utf-8",
    )
    (repo_root / "packages" / "shell" / "files" / "profile").write_text("greeting = hello\n", encoding="utf-8")
    (repo_root / "profiles" / "default.toml").write_text("", encoding="utf-8")
    (home / ".profile").write_text("greeting = hello\n", encoding="utf-8")

    config_path = write_single_repo_config(tmp_path, repo_name="fixture", repo_path=repo_root)
    write_tracked_packages_state(tmp_path / "state", repo_name="fixture", entries=[("shell", "default")])

    # Patch Capture is checked on the effective target, once extends are merged.
    with pytest.raises(ValueError, match='capture = "patch" requires non-raw render'):
        DotmanEngine.from_config_path(config_path).resolve_sync_scope()


def test_unknown_target_preset_fails_engine_load(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    (repo_root / "packages" / "shell").mkdir(parents=True)
    (repo_root / "profiles").mkdir()
    (repo_root / "packages" / "shell" / "package.toml").write_text(
        "\n".join(
            [
                'id = "shell"',
                "",
                "[targets.profile]",
                'source = "files/profile"',
                'path = "~/.profile"',
                'preset = "missing"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    (repo_root / "profiles" / "default.toml").write_text("", encoding="utf-8")

    config_path = write_single_repo_config(tmp_path, repo_name="fixture", repo_path=repo_root)

    with pytest.raises(ValueError, match="unknown preset 'missing'"):
        DotmanEngine.from_config_path(config_path)


@pytest.mark.parametrize(
    ("target_lines", "source_text", "expected_live"),
    [
        # Jinja markers alone never opt a plain file into rendering.
        ([], "profile={{ profile }}\n", "profile={{ profile }}\n"),
        (['render = "jinja"'], "export SHELL_PROFILE=1\n{% include 'env.core.sh' %}\n", "export SHELL_PROFILE=1\nexport CORE_ENV=1\n"),
    ],
)
def test_push_renders_file_only_when_render_is_explicit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target_lines: list[str],
    source_text: str,
    expected_live: str,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    repo_root = tmp_path / "repo"
    (repo_root / "packages" / "shell" / "files").mkdir(parents=True)
    (repo_root / "profiles").mkdir()
    (repo_root / "packages" / "shell" / "package.toml").write_text(
        "\n".join(['id = "shell"', "", "[targets.profile]", 'source = "files/profile"', 'path = "~/.profile"', *target_lines, ""]),
        encoding="utf-8",
    )
    (repo_root / "packages" / "shell" / "files" / "profile").write_text(source_text, encoding="utf-8")
    (repo_root / "packages" / "shell" / "files" / "env.core.sh").write_text("export CORE_ENV=1\n", encoding="utf-8")
    (repo_root / "profiles" / "default.toml").write_text("", encoding="utf-8")
    engine = DotmanEngine.from_config_path(write_single_repo_config(tmp_path, repo_name="fixture", repo_path=repo_root))

    assert execute_tracked_push(engine, tmp_path, entries=[("shell", "default")]).status == "completed"

    assert (home / ".profile").read_text(encoding="utf-8") == expected_live


def test_sandbox_host_push_composes_profile_vars_and_namespaced_packages(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    engine = DotmanEngine.from_config_path(write_manager_config(tmp_path))
    write_tracked_packages_state(tmp_path / "state", repo_name="sandbox", entries=[("host/linux-meta", "host/linux")])

    scope = engine.resolve_sync_scope()
    assert {selection.identity.repo for selection in scope.package_selections} == {"sandbox"}
    assert "linux/1password" in {selection.package_id for selection in scope.package_selections}

    observations = observe_tracked_push(engine, tmp_path, entries=[("host/linux-meta", "host/linux")], repo_name="sandbox")
    # The host profile's desktop variable selects the niri-specific sunshine source.
    assert observations["sandbox:sunshine.selected_config"].repository_path.name == "sunshine-niri.conf"


def test_sandbox_nested_directory_and_file_targets_push_without_collision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    engine = DotmanEngine.from_config_path(write_manager_config(tmp_path))

    observations = observe_tracked_push(engine, tmp_path, entries=[("gsettings", "host/linux")], repo_name="sandbox")

    assert {canonical.split("/")[0].removeprefix("sandbox:gsettings.") for canonical in observations} == {
        "desktop",
        "nautilus",
        "gtk3_settings",
        "gtk4_settings",
    }
    assert "sandbox:gsettings.gtk3_dir/settings.ini" not in observations
    assert "sandbox:gsettings.gtk4_dir/settings.ini" not in observations


@pytest.mark.parametrize(
    ("target_lines", "repo_children", "live_children", "live_directory_link", "dir_symlink_mode", "expected_live"),
    [
        # Missing repo source: the live directory decides the target type and push deletes its children.
        ([], None, {"alpha.toml": "live"}, False, None, {}),
        # Both paths missing: nothing to publish.
        ([], None, None, False, None, None),
        (['type = "directory"'], None, None, False, None, None),
        # A followed live directory symlink is published through, with or without an explicit type.
        (['type = "directory"', 'sync_policy = "push-only-delete"'], None, {"old.txt": "old"}, True, "follow", {}),
        (['sync_policy = "push-only-delete"'], None, {"old.txt": "old"}, True, "follow", {}),
        # Existing trees publish per child: update, create, and delete.
        (
            [],
            {"alpha.toml": "repo alpha", "beta.toml": "repo beta"},
            {"alpha.toml": "live alpha", "gamma.toml": "live gamma"},
            False,
            None,
            {"alpha.toml": "repo alpha", "beta.toml": "repo beta"},
        ),
    ],
)
def test_push_directory_target_publishes_repository_tree(
    tmp_path: Path,
    target_lines: list[str],
    repo_children: dict[str, str] | None,
    live_children: dict[str, str] | None,
    live_directory_link: bool,
    dir_symlink_mode: str | None,
    expected_live: dict[str, str] | None,
) -> None:
    repo_root, live_root = write_sample_directory_repo(
        tmp_path,
        target_lines,
        repo_children=repo_children,
        live_children=live_children,
        live_directory_link=live_directory_link,
    )
    engine = DotmanEngine.from_config_path(
        write_single_repo_config(tmp_path, repo_name="fixture", repo_path=repo_root),
        dir_symlink_mode=dir_symlink_mode,
    )

    assert execute_tracked_push(engine, tmp_path, entries=[("sample", "default")]).status == "completed"

    assert (read_tree(live_root) if live_root.exists() else None) == expected_live
    assert read_tree(repo_root / "packages" / "sample" / "files") == {
        f"config/{name}": text for name, text in (repo_children or {}).items()
    }


def test_target_type_rejects_unknown_values(tmp_path: Path) -> None:
    repo_root, _live_root = write_sample_directory_repo(tmp_path, ['type = "socket"'])
    write_tracked_packages_state(tmp_path / "state", repo_name="fixture", entries=[("sample", "default")])

    with pytest.raises(ValueError, match="unsupported target type 'socket'"):
        DotmanEngine.from_config_path(
            write_single_repo_config(tmp_path, repo_name="fixture", repo_path=repo_root)
        ).resolve_sync_scope()


@pytest.mark.parametrize(
    ("target_lines", "repo_file", "live_children", "live_directory_link", "diagnostic_code"),
    [
        (['type = "directory"'], True, None, False, "unsupported-entry"),
        (['type = "file"'], True, {}, False, "unsupported-entry"),
        # Without follow mode a live directory symlink is not a directory endpoint.
        (['type = "directory"', 'sync_policy = "push-only-delete"'], False, {"old.txt": "old"}, True, "directory-symlink"),
    ],
)
def test_push_rejects_target_type_shape_mismatch(
    tmp_path: Path,
    target_lines: list[str],
    repo_file: bool,
    live_children: dict[str, str] | None,
    live_directory_link: bool,
    diagnostic_code: str,
) -> None:
    repo_root, live_root = write_sample_directory_repo(
        tmp_path,
        target_lines,
        repo_file=repo_file,
        live_children=live_children,
        live_directory_link=live_directory_link,
    )
    engine = DotmanEngine.from_config_path(write_single_repo_config(tmp_path, repo_name="fixture", repo_path=repo_root))

    with open_tracked_push_session(engine, tmp_path, entries=[("sample", "default")], preview=False) as session:
        row, = session.view.rows
        assert (row.row_id, row.kind, row.approved) == ("fixture:sample.config", "diagnostic", False)
        assert [diagnostic.code for diagnostic in row.observation.diagnostics if diagnostic.severity == "error"] == [diagnostic_code]
        session.execute()

    assert (read_tree(live_root) if live_root.exists() else None) == live_children


def test_sync_policy_resolution_survives_profile_and_local_context(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    repo_root = tmp_path / "repo"
    source_root = repo_root / "packages" / "app" / "files"
    source_root.mkdir(parents=True)
    (source_root / "config").write_text("config\n", encoding="utf-8")
    (repo_root / "profiles").mkdir()
    (repo_root / "profiles" / "work.toml").write_text("", encoding="utf-8")
    (repo_root / "local.example.toml").write_text('[vars]\nsuffix = "local"\n', encoding="utf-8")
    (repo_root / "packages" / "app" / "package.toml").write_text(
        '''id = "app"
sync_policy = "push-only"

[targets.config]
source = "files/config"
path = "~/.config/{{ profile }}-{{ suffix }}"
''',
        encoding="utf-8",
    )
    config_path = write_single_repo_config(tmp_path, repo_name="fixture", repo_path=repo_root)

    engine = DotmanEngine.from_config_path(config_path)
    initialize_git_repository(repo_root)

    observation, = observe_tracked_push(engine, tmp_path, entries=[("app", "work")]).values()
    assert observation.live_path == home / ".config" / "work-local"
    assert observation.configured_policy == "push-only"
    with open_tracked_pull_session(engine, tmp_path, entries=[("app", "work")]) as session:
        assert session.view.observations == ()
