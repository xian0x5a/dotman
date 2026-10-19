"""Command Deck CLI adapters; sessions own Approval and frozen execution."""

from __future__ import annotations

import json
from dataclasses import replace
import sys

from dotman.edit_resolution import EditResolver
from dotman.interaction import Interaction
from dotman.interaction_policy import interaction_scope
from dotman.progress import make_planning_sink
from dotman.sync_timeline import SyncTimelineRenderer
from dotman.cli_style import SYNC_TERM_STYLE_BY_NAME, render_annotation_parentheses, render_sync_term, render_package_label, render_summary_stat, style_text, MENU_REPO_STYLE
from dotman.sync_scope import _parse_scope_selector, split_scope_child_path
from dotman.sync_base_store import DirectoryChildPresent, FilePresent, Missing
from dotman.sync_session import (
    AdditionalRow, BatchSetApproval, PrepareSourceReview, AuxiliaryRow, CommandRejected, EditProposal, PrepareProposalReview, Preview, SessionOpenFailed,
    SessionRow, SetApproval, SetCommit, SetIncluded, SetResolutionIntent, RetryMaterialization, SyncSession, has_errors,
)
from dotman.ui_context import ui_config_scope


def selection_uses_inclusion(row) -> bool:
    # Directory children are ordinary Proposal rows; only auxiliary work uses
    # inclusion because it has no independent Proposal/Approval.
    return isinstance(row, AuxiliaryRow)


def set_selected(session: SyncSession, row, selected: bool):
    view = session.view
    command = SetIncluded if selection_uses_inclusion(row) else SetApproval
    return session.dispatch(command(view.session_id, view.revision, row.row_id, selected))


def set_all_selected(session: SyncSession, selected: bool, row_ids: tuple[str, ...]):
    view = session.view
    return session.dispatch(BatchSetApproval(view.session_id, view.revision, selected, row_ids))


def additional_label(row, *, use_color: bool = False) -> str:
    repo = style_text(row.repo, *MENU_REPO_STYLE) if use_color else row.repo
    return f"{repo}:{row.path}"


def row_diagnostics(row):
    if isinstance(row, AdditionalRow):
        return ()
    return row.diagnostics if isinstance(row, AuxiliaryRow) else (
        *row.observation.diagnostics, *row.diagnostics,
        *(row.proposal.checkpoint_warnings if row.proposal else ()),
    )


def auxiliary_resolution(kind: str) -> str:
    return {"probe": "Probe Work", "hook": "Hook Work", "directory-root": "Directory Root Work",
            "guard-skip": "Guard skipped"}[kind]


def auxiliary_label(scope: str, kind: str, directions, *, path_rule_pattern: str | None = None,
                    use_color: bool = False) -> str:
    if use_color:
        if ":" in scope:
            identity = _parse_scope_selector(scope)
            scope = render_package_label(
                repo_name=identity.repo, package_id=identity.package_id,
                bound_profile=identity.bound_profile, target_name=identity.target_name,
                use_color=True,
            )
        else:
            scope = style_text(scope, *MENU_REPO_STYLE)
    if kind == "hook":
        annotations = " ".join(f"({direction}-hooks)" for direction in directions)
    elif kind == "guard-skip":
        annotations = " ".join(f"(guard_{direction})" for direction in directions)
        if path_rule_pattern is not None:
            annotations += f" (path rule: {path_rule_pattern})"
    else:
        annotations = ""
    return f"{scope} {annotations}".rstrip()


def guard_skip_explanation(row) -> str:
    """Guard exit 100 omits work by design; say which Guard and why."""
    reason = f" ({row.guard_skip.reason})" if row.guard_skip.reason else ""
    return f"guard_{row.directions[0]} exited 100{reason}"


def review(session: SyncSession, row_id: str):
    view = session.view
    row = next(row for row in view.rows if row.row_id == row_id)
    command = PrepareSourceReview if isinstance(row, AdditionalRow) else PrepareProposalReview
    return session.dispatch(command(view.session_id, view.revision, row_id))


