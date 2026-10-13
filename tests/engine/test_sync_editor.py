import os
import json
import shlex

import pytest

from dotman.sync_base_store import FilePresent, Missing
from dotman.sync_session import EditProposal, SetApproval
from tests.engine.test_sync_session import make_engine, open_session


def dispatch(session, kind, **kwargs):
    return session.dispatch(kind(session.view.session_id, session.view.revision,
                                 session.view.rows[0].row_id, **kwargs))


@pytest.mark.parametrize('policy', ['push-only', 'pull-only', 'both', 'push-only-delete'])
def test_editor_stages_policy_constrained_outcome(tmp_path, monkeypatch, policy):
    engine = make_engine(tmp_path, monkeypatch, [('a', policy, b'repo', b'live',
        'editor = { run = "printf edited > \\"$DOTMAN_SOURCE\\"", io = "pipe" }')])
    session = open_session(engine)
    dispatch(session, SetApproval, approved=True)
    result = dispatch(session, EditProposal)
    row = result.view.rows[0]
    assert result.result.status == 'saved'
    assert row.approved and row.proposal.intent == "editor"
    assert row.proposal.repository == FilePresent(b'edited')
    assert row.proposal.primary_source_change == FilePresent(b'edited')
    assert row.proposal.live == (FilePresent(b'live') if policy == 'pull-only' else Missing() if policy == 'push-only-delete' else FilePresent(b'edited'))
    assert (tmp_path / 'repo/packages/app/a').read_bytes() == b'repo'
    assert (tmp_path / 'live/a').read_bytes() == b'live'


@pytest.mark.parametrize('exit_code,status', [(130, 'cancelled'), (7, 'command-failed')])
def test_editor_failure_is_local_and_retryable(tmp_path, monkeypatch, exit_code, status):
    engine = make_engine(tmp_path, monkeypatch, [('a', 'push-only', b'repo', b'live',
        f"""editor = {{ run = "sh -c 'exit {exit_code}'", io = "pipe" }}""")])
    session = open_session(engine)
    dispatch(session, SetApproval, approved=True)
    previous = session.view.rows[0].proposal
    result = dispatch(session, EditProposal)
    assert result.result.status == status
    assert result.view.rows[0].proposal == previous
    assert result.view.rows[0].approved
    session.check_cancelled()


def test_unchanged_editor_quit_keeps_resolution_and_proposal(tmp_path, monkeypatch):
    engine = make_engine(tmp_path, monkeypatch, [('a', 'push-only', b'repo', b'live',
        'editor = { run = "true", io = "pipe" }')])
    session = open_session(engine)
    dispatch(session, SetApproval, approved=True)
    before = session.view.rows[0]
    result = dispatch(session, EditProposal)
    assert result.result.status == 'unchanged'
    assert result.view.rows[0] == before


def test_editor_recovers_capture_failure_without_enabling_publication(tmp_path, monkeypatch):
    from dotman.sync_session import PrepareProposalReview
    engine = make_engine(tmp_path, monkeypatch, [('a', 'pull-only', None, b'live',
        'capture = "false"\ncompare = { repo = "raw", live = "raw" }\neditor = { run = "printf recovered > \\"$DOTMAN_SOURCE\\"", io = "pipe" }')])
    session = open_session(engine)
    failed = dispatch(session, PrepareProposalReview)
    assert failed.result.diagnostics[0].code == 'capture-failed'
    saved = dispatch(session, EditProposal)
    assert saved.result.status == 'saved'
    assert saved.view.rows[0].proposal.repository == FilePresent(b'recovered')
    assert saved.view.rows[0].proposal.publication_effects == ()
    assert not (tmp_path / 'repo/packages/app/a').exists()


def test_additional_candidate_is_retained_but_not_consumed(tmp_path, monkeypatch):
    engine = make_engine(tmp_path, monkeypatch, [('a', 'push-only', b'repo', b'live',
        'render = "cat $DOTMAN_PACKAGE_ROOT/shared"\n'
        'editor = { run = "printf candidate > \\"$DOTMAN_EDITOR_ADDITIONAL_SOURCE_PATHS\\"", io = "pipe", additional_sources = ["shared"] }')])
    shared = tmp_path / 'repo/packages/app/shared'
    shared.write_bytes(b'original')
    session = open_session(engine)
    saved = dispatch(session, EditProposal)
    assert saved.result.status == 'saved'
    assert saved.view.rows[0].proposal.live == FilePresent(b'original')
    assert saved.view.rows[0].proposal.additional_changes[0].candidate == b'candidate'
    assert shared.read_bytes() == b'original'


