"""Server persona wiring tests for RFC 0024 PR 2 — autonomy.timers.

Split out from test_server_persona_wiring.py to keep that file under the
500-line review-friendly limit. Pins the precedence/back-compat
contracts:

- Both ``timers`` and ``tick_interval_seconds`` set → ``timers`` wins, INFO log.
- ``tick_interval_seconds`` only → PR 1 back-compat synthesised legacy timer.
- ``timers`` only → no legacy timer; configured timers register on EventLoop.
- ``timers: []`` (v0.3.3 default) → zero timers; substrate exists but quiet.
- ``timers`` set → "Started" INFO log names ``timers=N`` instead of the
  dead legacy ``interval=60s``; legacy path keeps the interval text.
- ``timers`` set → COST cadence WARNING enumerates configured timers
  instead of the dead legacy ``tick_interval=60s`` value.

Partial-init failure-cleanup tests live in the sibling file
``test_server_persona_wiring_timers_failures.py`` — split (PR 5.1) to keep
both files under the 500-line review-friendly cap.
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

    async def test_empty_timers_with_tick_interval_still_suppresses_legacy(
        self, caplog,
    ):
        """``timers: []`` *with* ``tick_interval_seconds`` set — the empty
        list still wins.

        The precedence gate keys on ``timers is not None``, not truthiness
        (see ``server_persona.py``: ``register_legacy_timer=timers is None``
        and the ``timers is not None and "tick_interval_seconds" in autonomy``
        INFO branch). So an operator who writes ``timers: []`` alongside a
        leftover ``tick_interval_seconds`` gets *zero* wakes, not the
        synthesised legacy timer — and the precedence INFO line still fires.

        Regression backstop for PR 2 review (8): a future refactor flipping
        the gate to ``if timers:`` would silently restore the legacy
        fallback for the empty-list case (the operator would start paying
        the dead ``tick_interval`` cadence again). This test fails the
        moment that happens.
        """
        config = {
            **_PERSONA_CONFIG,
            "autonomy": {
                "level": "semi-autonomous",
                "tick_interval_seconds": 60,
                "timers": [],
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
        # Empty list suppressed the back-compat legacy timer ...
        assert not scheduler.event_loop.has_timer("legacy_tick")
        assert scheduler.event_loop.is_running
        # ... and the precedence INFO line still names the override.
        precedence_logs = [
            r for r in caplog.records
            if "autonomy.timers" in r.message
            and "tick_interval_seconds" in r.message
        ]
        assert len(precedence_logs) >= 1

        await scheduler.stop()
        await agent.close_memory()

    async def test_cost_warning_names_timers_when_timers_set(self, caplog):
        """The COST WARNING must not advertise the dead
        ``tick_interval=60s`` when ``timers`` is configured.

        Why: the COST warning is the loudest operator-facing signal
        about what the persona will spend on LLM tokens — it is
        emitted at the exact moment autonomous spend can begin (per
        the inline comment on the surrounding block).  Reporting a
        legacy ``tick_interval`` value that the runtime ignores
        actively misleads cost reasoning ("but I set the interval to
        60!").  The fix mirrors the "Started" log: when ``timers``
        is set, the warning enumerates the configured timers'
        ``interval_seconds`` so operators see the real cadence(s).
        Defense-in-depth alongside [test_started_log_names_timers_when_timers_set].
        """
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
                    {
                        "id": "reflection",
                        "interval_seconds": 300,
                        "kind": "reflection",
                    },
                ],
            },
        }
        agent = create_persona_agent(
            agent_id="ember-owl", config=config, llm_client=_make_client(),
        )
        dispatcher = EventDispatcher()
        schedulers: dict = {}

        with caplog.at_level(logging.WARNING, logger="Persatrix.agent.server_persona"):
            await initialize_persona_agents(
                {"ember-owl": agent}, dispatcher, schedulers,
            )

        cost_cadence_logs = [
            r for r in caplog.records
            if r.levelname == "WARNING"
            and "COST:" in r.message
            and ("tick_interval" in r.message or "timers=" in r.message)
        ]
        assert len(cost_cadence_logs) == 1, (
            "expected exactly one COST cadence warning, got "
            f"{[r.message for r in cost_cadence_logs]}"
        )
        msg = cost_cadence_logs[0].getMessage()
        # The new branch reports the actual configured timer intervals …
        assert "timers=" in msg, (
            f"expected 'timers=' enumeration in COST warning when "
            f"timers is set; got {msg!r}"
        )
        # … and does not advertise the dead legacy interval.
        assert "tick_interval=" not in msg, (
            f"COST warning must not name the dead legacy interval "
            f"when timers is set; got {msg!r}"
        )

        await schedulers["ember-owl"].stop()
        await agent.close_memory()

    async def test_started_log_names_timers_when_timers_set(self, caplog):
        """The "Started tick scheduler" INFO log must NOT advertise the
        dead ``interval=60s`` value when ``timers`` is configured.

        Why: ``interval`` is the legacy ``tick_interval_seconds`` (default
        60) and is only meaningful when ``register_legacy_timer=True``.
        When ``timers`` is set the synthesised legacy timer is suppressed,
        so logging "interval=60s" on startup misleads an operator into
        believing the persona ticks every 60s — the actual cadence is
        whatever the configured timers carry.  This test pins the
        branch: ``timers`` set → log names the timer count; legacy path
        → log keeps the interval text.
        """
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
                    {
                        "id": "reflection",
                        "interval_seconds": 300,
                        "kind": "reflection",
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

        started_logs = [
            r for r in caplog.records if "Started tick scheduler" in r.message
        ]
        assert len(started_logs) == 1, (
            f"expected exactly one 'Started tick scheduler' log, "
            f"got {len(started_logs)}: {[r.message for r in started_logs]}"
        )
        msg = started_logs[0].getMessage()
        # The new branch enumerates the configured timers (id@interval) …
        assert "timers=[" in msg, (
            f"expected 'timers=[…]' enumeration in started-log for "
            f"timers-set branch, got {msg!r}"
        )
        assert "memory_consolidation@30s" in msg
        assert "reflection@300s" in msg
        # … and does not mislead the operator with the dead legacy
        # ``tick_interval`` token (which would carry the unused default 60).
        assert "tick_interval=" not in msg, (
            f"started-log must not name the dead legacy interval when "
            f"timers is set; got {msg!r}"
        )

        await schedulers["ember-owl"].stop()
        await agent.close_memory()

    async def test_started_log_names_interval_for_legacy_path(self, caplog):
        """The legacy ``tick_interval_seconds``-only path still logs the
        interval text — preserves PR 1 back-compat operator UX so no
        log scraping breaks for personas that haven't migrated to
        ``autonomy.timers`` yet."""
        config = {
            **_PERSONA_CONFIG,
            "autonomy": {
                "level": "semi-autonomous",
                "tick_interval_seconds": 45,
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

        started_logs = [
            r for r in caplog.records if "Started tick scheduler" in r.message
        ]
        assert len(started_logs) == 1
        msg = started_logs[0].getMessage()
        assert "tick_interval=45s" in msg, (
            f"legacy path must still report the configured interval; "
            f"got {msg!r}"
        )

        await schedulers["ember-owl"].stop()
        await agent.close_memory()
