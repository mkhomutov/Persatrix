"""No reply text makes the action parser raise (ISSUE-0206).

``parse_actions`` expects a JSON list of objects, each with an
``action_type`` and an object ``payload``, and promises that anything
else becomes one ``COMPLETE_TASK`` carrying the raw text.  It kept that
promise only for text that was not JSON.  JSON of the wrong shape (one
action object in a fence, a list of strings, a payload that is a string)
raised, the turn was dropped after the model call was paid for, and the
inbound message left no episode.

The rule these tests pin:

* a reply that is not a list, or has no usable item, is the raw-text
  ``COMPLETE_TASK``;
* in a list, an item that is not an object, or whose payload is not an
  object, is dropped and the others are kept, the same way an item with
  an unknown ``action_type`` always was;
* an explicit ``do_nothing`` is the one exception: it means silence
  whatever its payload holds, as it always did.
"""

from __future__ import annotations

import json
import logging
import random
from typing import Any

import pytest

from agents.base import TaskStatus
from agents.llm_client import LLMResponse
from agents.persona import create_persona_agent
from agents.persona_runtime.action_parser import parse_actions
from agents.persona_runtime.action_validation import validate_action_payload
from agents.persona_runtime.channel_reply import synthesize_channel_reply
from agents.persona_types import ActionType, AgentAction, AgentEvent, EventType

from ._persona_test_helpers import _PERSONA_CONFIG, _make_client, _task

GOOD = {"action_type": "complete_task", "payload": {"result": "hi"}}
GOOD_SEND = {
    "action_type": "send_channel_message",
    "payload": {"channel_id": "general", "content": "hello"},
}

#: Payloads that are valid JSON and not an object.
BAD_PAYLOADS: list[Any] = ["a string", None, ["a", "list"], 3, 1.5, True]


def _fenced(value: Any) -> str:
    body = value if isinstance(value, str) else json.dumps(value)
    return f"```json\n{body}\n```"


def _parse(text: str) -> list[AgentAction]:
    return parse_actions(LLMResponse(text=text))


def _assert_raw_text(text: str) -> None:
    actions = _parse(text)
    assert [(a.action_type, a.payload) for a in actions] == [
        (ActionType.COMPLETE_TASK, {"result": text}),
    ]


# ─── The whole reply is the wrong shape: raw text ────────────────────

_NOT_A_LIST_OF_ACTIONS = {
    # The issue's list.
    "one action object in a fence": _fenced(GOOD),
    "a list of strings": json.dumps(["a", "b"]),
    "a list of numbers": json.dumps([1, 2.5]),
    "a fenced number": _fenced(5),
    "a fenced null": _fenced(None),
    # Their neighbours.
    "a fenced true": _fenced(True),
    "a fenced string": _fenced('"just text"'),
    "a fenced empty object": _fenced({}),
    "a list of nulls": json.dumps([None, None]),
    "a list of booleans": json.dumps([True, False]),
    "a nested list holding an action": json.dumps([[GOOD]]),
    "a fenced nested list": _fenced([[GOOD_SEND]]),
    "an action_type that is a number": json.dumps([{"action_type": 5, "payload": {}}]),
    "an action_type that is null": json.dumps([{"action_type": None, "payload": {}}]),
    "an action_type that is a list": json.dumps(
        [{"action_type": ["complete_task"], "payload": {}}],
    ),
    "an action_type that is an object": json.dumps(
        [{"action_type": {"is": "complete_task"}, "payload": {}}],
    ),
    "an action_type that is true": json.dumps([{"action_type": True, "payload": {}}]),
    # Deep enough that the decoder gives up on Python 3.11 and 3.12 alike.
    "brackets nested 100,000 deep": "[" * 100_000 + "]" * 100_000,
    "objects nested 100,000 deep, fenced": _fenced(
        '{"a":' * 100_000 + "1" + "}" * 100_000,
    ),
}


@pytest.mark.parametrize(
    "text", _NOT_A_LIST_OF_ACTIONS.values(), ids=_NOT_A_LIST_OF_ACTIONS.keys(),
)
def test_a_reply_of_the_wrong_shape_becomes_the_raw_text(text: str) -> None:
    _assert_raw_text(text)


