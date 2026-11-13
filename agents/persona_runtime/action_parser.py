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
from collections import Counter

from ..llm_client import LLMResponse
from ..persona_types import ActionType, AgentAction
from .action_validation import validate_action_payload

__all__ = ["parse_actions"]

logger = logging.getLogger(__name__)


#: What a JSON value is called, for a log line an operator reads.
_JSON_KINDS: dict[type, str] = {
    dict: "an object", list: "a list", str: "a string", bool: "a boolean",
    int: "a number", float: "a number", type(None): "null",
}

#: How many unknown ``action_type`` values one log line quotes, and how
#: much of each.  The model writes them, so neither is bounded otherwise.
_QUOTED_TYPES = 3
_QUOTED_CHARS = 40
_UNKNOWN_TYPE = "an unknown action_type"


def _json_kind(value: object) -> str:
    return _JSON_KINDS.get(type(value), type(value).__name__)


def _log_dropped(
    dropped: Counter[str], unknown_types: list[str], total: int,
) -> None:
    """One WARNING for a whole reply, however many items it dropped."""
    reasons = []
    for why, count in dropped.items():
        reason = f"{why}: {count}"
        if why == _UNKNOWN_TYPE:
            reason += " (such as " + ", ".join(unknown_types) + ")"
        reasons.append(reason)
    logger.warning(
        "Dropped %d of %d items from the reply's action list: %s",
        sum(dropped.values()), total, "; ".join(reasons),
    )


def _raw_text(text: str) -> list[AgentAction]:
    """The fallback: one ``COMPLETE_TASK`` carrying the reply as written."""
    return [AgentAction(
        action_type=ActionType.COMPLETE_TASK,
        payload={"result": text},
    )]


