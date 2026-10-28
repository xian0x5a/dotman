from __future__ import annotations

import os
import stat
from dataclasses import FrozenInstanceError

import pytest

from dotman.engine import DotmanEngine
from dotman.sync_session import SyncSession
from dotman.sync_base_store import FilePresent, Missing
from tests.helpers import write_sync_repository


def make_engine(tmp_path, monkeypatch, targets, **repository):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    return DotmanEngine.from_config_path(write_sync_repository(tmp_path, targets, **repository))


def open_session(engine, *, preview=True, **kwargs):
    session = engine.open_sync_session(
        engine.resolve_sync_scope(), preview=preview, **kwargs
    )
    assert isinstance(session, SyncSession), session
    return session


def test_open_freezes_classification_typed_missing_and_stable_rows(
    tmp_path, monkeypatch
):
    engine = make_engine(
        tmp_path,
        monkeypatch,
        [
            ("agree", "both", b"same", b"same", ""),
            ("drift", "push-only", b"repo", b"live", ""),
            ("missing", "pull-only", None, b"", ""),
        ],
    )
    session = open_session(engine)
    view = session.view
    assert [unit.state for unit in view.observations] == [
        "directly-in-sync",
        "drifted",
        "drifted",
    ]
    assert [row.row_id for row in view.rows] == ["main:app.drift", "main:app.missing"]
    assert view.observations[2].repository == Missing()
    assert view.observations[2].live == FilePresent(b"")
    (tmp_path / "repo/packages/app/drift").write_bytes(b"new")
    (tmp_path / "live/drift").write_bytes(b"new")
    assert session.view == view
    assert view.observations[1].repository == FilePresent(b"repo")
    with pytest.raises(FrozenInstanceError):
        view.rows[0].included = False


@pytest.mark.parametrize(
    "policy,extra,source,live,state,comparison",
    [
        (
            "push-only",
            'render = "printf rendered"\n[targets.unit.compare]\nrepo = "exit 9"\nlive = "exit 9"',
            b"repo",
            b"rendered",
            "directly-in-sync",
            b"rendered",
        ),
        (
            "both",
            '[targets.unit.compare]\nrepo = "printf equal"\nlive = "printf equal"',
            b"repo",
            b"live",
            "directly-in-sync",
            b"equal",
        ),
        (
            "pull-only",
            'capture = "printf captured"',
            b"captured",
            b"live",
            "directly-in-sync",
            b"captured",
        ),
        (
            "push-only-delete",
            'render = "exit 9"',
            b"repo",
            None,
            "directly-in-sync",
            None,
        ),
        ("push-only", "", None, b"", "drifted", None),
        ("both", "", None, None, "directly-in-sync", None),
    ],
)
def test_policy_defined_comparison(
    policy, extra, source, live, state, comparison, tmp_path, monkeypatch
):
    engine = make_engine(tmp_path, monkeypatch, [("unit", policy, source, live, extra)])
    observation = open_session(engine).view.observations[0]
    assert observation.state == state
    assert observation.comparison_repository == (
        Missing() if comparison is None else FilePresent(comparison)
    )


def test_observation_failure_is_visible_and_does_not_discard_peers(
    tmp_path, monkeypatch
):
    engine = make_engine(
        tmp_path,
        monkeypatch,
        [
            ("bad", "push-only", b"source", b"live", 'render = "exit 7"'),
            ("good", "pull-only", b"repo", b"live", ""),
            ("last", "push-only", b"repo", b"live", ""),
        ],
    )
    view = open_session(engine).view
    assert [unit.identity.target_name for unit in view.observations] == [
        "bad",
        "good",
        "last",
    ]
    assert [row.kind for row in view.rows] == ["diagnostic", "drift", "drift"]
    assert view.rows[0].included is False
    assert view.rows[0].observation.diagnostics[0].code == "observation-failed"


