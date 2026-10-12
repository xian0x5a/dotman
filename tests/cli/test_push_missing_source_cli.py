"""Permanent Push CLI reports missing repository sources as deletion/noop work."""

import json

import pytest

from dotman.cli import main
from tests.engine.test_sync_session import make_engine


@pytest.mark.parametrize("live,expected_action", [(b"live", "delete"), (None, "noop")])
@pytest.mark.parametrize("patch_capture", [False, True])
def test_push_preview_missing_source_preserves_live_and_checkpoint(
    tmp_path, monkeypatch, capsys, live, expected_action, patch_capture,
):
    settings = (
        'render = "jinja"\ncapture = "patch"\n[targets.unit.compare]\nrepo = "render"\nlive = "raw"'
        if patch_capture else 'render = "printf generated"'
    )
    engine = make_engine(tmp_path, monkeypatch, [("unit", "both", None, live, settings)])
    args = ["--config", str(engine.config.config_path), "--json", "--no-confirm", "push", "--dry-run", "main:app"]
    assert main(args) == 0
    result = json.loads(capsys.readouterr().out)
    if expected_action == "delete":
        unit, = result["sync_units"]
        assert unit["result"] == "would-converge"
        assert [effect["kind"] for effect in unit["effects"]] == ["delete"]
        assert result["summary"]["live_deletions"] == 1
    else:
        assert result["sync_units"] == []
        assert result["summary"]["in_sync_units"] == 1
    source = tmp_path / "repo/packages/app/unit"
    destination = tmp_path / "live/unit"
    assert not source.exists()
    assert (destination.read_bytes() if live is not None else None) == live
    assert not list((tmp_path / "state/dotman/repos/main").glob("*.json"))
