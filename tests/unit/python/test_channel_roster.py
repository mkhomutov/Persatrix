"""Channel-roster module (v0.3.7 conversation test-findings PR plan, F-4,
slice A — pure fetch + render, no runtime wiring).

F-4: in a group channel with no shared world-state, personas confabulate
who is present and what each other does ("what project do you work on
together?" drew three different answers; "do you know each other?"
diverged). The fix is a **channel roster** injected into the per-event
context: the channel description plus a list of members with their names
and roles, sourced from the orchestrator (`GET /api/v1/channels/{id}` for
membership + `GET /api/v1/agents` for the id→name/role directory, one call
each — no N+1).

This slice (A) lands the pure, self-contained building blocks with full
unit coverage and **no wiring** into `_inject_memory_context` (zero
behaviour change): the `build_roster` join, the `render_roster_section`
renderer, and the `HttpChannelRosterFetcher`. Slice B wires the section
into the budgeted injection path (and the `memory_context` refactor that
needs).
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import MagicMock

import aiohttp
from aiohttp import web

from agents.memory.working import WorkingMemory
from agents.persona_runtime.channel_roster import (
    ROSTER_SECTION_NAME,
    ROSTER_SECTION_PRIORITY,
    HttpChannelRosterFetcher,
    RosterMember,
    build_roster,
    inject_channel_roster,
    render_roster_section,
)

_AGENTS = [
    {"id": "ember-owl", "name": "Ember Owl", "role": "Engineering leadership"},
    {"id": "iron-fox", "name": "Iron Fox", "role": "Staff engineering"},
    {"id": "nova-sparrow", "name": "Nova Sparrow", "role": "Product management"},
]
_CHANNEL = {
    "id": "group:planning",
    "name": "planning",
    "description": "engineering + product planning discussion",
    "members": [
        {"id": "ember-owl", "respond": "when_mentioned"},
        {"id": "iron-fox", "respond": "always"},
        {"id": "nova-sparrow", "respond": "always"},
    ],
}


# ─── build_roster (pure) ──────────────────────────────────────


class TestBuildRoster:
    def test_joins_members_with_name_and_role_in_order(self) -> None:
        roster = build_roster(_CHANNEL, _AGENTS, self_agent_id="iron-fox")
        assert [m.id for m in roster] == ["ember-owl", "iron-fox", "nova-sparrow"]
        assert roster[0] == RosterMember(
            id="ember-owl", name="Ember Owl",
            role="Engineering leadership", is_self=False,
        )
        # The viewing persona is marked.
        assert roster[1].is_self is True
        assert all(not m.is_self for m in (roster[0], roster[2]))

    def test_unknown_member_falls_back_to_id(self) -> None:
        ch = {**_CHANNEL, "members": [{"id": "ghost"}]}
        roster = build_roster(ch, _AGENTS, self_agent_id="iron-fox")
        assert roster == [
            RosterMember(id="ghost", name="ghost", role="", is_self=False),
        ]

    def test_malformed_members_are_skipped(self) -> None:
        ch = {**_CHANNEL, "members": ["not-a-dict", {}, {"id": "iron-fox"}]}
        roster = build_roster(ch, _AGENTS, self_agent_id="iron-fox")
        assert [m.id for m in roster] == ["iron-fox"]

    def test_empty_membership_is_empty(self) -> None:
        assert build_roster({"members": []}, _AGENTS, self_agent_id="x") == []


# ─── render_roster_section (pure) ─────────────────────────────


class TestRenderRosterSection:
    def test_renders_channel_brief_and_members(self) -> None:
        roster = build_roster(_CHANNEL, _AGENTS, self_agent_id="iron-fox")
        section = render_roster_section(_CHANNEL, roster)
        assert section is not None
        assert section.name == ROSTER_SECTION_NAME
        assert section.priority == ROSTER_SECTION_PRIORITY
        assert section.token_count > 0
        body = section.content
        assert "planning" in body
        assert "engineering + product planning discussion" in body
        assert "Ember Owl" in body and "Engineering leadership" in body
        # The viewing persona is flagged so it does not talk about itself
        # in the third person.
        assert "(you)" in body
        # The "(you)" marker is on Iron Fox's line, not Ember Owl's.
        iron_line = next(ln for ln in body.splitlines() if "Iron Fox" in ln)
        assert "(you)" in iron_line

    def test_section_is_non_compressible(self) -> None:
        # The roster is a structured membership list, not recalled prose:
        # summarizing it under budget pressure could drop members or mangle
        # roles, reintroducing the very F-4 confabulation it exists to
        # prevent. Pin it non-compressible so the summarizer never touches it
        # (the lower conversation/history tiers absorb budget pressure first).
        roster = build_roster(_CHANNEL, _AGENTS, self_agent_id="iron-fox")
        section = render_roster_section(_CHANNEL, roster)
        assert section is not None
        assert section.compressible is False

    def test_member_without_role_omits_the_dash(self) -> None:
        roster = [RosterMember(id="ghost", name="ghost", role="", is_self=False)]
        section = render_roster_section(_CHANNEL, roster)
        assert section is not None
        ghost_line = next(
            ln for ln in section.content.splitlines() if "ghost" in ln
        )
        assert "—" not in ghost_line

    def test_empty_members_returns_none(self) -> None:
        assert render_roster_section(_CHANNEL, []) is None


# ─── HttpChannelRosterFetcher (loopback server) ───────────────


_SENTINEL = object()


@asynccontextmanager
async def _serve(*, channel_status: int = 200,
                 agents_status: int = 200,
                 channel_body: Any = _SENTINEL,
                 agents_body: Any = _SENTINEL) -> AsyncIterator[str]:
    async def get_channel(request: web.Request) -> web.Response:
        if channel_status != 200:
            return web.json_response({"error": "x"}, status=channel_status)
        body = _CHANNEL if channel_body is _SENTINEL else channel_body
        return web.json_response(body)

    async def list_agents(request: web.Request) -> web.Response:
        if agents_status != 200:
            return web.json_response({"error": "x"}, status=agents_status)
        body = _AGENTS if agents_body is _SENTINEL else agents_body
        return web.json_response(body)

    app = web.Application()
    app.router.add_get("/api/v1/channels/{id}", get_channel)
    app.router.add_get("/api/v1/agents", list_agents)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        await runner.cleanup()


class TestHttpChannelRosterFetcher:
    async def test_fetch_returns_channel_meta_and_agents(self) -> None:
        async with _serve() as base, aiohttp.ClientSession() as session:
            fetcher = HttpChannelRosterFetcher(
                session=session, orchestrator_url=base,
            )
            result = await fetcher.fetch("group:planning")
        assert result is not None
        channel_meta, agents = result
        assert channel_meta["name"] == "planning"
        assert {a["id"] for a in agents} == {
            "ember-owl", "iron-fox", "nova-sparrow",
        }

    async def test_channel_error_returns_none(self) -> None:
        async with _serve(channel_status=404) as base, \
                aiohttp.ClientSession() as session:
            fetcher = HttpChannelRosterFetcher(
                session=session, orchestrator_url=base,
            )
            assert await fetcher.fetch("group:planning") is None

    async def test_agents_error_returns_none(self) -> None:
        async with _serve(agents_status=500) as base, \
                aiohttp.ClientSession() as session:
            fetcher = HttpChannelRosterFetcher(
                session=session, orchestrator_url=base,
            )
            assert await fetcher.fetch("group:planning") is None

    async def test_transport_failure_returns_none(self) -> None:
        # No server listening: the GET raises (connection refused) and the
        # fetcher swallows it across the seam rather than propagating. Bind a
        # server only to reserve a free port, then tear it down so the address
        # is dead but deterministic (no flaky guessed port).
        async with _serve() as base:
            dead_base = base
        async with aiohttp.ClientSession() as session:
            fetcher = HttpChannelRosterFetcher(
                session=session, orchestrator_url=dead_base,
            )
            assert await fetcher.fetch("group:planning") is None

    async def test_non_dict_channel_body_returns_none(self) -> None:
        # 200 OK but the channel payload is the wrong shape (a list, not the
        # expected object): the isinstance(channel_meta, dict) guard rejects it.
        async with _serve(channel_body=["not", "a", "dict"]) as base, \
                aiohttp.ClientSession() as session:
            fetcher = HttpChannelRosterFetcher(
                session=session, orchestrator_url=base,
            )
            assert await fetcher.fetch("group:planning") is None

    async def test_non_list_agents_body_returns_none(self) -> None:
        # 200 OK but the agents payload is the wrong shape (an object, not the
        # expected directory list): the isinstance(agents, list) guard rejects.
        async with _serve(agents_body={"not": "a list"}) as base, \
                aiohttp.ClientSession() as session:
            fetcher = HttpChannelRosterFetcher(
                session=session, orchestrator_url=base,
            )
            assert await fetcher.fetch("group:planning") is None


# ─── inject_channel_roster (injection wiring) ─────────────────


def _event(channel_id: str) -> MagicMock:
    """A stand-in AgentEvent — the helper only reads ``channel_id``."""
    event = MagicMock()
    event.channel_id = channel_id
    return event


class _FakeFetcher:
    def __init__(self, result: object) -> None:
        self._result = result

    async def fetch(self, channel_id: str):  # noqa: ANN201
        return self._result


class _RaisingFetcher:
    """A fetcher whose ``fetch`` raises — exercises the non-fatal
    ``except Exception`` branch (distinct from a clean ``None`` return)."""

    async def fetch(self, channel_id: str):  # noqa: ANN201
        raise RuntimeError("orchestrator unreachable")


class TestInjectChannelRoster:
    async def test_group_event_injects_roster(self) -> None:
        wm = WorkingMemory(max_tokens=8192)
        fetcher = _FakeFetcher((_CHANNEL, _AGENTS))
        await inject_channel_roster(
            wm, fetcher, _event("group:planning"), "iron-fox",
        )
        section = wm.get_section(ROSTER_SECTION_NAME)
        assert section is not None
        assert "Ember Owl" in section.content
        # The viewing persona (iron-fox) is flagged.
        iron_line = next(
            ln for ln in section.content.splitlines() if "Iron Fox" in ln
        )
        assert "(you)" in iron_line

    async def test_dm_event_injects_no_roster(self) -> None:
        wm = WorkingMemory(max_tokens=8192)
        fetcher = _FakeFetcher((_CHANNEL, _AGENTS))
        await inject_channel_roster(
            wm, fetcher, _event("dm:local:iron-fox"), "iron-fox",
        )
        assert wm.get_section(ROSTER_SECTION_NAME) is None

    async def test_no_fetcher_injects_no_roster(self) -> None:
        wm = WorkingMemory(max_tokens=8192)
        await inject_channel_roster(
            wm, None, _event("group:planning"), "iron-fox",
        )
        assert wm.get_section(ROSTER_SECTION_NAME) is None

    async def test_fetch_failure_injects_no_roster(self) -> None:
        wm = WorkingMemory(max_tokens=8192)
        await inject_channel_roster(
            wm, _FakeFetcher(None), _event("group:planning"), "iron-fox",
        )
        assert wm.get_section(ROSTER_SECTION_NAME) is None

    async def test_stale_roster_cleared_on_a_later_dm_turn(self) -> None:
        wm = WorkingMemory(max_tokens=8192)
        fetcher = _FakeFetcher((_CHANNEL, _AGENTS))
        await inject_channel_roster(
            wm, fetcher, _event("group:planning"), "iron-fox",
        )
        assert wm.get_section(ROSTER_SECTION_NAME) is not None
        # A subsequent DM turn must not carry the prior group's roster.
        await inject_channel_roster(
            wm, fetcher, _event("dm:local:iron-fox"), "iron-fox",
        )
        assert wm.get_section(ROSTER_SECTION_NAME) is None

    async def test_fetch_raising_is_non_fatal_and_warns(
        self, caplog: Any,
    ) -> None:
        """A fetcher whose ``fetch`` *raises* (not just returns ``None``) is
        swallowed: no roster section, and the failure is logged at WARNING
        so operators can see it. Covers the ``except Exception`` branch the
        ``None``-return case does not exercise."""
        wm = WorkingMemory(max_tokens=8192)
        with caplog.at_level(logging.WARNING):
            await inject_channel_roster(
                wm, _RaisingFetcher(), _event("group:planning"), "iron-fox",
            )
        assert wm.get_section(ROSTER_SECTION_NAME) is None
        assert any(
            "roster injection failed" in r.getMessage() for r in caplog.records
        )

    async def test_stale_roster_cleared_even_when_refresh_raises(self) -> None:
        """The stale-section clear happens before the fetch, so a later group
        turn whose refresh raises must not leave the prior roster lingering."""
        wm = WorkingMemory(max_tokens=8192)
        await inject_channel_roster(
            wm, _FakeFetcher((_CHANNEL, _AGENTS)),
            _event("group:planning"), "iron-fox",
        )
        assert wm.get_section(ROSTER_SECTION_NAME) is not None
        await inject_channel_roster(
            wm, _RaisingFetcher(), _event("group:planning"), "iron-fox",
        )
        assert wm.get_section(ROSTER_SECTION_NAME) is None