def test_commands_are_revision_checked_immutable_and_terminal(tmp_path, monkeypatch):
    from dotman.sync_session import (
        Abort,
        CommandAccepted,
        CommandRejected,
        Execute,
        SetIncluded,
    )

    engine = make_engine(
        tmp_path, monkeypatch, [("unit", "both", b"repo", b"live", "")]
    )
    session = open_session(engine)
    original = session.view
    result = session.dispatch(
        SetIncluded(original.session_id, original.revision, "main:app.unit", False)
    )
    assert isinstance(result, CommandAccepted)
    assert not result.view.rows[0].included
    assert original.rows[0].included
    changed = session.view
    stale = session.dispatch(
        SetIncluded(original.session_id, original.revision, "main:app.unit", True)
    )
    assert isinstance(stale, CommandRejected)
    assert stale.reason == "stale"
    assert session.view is changed
    for command, reason in [
        (
            SetIncluded("other", changed.revision, "main:app.unit", True),
            "foreign-session",
        ),
        (
            SetIncluded(changed.session_id, changed.revision, "unknown", True),
            "unknown-row",
        ),
        (
            SetIncluded(changed.session_id, changed.revision, "main:app.unit", 1),
            "invalid",
        ),
        (Execute(changed.session_id, changed.revision), "preview"),
        (object(), "invalid"),
    ]:
        rejection = session.dispatch(command)
        assert isinstance(rejection, CommandRejected)
        assert rejection.reason == reason
        assert rejection.view is changed
        assert session.view is changed
    ended = session.dispatch(Abort(changed.session_id, changed.revision))
    assert isinstance(ended, CommandAccepted)
    assert ended.result.status == "aborted"
    assert ended.view.terminal
    assert ended.view.allowed_commands == ()
    rejected = session.dispatch(Abort(ended.view.session_id, ended.view.revision))
    assert isinstance(rejected, CommandRejected)
    assert rejected.reason == "terminal"


def test_diagnostic_rows_cannot_be_included_and_execute_is_honest(
    tmp_path, monkeypatch
):
    from dotman.sync_session import CommandRejected, SetIncluded

    engine = make_engine(
        tmp_path,
        monkeypatch,
        [
            ("bad", "push-only", b"repo", b"live", 'render = "exit 7"'),
            ("good", "both", b"repo", b"live", ""),
        ],
    )
    session = open_session(engine, preview=False)
    view = session.view
    rejected = session.dispatch(
        SetIncluded(view.session_id, view.revision, "main:app.bad", True)
    )
    assert isinstance(rejected, CommandRejected)
    assert rejected.reason == "disallowed"
    assert session.view is view
    result = session.execute()
    assert result.result.status == "failed"
    assert [unit.status for unit in result.result.units] == [
        "observation-failed",
        "pending",
    ]
    assert result.view.terminal
    assert (tmp_path / "live/good").read_bytes() == b"live"


def test_execute_with_drift_does_not_claim_convergence(tmp_path, monkeypatch):
    engine = make_engine(
        tmp_path, monkeypatch, [("unit", "both", b"repo", b"live", "")]
    )
    session = open_session(engine, preview=False)
    result = session.execute()
    assert result.result.status == "completed"
    assert result.result.exit_code == 0
    assert result.result.units[0].status == "pending"
    assert session.view.terminal


def test_real_sessions_share_manager_lock_preview_does_not_take_or_create_it(
    tmp_path, monkeypatch
):
    from dotman.sync_session import SessionOpenFailed

    engine = make_engine(
        tmp_path, monkeypatch, [("unit", "push-only", b"same", b"same", "")]
    )
    state_root = tmp_path / "state/dotman"
    before = {
        p.relative_to(state_root): p.read_bytes()
        for p in state_root.rglob("*")
        if p.is_file()
    }
    preview = open_session(engine)
    after = {
        p.relative_to(state_root): p.read_bytes()
        for p in state_root.rglob("*")
        if p.is_file()
    }
    assert before == after
    preview.abort()
    session = open_session(engine, preview=False)
    blocked = engine.open_sync_session(engine.resolve_sync_scope())
    assert isinstance(blocked, SessionOpenFailed)
    assert blocked.diagnostic.code == "operation-busy"
    with open_session(engine) as concurrent_preview:
        assert concurrent_preview.view.preview
    session.abort()
    with open_session(engine, preview=False) as next_session:
        assert not next_session.view.terminal
    assert next_session.view.terminal