def select_all_commits(session: SyncSession) -> None:
    for option in session.view.commit_options:
        view = session.view
        session.dispatch(SetCommit(view.session_id, view.revision, option.repo, True))


def edit_proposal(session: SyncSession, row_id: str):
    view = session.view
    return session.dispatch(EditProposal(view.session_id, view.revision, row_id))


def set_resolution_intent(session: SyncSession, row_id: str, intent: str):
    view = session.view
    return session.dispatch(SetResolutionIntent(view.session_id, view.revision, row_id, intent))


def retry_materialization(session: SyncSession, row_id: str):
    view = session.view
    return session.dispatch(RetryMaterialization(view.session_id, view.revision, row_id))


class CommandDeckCrashed(RuntimeError):
    """The Deck stopped on an unhandled error; Textual has already printed it."""


class SyncDeckCommandRunner:
    command_names = frozenset({"sync"})
    operation = "sync"

    def _open(self, engine, scope, args, timeline):
        return engine.open_sync_session(scope, **self._session_options(args, timeline))

    @staticmethod
    def _session_options(args, timeline) -> dict:
        # Only the human timeline shows live hook output; JSON stdout stays one document.
        return dict(preview=args.dry_run, run_noop=getattr(args, "run_noop", False),
                    commit=getattr(args, "commit", False),
                    sink=make_planning_sink(json_output=args.json_output, unit="target"),
                    event_sink=timeline, stream_output=timeline is not None)

    def _select_defaults(self, session):
        set_all_selected(session, True, tuple(row.row_id for row in session.view.rows))
        # No Base means no evidence of which side changed; --no-confirm runs
        # leave that drift for a first review instead of guessing a side.
        for row in session.view.rows:
            if needs_first_review(row):
                view = session.view
                session.dispatch(SetApproval(view.session_id, view.revision, row.row_id, False))

    def _resolve_scope_inputs(self, engine, scopes, *, interaction: Interaction | None) -> list[str]:
        """Resolve shorthand or ambiguous scopes to the canonical identities Sync requires."""
        resolver = EditResolver(engine.config, engine=engine, interaction=interaction, use_color=self._use_color)

        def resolve(text: str) -> str:
            # Directory-child paths are not tracked targets; resolve the owning
            # target and keep the child suffix verbatim for the engine to validate.
            target_text, child_path = split_scope_child_path(text)
            identity = resolver.resolve_tracked_identity(target_text, subject="sync scope")
            return identity if child_path is None else f"{identity}/{child_path}"

        return [resolve(text) for text in scopes]

    def __init__(self, *, engine_factory, use_color: bool, interaction: Interaction | None = None) -> None:
        self._engine_factory = engine_factory
        self._use_color = use_color
        self._interaction = interaction

    def run(self, args) -> int:
        with interaction_scope(no_confirm=args.no_confirm):
            return self._run(args)

    def _run(self, args) -> int:
        interactive = (
            not args.json_output and not args.no_confirm
            and sys.stdin.isatty() and sys.stdout.isatty()
        )
        if not interactive and not args.no_confirm:
            self._emit(args, None, None, diagnostic={
                "code": "decision-required",
                "message": f"{self.operation.title()} requires a terminal or explicit --no-confirm.",
            })
            return 1
        # Configuration load failures keep the CLI's structured error and hint.
        engine = self._engine_factory(args.config)
        try:
            scope = engine.resolve_sync_scope(self._resolve_scope_inputs(
                engine, args.scopes, interaction=self._interaction if interactive else None,
            ), include_dependencies=not args.no_deps)
        except ValueError as exc:
            self._emit(args, None, None, diagnostic={
                "code": "invalid-input", "message": str(exc),
            })
            return 2
        except (KeyboardInterrupt, InterruptedError):
            self._emit(args, None, None, diagnostic={
                "code": "interrupted", "message": f"{self.operation.title()} preflight interrupted",
            })
            return 130
        ui = engine.config.ui
        if getattr(args, 'full_path', None) is not None:
            ui = replace(ui, full_paths=args.full_path)
        with ui_config_scope(ui):
            timeline = None if args.json_output or args.dry_run else SyncTimelineRenderer(use_color=self._use_color)
            opened = self._open(engine, scope, args, timeline)
            if isinstance(opened, SessionOpenFailed):
                self._emit(args, None, None, diagnostic={
                    "code": opened.diagnostic.code, "message": opened.diagnostic.message,
                }, output_line=opened.output_line)
                return 130 if opened.diagnostic.code == "interrupted" else 1
            with opened as session:
                # Clean in-sync units never become rows and Guard skips are never
                # selectable; without a row to decide, log instead of opening the Deck.
                deck_review = interactive and any(
                    not (isinstance(row, AuxiliaryRow) and row.kind == "guard-skip")
                    for row in session.view.rows
                )
                if deck_review:
                    from dotman.sync_deck import run_command_deck
                    # The Deck shows Commit Work on confirmation, so it starts on there;
                    # runs that never show it commit only with --commit.
                    select_all_commits(session)
                    try:
                        confirmed = run_command_deck(session, use_color=self._use_color)
                    except (KeyboardInterrupt, InterruptedError):
                        confirmed = False
                    except CommandDeckCrashed:
                        session.abort()
                        # No session result: its "aborted" status would report a crash as a user abort.
                        self._emit(args, session, None, diagnostic={
                            "code": "deck-failed",
                            "message": f"{self.operation.title()} failed: the Command Deck stopped on an error",
                        }, deck_dismissed=True)
                        return 1
                    if not confirmed:
                        aborted = session.abort()
                        self._emit(args, session, aborted.result, diagnostic={
                            "code": "interrupted", "message": f"{self.operation.title()} aborted",
                        }, deck_dismissed=True)
                        return 130
                elif args.no_confirm:
                    self._select_defaults(session)

                # No-confirm failures must not permit a partially understood
                # workset to mutate unrelated units.
                blocked = any(
                    has_errors(row_diagnostics(row))
                    for row in session.view.rows if not isinstance(row, AdditionalRow)
                )
                if not interactive and blocked:
                    interrupted = any(
                        item.code == "interrupted"
                        for row in session.view.rows
                        for item in row_diagnostics(row)
                    )
                    self._emit(args, session, None, diagnostic={
                        "code": "interrupted", "message": f"{self.operation.title()} materialization interrupted",
                    } if interrupted else None)
                    return 130 if interrupted else 1
                if not interactive and any(
                    row.kind == "drift" and row.included
                    and "set-approval" not in row.allowed_commands
                    for row in session.view.rows
                ):
                    self._emit(args, session, None, diagnostic={
                        "code": "decision-required",
                        "message": "Participating drift has no supported automatic resolution.",
                    })
                    return 1
                if args.dry_run:
                    view = session.view
                    dispatched = session.dispatch(Preview(view.session_id, view.revision))
                else:
                    if timeline is not None:
                        self._print_header(args)
                        # Guard skips are planning results shown where work is reviewed:
                        # the Deck when it opened, otherwise above the timeline.
                        # A report carries them itself.
                        if not deck_review and not getattr(args, "report", False):
                            self._print_planning_skips(
                                guard_skip_summaries(row for row in session.view.rows if isinstance(row, AuxiliaryRow)),
                                no_base_skip_summaries(args, session.view.rows))
                    dispatched = session.execute()
                if isinstance(dispatched, CommandRejected):
                    self._emit(args, session, None, diagnostic={
                        "code": dispatched.diagnostics[0].code if dispatched.diagnostics else "command-rejected",
                        "message": dispatched.diagnostics[0].message if dispatched.diagnostics else dispatched.reason,
                    })
                    return 1
                self._emit(args, session, dispatched.result, timeline=timeline)
                return dispatched.result.exit_code

    def _print_planning_skips(self, guard_skips, no_base_skips) -> None:
        for skip in guard_skips:
            label = guard_skip_label(skip["identity"], skip["direction"], skip["path_rule_pattern"], use_color=False)
            reason = f": {skip['reason']}" if skip["reason"] else ""
            print(f"  {dim_when(f'[skipped] {label}', use_color=self._use_color)}")
            print(f"      {dim_when(f'Guard skipped{reason}', use_color=self._use_color)}")
        # Unlike Guard skips these need action, so they stay undimmed.
        for skip in no_base_skips:
            print(f"  [{render_sync_term('skipped', use_color=self._use_color)}] {skip['identity']} (no Base)")
            print(f"      {NO_BASE_SKIP_HINT}")

    def _print_header(self, args) -> None:
        print(f":: {self.operation.title()}" + (" preview" if args.dry_run else ""), flush=True)

    def _emit(self, args, session: SyncSession | None, result, *, diagnostic=None, output_line=None,
              timeline: SyncTimelineRenderer | None = None, deck_dismissed: bool = False) -> None:
        payload = sync_document(args, session, result, diagnostic=diagnostic)
        if args.json_output:
            print(json.dumps(payload))
            return
        if timeline is None:
            self._print_header(args)
        term = lambda text: render_sync_term(text, use_color=self._use_color)
        # --report prints every entry; otherwise, after a timeline, the log is a
        # recap of only what the timeline could not show.
        report = getattr(args, "report", False)
        if deck_dismissed and not report:
            # Nothing ran; every entry would only echo a dismissed Deck row as pending.
            print(f":: {term(payload['status'])} — no changes made")
            print(diagnostic["message"], file=sys.stderr)
            return
        recap = timeline is not None and not report
        timeline_errors = timeline.shown_errors if recap else set()
        # Work stopped by an earlier failure is counted, not listed, so the failure
        # stays next to the summary line.
        skipped_count = 0

        def visible_entry(selected, outcome, diagnostics) -> tuple[str, list[str]] | None:
            """The lead and diagnostic lines of a visible entry, or None to hide it."""
            nonlocal skipped_count
            if not (selected or diagnostics or report):
                # Unselected entries only matter when they explain a problem.
                return None
            # A no-op offers no Approval, so "unselected" would misreport a user choice.
            lead = outcome if selected or diagnostics or outcome == "noop" else "unselected"
            messages = [item["message"] for item in diagnostics if item["message"] not in timeline_errors]
            if recap and not messages and outcome == "skipped":
                skipped_count += 1
                return None
            if recap and not messages and outcome in ("ok", "noop", "failed", "interrupted"):
                return None
            return lead, messages

        no_base_skipped = {skip["identity"] for skip in payload["no_base_skips"]}
        for unit in payload["sync_units"]:
            if unit["identity"] in no_base_skipped:
                continue
            entry = visible_entry(unit["selected"], entry_outcome(unit["result"], unit["diagnostics"]), unit["diagnostics"])
            if entry is None:
                continue
            lead, messages = entry
            print(f"  [{term(lead)}] {unit['identity']}")
            if not recap:
                if unit["resolution"]:
                    label = resolution_label(unit["resolution"])
                    guessed = unit["resolution_guessed"]
                    print(f"      {render_resolution(label, guessed=guessed, use_color=self._use_color)}"
                          f"{render_guess_annotation(guessed, use_color=self._use_color)}")
                if unit["primary_source_change"]:
                    print(f"      repository {unit['primary_source_change']['kind']}")
                for effect in unit["effects"]:
                    print(f"      {effect['kind']}")
            for message in messages:
                print(f"      {message}")
        for change in payload["additional_source_changes"]:
            # Additional Source Changes apply outside the step timeline, so always list them.
            if not (change["approved"] or change["diagnostics"] or report):
                continue
            outcome = entry_outcome(change["result"], change["diagnostics"])
            lead = outcome if change["approved"] or change["diagnostics"] else "unselected"
            messages = [item["message"] for item in change["diagnostics"]]
            print(f"  [{term(lead)}] {change['repo']}:{change['path']}")
            print(f"      {term('Additional Source Change')}: {change['kind']}")
            for message in messages:
                print(f"      {message}")
        for kind, key in (("probe", "probe_work"), ("directory-root", "directory_root_work"), ("hook", "hook_work")):
            for work in payload[key]:
                outcome = auxiliary_outcome(work["identity"], payload["stages"], kind=kind, preview=args.dry_run,
                                            run_completed=payload["status"] == "completed",
                                            diagnostics=work["diagnostics"])
                entry = visible_entry(work["selected"], outcome, work["diagnostics"])
                if entry is None:
                    continue
                lead, messages = entry
                label = auxiliary_label(work["identity"], kind, work["directions"], use_color=self._use_color)
                print(f"  [{term(lead)}] {label}")
                print(f"      {term(auxiliary_resolution(kind))}")
                for message in messages:
                    print(f"      {message}")
        # A pre-approved commit that did not happen must be named; the timeline
        # already showed a failed commit's error.
        for work in payload["commit_work"]:
            if work["result"] == "skipped" or work["result"] in ("failed", "interrupted") and work["error"] not in timeline_errors:
                reason = "earlier work failed" if work["result"] == "skipped" else work["error"]
                print(f"  [{term(work['result'])}] {work['repo']} commit ({reason})")
        # Execution diagnostics copy their failed step's error; print each failure once.
        shown = timeline_errors | {item["message"] for entry in (*payload["sync_units"], *payload["additional_source_changes"])
                                   for item in entry["diagnostics"]}
        if not recap:
            self._print_planning_skips(payload["guard_skips"], payload["no_base_skips"])
        summary = payload["summary"]
        stats = summary_stats(
            (("approved", summary["approved_units"]), ("repos", summary["repository_changes"])),
            live=summary,
            trailing=(("in-sync", summary["in_sync_units"]),) + ((("skipped", skipped_count),) if skipped_count else ())
            + ((("commits", summary["commits"]),) if summary["commits"] else ()),
            use_color=self._use_color,
        )
        print(f":: {render_sync_term(payload['status'], use_color=self._use_color)} — {stats}")
        for item in payload["summary"]["diagnostics"]:
            if item is not diagnostic and item["message"] in shown:
                continue
            # Command output belongs to the session-open diagnostic and stays out of JSON.
            reason = f": {output_line}" if output_line and item is diagnostic else ""
            print(f"{item['message']}{reason}", file=sys.stderr)


