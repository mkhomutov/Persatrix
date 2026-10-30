"""The selection pipeline and manual mode's policy (RFC 0028 §C; PR plan PR 2).

At a decision checkpoint the pipeline takes what the runtime proposes,
removes what a constraint input forbids, lets the policy pick among the rest,
and holds the pick when an approval gate says it needs a person. Phase 1
wires no constraint input and no approval gate (E-2): those arrive with
PR 7, PR 9, B3 and RFC 0012 C3, so these tests drive both steps with fakes.
"""

from __future__ import annotations

import itertools
import logging
from collections.abc import Sequence

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
    record = decide(_context(), [proposal], BaselinePolicy())
    outcome = record.outcome
    assert (outcome.action_class, outcome.selected_id) == (proposal.action_class, "c0")
    assert outcome.reason == "runtime_proposal"
    assert outcome.confidence is None  # manual mode does not score
    assert outcome.rejected == ()
    assert record.candidates == (proposal,)
    assert record.context == _context()


def test_the_baseline_ranks_by_the_runtimes_order_alone() -> None:
    # Not by id, class or content: the runtime lists its own proposal first.
    assert decide(_context(), [NOTHING, PUBLISH, TOOL], BaselinePolicy()).outcome.selected_id \
        == "nothing"
    assert decide(_context(), [TOOL, NOTHING, PUBLISH], BaselinePolicy()).outcome.selected_id \
        == "tool"


def test_deciding_again_gives_the_same_outcome() -> None:
    records = [
        decide(_context(), [PUBLISH, TOOL, NOTHING], BaselinePolicy(),
               constraints=[_forbid("publish")], approval_gates=[_hold(ActionClass.DELEGATE)])
        for _ in range(3)
    ]
    assert {r.outcome for r in records} == {records[0].outcome}
    # Only the record's own id and time are new each time.
    assert len({r.record_id for r in records}) == 3


@pytest.mark.parametrize("mode", [PolicyMode.SEMI_AUTOMATED, PolicyMode.AUTOMATED])
def test_the_baseline_is_manual_modes_policy_only(mode: PolicyMode) -> None:
    # A record names its mode so calibration can tell a scored record from
    # an unscored one; the baseline under another mode would mislabel it.
    with pytest.raises(ValueError):
        BaselinePolicy().select(_context(mode), [PUBLISH])


def test_the_baseline_defers_when_given_nothing() -> None:
    outcome = BaselinePolicy().select(_context(), [])
    assert (outcome.action_class, outcome.selected_id) == (ActionClass.DEFER, None)
    assert outcome.reason == "no_candidate"
    # The same record whether the runtime asks the policy or the pipeline.
    assert outcome == decide(_context(), [], BaselinePolicy()).outcome


def test_the_baseline_rationale_carries_nothing_from_the_candidate() -> None:
    rationales = {decide(_context(), [c], BaselinePolicy()).outcome.rationale for c in _ALL}
    assert len(rationales) == 1
    assert rationales != {""}


# ─── Step 2: constraint inputs remove candidates ───────────


def test_a_constraint_input_removes_the_candidates_it_refuses() -> None:
    record = decide(_context(), [PUBLISH, TOOL], BaselinePolicy(),
                    constraints=[_forbid("publish")])
    assert record.outcome.selected_id == "tool"
    assert record.outcome.rejected == (Rejection("publish", "directive_forbids"),)
    # The record keeps every candidate, the removed one included.
    assert record.candidates == (PUBLISH, TOOL)


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


def test_an_approval_gate_holds_the_selection_and_the_outcome_defers() -> None:
    record = decide(_context(), [PUBLISH, NOTHING], BaselinePolicy(),
                    approval_gates=[_hold(ActionClass.PUBLISH_CHANNEL)])
    # The held candidate is not swapped for the next one: nothing runs.
    assert record.outcome.action_class is ActionClass.DEFER
    assert record.outcome.selected_id is None
    assert record.outcome.reason == "approval_required"
    assert record.outcome.rejected == (Rejection("publish", "approval_required"),)


def test_an_approval_gate_sees_only_the_selection() -> None:
    asked: list[str] = []
    record = decide(_context(), [TOOL, PUBLISH], BaselinePolicy(),
                    approval_gates=[_noting_gate(asked)])
    assert asked == ["tool"]
    assert record.outcome.selected_id == "tool"


def test_the_first_approval_gate_to_hold_names_the_reason() -> None:
    record = decide(_context(), [PUBLISH], BaselinePolicy(),
                    approval_gates=[_hold(ActionClass.DELEGATE),
                                    _hold(ActionClass.PUBLISH_CHANNEL, "hitl_required"),
                                    _hold(ActionClass.PUBLISH_CHANNEL)])
    assert record.outcome.reason == "hitl_required"
    assert record.outcome.rejected == (Rejection("publish", "hitl_required"),)