def test_real_execute_releases_operation_lock(tmp_path, monkeypatch):
    engine = make_engine(
        tmp_path, monkeypatch, [("unit", "push-only", b"same", b"same", "")]
    )
    session = open_session(engine, preview=False)
    assert session.execute().result.status == "completed"
    with open_session(engine, preview=False):
        pass


def test_base_facts_freeze_and_real_direct_agreement_acknowledges(
    tmp_path, monkeypatch
):
    engine = make_engine(
        tmp_path, monkeypatch, [("unit", "both", b"same", b"same", "")]
    )
    with open_session(engine) as preview:
        before = preview.view.observations[0]
        assert before.base.status == "unavailable"
        assert before.base.reason == "absent"
        assert not before.base.acknowledged
        assert before.repository == FilePresent(b"same")
    with open_session(engine, preview=False) as real:
        observation = real.view.observations[0]
        assert observation.base.acknowledged
        frozen = real.view
        (tmp_path / "repo/packages/app/unit").write_bytes(b"changed")
        assert real.view == frozen
    with open_session(engine) as later:
        base = later.view.observations[0].base
        assert base.status == "usable"
        assert base.record.payload == FilePresent(b"same")


def test_real_direct_dirty_agreement_checkpoints_repository_outcome(
    tmp_path, monkeypatch
):
    engine = make_engine(
        tmp_path, monkeypatch, [("unit", "both", b"committed", b"dirty", "")]
    )
    (tmp_path / "repo/packages/app/unit").write_bytes(b"dirty")
    with open_session(engine, preview=False) as session:
        unit = session.view.observations[0]
        assert unit.state == "directly-in-sync"
        assert unit.base.acknowledged
    with open_session(engine) as later:
        record = later.view.observations[0].base.record
        assert record.payload == FilePresent(b"dirty")


def test_eligibility_cleanup_precedes_guards_but_preview_preserves_base(
    tmp_path, monkeypatch
):
    engine = make_engine(
        tmp_path, monkeypatch, [("unit", "both", b"same", b"same", "")]
    )
    with open_session(engine, preview=False):
        pass
    manifest = tmp_path / "repo/packages/app/package.toml"
    manifest.write_text(
        manifest.read_text().replace(
            'sync_policy = "both"', 'sync_policy = "push-only"'
        )
        + '\n[targets.unit.hooks]\nguard_push = "exit 100"'
    )
    engine = DotmanEngine(engine.config)
    with open_session(engine) as preview:
        assert preview.view.observations[0].diagnostics[0].code == "no-route"
    with open_session(engine, preview=False):
        pass
    manifest.write_text(
        manifest.read_text()
        .replace('sync_policy = "push-only"', 'sync_policy = "both"')
        .split("[targets.unit.hooks]")[0]
    )
    engine = DotmanEngine(engine.config)
    with open_session(engine) as later:
        assert later.view.observations[0].base.reason == "absent"


@pytest.mark.parametrize(
    "guard,policy,expected",
    [
        ("guard_pull", "both", "push-only"),
        ("guard_push", "both", "pull-only"),
        ("guard_push", "push-only", "no-route"),
        ("guard_pull", "pull-only", "no-route"),
    ],
)
def test_guards_freeze_effective_capability_without_changing_eligibility(
    guard, policy, expected, tmp_path, monkeypatch
):
    engine = make_engine(
        tmp_path,
        monkeypatch,
        [
            (
                "unit",
                policy,
                b"repo",
                b"live",
                f'[targets.unit.hooks]\n{guard} = "exit 100"',
            ),
        ],
    )
    with open_session(engine) as session:
        unit = session.view.observations[0]
        assert unit.effective_policy == expected
        assert unit.configured_policy == policy
        assert unit.base.status == (
            "not-applicable" if policy == "push-only" else "unavailable"
        )
        if expected == "no-route":
            assert unit.state == "observation-failed"
            assert session.view.rows[0].allowed_commands == ()


