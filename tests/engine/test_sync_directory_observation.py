import pytest

from dotman.engine import DotmanEngine
from dotman.sync_base_store import DirectoryChildPresent, Missing
from dotman.sync_session import SyncSession
from tests.engine.test_sync_session import make_engine
from tests.helpers import resolved_child_metadata


def directory_engine(tmp_path, monkeypatch, *, extra='', policy='both', inferred=False):
    make_engine(tmp_path, monkeypatch, [('tree', policy, None, None, '')])
    manifest = tmp_path / 'repo/packages/app/package.toml'
    text = manifest.read_text().replace('type = "file"', '' if inferred else 'type = "directory"')
    manifest.write_text(text + '\n' + extra)
    (tmp_path / 'repo/repo.toml').write_text('[ignore]\ngitignore = true\nskip_markers = [".dotman-skip"]\n')
    return DotmanEngine.from_config_path(tmp_path / 'config.toml')


def put(root, name, content=b'bytes'):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def open_directory(engine, selectors=None, *, preview=True):
    opened = engine.open_sync_session(engine.resolve_sync_scope(selectors), preview=preview)
    assert isinstance(opened, SyncSession), opened
    return opened


def test_directory_direct_agreement_checkpoints_dirty_child_executable_state(tmp_path, monkeypatch):
    engine = directory_engine(tmp_path, monkeypatch)
    repo, live = tmp_path / 'repo/packages/app/tree', tmp_path / 'live/tree'
    put(repo, 'child', b'dirty').chmod(0o755)
    put(live, 'child', b'dirty').chmod(0o700)
    with open_directory(engine, preview=False) as session:
        child, = session.view.observations
        assert child.state == 'directly-in-sync'
        assert child.base.acknowledged
    with open_directory(engine) as session:
        child, = session.view.observations
        assert child.base.record.payload == DirectoryChildPresent(b'dirty', True)


def test_directory_census_unions_independent_children_without_aggregate(tmp_path, monkeypatch):
    engine = directory_engine(tmp_path, monkeypatch)
    repo, live = tmp_path / 'repo/packages/app/tree', tmp_path / 'live/tree'
    put(repo, 'same', b'equal'); put(live, 'same', b'equal')
    put(repo, 'nested/repo-only', b'repo'); put(live, 'live-only', b'live')
    (repo / 'empty').mkdir()
    with open_directory(engine) as session:
        units = session.view.observations
        assert [unit.identity.canonical for unit in units] == [
            'main:app.tree/live-only', 'main:app.tree/nested/repo-only', 'main:app.tree/same',
        ]
        assert [unit.state for unit in units] == ['drifted', 'drifted', 'directly-in-sync']
        assert units[0].repository == Missing()
        assert units[0].live == DirectoryChildPresent(b'live', False)
        assert units[1].live == Missing()
        assert [row.row_id for row in session.view.rows] == [
            'main:app.tree/live-only', 'main:app.tree/nested/repo-only',
        ]


def test_combined_controls_exclude_both_trees_before_observation(tmp_path, monkeypatch):
    engine = directory_engine(tmp_path, monkeypatch, extra='[targets.tree.ignore]\npatterns = ["excluded/", "*.secret"]')
    repo, live = tmp_path / 'repo/packages/app/tree', tmp_path / 'live/tree'
    # Repository Git controls apply to live-only paths too; live controls are not policy.
    put(repo, '.gitignore', b'*.ignored\nnested/hidden\n')
    put(repo, 'nested/.gitignore', b'*.local\n')
    put(live, '.gitignore', b'keep\n')
    for root in (repo, live):
        for name in ('keep', 'a.ignored', 'nested/a.local', 'nested/hidden', 'excluded/bad', 'private.secret', 'repo-marked/a', 'live-marked/a'):
            put(root, name)
    put(repo, 'repo-marked/.dotman-skip')
    put(live, 'live-marked/.dotman-skip')
    with open_directory(engine) as session:
        assert [unit.identity.child_path for unit in session.view.observations] == ['keep']


