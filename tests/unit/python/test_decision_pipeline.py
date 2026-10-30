"""The selection pipeline and manual mode's policy (RFC 0028 §C; PR plan PR 2).

At a decision checkpoint the pipeline takes what the runtime proposes,
removes what a constraint input forbids, lets the policy pick among the rest,
and holds the pick when an approval gate says it needs a person. Phase 1
wires no constraint input and no approval gate (E-2): those arrive with
PR 7, PR 9, B3 and RFC 0012 C3, so these tests drive both steps with fakes.
"""

from __future__ import annotations

import dataclasses
import itertools
import logging
import traceback
import warnings
from collections.abc import Sequence
from typing import Any

import pytest

from agents.decision import (
    ActionClass,
    ApprovalGate,
    BaselinePolicy,
    Checkpoint,
    ConstraintInput,
    DecisionCandidate,
    DecisionContext,
    DecisionOutcome,
    DecisionPolicy,
    DecisionRecord,
    PolicyMode,
    Rejection,
    class_of_action,
    decide,
    hash_content,
)
from agents.persona_types import ActionType

# ─── Builders ──────────────────────────────────────────────


def _context(mode: PolicyMode = PolicyMode.MANUAL) -> DecisionContext:
    return DecisionContext(checkpoint=Checkpoint.PRE_ACT, agent_id="ember-owl", mode=mode)


def _candidate(candidate_id: str,
               action_type: ActionType = ActionType.SEND_CHANNEL_MESSAGE) -> DecisionCandidate:
    return DecisionCandidate(
        candidate_id=candidate_id,
        action_class=class_of_action(action_type),
        action=action_type.value,
        target="general",
        content_hash=hash_content(f"text of {candidate_id}"),
    )


PUBLISH = _candidate("publish")
TOOL = _candidate("tool", ActionType.USE_TOOL)
NOTHING = _candidate("nothing", ActionType.DO_NOTHING)
_ALL = (PUBLISH, TOOL, NOTHING)


def _forbid(*candidate_ids: str, reason: str = "directive_forbids") -> ConstraintInput:
    def constraint(context: DecisionContext, candidate: DecisionCandidate) -> str | None:
        return reason if candidate.candidate_id in candidate_ids else None

    return constraint


def _hold(action_class: ActionClass, reason: str = "approval_required") -> ApprovalGate:
    def gate(context: DecisionContext, candidate: DecisionCandidate) -> str | None:
        return reason if candidate.action_class is action_class else None

    return gate


def _noting_gate(asked: list[str]) -> ApprovalGate:
    def gate(context: DecisionContext, candidate: DecisionCandidate) -> str | None:
        asked.append(candidate.candidate_id)
        return None

    return gate


class _Recording:
    """A policy that notes what it was given and hands it to the baseline."""

    def __init__(self) -> None:
        self.seen: list[tuple[DecisionCandidate, ...]] = []

    def select(
        self, context: DecisionContext, candidates: Sequence[DecisionCandidate]
    ) -> DecisionOutcome:
        self.seen.append(tuple(candidates))
        return BaselinePolicy().select(context, candidates)


class _Picks:
    """A policy that answers with a fixed outcome, whatever it is given."""

    def __init__(self, outcome: DecisionOutcome) -> None:
        self.outcome = outcome

    def select(
        self, context: DecisionContext, candidates: Sequence[DecisionCandidate]
    ) -> DecisionOutcome:
        return self.outcome


# ─── E-2: with no inputs, manual mode changes no choice ────


@pytest.mark.parametrize("action_type", list(ActionType))
def test_with_no_inputs_manual_mode_selects_the_runtimes_proposal(
    action_type: ActionType,
) -> None:
    proposal = _candidate("c0", action_type)
    # The baseline's own pick, unchanged: its proposal, unscored, nothing rejected.
    assert decide(_context(), [proposal], BaselinePolicy()).outcome \
        == BaselinePolicy().select(_context(), [proposal])


def test_deciding_again_gives_the_same_outcome() -> None:
    records = [
        decide(_context(), [PUBLISH, TOOL, NOTHING], BaselinePolicy(),
               constraints=[_forbid("publish")], approval_gates=[_hold(ActionClass.DELEGATE)])
        for _ in range(3)
    ]
    assert {r.outcome for r in records} == {records[0].outcome}
    # Only the record's own id and time are new each time.
    assert len({r.record_id for r in records}) == 3


