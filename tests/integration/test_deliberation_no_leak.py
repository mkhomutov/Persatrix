"""RFC 0051 Phase 2 (v0.3.10) — the privacy wall: the plan never leaks.

PR 3 of the RFC 0051 PR plan (``docs/rfcs/0051-pr-plan.md``). The
``CompositionPlan`` is the most context-revealing artifact a persona produces,
so [RFC 0051 §E](../../docs/rfcs/0051-reasoning-before-posting.md) walls it: it
is threaded into the Tier-C compose as a *private* system-prompt section, but is
**never an AgentAction, never published, never persisted** — so RFC 0034
transcript reconstruction can never surface it into a peer's ``messages`` array.

This is the load-bearing test of that wall. It drives the real
``_LLMPersonaAgent`` action loop on a ``should_post=true`` turn carrying a plan
(the seam is patched to inject one — the seam→plan parse is pinned in
``tests/unit/python/test_salience_gate_plan.py``) and asserts:

* **Positive control** — the plan's private ``intent`` *does* reach the compose
  call's system prompt (so the test would fail loudly if the plan silently never
  threaded, rather than passing vacuously).
* **No leak to the channel** — the plan text appears in **zero** published
  ``SEND_CHANNEL_MESSAGE`` payloads.
* **No leak to the store** — the plan text appears in **zero** of the actions
  handed to ``_store_event_episode`` (the channel/episodic store an RFC 0034
  reconstruction reads), so no peer can ever reconstruct it.

PR 9 (Phase 5b) extends the wall to the **reflexion** intermediates (RFC 0051
§E): under ``mode: plan`` with ``reasoning.revise ≥ 1`` a cheap critic flags a
weak draft and a quality revise rewrites it — only the *final* revised message is
published. ``TestReflexionDraftAndCritiqueNeverLeak`` drives a real rewrite turn
and asserts the **discarded first-pass draft** and the **critic note** appear in
zero published messages and zero stored episodes (only the revised text ships).
The wall is structural: reflexion replaces the message content *before* the store
(``action_loop`` runs ``maybe_revise_channel_message`` then ``_store_event_episode``),
and the discarded draft / critic note are never wrapped in an ``AgentAction``.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agents.llm_client import LLMClient, LLMResponse, StopReason, Usage
from agents.model_aliases import use_alias_map
from agents.persona import create_persona_agent
from agents.persona_runtime import _LLMPersonaAgent
from agents.persona_runtime.deliberation_plan import CompositionPlan
from agents.persona_runtime.salience_gate import SalienceOutcome
from agents.persona_types import ActionType, AgentEvent, EventType
from agents.tools.registry import clear_registry

pytestmark = pytest.mark.asyncio

# Patch the seam where the action loop looks it up.
_SEAM_PATH = "agents.persona_runtime.action_loop.run_salience_gate"

# Distinctive markers — one per genuinely-private field. If any surfaces in a
# published message or stored episode the §E wall has been breached. Every field
# that carries *content* (not the public ``addressed_to`` participant id) gets its
# own marker so a partial leak of any single field is caught, not just ``intent``.
_PRIVATE_INTENT = "WALLED-PLAN-7f3a name the write-path risk no peer can see"
_PRIVATE_POINT = "WALLED-PLAN-kp9 Redis serializes writes under contention"
_PRIVATE_AVOID = "WALLED-PLAN-av2 that Redis is fast for reads"
_PRIVATE_MARKERS = (_PRIVATE_INTENT, _PRIVATE_POINT, _PRIVATE_AVOID)

_PLAN = CompositionPlan(
    intent=_PRIVATE_INTENT,
    key_points=(_PRIVATE_POINT, "our p99 is write-heavy"),
    addressed_to="iron-fox",
    avoid_restating=(_PRIVATE_AVOID,),
)

# PR 9 reflexion markers. The discarded first-pass draft (the compose output the
# critic flags) and the critic's note are the Phase-5 intermediates the §E wall
# must also cover; only the final revised message (``_REVISED_MESSAGE``, carrying
# no marker) may ship.
_DISCARDED_DRAFT = "WALLED-DRAFT-1a first-pass reply no peer should ever see"
_PRIVATE_CRITIQUE = "WALLED-CRITIQUE-2b the draft buries the write-path risk"
_REVISED_MESSAGE = "Final answer: Redis, with write-path caveats noted."
_REFLEXION_MARKERS = (_DISCARDED_DRAFT, _PRIVATE_CRITIQUE)

# The leased ``fast`` alias the reflexion critic resolves to — a mock model whose
# ``model`` name (``mock-fast``) lets the provider tell a critic call apart from
# the compose / revise quality calls (mirrors the reflexion unit tests).
_FAST_ALIAS_MAP: dict[str, dict] = {
    "fast": {
        "provider": "mock",
        "model": "mock-fast",
        "input_per_1m_tokens": 0.0,
        "output_per_1m_tokens": 0.0,
    },
}

# The Tier-C compose emits this first-pass draft; built with json.dumps so the
# marker can never break the action JSON (no brace-escaping in an f-string).
_COMPOSE_DRAFT_TEXT = (
    '```json\n[{"action_type": "send_channel_message", '
    '"payload": {"channel_id": "group:planning", "content": '
    + json.dumps(_DISCARDED_DRAFT) + "}}]\n```"
)

_SEED = [
    {"role": "user", "content": "We should pick a cache datastore."},
    {"role": "assistant", "content": "Redis is the obvious fit."},
    {"role": "user", "content": "What datastore should we pick for the cache?"},
]


@pytest.fixture(autouse=True)
def _clean_registry():
    clear_registry()
    yield
    clear_registry()


def _persona_config(agent_id: str) -> dict:
    return {
        "id": agent_id,
        "model": "test-model",
        "role": "RFC 0051 no-leak test persona",
        "type": "persona",
        "max_llm_calls": 3,
        "max_tokens": 512,
        "tools": [],
        "persona": {
            "name": agent_id,
            "background": "Used by the RFC 0051 PR 3 no-leak test.",
            "behavior": {
                "directness": "balanced",
                "formality": "professional",
                "risk_tolerance": "moderate",
            },
        },
        "memory": {"db_path": ":memory:", "working": {"max_tokens": 50000}},
        "relationships": [],
    }


def _compose_client() -> tuple[LLMClient, MagicMock]:
    """The quality compose turn emits one channel reply whose content is plain
    'ack' — deliberately *not* echoing the plan, so any plan text in a published
    message would have to come from a leak, not the model parroting it."""
    provider = AsyncMock()
    provider.create_message = AsyncMock(return_value=LLMResponse(
        text=(
            '```json\n[{"action_type": "send_channel_message", '
            '"payload": {"channel_id": "group:planning", "content": "ack"}}]\n```'
        ),
        stop_reason=StopReason.END_TURN,
        usage=Usage(10, 5),
    ))
    provider.format_tool_definitions = MagicMock(return_value=[])
    provider.append_tool_round = MagicMock(side_effect=lambda msgs, resp, results: msgs)
    return LLMClient(provider), provider.create_message


async def _make_agent(agent_id: str = "ember-owl") -> tuple[_LLMPersonaAgent, MagicMock]:
    client, create_message = _compose_client()
    agent = create_persona_agent(
        agent_id=agent_id, config=_persona_config(agent_id), llm_client=client,
    )
    await agent.initialize_memory()
    return agent, create_message


def _reflexion_client() -> tuple[LLMClient, MagicMock]:
    """A provider that drives a real reflexion rewrite over one turn. It tells the
    three call shapes apart so each produces the right artifact:

    * the **critic** (``model == 'mock-fast'``) flags the first draft weak (with a
      private critique note) then passes the rewrite — ``weak: yes`` → ``weak: no``;
    * the first **quality** call is the Tier-C **compose** → the discarded first-pass
      draft (``_DISCARDED_DRAFT``);
    * the next quality call is the **revise** → the clean ``_REVISED_MESSAGE`` (no
      marker), the only text that may ship.
    """
    provider = AsyncMock()
    critic_verdicts = iter([f"weak: yes\ncritique: {_PRIVATE_CRITIQUE}", "weak: no"])
    quality_calls = {"n": 0}

    def _respond(**kwargs):
        if kwargs.get("model") == "mock-fast":  # the leased critic pass
            return LLMResponse(
                text=next(critic_verdicts), stop_reason=StopReason.END_TURN, usage=Usage(5, 3),
            )
        quality_calls["n"] += 1
        if quality_calls["n"] == 1:  # the Tier-C compose → discarded first draft
            return LLMResponse(
                text=_COMPOSE_DRAFT_TEXT, stop_reason=StopReason.END_TURN, usage=Usage(10, 5),
            )
        return LLMResponse(  # the revise → the only text that ships
            text=_REVISED_MESSAGE, stop_reason=StopReason.END_TURN, usage=Usage(8, 4),
        )

    provider.create_message = AsyncMock(side_effect=_respond)
    provider.format_tool_definitions = MagicMock(return_value=[])
    provider.append_tool_round = MagicMock(side_effect=lambda msgs, resp, results: msgs)
    return LLMClient(provider), provider.create_message


async def _make_reflexion_agent(
    agent_id: str = "ember-owl",
) -> tuple[_LLMPersonaAgent, MagicMock]:
    client, create_message = _reflexion_client()
    agent = create_persona_agent(
        agent_id=agent_id, config=_persona_config(agent_id), llm_client=client,
    )
    await agent.initialize_memory()
    return agent, create_message


def _event() -> AgentEvent:
    return AgentEvent(
        event_type=EventType.CHANNEL_MESSAGE,
        payload={
            "content": "What datastore should we pick for the cache?",
            "channel_type": "group",
            "respond_policy": "always",
            "mentions": [],
            "salience_gated": True,
        },
        channel_id="group:planning",
        sender_id="alice",
    )


def _action_text(action) -> str:
    """Every published action's payload flattened to a string for leak scanning."""
    return json.dumps(action.payload, default=str)


