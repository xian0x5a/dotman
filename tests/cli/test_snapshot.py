from __future__ import annotations

import json
from pathlib import Path

from dotman.cli import main
from dotman.models import SnapshotConfig, TargetPlan
from dotman.snapshot import create_push_snapshot, list_snapshots
from dotman.sync_session import AuthorizeSymlinkReplacement, SetApproval
from tests.helpers import capture_parser_help, make_package_plan, write_named_manager_config


def test_create_push_snapshot_skips_probe_targets(tmp_path: Path) -> None:
    plan = make_package_plan(
        operation="push",
        repo_name="sandbox",
        package_id="app",
        requested_profile="default",
        target_plans=[
            TargetPlan(
                package_id="app",
                target_name="version",
                repo_path=Path("/repo/app"),
                live_path=Path("/repo/app"),
                action="probe",
                target_kind="probe",
                projection_kind="probe",
                probe_command="exit 0",
            )
        ],
    )

    snapshot = create_push_snapshot(
        [plan],
        SnapshotConfig(enabled=True, path=tmp_path / "snapshots", max_generations=5),
    )

    assert snapshot is None
    assert not (tmp_path / "snapshots").exists()


def _write_snapshot_execution_repo(
    repo_root: Path,
    *,
    package_id: str = "app",
    guard_push_exit_code: int | None = None,
) -> None:
    package_root = repo_root / "packages" / package_id
    (package_root / "files").mkdir(parents=True)
    (repo_root / "profiles").mkdir(parents=True)

    (repo_root / "profiles" / "default.toml").write_text("", encoding="utf-8")
    (package_root / "files" / "config.txt").write_text("repo value\n", encoding="utf-8")
    hooks = []
    if guard_push_exit_code is not None:
        if guard_push_exit_code == 100:
            guard_push = "printf 'guard push\\n'; exit 100"
        elif guard_push_exit_code == 0:
            guard_push = "printf 'guard push\\n'"
        else:
            guard_push = f"printf 'guard push failed\\n'; exit {guard_push_exit_code}"
        hooks = ["", "[hooks]", f'guard_push = "{guard_push}"']
    (package_root / "package.toml").write_text(
        "\n".join(
            [
                f'id = "{package_id}"',
                "",
                "[targets.config]",
                'source = "files/config.txt"',
                f'path = "~/.config/{package_id}/config.txt"',
                *hooks,
                '',
            ]
        ),
        encoding="utf-8",
    )


def _write_tracked_binding(state_root: Path, *, repo_name: str = "fixture", selector: str = "app") -> None:
    state_dir = state_root / "dotman" / "repos" / repo_name
    state_dir.mkdir(parents=True, exist_ok=True)
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


def _write_snapshot_config(tmp_path: Path, repo_root: Path, *, max_generations: int = 5) -> Path:
    config_path = write_named_manager_config(tmp_path, {"fixture": repo_root})
    with config_path.open("a", encoding="utf-8") as handle:
        handle.write(
            "\n".join(
                [
                    "[snapshots]",
                    f'path = "{tmp_path / "snapshots"}"',
                    f"max_generations = {max_generations}",
                    "",
                ]
            )
        )
    return config_path


def test_restore_help_lists_dry_run_and_full_path_flags(capsys) -> None:
    output = capture_parser_help(capsys, "restore")
    assert "usage: dotman restore [-h] [-d] [--full-path] [<snapshot>]" in output
    assert "-d, --dry-run" in output
    assert "--full-path" in output


def test_list_help_includes_snapshots_subcommand(capsys) -> None:
    output = capture_parser_help(capsys, "list")
    assert "List available snapshots" in output


def test_info_help_includes_snapshot_subcommand(capsys) -> None:
    output = capture_parser_help(capsys, "info")
    assert "Show snapshot details" in output


def test_info_snapshot_help_lists_full_path_flag(capsys) -> None:
    output = capture_parser_help(capsys, "info", "snapshot")
    assert "usage: dotman info snapshot [-h] [--full-path] <snapshot>" in output
    assert "--full-path" in output


def _authorize_symlink_replacement_in_command_deck(monkeypatch) -> None:
    """Symlinked live files need explicit Command Deck authorization before Push replaces them."""
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)

    def authorize(session, *, use_color):
        for row in session.view.rows:
            view = session.view
            session.dispatch(AuthorizeSymlinkReplacement(view.session_id, view.revision, row.row_id))
            view = session.view
            session.dispatch(SetApproval(view.session_id, view.revision, row.row_id, True))
        return all(row.approved and row.symlink_authorized for row in session.view.rows)

    monkeypatch.setattr("dotman.sync_deck.run_command_deck", authorize)


