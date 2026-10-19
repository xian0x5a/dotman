import pytest

from dotman.sync_base_store import FilePresent
from dotman.sync_session import EditProposal, PrepareProposalReview, SetApproval, SetResolutionIntent, RetryMaterialization
from tests.engine.test_sync_convergence import command
from tests.engine.test_sync_session import make_engine, open_session

BASE = b"first\nmiddle\nlast\n"
REPO = b"repository\nmiddle\nlast\n"
LIVE = b"first\nmiddle\nlive\n"
MERGED = b"repository\nmiddle\nlive\n"


def established(tmp_path, monkeypatch, extra=""):
    engine = make_engine(tmp_path, monkeypatch, [("unit", "both", BASE, BASE, extra)])
    with open_session(engine, preview=False):
        pass
    (tmp_path / "repo/packages/app/unit").write_bytes(REPO)
    (tmp_path / "live/unit").write_bytes(LIVE)
    return engine


def test_merge_default_is_lazy_and_executes_frozen_three_way_outcome(tmp_path, monkeypatch):
    marker = tmp_path / "captures"
    engine = established(tmp_path, monkeypatch,
        f'capture = "echo capture >> {marker}; cat $DOTMAN_LIVE_PATH"\ncompare = {{ repo = "raw", live = "raw" }}')
    with open_session(engine, preview=False) as session:
        row = session.view.rows[0]
        assert row.intent == "merge"
        assert set(row.allowed_intents) == {"merge", "use-live", "use-repository"}
        assert row.fallback_reason is None and row.proposal is None
        assert not marker.exists()
        # Merge inputs come from frozen repository evidence, not later disk state.
        (tmp_path / "repo/packages/app/unit").write_bytes(b"external")
        command(session, PrepareProposalReview, row.row_id)
        assert not session.view.rows[0].approved
        command(session, SetApproval, row.row_id, True)
        proposal = session.view.rows[0].proposal
        assert proposal.repository == FilePresent(MERGED)
        assert proposal.live == FilePresent(MERGED)
        assert proposal.capture == FilePresent(LIVE)
        assert proposal.primary_source_change == FilePresent(MERGED)
        assert proposal.publication_effects[0].content == MERGED
        assert session.execute().result.units[0].status == "converged"
    assert marker.read_text().splitlines() == ["capture"]
    assert (tmp_path / "repo/packages/app/unit").read_bytes() == MERGED
    assert (tmp_path / "live/unit").read_bytes() == MERGED
    with open_session(engine) as session:
        assert session.view.observations[0].base.record.payload == FilePresent(MERGED)


@pytest.mark.parametrize("repo,live,expected", [(b"repo", b"live", "use-repository"), (b"repo", None, "use-repository"), (None, b"live", "use-live")])
def test_without_base_repository_fallback_and_disallowed_merge_are_visible(tmp_path, monkeypatch, repo, live, expected):
    # No-Base default keeps whichever side still has the file, else the repository.
    engine = make_engine(tmp_path, monkeypatch, [("unit", "both", repo, live, "")])
    with open_session(engine) as session:
        row = session.view.rows[0]
        assert row.intent == expected and row.fallback_reason == "absent"
        assert set(row.allowed_intents) == {"use-live", "use-repository"}
        before = session.view
        assert command(session, SetResolutionIntent, row.row_id, "merge").reason == "disallowed"
        assert session.view == before


def test_explicit_resolution_choice_ends_the_guess_but_keeps_the_base_reason(tmp_path, monkeypatch):
    engine = make_engine(tmp_path, monkeypatch, [("unit", "both", b"repo", b"live", "")])
    with open_session(engine) as session:
        assert session.view.rows[0].resolution_guessed
        # Approval accepts the guess; it does not choose a side.
        command(session, SetApproval, "main:app.unit", True)
        assert session.view.rows[0].resolution_guessed
        # Choosing back the guessed side is still a decision, and nothing restores the guess.
        command(session, SetResolutionIntent, "main:app.unit", "use-live")
        command(session, SetResolutionIntent, "main:app.unit", "use-repository")
        row = session.view.rows[0]
        assert not row.resolution_guessed and row.fallback_reason == "absent"


