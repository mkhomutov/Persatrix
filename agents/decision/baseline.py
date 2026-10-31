"""Manual mode's policy (RFC 0028 §B; PR plan PR 2, E-1 and E-2).

Manual mode is the default (E-1). In it the agent does what its runtime
proposed: ``BaselinePolicy`` selects the first candidate it is given, which
the runtime lists first, and scores nothing. So with no constraint input
and no approval gate (E-2), a checkpoint changes no choice. Its records are
the baseline that semi-automated mode's scoring (PR 7) is calibrated
against.
"""

from __future__ import annotations

from collections.abc import Sequence

from .decision_types import (
    DecisionCandidate,
    DecisionContext,
    DecisionOutcome,
    PolicyMode,
)
from .pipeline import NO_CANDIDATE, RUNTIME_PROPOSAL

__all__ = ["BaselinePolicy"]


class BaselinePolicy:
    """Selects the first candidate it is given, the runtime's own proposal,
    unscored."""

    def select(
        self, context: DecisionContext, candidates: Sequence[DecisionCandidate]
    ) -> DecisionOutcome:
        if context.mode is not PolicyMode.MANUAL:
            # A record names its mode so calibration can tell a scored record
            # from an unscored one; this policy scores nothing.
            raise ValueError(f"the baseline policy is manual mode's, not {context.mode.value}")
        if not candidates:
            # The pipeline's own outcome when nothing was proposed, so asking
            # the policy directly gives the same record.
            return NO_CANDIDATE
        # The pipeline gives only the candidates the constraint inputs left.
        # When they removed the proposal, the first left is the runtime's next
        # choice in its own order, and the pipeline makes the reason say so.
        first = candidates[0]
        return DecisionOutcome(
            first.action_class,
            selected_id=first.candidate_id,
            reason=RUNTIME_PROPOSAL,
            rationale="manual mode takes the first candidate left in the runtime's own order",
        )
