"""RFC 0052 §E — the convener-side convene-wake dispatch branch (v0.3.11
PR 7c-ii-a). TDD-first: pins that a ``ScheduledWake(callback_kind="convene")``
routes to the convene trigger — recover the channel from the timer id, call the
convene client — and NEVER falls through to the ordinary idle/LLM tick path (a
convene wake is a re-open signal, not a heartbeat; running a tick on it would be
both a misfire and unbudgeted spend on an unattended channel).

Ships DARK: nothing registers a ``convene`` timer yet (the ``agents.yaml``
writer is PR 7c-ii-b), so this branch is unreachable in production and is
exercised only here — exactly as PR 7c-i's ``StandingConveneTimers`` producer
shipped exported-but-unconsumed.
"""

from __future__ import annotations

import logging
import types
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

from agents.convene_client import HTTPConveneClient
from agents.event_loop import ScheduledWake
from agents.server_persona_timers import wire_convene_clients
from agents.tick import TickScheduler


def _make_scheduler(
    *, convene_client: object | None, executor: object | None = None
) -> tuple[TickScheduler, MagicMock]:
    """A TickScheduler over a mock agent, so we can drive ``_handle_scheduled_wake``
    directly and assert which path it took. Returns (scheduler, agent)."""
    agent = MagicMock()
    agent.agent_id = "nova-sparrow"
    agent.on_tick = AsyncMock(return_value=[])
    scheduler = TickScheduler(
        agent,
        interval=60.0,
        idle_after_ticks=10,
        executor=executor,
        register_legacy_timer=False,
        convene_client=convene_client,
    )
    return scheduler, agent


def _convene_wake(name: str = "planning") -> ScheduledWake:
    return ScheduledWake(timer_id=f"convene-{name}", callback_kind="convene")


@pytest.mark.asyncio
async def test_convene_wake_triggers_convene_not_tick() -> None:
    client = MagicMock()
    client.convene = AsyncMock()
    scheduler, agent = _make_scheduler(convene_client=client)

    await scheduler._handle_scheduled_wake(_convene_wake("planning"))

    # Recovered the channel from the timer id and triggered the convene...
    client.convene.assert_awaited_once_with("group:planning")
    # ...and did NOT run the ordinary tick (no LLM turn, no idle accounting).
    agent.on_tick.assert_not_called()
    assert scheduler.idle_count == 0


@pytest.mark.asyncio
async def test_tick_wake_still_ticks_and_never_convenes() -> None:
    client = MagicMock()
    client.convene = AsyncMock()
    scheduler, agent = _make_scheduler(convene_client=client)

    await scheduler._handle_scheduled_wake(
        ScheduledWake(timer_id="legacy_tick", callback_kind="tick"),
    )

    # The legacy/idle tick path is byte-for-byte unchanged: on_tick runs, the
    # convene client is never touched.
    agent.on_tick.assert_awaited_once()
    client.convene.assert_not_called()
    assert scheduler.idle_count == 1  # empty action list => idle increment


@pytest.mark.asyncio
async def test_unrecoverable_timer_id_drops_without_convening(
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = MagicMock()
    client.convene = AsyncMock()
    scheduler, agent = _make_scheduler(convene_client=client)

    # A ``convene``-kind wake whose id decodes to no channel (empty name). The
    # authoritative signal is the callback_kind, but the id is the only channel
    # reference — an unrecoverable one is dropped, never mis-dispatched.
    with caplog.at_level(logging.WARNING, logger="agents.tick"):
        await scheduler._handle_scheduled_wake(
            ScheduledWake(timer_id="convene-", callback_kind="convene"),
        )

    client.convene.assert_not_called()
    agent.on_tick.assert_not_called()
    assert any("convene-" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_convene_wake_without_client_does_not_crash(
    caplog: pytest.LogCaptureFixture,
) -> None:
    scheduler, agent = _make_scheduler(convene_client=None)

    with caplog.at_level(logging.WARNING, logger="agents.tick"):
        await scheduler._handle_scheduled_wake(_convene_wake("planning"))

    # No client wired (a scheduler built before the post-session injection):
    # log and drop, never fall through to a tick.
    agent.on_tick.assert_not_called()
    assert any("group:planning" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_declined_convene_is_logged_and_dropped(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # A convening declined at a §E bound (429) / conflict (409) / unreachable
    # (503) surfaces as ClientResponseError — the handler must swallow it so the
    # event loop survives to the next scheduled fire, not propagate and wedge it.
    client = MagicMock()
    client.convene = AsyncMock(
        side_effect=aiohttp.ClientResponseError(
            request_info=types.SimpleNamespace(
                real_url="u", url="u", method="POST", headers={},
            ),
            history=(),
            status=429,
        ),
    )
    scheduler, agent = _make_scheduler(convene_client=client)

    with caplog.at_level(logging.WARNING, logger="agents.tick"):
        # Must NOT raise.
        await scheduler._handle_scheduled_wake(_convene_wake("planning"))

    client.convene.assert_awaited_once_with("group:planning")
    agent.on_tick.assert_not_called()
    assert any(
        "group:planning" in r.getMessage() for r in caplog.records
    ), "the declined convening is logged for the operator"


def test_wire_convene_clients_injects_into_every_scheduler() -> None:
    # The post-session injection (server.py) is what makes the dark handler
    # reachable once PR 7c-ii-b registers a convene timer — without it a fired
    # wake would log-and-drop on a client-less scheduler. Pin that every started
    # scheduler receives an HTTPConveneClient built on the shared session.
    schedulers: dict[str, TickScheduler] = {}
    for name in ("nova-sparrow", "iron-fox"):
        sched, _ = _make_scheduler(convene_client=None)
        assert sched._convene_client is None  # pre-wire baseline
        schedulers[name] = sched

    wire_convene_clients(
        schedulers, session=MagicMock(), orchestrator_url="http://orch:8080",
    )

    for sched in schedulers.values():
        assert isinstance(sched._convene_client, HTTPConveneClient)
