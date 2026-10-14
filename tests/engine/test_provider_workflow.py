"""Providers see the invoking workflow; Guards and hooks keep their direction."""
import json
import platform

import pytest

from dotman.engine import DotmanEngine
from dotman.sync_session import CommandAccepted, EditProposal, SetApproval, SetIncluded, SetResolutionIntent
from tests.engine.test_sync_session import make_engine
from tests.engine.test_sync_directory_observation import directory_engine, put


def send(session, command, row_id, *args):
    view = session.view
    result = session.dispatch(command(view.session_id, view.revision, row_id, *args))
    assert isinstance(result, CommandAccepted), result
    return result


@pytest.mark.parametrize("operation", ["pull", "sync"])
@pytest.mark.parametrize("live_view", ["raw", "capture", 'printf "%s-live" "$DOTMAN_OPERATION"'])
@pytest.mark.parametrize("directory", [False, True])
def test_frozen_projections_and_capture_use_workflow_identity(tmp_path, monkeypatch, operation, live_view, directory):
    config = (
        'capture = \'printf "%s" "$DOTMAN_OPERATION"\'\n'
        'render = \'printf "%s-rendered" "$DOTMAN_OPERATION"\'\n'
        f'compare = {{repo = \'printf "%s-repo" "$DOTMAN_OPERATION"\', live = {json.dumps(live_view)}}}'
    )
    if directory:
        engine = directory_engine(tmp_path, monkeypatch, extra=f'[targets.tree.path_rules.provider]\npattern = "*"\n{config}')
        source = put(tmp_path / "repo/packages/app/tree", "child", b"repo")
        live = put(tmp_path / "live/tree", "child", b"live")
    else:
        engine = make_engine(tmp_path, monkeypatch, [("unit", "both", b"repo", b"live", config)])
        source, live = tmp_path / "repo/packages/app/unit", tmp_path / "live/unit"

    with getattr(engine, f"open_{operation}_session")(engine.resolve_sync_scope()) as session:
        row, = session.view.rows
        assert row.observation.comparison_repository.content == f"{operation}-repo".encode()
        expected_live_view = b"live" if live_view == "raw" else (
            operation.encode() if live_view == "capture" else f"{operation}-live".encode()
        )
        assert row.observation.comparison_live.content == expected_live_view
        if operation == "sync":
            send(session, SetResolutionIntent, row.row_id, "use-live")
        send(session, SetApproval, row.row_id, True)
        assert session.view.rows[0].proposal.repository.content == operation.encode()
        assert session.execute().result.status == "completed"
    assert source.read_bytes() == operation.encode()
    assert live.read_bytes() == (b"live" if operation == "pull" else b"sync-rendered")


@pytest.mark.parametrize("operation", ["pull", "sync"])
@pytest.mark.parametrize("policy", ["both", "pull-only"])
def test_transactional_editor_uses_workflow_not_metadata_direction(tmp_path, monkeypatch, operation, policy):
    engine = make_engine(tmp_path, monkeypatch, [(
        "unit", policy, b"repo", b"live",
        'editor = {run = \'printf "%s" "$DOTMAN_OPERATION" > "$DOTMAN_SOURCE"\', io = "pipe"}',
    )])
    with getattr(engine, f"open_{operation}_session")(engine.resolve_sync_scope()) as session:
        row, = session.view.rows
        result = send(session, EditProposal, row.row_id)
        assert result.result.status == "saved"
        assert session.view.rows[0].proposal.repository.content == operation.encode()
        assert (tmp_path / "repo/packages/app/unit").read_bytes() == b"repo"


@pytest.mark.parametrize("operation", ["pull", "sync", "push"])
def test_probe_uses_workflow_while_guards_and_hooks_keep_direction(tmp_path, monkeypatch, operation):
    make_engine(tmp_path, monkeypatch, [])
    manifest = tmp_path / "repo/packages/app/package.toml"
    manifest.write_text(
        '[targets.probe]\n'
        f'probe = \'test "$DOTMAN_OPERATION" = {operation}\'\n'
        '[targets.probe.hooks]\n'
        'guard_push = \'test "$DOTMAN_OPERATION" = push\'\n'
        'guard_pull = \'test "$DOTMAN_OPERATION" = pull\'\n'
        'pre_push = \'test "$DOTMAN_OPERATION" = push\'\n'
        'pre_pull = \'test "$DOTMAN_OPERATION" = pull\'\n'
    )
    engine = DotmanEngine.from_config_path(tmp_path / "config.toml")
    with getattr(engine, f"open_{operation}_session")(engine.resolve_sync_scope()) as session:
        row, = session.view.rows
        assert row.kind == "probe"
        send(session, SetIncluded, row.row_id, True)
        assert session.execute().result.status == "completed"


def test_push_render_keeps_push_workflow_identity(tmp_path, monkeypatch):
    engine = make_engine(tmp_path, monkeypatch, [
        ("unit", "both", b"repo", b"live", 'render = \'printf "%s" "$DOTMAN_OPERATION"\''),
    ])
    with engine.open_push_session(engine.resolve_sync_scope()) as session:
        assert session.execute().result.status == "completed"
    assert (tmp_path / "live/unit").read_bytes() == b"push"
    assert (tmp_path / "repo/packages/app/unit").read_bytes() == b"repo"


def test_commands_and_hooks_receive_cpu_arch_env(tmp_path, monkeypatch):
    monkeypatch.setattr(platform, "machine", lambda: "aarch64")
    hook_log = tmp_path / "hooks.log"
    engine = make_engine(tmp_path, monkeypatch, [(
        "unit", "both", b"repo", b"live",
        "render = 'printf \"%s\" \"$DOTMAN_CPU_ARCH\"'\n"
        f"hooks.pre_push = 'echo \"target $DOTMAN_CPU_ARCH\" >> {hook_log}'\n"
        "[hooks]\n"
        f"pre_push = 'echo \"package $DOTMAN_CPU_ARCH\" >> {hook_log}'",
    )])
    with engine.open_push_session(engine.resolve_sync_scope()) as session:
        assert session.execute().result.status == "completed"
    assert (tmp_path / "live/unit").read_text() == "arm64"
    assert sorted(hook_log.read_text().splitlines()) == ["package arm64", "target arm64"]
