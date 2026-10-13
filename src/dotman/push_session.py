"""Permanent fixed repository-to-live orchestration over frozen Proposal work."""

from dotman.sync_observation import observe_scope
from dotman.sync_session import ProposalSession


class PushSession(ProposalSession):
    operation = "push"
    additional_default_approval = True
    # Push writes repository sources only through deliberate edits; it offers no Commit Work.
    offers_commit = False

    @staticmethod
    def _observe(context, scope, **kwargs):
        return observe_scope(context, scope, directions=("push",),
                             omit_no_route=True, **kwargs)

    def _prepare_workset(self):
        # Push is opt-out: every drifted Proposal and includable auxiliary row
        # starts selected, and the direction is fixed to Use repository.
        self._prepare_fixed_direction_workset("use-repository")