def test_push_execute_creates_snapshot_and_restore_restores_latest_snapshot(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    repo_root = tmp_path / "repo"
    _write_snapshot_execution_repo(repo_root)
    config_path = _write_snapshot_config(tmp_path, repo_root)
    _write_tracked_binding(tmp_path / "state")

    live_path = home / ".config" / "app" / "config.txt"
    live_path.parent.mkdir(parents=True, exist_ok=True)
    live_path.write_text("before push\n", encoding="utf-8")

    push_exit_code = main(["--no-confirm", "--config", str(config_path), "push"])

    assert push_exit_code == 0
    assert live_path.read_text(encoding="utf-8") == "repo value\n"
    snapshots = list_snapshots(tmp_path / "snapshots")
    assert len(snapshots) == 1
    assert snapshots[0].status == "applied"
    assert snapshots[0].entries[0].live_path == live_path
    assert snapshots[0].entries[0].existed_before is True
    assert snapshots[0].entries[0].push_action == "update"
    assert snapshots[0].entries[0].content_path is not None
    assert (snapshots[0].root / snapshots[0].entries[0].content_path).read_text(encoding="utf-8") == "before push\n"

    live_path.write_text("mutated after push\n", encoding="utf-8")

    capsys.readouterr()
    dry_run_exit_code = main(["--no-confirm", "--config", str(config_path), "--json", "restore", "--dry-run"])

    assert dry_run_exit_code == 0
    dry_run_output = capsys.readouterr().out
    dry_run_payload = json.loads(dry_run_output)
    assert dry_run_payload["operation"] == "restore"
    assert ("roll" + "back") not in dry_run_output
    assert live_path.read_text(encoding="utf-8") == "mutated after push\n"

    restore_exit_code = main(["--no-confirm", "--config", str(config_path), "restore"])

    assert restore_exit_code == 0
    assert live_path.read_text(encoding="utf-8") == "before push\n"
    restored_snapshots = list_snapshots(tmp_path / "snapshots")
    assert restored_snapshots[0].status == "applied"
    assert restored_snapshots[0].restore_count == 1
    assert restored_snapshots[0].last_restored_at is not None
    assert "executing restore" in capsys.readouterr().out


def test_push_execute_creates_snapshot_only_when_first_live_mutation_begins(
    tmp_path: Path,
    monkeypatch,
) -> None:
    home = tmp_path / "home"
    data_home = tmp_path / "data"
    home.mkdir()
    data_home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))

    guarded_repo = tmp_path / "guarded-repo"
    mutating_repo = tmp_path / "mutating-repo"
    _write_snapshot_execution_repo(guarded_repo, package_id="alpha-app", guard_push_exit_code=100)
    _write_snapshot_execution_repo(mutating_repo, package_id="beta-app")
    config_path = write_named_manager_config(tmp_path, {"alpha": guarded_repo, "beta": mutating_repo})
    _write_tracked_binding(tmp_path / "state", repo_name="alpha", selector="alpha-app")
    _write_tracked_binding(tmp_path / "state", repo_name="beta", selector="beta-app")

    guarded_live_path = home / ".config" / "alpha-app" / "config.txt"
    guarded_live_path.parent.mkdir(parents=True, exist_ok=True)
    guarded_live_path.write_text("before alpha\n", encoding="utf-8")

    mutating_live_path = home / ".config" / "beta-app" / "config.txt"
    mutating_live_path.parent.mkdir(parents=True, exist_ok=True)
    mutating_live_path.write_text("before beta\n", encoding="utf-8")

    push_exit_code = main(["--no-confirm", "--config", str(config_path), "push"])

    assert push_exit_code == 0
    assert guarded_live_path.read_text(encoding="utf-8") == "before alpha\n"
    assert mutating_live_path.read_text(encoding="utf-8") == "repo value\n"
    snapshots = list_snapshots(data_home / "dotman" / "snapshots")
    assert len(snapshots) == 1
    assert snapshots[0].status == "applied"


