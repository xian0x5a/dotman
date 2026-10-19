from __future__ import annotations

import json
import pytest
from types import SimpleNamespace

from dotman.cli_parser import build_parser
from tests.engine.test_sync_session import make_engine


def arguments(**overrides):
    return SimpleNamespace(**dict(
        dict(command="sync", config=None, scopes=[], no_deps=False, dry_run=True,
             no_confirm=True, json_output=True), **overrides))


def runner_for(engine):
    from dotman.sync_deck_command import SyncDeckCommandRunner
    return SyncDeckCommandRunner(engine_factory=lambda _: engine, use_color=False)


def test_sync_parser_accepts_multiple_exact_scopes_and_explicit_approval():
    args = build_parser().parse_args(["--no-confirm", "sync", "main:app.one", "main:app.two", "--dry-run"])
    assert args.scopes == ["main:app.one", "main:app.two"]
    assert args.no_confirm and args.dry_run


@pytest.mark.parametrize("command", ["sync", "pull", "push"])
def test_no_deps_leaves_dependency_targets_out_of_a_package_scope(tmp_path, monkeypatch, capsys, command):
    engine = make_engine(tmp_path, monkeypatch, [("own", "push-only", b"r", b"l", "")],
                         dependency_targets=[("shared", "push-only", b"r", b"l", "")])
    args = build_parser().parse_args(["--no-confirm", "--json", command, "--dry-run", "--no-deps", "main:app"])
    assert args.no_deps

    assert runner_for(engine).run(arguments(scopes=["main:app"], no_deps=True)) == 0
    units = [unit["identity"] for unit in json.loads(capsys.readouterr().out)["sync_units"]]
    assert units == ["main:app.own"]

    assert runner_for(engine).run(arguments(scopes=["main:app"])) == 0
    units = [unit["identity"] for unit in json.loads(capsys.readouterr().out)["sync_units"]]
    assert sorted(units) == ["main:app.own", "main:base.shared"]


def test_no_confirm_preview_selects_defaults_without_writes_or_content_leaks(tmp_path, monkeypatch, capsys):
    engine = make_engine(tmp_path, monkeypatch, [("unit", "push-only", b"repo-secret", b"live-secret", "")])
    assert runner_for(engine).run(arguments()) == 0
    output = capsys.readouterr().out
    payload = json.loads(output)
    assert set(payload) == {
        "operation", "mode", "status", "scope", "summary", "sync_units",
        "additional_source_changes", "guard_skips", "no_base_skips", "probe_work", "directory_root_work", "hook_work", "commit_work", "stages",
    }
    assert payload["mode"] == "dry-run"
    unit = payload["sync_units"][0]
    assert unit["approved"] is True
    assert unit["effects"][0]["kind"] == "write"
    assert "repo-secret" not in output and "live-secret" not in output
    assert "base64" not in output and "content" not in output
    assert (tmp_path / "live/unit").read_bytes() == b"live-secret"


@pytest.mark.parametrize("dry_run", [False, True])
def test_nonterminal_sync_requires_no_confirm_before_opening_session(tmp_path, monkeypatch, capsys, dry_run):
    engine = make_engine(tmp_path, monkeypatch, [("unit", "push-only", b"repo", b"live", "")])
    def forbidden(*args, **kwargs):
        raise AssertionError("unapproved --no-confirm execution must not open a real session")
    monkeypatch.setattr(engine, "open_sync_session", forbidden)
    assert runner_for(engine).run(arguments(dry_run=dry_run, no_confirm=False)) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "failed"
    assert (tmp_path / "live/unit").read_bytes() == b"live"


def test_no_confirm_sync_executes_explicit_approval(tmp_path, monkeypatch, capsys):
    engine = make_engine(tmp_path, monkeypatch, [("unit", "push-only", b"repo", b"live", "")])
    assert runner_for(engine).run(arguments(dry_run=False, no_confirm=True)) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["sync_units"][0]["result"] == "converged"
    assert (tmp_path / "live/unit").read_bytes() == b"repo"


