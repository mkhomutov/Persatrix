"""ISSUE-0118 (v0.3.13 PR 1) — the in-loop tool round stays correctly scoped.

Regression pin for the surface ISSUE-0118 named: a tool recall inside
``_on_event_inner``'s multi-turn loop inherits the handler's scope
binding — the injection path and the tool path agree on the request's
epoch.  Driven through the REAL machinery (``on_event`` binds
``request_scope_from_metadata``; ``asyncio.wait_for``'s child task
copies the ContextVars; ``_execute_tools`` runs the recall) with a
scripted LLM electing the tool call, so a refactor moving tool
execution off the handler task flips this red instead of shipping the
leak silently.  (PR #809 review finding 1: the earlier direct-call
shape asserted only the memory tiers' scope filtering and never touched
the loop it claimed to pin.)

Split from :mod:`test_dispatch_context_scope_threading` at the 500-line
code cap when the principal axis joined the executor-hop threading
(PR #809 review finding 4); like :mod:`test_recall_tool_epoch_wall`,
this module declares its own minimal helpers rather than importing from
a sibling test module.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from agents.acting_classification import acting_classification_scope_from_metadata
from agents.channel_event_classification import CHANNEL_CLASSIFICATION_METADATA_KEY
from agents.epoch_id import (
    EVENT_EPOCH_METADATA_KEY,
    epoch_scope,
    resolve_epoch_id_silent,
)
from agents.llm_client import LLMResponse, LLMToolResult, StopReason, ToolCall, Usage
from agents.persona import create_persona_agent
from agents.persona_types import AgentEvent, EventType
from agents.session_id import EVENT_SESSION_METADATA_KEY
from agents.tools.registry import clear_registry

from ._persona_test_helpers import _PERSONA_CONFIG, _make_client


@pytest.fixture(autouse=True)
def _clean_registry():
    """``create_persona_agent`` registers memory tools globally — isolate
    each test."""
    clear_registry()
    yield
    clear_registry()


def _tool_func(tools, name: str):
    """The named tool's callable, narrowed for mypy (``func`` is Optional
    on :class:`~agents.tools.registry.ToolDefinition`)."""
    func = next(t for t in tools if t.name == name).func
    assert func is not None
    return func


def _acting_internal():
    """Bind an ``internal`` acting classification for the note stores: an
    unbound store stamps ``internal`` (rule (a)) while an unbound RECALL
    floors to ``public`` (rule (b)) — the on_event turn below binds its
    own recall-side classification off the event metadata."""
    return acting_classification_scope_from_metadata(
        {CHANNEL_CLASSIFICATION_METADATA_KEY: "internal"},
    )


class TestInLoopToolRoundParity:
    async def test_tool_round_inside_on_event_resolves_request_epoch(self) -> None:
        """Two notes exist — one in the construction world, one in a
        fresh epoch.  An event delivered under the fresh epoch whose
        scripted turn elects ``recall_notes`` must feed the model ONLY
        the fresh note; a binding lost across a future refactor would
        surface the construction-world note instead (the live 2026-07-30
        leak's mechanism class, in-loop edition)."""
        responses = [
            LLMResponse(
                text=None,
                tool_calls=[ToolCall(
                    id="tc1", name="recall_notes", input={"query": "atlas"},
                )],
                stop_reason=StopReason.TOOL_USE,
                usage=Usage(100, 50),
            ),
            LLMResponse(
                text="Nothing notable on atlas.",
                stop_reason=StopReason.END_TURN,
                usage=Usage(200, 100),
            ),
        ]
        client = _make_client(responses)
        # Observe the ACTUAL in-loop results at the append seam — the one
        # place they cross the client boundary — rather than stubbing any
        # part of the loop under test.
        tool_rounds: list[list[LLMToolResult]] = []

        def _capture(
            msgs: list, _resp: LLMResponse, results: list[LLMToolResult],
        ) -> list:
            tool_rounds.append(list(results))
            return [*msgs, {"role": "assistant", "content": "tool round"},
                    {"role": "user", "content": "tool results"}]

        client._provider.append_tool_round = MagicMock(  # type: ignore[method-assign]
            side_effect=_capture,
        )

        agent = create_persona_agent(
            agent_id="ember-owl", config={**_PERSONA_CONFIG}, llm_client=client,
        )
        await agent.initialize_memory()
        try:
            store_note = _tool_func(agent._memory_tools, "store_note")
            with _acting_internal():
                await store_note(topic="atlas", content="Atlas ships Friday")
                with epoch_scope("mt-crossroom-fresh"):
                    await store_note(
                        topic="atlas", content="fresh world is empty-ish",
                    )
            # Vacuous-pass guard: the construction world must actually
            # differ from the probe epoch.
            assert resolve_epoch_id_silent() != "mt-crossroom-fresh"

            actions = await agent.on_event(AgentEvent(
                event_type=EventType.CHANNEL_MESSAGE,
                # ``respond_policy=always`` with no mentions is the
                # open-floor admit; the channel is ungoverned, so the
                # Tier B salience bid stays out of the turn.
                payload={"content": "any news on atlas?",
                         "respond_policy": "always"},
                channel_id="group:general",
                sender_id="iron-fox",
                metadata={
                    EVENT_EPOCH_METADATA_KEY: "mt-crossroom-fresh",
                    EVENT_SESSION_METADATA_KEY: "conv-x",
                    CHANNEL_CLASSIFICATION_METADATA_KEY: "internal",
                },
            ))

            assert actions, "scripted END_TURN must still parse into actions"
            assert len(tool_rounds) == 1
            [result] = tool_rounds[0]
            assert result.is_error is False
            assert "fresh world is empty-ish" in result.content
            assert "Atlas ships Friday" not in result.content
        finally:
            await agent.close_memory()
