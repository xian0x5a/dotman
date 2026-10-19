from __future__ import annotations

import re
from pathlib import Path

import pytest

from dotman.cli_emit import HumanExecutionRenderer
from dotman.operation_runner import (
    RestoreActionFinished,
    RestoreActionStarted,
    RestoreOperationFinished,
    RestoreOperationStarted,
)
from dotman.snapshot import RestoreAction, RestoreActionResult, RestoreResult, SnapshotRecord


@pytest.mark.parametrize("use_color", [False, True])
def test_human_renderer_consumes_restore_events_without_mutating_filesystem(tmp_path: Path, capsys, use_color) -> None:
    live_path = tmp_path / "live" / "config.txt"
    snapshot = SnapshotRecord(
        snapshot_id="snapshot-1",
        created_at="2026-07-29T00:00:00Z",
        status="applied",
        root=tmp_path / "snapshots" / "snapshot-1",
        entries=(),
    )
    action = RestoreAction(
        live_path=live_path,
        snapshot_path=snapshot.root / "restore" / "config.txt",
        action="create",
        before_bytes=b"",
        after_bytes=b"restored\n",
        desired_mode=0o600,
    )
    action_result = RestoreActionResult(action=action, status="ok")
    result = RestoreResult(snapshot=snapshot, actions=(action_result,), status="ok")
    renderer = HumanExecutionRenderer(full_paths=True, use_color=use_color)

    renderer.render_restore_event(RestoreOperationStarted(snapshot=snapshot, action_count=1))
    renderer.render_restore_event(RestoreActionStarted(action=action, index=1, total=1))
    renderer.render_restore_event(RestoreActionFinished(result=action_result, index=1, total=1))
    renderer.render_restore_event(RestoreOperationFinished(result))

    # Color must not change the layout: the action column pads the same either way.
    output = re.sub(r"\x1b\[[0-9;]*m", "", capsys.readouterr().out)
    assert ":: executing restore" in output
    assert f"[1/1] create      {live_path}" in output
    assert "ok" in output
    assert not live_path.exists()