def test_no_confirm_observation_failure_prevents_other_publication(tmp_path, monkeypatch, capsys):
    engine = make_engine(tmp_path, monkeypatch, [("unit", "push-only", b"repo", b"live", ""),
                                               ("bad", "push-only", b"repo", b"live", "")])
    (tmp_path / "live/bad").unlink()
    (tmp_path / "live/bad").mkdir()
    assert runner_for(engine).run(arguments(dry_run=False, no_confirm=True)) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "failed"
    assert (tmp_path / "live/unit").read_bytes() == b"live"


def test_confirmation_freezes_selection_and_cancel_restores_workset(tmp_path, monkeypatch):
    from dotman.sync_deck import CommandDeck
    engine = make_engine(tmp_path, monkeypatch, [
        ("one", "push-only", b"repo", b"live", ""),
        ("two", "push-only", b"repo", b"live", ""),
    ])
    with engine.open_sync_session(engine.resolve_sync_scope([]), preview=True) as session:
        deck = CommandDeck(session, use_color=False)
        deck.select()
        deck.confirm()
        assert deck.confirming
        frozen = session.view
        deck.select()
        deck.select_all(False)
        deck.open_review()
        assert deck.focus == 0 and not deck.reviewing
        assert session.view == frozen
        assert "approved: 1" in deck.confirmation_text()
        assert "live: 1" in deck.confirmation_text()
        # Key hints belong to the help bar alone; the body must not repeat them.
        assert "Esc" not in deck.confirmation_text()
        deck.back()
        assert not deck.confirming
        assert session.view == frozen
        deck.focus = 1
        deck.select()
        assert all(row.approved for row in session.view.rows)


def test_review_shows_newline_only_publication_and_exact_mode(tmp_path, monkeypatch):
    from dotman.sync_deck import CommandDeck
    engine = make_engine(tmp_path, monkeypatch, [
        ("unit", "push-only", b"same\n", b"same", 'chmod = "0600"'),
    ])
    (tmp_path / "live/unit").chmod(0o644)
    with engine.open_sync_session(engine.resolve_sync_scope([]), preview=True) as session:
        deck = CommandDeck(session, use_color=False)
        deck.open_review()
        text = deck.review_text()
        assert "No newline at end of file" in text
        assert "0600" in text
        assert "5 bytes" in text


def test_interactive_preview_hides_unselected_healthy_work(tmp_path, monkeypatch, capsys):
    from dotman import sync_deck
    import sys

    engine = make_engine(tmp_path, monkeypatch, [("unit", "push-only", b"repo", b"live", "")])
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)

    def leave_unselected(session, *, use_color):
        assert not session.view.rows[0].approved
        return True

    monkeypatch.setattr(sync_deck, "run_command_deck", leave_unselected)
    assert runner_for(engine).run(arguments(no_confirm=False, json_output=False)) == 0
    assert "main:app.unit" not in capsys.readouterr().out
    assert (tmp_path / "live/unit").read_bytes() == b"live"


@pytest.mark.parametrize("dry_run", [False, True])
def test_interactive_sync_logs_directly_when_everything_is_in_sync(tmp_path, monkeypatch, capsys, dry_run):
    from dotman import sync_deck
    import sys

    engine = make_engine(tmp_path, monkeypatch, [("unit", "push-only", b"same", b"same", "")])
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)

    def forbidden(session, *, use_color):
        raise AssertionError("nothing to review must not open the Deck")

    monkeypatch.setattr(sync_deck, "run_command_deck", forbidden)
    assert runner_for(engine).run(arguments(dry_run=dry_run, no_confirm=False, json_output=False)) == 0
    assert "in-sync: 1" in capsys.readouterr().out


def test_failed_materialization_stops_no_confirm_publication(tmp_path, monkeypatch, capsys):
    engine = make_engine(tmp_path, monkeypatch, [
        ("one", "push-only", b"repo", b"live", ""),
        ("two", "push-only", b"repo", b"live", ""),
    ])
    live = tmp_path / "live/two"
    referent = tmp_path / "referent"
    referent.write_bytes(b"live")
    live.unlink()
    live.symlink_to(referent)
    assert runner_for(engine).run(arguments(dry_run=False)) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "failed"
    assert payload["sync_units"][1]["diagnostics"]
    assert (tmp_path / "live/one").read_bytes() == b"live"
    assert live.is_symlink() and referent.read_bytes() == b"live"