_NOT_SILENCE = [t for t in ActionType if t is not ActionType.DO_NOTHING]


@pytest.mark.parametrize("payload", BAD_PAYLOADS, ids=repr)
@pytest.mark.parametrize("action_type", _NOT_SILENCE, ids=lambda t: t.value)
def test_a_payload_that_is_not_an_object_becomes_the_raw_text(
    action_type: ActionType, payload: Any,
) -> None:
    """Every action type but ``do_nothing``.  Five of them used to raise:
    three in the validator, which reads their fields, and
    ``complete_task`` and ``end_interaction_vote`` later, in reply
    synthesis.  The tool and approval types did not raise: the bad
    payload travelled on and the turn did nothing.  All nine are now an
    unusable item, so a reply holding nothing else is its own text."""
    _assert_raw_text(json.dumps([{"action_type": action_type.value, "payload": payload}]))
    _assert_raw_text(_fenced([{"action_type": action_type.value, "payload": payload}]))


@pytest.mark.parametrize("payload", BAD_PAYLOADS, ids=repr)
def test_an_explicit_do_nothing_is_silence_whatever_its_payload(payload: Any) -> None:
    """The model said to stay silent.  Its payload carries nothing the
    runtime reads, so a wrong one must not turn the silence into the raw
    JSON posted to the channel."""
    for text in (
        json.dumps([{"action_type": "do_nothing", "payload": payload}]),
        _fenced([{"action_type": "do_nothing", "payload": payload}]),
    ):
        assert [(a.action_type, a.payload) for a in _parse(text)] == [
            (ActionType.DO_NOTHING, {}),
        ]


@pytest.mark.parametrize("payload", BAD_PAYLOADS, ids=repr)
def test_an_item_that_names_no_action_and_has_a_bad_payload_is_not_silence(
    payload: Any,
) -> None:
    """Only a ``do_nothing`` the model wrote out is kept.  An item with
    no ``action_type`` reads as ``do_nothing`` when its payload is an
    object or absent; with a payload that is not an object it is one
    more unusable item."""
    _assert_raw_text(json.dumps([{"payload": payload}]))


def test_an_explicit_do_nothing_with_a_bad_payload_keeps_the_good_items() -> None:
    actions = _parse(json.dumps([{"action_type": "do_nothing", "payload": None}, GOOD]))
    assert [a.action_type for a in actions] == [
        ActionType.DO_NOTHING, ActionType.COMPLETE_TASK,
    ]


# ─── One bad item among good ones: the good ones are kept ────────────


@pytest.mark.parametrize(
    "bad",
    [
        "a string",
        7,
        None,
        [GOOD],
        {"action_type": "complete_task", "payload": "not an object"},
        {"action_type": "delegate", "payload": None},
        {"action_type": "send_channel_message", "payload": ["general", "hi"]},
        {"action_type": ["send_channel_message"], "payload": {}},
    ],
    ids=repr,
)
@pytest.mark.parametrize("position", [0, 1, 2])
def test_a_bad_item_is_dropped_and_the_good_ones_are_kept_in_order(
    bad: Any, position: int, caplog: pytest.LogCaptureFixture,
) -> None:
    items: list[Any] = [GOOD_SEND, GOOD]
    items.insert(position, bad)
    with caplog.at_level(logging.WARNING, logger="agents.persona_runtime"):
        actions = _parse(json.dumps(items))
    assert [(a.action_type, a.payload) for a in actions] == [
        (ActionType.SEND_CHANNEL_MESSAGE, GOOD_SEND["payload"]),
        (ActionType.COMPLETE_TASK, GOOD["payload"]),
    ]
    assert caplog.records, "a dropped item is logged"


def _warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        r.getMessage() for r in caplog.records
        if r.levelno == logging.WARNING and r.name.endswith("action_parser")
    ]


