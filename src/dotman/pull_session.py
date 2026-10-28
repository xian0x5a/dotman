"""Permanent fixed live-to-repository orchestration over frozen Proposal work."""
from dataclasses import replace

from dotman.interaction_policy import no_confirm_enabled
from dotman.sync_observation import observe_scope
from dotman.sync_session import (
    ProposalSession, SessionRow, AdditionalRow, Proposal, AuxiliaryRow,
)
from dotman.sync_publication import HookActivation
from dotman.sync_repository_apply import RepositoryApplyUnit, execute_repository_apply


class PullSession(ProposalSession):
    operation = "pull"
    additional_default_approval = True

    @staticmethod
    def _observe(context, scope, **kwargs):
        return observe_scope(context, scope, directions=("pull",),
                             omit_no_route=True, **kwargs)

    @staticmethod
    def _freeze_obsolete_bases(context, observed):
        return (), {}

    def _prepare_workset(self):
        # Pull is opt-out for auxiliary work too; --no-confirm execution must not
        # reapprove the whole workset and thereby retry failed Proposals.
        self._prepare_fixed_direction_workset("use-live")

    def _materialize_row(self, row):
        observation = row.observation
        edited = self._edited_outcomes.get(row.row_id)
        repository = edited[0] if edited else self._capture(observation)
        return self._finalize_proposal(observation, Proposal(
            repository, observation.live,
            repository if repository != observation.repository else None,
            (), intent="editor" if edited else "use-live",
            capture=None if edited else repository,
            reconciliation="edited repository outcome" if edited else "captured repository outcome",
            generation=edited[1] if edited else self._next_generation(row.row_id),
            additional_changes=self._row_additional(row),
        ))

    def _publish(self, stores):
        selected = tuple(row for row in self.view.rows
                         if isinstance(row, SessionRow) and row.included
                         and row.approved and row.proposal is not None)
        auxiliary = tuple(
            HookActivation(row.scope, self._resolved_inputs_identity(row.scope) if row.kind == "probe" else None)
            for row in self.view.rows
            if isinstance(row, AuxiliaryRow) and row.included and "pull" in row.directions
        )
        self._additional_results, diagnostics, steps = self._apply_additional(
            tuple(row for row in self.view.rows if isinstance(row, AdditionalRow))
        )
        by_id = {row.row_id: row for row in selected}
        result = execute_repository_apply(
            self._repository_metadata,
            tuple(RepositoryApplyUnit(row.row_id, row.observation.identity,
                                      row.proposal.primary_source_change,
                                      requires_publication=False) for row in selected),
            command_runtime=self._context.projection.command_runtime,
            complete=lambda unit: self._acknowledge(by_id[unit.row_id], stores), auxiliary=auxiliary, run_noop=self._run_noop,
            no_confirm=no_confirm_enabled(),
            check_cancelled=self.check_cancelled, blocked=bool(diagnostics),
            stream_output=self._stream_output, observe=self._emit,
        )
        units, failures, apply_steps = self._execution_outcome(result, "repository-apply")
        return {identity: ("applied" if status == "converged" else status, errors)
                for identity, (status, errors) in units.items()}, diagnostics + failures, steps + apply_steps

    def _result(self, *, preview=False, aborted=False):
        result = super()._result(preview=preview, aborted=aborted)
        return replace(result, units=tuple(
            replace(unit, status="would-apply") if unit.status == "would-converge" else unit
            for unit in result.units
        ))
