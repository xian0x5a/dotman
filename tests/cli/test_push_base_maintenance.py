import json
import os

import pytest

from dotman.cli import main
from dotman.engine import DotmanEngine
from dotman.sync_base_store import SyncBaseStore
from tests.cli.test_sync_base_inspection import store_record
from tests.engine.test_sync_session import make_engine


@pytest.mark.parametrize('preview', [False, True])
@pytest.mark.parametrize('policy', ['push-only', 'push-only-delete'])
def test_real_push_discards_ineligible_base_before_failing_guard(tmp_path, monkeypatch, capsys, preview, policy):
    engine = make_engine(tmp_path, monkeypatch, [('unit', 'both', b'same', b'same', '')])
    store_record(engine)
    manifest = tmp_path / 'repo/packages/app/package.toml'
    manifest.write_text(manifest.read_text().replace('sync_policy = "both"', f'sync_policy = "{policy}"')
                        + '\n[targets.unit.hooks]\nguard_push = "exit 9"')
    args = ['--config', str(engine.config.config_path), '--json', '--no-confirm', 'push']
    if preview:
        args.append('--dry-run')
    assert main(args) != 0
    capsys.readouterr()
    with SyncBaseStore.open(engine._tracked_state_context.state_root, 'main', read_only=True) as store:
        assert (store.read(b'main:app.unit') is not None) is preview


def test_push_query_retains_unselected_and_eligible_bases(tmp_path, monkeypatch, capsys):
    from tests.helpers import write_tracked_packages_state

    engine = make_engine(tmp_path, monkeypatch, [
        ('selected', 'both', b'same', b'same', ''),
        ('eligible', 'both', b'same', b'same', ''),
    ])
    peer = tmp_path / 'repo/packages/peer'
    peer.mkdir()
    (peer / 'unit').write_bytes(b'same')
    (peer / 'package.toml').write_text(
        f'[targets.unit]\nsource = "unit"\npath = "{tmp_path / "live/peer"}"\ntype = "file"\nsync_policy = "both"'
    )
    write_tracked_packages_state(tmp_path / 'state', repo_name='main', entries=[('app', 'default'), ('peer', 'default')])
    engine = DotmanEngine(engine.config)
    for identity in ('main:app.selected', 'main:app.eligible', 'main:peer.unit'):
        store_record(engine, identity=identity)
    manifest = tmp_path / 'repo/packages/app/package.toml'
    manifest.write_text(manifest.read_text().replace('sync_policy = "both"', 'sync_policy = "push-only"', 1)
                        + '\n[targets.selected.hooks]\nguard_push = "exit 9"')
    peer_manifest = peer / 'package.toml'
    peer_manifest.write_text(peer_manifest.read_text().replace('sync_policy = "both"', 'sync_policy = "push-only"'))
    assert main(['--config', str(engine.config.config_path), '--json', '--no-confirm', 'push', 'main:app']) != 0
    capsys.readouterr()
    with SyncBaseStore.open(engine._tracked_state_context.state_root, 'main', read_only=True) as store:
        assert store.read(b'main:app.selected') is None
        assert store.read(b'main:peer.unit') is not None
        assert store.read(b'main:app.eligible') is not None


def test_push_invalid_static_resolution_preserves_base(tmp_path, monkeypatch, capsys):
    engine = make_engine(tmp_path, monkeypatch, [('unit', 'both', b'same', b'same', '')])
    store_record(engine)
    assert main(['--config', str(engine.config.config_path), '--json', '--no-confirm', 'push', 'main:app.unknown']) != 0
    capsys.readouterr()
    with SyncBaseStore.open(engine._tracked_state_context.state_root, 'main', read_only=True) as store:
        assert store.read(b'main:app.unit') is not None