def test_json_summary_counts_only_selected_effects(tmp_path, monkeypatch):
    from dotman.sync_deck_command import review, sync_document
    from dotman.sync_session import Preview

    engine = make_engine(tmp_path, monkeypatch, [("unit", "push-only", b"repo", b"live", "")])
    with engine.open_sync_session(engine.resolve_sync_scope([]), preview=True) as session:
        review(session, session.view.rows[0].row_id)
        view = session.view
        result = session.dispatch(Preview(view.session_id, view.revision)).result
        payload = sync_document(arguments(), session, result)
        assert payload["sync_units"][0]["effects"]
        assert payload["sync_units"][0]["approved"] is False
        assert payload["summary"]["live_writes"] == 0
        assert payload["summary"]["live_deletions"] == 0


@pytest.mark.parametrize("failure,code,status", [
    (ValueError("invalid scope"), 2, "failed"),
    (KeyboardInterrupt(), 130, "aborted"),
])
def test_json_preflight_failure_is_one_document(tmp_path, monkeypatch, capsys, failure, code, status):
    engine = make_engine(tmp_path, monkeypatch, [("unit", "push-only", b"repo", b"live", "")])
    def fail(*args, **kwargs):
        raise failure
    monkeypatch.setattr(engine, "resolve_sync_scope", fail)
    assert runner_for(engine).run(arguments()) == code
    assert json.loads(capsys.readouterr().out)["status"] == status


def test_interrupted_materialization_reports_abort_without_publication(tmp_path, monkeypatch, capsys):
    from dotman import sync_session
    engine = make_engine(tmp_path, monkeypatch, [("unit", "push-only", b"repo", b"live", "")])
    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt()
    monkeypatch.setattr(sync_session, "materialize", interrupt)
    assert runner_for(engine).run(arguments(dry_run=False)) == 130
    assert json.loads(capsys.readouterr().out)["status"] == "aborted"
    assert (tmp_path / "live/unit").read_bytes() == b"live"


@pytest.mark.parametrize("interrupt", [False, True])
def test_interactive_abort_emits_final_summary_and_releases_session(tmp_path, monkeypatch, capsys, interrupt):
    import sys
    from dotman import sync_deck

    engine = make_engine(tmp_path, monkeypatch, [("unit", "push-only", b"repo", b"live", "")])
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    sessions = []
    def abort(session, **kwargs):
        from dotman.sync_deck_command import set_all_selected
        sessions.append(session)
        set_all_selected(session, True, tuple(row.row_id for row in session.view.rows))
        if interrupt:
            raise KeyboardInterrupt()
        return False
    monkeypatch.setattr(sync_deck, "run_command_deck", abort)
    assert runner_for(engine).run(arguments(dry_run=False, no_confirm=False, json_output=False)) == 130
    captured = capsys.readouterr()
    # Nothing ran, so the log must not echo the dismissed rows back as pending.
    assert captured.out.splitlines() == [":: Sync", ":: aborted — no changes made"]
    assert "Sync aborted" in captured.err
    assert sessions[0].view.terminal
    assert (tmp_path / "live/unit").read_bytes() == b"live"


def test_json_reports_actual_failed_hook_without_leaking_captured_output(tmp_path, monkeypatch, capsys):
    engine = make_engine(tmp_path, monkeypatch, [
        ("unit", "push-only", b"repo", b"live",
         '[targets.unit.hooks]\npost_push = "printf hook-secret; exit 7"'),
    ])
    assert runner_for(engine).run(arguments(dry_run=False)) == 1
    output = capsys.readouterr().out
    payload = json.loads(output)
    assert "hook-secret" not in output
    assert payload["status"] == "failed"
    assert payload["sync_units"][0]["result"] == "converged"
    assert payload["summary"]["diagnostics"]
    assert all(set(step) == {
        "stage", "kind", "action", "scope", "scope_identity", "repo", "package_id",
        "status", "skip_reason", "exit_code", "error",
    } for step in payload["stages"])
    assert all(step["stage"] == "live-publication" for step in payload["stages"])
    assert any(step["action"] == "post_push" and step["status"] == "failed"
               and step["exit_code"] == 7 for step in payload["stages"])