def test_edited_outcome_ends_the_guess(tmp_path, monkeypatch):
    engine = make_engine(tmp_path, monkeypatch, [
        ("unit", "both", b"repo", b"live", 'editor = { run = "printf edited > \\"$DOTMAN_SOURCE\\"", io = "pipe" }'),
    ])
    with open_session(engine) as session:
        command(session, EditProposal, "main:app.unit")
        row = session.view.rows[0]
        assert row.proposal.intent == "editor" and not row.resolution_guessed


@pytest.mark.parametrize("intent,expected", [("use-repository", REPO), ("use-live", LIVE)])
def test_intent_effects_and_capture_cache(tmp_path, monkeypatch, intent, expected):
    marker = tmp_path / "captures"
    engine = established(tmp_path, monkeypatch,
        f'capture = "echo capture >> {marker}; cat $DOTMAN_LIVE_PATH"\ncompare = {{ repo = "raw", live = "raw" }}')
    with open_session(engine, preview=False) as session:
        command(session, SetResolutionIntent, "main:app.unit", intent)
        assert not marker.exists()
        command(session, SetApproval, "main:app.unit", True)
        row = session.view.rows[0]
        assert row.proposal.repository == FilePresent(expected)
        assert (row.proposal.primary_source_change is None) == (intent == "use-repository")
        assert bool(row.proposal.publication_effects) == (intent == "use-repository")
        assert session.execute().result.units[0].status == "converged"
    assert marker.exists() == (intent == "use-live")


def test_conflict_is_typed_retryable_and_does_not_change_intent(tmp_path, monkeypatch):
    engine = established(tmp_path, monkeypatch)
    (tmp_path / "live/unit").write_bytes(b"conflict\nmiddle\nlast\n")
    with open_session(engine) as session:
        command(session, SetApproval, "main:app.unit", True)
        row = session.view.rows[0]
        assert row.intent == "merge" and row.proposal is None and not row.approved
        assert row.diagnostics[0].code == "reconciliation-conflict"
        command(session, RetryMaterialization, row.row_id)
        assert session.view.rows[0].diagnostics[0].code == "reconciliation-conflict"
        command(session, SetResolutionIntent, row.row_id, "use-live")
        assert session.view.rows[0].proposal is None
        command(session, SetApproval, row.row_id, True)
        assert session.view.rows[0].approved


def test_standing_approval_rematerializes_intent_without_recapture(tmp_path, monkeypatch):
    marker = tmp_path / "captures"
    engine = established(tmp_path, monkeypatch,
        f'capture = "echo capture >> {marker}; cat $DOTMAN_LIVE_PATH"\ncompare = {{ repo = "raw", live = "raw" }}')
    with open_session(engine) as session:
        command(session, SetApproval, "main:app.unit", True)
        for intent in ("use-live", "use-repository", "merge"):
            command(session, SetResolutionIntent, "main:app.unit", intent)
            row = session.view.rows[0]
            assert row.approved and row.proposal.intent == intent
        assert marker.read_text().splitlines() == ["capture"]


def test_use_live_checkpoints_final_repository_after_publication(tmp_path, monkeypatch):
    from dotman.sync_base_lifecycle import SyncBaseLifecycle
    extra = 'capture = "sed s/live/repository/ $DOTMAN_LIVE_PATH"\nrender = "sed s/repository/published/ $DOTMAN_SOURCE"\ncompare = { repo = "raw", live = "raw" }'
    engine = make_engine(tmp_path, monkeypatch, [("unit", "both", b"old", b"live", extra)])
    payloads = []
    complete = SyncBaseLifecycle.complete
    def record_fact(self, frozen, proposal):
        payloads.append(frozen.payload)
        assert (tmp_path / "repo/packages/app/unit").read_bytes() == b"repository"
        assert (tmp_path / "live/unit").read_bytes() == b"published"
        return complete(self, frozen, proposal)
    monkeypatch.setattr(SyncBaseLifecycle, "complete", record_fact)
    with open_session(engine, preview=False) as session:
        command(session, SetResolutionIntent, "main:app.unit", "use-live")
        command(session, SetApproval, "main:app.unit", True)
        proposal = session.view.rows[0].proposal
        assert proposal.repository == FilePresent(b"repository")
        assert proposal.live == FilePresent(b"published")
        result = session.execute().result
        assert result.units[0].status == "converged"
        assert [step.stage for step in result.steps] == ["repository-apply", "live-publication"]
    assert payloads == [FilePresent(b"repository")]


