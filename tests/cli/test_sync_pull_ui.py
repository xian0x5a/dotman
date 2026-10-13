import os
from dataclasses import replace
from types import SimpleNamespace

import pytest

from dotman.cli_style import render_sync_term
from dotman.sync_base_store import FilePresent, Missing
from dotman.sync_deck import CommandDeck
from dotman.sync_deck_command import sync_document
from dotman.sync_session import Proposal
from tests.engine.test_sync_session import make_engine


@pytest.mark.parametrize("outcome,kind", [(FilePresent(b"captured\n"), "write"), (Missing(), "delete"), (None, None)])
def test_pull_review_and_document_show_repository_effect(tmp_path, monkeypatch, outcome, kind):
    engine = make_engine(tmp_path, monkeypatch, [("unit", "push-only", b"repo\n", b"live\n", "")])
    with engine.open_sync_session(engine.resolve_sync_scope([]), preview=True) as opened:
        row = opened.view.rows[0]
        observation = replace(row.observation, configured_policy="pull-only", effective_policy="pull-only")
        proposal = Proposal(
            repository=outcome if outcome is not None else observation.repository,
            live=observation.live, primary_source_change=outcome,
            publication_effects=(), intent="use-live",
            capture=outcome if outcome is not None else observation.repository,
            reconciliation="replacement",
        )
        row = replace(row, observation=observation, allowed_intents=("use-live",), intent="use-live", proposal=proposal, approved=True)
        session = SimpleNamespace(view=replace(opened.view, observations=(observation,), rows=(row,)))
        deck = CommandDeck(session, use_color=False)
        review = deck.review_text()
        assert "Resolution: Use live" in review
        assert f"Capture: {'missing' if isinstance(outcome, Missing) else 'present'}" in review
        assert "Reconciliation: replacement" in review
        assert f"Primary Source Change: {kind or 'none'}" in review
        assert "Live remains unchanged" in review
        # Drift is shown only when the outcome previews cannot explain the drifted row.
        assert (":: Drift" in review) == (kind is None)
        if kind == "write":
            assert "-repo" in review and "+captured" in review
        elif kind == "delete":
            assert "+++ /dev/null" in review
        else:
            assert "Nothing will be written; Approval records the Sync Base" in review
        # Approval status lives in Decision; the Publication Effects section only lists effects.
        assert "Approval still required" not in review
        deck.confirming = True
        assert f"repos: {int(kind is not None)} · live: 0" in deck.confirmation_text()
        document = sync_document(SimpleNamespace(dry_run=True, scopes=[]), session, None)
        unit = document["sync_units"][0]
        assert unit["resolution_intent"] == "use-live"
        assert unit["effects"] == []
        assert document["summary"]["repository_changes"] == int(kind is not None)
        assert (unit["primary_source_change"]["kind"] if kind else unit["primary_source_change"]) == kind
        assert "captured" not in str(document)


def test_use_live_uses_shared_resolution_style():
    assert render_sync_term("Use live", use_color=True) == render_sync_term("Use repository", use_color=True).replace("Use repository", "Use live")


