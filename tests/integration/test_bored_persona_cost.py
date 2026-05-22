"""RFC 0024 Phase 4 — the "bored persona" cost-regression gate.

*The* v0.3.3 "Idle Truly Idle" acceptance ([RFC 0024 §Test Strategy](
../../docs/rfcs/0024-event-driven-scheduling.md#test-strategy),
[v0.3.3-plan acceptance row 1](../../docs/v0.3.3-plan.md#acceptance-for-v033)).

A persona with **no scheduled timers** (``autonomy.timers: []`` →
``register_legacy_timer=False``) and **no inbound traffic** must cost
nothing: its :class:`~agents.event_loop.EventLoop` supervisor parks on
``queue.get()`` and the agent is never invoked. Concretely, across an
observation window:

* zero LLM provider calls (``provider.create_message``),
* zero ``_inject_memory_context`` invocations (no SQLite recall query),
* zero wallet lease requests at the RFC 0023 ``WalletService`` boundary
  (``WalletClient.lease``),
* every ``agent.wake.{inbound,scheduled,salience,dropped}`` counter reads
  zero — nothing was ever enqueued.

The window is a short **wall-clock** smoke pass: an event-driven loop with
no timers has nothing for a fake clock to advance, so the meaningful check
is simply that real elapsed time produces no activity. The pre-v0.3.3
polling ``TickScheduler`` would have fired at least one ``on_tick`` (and
thus a recall query + LLM call + lease) inside this same window — that is
the regression class this gate defends against, structurally, for every
file in the ``cost-regression-gate`` CI trigger set.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from agents.dispatch import EventDispatcher
from agents.llm_client import LLMClient
from agents.observability import metrics as pmetrics
from agents.persona import create_persona_agent
from agents.tick import TickScheduler
from agents.tools.registry import clear_registry
from agents.wallet_client import WalletClient

# Wall-clock observation window. Long enough that a regression to a
# polling loop (sub-second to a few-second cadence) would fire at least
# one tick; short enough to keep the gate fast. The legacy default tick
# interval was 60s, but a *misconfigured* fast cadence is exactly the
# leak class this gate guards, so the window need only exceed a plausibly
# short poll interval, not the full 60s.
_OBSERVE_SECONDS = 1.0


@pytest.fixture(autouse=True)
def _clean_registry() -> Iterator[None]:
    clear_registry()
    yield
    clear_registry()


@pytest.fixture
def metric_reader() -> Iterator[InMemoryMetricReader]:
    reader = InMemoryMetricReader()
    pmetrics.init_metrics(reader=reader)
    try:
        yield reader
    finally:
        asyncio.run(pmetrics.shutdown())


_PERSONA_CONFIG: dict[str, Any] = {
    "id": "bored-persona",
    "type": "persona",
    "name": "Bored Persona",
    "role": "Integration-test persona that should cost nothing when idle",
    "model": "claude-sonnet-4-6",
    "temperature": 0.3,
    "max_llm_calls": 3,
    "max_tokens": 128,
    "persona": {
        "background": "Test fixture.",
        "behavior": {"directness": "balanced"},
    },
    "permissions": {"memory": {"read": True, "write": True}},
    "memory": {"db_path": ":memory:"},
}


def _wake_counter_total(reader: InMemoryMetricReader, name: str) -> int:
    """Sum all data-point values recorded for the counter ``name``."""
    data = reader.get_metrics_data()
    if data is None:
        return 0
    total = 0
    for rm in data.resource_metrics:
        for sm in rm.scope_metrics:
            for m in sm.metrics:
                if m.name != name:
                    continue
                for dp in m.data.data_points:  # type: ignore[union-attr]
                    total += int(getattr(dp, "value", 0))
    return total


async def test_bored_persona_costs_nothing(
    metric_reader: InMemoryMetricReader,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = AsyncMock()
    provider.name = "anthropic"
    # A single provider call here would be a regression: side_effect raises
    # if the idle loop ever reaches the LLM boundary.
    provider.create_message = AsyncMock(
        side_effect=AssertionError("idle persona must not call the LLM provider"),
    )
    provider.format_tool_definitions = MagicMock(return_value=[])
    provider.append_tool_round = MagicMock(return_value=[])

    # Wallet boundary: a strict mock so any ``lease(...)`` is observable.
    wallet = MagicMock(spec=WalletClient)

    llm_client = LLMClient(provider, wallet=wallet)
    agent = create_persona_agent(
        agent_id="bored-persona",
        config=_PERSONA_CONFIG,
        llm_client=llm_client,
    )
    await agent.initialize_memory()

    # Spy the cost entry points with ``wraps`` so a stray invocation still
    # runs the real path (and is counted) rather than being silently
    # stubbed out. The idle loop must touch none of them.
    on_event_spy = AsyncMock(wraps=agent.on_event)
    on_tick_spy = AsyncMock(wraps=agent.on_tick)
    inject_spy = AsyncMock(wraps=agent._inject_memory_context)
    monkeypatch.setattr(agent, "on_event", on_event_spy)
    monkeypatch.setattr(agent, "on_tick", on_tick_spy)
    monkeypatch.setattr(agent, "_inject_memory_context", inject_spy)

    dispatcher = EventDispatcher(agents={"bored-persona": agent})
    # ``autonomy.timers: []`` → no legacy back-compat timer, no timers at
    # all. The supervisor starts and immediately parks on ``queue.get()``.
    scheduler = TickScheduler(
        agent,
        executor=dispatcher.executor,
        register_legacy_timer=False,
    )

    scheduler.start()
    try:
        # Structural root cause of "idle truly idle": there is no polling
        # timer to fire. (The legacy synthesised tick is suppressed and no
        # ``autonomy.timers`` were registered.)
        assert scheduler.is_running, "the loop must be alive — idle, not dead"
        assert scheduler.event_loop.has_timer("legacy_tick") is False

        # Observe real elapsed time. A polling regression would tick here.
        await asyncio.sleep(_OBSERVE_SECONDS)
    finally:
        await scheduler.stop(timeout=1.0)

    # ── The agent was never invoked → no cost was incurred. ──
    assert on_tick_spy.await_count == 0, "idle persona must not run on_tick"
    assert on_event_spy.await_count == 0, "idle persona must not run on_event"
    assert inject_spy.await_count == 0, (
        "idle persona must not run a memory-recall query "
        "(_inject_memory_context)"
    )
    assert provider.create_message.await_count == 0, (
        "idle persona must not reach the LLM provider"
    )
    assert wallet.lease.call_count == 0, (
        "idle persona must not request a wallet lease "
        "(zero LeaseRequest at the WalletService boundary)"
    )

    # ── No wake of any kind was enqueued. ──
    for counter in (
        "agent.wake.inbound",
        "agent.wake.scheduled",
        "agent.wake.salience",
        "agent.wake.dropped",
    ):
        assert _wake_counter_total(metric_reader, counter) == 0, (
            f"idle persona recorded {counter} > 0 — something woke the loop"
        )
