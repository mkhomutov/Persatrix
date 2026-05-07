"""RFC 0011 PR 5 follow-up — on-startup channel catch-up fetcher.

Resolves [RFC 0011 OQ #8](docs/rfcs/0011-channels-bridges.md): on
persona-runtime boot, each subscribed channel is queried for the last
N messages and replayed through the same ``CHANNEL_MESSAGE`` ingest
path (sanitization + ``InteractionTracker.add_turn``) but **without**
triggering an outbound ``SEND_CHANNEL_MESSAGE`` (the replay-mode flag
on ``AgentEvent.metadata`` bypasses the response gate).

Wire shape pinned by these tests:

* ``GET /api/v1/channels`` lists all channels (without members).
* ``GET /api/v1/channels/{id}`` returns the channel with its member list.
* ``GET /api/v1/channels/{id}/messages?limit=50`` returns the most recent
  history. Backed by :func:`internal.server.handleGetChannelHistory`
  (Go orchestrator) which orders messages newest-first.

The fetcher is **best-effort**: a transport or HTTP error on any
endpoint logs a warning and continues — startup must not block on a
flapping orchestrator. Watermark-based recovery is deferred per OQ #8;
v0.3.0 ships at-most-once with on-startup last-N as the only catch-up
trigger.
"""

from __future__ import annotations

from datetime import datetime, timezone

import aiohttp
import pytest
from aiohttp import web

from agents.channel_catchup import replay_channel_history
from agents.persona_types import AgentEvent, EventType


# ─── Loopback orchestrator fixtures ────────────────────────


def _msg(
    *,
    msg_id: str,
    channel_id: str,
    sender_id: str,
    content: str,
    ts: datetime | None = None,
    mentions: list[str] | None = None,
) -> dict:
    """Build a JSON message body in the wire shape from
    ``internal/server/channel_types.go::channelMessageResponse``."""
    return {
        "id": msg_id,
        "channel_id": channel_id,
        "sender_id": sender_id,
        "content": content,
        "timestamp": (ts or datetime(2026, 5, 7, 10, 0, tzinfo=timezone.utc)).isoformat(),
        "mentions": list(mentions or []),
    }


def _channel(*, channel_id: str, name: str = "", channel_type: str = "group") -> dict:
    return {
        "id": channel_id,
        "name": name or channel_id.split(":", 1)[-1],
        "channel_type": channel_type,
        "description": "",
        "created_at": "2026-05-01T00:00:00+00:00",
    }


@pytest.fixture
async def orchestrator():
    """Loopback orchestrator with the four endpoints the fetcher needs.

    Returns a dict the test can mutate to set up channels, members per
    channel, and history per channel; plus a ``log`` list capturing the
    URL of every request the fetcher issued (used to pin the call shape).
    """
    state: dict = {
        "channels": [],            # list of channel JSON
        "members": {},              # channel_id -> [member JSON]
        "history": {},              # channel_id -> [message JSON]
        "log": [],                  # request paths
        "fail_paths": set(),        # paths that return 500
    }

    async def list_channels(request: web.Request) -> web.Response:
        state["log"].append(request.path)
        if request.path in state["fail_paths"]:
            return web.json_response({"error": "boom"}, status=500)
        return web.json_response({"channels": state["channels"]})

    async def get_channel(request: web.Request) -> web.Response:
        state["log"].append(request.path)
        if request.path in state["fail_paths"]:
            return web.json_response({"error": "boom"}, status=500)
        cid = request.match_info["id"]
        ch = next((c for c in state["channels"] if c["id"] == cid), None)
        if ch is None:
            return web.json_response({"error": "NOT_FOUND"}, status=404)
        out = dict(ch)
        out["members"] = state["members"].get(cid, [])
        return web.json_response(out)

    async def history(request: web.Request) -> web.Response:
        state["log"].append(request.path_qs)
        if request.path in state["fail_paths"]:
            return web.json_response({"error": "boom"}, status=500)
        cid = request.match_info["id"]
        return web.json_response({"messages": state["history"].get(cid, [])})

    app = web.Application()
    app.router.add_get("/api/v1/channels", list_channels)
    app.router.add_get("/api/v1/channels/{id}", get_channel)
    app.router.add_get("/api/v1/channels/{id}/messages", history)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
    try:
        yield f"http://127.0.0.1:{port}", state
    finally:
        await runner.cleanup()