@pytest.mark.parametrize("dry_run", [True, False])
def test_mixed_cli_reports_repository_and_live_effects(tmp_path, monkeypatch, capsys, dry_run):
    import json
    from tests.cli.test_sync_deck_command import arguments, runner_for

    engine = make_engine(tmp_path, monkeypatch, [
        ("push", "push-only", b"push-repo", b"push-live", ""),
        ("pull", "pull-only", b"pull-repo", b"pull-live", ""),
    ])
    assert runner_for(engine).run(arguments(dry_run=dry_run)) == 0
    document = json.loads(capsys.readouterr().out)
    units = {unit["identity"]: unit for unit in document["sync_units"]}
    push = units["main:app.push"]
    pull = units["main:app.pull"]
    assert document["summary"]["repository_changes"] == 1
    assert document["summary"]["live_writes"] == 1
    assert document["summary"]["live_deletions"] == 0
    assert push["resolution_intent"] == "use-repository"
    assert push["primary_source_change"] is None
    assert [effect["kind"] for effect in push["effects"]] == ["write"]
    assert pull["resolution_intent"] == "use-live"
    assert pull["primary_source_change"] == {
        "kind": "write", "path": str(tmp_path / "repo/packages/app/pull"),
        "bytes": len(b"pull-live"),
    }
    assert pull["effects"] == []
    assert pull["base"] == {
        "status": "unavailable",
        "qualified": True, "acknowledged": not dry_run,
    }
    assert push["base"]["acknowledged"] is False
    assert {unit["result"] for unit in units.values()} == {
        "would-converge" if dry_run else "converged"
    }
    assert (tmp_path / "repo/packages/app/pull").read_bytes() == (b"pull-repo" if dry_run else b"pull-live")
    assert (tmp_path / "live/pull").read_bytes() == b"pull-live"
    assert (tmp_path / "repo/packages/app/push").read_bytes() == b"push-repo"
    assert (tmp_path / "live/push").read_bytes() == (b"push-live" if dry_run else b"push-repo")
    if dry_run:
        assert document["stages"] == []
    else:
        stages = [step["stage"] for step in document["stages"]]
        assert "repository-apply" in stages and "live-publication" in stages
        assert stages.index("repository-apply") < stages.index("live-publication")


@pytest.mark.parametrize('projected', [False, True])
def test_no_write_review_explains_drift_separately_from_repository_effect(tmp_path, monkeypatch, projected):
    marker = tmp_path / 'capture-count'
    comparison = ('{ repo = "printf compared-repo", live = "printf compared-live" }'
                  if projected else '{ repo = "raw", live = "raw" }')
    engine = make_engine(tmp_path, monkeypatch, [
        ('unit', 'pull-only', b'repo', b'live-drift',
         f'capture = "echo capture >> {marker}; printf repo"\ncompare = {comparison}'),
    ])
    with engine.open_sync_session(engine.resolve_sync_scope([]), preview=True) as session:
        deck = CommandDeck(session, use_color=False)
        assert not marker.exists()
        (tmp_path / 'repo/packages/app/unit').write_bytes(b'external-repo')
        (tmp_path / 'live/unit').write_bytes(b'external-live')
        deck.open_review()
        assert not session.view.rows[0].approved
        assert session.view.rows[0].proposal.primary_source_change is None
        text = deck.review_text()
        outcome, evidence = text.split(':: Drift', 1)
        assert 'Nothing to do: Approval would neither write nor record a Sync Base' in evidence
        assert '--- compared repository' in evidence
        assert '+++ compared live' in evidence
        assert ('-compared-repo' if projected else '-repo') in evidence
        assert ('+compared-live' if projected else '+live-drift') in evidence
        assert 'No content difference' in outcome
        assert 'Primary Source Change: none' in text
        assert 'Live remains unchanged' in text
        assert 'external-' not in text
        deck.select(True)
        assert not session.view.rows[0].approved
        assert deck.notice.startswith('Nothing to do')
        assert deck.review_text() == text
        assert marker.read_text().splitlines() == ['capture']


