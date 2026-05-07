"""RFC 0011 PR 5 follow-up — PR-265 deep-review fixes.

Pins the catch-up fetcher behaviours added in response to the PR-265
review findings. Kept separate from ``test_channel_catchup.py`` so
neither file blows past the 500-line review cap; both files share the
loopback orchestrator fixture from
``tests/unit/python/_catchup_test_helpers.py``.

Findings covered here:

* **S2** — wire ``msg["timestamp"]`` is forwarded into
  ``AgentEvent.timestamp``; without this, replayed events default to
  ``time.time()`` at boot and would defeat the RFC 0021 P1 now-anchor
  / recency rendering by making catch-up history render as "just now".
  Also pins the defensive fallback when the wire field is missing or
  unparseable.
* **L1** — ``GET /api/v1/channels`` carries an explicit ``?limit=``
  query string so the Go-side ``channelDefaultListLimit = 50`` silent
  cap cannot mask catch-up for high-fanout agents.
"""

from __future__ import annotations

from datetime import datetime, timezone

import aiohttp
import pytest

from agents.channel_catchup import replay_channel_history

# The ``orchestrator`` fixture is registered via ``conftest.py`` so
# tests get it injected by name — no per-file import needed (which
# would trigger ruff F811 on every fixture parameter).
from ._catchup_test_helpers import _SpyAgent, _channel, _msg


class TestReplayPreservesTimestamp:
    """PR-265 S2: forward wire timestamp into ``AgentEvent.timestamp``."""

    async def test_replay_preserves_message_timestamp(self, orchestrator):
        """The replayed ``AgentEvent.timestamp`` MUST come from the wire
        ``msg["timestamp"]`` field, not the boot time of the catch-up
        run.

        Why: the live ingest path (``ReceiveChannelMessage`` →
        ``validate_channel_message_event``) propagates the
        orchestrator-authored RFC 3339 timestamp into
        ``AgentEvent.timestamp``. The replay path must mirror this so
        downstream consumers — episodic ``started_at``, the PR-4
        summariser's ``Turn.payload["timestamp"]``, and especially the
        RFC 0021 P1 now-anchor / recency rendering — see real message
        ages instead of "everything happened at boot".

        Without this, replayed messages render as "just now" the first
        time the persona reasons about catch-up history, defeating the
        relative-time signal that RFC 0021 is built on.
        """
        base_url, state = orchestrator
        state["channels"] = [_channel(channel_id="group:planning")]
        state["members"]["group:planning"] = [
            {"id": "ember-owl", "respond": "when_mentioned",
             "joined_at": "2026-05-01T00:00:00+00:00"},
        ]
        original_ts = datetime(2026, 5, 7, 9, 30, tzinfo=timezone.utc)
        state["history"]["group:planning"] = [
            _msg(msg_id="m1", channel_id="group:planning",
                 sender_id="iron-fox", content="hello from earlier",
                 ts=original_ts),
        ]

        agent = _SpyAgent("ember-owl")
        async with aiohttp.ClientSession() as session:
            await replay_channel_history(
                agent=agent, orchestrator_url=base_url, session=session,
            )

        assert len(agent.events) == 1
        # Timestamp on the replayed event MUST equal the wire timestamp
        # (parsed to epoch seconds), not the time the fetcher ran.
        assert agent.events[0].timestamp == pytest.approx(
            original_ts.timestamp(), abs=0.001,
        )

    async def test_replay_falls_back_when_timestamp_missing_or_invalid(
        self, orchestrator,
    ):
        """Defensive: if the wire ``timestamp`` is absent, empty, or not
        a valid RFC 3339 string, the fetcher must not crash and must not
        propagate ``None`` into ``AgentEvent.timestamp`` (which is typed
        ``float``). Falling back to the dataclass default
        (``time.time()``) is acceptable — the same fallback that pre-S2
        replay used unconditionally.

        Symmetric with ``parse_channel_timestamp`` returning ``None`` on
        the live path; the wire validator there rejects the message,
        but for best-effort catch-up we ingest with the boot fallback
        rather than dropping the row entirely.
        """
        base_url, state = orchestrator
        state["channels"] = [_channel(channel_id="group:planning")]
        state["members"]["group:planning"] = [
            {"id": "ember-owl", "respond": "when_mentioned",
             "joined_at": "2026-05-01T00:00:00+00:00"},
        ]
        # Message with empty timestamp string — mimics a malformed row.
        state["history"]["group:planning"] = [
            {
                "id": "m1",
                "channel_id": "group:planning",
                "sender_id": "iron-fox",
                "content": "no timestamp",
                "timestamp": "",
                "mentions": [],
            },
        ]

        agent = _SpyAgent("ember-owl")
        async with aiohttp.ClientSession() as session:
            await replay_channel_history(
                agent=agent, orchestrator_url=base_url, session=session,
            )

        # Replay still happens — best-effort path.
        assert len(agent.events) == 1
        # Fallback is a finite float (the dataclass default).
        assert isinstance(agent.events[0].timestamp, float)


class TestChannelListExplicitLimit:
    """PR-265 L1: ``GET /api/v1/channels`` carries an explicit
    ``?limit=`` so the Go-side default cap cannot silently mask
    catch-up for high-fanout agents.
    """

    async def test_channel_list_passes_explicit_limit(self, orchestrator):
        """``GET /api/v1/channels`` must carry an explicit ``?limit=``
        query string so an agent enrolled in >50 channels does not
        silently miss catch-up for the tail of its membership.

        The Go orchestrator's ``handleListChannels`` falls back to
        ``channelDefaultListLimit = 50`` when no limit is supplied
        (``internal/server/channel_handlers.go``). The fetcher requests
        the maximum allowed (``channelMaxLimit = 1000``) so the
        per-agent membership is fully covered. Above 1000 the server
        clamps; that ceiling is the acknowledged design contract.
        """
        base_url, state = orchestrator
        # Empty channel list — we only care about the request path.
        agent = _SpyAgent("ember-owl")
        async with aiohttp.ClientSession() as session:
            await replay_channel_history(
                agent=agent, orchestrator_url=base_url, session=session,
            )

        assert any(
            p.startswith("/api/v1/channels?") and "limit=" in p
            for p in state["log"]
        ), f"expected explicit ?limit= on channel-list call; got {state['log']!r}"


# pytest-asyncio plugin auto-detects ``async def`` tests via
# ``asyncio_mode = "auto"`` in ``pyproject.toml``; no marker needed.