def test_hard_guard_failure_and_interruption_are_typed_and_release_lock(
    tmp_path, monkeypatch
):
    from dotman.sync_session import SessionOpenFailed

    engine = make_engine(
        tmp_path,
        monkeypatch,
        [
            (
                "unit",
                "both",
                b"repo",
                b"live",
                '[targets.unit.hooks]\nguard_push = "exit 7"',
            ),
        ],
    )
    failed = engine.open_sync_session(engine.resolve_sync_scope())
    assert isinstance(failed, SessionOpenFailed)
    assert failed.diagnostic.code == "planning-failed"
    manifest = tmp_path / "repo/packages/app/package.toml"
    manifest.write_text(manifest.read_text().replace("exit 7", "exit 0"))
    with open_session(DotmanEngine(engine.config), preview=False):
        pass


def test_observation_without_git_history_can_acknowledge(tmp_path, monkeypatch):
    engine = make_engine(
        tmp_path, monkeypatch, [("unit", "both", b"same", b"same", "")]
    )
    # Checkpoint evidence is independent of Git history.
    (tmp_path / "repo/.git/HEAD").write_text("ref: refs/heads/unborn\n")
    with open_session(engine, preview=False) as session:
        assert session.view.observations[0].state == "directly-in-sync"
        assert session.view.observations[0].base.acknowledged


def test_checkpoint_survives_removing_git_metadata(tmp_path, monkeypatch):
    from dotman.sync_session import PrepareProposalReview, CommandAccepted
    from tests.engine.test_sync_convergence import command

    engine = make_engine(
        tmp_path, monkeypatch, [("unit", "both", b"same", b"same", "")]
    )
    with open_session(engine, preview=False):
        pass
    (tmp_path / "repo/.git").rename(tmp_path / "git-metadata")
    (tmp_path / "repo/packages/app/unit").write_bytes(b"changed")
    with open_session(engine) as session:
        unit = session.view.observations[0]
        assert unit.base.status == "usable"
        assert unit.base.record.payload == FilePresent(b"same")
        assert not unit.diagnostics
        result = command(session, PrepareProposalReview, "main:app.unit")
        assert isinstance(result, CommandAccepted)
        assert session.view.rows[0].proposal.repository == FilePresent(b"changed")


def test_command_views_read_real_endpoints_while_evidence_stays_frozen(
    tmp_path, monkeypatch
):
    from dotman.command_runtime import CommandResult, MemoryCommandRuntime, ShellCommand
    from dotman.sync_session import (
        SessionOpened,
        SessionChanged,
        SessionFinished,
        SetIncluded,
    )

    engine = make_engine(
        tmp_path,
        monkeypatch,
        [
            (
                "unit",
                "both",
                b"repo",
                b"live",
                '[targets.unit.compare]\nrepo = "repo-view"\nlive = "live-view"',
            )
        ],
    )
    actual = engine.command_runtime
    calls = []

    def respond(request):
        runtime.queue(respond)
        if isinstance(request.command, ShellCommand):
            calls.append(request.command.source)
            assert request.env["DOTMAN_REPO_PATH"] == str(tmp_path / "repo/packages/app/unit")
            assert request.env["DOTMAN_LIVE_PATH"] == str(tmp_path / "live/unit")
            (tmp_path / "repo/packages/app/unit").write_bytes(b"changed")
            (tmp_path / "live/unit").write_bytes(b"changed")
            return CommandResult(0, stdout=request.command.source.encode())
        return actual.run(request)

    runtime = MemoryCommandRuntime([respond])
    engine = DotmanEngine(engine.config, command_runtime=runtime)
    events = []
    with open_session(engine, event_sink=events.append) as session:
        original = session.view
        session.dispatch(
            SetIncluded(original.session_id, original.revision, "main:app.unit", False)
        )
        session.abort()
        assert original.observations[0].repository == FilePresent(b"repo")
        assert original.observations[0].live == FilePresent(b"live")
    assert calls == ["repo-view", "live-view"]
    assert [type(event) for event in events] == [
        SessionOpened,
        SessionChanged,
        SessionFinished,
    ]