def parse_actions(response: LLMResponse) -> list[AgentAction]:
    """Parse the LLM response text into a list of ``AgentAction``.

    The LLM is expected to return a JSON array of actions: a list of
    objects, each with an ``action_type`` string and an object
    ``payload``. Falls back to a single ``COMPLETE_TASK`` with the raw
    text when the reply is anything else: text that is not JSON, or JSON
    of another shape (one action object, a number, a list with no usable
    action in it). Parsed actions are validated per action type before
    returning. No reply text makes this function raise (ISSUE-0206).

    Inside a list, an item that cannot be an action is dropped and the
    rest are kept: an item that is not an object, one whose
    ``action_type`` is not a known string, one whose ``payload`` is not
    an object. Dropping it, and not standing a ``DO_NOTHING`` in for it,
    lets a list with no usable item reach the raw-text fallback instead
    of reading as deliberate silence. One WARNING per reply says how
    many items were dropped and why.

    Dropping has a cost the log line is the only trace of: the words in
    a dropped message are lost, and a vote can be published without the
    synthesis that was meant to go with it.

    The one exception is an explicit ``do_nothing``. The model wrote out
    that it stays silent, and nothing reads that action's payload, so it
    is kept whatever the payload holds. An item with no ``action_type``
    reads as ``do_nothing`` only when its payload is an object or absent.

    Non-empty prose surrounding a fenced ```json block whose actions
    include an ``END_INTERACTION_VOTE`` is preserved as a trailing
    ``COMPLETE_TASK`` carrying it in ``payload["result"]``, which
    ``channel_reply.fold_prose_into_end_vote`` then folds INTO the vote's
    ``content`` (ISSUE-0097 defect 2) — the RFC 0030 chair-stall escalation
    rescue: a chair (or a concurrer) that writes its synthesis/agreement as
    prose beside the vote block (against the ``chair-escalation`` snippet
    guidance) must not lose the text from the channel record, and it travels
    inside the single vote publish rather than as a separate message. The
    vote is the *only* shape that preserves
    prose (PR 610 review): beside any other block, prose is overwhelmingly
    schema narration ("Here are my actions:") or a narrated decision to
    stay silent, and a preserved ``COMPLETE_TASK`` would let the promotion
    seam publish boilerplate — or stamp a post over a deliberate
    ``do_nothing`` silence, defeating the ``reply-discretion`` affordance.
    """
    text = response.text or ""
    surrounding_prose = ""
    try:
        stripped = text.strip()
        if stripped.startswith("["):
            raw_actions = json.loads(stripped)
        elif "```json" in stripped:
            # Extract the first JSON code block — more robust than
            # str.index() against nested fences (review finding P-1).
            # The fence markers and the JSON may share a single line
            # (```json [..] ```) or sit on their own lines (```json\n..\n```),
            # and the inner edges may carry stray spaces/tabs or CRLF: the
            # chair's forced-turn end_interaction_vote was published RAW as a
            # one-line ```json [..] ``` because the prior newline-only anchor
            # (```json\n..\n```) could not match it, so the structured vote was
            # lost and the room saw a double-synthesis (ISSUE-0101 /
            # MT-CHANNEL-GOV-004 Edge Case 2). Possessive whitespace
            # quantifiers ([ \t]*+) keep the match linear on pathological
            # many-backtick input — the polynomial-backtracking guard the
            # newline anchor originally bought (PR #54 review), now without the
            # one-line false negative.
            m = re.search(
                r"```json[ \t]*+\r?\n?(.*?)\r?\n?[ \t]*+```", stripped, re.DOTALL,
            )
            if m is None:
                return _raw_text(text)
            raw_actions = json.loads(m.group(1).strip())
            prose_parts = (stripped[: m.start()], stripped[m.end():])
            surrounding_prose = "\n\n".join(
                part.strip() for part in prose_parts if part.strip()
            )
        else:
            return _raw_text(text)

        if not isinstance(raw_actions, list):
            # Valid JSON that is not a list: one action object in a fence,
            # a number, null. Only a fenced block gets here; the same
            # object with no fence took the ``else`` above.
            logger.warning(
                "The reply's fenced JSON is %s, not a list of actions; "
                "using the reply's text",
                _json_kind(raw_actions),
            )
            return _raw_text(text)

        actions: list[AgentAction] = []
        dropped: Counter[str] = Counter()
        unknown_types: list[str] = []
        for raw in raw_actions:
            if not isinstance(raw, dict):
                dropped[f"{_json_kind(raw)} where an action object belongs"] += 1
                continue
            try:
                # A value that is not a string (a number, null, a list) is
                # no member either, and raises the same ValueError.
                action_type = ActionType(raw.get("action_type", "do_nothing"))
            except ValueError:
                dropped[_UNKNOWN_TYPE] += 1
                if len(unknown_types) < _QUOTED_TYPES:
                    unknown_types.append(repr(raw.get("action_type"))[:_QUOTED_CHARS])
                continue
            payload = raw.get("payload", {})
            if not isinstance(payload, dict):
                if raw.get("action_type") == ActionType.DO_NOTHING.value:
                    # Explicit silence; see the docstring.
                    actions.append(AgentAction(ActionType.DO_NOTHING, {}))
                    continue
                # The validator would stand a DO_NOTHING in for it; see the
                # docstring for why the parser drops it.
                dropped[
                    f"a {action_type.value} whose payload is {_json_kind(payload)}"
                ] += 1
                continue
            # Validate payload per action type (PR #54 review: unvalidated
            # LLM output). Full ActionExecutor validation deferred to PR 5b;
            # this enforces required-field constraints at parse time.
            validated = validate_action_payload(AgentAction(
                action_type=action_type,
                payload=payload,
            ))
            actions.append(validated)
        if dropped:
            _log_dropped(dropped, unknown_types, len(raw_actions))
        if not actions:
            # Full-raw-text fallback wins over the prose seam below: with
            # every parsed action dropped, the prose alone would be a lossy
            # echo of the turn (the fence content vanishes from the record).
            return _raw_text(text)
        if surrounding_prose and any(
            a.action_type is ActionType.END_INTERACTION_VOTE for a in actions
        ):
            actions.append(AgentAction(
                action_type=ActionType.COMPLETE_TASK,
                payload={"result": surrounding_prose},
            ))
        return actions

    except (json.JSONDecodeError, ValueError, RecursionError):
        # RecursionError: JSON nested deeper than the decoder will follow.
        return _raw_text(text)
