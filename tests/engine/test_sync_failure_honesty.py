"""Ordered, non-transactional results are the execution contract."""
from dataclasses import replace

import pytest

from dotman import sync_publication as publication
from dotman.sync_session import SetApproval, SetResolutionIntent
from tests.engine.test_sync_convergence import command
from tests.engine.test_sync_publication import prepare
from tests.engine.test_sync_session import make_engine, open_session


def test_publication_ack_failure_has_exact_boundary_and_unattempted_tail(tmp_path, monkeypatch):
    metadata, units = prepare(tmp_path, monkeypatch, [
        (name, "both", b"repo", b"live", "") for name in ("first", "second")
    ])
    def fail(unit):
        raise RuntimeError("ack denied")
    result = publication.execute_publication(
        metadata, units, complete=fail,
        snapshot_config=publication.SnapshotConfig(False, tmp_path / "snapshots", 10),
    )
    assert [(s.step.kind, s.step.action, s.status) for s in result.steps] == [
        ("target", "write", "ok"), ("unit-completion", "complete", "failed"),
        ("target", "write", "unattempted"), ("unit-completion", "complete", "unattempted"),
    ]
    assert [u.status for u in result.units] == ["failed", "skipped"]
    assert (tmp_path / "live/first").read_bytes() == b"frozen"
    assert (tmp_path / "live/second").read_bytes() == b"live"


def test_repository_success_then_push_pre_failure_is_not_skipped(tmp_path, monkeypatch):
    engine = make_engine(tmp_path, monkeypatch, [
        ("unit", "both", b"old", b"live",
         'render = "sed s/live/published/ $DOTMAN_SOURCE"\ncompare = { repo = "raw", live = "raw" }\n'
         '[targets.unit.hooks]\npre_push = "exit 7"'),
    ])
    with open_session(engine, preview=False) as session:
        command(session, SetResolutionIntent, "main:app.unit", "use-live")
        command(session, SetApproval, "main:app.unit", True)
        result = session.execute().result
    assert result.units[0].status == "not-converged"
    assert [(s.stage, s.action, s.status) for s in result.steps] == [
        ("repository-apply", "update", "ok"),
        ("live-publication", "pre_push", "failed"),
        ("live-publication", "write", "unattempted"),
        ("live-publication", "complete", "unattempted"),
    ]
    assert (tmp_path / "repo/packages/app/unit").read_bytes() == b"live"
    assert (tmp_path / "live/unit").read_bytes() == b"live"


def test_cancellation_between_effects_keeps_mutation_and_stops_chmod(tmp_path, monkeypatch):
    metadata, units = prepare(tmp_path, monkeypatch, [("unit", "both", b"repo", b"live", "")])
    from dotman.sync_session import PublicationEffect
    units = [replace(units[0], effects=(*units[0].effects,
        PublicationEffect("chmod", units[0].effects[0].path, mode=0o600)))]
    cancelled = False
    write = publication.file_access.write_bytes_atomic
    def write_and_cancel(*args, **kwargs):
        nonlocal cancelled
        write(*args, **kwargs)
        cancelled = True
    def check():
        if cancelled:
            raise InterruptedError("cancelled")
    monkeypatch.setattr(publication.file_access, "write_bytes_atomic", write_and_cancel)
    result = publication.execute_publication(
        metadata, units, check_cancelled=check,
        snapshot_config=publication.SnapshotConfig(False, tmp_path / "snapshots", 10),
    )
    assert result.interrupted
    assert [(s.step.action, s.status) for s in result.steps] == [
        ("write", "ok"), ("chmod", "interrupted"), ("complete", "unattempted"),
    ]
    assert (tmp_path / "live/unit").read_bytes() == b"frozen"


def test_complete_repository_stage_precedes_publication_and_keeps_frozen_bytes(tmp_path, monkeypatch):
    log = tmp_path / "order"
    engine = make_engine(tmp_path, monkeypatch, [
        (name, "both", b"old", b"live",
         'render = "sed s/live/published/ $DOTMAN_SOURCE"\ncompare = { repo = "raw", live = "raw" }\n'
         f'[targets.{name}.hooks]\npost_pull = "echo pull:{name} >> {log}"\n'
         f'pre_push = "echo push:{name} >> {log}; echo external > $DOTMAN_SOURCE"')
        for name in ("first", "second")
    ])
    with open_session(engine, preview=False) as session:
        for row in session.view.rows:
            command(session, SetResolutionIntent, row.row_id, "use-live")
            command(session, SetApproval, row.row_id, True)
        def forbidden(*args, **kwargs):
            pytest.fail("execution must not rematerialize frozen work")
        monkeypatch.setattr(session, "_capture", forbidden)
        monkeypatch.setattr(session, "_render", forbidden)
        result = session.execute().result
    assert [u.status for u in result.units] == ["converged", "converged"]
    assert log.read_text().splitlines() == ["pull:first", "pull:second", "push:first", "push:second"]
    assert all((tmp_path / f"live/{name}").read_bytes() == b"published"
               for name in ("first", "second"))


