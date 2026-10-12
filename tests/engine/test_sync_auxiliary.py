from __future__ import annotations

import pytest

from dotman.engine import DotmanEngine
from dotman.sync_session import CommandRejected, SetApproval, SetResolutionIntent, SetIncluded, SyncSession, SessionOpenFailed
from tests.engine.test_sync_session import make_engine, open_session


def auxiliary_engine(tmp_path, monkeypatch, manifest, *, repo_hooks=''):
    engine = make_engine(tmp_path, monkeypatch, [])
    (tmp_path / 'repo/packages/app/package.toml').write_text('id = "app"\n' + manifest)
    if repo_hooks:
        (tmp_path / 'repo/repo.toml').write_text('[hooks]\n' + repo_hooks)
    return DotmanEngine.from_config_path(engine.config.config_path)


def include(session, row):
    view = session.view
    return session.dispatch(SetIncluded(view.session_id, view.revision, row.row_id, True))


def test_probe_is_auxiliary_selected_once_without_file_or_base_work(tmp_path, monkeypatch):
    log = tmp_path / 'log'
    engine = auxiliary_engine(tmp_path, monkeypatch, f'''
[targets.check]
probe = "echo probe >> {log}"
[targets.check.hooks]
guard_pull = "echo guard >> {log}; exit 100"
pre_pull = "exit 9"
pre_push = "echo push >> {log}"
''')
    scope = engine.resolve_sync_scope(['main:app.check'])
    with engine.open_sync_session(scope) as session:
        assert session.view.observations == ()
        row, guard = session.view.rows
        assert (row.kind, row.scope, row.directions, row.included) == ('probe', 'main:app.check', ('push',), False)
        assert (guard.kind, guard.directions) == ('guard-skip', ('pull',))
        assert not any(hasattr(row, name) for name in ('observation', 'proposal', 'intent', 'approved'))
        rejected = session.dispatch(SetApproval(session.view.session_id, session.view.revision, row.row_id, True))
        assert isinstance(rejected, CommandRejected)
        include(session, row)
        result = session.execute().result
        assert result.status == 'completed'
        assert result.units == ()
    assert log.read_text().splitlines() == ['guard', 'probe', 'push']
    from dotman.sync_base_store import BASES_DIRECTORY_NAME, LOCK_FILE_NAME
    assert not list((tmp_path / 'state').rglob(BASES_DIRECTORY_NAME))
    assert not list((tmp_path / 'state').rglob(LOCK_FILE_NAME))


@pytest.mark.parametrize('command,guard,expected_rows,failed', [
    ('exit 100', 'exit 0', 0, False),
    # The Guard row explains why the probe is gone.
    ('exit 9', 'exit 100', 1, False),
    ('exit 9', 'exit 0', 0, True),
])
def test_probe_runs_only_with_surviving_capability(tmp_path, monkeypatch, command, guard, expected_rows, failed):
    engine = auxiliary_engine(tmp_path, monkeypatch, f'''
[targets.check]
sync_policy = "push-only"
probe = "{command}"
[targets.check.hooks]
guard_push = "{guard}"
''')
    opened = engine.open_sync_session(engine.resolve_sync_scope(), preview=True)
    if failed:
        assert isinstance(opened, SessionOpenFailed)
        assert 'probe failed' in opened.diagnostic.message
    else:
        assert isinstance(opened, SyncSession)
        assert len(opened.view.rows) == expected_rows
        assert opened.view.observations == ()


@pytest.mark.parametrize('at_package', [False, True])
def test_probe_rejects_effective_deletion_policy(tmp_path, monkeypatch, at_package):
    manifest = ('sync_policy = "push-only-delete"\n' if at_package else '') + '[targets.check]\nprobe = "true"\n' + ('' if at_package else 'sync_policy = "push-only-delete"\n')
    with pytest.raises(ValueError, match='push-only-delete'):
        engine = auxiliary_engine(tmp_path, monkeypatch, manifest)
        engine.resolve_sync_scope()


def test_directional_guard_order_is_scope_first_and_never_repeated(tmp_path, monkeypatch):
    log = tmp_path / 'order'
    hooks = lambda scope: '\n'.join(f'guard_{direction} = "echo {scope}-{direction} >> {log}"' for direction in ('push', 'pull'))
    engine = make_engine(tmp_path, monkeypatch, [('unit', 'both', b'repo', b'live', '[targets.unit.hooks]\n' + hooks('target'))])
    manifest = tmp_path / 'repo/packages/app/package.toml'
    manifest.write_text(manifest.read_text() + '\n[hooks]\n' + hooks('package'))
    (tmp_path / 'repo/repo.toml').write_text('[hooks]\n' + hooks('repo'))
    from tests.helpers import write_named_manager_config
    engine = DotmanEngine.from_config_path(write_named_manager_config(tmp_path, {'main': tmp_path / 'repo'}))
    with open_session(engine, preview=False) as session:
        session.execute()
    assert log.read_text().splitlines() == [f'{scope}-{direction}' for scope in ('repo', 'package', 'target') for direction in ('push', 'pull')]


