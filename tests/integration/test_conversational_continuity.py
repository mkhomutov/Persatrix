"""RFC 0034 Phase 1 PR 3 — DM conversational-continuity integration test.

Minimal repro of [ISSUE-0052](docs/issues/ISSUE-0052-persona-conversational-working-memory-gap.md):
before RFC 0034 the persona's LLM ``messages`` array was rebuilt each
turn holding **only** the current event, so mid-conversation the model
never saw its own prior question and treated every turn as the first.

This test asserts the **substrate guarantee** — the *shape* of the
``messages`` payload the persona sends to the LLM on turn 2 — not the
model's prose. Prose-level acceptance (the persona actually answering
"what did you just ask?") is `MT-PERSONA-CONVERSATION-001`, executed in
v0.3.1 release prep.

The channel-history fetcher is a hand-curated fake: it does not observe
what the agent emitted, it replays a fixed transcript. That is the
intended seam — PR 3 wires :func:`build_conversation_messages` into the
action loop; the fetcher's own HTTP contract is covered by
``tests/unit/python/test_channel_history_fetcher.py``.

(The plan placed this file under ``tests/integration/persona/``; the
integration suite is flat under ``tests/integration/`` — same path
discrepancy the RFC 0034 PR 1 scope note recorded for the fetcher.)
"""

from __future__ import annotations

from typing import Any

import pytest

from agents.llm_client import LLMClient, LLMResponse, StopReason, Usage
from agents.persona import create_persona_agent
from agents.persona_runtime import _LLMPersonaAgent, conversation_window
from agents.persona_types import AgentEvent, EventType
from agents.tools.registry import clear_registry

_AGENT_ID = "ember-owl"
_CHANNEL = "dm:user:ember-owl"
_TURN1_QUESTION = "What is your favourite season?"


@pytest.fixture(autouse=True)
def _clean_registry():
    clear_registry()
    yield
    clear_registry()


@pytest.fixture(autouse=True)
def _clear_window_cache():
    """The conversation-window fetch cache is module-level (RFC 0034 §F);
    clear it around every test so one test's window cannot bleed into
    the next."""
    conversation_window._WINDOW_CACHE.clear()
    yield
    conversation_window._WINDOW_CACHE.clear()


# ─── Fakes ─────────────────────────────────────────────────────


class _FakeChannelHistoryFetcher:
    """Duck-typed :class:`ChannelHistoryFetcher` — the seam PR 3 wires.

    Returns a per-call curated history (``results``, popped left to
    right) or raises ``raises`` on every call. Records each ``fetch``
    call so the test can pin that the window actually consulted it.
    """

    def __init__(
        self,
        *,
        results: list[list[dict[str, Any]]] | None = None,
        raises: Exception | None = None,
    ) -> None:
        self.calls: list[tuple[str, int]] = []
        self._results = results
        self._raises = raises

    async def fetch(
        self, channel_id: str, *, limit: int,
    ) -> list[dict[str, Any]] | None:
        self.calls.append((channel_id, limit))
        if self._raises is not None:
            raise self._raises
        assert self._results, "fetcher called more times than scripted"
        return self._results.pop(0)


class _RecordingProvider:
    """LLM provider that records every ``messages`` payload it is sent
    and returns scripted replies in order.

    Records a shallow copy of each turn dict — the action loop mutates
    the ``messages`` list in place across tool rounds, so capturing the
    live reference would let a later turn rewrite an earlier recording.
    """

    def __init__(self, replies: list[str]) -> None:
        self._replies = list(replies)
        self.recorded: list[list[dict[str, Any]]] = []
        # ``LLMProvider`` requires a ``name`` (OTEL ``gen_ai.system``).
        self.name = "recording"

    async def create_message(self, **kwargs: Any) -> LLMResponse:
        messages = kwargs["messages"]
        self.recorded.append([dict(turn) for turn in messages])
        text = self._replies.pop(0) if self._replies else "[]"
        return LLMResponse(
            text=text, stop_reason=StopReason.END_TURN, usage=Usage(10, 5),
        )

    def format_tool_definitions(self, tools: Any) -> list[Any]:
        return []

    def append_tool_round(
        self, messages: Any, response: Any, tool_results: Any,
    ) -> Any:
        return messages