# ─── Step 2: constraint inputs remove candidates ───────────


def test_a_constraint_input_removes_the_candidates_it_refuses() -> None:
    record = decide(_context(), [PUBLISH, TOOL], BaselinePolicy(),
                    constraints=[_forbid("publish")])
    assert (record.outcome.selected_id, record.outcome.rejected) \
        == ("tool", (Rejection("publish", "directive_forbids"),))


def test_the_record_keeps_every_candidate_the_removed_ones_included() -> None:
    record = decide(_context(), _ALL, BaselinePolicy(), constraints=[_forbid("tool")])
    assert (record.context, record.candidates) == (_context(), _ALL)


def test_constraint_inputs_given_as_an_iterator_check_every_candidate() -> None:
    # Each candidate is checked by every input, so the inputs are read once
    # up front; an iterator read per candidate would be empty after the first.
    record = decide(_context(), [PUBLISH, TOOL], BaselinePolicy(),
                    constraints=iter([_forbid("tool"), _forbid("publish")]))
    assert record.outcome.reason == "no_permitted_candidate"


@pytest.mark.parametrize(
    ("removed", "selected", "reason"),
    [("tool", "publish", "runtime_proposal"), ("publish", "tool", "proposal_removed")],
)
def test_manual_modes_pick_says_whether_it_is_the_runtimes_proposal(
    removed: str, selected: str, reason: str,
) -> None:
    # The baseline sees only the survivors, so it cannot tell its first
    # candidate from the runtime's proposal; the pipeline can.
    record = decide(_context(), [PUBLISH, TOOL], BaselinePolicy(),
                    constraints=[_forbid(removed)])
    assert (record.outcome.selected_id, record.outcome.reason) == (selected, reason)


def test_the_first_constraint_input_to_refuse_names_the_reason() -> None:
    asked: list[tuple[str, str]] = []

    def noting(name: str, refuses: bool) -> ConstraintInput:
        def constraint(context: DecisionContext, candidate: DecisionCandidate) -> str | None:
            asked.append((name, candidate.candidate_id))
            return name if refuses else None

        return constraint

    record = decide(_context(), [PUBLISH], BaselinePolicy(),
                    constraints=[noting("allows", False), noting("first_no", True),
                                 noting("second_no", True)])
    assert record.outcome.rejected == (Rejection("publish", "first_no"),)
    assert asked == [("allows", "publish"), ("first_no", "publish")]


def test_the_policy_sees_only_the_survivors_in_the_runtimes_order() -> None:
    policy = _Recording()
    decide(_context(), [PUBLISH, TOOL, NOTHING], policy, constraints=[_forbid("tool")])
    assert policy.seen == [(PUBLISH, NOTHING)]


@pytest.mark.parametrize(
    "removed",
    [set(ids) for n in range(len(_ALL) + 1)
     for ids in itertools.combinations([c.candidate_id for c in _ALL], n)],
    ids=lambda removed: "+".join(sorted(removed)) or "none",
)
def test_manual_mode_never_selects_a_candidate_a_constraint_removed(removed: set[str]) -> None:
    record = decide(_context(), _ALL, BaselinePolicy(), constraints=[_forbid(*removed)])
    left = [c for c in _ALL if c.candidate_id not in removed]
    assert record.outcome.selected_id not in removed
    if left:
        assert record.outcome.selected_id == left[0].candidate_id
    else:
        assert record.outcome.action_class is ActionClass.DEFER
    assert {r.candidate_id for r in record.outcome.rejected} == removed


# ─── §C: when nothing survives, the outcome is defer ───────


def test_no_candidate_defers_without_asking_the_policy() -> None:
    policy = _Recording()
    record = decide(_context(), [], policy)
    assert record.outcome == DecisionOutcome(ActionClass.DEFER, reason="no_candidate")
    assert policy.seen == []