@pytest.mark.parametrize('run_noop', [False, True])
def test_empty_package_retains_only_eligible_directional_hook_rows(tmp_path, monkeypatch, run_noop):
    log = tmp_path / 'hooks'
    engine = auxiliary_engine(tmp_path, monkeypatch, f'''
[hooks]
pre_pull = {{run = "echo pull >> {log}", run_noop = true}}
pre_push = "echo push >> {log}"
''')
    with open_session(engine, preview=False, run_noop=run_noop) as session:
        assert session.view.observations == ()
        rows = session.view.rows
        assert {(row.scope, row.directions) for row in rows} == ({('main:app', ('pull',)), ('main:app', ('push',))} if run_noop else {('main:app', ('pull',))})
        assert all(row.kind == 'hook' and not row.included for row in rows)
        for row in rows:
            include(session, row)
        result = session.execute().result
        assert result.status == 'completed'
        assert result.units == ()
    assert log.read_text().splitlines() == (['pull', 'push'] if run_noop else ['pull'])


def test_repo_noop_hook_survives_package_guard_removal(tmp_path, monkeypatch):
    log = tmp_path / 'log'
    engine = auxiliary_engine(tmp_path, monkeypatch, '''
[hooks]
guard_push = "exit 100"
[targets.check]
probe = "exit 9"
sync_policy = "push-only"
''', repo_hooks=f'pre_push = {{run = "echo repo >> {log}", run_noop = true}}\n')
    with open_session(engine, preview=False) as session:
        row, guard = session.view.rows
        assert (row.scope, row.kind, row.directions) == ('main', 'hook', ('push',))
        assert (guard.scope, guard.kind) == ('main:app', 'guard-skip')
        include(session, row)
        assert session.execute().result.status == 'completed'
    assert log.read_text().splitlines() == ['repo']


def test_preview_freezes_probe_and_hooks_without_running_or_repeating_them(tmp_path, monkeypatch):
    from dotman.sync_session import Preview
    log = tmp_path / 'log'
    engine = auxiliary_engine(tmp_path, monkeypatch, f'''
[targets.check]
probe = "echo probe >> {log}"
[targets.check.hooks]
pre_push = "exit 9"
pre_pull = "exit 9"
''')
    with open_session(engine) as session:
        include(session, session.view.rows[0])
        for _ in range(2):
            view = session.view
            result = session.dispatch(Preview(view.session_id, view.revision)).result
            assert result.units == ()
            assert result.steps == ()
            assert result.status == 'completed'
    assert log.read_text().splitlines() == ['probe']


def test_hook_failure_is_operation_failure_without_auxiliary_convergence(tmp_path, monkeypatch):
    engine = auxiliary_engine(tmp_path, monkeypatch, '''
[targets.check]
probe = "true"
[targets.check.hooks]
pre_pull = "exit 7"
pre_push = "exit 9"
''')
    with open_session(engine, preview=False) as session:
        include(session, session.view.rows[0])
        result = session.execute().result
        assert result.status == 'failed'
        assert result.units == ()
        assert [(step.stage, step.exit_code, step.status) for step in result.steps] == [
            ('repository-apply', 7, 'failed'), ('live-publication', None, 'unattempted')]


@pytest.mark.parametrize('policy,direction', [('push-only', 'push'), ('push-only-delete', 'push'), ('pull-only', 'pull')])
def test_removed_one_sided_route_is_visible_nonapprovable(tmp_path, monkeypatch, policy, direction):
    engine = make_engine(tmp_path, monkeypatch, [('unit', policy, b'repo', b'live', f'[targets.unit.hooks]\nguard_{direction} = "exit 100"')])
    with open_session(engine) as session:
        row, guard = session.view.rows
        assert guard.kind == 'guard-skip'
        assert row.observation.configured_policy == policy
        assert row.observation.effective_policy == 'no-route'
        assert row.observation.base.status == ('unavailable' if policy == 'pull-only' else 'not-applicable')
        assert row.kind == 'diagnostic'
        assert row.allowed_commands == ()
        assert [item.code for item in row.observation.diagnostics] == ['no-route']


def test_no_confirm_json_executes_auxiliary_work_without_file_output(tmp_path, monkeypatch, capsys):
    import json
    from dotman.cli import main

    log = tmp_path / 'log'
    engine = auxiliary_engine(tmp_path, monkeypatch, f'''
[hooks]
pre_push = "echo hook >> {log}"
[targets.check]
probe = "true"
sync_policy = "pull-only"
''')
    assert main(['--config', str(engine.config.config_path), '--json', '--no-confirm', 'sync', '--run-noop']) == 0
    document = json.loads(capsys.readouterr().out)
    assert document['sync_units'] == []
    assert document['probe_work'] == [{'identity': 'main:app.check', 'selected': True, 'directions': ['pull'], 'diagnostics': []}]
    assert document['hook_work'] == [{'identity': 'main:app', 'selected': True, 'directions': ['push'], 'diagnostics': []}]
    assert document['status'] == 'completed'
    assert document['summary']['selected_auxiliary'] == 2
    assert log.read_text().splitlines() == ['hook']