def _persona_config() -> dict[str, Any]:
    return {
        "id": _AGENT_ID,
        "model": "test-model",
        "role": "Conversational-continuity test persona",
        "type": "persona",
        "max_llm_calls": 5,
        "max_tokens": 1024,
        "tools": [],
        "persona": {
            "name": "Ember Owl",
            "background": "RFC 0034 Phase 1 PR 3 conversational-continuity test.",
            "behavior": {
                "directness": "balanced",
                "formality": "professional",
                "risk_tolerance": "moderate",
            },
        },
        "memory": {
            "db_path": ":memory:",
            "working": {"max_tokens": 50000},
        },
        # Exercises the resolve_conversation_window_config path against a
        # real per-agent block — resolved lazily on the first persona turn
        # by _ConversationWindowMixin._build_seed_messages (RFC 0034 PR 3).
        "conversation_window": {
            "enabled": True,
            "max_turns": 20,
            "max_tokens": 2048,
        },
        "relationships": [],
    }


async def _make_agent(
    provider: _RecordingProvider, *, config: dict[str, Any] | None = None,
) -> _LLMPersonaAgent:
    agent = create_persona_agent(
        agent_id=_AGENT_ID,
        config=config if config is not None else _persona_config(),
        llm_client=LLMClient(provider),
    )
    await agent.initialize_memory()
    return agent


def _dm_event(content: str, message_id: str) -> AgentEvent:
    return AgentEvent(
        event_type=EventType.CHANNEL_MESSAGE,
        payload={
            "content": content,
            "channel_type": "dm",
            "respond_policy": "always",
            "mentions": [],
            "thread_parent_sender_id": "",
        },
        channel_id=_CHANNEL,
        sender_id="user",
        message_id=message_id,
    )


def _row(message_id: str, sender_id: str, content: str) -> dict[str, Any]:
    """One channel-history row in the shape the history endpoint returns."""
    return {"id": message_id, "sender_id": sender_id, "content": content}


# ─── Tests ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_persona_sees_its_own_prior_turn_on_the_next_dm_turn() -> None:
    """ISSUE-0052 repro: the turn-2 LLM call carries the persona's turn-1
    assistant message in the ``messages`` array.

    Turn 1 — empty channel history → the seed is the current event
    alone (pre-RFC-0034 behaviour, the regression baseline).
    Turn 2 — the curated history holds the turn-1 peer message and the
    persona's turn-1 reply → the window reconstructs
    ``[user, assistant, user]`` so the model sees the in-progress
    transcript.
    """
    provider = _RecordingProvider(
        replies=[
            # Turn 1: persona asks a question.
            '```json\n[{"action_type": "send_channel_message", '
            f'"payload": {{"channel_id": "{_CHANNEL}", '
            f'"content": "{_TURN1_QUESTION}"}}}}]\n```',
            # Turn 2: reply shape is irrelevant — we assert on the
            # recorded turn-2 ``messages`` payload, not the model output.
            '```json\n[{"action_type": "do_nothing", "payload": {}}]\n```',
        ],
    )
    agent = await _make_agent(provider)
    fetcher = _FakeChannelHistoryFetcher(
        results=[
            # Turn 1 fetch — channel has no prior history.
            [],
            # Turn 2 fetch — newest-first, as the history endpoint
            # returns it: the persona's turn-1 reply, then the turn-1
            # peer message.
            [
                _row("m2", _AGENT_ID, _TURN1_QUESTION),
                _row("m1", "user", "Hi there"),
            ],
        ],
    )
    agent.set_history_fetcher(fetcher)

    await agent.on_event(_dm_event("Hi there", message_id="m1"))
    await agent.on_event(_dm_event("what did you just ask?", message_id="m3"))

    assert len(provider.recorded) == 2, "expected one LLM call per turn"

    # Turn 1: empty history → current event alone (regression baseline).
    turn1 = provider.recorded[0]
    assert len(turn1) == 1
    assert turn1[0]["role"] == "user"
    assert "Hi there" in turn1[0]["content"]

    # Turn 2: the window reconstructs the transcript.
    turn2 = provider.recorded[1]
    assert len(turn2) == 3, f"expected [user, assistant, user]; got {turn2!r}"
    # The persona's own turn-1 question is replayed as an assistant turn —
    # raw, never delimiter-wrapped (RFC 0034 §C).
    assert turn2[1] == {"role": "assistant", "content": _TURN1_QUESTION}
    # The turn-1 peer message precedes it as a user turn.
    assert turn2[0]["role"] == "user"
    assert "Hi there" in turn2[0]["content"]
    # The current event is always the final turn.
    assert turn2[-1]["role"] == "user"
    assert "what did you just ask?" in turn2[-1]["content"]

    # The window actually consulted the fetcher both turns.
    assert [c[0] for c in fetcher.calls] == [_CHANNEL, _CHANNEL]