@pytest.mark.parametrize("dry_run", [False, True])
def test_no_confirm_both_fallback_reports_reason_and_skips(tmp_path, monkeypatch, capsys, dry_run):
    engine = make_engine(tmp_path, monkeypatch, [
        ("push", "push-only", b"repo", b"live", ""),
        ("both", "both", b"repo", b"live", ""),
    ])
    assert runner_for(engine).run(arguments(dry_run=dry_run)) == 0
    payload = json.loads(capsys.readouterr().out)
    unit = next(unit for unit in payload["sync_units"] if unit["policy"] == "both")
    assert unit["resolution_intent"] == "use-repository"
    assert unit["fallback_reason"] == "absent"
    assert unit["allowed_intents"] == ["use-repository", "use-live"]
    assert not unit["selected"]
    assert (tmp_path / "repo/packages/app/both").read_bytes() == b"repo"
    assert (tmp_path / "live/both").read_bytes() == b"live"
    assert (tmp_path / "live/push").read_bytes() == (b"live" if dry_run else b"repo")


def test_report_marks_an_approved_guess_until_a_side_is_chosen(tmp_path, monkeypatch, capsys):
    from dotman import sync_deck
    from dotman.sync_session import SetApproval, SetResolutionIntent
    import sys

    engine = make_engine(tmp_path, monkeypatch, [
        ("guessed", "both", b"repo", b"live", ""),
        ("chosen", "both", b"repo", b"live", ""),
    ])
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)

    def approve_both(session, *, use_color):
        for row_id in ("main:app.guessed", "main:app.chosen"):
            view = session.view
            session.dispatch(SetApproval(view.session_id, view.revision, row_id, True))
        view = session.view
        session.dispatch(SetResolutionIntent(view.session_id, view.revision, "main:app.chosen", "use-repository"))
        return True

    monkeypatch.setattr(sync_deck, "run_command_deck", approve_both)
    assert runner_for(engine).run(arguments(no_confirm=False, dry_run=True, json_output=False, report=True)) == 0
    out = capsys.readouterr().out
    assert _in_order(out, "main:app.guessed", "Use repository (guessed)", "main:app.chosen")
    assert out.count("(guessed)") == 1
    assert "Fallback:" not in out


def test_json_failed_hook_identifies_exact_instance_target(tmp_path, monkeypatch, capsys):
    from dotman.engine import DotmanEngine

    engine = make_engine(tmp_path, monkeypatch, [
        ("unit", "push-only", b"repo", b"live",
         '[targets.unit.hooks]\npost_push = "exit 7"'),
    ])
    manifest = tmp_path / "repo/packages/app/package.toml"
    manifest.write_text('binding_mode = "multi_instance"\n' + manifest.read_text())
    engine = DotmanEngine(engine.config)
    assert runner_for(engine).run(arguments(dry_run=False)) == 1
    payload = json.loads(capsys.readouterr().out)
    failed_hook = next(step for step in payload["stages"] if step["status"] == "failed")
    assert failed_hook["scope_identity"] == "main:app<default>.unit"
    assert failed_hook["scope"] == "target"
    assert payload["sync_units"][0]["identity"] == failed_hook["scope_identity"]
    assert payload["sync_units"][0]["result"] == "converged"

@pytest.mark.parametrize("dry_run", [True, False])
def test_no_confirm_topology_blocker_fails_with_typed_diagnostic(tmp_path, monkeypatch, capsys, dry_run):
    from tests.engine.test_sync_directory_observation import directory_engine, put

    engine = directory_engine(tmp_path, monkeypatch, policy="push-only",
                              extra='[targets.tree.ignore]\npatterns = ["node/private"]')
    put(tmp_path / "repo/packages/app/tree", "node", b"new")
    blocked = put(tmp_path / "live/tree", "node/private", b"private")
    assert runner_for(engine).run(arguments(dry_run=dry_run)) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "failed"
    assert payload["summary"]["diagnostics"][0]["code"] == "structural-conflict"
    assert blocked.read_bytes() == b"private"


def test_human_summary_reports_completion_counts_and_canonical_identity(tmp_path, monkeypatch, capsys):
    engine = make_engine(tmp_path, monkeypatch, [("unit", "push-only", b"repo", b"live", "")])
    assert runner_for(engine).run(arguments(json_output=False)) == 0
    output = capsys.readouterr().out
    assert "main:app.unit" in output
    assert "completed" in output
    assert "approved: 1" in output
    assert "live: 1" in output


