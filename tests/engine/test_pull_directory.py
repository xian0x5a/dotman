"""Pull's independent children and frozen symlink inputs at the session boundary."""
import pytest

from dotman.sync_base_store import DirectoryChildPresent, Missing
from dotman.sync_session import CommandRejected, SetApproval
from tests.engine.test_sync_convergence import command
from tests.engine.test_sync_directory_observation import directory_engine, put
from tests.engine.test_sync_symlinks import configured, linked_file


def test_pull_children_have_independent_opt_out_frozen_bytes_and_modes(tmp_path, monkeypatch):
    engine = directory_engine(tmp_path, monkeypatch, extra='''
[targets.tree.path_rules.mode]
pattern = "*"
chmod = "0700"
''')
    repo, live = tmp_path / "repo/packages/app/tree", tmp_path / "live/tree"
    put(repo, "deleted/nested", b"old")
    put(repo, "keep", b"repo")
    put(live, "keep", b"live")
    put(live, "created/nested", b"frozen").chmod(0o700)
    with engine.open_pull_session(engine.resolve_sync_scope()) as session:
        rows = {row.observation.identity.child_path: row for row in session.view.rows}
        assert all(row.approved and row.intent == "use-live" for row in rows.values())
        assert rows["created/nested"].proposal.repository == DirectoryChildPresent(b"frozen", True)
        assert rows["deleted/nested"].proposal.repository == Missing()
        assert all(not row.proposal.publication_effects for row in rows.values())
        command(session, SetApproval, rows["keep"].row_id, False)
        (live / "created/nested").write_bytes(b"external")
        (live / "created/nested").chmod(0o600)
        result = session.execute().result
        assert {unit.identity: unit.status for unit in result.units} == {
            "main:app.tree/created/nested": "applied",
            "main:app.tree/deleted/nested": "applied",
            "main:app.tree/keep": "pending",
        }
    assert (repo / "created/nested").read_bytes() == b"frozen"
    assert (repo / "created/nested").stat().st_mode & 0o111
    assert not (repo / "deleted").exists()
    assert repo.is_dir()
    assert (repo / "keep").read_bytes() == b"repo"
    assert (live / "created/nested").stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("to_directory", [True, False])
def test_pull_topology_requires_selected_deletion_before_writer(tmp_path, monkeypatch, to_directory):
    engine = directory_engine(tmp_path, monkeypatch)
    repo, live = tmp_path / "repo/packages/app/tree", tmp_path / "live/tree"
    old, new = ("node", "node/child") if to_directory else ("node/child", "node")
    put(repo, old, b"old")
    put(live, new, b"new")
    with engine.open_pull_session(engine.resolve_sync_scope()) as session:
        deletion = next(row for row in session.view.rows if row.observation.identity.child_path == old)
        command(session, SetApproval, deletion.row_id, False)
        assert isinstance(session.execute(), CommandRejected)
        assert (repo / old).read_bytes() == b"old"
        command(session, SetApproval, deletion.row_id, True)
        result = session.execute().result
        assert result.status == "completed"
        assert [step.scope_identity for step in result.steps if step.action in {"delete", "update"}] == [
            f"main:app.tree/{old}", f"main:app.tree/{new}",
        ]
    assert (repo / new).read_bytes() == b"new"


@pytest.mark.parametrize("mode,missing", [("prompt", False), ("follow", False), ("follow", True)])
def test_pull_file_links_are_frozen_inputs_never_publication(tmp_path, monkeypatch, mode, missing):
    engine, link, referent = linked_file(tmp_path, monkeypatch, mode=mode, policy="both", missing=missing)
    with engine.open_pull_session(engine.resolve_sync_scope()) as session:
        row, = session.view.rows
        assert row.approved and not row.proposal.publication_effects
        replacement = tmp_path / "replacement"
        replacement.write_bytes(b"external")
        link.unlink()
        link.symlink_to(replacement)
        assert session.execute().result.units[0].status == "applied"
    source = tmp_path / "repo/packages/app/unit"
    assert (source.read_bytes() if source.exists() else None) == (None if missing else b"old")
    assert link.is_symlink() and replacement.read_bytes() == b"external"
    assert referent.exists() is not missing


@pytest.mark.parametrize("mode", ["fail", "follow"])
def test_pull_directory_link_policy_and_lexical_identity(tmp_path, monkeypatch, mode):
    engine = configured(directory_engine(tmp_path, monkeypatch), tmp_path, directory=mode)
    repo, live = tmp_path / "repo/packages/app/tree", tmp_path / "live/tree"
    put(repo, "nested/child", b"repo")
    referent = tmp_path / "referent"
    put(referent, "child", b"frozen")
    live.mkdir(parents=True)
    (live / "nested").symlink_to(referent, target_is_directory=True)
    with engine.open_pull_session(engine.resolve_sync_scope()) as session:
        if mode == "fail":
            assert any(unit.state == "observation-failed" for unit in session.view.observations)
            assert session.execute().result.status == "failed"
            assert (repo / "nested/child").read_bytes() == b"repo"
        else:
            row, = session.view.rows
            assert row.row_id == "main:app.tree/nested/child"
            (referent / "child").write_bytes(b"external")
            assert session.execute().result.units[0].status == "applied"
            assert (repo / "nested/child").read_bytes() == b"frozen"
    assert (live / "nested").is_symlink()