@pytest.mark.asyncio
async def test_fetch_failure_degrades_to_current_event_only() -> None:
    """When the history fetch raises, the seed degrades to the current
    event alone and the persona still produces a turn — no exception
    bubbles to the dispatcher (RFC 0034 §F fall-back)."""
    provider = _RecordingProvider(
        replies=['```json\n[{"action_type": "do_nothing", "payload": {}}]\n```'],
    )
    agent = await _make_agent(provider)
    agent.set_history_fetcher(
        _FakeChannelHistoryFetcher(raises=RuntimeError("orchestrator down")),
    )

    actions = await agent.on_event(_dm_event("hello", message_id="m1"))

    assert actions, "persona must still produce a turn on fetch failure"
    assert len(provider.recorded) == 1
    seed = provider.recorded[0]
    assert len(seed) == 1
    assert seed[0]["role"] == "user"
    assert "hello" in seed[0]["content"]


@pytest.mark.asyncio
async def test_unwired_fetcher_degrades_to_current_event_only() -> None:
    """With no history fetcher wired the seed degrades to the current
    event alone — identical to pre-RFC-0034 behaviour.

    ``set_history_fetcher`` is injected post-construction by
    ``AgentServer.start``; until it runs ``_history_fetcher`` is ``None``
    and ``_build_seed_messages`` short-circuits before
    ``build_conversation_messages`` (``conversation_seed.py``). The
    task-only / partial-init code paths in production, and every
    persona-runtime test that does not wire a fetcher, depend on this
    branch — the wider suite covers it only *implicitly*. This test pins
    the seed *shape* explicitly so a future change to
    ``_build_seed_messages`` cannot silently regress the unwired path.

    Distinct from ``test_fetch_failure_degrades_to_current_event_only``:
    that exercises a wired fetcher that *raises*; this exercises the
    fetcher being *absent*. The absence wins over the
    ``conversation_window`` block in ``_persona_config`` (``enabled:
    True``) — the seed is a single ``user`` turn regardless of config.
    """
    provider = _RecordingProvider(
        replies=['```json\n[{"action_type": "do_nothing", "payload": {}}]\n```'],
    )
    agent = await _make_agent(provider)
    # Deliberately no agent.set_history_fetcher(...) — this is the
    # unwired path; the omission is the subject under test.

    actions = await agent.on_event(_dm_event("hello", message_id="m1"))

    assert actions, "persona must still produce a turn with no fetcher wired"
    assert len(provider.recorded) == 1
    seed = provider.recorded[0]
    assert len(seed) == 1
    assert seed[0]["role"] == "user"
    assert "hello" in seed[0]["content"]


