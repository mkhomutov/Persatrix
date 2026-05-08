"""RFC 0020 PR 6 slice 4 — single-turn parity follow-ups (PR-2 review #9 + #10).

Sibling of :mod:`test_interaction_single_turn_parity` covering the
slice-4 review residuals that the original PR-2 parity suite did not
exercise:

* **PR-2 review #9** — telemetry probe at the runtime call site.  PR 1
  wired ``agent.interactions.opened`` / ``.closed`` /
  ``.closed.by_structural`` inside :class:`InteractionTracker`; PR 2
  made ``_store_event_episode`` the first runtime caller.  Without a
  probe at this layer a regression that drops the close call (or
  routes single-turn events through the legacy path) would silently
  zero the counters in production.
* **PR-2 review #10** — parametrised matrix over every member of
  ``_SINGLE_TURN_EVENT_TYPES`` plus an explicit unknown-event fallback
  test that pins the warning + legacy-shape contract.

Split off from the parity suite so each module stays under the 500-line
file-size cap (``scripts/checks/file_size.py --strict``).
"""

from __future__ import annotations

import logging

import pytest

from agents.persona_types import AgentEvent, EventType
from agents.tools.registry import clear_registry

from ._persona_parity_helpers import all_episodes, counter_total, make_agent


@pytest.fixture(autouse=True)
def _clean_registry():
    clear_registry()
    yield
    clear_registry()


@pytest.mark.asyncio
class TestSingleTurnParityFollowups:
    """PR-2 review #9 + #10 follow-ups — extends ``TestSingleTurnParity``."""

    @pytest.mark.parametrize(
        "event_type",
        [
            EventType.TASK_ASSIGNED,
            EventType.SUB_AGENT_COMPLETED,
            EventType.APPROVAL_REQUESTED,
            EventType.APPROVAL_RESPONSE,
            EventType.AGENT_JOINED,
            EventType.AGENT_LEFT,
        ],
    )
    async def test_all_single_turn_event_types_route_through_tracker(
        self, event_type: EventType,
    ):
        """PR-2 review #10 — every single-turn ``EventType`` member routes.

        The original parity suite exercised only ``TICK`` + ``TASK_ASSIGNED``;
        a future ``EventType`` admitted to ``_SINGLE_TURN_EVENT_TYPES``
        without a routing test could regress the scope-labelling contract
        (``scope == event_type.value``, ``turn_count == 1``) silently.
        Parametrise over the full set so the contract is enforced for
        every member.  ``TICK`` is exercised by
        ``TestSingleTurnParity.test_n_ticks_produce_n_closed_episodes``
        (it has the empty-context short-circuit and needs the
        ``recent_context`` priming).
        """
        agent = await make_agent()
        await agent.on_event(AgentEvent(
            event_type=event_type,
            payload={"task": f"parity test for {event_type.value}"},
        ))

        episodes = await all_episodes(agent)
        assert len(episodes) == 1
        ep = episodes[0]
        assert ep["interaction_id"], (
            f"{event_type.value} must route through InteractionTracker "
            "(RFC 0020 §G single-turn allowlist)"
        )
        assert ep["turn_count"] == 1
        assert ep["scope"] == event_type.value
        assert ep["closed_at"] is not None
        assert ep["summary"].startswith(f"Event: {event_type.value} → Actions:")

    async def test_unknown_event_type_falls_back_to_legacy_shape(
        self, caplog, monkeypatch,
    ):
        """PR-2 review #10 — unrecognised event types land legacy-shaped + warn.

        ``_store_event_episode`` carries a defensive branch for an
        ``EventType`` that is in neither ``_MULTI_TURN_EVENT_TYPES`` nor
        ``_SINGLE_TURN_EVENT_TYPES``: persist the row in the legacy
        NULL-interaction shape and log a warning so the routing-table
        gap is visible in tests / logs.  The branch was previously
        uncovered.  Empty both classification frozensets on the agent
        instance so any event lands in the fallback, then assert:

        1. The episode is persisted with NULL interaction columns.
        2. A warning is emitted naming the event type and pointing at
           the ``_{MULTI,SINGLE}_TURN_EVENT_TYPES`` constants.
        """
        agent = await make_agent()
        # Empty both routing tables on the instance; any event type
        # now falls through to the legacy-shape branch.
        monkeypatch.setattr(
            agent, "_MULTI_TURN_EVENT_TYPES", frozenset(),
        )
        monkeypatch.setattr(
            agent, "_SINGLE_TURN_EVENT_TYPES", frozenset(),
        )

        with caplog.at_level(
            logging.WARNING,
            logger="agents.persona_runtime.episode_routing",
        ):
            await agent.on_event(AgentEvent(
                event_type=EventType.TASK_ASSIGNED,
                payload={"task": "noop"},
            ))

        episodes = await all_episodes(agent)
        assert len(episodes) == 1
        ep = episodes[0]
        # Legacy NULL-interaction shape — no interaction_id, no scope.
        assert ep["interaction_id"] is None
        assert ep["scope"] is None
        assert ep["turn_count"] is None

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert any(
            "task_assigned" in r.getMessage()
            and "_TURN_EVENT_TYPES" in r.getMessage()
            for r in warnings
        ), (
            "warning must name the unrecognised event type and point to "
            "the routing-table constants so the gap is fixable"
        )

    async def test_telemetry_counters_increment_on_single_turn_event(self):
        """PR-2 review #9 — pin the counter contract at the runtime site.

        PR 1 wires ``agent.interactions.opened`` and
        ``agent.interactions.closed.by_structural`` inside
        :class:`InteractionTracker`; PR 2 makes ``_store_event_episode``
        the first runtime caller.  Without a probe at this layer a
        regression that drops the close call (or routes single-turn
        events through the legacy path) would silently zero the
        counters in production.  Use :class:`InMemoryMetricReader` so
        the assertion is decoupled from the OTLP exporter.
        """
        from opentelemetry.sdk.metrics.export import InMemoryMetricReader

        from agents.observability import metrics as metrics_mod

        # Snapshot the module globals so the probe leaves no residue.
        saved_provider = metrics_mod._provider
        saved_instruments = metrics_mod._instruments
        metrics_mod._provider = None
        metrics_mod._instruments = None
        try:
            reader = InMemoryMetricReader()
            metrics_mod.init_metrics(reader=reader)
            agent = await make_agent()
            await agent.on_event(AgentEvent(
                event_type=EventType.TASK_ASSIGNED,
                payload={"task": "telemetry parity"},
            ))
            opened = counter_total(reader, "agent.interactions.opened")
            closed = counter_total(reader, "agent.interactions.closed")
            by_structural = counter_total(
                reader, "agent.interactions.closed.by_structural",
            )
            assert opened == 1
            assert closed == 1
            assert by_structural == 1
        finally:
            if metrics_mod._provider is not None:
                await metrics_mod.shutdown()
            metrics_mod._provider = saved_provider
            metrics_mod._instruments = saved_instruments
