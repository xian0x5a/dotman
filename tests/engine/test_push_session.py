from __future__ import annotations

import pytest

from dotman.sync_session import CommandRejected, Preview, SetResolutionIntent
from tests.cli.test_sync_base_inspection import store_record
from tests.engine.test_sync_session import make_engine


def read_optional(path):
    return path.read_bytes() if path.exists() else None


@pytest.mark.parametrize("policy,source,live,published", [
    ("both", b"repo", b"live", b"repo"),
    ("push-only", b"repo", b"live", b"repo"),
    ("push-only", b"new", None, b"new"),
    ("push-only", None, b"old", None),
    ("push-only-delete", b"repo", b"live", None),
])
def test_push_fixed_opt_out_publishes_repository_outcome(tmp_path, monkeypatch, policy, source, live, published):
    engine = make_engine(tmp_path, monkeypatch, [("unit", policy, source, live, "")])
    with engine.open_push_session(engine.resolve_sync_scope()) as session:
        row = session.view.rows[0]
        assert row.approved
        assert row.intent == "use-repository"
        assert row.allowed_intents == ()
        assert "set-resolution-intent" not in row.allowed_commands
        view = session.view
        assert isinstance(session.dispatch(SetResolutionIntent(
            view.session_id, view.revision, row.row_id, "use-live")), CommandRejected)
        result = session.execute().result
    assert result.status == "completed"
    assert result.units[0].status == "converged"
    assert read_optional(tmp_path / "live/unit") == published
    assert read_optional(tmp_path / "repo/packages/app/unit") == source


def test_push_ignores_pull_only_and_pull_guard(tmp_path, monkeypatch):
    engine = make_engine(tmp_path, monkeypatch, [
        ("unit", "both", b"repo", b"live", '[targets.unit.hooks]\nguard_pull = "exit 9"'),
        ("pull", "pull-only", b"repo", b"live", ""),
    ])
    with engine.open_push_session(engine.resolve_sync_scope()) as session:
        assert [unit.identity.target_name for unit in session.view.observations] == ["unit"]
        assert session.execute().result.status == "completed"
    assert (tmp_path / "live/pull").read_bytes() == b"live"
    assert (tmp_path / "live/unit").read_bytes() == b"repo"


def test_push_guard_omits_work_before_observation(tmp_path, monkeypatch):
    engine = make_engine(tmp_path, monkeypatch, [
        ("unit", "both", b"repo", b"live", '[targets.unit.hooks]\nguard_push = "exit 100"'),
    ])
    with engine.open_push_session(engine.resolve_sync_scope()) as session:
        assert session.view.observations == ()
        assert session.execute().result.status == "completed"
    assert (tmp_path / "live/unit").read_bytes() == b"live"


@pytest.mark.parametrize("preview", [False, True])
def test_real_push_discards_ineligible_base_before_failing_guard(tmp_path, monkeypatch, preview):
    from dotman.engine import DotmanEngine
    from dotman.sync_base_store import SyncBaseStore
    from dotman.sync_session import SessionOpenFailed

    engine = make_engine(tmp_path, monkeypatch, [("unit", "both", b"same", b"same", "")])
    store_record(engine)
    manifest = tmp_path / "repo/packages/app/package.toml"
    manifest.write_text(manifest.read_text().replace('sync_policy = "both"', 'sync_policy = "push-only"')
                        + '\n[targets.unit.hooks]\nguard_push = "exit 9"')
    engine = DotmanEngine(engine.config)
    assert isinstance(engine.open_push_session(engine.resolve_sync_scope(), preview=preview), SessionOpenFailed)
    with SyncBaseStore.open(engine._tracked_state_context.state_root, "main", read_only=True) as store:
        assert (store.read(b"main:app.unit") is not None) is preview


def test_push_acknowledges_both_policy_publication(tmp_path, monkeypatch):
    from dotman.sync_base_store import FilePresent, SyncBaseStore

    engine = make_engine(tmp_path, monkeypatch, [("unit", "both", b"repo", b"live", "")])
    with engine.open_push_session(engine.resolve_sync_scope()) as session:
        result = session.execute().result
    assert result.units[0].acknowledged
    with SyncBaseStore.open(engine._tracked_state_context.state_root, "main", read_only=True) as store:
        assert store.read(b"main:app.unit").payload == FilePresent(b"repo")