def test_results_omit_directly_in_sync_units_and_count_them(tmp_path, monkeypatch, capsys):
    engine = make_engine(tmp_path, monkeypatch, [
        ("drift", "push-only", b"repo", b"live", ""),
        ("same", "push-only", b"same", b"same", ""),
    ])
    assert runner_for(engine).run(arguments(json_output=False)) == 0
    output = capsys.readouterr().out
    assert "main:app.drift" in output
    assert "main:app.same" not in output
    assert "directly-in-sync" not in output
    assert "in-sync: 1" in output
    assert runner_for(engine).run(arguments()) == 0
    payload = json.loads(capsys.readouterr().out)
    assert [unit["identity"] for unit in payload["sync_units"]] == ["main:app.drift"]
    assert payload["scope"] == ["main:app.drift"]
    assert payload["summary"]["in_sync_units"] == 1

@pytest.mark.parametrize("projection", [
    'render = "cat $DOTMAN_SOURCE >&2; printf \'%s\' $DOTMAN_SOURCE >&2; exit 7"',
    'capture = "cat $DOTMAN_SOURCE >&2; printf \'%s\' $DOTMAN_SOURCE >&2; exit 7"\ncompare = { repo = "raw", live = "raw" }',
    'compare = { repo = "cat $DOTMAN_SOURCE >&2; printf \'%s\' $DOTMAN_SOURCE >&2; exit 7", live = "raw" }',
])
def test_failed_projection_json_never_exposes_command_output_or_workspace(tmp_path, monkeypatch, capsys, projection):
    # Render runs only for push work; Capture and repo comparison for pull work.
    policy = "push-only" if projection.startswith("render") else "pull-only"
    engine = make_engine(tmp_path, monkeypatch, [
        ("unit", policy, b"repo-private-content", b"live-private-content", projection),
    ])
    assert runner_for(engine).run(arguments()) == 1
    output = capsys.readouterr().out
    payload = json.loads(output)
    assert payload["status"] == "failed"
    assert "private-content" not in output
    assert "dotman-projection-" not in output
    assert "dotman-comparison-" not in output

@pytest.mark.parametrize("full_path", [False, True])
def test_sync_review_shows_full_paths_regardless_of_full_path_option(tmp_path, monkeypatch, capsys, full_path):
    import sys
    from dotman import sync_deck

    engine = make_engine(tmp_path, monkeypatch, [("unit", "push-only", b"repo", b"live", "")])
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)

    def inspect(session, **kwargs):
        deck = sync_deck.CommandDeck(session, use_color=False)
        deck.open_review()
        # The review wraps long values, so compaction would only hide path identity.
        assert f"Live path: {tmp_path / 'live/unit'}" in deck.review_text()
        return True

    monkeypatch.setattr(sync_deck, "run_command_deck", inspect)
    assert runner_for(engine).run(arguments(no_confirm=False, json_output=False, full_path=full_path)) == 0

@pytest.mark.parametrize("command", ["guard", "probe"])
def test_failed_planning_commands_do_not_copy_output_into_json(tmp_path, monkeypatch, capsys, command):
    engine = make_engine(tmp_path, monkeypatch, [("unit", "push-only", b"repo", b"live", "")])
    manifest = tmp_path / "repo/packages/app/package.toml"
    if command == "guard":
        manifest.write_text(manifest.read_text() + '\n[targets.unit.hooks]\nguard_push = "printf private-command-output >&2; exit 7"\n')
    else:
        manifest.write_text('[targets.unit]\nprobe = "printf private-command-output >&2; exit 7"\nsync_policy = "push-only"\n')
    from dotman.engine import DotmanEngine
    engine = DotmanEngine.from_config_path(engine.config.config_path)
    assert runner_for(engine).run(arguments()) == 1
    output = capsys.readouterr().out
    assert json.loads(output)["summary"]["diagnostics"]
    assert "private-command-output" not in output

