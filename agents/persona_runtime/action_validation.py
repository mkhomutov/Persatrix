"""LLM action-payload validation (extracted from action_loop.py).

Enforces required fields per :class:`ActionType` so malformed LLM
output cannot reach downstream execution. Invalid actions are
replaced with :class:`ActionType.DO_NOTHING` (PR #54 review: unvalidated
payloads).

Split from ``action_loop.py`` to keep that module under the 500-line
review-friendly cap (RFC 0011 PR 4b).
"""

from __future__ import annotations

import logging
import re
from typing import Final

from ..persona_types import ActionType, AgentAction

logger = logging.getLogger(__name__)

__all__ = ["validate_action_payload"]


# Hard upper bounds for LLM-provided SPAWN_SUB_AGENT resource fields.
# Caps are enforced at validation time so the boundary is in place when
# execution is wired in a future RFC.
_MAX_SUB_AGENT_TOKENS: Final[int] = 100_000
_MAX_SUB_AGENT_TIMEOUT_SECONDS: Final[int] = 3_600  # 1 hour
_MAX_SUB_AGENT_LLM_CALLS: Final[int] = 50

# Agent ID format shared with server.py — cross-component contract.
_AGENT_ID_RE: Final[re.Pattern[str]] = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")


def validate_action_payload(action: AgentAction) -> AgentAction:
    """Validate ``action``; return DO_NOTHING when required fields are missing.

    Mutates ``action.payload`` in place to clamp out-of-range numeric
    resource fields (SPAWN_SUB_AGENT). Returns either the (possibly
    clamped) original action or a fresh ``DO_NOTHING`` replacement.
    """
    p = action.payload
    match action.action_type:
        case ActionType.DELEGATE:
            agent_id = p.get("agent_id")
            if not isinstance(agent_id, str) or not _AGENT_ID_RE.match(agent_id):
                logger.warning(
                    "DELEGATE action has invalid agent_id %r, replacing with DO_NOTHING",
                    agent_id,
                )
                return AgentAction(ActionType.DO_NOTHING, {})
            if not isinstance(p.get("task"), str) or not p["task"].strip():
                logger.warning(
                    "DELEGATE action missing non-empty 'task', replacing with DO_NOTHING",
                )
                return AgentAction(ActionType.DO_NOTHING, {})
        case ActionType.SEND_CHANNEL_MESSAGE:
            if not isinstance(p.get("channel_id"), str) or not p["channel_id"].strip():
                logger.warning(
                    "SEND_CHANNEL_MESSAGE missing non-empty 'channel_id',"
                    " replacing with DO_NOTHING",
                )
                return AgentAction(ActionType.DO_NOTHING, {})
            if not isinstance(p.get("content"), str) or not p["content"].strip():
                logger.warning(
                    "SEND_CHANNEL_MESSAGE missing non-empty 'content',"
                    " replacing with DO_NOTHING",
                )
                return AgentAction(ActionType.DO_NOTHING, {})
        case ActionType.SPAWN_SUB_AGENT:
            if not isinstance(p.get("role"), str) or not p["role"].strip():
                logger.warning(
                    "SPAWN_SUB_AGENT missing non-empty 'role',"
                    " replacing with DO_NOTHING",
                )
                return AgentAction(ActionType.DO_NOTHING, {})
            if not isinstance(p.get("task"), str) or not p["task"].strip():
                logger.warning(
                    "SPAWN_SUB_AGENT missing non-empty 'task',"
                    " replacing with DO_NOTHING",
                )
                return AgentAction(ActionType.DO_NOTHING, {})
            for field_name, cap in (
                ("max_tokens", _MAX_SUB_AGENT_TOKENS),
                ("timeout_seconds", _MAX_SUB_AGENT_TIMEOUT_SECONDS),
                ("max_llm_calls", _MAX_SUB_AGENT_LLM_CALLS),
            ):
                if field_name in p:
                    try:
                        val = int(p[field_name])
                    except (TypeError, ValueError):
                        logger.warning(
                            "SPAWN_SUB_AGENT %s is not numeric (%r), removing",
                            field_name, p[field_name],
                        )
                        del p[field_name]
                        continue
                    if val > cap:
                        logger.warning(
                            "SPAWN_SUB_AGENT %s %d exceeds cap %d, clamping",
                            field_name, val, cap,
                        )
                        p[field_name] = cap
        case _:
            pass  # COMPLETE_TASK, DO_NOTHING, approvals — no payload constraints
    return action
