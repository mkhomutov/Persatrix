"""Tests for the ``memory-tool-usage`` snippet honesty fix (v0.3.7
conversation test-findings PR plan, F-3a / PR 4).

The snippet told every memory-capable persona *"Your memory persists
across conversations."* That promise is **false for the dominant case**:
notes are written room-scoped (session = room, RFC 0031 / memory-scope-
axes.md) and recall defaults to the active session, so a fact saved in
one channel is invisible in another. Probed live, a persona that had
stored a person's name said *"I don't have any notes about your name"* in
a fresh channel — having promised cross-conversation memory.

PR 4 is the **honesty half** and lands *before* PR 5 makes person-keyed
recall actually cross rooms. So the replacement must describe **current**
behaviour — notes persist and accumulate but are scoped to the
conversation you are in — and must **not** swap one false promise
("persists across conversations") for another ("remembered across rooms")
that is not yet true. PR 5 updates this wording when cross-room recall
lands.

The working instruction is unchanged: the persona must still *call*
store_note / recall_notes rather than acknowledge verbally (the live DB
confirmed those stores land — the bug was scope + the prompt promise, not
the tool call). These tests pin both the removal of the false promise and
the honest scope statement; the byte-identical golden in
``test_persona_section_composer`` locks the exact bytes.
"""

from __future__ import annotations

from copy import deepcopy

from agents.persona import create_persona_agent
from agents.prompt_loader import load_snippet

from ._persona_test_helpers import _PERSONA_CONFIG, _make_client

_SNIPPET = "memory-tool-usage"


# ─── Snippet loader ───────────────────────────────────────────


class TestSnippetLoader:
    def test_snippet_loads(self) -> None:
        assert load_snippet(_SNIPPET).strip()

    def test_working_tool_instruction_preserved(self) -> None:
        """The behaviour that *works* must survive the rewording: the
        persona is told to actually call the tools, not just acknowledge.
        """
        lower = load_snippet(_SNIPPET).lower()
        assert "store_note" in lower
        assert "recall_notes" in lower
        assert "must call store_note" in lower
        assert "do not just acknowledge" in lower
        # The contact-note convention PR 5 builds on stays in place.
        assert "contact:<user_id>" in load_snippet(_SNIPPET)

    def test_false_cross_conversation_promise_removed(self) -> None:
        """The blanket "memory persists across conversations" claim — false
        while notes are room-scoped — must be gone, and not replaced with
        an equally-unbacked "across rooms" promise ahead of PR 5.
        """
        lower = load_snippet(_SNIPPET).lower()
        assert "persists across conversations" not in lower
        assert "across rooms" not in lower
        assert "across conversations" not in lower

    def test_honest_room_scoped_statement_present(self) -> None:
        """The replacement states the current, true scope: notes persist
        but are scoped to the conversation, and an empty recall should be
        admitted plainly rather than guessed around.
        """
        lower = load_snippet(_SNIPPET).lower()
        assert "scoped to the conversation" in lower
        assert "say so plainly" in lower


# ─── System-prompt integration ────────────────────────────────


class TestSystemPromptIntegration:
    """The snippet is conditional on memory tools (unchanged): present when
    the persona has them, omitted otherwise.
    """

    async def _make_agent(self, config: dict | None = None):
        cfg = config or deepcopy(_PERSONA_CONFIG)
        agent = create_persona_agent(
            agent_id=cfg["id"], config=cfg, llm_client=_make_client(),
        )
        await agent.initialize_memory()
        return agent

    async def test_snippet_present_with_memory_tools(self) -> None:
        agent = await self._make_agent()
        try:
            prompt = agent._build_system_prompt()
            assert load_snippet(_SNIPPET) in prompt
            assert "persists across conversations" not in prompt.lower()
        finally:
            await agent.close_memory()

    async def test_snippet_omitted_without_memory_tools(self) -> None:
        agent = await self._make_agent()
        try:
            agent._memory_tools = []
            prompt = agent._build_system_prompt()
            assert "store_note" not in prompt
        finally:
            await agent.close_memory()
