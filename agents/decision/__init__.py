"""The Decision Policy Engine's contract (RFC 0028).

The types every decision checkpoint shares, in task agents and persona
agents alike, and the class of each runtime action. No runtime calls this
package yet; the PR plan (``docs/rfcs/0028-pr-plan.md``) wires it in.
Nothing here may import a runtime loop, so both loops can import it.
"""

from .action_classes import (
    ACTION_TYPE_CLASSES,
    STOP_REASON_CLASSES,
    class_of_action,
    class_of_step,
)
from .types import (
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
    Rejection,
    hash_content,
)

__all__ = [
    "ACTION_TYPE_CLASSES",
    "RECORD_FORMAT_VERSION",
    "STOP_REASON_CLASSES",
    "ActionClass",
    "Checkpoint",
    "DecisionCandidate",
    "DecisionContext",
    "DecisionOutcome",
    "DecisionPolicy",
    "DecisionRecord",
    "DecisionSubject",
    "DirectiveVerdict",
    "Rejection",
    "class_of_action",
    "class_of_step",
    "hash_content",
]