@pytest.mark.parametrize("intent", ["merge", "use-repository"])
def test_acknowledgment_occurs_after_own_effects_with_final_repository(tmp_path, monkeypatch, intent):
    from dotman.sync_base_lifecycle import SyncBaseLifecycle
    engine = established(tmp_path, monkeypatch)
    payloads = []
    complete = SyncBaseLifecycle.complete
    def record_fact(self, frozen, proposal):
        payloads.append(frozen.payload)
        expected = MERGED if intent == "merge" else REPO
        assert (tmp_path / "live/unit").read_bytes() == expected
        assert (tmp_path / "repo/packages/app/unit").read_bytes() == expected
        return complete(self, frozen, proposal)
    monkeypatch.setattr(SyncBaseLifecycle, "complete", record_fact)
    with open_session(engine, preview=False) as session:
        command(session, SetResolutionIntent, "main:app.unit", intent)
        command(session, SetApproval, "main:app.unit", True)
        assert session.execute().result.units[0].status == "converged"
    assert payloads == [FilePresent(MERGED if intent == "merge" else REPO)]


def test_publication_failure_keeps_previous_base_after_repository_apply(tmp_path, monkeypatch):
    from dotman import file_access
    from dotman.sync_base_lifecycle import SyncBaseLifecycle
    engine = established(tmp_path, monkeypatch)
    write = file_access.write_bytes_atomic
    def fail_live(path, content, **kwargs):
        if path == tmp_path / "live/unit":
            raise OSError("live unavailable")
        return write(path, content, **kwargs)
    monkeypatch.setattr(file_access, "write_bytes_atomic", fail_live)
    def forbidden(*args, **kwargs):
        pytest.fail("Failed unit must not acknowledge")
    monkeypatch.setattr(SyncBaseLifecycle, "complete", forbidden)
    with open_session(engine, preview=False) as session:
        before = session.view.observations[0].base.record
        command(session, SetApproval, "main:app.unit", True)
        result = session.execute().result
        assert result.units[0].status == "execution-failed"
    assert (tmp_path / "repo/packages/app/unit").read_bytes() == MERGED
    assert (tmp_path / "live/unit").read_bytes() == LIVE
    with open_session(engine) as later:
        assert later.view.observations[0].base.record == before


def test_capture_failure_is_typed_and_retry_merges_current_live(tmp_path, monkeypatch):
    ready = tmp_path / "ready"
    engine = established(tmp_path, monkeypatch,
        f'capture = "test -f {ready} || exit 8; cat $DOTMAN_LIVE_PATH"\ncompare = {{ repo = "raw", live = "raw" }}')
    with open_session(engine) as session:
        command(session, SetApproval, "main:app.unit", True)
        row = session.view.rows[0]
        assert row.intent == "merge" and not row.approved and row.proposal is None
        assert row.diagnostics[0].code == "capture-failed"
        ready.touch()
        (tmp_path / "live/unit").write_bytes(b"external")
        command(session, RetryMaterialization, row.row_id)
        row = session.view.rows[0]
        # The retried Capture sees the live edit, so Merge reports it instead of
        # overwriting it with a result built from stale live bytes.
        assert row.intent == "merge" and not row.approved and row.proposal is None
        assert row.diagnostics[0].code == "reconciliation-conflict"