def test_open_interruption_is_typed_and_releases_lock(tmp_path, monkeypatch):
    from dotman.command_runtime import CommandResult, MemoryCommandRuntime, ShellCommand
    from dotman.sync_session import SessionOpenFailed

    engine = make_engine(
        tmp_path,
        monkeypatch,
        [("unit", "push-only", b"repo", b"live", 'render = "interrupt"')],
    )
    actual = engine.command_runtime

    def respond(request):
        runtime.queue(respond)
        return (
            CommandResult(130)
            if isinstance(request.command, ShellCommand)
            else actual.run(request)
        )

    runtime = MemoryCommandRuntime([respond])
    interrupted = DotmanEngine(
        engine.config, command_runtime=runtime
    ).open_sync_session(engine.resolve_sync_scope())
    assert isinstance(interrupted, SessionOpenFailed)
    assert interrupted.diagnostic.code == "interrupted"
    with open_session(engine, preview=False):
        pass


def test_open_callback_programming_error_escapes_and_releases_lock(
    tmp_path, monkeypatch
):
    engine = make_engine(
        tmp_path, monkeypatch, [("unit", "push-only", b"same", b"same", "")]
    )

    def broken_sink(_event):
        raise ValueError("broken adapter")

    with pytest.raises(ValueError, match="broken adapter"):
        engine.open_sync_session(engine.resolve_sync_scope(), event_sink=broken_sink)
    with open_session(engine, preview=False):
        pass


def test_direct_agreement_acknowledgment_failure_stays_visible(tmp_path, monkeypatch):
    from dotman.sync_base_store import SyncBaseStore, SyncBaseStoreError

    engine = make_engine(
        tmp_path, monkeypatch, [("unit", "both", b"same", b"same", "")]
    )
    def fail(*args):
        raise SyncBaseStoreError("checkpoint unavailable")
    monkeypatch.setattr(SyncBaseStore, "replace", fail)
    with open_session(engine, preview=False) as session:
        assert session.view.observations[0].state == "directly-in-sync"
        row = session.view.rows[0]
        assert row.kind == "diagnostic"
        assert row.allowed_commands == ()
        assert row.observation.diagnostics[0].code == "base-acknowledgment-failed"
        assert not row.observation.base.acknowledged
        assert row.observation.diagnostics[0].severity == "warning"
        assert session.execute().result.status == "completed"


@pytest.mark.parametrize("shape", ["symlink", "fifo", "directory"])
def test_bad_repository_endpoint_is_a_typed_visible_failure(
    shape, tmp_path, monkeypatch
):
    import os

    engine = make_engine(tmp_path, monkeypatch, [("unit", "both", None, b"live", "")])
    source = tmp_path / "repo/packages/app/unit"
    if shape == "symlink":
        source.symlink_to(tmp_path / "live/unit")
    elif shape == "fifo":
        os.mkfifo(source)
    else:
        source.mkdir()
    with open_session(engine) as session:
        unit = session.view.observations[0]
        assert unit.state == "observation-failed"
        assert session.view.rows[0].allowed_commands == ()
        assert unit.repository is None


def test_follow_dangling_live_link_is_missing_but_prompt_requires_valid_referent(
    tmp_path, monkeypatch
):
    from dataclasses import replace

    engine = make_engine(tmp_path, monkeypatch, [("unit", "both", b"repo", None, "")])
    (tmp_path / "live").mkdir()
    (tmp_path / "live/unit").symlink_to(tmp_path / "missing-referent")
    with open_session(engine) as prompt:
        assert prompt.view.observations[0].state == "observation-failed"
    with open_session(
        DotmanEngine(replace(engine.config, file_symlink_mode="follow"))
    ) as follow:
        assert follow.view.observations[0].live == Missing()
        assert follow.view.observations[0].state == "drifted"


