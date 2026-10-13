"""Standing Selection and frozen execution through the public session boundary."""

import json

import pytest

from dotman.engine import DotmanEngine
from dotman.sync_base_store import FilePresent
from dotman.sync_session import (
    AdditionalRow, AuxiliaryRow, BatchSetApproval, CommandRejected,
    EditProposal, PrepareProposalReview, SetApproval, SetIncluded,
)
from tests.engine.test_sync_additional import all_row_ids, command
from tests.engine.test_sync_session import make_engine, open_session


@pytest.mark.parametrize('names', [('a', 'b'), ('b', 'a')])
def test_batch_final_inputs_do_not_depend_on_proposal_order(tmp_path, monkeypatch, names):
    config = ('render = "cat $DOTMAN_PACKAGE_ROOT/shared"\n'
              'editor = { run = "printf candidate > \\"$DOTMAN_EDITOR_ADDITIONAL_SOURCE_PATHS\\"", io = "pipe", additional_sources = ["shared"] }')
    engine = make_engine(tmp_path, monkeypatch, [
        (name, 'push-only', b'repo', b'live', config) for name in names
    ])
    (tmp_path / 'repo/packages/app/shared').write_bytes(b'original')
    with open_session(engine) as session:
        command(session, EditProposal, session.view.rows[0])
        command(session, BatchSetApproval, approved=True, row_ids=all_row_ids(session))
        proposals = [row for row in session.view.rows if not isinstance(row, AdditionalRow)]
        assert {row.row_id: row.proposal.live for row in proposals} == {
            'main:app.a': FilePresent(b'candidate'), 'main:app.b': FilePresent(b'candidate'),
        }
        assert all(row.approved for row in session.view.rows)
        command(session, BatchSetApproval, approved=False, row_ids=all_row_ids(session))
        assert all(not row.approved for row in session.view.rows)
        assert all(row.proposal is None for row in session.view.rows if not isinstance(row, AdditionalRow))


def test_mixed_batch_selection_keeps_approval_and_inclusion_distinct(tmp_path, monkeypatch):
    engine = make_engine(tmp_path, monkeypatch, [
        ('agree', 'push-only', b'same', b'same', ''),
        ('drift', 'push-only', b'repo', b'live', ''),
        ('blocked', 'push-only', b'repo', b'live', '[targets.blocked.hooks]\nguard_push = "exit 100"'),
    ])
    manifest = tmp_path / 'repo/packages/app/package.toml'
    manifest.write_text(manifest.read_text() + '\n[targets.check]\nprobe = "true"\n'
                        '[hooks]\npre_push = {run = "true", run_noop = true}\n')
    engine = DotmanEngine.from_config_path(engine.config.config_path)
    with open_session(engine) as session:
        assert 'main:app.agree' not in {row.row_id for row in session.view.rows}
        command(session, BatchSetApproval, approved=True, row_ids=all_row_ids(session))
        rows = {row.row_id: row for row in session.view.rows}
        assert rows['main:app.drift'].approved
        assert not rows['main:app.blocked'].approved
        auxiliary = [row for row in rows.values() if isinstance(row, AuxiliaryRow) and row.kind != 'guard-skip']
        assert {row.kind for row in auxiliary} == {'probe', 'hook'}
        assert not rows['main:app.blocked (guard_push)'].included
        assert all(row.included and not hasattr(row, 'approved') for row in auxiliary)
        before = session.view
        assert isinstance(command(session, SetApproval, auxiliary[0], approved=True), CommandRejected)
        assert session.view == before
        command(session, BatchSetApproval, approved=False, row_ids=all_row_ids(session))
        assert all(not row.included for row in session.view.rows if isinstance(row, AuxiliaryRow))
        drift = next(row for row in session.view.rows if row.row_id == 'main:app.drift')
        assert drift.included and not drift.approved


def test_execute_consumes_reviewed_materialization_without_running_providers(tmp_path, monkeypatch):
    marker = tmp_path / 'reject-provider'
    capture = f'test ! -e {marker} || exit 7; cat "$DOTMAN_LIVE_PATH"'
    engine = make_engine(tmp_path, monkeypatch, [
        ('unit', 'pull-only', b'repo', b'live',
         f'capture = {json.dumps(capture)}\ncompare = {{repo = "raw", live = "raw"}}'),
    ])
    with open_session(engine, preview=False) as session:
        command(session, PrepareProposalReview, session.view.rows[0])
        command(session, SetApproval, session.view.rows[0], approved=True)
        reviewed = session.view.rows[0].proposal
        marker.touch()
        (tmp_path / 'live/unit').write_bytes(b'external')
        result = session.execute()
        assert result.result.status == 'completed'
        assert session.view.rows[0].proposal == reviewed
        assert (tmp_path / 'repo/packages/app/unit').read_bytes() == b'live'
        assert session.view.terminal
        before = session.view
        assert isinstance(command(session, SetIncluded, before.rows[0], included=False), CommandRejected)
        assert session.view == before