def test_reconciliation_provider_failure_is_typed_and_retry_reuses_capture(tmp_path, monkeypatch):
    from dotman.command_runtime import ArgvCommand, CommandResult
    marker = tmp_path / "captures"
    engine = established(tmp_path, monkeypatch,
        f'capture = "echo capture >> {marker}; cat $DOTMAN_LIVE_PATH"\ncompare = {{ repo = "raw", live = "raw" }}')
    with open_session(engine) as session:
        runtime = session._context.projection.command_runtime
        run = runtime.run
        def failing_merge(request):
            if isinstance(request.command, ArgvCommand) and request.command.arguments[:2] == ("git", "merge-file"):
                return CommandResult(255, stderr=b"provider failed")
            return run(request)
        monkeypatch.setattr(runtime, "run", failing_merge)
        command(session, SetApproval, "main:app.unit", True)
        row = session.view.rows[0]
        assert row.intent == "merge" and not row.approved
        assert row.diagnostics[0].code == "reconciliation-failed"
        monkeypatch.setattr(runtime, "run", run)
        command(session, RetryMaterialization, row.row_id)
        assert session.view.rows[0].proposal.repository == FilePresent(MERGED)
        assert marker.read_text().splitlines() == ["capture"]


def test_failed_standing_intent_change_clears_only_that_approval(tmp_path, monkeypatch):
    engine = established(tmp_path, monkeypatch)
    (tmp_path / "live/unit").write_bytes(b"conflict\nmiddle\nlast\n")
    with open_session(engine) as session:
        command(session, SetResolutionIntent, "main:app.unit", "use-repository")
        command(session, SetApproval, "main:app.unit", True)
        command(session, SetResolutionIntent, "main:app.unit", "merge")
        row = session.view.rows[0]
        assert row.intent == "merge" and not row.approved and row.proposal is None
        assert row.diagnostics[0].code == "reconciliation-conflict"


def test_use_repository_reuses_frozen_render_comparison(tmp_path, monkeypatch):
    marker = tmp_path / "renders"
    engine = make_engine(tmp_path, monkeypatch, [
        ("unit", "both", b"repository", b"live",
         f'render = "echo render >> {marker}; cat $DOTMAN_SOURCE"\ncompare = {{ repo = "render", live = "raw" }}'),
    ])
    with open_session(engine) as session:
        assert marker.read_text().splitlines() == ["render"]
        command(session, SetResolutionIntent, "main:app.unit", "use-repository")
        command(session, SetApproval, "main:app.unit", True)
        assert session.view.rows[0].proposal.live == FilePresent(b"repository")
        assert marker.read_text().splitlines() == ["render"]


def test_failed_acknowledgment_keeps_previous_base_after_both_effects(tmp_path, monkeypatch):
    from dotman.sync_base_store import SyncBaseStore, SyncBaseStoreError
    engine = established(tmp_path, monkeypatch)
    with open_session(engine, preview=False) as session:
        before = session.view.observations[0].base.record
        command(session, SetApproval, "main:app.unit", True)
        def fail(*args, **kwargs):
            raise SyncBaseStoreError("ack unavailable")
        with monkeypatch.context() as patch:
            patch.setattr(SyncBaseStore, "replace", fail)
            result = session.execute().result
        assert result.status == "completed"
        assert result.units[0].status == "converged"
        assert not result.units[0].acknowledged
        assert result.units[0].diagnostics[0].code == "base-acknowledgment-failed"
        assert result.units[0].diagnostics[0].severity == "warning"
    assert (tmp_path / "repo/packages/app/unit").read_bytes() == MERGED
    assert (tmp_path / "live/unit").read_bytes() == MERGED
    with open_session(engine) as later:
        assert later.view.observations[0].base.record == before


def test_earlier_published_base_commits_before_later_publication_failure(tmp_path, monkeypatch):
    from dotman import file_access
    engine = make_engine(tmp_path, monkeypatch, [
        (name, "both", BASE, BASE, "") for name in ("first", "second")
    ])
    with open_session(engine, preview=False):
        pass
    for name in ("first", "second"):
        (tmp_path / f"repo/packages/app/{name}").write_bytes(REPO)
        (tmp_path / f"live/{name}").write_bytes(LIVE)
    write = file_access.write_bytes_atomic
    def fail_second(path, content, **kwargs):
        if path == tmp_path / "live/second":
            raise OSError("second failed")
        return write(path, content, **kwargs)
    monkeypatch.setattr(file_access, "write_bytes_atomic", fail_second)
    with open_session(engine, preview=False) as session:
        before = [unit.base.record for unit in session.view.observations]
        for row in session.view.rows:
            command(session, SetApproval, row.row_id, True)
        result = session.execute().result
        assert [unit.status for unit in result.units] == ["converged", "execution-failed"]
    with open_session(engine) as later:
        after = [unit.base.record for unit in later.view.observations]
        assert after[0].payload == FilePresent(MERGED)
        assert before[0].payload == FilePresent(BASE)
        assert after[1] == before[1]


