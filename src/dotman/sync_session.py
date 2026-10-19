"""One-shot Sync boundary: immutable views, semantic commands and typed outcomes."""

from __future__ import annotations

from contextlib import ExitStack
from dataclasses import dataclass, replace
from typing import Callable, Literal, Mapping
from pathlib import Path
from uuid import uuid4
import stat

from dotman.interaction_policy import no_confirm_enabled
from dotman.command_runtime import CommandOperation, command_operation, command_runtime_session
from dotman.file_access import sudo_session
from dotman.execution import ExecutionStep, ExecutionStepResult, StepFinished, StepStarted, directory_synced_file_mode
from dotman.atomic_files import default_created_file_mode
from dotman.capture import CaptureError
from dotman.sync_capture import capture_observation
from dotman.sync_observation import _identity
from dotman.sync_auxiliary import AuxiliaryRow, guard_skip_rows, plan_auxiliary, retain_directional_hooks
from dotman.sync_commit import CommitFailed, GitWorkTree, RepositoryCommit, commit_sources, probe_work_tree, render_commit_message
from dotman.sync_reconciliation import reconcile, unresolved_conflict_blocks, ReconciliationConflict, ReconciliationFailed
from dotman.projection import PlanningCommandError, project_file_view
from dotman.models import GuardSkip, ResolvedPackageIdentity, ResolvedSyncScope, package_ref_text, repo_qualified_target_text
from dotman.planning import PlanningContext
from dotman.planning_guards import GuardPlanningError
from dotman.progress import ProgressSink
from dotman.sync_base_store import SyncBaseStore, SyncBaseStoreError, FilePresent, Missing, DirectoryChildPresent, SyncBasePayload
from dotman.sync_base_lifecycle import (
    BaseUnit, FrozenBaseUnit, ProposalCompletion, SyncBaseLifecycle,
)
from dotman.operation_lock import OperationBusy, OperationLock, OperationLockError
from dotman.sync_observation import Diagnostic, Observation, observe_scope, _resolve_inputs
from dotman.sync_publication import HookActivation, PublicationResult, PublicationUnit, activates_hooks, execute_publication, prepare_publication, freeze_child_metadata
from dotman.sync_repository_apply import (
    RepositoryApplyUnit, apply_repository_source, execute_repository_apply, prepare_repository_apply,
)


from dotman.sync_editor import AdditionalEdit, EditorCommandFailed, edit_sources, freeze_additional_sources, repository_workspace

from dotman.sync_path_policy import SyncPathError

# "editor" becomes selectable once the Editor saves an outcome for the row.
ResolutionIntent = Literal["use-repository", "use-live", "merge", "editor"]


CommandName = Literal["set-commit", "authorize-symlink-replacement", "batch-set-approval", "prepare-source-review", "edit-proposal", "set-resolution-intent", "retry-materialization", "set-included", "set-approval", "prepare-proposal-review", "preview", "execute", "abort"]


@dataclass(frozen=True)
class PublicationEffect:
    kind: Literal["write", "delete", "chmod", "reapply"]
    path: Path
    content: bytes | None = None
    mode: int | None = None


@dataclass(frozen=True)
class ConflictDiagnostic(Diagnostic):
    """A reconciliation conflict with the Capture it merged and its zdiff3 merge output, if any."""

    conflict: SyncBasePayload | None = None
    capture: SyncBasePayload | None = None


def reconciliation_diagnostic(
    exc: ReconciliationConflict | ReconciliationFailed, capture: SyncBasePayload | None,
) -> Diagnostic:
    if isinstance(exc, ReconciliationConflict):
        return ConflictDiagnostic("reconciliation-conflict", str(exc), conflict=exc.conflict, capture=capture)
    return Diagnostic("reconciliation-failed", str(exc))


def conflict_diagnostic(row: SessionRow) -> ConflictDiagnostic | None:
    return next((item for item in row.diagnostics if isinstance(item, ConflictDiagnostic)), None)


@dataclass(frozen=True)
class Proposal:
    repository: SyncBasePayload
    live: SyncBasePayload
    primary_source_change: SyncBasePayload | None
    publication_effects: tuple[PublicationEffect, ...]
    intent: ResolutionIntent | None = "use-repository"
    capture: SyncBasePayload | None = None
    reconciliation: str | None = None
    generation: int = 0
    additional_changes: tuple[AdditionalEdit, ...] = ()
    checkpoint_qualified: bool = False
    checkpoint_warnings: tuple[Diagnostic, ...] = ()
    noop: bool = False
    """Approval would change nothing: no writes, and no new Sync Base to record."""


def materialize(
    observation: Observation,
    *,
    capture: Callable[[Observation], SyncBasePayload] | None = None,
    intent: ResolutionIntent | None = None,
    render: Callable[[Observation, SyncBasePayload], SyncBasePayload] | None = None,
    merge: Callable[[Observation, SyncBasePayload], SyncBasePayload] | None = None,
    symlink_authorized: bool = False,
    reapply_hooks: bool = False,
) -> Proposal:
    intent = intent or ("use-live" if observation.effective_policy == "pull-only" else "use-repository")
    repository = observation.repository
    captured = None
    if repository is None or observation.live is None:
        raise ValueError("Proposal requires successfully frozen file endpoints")
    if intent in ("use-live", "merge"):
        if capture is None:
            raise ValueError("Resolution requires a frozen Capture provider")
        captured = capture(observation)
        if intent == "merge":
            if merge is None:
                raise ValueError("Merge requires usable frozen ancestry")
            repository = merge(observation, captured)
        else:
            repository = captured
    primary = repository if repository != observation.repository else None
    if observation.effective_policy == "pull-only":
        return Proposal(repository, observation.live, primary, (), intent, captured,
                        "captured repository outcome")
    if observation.effective_policy == "both":
        if render is None:
            raise ValueError("Both-policy publication requires frozen outcome projection")
        live = render(observation, repository)
    else:
        live = observation.comparison_repository
    if live is None:
        raise ValueError("Proposal requires successfully frozen publication outcome")
    effects = []
    path = observation.live_path
    if isinstance(live, DirectoryChildPresent) and observation.chmod is not None:
        live = replace(live, executable=bool(int(observation.chmod, 8) & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)))
    # An executable-only change is a chmod effect, not a redundant payload write.
    content_changed = (
        isinstance(live, Missing) != isinstance(observation.live, Missing)
        or not isinstance(live, Missing) and live.content != observation.live.content
    )
    # Equal bytes can still hide drift when Capture reads state that push hooks
    # apply from the live file (e.g. a settings dump). Reapplying runs those hooks
    # without rewriting the file; without hooks nothing could change what Capture
    # reads. Mode drift has its own chmod effect, so only content counts here.
    reapply = False
    if (not content_changed and observation.effective_policy == "both" and reapply_hooks
            and capture is not None and observation.comparison_repository != observation.comparison_live
            and isinstance(live, (FilePresent, DirectoryChildPresent))):
        if captured is None:
            captured = capture(observation)
        reapply = isinstance(captured, Missing) or captured.content != repository.content
    if reapply:
        if path is None:
            raise ValueError("Publication requires a live endpoint path")
        effects.append(PublicationEffect("reapply", path))
    mode = None
    if isinstance(live, (FilePresent, DirectoryChildPresent)):
        mode = int(observation.chmod, 8) if observation.chmod is not None else None
        if isinstance(live, DirectoryChildPresent) and mode is None:
            mode = observation.live_mode
            if not isinstance(observation.live, DirectoryChildPresent) or live.executable != observation.live.executable:
                mode = directory_synced_file_mode(
                    destination_mode=observation.live_mode if observation.live_mode is not None else default_created_file_mode(),
                    source_mode=stat.S_IXUSR if live.executable else 0,
                )
    if content_changed:
        if path is None:
            raise ValueError("Publication requires a live endpoint path")
        effects.append(
            # The write applies the planned mode itself, so new content is never
            # readable with broader permissions until the chmod effect runs.
            PublicationEffect("write", path, content=live.content, mode=mode)
            if isinstance(live, (FilePresent, DirectoryChildPresent))
            else PublicationEffect("delete", path)
        )
    if mode is not None and (observation.live_mode != mode or isinstance(observation.live, Missing)):
        if path is None:
            raise ValueError("Publication requires a live endpoint path")
        effects.append(PublicationEffect("chmod", path, mode=mode))
    if (
        effects and observation.live_is_symlink
        and observation.file_symlink_mode == "prompt"
        and any(effect.kind in ("write", "chmod") for effect in effects)
    ):
        if not symlink_authorized:
            raise SyncPathError("symlink-authorization-required", "Live symlink replacement requires explicit authorization")
        # A mode-only publication must replace the link too, using frozen bytes.
        if not any(effect.kind == "write" for effect in effects):
            effects.insert(0, PublicationEffect("write", path, content=live.content, mode=mode))
    return Proposal(repository, live, primary, tuple(effects), intent, captured,
                    "three-way merged repository outcome" if intent == "merge" else
                    "captured repository outcome" if intent == "use-live" else "kept repository outcome")


def has_errors(diagnostics: tuple[Diagnostic, ...]) -> bool:
    return any(item.severity == "error" for item in diagnostics)


def supports_proposal(unit: Observation) -> bool:
    return (
        unit.state == "drifted" and not has_errors(unit.diagnostics)
        and unit.configured_policy in ("push-only", "push-only-delete", "pull-only", "both")
        and unit.effective_policy in ("push-only", "push-only-delete", "pull-only", "both")
    )


