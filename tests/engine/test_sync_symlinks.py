import pytest

from dotman.engine import DotmanEngine
from dotman.sync_base_store import Missing
from dotman.sync_session import SetApproval
from tests.engine.test_sync_convergence import command
from tests.engine.test_sync_session import make_engine, open_session
from tests.engine.test_sync_directory_observation import directory_engine, open_directory, put


def configured(engine, tmp_path, *, file="prompt", directory="fail"):
    path = tmp_path / "config.toml"
    path.write_text(path.read_text() + f'\n[symlinks]\nfile_symlink_mode = "{file}"\ndir_symlink_mode = "{directory}"\n')
    return DotmanEngine.from_config_path(path)


def linked_file(tmp_path, monkeypatch, *, mode="prompt", policy="push-only", missing=False):
    engine = make_engine(tmp_path, monkeypatch, [("unit", policy, b"frozen", None, "")])
    engine = configured(engine, tmp_path, file=mode)
    path = tmp_path / "live/unit"
    path.parent.mkdir(exist_ok=True)
    referent = tmp_path / "referent"
    if not missing:
        referent.write_bytes(b"old")
    path.symlink_to(referent)
    return engine, path, referent


def test_prompt_requires_semantic_authorization_and_replaces_link(tmp_path, monkeypatch):
    from dotman.sync_session import AuthorizeSymlinkReplacement
    engine, path, referent = linked_file(tmp_path, monkeypatch)
    with open_session(engine, preview=False) as session:
        row, = session.view.rows
        command(session, SetApproval, row.row_id, True)
        assert not session.view.rows[0].approved
        assert session.view.rows[0].diagnostics[0].code == "symlink-authorization-required"
        command(session, AuthorizeSymlinkReplacement, row.row_id)
        command(session, SetApproval, row.row_id, True)
        assert session.execute().result.units[0].status == "converged"
    assert not path.is_symlink()
    assert path.read_bytes() == b"frozen"
    assert referent.read_bytes() == b"old"


@pytest.mark.parametrize("missing", [False, True])
def test_follow_writes_then_current_referent_without_content_reread(tmp_path, monkeypatch, missing):
    engine, path, original = linked_file(tmp_path, monkeypatch, mode="follow", missing=missing)
    with open_session(engine, preview=False) as session:
        row, = session.view.rows
        if missing:
            assert row.observation.live == Missing()
        command(session, SetApproval, row.row_id, True)
        current = tmp_path / "current"
        path.unlink()
        path.symlink_to(current)
        from dotman import sync_observation
        monkeypatch.setattr(sync_observation, "read_bytes", lambda *_: pytest.fail("execution re-observed content"))
        assert session.execute().result.units[0].status == "converged"
    assert path.is_symlink()
    assert current.read_bytes() == b"frozen"
    assert original.exists() is not missing


@pytest.mark.parametrize("mode", ["prompt", "follow"])
def test_deletion_preserves_only_follow_link(tmp_path, monkeypatch, mode):
    engine, path, referent = linked_file(tmp_path, monkeypatch, mode=mode, policy="push-only-delete")
    with open_session(engine, preview=False) as session:
        command(session, SetApproval, session.view.rows[0].row_id, True)
        assert session.execute().result.units[0].status == "converged"
    assert path.is_symlink() == (mode == "follow")
    assert referent.exists() == (mode == "prompt")


def test_directory_link_introduced_after_observation_is_rejected(tmp_path, monkeypatch):
    engine = directory_engine(tmp_path, monkeypatch, policy="push-only")
    repo, live = tmp_path / "repo/packages/app/tree", tmp_path / "live/tree"
    put(repo, "nested/child", b"frozen")
    put(live, "nested/child", b"old")
    with open_directory(engine, preview=False) as session:
        command(session, SetApproval, session.view.rows[0].row_id, True)
        (live / "nested").rename(tmp_path / "elsewhere")
        (live / "nested").symlink_to(tmp_path / "elsewhere", target_is_directory=True)
        result = session.execute().result
        assert result.units[0].status == "execution-failed"
    assert (tmp_path / "elsewhere/child").read_bytes() == b"old"


def test_follow_directory_retarget_preserves_identity(tmp_path, monkeypatch):
    from dotman.sync_base_lifecycle import BaseUnit
    engine = configured(directory_engine(tmp_path, monkeypatch, policy="push-only"), tmp_path, directory="follow")
    repo, live = tmp_path / "repo/packages/app/tree", tmp_path / "live/tree"
    put(repo, "nested/child", b"frozen")
    first, second = tmp_path / "first", tmp_path / "second"
    put(first, "child", b"old")
    put(second, "child", b"later")
    live.mkdir(parents=True)
    (live / "nested").symlink_to(first, target_is_directory=True)
    with open_directory(engine, preview=False) as session:
        row, = session.view.rows
        observed = row.observation
        first_base = BaseUnit(observed.identity, "push-only")
        command(session, SetApproval, row.row_id, True)
        (live / "nested").unlink()
        (live / "nested").symlink_to(second, target_is_directory=True)
        result = session.execute().result
        assert result.units[0].status == "converged"
    with open_directory(engine) as session:
        observed = session.view.observations[0]
        second_base = BaseUnit(observed.identity, "push-only")
        assert observed.identity.canonical == "main:app.tree/nested/child"
        assert first_base.identity_bytes == second_base.identity_bytes
    assert (first / "child").read_bytes() == b"old"
    assert (second / "child").read_bytes() == b"frozen"


