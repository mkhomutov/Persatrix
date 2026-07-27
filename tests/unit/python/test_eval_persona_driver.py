"""Unit tests for the RFC 0044 Phase 1 persona-runtime driver (PR 3).

``PersonaRuntimeDriver`` is the real-runtime adapter: it builds a persona agent
around an injected :class:`~agents.llm_types.LLMProvider` + a
:class:`~agents.clock.FrozenClock`, drives each recipe interaction/turn through
``agent.on_event``, injects the ``elapsed`` delta into the RFC 0021 temporal seam
(OQ #5), and snapshots the terminal state into the ``persona:<id>:...`` key space
``evaluate`` compares against. It is the seam that produces an ``EvalRun`` from a
recipe.

These tests drive the *real* persona runtime against an in-memory persona config
(no disk, no network, no API key). Determinism comes from the ``FrozenClock`` and
the ``:memory:`` SQLite DB — the same properties that let a golden recorded once
replay byte-stably (RFC 0044 §D). The record→replay symmetry test proves the full
CI-safe path end-to-end through the runtime.
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Any

import pytest

from agents.clock import FrozenClock
from agents.llm_types import LLMResponse, StopReason, Usage
from evaluators.eval_set import load_eval_set
from evaluators.persona_driver import (
    DEFAULT_EPOCH,
    PersonaRuntimeDriver,
    default_config_resolver,
)
from evaluators.replay_llm_client import RecordingProvider, ReplayProvider

# A synthetic ember-owl config — the in-memory analogue of a config/agents.yaml
# entry (mirrors tests/integration/_temporal_test_helpers.PERSONA_CONFIG).
_CONFIG: dict[str, Any] = {
    "id": "ember-owl",
    "type": "persona",
    "name": "Ember Owl",
    "role": "eval-driver test fixture",
    "model": "claude-sonnet-4-6",
    "temperature": 0.0,
    "max_llm_calls": 3,
    "max_tokens": 256,
    "persona": {"background": "Test fixture.", "behavior": {}},
    "permissions": {"memory": {"read": True, "write": True}},
    "memory": {"db_path": ":memory:"},
}


def _resolver() -> Any:
    return lambda name: dict(_CONFIG, id=name)


class _ScriptFake:
    """Call-index-scripted live provider: the nth ``create_message`` returns
    ``replies[n]`` as plain text (the runtime synthesizes it into a channel
    reply). Keyed by call index, not content, so it is robust to memory context
    leaking tokens into the request messages. Records each call's ``messages``
    array so a test can inspect what the runtime assembled per turn."""

    name = "fake-live"

    def __init__(self, replies: list[str]) -> None:
        self.replies = replies
        self.calls = 0
        self.seen_messages: list[list[Any]] = []

    async def create_message(self, *, model, messages, system, tools, max_tokens, temperature):
        self.seen_messages.append(messages)
        reply = self.replies[min(self.calls, len(self.replies) - 1)]
        self.calls += 1
        return LLMResponse(text=reply, stop_reason=StopReason.END_TURN, usage=Usage(5, 5))

    def format_tool_definitions(self, tools: list[dict]) -> list[dict]:
        return list(tools)

    def append_tool_round(self, messages, response, tool_results):
        return list(messages)


_TWO_TURN = textwrap.dedent(
    """
    id: EVAL-MEMORY-001
    title: two-interaction drive
    setup:
      persona: ember-owl
      user: alice
      seed_state:
        persona:ember-owl:trust.scores.alice: 0.3
    interactions:
      - id: i1
        turns:
          - user: "Hi Ember — I'm Alice from the data-platform team."
          - assistant: {match: contains, value: "Alice"}
      - id: i2
        elapsed: 5m
        turns:
          - user: "What do you remember about me?"
          - assistant: {match: contains, value: "data-platform"}
    """
).strip()

_REPLIES = ["Nice to meet you, Alice!", "You're Alice from the data-platform team."]


def _recipe(tmp_path: Path, body: str = _TWO_TURN) -> Any:
    p = tmp_path / "recipe.yaml"
    p.write_text(body, encoding="utf-8")
    return load_eval_set(p)


# ─── driving the real runtime ────────────────────────────────────────────────


async def test_driver_produces_ordered_turn_outputs(tmp_path: Path) -> None:
    es = _recipe(tmp_path)
    driver = PersonaRuntimeDriver(config_resolver=_resolver())
    run = await driver.run(es, _ScriptFake(list(_REPLIES)))
    assert run.turn_outputs == _REPLIES  # one output per assistant turn, in order


_TWO_USER_ONE_ASSISTANT = textwrap.dedent(
    """
    id: EVAL-MEMORY-001
    title: two user turns, one assistant expectation
    setup:
      persona: ember-owl
      user: alice
    interactions:
      - id: i1
        turns:
          - user: "first message"
          - user: "second message"
          - assistant: {match: contains, value: "SECOND"}
    """
).strip()


async def test_driver_aligns_output_to_assistant_turn_not_user_turn(tmp_path: Path) -> None:
    # Regression: outputs must align to ASSISTANT turns — what `evaluate` checks
    # positionally — not to USER turns. With two user turns before one assistant
    # expectation, the single asserted output is the reply to the *last* user turn.
    # (Appending per user turn would put the reply to the FIRST message in slot 0,
    # so `evaluate` would check the wrong reply — a silent wrong verdict.)
    es = _recipe(tmp_path, _TWO_USER_ONE_ASSISTANT)
    driver = PersonaRuntimeDriver(config_resolver=_resolver())
    run = await driver.run(es, _ScriptFake(["reply to FIRST", "reply to SECOND"]))
    assert run.turn_outputs == ["reply to SECOND"]


async def test_driver_snapshots_seeded_trust(tmp_path: Path) -> None:
    es = _recipe(tmp_path)
    driver = PersonaRuntimeDriver(config_resolver=_resolver())
    run = await driver.run(es, _ScriptFake(list(_REPLIES)))
    # The seed_state key round-trips into the terminal_state snapshot in the
    # persona:<id>:trust.scores.<peer> key space evaluate() compares against.
    assert "persona:ember-owl:trust.scores.alice" in run.terminal_state
    assert run.terminal_state["persona:ember-owl:trust.scores.alice"] == pytest.approx(0.3)


async def test_driver_injects_elapsed_into_clock(tmp_path: Path) -> None:
    # OQ #5: the runner injects the `elapsed` delta into the temporal seam. With
    # an injected clock we can observe the advance directly: i2 carries elapsed=5m.
    es = _recipe(tmp_path)
    clock = FrozenClock(DEFAULT_EPOCH, tz="UTC")
    driver = PersonaRuntimeDriver(config_resolver=_resolver(), clock=clock)
    await driver.run(es, _ScriptFake(list(_REPLIES)))
    assert clock.now() == DEFAULT_EPOCH + 300.0  # 5m of simulated time elapsed


async def test_driver_events_empty_pre_rfc0041(tmp_path: Path) -> None:
    # Phase 1 has no typed-event stream (RFC 0041 unlanded); the driver reports an
    # empty event list so event_count/event_sequence assertions degrade gracefully.
    es = _recipe(tmp_path)
    driver = PersonaRuntimeDriver(config_resolver=_resolver())
    run = await driver.run(es, _ScriptFake(list(_REPLIES)))
    assert run.events == []


# ─── RFC 0034 working memory (channel-gated) ─────────────────────────────────

_WORKING_MEMORY = textwrap.dedent(
    """
    id: EVAL-WORKING-001
    title: working-memory drive
    setup:
      persona: ember-owl
      user: sam
      channel: dm:sam:ember-owl
    interactions:
      - id: i1
        turns:
          - user: "Can you kick off the deploy for me?"
          - assistant: {match: contains, value: "staging"}
          - user: "As soon as possible please — the team is waiting."
          - assistant: {match: must_reference, values: ["staging"]}
    """
).strip()

# The persona's clarifying question, then a reply that leans on it — the second
# only makes sense if the model saw the first, which is exactly what the window
# must feed back.
_WM_REPLIES = [
    "Happy to — should I deploy to staging or production?",
    "I still need to know: staging or production?",
]


async def test_driver_engages_working_memory_when_channel_set(tmp_path: Path) -> None:
    """With ``setup.channel`` declared, the driver wires the in-process history
    fetcher so the RFC 0034 window reconstructs the in-channel transcript: turn 2's
    ``messages`` array carries turn 1's user message *and the persona's own prior
    reply*. This is the seam that makes a working-memory seed a real regression bar
    — drop it and turn 2's request (hence its golden hash) changes."""
    es = _recipe(tmp_path, _WORKING_MEMORY)
    fake = _ScriptFake(list(_WM_REPLIES))
    driver = PersonaRuntimeDriver(config_resolver=_resolver())

    await driver.run(es, fake)

    assert len(fake.seen_messages) == 2  # one create_message per user turn
    assert len(fake.seen_messages[0]) == 1  # turn 1: no history yet → current only
    # Turn 2: the reconstructed window — prior user turn, the persona's own reply,
    # then the current turn.
    turn2 = fake.seen_messages[1]
    assert len(turn2) == 3
    assert turn2[-1]["role"] == "user"  # current event is always last (and a user turn)
    assistant_turns = [m for m in turn2 if m["role"] == "assistant"]
    assert assistant_turns, "the persona's own prior reply must be replayed as an assistant turn"
    assert assistant_turns[0]["content"] == _WM_REPLIES[0]  # its verbatim first reply
    # The prior user turn is replayed too (peer-labelled per RFC 0034 §C/§G).
    assert any("kick off the deploy" in str(m.get("content")) for m in turn2 if m["role"] == "user")


