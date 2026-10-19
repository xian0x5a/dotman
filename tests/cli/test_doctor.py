from __future__ import annotations

import json
from pathlib import Path

import pytest
from dotman.cli import main
import dotman.doctor as doctor

from tests.helpers import write_manager_config, write_single_repo_config


def test_doctor_cli_reports_ok_for_valid_config(tmp_path: Path, capsys) -> None:
    config_path = write_manager_config(tmp_path)

    exit_code = main(["--config", str(config_path), "--json", "doctor"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["config_path"] == str(config_path.resolve())
    assert payload["invalid_package_entries"] == []
    assert payload["ok"] is True
    assert payload["repo_count"] == 2
    assert all(check["status"] != "failed" for check in payload["checks"])
    assert any(check["key"] == "repo_path" and check["repo_name"] == "example" for check in payload["checks"])


def test_push_cli_reports_missing_default_config_with_hint(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    config_home = tmp_path / "xdg-config"
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))

    exit_code = main(["--no-confirm", "push", "--dry-run"])

    # A missing config is a usage/config error (2), with or without --no-confirm.
    assert exit_code == 2
    error_output = capsys.readouterr().err
    assert "Traceback" not in error_output
    assert "manager config file does not exist" in error_output
    assert str((config_home / "dotman" / "config.toml").resolve()) in error_output
    assert "--config <config-path>" in error_output


def test_doctor_cli_reports_directory_config_path_with_same_hint(tmp_path: Path, capsys) -> None:
    config_dir = tmp_path / "config-dir"
    config_dir.mkdir()

    exit_code = main(["--config", str(config_dir), "doctor"])

    assert exit_code == 2
    error_output = capsys.readouterr().err
    assert "manager config path is not a file" in error_output
    assert str(config_dir.resolve()) in error_output
    assert "Create config.toml with at least one [repos.<name>] entry, or pass --config <config-path>." in error_output


def test_doctor_cli_reports_missing_repo_path(tmp_path: Path, capsys) -> None:
    missing_repo_path = tmp_path / "missing-repo"
    config_path = write_single_repo_config(tmp_path, repo_name="fixture", repo_path=missing_repo_path)

    exit_code = main(["--config", str(config_path), "--json", "doctor"])

    assert exit_code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["invalid_package_entries"] == []
    assert any(
        check["key"] == "repo_path"
        and check["repo_name"] == "fixture"
        and check["status"] == "failed"
        and check["path"] == str(missing_repo_path.resolve())
        for check in payload["checks"]
    )


def test_doctor_cli_reports_required_and_optional_dependencies(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    config_path = write_manager_config(tmp_path)
    monkeypatch.setenv("EDITOR", "nvim")

    def fake_which(name: str) -> str | None:
        if name == "git":
            return None
        if name == "fzf":
            return None
        if name == "less":
            return None
        if name == "sudo":
            return None
        if name == "nvim":
            return "/usr/bin/nvim"
        return f"/usr/bin/{name}"

    monkeypatch.setattr(doctor.shutil, "which", fake_which)

    exit_code = main(["--config", str(config_path), "--json", "doctor"])

    assert exit_code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert any(
        check["key"] == "dependency_git" and check["status"] == "failed"
        for check in payload["checks"]
    )
    assert any(
        check["key"] == "dependency_fzf" and check["status"] == "warn"
        for check in payload["checks"]
    )
    assert any(
        check["key"] == "dependency_less" and check["status"] == "warn"
        for check in payload["checks"]
    )
    assert any(
        check["key"] == "dependency_sudo" and check["status"] == "warn"
        for check in payload["checks"]
    )
    assert any(
        check["key"] == "editor"
        and check["status"] == "ok"
        and check["path"] == "/usr/bin/nvim"
        for check in payload["checks"]
    )


def test_doctor_cli_groups_human_output_by_category(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    missing_repo_path = tmp_path / "missing-repo"
    config_path = write_single_repo_config(tmp_path, repo_name="fixture", repo_path=missing_repo_path)
    monkeypatch.delenv("VISUAL", raising=False)
    monkeypatch.delenv("EDITOR", raising=False)

    def fake_which(name: str) -> str | None:
        if name in {"git", "fzf", "less", "sudo"}:
            return None
        return f"/usr/bin/{name}"

    monkeypatch.setattr(doctor.shutil, "which", fake_which)

    exit_code = main(["--config", str(config_path), "doctor"])

    assert exit_code == 1
    output = capsys.readouterr().out
    assert "failed checks:" in output
    assert "warnings:" in output
    assert "dependencies:" in output
    assert "repository:" in output
    assert "environment:" in output
    assert "git is not installed" in output
    assert "repo path does not exist" in output
    assert "no editor configured" in output


@pytest.mark.parametrize(
    "state_text",
    [
        pytest.param("schema_version = 1\n[[packages]\n", id="invalid-toml"),
        pytest.param('[[packages]]\nrepo = "fixture"\npackage_id = "app"\nprofile = "default"\n', id="missing-schema-version"),
        pytest.param('schema_version = 1\n[[packages]]\nrepo = "fixture"\npackage_id = "app"\n', id="row-missing-profile"),
    ],
)
def test_doctor_cli_reports_corrupt_tracked_state_as_failed_check(
    tmp_path: Path,
    capsys,
    state_text: str,
) -> None:
    repo_root = tmp_path / "repo"
    (repo_root / "profiles").mkdir(parents=True)
    (repo_root / "profiles" / "default.toml").write_text("", encoding="utf-8")
    (repo_root / "packages" / "app").mkdir(parents=True)
    (repo_root / "packages" / "app" / "package.toml").write_text('', encoding="utf-8")
    config_path = write_single_repo_config(tmp_path, repo_name="fixture", repo_path=repo_root)
    state_path = tmp_path / "state" / "dotman" / "repos" / "fixture" / "tracked-packages.toml"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(state_text, encoding="utf-8")

    exit_code = main(["--config", str(config_path), "--json", "doctor"])

    captured = capsys.readouterr()
    assert "Traceback" not in captured.err
    # Failed checks exit 1; the report itself is still the main contract.
    assert exit_code == 1
    assert captured.out, f"doctor printed no report; stderr: {captured.err}"
    payload = json.loads(captured.out)
    assert payload["ok"] is False
    assert any(
        check["status"] == "failed" and check["path"] == str(state_path)
        for check in payload["checks"]
    )
