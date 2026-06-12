"""Parse an LLM response into a list of ``AgentAction``.

Free-function helper split out of ``action_loop.py`` so that file stays
under the 500-line review limit, mirroring the existing
``channel_ingest`` / ``channel_reply`` / ``wallet_cause`` splits. The
implementation has no ``self`` access — it operates purely on the
response text — so the move is mechanical.
"""

from __future__ import annotations

import json
import logging
import re

from ..llm_client import LLMResponse
from ..persona_types import ActionType, AgentAction
from .action_validation import validate_action_payload

__all__ = ["parse_actions"]

logger = logging.getLogger(__name__)


def parse_actions(response: LLMResponse) -> list[AgentAction]:
    """Parse the LLM response text into a list of ``AgentAction``.

    The LLM is expected to return a JSON array of actions. Falls back to
    a single ``COMPLETE_TASK`` with the raw text if parsing fails. Parsed
    actions are validated per action type before returning.

    Non-empty prose surrounding a fenced ```json block is preserved as a
    trailing ``COMPLETE_TASK`` carrying it in ``payload["result"]``, so
    ``channel_reply.synthesize_channel_reply`` can promote it into a
    channel publish. Notably for the RFC 0030 chair-stall escalation: a
    chair that writes its synthesis as prose beside the vote block (against
    the ``chair-escalation`` snippet guidance) must not lose the synthesis
    from the channel record.
    """
    text = response.text or ""
    surrounding_prose = ""
    try:
        stripped = text.strip()
        if stripped.startswith("["):
            raw_actions = json.loads(stripped)
        elif "```json" in stripped:
            # Use regex to extract the first JSON code block — more robust
            # than str.index() against nested fences (review finding P-1).
            # Newline anchors (not \s*) to avoid polynomial backtracking on
            # pathological input with many backtick sequences (PR #54 review).
            m = re.search(r"```json\n(.*?)\n```", stripped, re.DOTALL)
            if m is None:
                return [AgentAction(
                    action_type=ActionType.COMPLETE_TASK,
                    payload={"result": text},
                )]
            raw_actions = json.loads(m.group(1))
            prose_parts = (stripped[: m.start()], stripped[m.end():])
            surrounding_prose = "\n\n".join(
                part.strip() for part in prose_parts if part.strip()
            )
        else:
            return [AgentAction(
                action_type=ActionType.COMPLETE_TASK,
                payload={"result": text},
            )]

        actions: list[AgentAction] = []
        for raw in raw_actions:
            try:
                action_type = ActionType(raw.get("action_type", "do_nothing"))
            except ValueError:
                logger.warning("Unknown action_type %r, skipping", raw.get("action_type"))
                continue
            # Validate payload per action type (PR #54 review: unvalidated
            # LLM output). Full ActionExecutor validation deferred to PR 5b;
            # this enforces required-field constraints at parse time.
            validated = validate_action_payload(AgentAction(
                action_type=action_type,
                payload=raw.get("payload", {}),
            ))
            actions.append(validated)
        if not actions:
            # Full-raw-text fallback wins over the prose seam below: with
            # every parsed action dropped, the prose alone would be a lossy
            # echo of the turn (the fence content vanishes from the record).
            return [AgentAction(
                action_type=ActionType.COMPLETE_TASK,
                payload={"result": text},
            )]
        if surrounding_prose:
            actions.append(AgentAction(
                action_type=ActionType.COMPLETE_TASK,
                payload={"result": surrounding_prose},
            ))
        return actions

    except (json.JSONDecodeError, ValueError):
        return [AgentAction(
            action_type=ActionType.COMPLETE_TASK,
            payload={"result": text},
        )]
