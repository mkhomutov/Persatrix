"""The selection pipeline (RFC 0028 §C; PR plan PR 2, E-2).

At a decision checkpoint an agent has one or more things it could do next.
The pipeline settles which one runs, in §C's five steps:

1. **Build** the candidates from what the runtime proposes, its own
   proposal first.
2. **Constrain**: each constraint input may remove a candidate it forbids.
3. **Score**: the policy ranks the candidates left and picks one. Manual
   mode's policy, ``BaselinePolicy``, takes the runtime's proposal.
4. **Gate**: each approval gate may hold the pick for a person's approval.
5. **Select**: what is left is the selection, written as one
   ``DecisionRecord`` with every candidate in it, the removed ones too.

Phase 1 wires nothing into steps 2 and 4 (E-2), so the runtime's call
changes no choice. The constraint inputs come with PR 7 (the permission and
channel-membership checks, read-only) and RFC 0012 C3 (accepted directives);
the approval gates with PR 9 (the approval list) and B3 (required-HITL
classes). A held pick is not swapped for the next candidate: the outcome
defers, and the held candidate is listed with the gate's reason.

The outcome's own reason codes:

- ``no_candidate`` — the runtime proposed nothing.
- ``no_permitted_candidate`` — constraint inputs removed every candidate;
  each rejection says which input removed it.
- ``constraint_error``, ``policy_error``, ``approval_gate_error`` — that step
  broke: it raised, or answered with something other than a reason code or
  a valid pick. The step fails closed, so whatever it could not clear does
  not run. Its log line says which step broke, for which agent and on which
  candidate id, and never repeats the step's answer, which may be text
  (E-10).
"""

from __future__ import annotations

import dataclasses
import logging
from collections.abc import Callable, Sequence

from .decision_types import (
    ActionClass,
    DecisionCandidate,
    DecisionContext,
    DecisionOutcome,
    DecisionPolicy,
    DecisionRecord,
    Rejection,
)

__all__ = ["ApprovalGate", "ConstraintInput", "decide"]

logger = logging.getLogger(__name__)

#: A check on one candidate: a reason code to refuse it, or ``None``.
_Check = Callable[[DecisionContext, DecisionCandidate], str | None]

#: A hard constraint (§C step 2): the reason code of the rule *candidate*
#: breaks, which removes it, or ``None`` to let it through.
ConstraintInput = _Check

#: An approval gate (§C step 4): the reason code the selected candidate is
#: held for, such as ``approval_required``, or ``None`` to let it run.
ApprovalGate = _Check


def decide(
    context: DecisionContext,
    candidates: Sequence[DecisionCandidate],
    policy: DecisionPolicy,
    *,
    constraints: Sequence[ConstraintInput] = (),
    approval_gates: Sequence[ApprovalGate] = (),
) -> DecisionRecord:
    """Settle which of *candidates* runs, and record why.

    Pure in what it decides: the same context, candidates, policy and inputs
    give the same outcome every time; only the record's own id and time are
    new. Duplicate candidate ids raise ``ValueError``, since no rejection or
    selection could say which of them it meant.
    """
    # 1. Build.
    built = tuple(candidates)
    if len({c.candidate_id for c in built}) != len(built):
        raise ValueError("candidate ids must be unique")
    # 2. Constrain.
    survivors, removed = _constrain(context, built, constraints)
    if not built:
        outcome = DecisionOutcome(ActionClass.DEFER, reason="no_candidate")
    elif not survivors:
        outcome = DecisionOutcome(ActionClass.DEFER, reason="no_permitted_candidate")
    else:
        # 3. Score, and 4. gate.
        outcome = _gate(context, survivors, _pick(context, survivors, policy), approval_gates)
    # 5. Select.
    return DecisionRecord(
        context=context,
        candidates=built,
        outcome=dataclasses.replace(outcome, rejected=(*removed, *outcome.rejected)),
    )


def _constrain(
    context: DecisionContext,
    candidates: tuple[DecisionCandidate, ...],
    constraints: Sequence[ConstraintInput],
) -> tuple[tuple[DecisionCandidate, ...], tuple[Rejection, ...]]:
    """The candidates every constraint input lets through, in the runtime's
    order, and a rejection for each of the others."""
    survivors: list[DecisionCandidate] = []
    removed: list[Rejection] = []
    for candidate in candidates:
        rejection = _first_refusal(
            context, candidate, constraints, "constraint input", "constraint_error"
        )
        if rejection is None:
            survivors.append(candidate)
        else:
            removed.append(rejection)
    return tuple(survivors), tuple(removed)


def _pick(
    context: DecisionContext,
    survivors: tuple[DecisionCandidate, ...],
    policy: DecisionPolicy,
) -> DecisionOutcome:
    """The policy's outcome over *survivors*, or ``policy_error``.

    The outcome must make a valid record over the candidates the policy was
    given: a pick among them under its own class, or a deferral with a
    reason. Anything else — a candidate a constraint removed, one never
    proposed — and a policy that raises, leaves nothing selected.
    """
    try:
        outcome = policy.select(context, survivors)
        if not isinstance(outcome, DecisionOutcome):
            raise TypeError(f"a policy returns a DecisionOutcome, not {type(outcome).__name__}")
        DecisionRecord(context=context, candidates=survivors, outcome=outcome)
    except Exception:
        logger.exception(
            "The decision policy of agent %r failed at its %s checkpoint, "
            "so nothing was selected",
            context.agent_id, context.checkpoint.value,
        )
        return DecisionOutcome(ActionClass.DEFER, reason="policy_error")
    return outcome


def _gate(
    context: DecisionContext,
    survivors: tuple[DecisionCandidate, ...],
    outcome: DecisionOutcome,
    approval_gates: Sequence[ApprovalGate],
) -> DecisionOutcome:
    """*outcome*, deferred with the reason of the first approval gate that
    holds its selection. The policy's confidence and rationale stay."""
    if outcome.selected_id is None:
        return outcome
    selected = next(c for c in survivors if c.candidate_id == outcome.selected_id)
    held = _first_refusal(context, selected, approval_gates, "approval gate", "approval_gate_error")
    if held is None:
        return outcome
    return dataclasses.replace(
        outcome,
        action_class=ActionClass.DEFER,
        selected_id=None,
        reason=held.reason,
        rejected=(*outcome.rejected, held),
    )


def _first_refusal(
    context: DecisionContext,
    candidate: DecisionCandidate,
    checks: Sequence[_Check],
    what: str,
    error_reason: str,
) -> Rejection | None:
    """The rejection the first of *checks* to refuse *candidate* gives it.

    A check that raises, or answers with anything but ``None`` or a reason
    code, refuses with *error_reason*.
    """
    for check in checks:
        try:
            reason = check(context, candidate)
            if reason is not None:
                return Rejection(candidate.candidate_id, reason)
        except Exception:
            logger.exception(
                "Agent %r could not run a %s on candidate %r at its %s checkpoint, "
                "so the candidate does not run",
                context.agent_id, what, candidate.candidate_id, context.checkpoint.value,
            )
            return Rejection(candidate.candidate_id, error_reason)
    return None
