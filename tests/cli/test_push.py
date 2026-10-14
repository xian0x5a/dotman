from __future__ import annotations

import json
from pathlib import Path

from dotman.cli import main

from tests.helpers import write_manager_config


def write_push_repo(tmp_path: Path, monkeypatch, tracked_package: str) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    config_path = write_manager_config(tmp_path)
    state_dir = tmp_path / "state" / "dotman" / "repos" / "example"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "tracked-packages.toml").write_text(
        "\n".join([
            "schema_version = 1",
            "",
            "[[packages]]",
            'repo = "example"',
            f'package_id = "{tracked_package}"',
            'profile = "basic"',
            "",
        ]),
        encoding="utf-8",
    )
    return config_path


def test_push_cli_short_dry_run_previews_tracked_scope_without_writes(tmp_path, monkeypatch, capsys) -> None:
    config_path = write_push_repo(tmp_path, monkeypatch, "git")

    assert main(["--config", str(config_path), "--json", "--no-confirm", "push", "-d"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["operation"] == "push"
    assert payload["mode"] == "dry-run"
    assert payload["scope"] == ["example:git.gitconfig"]
    unit = payload["sync_units"][0]
    assert unit["approved"] is True
    assert unit["resolution"] == "use-repository"
    assert unit["allowed_intents"] == []
    assert unit["effects"][0]["kind"] == "write"
    assert unit["result"] == "would-converge"
    assert not (tmp_path / "home/.gitconfig").exists()


def test_push_cli_accepts_package_owned_by_tracked_root(tmp_path, monkeypatch, capsys) -> None:
    config_path = write_push_repo(tmp_path, monkeypatch, "core-cli-meta")

    assert main([
        "--config", str(config_path), "--json", "--no-confirm", "push", "--dry-run", "example:nvim",
    ]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["scope"] == ["example:nvim.init_lua"]
    assert all(unit["approved"] for unit in payload["sync_units"])


def test_push_cli_rejects_untracked_scope(tmp_path, monkeypatch, capsys) -> None:
    config_path = write_push_repo(tmp_path, monkeypatch, "git")

    assert main([
        "--config", str(config_path), "--json", "--no-confirm", "push", "--dry-run", "example:nvim",
    ]) == 2

    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "failed"
    assert payload["summary"]["diagnostics"][0]["code"] == "invalid-input"
    assert "did not match any tracked package or target" in payload["summary"]["diagnostics"][0]["message"]


def test_push_cli_human_preview_names_leaf_targets_not_root_package(tmp_path, monkeypatch, capsys) -> None:
    config_path = write_push_repo(tmp_path, monkeypatch, "core-cli-meta")

    assert main(["--config", str(config_path), "--no-confirm", "push", "--dry-run"]) == 0

    output = capsys.readouterr().out
    assert ":: Push preview" in output
    assert "[would-apply] example:git.gitconfig" in output
    assert "[would-apply] example:nvim.init_lua" in output
    assert "Use repository" in output
    assert "core-cli-meta" not in output
    assert not (tmp_path / "home/.gitconfig").exists()


def test_push_cli_reports_the_planned_mode_only_on_the_chmod_effect(tmp_path, monkeypatch, capsys) -> None:
    # The write applies the mode atomically, but the mode change is reported once, as chmod.
    config_path = write_push_repo(tmp_path, monkeypatch, "git")

    assert main(["--config", str(config_path), "--json", "--no-confirm", "push", "-d"]) == 0

    effects = json.loads(capsys.readouterr().out)["sync_units"][0]["effects"]
    assert [(effect["kind"], effect.get("mode")) for effect in effects] == [("write", None), ("chmod", "0600")]
