"""Partial-init failure-cleanup tests for RFC 0024 PR 2 — autonomy.timers.

Split out of ``test_server_persona_wiring_timers.py`` to keep both files
under the 500-line review-friendly cap (PR 2 review (8) / PR 5.1: the
precedence + cadence wiring tests stay in the sibling file; the
failure-handling concern lives here).

Pins the partial-init cleanup contract: a timer that fails to register
after the scheduler is already started must leave neither the caller's
local ``tick_schedulers`` dict nor ``EventDispatcher``'s registry holding
a half-initialised entry, and a cleanup error must not mask the original
diagnostic.
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


class TestPartialInitFailureCleanup:
    async def test_partial_register_failure_stops_scheduler(self):
        """A misconfigured timer in the middle of ``autonomy.timers`` causes
        ``register_timer`` to raise after the scheduler is already started.

        The wiring path must stop the scheduler and leave **both** registries
        — the caller's local ``tick_schedulers`` dict *and*
        ``EventDispatcher._tick_schedulers`` — free of the half-initialised
        entry before propagating the error.  Without the dispatcher
        cleanup, a stopped scheduler stays addressable via
        :meth:`EventDispatcher.dispatch` (see ``dispatch.py`` where
        ``self._tick_schedulers.get(target_id)`` is the only filter); any
        subsequent caller that reuses the dispatcher would route wakes to
        a dead ``EventLoop``.

        Failure mode the cross-field jitter cap surfaces: schema validates
        each timer's fields independently, so an ``interval_seconds: 1.0,
        jitter_max_seconds: 0.5`` combination is schema-valid but rejected
        at the ``register_timer`` API boundary.
        """
        config = {
            **_PERSONA_CONFIG,
            "autonomy": {
                "level": "autonomous",
                "timers": [
                    {
                        "id": "valid_timer",
                        "interval_seconds": 30,
                        "kind": "memory_consolidation",
                    },
                    {
                        # interval=1.0 leaves zero slack for jitter — the
                        # cross-field cap rejects this at API boundary.
                        "id": "bad_jitter",
                        "interval_seconds": 1.0,
                        "kind": "any",
                        "jitter_max_seconds": 0.5,
                    },
                ],
            },
        }
        agent = create_persona_agent(
            agent_id="ember-owl", config=config, llm_client=_make_client(),
        )
        dispatcher = EventDispatcher()
        schedulers: dict = {}

        with pytest.raises(ValueError, match="jitter_max"):
            await initialize_persona_agents(
                {"ember-owl": agent}, dispatcher, schedulers,
            )

        # Scheduler was never published to the caller's local registry —
        # no half-bring-up entry to confuse subsequent dispatches.
        assert "ember-owl" not in schedulers
        # Symmetric guarantee for the dispatcher: a failed init must not
        # leave a stopped scheduler routable.  Queried through the public
        # getter (PR 2 review (7)) rather than the private dict.
        assert not dispatcher.has_tick_scheduler("ember-owl")

        await agent.close_memory()

    async def test_partial_register_failure_preserves_original_exception_when_stop_raises(
        self, monkeypatch, caplog,
    ):
        """When ``scheduler.stop()`` itself raises during partial-init
        cleanup, the original ``register_timer`` ``ValueError`` must still
        propagate (not be masked by the cleanup error) so operators see
        the actionable misconfiguration. The cleanup error is surfaced
        via ``logger.exception`` rather than silently swallowed. Without
        the inner ``try/except``, callers (``pytest.raises``, ops
        dashboards) see "stop failed" instead of "timer X is misconfigured"
        — exactly the diagnostic that pinpoints the YAML line to fix.
        """
        config = {
            **_PERSONA_CONFIG,
            "autonomy": {
                "level": "autonomous",
                "timers": [{
                    "id": "bad_jitter", "interval_seconds": 1.0,
                    "kind": "any", "jitter_max_seconds": 0.5,
                }],
            },
        }

        # Monkeypatch the class method — the test body never calls stop().
        from agents.tick import TickScheduler

        stop_calls = 0

        async def _stop_raises(self, timeout: float = 10.0) -> None:
            nonlocal stop_calls
            stop_calls += 1
            raise RuntimeError("simulated cleanup failure")

        monkeypatch.setattr(TickScheduler, "stop", _stop_raises)

        agent = create_persona_agent(
            agent_id="ember-owl", config=config, llm_client=_make_client(),
        )
        dispatcher = EventDispatcher()
        schedulers: dict = {}

        with caplog.at_level(logging.ERROR, logger="Persatrix.agent.server_persona"):
            with pytest.raises(ValueError, match="jitter_max"):
                await initialize_persona_agents(
                    {"ember-owl": agent}, dispatcher, schedulers,
                )

        assert stop_calls == 1, f"expected stop() called once, got {stop_calls}"

        # Cleanup failure surfaced via logger.exception (ERROR + exc_info).
        cleanup_errors = [
            r for r in caplog.records
            if r.levelname == "ERROR"
            and r.exc_info is not None
            and isinstance(r.exc_info[1], RuntimeError)
            and "simulated cleanup failure" in str(r.exc_info[1])
        ]
        assert cleanup_errors, (
            f"expected the cleanup RuntimeError to be logged via logger.exception; "
            f"records: {[(r.levelname, r.message) for r in caplog.records]}"
        )

        # Partial-init invariants from the sibling test still hold.
        assert "ember-owl" not in schedulers
        assert not dispatcher.has_tick_scheduler("ember-owl")

        await agent.close_memory()