async def test_driver_no_working_memory_without_channel(tmp_path: Path) -> None:
    """The gate: a recipe with no ``setup.channel`` stays on the pre-window
    current-event-only path — every turn's ``messages`` array holds only the
    current turn, byte-identical to before this seam. This is what keeps a landed
    channel-less golden (EVAL-MEMORY-001) unchanged when working memory is added."""
    body = "\n".join(
        line for line in _WORKING_MEMORY.splitlines() if "channel:" not in line
    )
    es = _recipe(tmp_path, body)
    fake = _ScriptFake(list(_WM_REPLIES))
    driver = PersonaRuntimeDriver(config_resolver=_resolver())

    await driver.run(es, fake)

    assert len(fake.seen_messages) == 2
    assert all(len(msgs) == 1 for msgs in fake.seen_messages), (
        "no channel → no conversation window → current-event-only on every turn"
    )


# ─── record → replay symmetry through the runtime ────────────────────────────


async def test_record_then_replay_through_runtime_hits(tmp_path: Path) -> None:
    """The CI-safe path end-to-end: recording a golden while driving the real
    runtime, then replaying it, reproduces the run and never misses the cassette.

    A miss would raise ReplayCassetteMissError, so a clean run *is* the assertion
    that record and replay canonicalize the same requests through the runtime."""
    es = _recipe(tmp_path)
    driver = PersonaRuntimeDriver(config_resolver=_resolver())

    recorder = RecordingProvider(_ScriptFake(list(_REPLIES)))
    recorded = await driver.run(es, recorder)
    assert recorded.turn_outputs == _REPLIES
    assert len(recorder.cassette) >= 1  # at least one request was captured

    replay = ReplayProvider(recorder.cassette)
    replayed = await driver.run(es, replay)  # no ReplayCassetteMissError == HIT
    assert replayed.turn_outputs == recorded.turn_outputs
    assert replayed.terminal_state == recorded.terminal_state