def test_push_without_bases_does_not_create_base_store(tmp_path, monkeypatch, capsys):
    engine = make_engine(tmp_path, monkeypatch, [('unit', 'push-only', b'same', b'same', '')])
    assert main(['--config', str(engine.config.config_path), '--json', '--no-confirm', 'push']) == 0
    capsys.readouterr()
    from dotman.sync_base_store import BASES_DIRECTORY_NAME, LOCK_FILE_NAME
    assert not list(engine._tracked_state_context.state_root.rglob(BASES_DIRECTORY_NAME))
    assert not list(engine._tracked_state_context.state_root.rglob(LOCK_FILE_NAME))


def test_push_child_policy_cleanup_follows_configuration_not_discovery(tmp_path, monkeypatch, capsys):
    from tests.engine.test_sync_directory_observation import directory_engine, put

    engine = directory_engine(tmp_path, monkeypatch)
    root = tmp_path / 'repo/packages/app/tree'
    for child in ('selected', 'ignored', 'eligible'):
        put(root, child)
    for child in ('selected', 'ignored', 'eligible', 'missing'):
        store_record(engine, identity=f'main:app.tree/{child}')
    manifest = tmp_path / 'repo/packages/app/package.toml'
    manifest.write_text(manifest.read_text() + '''
[targets.tree.ignore]
patterns = ["ignored"]
[targets.tree.path_rules.selected]
pattern = "selected"
sync_policy = "push-only"
[targets.tree.path_rules.ignored]
pattern = "ignored"
sync_policy = "push-only"
[targets.tree.path_rules.missing]
pattern = "missing"
sync_policy = "push-only"
[targets.tree.hooks]
guard_push = "exit 9"
''')
    assert main(['--config', str(engine.config.config_path), '--json', '--no-confirm', 'push']) != 0
    capsys.readouterr()
    # Ineligibility is configured, so exclusion or absence cannot keep a stale Base.
    with SyncBaseStore.open(engine._tracked_state_context.state_root, 'main', read_only=True) as store:
        for child in ('selected', 'ignored', 'missing'):
            assert store.read(f'main:app.tree/{child}'.encode()) is None
        assert store.read(b'main:app.tree/eligible') is not None


def test_push_ineligible_base_maintenance_failure_warns_without_blocking(tmp_path, monkeypatch, capsys):
    from dotman.sync_base_store import LOCK_FILE_NAME

    engine = make_engine(tmp_path, monkeypatch, [('unit', 'push-only', b'repo', b'live', '')])
    record = store_record(engine)
    lock = record.parents[2] / LOCK_FILE_NAME
    # A hard link is unrepairable; loose modes would self-heal on writable open.
    os.link(lock, tmp_path / 'lock-alias')
    before = record.read_bytes()
    assert main(['--config', str(engine.config.config_path), '--json', '--no-confirm', 'push']) == 0
    unit = json.loads(capsys.readouterr().out)['sync_units'][0]
    assert unit['identity'] == 'main:app.unit'
    assert unit['result'] == 'converged'
    assert {'code': 'base-maintenance-failed', 'severity': 'warning'}.items() <= unit['diagnostics'][0].items()
    assert 'Sync Base' in unit['diagnostics'][0]['message']
    assert record.read_bytes() == before
    assert lock.stat().st_nlink == 2
    assert (tmp_path / 'live/unit').read_bytes() == b'repo'


def test_push_lock_contention_never_cleans_bases(tmp_path, monkeypatch, capsys):
    from dotman.operation_lock import OperationLock

    engine = make_engine(tmp_path, monkeypatch, [('unit', 'push-only', b'same', b'same', '')])
    store_record(engine)
    with OperationLock.acquire(engine._tracked_state_context.state_root):
        assert main(['--config', str(engine.config.config_path), '--json', '--no-confirm', 'push']) != 0
    capsys.readouterr()
    with SyncBaseStore.open(engine._tracked_state_context.state_root, 'main', read_only=True) as store:
        assert store.read(b'main:app.unit') is not None

