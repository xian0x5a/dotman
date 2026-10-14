from __future__ import annotations

from pathlib import Path

from dotman.command_runtime import CommandResult, MemoryCommandRuntime, ShellCommand
from dotman.engine import DotmanEngine
from dotman.sync_base_store import FilePresent
from dotman.sync_session import AuxiliaryRow, SessionRow
from tests.helpers import write_single_repo_config, write_tracked_packages_state


def test_push_session_uses_injected_runtime_for_guard_probe_and_projection(
    tmp_path: Path,
    monkeypatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    repo_root = tmp_path / "repo"
    (repo_root / "profiles").mkdir(parents=True)
    (repo_root / "profiles" / "default.toml").write_text("", encoding="utf-8")
    package_root = repo_root / "packages" / "app"
    (package_root / "files").mkdir(parents=True)
    (package_root / "files" / "config.txt").write_text("source\n", encoding="utf-8")
    (package_root / "package.toml").write_text(
        "\n".join(
            [
                "",
                "[hooks]",
                'guard_push = "guard-command"',
                "",
                "[targets.available]",
                'probe = "probe-command"',
                "",
                "[targets.config]",
                'source = "files/config.txt"',
                'path = "~/.config/app/config.txt"',
                'render = "render-command"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    def respond(request) -> CommandResult:
        projected = request.command == ShellCommand("render-command")
        return CommandResult(exit_code=0, stdout=b"projected\n" if projected else b"")

    runtime = MemoryCommandRuntime([respond] * 3)
    config_path = write_single_repo_config(tmp_path, repo_name="fixture", repo_path=repo_root)
    write_tracked_packages_state(tmp_path / "state", repo_name="fixture", entries=[("app", "default")])
    engine = DotmanEngine.from_config_path(config_path, command_runtime=runtime)

    with engine.open_push_session(engine.resolve_sync_scope(), preview=True) as session:
        rows = session.view.rows
        observation, = session.view.observations

    # Guard gates the package, so it runs before any probe or projection.
    assert runtime.requests[0].command == ShellCommand("guard-command")
    assert {request.command for request in runtime.requests[1:]} == {
        ShellCommand("probe-command"),
        ShellCommand("render-command"),
    }
    assert runtime.requests[0].excluded_env_keys == frozenset({"DOTMAN_NO_CONFIRM"})
    assert [type(row) for row in rows] == [SessionRow, AuxiliaryRow]
    assert observation.comparison_repository == FilePresent(b"projected\n")