def test_repository_partial_success_is_retained_when_later_repository_work_fails(tmp_path, monkeypatch):
    engine = make_engine(tmp_path, monkeypatch, [
        (name, "both", b"old", b"live",
         'render = "sed s/live/published/ $DOTMAN_SOURCE"\ncompare = { repo = "raw", live = "raw" }\n'
         + ('[targets.second.hooks]\npre_pull = "exit 9"' if name == "second" else ""))
        for name in ("first", "second")
    ])
    with open_session(engine, preview=False) as session:
        for row in session.view.rows:
            command(session, SetResolutionIntent, row.row_id, "use-live")
            command(session, SetApproval, row.row_id, True)
        result = session.execute().result
    assert [u.status for u in result.units] == ["not-converged", "skipped"]
    assert (tmp_path / "repo/packages/app/first").read_bytes() == b"live"
    assert (tmp_path / "repo/packages/app/second").read_bytes() == b"old"
    assert all(s.status == "unattempted" for s in result.steps if s.stage == "live-publication")
    assert all((tmp_path / f"live/{name}").read_bytes() == b"live" for name in ("first", "second"))


def test_ineligible_no_write_completes_only_at_its_ordered_repository_position(tmp_path, monkeypatch):
    from dotman import sync_repository_apply as repository
    from tests.engine.test_sync_repository_apply import prepare as prepare_repository
    metadata, units = prepare_repository(tmp_path, monkeypatch, ["first", "second", "third"])
    units = [replace(unit, outcome=None) if index != 1 else unit for index, unit in enumerate(units)]
    (tmp_path / "repo/packages/app/second").unlink()
    (tmp_path / "repo/packages/app/second").mkdir()
    completed = []
    result = repository.execute_repository_apply(metadata, units, complete=completed.append)
    assert completed == [units[0]]
    assert [u.status for u in result.units] == ["ok", "failed", "skipped"]
    assert [(s.step.target_plan.target_name, s.step.action, s.status) for s in result.steps] == [
        ("second", "update", "failed"), ("second", "complete", "unattempted"),
        ("third", "complete", "unattempted"),
    ]


def test_stage_traversal_uses_configured_repo_and_package_target_plan_order(tmp_path, monkeypatch):
    from dotman import sync_repository_apply as repository
    from tests.engine.test_sync_repository_apply import prepare as prepare_repository
    metadata, original = prepare_repository(tmp_path, monkeypatch, [f"file{i}" for i in range(8)])
    base = metadata.packages[0]
    packages, units = [], []
    for index, (repo, package_name) in enumerate([
        ("z-repo", "z-dependency"), ("a-repo", "z-dependency"),
        ("z-repo", "a-dependent"), ("a-repo", "a-dependent"),
    ]):
        selection = replace(base.selection, identity=replace(base.selection.identity,
                            repo=repo, package_id=package_name))
        targets = [replace(base.target_plans[i], package_id=package_name)
                   for i in (index * 2 + 1, index * 2)]
        packages.append(replace(base, selection=selection, target_plans=targets))
        for target in targets:
            identity = publication._target_identity(packages[-1], target)
            units.append(repository.RepositoryApplyUnit(identity.canonical, identity, original[0].outcome))
    metadata = replace(metadata, packages=tuple(packages), repo_hooks=(("z-repo", ()), ("a-repo", ())))
    completed = []
    result = repository.execute_repository_apply(metadata, tuple(reversed(units)), complete=lambda u: completed.append(u.row_id))
    expected = [units[index].row_id for index in (0, 1, 4, 5, 2, 3, 6, 7)]
    assert result.error is None
    assert completed == expected
    assert [unit.row_id for unit in result.units] == expected
    assert [s.step.target_plan.target_name for s in result.steps] == [
        identity.rsplit(".", 1)[1] for identity in expected]


@pytest.mark.parametrize("error_type,status", [(PermissionError, "execution-failed"), (InterruptedError, "interrupted")])
def test_failed_chmod_keeps_published_bytes_and_prior_authoritative_base(tmp_path, monkeypatch, error_type, status):
    from dotman.sync_base_store import SyncBaseStore
    from tests.engine.test_sync_both_convergence import BASE, REPO, LIVE, MERGED
    engine = make_engine(tmp_path, monkeypatch, [("unit", "both", BASE, BASE, 'chmod = "0600"')])
    live = tmp_path / "live/unit"
    live.chmod(0o600)
    with open_session(engine, preview=False):
        pass
    (tmp_path / "repo/packages/app/unit").write_bytes(REPO)
    live.write_bytes(LIVE)
    live.chmod(0o644)
    with open_session(engine, preview=False) as session:
        before = session.view.observations[0].base.record
        command(session, SetApproval, "main:app.unit", True)
        assert [e.kind for e in session.view.rows[0].proposal.publication_effects] == ["write", "chmod"]
        def fail(*args, **kwargs):
            raise error_type("mode denied")
        def forbidden(*args, **kwargs):
            pytest.fail("failed chmod cannot replace the authoritative Base")
        with monkeypatch.context() as patch:
            patch.setattr(publication.file_access, "chmod", fail)
            patch.setattr(SyncBaseStore, "replace", forbidden)
            result = session.execute().result
        assert result.units[0].status == status
        assert [(s.action, s.status) for s in result.steps if s.stage == "live-publication"] == [
            ("write", "ok"), ("chmod", "interrupted" if status == "interrupted" else "failed"),
            ("complete", "unattempted"),
        ]
    assert live.read_bytes() == MERGED
    assert (tmp_path / "repo/packages/app/unit").read_bytes() == MERGED
    with open_session(engine) as later:
        assert later.view.observations[0].base.record == before
