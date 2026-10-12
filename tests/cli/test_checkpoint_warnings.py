import json

import pytest

from dotman.sync_base_store import SyncBaseStore, SyncBaseStoreError
from tests.cli.test_sync_deck_command import arguments, runner_for
from tests.engine.test_sync_session import make_engine


@pytest.mark.parametrize('json_output', [True, False])
def test_optional_checkpoint_failure_reports_completion_separately(tmp_path, monkeypatch, capsys, json_output):
    engine = make_engine(tmp_path, monkeypatch, [('unit', 'pull-only', b'repo', b'live', '')])
    def fail(*args, **kwargs):
        raise SyncBaseStoreError('checkpoint unavailable')
    monkeypatch.setattr(SyncBaseStore, 'replace', fail)
    assert runner_for(engine).run(arguments(json_output=json_output, dry_run=False)) == 0
    output = capsys.readouterr().out
    if json_output:
        unit = json.loads(output)['sync_units'][0]
        assert unit['result'] == 'converged'
        assert unit['base']['qualified']
        assert not unit['base']['acknowledged']
        assert unit['diagnostics'][0]['severity'] == 'warning'
    else:
        assert '[ok] main:app.unit' in output
        # Base state is reported only through diagnostics, never as a result line.
        assert 'Base' not in output
        assert 'checkpoint unavailable' in output
    assert (tmp_path / 'repo/packages/app/unit').read_bytes() == b'live'


@pytest.mark.parametrize('operation', ['push', 'pull', 'sync'])
@pytest.mark.parametrize('direct', [False, True])
def test_committed_checkpoint_reports_durability_warning_without_losing_acknowledgment(
    tmp_path, monkeypatch, capsys, operation, direct,
):
    from dotman.cli import main
    from dotman.sync_base_store import SyncBaseStoreDurabilityError

    # No-confirm Sync skips no-Base both-policy drift, so drifted Sync uses pull-only.
    policy = 'pull-only' if operation == 'sync' and not direct else 'both'
    engine = make_engine(tmp_path, monkeypatch, [('unit', policy, b'repo', b'repo' if direct else b'live', '')])
    replace_record = SyncBaseStore.replace

    def fail_after_commit(store, record):
        replace_record(store, record)
        raise SyncBaseStoreDurabilityError('checkpoint committed; durability uncertain')

    monkeypatch.setattr(SyncBaseStore, 'replace', fail_after_commit)
    assert main(['--config', str(engine.config.config_path), '--json', '--no-confirm', operation]) == 0
    unit = json.loads(capsys.readouterr().out)['sync_units'][0]
    assert unit['base']['acknowledged']
    assert unit['diagnostics'][0]['code'] == 'base-durability-uncertain'
    assert unit['diagnostics'][0]['severity'] == 'warning'


@pytest.mark.parametrize('fails', [False, True])
def test_push_human_reports_base_only_through_warnings(tmp_path, monkeypatch, capsys, fails):
    from dotman.cli import main

    engine = make_engine(tmp_path, monkeypatch, [('unit', 'both', b'repo', b'live', '')])
    if fails:
        def fail(*args, **kwargs):
            raise SyncBaseStoreError('checkpoint unavailable')
        monkeypatch.setattr(SyncBaseStore, 'replace', fail)
    assert main(['--config', str(engine.config.config_path), '--no-confirm', 'push']) == 0
    output = capsys.readouterr().out
    assert '[1/1] write' in output
    # A clean unit is not recapped; a checkpoint warning is.
    assert ('[ok] main:app.unit' in output) == fails
    # Base state is reported only through unit diagnostics, never as a result line.
    assert 'Base' not in output
    assert ('checkpoint unavailable' in output) == fails
    assert (tmp_path / 'live/unit').read_bytes() == b'repo'


def test_optional_checkpoint_validation_failure_does_not_block_no_confirm_apply(tmp_path, monkeypatch, capsys):
    engine = make_engine(tmp_path, monkeypatch, [('unit', 'pull-only', b'repo', b'live',
        'render = "exit 7"\ncompare = {repo = "raw", live = "raw"}')])
    assert runner_for(engine).run(arguments(dry_run=False)) == 0
    unit = json.loads(capsys.readouterr().out)['sync_units'][0]
    assert unit['result'] == 'converged'
    assert unit['base']['qualified'] is False
    assert unit['base']['acknowledged'] is False
    assert unit['diagnostics'][0]['code'] == 'base-validation-failed'
    assert unit['diagnostics'][0]['severity'] == 'warning'
