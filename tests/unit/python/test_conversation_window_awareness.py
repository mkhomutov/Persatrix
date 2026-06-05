"""Tests for the ``conversation-window-awareness`` safety snippet (v0.3.7
conversation test-findings PR plan, F-2).

The persona runtime reconstructs the in-progress conversation as a rolling
transcript in the LLM ``messages`` array every turn (RFC 0034 Conversation
Window). Nothing in the *system prompt*, however, told the persona that
this view exists. Probed on the live stack, personas denied being able to
read past messages ("I don't have access to past messages… limited to
recent messages in this session") and, asked how many messages they could
see, hedged with "no specific count" — even though the window *was*
populated and recent context demonstrably worked. The model was left to
fall back on the generic "I'm an AI, I don't retain conversations"
disclaimer.

This snippet tells the persona, plainly, that it sees a rolling transcript
of the recent conversation, that older turns scroll out of view as the
conversation grows, and that it should describe this honestly rather than
deny memory or invent a hard message count. It is a *behavioural /
perceptual* nudge — like the now-anchor it grounds the persona in its
current situation — and renders unconditionally for every persona.

These tests pin the snippet content and its unconditional render +
ordering (immediately after the now-anchor, before the user-message
delimiter contract) so a future prompt edit that drops it fails here with
a focused diagnostic; the byte-identical golden in
``test_persona_section_composer`` locks the exact bytes.
"""

from __future__ import annotations

from copy import deepcopy

from agents.persona import create_persona_agent
from agents.prompt_loader import load_snippet

from ._persona_test_helpers import _PERSONA_CONFIG, _make_client

# ─── Snippet loader ───────────────────────────────────────────


class TestSnippetLoader:
    """The snippet resolves under ``prompts/runtime/safety/`` via
    ``load_snippet`` and carries the F-2 window-awareness guidance.
    """

    def test_conversation_window_awareness_snippet_loads(self) -> None:
        text = load_snippet("conversation-window-awareness")
        assert text.strip()

    def test_describes_the_rolling_transcript(self) -> None:
        """The snippet must tell the persona it sees a rolling transcript
        of the recent conversation that scrolls as the conversation grows.
        """
        lower = load_snippet("conversation-window-awareness").lower()
        assert "transcript" in lower
        assert "recent" in lower
        assert "scroll" in lower

    def test_forbids_denying_memory_and_inventing_a_count(self) -> None:
        """Directly targets the two F-2 symptoms: the persona must not
        deny having any memory / access to prior messages, and must not
        fabricate a specific message-count limit.
        """
        lower = load_snippet("conversation-window-awareness").lower()
        assert "no memory" in lower
        assert "specific number" in lower


# ─── System-prompt integration ────────────────────────────────


class TestSystemPromptIntegration:
    """The snippet renders unconditionally into the persona system prompt
    — including for a persona with no memory tools — and sits immediately
    after the now-anchor, before the user-message delimiter contract.
    """

    async def _make_agent(self, config: dict | None = None):
        cfg = config or deepcopy(_PERSONA_CONFIG)
        agent = create_persona_agent(
            agent_id=cfg["id"], config=cfg, llm_client=_make_client(),
        )
        await agent.initialize_memory()
        return agent

    async def test_window_snippet_appears_in_prompt(self) -> None:
        agent = await self._make_agent()
        try:
            prompt = agent._build_system_prompt()
            assert load_snippet("conversation-window-awareness") in prompt
        finally:
            await agent.close_memory()

    async def test_renders_even_without_memory_tools(self) -> None:
        agent = await self._make_agent()
        try:
            agent._memory_tools = []
            prompt = agent._build_system_prompt()
            assert load_snippet("conversation-window-awareness") in prompt
        finally:
            await agent.close_memory()

    async def test_ordering_after_now_anchor_before_delimiters(self) -> None:
        """Ordering is stable: now-anchor → conversation-window-awareness
        → user-message-delimiters. The now-anchor has no ``load_snippet``
        text (it is a rendered persona section), so it is located by its
        stable ``Current time:`` marker.
        """
        agent = await self._make_agent()
        try:
            prompt = agent._build_system_prompt()
            anchor_pos = prompt.find("Current time:")
            window_pos = prompt.find(
                load_snippet("conversation-window-awareness"),
            )
            delim_pos = prompt.find(load_snippet("user-message-delimiters"))
            assert -1 < anchor_pos < window_pos < delim_pos
        finally:
            await agent.close_memory()