def test_scoped_batch_selects_only_listed_rows_and_rematerializes_dependents(tmp_path, monkeypatch):
    from tests.engine.test_sync_additional import setup
    session, _ = setup(tmp_path, monkeypatch)
    a, b, additional = session.view.rows
    command(session, BatchSetApproval, approved=True, row_ids=(a.row_id,))
    a, b, additional = session.view.rows
    assert a.approved and not b.approved and not additional.approved
    assert a.proposal.live == FilePresent(b'original')
    # Approving only the shared source still rematerializes the approved Proposal that reads it.
    command(session, BatchSetApproval, approved=True, row_ids=(additional.row_id,))
    a, b, additional = session.view.rows
    assert additional.approved and not b.approved
    assert a.proposal.live == FilePresent(b'candidate')
    before = session.view
    assert isinstance(command(session, BatchSetApproval, approved=False, row_ids=('main:app.missing',)),
                      CommandRejected)
    assert session.view == before


@pytest.mark.parametrize('edited', [False, True], ids=['captured', 'edited'])
@pytest.mark.parametrize('batch', [False, True], ids=['single', 'batch'])
@pytest.mark.parametrize('operation', ['sync', 'pull', 'push'])
def test_approval_round_trip_keeps_the_same_proposal(tmp_path, monkeypatch, operation, batch, edited):
    """Approval is unrelated to the Proposal: unselect and reselect neither drop nor rebuild it."""
    policy = 'push-only' if operation == 'push' else 'pull-only'
    engine = make_engine(tmp_path, monkeypatch, [
        ('unit', policy, b'repo\n', b'live\n',
         'editor = { run = "printf \'tweak\\\\n\' >> \\"$DOTMAN_SOURCE\\"", io = "pipe" }'),
    ])
    opener = {'sync': engine.open_sync_session, 'pull': engine.open_pull_session,
              'push': engine.open_push_session}[operation]
    with opener(engine.resolve_sync_scope(), preview=False) as session:
        def set_approval(approved):
            row = session.view.rows[0]
            if batch:
                command(session, BatchSetApproval, approved=approved, row_ids=(row.row_id,))
            else:
                command(session, SetApproval, row, approved=approved)
        set_approval(True)
        if edited:
            assert command(session, EditProposal, session.view.rows[0]).result.status == 'saved'
        before = session.view.rows[0].proposal
        assert before is not None and before.intent == ('editor' if edited else before.intent)
        set_approval(False)
        row = session.view.rows[0]
        assert not row.approved
        assert row.proposal == before
        set_approval(True)
        row = session.view.rows[0]
        assert row.approved
        assert row.proposal == before
        assert row.proposal.generation == before.generation
        assert session.execute().result.status == 'completed'
    written = tmp_path / ('live/unit' if operation == 'push' else 'repo/packages/app/unit')
    origin = b'repo\n' if operation == 'push' else b'live\n'
    assert written.read_bytes() == origin + (b'tweak\n' if edited else b'')


@pytest.mark.parametrize('batch', [False, True], ids=['single', 'batch'])
def test_shared_source_approval_rebuilds_only_referencing_proposals(tmp_path, monkeypatch, batch):
    """A Proposal is rebuilt when an Additional Source it reads changes approval, and only then."""
    reader = ('render = "cat $DOTMAN_PACKAGE_ROOT/shared"\n'
              'editor = { run = "printf candidate > \\"$DOTMAN_EDITOR_ADDITIONAL_SOURCE_PATHS\\"", '
              'io = "pipe", additional_sources = ["shared"] }')
    engine = make_engine(tmp_path, monkeypatch, [
        ('author', 'push-only', b'repo', b'live', reader),
        ('reader', 'push-only', b'repo', b'live', reader),
        ('unselected', 'push-only', b'repo', b'live', reader),
        ('unrelated', 'push-only', b'repo', b'live', ''),
    ])
    (tmp_path / 'repo/packages/app/shared').write_bytes(b'original')
    with open_session(engine) as session:
        command(session, EditProposal, session.view.rows[0])
        command(session, BatchSetApproval, approved=True, row_ids=tuple(
            row.row_id for row in session.view.rows if not isinstance(row, AdditionalRow)))
        rows = {row.row_id: row for row in session.view.rows}
        command(session, SetApproval, rows['main:app.unselected'], approved=False)
        rows = {row.row_id: row for row in session.view.rows}
        additional = next(row for row in rows.values() if isinstance(row, AdditionalRow))
        assert not additional.approved
        before = {row_id: row.proposal for row_id, row in rows.items() if not isinstance(row, AdditionalRow)}
        assert all(before.values())
        if batch:
            command(session, BatchSetApproval, approved=True, row_ids=(additional.row_id,))
        else:
            command(session, SetApproval, additional, approved=True)
        rows = {row.row_id: row for row in session.view.rows}
        reader, unselected = rows['main:app.reader'], rows['main:app.unselected']
        # Approved reader: rebuilt from the new input as a new Proposal.
        assert reader.approved and reader.proposal.live == FilePresent(b'candidate')
        assert reader.proposal.generation > before['main:app.reader'].generation
        # Unselected reader: its stale Proposal is dropped, not silently kept.
        assert not unselected.approved and unselected.proposal is None
        # Unrelated row: untouched.
        assert rows['main:app.unrelated'].proposal == before['main:app.unrelated']
