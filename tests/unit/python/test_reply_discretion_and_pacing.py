"""Tests for the reply-discretion + conversational-pacing prompt snippets.

These two safety snippets land alongside the existing
``user-message-delimiters`` / ``external-data-handling`` /
``memory-tool-usage`` snippets in ``prompts/runtime/safety/`` and are
loaded by ``_build_system_prompt`` in
``agents/persona_runtime/prompt_assembly.py``.

They are *behavioural* nudges, not config-driven sections:

- **reply-discretion** tells the persona that producing no reply is a
  valid turn outcome on group channels (the response gate already
  decided we may speak — but having permission to speak is not the same
  as a duty to speak). It also pins the DM-channel invariant that a DM
  always expects at least a brief reply, mirroring the
  ``response_gate.py`` rule that "a DM with no reply is broken by
  construction".
- **conversational-pacing** tells the persona to match the length and
  register of the inbound message, so a one-line greeting does not
  produce a paragraph and vice versa.

Both snippets are always-on (unconditional) and rendered after the
existing safety snippets but before the conditional memory-tool-usage
snippet.

The byte-identical golden in ``test_persona_section_composer.py``
locks the full prompt-bytes contract; this module covers the snippet
loaders, the unconditional-render contract, and substring presence so
a future refactor that drops one of these snippets fails specifically
here with a focused diagnostic.
"""

from __future__ import annotations

from copy import deepcopy

from agents.persona import create_persona_agent
from agents.prompt_loader import load_snippet

from ._persona_test_helpers import _PERSONA_CONFIG, _make_client


# ─── Snippet loaders ──────────────────────────────────────────


class TestSnippetLoaders:
    """The two new snippets resolve under ``prompts/runtime/safety/``
    via the existing ``load_snippet`` API. The loader strips one
    trailing newline; the rest of the file content is returned as-is.
    """

    def test_reply_discretion_snippet_loads(self) -> None:
        text = load_snippet("reply-discretion")
        # Snippet is non-empty and carries the central guidance: staying
        # silent is a valid outcome on group channels, and DMs always
        # expect a reply.
        assert text.strip()
        assert "silent" in text.lower()
        assert "dm" in text.lower() or "direct message" in text.lower()

    def test_conversational_pacing_snippet_loads(self) -> None:
        text = load_snippet("conversational-pacing")
        assert text.strip()
        # Central guidance: match length / register of the inbound
        # message rather than producing fixed-shape paragraphs.
        assert "match" in text.lower()
        assert "length" in text.lower() or "register" in text.lower()


# ─── System-prompt integration ────────────────────────────────


class TestSystemPromptIntegration:
    """The snippets render unconditionally into the persona system
    prompt, ordered after external-data-handling and before
    memory-tool-usage.
    """

    async def _make_agent(self, config: dict | None = None):
        cfg = config or deepcopy(_PERSONA_CONFIG)
        agent = create_persona_agent(
            agent_id=cfg["id"], config=cfg, llm_client=_make_client(),
        )
        await agent.initialize_memory()
        return agent

    async def test_reply_discretion_appears_in_prompt(self) -> None:
        agent = await self._make_agent()
        try:
            prompt = agent._build_system_prompt()
            snippet = load_snippet("reply-discretion")
            assert snippet in prompt
        finally:
            await agent.close_memory()

    async def test_conversational_pacing_appears_in_prompt(self) -> None:
        agent = await self._make_agent()
        try:
            prompt = agent._build_system_prompt()
            snippet = load_snippet("conversational-pacing")
            assert snippet in prompt
        finally:
            await agent.close_memory()

    async def test_snippets_render_even_without_memory_tools(self) -> None:
        """Reply-discretion and conversational-pacing are unconditional
        — they are not gated on memory tools the way ``memory-tool-usage``
        is. A persona configured without memory tools still receives the
        conversational guidance.
        """
        agent = await self._make_agent()
        try:
            agent._memory_tools = []
            prompt = agent._build_system_prompt()
            assert load_snippet("reply-discretion") in prompt
            assert load_snippet("conversational-pacing") in prompt
            # Sanity: the memory-tool nudge is omitted in this branch.
            assert "store_note" not in prompt
        finally:
            await agent.close_memory()

    async def test_ordering_after_external_data_before_memory(self) -> None:
        """Snippet ordering is stable: external-data-handling →
        reply-discretion → conversational-pacing → memory-tool-usage.

        A drift in ordering would not break behaviour, but would break
        the byte-identical golden in ``test_persona_section_composer``
        and would muddy diffs in future prompt audits.
        """
        agent = await self._make_agent()
        try:
            prompt = agent._build_system_prompt()
            ext_pos = prompt.find("<external_data>")
            disc_pos = prompt.find(load_snippet("reply-discretion"))
            pace_pos = prompt.find(load_snippet("conversational-pacing"))
            mem_pos = prompt.find("store_note")

            assert -1 < ext_pos < disc_pos < pace_pos < mem_pos
        finally:
            await agent.close_memory()