def allowed_intents(unit: Observation) -> tuple[ResolutionIntent, ...]:
    if not supports_proposal(unit):
        return ()
    if unit.effective_policy == "both":
        return ("use-repository", "use-live", "merge") if unit.base.status == "usable" else ("use-repository", "use-live")
    return ("use-live",) if unit.effective_policy == "pull-only" else ("use-repository",)


def default_intent(unit: Observation) -> ResolutionIntent | None:
    if not supports_proposal(unit):
        return None
    if unit.effective_policy == "both":
        if unit.base.status == "usable":
            return "merge"
        # Without Base evidence keep whichever side still has the file when
        # exactly one side is Missing; otherwise the repository is the declared config.
        repo_missing = isinstance(unit.repository, Missing)
        live_missing = isinstance(unit.live, Missing)
        if repo_missing != live_missing:
            return "use-live" if repo_missing else "use-repository"
        return "use-repository"
    return allowed_intents(unit)[0]


@dataclass(frozen=True)
class SessionRow:
    row_id: str
    kind: Literal["drift", "diagnostic"]
    included: bool
    observation: Observation
    allowed_commands: tuple[CommandName, ...]
    approved: bool = False
    symlink_authorized: bool = False
    editor_io: Literal["tty", "pipe"] = "tty"
    additional_changes: tuple[AdditionalEdit, ...] = ()
    proposal: Proposal | None = None
    allowed_intents: tuple[ResolutionIntent, ...] = ()
    intent: ResolutionIntent | None = None
    fallback_reason: str | None = None
    # Choosing a Resolution or editing the outcome is a decision; Approval only accepts a default.
    resolution_chosen: bool = False
    diagnostics: tuple[Diagnostic, ...] = ()
    # Package inputs whose dependency closure brought this row into scope; empty when requested.
    included_via: tuple[ResolvedPackageIdentity, ...] = ()

    @property
    def resolution_guessed(self) -> bool:
        """A no-Base default nobody has decided on yet; the fallback reason itself stays as evidence."""
        return self.fallback_reason is not None and not self.resolution_chosen

    @property
    def approvable(self) -> bool:
        """A no-op Proposal keeps the row visible as evidence but offers no Approval."""
        return "set-approval" in self.allowed_commands and not (self.proposal and self.proposal.noop)


@dataclass(frozen=True)
class AdditionalRow:
    row_id: str
    repo: str
    path: Path
    change: AdditionalEdit
    references: tuple[str, ...]
    approved: bool = False
    kind: Literal["additional"] = "additional"
    allowed_commands: tuple[CommandName, ...] = ("set-approval", "prepare-source-review")


@dataclass(frozen=True)
class BatchSetApproval:
    session_id: str
    revision: int
    approved: bool
    # The rows to select or clear, e.g. those a filtered Command Deck shows.
    row_ids: tuple[str, ...]


@dataclass(frozen=True)
class PrepareSourceReview:
    session_id: str
    revision: int
    row_id: str


@dataclass(frozen=True)
class SourceReview:
    row_id: str
    change: AdditionalEdit
    references: tuple[str, ...]


@dataclass(frozen=True)
class BatchApprovalChanged:
    approved: bool


@dataclass(frozen=True)
class AdditionalSourceResult:
    row_id: str
    repo: str
    path: Path
    status: Literal["pending", "would-apply", "applied", "execution-failed", "skipped", "interrupted"]
    diagnostics: tuple[Diagnostic, ...] = ()


@dataclass(frozen=True)
class CommitOption:
    """Commit Work for one repo: commit what the run writes there, once it succeeds."""

    repo: str
    # None on a detached HEAD.
    branch: str | None
    selected: bool = False


@dataclass(frozen=True)
class SetCommit:
    session_id: str
    revision: int
    repo: str
    selected: bool


@dataclass(frozen=True)
class CommitChanged:
    repo: str
    selected: bool


@dataclass(frozen=True)
class SessionView:
    session_id: str
    revision: int
    preview: bool
    terminal: bool
    observations: tuple[Observation, ...]
    rows: tuple[SessionRow | AuxiliaryRow | AdditionalRow, ...]
    allowed_commands: tuple[CommandName, ...]
    topology_diagnostics: tuple[Diagnostic, ...] = ()
    operation: str = "sync"
    commit_options: tuple[CommitOption, ...] = ()


@dataclass(frozen=True)
class SetIncluded:
    session_id: str
    revision: int
    row_id: str
    included: bool


@dataclass(frozen=True)
class Execute:
    session_id: str
    revision: int


@dataclass(frozen=True)
class Abort:
    session_id: str
    revision: int


@dataclass(frozen=True)
class AuthorizeSymlinkReplacement:
    session_id: str
    revision: int
    row_id: str


@dataclass(frozen=True)
class SetApproval:
    session_id: str
    revision: int
    row_id: str
    approved: bool


@dataclass(frozen=True)
class PrepareProposalReview:
    session_id: str
    revision: int
    row_id: str


@dataclass(frozen=True)
class SetResolutionIntent:
    session_id: str
    revision: int
    row_id: str
    intent: ResolutionIntent


@dataclass(frozen=True)
class RetryMaterialization:
    session_id: str
    revision: int
    row_id: str


@dataclass(frozen=True)
class Preview:
    session_id: str
    revision: int


@dataclass(frozen=True)
class EditProposal:
    session_id: str
    revision: int
    row_id: str


@dataclass(frozen=True)
class ProposalEdit:
    row_id: str
    status: Literal["saved", "unchanged", "cancelled", "command-failed", "materialization-failed", "unresolved-conflict"]
    diagnostics: tuple[Diagnostic, ...] = ()


SessionCommand = SetCommit | AuthorizeSymlinkReplacement | BatchSetApproval | PrepareSourceReview | EditProposal | SetResolutionIntent | RetryMaterialization | SetIncluded | SetApproval | PrepareProposalReview | Preview | Execute | Abort


@dataclass(frozen=True)
class ApprovalChanged:
    row_id: str
    approved: bool


@dataclass(frozen=True)
class ProposalReview:
    row_id: str
    proposal: Proposal | None
    diagnostics: tuple[Diagnostic, ...] = ()


@dataclass(frozen=True)
class InclusionChanged:
    row_id: str
    included: bool


@dataclass(frozen=True)
class SyncUnitResult:
    identity: str
    status: Literal["directly-in-sync", "pending", "noop", "excluded", "observation-failed", "converged", "would-converge", "applied", "would-apply", "execution-failed", "interrupted", "skipped", "not-converged"]
    diagnostics: tuple[Diagnostic, ...] = ()
    acknowledged: bool = False


@dataclass(frozen=True)
class SyncStepOutcome:
    """Semantic execution evidence without private plans or captured payloads."""

    stage: Literal["repository-apply", "live-publication", "repository-commit"]
    kind: str
    action: str
    scope: str
    scope_identity: str | None
    repo: str
    package_id: str | None
    status: str
    skip_reason: str | None = None
    exit_code: int | None = None
    error: str | None = None


def _step_scope_identity(step: ExecutionStep) -> str | None:
    if not step.repo_name:
        return None
    if step.scope_kind == "repo":
        return step.repo_name
    package = step.package_plan
    if step.package_id is None or package is None:
        return None
    if step.scope_kind == "target" and step.target_plan is not None:
        return repo_qualified_target_text(
            repo_name=step.repo_name,
            package_id=step.package_id,
            bound_profile=package.bound_profile,
            target_name=step.target_plan.target_name,
        ) + (f"/{step.target_plan.child_path}" if step.target_plan.child_path is not None else "")
    return f"{step.repo_name}:{package_ref_text(package_id=step.package_id, bound_profile=package.bound_profile)}"


def _commit_step_outcome(repo: str, status: str, *, skip_reason: str | None = None,
                         error: str | None = None) -> SyncStepOutcome:
    return SyncStepOutcome("repository-commit", "commit", "commit", "repo", repo, repo, None, status,
                           skip_reason=skip_reason, error=error)


@dataclass(frozen=True)
class SyncResult:
    status: Literal["completed", "incomplete", "failed", "aborted"]
    units: tuple[SyncUnitResult, ...]
    diagnostics: tuple[Diagnostic, ...] = ()
    steps: tuple[SyncStepOutcome, ...] = ()
    additional_changes: tuple[AdditionalSourceResult, ...] = ()
    commits: tuple[RepositoryCommit, ...] = ()

    @property
    def exit_code(self) -> int:
        return {"completed": 0, "incomplete": 1, "failed": 1, "aborted": 130}[
            self.status
        ]


@dataclass(frozen=True)
class CommandAccepted:
    view: SessionView
    result: CommitChanged | InclusionChanged | ApprovalChanged | BatchApprovalChanged | SourceReview | ProposalReview | ProposalEdit | SyncResult


@dataclass(frozen=True)
class CommandRejected:
    view: SessionView
    reason: Literal[
        "invalid",
        "foreign-session",
        "stale",
        "unknown-row",
        "disallowed",
        "preview",
        "terminal",
    ]
    diagnostics: tuple[Diagnostic, ...] = ()


@dataclass(frozen=True)
class SessionOpenFailed:
    diagnostic: Diagnostic
    # First line of failed command output; human-only, never projected into JSON.
    output_line: str | None = None


@dataclass(frozen=True)
class SessionOpened:
    view: SessionView


@dataclass(frozen=True)
class SessionChanged:
    view: SessionView


@dataclass(frozen=True)
class SessionFinished:
    view: SessionView
    result: SyncResult