def test_render_failure_clears_only_edited_approval(tmp_path, monkeypatch):
    marker = tmp_path / 'render-fails'
    render_command = f'test ! -e {shlex.quote(str(marker))} && cat "$DOTMAN_SOURCE"'
    render = f'render = {json.dumps(render_command)}\n'
    engine = make_engine(tmp_path, monkeypatch, [
        ('a', 'push-only', b'repo', b'live', render + 'editor = { run = "printf changed > \\"$DOTMAN_SOURCE\\"", io = "pipe" }'),
        ('b', 'push-only', b'repo', b'live', ''),
    ])
    session = open_session(engine)
    for row in session.view.rows:
        session.dispatch(SetApproval(session.view.session_id, session.view.revision, row.row_id, True))
    marker.touch()
    result = dispatch(session, EditProposal)
    assert result.result.status == 'materialization-failed'
    assert not result.view.rows[0].approved
    assert result.view.rows[1].approved


def test_editor_cancellation_can_retry_and_execute_exact_saved_bytes(tmp_path, monkeypatch):
    marker = tmp_path / 'editor-can-save'
    command = f'test -e {shlex.quote(str(marker))} || exit 130; printf saved > "$DOTMAN_SOURCE"'
    engine = make_engine(tmp_path, monkeypatch, [('a', 'push-only', b'repo', b'live',
        f'editor = {{ run = {json.dumps(command)}, io = "pipe" }}')])
    session = open_session(engine, preview=False)
    dispatch(session, SetApproval, approved=True)
    assert dispatch(session, EditProposal).result.status == 'cancelled'
    marker.touch()
    assert dispatch(session, EditProposal).result.status == 'saved'
    result = session.execute()
    assert result.result.units[0].status == 'converged'
    assert (tmp_path / 'repo/packages/app/a').read_bytes() == b'saved'
    assert (tmp_path / 'live/a').read_bytes() == b'saved'


def test_other_primary_cannot_be_edited_as_additional(tmp_path, monkeypatch):
    engine = make_engine(tmp_path, monkeypatch, [
        ('a', 'push-only', b'repo', b'live', 'editor = { run = "true", io = "pipe", additional_sources = ["b"] }'),
        ('b', 'push-only', b'repo', b'live', ''),
    ])
    session = open_session(engine)
    result = dispatch(session, EditProposal)
    assert result.result.status == 'materialization-failed'
    assert 'Primary Source' in result.result.diagnostics[0].message


@pytest.mark.parametrize('command', ['true', 'printf changed > "$DOTMAN_SOURCE"'])
def test_editor_does_not_print_workspace_notices(tmp_path, monkeypatch, capsys, command):
    engine = make_engine(tmp_path, monkeypatch, [('a', 'push-only', b'repo', b'live',
        f'editor = {{ run = {json.dumps(command)}, io = "pipe" }}')])
    session = open_session(engine)
    dispatch(session, EditProposal)
    assert capsys.readouterr().out == ''


def test_editor_cancel_before_dispatch_is_local(tmp_path, monkeypatch):
    engine = make_engine(tmp_path, monkeypatch, [('a', 'push-only', b'repo', b'live',
        'editor = { run = "true", io = "pipe" }')])
    session = open_session(engine)
    session.request_editor_cancel()
    assert dispatch(session, EditProposal).result.status == 'cancelled'
    assert dispatch(session, EditProposal).result.status == 'unchanged'
    session.check_cancelled()


def test_retry_preserves_edited_candidate_after_render_failure(tmp_path, monkeypatch):
    from dotman.sync_session import RetryMaterialization
    marker = tmp_path / 'render-fails'
    render_command = f'test ! -e {shlex.quote(str(marker))} && cat "$DOTMAN_SOURCE"'
    engine = make_engine(tmp_path, monkeypatch, [('a', 'push-only', b'repo', b'live',
        f'render = {json.dumps(render_command)}\n' + 'editor = { run = "printf edited > \\"$DOTMAN_SOURCE\\"", io = "pipe" }')])
    session = open_session(engine)
    marker.touch()
    assert dispatch(session, EditProposal).result.status == 'materialization-failed'
    marker.rename(tmp_path / 'render-recovered')
    result = dispatch(session, RetryMaterialization)
    proposal = result.view.rows[0].proposal
    assert proposal.repository == FilePresent(b'edited')
    assert proposal.intent == 'editor'
    assert proposal.reconciliation == 'edited repository outcome'


