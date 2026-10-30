"""The selection pipeline (RFC 0028 §C; PR plan PR 2, E-2).

At a decision checkpoint an agent has one or more things it could do next.
The pipeline settles which one runs, in §C's five steps:

1. **Build** the candidates from what the runtime proposes, its own
   proposal first.
2. **Constrain**: each constraint input may remove a candidate it forbids.
3. **Score**: the policy ranks the candidates left and picks one. Manual
   mode's policy, ``BaselinePolicy``, takes the first of them.
4. **Gate**: each approval gate may hold the pick for a person's approval.
5. **Select**: what is left is the selection, written as one
   ``DecisionRecord`` with every candidate in it, the removed ones too.

Phase 1 wires nothing into steps 2 and 4 (E-2), so the runtime's call
changes no choice. The constraint inputs come with PR 7 (the permission and
channel-membership checks, read-only) and RFC 0012 C3 (accepted directives);
the approval gates with PR 9 (the approval list) and B3 (required-HITL
classes). A held pick is not swapped for the next candidate: the outcome
defers, and the held candidate is listed with the gate's reason. The
policy's score and rationale go with the pick, since nothing was taken.

The outcome's own reason codes:

- ``no_candidate`` — the runtime proposed nothing.
- ``no_permitted_candidate`` — constraint inputs removed every candidate;
  each rejection says which input removed it.
- ``proposal_removed`` — a pick the policy gave as ``runtime_proposal``,
  when a constraint input had removed the runtime's own proposal. The policy
  sees only the candidates left, so it cannot tell; the pipeline can.
- ``constraint_error``, ``policy_error``, ``approval_gate_error`` — that step
  broke: it raised, was async, or answered with something other than a
  reason code or a valid pick. The step fails closed, so whatever it could
  not clear does not run. A constraint input that broke on a candidate gives
  the rejection only when no other input refused it, and names the outcome
  only when nothing is left, since then it may have removed the one that was
  permitted. Its log line names the agent, the check by its place and where
  it is defined, the candidate's id and what went wrong; never the step's
  answer or the message of an error it raised, either of which may be text
  (E-10).

A check answers at once, so a runtime fetches what one reads, such as a
roster, before calling ``decide``.
"""

from __future__ import annotations

import dataclasses
import inspect
import logging
from collections.abc import Callable, Iterable, Sequence

from .decision_types import (
    ActionClass,
    Checkpoint,
    DecisionCandidate,
    DecisionContext,
    DecisionOutcome,
    DecisionPolicy,
    DecisionRecord,
    PolicyMode,
    Rejection,
    check_candidates,
    check_outcome,
)

__all__ = [
    "NO_CANDIDATE",
    "PROPOSAL_REMOVED",
    "RUNTIME_PROPOSAL",
    "ApprovalGate",
    "ConstraintInput",
    "decide",
]

logger = logging.getLogger(__name__)

#: The outcome when the runtime proposed nothing, from the pipeline or the
#: baseline alike.
NO_CANDIDATE = DecisionOutcome(ActionClass.DEFER, reason="no_candidate")

#: A pick that is the runtime's own proposal, its first candidate.
RUNTIME_PROPOSAL = "runtime_proposal"

#: A ``runtime_proposal`` pick after a constraint input removed the proposal.
PROPOSAL_REMOVED = "proposal_removed"

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
    constraints: Iterable[ConstraintInput] = (),
    approval_gates: Iterable[ApprovalGate] = (),
) -> DecisionRecord:
    """Settle which of *candidates* runs, and record why.

    Pure in what it decides: the same context, candidates, policy and inputs
    give the same outcome every time; only the record's own id and time are
    new. Before any step runs, duplicate candidate ids raise ``ValueError``,
    since no rejection or selection could say which of them it meant, and a
    context whose checkpoint or mode is not its enum raises ``TypeError``,
    since a broken step's log line names the one and the score check reads
    the other.
    """
    if not isinstance(context.checkpoint, Checkpoint) or not isinstance(context.mode, PolicyMode):
        raise TypeError("a decision context names its checkpoint and mode by their enums")
    # Every candidate goes through every constraint input, so an iterator is
    # read once, here.
    checks, gates = tuple(constraints), tuple(approval_gates)
    # 1. Build.
    built = tuple(candidates)
    check_candidates(built)
    # 2. Constrain.
    survivors, removed, broke = _constrain(context, built, checks)
    if not built:
        outcome = NO_CANDIDATE
    elif not survivors:
        outcome = DecisionOutcome(
            ActionClass.DEFER, reason="constraint_error" if broke else "no_permitted_candidate"
        )
    else:
        # 3. Score, and 4. gate.
        picked = _relabel(_pick(context, survivors, policy), built[0])
        outcome = _gate(context, survivors, picked, gates)
    # 5. Select.
    return DecisionRecord(
        context=context,
        candidates=built,
        outcome=dataclasses.replace(outcome, rejected=(*removed, *outcome.rejected)),
    )


def _constrain(
    context: DecisionContext,
    candidates: tuple[DecisionCandidate, ...],
    constraints: tuple[ConstraintInput, ...],
) -> tuple[tuple[DecisionCandidate, ...], tuple[Rejection, ...], bool]:
    """The candidates every constraint input lets through, in the runtime's
    order; a rejection for each of the others; and whether one was removed
    only because an input broke on it.

    The first input to refuse a candidate names its rejection, and the rest
    are not asked. One that breaks is passed over, so a later refusal still
    names it: ``constraint_error`` does only when no input refuses.
    """
    survivors: list[DecisionCandidate] = []
    removed: list[Rejection] = []
    broke_any = False
    for candidate in candidates:
        rejection, broke = None, False
        for index, constraint in enumerate(constraints):
            rejection, failed = _ask(context, candidate, constraint, f"constraints[{index}]")
            if rejection is not None:
                break
            broke = broke or failed
        if rejection is not None:
            removed.append(rejection)
        elif broke:
            removed.append(Rejection(candidate.candidate_id, "constraint_error"))
            broke_any = True
        else:
            survivors.append(candidate)
    return tuple(survivors), tuple(removed), broke_any