SessionEvent = SessionOpened | SessionChanged | SessionFinished | StepStarted | StepFinished
SessionEventSink = Callable[[SessionEvent], None]


class ProposalSession:
    """Shared frozen workset, editing, approval and command lifecycle."""

    operation = "sync"
    additional_default_approval = False
    offers_commit = True

    @staticmethod
    def _observe(context, scope, **kwargs):
        return observe_scope(context, scope, **kwargs)

    def _prepare_workset(self) -> None:
        pass

    def _prepare_fixed_direction_workset(self, drift_intent: str | None) -> None:
        """Start an opt-out workset whose direction is fixed for every drifted row."""
        self._view = replace(self.view, rows=tuple(
            replace(row, approved=row.kind == "drift", intent=drift_intent if row.kind == "drift" else None,
                    allowed_intents=(), fallback_reason=None,
                    allowed_commands=tuple(command for command in row.allowed_commands
                                           if command != "set-resolution-intent"))
            if isinstance(row, SessionRow)
            else replace(row, included=True)
            if isinstance(row, AuxiliaryRow) and "set-included" in row.allowed_commands
            else row
            for row in self.view.rows
        ))
        self._invalidate_inputs({row.row_id for row in self.view.rows if isinstance(row, SessionRow)},
                                clear_cache=False)

    def __init__(
        self,
        observations: tuple[Observation, ...],
        *,
        preview: bool,
        auxiliary: tuple[AuxiliaryRow, ...] = (),
        event_sink: SessionEventSink | None = None,
        included_via: Mapping[str, tuple[ResolvedPackageIdentity, ...]] | None = None,
    ) -> None:
        included_via = included_via or {}
        self._command_operation = CommandOperation()
        self._editor_operation = CommandOperation()
        self._additional_candidates = {}
        self._additional_approvals = {}
        self._additional_results = ()
        self._edited_outcomes = {}
        self._proposal_generations = {}
        self._editor_preimages = {}
        self._editor_input_errors = {}
        self._captures = {}
        self._acknowledged = set()
        self._checkpoint_warnings = {}
        self._obsolete_bases = ()
        self._root_inputs = {}
        # A comparison Render is already a valid projection of these frozen
        # repository inputs. Reusing it also avoids volatile provider reruns.
        self._renders = {
            (unit.identity, unit.repository): unit.comparison_repository
            for unit in observations
            if (unit.compare_repo == "render" or unit.effective_policy == "push-only") and unit.comparison_repository is not None
        }
        self._event_sink = event_sink
        self._stream_output = False
        self._operation_lock: OperationLock | None = None
        self._view = SessionView(
            uuid4().hex,
            0,
            preview,
            False,
            observations,
            tuple(
                SessionRow(
                    unit.identity.canonical,
                    "drift"
                    if unit.state == "drifted" and not has_errors(unit.diagnostics)
                    else "diagnostic",
                    unit.state == "drifted" and not has_errors(unit.diagnostics),
                    unit,
                    (("authorize-symlink-replacement",) if unit.live_is_symlink and unit.file_symlink_mode == "prompt" and unit.effective_policy in ("push-only", "both") else ()) + ("set-included", "set-approval", "prepare-proposal-review", "set-resolution-intent", "retry-materialization", "edit-proposal")
                    if supports_proposal(unit)
                    else ("set-included",)
                    if unit.state == "drifted" and not has_errors(unit.diagnostics)
                    else (),
                    allowed_intents=allowed_intents(unit),
                    intent=default_intent(unit),
                    fallback_reason=(unit.base.reason or "absent")
                    if supports_proposal(unit) and unit.effective_policy == "both" and unit.base.status != "usable" else None,
                    included_via=included_via.get(replace(unit.identity, child_path=None).canonical, ()),
                )
                for unit in observations
                if unit.state != "directly-in-sync" or unit.diagnostics
            ) + tuple(replace(row, included_via=included_via.get(row.scope, ())) for row in auxiliary),
            ("batch-set-approval", "preview", "abort") if preview else ("batch-set-approval", "preview", "execute", "abort"),
            operation=self.operation,
        )

    @classmethod
    def open(
        cls,
        context: PlanningContext,
        scope: ResolvedSyncScope,
        *,
        preview: bool = False,
        run_noop: bool = False,
        event_sink: SessionEventSink | None = None,
        sink: ProgressSink | None = None,
        stream_output: bool = False,
        commit: bool = False,
    ) -> SyncSession | SessionOpenFailed:
        with command_operation() as operation, ExitStack() as resources:
            lock = None
            try:
                if not preview:
                    lock = resources.enter_context(
                        OperationLock.acquire(context.tracked_state.state_root)
                    )
                resolved_inputs = _resolve_inputs(scope, operation=cls.operation)
                observed = cls._observe(
                    context, scope, preview=preview, run_noop=run_noop, resolved_inputs=resolved_inputs,
                    operation=cls.operation, sink=sink,
                )
                observations = observed.observations

                # Keep no-write completion metadata even when a direction is
                # removed; only Guard-admitted hooks may activate at execution.
                selected_inputs = {}
                for candidates in resolved_inputs[1].values():
                    for item in candidates:
                        key = item.selection.identity
                        if key not in selected_inputs:
                            selected_inputs[key] = replace(item, target_metadata=[])
                # Directional candidates retain empty hook scopes, but their
                # policy filtering must not reorder the frozen target workset.
                for item, target in resolved_inputs[0].values():
                    selected_inputs[item.selection.identity].target_metadata.append(target)
                publication_metadata = retain_directional_hooks(prepare_publication(
                    tuple(selected_inputs.values()), file_symlink_mode=context.config.file_symlink_mode,
                    dir_symlink_mode=context.config.dir_symlink_mode,
                ), observed.hook_scopes["push"])
                # Guards gate automatic flow, not deliberate Editor repository
                # writes. Freeze all destination-stage hooks; execution activates
                # them only for actual Primary writes or admitted auxiliary work.
                repository_metadata = prepare_repository_apply(tuple(selected_inputs.values()))
                auxiliary = plan_auxiliary(
                    resolved_inputs[0], observed.directional,
                    {"pull": retain_directional_hooks(repository_metadata, observed.hook_scopes["pull"]),
                     "push": publication_metadata},
                    dir_symlink_mode=context.config.dir_symlink_mode,
                    command_runtime=context.projection.command_runtime, run_noop=run_noop, sink=sink,
                ) + guard_skip_rows(observed.guard_skips)
                commit_trees = cls._probe_commit_trees(context, observations) if cls.offers_commit else {}
                obsolete_bases, maintenance_failures = (
                    cls._freeze_obsolete_bases(context, observed) if not preview else ((), {})
                )
                # A failure only concerns the units of the directory it was reading.
                observations = tuple(
                    replace(unit, diagnostics=(*unit.diagnostics, failure))
                    if (failure := maintenance_failures.get(replace(unit.identity, child_path=None)))
                    else unit
                    for unit in observations
                )
            except OperationBusy as exc:
                return SessionOpenFailed(Diagnostic("operation-busy", str(exc)))
            except OperationLockError as exc:
                return SessionOpenFailed(Diagnostic("operation-lock-failed", str(exc)))
            except SyncBaseStoreError as exc:
                return SessionOpenFailed(Diagnostic("base-failed", str(exc)))
            except GuardPlanningError as exc:
                # Guard output may contain managed content; report typed command evidence only.
                scope = GuardSkip(exc.scope_kind, exc.repo_name, exc.package_id, exc.bound_profile,
                                  exc.target_name, exc.path_rule_pattern).scope_label
                pattern = f" (path rule: {exc.path_rule_pattern})" if exc.path_rule_pattern is not None else ""
                return SessionOpenFailed(Diagnostic(
                    "planning-failed", f"{scope}{pattern} {exc.hook_name} failed with exit {exc.exit_code}",
                ), output_line=exc.output_line)
            except PlanningCommandError as exc:
                return SessionOpenFailed(Diagnostic("planning-failed", str(exc)), output_line=exc.output_line)
            except ValueError as exc:
                return SessionOpenFailed(Diagnostic("planning-failed", str(exc)))
            except (KeyboardInterrupt, InterruptedError):
                return SessionOpenFailed(
                    Diagnostic("interrupted", "Sync opening interrupted")
                )
            except OSError as exc:
                return SessionOpenFailed(Diagnostic("observation-failed", str(exc)))
            finally:
                # Observation starts the bar once its unit count is known.
                if sink is not None:
                    sink.close()
            resolved_inputs = (observed.inputs, resolved_inputs[1])
            session = cls(observations, preview=preview, auxiliary=auxiliary, event_sink=event_sink,
                          included_via=scope.included_via)
            session._obsolete_bases = obsolete_bases
            session._commit_trees = commit_trees
            session._root_inputs = {_identity(target): (item, target) for item in selected_inputs.values() for target in item.target_metadata}
            session._run_noop = run_noop
            session._stream_output = stream_output
            session._operation_lock = lock
            session._context = context
            session._command_operation = operation
            session._resolved_inputs = resolved_inputs[0]
            session._view = replace(session.view, commit_options=tuple(
                CommitOption(repo, tree.branch, commit) for repo, tree in commit_trees.items()
            ), rows=tuple(
                replace(row, editor_io=resolved_inputs[0][row.observation.identity][1].editor.io)
                if isinstance(row, SessionRow) and supports_proposal(row.observation) else row
                for row in session.view.rows
            ))
            for observation in observations:
                if not supports_proposal(observation):
                    continue
                item, metadata = resolved_inputs[0][observation.identity]
                try:
                    session._editor_preimages[observation.identity] = freeze_additional_sources(
                        metadata=metadata, repo_root=item.repo.root,
                        primary_paths=tuple(unit.repository_path for unit in observations),
                    )
                except (ValueError, OSError) as exc:
                    session._editor_input_errors[observation.identity] = str(exc)
            session._frozen_bases = {
                observation.identity: FrozenBaseUnit(
                    BaseUnit(observation.identity, observation.configured_policy),
                    observation.repository,
                )
                for observation in observations
                if observation.configured_policy in ("pull-only", "both") and observation.repository is not None
            }
            session._publication_metadata = freeze_child_metadata(publication_metadata, observations)
            session._repository_metadata = freeze_child_metadata(repository_metadata, observations)
            session._prepare_workset()
            # Adapter exceptions are programming failures, not planning results.
            session._emit(SessionOpened(session.view))
            resources.pop_all()
            return session

    @staticmethod
    def _probe_commit_trees(context, observations) -> dict[str, GitWorkTree]:
        """Git work trees of repos with drift, in configured repo order."""
        drifted = {unit.identity.repo for unit in observations if unit.state == "drifted"}
        trees = {}
        for repo in context.config.ordered_repos:
            if repo.name in drifted:
                tree = probe_work_tree(repo.path, context.projection.command_runtime)
                if tree is not None:
                    trees[repo.name] = tree
        return trees

    @staticmethod
    def _freeze_obsolete_bases(context, observed) -> tuple[tuple, dict]:
        """Obsolete child Bases of complete censuses, and per-directory read failures."""
        candidates, failures = [], {}
        for identity, item, census, complete in observed.directory_censuses:
            if not complete:
                continue
            state_root, state_key = context.tracked_state.state_root, item.repo.config.state_key
            try:
                # Absent storage holds no obsolete Base; do not create it.
                if not SyncBaseStore.exists(state_root, state_key):
                    continue
                with SyncBaseStore.open(state_root, state_key, create=False) as store:
                    stored = store.target_records(identity.canonical.encode())
            except (SyncBaseStoreError, OSError) as exc:
                failures[identity] = Diagnostic("base-maintenance-failed", str(exc), severity="warning")
                continue
            prefix = (identity.canonical + "/").encode()
            present = {prefix + path.encode() for path, _ in census.entries}
            candidates.extend(
                (identity, state_key, record.identity) for record in stored
                if record.identity.startswith(prefix) and record.identity not in present
            )
        return tuple(candidates), failures

    def request_editor_cancel(self) -> None:
        self._editor_operation.request_cancel()

    def request_cancel(self) -> None:
        """Cancel owned commands without racing a session view mutation."""
        self._command_operation.request_cancel()
        self._editor_operation.request_cancel()

    def check_cancelled(self) -> None:
        self._command_operation.check_cancelled()

    @property
    def view(self) -> SessionView:
        diagnostics = self._structural_conflicts()
        if diagnostics != self._view.topology_diagnostics:
            self._view = replace(self._view, topology_diagnostics=diagnostics)
        return self._view

    def _emit(self, event: SessionEvent) -> None:
        if self._event_sink is not None:
            self._event_sink(event)

    def dispatch(self, command: SessionCommand) -> CommandAccepted | CommandRejected:
        with command_operation(self._command_operation):
            return self._dispatch(command)

    def _dispatch(self, command: SessionCommand) -> CommandAccepted | CommandRejected:
        view = self.view
        if type(command) not in (SetCommit, AuthorizeSymlinkReplacement, BatchSetApproval, PrepareSourceReview, EditProposal, SetResolutionIntent, RetryMaterialization, SetIncluded, SetApproval, PrepareProposalReview, Preview, Execute, Abort):
            return CommandRejected(view, "invalid")
        if view.terminal:
            return CommandRejected(view, "terminal")
        if type(command.session_id) is not str or type(command.revision) is not int:
            return CommandRejected(view, "invalid")
        if command.session_id != view.session_id:
            return CommandRejected(view, "foreign-session")
        if command.revision != view.revision:
            return CommandRejected(view, "stale")
        if isinstance(command, BatchSetApproval):
            if type(command.approved) is not bool or type(command.row_ids) is not tuple or not all(
                type(row_id) is str for row_id in command.row_ids
            ):
                return CommandRejected(view, "invalid")
            scope = set(command.row_ids)
            if not scope <= {row.row_id for row in view.rows}:
                return CommandRejected(view, "unknown-row")
            # Establish every final row state before any provider sees inputs.
            changed_references = set()
            for row in view.rows:
                if isinstance(row, AdditionalRow) and row.row_id in scope:
                    if row.approved != command.approved:
                        changed_references.update(row.references)
                    self._additional_approvals[row.change.path] = command.approved
            for row in view.rows:
                if isinstance(row, SessionRow) and row.row_id in changed_references:
                    self._clear_input_cache(row)
            # Unapprovable units, such as no-ops, keep their Approval state.
            approval_scope = {row.row_id for row in view.rows
                              if row.row_id in scope and isinstance(row, SessionRow) and row.approvable}
            rows = tuple(
                replace(row, approved=command.approved)
                if row.row_id in approval_scope
                else row if row.row_id not in scope
                else replace(row, approved=command.approved)
                if isinstance(row, AdditionalRow)
                else replace(row, included=command.approved)
                if isinstance(row, AuxiliaryRow) and "set-included" in row.allowed_commands
                else row
                for row in view.rows
            )
            self._view = replace(view, rows=rows)
            # Approval alone never changes a Proposal, as with SetApproval: rematerialize
            # only rows fed by a changed shared source, and approved rows still lacking one.
            self._invalidate_inputs(changed_references | {
                row.row_id for row in rows
                if row.row_id in approval_scope and row.approved and row.proposal is None
            }, clear_cache=False)
            self._view = replace(self.view, revision=view.revision + 1)
            self._emit(SessionChanged(self.view))
            return CommandAccepted(self.view, BatchApprovalChanged(command.approved))
        if isinstance(command, (SetApproval, PrepareSourceReview)):
            if type(command.row_id) is not str or (
                isinstance(command, SetApproval) and type(command.approved) is not bool
            ):
                return CommandRejected(view, "invalid")
            source = next((row for row in view.rows if row.row_id == command.row_id), None)
            if source is None:
                return CommandRejected(view, "unknown-row")
            if isinstance(source, AdditionalRow):
                if isinstance(command, PrepareSourceReview):
                    return CommandAccepted(view, SourceReview(source.row_id, source.change, source.references))
                changed = source.approved != command.approved
                self._additional_approvals[source.change.path] = command.approved
                self._refresh_additional_rows()
                if changed:
                    self._invalidate_inputs(set(source.references))
                self._view = replace(self.view, revision=view.revision + 1)
                self._emit(SessionChanged(self.view))
                return CommandAccepted(self.view, ApprovalChanged(source.row_id, command.approved))
            if isinstance(command, PrepareSourceReview):
                return CommandRejected(view, "disallowed")
        if isinstance(command, EditProposal):
            if type(command.row_id) is not str:
                return CommandRejected(view, "invalid")
            row = next((row for row in view.rows if row.row_id == command.row_id), None)
            if row is None:
                return CommandRejected(view, "unknown-row")
            if "edit-proposal" not in row.allowed_commands:
                return CommandRejected(view, "disallowed")
            return self._edit_proposal(row)
        if isinstance(command, SetCommit):
            if type(command.repo) is not str or type(command.selected) is not bool:
                return CommandRejected(view, "invalid")
            if command.repo not in {option.repo for option in view.commit_options}:
                return CommandRejected(view, "unknown-row")
            self._view = replace(view, revision=view.revision + 1, commit_options=tuple(
                replace(option, selected=command.selected) if option.repo == command.repo else option
                for option in view.commit_options
            ))
            self._emit(SessionChanged(self.view))
            return CommandAccepted(self.view, CommitChanged(command.repo, command.selected))
        if isinstance(command, SetIncluded):
            if type(command.row_id) is not str or type(command.included) is not bool:
                return CommandRejected(view, "invalid")
            row = next((row for row in view.rows if row.row_id == command.row_id), None)
            if row is None:
                return CommandRejected(view, "unknown-row")
            if "set-included" not in row.allowed_commands:
                return CommandRejected(view, "disallowed")
            self._view = replace(
                view,
                revision=view.revision + 1,
                rows=tuple(
                    replace(item, included=command.included)
                    if item.row_id == row.row_id
                    else item
                    for item in view.rows
                ),
            )
            result = CommandAccepted(
                self.view, InclusionChanged(row.row_id, command.included)
            )
            self._emit(SessionChanged(self.view))
            return result
        if isinstance(command, (AuthorizeSymlinkReplacement, SetApproval, PrepareProposalReview, SetResolutionIntent, RetryMaterialization)):
            if type(command.row_id) is not str or (
                isinstance(command, SetApproval) and type(command.approved) is not bool
            ):
                return CommandRejected(view, "invalid")
            row = next((row for row in view.rows if row.row_id == command.row_id), None)
            if row is None:
                return CommandRejected(view, "unknown-row")
            name = {
                AuthorizeSymlinkReplacement: "authorize-symlink-replacement",
                SetApproval: "set-approval", PrepareProposalReview: "prepare-proposal-review",
                SetResolutionIntent: "set-resolution-intent", RetryMaterialization: "retry-materialization",
            }[type(command)]
            if name not in row.allowed_commands or (isinstance(command, SetApproval) and not row.approvable):
                return CommandRejected(view, "disallowed")
            if isinstance(command, AuthorizeSymlinkReplacement):
                row = replace(row, symlink_authorized=True, proposal=None, diagnostics=())
            if isinstance(command, SetResolutionIntent):
                if command.intent not in ("use-repository", "use-live", "merge", "editor"):
                    return CommandRejected(view, "invalid")
                if command.intent not in row.allowed_intents:
                    return CommandRejected(view, "disallowed")
                if command.intent == "editor":
                    # The kept edit returns as a new Proposal; its earlier generation never revives.
                    repository, _generation = self._edited_outcomes[row.row_id]
                    self._edited_outcomes[row.row_id] = (repository, self._next_generation(row.row_id))
                row = replace(row, intent=command.intent, resolution_chosen=True, proposal=None, diagnostics=())
            if isinstance(command, RetryMaterialization):
                row = replace(row, proposal=None)
            proposal, diagnostics = row.proposal, row.diagnostics
            approved = command.approved if isinstance(command, SetApproval) else row.approved
            if proposal is None and (approved or isinstance(command, (PrepareProposalReview, RetryMaterialization))):
                try:
                    proposal = self._materialize_row(row)
                    self.check_cancelled()
                    diagnostics = ()
                except (KeyboardInterrupt, InterruptedError):
                    self.request_cancel()
                    diagnostics = (Diagnostic("interrupted", "Materialization interrupted"),)
                    approved = False
                except CaptureError as exc:
                    diagnostics = (Diagnostic("capture-failed", str(exc)),)
                    approved = False
                except (ReconciliationConflict, ReconciliationFailed) as exc:
                    diagnostics = (reconciliation_diagnostic(exc, self._captures.get(row.observation.identity)),)
                    approved = False
                except (ValueError, OSError) as exc:
                    diagnostics = (Diagnostic(exc.code if isinstance(exc, SyncPathError) else "materialization-failed", str(exc)),)
                    approved = False
            updated = replace(row, approved=approved and not (proposal and proposal.noop),
                              proposal=proposal, diagnostics=diagnostics)
            self._view = replace(view, revision=view.revision + 1, rows=tuple(
                updated if item.row_id == row.row_id else item for item in view.rows
            ))
            self._emit(SessionChanged(self.view))
            return CommandAccepted(self.view,
                ApprovalChanged(row.row_id, updated.approved) if isinstance(command, SetApproval)
                else ProposalReview(row.row_id, proposal, diagnostics))
        if isinstance(command, Execute) and view.preview:
            return CommandRejected(view, "preview")
        if isinstance(command, (Preview, Execute)) and any(
            isinstance(row, SessionRow) and row.approved and row.included and row.proposal is None
            for row in view.rows
        ):
            return CommandRejected(view, "disallowed")
        if isinstance(command, (Preview, Execute)) and self._structural_conflicts():
            return CommandRejected(view, "disallowed", self._structural_conflicts())
        if isinstance(command, Preview):
            return CommandAccepted(view, self._result(preview=True))
        result = self._finish(aborted=isinstance(command, Abort))
        return CommandAccepted(self.view, result)


    def _structural_conflicts(self) -> tuple[Diagnostic, ...]:
        rows = {row.observation.identity: row for row in self._view.rows if isinstance(row, SessionRow)}
        failures = []
        for identity, row in rows.items():
            if not (row.included and row.approved and row.proposal is not None):
                continue
            for repository, blockers in ((True, row.observation.repository_blockers), (False, row.observation.live_blockers)):
                writer = (isinstance(row.proposal.primary_source_change, DirectoryChildPresent) if repository
                          else any(effect.kind == "write" for effect in row.proposal.publication_effects))
                if not writer:
                    continue
                for path in blockers:
                    blocker = rows.get(replace(identity, child_path=path))
                    deletion = blocker is not None and blocker.included and blocker.approved and blocker.proposal is not None and (
                        isinstance(blocker.proposal.primary_source_change, Missing) if repository
                        else any(effect.kind == "delete" for effect in blocker.proposal.publication_effects))
                    if not deletion:
                        failures.append(Diagnostic("structural-approval-required" if path in row.observation.managed_children else "structural-conflict",
                                                   f"{identity.canonical}: requires deletion of {path}"))
        return tuple(failures)

    def _row_additional(self, row: SessionRow) -> tuple[AdditionalEdit, ...]:
        return tuple(
            self._additional_candidates[path]
            for path in self._editor_preimages.get(row.observation.identity, {})
            if path in self._additional_candidates
        )

    def _references(self, paths: set[Path]) -> set[str]:
        return {
            row.row_id for row in self.view.rows
            if isinstance(row, SessionRow)
            and paths.intersection(self._editor_preimages.get(row.observation.identity, {}))
        }

    def _refresh_additional_rows(self) -> None:
        proposals = []
        for row in self.view.rows:
            if isinstance(row, AdditionalRow):
                continue
            if isinstance(row, SessionRow):
                changes = self._row_additional(row)
                row = replace(
                    row, additional_changes=changes,
                    proposal=replace(row.proposal, additional_changes=changes) if row.proposal else None,
                )
            proposals.append(row)
        proposals = tuple(proposals)
        sources = []
        for path, change in self._additional_candidates.items():
            references = tuple(row.row_id for row in proposals
                               if isinstance(row, SessionRow) and change in self._row_additional(row))
            owner = next(row for row in proposals if row.row_id in references)
            item, _ = self._resolved_inputs[owner.observation.identity]
            relative = path.relative_to(item.repo.root)
            repo = owner.observation.identity.repo
            sources.append(AdditionalRow(
                f"{repo}:additional/{relative.as_posix()}", repo, relative, change,
                references, self._additional_approvals.get(path, self.additional_default_approval),
            ))
        repo_order = list(dict.fromkeys(unit.identity.repo for unit in self.view.observations))
        sources.sort(key=lambda row: (repo_order.index(row.repo), row.path.as_posix()))
        self._view = replace(self.view, rows=proposals + tuple(sources))

    def _input_bytes(self, observation: Observation) -> dict[Path, bytes]:
        return {
            path: self._additional_candidates[path].candidate
            if path in self._additional_candidates and self._additional_approvals.get(path, self.additional_default_approval)
            else before
            for path, before in self._editor_preimages.get(observation.identity, {}).items()
        }

    def _clear_input_cache(self, row: SessionRow) -> None:
        identity = row.observation.identity
        self._captures.pop(identity, None)
        self._renders = {key: value for key, value in self._renders.items() if key[0] != identity}

    def _invalidate_inputs(self, references: set[str], *, clear_cache: bool = True) -> None:
        rows = []
        for row in self.view.rows:
            if not isinstance(row, SessionRow) or row.row_id not in references:
                rows.append(row)
                continue
            if clear_cache:
                self._clear_input_cache(row)
            # Unapproval is not a successful retry: retain unresolved diagnostics.
            row = replace(row, proposal=None)
            if row.approved:
                try:
                    self.check_cancelled()
                    proposal = self._materialize_row(row)
                    row = replace(row, approved=not proposal.noop, proposal=proposal, diagnostics=())
                    self.check_cancelled()
                except (KeyboardInterrupt, InterruptedError):
                    row = replace(row, approved=False, proposal=None,
                                  diagnostics=(Diagnostic("interrupted", "Materialization interrupted"),))
                except (ReconciliationConflict, ReconciliationFailed) as exc:
                    row = replace(row, approved=False, proposal=None, diagnostics=(
                        reconciliation_diagnostic(exc, self._captures.get(row.observation.identity)),))
                except (CaptureError, ValueError, OSError) as exc:
                    code = ("capture-failed" if isinstance(exc, CaptureError) else
                            exc.code if isinstance(exc, SyncPathError) else "materialization-failed")
                    row = replace(row, approved=False, proposal=None, diagnostics=(Diagnostic(code, str(exc)),))
            rows.append(row)
        self._view = replace(self.view, rows=tuple(rows))

    def _next_generation(self, row_id: str) -> int:
        generation = self._proposal_generations.get(row_id, 0) + 1
        self._proposal_generations[row_id] = generation
        return generation

    def _materialize_row(self, row: SessionRow) -> Proposal:
        if row.intent != "editor":
            observation = row.observation
            if observation.effective_policy == "push-only":
                observation = replace(observation, comparison_repository=self._render(observation, observation.repository))
            proposal = materialize(
                observation, intent=row.intent, capture=self._capture,
                render=self._render, merge=self._merge, symlink_authorized=row.symlink_authorized,
                reapply_hooks=self._reapplies(observation),
            )
            return self._finalize_proposal(observation, replace(
                proposal, generation=self._next_generation(row.row_id), additional_changes=self._row_additional(row)))
        repository, generation = self._edited_outcomes[row.row_id]
        observation = row.observation
        if self._unresolved_conflict(row, repository):
            # Blocked like the merge conflict it came from; the saved text stays the
            # conflict evidence, so the next Editor run resumes from it.
            raise ReconciliationConflict("Saved edit still contains unresolved conflict blocks", repository)
        if observation.effective_policy in ("push-only", "both"):
            live = self._render(observation, repository)
        elif observation.effective_policy == "push-only-delete":
            live = Missing()
        else:
            live = observation.live
        proposal = materialize(
            replace(observation, repository=repository, comparison_repository=live),
            intent="use-repository", capture=self._capture, render=lambda *_: live,
            symlink_authorized=row.symlink_authorized, reapply_hooks=self._reapplies(observation),
        )
        return self._finalize_proposal(observation, replace(
            proposal, primary_source_change=repository if repository != observation.repository else None,
            intent="editor", generation=generation, reconciliation="edited repository outcome",
            additional_changes=self._row_additional(row),
        ))

    def _reapplies(self, observation: Observation) -> bool:
        return observation.effective_policy == "both" and activates_hooks(
            self._publication_metadata, observation.identity, direction="push")

    def _editor_initial(self, row: SessionRow) -> SyncBasePayload:
        """The selected Resolution's repository outcome, which the Editor opens."""
        if row.proposal is not None:
            return row.proposal.repository
        # A merge conflict has no Proposal; its zdiff3 output is what the user resolves.
        # A presence conflict has no merge text, so it falls back to the repository source.
        if (conflict := conflict_diagnostic(row)) is not None:
            return conflict.conflict if conflict.conflict is not None else row.observation.repository
        # A reselected edit stays unmaterialized until approved or reviewed.
        if row.intent == "editor":
            return self._edited_outcomes[row.row_id][0]
        # Unreviewed Capture-based outcomes are materialized now; a failure stops this
        # attempt, and the next one recovers from the repository source below.
        if row.intent in ("use-live", "merge") and not row.diagnostics:
            try:
                return self._materialize_row(row).repository
            except ReconciliationConflict as exc:
                return exc.conflict if exc.conflict is not None else row.observation.repository
        return row.observation.repository

    @staticmethod
    def _select_edit(row: SessionRow) -> SessionRow:
        # Push/Pull rows carry no Resolution choice, so Edited is not offered as one.
        choosable = "set-resolution-intent" in row.allowed_commands and "editor" not in row.allowed_intents
        return replace(row, intent="editor", resolution_chosen=True,
                       allowed_intents=row.allowed_intents + (("editor",) if choosable else ()))

    def _unresolved_conflict(self, row: SessionRow, repository: SyncBasePayload) -> bool:
        present = (FilePresent, DirectoryChildPresent)
        if not isinstance(repository, present):
            return False
        sources = (row.observation.repository, self._captures.get(row.observation.identity))
        return bool(unresolved_conflict_blocks(
            repository.content, known_sources=tuple(source.content for source in sources if isinstance(source, present)),
        ))

    def _edit_proposal(self, row: SessionRow) -> CommandAccepted:
        # The idle operation also holds cancellation admitted before dispatch.
        operation = self._editor_operation
        status, diagnostics = "saved", ()
        updated = row
        # From launching the Editor until a save commits, a failure saved nothing:
        # like cancellation it leaves the Proposal and Approval intact, so the next
        # attempt reopens the same outcome. Initial materialization failures and
        # failed saved edits still mark the row.
        unsaved_attempt = False
        prior_view = self.view
        prior_captures, prior_renders = dict(self._captures), dict(self._renders)
        prior_generations = dict(self._proposal_generations)
        prior_edited = self._edited_outcomes.get(row.row_id)
        prior_candidates = dict(self._additional_candidates)
        prior_additional = self._row_additional(row)
        try:
            with command_operation(operation):
                operation.check_cancelled()
                self.check_cancelled()
                item, metadata = self._resolved_inputs[row.observation.identity]
                if row.observation.identity in self._editor_input_errors:
                    raise ValueError(self._editor_input_errors[row.observation.identity])
                initial = self._editor_initial(row)
                unsaved_attempt = True
                output = edit_sources(
                    observation=row.observation, initial=initial,
                    metadata=metadata, repo_root=item.repo.root,
                    additional=prior_additional,
                    preimages=self._editor_preimages[row.observation.identity],
                )
                operation.check_cancelled()
                if output.exit_code in (130, 143):
                    raise InterruptedError("Editor cancelled")
                if output.exit_code:
                    status = "command-failed"
                    diagnostics = (Diagnostic("editor-command-failed", f"Editor exited with status {output.exit_code}"),)
                elif output.repository == initial and output.additional == prior_additional:
                    # Quitting without changes is not an edit; an identical Edited
                    # choice would only duplicate the current Resolution.
                    status = "unchanged"
                else:
                    unsaved_attempt = False
                    previous = row.proposal
                    generation = self._next_generation(row.row_id)
                    # Commit saved sources before projection: a failed Render must
                    # remain retryable from the user's edits, not automatic intent.
                    self._edited_outcomes[row.row_id] = (output.repository, generation)
                    row = self._select_edit(row)
                    for path in self._editor_preimages[row.observation.identity]:
                        self._additional_candidates.pop(path, None)
                    self._additional_candidates.update({change.path: change for change in output.additional})
                    # Staged unapproved bytes are review metadata, not provider inputs.
                    changed_input_paths = {
                        path for path in prior_candidates.keys() | self._additional_candidates.keys()
                        if self._additional_approvals.get(path, self.additional_default_approval)
                        and prior_candidates.get(path) != self._additional_candidates.get(path)
                    }
                    self._refresh_additional_rows()
                    affected = self._references(changed_input_paths)
                    self._invalidate_inputs(affected - {row.row_id})
                    if row.row_id in affected:
                        self._clear_input_cache(row)
                    # Primary bytes already key Render reuse; unchanged Additional
                    # inputs must not evict successful Render or Capture results.
                    if previous is not None and output.repository == previous.repository and row.row_id not in affected:
                        proposal = replace(
                            previous, intent="editor", generation=generation,
                            reconciliation="edited repository outcome",
                            additional_changes=output.additional,
                        )
                    else:
                        proposal = self._materialize_row(row)
                    operation.check_cancelled()
                    updated = replace(row, approved=row.approved and not proposal.noop, proposal=proposal,
                                      diagnostics=(), additional_changes=output.additional)
        except (KeyboardInterrupt, InterruptedError):
            status = "cancelled"
            diagnostics = (Diagnostic("editor-cancelled", "Editor cancelled"),)
            if prior_edited is None:
                self._edited_outcomes.pop(row.row_id, None)
            else:
                self._edited_outcomes[row.row_id] = prior_edited
            self._additional_candidates = prior_candidates
            self._view = prior_view
            self._captures, self._renders = prior_captures, prior_renders
            self._proposal_generations = prior_generations
        except ReconciliationConflict as exc:
            status = "unresolved-conflict"
            diagnostics = (reconciliation_diagnostic(exc, self._captures.get(row.observation.identity)),)
        except EditorCommandFailed as exc:
            status = "command-failed"
            diagnostics = (Diagnostic("editor-command-failed", str(exc)),)
        except (ValueError, OSError) as exc:
            status = "materialization-failed"
            diagnostics = (Diagnostic("editor-materialization-failed", str(exc)),)
        finally:
            self._editor_operation = CommandOperation()
        if status not in ("saved", "unchanged", "cancelled") and not unsaved_attempt:
            updated = replace(row, approved=False, proposal=None, diagnostics=diagnostics,
                              additional_changes=self._row_additional(row))
        self._view = replace(self.view, revision=self.view.revision + 1, rows=tuple(
            updated if candidate.row_id == row.row_id else candidate for candidate in self.view.rows
        ))
        self._refresh_additional_rows()
        self._emit(SessionChanged(self.view))
        return CommandAccepted(self.view, ProposalEdit(row.row_id, status, diagnostics))

    def execute(self) -> CommandAccepted | CommandRejected:
        """Execute the current view; adapters with cached views use Execute tokens."""
        return self.dispatch(Execute(self.view.session_id, self.view.revision))

    def abort(self) -> CommandAccepted | CommandRejected:
        return self.dispatch(Abort(self.view.session_id, self.view.revision))

    def _result(self, *, preview: bool = False, aborted: bool = False) -> SyncResult:
        rows = {row.row_id: row for row in self.view.rows}
        # One sudo scope for the whole execution: protected writes and root
        # hooks share one authentication notice, and the keepalive stops a
        # long run from prompting again once sudo's ticket would expire.
        with sudo_session():
            published, operation_diagnostics, steps = (
                ({}, (), ()) if preview or aborted else self._publish()
            )
        units = []
        for observation in self.view.observations:
            identity = observation.identity.canonical
            status = observation.state
            diagnostics = observation.diagnostics
            if status == "drifted":
                row = rows[identity]
                diagnostics += row.diagnostics
                status = ("excluded" if not row.included
                          else "noop" if row.proposal is not None and row.proposal.noop else "pending")
                if row.included and row.approved and row.proposal is not None and not aborted:
                    if preview:
                        status = "would-converge"
                    else:
                        status, failures = published[identity]
                        diagnostics += failures
            if observation.state == "drifted" and rows[identity].proposal is not None:
                diagnostics += rows[identity].proposal.checkpoint_warnings
            diagnostics += self._checkpoint_warnings.get(identity, ())
            units.append(SyncUnitResult(identity, status, diagnostics,
                                       observation.base.acknowledged or identity in self._acknowledged))
        status = (
            "aborted" if aborted or any(d.code == "interrupted" for d in operation_diagnostics) or any(
                unit.status == "interrupted" or any(d.code == "interrupted" for d in unit.diagnostics)
                for unit in units
            )
            else "failed" if has_errors(operation_diagnostics) or any(has_errors(unit.diagnostics) or unit.status in ("observation-failed", "execution-failed") for unit in units)
            else "incomplete" if any(
                unit.status == "pending" and not supports_proposal(rows[unit.identity].observation)
                for unit in units
            )
            else "completed"
        )
        additional = tuple(
            AdditionalSourceResult(row.row_id, row.repo, row.path,
                                   "would-apply" if preview and row.approved and not aborted else "pending")
            for row in self.view.rows if isinstance(row, AdditionalRow)
        ) if preview or aborted else self._additional_results
        if any(change.status == "execution-failed" for change in additional):
            status = "failed"
        commits, commit_steps = ((), ()) if preview or aborted else self._commit(run_completed=status == "completed")
        if any(item.status == "interrupted" for item in commits):
            status = "aborted"
        elif any(item.status == "failed" for item in commits):
            status = "failed"
        return SyncResult(status, tuple(units), operation_diagnostics, steps + commit_steps, additional, commits)

    def commit_paths(self, repo: str) -> tuple[tuple[Path, ...], tuple]:
        """Repository paths the current selection writes in `repo`, and the targets they belong to."""
        identities = {observation.identity.canonical: observation.identity for observation in self.view.observations}
        paths, targets = [], []
        for row in self.view.rows:
            if (isinstance(row, SessionRow) and row.observation.identity.repo == repo and row.included
                    and row.approved and row.proposal is not None
                    and row.proposal.primary_source_change is not None
                    and row.observation.repository_path is not None):
                paths.append(row.observation.repository_path)
                targets.append(row.observation.identity)
            elif isinstance(row, AdditionalRow) and row.repo == repo and row.approved:
                paths.append(row.change.path)
                targets += [identities[reference] for reference in row.references if reference in identities]
        return tuple(paths), tuple(targets)

    def commit_message(self, repo: str) -> str | None:
        """The message Commit Work would use for the current selection, or None when it writes nothing."""
        paths, targets = self.commit_paths(repo)
        if not paths:
            return None
        return render_commit_message(self._context.config.git.commit_message,
                                     operation=self.operation, repo=repo, targets=targets)

    def _commit(self, *, run_completed: bool) -> tuple[tuple[RepositoryCommit, ...], tuple[SyncStepOutcome, ...]]:
        commits, steps = [], []
        for option in self.view.commit_options:
            if not option.selected:
                continue
            repo = option.repo
            paths, _ = self.commit_paths(repo)
            if not run_completed:
                # A partial run leaves its writes for the user to inspect before committing.
                commits.append(RepositoryCommit(repo, "skipped"))
                steps.append(_commit_step_outcome(repo, "unattempted", skip_reason="earlier-failure"))
                continue
            if not paths:
                commits.append(RepositoryCommit(repo, "no-changes"))
                steps.append(_commit_step_outcome(repo, "skipped", skip_reason="no-changes"))
                continue
            message = self.commit_message(repo)
            step = ExecutionStep(repo_name=repo, kind="commit", action="commit", scope_kind="repo",
                                 description=message.splitlines()[0])
            self._emit(StepStarted("repository-commit", step, 1, 1))
            try:
                self.check_cancelled()
                commit = commit_sources(self._commit_trees[repo].root, paths, message,
                                        self._context.projection.command_runtime)
                outcome = RepositoryCommit(repo, "committed" if commit else "no-changes", commit, message)
                result = ExecutionStepResult(step, "ok" if commit else "skipped",
                                             skip_reason=None if commit else "no-changes")
            except (KeyboardInterrupt, InterruptedError):
                outcome = RepositoryCommit(repo, "interrupted", message=message, error="Commit interrupted")
                result = ExecutionStepResult(step, "interrupted", error=outcome.error)
            except (CommitFailed, OSError) as exc:
                outcome = RepositoryCommit(repo, "failed", message=message, error=str(exc))
                result = ExecutionStepResult(step, "failed", error=outcome.error)
            self._emit(StepFinished("repository-commit", result, 1, 1))
            commits.append(outcome)
            steps.append(_commit_step_outcome(repo, result.status, skip_reason=result.skip_reason, error=result.error))
        return tuple(commits), tuple(steps)

    def _capture(self, observation: Observation) -> SyncBasePayload:
        self.check_cancelled()
        item, metadata = self._resolved_inputs[observation.identity]
        if observation.identity not in self._captures:
            try:
                with ExitStack() as resources:
                    preimages = self._input_bytes(observation)
                    if preimages:
                        metadata, _, _ = resources.enter_context(repository_workspace(
                            metadata=metadata, repo_root=item.repo.root, preimages=preimages,
                        ))
                    self._captures[observation.identity] = capture_observation(
                        observation, metadata=metadata, context=item.package_context.context,
                        command_runtime=self._context.projection.command_runtime,
                        reuse_comparison=preimages == self._editor_preimages.get(observation.identity, {}),
                        validated_render=lambda candidate, outcome: self._renders.__setitem__(
                            (observation.identity, candidate), outcome),
                    )
            except (KeyboardInterrupt, InterruptedError, CaptureError):
                raise
            except (ValueError, OSError) as exc:
                raise CaptureError(observation.repository_path, str(exc)) from exc
        return self._captures[observation.identity]

    def _render(self, observation: Observation, repository: SyncBasePayload) -> SyncBasePayload:
        self.check_cancelled()
        key = (observation.identity, repository)
        if key not in self._renders:
            item, metadata = self._resolved_inputs[observation.identity]
            with ExitStack() as resources:
                preimages = self._input_bytes(observation)
                if preimages:
                    metadata, _, _ = resources.enter_context(repository_workspace(
                        metadata=metadata, repo_root=item.repo.root, preimages=preimages,
                    ))
                outcome = project_file_view(
                    self._context.projection.command_runtime, metadata=metadata,
                    context=item.package_context.context,
                    repository=repository.content if isinstance(repository, (FilePresent, DirectoryChildPresent)) else None,
                    live=observation.live.content if isinstance(observation.live, (FilePresent, DirectoryChildPresent)) else None,
                    view="render", repo_side=True, repository_is_proposal=True,
                )
            self._renders[key] = Missing() if outcome is None else (
                DirectoryChildPresent(outcome, repository.executable)
                if isinstance(repository, DirectoryChildPresent) else FilePresent(outcome)
            )
        return self._renders[key]

    def _merge(self, observation: Observation, captured: SyncBasePayload) -> SyncBasePayload:
        self.check_cancelled()
        if observation.base.status != "usable" or observation.base.record is None:
            raise ValueError("Merge requires a usable Sync Base")
        return reconcile(
            observation.base.record.payload, observation.repository, captured,
            command_runtime=self._context.projection.command_runtime,
        )

    def _qualify_checkpoint(self, observation: Observation, proposal: Proposal) -> Proposal:
        if observation.configured_policy not in ("pull-only", "both"):
            return proposal
        # Publication already freezes the required forward projection. A no-write
        # publication outcome carries the same proof without inventing an effect.
        if self.operation in ("sync", "push") and observation.effective_policy in ("push-only", "both"):
            return replace(proposal, checkpoint_qualified=True)
        try:
            rendered = self._render(observation, proposal.repository)
        except (KeyboardInterrupt, InterruptedError):
            raise
        except (ValueError, OSError) as exc:
            return replace(proposal, checkpoint_warnings=(Diagnostic(
                "base-validation-failed", str(exc), severity="warning"),))
        return replace(proposal, checkpoint_qualified=rendered == observation.live)

    def _finalize_proposal(self, observation: Observation, proposal: Proposal) -> Proposal:
        """Every materialized Proposal settles its checkpoint proof, then whether Approval matters."""
        return self._settle_noop(observation, self._qualify_checkpoint(observation, proposal))

    def _settle_noop(self, observation: Observation, proposal: Proposal) -> Proposal:
        # Without writes, Approval's only effect is the Sync Base acknowledgment at
        # completion; when that cannot record anything new, Approval changes nothing.
        if proposal.primary_source_change is not None or proposal.publication_effects:
            return proposal
        frozen = self._frozen_bases.get(observation.identity)
        records_base = proposal.checkpoint_qualified and frozen is not None and frozen.unit.eligible
        base_current = records_base and observation.base.record == replace(frozen, payload=proposal.repository).record()
        return replace(proposal, noop=not records_base or base_current)

    def _acknowledge(self, row: SessionRow) -> None:
        observation = row.observation
        if not row.proposal.checkpoint_qualified:
            return
        item, _metadata = self._resolved_inputs[observation.identity]
        try:
            with SyncBaseStore.open(
                self._context.tracked_state.state_root, item.repo.config.state_key,
            ) as store:
                lifecycle = SyncBaseLifecycle(store, operation=self.operation, preview=False)
                completion = lifecycle.complete(
                    replace(self._frozen_bases[observation.identity], payload=row.proposal.repository),
                    ProposalCompletion(
                        intent=row.proposal.intent, approved=True,
                        publication_effects="succeeded" if row.proposal.publication_effects else "not-required",
                        primary_effect="succeeded" if row.proposal.primary_source_change is not None else "not-required",
                    ),
                )
                if completion.acknowledged:
                    self._acknowledged.add(row.row_id)
                if completion.failure is not None:
                    self._checkpoint_warnings[row.row_id] = (Diagnostic(
                        completion.warning_code, str(completion.failure), severity="warning"),)
        except (KeyboardInterrupt, InterruptedError):
            raise
        except (SyncBaseStoreError, OSError) as exc:
            self._checkpoint_warnings[row.row_id] = (Diagnostic(
                "base-acknowledgment-failed", str(exc), severity="warning"),)

    def _publish(self) -> tuple[
        dict[str, tuple[str, tuple[Diagnostic, ...]]],
        tuple[Diagnostic, ...],
        tuple[SyncStepOutcome, ...],
    ]:
        selected = tuple(
            row for row in self.view.rows
            if isinstance(row, SessionRow) and row.included and row.approved and row.proposal is not None
        )
        auxiliary = {
            direction: tuple(
                HookActivation(row.scope, self._resolved_inputs_identity(row.scope) if row.kind in ("probe", "directory-root") else None)
                for row in self.view.rows
                if isinstance(row, AuxiliaryRow) and row.included and direction in row.directions
            )
            for direction in ("pull", "push")
        }
        additional_rows = tuple(row for row in self.view.rows if isinstance(row, AdditionalRow))
        self._additional_results, additional_diagnostics, additional_steps = self._apply_additional(additional_rows)
        if not selected and not any(auxiliary.values()):
            if not additional_diagnostics:
                additional_diagnostics = self._reclaim_obsolete_bases()
            return {}, additional_diagnostics, additional_steps
        by_id = {row.row_id: row for row in selected}
        def complete(unit: RepositoryApplyUnit) -> None:
            self._acknowledge(by_id[unit.row_id])

        # Even an ineligible no-write Proposal has an ordered completion boundary.
        result = execute_repository_apply(
            self._repository_metadata,
            tuple(RepositoryApplyUnit(
                row.row_id, row.observation.identity, row.proposal.primary_source_change,
                requires_publication=bool(row.proposal.publication_effects),
            ) for row in selected),
            command_runtime=self._context.projection.command_runtime,
            complete=complete, auxiliary=auxiliary["pull"], run_noop=self._run_noop,
            no_confirm=no_confirm_enabled(),
            check_cancelled=self.check_cancelled, blocked=bool(additional_diagnostics),
            stream_output=self._stream_output, observe=self._emit,
        )
        units, diagnostics, repository_steps = self._execution_outcome(result, "repository-apply")
        steps = additional_steps + repository_steps
        diagnostics = additional_diagnostics + diagnostics
        for row in selected:
            if row.proposal.publication_effects and units[row.row_id][0] == "converged":
                units[row.row_id] = ("not-converged", ())
        publication = tuple(row for row in selected if row.proposal.publication_effects)
        if publication or auxiliary["push"]:
            blocked = bool(diagnostics)
            published, publication_diagnostics, publication_steps = self._publish_effects(
                publication, auxiliary["push"], blocked=blocked,
            )
            for identity, outcome in published.items():
                # A later stage cannot erase repository failure or successful
                # repository work that has not reached its publication boundary.
                if not blocked and not (outcome[0] == "skipped" and units[identity][0] == "not-converged"):
                    units[identity] = outcome
            diagnostics += publication_diagnostics
            steps += publication_steps
        if not diagnostics:
            diagnostics = self._reclaim_obsolete_bases()
        return units, diagnostics, steps

    def _reclaim_obsolete_bases(self) -> tuple[Diagnostic, ...]:
        # Any unincluded or diagnosed row withholds its whole directory's proof.
        withheld = {
            replace(row.observation.identity, child_path=None) for row in self.view.rows
            if isinstance(row, SessionRow) and (not row.included or row.observation.diagnostics or row.diagnostics)
        }
        keys_by_store = {}
        for identity, state_key, key in self._obsolete_bases:
            if identity not in withheld:
                keys_by_store.setdefault(state_key, []).append(key)
        try:
            for state_key, keys in keys_by_store.items():
                self.check_cancelled()
                with SyncBaseStore.open(self._context.tracked_state.state_root, state_key) as store:
                    for key in keys:
                        self.check_cancelled()
                        store.delete(key)
        except (KeyboardInterrupt, InterruptedError):
            return (Diagnostic("interrupted", "Directory Base maintenance interrupted"),)
        except (SyncBaseStoreError, OSError) as exc:
            return (Diagnostic("base-maintenance-failed", str(exc), severity="warning"),)
        return ()

    def _apply_additional(self, rows: tuple[AdditionalRow, ...]):
        if not rows:
            return (), (), ()
        repo_order = {name: index for index, (name, _) in enumerate(self._repository_metadata.repo_hooks)}
        rows = tuple(sorted(rows, key=lambda row: (repo_order[row.repo], row.path.as_posix())))
        results, steps, diagnostics = [], [], ()
        with command_runtime_session(self._context.projection.command_runtime):
            for row in rows:
                status, failures = "pending", ()
                if row.approved:
                    status = "skipped"
                    if not diagnostics:
                        try:
                            self.check_cancelled()
                            apply_repository_source(row.change.path, FilePresent(row.change.candidate),
                                                    repo_root=row.change.path.parents[len(row.path.parts) - 1])
                            status = "applied"
                        except (KeyboardInterrupt, InterruptedError):
                            status = "interrupted"
                            failures = (Diagnostic("interrupted", "Additional Source apply interrupted"),)
                        except (OSError, ValueError, RuntimeError) as exc:
                            status = "execution-failed"
                            failures = (Diagnostic("additional-source-failed", str(exc)),)
                        diagnostics += failures
                    steps.append(SyncStepOutcome(
                        "repository-apply", "additional-source", "update", "repo", row.row_id,
                        row.repo, None, "ok" if status == "applied" else
                        "failed" if status == "execution-failed" else "unattempted" if status == "skipped" else status,
                        skip_reason="earlier-failure" if status == "skipped" else None,
                        error=failures[0].message if failures else None,
                    ))
                results.append(AdditionalSourceResult(row.row_id, row.repo, row.path, status, failures))
        return tuple(results), diagnostics, tuple(steps)

    def _resolved_inputs_identity(self, scope: str):
        return next(identity for identity in (*self._resolved_inputs, *self._root_inputs) if identity.canonical == scope)

    def _selected_root_work(self) -> tuple[PublicationUnit, ...]:
        work = []
        for row in self.view.rows:
            if not (isinstance(row, AuxiliaryRow) and row.kind == "directory-root" and row.included):
                continue
            identity = self._resolved_inputs_identity(row.scope)
            _, metadata = self._root_inputs[identity]
            work.append(PublicationUnit(
                row.row_id, identity,
                (PublicationEffect("chmod", metadata.live_path, mode=int(metadata.chmod, 8)),),
                auxiliary=True,
            ))
        return tuple(work)

    def _publish_effects(self, selected: tuple[SessionRow, ...], auxiliary: tuple[HookActivation, ...] = (), *, blocked: bool = False) -> tuple[
        dict[str, tuple[str, tuple[Diagnostic, ...]]],
        tuple[Diagnostic, ...],
        tuple[SyncStepOutcome, ...],
    ]:
        by_id = {row.row_id: row for row in selected}
        def complete(unit: PublicationUnit) -> None:
            self._acknowledge(by_id[unit.row_id])

        result = execute_publication(
            self._publication_metadata,
            tuple(PublicationUnit(
                row.row_id, row.observation.identity, row.proposal.publication_effects,
                symlink_authorized=row.symlink_authorized,
            ) for row in selected) + self._selected_root_work(),
            complete=complete,
            snapshot_config=self._context.config.snapshots,
            auxiliary=auxiliary, run_noop=self._run_noop,
            no_confirm=no_confirm_enabled(),
            check_cancelled=self.check_cancelled, blocked=blocked,
            command_runtime=self._context.projection.command_runtime,
            stream_output=self._stream_output, observe=self._emit,
        )
        units, diagnostics, steps = self._execution_outcome(result, "live-publication")
        units = {row_id: outcome for row_id, outcome in units.items() if row_id in by_id}
        return units, diagnostics, steps

    @staticmethod
    def _execution_outcome(
        result: PublicationResult,
        stage: Literal["repository-apply", "live-publication"],
    ) -> tuple[
        dict[str, tuple[str, tuple[Diagnostic, ...]]],
        tuple[Diagnostic, ...],
        tuple[SyncStepOutcome, ...],
    ]:
        code = "interrupted" if result.interrupted else "execution-failed"
        diagnostics = () if result.error is None else (Diagnostic(code, result.error),)
        units = {
            unit.row_id: (
                "converged" if unit.status == "ok" else "skipped" if unit.status == "skipped" else code,
                () if unit.error is None else (Diagnostic(unit.diagnostic_code or code, unit.error),),
            )
            for unit in result.units
        }
        steps = tuple(
            SyncStepOutcome(
                stage=stage,
                kind=item.step.kind,
                action=item.step.action,
                scope=item.step.scope_kind,
                scope_identity=_step_scope_identity(item.step),
                repo=item.step.repo_name,
                package_id=item.step.package_id,
                status=item.status,
                skip_reason=item.skip_reason,
                exit_code=item.exit_code,
                error=item.error,
            )
            for item in result.steps
        )
        return units, diagnostics, steps

    def _finish(self, *, aborted: bool) -> SyncResult:
        try:
            result = self._result(aborted=aborted)
        finally:
            if self._operation_lock is not None:
                self._operation_lock.close()
                self._operation_lock = None
        self._view = replace(
            self.view,
            revision=self.view.revision + 1,
            terminal=True,
            allowed_commands=(),
            rows=tuple(replace(row, allowed_commands=()) for row in self.view.rows),
        )
        self._emit(SessionFinished(self.view, result))
        return result

    def __enter__(self) -> SyncSession:
        return self

    def __exit__(self, *_exc) -> None:
        if not self.view.terminal:
            self.abort()


class SyncSession(ProposalSession):
    """Two-sided convergence orchestration over the shared Proposal workset."""