def test_additional_metadata_survives_resolution_change_and_frozen_inputs(tmp_path, monkeypatch):
    from dotman.sync_session import SetResolutionIntent
    engine = make_engine(tmp_path, monkeypatch, [('a', 'push-only', b'repo', b'live',
        'render = "cat $DOTMAN_SOURCE $DOTMAN_PACKAGE_ROOT/shared"\n'
        'editor = { run = "printf edited > \\"$DOTMAN_SOURCE\\"; printf candidate > \\"$DOTMAN_EDITOR_ADDITIONAL_SOURCE_PATHS\\"", io = "pipe", additional_sources = ["shared"] }')])
    shared = tmp_path / 'repo/packages/app/shared'
    shared.write_bytes(b'frozen')
    session = open_session(engine)
    shared.write_bytes(b'external')
    result = dispatch(session, EditProposal)
    assert result.view.rows[0].proposal.live == FilePresent(b'editedfrozen')
    assert result.view.rows[0].additional_changes[0].before == b'frozen'
    result = dispatch(session, SetResolutionIntent, intent='use-repository')
    assert result.view.rows[0].proposal is None
    assert result.view.rows[0].additional_changes[0].candidate == b'candidate'
    assert dispatch(session, EditProposal).view.rows[0].proposal.live == FilePresent(b'editedfrozen')
    assert shared.read_bytes() == b'external'


def test_failed_edited_render_keeps_additional_review_evidence(tmp_path, monkeypatch):
    marker = tmp_path / 'render-fails'
    render_command = f'test ! -e {shlex.quote(str(marker))} && cat "$DOTMAN_SOURCE"'
    engine = make_engine(tmp_path, monkeypatch, [('a', 'push-only', b'repo', b'live',
        f'render = {json.dumps(render_command)}\n' + 'editor = { run = "printf edited > \\"$DOTMAN_SOURCE\\"; printf candidate > \\"$DOTMAN_EDITOR_ADDITIONAL_SOURCE_PATHS\\"", io = "pipe", additional_sources = ["shared"] }')])
    (tmp_path / 'repo/packages/app/shared').write_bytes(b'frozen')
    session = open_session(engine)
    marker.touch()
    row = dispatch(session, EditProposal).view.rows[0]
    assert row.proposal is None
    assert row.additional_changes[0].before == b'frozen'
    assert row.additional_changes[0].candidate == b'candidate'


@pytest.mark.parametrize('operation_abort', [False, True])
def test_running_editor_cancellation_preserves_approval(tmp_path, monkeypatch, operation_abort):
    from concurrent.futures import ThreadPoolExecutor
    import time
    marker = tmp_path / 'editor-started'
    command = f'printf ready > {shlex.quote(str(marker))}; sleep 30; : "$DOTMAN_SOURCE"'
    engine = make_engine(tmp_path, monkeypatch, [('a', 'push-only', b'repo', b'live',
        f'editor = {{ run = {json.dumps(command)}, io = "pipe" }}')])
    session = open_session(engine)
    dispatch(session, SetApproval, approved=True)
    prior = session.view.rows[0].proposal
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(dispatch, session, EditProposal)
        try:
            deadline = time.monotonic() + 2
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert marker.exists(), 'Editor did not start'
        finally:
            if operation_abort:
                session.request_cancel()
            else:
                session.request_editor_cancel()
        result = future.result(timeout=3)
    assert result.result.status == 'cancelled'
    assert result.view.rows[0].approved
    assert result.view.rows[0].proposal == prior
    if operation_abort:
        with pytest.raises(InterruptedError):
            session.check_cancelled()
    else:
        session.check_cancelled()


def test_editor_generations_never_revive_after_intent_changes(tmp_path, monkeypatch):
    from dotman.sync_session import SetResolutionIntent
    engine = make_engine(tmp_path, monkeypatch, [('a', 'push-only', b'repo', b'live',
        'editor = { run = "printf x >> \\"$DOTMAN_SOURCE\\"", io = "pipe" }')])
    session = open_session(engine)
    first = dispatch(session, EditProposal).view.rows[0].proposal.generation
    dispatch(session, SetResolutionIntent, intent='use-repository')
    second = dispatch(session, EditProposal).view.rows[0].proposal.generation
    assert second > first