def test_a_held_selection_keeps_the_policys_confidence_and_rationale() -> None:
    scored = _Picks(DecisionOutcome(ActionClass.PUBLISH_CHANNEL, selected_id="publish",
                                    confidence=0.75, rationale="highest score"))
    record = decide(_context(PolicyMode.SEMI_AUTOMATED), [PUBLISH], scored,
                    approval_gates=[_hold(ActionClass.PUBLISH_CHANNEL)])
    assert (record.outcome.confidence, record.outcome.rationale) == (0.75, "highest score")


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


@pytest.mark.parametrize("constraint", [_raises, _says_text], ids=["raises", "text-reason"])
def test_a_constraint_input_that_breaks_removes_its_candidate(
    constraint: ConstraintInput, caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.ERROR, logger="agents.decision.pipeline"):
        record = decide(_context(), [PUBLISH, TOOL], BaselinePolicy(),
                        constraints=[_forbid("tool"), constraint])
    assert record.outcome.rejected == (Rejection("publish", "constraint_error"),
                                       Rejection("tool", "directive_forbids"))
    # Nothing is left, and a check broke: not the same as nothing permitted.
    assert record.outcome.reason == "constraint_error"
    # E-10: text a check returns or raises reaches neither the record nor the log.
    assert "launch" not in record.to_json()
    assert "launch" not in caplog.text
    assert "ember-owl" in caplog.text
    # The error's type is logged, so an operator can tell what broke.
    assert ("RuntimeError" if constraint is _raises else "ValueError") in caplog.text


def test_a_constraint_input_that_breaks_on_one_candidate_leaves_the_rest() -> None:
    def breaks_on_publish(context: DecisionContext, candidate: DecisionCandidate) -> str | None:
        return _raises(context, candidate) if candidate.candidate_id == "publish" else None

    record = decide(_context(), [PUBLISH, TOOL], BaselinePolicy(),
                    constraints=[breaks_on_publish])
    assert record.outcome.selected_id == "tool"
    assert record.outcome.rejected == (Rejection("publish", "constraint_error"),)


class _Raises:
    def select(self, context: DecisionContext,
               candidates: Sequence[DecisionCandidate]) -> DecisionOutcome:
        raise RuntimeError("the policy is down: the launch is on Friday")


@pytest.mark.parametrize(
    "policy",
    [
        _Raises(),
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
    ids=["raises", "removed", "unknown", "wrong-class", "no-selection", "foreign-rejection",
         "no-outcome", "scored-in-manual"],
)
def test_a_policy_that_breaks_defers_and_no_gate_is_asked(
    policy: DecisionPolicy, caplog: pytest.LogCaptureFixture,
) -> None:
    asked: list[str] = []
    with caplog.at_level(logging.ERROR, logger="agents.decision.pipeline"):
        record = decide(_context(), [PUBLISH, TOOL], policy,
                        constraints=[_forbid("tool")], approval_gates=[_noting_gate(asked)])
    assert record.outcome == DecisionOutcome(
        ActionClass.DEFER, reason="policy_error",
        rejected=(Rejection("tool", "directive_forbids"),),
    )
    assert asked == []
    assert "launch" not in caplog.text
    assert "ember-owl" in caplog.text


def test_a_scored_pick_stands_outside_manual_mode() -> None:
    scored = DecisionOutcome(ActionClass.PUBLISH_CHANNEL, selected_id="publish", confidence=0.8)
    record = decide(_context(PolicyMode.SEMI_AUTOMATED), [PUBLISH], _Picks(scored))
    assert record.outcome == scored


@pytest.mark.parametrize("gate", [_raises, _says_text], ids=["raises", "text-reason"])
def test_an_approval_gate_that_breaks_holds_the_selection(
    gate: ApprovalGate, caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.ERROR, logger="agents.decision.pipeline"):
        record = decide(_context(), [PUBLISH, NOTHING], BaselinePolicy(),
                        approval_gates=[gate, _hold(ActionClass.PUBLISH_CHANNEL)])
    assert record.outcome.action_class is ActionClass.DEFER
    assert record.outcome.reason == "approval_gate_error"
    assert record.outcome.rejected == (Rejection("publish", "approval_gate_error"),)
    assert "launch" not in record.to_json()
    assert "launch" not in caplog.text
    assert "ember-owl" in caplog.text


def test_the_baseline_under_another_mode_defers_through_the_pipeline() -> None:
    record = decide(_context(PolicyMode.SEMI_AUTOMATED), [PUBLISH], BaselinePolicy())
    assert record.outcome == DecisionOutcome(ActionClass.DEFER, reason="policy_error")


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
