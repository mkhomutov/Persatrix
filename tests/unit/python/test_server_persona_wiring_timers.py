"""Server persona wiring tests for RFC 0024 PR 2 — autonomy.timers.

Split out from test_server_persona_wiring.py to keep that file under the
500-line review-friendly limit. Pins the four precedence/back-compat
contracts:

- Both ``timers`` and ``tick_interval_seconds`` set → ``timers`` wins, INFO log.
- ``tick_interval_seconds`` only → PR 1 back-compat synthesised legacy timer.
- ``timers`` only → no legacy timer; configured timers register on EventLoop.
- ``timers: []`` (v0.3.3 default) → zero timers; substrate exists but quiet.
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from agents.dispatch import EventDispatcher
from agents.llm_client import LLMClient, LLMResponse
from agents.persona import create_persona_agent
from agents.server_persona import initialize_persona_agents
from agents.tools.registry import clear_registry


@pytest.fixture(autouse=True)
def _clean_registry():
    clear_registry()
    yield
    clear_registry()


def _make_client() -> LLMClient:
    mock_provider = AsyncMock()
    mock_provider.create_message = AsyncMock(
        return_value=LLMResponse(text="ok"),
    )
    mock_provider.format_tool_definitions = MagicMock(return_value=[])
    mock_provider.append_tool_round = MagicMock(
        side_effect=lambda msgs, resp, results: msgs,
    )
    return LLMClient(mock_provider)


_PERSONA_CONFIG: dict = {
    "id": "ember-owl",
    "type": "persona",
    "name": "Ember Owl",
    "role": "Engineering leadership",
    "model": "test-model",
    "temperature": 0.7,
    "max_llm_calls": 10,
    "max_tokens": 4096,
    "persona": {
        "title": "VP of Engineering",
        "background": "15 years.",
        "behavior": {},
    },
    "permissions": {"memory": {"read": True, "write": True}},
    "memory": {"db_path": ":memory:", "notes": {"max_notes": 100}},
}


class TestAutonomyTimersWiring:
    async def test_timers_wins_when_both_set(self, caplog):
        """``timers`` and ``tick_interval_seconds`` both present — ``timers``
        wins and the loader emits one INFO line naming the precedence."""
        config = {
            **_PERSONA_CONFIG,
            "autonomy": {
                "level": "semi-autonomous",
                "tick_interval_seconds": 60,
                "timers": [
                    {
                        "id": "memory_consolidation",
                        "interval_seconds": 30,
                        "kind": "memory_consolidation",
                    },
                ],
            },
        }
        agent = create_persona_agent(
            agent_id="ember-owl", config=config, llm_client=_make_client(),
        )
        dispatcher = EventDispatcher()
        schedulers: dict = {}

        with caplog.at_level(logging.INFO, logger="Persatrix.agent.server_persona"):
            await initialize_persona_agents(
                {"ember-owl": agent}, dispatcher, schedulers,
            )

        scheduler = schedulers["ember-owl"]
        assert scheduler.event_loop.has_timer("memory_consolidation")
        assert not scheduler.event_loop.has_timer("legacy_tick")
        precedence_logs = [
            r for r in caplog.records
            if "autonomy.timers" in r.message
            and "tick_interval_seconds" in r.message
        ]
        assert len(precedence_logs) >= 1

        await scheduler.stop()
        await agent.close_memory()

    async def test_tick_interval_only_synthesises_legacy_timer(self):
        """``timers`` absent → PR 1 back-compat synthesised legacy timer.

        Phase 5 / v0.4.0 emits the deprecation warning on
        ``tick_interval_seconds``; Phase 2 stays silent.
        """
        config = {
            **_PERSONA_CONFIG,
            "autonomy": {
                "level": "semi-autonomous",
                "tick_interval_seconds": 60,
            },
        }
        agent = create_persona_agent(
            agent_id="ember-owl", config=config, llm_client=_make_client(),
        )
        dispatcher = EventDispatcher()
        schedulers: dict = {}
        await initialize_persona_agents(
            {"ember-owl": agent}, dispatcher, schedulers,
        )

        scheduler = schedulers["ember-owl"]
        assert scheduler.event_loop.has_timer("legacy_tick")

        await scheduler.stop()
        await agent.close_memory()

    async def test_timers_only_no_legacy_timer(self):
        """``timers`` set, ``tick_interval_seconds`` absent — only configured
        timers register; no synthesised legacy timer."""
        config = {
            **_PERSONA_CONFIG,
            "autonomy": {
                "level": "autonomous",
                "timers": [
                    {
                        "id": "memory_consolidation",
                        "interval_seconds": 30,
                        "kind": "memory_consolidation",
                    },
                ],
            },
        }
        agent = create_persona_agent(
            agent_id="ember-owl", config=config, llm_client=_make_client(),
        )
        dispatcher = EventDispatcher()
        schedulers: dict = {}
        await initialize_persona_agents(
            {"ember-owl": agent}, dispatcher, schedulers,
        )

        scheduler = schedulers["ember-owl"]
        assert scheduler.event_loop.has_timer("memory_consolidation")
        assert not scheduler.event_loop.has_timer("legacy_tick")

        await scheduler.stop()
        await agent.close_memory()

    async def test_empty_timers_list_no_timers_registered(self):
        """v0.3.3 default — stock personas ship ``timers: []`` so an
        autonomous persona registers *zero* timers. Substrate is alive but
        nothing produces a wake. This is the bored-persona idle-cost
        contract at the timer-registry level."""
        config = {
            **_PERSONA_CONFIG,
            "autonomy": {"level": "autonomous", "timers": []},
        }
        agent = create_persona_agent(
            agent_id="ember-owl", config=config, llm_client=_make_client(),
        )
        dispatcher = EventDispatcher()
        schedulers: dict = {}
        await initialize_persona_agents(
            {"ember-owl": agent}, dispatcher, schedulers,
        )

        scheduler = schedulers["ember-owl"]
        assert not scheduler.event_loop.has_timer("legacy_tick")
        assert scheduler.event_loop.is_running

        await scheduler.stop()
        await agent.close_memory()