# ─── isolated in-memory DB (golden portability + no production pollution) ─────


async def test_driver_forces_isolated_in_memory_db(tmp_path: Path) -> None:
    """The driver MUST run against an isolated ``:memory:`` DB — never the
    persona's configured file ``db_path``.

    Two properties depend on this (the module docstring's determinism contract):
    a golden recorded once must replay byte-stably on a *fresh* clone (a persona's
    file DB carries ambient rows that would shift the recalled prompt → a cassette
    miss), and an eval must never pollute production memory. So a resolver that
    hands the driver a real file ``db_path`` must not cause that file to appear."""
    sentinel = tmp_path / "production_memory.db"
    assert not sentinel.exists()

    def resolver(name: str) -> dict[str, Any]:
        cfg = dict(_CONFIG, id=name)
        cfg["memory"] = {"db_path": str(sentinel)}  # a file DB the eval must ignore
        return cfg

    driver = PersonaRuntimeDriver(config_resolver=resolver)
    run = await driver.run(_recipe(tmp_path), _ScriptFake(list(_REPLIES)))

    assert run.turn_outputs == _REPLIES  # the run still works, on :memory:
    assert not sentinel.exists(), (
        "the eval driver must force :memory: and never touch the persona's file db_path"
    )


# ─── config resolver ─────────────────────────────────────────────────────────