# Collapse detailed Sync statuses into the execution log vocabulary
# (ok/failed/interrupted/skipped), plus preview and not-yet-run states.
ENTRY_OUTCOME_BY_STATUS = {
    "applied": "ok",
    "converged": "ok",
    "directly-in-sync": "ok",
    "would-apply": "would-apply",
    "would-converge": "would-apply",
    "execution-failed": "failed",
    "observation-failed": "failed",
    "not-converged": "failed",
    "interrupted": "interrupted",
    "skipped": "skipped",
    "pending": "pending",
    "excluded": "pending",
    "noop": "noop",
}


def entry_outcome(status: str | None, diagnostics) -> str:
    if status is None:
        # No execution result: blocked before execution, or the session failed early.
        return "failed" if any(item.get("severity", "error") == "error" for item in diagnostics) else "pending"
    return ENTRY_OUTCOME_BY_STATUS[status]


def auxiliary_outcome(identity: str, stages, *, kind: str, preview: bool, run_completed: bool, diagnostics) -> str:
    """Auxiliary Work has no unit result; derive its outcome from the steps run at its scope."""
    statuses = {stage["status"] for stage in stages if stage["scope_identity"] == identity}
    for status in ("failed", "interrupted"):
        if status in statuses:
            return status
    if "ok" in statuses:
        return "ok"
    if statuses:
        return "skipped"
    outcome = entry_outcome("would-apply" if preview else None, diagnostics)
    # A probe already ran during planning; when it has no hooks of its own, a
    # finished run leaves nothing at its scope, which is done rather than pending.
    # Hook and directory-root work without a step never ran, and errors stay failed.
    if kind == "probe" and run_completed and outcome == "pending":
        return "ok"
    return outcome


