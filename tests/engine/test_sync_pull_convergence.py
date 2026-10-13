import pytest

from dotman.sync_base_store import FilePresent, SyncBaseStoreError
from dotman.sync_session import ApprovalChanged, PrepareProposalReview, SetApproval
from tests.engine.test_sync_convergence import command
from tests.engine.test_sync_session import make_engine, open_session


def test_pull_review_lazily_captures_live_and_execution_only_applies_repository(tmp_path, monkeypatch):
    marker = tmp_path / 'captures'
    engine = make_engine(tmp_path, monkeypatch, [
        ('unit', 'pull-only', b'repo', b'live',
         f'capture = "echo capture >> {marker}; cat $DOTMAN_LIVE_PATH"\ncompare = {{ repo = "raw", live = "raw" }}'),
    ])
    with open_session(engine, preview=False) as session:
        row = session.view.rows[0]
        assert row.allowed_intents == ('use-live',)
        assert row.proposal is None and not row.approved
        assert not marker.exists()
        command(session, PrepareProposalReview, row.row_id)
        proposal = session.view.rows[0].proposal
        assert proposal.intent == 'use-live'
        assert proposal.repository == FilePresent(b'live')
        assert proposal.live == FilePresent(b'live')
        assert proposal.primary_source_change == FilePresent(b'live')
        assert proposal.publication_effects == ()
        assert not session.view.rows[0].approved
        command(session, SetApproval, row.row_id, True)
        result = session.execute().result
        assert result.status == 'completed'
        assert result.units[0].status == 'converged'
        assert all(step.stage == 'repository-apply' for step in result.steps)
    assert marker.read_text().splitlines() == ['capture']
    assert (tmp_path / 'repo/packages/app/unit').read_bytes() == b'live'
    assert (tmp_path / 'live/unit').read_bytes() == b'live'
    with open_session(engine) as later:
        record = later.view.observations[0].base.record
        assert record.payload == FilePresent(b'live')
    assert not list((tmp_path / 'state').rglob('manifest.json'))


def test_capture_reads_files_beside_the_live_endpoint(tmp_path, monkeypatch):
    engine = make_engine(tmp_path, monkeypatch, [
        ('unit', 'pull-only', b'repo', b'live',
         'capture = "cat \\"$(dirname \\"$DOTMAN_LIVE_PATH\\")/sibling\\""\ncompare = { live = "capture" }'),
    ])
    (tmp_path / 'live/sibling').write_bytes(b'from sibling')
    with open_session(engine) as session:
        observation = session.view.observations[0]
        assert observation.diagnostics == ()
        assert observation.comparison_live == FilePresent(b'from sibling')


def test_lossy_pull_no_write_is_a_noop_without_acknowledgment(tmp_path, monkeypatch):
    engine = make_engine(tmp_path, monkeypatch, [
        ('unit', 'pull-only', b'repo', b'live',
         'capture = "printf repo"\ncompare = { repo = "raw", live = "raw" }'),
    ])
    with open_session(engine, preview=False) as session:
        assert session.execute().result.units[0].status == 'pending'
    with open_session(engine, preview=False) as session:
        assert session.view.observations[0].base.record is None
        command(session, SetApproval, 'main:app.unit', True)
        row = session.view.rows[0]
        assert row.proposal.primary_source_change is None
        assert row.proposal.noop and not row.approved
        result = session.execute().result
        assert result.units[0].status == 'noop'
        assert result.steps == ()
    with open_session(engine) as later:
        record = later.view.observations[0].base.record
        assert record is None


def test_approving_a_noop_proposal_reports_the_row_stays_unapproved(tmp_path, monkeypatch):
    # Capture reproduces the repository while compare sees drift: a No-op Proposal offers no Approval.
    engine = make_engine(tmp_path, monkeypatch, [
        ('unit', 'pull-only', b'repo', b'live',
         'capture = "printf repo"\ncompare = { repo = "raw", live = "raw" }'),
    ])
    with open_session(engine, preview=False) as session:
        accepted = command(session, SetApproval, 'main:app.unit', True)
        [row] = accepted.view.rows
        assert row.proposal.noop and not row.approved
        assert accepted.result == ApprovalChanged('main:app.unit', False)


@pytest.mark.parametrize('source,live', [(b'repo', None), (None, b''), (b'repo', b'live')])
def test_pull_typed_outcomes_leave_live_untouched(source, live, tmp_path, monkeypatch):
    engine = make_engine(tmp_path, monkeypatch, [('unit', 'pull-only', source, live, '')])
    with open_session(engine, preview=False) as session:
        command(session, SetApproval, 'main:app.unit', True)
        assert session.execute().result.units[0].status == 'converged'
    repository = tmp_path / 'repo/packages/app/unit'
    endpoint = tmp_path / 'live/unit'
    assert (repository.read_bytes() if repository.exists() else None) == live
    assert (endpoint.read_bytes() if endpoint.exists() else None) == live


