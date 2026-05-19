"""Integration tests for RFC 0019 PR 2 — Span Links.

Covers the two link cases wireable today:

* **Persona event → triggered tick** — when ``EventDispatcher.dispatch()``
  wakes a tick scheduler from inside an active event span, the next
  ``on_tick()`` carries ``Link(link.kind="trigger")`` back to that event.

* **Sub-agent spawn span exists** — the ``SPAWN_SUB_AGENT`` action route
  emits ``agent.subagent.spawn`` with the documented attributes even though
  the actual spawner is deferred to RFC 0009.  The Link from the future
  sub-agent's root span back to this span is verified by RFC 0009.

The third / fourth link cases (channel bridge, mesh A2A) are tracked in
``docs/observability.md § 10.3`` and ship with their owning RFCs.
"""

from __future__ import annotations

import copy
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from agents.dispatch import ActionExecutor, EventDispatcher
from agents.llm_client import LLMClient, LLMResponse, StopReason, Usage
from agents.observability.spans import (
    PERSONA_EVENT_SPAN,
    PERSONA_TICK_SPAN,
    SUBAGENT_SPAWN_SPAN,
)
from agents.persona import create_persona_agent
from agents.persona_runtime import _LLMPersonaAgent
from agents.persona_types import ActionType, AgentAction, AgentEvent, EventType
from agents.tick import TickScheduler

_PERSONA_CONFIG: dict[str, Any] = {
    "id": "linked-agent",
    "type": "persona",
    "name": "Linked Agent",
    "role": "Testing",
    "model": "test-model",
    "temperature": 0.7,
    "max_llm_calls": 10,
    "max_tokens": 4096,
    "persona": {"background": "Test background.", "behavior": {}},
    "permissions": {"memory": {"read": True, "write": True}},
    "memory": {
        "db_path": ":memory:",
        "notes": {"max_notes": 100, "auto_reflect_after": 5},
    },
}


def _make_client(text: str = '[{"action_type": "do_nothing", "payload": {}}]') -> LLMClient:
    provider = AsyncMock()
    provider.create_message = AsyncMock(
        return_value=LLMResponse(
            text=text,
            stop_reason=StopReason.END_TURN,
            usage=Usage(input_tokens=5, output_tokens=5),
        ),
    )
    provider.format_tool_definitions = MagicMock(return_value=[])
    provider.append_tool_round = MagicMock(return_value=[])
    return LLMClient(provider)


async def _make_agent() -> _LLMPersonaAgent:
    agent = create_persona_agent(
        agent_id="linked-agent",
        config=copy.deepcopy(_PERSONA_CONFIG),
        llm_client=_make_client(),
    )
    await agent.initialize_memory()
    return agent


@pytest.fixture
def exporter() -> InMemorySpanExporter:
    """Install a fresh ``InMemorySpanExporter`` on the active provider.

    See the matching fixture in ``agents/tests/test_observability_spans.py``
    for why this avoids ``init_tracing`` (would break the gRPC propagation
    test downstream).
    """
    provider = trace.get_tracer_provider()
    if not isinstance(provider, TracerProvider):
        provider = TracerProvider()
        trace.set_tracer_provider(provider)
    exp = InMemorySpanExporter()
    processor = SimpleSpanProcessor(exp)
    provider.add_span_processor(processor)
    yield exp
    processor.shutdown()


# ─── persona event → tick Link ───────────────────────────────────────────────


class TestEventTriggersTickLink:
    async def test_dispatch_inside_event_span_links_to_next_tick(
        self, exporter: InMemorySpanExporter,
    ) -> None:
        agent = await _make_agent()
        dispatcher = EventDispatcher(agents={"linked-agent": agent})
        scheduler = TickScheduler(agent=agent, interval=60.0)
        dispatcher.register_tick_scheduler("linked-agent", scheduler)

        # Simulate the orchestrator dispatching an event — capture the
        # event span's context so the test can compare it to the Link the
        # next on_tick() emits.
        tracer = trace.get_tracer("test")
        with tracer.start_as_current_span("orchestrator.event") as event_span:
            event_ctx = event_span.get_span_context()
            await dispatcher.dispatch(
                "linked-agent",
                AgentEvent(
                    event_type=EventType.CHANNEL_MESSAGE,
                    payload={"text": "hi"},
                    sender_id="other",
                ),
            )

        # The dispatcher should have queued a Link for the next tick.
        assert len(agent._pending_tick_links) == 1
        queued = agent._pending_tick_links[0]
        assert queued.context.trace_id == event_ctx.trace_id
        assert queued.context.span_id == event_ctx.span_id
        assert queued.attributes is not None
        assert queued.attributes["link.kind"] == "trigger"

        # Now run on_tick() — it must drain the link onto its span.
        exporter.clear()
        await agent.on_tick()

        tick_spans = [
            s for s in exporter.get_finished_spans()
            if s.name == PERSONA_TICK_SPAN
        ]
        assert tick_spans, "agent.persona.tick span missing"
        tick = tick_spans[-1]
        assert len(tick.links) == 1
        assert tick.links[0].context.trace_id == event_ctx.trace_id
        assert tick.links[0].context.span_id == event_ctx.span_id
        assert tick.links[0].attributes["link.kind"] == "trigger"

        # And on_tick() must have drained the queue.
        assert len(agent._pending_tick_links) == 0

    async def test_dispatch_outside_span_queues_no_link(
        self, exporter: InMemorySpanExporter,
    ) -> None:
        """No active span → no Link queued (the link target would be invalid)."""
        agent = await _make_agent()
        dispatcher = EventDispatcher(agents={"linked-agent": agent})
        scheduler = TickScheduler(agent=agent, interval=60.0)
        dispatcher.register_tick_scheduler("linked-agent", scheduler)

        await dispatcher.dispatch(
            "linked-agent",
            AgentEvent(
                event_type=EventType.CHANNEL_MESSAGE,
                payload={"text": "hi"},
                sender_id="other",
            ),
        )

        assert len(agent._pending_tick_links) == 0


# ─── sub-agent spawn span ─────────────────────────────────────────────────────


class TestSubAgentSpawnSpan:
    async def test_spawn_action_emits_span(
        self, exporter: InMemorySpanExporter,
    ) -> None:
        executor = ActionExecutor()
        await executor.execute(
            "parent-agent",
            [AgentAction(
                action_type=ActionType.SPAWN_SUB_AGENT,
                payload={"role": "researcher"},
            )],
        )

        spans = [
            s for s in exporter.get_finished_spans()
            if s.name == SUBAGENT_SPAWN_SPAN
        ]
        assert len(spans) == 1
        attrs = spans[0].attributes
        assert attrs["agent.id"] == "parent-agent"
        assert attrs["subagent.role"] == "researcher"
        assert attrs["subagent.status"] == "not_implemented"


# ─── event span phase events ─────────────────────────────────────────────────


class TestEventSpanPhases:
    async def test_event_span_records_phase_events(
        self, exporter: InMemorySpanExporter,
    ) -> None:
        agent = await _make_agent()
        await agent.on_event(
            AgentEvent(
                event_type=EventType.CHANNEL_MESSAGE,
                payload={"text": "phase-test"},
                sender_id="other",
            ),
        )

        event_spans = [
            s for s in exporter.get_finished_spans()
            if s.name == PERSONA_EVENT_SPAN
        ]
        assert event_spans, "agent.persona.event span missing"
        event_names = [e.name for e in event_spans[-1].events]
        # All four phases recorded as span events, not nested spans.
        assert event_names == ["received", "queued", "handled", "completed"]