def test_dropped_items_are_logged_once_per_reply_with_their_counts(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A model can return thousands of items; the log gets one line."""
    items: list[Any] = ["a string"] * 5_000
    items += [None, 7, {"action_type": "fly", "payload": {}}]
    items += [{"action_type": "delegate", "payload": None}, GOOD]
    with caplog.at_level(logging.WARNING):
        actions = _parse(json.dumps(items))
    assert [a.action_type for a in actions] == [ActionType.COMPLETE_TASK]
    (line,) = _warnings(caplog)
    assert "5004 of 5005" in line
    assert "5000" in line and "a string" in line
    assert "null" in line and "a number" in line
    assert "'fly'" in line
    assert "delegate" in line
    assert "NoneType" not in line, "a Python type name is not a JSON kind"


def test_many_unknown_action_types_do_not_make_the_log_line_long(
    caplog: pytest.LogCaptureFixture,
) -> None:
    items = [{"action_type": f"made-up-{n}-{'x' * 200}", "payload": {}} for n in range(500)]
    with caplog.at_level(logging.WARNING):
        _parse(json.dumps(items))
    (line,) = _warnings(caplog)
    assert "500 of 500" in line
    assert len(line) < 600


@pytest.mark.parametrize(
    ("value", "kind"),
    [(GOOD, "an object"), (None, "null"), (5, "a number"), (True, "a boolean"),
     ('"just text"', "a string")],
    ids=["object", "null", "number", "boolean", "string"],
)
def test_a_fenced_value_that_is_not_a_list_is_logged(
    value: Any, kind: str, caplog: pytest.LogCaptureFixture,
) -> None:
    """One action object in a fence used to end in an error with a
    traceback at the dispatch boundary.  It is the raw text now, and the
    operator still needs a line saying the reply was not read as
    actions."""
    with caplog.at_level(logging.WARNING):
        _assert_raw_text(_fenced(value))
    (line,) = _warnings(caplog)
    assert kind in line
    assert "not a list" in line


def test_a_reply_read_in_full_logs_nothing(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        _parse(json.dumps([GOOD_SEND, GOOD, {"action_type": "do_nothing", "payload": None}]))
    assert _warnings(caplog) == []


def test_a_dropped_item_does_not_become_a_silent_do_nothing() -> None:
    """A list whose only item is malformed must reach the raw-text
    fallback.  Standing in a ``DO_NOTHING`` for it would make the list
    non-empty and turn a garbled reply into deliberate silence."""
    actions = _parse(json.dumps([{"action_type": "delegate", "payload": "x"}]))
    assert [a.action_type for a in actions] == [ActionType.COMPLETE_TASK]


# ─── The validator on its own ────────────────────────────────────────


@pytest.mark.parametrize("payload", BAD_PAYLOADS, ids=repr)
@pytest.mark.parametrize("action_type", list(ActionType), ids=lambda t: t.value)
def test_the_validator_replaces_a_non_object_payload(
    action_type: ActionType, payload: Any,
) -> None:
    """The parser no longer hands it one, but the validator is the last
    check before execution and must not raise for any caller."""
    validated = validate_action_payload(AgentAction(action_type, payload))
    assert validated.action_type is ActionType.DO_NOTHING
    assert validated.payload == {}


@pytest.mark.parametrize("literal", ["Infinity", "-Infinity", "1e999", "NaN"])
@pytest.mark.parametrize("field", ["max_tokens", "timeout_seconds", "max_llm_calls"])
def test_a_sub_agent_limit_that_is_not_a_finite_number_is_removed(
    field: str, literal: str,
) -> None:
    """Python's JSON decoder accepts ``Infinity`` and reads ``1e999`` as
    it; ``int()`` of either raised ``OverflowError`` past the clamp."""
    text = (
        '[{"action_type": "spawn_sub_agent", "payload": '
        f'{{"role": "researcher", "task": "look it up", "{field}": {literal}}}}}]'
    )
    (action,) = _parse(text)
    assert action.action_type is ActionType.SPAWN_SUB_AGENT
    assert field not in action.payload
    assert action.payload["role"] == "researcher"


# ─── Through reply synthesis, where the bad payload used to fail ─────


@pytest.mark.parametrize("payload", BAD_PAYLOADS, ids=repr)
def test_a_wrong_shaped_reply_on_a_dm_is_published_as_its_text(payload: Any) -> None:
    text = json.dumps([{"action_type": "complete_task", "payload": payload}])
    event = AgentEvent(
        event_type=EventType.CHANNEL_MESSAGE,
        payload={"content": "hello"},
        sender_id="user-1",
        channel_id="dm:user-1:ember-owl",
    )
    actions = synthesize_channel_reply(event, _parse(text), agent_id="ember-owl")
    assert actions[0].action_type is ActionType.SEND_CHANNEL_MESSAGE
    assert actions[0].payload["content"] == text


# ─── A whole persona turn ────────────────────────────────────────────


@pytest.mark.parametrize(
    "text",
    [
        _fenced(GOOD),
        json.dumps(["a", "b"]),
        json.dumps([{"action_type": "send_channel_message", "payload": None}]),
        _fenced(None),
    ],
    ids=["fenced object", "list of strings", "null payload", "fenced null"],
)
async def test_a_turn_with_a_wrong_shaped_reply_is_answered_not_dropped(
    text: str,
) -> None:
    """The model call is paid for by the time the reply is parsed.  The
    turn used to end in an ``AttributeError`` or ``TypeError`` there."""
    agent = create_persona_agent(
        agent_id="ember-owl",
        config=_PERSONA_CONFIG,
        llm_client=_make_client([LLMResponse(text=text)]),
    )
    await agent.initialize_memory()
    try:
        output = await agent.handle(_task())
    finally:
        await agent.close_memory()
    assert output.status is TaskStatus.COMPLETED
    assert output.result == text


async def test_a_dm_turn_with_a_wrong_shaped_reply_publishes_its_text() -> None:
    """On a channel the error used to reach the dispatch boundary, which
    logs it and publishes nothing, so the person waiting got no reply."""
    text = json.dumps([{"action_type": "complete_task", "payload": "done"}])
    agent = create_persona_agent(
        agent_id="ember-owl",
        config=_PERSONA_CONFIG,
        llm_client=_make_client([LLMResponse(text=text)]),
    )
    await agent.initialize_memory()
    try:
        actions = await agent.on_event(AgentEvent(
            event_type=EventType.CHANNEL_MESSAGE,
            payload={"content": "hello"},
            sender_id="user-1",
            channel_id="dm:user-1:ember-owl",
        ))
    finally:
        await agent.close_memory()
    sends = [a for a in actions if a.action_type is ActionType.SEND_CHANNEL_MESSAGE]
    assert [a.payload["content"] for a in sends] == [text]
    assert sends[0].payload["channel_id"] == "dm:user-1:ember-owl"


@pytest.mark.parametrize(
    ("channel_id", "posted"),
    [("group:eng", []), ("dm:user-1:ember-owl", ["\u2026"])],
    ids=["group", "dm"],
)
@pytest.mark.parametrize("payload", [None, "x"], ids=repr)
async def test_a_do_nothing_with_a_bad_payload_is_still_silence_on_a_channel(
    payload: Any, channel_id: str, posted: list[str],
) -> None:
    """A group channel gets nothing and a DM gets the placeholder, the
    same as for a well-formed ``do_nothing``; neither gets the raw JSON."""
    text = json.dumps([{"action_type": "do_nothing", "payload": payload}])
    agent = create_persona_agent(
        agent_id="ember-owl",
        config=_PERSONA_CONFIG,
        llm_client=_make_client([LLMResponse(text=text)]),
    )
    await agent.initialize_memory()
    try:
        actions = await agent.on_event(AgentEvent(
            event_type=EventType.CHANNEL_MESSAGE,
            payload={"content": "hello", "respond_policy": "always"},
            sender_id="user-1",
            channel_id=channel_id,
        ))
    finally:
        await agent.close_memory()
    sends = [a for a in actions if a.action_type is ActionType.SEND_CHANNEL_MESSAGE]
    assert [a.payload["content"] for a in sends] == posted


# ─── Any JSON value at all ───────────────────────────────────────────

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