@pytest.mark.parametrize("no_write", [False, True])
def test_acknowledgment_failure_does_not_prevent_convergence(tmp_path, monkeypatch, no_write):
    from dotman.sync_base_store import SyncBaseStore
    extra = 'capture = "printf repo"\ncompare = { repo = "raw", live = "raw" }' if no_write else ''
    engine = make_engine(tmp_path, monkeypatch, [('unit', 'pull-only', b'repo', b'live', extra)])
    with open_session(engine, preview=False) as session:
        command(session, SetApproval, 'main:app.unit', True)
        def fail(*args, **kwargs):
            raise SyncBaseStoreError('ack unavailable')
        monkeypatch.setattr(SyncBaseStore, 'replace', fail)
        result = session.execute().result
        assert result.status == 'completed'
        # The lossy no-write Capture cannot record a Sync Base, so it is a no-op.
        assert result.units[0].status == ('noop' if no_write else 'converged')
        assert not result.units[0].acknowledged
        if not no_write:
            assert result.units[0].diagnostics[0].code == 'base-acknowledgment-failed'
            assert result.units[0].diagnostics[0].severity == 'warning'
    assert (tmp_path / 'repo/packages/app/unit').read_bytes() == (b'repo' if no_write else b'live')


@pytest.mark.parametrize("render", ["exit 9", "printf unequal"])
def test_optional_forward_check_failure_does_not_block_repository_work(tmp_path, monkeypatch, render):
    engine = make_engine(tmp_path, monkeypatch, [
        ('unit', 'pull-only', b'repo', b'live',
         f'render = "{render}"\ncapture = "raw"\ncompare = {{ repo = "raw", live = "raw" }}'),
    ])
    with open_session(engine, preview=False) as session:
        command(session, SetApproval, 'main:app.unit', True)
        row = session.view.rows[0]
        assert row.approved
        result = session.execute().result
        assert result.status == 'completed'
        assert result.exit_code == 0
        assert result.units[0].status == 'converged'
        assert not result.units[0].acknowledged
        if render == 'exit 9':
            assert row.proposal.checkpoint_warnings
            assert all(d.severity == 'warning' for d in result.units[0].diagnostics)
    assert (tmp_path / 'repo/packages/app/unit').read_bytes() == b'live'
    with open_session(engine) as later:
        assert later.view.observations[0].base.record is None


def test_capture_comparison_is_reused_and_configured_agreement_never_runs_capture(tmp_path, monkeypatch):
    marker = tmp_path / 'captures'
    engine = make_engine(tmp_path, monkeypatch, [
        ('captureview', 'pull-only', b'repo', b'live', f'capture = "echo capture >> {marker}; printf captured"'),
        ('agree', 'pull-only', b'repo', b'live', 'capture = "exit 9"\ncompare = { repo = "printf equal", live = "printf equal" }'),
    ])
    with open_session(engine) as session:
        assert [row.row_id for row in session.view.rows] == ['main:app.captureview']
        assert marker.read_text().splitlines() == ['capture']
        command(session, SetApproval, 'main:app.captureview', True)
        assert session.view.rows[0].proposal.repository == FilePresent(b'captured')
        assert marker.read_text().splitlines() == ['capture']


def test_failed_lazy_capture_remains_unapproved_and_retry_reads_current_live(tmp_path, monkeypatch):
    marker = tmp_path / 'ready'
    engine = make_engine(tmp_path, monkeypatch, [
        ('unit', 'pull-only', b'repo', b'live',
         f'capture = "test -f {marker} || exit 8; cat $DOTMAN_LIVE_PATH"\ncompare = {{ repo = "raw", live = "raw" }}'),
    ])
    with open_session(engine) as session:
        command(session, SetApproval, 'main:app.unit', True)
        row = session.view.rows[0]
        assert not row.approved and row.proposal is None
        assert row.diagnostics[0].code == 'capture-failed'
        marker.touch()
        (tmp_path / 'live/unit').write_bytes(b'external')
        command(session, SetApproval, row.row_id, True)
        row = session.view.rows[0]
        assert row.approved and not row.diagnostics
        # Retry reruns Capture against the live endpoint, so live-side fixes apply.
        assert row.proposal.repository == FilePresent(b'external')


def test_repository_apply_failure_prevents_live_publication_and_base(tmp_path, monkeypatch):
    engine = make_engine(tmp_path, monkeypatch, [
        ('push', 'push-only', b'repo', b'live', ''),
        ('pull', 'pull-only', b'repo', b'live', '[targets.pull.hooks]\npre_pull = "exit 8"'),
    ])
    with open_session(engine, preview=False) as session:
        for row in session.view.rows:
            command(session, SetApproval, row.row_id, True)
        result = session.execute().result
        assert result.status == 'failed'
        assert {unit.status for unit in result.units} == {'skipped'}
        assert all(step.status == 'unattempted' for step in result.steps
                   if step.stage == 'live-publication')
    assert (tmp_path / 'live/push').read_bytes() == b'live'
    assert (tmp_path / 'repo/packages/app/pull').read_bytes() == b'repo'
    with open_session(engine) as later:
        assert later.view.observations[1].base.record is None


