from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from dotman.cli import main
from dotman.sync_session import EditProposal, SetApproval
from tests.engine.test_sync_session import make_engine
from tests.helpers import write_named_manager_config


def _write_basic_execution_repo(
    repo_root: Path,
    *,
    failing_guard: bool = False,
    guard_push_exit_code: int | None = None,
    guard_pull_exit_code: int | None = None,
    package_id: str = "app",
    live_dir_name: str = "app",
) -> None:
    package_root = repo_root / "packages" / package_id
    (package_root / "files").mkdir(parents=True)
    (repo_root / "profiles").mkdir(parents=True)

    (repo_root / "profiles" / "default.toml").write_text("", encoding="utf-8")
    (package_root / "files" / "config.txt").write_text("repo value\n", encoding="utf-8")

    if failing_guard and guard_push_exit_code is None:
        guard_push_exit_code = 1

    guard_push = "printf 'guard push\\n'"
    if guard_push_exit_code is not None:
        if guard_push_exit_code == 100:
            guard_push = "printf 'guard push\\n'; exit 100"
        elif guard_push_exit_code != 0:
            guard_push = f"printf 'guard push failed\\n'; exit {guard_push_exit_code}"

    guard_pull = "printf 'guard pull\\n'"
    if guard_pull_exit_code is not None:
        if guard_pull_exit_code == 100:
            guard_pull = "printf 'guard pull\\n'; exit 100"
        elif guard_pull_exit_code != 0:
            guard_pull = f"printf 'guard pull failed\\n'; exit {guard_pull_exit_code}"

    (package_root / "package.toml").write_text(
        "\n".join(
            [
                "",
                "[targets.config]",
                'source = "files/config.txt"',
                f'path = "~/.config/{live_dir_name}/config.txt"',
                'chmod = "600"',
                "",
                "[hooks]",
                f"guard_push = \"{guard_push}\"",
                "pre_push = \"printf 'pre push\\n'\"",
                "post_push = \"printf 'post push\\n'\"",
                f"guard_pull = \"{guard_pull}\"",
                "pre_pull = \"printf 'pre pull\\n'\"",
                "post_pull = \"printf 'post pull\\n'\"",
                "",
            ]
        ),
        encoding="utf-8",
    )



def _write_directory_execution_repo(
    repo_root: Path,
    *,
    package_id: str = "app",
    live_dir_name: str = "app",
    target_config: list[str] | None = None,
    path_rules: list[str] | None = None,
) -> None:
    package_root = repo_root / "packages" / package_id
    (package_root / "files" / "config").mkdir(parents=True)
    (repo_root / "profiles").mkdir(parents=True)

    (repo_root / "profiles" / "default.toml").write_text("", encoding="utf-8")
    (package_root / "files" / "config" / "nested.txt").write_text("repo directory value\n", encoding="utf-8")
    target_lines = [
        "",
        "[targets.config]",
        'source = "files/config"',
        f'path = "~/.config/{live_dir_name}"',
    ]
    if target_config:
        target_lines.extend(target_config)
    target_lines.append("")
    if path_rules:
        target_lines.extend(path_rules)
        target_lines.append("")

    (package_root / "package.toml").write_text(
        "\n".join(
            target_lines
            + [
                "[hooks]",
                "guard_push = \"printf 'guard push\\n'\"",
                "pre_push = \"printf 'pre push\\n'\"",
                "post_push = \"printf 'post push\\n'\"",
                "",
            ]
        ),
        encoding="utf-8",
    )



def _write_tracked_binding(state_root: Path, *, repo_name: str = "fixture", selector: str = "app") -> None:
    state_dir = state_root / "dotman" / "repos" / repo_name
    state_dir.mkdir(parents=True, exist_ok=True)
    # Match the private manager layout; these are healthy execution fixtures,
    # not tests of rejected storage permissions.
    for directory in (state_root / "dotman", state_root / "dotman" / "repos", state_dir):
        directory.chmod(0o700)
    (state_dir / "tracked-packages.toml").write_text(
        "\n".join(
            [
                "schema_version = 1",
                "",
                "[[packages]]",
                f'repo = "{repo_name}"',
                f'package_id = "{selector}"',
                'profile = "default"',
                "",
            ]
        ),
        encoding="utf-8",
    )