@pytest.mark.parametrize("policy", ["push-only", "push-only-delete", "pull-only", "both"])
@pytest.mark.parametrize("edited", [False, True, "identical"])
def test_editor_primary_activates_pull_hooks_only_for_repository_write(tmp_path, monkeypatch, policy, edited):
    import json
    log = tmp_path / "pull-hooks"
    guard_log = tmp_path / "pull-guards"
    engine = make_engine(tmp_path, monkeypatch, [("a", policy, b"edited" if edited == "identical" else b"repo", b"live",
        'editor = { run = "printf edited > \\"$DOTMAN_SOURCE\\"", io = "pipe" }\n'
        '[targets.a.hooks]\n'
        f'pre_pull = {json.dumps(f"echo pre >> {log}")}\n'
        f'post_pull = {json.dumps(f"echo post >> {log}")}\n'
        f'guard_pull = {json.dumps(f"echo guard >> {guard_log}; exit 100" if policy in ("push-only", "push-only-delete", "both") else f"echo guard >> {guard_log}")}')])
    session = open_session(engine, preview=False)
    guards = guard_log.read_bytes() if guard_log.exists() else None
    if edited:
        unchanged = edited == "identical" and policy != "pull-only"
        assert dispatch(session, EditProposal).result.status == ("unchanged" if unchanged else "saved")
    dispatch(session, SetApproval, approved=True)
    result = session.execute()
    # An identical pull-only edit writes nothing and cannot record a Sync Base.
    assert result.result.units[0].status == ("noop" if edited == "identical" and policy == "pull-only" else "converged")
    repository_write = edited is True or (not edited and policy == "pull-only")
    assert (log.read_text().splitlines() if log.exists() else []) == (
        ["pre", "post"] if repository_write else [])
    assert (guard_log.read_bytes() if guard_log.exists() else None) == guards


def test_edit_stays_selectable_after_switching_resolution(tmp_path, monkeypatch):
    from dotman.sync_session import SetResolutionIntent
    engine = make_engine(tmp_path, monkeypatch, [('a', 'push-only', b'repo', b'live',
        'editor = { run = "printf edited > \\"$DOTMAN_SOURCE\\"", io = "pipe" }')])
    session = open_session(engine)
    dispatch(session, SetApproval, approved=True)
    edited = dispatch(session, EditProposal).view.rows[0]
    assert (edited.intent, edited.allowed_intents) == ('editor', ('use-repository', 'editor'))
    row = dispatch(session, SetResolutionIntent, intent='use-repository').view.rows[0]
    assert row.proposal.repository == FilePresent(b'repo')
    assert row.allowed_intents == ('use-repository', 'editor')
    row = dispatch(session, SetResolutionIntent, intent='editor').view.rows[0]
    assert row.proposal.intent == 'editor' and row.proposal.repository == FilePresent(b'edited')
    # A restored edit is a new Proposal, never a revived earlier generation.
    assert row.proposal.generation > edited.proposal.generation


def test_editor_continues_a_reselected_edit_before_materialization(tmp_path, monkeypatch):
    from dotman.sync_session import SetResolutionIntent
    engine = make_engine(tmp_path, monkeypatch, [('a', 'push-only', b'repo', b'live',
        'editor = { run = "printf x >> \\"$DOTMAN_SOURCE\\"", io = "pipe" }')])
    session = open_session(engine)
    dispatch(session, EditProposal)
    dispatch(session, SetResolutionIntent, intent='use-repository')
    assert dispatch(session, SetResolutionIntent, intent='editor').view.rows[0].proposal is None
    assert dispatch(session, EditProposal).view.rows[0].proposal.repository == FilePresent(b'repoxx')


def test_editor_stops_on_failed_capture_then_recovers_from_repository(tmp_path, monkeypatch):
    from dotman.sync_session import SetResolutionIntent
    engine = make_engine(tmp_path, monkeypatch, [('a', 'both', b'repo', b'live',
        'capture = "false"\ncompare = { repo = "raw", live = "raw" }\n'
        'editor = { run = "printf x >> \\"$DOTMAN_SOURCE\\"", io = "pipe" }')])
    session = open_session(engine)
    dispatch(session, SetResolutionIntent, intent='use-live')
    # Opening repository bytes as if they were Use live would mislead the edit.
    assert dispatch(session, EditProposal).result.status == 'materialization-failed'
    saved = dispatch(session, EditProposal)
    assert saved.result.status == 'saved'
    assert saved.view.rows[0].proposal.repository == FilePresent(b'repox')


