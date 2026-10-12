import json
import os
import pty
import subprocess
import sys

import pytest

from dotman.sync_deck_command import PullDeckCommandRunner
from tests.cli.test_sync_deck_command import arguments
from tests.engine.test_sync_session import make_engine


@pytest.mark.parametrize("hook", ["pre_pull", "post_pull"])
def test_no_confirm_pull_propagates_mode_to_hooks(tmp_path, monkeypatch, capsys, hook):
    from dotman.cli import main

    marker = tmp_path / "hook-mode"
    engine = make_engine(tmp_path, monkeypatch, [
        ("unit", "both", b"repo", b"live",
         f'[targets.unit.hooks]\n{hook} = '
         + json.dumps(f'printf "$DOTMAN_NO_CONFIRM" > {marker}')),
    ])
    assert main([
        "--config", str(engine.config.config_path), "--json", "--no-confirm", "pull",
    ]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "completed"
    assert marker.read_text() == "1"


def test_no_confirm_pull_rejects_tty_hook_even_with_a_terminal(tmp_path, monkeypatch):
    marker = tmp_path / "tty-launched"
    engine = make_engine(tmp_path, monkeypatch, [
        ("unit", "both", b"repo", b"live",
         '[targets.unit.hooks]\npre_pull = { run = '
         + json.dumps(f"touch {marker}") + ', io = "tty" }'),
    ])
    # A pipe-only probe would fail the terminal check even if --no-confirm policy
    # were lost. A real PTY proves that the operation policy prevents launch.
    master, slave = pty.openpty()
    try:
        process = subprocess.Popen(
            [sys.executable, "-m", "dotman.cli", "--config",
             str(engine.config.config_path), "--no-confirm", "pull"],
            stdin=slave, stdout=slave, stderr=slave, start_new_session=True,
        )
        try:
            exit_code = process.wait(timeout=5)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
        assert exit_code == 1
        assert not marker.exists()
        assert (tmp_path / "repo/packages/app/unit").read_bytes() == b"repo"
    finally:
        os.close(master)
        os.close(slave)


@pytest.mark.parametrize("dry_run", [False, True])
def test_no_confirm_pull_failure_never_applies_healthy_subset(tmp_path, monkeypatch, capsys, dry_run):
    engine = make_engine(tmp_path, monkeypatch, [
        ("bad", "both", b"repo", b"live", 'capture = "exit 9"\ncompare = {repo = "raw", live = "raw"}'),
        ("good", "both", b"repo", b"live", ""),
    ])
    runner = PullDeckCommandRunner(engine_factory=lambda _: engine, use_color=False)
    assert runner.run(arguments(command="pull", dry_run=dry_run)) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["operation"] == "pull"
    assert payload["sync_units"][0]["approved"] is False
    assert payload["sync_units"][1]["approved"] is True
    assert payload["stages"] == []
    assert (tmp_path / "repo/packages/app/good").read_bytes() == b"repo"


def test_no_confirm_pull_reports_applied_not_converged(tmp_path, monkeypatch, capsys):
    engine = make_engine(tmp_path, monkeypatch, [("unit", "both", b"repo", b"live", "")])
    runner = PullDeckCommandRunner(engine_factory=lambda _: engine, use_color=False)
    assert runner.run(arguments(command="pull", dry_run=False)) == 0
    payload = json.loads(capsys.readouterr().out)
    unit = payload["sync_units"][0]
    assert unit["result"] == "applied"
    assert unit["resolution_intent"] is None
    assert unit["allowed_intents"] == []
    assert unit["base"]["acknowledged"] is True
    assert all(step["stage"] == "repository-apply" for step in payload["stages"])


def test_pull_parser_accepts_exact_multiple_scopes():
    from dotman.cli_parser import build_parser
    args = build_parser().parse_args(["--no-confirm", "pull", "main:app.a", "main:app.b"])
    assert args.scopes == ["main:app.a", "main:app.b"]


def test_pull_conflicts_with_live_sync_session_and_releases_lock(tmp_path, monkeypatch, capsys):
    from tests.engine.test_sync_session import open_session

    engine = make_engine(tmp_path, monkeypatch, [("unit", "both", b"repo", b"live", "")])
    runner = PullDeckCommandRunner(engine_factory=lambda _: engine, use_color=False)
    with open_session(engine, preview=False):
        assert runner.run(arguments(command="pull", dry_run=False)) == 1
        assert json.loads(capsys.readouterr().out)["summary"]["diagnostics"][0]["code"] == "operation-busy"
    assert runner.run(arguments(command="pull", dry_run=False)) == 0


def test_pull_releases_lock_after_observation_failure(tmp_path, monkeypatch, capsys):
    from dotman.config import default_state_root
    from dotman.operation_lock import OperationBusy, OperationLock
    from dotman.pull_session import PullSession

    engine = make_engine(tmp_path, monkeypatch, [("unit", "both", b"repo", b"live", "")])

    def fail_observation(*args, **kwargs):
        with pytest.raises(OperationBusy):
            OperationLock.acquire(default_state_root())
        raise ValueError("observation failed")

    monkeypatch.setattr(PullSession, "_observe", staticmethod(fail_observation))
    runner = PullDeckCommandRunner(engine_factory=lambda _: engine, use_color=False)
    assert runner.run(arguments(command="pull", dry_run=False)) == 1
    assert json.loads(capsys.readouterr().out)["summary"]["diagnostics"][0]["message"] == "observation failed"
    with OperationLock.acquire(default_state_root()):
        pass