@pytest.mark.asyncio
async def test_unwired_fetcher_skips_conversation_window_config_resolution() -> None:
    """The unwired-fetcher short-circuit is *total* — it resolves no
    conversation-window config.

    ``_build_seed_messages`` returns the current-event-only seed the
    moment ``_history_fetcher`` is ``None``. On that path it must do no
    other conversation-window work: a reader expecting the ``None``-fetcher
    branch to be a complete short-circuit would be surprised to find the
    lazy ``resolve_conversation_window_config`` cache populated by a turn
    that never built a window.

    ``_conversation_window_config`` is the observable witness — the cache
    slot the resolver populates. It starts at its ``None`` class default;
    on the unwired path it must *stay* ``None``. (The test is white-box by
    necessity: the resolved config is discarded on this path, so the only
    difference a fix makes is the cache slot itself.)

    Regression guard for the RFC 0034 PR 3 review finding that the resolve
    ran *before* the ``_history_fetcher is None`` check — harmless work,
    but work the short-circuit implies it skips. Pairs with
    ``test_unwired_fetcher_degrades_to_current_event_only`` (seed shape);
    this test pins the *absence of side effects* on the same path.
    """
    provider = _RecordingProvider(
        replies=['```json\n[{"action_type": "do_nothing", "payload": {}}]\n```'],
    )
    agent = await _make_agent(provider)
    # Deliberately no agent.set_history_fetcher(...) — the unwired path.

    await agent.on_event(_dm_event("hello", message_id="m1"))

    assert agent._conversation_window_config is None, (
        "unwired short-circuit must resolve no conversation-window config"
    )


@pytest.mark.asyncio
async def test_disabled_config_degrades_to_current_event_only() -> None:
    """A persona whose per-agent ``conversation_window.enabled`` is
    ``false`` seeds the current event alone — even with a history fetcher
    wired and a non-empty channel transcript available to it.

    ``enabled: false`` is the RFC 0034 §F operator escape hatch. Its
    *substrate* semantics — ``build_conversation_messages`` returning
    ``[current_turn]`` on a disabled config — are already pinned at the
    unit layer by ``test_conversation_window.py::TestDisabled`` (RFC 0034
    PR 2). This integration test exists for a distinct, PR-3-only reason:
    it is the only test that pins the *config pass-through wiring* — that
    ``_ConversationWindowMixin._build_seed_messages`` resolves the
    persona's own ``conversation_window`` block and forwards the
    resulting :class:`ConversationWindowConfig` to
    ``build_conversation_messages``.

    ``test_persona_sees_its_own_prior_turn_on_the_next_dm_turn`` cannot
    pin that wiring: its ``_persona_config`` block (``enabled: true``,
    ``max_turns: 20``, ``max_tokens: 2048``) is value-identical to the
    ``ConversationWindowConfig`` dataclass defaults, so a regression that
    dropped the resolved config and fell back to defaults inside
    ``_build_seed_messages`` would leave that test green. ``enabled:
    false`` is the maximally distinct case: if the resolved per-agent
    block did not reach ``build_conversation_messages`` the window would
    reconstruct the transcript from the wired history and the seed would
    carry more than one turn — so this test fails loudly on exactly that
    regression.

    The fetcher is wired and scripted with a real prior turn so the
    suppression must come from the disabled config, not from an absent
    fetcher (``test_unwired_fetcher_degrades_to_current_event_only``) or
    an empty history. A disabled config short-circuits in
    ``build_conversation_messages`` *before* the fetch, so the fetcher is
    never consulted — asserted here as the sharper witness that the
    window genuinely did not run.
    """
    provider = _RecordingProvider(
        replies=['```json\n[{"action_type": "do_nothing", "payload": {}}]\n```'],
    )
    config = _persona_config()
    config["conversation_window"] = {
        "enabled": False, "max_turns": 20, "max_tokens": 2048,
    }
    agent = await _make_agent(provider, config=config)
    # A fetcher with a real prior turn is wired — so the disabled config,
    # not a missing fetcher or an empty transcript, is what must suppress
    # the window.
    fetcher = _FakeChannelHistoryFetcher(
        results=[[_row("m1", "user", "an earlier peer line")]],
    )
    agent.set_history_fetcher(fetcher)

    actions = await agent.on_event(_dm_event("hello", message_id="m2"))

    assert actions, "persona must still produce a turn with the window disabled"
    assert len(provider.recorded) == 1
    seed = provider.recorded[0]
    assert len(seed) == 1, f"disabled window must seed one turn; got {seed!r}"
    assert seed[0]["role"] == "user"
    assert "hello" in seed[0]["content"]
    # A disabled config short-circuits before the fetch — the wired
    # fetcher is never consulted.
    assert fetcher.calls == [], "disabled window must not consult the fetcher"