def dim_when(text: str, *, use_color: bool) -> str:
    return style_text(text, "2") if use_color else text


def guard_skip_label(scope: str, direction: str, path_rule_pattern: str | None, *, use_color: bool) -> str:
    """Guard-skipped work is recessive everywhere: omitted, never actionable."""
    label = auxiliary_label(scope, "guard-skip", (direction,), path_rule_pattern=path_rule_pattern)
    return dim_when(label, use_color=use_color)


NO_BASE_SKIP_HINT = "Needs a first review: run dotman sync, or push/pull to choose a side"


def needs_first_review(row) -> bool:
    """Both-policy drift without a usable Base: no evidence of which side changed."""
    return isinstance(row, SessionRow) and row.fallback_reason is not None


def no_base_skip_summaries(args, rows) -> list[dict]:
    if not getattr(args, "no_confirm", False):
        return []
    return [{"identity": row.row_id, "reason": row.fallback_reason}
            for row in rows if needs_first_review(row) and not row.approved]


def guard_skip_summaries(auxiliary) -> list[dict]:
    return [
        {"identity": row.scope, "direction": row.directions[0], "scope_kind": row.guard_skip.scope_kind,
         "path_rule_pattern": row.guard_skip.path_rule_pattern, "reason": row.guard_skip.reason}
        for row in auxiliary if row.kind == "guard-skip"
    ]


