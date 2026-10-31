"""Manual mode's policy (RFC 0028 §B; PR plan PR 2, E-1 and E-2).

``BaselinePolicy`` selects the first candidate it is given, which the
runtime lists first, and scores nothing, so with no constraint input and no
approval gate a checkpoint changes no choice. The pipeline that runs it is
in test_decision_pipeline.py.
"""

from __future__ import annotations

import pytest

from agents.decision import (
    ActionClass,
    BaselinePolicy,
    Checkpoint,
    DecisionCandidate,
    DecisionContext,
    DecisionOutcome,
    PolicyMode,
    class_of_action,
    decide,
    hash_content,
)
from agents.persona_types import ActionType


def _context(mode: PolicyMode = PolicyMode.MANUAL) -> DecisionContext:
    return DecisionContext(checkpoint=Checkpoint.PRE_ACT, agent_id="ember-owl", mode=mode)


def _candidate(candidate_id: str, action_type: ActionType) -> DecisionCandidate:
    return DecisionCandidate(
        candidate_id=candidate_id,
        action_class=class_of_action(action_type),
        action=action_type.value,
        content_hash=hash_content(f"text of {candidate_id}"),
    )


PUBLISH = _candidate("publish", ActionType.SEND_CHANNEL_MESSAGE)
TOOL = _candidate("tool", ActionType.USE_TOOL)
NOTHING = _candidate("nothing", ActionType.DO_NOTHING)


def test_the_baseline_takes_the_first_candidate_unscored() -> None:
    outcome = BaselinePolicy().select(_context(), [TOOL, PUBLISH])
    assert (outcome.action_class, outcome.selected_id, outcome.reason, outcome.confidence) \
        == (ActionClass.TOOL_CALL, "tool", "runtime_proposal", None)


@pytest.mark.parametrize(
    "candidates", [(NOTHING, PUBLISH, TOOL), (TOOL, NOTHING, PUBLISH)], ids=["nothing", "tool"]
)
def test_the_baseline_ranks_by_the_runtimes_order_alone(
    candidates: tuple[DecisionCandidate, ...],
) -> None:
    # Not by id, class or content: the runtime lists its own proposal first.
    assert BaselinePolicy().select(_context(), candidates).selected_id \
        == candidates[0].candidate_id


@pytest.mark.parametrize("mode", [PolicyMode.SEMI_AUTOMATED, PolicyMode.AUTOMATED])
def test_the_baseline_is_manual_modes_policy_only(mode: PolicyMode) -> None:
    # A record names its mode so calibration can tell a scored record from
    # an unscored one; the baseline under another mode would mislabel it.
    with pytest.raises(ValueError):
        BaselinePolicy().select(_context(mode), [PUBLISH])


def test_the_baseline_under_another_mode_defers_through_the_pipeline() -> None:
    record = decide(_context(PolicyMode.SEMI_AUTOMATED), [PUBLISH], BaselinePolicy())
    assert record.outcome == DecisionOutcome(ActionClass.DEFER, reason="policy_error")


def test_the_baseline_given_nothing_gives_the_pipelines_own_outcome() -> None:
    # The same record whether the runtime asks the policy or the pipeline.
    assert BaselinePolicy().select(_context(), []) \
        == decide(_context(), [], BaselinePolicy()).outcome


def test_the_baseline_rationale_carries_nothing_from_the_candidate() -> None:
    rationales = {BaselinePolicy().select(_context(), [c]).rationale
                  for c in (PUBLISH, TOOL, NOTHING)}
    assert len(rationales) == 1
    assert rationales != {""}