def test_unsupported_repository_nodes_are_path_local_and_exclusions_win(tmp_path, monkeypatch):
    import os

    engine = directory_engine(tmp_path, monkeypatch, extra='[targets.tree.ignore]\npatterns = ["ignored/", "skip-link/"]')
    repo, live = tmp_path / 'repo/packages/app/tree', tmp_path / 'live/tree'
    put(repo, 'good', b'repo'); put(live, 'good', b'live')
    external = tmp_path / 'external'
    put(external, 'must-not-traverse')
    (repo / 'link').symlink_to(external, target_is_directory=True)
    (repo / 'skip-link').symlink_to(external, target_is_directory=True)
    os.mkfifo(repo / 'pipe')
    put(repo, 'ignored/keep')
    os.mkfifo(repo / 'ignored/pipe')
    (repo / 'dangling').symlink_to(tmp_path / 'absent')
    with open_directory(engine) as session:
        units = {unit.identity.child_path: unit for unit in session.view.observations}
        assert set(units) == {'good', 'link', 'pipe', 'dangling'}
        assert units['good'].state == 'drifted'
        for name, code in [('link', 'repository-symlink'), ('dangling', 'repository-symlink'), ('pipe', 'unsupported-entry')]:
            assert units[name].state == 'observation-failed'
            assert units[name].diagnostics[0].code == code
            assert name in units[name].diagnostics[0].message
    with open_directory(engine, ['main:app.tree/good']) as session:
        assert [unit.identity.child_path for unit in session.view.observations] == ['good']


def test_child_rules_resolve_on_missing_endpoints_and_guards_run_once(tmp_path, monkeypatch):
    log = tmp_path / 'guards'
    engine = directory_engine(tmp_path, monkeypatch, policy='push-only', extra=f'''
[targets.tree.hooks]
guard_push = "echo target-push >> {log}"
guard_pull = "echo target-pull >> {log}"
[targets.tree.path_rules.incoming]
pattern = "incoming/*"
sync_policy = "both"
render = "printf rendered"
capture = "printf captured"
chmod = "0700"
[targets.tree.path_rules.incoming.compare]
repo = "render"
live = "raw"
[targets.tree.path_rules.incoming.hooks]
guard_push = "echo rule-push >> {log}; exit 100"
guard_pull = "echo rule-pull >> {log}"
''')
    repo, live = tmp_path / 'repo/packages/app/tree', tmp_path / 'live/tree'
    put(live, 'incoming/a', b'live'); put(live, 'incoming/b', b'live')
    put(repo, 'outgoing', b'repo')
    with open_directory(engine) as session:
        units = {unit.identity.child_path: unit for unit in session.view.observations}
        for name in ('incoming/a', 'incoming/b'):
            unit = units[name]
            assert unit.repository == Missing()
            assert unit.configured_policy == 'both'
            assert unit.effective_policy == 'pull-only'
            resolved = resolved_child_metadata(engine, name)
            assert (resolved.render_command, resolved.capture_command) == ('printf rendered', 'printf captured')
            assert (unit.compare_repo, unit.compare_live, unit.chmod) == ('render', 'raw', '0700')
        assert units['outgoing'].effective_policy == 'push-only'
        assert log.read_text().splitlines() == ['target-push', 'target-pull', 'rule-push', 'rule-pull']


def test_live_child_under_repository_link_never_reads_external_tree(tmp_path, monkeypatch):
    engine = directory_engine(tmp_path, monkeypatch)
    repo, live = tmp_path / 'repo/packages/app/tree', tmp_path / 'live/tree'
    repo.mkdir()
    external = tmp_path / 'external'
    put(external, 'child', b'must not read')
    put(live, 'link/child', b'must not read')
    (repo / 'link').symlink_to(external, target_is_directory=True)
    with open_directory(engine) as session:
        child = next(unit for unit in session.view.observations if unit.identity.child_path == 'link/child')
        assert child.state == 'observation-failed'
        assert child.repository is None
        assert child.diagnostics[0].code == 'repository-symlink'