def live_counts(unit_effect_kinds) -> dict[str, int]:
    """Count live targets once each, however many effects (e.g. write plus chmod) they carry."""
    units = [set(kinds) for kinds in unit_effect_kinds]
    return {"live_changes": sum(bool(kinds) for kinds in units),
            "live_deletions": sum("delete" in kinds for kinds in units),
            "live_reapplies": sum("reapply" in kinds for kinds in units)}


def summary_stats(leading, *, live, trailing=(), use_color) -> str:
    """Join counts as dimmed `label: n` stats; every changed live target shares one `live` stat."""
    stat = render_summary_stat(label="live", value=live["live_changes"], use_color=use_color)
    # Deletions are destructive and reapplies may overwrite manual live state
    # without a content diff, so both stay visible inside the merged live count.
    notes = [f"{count} {label}" for count, label in (
        (live["live_deletions"], "deleted"), (live["live_reapplies"], "reapplied")) if count]
    if notes:
        stat += f" ({', '.join(notes)})"
    stats = [render_summary_stat(label=label, value=value, use_color=use_color) for label, value in leading]
    stats.append(stat)
    stats.extend(render_summary_stat(label=label, value=value, use_color=use_color) for label, value in trailing)
    return " · ".join(stats)


def sync_document(args, session, result, *, diagnostic=None) -> dict:
    """Project only public result metadata; frozen payload bytes never leave the session."""
    view = session.view if session is not None else None
    rows = {row.row_id: row for row in view.rows if not isinstance(row, (AuxiliaryRow, AdditionalRow))} if view else {}
    auxiliary = [row for row in view.rows if isinstance(row, AuxiliaryRow)] if view else []
    additional = [row for row in view.rows if isinstance(row, AdditionalRow)] if view else []
    additional_outcomes = {item.row_id: item for item in result.additional_changes} if result else {}
    def auxiliary_work(kind):
        return [{"identity": row.scope, "selected": row.included,
                 "directions": list(row.directions),
                 "diagnostics": [{"code": item.code, "message": item.message} for item in row.diagnostics]}
                for row in auxiliary if row.kind == kind]
    outcomes = {unit.identity: unit for unit in result.units} if result else {}
    commits = {item.repo: item for item in result.commits} if result else {}
    units = []
    in_sync_units = 0
    for observation in view.observations if view else ():
        identity = observation.identity.canonical
        row = rows.get(identity)
        proposal = row.proposal if row else None
        outcome = outcomes.get(identity)
        unit_diagnostics = outcome.diagnostics if outcome else (
            *observation.diagnostics, *(row.diagnostics if row else ()),
            *(proposal.checkpoint_warnings if proposal else ()),
        )
        # Clean in-sync units have nothing to approve; listing them buries actionable
        # units and bloats output on large scopes, so only their count is reported.
        if observation.state == "directly-in-sync" and not unit_diagnostics:
            in_sync_units += 1
            continue
        diagnostics = [
            {"code": item.code, "message": item.message, "severity": item.severity}
            for item in unit_diagnostics
        ]
        intent = row.intent if row else None
        if proposal:
            materialization = "ready"
        elif row and row.diagnostics:
            materialization = "failed"
        elif row and "prepare-proposal-review" in row.allowed_commands:
            materialization = "pending"
        else:
            materialization = "not-applicable"
        units.append({
            "identity": identity,
            "policy": observation.effective_policy,
            "observation": observation.state,
            "resolution_intent": intent,
            "resolution": proposal.intent if proposal else intent,
            "generation": proposal.generation if proposal else None,
            "additional_source_changes": [item.row_id for item in additional if identity in item.references],
            "allowed_intents": list(row.allowed_intents) if row else [],
            "fallback_reason": row.fallback_reason if row else None,
            "resolution_guessed": bool(row and row.resolution_guessed),
            "capture": ("missing" if isinstance(proposal.capture, Missing) else "present") if proposal and proposal.capture is not None else None,
            "reconciliation": proposal.reconciliation if proposal else None,
            "selected": bool(row and (row.included if selection_uses_inclusion(row) else row.approved)),
            "approved": bool(row and row.approved),
            "symlink_replacement_authorized": bool(row and row.symlink_authorized),
            "materialization": materialization,
            "primary_source_change": primary_change_summary(proposal, observation.repository_path),
            "effects": [effect_summary(effect) for effect in proposal.publication_effects] if proposal else [],
            "base": {
                "status": observation.base.status,
                "qualified": proposal.checkpoint_qualified if proposal else None,
                "acknowledged": outcome.acknowledged if outcome else observation.base.acknowledged,
            },
            "result": outcome.status if outcome else None,
            "diagnostics": diagnostics,
        })
    return {
        "operation": getattr(args, "command", "sync"),
        "mode": "dry-run" if args.dry_run else "execute",
        "status": result.status if result else "aborted" if diagnostic and diagnostic["code"] == "interrupted" else "failed",
        "scope": list(dict.fromkeys([unit["identity"] for unit in units] + [row.scope for row in auxiliary])) if view else list(args.scopes),
        "summary": {
            "sync_units": len(units),
            "in_sync_units": in_sync_units,
            "selected_auxiliary": sum(row.included for row in auxiliary),
            "approved_units": sum(unit["approved"] for unit in units),
            "repository_changes": sum(unit["primary_source_change"] is not None for unit in units if unit["selected"]) + sum(row.approved for row in additional),
            "approved_additional_sources": sum(row.approved for row in additional),
            "commits": sum(item.status == "committed" for item in commits.values()),
            "live_writes": sum(effect["kind"] == "write" for unit in units if unit["selected"] for effect in unit["effects"]),
            **live_counts([effect["kind"] for effect in unit["effects"]] for unit in units if unit["selected"]),
            "diagnostics": ([diagnostic] if diagnostic else []) + [
                {"code": item.code, "message": item.message, "severity": item.severity}
                for item in result.diagnostics
            ] if result else ([diagnostic] if diagnostic else []),
        },
        "sync_units": units,
        "additional_source_changes": [
            {
                "row_id": row.row_id, "repo": row.repo, "path": str(row.path),
                "approved": row.approved, "references": list(row.references),
                "kind": "write",
                "bytes": len(row.change.candidate),
                "result": additional_outcomes[row.row_id].status if row.row_id in additional_outcomes else None,
                "diagnostics": [
                    {"code": item.code, "message": item.message}
                    for item in additional_outcomes[row.row_id].diagnostics
                ] if row.row_id in additional_outcomes else [],
            }
            for row in additional
        ],
        "guard_skips": guard_skip_summaries(auxiliary),
        "no_base_skips": no_base_skip_summaries(args, view.rows if view else ()),
        "probe_work": auxiliary_work("probe"),
        "directory_root_work": auxiliary_work("directory-root"),
        "hook_work": auxiliary_work("hook"),
        "commit_work": [
            {
                "repo": option.repo, "branch": option.branch, "selected": option.selected,
                "result": commits[option.repo].status if option.repo in commits else None,
                "commit": commits[option.repo].commit if option.repo in commits else None,
                "message": commits[option.repo].message if option.repo in commits else None,
                "error": commits[option.repo].error if option.repo in commits else None,
            }
            for option in (view.commit_options if view else ())
        ],
        # Preserve attempted and unattempted execution evidence, never infer success
        # from a materialized Proposal.
        "stages": [
            {
                "stage": item.stage,
                "kind": item.kind,
                "action": item.action,
                "scope": item.scope,
                "scope_identity": item.scope_identity,
                "repo": item.repo,
                "package_id": item.package_id,
                "status": item.status,
                "skip_reason": item.skip_reason,
                "exit_code": item.exit_code,
                "error": item.error,
            }
            for item in result.steps
        ] if result else [],
    }