def test_every_candidate_removed_defers_without_asking_the_policy_or_a_gate() -> None:
    policy = _Recording()
    asked: list[str] = []
    record = decide(_context(), [PUBLISH, TOOL], policy,
                    constraints=[_forbid("publish"), _forbid("tool", reason="permission_denied")],
                    approval_gates=[_noting_gate(asked)])
    assert record.outcome == DecisionOutcome(
        ActionClass.DEFER,
        reason="no_permitted_candidate",
        rejected=(Rejection("publish", "directive_forbids"),
                  Rejection("tool", "permission_denied")),
    )
    assert (policy.seen, asked) == ([], [])


# ─── Step 4: the approval gate holds a selection ───────────


_SCORED = _Picks(DecisionOutcome(ActionClass.PUBLISH_CHANNEL, selected_id="publish",
                                  confidence=0.75, rationale="highest score"))


@pytest.mark.parametrize(
    ("mode", "policy"),
    [(PolicyMode.MANUAL, BaselinePolicy()), (PolicyMode.SEMI_AUTOMATED, _SCORED)],
    ids=["manual", "scored"],
)
def test_an_approval_gate_holds_the_selection_and_the_outcome_defers(
    mode: PolicyMode, policy: DecisionPolicy,
) -> None:
    record = decide(_context(mode), [PUBLISH, NOTHING], policy,
                    approval_gates=[_hold(ActionClass.PUBLISH_CHANNEL)])
    # The held candidate is not swapped for the next one, and the policy's
    # score and rationale go with its pick: nothing was taken.
    assert record.outcome == DecisionOutcome(
        ActionClass.DEFER, reason="approval_required",
        rejected=(Rejection("publish", "approval_required"),),
    )


def test_an_approval_gate_sees_only_the_selection() -> None:
    asked: list[str] = []
    record = decide(_context(), [TOOL, PUBLISH], BaselinePolicy(),
                    approval_gates=[_noting_gate(asked)])
    assert (asked, record.outcome.selected_id) == (["tool"], "tool")


def test_the_first_approval_gate_to_hold_names_the_reason() -> None:
    record = decide(_context(), [PUBLISH], BaselinePolicy(),
                    approval_gates=[_hold(ActionClass.DELEGATE),
                                    _hold(ActionClass.PUBLISH_CHANNEL, "hitl_required"),
                                    _hold(ActionClass.PUBLISH_CHANNEL)])
    assert (record.outcome.reason, record.outcome.rejected) \
        == ("hitl_required", (Rejection("publish", "hitl_required"),))


def test_rejections_keep_the_order_they_were_made_in() -> None:
    # The constraint inputs', in the runtime's order; the policy's own; the
    # approval gate's last. One order, so one decision has one JSON line.
    rejects_tool = _Picks(DecisionOutcome(ActionClass.PUBLISH_CHANNEL, selected_id="publish",
                                          rejected=(Rejection("tool", "low_score"),)))
    late = _candidate("late")
    record = decide(_context(), [late, PUBLISH, TOOL, NOTHING], rejects_tool,
                    constraints=[_forbid("nothing", "late")],
                    approval_gates=[_hold(ActionClass.PUBLISH_CHANNEL)])
    assert [(r.candidate_id, r.reason) for r in record.outcome.rejected] == [
        ("late", "directive_forbids"),
        ("nothing", "directive_forbids"),
        ("tool", "low_score"),
        ("publish", "approval_required"),
    ]


# ─── Fail closed: a step that breaks lets nothing run ──────


def _raises(context: DecisionContext, candidate: DecisionCandidate) -> str | None:
    raise RuntimeError("the check is down: the launch is on Friday")


def _says_text(context: DecisionContext, candidate: DecisionCandidate) -> str | None:
    return "the launch is on Friday"


async def _is_async(context: DecisionContext, candidate: DecisionCandidate) -> str | None:
    return None


_BROKEN = [_raises, _says_text, _is_async]
_BROKEN_IDS = ["raises", "text-reason", "async"]


@pytest.mark.parametrize("constraint", _BROKEN, ids=_BROKEN_IDS)
def test_a_constraint_input_that_breaks_removes_its_candidate(
    constraint: ConstraintInput,
) -> None:
    record = decide(_context(), [PUBLISH, TOOL], BaselinePolicy(),
                    constraints=[_forbid("tool"), constraint])
    # Nothing is left, and a check broke: not the same as nothing permitted.
    assert record.outcome == DecisionOutcome(
        ActionClass.DEFER, reason="constraint_error",
        rejected=(Rejection("publish", "constraint_error"),
                  Rejection("tool", "directive_forbids")),
    )


