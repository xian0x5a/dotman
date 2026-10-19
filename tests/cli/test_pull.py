from __future__ import annotations

import json
from pathlib import Path

import pytest

from dotman.cli import main
from dotman.sync_session import SetApproval
from tests.engine.test_sync_session import make_engine
from tests.helpers import write_tracked_packages_state


@pytest.fixture
def pull_repo(tmp_path, monkeypatch):
    make_engine(tmp_path, monkeypatch, [
        ("first", "both", b"repository first", b"live first", ""),
        ("second", "both", b"repository second", b"live second", ""),
    ])
    return tmp_path


@pytest.mark.parametrize(
    "scopes,identities",
    [
        ([], ["main:app.first", "main:app.second"]),
        (["main:app"], ["main:app.first", "main:app.second"]),
        (["main:app.first"], ["main:app.first"]),
    ],
)
def test_pull_cli_previews_tracked_scopes_without_writes(
    pull_repo: Path, capsys, scopes, identities,
):
    tracked = pull_repo / "state/dotman/repos/main/tracked-packages.toml"
    tracked_before = tracked.read_bytes()

    assert main([
        "--config", str(pull_repo / "config.toml"), "--json", "--no-confirm",
        "pull", "--dry-run", *scopes,
    ]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["operation"] == "pull"
    assert payload["mode"] == "dry-run"
    assert payload["scope"] == identities
    assert [unit["identity"] for unit in payload["sync_units"]] == identities
    for unit in payload["sync_units"]:
        assert unit["approved"]
        assert unit["primary_source_change"]["kind"] == "write"
        assert unit["resolution_intent"] == "use-live"
        assert unit["allowed_intents"] == []
    assert tracked.read_bytes() == tracked_before
    for name in ("first", "second"):
        assert (pull_repo / f"repo/packages/app/{name}").read_bytes() == f"repository {name}".encode()
        assert (pull_repo / f"live/{name}").read_bytes() == f"live {name}".encode()


def test_pull_cli_human_preview_uses_canonical_target_identity(pull_repo, capsys):
    assert main([
        "--config", str(pull_repo / "config.toml"), "--no-confirm",
        "pull", "--dry-run", "main:app.first",
    ]) == 0

    output = capsys.readouterr().out
    assert ":: Pull preview" in output
    assert "[would-apply] main:app.first" in output
    assert "repository write" in output
    assert "main:app.second" not in output


def test_pull_cli_command_deck_can_opt_out_before_execution(
    pull_repo, monkeypatch, capsys,
):
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)

    def confirm_subset(session, *, use_color):
        view = session.view
        assert [row.row_id for row in view.rows] == ["main:app.first", "main:app.second"]
        assert all(row.approved for row in view.rows)
        session.dispatch(SetApproval(
            view.session_id, view.revision, "main:app.second", False,
        ))
        return True

    monkeypatch.setattr("dotman.sync_deck.run_command_deck", confirm_subset)
    assert main(["--config", str(pull_repo / "config.toml"), "pull"]) == 0
    assert (pull_repo / "repo/packages/app/first").read_bytes() == b"live first"
    assert (pull_repo / "repo/packages/app/second").read_bytes() == b"repository second"
    output = capsys.readouterr().out
    assert "[1/1] update" in output
    # Opted-out entries without diagnostics are noise in the final log.
    assert "main:app.second" not in output


@pytest.mark.parametrize("interrupt", [False, True])
def test_pull_cli_returns_130_when_command_deck_aborts(
    pull_repo, monkeypatch, capsys, interrupt,
):
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)

    def abort(session, *, use_color):
        if interrupt:
            raise KeyboardInterrupt
        return False

    monkeypatch.setattr("dotman.sync_deck.run_command_deck", abort)
    assert main(["--config", str(pull_repo / "config.toml"), "pull"]) == 130
    assert "Pull aborted" in capsys.readouterr().err
    for name in ("first", "second"):
        assert (pull_repo / f"repo/packages/app/{name}").read_bytes() == f"repository {name}".encode()


def test_pull_cli_reports_command_deck_crash_as_failure_not_abort(
    pull_repo, monkeypatch, capsys,
):
    from dotman.sync_deck import SyncDeckApp

    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    # The real Deck runs; only the terminal driver is swapped for Textual's
    # headless one, since pytest has no terminal to draw on.
    run_on_terminal = SyncDeckApp.run
    monkeypatch.setattr(SyncDeckApp, "run", lambda self, **kwargs: run_on_terminal(self, **{**kwargs, "headless": True}))

    def crash(self):
        raise RuntimeError("deck bug")

    # Raising inside the app's event loop is how a Deck or provider bug surfaces.
    monkeypatch.setattr(SyncDeckApp, "on_mount", crash)

    assert main(["--config", str(pull_repo / "config.toml"), "pull"]) == 1
    captured = capsys.readouterr()
    summary = [line for line in captured.out.splitlines() if line.startswith(":: ") and line != ":: Pull"]
    assert summary and summary[-1].startswith(":: failed"), summary
    # Exact line match: the Textual traceback above it quotes test source lines.
    assert "Pull aborted" not in captured.err.splitlines()
    for name in ("first", "second"):
        assert (pull_repo / f"repo/packages/app/{name}").read_bytes() == f"repository {name}".encode()


def test_pull_cli_accepts_package_owned_by_tracked_root(pull_repo, capsys):
    root = pull_repo / "repo/packages/root"
    root.mkdir()
    (root / "package.toml").write_text('depends = ["app"]\n')
    write_tracked_packages_state(
        pull_repo / "state", repo_name="main", entries=[("root", "default")],
    )

    assert main([
        "--config", str(pull_repo / "config.toml"), "--json", "--no-confirm",
        "pull", "--dry-run", "main:app",
    ]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["scope"] == ["main:app.first", "main:app.second"]
    assert all(unit["approved"] for unit in payload["sync_units"])