def test_probe_activates_each_surviving_family_with_directional_environment(tmp_path, monkeypatch):
    log = tmp_path / 'log'
    engine = auxiliary_engine(tmp_path, monkeypatch, f'''
[targets.check]
probe = "true"
[targets.check.hooks]
pre_pull = "echo $DOTMAN_OPERATION >> {log}"
pre_push = "echo $DOTMAN_OPERATION >> {log}"
''')
    with open_session(engine, preview=False) as session:
        include(session, session.view.rows[0])
        assert session.execute().result.status == 'completed'
    assert log.read_text().splitlines() == ['pull', 'push']


@pytest.mark.parametrize('first_is_probe', [False, True])
def test_mixed_policy_execution_keeps_frozen_target_order(tmp_path, monkeypatch, first_is_probe):
    log = tmp_path / 'order'
    engine = make_engine(tmp_path, monkeypatch, [
        ('first', 'pull-only', b'repo', b'live', f'[targets.first.hooks]\npre_pull = "echo first >> {log}"'),
        ('second', 'both', b'repo', b'live', f'[targets.second.hooks]\npre_pull = "echo second >> {log}"'),
    ])
    if first_is_probe:
        manifest = tmp_path / 'repo/packages/app/package.toml'
        text = manifest.read_text()
        start = text.index('[targets.first]')
        end = text.index('[targets.first.hooks]')
        manifest.write_text(text[:start] + '[targets.first]\nprobe = "true"\nsync_policy = "pull-only"\n' + text[end:])
        engine = DotmanEngine.from_config_path(engine.config.config_path)

    with open_session(engine, preview=False) as session:
        assert [target.target_name for target in engine.resolve_sync_scope().targets] == ['first', 'second']
        if not first_is_probe:
            assert [row.row_id for row in session.view.rows] == ['main:app.first', 'main:app.second']
        for row in session.view.rows:
            if row.kind == 'probe':
                include(session, row)
            else:
                view = session.view
                if row.observation.effective_policy == 'both':
                    session.dispatch(SetResolutionIntent(view.session_id, view.revision, row.row_id, 'use-live'))
                    view = session.view
                session.dispatch(SetApproval(view.session_id, view.revision, row.row_id, True))
        result = session.execute().result
        assert result.status == 'completed'
    assert log.read_text().splitlines() == ['first', 'second']


@pytest.mark.parametrize('selector', ['main:app.check', 'app.check'])
def test_exact_cli_selector_resolves_probe_target(tmp_path, monkeypatch, capsys, selector):
    import json
    from dotman.cli import main

    log = tmp_path / 'log'
    engine = auxiliary_engine(tmp_path, monkeypatch, f'''
[targets.check]
probe = "echo probe >> {log}"
sync_policy = "push-only"
''')
    assert main(['--config', str(engine.config.config_path), '--json', '--no-confirm', 'push', selector]) == 0
    document = json.loads(capsys.readouterr().out)
    assert [work['identity'] for work in document['probe_work']] == ['main:app.check']
    assert log.read_text().splitlines() == ['probe']


def test_edit_query_never_resolves_to_a_probe_target(tmp_path, monkeypatch):
    from dotman.edit_resolution import EditResolver

    engine = auxiliary_engine(tmp_path, monkeypatch, '[targets.check]\nprobe = "true"\n')
    resolver = EditResolver(engine.config, engine=engine, interaction=None, use_color=False)
    with pytest.raises(ValueError, match='did not match'):
        resolver.resolve_query_path('app.check')


def test_bare_probe_target_name_never_shadows_a_package(tmp_path, monkeypatch, capsys):
    import json
    from dotman.cli import main
    from tests.helpers import write_tracked_packages_state

    engine = auxiliary_engine(tmp_path, monkeypatch, '[targets.tool]\nprobe = "true"\nsync_policy = "push-only"\n')
    (tmp_path / 'repo/packages/tool').mkdir()
    (tmp_path / 'repo/packages/tool/package.toml').write_text(
        'id = "tool"\n[targets.run]\nprobe = "true"\nsync_policy = "push-only"\n')
    write_tracked_packages_state(tmp_path / 'state', repo_name='main', entries=[('app', 'default'), ('tool', 'default')])
    config = str(engine.config.config_path)
    assert main(['--config', config, '--json', '--no-confirm', 'push', '--dry-run', 'tool']) == 0
    document = json.loads(capsys.readouterr().out)
    assert [work['identity'] for work in document['probe_work']] == ['main:tool.run']
