"""
RFC 0020 PR 4 — two-phase write + failure modes + self-DM guard.

Split out of ``test_summarize_on_close.py`` to keep that file under
the 500-line cap enforced by ``scripts/checks/file_size.py --strict``
after PR #229 review fixes added Must-Fix #1 (sentinel-visible-mid-
flight) and Should-Fix #3 / #4 coverage.

Pins:

* :class:`TestTwoPhaseWrite` — PR #229 review Must-Fix #1: the row
  must exist with the ``[summary pending]`` sentinel between Phase 1
  (synchronous INSERT) and Phase 2 (background ``UPDATE``) so a
  process crash mid-LLM leaves the janitor a real row to sweep.
* :class:`TestSummarisationFailureModes` — PR #229 review Should-Fix
  #3: timeout and empty-text branches of
  :func:`summarize_closed_interaction` fall back to
  :data:`SUMMARY_UNAVAILABLE_TEXT`.
* :func:`test_extract_peer_self_dm_returns_none` — PR #229 review
  Should-Fix #4: a self-DM scope must not return the agent as its
  own peer.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from agents.llm_client import LLMClient, LLMResponse, StopReason, Usage
from agents.memory.interactions import (
    SUMMARY_PENDING_TEXT,
    SUMMARY_UNAVAILABLE_TEXT,
)
from agents.persona_types import AgentEvent, EventType
from agents.tools.registry import clear_registry

from ._summarize_close_helpers import (
    LLM_SUMMARY_TEXT,
    drain,
    episode_summary,
    make_agent,
    make_summary_client,
    send_n_turns,
)


@pytest.fixture(autouse=True)
def _clean_registry():
    clear_registry()
    yield
    clear_registry()


@pytest.mark.asyncio
class TestTwoPhaseWrite:
    """The close path commits ``[summary pending]`` *before* the LLM call.

    Pins PR #229 review Must-Fix #1: the row must exist with the
    pending sentinel between Phase 1 (synchronous INSERT) and Phase 2
    (background ``UPDATE``) so that a process crash mid-LLM leaves
    the janitor a real row to sweep.  Without this guarantee the
    sentinel + janitor are dead code.
    """

    async def test_pending_sentinel_visible_before_drain(self):
        """Between ``chat_end`` and ``drain``, the row carries the sentinel."""
        # A summary client whose summarisation call hangs until we
        # release a sentinel — lets us assert on the row state while
        # the background task is parked mid-LLM.
        import asyncio as _asyncio
        gate = _asyncio.Event()

        mock_provider = AsyncMock()

        async def _route(*, model, messages, system, tools, max_tokens, temperature):
            if max_tokens == 256:
                await gate.wait()  # park the summariser
                return LLMResponse(
                    text=LLM_SUMMARY_TEXT,
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
        agent = await make_agent(client=LLMClient(mock_provider))
        peer = "iron-fox"
        await send_n_turns(agent, peer, 3)
        await agent.on_event(AgentEvent(
            event_type=EventType.MESSAGE_RECEIVED,
            payload={"content": "bye"},
            sender_id=peer,
            metadata={"chat_end": True},
        ))

        # Give the freshly-spawned background task a chance to start
        # and park on ``gate.wait()``.  The sentinel row is committed
        # synchronously before the task is scheduled, so the row is
        # already visible — this sleep only ensures we observe the
        # mid-flight state rather than racing the scheduler.
        await _asyncio.sleep(0)

        summary_before = await episode_summary(agent)
        assert summary_before == SUMMARY_PENDING_TEXT, (
            "Phase 1 must commit the pending sentinel before the LLM call"
        )

        # Release the summariser and drain.
        gate.set()
        await drain(agent)

        summary_after = await episode_summary(agent)
        assert summary_after == LLM_SUMMARY_TEXT


@pytest.mark.asyncio
class TestSummarisationFailureModes:
    """The non-generic-exception branches must also fall back cleanly.

    The original PR only exercised the generic ``RuntimeError`` branch
    of :func:`summarize_closed_interaction`.  These tests pin the
    ``TimeoutError`` and empty-text branches so a future refactor of
    the LLM call site cannot silently drop either.
    """

    async def test_timeout_falls_back_to_unavailable(self, monkeypatch):
        """A summariser hang past ``SUMMARIZATION_TIMEOUT_SEC`` → fallback."""
        # Shrink the timeout to keep the test fast.  Patching the
        # module-level constant is safe because the symbol is read
        # at call time, not captured at import.
        from agents.persona_runtime import summarize_close as _sc
        monkeypatch.setattr(_sc, "SUMMARIZATION_TIMEOUT_SEC", 0.05)

        async def _hang(*args, **kwargs):
            import asyncio as _a
            await _a.sleep(10.0)
            raise AssertionError("should have timed out")

        mock_provider = AsyncMock()

        async def _route(*, model, messages, system, tools, max_tokens, temperature):
            if max_tokens == 256:
                return await _hang()
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

        agent = await make_agent(client=LLMClient(mock_provider))
        peer = "iron-fox"
        await send_n_turns(agent, peer, 3)
        await agent.on_event(AgentEvent(
            event_type=EventType.MESSAGE_RECEIVED,
            payload={"content": "bye"},
            sender_id=peer,
            metadata={"chat_end": True},
        ))
        await drain(agent)

        summary = await episode_summary(agent)
        assert summary == SUMMARY_UNAVAILABLE_TEXT

    async def test_empty_response_falls_back_to_unavailable(self):
        """An LLM reply with empty / whitespace-only text → fallback."""
        agent = await make_agent(client=make_summary_client(text="   "))
        peer = "iron-fox"
        await send_n_turns(agent, peer, 3)
        await agent.on_event(AgentEvent(
            event_type=EventType.MESSAGE_RECEIVED,
            payload={"content": "bye"},
            sender_id=peer,
            metadata={"chat_end": True},
        ))
        await drain(agent)

        summary = await episode_summary(agent)
        assert summary == SUMMARY_UNAVAILABLE_TEXT


def test_extract_peer_self_dm_returns_none():
    """``dm:<id>:<id>`` (self-DM) must not return the agent as its own peer.

    ``scope_for_dm`` sorts but does not de-duplicate, so a future
    caller passing ``self.agent_id`` as ``other_id`` (intentional
    self-talk or a routing bug) would otherwise produce a scope that
    extracts ``(agent_id, ...)`` and let ``record_interaction`` write
    a self-relationship row.  The guard keeps the relationship-memory
    invariant ``other_id != agent_id`` defensive.
    """
    from agents.memory.interactions import Interaction, Turn
    from agents.persona_runtime.summarize_close import (
        extract_peer_from_interaction,
    )

    interaction = Interaction(
        interaction_id="abc",
        scope="dm:agent:agent",
        started_at=0.0,
        closed_at=1.0,
        close_reason="structural",
        turns=[Turn(at=0.0, payload={})],
    )
    peer, peer_type = extract_peer_from_interaction("agent", interaction)
    assert peer is None
    assert peer_type == "agent"
