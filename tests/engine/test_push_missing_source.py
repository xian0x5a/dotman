"""Permanent Push treats a missing explicit repository file as an endpoint, not a Render input."""

import pytest

from dotman.sync_base_store import Missing, SyncBaseStore
from dotman.sync_session import Preview
from tests.engine.test_sync_session import make_engine


def push_tracked_scope(engine):
    """Push the tracked scope and return the frozen Observation it published from."""
    with engine.open_push_session(engine.resolve_sync_scope()) as session:
        observation, = session.view.observations
        result = session.execute().result
    assert result.status == "completed", result
    assert observation.repository == Missing()
    return observation


def expected_state(live):
    return "drifted" if live is not None else "directly-in-sync"


@pytest.mark.parametrize("transformed", [False, True])
@pytest.mark.parametrize("live", [b"live", None])
def test_missing_repository_file_publishes_typed_absence(tmp_path, monkeypatch, transformed, live):
    marker = tmp_path / "render-ran"
    render = f'render = "touch {marker}; printf rendered"' if transformed else ""
    engine = make_engine(tmp_path, monkeypatch, [("unit", "both", None, live, render)])
    source = tmp_path / "repo/packages/app/unit"
    destination = tmp_path / "live/unit"
    assert not source.exists()

    assert push_tracked_scope(engine).state == expected_state(live)
    assert not destination.exists()
    assert not source.exists()
    assert not marker.exists()
    with SyncBaseStore.open(engine._tracked_state_context.state_root, "main", read_only=True) as store:
        assert store.read(b"main:app.unit").payload == Missing()


@pytest.mark.parametrize("live", [b"live", None])
def test_missing_patch_capture_source_is_valid_push_endpoint(tmp_path, monkeypatch, live):
    patch_settings = 'render = "jinja"\ncapture = "patch"\n[targets.unit.compare]\nrepo = "render"\nlive = "raw"'
    engine = make_engine(tmp_path, monkeypatch, [("unit", "both", None, live, patch_settings)])
    source = tmp_path / "repo/packages/app/unit"
    destination = tmp_path / "live/unit"
    assert push_tracked_scope(engine).state == expected_state(live)
    assert not source.exists()
    assert not destination.exists()


def test_missing_patch_capture_source_still_validates_static_configuration(tmp_path, monkeypatch):
    invalid_patch_settings = 'capture = "patch"\n[targets.unit.compare]\nrepo = "render"\nlive = "raw"'
    with pytest.raises(ValueError, match='capture = "patch" requires non-raw render'):
        make_engine(tmp_path, monkeypatch, [("unit", "both", None, b"live", invalid_patch_settings)]).resolve_sync_scope()


@pytest.mark.parametrize("live", [b"live", None])
def test_missing_repository_preview_is_read_only(tmp_path, monkeypatch, live):
    engine = make_engine(tmp_path, monkeypatch, [("unit", "both", None, live, 'render = "printf rendered"')])
    source = tmp_path / "repo/packages/app/unit"
    destination = tmp_path / "live/unit"
    state_dir = tmp_path / "state/dotman/repos/main"
    state_before = sorted(state_dir.iterdir())
    with engine.open_push_session(engine.resolve_sync_scope(), preview=True) as session:
        observation, = session.view.observations
        view = session.view
        result = session.dispatch(Preview(view.session_id, view.revision)).result
    assert observation.state == expected_state(live)
    assert result.status == "completed", result
    assert sorted(state_dir.iterdir()) == state_before
    assert not source.exists()
    assert (destination.read_bytes() if live is not None else None) == live


def test_missing_repository_source_deletion_has_restorable_snapshot(tmp_path, monkeypatch):
    from dotman.snapshot import build_restore_actions, execute_restore_action, list_snapshots

    engine = make_engine(tmp_path, monkeypatch, [("unit", "both", None, b"live", "")])
    push_tracked_scope(engine)
    destination = tmp_path / "live/unit"
    assert not destination.exists()
    saved, = list_snapshots(engine.config.snapshots.path)
    assert saved.entries[0].push_action == "delete"
    assert saved.entries[0].existed_before
    for action in build_restore_actions(saved):
        assert execute_restore_action(action).status == "ok"
    assert destination.read_bytes() == b"live"


def test_missing_repository_source_follow_link_deletes_referent_only(tmp_path, monkeypatch):
    from dotman.engine import DotmanEngine

    engine = make_engine(tmp_path, monkeypatch, [("unit", "both", None, None, "")])
    config = engine.config.config_path
    config.write_text(config.read_text() + '\n[symlinks]\nfile_symlink_mode = "follow"\n')
    engine = DotmanEngine.from_config_path(config)
    referent = tmp_path / "referent"
    referent.write_bytes(b"remove")
    link = tmp_path / "live/unit"
    link.parent.mkdir(exist_ok=True)
    link.symlink_to(referent)
    assert push_tracked_scope(engine).state == "drifted"
    assert link.is_symlink()
    assert not referent.exists()


def test_broken_repository_source_symlink_is_not_typed_missing(tmp_path, monkeypatch):
    engine = make_engine(tmp_path, monkeypatch, [("unit", "both", None, b"live", "")])
    (tmp_path / "repo/packages/app/unit").symlink_to("unavailable")
    with engine.open_push_session(engine.resolve_sync_scope()) as session:
        observation, = session.view.observations
        result = session.execute().result
    assert observation.state == "observation-failed"
    assert observation.repository != Missing()
    assert result.status == "failed"
    assert (tmp_path / "live/unit").read_bytes() == b"live"