def _pick(
    context: DecisionContext,
    survivors: tuple[DecisionCandidate, ...],
    policy: DecisionPolicy,
) -> DecisionOutcome:
    """The policy's outcome over *survivors*, or ``policy_error``.

    The outcome must make a valid record over the candidates the policy was
    given: a pick among them under its own class, or a deferral with a
    reason, unscored in manual mode. Anything else — a candidate a
    constraint removed, one never proposed, a score manual mode's records
    never carry — and a policy that raises or is async, leaves nothing
    selected.
    """
    try:
        outcome = policy.select(context, survivors)
    except Exception as exc:
        # The error's type only: its message may quote the turn (E-10).
        return _policy_broke(context, policy, f"it raised {type(exc).__name__}")
    if inspect.iscoroutine(outcome):
        outcome.close()  # never run, so never left to warn it was not awaited
        return _policy_broke(context, policy, "it is async, and a policy answers at once")
    try:
        if not isinstance(outcome, DecisionOutcome):
            raise ValueError(f"a policy returns a DecisionOutcome, not {type(outcome).__name__}")
        check_outcome(context, survivors, outcome)
    except ValueError as exc:
        # A record rule's own words, which quote nothing from the turn.
        return _policy_broke(context, policy, f"its outcome is not a valid pick ({exc})")
    return outcome


def _relabel(outcome: DecisionOutcome, proposal: DecisionCandidate) -> DecisionOutcome:
    """*outcome*, with a ``runtime_proposal`` pick of a candidate other than
    the runtime's *proposal* made ``proposal_removed``: the policy, given only
    the candidates left, took its first for the proposal."""
    if outcome.reason != RUNTIME_PROPOSAL or outcome.selected_id in (None, proposal.candidate_id):
        return outcome
    return dataclasses.replace(outcome, reason=PROPOSAL_REMOVED)


def _gate(
    context: DecisionContext,
    survivors: tuple[DecisionCandidate, ...],
    outcome: DecisionOutcome,
    approval_gates: tuple[ApprovalGate, ...],
) -> DecisionOutcome:
    """*outcome*, or a deferral when an approval gate holds its selection:
    with the reason of the first gate that holds it, or ``approval_gate_error``
    from the first that breaks, since it could not say whether a person must
    approve. The policy's rejections stay; its score and rationale go with
    the pick."""
    if outcome.selected_id is None:
        return outcome
    selected = next(c for c in survivors if c.candidate_id == outcome.selected_id)
    for index, gate in enumerate(approval_gates):
        held, broke = _ask(context, selected, gate, f"approval_gates[{index}]")
        if broke:
            held = Rejection(selected.candidate_id, "approval_gate_error")
        if held is not None:
            return DecisionOutcome(
                ActionClass.DEFER, reason=held.reason, rejected=(*outcome.rejected, held)
            )
    return outcome


def _ask(
    context: DecisionContext, candidate: DecisionCandidate, check: _Check, slot: str
) -> tuple[Rejection | None, bool]:
    """The rejection *check*, the runtime's ``slot``, gives *candidate*, or
    ``None`` when it lets it through; and whether it broke instead. A check
    that raises, is async, or answers with anything but ``None`` or a reason
    code breaks, and its log line says which."""
    try:
        answer = check(context, candidate)
    except Exception as exc:
        # The error's type only: its message may quote the turn (E-10).
        return _check_broke(context, candidate, check, slot, f"it raised {type(exc).__name__}")
    if inspect.iscoroutine(answer):
        answer.close()  # never run, so never left to warn it was not awaited
        return _check_broke(context, candidate, check, slot,
                            "it is async, and a check answers at once")
    if answer is None:
        return None, False
    try:
        return Rejection(candidate.candidate_id, answer), False
    except (TypeError, ValueError):
        # Never the answer itself, which may be text (E-10).
        return _check_broke(context, candidate, check, slot, "its answer is not a reason code")


def _check_broke(
    context: DecisionContext, candidate: DecisionCandidate, check: _Check, slot: str, why: str
) -> tuple[None, bool]:
    logger.error(
        "Agent %r could not run %s (%s) on candidate %r at its %s checkpoint: %s, "
        "so the candidate does not run",
        context.agent_id, slot, _defined_at(check), candidate.candidate_id,
        context.checkpoint.value, why,
    )
    return None, True


def _policy_broke(context: DecisionContext, policy: DecisionPolicy, why: str) -> DecisionOutcome:
    logger.error(
        "The decision policy (%s) of agent %r broke at its %s checkpoint: %s, "
        "so nothing was selected",
        _defined_at(policy), context.agent_id, context.checkpoint.value, why,
    )
    return DecisionOutcome(ActionClass.DEFER, reason="policy_error")


def _defined_at(step: object) -> str:
    """Where *step* is defined, as ``module.qualname``: code, never turn text.
    An object that is not a function or class names its class."""
    named = step if hasattr(step, "__qualname__") else type(step)
    return f"{getattr(named, '__module__', '?')}.{getattr(named, '__qualname__', '?')}"