@pytest.mark.parametrize("preview", [False, True])
def test_push_snapshot_only_for_real_publication(tmp_path, monkeypatch, preview):
    from dotman.snapshot import list_snapshots

    engine = make_engine(tmp_path, monkeypatch, [("unit", "push-only", b"repo", b"live", "")])
    with engine.open_push_session(engine.resolve_sync_scope(), preview=preview) as session:
        view = session.view
        result = (session.dispatch(Preview(view.session_id, view.revision)) if preview else session.execute()).result
    assert result.units[0].status == ("would-converge" if preview else "converged")
    assert (tmp_path / "live/unit").read_bytes() == (b"live" if preview else b"repo")
    snapshots = list_snapshots(engine.config.snapshots.path) if engine.config.snapshots.path.exists() else []
    assert len(snapshots) == (0 if preview else 1)


def test_push_replaces_dangling_live_link_only_after_authorization(tmp_path, monkeypatch):
    from dotman.sync_session import AuthorizeSymlinkReplacement, SetApproval

    engine = make_engine(tmp_path, monkeypatch, [("unit", "both", b"repo", None, "")])
    live = tmp_path / "live/unit"
    live.parent.mkdir(exist_ok=True)
    live.symlink_to(tmp_path / "live/missing")
    with engine.open_push_session(engine.resolve_sync_scope()) as session:
        row = session.view.rows[0]
        assert "authorize-symlink-replacement" in row.allowed_commands
        assert not row.approved and row.diagnostics[0].code == "symlink-authorization-required"
        view = session.view
        session.dispatch(AuthorizeSymlinkReplacement(view.session_id, view.revision, row.row_id))
        view = session.view
        session.dispatch(SetApproval(view.session_id, view.revision, row.row_id, True))
        assert session.view.rows[0].proposal is not None
        assert session.execute().result.status == "completed"
    assert not live.is_symlink() and live.read_bytes() == b"repo"


def test_pull_still_rejects_dangling_live_link(tmp_path, monkeypatch):
    engine = make_engine(tmp_path, monkeypatch, [("unit", "both", b"repo", None, "")])
    live = tmp_path / "live/unit"
    live.parent.mkdir(exist_ok=True)
    live.symlink_to(tmp_path / "live/missing")
    with engine.open_pull_session(engine.resolve_sync_scope(), preview=True) as session:
        unit, = session.view.observations
        assert unit.state == "observation-failed"
        assert unit.diagnostics[0].code == "symlink-referent"


DEPENDENCY_NOOP_HOOK = '[hooks]\npre_push = [{ run = "true", run_noop = true }]'


def test_target_input_runs_no_dependency_hooks(tmp_path, monkeypatch):
    engine = make_engine(tmp_path, monkeypatch, [("own", "push-only", b"r", b"l", "")],
                         dependency_targets=[("shared", "push-only", b"r", b"l", "")],
                         dependency_extra=DEPENDENCY_NOOP_HOOK)
    with engine.open_push_session(engine.resolve_sync_scope(["main:app.own"]), preview=True) as session:
        assert [row.row_id for row in session.view.rows] == ["main:app.own"]
    with engine.open_push_session(engine.resolve_sync_scope(["main:app"]), preview=True) as session:
        assert "main:base (push-hooks)" in [row.row_id for row in session.view.rows]


def test_push_authenticates_sudo_once_for_all_protected_writes(tmp_path, monkeypatch, capsys):
    # The conftest sudo mock grants the ticket and runs the privileged helper
    # unelevated, so protected writes still go through the sudo path.
    import os

    from dotman import file_access

    if os.geteuid() == 0:
        pytest.skip("root never requests sudo")
    live_root = tmp_path / "live"
    monkeypatch.setattr(file_access, "needs_sudo_for_write", lambda path: live_root in path.parents)
    engine = make_engine(tmp_path, monkeypatch, [
        ("first", "push-only", b"repo-first", b"live", ""),
        ("second", "push-only", b"repo-second", b"live", ""),
    ])

    with engine.open_push_session(engine.resolve_sync_scope()) as session:
        assert session.execute().result.status == "completed"

    assert (live_root / "first").read_bytes() == b"repo-first"
    assert (live_root / "second").read_bytes() == b"repo-second"
    sudo_notices = [line for line in capsys.readouterr().err.splitlines() if "[sudo]" in line]
    assert len(sudo_notices) == 1, sudo_notices