def test_child_mode_comparison_uses_executable_state_and_rule_chmod(tmp_path, monkeypatch):
    engine = directory_engine(tmp_path, monkeypatch, extra='''
[targets.tree.path_rules.exact]
pattern = "exact"
sync_policy = "push-only"
chmod = "0600"
''')
    repo, live = tmp_path / 'repo/packages/app/tree', tmp_path / 'live/tree'
    for name in ('permissions', 'executable', 'exact'):
        put(repo, name).chmod(0o644)
        put(live, name).chmod(0o600)
    (live / 'executable').chmod(0o700)
    (live / 'exact').chmod(0o644)
    with open_directory(engine) as session:
        units = {unit.identity.child_path: unit for unit in session.view.observations}
        assert units['permissions'].state == 'directly-in-sync'
        assert units['executable'].state == 'drifted'
        assert units['executable'].repository.executable is False
        assert units['exact'].state == 'drifted'


def test_children_freeze_independent_selection_and_endpoints(tmp_path, monkeypatch):
    from dotman.sync_session import SetApproval, SetIncluded

    engine = directory_engine(tmp_path, monkeypatch)
    repo, live = tmp_path / 'repo/packages/app/tree', tmp_path / 'live/tree'
    put(repo, 'a', b'repo'); put(live, 'a', b'live')
    put(repo, 'b', b'repo'); put(live, 'b', b'live')
    with open_directory(engine, ['main:app.tree/a', 'main:app.tree', 'main:app.tree/a'], preview=False) as session:
        before = session.view.observations
        assert len(before) == 2
        assert all(not unit.base.acknowledged for unit in before)
        a, _b = session.view.rows
        assert a.observation.state == 'drifted' and not a.observation.diagnostics
        assert 'set-approval' in a.allowed_commands
        session.dispatch(SetApproval(session.view.session_id, session.view.revision, a.row_id, True))
        session.dispatch(SetIncluded(session.view.session_id, session.view.revision, a.row_id, False))
        assert not session.view.rows[0].included and session.view.rows[1].included
        put(repo, 'b', b'changed after open')
        assert session.view.observations == before
        result = session.execute().result
        assert [unit.status for unit in result.units] == ['excluded', 'pending']
        assert result.status == 'completed'
        assert (live / 'b').read_bytes() == b'live'
        assert (repo / 'b').read_bytes() == b'changed after open'


def test_live_directory_links_keep_lexical_identity_and_failures_local(tmp_path, monkeypatch):
    engine = directory_engine(tmp_path, monkeypatch)
    repo, live = tmp_path / 'repo/packages/app/tree', tmp_path / 'live/tree'
    external = tmp_path / 'external'
    put(external, 'child', b'live')
    put(repo, 'good', b'repo')
    live.mkdir(parents=True)
    (live / 'link').symlink_to(external, target_is_directory=True)
    with open_directory(engine) as session:
        units = {unit.identity.child_path: unit for unit in session.view.observations}
        assert set(units) == {'good', 'link'}
        assert units['link'].diagnostics[0].code == 'directory-symlink'
    config = tmp_path / 'config.toml'
    config.write_text(config.read_text() + '\n[symlinks]\ndir_symlink_mode = "follow"\n')
    engine = DotmanEngine.from_config_path(config)
    with open_directory(engine) as session:
        units = {unit.identity.child_path: unit for unit in session.view.observations}
        assert set(units) == {'good', 'link/child'}
        assert units['link/child'].live == DirectoryChildPresent(b'live', False)
        assert units['link/child'].live_path == live / 'link/child'


def test_inferred_directory_and_root_marker_do_not_become_payloads(tmp_path, monkeypatch):
    engine = directory_engine(tmp_path, monkeypatch, inferred=True)
    repo, live = tmp_path / 'repo/packages/app/tree', tmp_path / 'live/tree'
    put(repo, 'child')
    with open_directory(engine, ['main:app.tree/child']) as session:
        assert [unit.identity.canonical for unit in session.view.observations] == ['main:app.tree/child']
    put(live, '.dotman-skip')
    with open_directory(engine) as session:
        assert session.view.observations == ()
        assert session.view.rows == ()


def test_partial_child_selection_retains_ancestor_discovery_failure(tmp_path, monkeypatch):
    engine = directory_engine(tmp_path, monkeypatch)
    repo = tmp_path / 'repo/packages/app/tree'
    repo.mkdir()
    external = tmp_path / 'external'
    put(external, 'child', b'not managed')
    (repo / 'link').symlink_to(external, target_is_directory=True)
    with open_directory(engine, ['main:app.tree/link/child']) as session:
        unit, = session.view.observations
        assert unit.identity.canonical == 'main:app.tree/link/child'
        assert unit.state == 'observation-failed'
        assert unit.diagnostics[0].code == 'repository-symlink'
        assert unit.repository is None