ZDIFF3_CONFLICT = (b"<<<<<<< repository\nrepository\n||||||| Sync Base\nfirst\n=======\nconflict\n"
                   b">>>>>>> Capture\nmiddle\nlast\n")


def test_conflict_keeps_zdiff3_merge_output_as_evidence(tmp_path, monkeypatch):
    engine = established(tmp_path, monkeypatch)
    (tmp_path / "live/unit").write_bytes(b"conflict\nmiddle\nlast\n")
    with open_session(engine) as session:
        command(session, SetApproval, "main:app.unit", True)
        diagnostic = session.view.rows[0].diagnostics[0]
        assert diagnostic.code == "reconciliation-conflict"
        assert diagnostic.conflict == FilePresent(ZDIFF3_CONFLICT)
        assert diagnostic.capture == FilePresent(b"conflict\nmiddle\nlast\n")


def test_editor_starts_from_conflict_output(tmp_path, monkeypatch):
    seen = tmp_path / "seen"
    engine = established(tmp_path, monkeypatch,
        f'editor = {{ run = "cp \\"$DOTMAN_SOURCE\\" {seen}; printf resolved > \\"$DOTMAN_SOURCE\\"", io = "pipe" }}')
    (tmp_path / "live/unit").write_bytes(b"conflict\nmiddle\nlast\n")
    with open_session(engine) as session:
        command(session, SetApproval, "main:app.unit", True)
        assert session.view.rows[0].diagnostics[0].code == "reconciliation-conflict"
        command(session, EditProposal, "main:app.unit")
        assert seen.read_bytes() == ZDIFF3_CONFLICT
        row = session.view.rows[0]
        assert row.diagnostics == () and row.proposal.repository == FilePresent(b"resolved")


def test_saved_edit_with_unresolved_conflict_stays_blocked_without_reopening(tmp_path, monkeypatch):
    runs = tmp_path / "editor-runs"
    engine = established(tmp_path, monkeypatch,
        f'editor = {{ run = "echo run >> {runs}; printf \'kept\\\\n\' >> \\"$DOTMAN_SOURCE\\"", io = "pipe" }}')
    (tmp_path / "live/unit").write_bytes(b"conflict\nmiddle\nlast\n")
    with open_session(engine) as session:
        command(session, SetApproval, "main:app.unit", True)
        result = command(session, EditProposal, "main:app.unit")
        row = session.view.rows[0]
        assert result.result.status == "unresolved-conflict"
        assert not row.approved and row.proposal is None
        # The saved text becomes the evidence, so the next Editor run resumes from it.
        assert row.diagnostics[0].code == "reconciliation-conflict"
        assert row.diagnostics[0].conflict == FilePresent(ZDIFF3_CONFLICT + b"kept\n")
        assert runs.read_text().splitlines() == ["run"]
        # Approval rechecks the saved edit instead of re-merging over it.
        command(session, SetApproval, "main:app.unit", True)
        row = session.view.rows[0]
        assert not row.approved and row.diagnostics[0].conflict == FilePresent(ZDIFF3_CONFLICT + b"kept\n")


@pytest.mark.parametrize("source", [
    # Present in the frozen repository source, so intentional.
    b"<<<<<<< repository\nx\n||||||| Sync Base\ny\n=======\nz\n>>>>>>> Capture\n",
    # Not dotman's labels.
    b"<<<<<<< mine\nx\n=======\nz\n>>>>>>> theirs\n",
])
def test_saved_edit_keeps_intentional_conflict_markers(tmp_path, monkeypatch, source):
    engine = make_engine(tmp_path, monkeypatch, [("unit", "push-only", source, b"live",
                                                  'editor = { run = "printf kept >> \\"$DOTMAN_SOURCE\\"", io = "pipe" }')])
    with open_session(engine) as session:
        command(session, SetApproval, "main:app.unit", True)
        result = command(session, EditProposal, "main:app.unit")
        assert result.result.status == "saved"
        assert session.view.rows[0].approved


