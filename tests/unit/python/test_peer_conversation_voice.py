"""Tests for the peer-conversation-voice prompt snippet (v0.3.7, RFC 0030
relevance amendment — Workstream 1c of the v0.3.7 realism plan).

This safety snippet lands alongside the existing ``reply-discretion`` /
``conversational-pacing`` snippets in ``prompts/runtime/safety/`` and is
loaded by ``_build_system_prompt`` in
``agents/persona_runtime/prompt_assembly.py``.

It is a *behavioural* nudge, not a config-driven section. Where the
response gate (``response_gate.py``) decides **whether** a persona may
speak on a group channel, and ``reply-discretion`` says producing no
reply is a valid outcome, ``peer-conversation-voice`` shapes **how** the
persona speaks when it does: as a colleague among peers rather than an
assistant serving a user — address people by name, build on what was
already said this round, and disagree/defer like a colleague instead of
performing helpfulness.

Per the v0.3.7 plan (Workstream 1c), the snippet frames group-channel
turns; like ``reply-discretion`` it carries the DM carve-out inline and
renders unconditionally (the prompt assembler has no per-turn channel
context), so the prose — not a code gate — distinguishes the registers.
A prompt-assembly unit test asserts it is present for group-channel
turns (which, given unconditional render, is every assembled prompt).

The byte-identical golden in ``test_persona_section_composer.py`` locks
the full prompt-bytes contract; this module covers the snippet loader,
the unconditional-render contract, substring presence, and ordering so a
future refactor that drops the snippet fails specifically here with a
focused diagnostic.
"""

from __future__ import annotations

from copy import deepcopy

from agents.persona import create_persona_agent
from agents.prompt_loader import load_snippet

from ._persona_test_helpers import _PERSONA_CONFIG, _make_client

# ─── Snippet loader ───────────────────────────────────────────


class TestSnippetLoader:
    """The snippet resolves under ``prompts/runtime/safety/`` via the
    existing ``load_snippet`` API. The loader strips one trailing
    newline; the rest of the file content is returned as-is.
    """

    def test_peer_conversation_voice_snippet_loads(self) -> None:
        text = load_snippet("peer-conversation-voice")
        assert text.strip()
        lower = text.lower()
        # Central guidance: the persona is a colleague/peer, not an
        # assistant; it should build on what others said and carry the
        # DM carve-out inline.
        assert "peer" in lower or "colleague" in lower
        assert "assistant" in lower
        assert "dm" in lower or "direct message" in lower


# ─── System-prompt integration ────────────────────────────────


class TestSystemPromptIntegration:
    """The snippet renders unconditionally into the persona system
    prompt, ordered after conversational-pacing and before the
    conditional memory-tool-usage snippet.
    """

    async def _make_agent(self, config: dict | None = None):
        cfg = config or deepcopy(_PERSONA_CONFIG)
        agent = create_persona_agent(
            agent_id=cfg["id"], config=cfg, llm_client=_make_client(),
        )
        await agent.initialize_memory()
        return agent

    async def test_peer_voice_appears_in_prompt(self) -> None:
        agent = await self._make_agent()
        try:
            prompt = agent._build_system_prompt()
            snippet = load_snippet("peer-conversation-voice")
            assert snippet in prompt
        finally:
            await agent.close_memory()

    async def test_peer_voice_renders_even_without_memory_tools(self) -> None:
        """The peer-voice nudge is unconditional — it is not gated on
        memory tools the way ``memory-tool-usage`` is. A persona
        configured without memory tools still receives it.
        """
        agent = await self._make_agent()
        try:
            agent._memory_tools = []
            prompt = agent._build_system_prompt()
            assert load_snippet("peer-conversation-voice") in prompt
            # Sanity: the memory-tool nudge is omitted in this branch.
            assert "store_note" not in prompt
        finally:
            await agent.close_memory()

    async def test_ordering_after_pacing_before_memory(self) -> None:
        """Snippet ordering is stable: reply-discretion →
        conversational-pacing → peer-conversation-voice →
        memory-tool-usage.

        A drift in ordering would not break behaviour, but would break
        the byte-identical golden in ``test_persona_section_composer``
        and would muddy diffs in future prompt audits.
        """
        agent = await self._make_agent()
        try:
            prompt = agent._build_system_prompt()
            disc_pos = prompt.find(load_snippet("reply-discretion"))
            pace_pos = prompt.find(load_snippet("conversational-pacing"))
            peer_pos = prompt.find(load_snippet("peer-conversation-voice"))
            mem_pos = prompt.find("store_note")

            assert -1 < disc_pos < pace_pos < peer_pos < mem_pos
        finally:
            await agent.close_memory()