class TestPlanThreadsIntoComposeButNeverLeaks:
    async def test_plan_reaches_compose_prompt_but_not_messages_or_store(self):
        agent, compose = await _make_agent()
        stored_actions: list = []
        original_store = agent._store_event_episode

        async def _capture_store(event, actions):
            stored_actions.extend(actions)
            return await original_store(event, actions)

        outcome = SalienceOutcome(
            silence=False, user_message="formatted", seed=list(_SEED), plan=_PLAN,
        )
        with patch(_SEAM_PATH, new=AsyncMock(return_value=outcome)), \
                patch.object(agent, "_store_event_episode", side_effect=_capture_store):
            actions = await agent.on_event(_event())

        # Positive control: *every* private field threaded into the compose
        # prompt (not just intent), so a silently-dropped field fails loudly here
        # rather than passing the no-leak scan below vacuously.
        compose.assert_awaited_once()
        system_prompt = compose.await_args.kwargs["system"]
        for marker in _PRIVATE_MARKERS:
            assert marker in system_prompt, f"plan field must reach the compose prompt: {marker}"

        # The turn actually posted (so the no-leak assertions are about a real
        # published message, not a suppressed turn).
        published = [a for a in actions if a.action_type is ActionType.SEND_CHANNEL_MESSAGE]
        assert published, "the should_post=true turn must publish a message"

        # No leak to the channel: no private field is in any published payload.
        for action in actions:
            text = _action_text(action)
            for marker in _PRIVATE_MARKERS:
                assert marker not in text

        # No leak to the store: no private field is in any persisted-episode
        # action (what an RFC 0034 reconstruction would hand a peer).
        assert stored_actions, "the turn must store an episode"
        for action in stored_actions:
            text = _action_text(action)
            for marker in _PRIVATE_MARKERS:
                assert marker not in text

    async def test_no_plan_outcome_composes_without_a_plan_section(self):
        """The dark default: a speak outcome with ``plan=None`` (today's
        production path, since the seam passes ``mode: off``) composes exactly as
        before — no private section, nothing to leak."""
        agent, compose = await _make_agent()
        outcome = SalienceOutcome(
            silence=False, user_message="formatted", seed=list(_SEED), plan=None,
        )
        with patch(_SEAM_PATH, new=AsyncMock(return_value=outcome)):
            await agent.on_event(_event())

        compose.assert_awaited_once()
        assert _PRIVATE_INTENT not in compose.await_args.kwargs["system"]