def test_default_config_resolver_resolves_by_name(tmp_path: Path) -> None:
    cfg = tmp_path / "agents.yaml"
    cfg.write_text(
        textwrap.dedent(
            """
            agents:
              - id: ember-owl
                type: persona
                name: Ember Owl
              - id: iron-fox
                type: persona
                name: Iron Fox
            """
        ).strip(),
        encoding="utf-8",
    )
    resolve = default_config_resolver(cfg)
    assert resolve("ember-owl")["name"] == "Ember Owl"


def test_default_config_resolver_unknown_persona_raises(tmp_path: Path) -> None:
    cfg = tmp_path / "agents.yaml"
    cfg.write_text("agents:\n  - id: ember-owl\n    type: persona\n", encoding="utf-8")
    resolve = default_config_resolver(cfg)
    with pytest.raises(KeyError):
        resolve("ghost-persona")


# ─── RFC 0049 PR 2: shadow-trace capture ─────────────────────────────────────


def test_capture_shadow_traces_collects_payloads() -> None:
    """The capture handler collects the structured payload off each shadow
    record — and only records carrying the payload attribute."""
    import logging

    from agents.persona_runtime.facts_shadow import (
        SHADOW_LOGGER_NAME,
        SHADOW_TRACE_ATTR,
    )
    from evaluators.persona_driver import capture_shadow_traces

    shadow_logger = logging.getLogger(SHADOW_LOGGER_NAME)
    with capture_shadow_traces() as traces:
        shadow_logger.info(
            "shadow", extra={SHADOW_TRACE_ATTR: {"candidates": [1]}},
        )
        shadow_logger.info("no payload — ignored")
    assert traces == [{"candidates": [1]}]


def test_capture_shadow_traces_restores_logger() -> None:
    """The capture must not perturb logging outside the block: handler
    detached, level restored — even when it had to lower the level to
    admit INFO records."""
    import logging

    from agents.persona_runtime.facts_shadow import SHADOW_LOGGER_NAME
    from evaluators.persona_driver import capture_shadow_traces

    shadow_logger = logging.getLogger(SHADOW_LOGGER_NAME)
    prev_level, prev_handlers = shadow_logger.level, list(shadow_logger.handlers)
    shadow_logger.setLevel(logging.ERROR)  # would filter INFO without the lower
    try:
        with capture_shadow_traces() as traces:
            logging.getLogger(SHADOW_LOGGER_NAME).info(
                "shadow", extra={"facts_shadow": {"candidates": []}},
            )
        assert traces == [{"candidates": []}]
        assert shadow_logger.level == logging.ERROR
        assert shadow_logger.handlers == prev_handlers
    finally:
        shadow_logger.setLevel(prev_level)


async def test_driver_run_carries_shadow_traces_field(tmp_path: Path) -> None:
    """A single-room recipe captures no traces — the field exists and is
    empty, so landed artifacts/goldens are unperturbed."""
    es = _recipe(tmp_path)
    driver = PersonaRuntimeDriver(config_resolver=_resolver())
    run = await driver.run(es, _ScriptFake(list(_REPLIES)))
    assert run.shadow_traces == []
