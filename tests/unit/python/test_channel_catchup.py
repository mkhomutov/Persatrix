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

from datetime import UTC, datetime

import aiohttp

from agents.channel_catchup import replay_channel_history
from agents.persona_types import EventType

# Helpers (``_msg``, ``_channel``, ``_SpyAgent``) live in a private
# sibling module so this file and the PR-265-review follow-up file
# ``test_channel_catchup_followups.py`` can share one loopback
# orchestrator fixture without either file blowing past the 500-line
# review cap. The ``orchestrator`` fixture itself is registered via
# ``conftest.py`` so tests get it injected by name without an import
# (avoids ruff F811 on every fixture parameter).
from ._catchup_test_helpers import _channel, _msg, _SpyAgent

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
        # Single GET /api/v1/channels — no per-channel calls. The
        # fetcher carries an explicit ``?limit=`` (PR-265 L1: prevent
        # the silent 50-channel default cap from masking high-fanout
        # agents).
        assert len(state["log"]) == 1
        assert state["log"][0].startswith("/api/v1/channels?")
        assert "limit=" in state["log"][0]

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
        # ``list_channels`` is logged via ``path_qs`` so the recorded
        # entry includes the explicit ``?limit=`` query string.
        assert any(p.startswith("/api/v1/channels?") for p in state["log"])
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
                 ts=datetime(2026, 5, 7, 10, 1, tzinfo=UTC),
                 mentions=["ember-owl"]),
            _msg(msg_id="m1", channel_id="group:planning",
                 sender_id="iron-fox", content="hello",
                 ts=datetime(2026, 5, 7, 10, 0, tzinfo=UTC)),
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
                 ts=datetime(2026, 5, 7, 10, 2, tzinfo=UTC)),
            _msg(msg_id="m2", channel_id="group:planning",
                 sender_id="iron-fox", content="second",
                 ts=datetime(2026, 5, 7, 10, 1, tzinfo=UTC)),
            _msg(msg_id="m1", channel_id="group:planning",
                 sender_id="iron-fox", content="first",
                 ts=datetime(2026, 5, 7, 10, 0, tzinfo=UTC)),
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

    async def test_catchup_fetch_is_scoped_to_agent(self, orchestrator):
        """RFC 0036 §G: the boot-time catch-up replay passes the agent's id
        as ``as_participant`` so episodic seeding is membership-scoped — a
        re-added persona does not re-ingest the removal-gap messages it
        missed. (The current-state member check that already gates the
        per-channel fetch stays as the cheap pre-filter; this scopes the
        *rows* server-side to the agent's stints.)"""
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

        assert any(
            "/messages?limit=50&as_participant=ember-owl" in p
            for p in state["log"]
        ), f"catch-up fetch must carry as_participant; got {state['log']!r}"

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


class TestReplayWireMetadata:
    """The startup replay must carry the validated wire interaction keys
    off the REST history rows (``messageToResponse`` returns the
    router-stamped metadata verbatim) — without them, a replayed span
    covering a vote-closed conversation and the channel's next topic
    merges into ONE local record and the first live wire id becomes
    adoption-not-rotation, silently disarming the RFC 0030 close
    propagation after every restart (PR 607 second-pass review)."""

    def test_replay_event_carries_validated_wire_keys(self):
        from agents.channel_catchup import _build_replay_event

        msg = _msg(
            msg_id="m1", channel_id="group:planning",
            sender_id="iron-fox", content="hello",
        )
        msg["metadata"] = {
            "interaction_id": "wire-B",
            "previous_interaction_id": "wire-A",
            "previous_interaction_close_trigger": "end_votes",
            "cascade_depth": 1,
        }
        event = _build_replay_event(
            msg, "group:planning", "when_mentioned",
            _channel(channel_id="group:planning"),
        )
        assert event.metadata["replay_mode"] is True
        assert event.metadata["interaction_id"] == "wire-B"
        assert event.metadata["previous_interaction_id"] == "wire-A"
        assert event.metadata["previous_interaction_close_trigger"] == "end_votes"
        # Only the validated wire keys ride along — the row's other
        # metadata (cascade_depth etc.) is not the replay's business.
        assert "cascade_depth" not in event.metadata

    def test_replay_event_revalidates_like_the_live_seed_point(self):
        """Same posture as ``seed_wire_metadata``: an oversized id reads
        as untracked, a half pair / unrecognised trigger seeds nothing,
        and rows with no metadata (pre-v0.3.8 history) replay exactly
        as before."""
        from agents.channel_catchup import _build_replay_event

        base = _msg(
            msg_id="m1", channel_id="group:planning",
            sender_id="iron-fox", content="hello",
        )
        channel = _channel(channel_id="group:planning")

        oversized = dict(base, metadata={"interaction_id": "x" * 129})
        event = _build_replay_event(
            oversized, "group:planning", "when_mentioned", channel,
        )
        assert "interaction_id" not in event.metadata

        junk_trigger = dict(base, metadata={
            "interaction_id": "wire-B",
            "previous_interaction_id": "wire-A",
            "previous_interaction_close_trigger": "cosmic-rays",
        })
        event = _build_replay_event(
            junk_trigger, "group:planning", "when_mentioned", channel,
        )
        assert event.metadata["interaction_id"] == "wire-B"
        assert "previous_interaction_id" not in event.metadata
        assert "previous_interaction_close_trigger" not in event.metadata

        event = _build_replay_event(
            dict(base), "group:planning", "when_mentioned", channel,
        )
        assert event.metadata == {"replay_mode": True}


# pytest-asyncio plugin auto-detects ``async def`` tests via
# ``asyncio_mode = "auto"`` in ``pyproject.toml``; no marker needed.
