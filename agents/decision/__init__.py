"""The Decision Policy Engine (RFC 0028).

The types every decision checkpoint shares, in task agents and persona
agents alike; the class of each runtime action; and the selection pipeline
that settles which candidate runs, with manual mode's baseline policy. No
runtime calls this package yet; the PR plan (``docs/rfcs/0028-pr-plan.md``)
wires it in. Nothing here may import a runtime loop, so both loops can
import it.
"""

from .action_classes import (
    ACTION_TYPE_CLASSES,
    STOP_REASON_CLASSES,
    class_of_action,
    class_of_step,
)
from .baseline import BaselinePolicy
from .decision_types import (
    RECORD_FORMAT_VERSION,
    ActionClass,
    Checkpoint,
    DecisionCandidate,
    DecisionContext,
    DecisionOutcome,
    DecisionPolicy,
    DecisionRecord,
    DecisionSubject,
    DirectiveVerdict,
    PolicyMode,
    Rejection,
    hash_arguments,
    hash_content,
)
from .pipeline import ApprovalGate, ConstraintInput, decide

__all__ = [
    "ACTION_TYPE_CLASSES",
    "RECORD_FORMAT_VERSION",
    "STOP_REASON_CLASSES",
    "ActionClass",
    "ApprovalGate",
    "BaselinePolicy",
    "Checkpoint",
    "ConstraintInput",
    "DecisionCandidate",
    "DecisionContext",
    "DecisionOutcome",
    "DecisionPolicy",
    "DecisionRecord",
    "DecisionSubject",
    "DirectiveVerdict",
    "PolicyMode",
    "Rejection",
    "class_of_action",
    "class_of_step",
    "decide",
    "hash_arguments",
    "hash_content",
]