@pytest.mark.parametrize('comparison,repo_fact,live_fact', [
    ('{ repo = "raw", live = { run = "render" } }', 'raw', '{ run = "render" }'),
    ('{ repo = { run = "render" }, live = "raw" }', '{ run = "render" }', 'raw'),
])
def test_drift_shows_forced_compare_view_as_written(tmp_path, monkeypatch, comparison, repo_fact, live_fact):
    # A `{ run = "<builtin>" }` view is stored with an internal marker; Drift must show the manifest form.
    # The forced view runs a literal `render` command, so put one on PATH.
    bin_directory = tmp_path / 'bin'
    bin_directory.mkdir()
    (bin_directory / 'render').write_text('#!/bin/sh\ncat\n')
    (bin_directory / 'render').chmod(0o755)
    monkeypatch.setenv('PATH', f'{bin_directory}{os.pathsep}{os.environ["PATH"]}')
    engine = make_engine(tmp_path, monkeypatch, [
        ('unit', 'pull-only', b'repo', b'live-drift', f'capture = "printf repo"\ncompare = {comparison}'),
    ])
    with engine.open_sync_session(engine.resolve_sync_scope([]), preview=True) as session:
        text = CommandDeck(session, use_color=False).review_text()
    evidence = text.split(':: Drift', 1)[1]
    assert f'Repository comparison: {repo_fact}' in evidence
    assert f'Live comparison: {live_fact}' in evidence
    assert '__dotman_command__' not in text


def test_merge_review_reports_capture_reconciliation_and_both_outcomes(tmp_path, monkeypatch):
    engine = make_engine(tmp_path, monkeypatch, [('unit', 'both', b'repo', b'live', '')])
    with engine.open_sync_session(engine.resolve_sync_scope([]), preview=True) as opened:
        row = opened.view.rows[0]
        proposal = Proposal(
            repository=FilePresent(b'merged'), live=FilePresent(b'rendered'),
            primary_source_change=FilePresent(b'merged'), publication_effects=(),
            intent='merge', capture=FilePresent(b'captured'), reconciliation='three-way merge',
        )
        row = replace(row, intent='merge', proposal=proposal, fallback_reason=None)
        session = SimpleNamespace(view=replace(opened.view, rows=(row,)))
        review = CommandDeck(session, use_color=False).review_text()
        assert 'Resolution: Merge' in review
        assert 'Capture: present' in review and 'captured' not in review
        assert 'Reconciliation: three-way merge' in review
        assert '+merged' in review
        assert '+rendered' in review
        assert 'Live remains unchanged' not in review
        unit = sync_document(SimpleNamespace(dry_run=True, scopes=[]), session, None)['sync_units'][0]
        assert unit['resolution_intent'] == 'merge'
        assert unit['capture'] == 'present'
        assert unit['reconciliation'] == 'three-way merge'
        assert unit['fallback_reason'] is None
        assert 'captured' not in str(unit)


def test_conflict_review_shows_zdiff3_blocks_colored_by_side(tmp_path, monkeypatch):
    from dotman.sync_session import ConflictDiagnostic
    engine = make_engine(tmp_path, monkeypatch, [('unit', 'both', b'repo', b'live', '')])
    context = b''.join(b'line %d\n' % index for index in range(10))
    conflict = FilePresent(context + b'<<<<<<< repository\nours\n||||||| Sync Base\nbase\n=======\n'
                           b'theirs\n>>>>>>> Capture\n' + context)
    with engine.open_sync_session(engine.resolve_sync_scope([]), preview=True) as opened:
        row = replace(opened.view.rows[0], intent='merge', diagnostics=(ConflictDiagnostic(
            'reconciliation-conflict', 'Repository and Capture contain conflicting changes',
            conflict=conflict, capture=FilePresent(b'theirs\n')),))
        session = SimpleNamespace(view=replace(opened.view, rows=(row,)))
        plain = CommandDeck(session, use_color=False).review_text()
        section = plain.split(':: Merge conflicts', 1)[1]
        # The Capture ran and the merge conflicted; State must not report them as pending.
        assert 'Capture: present' in plain and 'Reconciliation: conflict' in plain
        # Three context lines around each block; farther lines are elided.
        assert 'line 6' not in section and 'line 7' in section and 'line 2' in section and 'line 3' not in section
        assert '<<<<<<< repository' in section and '>>>>>>> Capture' in section
        colored = CommandDeck(session, use_color=True).review_text()
        assert '\x1b[32mours' in colored and '\x1b[2mbase' in colored and '\x1b[34mtheirs' in colored