def test_preview_rejects_unsafe_existing_store_instead_of_hiding_it(
    tmp_path, monkeypatch
):
    from dotman.sync_base_store import LOCK_FILE_NAME, SyncBaseStore

    engine = make_engine(
        tmp_path, monkeypatch, [("unit", "both", b"same", b"same", "")]
    )
    with SyncBaseStore.open(tmp_path / "state/dotman", "main") as store:
        directory = store.repo_state_directory
    (directory / LOCK_FILE_NAME).unlink()
    (directory / LOCK_FILE_NAME).symlink_to(tmp_path / "absent")
    with open_session(engine) as session:
        unit = session.view.observations[0]
        assert unit.base.status == "unavailable"
        assert unit.diagnostics[0].severity == "warning"
    assert (directory / LOCK_FILE_NAME).is_symlink()


def test_push_only_exact_live_outcome_includes_configured_file_mode(
    tmp_path, monkeypatch
):
    engine = make_engine(
        tmp_path,
        monkeypatch,
        [("unit", "push-only", b"same", b"same", 'chmod = "600"')],
    )
    (tmp_path / "live/unit").chmod(0o644)
    with open_session(engine) as session:
        unit = session.view.observations[0]
        assert unit.state == "drifted"
        assert unit.live_mode == 0o644
        assert unit.chmod == "600"


@pytest.mark.parametrize("live", [None, b"old secret"], ids=["new-live-file", "existing-0644-live-file"])
def test_push_never_exposes_content_beyond_the_configured_file_mode(tmp_path, monkeypatch, live):
    from tests.test_atomic_files import _record_modes_of_files_holding_content

    engine = make_engine(tmp_path, monkeypatch, [("unit", "push-only", b"secret", live, 'chmod = "600"')])
    live_path = tmp_path / "live/unit"
    if live is not None:
        live_path.chmod(0o644)
    original_umask = os.umask(0o022)
    try:
        observations = _record_modes_of_files_holding_content(monkeypatch)
        with engine.open_push_session(engine.resolve_sync_scope()) as session:
            result = session.execute().result
    finally:
        os.umask(original_umask)

    live_observations = [(label, mode) for label, mode in observations if str(live_path.parent) in label]
    assert result.status == "completed"
    assert live_observations, "expected the live write to be observed"
    assert [(label, oct(mode)) for label, mode in live_observations if mode & ~0o600] == []
    assert live_path.read_bytes() == b"secret"
    assert stat.S_IMODE(live_path.stat().st_mode) == 0o600


def test_unit_local_base_read_failure_preserves_unrelated_observation(
    tmp_path, monkeypatch
):
    engine = make_engine(
        tmp_path,
        monkeypatch,
        [
            ("first", "both", b"same", b"same", ""),
            ("second", "both", b"same", b"same", ""),
        ],
    )
    with open_session(engine, preview=False):
        pass
    from dotman.sync_base_store import SyncBaseStore, SyncBaseStoreError
    read = SyncBaseStore.read
    def fail_first(store, identity):
        if identity == b"main:app.first":
            raise SyncBaseStoreError("checkpoint unreadable")
        return read(store, identity)
    monkeypatch.setattr(SyncBaseStore, "read", fail_first)
    with open_session(engine) as session:
        assert [unit.state for unit in session.view.observations] == [
            "directly-in-sync",
            "directly-in-sync",
        ]
        assert session.view.observations[0].diagnostics[0].severity == "warning"
        assert session.view.observations[1].base.status == "usable"
        assert [row.row_id for row in session.view.rows] == ["main:app.first"]