def test_nested_git_controls_override_parent_before_lexical_child_order(tmp_path, monkeypatch):
    engine = directory_engine(tmp_path, monkeypatch)
    repo, live = tmp_path / 'repo/packages/app/tree', tmp_path / 'live/tree'
    put(repo, '.gitignore', b'*.ignored\nblocked/\n')
    put(repo, '.early/.gitignore', b'!keep.ignored\n# comment\n\n')
    put(repo, 'blocked/.gitignore', b'!keep.ignored\n')
    put(live, '.early/keep.ignored')
    put(live, 'blocked/keep.ignored')
    with open_directory(engine) as session:
        assert [unit.identity.child_path for unit in session.view.observations] == ['.early/keep.ignored']


def test_directory_only_patterns_do_not_exclude_regular_files(tmp_path, monkeypatch):
    engine = directory_engine(tmp_path, monkeypatch, extra='[targets.tree.ignore]\npatterns = ["dotman-dir/"]')
    repo, live = tmp_path / 'repo/packages/app/tree', tmp_path / 'live/tree'
    put(repo, '.gitignore', b'git-dir/\n')
    put(repo, 'git-dir')
    put(live, 'dotman-dir')
    with open_directory(engine) as session:
        assert [unit.identity.child_path for unit in session.view.observations] == ['dotman-dir', 'git-dir']


@pytest.mark.parametrize('exact', [False, True])
def test_rejected_live_directory_link_blocks_existing_descendant_candidates(tmp_path, monkeypatch, exact):
    from pathlib import Path

    engine = directory_engine(tmp_path, monkeypatch)
    repo, live = tmp_path / 'repo/packages/app/tree', tmp_path / 'live/tree'
    put(repo, 'link/child', b'repo')
    put(repo, 'safe', b'safe')
    external = tmp_path / 'external'
    put(external, 'child', b'external')
    live.mkdir(parents=True)
    (live / 'link').symlink_to(external, target_is_directory=True)
    read_bytes = Path.read_bytes

    def reject_child_read(path):
        assert path not in (repo / 'link/child', live / 'link/child'), 'rejected traversal reached payload read'
        return read_bytes(path)

    monkeypatch.setattr(Path, 'read_bytes', reject_child_read)
    selectors = ['main:app.tree/link/child'] if exact else None
    with open_directory(engine, selectors) as session:
        units = {unit.identity.child_path: unit for unit in session.view.observations}
        child = units['link/child']
        assert child.state == 'observation-failed'
        assert child.repository is None and child.live is None
        assert child.diagnostics[0].code == 'directory-symlink'
        if not exact:
            assert units['safe'].state == 'drifted'


@pytest.mark.parametrize('exact', [False, True])
def test_unknown_git_controls_block_existing_descendant_candidates(tmp_path, monkeypatch, exact):
    from pathlib import Path
    from dotman import file_access

    engine = directory_engine(tmp_path, monkeypatch)
    repo, live = tmp_path / 'repo/packages/app/tree', tmp_path / 'live/tree'
    control = put(repo, 'private/.gitignore', b'secret\n')
    secret = put(live, 'private/secret', b'must not observe')
    put(repo, 'safe', b'safe')
    read_bytes = Path.read_bytes

    def denied_control_read(path):
        if path == control:
            raise PermissionError('control access denied')
        assert path != secret, 'unknown controls allowed payload read'
        return read_bytes(path)

    def deny_elevation(_reason):
        raise PermissionError('control access denied')

    monkeypatch.setattr(Path, 'read_bytes', denied_control_read)
    monkeypatch.setattr(file_access, 'request_sudo', deny_elevation)
    selectors = ['main:app.tree/private/secret'] if exact else None
    with open_directory(engine, selectors) as session:
        units = {unit.identity.child_path: unit for unit in session.view.observations}
        child = units['private/secret']
        assert child.state == 'observation-failed'
        assert child.repository is None and child.live is None
        assert child.diagnostics[0].code == 'census-failed'
        assert 'private' in child.diagnostics[0].message
        if not exact:
            assert units['safe'].state == 'drifted'


