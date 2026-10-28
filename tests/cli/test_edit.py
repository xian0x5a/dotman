from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import dotman.cli_interaction as cli

from dotman.cli import main
from dotman.command_runtime import ArgvCommand, CommandResult, MemoryCommandRuntime
from dotman.interaction import ScriptedInteraction

from tests.helpers import write_named_manager_config


def _patch_cli_runtime(monkeypatch, fake_run) -> MemoryCommandRuntime:
    def run_editor(request):
        assert isinstance(request.command, ArgvCommand)
        completed = fake_run(list(request.command.arguments), False)
        return CommandResult(exit_code=completed.returncode)

    runtime = MemoryCommandRuntime([run_editor])
    monkeypatch.setattr(cli, "current_command_runtime", lambda: runtime)
    return runtime


def _write_edit_repo(repo_root: Path) -> None:
    (repo_root / "profiles").mkdir(parents=True, exist_ok=True)
    (repo_root / "packages" / "git" / "files").mkdir(parents=True, exist_ok=True)
    (repo_root / "packages" / "altgit" / "files").mkdir(parents=True, exist_ok=True)
    (repo_root / "packages" / "ssh" / "files" / "ssh").mkdir(parents=True, exist_ok=True)
    (repo_root / "packages" / "nvim" / "files" / "config" / "nvim").mkdir(parents=True, exist_ok=True)
    (repo_root / "packages" / "note" / "files").mkdir(parents=True, exist_ok=True)

    (repo_root / "profiles" / "basic.toml").write_text("", encoding="utf-8")
    (repo_root / "packages" / "git" / "package.toml").write_text(
        "\n".join(
            [
                "",
                "[targets.gitconfig]",
                'source = "files/gitconfig"',
                'path = "~/.gitconfig"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    (repo_root / "packages" / "git" / "files" / "gitconfig").write_text("[user]\n", encoding="utf-8")
    (repo_root / "packages" / "altgit" / "files" / "gitconfig").write_text("[user]\n", encoding="utf-8")
    (repo_root / "packages" / "ssh" / "files" / "ssh" / "config").write_text("Host *\n", encoding="utf-8")
    (repo_root / "packages" / "nvim" / "files" / "config" / "nvim" / "init.lua").write_text(
        "return {}\n",
        encoding="utf-8",
    )
    (repo_root / "packages" / "note" / "files" / "note.txt").write_text("note\n", encoding="utf-8")
    (repo_root / "packages" / "altgit" / "package.toml").write_text(
        "\n".join(
            [
                "",
                "[targets.gitconfig]",
                'source = "files/gitconfig"',
                'path = "~/.config/altgit/config"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    (repo_root / "packages" / "ssh" / "package.toml").write_text(
        "\n".join(
            [
                "",
                "[targets.ssh_dir]",
                'source = "files/ssh"',
                'path = "~/.ssh"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    (repo_root / "packages" / "nvim" / "package.toml").write_text(
        "\n".join(
            [
                "",
                "[targets.init_lua]",
                'source = "files/config/nvim/init.lua"',
                'path = "~/.config/nvim/init.lua"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    (repo_root / "packages" / "note" / "package.toml").write_text(
        "\n".join(
            [
                "",
                "[targets.note]",
                'source = "files/note.txt"',
                'path = "~/.config/note.txt"',
                "",
            ]
        ),
        encoding="utf-8",
    )


def _write_tracked_binding_states(
    state_root: Path,
    *,
    repo_name: str,
    bindings: list[tuple[str, str]],
) -> None:
    state_dir = state_root / "dotman" / "repos" / repo_name
    state_dir.mkdir(parents=True, exist_ok=True)
    lines = ["schema_version = 1", ""]
    for selector, profile in bindings:
        lines.extend(
            [
                "[[packages]]",
                f'repo = "{repo_name}"',
                f'package_id = "{selector}"',
                f'profile = "{profile}"',
                "",
            ]
        )
    (state_dir / "tracked-packages.toml").write_text("\n".join(lines), encoding="utf-8")


def _write_tracked_binding_state(state_root: Path, *, repo_name: str, selector: str, profile: str) -> None:
    _write_tracked_binding_states(
        state_root,
        repo_name=repo_name,
        bindings=[(selector, profile)],
    )


def test_edit_config_cli_prints_selected_config_path_when_no_editor_is_configured(
    tmp_path: Path,
    capsys,
) -> None:

    config_path = tmp_path / "broken.toml"
    config_path.write_text("not valid toml = [", encoding="utf-8")

    exit_code = main(["--config", str(config_path), "edit", "config"])

    assert exit_code == 0
    assert capsys.readouterr().out == f"No editor configured. Config path: {config_path.resolve()}\n"


def test_edit_config_cli_opens_selected_config_without_loading_it(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config_path = tmp_path / "broken.toml"
    config_path.write_text("not valid toml = [", encoding="utf-8")

    monkeypatch.setenv("EDITOR", "nvim -d")
    recorded: dict[str, object] = {}

    def fake_run(command: list[str], check: bool):
        recorded["command"] = command
        recorded["check"] = check
        return SimpleNamespace(returncode=0)

    _patch_cli_runtime(monkeypatch, fake_run)

    exit_code = main(["--config", str(config_path), "edit", "config"])

    assert exit_code == 0
    assert recorded["check"] is False
    assert recorded["command"] == ["nvim", str(config_path.resolve())]


def test_edit_repo_cli_prints_selected_repo_path_when_no_editor_is_configured(
    tmp_path: Path,
    capsys,
) -> None:

    repo_root = tmp_path / "repo"
    _write_edit_repo(repo_root)
    config_path = write_named_manager_config(tmp_path, {"fixture": repo_root})

    exit_code = main(["--config", str(config_path), "edit", "repo", "fixture"])

    assert exit_code == 0
    assert capsys.readouterr().out == f"No editor configured. Repo path: {repo_root.resolve()}\n"


def test_edit_repo_cli_opens_selected_repo_without_loading_repo_contents(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / "repo.toml").write_text("not valid toml = [", encoding="utf-8")
    config_path = write_named_manager_config(tmp_path, {"fixture": repo_root})

    monkeypatch.setenv("EDITOR", "nvim -d")
    recorded: dict[str, object] = {}

    def fake_run(command: list[str], check: bool):
        recorded["command"] = command
        recorded["check"] = check
        return SimpleNamespace(returncode=0)

    _patch_cli_runtime(monkeypatch, fake_run)

    exit_code = main(["--config", str(config_path), "edit", "repo", "fixture"])

    assert exit_code == 0
    assert recorded["check"] is False
    assert recorded["command"] == ["nvim", str(repo_root.resolve())]


def test_edit_repo_cli_rejects_non_exact_repo_in_non_interactive_mode(
    tmp_path: Path,
    capsys,
) -> None:
    alpha_root = tmp_path / "alpha"
    beta_root = tmp_path / "beta"
    _write_edit_repo(alpha_root)
    _write_edit_repo(beta_root)
    config_path = write_named_manager_config(tmp_path, {"alpha": alpha_root, "beta": beta_root})

    exit_code = main(["--config", str(config_path), "edit", "repo", "bet"])

    assert exit_code == 1
    assert "edit repo 'bet' is not exact; use 'beta'" in capsys.readouterr().err


def test_edit_local_cli_prints_repo_local_override_path_when_no_editor_is_configured(
    tmp_path: Path,
    capsys,
) -> None:

    repo_root = tmp_path / "repo"
    _write_edit_repo(repo_root)
    config_path = write_named_manager_config(tmp_path, {"fixture": repo_root})
    local_path = tmp_path / "xdg-config" / "dotman" / "repos" / "fixture" / "local.toml"

    exit_code = main(["--config", str(config_path), "edit", "local", "fixture"])

    assert exit_code == 0
    assert capsys.readouterr().out == f"No editor configured. Local override path: {local_path}\n"


def test_edit_local_cli_defaults_to_only_repo_and_creates_parent_for_editor(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo_root = tmp_path / "repo"
    _write_edit_repo(repo_root)
    config_path = write_named_manager_config(tmp_path, {"fixture": repo_root})
    local_path = tmp_path / "xdg-config" / "dotman" / "repos" / "fixture" / "local.toml"
    assert not local_path.parent.exists()

    monkeypatch.setenv("EDITOR", "nvim -d")
    recorded: dict[str, object] = {}

    def fake_run(command: list[str], check: bool):
        recorded["command"] = command
        recorded["check"] = check
        recorded["parent_exists"] = local_path.parent.exists()
        return SimpleNamespace(returncode=0)

    _patch_cli_runtime(monkeypatch, fake_run)

    exit_code = main(["--config", str(config_path), "edit", "local"])

    assert exit_code == 0
    assert recorded["check"] is False
    assert recorded["parent_exists"] is True
    assert recorded["command"] == ["nvim", str(local_path)]


def test_edit_local_cli_opens_malformed_local_override_without_loading_it(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo_root = tmp_path / "repo"
    _write_edit_repo(repo_root)
    config_path = write_named_manager_config(tmp_path, {"fixture": repo_root})
    local_path = tmp_path / "xdg-config" / "dotman" / "repos" / "fixture" / "local.toml"
    local_path.parent.mkdir(parents=True, exist_ok=True)
    local_path.write_text("not valid toml = [", encoding="utf-8")

    monkeypatch.setenv("EDITOR", "nvim")
    recorded: dict[str, object] = {}

    def fake_run(command: list[str], check: bool):
        recorded["command"] = command
        recorded["check"] = check
        return SimpleNamespace(returncode=0)

    _patch_cli_runtime(monkeypatch, fake_run)

    exit_code = main(["--config", str(config_path), "edit", "local", "fixture"])

    assert exit_code == 0
    assert recorded["check"] is False
    assert recorded["command"] == ["nvim", str(local_path)]


def test_edit_local_cli_interactively_resolves_missing_repo(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    alpha_root = tmp_path / "alpha"
    beta_root = tmp_path / "beta"
    _write_edit_repo(alpha_root)
    _write_edit_repo(beta_root)
    config_path = write_named_manager_config(tmp_path, {"alpha": alpha_root, "beta": beta_root})
    beta_local_path = tmp_path / "xdg-config" / "dotman" / "repos" / "beta" / "local.toml"

    interaction = ScriptedInteraction(choices=["beta"])
    monkeypatch.setenv("EDITOR", "nvim")
    recorded: dict[str, object] = {}

    def fake_run(command: list[str], check: bool):
        recorded["command"] = command
        recorded["check"] = check
        return SimpleNamespace(returncode=0)

    _patch_cli_runtime(monkeypatch, fake_run)

    exit_code = main(
        ["--config", str(config_path), "edit", "local"],
        interaction=interaction,
    )

    assert exit_code == 0
    assert interaction.requests[0].header_text == "Select a repo for local overrides:"
    assert recorded["command"] == ["nvim", str(beta_local_path)]


def test_edit_local_cli_interactively_prompts_for_unique_partial_repo(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    alpha_root = tmp_path / "alpha"
    beta_root = tmp_path / "beta"
    _write_edit_repo(alpha_root)
    _write_edit_repo(beta_root)
    config_path = write_named_manager_config(tmp_path, {"alpha": alpha_root, "beta": beta_root})
    beta_local_path = tmp_path / "xdg-config" / "dotman" / "repos" / "beta" / "local.toml"

    interaction = ScriptedInteraction(choices=["beta"])
    monkeypatch.setenv("EDITOR", "nvim")
    recorded: dict[str, object] = {}

    def fake_run(command: list[str], check: bool):
        recorded["command"] = command
        recorded["check"] = check
        return SimpleNamespace(returncode=0)

    _patch_cli_runtime(monkeypatch, fake_run)

    exit_code = main(
        ["--config", str(config_path), "edit", "local", "bet"],
        interaction=interaction,
    )

    assert exit_code == 0
    assert interaction.requests[0].header_text == "Select a repo for local overrides:"
    assert recorded["command"] == ["nvim", str(beta_local_path)]


def test_edit_local_cli_rejects_unique_partial_repo_in_non_interactive_mode(
    tmp_path: Path,
    capsys,
) -> None:
    alpha_root = tmp_path / "alpha"
    beta_root = tmp_path / "beta"
    _write_edit_repo(alpha_root)
    _write_edit_repo(beta_root)
    config_path = write_named_manager_config(tmp_path, {"alpha": alpha_root, "beta": beta_root})

    exit_code = main(["--config", str(config_path), "edit", "local", "bet"])

    assert exit_code == 1
    assert "edit local repo 'bet' is not exact; use 'beta'" in capsys.readouterr().err


def test_edit_local_cli_interactively_resolves_ambiguous_repo(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    alpha_root = tmp_path / "alpha"
    alpine_root = tmp_path / "alpine"
    _write_edit_repo(alpha_root)
    _write_edit_repo(alpine_root)
    config_path = write_named_manager_config(tmp_path, {"alpha": alpha_root, "alpine": alpine_root})
    alpine_local_path = tmp_path / "xdg-config" / "dotman" / "repos" / "alpine" / "local.toml"

    interaction = ScriptedInteraction(choices=["alpine"])
    monkeypatch.setenv("EDITOR", "nvim")
    recorded: dict[str, object] = {}

    def fake_run(command: list[str], check: bool):
        recorded["command"] = command
        recorded["check"] = check
        return SimpleNamespace(returncode=0)

    _patch_cli_runtime(monkeypatch, fake_run)

    exit_code = main(
        ["--config", str(config_path), "edit", "local", "al"],
        interaction=interaction,
    )

    assert exit_code == 0
    assert interaction.requests[0].header_text == "Select a repo for local overrides:"
    assert recorded["command"] == ["nvim", str(alpine_local_path)]


def test_edit_local_cli_requires_repo_when_multiple_repos_in_non_interactive_mode(
    tmp_path: Path,
    capsys,
) -> None:
    alpha_root = tmp_path / "alpha"
    beta_root = tmp_path / "beta"
    _write_edit_repo(alpha_root)
    _write_edit_repo(beta_root)
    config_path = write_named_manager_config(tmp_path, {"alpha": alpha_root, "beta": beta_root})

    exit_code = main(["--config", str(config_path), "edit", "local"])

    assert exit_code == 1
    assert "edit local repo is required in non-interactive mode: alpha, beta" in capsys.readouterr().err


def test_edit_cli_prints_package_directory_when_no_editor_is_configured(
    tmp_path: Path,
    capsys,
) -> None:

    repo_root = tmp_path / "repo"
    _write_edit_repo(repo_root)
    config_path = write_named_manager_config(tmp_path, {"fixture": repo_root})
    _write_tracked_binding_state(tmp_path / "state", repo_name="fixture", selector="git", profile="basic")

    exit_code = main(["--config", str(config_path), "edit", "package", "git"])

    assert exit_code == 0
    assert (
        capsys.readouterr().out
        == f"No editor configured. Source path: {repo_root / 'packages' / 'git'}\n"
    )


def test_edit_cli_opens_tracked_package_directory_with_editor(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo_root = tmp_path / "repo"
    _write_edit_repo(repo_root)
    config_path = write_named_manager_config(tmp_path, {"fixture": repo_root})
    _write_tracked_binding_state(tmp_path / "state", repo_name="fixture", selector="git", profile="basic")

    monkeypatch.setenv("EDITOR", "nvim -d")
    recorded: dict[str, object] = {}

    def fake_run(command: list[str], check: bool):
        recorded["command"] = command
        recorded["check"] = check
        return SimpleNamespace(returncode=0)

    _patch_cli_runtime(monkeypatch, fake_run)

    exit_code = main(["--config", str(config_path), "edit", "package", "git"])

    assert exit_code == 0
    assert recorded["check"] is False
    assert recorded["command"] == ["nvim", str(repo_root / "packages" / "git")]


def test_edit_cli_sugar_opens_tracked_package_directory_with_editor(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo_root = tmp_path / "repo"
    _write_edit_repo(repo_root)
    config_path = write_named_manager_config(tmp_path, {"fixture": repo_root})
    _write_tracked_binding_state(tmp_path / "state", repo_name="fixture", selector="git", profile="basic")

    monkeypatch.setenv("EDITOR", "nvim -d")
    recorded: dict[str, object] = {}

    def fake_run(command: list[str], check: bool):
        recorded["command"] = command
        recorded["check"] = check
        return SimpleNamespace(returncode=0)

    _patch_cli_runtime(monkeypatch, fake_run)

    exit_code = main(["--config", str(config_path), "edit", "git"])

    assert exit_code == 0
    assert recorded["check"] is False
    assert recorded["command"] == ["nvim", str(repo_root / "packages" / "git")]


def test_edit_cli_sugar_keeps_repo_qualified_target_query_target_intent(
    tmp_path: Path,
    monkeypatch,
) -> None:
    alpha_root = tmp_path / "alpha"
    beta_root = tmp_path / "beta"
    _write_edit_repo(alpha_root)
    _write_edit_repo(beta_root)
    (beta_root / "packages" / "nvim" / "package.toml").write_text(
        "\n".join(
            [
                "",
                "[targets.init_lua]",
                'source = "files/config/nvim/init.lua"',
                'path = "~/.config/nvim-beta/init.lua"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    config_path = write_named_manager_config(tmp_path, {"alpha": alpha_root, "beta": beta_root})
    _write_tracked_binding_state(tmp_path / "state", repo_name="alpha", selector="nvim", profile="basic")
    _write_tracked_binding_state(tmp_path / "state", repo_name="beta", selector="nvim", profile="basic")

    monkeypatch.setenv("EDITOR", "nvim")
    recorded: dict[str, object] = {}

    def fake_run(command: list[str], check: bool):
        recorded["command"] = command
        recorded["check"] = check
        return SimpleNamespace(returncode=0)

    _patch_cli_runtime(monkeypatch, fake_run)

    exit_code = main(["--config", str(config_path), "edit", "beta:nvim.init_lua"])

    assert exit_code == 0
    assert recorded["command"] == [
        "nvim",
        str(beta_root / "packages" / "nvim" / "files" / "config" / "nvim" / "init.lua"),
    ]


def test_edit_target_cli_prints_repo_file_path_when_no_editor_is_configured(
    tmp_path: Path,
    capsys,
) -> None:

    repo_root = tmp_path / "repo"
    _write_edit_repo(repo_root)
    config_path = write_named_manager_config(tmp_path, {"fixture": repo_root})
    _write_tracked_binding_states(
        tmp_path / "state",
        repo_name="fixture",
        bindings=[("git", "basic"), ("ssh", "basic"), ("nvim", "basic"), ("altgit", "basic")],
    )

    exit_code = main(["--config", str(config_path), "edit", "target", "nvim.init_lua"])

    assert exit_code == 0
    assert (
        capsys.readouterr().out
        == "No editor configured. Source path: "
        f"{repo_root / 'packages' / 'nvim' / 'files' / 'config' / 'nvim' / 'init.lua'}\n"
    )


def test_edit_cli_sugar_rejects_cross_kind_exact_ambiguity(
    tmp_path: Path,
    capsys,
) -> None:
    repo_root = tmp_path / "repo"
    _write_edit_repo(repo_root)
    config_path = write_named_manager_config(tmp_path, {"fixture": repo_root})
    _write_tracked_binding_states(
        tmp_path / "state",
        repo_name="fixture",
        bindings=[("note", "basic")],
    )

    exit_code = main(["--config", str(config_path), "edit", "note"])

    assert exit_code == 1
    err = capsys.readouterr().err
    assert "edit query 'note' is ambiguous:" in err
    assert "package fixture:note" in err
    assert "target fixture:note.note" in err


def test_edit_cli_sugar_rejects_abbreviated_global_option(
    tmp_path: Path,
    capsys,
) -> None:
    config_path = write_named_manager_config(tmp_path, {"fixture": tmp_path / "repo"})

    with pytest.raises(SystemExit) as exit_info:
        main(["--conf", str(config_path), "edit", "git"])

    assert exit_info.value.code == 2
    err = capsys.readouterr().err
    assert "'query'" not in err


def test_edit_target_cli_opens_tracked_file_target_repo_path_with_editor(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo_root = tmp_path / "repo"
    _write_edit_repo(repo_root)
    config_path = write_named_manager_config(tmp_path, {"fixture": repo_root})
    _write_tracked_binding_states(
        tmp_path / "state",
        repo_name="fixture",
        bindings=[("git", "basic"), ("ssh", "basic"), ("nvim", "basic")],
    )

    monkeypatch.setenv("EDITOR", "nvim -d")
    recorded: dict[str, object] = {}

    def fake_run(command: list[str], check: bool):
        recorded["command"] = command
        recorded["check"] = check
        return SimpleNamespace(returncode=0)

    _patch_cli_runtime(monkeypatch, fake_run)

    exit_code = main(["--config", str(config_path), "edit", "target", "nvim.init_lua"])

    assert exit_code == 0
    assert recorded["check"] is False
    assert recorded["command"] == [
        "nvim",
        str(repo_root / "packages" / "nvim" / "files" / "config" / "nvim" / "init.lua"),
    ]


def test_edit_target_cli_opens_tracked_directory_target_repo_path_with_editor(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo_root = tmp_path / "repo"
    _write_edit_repo(repo_root)
    config_path = write_named_manager_config(tmp_path, {"fixture": repo_root})
    _write_tracked_binding_states(
        tmp_path / "state",
        repo_name="fixture",
        bindings=[("ssh", "basic")],
    )

    monkeypatch.setenv("EDITOR", "nvim")
    recorded: dict[str, object] = {}

    def fake_run(command: list[str], check: bool):
        recorded["command"] = command
        recorded["check"] = check
        return SimpleNamespace(returncode=0)

    _patch_cli_runtime(monkeypatch, fake_run)

    exit_code = main(["--config", str(config_path), "edit", "target", "ssh_dir"])

    assert exit_code == 0
    assert recorded["check"] is False
    assert recorded["command"] == ["nvim", str(repo_root / "packages" / "ssh" / "files" / "ssh")]


def test_edit_target_cli_resolves_repo_qualified_target_query(
    tmp_path: Path,
    monkeypatch,
) -> None:
    alpha_root = tmp_path / "alpha"
    beta_root = tmp_path / "beta"
    _write_edit_repo(alpha_root)
    _write_edit_repo(beta_root)
    (beta_root / "packages" / "nvim" / "package.toml").write_text(
        "\n".join(
            [
                "",
                "[targets.init_lua]",
                'source = "files/config/nvim/init.lua"',
                'path = "~/.config/nvim-beta/init.lua"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    config_path = write_named_manager_config(tmp_path, {"alpha": alpha_root, "beta": beta_root})
    _write_tracked_binding_state(tmp_path / "state", repo_name="alpha", selector="nvim", profile="basic")
    _write_tracked_binding_state(tmp_path / "state", repo_name="beta", selector="nvim", profile="basic")

    monkeypatch.setenv("EDITOR", "nvim")
    recorded: dict[str, object] = {}

    def fake_run(command: list[str], check: bool):
        recorded["command"] = command
        recorded["check"] = check
        return SimpleNamespace(returncode=0)

    _patch_cli_runtime(monkeypatch, fake_run)

    exit_code = main(["--config", str(config_path), "edit", "target", "beta:nvim.init_lua"])

    assert exit_code == 0
    assert recorded["command"] == [
        "nvim",
        str(beta_root / "packages" / "nvim" / "files" / "config" / "nvim" / "init.lua"),
    ]


def test_edit_target_cli_interactively_selects_ambiguous_target(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo_root = tmp_path / "repo"
    _write_edit_repo(repo_root)
    config_path = write_named_manager_config(tmp_path, {"fixture": repo_root})
    _write_tracked_binding_states(
        tmp_path / "state",
        repo_name="fixture",
        bindings=[("git", "basic"), ("altgit", "basic")],
    )

    git_path = repo_root / "packages" / "git" / "files" / "gitconfig"
    interaction = ScriptedInteraction(choices=[git_path])
    monkeypatch.setenv("EDITOR", "nvim")
    recorded: dict[str, object] = {}

    def fake_run(command: list[str], check: bool):
        recorded["command"] = command
        recorded["check"] = check
        return SimpleNamespace(returncode=0)

    _patch_cli_runtime(monkeypatch, fake_run)

    exit_code = main(
        ["--config", str(config_path), "edit", "target", "gitconfig"],
        interaction=interaction,
    )

    assert exit_code == 0
    assert interaction.requests[0].header_text == "Select a tracked target for 'gitconfig':"
    assert recorded["command"] == ["nvim", str(repo_root / "packages" / "git" / "files" / "gitconfig")]


def test_edit_target_cli_rejects_ambiguous_target_query_in_non_interactive_mode(
    tmp_path: Path,
    capsys,
) -> None:
    repo_root = tmp_path / "repo"
    _write_edit_repo(repo_root)
    config_path = write_named_manager_config(tmp_path, {"fixture": repo_root})
    _write_tracked_binding_states(
        tmp_path / "state",
        repo_name="fixture",
        bindings=[("git", "basic"), ("altgit", "basic")],
    )

    exit_code = main(["--config", str(config_path), "edit", "target", "gitconfig"])

    assert exit_code == 1
    assert "tracked target 'gitconfig' is ambiguous:" in capsys.readouterr().err


def test_edit_target_cli_rejects_untracked_package_target(
    tmp_path: Path,
    capsys,
) -> None:
    repo_root = tmp_path / "repo"
    _write_edit_repo(repo_root)
    config_path = write_named_manager_config(tmp_path, {"fixture": repo_root})
    _write_tracked_binding_state(tmp_path / "state", repo_name="fixture", selector="git", profile="basic")

    exit_code = main(["--config", str(config_path), "edit", "target", "note.note"])

    assert exit_code == 2
    assert "tracked target 'note.note' did not match any tracked target" in capsys.readouterr().err


def test_edit_target_cli_rejects_malformed_target_query(
    tmp_path: Path,
    capsys,
) -> None:
    repo_root = tmp_path / "repo"
    _write_edit_repo(repo_root)
    config_path = write_named_manager_config(tmp_path, {"fixture": repo_root})
    _write_tracked_binding_state(tmp_path / "state", repo_name="fixture", selector="nvim", profile="basic")

    exit_code = main(["--config", str(config_path), "edit", "target", "nvim."])

    assert exit_code == 2
    assert "invalid tracked target selector 'nvim.'" in capsys.readouterr().err