def test_no_confirm_sync_propagates_mode_to_both_hook_families(tmp_path, monkeypatch, capsys):
    engine = make_engine(tmp_path, monkeypatch, [
        ("in", "pull-only", b"repo", b"live", '[targets.in.hooks]\npre_pull = "test \\"$DOTMAN_NO_CONFIRM\\" = 1"'),
        ("out", "push-only", b"repo", b"live", '[targets.out.hooks]\npre_push = "test \\"$DOTMAN_NO_CONFIRM\\" = 1"'),
    ])
    assert runner_for(engine).run(arguments(dry_run=False)) == 0
    payload = json.loads(capsys.readouterr().out)
    assert {step["action"] for step in payload["stages"] if step["kind"] == "hook"} == {"pre_pull", "pre_push"}
    assert all(step["status"] == "ok" for step in payload["stages"])

@pytest.mark.parametrize("hook", ["pre_pull", "pre_push"])
def test_interrupted_hook_json_keeps_exit_evidence_without_output(tmp_path, monkeypatch, capsys, hook):
    policy = "pull-only" if hook == "pre_pull" else "push-only"
    engine = make_engine(tmp_path, monkeypatch, [
        ("unit", policy, b"repo", b"live", f'[targets.unit.hooks]\n{hook} = "printf SECRET_PAYLOAD >&2; exit 130"'),
    ])
    assert runner_for(engine).run(arguments(dry_run=False)) == 130
    output = capsys.readouterr().out
    document = json.loads(output)
    assert document["status"] == "aborted"
    assert "SECRET_PAYLOAD" not in output
    interrupted = [step for step in document["stages"] if step["status"] == "interrupted"]
    assert len(interrupted) == 1
    assert interrupted[0]["action"] == hook
    assert interrupted[0]["exit_code"] == 130
    assert document["summary"]["diagnostics"][0]["code"] == "interrupted"


def test_sync_resolves_shorthand_scopes_to_canonical_identities(tmp_path, monkeypatch, capsys):
    engine = make_engine(tmp_path, monkeypatch, [
        ("one", "push-only", b"repo", b"live", ""),
        ("two", "push-only", b"repo", b"live", ""),
    ])
    assert runner_for(engine).run(arguments(scopes=["one"])) == 0
    payload = json.loads(capsys.readouterr().out)
    assert [unit["identity"] for unit in payload["sync_units"]] == ["main:app.one"]


def test_noninteractive_sync_rejects_ambiguous_scope_with_candidates(tmp_path, monkeypatch, capsys):
    engine = make_engine(tmp_path, monkeypatch, [
        ("one", "push-only", b"repo", b"live", ""),
        ("two", "push-only", b"repo", b"live", ""),
    ])
    assert runner_for(engine).run(arguments(scopes=["o"])) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "failed"
    assert "sync scope 'o' is ambiguous: target main:app.one, target main:app.two" in json.dumps(payload)


def _engine_with_manifest_suffix(tmp_path, monkeypatch, suffix):
    from dotman.engine import DotmanEngine
    engine = make_engine(tmp_path, monkeypatch, [("unit", "push-only", b"repo", b"live", "")])
    manifest = tmp_path / "repo/packages/app/package.toml"
    manifest.write_text(manifest.read_text() + suffix)
    return DotmanEngine.from_config_path(engine.config.config_path)


FAILING_PRE_PUSH = '\n[targets.unit.hooks]\npre_push = "echo; echo refusing to replace directory >&2; exit 3"\n'


def test_failed_hook_output_stays_out_of_json(tmp_path, monkeypatch, capsys):
    engine = _engine_with_manifest_suffix(tmp_path, monkeypatch, FAILING_PRE_PUSH)
    assert runner_for(engine).run(arguments(dry_run=False)) == 1
    output = capsys.readouterr().out
    payload = json.loads(output)
    assert (payload["stages"][0]["action"], payload["stages"][0]["status"]) == ("pre_push", "failed")
    assert "refusing" not in output


@pytest.mark.parametrize("command", ["guard", "probe"])
def test_failed_planning_commands_show_reason_in_human_output(tmp_path, monkeypatch, capsys, command):
    engine = make_engine(tmp_path, monkeypatch, [("unit", "push-only", b"repo", b"live", "")])
    manifest = tmp_path / "repo/packages/app/package.toml"
    if command == "guard":
        manifest.write_text(manifest.read_text() + '\n[targets.unit.hooks]\nguard_push = "printf \'\\nguard reason\\n\' >&2; exit 7"\n')
    else:
        manifest.write_text('[targets.unit]\nprobe = "printf \'\\nguard reason\\n\' >&2; exit 7"\nsync_policy = "push-only"\n')
    from dotman.engine import DotmanEngine
    engine = DotmanEngine.from_config_path(engine.config.config_path)
    assert runner_for(engine).run(arguments(json_output=False)) == 1
    assert " 7: guard reason" in capsys.readouterr().err


