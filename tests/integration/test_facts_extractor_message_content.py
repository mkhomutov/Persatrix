"""ISSUE-0054 root cause — the close-path extractor must see message text.

The v0.3.1 fence fix ([commit a6c3332]) unwrapped the ```` ```json ````
envelope but the RFC 0026 facts tier stayed inert: the close-path
summariser/extractor was fed only the deterministic per-turn action
envelope (``"Event: channel_message → Actions: [...]"``), never the
inbound message body, so the combined summarise + extract LLM call had
no facts to extract.

This module drives the full close path with distinctive message bodies
and pins both halves of the fix: the bodies reach the summariser prompt
(``_handle_multi_turn_event`` stashes them on the turn,
``close_entries.interaction_to_entries`` projects them into the prompt), and a
content-aware extractor consequently populates the ``facts`` table —
while the persisted ``context_json`` stays body-free per RFC 0020 §D.

Split out of ``test_facts_extractor_close.py`` to keep both files under
the 500-line review cap (``scripts/checks/file_size.py --strict``).
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from agents.llm_client import LLMClient, LLMResponse, StopReason, Usage
from agents.persona import create_persona_agent
from agents.persona_runtime import _LLMPersonaAgent
from agents.persona_runtime.summarize_close import SUMMARIZATION_MAX_OUTPUT_TOKENS
from agents.persona_types import AgentEvent, EventType
from agents.tools.registry import clear_registry

from ._summarize_close_helpers import PERSONA_CONFIG, drain

SUMMARY_TEXT = "Bob mentioned his daughter Mira and his preference for tea."


@pytest.fixture(autouse=True)
def _clean_registry():
    clear_registry()
    yield
    clear_registry()


async def _make_agent(client: LLMClient) -> _LLMPersonaAgent:
    agent = create_persona_agent(
        agent_id=PERSONA_CONFIG["id"],
        config=PERSONA_CONFIG,
        llm_client=client,
    )
    await agent.initialize_memory()
    return agent


def _make_content_aware_client(captured: list[str]) -> LLMClient:
    """Mock LLM whose summariser call records the prompt it receives
    and extracts a fact *only* when the prompt actually carries the
    inbound message body.

    This mirrors a real extractor LLM: it cannot invent
    ``has_child_named → Mira`` unless the word "Mira" reaches its
    input.  Pre-fix the close path fed the summariser only the
    per-turn action envelope (``"Event: channel_message → Actions:
    [...]"``), so the prompt never contained "Mira" and this mock
    returned ``facts: []`` — reproducing the ISSUE-0054 symptom (the
    ``facts`` table stays empty after a full interaction close).
    """
    mock_provider = AsyncMock()

    async def _route(*, model, messages, system, tools, max_tokens, temperature):
        if max_tokens == SUMMARIZATION_MAX_OUTPUT_TOKENS:
            prompt = messages[0]["content"]
            captured.append(prompt)
            facts: list[dict] = []
            if "Mira" in prompt:
                facts = [{
                    "subject": "bob",
                    "predicate": "has_child_named",
                    "object": "Mira",
                    "certainty": 0.95,
                }]
            return LLMResponse(
                text=json.dumps({"summary": SUMMARY_TEXT, "facts": facts}),
                stop_reason=StopReason.END_TURN,
                usage=Usage(120, 30),
            )
        return LLMResponse(
            text='```json\n[{"action_type": "do_nothing", "payload": {}}]\n```',
            stop_reason=StopReason.END_TURN,
            usage=Usage(10, 5),
        )

    mock_provider.create_message = AsyncMock(side_effect=_route)
    mock_provider.format_tool_definitions = MagicMock(return_value=[])
    mock_provider.append_tool_round = MagicMock(
        side_effect=lambda msgs, resp, results: msgs,
    )
    return LLMClient(mock_provider)


@pytest.mark.asyncio
class TestExtractorReceivesMessageContent:
    """ISSUE-0054 root cause — the close-path summariser/extractor must
    be fed the actual inbound message text, not just the deterministic
    per-turn action envelope.
    """

    async def test_message_bodies_reach_prompt_and_facts_persist(self):
        captured: list[str] = []
        agent = await _make_agent(_make_content_aware_client(captured))
        try:
            peer = "bob"
            for body in (
                "I'm picking up my daughter Mira from school",
                "She dislikes loud phone calls",
            ):
                await agent.on_event(AgentEvent(
                    event_type=EventType.CHANNEL_MESSAGE,
                    payload={"content": body},
                    sender_id=peer,
                ))
            await agent.on_event(AgentEvent(
                event_type=EventType.CHANNEL_MESSAGE,
                payload={"content": "thanks, bye"},
                sender_id=peer,
                metadata={"chat_end": True},
            ))
            await drain(agent)

            # The summariser call fired and its prompt carried the
            # inbound message bodies — the root-cause assertion.
            assert captured, "summariser LLM call never fired"
            prompt = captured[-1]
            assert "daughter Mira" in prompt
            assert "dislikes loud phone calls" in prompt

            # Consequently the content-aware extractor produced a fact.
            live = await agent.memory.facts.recall(subject="bob")
            assert len(live) == 1, (
                "facts table empty — ISSUE-0054 symptom reproduced"
            )
            assert live[0].predicate == "has_child_named"
            assert live[0].object == "Mira"
        finally:
            await agent.close_memory()

    async def test_single_turn_interaction_still_extracts_facts(self):
        """F-6 (v0.3.1 MT-MEMORY-005 re-run) — a fact stated in a
        one-turn interaction (a single message that then closes)
        must still reach the facts tier.  Pre-fix the close path
        short-circuited ``turn_count == 1`` onto a deterministic
        placeholder summary and dropped the facts half, so a fact
        stated in a single message that idle-closed was lost — the
        re-run's I3 budget-spreadsheet commitment never reached the
        ``facts`` table for exactly this reason.
        """
        captured: list[str] = []
        agent = await _make_agent(_make_content_aware_client(captured))
        try:
            peer = "bob"
            # One channel message that both states a fact and closes
            # the interaction → a single-turn conversational close.
            await agent.on_event(AgentEvent(
                event_type=EventType.CHANNEL_MESSAGE,
                payload={
                    "content": "I'm picking up my daughter Mira from school",
                },
                sender_id=peer,
                metadata={"chat_end": True},
            ))
            await drain(agent)

            # The summariser fired for the single-turn close and its
            # prompt carried the message body.
            assert captured, (
                "summariser LLM call never fired for the single-turn "
                "close — F-6 regression"
            )
            assert "daughter Mira" in captured[-1]

            # The fact reached the tier — not dropped by the
            # turn_count==1 short-circuit.
            live = await agent.memory.facts.recall(subject="bob")
            assert len(live) == 1, (
                "single-turn interaction dropped its fact — F-6 regression"
            )
            assert live[0].predicate == "has_child_named"
            assert live[0].object == "Mira"
        finally:
            await agent.close_memory()

    async def test_message_body_not_persisted_in_episode_context(self):
        """RFC 0020 §D still holds for the *persisted* episode: the
        message body is carried only on the in-memory turn for the
        close-path extractor and is stripped before the turn lands in
        ``context_json`` — the episodic store never doubles as a
        message log."""
        captured: list[str] = []
        agent = await _make_agent(_make_content_aware_client(captured))
        try:
            peer = "bob"
            secret_body = "picking up my daughter Mira super-secret-xyzzy"
            await agent.on_event(AgentEvent(
                event_type=EventType.CHANNEL_MESSAGE,
                payload={"content": secret_body},
                sender_id=peer,
            ))
            await agent.on_event(AgentEvent(
                event_type=EventType.CHANNEL_MESSAGE,
                payload={"content": "bye"},
                sender_id=peer,
                metadata={"chat_end": True},
            ))
            await drain(agent)

            # The extractor saw the body (in-memory carry works) ...
            assert captured and "super-secret-xyzzy" in captured[-1]

            # ... but it did NOT leak into the persisted context_json.
            db = agent._episodic_memory._ensure_db()
            async with db.execute(
                "SELECT context_json FROM episodes WHERE agent_id = ?",
                (agent.agent_id,),
            ) as cursor:
                rows = await cursor.fetchall()
            assert len(rows) == 1
            assert "super-secret-xyzzy" not in rows[0][0], (
                "message body leaked into context_json; RFC 0020 §D "
                "data-minimisation violated"
            )
        finally:
            await agent.close_memory()