@pytest.mark.parametrize("refusing_first", [False, True], ids=["broken-first", "refusing-first"])
def test_a_refusal_stands_whichever_side_of_a_broken_input_it_runs(
    refusing_first: bool,
) -> None:
    # The candidate would have been refused anyway, so no permitted one was lost.
    constraints = [_raises, _forbid("publish")]
    record = decide(_context(), [PUBLISH], BaselinePolicy(),
                    constraints=constraints[::-1] if refusing_first else constraints)
    assert record.outcome == DecisionOutcome(
        ActionClass.DEFER, reason="no_permitted_candidate",
        rejected=(Rejection("publish", "directive_forbids"),),
    )


def test_an_input_refusing_with_the_error_code_has_not_broken() -> None:
    record = decide(_context(), [PUBLISH], BaselinePolicy(),
                    constraints=[_forbid("publish", reason="constraint_error")])
    assert record.outcome.reason == "no_permitted_candidate"


@pytest.mark.parametrize("async_policy", [False, True], ids=["constraint", "policy"])
def test_an_async_step_is_closed_unrun(async_policy: bool) -> None:
    # Its coroutine never runs, and is not left to warn that it was never awaited.
    # mypy refuses either at a typed call site; this is the call it misses.
    policy: Any = _IsAsync() if async_policy else BaselinePolicy()
    constraints: Any = [] if async_policy else [_is_async]
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        decide(_context(), [PUBLISH], policy, constraints=constraints)
    assert [str(w.message) for w in caught if issubclass(w.category, RuntimeWarning)] == []


def _name(check: object) -> str:
    return f"{__name__}.{getattr(check, '__qualname__', type(check).__qualname__)}"


@pytest.mark.parametrize(
    ("step", "slot"),
    [("constraints", "constraints[1]"), ("approval_gates", "approval_gates[1]")],
)
@pytest.mark.parametrize(
    ("check", "why"),
    [(_raises, "it raised RuntimeError"),
     (_says_text, "its answer is not a reason code"),
     (_is_async, "it is async, and a check answers at once")],
    ids=_BROKEN_IDS,
)
def test_a_broken_checks_log_line_says_which_check_broke_and_how(
    step: str, slot: str, check: ConstraintInput, why: str, caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.ERROR, logger="agents.decision.pipeline"):
        decide(_context(), [PUBLISH], BaselinePolicy(), **{step: [_forbid("tool"), check]})
    # Pinned whole, so nothing the check returned or raised reaches it (E-10).
    assert caplog.messages == [
        f"Agent 'ember-owl' could not run {slot} ({_name(check)}) on candidate 'publish' "
        f"at its pre_act checkpoint: {why}, so the candidate does not run"
    ]


def test_a_constraint_input_that_breaks_on_one_candidate_leaves_the_rest() -> None:
    def breaks_on_publish(context: DecisionContext, candidate: DecisionCandidate) -> str | None:
        return _raises(context, candidate) if candidate.candidate_id == "publish" else None

    record = decide(_context(), [PUBLISH, TOOL], BaselinePolicy(),
                    constraints=[breaks_on_publish])
    assert (record.outcome.selected_id, record.outcome.rejected) \
        == ("tool", (Rejection("publish", "constraint_error"),))


class _Raises:
    def select(self, context: DecisionContext,
               candidates: Sequence[DecisionCandidate]) -> DecisionOutcome:
        raise RuntimeError("the policy is down: the launch is on Friday")


class _IsAsync:
    async def select(self, context: DecisionContext,
                     candidates: Sequence[DecisionCandidate]) -> DecisionOutcome:
        return BaselinePolicy().select(context, candidates)