def _human_execution(tmp_path, monkeypatch, capsys, *, suffix="", patch=None, exit_code=1, report=False):
    engine = _engine_with_manifest_suffix(tmp_path, monkeypatch, suffix)
    if patch:
        patch()
    assert runner_for(engine).run(arguments(dry_run=False, json_output=False, report=report)) == exit_code
    captured = capsys.readouterr()
    return captured.out, captured.err


def _in_order(text, *parts):
    positions = [text.index(part) for part in parts]
    return positions == sorted(positions)


def test_execution_streams_step_timeline_with_live_hook_output(tmp_path, monkeypatch, capsys):
    out, _ = _human_execution(
        tmp_path, monkeypatch, capsys, exit_code=0,
        suffix='\n[targets.unit.hooks]\npre_push = "echo checking-now"\npost_push = "true"\n',
    )
    assert _in_order(out, ":: Sync", "  main:app", "[1/3] pre_push", "      checking-now",
                     "ok", "[2/3] write", "[3/3] post_push", ":: completed")
    # Timeline replaces the per-entry log; a clean unit is not recapped.
    assert "[ok] main:app.unit" not in out
    assert "Use repository" not in out


def test_failed_hook_is_reported_once_in_timeline(tmp_path, monkeypatch, capsys):
    out, err = _human_execution(tmp_path, monkeypatch, capsys, suffix=FAILING_PRE_PUSH)
    # Skipped units are counted, not listed, so the failure stays next to the summary.
    assert _in_order(out, "[1/2] pre_push", "exit 3", "failed", ":: failed")
    assert "[skipped]" not in out
    assert out.rstrip().endswith("skipped: 1")
    # Streamed hook stderr is the only stderr: no repeated operation diagnostic.
    assert err.strip() == "refusing to replace directory"
    assert "[2/2] write" not in out


def test_failed_write_is_reported_once_in_timeline(tmp_path, monkeypatch, capsys):
    from dotman import file_access
    def fail(*args, **kwargs):
        raise OSError("disk full")
    out, err = _human_execution(tmp_path, monkeypatch, capsys,
                                patch=lambda: monkeypatch.setattr(file_access, "write_bytes_atomic", fail))
    assert _in_order(out, "[1/1] write", "disk full", "failed", ":: failed")
    assert (out + err).count("disk full") == 1


def test_step_failure_without_start_is_attributed_in_timeline(tmp_path, monkeypatch, capsys):
    from dotman import sync_publication
    def fail(*args, **kwargs):
        raise OSError("snapshot store unavailable")
    out, err = _human_execution(tmp_path, monkeypatch, capsys,
                                patch=lambda: monkeypatch.setattr(sync_publication, "mark_snapshot_status", fail))
    assert _in_order(out, "[1/1] write", "ok", "finalize", "snapshot store unavailable", ":: failed")
    assert (out + err).count("snapshot store unavailable") == 1


def test_successful_execution_has_no_skipped_stat(tmp_path, monkeypatch, capsys):
    out, _ = _human_execution(tmp_path, monkeypatch, capsys, exit_code=0)
    assert "skipped" not in out


@pytest.mark.parametrize("report", [False, True])
def test_guard_skips_print_before_the_timeline(tmp_path, monkeypatch, capsys, report):
    from dotman.sync_deck_command import PullDeckCommandRunner
    engine = make_engine(tmp_path, monkeypatch, [
        ("unit", "both", b"repo", b"live", ""),
        ("other", "both", b"repo", b"live", '[targets.other.hooks]\nguard_pull = "echo host mismatch >&2; exit 100"'),
    ])
    runner = PullDeckCommandRunner(engine_factory=lambda _: engine, use_color=False)
    assert runner.run(arguments(dry_run=False, json_output=False, command="pull", report=report)) == 0
    out = capsys.readouterr().out
    guard = ("[skipped] main:app.other (guard_pull)", "Guard skipped: host mismatch")
    # The report is the complete record, so it carries guard skips instead of the preamble.
    expected = ("[1/1] update", *guard) if report else (*guard, "[1/1] update")
    assert _in_order(out, ":: Pull", *expected, ":: completed")
    assert out.count("main:app.other") == 1