def test_preview_preserves_managed_files_git_and_existing_base_bytes(
    tmp_path, monkeypatch
):
    engine = make_engine(
        tmp_path, monkeypatch, [("unit", "both", b"same", b"same", "")]
    )
    with open_session(engine, preview=False):
        pass

    def snapshot():
        return {
            path.relative_to(tmp_path): (path.read_bytes(), path.stat().st_mode)
            for path in tmp_path.rglob("*")
            if path.is_file()
        }

    before = snapshot()
    with open_session(engine) as preview:
        assert preview.view.observations[0].base.status == "usable"
        assert not preview.view.observations[0].base.acknowledged
    assert snapshot() == before


def test_preview_does_not_treat_unreadable_store_directory_as_absent(
    tmp_path, monkeypatch
):

    engine = make_engine(
        tmp_path, monkeypatch, [("unit", "both", b"same", b"same", "")]
    )
    with open_session(engine, preview=False):
        pass
    scope = engine.resolve_sync_scope()
    directory = tmp_path / "state/dotman/repos/main"
    directory.chmod(0)
    try:
        with engine.open_sync_session(scope, preview=True) as session:
            unit = session.view.observations[0]
            assert unit.base.status == "unavailable"
            assert unit.diagnostics[0].severity == "warning"
        assert directory.stat().st_mode & 0o777 == 0
    finally:
        directory.chmod(0o700)


def test_projection_diagnostics_do_not_expose_private_staging_paths(
    tmp_path, monkeypatch
):
    engine = make_engine(
        tmp_path,
        monkeypatch,
        [
            (
                "unit",
                "push-only",
                b"repo",
                b"live",
                """render = 'printf "%s" "$DOTMAN_REPO_PATH" >&2; exit 1'""",
            ),
        ],
    )
    with open_session(engine) as session:
        message = session.view.rows[0].observation.diagnostics[0].message
        assert "dotman-proposal-" not in message
        assert str(tmp_path / "repo/packages/app/unit") in message


def test_cancelled_session_does_not_poison_next_operation_on_same_engine(tmp_path, monkeypatch):
    from dotman.sync_session import SetApproval

    def approve(session, row_id, approved):
        view = session.view
        return session.dispatch(SetApproval(view.session_id, view.revision, row_id, approved))

    marker = tmp_path / "captures"
    engine = make_engine(tmp_path, monkeypatch, [
        ("unit", "pull-only", b"repo", b"live",
         f'capture = "touch {marker}; cat $DOTMAN_LIVE_PATH"\ncompare = {{ repo = "raw", live = "raw" }}'),
    ])
    with open_session(engine, preview=False) as session:
        session.request_cancel()
        approve(session, session.view.rows[0].row_id, True)
        assert session.view.rows[0].diagnostics[0].code == "interrupted"
        assert not marker.exists()
        session.abort()
        cancelled_session = session
    with open_session(engine, preview=False) as session:
        cancelled_session.request_cancel()
        approve(session, session.view.rows[0].row_id, True)
        assert session.view.rows[0].approved
        assert marker.exists()
        assert session.execute().result.exit_code == 0


@pytest.mark.parametrize('preview', [False, True])
def test_interpretation_change_keeps_drifted_base_usable(tmp_path, monkeypatch, preview):
    # A Base is the last agreed repository-side content; a later vars or
    # projection change must not discard it, or drifted units lose their Merge ancestor.
    from dotman.sync_base_store import FilePresent, SyncBaseStore

    engine = make_engine(tmp_path, monkeypatch, [('config', 'both', b'repo', b'repo', '')])
    open_session(engine, preview=False).abort()
    (tmp_path / 'repo/profiles/default.toml').write_text('[vars]\nadded = "later"\n')
    (tmp_path / 'live/config').write_bytes(b'drift')
    engine = DotmanEngine(engine.config)
    session = open_session(engine, preview=preview)
    assert session.view.observations[0].base.status == 'usable'
    session.abort()
    with SyncBaseStore.open(tmp_path / 'state/dotman', 'main', read_only=True) as store:
        assert store.read(b'main:app.config').payload == FilePresent(b'repo')


