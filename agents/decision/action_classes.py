"""Which of §A's action classes each runtime action belongs to (PR plan E-4).

A decision checkpoint reasons about six classes of action, but a persona
emits ``ActionType`` members and a task agent's step ends in a
``StopReason``. These two tables translate, so a record names the class of
whatever the runtime was about to do, and an approval list or a gate written
against class names covers every action of that class. A new ``ActionType``
or ``StopReason`` member fails a unit test until it is given a class here.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from ..llm_types import StopReason
from ..persona_types import ActionType
from .decision_types import ActionClass

__all__ = [
    "ACTION_TYPE_CLASSES",
    "STOP_REASON_CLASSES",
    "class_of_action",
    "class_of_step",
]

ACTION_TYPE_CLASSES: Mapping[ActionType, ActionClass] = MappingProxyType({
    ActionType.SEND_CHANNEL_MESSAGE: ActionClass.PUBLISH_CHANNEL,
    # A persona run as a workflow step answers its task with this.
    ActionType.COMPLETE_TASK: ActionClass.RESPOND,
    ActionType.DELEGATE: ActionClass.DELEGATE,
    # Starting a sub-agent hands it part of the work: §A's pre-delegate case.
    ActionType.SPAWN_SUB_AGENT: ActionClass.DELEGATE,
    ActionType.USE_TOOL: ActionClass.TOOL_CALL,
    # The approval actions return not_implemented today. Asking for approval
    # asks another party before going on; granting or denying answers one.
    ActionType.REQUEST_APPROVAL: ActionClass.ASK_CLARIFICATION,
    ActionType.GRANT_APPROVAL: ActionClass.RESPOND,
    ActionType.DENY_APPROVAL: ActionClass.RESPOND,
    ActionType.DO_NOTHING: ActionClass.DEFER,
    # The vote goes out as a channel message other participants read
    # (agents/end_vote_action.py), so it is a publish like any other.
    ActionType.END_INTERACTION_VOTE: ActionClass.PUBLISH_CHANNEL,
})

STOP_REASON_CLASSES: Mapping[StopReason, ActionClass] = MappingProxyType({
    # The model's final reply is the task's answer.
    StopReason.END_TURN: ActionClass.RESPOND,
    StopReason.TOOL_USE: ActionClass.TOOL_CALL,
    # A truncated reply is not used: the step fails without acting.
    StopReason.MAX_TOKENS: ActionClass.DEFER,
})


def class_of_action(action_type: ActionType) -> ActionClass:
    """The class of a persona action."""
    return ACTION_TYPE_CLASSES[action_type]


def class_of_step(stop_reason: StopReason) -> ActionClass:
    """The class of what a task agent's model step chose."""
    return STOP_REASON_CLASSES[stop_reason]