# ─── Spy agent ─────────────────────────────────────────────


class _SpyAgent:
    """Records ``on_event`` invocations so the test can assert the
    AgentEvent shape (event_type, channel_id, sender_id, replay_mode)
    without booting the full persona runtime.

    Mirrors the surface ``replay_channel_history`` consumes: ``agent_id``
    + ``async on_event(event)``. The integration with the real
    ``_LLMPersonaAgent`` is covered by
    ``tests/unit/python/test_replay_mode_action_loop.py``.
    """

    def __init__(self, agent_id: str) -> None:
        self.agent_id = agent_id
        self.events: list[AgentEvent] = []

    async def on_event(self, event: AgentEvent):
        self.events.append(event)
        return []


# ─── Tests ─────────────────────────────────────────────────


class TestReplayChannelHistory:
    async def test_no_channels_no_replay(self, orchestrator):
        """Empty channel list → exactly one REST call (the list) and
        zero replay events. Boundary case for fresh deployments."""
        base_url, state = orchestrator
        agent = _SpyAgent("ember-owl")

        async with aiohttp.ClientSession() as session:
            await replay_channel_history(
                agent=agent,
                orchestrator_url=base_url,
                session=session,
            )

        assert agent.events == []
        # Single GET /api/v1/channels — no per-channel calls.
        assert state["log"] == ["/api/v1/channels"]

    async def test_skips_channels_where_agent_is_not_a_member(self, orchestrator):
        """The fetcher must filter on membership before fetching history.

        Pulling history for non-member channels is wasteful (~50 msgs ×
        N channels of REST traffic) and would silently ingest content
        the agent never received via the live dispatch path — a privacy
        leak for any future per-membership ACL.
        """
        base_url, state = orchestrator
        state["channels"] = [_channel(channel_id="group:planning")]
        # Agent is NOT a member.
        state["members"]["group:planning"] = [
            {"id": "iron-fox", "respond": "when_mentioned",
             "joined_at": "2026-05-01T00:00:00+00:00"},
        ]
        state["history"]["group:planning"] = [
            _msg(msg_id="m1", channel_id="group:planning",
                 sender_id="iron-fox", content="hi"),
        ]

        agent = _SpyAgent("ember-owl")
        async with aiohttp.ClientSession() as session:
            await replay_channel_history(
                agent=agent, orchestrator_url=base_url, session=session,
            )

        assert agent.events == []
        # The fetcher checked membership but did NOT fetch history.
        assert "/api/v1/channels" in state["log"]
        assert "/api/v1/channels/group:planning" in state["log"]
        assert not any(p.startswith("/api/v1/channels/group:planning/messages")
                       for p in state["log"])

    async def test_replays_history_for_member_channels(self, orchestrator):
        """Happy path: agent is a member → history fetched → each msg
        replayed through ``on_event`` with ``replay_mode=True``."""
        base_url, state = orchestrator
        state["channels"] = [_channel(channel_id="group:planning")]
        state["members"]["group:planning"] = [
            {"id": "ember-owl", "respond": "when_mentioned",
             "joined_at": "2026-05-01T00:00:00+00:00"},
            {"id": "iron-fox", "respond": "when_mentioned",
             "joined_at": "2026-05-01T00:00:00+00:00"},
        ]
        # Wire shape: orchestrator returns history newest-first
        # (`internal/server/handleGetChannelHistory`). The fetcher is
        # responsible for reversing before replay so the
        # InteractionTracker sees turns in conversational order.
        state["history"]["group:planning"] = [
            _msg(msg_id="m2", channel_id="group:planning",
                 sender_id="iron-fox", content="anyone here?",
                 ts=datetime(2026, 5, 7, 10, 1, tzinfo=timezone.utc),
                 mentions=["ember-owl"]),
            _msg(msg_id="m1", channel_id="group:planning",
                 sender_id="iron-fox", content="hello",
                 ts=datetime(2026, 5, 7, 10, 0, tzinfo=timezone.utc)),
        ]

        agent = _SpyAgent("ember-owl")
        async with aiohttp.ClientSession() as session:
            await replay_channel_history(
                agent=agent, orchestrator_url=base_url, session=session,
            )

        assert len(agent.events) == 2
        for evt in agent.events:
            assert evt.event_type is EventType.CHANNEL_MESSAGE
            assert evt.channel_id == "group:planning"
            assert evt.metadata.get("replay_mode") is True

        # Content + sender preserved through the JSON → AgentEvent
        # round-trip; replayed oldest-first.
        assert agent.events[0].payload["content"] == "hello"
        assert agent.events[0].sender_id == "iron-fox"
        assert agent.events[1].payload["content"] == "anyone here?"
        assert "ember-owl" in agent.events[1].payload["mentions"]

    async def test_replays_oldest_first(self, orchestrator):
        """Replay order must be chronological so the InteractionTracker
        sees turns in the same sequence as a live conversation. The
        history endpoint returns newest-first per RFC 0011 §C; the
        fetcher reverses before replay.
        """
        base_url, state = orchestrator
        state["channels"] = [_channel(channel_id="group:planning")]
        state["members"]["group:planning"] = [
            {"id": "ember-owl", "respond": "when_mentioned",
             "joined_at": "2026-05-01T00:00:00+00:00"},
        ]
        # Server returns newest-first.
        state["history"]["group:planning"] = [
            _msg(msg_id="m3", channel_id="group:planning",
                 sender_id="iron-fox", content="third",
                 ts=datetime(2026, 5, 7, 10, 2, tzinfo=timezone.utc)),
            _msg(msg_id="m2", channel_id="group:planning",
                 sender_id="iron-fox", content="second",
                 ts=datetime(2026, 5, 7, 10, 1, tzinfo=timezone.utc)),
            _msg(msg_id="m1", channel_id="group:planning",
                 sender_id="iron-fox", content="first",
                 ts=datetime(2026, 5, 7, 10, 0, tzinfo=timezone.utc)),
        ]

        agent = _SpyAgent("ember-owl")
        async with aiohttp.ClientSession() as session:
            await replay_channel_history(
                agent=agent, orchestrator_url=base_url, session=session,
            )

        contents = [e.payload["content"] for e in agent.events]
        assert contents == ["first", "second", "third"]

    async def test_uses_default_limit_50(self, orchestrator):
        """The default page size is 50 (matching RFC 0011 §C
        ``channelDefaultHistoryLimit``); the fetcher must surface the
        ``?limit=50`` query string so a future RFC bump on the
        server-side default cannot silently change ingest depth.
        """
        base_url, state = orchestrator
        state["channels"] = [_channel(channel_id="group:planning")]
        state["members"]["group:planning"] = [
            {"id": "ember-owl", "respond": "when_mentioned",
             "joined_at": "2026-05-01T00:00:00+00:00"},
        ]
        state["history"]["group:planning"] = []

        agent = _SpyAgent("ember-owl")
        async with aiohttp.ClientSession() as session:
            await replay_channel_history(
                agent=agent, orchestrator_url=base_url, session=session,
            )

        assert any("/messages?limit=50" in p for p in state["log"])

    async def test_custom_limit_passes_through(self, orchestrator):
        base_url, state = orchestrator
        state["channels"] = [_channel(channel_id="group:planning")]
        state["members"]["group:planning"] = [
            {"id": "ember-owl", "respond": "when_mentioned",
             "joined_at": "2026-05-01T00:00:00+00:00"},
        ]
        state["history"]["group:planning"] = []

        agent = _SpyAgent("ember-owl")
        async with aiohttp.ClientSession() as session:
            await replay_channel_history(
                agent=agent, orchestrator_url=base_url, session=session,
                limit=20,
            )

        assert any("limit=20" in p for p in state["log"])

    async def test_list_channels_failure_is_logged_and_returns(
        self, orchestrator, caplog,
    ):
        """A 5xx on the list endpoint is best-effort: log + return.

        Startup must not crash on a flapping orchestrator. The agent
        accepts at-most-once delivery; the missed catch-up window
        becomes a tracked operational signal via
        ``channel.delivery.missed`` (RFC 0011 OQ #2).
        """
        base_url, state = orchestrator
        state["fail_paths"].add("/api/v1/channels")
        agent = _SpyAgent("ember-owl")

        with caplog.at_level("WARNING"):
            async with aiohttp.ClientSession() as session:
                await replay_channel_history(
                    agent=agent, orchestrator_url=base_url, session=session,
                )

        assert agent.events == []
        # WARN surfaces the failure to the operator.
        assert any(
            "channel" in rec.message.lower() and "catch" in rec.message.lower()
            for rec in caplog.records
        )

    async def test_history_failure_for_one_channel_does_not_block_others(
        self, orchestrator,
    ):
        """A failing channel must not strand healthy ones.

        Two channels, one returns 500 on history. The healthy channel
        still replays. The fetcher logs WARN for the failure and
        continues — symmetric with the publish path's WARN-and-continue.
        """
        base_url, state = orchestrator
        state["channels"] = [
            _channel(channel_id="group:planning"),
            _channel(channel_id="group:dogfood"),
        ]
        for cid in ("group:planning", "group:dogfood"):
            state["members"][cid] = [
                {"id": "ember-owl", "respond": "when_mentioned",
                 "joined_at": "2026-05-01T00:00:00+00:00"},
            ]
        state["history"]["group:planning"] = [
            _msg(msg_id="m1", channel_id="group:planning",
                 sender_id="iron-fox", content="ok"),
        ]
        state["fail_paths"].add("/api/v1/channels/group:dogfood/messages")
        # group:dogfood history NOT populated — but the path itself fails.

        agent = _SpyAgent("ember-owl")
        async with aiohttp.ClientSession() as session:
            await replay_channel_history(
                agent=agent, orchestrator_url=base_url, session=session,
            )

        # Healthy channel replayed.
        assert any(e.channel_id == "group:planning" for e in agent.events)

    async def test_replay_includes_respond_policy_from_membership(
        self, orchestrator,
    ):
        """The fetcher must propagate ``respond_policy`` from the member
        record into ``payload["respond_policy"]`` so the synthetic event
        looks identical to a live ``ReceiveChannelMessage`` dispatch.
        Identical wire shape → the action_loop's replay-mode short-
        circuit is the only behavioural difference.
        """
        base_url, state = orchestrator
        state["channels"] = [_channel(channel_id="group:planning")]
        state["members"]["group:planning"] = [
            {"id": "ember-owl", "respond": "always",
             "joined_at": "2026-05-01T00:00:00+00:00"},
        ]
        state["history"]["group:planning"] = [
            _msg(msg_id="m1", channel_id="group:planning",
                 sender_id="iron-fox", content="hi"),
        ]

        agent = _SpyAgent("ember-owl")
        async with aiohttp.ClientSession() as session:
            await replay_channel_history(
                agent=agent, orchestrator_url=base_url, session=session,
            )

        assert len(agent.events) == 1
        assert agent.events[0].payload["respond_policy"] == "always"

    async def test_dm_channel_replays_when_agent_is_participant(
        self, orchestrator,
    ):
        """DM channels (``dm:a:b``) are members-by-construction. The
        fetcher must replay DM history when the agent is one of the
        two participants — the live path treats DM as ``always``-gated
        per RFC 0011 §D, but replay still skips the LLM."""
        base_url, state = orchestrator
        state["channels"] = [
            _channel(channel_id="dm:ember-owl:iron-fox", channel_type="dm"),
        ]
        state["members"]["dm:ember-owl:iron-fox"] = [
            {"id": "ember-owl", "respond": "always",
             "joined_at": "2026-05-01T00:00:00+00:00"},
            {"id": "iron-fox", "respond": "always",
             "joined_at": "2026-05-01T00:00:00+00:00"},
        ]
        state["history"]["dm:ember-owl:iron-fox"] = [
            _msg(msg_id="m1", channel_id="dm:ember-owl:iron-fox",
                 sender_id="iron-fox", content="quick q"),
        ]

        agent = _SpyAgent("ember-owl")
        async with aiohttp.ClientSession() as session:
            await replay_channel_history(
                agent=agent, orchestrator_url=base_url, session=session,
            )

        assert len(agent.events) == 1
        assert agent.events[0].channel_id == "dm:ember-owl:iron-fox"
        assert agent.events[0].metadata.get("replay_mode") is True


# pytest-asyncio plugin auto-detects ``async def`` tests via
# ``asyncio_mode = "auto"`` in ``pyproject.toml``; no marker needed.