def test_prompt_mode_only_effect_replaces_link_using_frozen_bytes(tmp_path, monkeypatch):
    from dotman.sync_session import AuthorizeSymlinkReplacement
    engine, path, referent = linked_file(tmp_path, monkeypatch)
    referent.write_bytes(b"frozen")
    referent.chmod(0o644)
    manifest = tmp_path / "repo/packages/app/package.toml"
    manifest.write_text(manifest.read_text() + '\nchmod = "0600"\n')
    engine = DotmanEngine.from_config_path(tmp_path / "config.toml")
    with open_session(engine, preview=False) as session:
        row, = session.view.rows
        command(session, AuthorizeSymlinkReplacement, row.row_id)
        command(session, SetApproval, row.row_id, True)
        assert [effect.kind for effect in session.view.rows[0].proposal.publication_effects] == ["write", "chmod"]
        assert session.execute().result.units[0].status == "converged"
    assert not path.is_symlink()
    assert path.read_bytes() == b"frozen"
    assert path.stat().st_mode & 0o777 == 0o600
    assert referent.stat().st_mode & 0o777 == 0o644


def test_follow_deletion_never_prunes_external_referent_parents(tmp_path, monkeypatch):
    engine, path, _referent = linked_file(tmp_path, monkeypatch, mode="follow", policy="push-only-delete")
    outside = tmp_path / "external/nested/file"
    put(outside.parent, outside.name)
    path.unlink()
    path.symlink_to(outside)
    with open_session(engine, preview=False) as session:
        command(session, SetApproval, session.view.rows[0].row_id, True)
        assert session.execute().result.units[0].status == "converged"
    assert outside.parent.is_dir()
    assert path.is_symlink()


@pytest.mark.parametrize("shape", ["directory", "loop"])
def test_follow_retarget_to_unsafe_shape_fails_without_mutation(tmp_path, monkeypatch, shape):
    engine, path, referent = linked_file(tmp_path, monkeypatch, mode="follow")
    with open_session(engine, preview=False) as session:
        command(session, SetApproval, session.view.rows[0].row_id, True)
        path.unlink()
        path.symlink_to(tmp_path if shape == "directory" else path)
        result = session.execute().result
        assert result.units[0].status == "execution-failed"
        assert result.units[0].diagnostics[0].code in ("unsupported-entry", "symlink-chain")
    assert referent.read_bytes() == b"old"
    assert path.is_symlink()


def test_repository_leaf_link_is_typed_observation_failure(tmp_path, monkeypatch):
    engine, _path, referent = linked_file(tmp_path, monkeypatch)
    source = tmp_path / "repo/packages/app/unit"
    source.unlink()
    source.symlink_to(referent)
    with open_session(engine) as session:
        observed, = session.view.observations
        assert observed.state == "observation-failed"
        assert observed.diagnostics[0].code == "repository-symlink"


@pytest.mark.parametrize("retarget_to_file", [False, True])
def test_follow_directory_root_mode_preserves_link(tmp_path, monkeypatch, retarget_to_file):
    from dotman.sync_session import SetIncluded
    engine = configured(directory_engine(tmp_path, monkeypatch, policy="push-only"), tmp_path, directory="follow")
    manifest = tmp_path / "repo/packages/app/package.toml"
    manifest.write_text(manifest.read_text().replace('type = "directory"', 'type = "directory"\nchmod = "0700"'))
    engine = DotmanEngine.from_config_path(tmp_path / "config.toml")
    external = tmp_path / "external"
    external.mkdir(mode=0o755)
    link = tmp_path / "live/tree"
    link.parent.mkdir()
    link.symlink_to(external, target_is_directory=True)
    with open_directory(engine, preview=False) as session:
        root, = [row for row in session.view.rows if row.kind == "directory-root"]
        command(session, SetIncluded, root.row_id, True)
        if retarget_to_file:
            replacement = tmp_path / "replacement"
            replacement.write_bytes(b"untouched")
            replacement.chmod(0o644)
            link.unlink()
            link.symlink_to(replacement)
        assert session.execute().result.exit_code == (1 if retarget_to_file else 0)
    assert link.is_symlink()
    assert external.stat().st_mode & 0o777 == (0o755 if retarget_to_file else 0o700)
    if retarget_to_file:
        assert replacement.stat().st_mode & 0o777 == 0o644


def test_follow_dereferences_after_snapshot_access(tmp_path, monkeypatch):
    from dotman import sync_publication
    engine, path, original = linked_file(tmp_path, monkeypatch, mode="follow")
    current = tmp_path / "after-snapshot"
    with open_session(engine, preview=False) as session:
        command(session, SetApproval, session.view.rows[0].row_id, True)
        def snapshot(*_args):
            path.unlink()
            path.symlink_to(current)
        monkeypatch.setattr(sync_publication, "create_push_snapshot", snapshot)
        assert session.execute().result.units[0].status == "converged"
    assert current.read_bytes() == b"frozen"
    assert original.read_bytes() == b"old"