@pytest.mark.parametrize('policy,chmod,repo_mode,live_mode,state', [
    ('both', '0600', 0o644, 0o644, 'drifted'),
    ('both', '0600', 0o644, 0o600, 'directly-in-sync'),
    ('both', '0700', 0o644, 0o700, 'drifted'),
    ('pull-only', '0600', 0o644, 0o644, 'directly-in-sync'),
    ('pull-only', '0700', 0o644, 0o700, 'drifted'),
])
def test_child_exact_mode_requires_surviving_push_and_preserves_executable_comparison(
    tmp_path, monkeypatch, policy, chmod, repo_mode, live_mode, state,
):
    engine = directory_engine(tmp_path, monkeypatch, extra=f'''
[targets.tree.path_rules.mode]
pattern = "child"
sync_policy = "{policy}"
chmod = "{chmod}"
''')
    put(tmp_path / 'repo/packages/app/tree', 'child').chmod(repo_mode)
    put(tmp_path / 'live/tree', 'child').chmod(live_mode)
    with open_directory(engine) as session:
        unit, = session.view.observations
        assert unit.effective_policy == policy
        assert unit.state == state


@pytest.mark.parametrize('target_policy', ['push-only', 'both'])
def test_exact_child_scopes_only_activate_their_configured_directions(tmp_path, monkeypatch, target_policy):
    from dotman.sync_session import SessionOpenFailed

    log = tmp_path / 'pull-guard'
    engine = directory_engine(tmp_path, monkeypatch, policy=target_policy, extra=f'''
[targets.tree.hooks]
guard_pull = "echo pull >> {log}; exit 7"
[targets.tree.path_rules.outgoing]
pattern = "outgoing"
sync_policy = "push-only"
[targets.tree.path_rules.incoming]
pattern = "incoming/*"
sync_policy = "both"
''')
    put(tmp_path / 'repo/packages/app/tree', 'outgoing')
    put(tmp_path / 'live/tree', 'incoming/a')
    with open_directory(engine, ['main:app.tree/outgoing']) as session:
        unit, = session.view.observations
        assert unit.effective_policy == 'push-only'
        assert not log.exists()
    # Full-target selection still admits the direction configured for incoming children.
    opened = engine.open_sync_session(engine.resolve_sync_scope(['main:app.tree']), preview=True)
    assert isinstance(opened, SessionOpenFailed)
    assert opened.diagnostic.code == 'planning-failed'
    assert 'guard_pull failed with exit 7' in opened.diagnostic.message
    assert log.read_text().splitlines() == ['pull']


def test_unified_exclusions_hide_synthesized_descendants_of_failed_scopes(tmp_path, monkeypatch):
    engine = directory_engine(tmp_path, monkeypatch, extra='[targets.tree.ignore]\npatterns = ["*.secret"]')
    repo = tmp_path / 'repo/packages/app/tree'
    repo.mkdir()
    external = tmp_path / 'external'
    put(external, 'private.secret', b'excluded')
    (repo / 'link').symlink_to(external, target_is_directory=True)
    with open_directory(engine, ['main:app.tree/link/private.secret']) as session:
        assert session.view.observations == ()
        assert session.view.rows == ()

@pytest.mark.parametrize("enabled", [True, False])
def test_package_gitignore_boolean_overrides_repository_for_sync_and_push(tmp_path, monkeypatch, enabled):
    engine = directory_engine(tmp_path, monkeypatch, policy="push-only")
    manifest = tmp_path / "repo/packages/app/package.toml"
    manifest.write_text(manifest.read_text() + f"\n[ignore]\ngitignore = {str(enabled).lower()}\n")
    put(tmp_path / "repo/packages/app/tree", ".gitignore", b"private\n")
    put(tmp_path / "repo/packages/app/tree", "private", b"payload")
    put(tmp_path / "repo/packages/app/tree", "visible", b"payload")
    engine = DotmanEngine.from_config_path(tmp_path / "config.toml")
    for open_session in (engine.open_sync_session, engine.open_push_session):
        with open_session(engine.resolve_sync_scope(), preview=True) as session:
            names = {item.identity.child_path for item in session.view.observations}
            assert ("private" in names) is not enabled
            assert "visible" in names