def test_policy_ineligibility_cleanup_survives_later_guard_failure(tmp_path, monkeypatch):
    from dotman.sync_base_store import SyncBaseStore
    from dotman.sync_session import SessionOpenFailed

    engine = make_engine(tmp_path, monkeypatch, [('unit', 'both', b'same', b'same', '')])
    with open_session(engine, preview=False):
        pass
    manifest = tmp_path / 'repo/packages/app/package.toml'
    manifest.write_text(manifest.read_text().replace('sync_policy = "both"', 'sync_policy = "push-only"')
                        + '\n[targets.unit.hooks]\nguard_push = "exit 1"')
    engine = DotmanEngine(engine.config)
    result = engine.open_sync_session(engine.resolve_sync_scope(), preview=False)
    assert isinstance(result, SessionOpenFailed)
    with SyncBaseStore.open(tmp_path / 'state/dotman', 'main', read_only=True) as store:
        assert store.read(b'main:app.unit') is None


@pytest.mark.parametrize('guard_scope', ['repo', 'package', 'target'])
@pytest.mark.parametrize('preview', [False, True])
@pytest.mark.parametrize('exact_child', [False, True])
def test_child_ineligibility_cleanup_precedes_ancestor_guard_failures(
    tmp_path, monkeypatch, guard_scope, preview, exact_child,
):
    from dotman.sync_base_store import SyncBaseStore
    from dotman.sync_session import SessionOpenFailed
    from tests.engine.test_sync_directory_observation import directory_engine, put

    engine = directory_engine(tmp_path, monkeypatch)
    put(tmp_path / 'repo/packages/app/tree', 'child', b'same')
    put(tmp_path / 'live/tree', 'child', b'same')
    with open_session(engine, preview=False):
        pass
    manifest = tmp_path / 'repo/packages/app/package.toml'
    content = manifest.read_text().replace('sync_policy = "both"', 'sync_policy = "push-only"')
    if guard_scope == 'package':
        content += '\n[hooks]\nguard_push = "exit 9"\n'
    elif guard_scope == 'target':
        content += '\n[targets.tree.hooks]\nguard_push = "exit 9"\n'
    else:
        repo_manifest = tmp_path / 'repo/repo.toml'
        repo_manifest.write_text(repo_manifest.read_text() + '\n[hooks]\nguard_push = "exit 9"\n')
    manifest.write_text(content)
    engine = DotmanEngine(engine.config)
    scope = engine.resolve_sync_scope(['main:app.tree/child'] if exact_child else None)
    assert isinstance(engine.open_sync_session(scope, preview=preview), SessionOpenFailed)
    with SyncBaseStore.open(tmp_path / 'state/dotman', 'main', read_only=True) as store:
        assert (store.read(b'main:app.tree/child') is not None) is preview


def test_execution_reports_step_events_in_order(tmp_path, monkeypatch):
    from dotman.execution import StepFinished, StepStarted
    from dotman.sync_session import SetApproval
    engine = make_engine(tmp_path, monkeypatch, [
        ("unit", "push-only", b"repo", b"live", '[targets.unit.hooks]\npre_push = "true"'),
    ])
    events = []
    with open_session(engine, preview=False, event_sink=events.append) as session:
        view = session.view
        session.dispatch(SetApproval(view.session_id, view.revision, "main:app.unit", True))
        session.execute()
    steps = [(type(event).__name__, event.index, event.total,
              (event.step if isinstance(event, StepStarted) else event.result.step).action,
              event.result.status if isinstance(event, StepFinished) else None)
             for event in events if isinstance(event, (StepStarted, StepFinished))]
    assert steps == [
        ("StepStarted", 1, 2, "pre_push", None), ("StepFinished", 1, 2, "pre_push", "ok"),
        ("StepStarted", 2, 2, "write", None), ("StepFinished", 2, 2, "write", "ok"),
    ]