@pytest.mark.parametrize(
    "policy",
    [
        _Raises(),
        _IsAsync(),
        # The candidate a constraint removed; one never proposed; a survivor
        # under another class; nothing selected and no deferral.
        _Picks(DecisionOutcome(ActionClass.TOOL_CALL, selected_id="tool")),
        _Picks(DecisionOutcome(ActionClass.PUBLISH_CHANNEL, selected_id="ghost")),
        _Picks(DecisionOutcome(ActionClass.RESPOND, selected_id="publish")),
        _Picks(DecisionOutcome(ActionClass.PUBLISH_CHANNEL)),
        # A rejection of a candidate it was never given.
        _Picks(DecisionOutcome(ActionClass.PUBLISH_CHANNEL, selected_id="publish",
                               rejected=(Rejection("tool", "low_score"),))),
        _Picks(None),  # type: ignore[arg-type]
        # A score, which manual mode's records never carry.
        _Picks(DecisionOutcome(ActionClass.PUBLISH_CHANNEL, selected_id="publish",
                               confidence=0.8)),
    ],
    ids=["raises", "async", "removed", "unknown", "wrong-class", "no-selection",
         "foreign-rejection", "no-outcome", "scored-in-manual"],
)
def test_a_policy_that_breaks_defers_and_no_gate_is_asked(policy: DecisionPolicy) -> None:
    asked: list[str] = []
    record = decide(_context(), [PUBLISH, TOOL], policy,
                    constraints=[_forbid("tool")], approval_gates=[_noting_gate(asked)])
    assert (record.outcome, asked) == (
        DecisionOutcome(ActionClass.DEFER, reason="policy_error",
                        rejected=(Rejection("tool", "directive_forbids"),)),
        [],
    )


def test_a_broken_policys_log_line_says_which_policy_broke_and_how(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Pinned whole: the error's type only, never its message (E-10).
    with caplog.at_level(logging.ERROR, logger="agents.decision.pipeline"):
        decide(_context(), [PUBLISH], _Raises())
    assert caplog.messages == [
        f"The decision policy ({_name(_Raises)}) of agent 'ember-owl' broke at its pre_act "
        "checkpoint: it raised RuntimeError, so nothing was selected"
    ]


def test_a_scored_pick_stands_outside_manual_mode() -> None:
    scored = DecisionOutcome(ActionClass.PUBLISH_CHANNEL, selected_id="publish", confidence=0.8)
    record = decide(_context(PolicyMode.SEMI_AUTOMATED), [PUBLISH], _Picks(scored))
    assert record.outcome == scored


@pytest.mark.parametrize("gate", _BROKEN, ids=_BROKEN_IDS)
def test_an_approval_gate_that_breaks_holds_the_selection(gate: ApprovalGate) -> None:
    # A gate that cannot say whether a person must approve holds the pick,
    # even when a later gate would have held it for approval.
    record = decide(_context(), [PUBLISH, NOTHING], BaselinePolicy(),
                    approval_gates=[gate, _hold(ActionClass.PUBLISH_CHANNEL)])
    assert record.outcome == DecisionOutcome(
        ActionClass.DEFER, reason="approval_gate_error",
        rejected=(Rejection("publish", "approval_gate_error"),),
    )


# ─── The record ────────────────────────────────────────────


def test_the_record_round_trips_as_one_json_line() -> None:
    record = decide(_context(), [PUBLISH, TOOL, NOTHING], BaselinePolicy(),
                    constraints=[_forbid("publish")],
                    approval_gates=[_hold(ActionClass.TOOL_CALL)])
    line = record.to_json()
    assert DecisionRecord.from_json(line) == record
    assert DecisionRecord.from_json(line).to_json() == line


def test_duplicate_candidate_ids_are_the_runtimes_error() -> None:
    # Nothing could tell which of the two a rejection or a selection meant.
    with pytest.raises(ValueError):
        decide(_context(), [PUBLISH, PUBLISH], BaselinePolicy())


@pytest.mark.parametrize("field", ["checkpoint", "mode"])
def test_a_context_naming_its_checkpoint_or_mode_by_text_is_the_runtimes_error(
    field: str,
) -> None:
    # Raised before any step runs: a broken step's log line, which names the
    # checkpoint, must not itself raise with the step's error chained to it.
    context = dataclasses.replace(_context(), **{field: getattr(_context(), field).value})
    with pytest.raises(TypeError) as caught:
        decide(context, [PUBLISH], BaselinePolicy(), constraints=[_raises])
    assert "launch" not in "".join(traceback.format_exception(caught.value))