def test_pull_preview_and_execution_never_snapshot_or_publish(tmp_path, monkeypatch):
    import dotman.sync_session as boundary
    from dotman.sync_session import Preview
    engine = make_engine(tmp_path, monkeypatch, [('unit', 'pull-only', b'repo', b'live', '')])
    def forbidden(*args, **kwargs):
        pytest.fail('pull-only must not enter live publication')
    monkeypatch.setattr(boundary, 'execute_publication', forbidden)
    with open_session(engine) as preview:
        command(preview, SetApproval, 'main:app.unit', True)
        assert command(preview, Preview).result.units[0].status == 'would-converge'
    assert (tmp_path / 'repo/packages/app/unit').read_bytes() == b'repo'
    with open_session(engine, preview=False) as session:
        assert session.view.observations[0].base.record is None
        command(session, SetApproval, 'main:app.unit', True)
        assert session.execute().result.units[0].status == 'converged'


def test_patch_capture_uses_frozen_comparison_and_repository(tmp_path, monkeypatch):
    engine = make_engine(tmp_path, monkeypatch, [
        ('unit', 'pull-only', b'template\nkeep\nold\n', b'rendered\nkeep\nnew\n',
         'render = "sed s/template/rendered/ $DOTMAN_SOURCE"\ncapture = "patch"\ncompare = { repo = "render", live = "raw" }'),
    ])
    with open_session(engine, preview=False) as session:
        assert session.view.rows[0].proposal is None
        (tmp_path / 'repo/packages/app/unit').write_bytes(b'external')
        (tmp_path / 'live/unit').write_bytes(b'external')
        command(session, SetApproval, 'main:app.unit', True)
        assert session.view.rows[0].proposal.repository == FilePresent(b'template\nkeep\nnew\n')
        assert session.execute().result.units[0].status == 'converged'
    assert (tmp_path / 'repo/packages/app/unit').read_bytes() == b'template\nkeep\nnew\n'
    assert (tmp_path / 'live/unit').read_bytes() == b'external'


def test_failed_ack_preserves_previous_base_and_earlier_committed_completion(tmp_path, monkeypatch):
    from dotman.sync_base_store import SyncBaseStore
    engine = make_engine(tmp_path, monkeypatch, [
        ('first', 'pull-only', b'repo', b'repo', ''),
        ('second', 'pull-only', b'repo', b'repo', ''),
        ('third', 'pull-only', b'repo', b'repo', ''),
    ])
    with open_session(engine, preview=False) as initial:
        assert all(unit.base.acknowledged for unit in initial.view.observations)
    for name in ('first', 'second', 'third'):
        (tmp_path / 'live' / name).write_bytes(b'changed')
    original = SyncBaseStore.replace
    with open_session(engine, preview=False) as session:
        prior = session.view.observations[1].base.record
        for row in session.view.rows:
            command(session, SetApproval, row.row_id, True)
        calls = 0
        def replace_record(store, record):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise SyncBaseStoreError('second acknowledgment failed')
            return original(store, record)
        with monkeypatch.context() as patch:
            patch.setattr(SyncBaseStore, 'replace', replace_record)
            result = session.execute().result
        assert [unit.status for unit in result.units] == ['converged'] * 3
        assert [unit.acknowledged for unit in result.units] == [True, False, True]
    with open_session(engine) as later:
        assert later.view.observations[0].base.record.payload == FilePresent(b'changed')
        assert later.view.observations[1].base.record == prior
        assert later.view.observations[2].base.record.payload == FilePresent(b'changed')
    assert (tmp_path / 'repo/packages/app/third').read_bytes() == b'changed'


def test_pull_convergence_and_base_survive_post_hook_failure(tmp_path, monkeypatch):
    engine = make_engine(tmp_path, monkeypatch, [
        ('unit', 'pull-only', b'repo', b'live', '[targets.unit.hooks]\npost_pull = "exit 9"'),
    ])
    with open_session(engine, preview=False) as session:
        command(session, SetApproval, 'main:app.unit', True)
        result = session.execute().result
        assert result.status == 'failed'
        assert result.units[0].status == 'converged'
    with open_session(engine) as later:
        assert later.view.observations[0].base.record.payload == FilePresent(b'live')


@pytest.mark.parametrize('interruption', [KeyboardInterrupt, InterruptedError])
def test_acknowledgment_interruption_is_typed_and_not_converged(tmp_path, monkeypatch, interruption):
    from dotman.sync_base_store import SyncBaseStore
    engine = make_engine(tmp_path, monkeypatch, [('unit', 'pull-only', b'repo', b'live', '')])
    with open_session(engine, preview=False) as session:
        command(session, SetApproval, 'main:app.unit', True)
        def interrupt(*args):
            raise interruption
        monkeypatch.setattr(SyncBaseStore, 'replace', interrupt)
        result = session.execute().result
        assert result.status == 'aborted'
        assert result.units[0].status == 'interrupted'
        assert result.exit_code == 130
    assert (tmp_path / 'repo/packages/app/unit').read_bytes() == b'live'
