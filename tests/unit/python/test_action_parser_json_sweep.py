"""No JSON value at all makes the action parser raise (ISSUE-0206).

The seeded sweep that goes with ``test_action_parser_wrong_shapes.py``:
that file pins each wrong shape by name, this one generates thousands of
replies and checks that none of them raises in the parser or in reply
synthesis.
"""

from __future__ import annotations

import json
import random
from typing import Any

from agents.llm_client import LLMResponse
from agents.persona_runtime.action_parser import parse_actions
from agents.persona_runtime.channel_reply import synthesize_channel_reply
from agents.persona_types import ActionType, AgentAction, AgentEvent, EventType


def _fenced(value: Any) -> str:
    body = value if isinstance(value, str) else json.dumps(value)
    return f"```json\n{body}\n```"


def _parse(text: str) -> list[AgentAction]:
    return parse_actions(LLMResponse(text=text))


_ACTION_TYPES: list[Any] = [t.value for t in ActionType] + [
    "no_such_action", "", 5, None, True, ["complete_task"], {"a": 1},
]
_FIELDS = [
    "channel_id", "content", "result", "agent_id", "task", "role", "mentions",
    "metadata", "max_tokens", "timeout_seconds", "max_llm_calls", "vote",
]


def _value(rng: random.Random, depth: int = 0) -> Any:
    """An arbitrary JSON value, nested at most four deep."""
    scalars: list[Any] = [
        None, True, False, 0, -1, 7, 10**30, 1.5, float("inf"), float("-inf"),
        float("nan"), "", "text", "general", "agent-b", "```json", "[", "\u00e9\U0001f600",
    ]
    roll = rng.random()
    if depth >= 4 or roll < 0.5:
        return rng.choice(scalars)
    if roll < 0.75:
        return [_value(rng, depth + 1) for _ in range(rng.randint(0, 4))]
    return {
        rng.choice([*_FIELDS, "action_type", "payload", "x"]): _value(rng, depth + 1)
        for _ in range(rng.randint(0, 4))
    }


_CHANNELS = ("dm:user-1:ember-owl", "group:eng")


def _action_like(rng: random.Random) -> Any:
    """Something close to an action, wrong in one or more places, or now
    and then a well-formed message or vote, so that the code behind a
    usable action runs too."""
    roll = rng.random()
    if roll < 0.12:
        return {"action_type": "send_channel_message", "payload": {
            "channel_id": rng.choice(_CHANNELS), "content": "hello",
            "mentions": _value(rng, 3),
        }}
    if roll < 0.24:
        return {"action_type": "end_interaction_vote", "payload": (
            {f: _value(rng, 3) for f in rng.sample(["content", "channel_id", "x"], 2)}
            if rng.random() < 0.7 else _value(rng, 2)
        )}
    action: dict[str, Any] = {}
    if rng.random() < 0.9:
        action["action_type"] = rng.choice(_ACTION_TYPES)
    if rng.random() < 0.9:
        action["payload"] = (
            {f: _value(rng, 2) for f in rng.sample(_FIELDS, rng.randint(0, 5))}
            if rng.random() < 0.6
            else _value(rng, 2)
        )
    return action


def _reply(rng: random.Random) -> str:
    roll = rng.random()
    if roll < 0.6:
        value: Any = [
            _action_like(rng) if rng.random() < 0.7 else _value(rng, 1)
            for _ in range(rng.randint(0, 5))
        ]
    elif roll < 0.8:
        value = _action_like(rng)
    else:
        value = _value(rng)
    text = json.dumps(value)  # NaN and Infinity are written as Python reads them
    wrap = rng.random()
    if wrap < 0.4:
        return text
    if wrap < 0.7:
        return _fenced(text)
    if wrap < 0.85:
        return f"Here is what I will do.\n{_fenced(text)}\nDone."
    return f"```json {text} ```"


def test_no_json_value_makes_the_parser_or_reply_synthesis_raise() -> None:
    """Five thousand generated replies from a fixed seed.

    The repository has no property-testing library, so this is a plain
    seeded sweep: every reply is valid JSON, most are wrong somewhere.
    Each must parse to at least one action with an object payload, and
    reply synthesis must accept the result on a DM and on a group channel.
    The group channel's id starts ``group:``, which is what the code that
    folds prose into an end vote looks for; the counts at the end fail
    the test if the sweep stops reaching that code.
    """
    rng = random.Random(206)
    events = [
        AgentEvent(
            event_type=EventType.CHANNEL_MESSAGE, payload={"content": "hello"},
            sender_id="user-1", channel_id=channel_id,
        )
        for channel_id in _CHANNELS
    ]
    votes_with_prose = sends = 0
    for _ in range(5_000):
        text = _reply(rng)
        try:
            actions = _parse(text)
            for event in events:
                synthesize_channel_reply(event, actions, agent_id="ember-owl")
        except Exception as exc:
            raise AssertionError(f"{type(exc).__name__}: {exc} for reply {text!r}") from exc
        assert actions, text
        for action in actions:
            assert isinstance(action.action_type, ActionType), text
            assert isinstance(action.payload, dict), text
        kinds = {a.action_type for a in actions}
        sends += ActionType.SEND_CHANNEL_MESSAGE in kinds
        votes_with_prose += {
            ActionType.END_INTERACTION_VOTE, ActionType.COMPLETE_TASK,
        } <= kinds
    assert sends >= 100, sends
    assert votes_with_prose >= 20, votes_with_prose