class TestReflexionDraftAndCritiqueNeverLeak:
    """RFC 0051 PR 9 (Phase 5b) — the privacy wall extends to the reflexion loop.

    A ``mode: plan`` + ``reasoning.revise ≥ 1`` turn produces two new private
    intermediates: the **discarded first-pass draft** (the compose output the
    critic flags) and the **critic note**. Only the *final revised* message is
    published; the §E wall must keep both intermediates out of every published
    message AND out of the episodic store (an RFC 0034 reconstruction)."""

    async def test_discarded_draft_and_critique_never_reach_messages_or_store(self):
        agent, create_message = await _make_reflexion_agent()
        stored_actions: list = []
        original_store = agent._store_event_episode

        async def _capture_store(event, actions):
            stored_actions.extend(actions)
            return await original_store(event, actions)

        # ``revise=1`` arms the loop; ``plan`` is what the critic critiques against.
        outcome = SalienceOutcome(
            silence=False, user_message="formatted", seed=list(_SEED), plan=_PLAN, revise=1,
        )
        with use_alias_map(_FAST_ALIAS_MAP), \
                patch(_SEAM_PATH, new=AsyncMock(return_value=outcome)), \
                patch.object(agent, "_store_event_episode", side_effect=_capture_store):
            actions = await agent.on_event(_event())

        # The critic actually ran (a 'mock-fast' call) → the rewrite genuinely
        # happened, so the discarded draft below is a real intermediate rather than
        # a draft that was never produced (a vacuous pass).
        models = [c.kwargs.get("model") for c in create_message.await_args_list]
        assert "mock-fast" in models, "the reflexion critic must have run"

        # Positive control: the published message is the REVISED text — the post
        # the turn actually shipped is the rewrite, not the discarded draft.
        published = [a for a in actions if a.action_type is ActionType.SEND_CHANNEL_MESSAGE]
        assert published, "the should_post=true turn must publish a message"
        assert any(_REVISED_MESSAGE in _action_text(a) for a in published), (
            "the revised draft is what ships"
        )

        # No leak to the channel: neither the discarded draft nor the critic note
        # appears in any published payload.
        for action in actions:
            text = _action_text(action)
            for marker in _REFLEXION_MARKERS:
                assert marker not in text, f"reflexion intermediate must not ship: {marker}"

        # No leak to the store: neither reaches the episodic store a peer would
        # reconstruct from (the store runs AFTER reflexion replaces the content).
        assert stored_actions, "the turn must store an episode"
        for action in stored_actions:
            text = _action_text(action)
            for marker in _REFLEXION_MARKERS:
                assert marker not in text, f"reflexion intermediate must not persist: {marker}"