def test_push_execute_replaces_symlinked_target_and_restore_restores_link(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    repo_root = tmp_path / "repo"
    _write_snapshot_execution_repo(repo_root)
    config_path = _write_snapshot_config(tmp_path, repo_root)
    _write_tracked_binding(tmp_path / "state")

    live_path = home / ".config" / "app" / "config.txt"
    live_path.parent.mkdir(parents=True, exist_ok=True)
    real_live_path = live_path.parent / "config-real.txt"
    real_live_path.write_text("before push\n", encoding="utf-8")
    live_path.symlink_to(real_live_path)

    _authorize_symlink_replacement_in_command_deck(monkeypatch)

    push_exit_code = main(["--config", str(config_path), "push"])

    assert push_exit_code == 0
    assert live_path.is_file()
    assert not live_path.is_symlink()
    assert live_path.read_text(encoding="utf-8") == "repo value\n"
    snapshots = list_snapshots(tmp_path / "snapshots")
    assert len(snapshots) == 1
    assert snapshots[0].entries[0].path_kind == "symlink"
    assert snapshots[0].entries[0].symlink_target == str(real_live_path)

    capsys.readouterr()
    restore_exit_code = main(["--config", str(config_path), "--no-confirm", "--json", "restore"])

    assert restore_exit_code == 0
    assert live_path.is_symlink()
    assert live_path.read_text(encoding="utf-8") == "before push\n"
    restore_output = capsys.readouterr().out
    restore_payload = json.loads(restore_output)
    assert restore_payload["operation"] == "restore"
    assert ("roll" + "back") not in restore_output



def test_push_execute_follows_symlinked_target_and_restore_restores_target_file(
    tmp_path: Path,
    monkeypatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    repo_root = tmp_path / "repo"
    _write_snapshot_execution_repo(repo_root)
    config_path = _write_snapshot_config(tmp_path, repo_root)
    _write_tracked_binding(tmp_path / "state")

    live_path = home / ".config" / "app" / "config.txt"
    live_path.parent.mkdir(parents=True, exist_ok=True)
    real_live_path = live_path.parent / "config-real.txt"
    real_live_path.write_text("before push\n", encoding="utf-8")
    live_path.symlink_to(real_live_path)

    push_exit_code = main(["--no-confirm", "--config", str(config_path), "--file-symlink-mode", "follow", "push"])

    assert push_exit_code == 0
    assert live_path.is_symlink()
    assert live_path.read_text(encoding="utf-8") == "repo value\n"
    assert real_live_path.read_text(encoding="utf-8") == "repo value\n"
    snapshots = list_snapshots(tmp_path / "snapshots")
    assert len(snapshots) == 1
    assert snapshots[0].entries[0].preserve_symlink_identity is False
    assert snapshots[0].entries[0].restore_path == real_live_path.resolve()

    restore_exit_code = main(["--no-confirm", "--config", str(config_path), "restore"])

    assert restore_exit_code == 0
    assert live_path.is_symlink()
    assert real_live_path.read_text(encoding="utf-8") == "before push\n"
    assert live_path.read_text(encoding="utf-8") == "before push\n"



def test_push_execute_replaces_broken_symlink_and_restore_restores_link(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    repo_root = tmp_path / "repo"
    _write_snapshot_execution_repo(repo_root)
    config_path = _write_snapshot_config(tmp_path, repo_root)
    _write_tracked_binding(tmp_path / "state")

    live_path = home / ".config" / "app" / "config.txt"
    live_path.parent.mkdir(parents=True, exist_ok=True)
    broken_target = live_path.parent / "missing-config.txt"
    live_path.symlink_to(broken_target)

    _authorize_symlink_replacement_in_command_deck(monkeypatch)

    push_exit_code = main(["--config", str(config_path), "push"])

    assert push_exit_code == 0
    assert live_path.is_file()
    assert live_path.read_text(encoding="utf-8") == "repo value\n"
    snapshots = list_snapshots(tmp_path / "snapshots")
    assert len(snapshots) == 1
    entry = snapshots[0].entries[0]
    assert entry.path_kind == "symlink"
    assert entry.symlink_target == str(broken_target)
    assert entry.content_path is None
    assert entry.preserve_symlink_identity is True

    restore_exit_code = main(["--config", str(config_path), "--no-confirm", "--json", "restore"])

    assert restore_exit_code == 0
    assert live_path.is_symlink()
    assert live_path.resolve(strict=False) == broken_target
    capsys.readouterr()



def test_push_execute_creates_missing_file_and_restore_deletes_it(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    repo_root = tmp_path / "repo"
    _write_snapshot_execution_repo(repo_root)
    config_path = _write_snapshot_config(tmp_path, repo_root)
    _write_tracked_binding(tmp_path / "state")

    live_path = home / ".config" / "app" / "config.txt"
    assert not live_path.exists()

    push_exit_code = main(["--no-confirm", "--config", str(config_path), "push"])

    assert push_exit_code == 0
    assert live_path.read_text(encoding="utf-8") == "repo value\n"
    snapshots = list_snapshots(tmp_path / "snapshots")
    assert len(snapshots) == 1
    assert snapshots[0].entries[0].live_path == live_path
    assert snapshots[0].entries[0].existed_before is False
    assert snapshots[0].entries[0].push_action == "create"
    assert snapshots[0].entries[0].content_path is None

    restore_exit_code = main(["--no-confirm", "--config", str(config_path), "restore"])

    assert restore_exit_code == 0
    assert not live_path.exists()



def test_push_dry_run_does_not_create_snapshot(tmp_path: Path, monkeypatch, capsys) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    repo_root = tmp_path / "repo"
    _write_snapshot_execution_repo(repo_root)
    config_path = _write_snapshot_config(tmp_path, repo_root)
    _write_tracked_binding(tmp_path / "state")

    exit_code = main(["--no-confirm", "--config", str(config_path), "--json", "push", "--dry-run"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "dry-run"
    assert payload["operation"] == "push"
    assert list_snapshots(tmp_path / "snapshots") == []


def test_push_snapshot_retention_prunes_oldest_generations(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    repo_root = tmp_path / "repo"
    _write_snapshot_execution_repo(repo_root)
    config_path = _write_snapshot_config(tmp_path, repo_root, max_generations=1)
    _write_tracked_binding(tmp_path / "state")

    live_path = home / ".config" / "app" / "config.txt"
    live_path.parent.mkdir(parents=True, exist_ok=True)

    live_path.write_text("first\n", encoding="utf-8")
    assert main(["--no-confirm", "--config", str(config_path), "push"]) == 0
    first_snapshot = list_snapshots(tmp_path / "snapshots")[0].snapshot_id

    live_path.write_text("second\n", encoding="utf-8")
    assert main(["--no-confirm", "--config", str(config_path), "push"]) == 0

    snapshots = list_snapshots(tmp_path / "snapshots")
    assert len(snapshots) == 1
    assert snapshots[0].snapshot_id != first_snapshot


def test_restore_without_snapshot_argument_restores_latest_snapshot(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    repo_root = tmp_path / "repo"
    _write_snapshot_execution_repo(repo_root)
    config_path = _write_snapshot_config(tmp_path, repo_root)
    _write_tracked_binding(tmp_path / "state")

    live_path = home / ".config" / "app" / "config.txt"
    live_path.parent.mkdir(parents=True, exist_ok=True)

    live_path.write_text("first\n", encoding="utf-8")
    assert main(["--no-confirm", "--config", str(config_path), "push"]) == 0

    live_path.write_text("second\n", encoding="utf-8")
    assert main(["--no-confirm", "--config", str(config_path), "push"]) == 0

    live_path.write_text("mutated after push\n", encoding="utf-8")

    restore_exit_code = main(["--no-confirm", "--config", str(config_path), "restore"])

    assert restore_exit_code == 0
    assert live_path.read_text(encoding="utf-8") == "second\n"


def test_restore_latest_argument_restores_latest_snapshot(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    repo_root = tmp_path / "repo"
    _write_snapshot_execution_repo(repo_root)
    config_path = _write_snapshot_config(tmp_path, repo_root)
    _write_tracked_binding(tmp_path / "state")

    live_path = home / ".config" / "app" / "config.txt"
    live_path.parent.mkdir(parents=True, exist_ok=True)

    live_path.write_text("first\n", encoding="utf-8")
    assert main(["--no-confirm", "--config", str(config_path), "push"]) == 0

    live_path.write_text("second\n", encoding="utf-8")
    assert main(["--no-confirm", "--config", str(config_path), "push"]) == 0

    live_path.write_text("mutated after push\n", encoding="utf-8")

    restore_exit_code = main(["--no-confirm", "--config", str(config_path), "restore", "latest"])

    assert restore_exit_code == 0
    assert live_path.read_text(encoding="utf-8") == "second\n"


def test_info_snapshot_latest_argument_resolves_latest_snapshot(tmp_path: Path, monkeypatch, capsys) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    repo_root = tmp_path / "repo"
    _write_snapshot_execution_repo(repo_root)
    config_path = _write_snapshot_config(tmp_path, repo_root)
    _write_tracked_binding(tmp_path / "state")

    live_path = home / ".config" / "app" / "config.txt"
    live_path.parent.mkdir(parents=True, exist_ok=True)

    live_path.write_text("first\n", encoding="utf-8")
    assert main(["--no-confirm", "--config", str(config_path), "push"]) == 0

    live_path.write_text("second\n", encoding="utf-8")
    assert main(["--no-confirm", "--config", str(config_path), "push"]) == 0
    latest_snapshot = list_snapshots(tmp_path / "snapshots")[0].snapshot_id
    capsys.readouterr()

    exit_code = main(["--no-confirm", "--config", str(config_path), "--json", "info", "snapshot", "latest"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["snapshot"]["snapshot_id"] == latest_snapshot


def test_list_snapshots_cli_emits_human_summary(tmp_path: Path, monkeypatch, capsys) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr("sys.stdout.isatty", lambda: False)

    repo_root = tmp_path / "repo"
    _write_snapshot_execution_repo(repo_root)
    config_path = _write_snapshot_config(tmp_path, repo_root)
    _write_tracked_binding(tmp_path / "state")

    live_path = home / ".config" / "app" / "config.txt"
    live_path.parent.mkdir(parents=True, exist_ok=True)
    live_path.write_text("before push\n", encoding="utf-8")
    assert main(["--no-confirm", "--config", str(config_path), "push"]) == 0
    capsys.readouterr()

    exit_code = main(["--no-confirm", "--config", str(config_path), "list", "snapshots"])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert ":: snapshots" in output
    assert "ref:" in output
    assert "status:" in output
    assert "paths:" in output
    assert "applied" in output


def test_list_snapshots_cli_emits_json(tmp_path: Path, monkeypatch, capsys) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    repo_root = tmp_path / "repo"
    _write_snapshot_execution_repo(repo_root)
    config_path = _write_snapshot_config(tmp_path, repo_root)
    _write_tracked_binding(tmp_path / "state")

    live_path = home / ".config" / "app" / "config.txt"
    live_path.parent.mkdir(parents=True, exist_ok=True)
    live_path.write_text("before push\n", encoding="utf-8")
    assert main(["--no-confirm", "--config", str(config_path), "push"]) == 0
    capsys.readouterr()

    exit_code = main(["--no-confirm", "--config", str(config_path), "--json", "list", "snapshots"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["operation"] == "list-snapshots"
    assert payload["snapshots"][0]["status"] == "applied"
    assert payload["snapshots"][0]["entry_count"] == 1
    assert payload["snapshots"][0]["restore_count"] == 0
    assert payload["snapshots"][0]["last_restored_at"] is None


def test_info_snapshot_cli_uses_full_paths_when_requested(tmp_path: Path, monkeypatch, capsys) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr("sys.stdout.isatty", lambda: False)

    repo_root = tmp_path / "repo"
    _write_snapshot_execution_repo(repo_root, package_id="very-long-app")
    config_path = _write_snapshot_config(tmp_path, repo_root)
    _write_tracked_binding(tmp_path / "state", selector="very-long-app")

    live_path = home / ".config" / "very-long-app" / "config.txt"
    live_path.parent.mkdir(parents=True, exist_ok=True)
    live_path.write_text("before push\n", encoding="utf-8")
    assert main(["--no-confirm", "--config", str(config_path), "push"]) == 0
    snapshot_id = list_snapshots(tmp_path / "snapshots")[0].snapshot_id
    capsys.readouterr()

    exit_code = main(["--no-confirm", "--config", str(config_path), "info", "snapshot", "--full-path", snapshot_id])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert str(live_path) in output
    assert ".../very-long-app/config.txt" not in output
    assert "reason: before update (push)" in output
    assert "provenance:" in output
    assert "fixture:very-long-app.config" in output
    assert "target:" not in output
    assert "binding:" not in output
    assert "[update]" not in output


def test_info_snapshot_cli_emits_json_with_recorded_paths(tmp_path: Path, monkeypatch, capsys) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    repo_root = tmp_path / "repo"
    _write_snapshot_execution_repo(repo_root)
    config_path = _write_snapshot_config(tmp_path, repo_root)
    _write_tracked_binding(tmp_path / "state")

    live_path = home / ".config" / "app" / "config.txt"
    live_path.parent.mkdir(parents=True, exist_ok=True)
    live_path.write_text("before push\n", encoding="utf-8")
    assert main(["--no-confirm", "--config", str(config_path), "push"]) == 0
    snapshot_id = list_snapshots(tmp_path / "snapshots")[0].snapshot_id
    capsys.readouterr()

    exit_code = main(["--no-confirm", "--config", str(config_path), "--json", "info", "snapshot", snapshot_id])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["operation"] == "info-snapshot"
    assert payload["snapshot"]["snapshot_id"] == snapshot_id
    assert payload["snapshot"]["entries"][0]["live_path"] == str(live_path)
    assert payload["snapshot"]["restore_count"] == 0