@pytest.mark.parametrize("live,intent,expected", [
    (LIVE, "use-live", LIVE),
    (LIVE, "merge", MERGED),
    (LIVE, "use-repository", REPO),
    (b"conflict\nmiddle\nlast\n", "merge", ZDIFF3_CONFLICT),
    # A presence conflict has no merge text, so the frozen repository source is the start.
    (None, "merge", REPO),
])
def test_editor_opens_the_selected_resolution_before_review(tmp_path, monkeypatch, live, intent, expected):
    seen = tmp_path / "seen"
    engine = established(tmp_path, monkeypatch,
        f'editor = {{ run = "cp \\"$DOTMAN_SOURCE\\" {seen}", io = "pipe" }}')
    if live is None:
        (tmp_path / "live/unit").unlink()
    else:
        (tmp_path / "live/unit").write_bytes(live)
    with open_session(engine) as session:
        command(session, SetResolutionIntent, "main:app.unit", intent)
        assert session.view.rows[0].proposal is None
        command(session, EditProposal, "main:app.unit")
        assert seen.read_bytes() == expected


def test_use_repository_reapplies_hooks_without_rewriting_when_capture_sees_other_live_state(tmp_path, monkeypatch):
    # Like a settings dump: the live file is only what a push hook applies, and
    # Capture reads the applied state, so equal file bytes can hide real drift.
    applied = tmp_path / "applied"
    applied.write_bytes(b"user-set\n")
    engine = make_engine(tmp_path, monkeypatch, [
        ("unit", "both", b"reset\n", b"reset\n",
         f'capture = "cat {applied}"\n[targets.unit.hooks]\npost_push = "cp $DOTMAN_LIVE_PATH {applied}"'),
    ])
    live = tmp_path / "live/unit"
    inode = live.stat().st_ino
    with open_session(engine, preview=False) as session:
        row = session.view.rows[0]
        assert row.intent == "use-repository"
        command(session, SetApproval, row.row_id, True)
        proposal = session.view.rows[0].proposal
        assert proposal.capture == FilePresent(b"user-set\n")
        assert [(effect.kind, effect.path) for effect in proposal.publication_effects] == [("reapply", live)]
        assert session.execute().result.units[0].status == "converged"
    # Reapplying runs hooks only: no rewrite and no snapshot of an unchanged file.
    assert live.stat().st_ino == inode
    assert not any(path.is_file() for path in tmp_path.rglob("snapshots/**/*"))
    assert applied.read_bytes() == b"reset\n"
    with open_session(engine) as session:
        assert not session.view.rows


def test_capture_drift_without_push_hooks_writes_nothing(tmp_path, monkeypatch):
    # Nothing could reapply the captured state, so promising a reapply would be false.
    applied = tmp_path / "applied"
    applied.write_bytes(b"user-set\n")
    engine = make_engine(tmp_path, monkeypatch, [("unit", "both", b"reset\n", b"reset\n", f'capture = "cat {applied}"')])
    with open_session(engine, preview=False) as session:
        command(session, SetApproval, "main:app.unit", True)
        proposal = session.view.rows[0].proposal
        assert proposal.primary_source_change is None and proposal.publication_effects == ()


def test_add_add_since_missing_base_merges_against_empty_ancestor(tmp_path, monkeypatch):
    # A Base recorded Missing; both sides then created different files (add/add).
    engine = make_engine(tmp_path, monkeypatch, [("unit", "both", None, None, "")])
    with open_session(engine, preview=False):
        pass
    (tmp_path / "repo/packages/app/unit").write_bytes(b"shared\nrepository\n")
    (tmp_path / "live").mkdir(exist_ok=True)
    (tmp_path / "live/unit").write_bytes(b"shared\nlive\n")
    with open_session(engine) as session:
        assert session.view.rows[0].intent == "merge"
        command(session, SetApproval, "main:app.unit", True)
        diagnostic = session.view.rows[0].diagnostics[0]
        assert diagnostic.code == "reconciliation-conflict"
        assert diagnostic.conflict == FilePresent(
            b"shared\n<<<<<<< repository\nrepository\n||||||| Sync Base\n=======\nlive\n>>>>>>> Capture\n")