def effect_summary(effect) -> dict:
    summary = {"kind": effect.kind, "path": str(effect.path)}
    if effect.content is not None:
        summary["bytes"] = len(effect.content)
    # A write also carries its planned mode, but only to apply it atomically;
    # the chmod effect is the one that reports the mode change.
    if effect.kind == "chmod" and effect.mode is not None:
        summary["mode"] = format(effect.mode, "04o")
    return summary


def resolution_label(intent: str) -> str:
    return {"use-repository": "Use repository", "use-live": "Use live", "merge": "Merge", "editor": "Edited"}[intent]


def render_resolution(label: str, *, guessed: bool, use_color: bool, fixed: bool = False) -> str:
    """A guessed Resolution takes the guess color; review and output add `render_guess_annotation` for NO_COLOR."""
    style = "guess" if guessed else "fixed" if fixed else None
    if style is None:
        return render_sync_term(label, use_color=use_color)
    return style_text(label, *SYNC_TERM_STYLE_BY_NAME[style]) if use_color else label


def render_guess_annotation(guessed: bool, *, use_color: bool) -> str:
    """Name a guessed Resolution in words; the workset column relies on color alone to save width."""
    return render_annotation_parentheses("guessed", use_color=use_color) if guessed else ""


def primary_change_summary(proposal, path) -> dict | None:
    if proposal is None or proposal.primary_source_change is None:
        return None
    change = proposal.primary_source_change
    present_types = (FilePresent, DirectoryChildPresent)
    summary = {"kind": "write" if isinstance(change, present_types) else "delete", "path": str(path)}
    if isinstance(change, present_types):
        summary["bytes"] = len(change.content)
        if isinstance(change, DirectoryChildPresent):
            summary["executable"] = change.executable
    return summary


class PullDeckCommandRunner(SyncDeckCommandRunner):
    """Pull has fixed initially approved work, not --no-confirm Sync defaults."""

    command_names = frozenset({"pull"})
    operation = "pull"

    def _open(self, engine, scope, args, timeline):
        return engine.open_pull_session(scope, **self._session_options(args, timeline))

    def _select_defaults(self, session):
        # Opening already materialized standing opt-out Approval. In particular,
        # do not retry and silently reauthorize a failed initial Proposal.
        pass


class PushDeckCommandRunner(SyncDeckCommandRunner):
    """Push has fixed initially approved work, not --no-confirm Sync defaults."""

    command_names = frozenset({"push"})
    operation = "push"

    def _open(self, engine, scope, args, timeline):
        return engine.open_push_session(scope, **self._session_options(args, timeline))

    def _select_defaults(self, session):
        # Opening already materialized standing opt-out Approval. In particular,
        # do not retry and silently reauthorize a failed initial Proposal.
        pass