def test_unchanged_editor_quit_adds_no_edited_choice(tmp_path, monkeypatch):
    # The demo pull-noop row: Capture drops the volatile line, so Use live writes nothing.
    engine = make_engine(tmp_path, monkeypatch, [('a', 'pull-only', b'window\n', b'window\nopened-at\n',
        "capture = '''grep -v '^opened-at' \"$DOTMAN_LIVE_PATH\"'''\n"
        'compare = { repo = "raw", live = "raw" }\neditor = { run = "true", io = "pipe" }')])
    session = open_session(engine)
    result = dispatch(session, EditProposal)
    assert result.result.status == 'unchanged'
    row = result.view.rows[0]
    # An identical Edited choice would only toggle between two outcomes that both write nothing.
    assert (row.intent, row.allowed_intents) == ('use-live', ('use-live',))


@pytest.mark.parametrize('flow', ['pull', 'sync-use-live'])
def test_failed_editor_command_keeps_proposal_and_retry_edits_incoming_content(tmp_path, monkeypatch, flow):
    """A failed Editor command saves nothing, so a retry still starts from the incoming live content (#212)."""
    from dotman.sync_session import SetResolutionIntent
    marker = tmp_path / 'editor-can-save'
    command = f'test -e {shlex.quote(str(marker))} || exit 7; printf \'tweak\\n\' >> "$DOTMAN_SOURCE"'
    engine = make_engine(tmp_path, monkeypatch, [('a', 'pull-only' if flow == 'pull' else 'both',
        b'old repo\n', b'new live\n', f'editor = {{ run = {json.dumps(command)}, io = "pipe" }}')])
    if flow == 'pull':
        session = engine.open_pull_session(engine.resolve_sync_scope(), preview=False)
    else:
        session = open_session(engine, preview=False)
        dispatch(session, SetResolutionIntent, intent='use-live')
    with session:
        dispatch(session, SetApproval, approved=True)
        before = session.view.rows[0]
        assert before.intent == 'use-live' and before.proposal is not None
        failed = dispatch(session, EditProposal)
        assert failed.result.status == 'command-failed'
        assert failed.result.diagnostics
        row = failed.view.rows[0]
        assert (row.intent, row.proposal, row.approved) == ('use-live', before.proposal, True)
        marker.touch()
        assert dispatch(session, EditProposal).result.status == 'saved'
        assert session.execute().result.status == 'completed'
    assert (tmp_path / 'repo/packages/app/a').read_bytes() == b'new live\ntweak\n'


@pytest.mark.skipif(os.geteuid() == 0, reason='root reads files regardless of mode bits')
@pytest.mark.parametrize('flow', ['pull', 'sync-use-live'])
def test_failed_editor_workspace_keeps_proposal_and_retry_edits_incoming_content(tmp_path, monkeypatch, flow):
    """An Editor workspace that cannot be prepared saves nothing, so a retry still starts from the incoming live content (#212)."""
    from dotman.sync_session import SetResolutionIntent
    command = 'printf \'tweak\\n\' >> "$DOTMAN_SOURCE"'
    engine = make_engine(tmp_path, monkeypatch, [('a', 'pull-only' if flow == 'pull' else 'both',
        b'old repo\n', b'new live\n', f'editor = {{ run = {json.dumps(command)}, io = "pipe" }}')])
    if flow == 'pull':
        session = engine.open_pull_session(engine.resolve_sync_scope(), preview=False)
    else:
        session = open_session(engine, preview=False)
        dispatch(session, SetResolutionIntent, intent='use-live')
    # The Editor copies the whole repository into an isolated workspace, so an
    # unreadable unrelated file makes that preparation fail before the editor runs.
    unreadable = tmp_path / 'repo/packages/app/unreadable'
    unreadable.write_bytes(b'secret\n')
    unreadable.chmod(0)
    try:
        with session:
            dispatch(session, SetApproval, approved=True)
            before = session.view.rows[0]
            assert before.intent == 'use-live' and before.proposal is not None
            failed = dispatch(session, EditProposal)
            assert failed.result.status != 'saved'
            assert failed.result.diagnostics
            row = failed.view.rows[0]
            assert (row.intent, row.proposal, row.approved) == ('use-live', before.proposal, True)
            unreadable.chmod(0o644)
            assert dispatch(session, EditProposal).result.status == 'saved'
            assert session.execute().result.status == 'completed'
    finally:
        unreadable.chmod(0o644)
    assert (tmp_path / 'repo/packages/app/a').read_bytes() == b'new live\ntweak\n'
