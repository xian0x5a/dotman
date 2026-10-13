from __future__ import annotations

import json

import pytest

from dotman.restore_commands import RestoreCommandRunner
from dotman.sync_deck_command import PushDeckCommandRunner
from tests.cli.test_sync_deck_command import arguments
from tests.engine.test_sync_session import make_engine


def test_restore_command_runner_declares_only_restore() -> None:
    runner = RestoreCommandRunner(
        config_loader=lambda _config_path: pytest.fail(
            "declaring commands must not load config"
        ),
        use_color=False,
    )

    assert runner.command_names == frozenset({"restore"})


def test_push_conflicts_with_live_sync_session_and_releases_lock(tmp_path, monkeypatch, capsys):
    from tests.engine.test_sync_session import open_session

    engine = make_engine(tmp_path, monkeypatch, [("unit", "both", b"repo", b"live", "")])
    runner = PushDeckCommandRunner(engine_factory=lambda _: engine, use_color=False)
    with open_session(engine, preview=False):
        assert runner.run(arguments(command="push", dry_run=False)) == 1
        assert json.loads(capsys.readouterr().out)["summary"]["diagnostics"][0]["code"] == "operation-busy"
        assert (tmp_path / "live/unit").read_bytes() == b"live"
        # A read-only preview remains possible while the lock is held.
        assert runner.run(arguments(command="push", dry_run=True)) == 0
        capsys.readouterr()
    assert runner.run(arguments(command="push", dry_run=False)) == 0
    assert (tmp_path / "live/unit").read_bytes() == b"repo"


def test_push_holds_lock_during_observation_and_releases_after_failure(tmp_path, monkeypatch, capsys):
    from dotman.config import default_state_root
    from dotman.operation_lock import OperationBusy, OperationLock
    from dotman.push_session import PushSession

    engine = make_engine(tmp_path, monkeypatch, [("unit", "both", b"repo", b"live", "")])

    def fail_observation(*args, **kwargs):
        with pytest.raises(OperationBusy):
            OperationLock.acquire(default_state_root())
        raise ValueError("observation failed")

    monkeypatch.setattr(PushSession, "_observe", staticmethod(fail_observation))
    runner = PushDeckCommandRunner(engine_factory=lambda _: engine, use_color=False)
    assert runner.run(arguments(command="push", dry_run=False)) == 1
    assert json.loads(capsys.readouterr().out)["summary"]["diagnostics"][0]["message"] == "observation failed"
    with OperationLock.acquire(default_state_root()):
        pass