# Raw comparison drifts (a vs A), but Capture folds live back to the repository: nothing is written.
FOLDING_CAPTURE = 'capture = "tr A-Z a-z < $DOTMAN_LIVE_PATH"\ncompare = { repo = "raw", live = "raw" }'


def test_noop_row_is_unselectable_and_review_explains_why(tmp_path, monkeypatch):
    import asyncio

    from textual.widgets import Static

    from dotman.sync_deck import SyncDeckApp, WorksetTable

    engine = make_engine(tmp_path, monkeypatch, [('unit', 'pull-only', b'a\n', b'A\n', FOLDING_CAPTURE)])

    async def interact():
        with engine.open_pull_session(engine.resolve_sync_scope()) as session:
            app = SyncDeckApp(CommandDeck(session, use_color=False))
            async with app.run_test() as pilot:
                marker, _target, _policy, resolution = (str(cell) for cell in app.query_one(WorksetTable).get_row_at(0))
                assert (marker, resolution) == ('[-]', 'No-op')
                await pilot.press('space')
                assert not session.view.rows[0].approved
                assert 'Nothing to do' in str(app.query_one('#notice', Static).render())
            deck = CommandDeck(session, use_color=False)
            deck.open_review()
            review = deck.review_text()
            assert 'Approval: not needed' in review
            assert 'Nothing to do: Approval would neither write nor record a Sync Base' in review
            assert 'compare does not match Capture' in review

    asyncio.run(asyncio.wait_for(interact(), timeout=10))


@pytest.mark.parametrize('key', ['a', 'u'])
def test_bulk_selection_keeps_noop_row_unselectable(tmp_path, monkeypatch, key):
    import asyncio

    from dotman.sync_deck import SyncDeckApp, WorksetTable

    engine = make_engine(tmp_path, monkeypatch, [('unit', 'pull-only', b'a\n', b'A\n', FOLDING_CAPTURE)])

    async def interact():
        with engine.open_pull_session(engine.resolve_sync_scope()) as session:
            app = SyncDeckApp(CommandDeck(session, use_color=False))
            async with app.run_test() as pilot:
                await pilot.press(key)
                await app.workers.wait_for_complete()
                await pilot.pause()
                marker, _target, _policy, resolution = (str(cell) for cell in app.query_one(WorksetTable).get_row_at(0))
                assert (marker, resolution) == ('[-]', 'No-op')
                assert not session.view.rows[0].approved

    asyncio.run(asyncio.wait_for(interact(), timeout=10))


def test_no_write_review_says_approval_records_the_sync_base(tmp_path, monkeypatch):
    # Render reproduces live, so Approval can record a Sync Base even though nothing is written.
    extra = 'render = "tr a-z A-Z < $DOTMAN_SOURCE"\n' + FOLDING_CAPTURE
    engine = make_engine(tmp_path, monkeypatch, [('unit', 'pull-only', b'a\n', b'A\n', extra)])
    with engine.open_pull_session(engine.resolve_sync_scope()) as session:
        assert session.view.rows[0].approved
        review = CommandDeck(session, use_color=False).review_text()
        assert 'Nothing will be written; Approval records the Sync Base' in review


@pytest.mark.parametrize('report', [False, True])
def test_no_confirm_noop_is_reported_as_noop_only_on_request(tmp_path, monkeypatch, capsys, report):
    from dotman.sync_deck_command import PullDeckCommandRunner

    engine = make_engine(tmp_path, monkeypatch, [('unit', 'pull-only', b'a\n', b'A\n', FOLDING_CAPTURE)])
    args = SimpleNamespace(config=engine.config.config_path, scopes=[], no_deps=False, dry_run=True, no_confirm=True,
                           json_output=False, run_noop=False, command='pull', report=report)
    assert PullDeckCommandRunner(engine_factory=lambda _: engine, use_color=False).run(args) == 0
    assert ('[noop] main:app.unit' in capsys.readouterr().out) == report