@pytest.mark.parametrize("command", ["sync", "push", "pull"])
def test_report_flag_is_accepted(command):
    assert build_parser().parse_args([command, "--report"]).report is True


def test_report_lists_every_entry_after_the_timeline(tmp_path, monkeypatch, capsys):
    from dotman import sync_deck
    from dotman.sync_session import SetApproval
    import sys
    engine = make_engine(tmp_path, monkeypatch, [
        ("unit", "push-only", b"repo", b"live", ""),
        ("second", "push-only", b"repo", b"live", ""),
    ])
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)

    def approve_first(session, *, use_color):
        view = session.view
        session.dispatch(SetApproval(view.session_id, view.revision, "main:app.unit", True))
        return True

    monkeypatch.setattr(sync_deck, "run_command_deck", approve_first)
    assert runner_for(engine).run(arguments(no_confirm=False, dry_run=False, json_output=False, report=True)) == 0
    out = capsys.readouterr().out
    assert _in_order(out, "[1/1] write", "[ok] main:app.unit", "Use repository", ":: completed")
    assert "[unselected] main:app.second" in out


def test_report_lists_skipped_work_instead_of_counting(tmp_path, monkeypatch, capsys):
    out, _ = _human_execution(tmp_path, monkeypatch, capsys, suffix=FAILING_PRE_PUSH, report=True)
    assert _in_order(out, "failed", "[skipped] main:app.unit", ":: failed")
    assert "skipped: 1" not in out


def no_base_engine(tmp_path, monkeypatch):
    # Fresh host: both-policy sources exist, live is Missing, no Base yet.
    return make_engine(tmp_path, monkeypatch, [
        ("fresh", "both", b"repo", None, ""), ("owned", "push-only", b"repo", b"live", ""),
    ])


def test_no_confirm_sync_skips_no_base_drift_and_keeps_repository_sources(tmp_path, monkeypatch, capsys):
    engine = no_base_engine(tmp_path, monkeypatch)
    assert runner_for(engine).run(arguments(dry_run=False, json_output=False)) == 0
    output = capsys.readouterr().out
    assert (tmp_path / "repo/packages/app/fresh").read_bytes() == b"repo"
    assert not (tmp_path / "live/fresh").exists()
    # Work with a determined direction still runs.
    assert (tmp_path / "live/owned").read_bytes() == b"repo"
    assert "[skipped] main:app.fresh (no Base)" in output
    assert "Needs a first review: run dotman sync, or push/pull to choose a side" in output
    assert output.count("main:app.fresh") == 1
    with engine.open_sync_session(engine.resolve_sync_scope(), preview=True) as session:
        fresh = next(unit for unit in session.view.observations if unit.identity.canonical == "main:app.fresh")
        assert fresh.base.status != "usable"


def test_no_confirm_sync_json_lists_no_base_skips(tmp_path, monkeypatch, capsys):
    engine = no_base_engine(tmp_path, monkeypatch)
    assert runner_for(engine).run(arguments()) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["no_base_skips"] == [{"identity": "main:app.fresh", "reason": "absent"}]
    units = {unit["identity"]: unit for unit in payload["sync_units"]}
    assert not units["main:app.fresh"]["selected"] and units["main:app.owned"]["selected"]


@pytest.mark.parametrize(("established", "reason"), [(True, "unreadable"), (False, "absent")])
def test_no_confirm_sync_json_reports_an_unreadable_base_apart_from_an_absent_one(
        tmp_path, monkeypatch, capsys, established, reason):
    engine = no_base_engine(tmp_path, monkeypatch)
    if established:
        assert runner_for(engine).run(arguments(dry_run=False)) == 0
        capsys.readouterr()
    store_directory = tmp_path / "state/dotman/repos/main"
    store_directory.rename(tmp_path / "relocated-store")
    # Opening fails closed on a symlinked store directory. A store that was
    # never created still holds no Base, so only an established one is unreadable.
    store_directory.symlink_to(tmp_path / "relocated-store")
    assert runner_for(engine).run(arguments(dry_run=False)) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["no_base_skips"] == [{"identity": "main:app.fresh", "reason": reason}]