def test_push_cli_executes_tracked_binding_and_emits_json_results(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    repo_root = tmp_path / "repo"
    _write_basic_execution_repo(repo_root)
    config_path = write_named_manager_config(tmp_path, {"fixture": repo_root})
    _write_tracked_binding(tmp_path / "state")

    exit_code = main(["--no-confirm", "--config", str(config_path), "--json", "push"])

    assert exit_code == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    payload = json.loads(captured.out)
    live_path = home / ".config" / "app" / "config.txt"
    assert live_path.read_text(encoding="utf-8") == "repo value\n"
    assert stat.S_IMODE(live_path.stat().st_mode) == 0o600
    assert payload["mode"] == "execute"
    assert payload["operation"] == "push"
    assert payload["status"] == "completed"
    unit = payload["sync_units"][0]
    assert unit["identity"] == "fixture:app.config"
    assert unit["result"] == "converged"
    assert unit["base"]["acknowledged"] is True
    assert unit["diagnostics"] == []
    assert [(step["action"], step["status"]) for step in payload["stages"]] == [
        ("pre_push", "ok"),
        ("write", "ok"),
        ("chmod", "ok"),
        ("post_push", "ok"),
    ]
    assert payload["guard_skips"] == []


def test_push_directory_target_create_preserves_repo_executable_bit(
    tmp_path: Path,
    monkeypatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    repo_root = tmp_path / "repo"
    _write_directory_execution_repo(repo_root)
    repo_path = repo_root / "packages" / "app" / "files" / "config" / "nested.txt"
    repo_path.chmod(0o755)
    config_path = write_named_manager_config(tmp_path, {"fixture": repo_root})
    _write_tracked_binding(tmp_path / "state")

    exit_code = main(["--no-confirm", "--config", str(config_path), "push"])

    assert exit_code == 0
    live_path = home / ".config" / "app" / "nested.txt"
    assert live_path.read_text(encoding="utf-8") == "repo directory value\n"
    assert stat.S_IMODE(live_path.stat().st_mode) == 0o755


def test_push_directory_target_updates_live_file_when_only_repo_executable_bit_changed(
    tmp_path: Path,
    monkeypatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    repo_root = tmp_path / "repo"
    _write_directory_execution_repo(repo_root)
    repo_path = repo_root / "packages" / "app" / "files" / "config" / "nested.txt"
    repo_path.chmod(0o755)
    live_path = home / ".config" / "app" / "nested.txt"
    live_path.parent.mkdir(parents=True)
    live_path.write_text("repo directory value\n", encoding="utf-8")
    live_path.chmod(0o644)
    config_path = write_named_manager_config(tmp_path, {"fixture": repo_root})
    _write_tracked_binding(tmp_path / "state")

    exit_code = main(["--no-confirm", "--config", str(config_path), "push"])

    assert exit_code == 0
    assert live_path.read_text(encoding="utf-8") == "repo directory value\n"
    assert stat.S_IMODE(live_path.stat().st_mode) == 0o755


def test_push_directory_target_ignores_non_executable_mode_drift(
    tmp_path: Path,
    monkeypatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    repo_root = tmp_path / "repo"
    _write_directory_execution_repo(repo_root)
    repo_path = repo_root / "packages" / "app" / "files" / "config" / "nested.txt"
    repo_path.chmod(0o600)
    live_path = home / ".config" / "app" / "nested.txt"
    live_path.parent.mkdir(parents=True)
    live_path.write_text("repo directory value\n", encoding="utf-8")
    live_path.chmod(0o644)
    config_path = write_named_manager_config(tmp_path, {"fixture": repo_root})
    _write_tracked_binding(tmp_path / "state")

    exit_code = main(["--no-confirm", "--config", str(config_path), "push"])

    assert exit_code == 0
    assert live_path.read_text(encoding="utf-8") == "repo directory value\n"
    assert stat.S_IMODE(live_path.stat().st_mode) == 0o644


def test_push_directory_target_path_rule_applies_child_chmod_on_create(
    tmp_path: Path,
    monkeypatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    repo_root = tmp_path / "repo"
    _write_directory_execution_repo(
        repo_root,
        path_rules=[
            "[targets.config.path_rules.rule]",
            'pattern = "nested.txt"',
            'chmod = "600"',
        ],
    )
    config_path = write_named_manager_config(tmp_path, {"fixture": repo_root})
    _write_tracked_binding(tmp_path / "state")

    exit_code = main(["--no-confirm", "--config", str(config_path), "push"])

    assert exit_code == 0
    live_path = home / ".config" / "app" / "nested.txt"
    assert live_path.read_text(encoding="utf-8") == "repo directory value\n"
    assert stat.S_IMODE(live_path.stat().st_mode) == 0o600


def test_push_directory_target_path_rule_repairs_child_chmod_drift(
    tmp_path: Path,
    monkeypatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    repo_root = tmp_path / "repo"
    _write_directory_execution_repo(
        repo_root,
        path_rules=[
            "[targets.config.path_rules.rule]",
            'pattern = "nested.txt"',
            'chmod = "600"',
        ],
    )
    live_path = home / ".config" / "app" / "nested.txt"
    live_path.parent.mkdir(parents=True)
    live_path.write_text("repo directory value\n", encoding="utf-8")
    live_path.chmod(0o644)
    config_path = write_named_manager_config(tmp_path, {"fixture": repo_root})
    _write_tracked_binding(tmp_path / "state")

    exit_code = main(["--no-confirm", "--config", str(config_path), "push"])

    assert exit_code == 0
    assert live_path.read_text(encoding="utf-8") == "repo directory value\n"
    assert stat.S_IMODE(live_path.stat().st_mode) == 0o600


def test_push_file_target_repairs_chmod_drift_without_content_change(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    repo_root = tmp_path / "repo"
    _write_basic_execution_repo(repo_root)
    live_path = home / ".config" / "app" / "config.txt"
    live_path.parent.mkdir(parents=True)
    live_path.write_text("repo value\n", encoding="utf-8")
    live_path.chmod(0o644)
    config_path = write_named_manager_config(tmp_path, {"fixture": repo_root})
    _write_tracked_binding(tmp_path / "state")

    exit_code = main(["--no-confirm", "--config", str(config_path), "--json", "push"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert [effect["kind"] for effect in payload["sync_units"][0]["effects"]] == ["chmod"]
    assert [step["action"] for step in payload["stages"]] == ["pre_push", "chmod", "post_push"]
    assert live_path.read_text(encoding="utf-8") == "repo value\n"
    assert stat.S_IMODE(live_path.stat().st_mode) == 0o600


def test_push_directory_target_uses_render_for_child_files(
    tmp_path: Path,
    monkeypatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    repo_root = tmp_path / "repo"
    _write_directory_execution_repo(repo_root, target_config=['render = "sh hooks/render.sh"'])
    package_root = repo_root / "packages" / "app"
    (package_root / "hooks").mkdir()
    (package_root / "hooks" / "render.sh").write_text(
        "#!/bin/sh\nsed 's/^/rendered:/' \"$DOTMAN_SOURCE\"\n",
        encoding="utf-8",
    )
    config_path = write_named_manager_config(tmp_path, {"fixture": repo_root})
    _write_tracked_binding(tmp_path / "state")

    exit_code = main(["--no-confirm", "--config", str(config_path), "push"])

    assert exit_code == 0
    live_path = home / ".config" / "app" / "nested.txt"
    assert live_path.read_text(encoding="utf-8") == "rendered:repo directory value\n"


def test_push_directory_target_path_rule_render_overrides_default_render(
    tmp_path: Path,
    monkeypatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    repo_root = tmp_path / "repo"
    _write_directory_execution_repo(
        repo_root,
        target_config=['render = "sh hooks/default-render.sh"'],
        path_rules=[
            "[targets.config.path_rules.rule]",
            'pattern = "*.json"',
            'render = "sh hooks/json-render.sh"',
        ],
    )
    package_root = repo_root / "packages" / "app"
    (package_root / "files" / "config" / "data.json").write_text("json value\n", encoding="utf-8")
    (package_root / "hooks").mkdir()
    (package_root / "hooks" / "default-render.sh").write_text(
        "#!/bin/sh\nsed 's/^/default:/' \"$DOTMAN_SOURCE\"\n",
        encoding="utf-8",
    )
    (package_root / "hooks" / "json-render.sh").write_text(
        "#!/bin/sh\nsed 's/^/json:/' \"$DOTMAN_SOURCE\"\n",
        encoding="utf-8",
    )
    config_path = write_named_manager_config(tmp_path, {"fixture": repo_root})
    _write_tracked_binding(tmp_path / "state")

    exit_code = main(["--no-confirm", "--config", str(config_path), "push"])

    assert exit_code == 0
    live_root = home / ".config" / "app"
    assert (live_root / "nested.txt").read_text(encoding="utf-8") == "default:repo directory value\n"
    assert (live_root / "data.json").read_text(encoding="utf-8") == "json:json value\n"


def test_push_cli_dry_run_emits_symlink_hazard_metadata(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    repo_root = tmp_path / "repo"
    _write_basic_execution_repo(repo_root)
    config_path = write_named_manager_config(tmp_path, {"fixture": repo_root})
    _write_tracked_binding(tmp_path / "state")

    live_root = home / ".config" / "app"
    live_root.mkdir(parents=True)
    symlink_target = live_root / "config-real.txt"
    symlink_target.write_text("live value\n", encoding="utf-8")
    (live_root / "config.txt").symlink_to(symlink_target)

    exit_code = main(["--no-confirm", "--config", str(config_path), "--json", "push", "--dry-run"])

    # No-confirm preview cannot authorize replacing the link, so the hazard is a unit error.
    assert exit_code == 1
    payload = json.loads(capsys.readouterr().out)
    unit = payload["sync_units"][0]
    assert unit["identity"] == "fixture:app.config"
    assert unit["approved"] is False
    assert unit["symlink_replacement_authorized"] is False
    assert [item["code"] for item in unit["diagnostics"]] == ["symlink-authorization-required"]
    assert payload["stages"] == []
    assert (live_root / "config.txt").is_symlink()
    assert symlink_target.read_text(encoding="utf-8") == "live value\n"



def test_push_cli_fails_fast_for_symlinked_live_target_in_non_interactive_mode(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    repo_root = tmp_path / "repo"
    _write_basic_execution_repo(repo_root)
    config_path = write_named_manager_config(tmp_path, {"fixture": repo_root})
    _write_tracked_binding(tmp_path / "state")

    live_root = home / ".config" / "app"
    live_root.mkdir(parents=True)
    symlink_target = live_root / "config-real.txt"
    symlink_target.write_text("live value\n", encoding="utf-8")
    (live_root / "config.txt").symlink_to(symlink_target)

    exit_code = main(["--no-confirm", "--config", str(config_path), "push"])

    assert exit_code == 1
    output = capsys.readouterr().out
    assert "[failed] fixture:app.config" in output
    assert "Live symlink replacement requires explicit authorization" in output
    assert ":: failed" in output
    assert (live_root / "config.txt").is_symlink()
    assert symlink_target.read_text(encoding="utf-8") == "live value\n"



def test_push_cli_no_confirm_refuses_unsafe_symlink_replacement(
    tmp_path: Path,
    monkeypatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    repo_root = tmp_path / "repo"
    _write_basic_execution_repo(repo_root)
    config_path = write_named_manager_config(tmp_path, {"fixture": repo_root})
    _write_tracked_binding(tmp_path / "state")

    live_root = home / ".config" / "app"
    live_root.mkdir(parents=True)
    symlink_target = live_root / "config-real.txt"
    symlink_target.write_text("live value\n", encoding="utf-8")
    (live_root / "config.txt").symlink_to(symlink_target)

    exit_code = main(["--no-confirm", "--config", str(config_path), "push"])

    assert exit_code == 1
    live_path = live_root / "config.txt"
    assert live_path.is_file()
    assert live_path.is_symlink()
    assert live_path.read_text(encoding="utf-8") == "live value\n"
    assert symlink_target.read_text(encoding="utf-8") == "live value\n"


def test_push_cli_fails_fast_for_symlinked_directory_live_target_in_non_interactive_mode(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    repo_root = tmp_path / "repo"
    _write_directory_execution_repo(repo_root)
    config_path = write_named_manager_config(tmp_path, {"fixture": repo_root})
    _write_tracked_binding(tmp_path / "state")

    live_root = home / ".config" / "app"
    live_root.parent.mkdir(parents=True, exist_ok=True)
    real_live_root = home / ".config" / "app-real"
    real_live_root.mkdir(parents=True)
    live_root.symlink_to(real_live_root, target_is_directory=True)

    exit_code = main(["--no-confirm", "--config", str(config_path), "--json", "push"])

    assert exit_code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "failed"
    assert {item["code"] for unit in payload["sync_units"] for item in unit["diagnostics"]} == {"directory-symlink"}
    assert payload["stages"] == []
    assert live_root.is_symlink()
    assert list(real_live_root.iterdir()) == []



def test_push_cli_follows_directory_symlink_when_configured(
    tmp_path: Path,
    monkeypatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    repo_root = tmp_path / "repo"
    _write_directory_execution_repo(repo_root)
    config_path = write_named_manager_config(tmp_path, {"fixture": repo_root})
    _write_tracked_binding(tmp_path / "state")

    live_root = home / ".config" / "app"
    live_root.parent.mkdir(parents=True, exist_ok=True)
    real_live_root = home / ".config" / "app-real"
    real_live_root.mkdir(parents=True)
    live_root.symlink_to(real_live_root, target_is_directory=True)

    exit_code = main(["--no-confirm", "--config", str(config_path), "--dir-symlink-mode", "follow", "push"])

    assert exit_code == 0
    assert live_root.is_symlink()
    assert (real_live_root / "nested.txt").read_text(encoding="utf-8") == "repo directory value\n"



def _write_in_sync_live_config(home: Path) -> Path:
    live_path = home / ".config" / "app" / "config.txt"
    live_path.parent.mkdir(parents=True)
    live_path.write_text("repo value\n", encoding="utf-8")
    live_path.chmod(0o600)
    return live_path


def test_push_cli_run_noop_executes_hooks_for_all_noop_push_plan(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    repo_root = tmp_path / "repo"
    _write_basic_execution_repo(repo_root)
    config_path = write_named_manager_config(tmp_path, {"fixture": repo_root})
    _write_tracked_binding(tmp_path / "state")
    _write_in_sync_live_config(home)

    exit_code = main(["--no-confirm", "--config", str(config_path), "--json", "push", "--run-noop"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["sync_units"] == []
    assert payload["summary"]["in_sync_units"] == 1
    assert payload["hook_work"] == [
        {"identity": "fixture:app", "selected": True, "directions": ["push"], "diagnostics": []},
    ]
    assert [(step["action"], step["status"]) for step in payload["stages"]] == [
        ("pre_push", "ok"),
        ("post_push", "ok"),
    ]
    # Hook-only work publishes nothing, so there is nothing to snapshot.
    assert not (tmp_path / "xdg-data" / "dotman" / "snapshots").exists()


def test_push_cli_human_execution_hides_direct_agreement_base_save(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    repo_root = tmp_path / "repo"
    _write_basic_execution_repo(repo_root)
    config_path = write_named_manager_config(tmp_path, {"fixture": repo_root})
    _write_tracked_binding(tmp_path / "state")
    _write_in_sync_live_config(home)

    exit_code = main(["--no-confirm", "--config", str(config_path), "push"])

    assert exit_code == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    output = captured.out
    assert "fixture:app" not in output
    assert "in-sync: 1" in output
    assert "acknowledge" not in output
    assert "Base" not in output


def test_push_cli_run_noop_dry_run_json_shows_hook_only_package(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    repo_root = tmp_path / "repo"
    _write_basic_execution_repo(repo_root)
    config_path = write_named_manager_config(tmp_path, {"fixture": repo_root})
    _write_tracked_binding(tmp_path / "state")
    _write_in_sync_live_config(home)

    exit_code = main(["--no-confirm", "--config", str(config_path), "--json", "push", "--dry-run", "--run-noop"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "dry-run"
    assert payload["sync_units"] == []
    assert [(work["identity"], work["directions"]) for work in payload["hook_work"]] == [("fixture:app", ["push"])]
    assert payload["stages"] == []


def test_push_cli_run_noop_hook_only_plan_soft_skips_guard_and_does_not_create_snapshot(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    repo_root = tmp_path / "repo"
    _write_basic_execution_repo(repo_root, guard_push_exit_code=100)
    config_path = write_named_manager_config(tmp_path, {"fixture": repo_root})
    _write_tracked_binding(tmp_path / "state")
    live_path = home / ".config" / "app" / "config.txt"
    live_path.parent.mkdir(parents=True)
    live_path.write_text("repo value\n", encoding="utf-8")

    exit_code = main(["--no-confirm", "--config", str(config_path), "push", "--run-noop"])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "[skipped] fixture:app (guard_push)" in output
    assert "Guard skipped: guard push" in output
    assert "Hook Work" not in output
    # Guard omission also skips the pending chmod repair.
    assert stat.S_IMODE(live_path.stat().st_mode) != 0o600
    assert not (tmp_path / "xdg-data" / "dotman" / "snapshots").exists()


def test_push_cli_run_noop_hook_only_plan_soft_skips_guard_in_json(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    repo_root = tmp_path / "repo"
    _write_basic_execution_repo(repo_root, guard_push_exit_code=100)
    config_path = write_named_manager_config(tmp_path, {"fixture": repo_root})
    _write_tracked_binding(tmp_path / "state")

    exit_code = main(["--no-confirm", "--config", str(config_path), "--json", "push", "--run-noop"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "execute"
    assert payload["sync_units"] == []
    assert payload["hook_work"] == []
    assert payload["stages"] == []
    assert payload["guard_skips"] == [{
        "identity": "fixture:app", "direction": "push", "scope_kind": "package",
        "path_rule_pattern": None, "reason": "guard push",
    }]
    assert not (home / ".config" / "app" / "config.txt").exists()


def test_push_cli_fails_fast_and_skips_post_push_after_failed_guard(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    repo_root = tmp_path / "repo"
    _write_basic_execution_repo(repo_root, failing_guard=True)
    config_path = write_named_manager_config(tmp_path, {"fixture": repo_root})
    _write_tracked_binding(tmp_path / "state")

    exit_code = main(["--no-confirm", "--config", str(config_path), "--json", "push"])

    assert exit_code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "failed"
    assert payload["summary"]["diagnostics"] == [
        {"code": "planning-failed", "message": "fixture:app guard_push failed with exit 1"},
    ]
    assert payload["stages"] == []
    assert not (home / ".config" / "app" / "config.txt").exists()


def test_push_cli_guard_failure_blocks_every_package_in_the_push(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    failing_repo_root = tmp_path / "repo-failing"
    healthy_repo_root = tmp_path / "repo-healthy"
    _write_basic_execution_repo(failing_repo_root, failing_guard=True, package_id="app", live_dir_name="app")
    _write_basic_execution_repo(healthy_repo_root, package_id="other", live_dir_name="other")
    config_path = write_named_manager_config(
        tmp_path,
        {"fixture-a": failing_repo_root, "fixture-b": healthy_repo_root},
    )
    _write_tracked_binding(tmp_path / "state", repo_name="fixture-a", selector="app")
    _write_tracked_binding(tmp_path / "state", repo_name="fixture-b", selector="other")

    exit_code = main(["--no-confirm", "--config", str(config_path), "push"])

    assert exit_code == 1
    captured = capsys.readouterr()
    assert ":: failed" in captured.out
    assert "guard_push failed with exit 1" in captured.err
    assert not (home / ".config" / "app" / "config.txt").exists()
    assert not (home / ".config" / "other" / "config.txt").exists()


def test_capture_patch_cli_emits_patched_repo_bytes(
    tmp_path: Path,
    capsys,
) -> None:
    repo_path = tmp_path / "config.txt"
    review_repo_path = tmp_path / "review-repo.txt"
    review_live_path = tmp_path / "review-live.txt"

    repo_path.write_text("greeting = {{ vars.greeting }}\nkeep\nmode = safe\n", encoding="utf-8")
    review_repo_path.write_text("greeting = hello\nkeep\nmode = safe\n", encoding="utf-8")
    review_live_path.write_text("greeting = hello\nkeep\nmode = fast\n", encoding="utf-8")

    exit_code = main(
        [
            "capture",
            "patch",
            "--repo-path",
            str(repo_path),
            "--render",
            "jinja",
            "--review-repo-path",
            str(review_repo_path),
            "--review-live-path",
            str(review_live_path),
            "--var",
            "greeting=hello",
        ]
    )

    assert exit_code == 0
    assert capsys.readouterr().out == "greeting = {{ vars.greeting }}\nkeep\nmode = fast\n"


def test_capture_patch_cli_jinja_renders_like_file_targets(
    tmp_path: Path,
    capsys,
) -> None:
    # File targets trim standalone block-tag lines; the CLI must project the
    # candidate the same way or it checks against a render Sync never produces.
    repo_path = tmp_path / "config.txt"
    review_repo_path = tmp_path / "review-repo.txt"
    review_live_path = tmp_path / "review-live.txt"

    repo_source = "keep\n{% if vars.feature %}\nfeature = on\n{% endif %}\nmiddle\nmode = safe\n"
    repo_path.write_text(repo_source, encoding="utf-8")
    review_repo_path.write_text("keep\nfeature = on\nmiddle\nmode = safe\n", encoding="utf-8")
    review_live_path.write_text("keep\nfeature = on\nmiddle\nmode = fast\n", encoding="utf-8")

    exit_code = main(
        [
            "capture",
            "patch",
            "--repo-path",
            str(repo_path),
            "--render",
            "jinja",
            "--review-repo-path",
            str(review_repo_path),
            "--review-live-path",
            str(review_live_path),
            "--var",
            "feature=true",
        ]
    )

    assert exit_code == 0
    assert capsys.readouterr().out == repo_source.replace("mode = safe", "mode = fast")



def test_capture_patch_cli_accepts_command_renderers(
    tmp_path: Path,
    capsys,
) -> None:
    repo_path = tmp_path / "config.txt"
    review_repo_path = tmp_path / "review-repo.txt"
    review_live_path = tmp_path / "review-live.txt"

    repo_path.write_text("greeting = @@greeting@@\nkeep\nmode = safe\n", encoding="utf-8")
    review_repo_path.write_text("greeting = hello\nkeep\nmode = safe\n", encoding="utf-8")
    review_live_path.write_text("greeting = hello\nkeep\nmode = fast\n", encoding="utf-8")

    exit_code = main(
        [
            "capture",
            "patch",
            "--repo-path",
            str(repo_path),
            "--render",
            'sed "s/@@greeting@@/$DOTMAN_VAR_greeting/g" "$DOTMAN_SOURCE"',
            "--review-repo-path",
            str(review_repo_path),
            "--review-live-path",
            str(review_live_path),
            "--var",
            "greeting=hello",
        ]
    )

    assert exit_code == 0
    assert capsys.readouterr().out == "greeting = @@greeting@@\nkeep\nmode = fast\n"


@pytest.mark.parametrize(
    "source,live,change",
    [(None, b"live", "write"), (b"repo", b"live", "write"), (b"repo", None, "delete")],
)
def test_pull_cli_applies_repository_changes_and_emits_stages(
    tmp_path, monkeypatch, capsys, source, live, change,
):
    make_engine(tmp_path, monkeypatch, [
        ("unit", "both", source, live,
         '[targets.unit.hooks]\npre_pull = "true"\npost_pull = "true"'),
    ])
    tracked = tmp_path / "state/dotman/repos/main/tracked-packages.toml"
    tracked_before = tracked.read_bytes()

    assert main([
        "--config", str(tmp_path / "config.toml"), "--no-confirm", "--json",
        "pull", "main:app.unit",
    ]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["operation"] == "pull"
    assert payload["mode"] == "execute"
    unit, = payload["sync_units"]
    assert unit["identity"] == "main:app.unit"
    assert unit["result"] == "applied"
    assert unit["primary_source_change"]["kind"] == change
    assert all(step["stage"] == "repository-apply" for step in payload["stages"])
    assert [step["action"] for step in payload["stages"]] == ["pre_pull", "update" if change == "write" else "delete", "post_pull"]
    assert all(step["status"] == "ok" for step in payload["stages"])
    repo_path = tmp_path / "repo/packages/app/unit"
    live_path = tmp_path / "live/unit"
    assert (repo_path.read_bytes() if repo_path.exists() else None) == live
    assert (live_path.read_bytes() if live_path.exists() else None) == live
    assert tracked.read_bytes() == tracked_before


def test_pull_cli_uses_capture_without_launching_configured_editor(tmp_path, monkeypatch, capsys):
    make_engine(tmp_path, monkeypatch, [
        ("unit", "both", b"repo", b"live",
         'capture = "printf captured"\ncompare = {repo = "raw", live = "raw"}\n'
         'editor = {run = "exit 9", io = "pipe"}'),
    ])

    assert main([
        "--config", str(tmp_path / "config.toml"), "--no-confirm", "--json",
        "pull", "main:app.unit",
    ]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["sync_units"][0]["result"] == "applied"
    assert (tmp_path / "repo/packages/app/unit").read_bytes() == b"captured"
    assert (tmp_path / "live/unit").read_bytes() == b"live"


@pytest.mark.parametrize("capture_fails", [False, True])
def test_pull_cli_editor_is_explicit_deck_action_with_approval(
    tmp_path, monkeypatch, capsys, capture_fails,
):
    make_engine(tmp_path, monkeypatch, [
        ("unit", "both", b"repo", b"live",
         ('capture = "false"\ncompare = {repo = "raw", live = "raw"}\n' if capture_fails else "")
         + 'editor = {run = "printf edited > \\"$DOTMAN_SOURCE\\"", io = "pipe"}'),
    ])
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)

    def edit_and_approve(session, *, use_color):
        row, = session.view.rows
        assert row.approved is not capture_fails
        assert bool(row.diagnostics) is capture_fails
        view = session.view
        edited = session.dispatch(EditProposal(view.session_id, view.revision, row.row_id))
        assert edited.result.status == "saved"
        assert (tmp_path / "repo/packages/app/unit").read_bytes() == b"repo"
        view = session.view
        session.dispatch(SetApproval(view.session_id, view.revision, row.row_id, True))
        return True

    monkeypatch.setattr("dotman.sync_deck.run_command_deck", edit_and_approve)

    assert main(["--config", str(tmp_path / "config.toml"), "pull", "main:app.unit"]) == 0
    assert (tmp_path / "repo/packages/app/unit").read_bytes() == b"edited"
    assert (tmp_path / "live/unit").read_bytes() == b"live"
    output = capsys.readouterr().out
    assert "[1/1] update" in output


def test_pull_cli_run_noop_executes_only_pull_hooks(tmp_path, monkeypatch, capsys):
    make_engine(tmp_path, monkeypatch, [("unit", "both", b"same", b"same", "")])
    package = tmp_path / "repo/packages/app/package.toml"
    with package.open("a") as stream:
        stream.write(
            '\n[hooks]\npre_pull = "true"\npost_pull = "true"\npre_push = "exit 9"\n'
        )

    assert main([
        "--config", str(tmp_path / "config.toml"), "--no-confirm", "--json",
        "pull", "--run-noop", "main:app",
    ]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert [step["action"] for step in payload["stages"]] == ["pre_pull", "post_pull"], payload
    assert all(step["status"] == "ok" for step in payload["stages"])
    assert (tmp_path / "repo/packages/app/unit").read_bytes() == b"same"
