"""Every runtime action has one of §A's classes (RFC 0028 PR plan, PR 1, E-4).

A persona emits ``ActionType`` members and a task agent's step ends in a
``StopReason``; a record, an approval list and a gate all speak in classes.
A new member of either enum fails here until it is given a class.
"""

from __future__ import annotations

import pytest

from agents.decision import (
    ACTION_TYPE_CLASSES,
    STOP_REASON_CLASSES,
    ActionClass,
    class_of_action,
    class_of_step,
)
from agents.llm_types import StopReason
from agents.persona_types import ActionType

EXPECTED_ACTION_CLASSES = {
    ActionType.SEND_CHANNEL_MESSAGE: ActionClass.PUBLISH_CHANNEL,
    ActionType.COMPLETE_TASK: ActionClass.RESPOND,
    ActionType.DELEGATE: ActionClass.DELEGATE,
    ActionType.SPAWN_SUB_AGENT: ActionClass.DELEGATE,
    ActionType.USE_TOOL: ActionClass.TOOL_CALL,
    ActionType.REQUEST_APPROVAL: ActionClass.ASK_CLARIFICATION,
    ActionType.GRANT_APPROVAL: ActionClass.RESPOND,
    ActionType.DENY_APPROVAL: ActionClass.RESPOND,
    ActionType.DO_NOTHING: ActionClass.DEFER,
    ActionType.END_INTERACTION_VOTE: ActionClass.PUBLISH_CHANNEL,
}


def test_every_action_type_member_has_a_class() -> None:
    # A new ActionType member fails here until it is given a class.
    assert set(ACTION_TYPE_CLASSES) == set(ActionType)


@pytest.mark.parametrize(("action_type", "expected"), list(EXPECTED_ACTION_CLASSES.items()))
def test_action_type_class(action_type: ActionType, expected: ActionClass) -> None:
    assert class_of_action(action_type) is expected


def test_every_task_agent_step_has_a_class() -> None:
    assert set(STOP_REASON_CLASSES) == set(StopReason)
    assert class_of_step(StopReason.END_TURN) is ActionClass.RESPOND
    assert class_of_step(StopReason.TOOL_USE) is ActionClass.TOOL_CALL
    assert class_of_step(StopReason.MAX_TOKENS) is ActionClass.DEFER


def test_the_class_tables_are_read_only() -> None:
    with pytest.raises(TypeError):
        ACTION_TYPE_CLASSES[ActionType.DO_NOTHING] = ActionClass.RESPOND  # type: ignore[index]
