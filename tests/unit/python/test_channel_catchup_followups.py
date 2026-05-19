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
* **first-pass L1** (``?limit=``) — ``GET /api/v1/channels`` carries
  an explicit ``?limit=`` query string so the Go-side
  ``channelDefaultListLimit = 50`` silent cap cannot mask catch-up for
  high-fanout agents.

Second-pass deep-review fixes (commit on this branch following
``ddbd5e8``):

* **second-pass L1** (validation parity) — the catch-up fetcher applies
  ``validate_channel_message_dict`` to every JSON row before building
  the ``AgentEvent``, mirroring the live ``ReceiveChannelMessage``
  defense-in-depth bounds (PR #248 trust-boundary asymmetry +
  PR-265 review L1).
* **second-pass L3** (wall-clock budget) — the per-agent catch-up
  pass is wrapped in ``asyncio.wait_for`` so a slow / hung
  orchestrator cannot indefinitely block boot.
* **second-pass L7** (success log) — one INFO log line at the end of
  ``replay_channel_history`` gives operators a single-line "catch-up
  ran" signal without having to join counter values across channels.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import aiohttp
import pytest

import agents.channel_catchup as channel_catchup
from agents.channel_catchup import replay_channel_history

# The ``orchestrator`` fixture is registered via ``conftest.py`` so
# tests get it injected by name — no per-file import needed (which
# would trigger ruff F811 on every fixture parameter).
from ._catchup_test_helpers import _channel, _msg, _SpyAgent


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
        original_ts = datetime(2026, 5, 7, 9, 30, tzinfo=UTC)
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

    async def test_replay_drops_row_when_timestamp_missing_or_invalid(
        self, orchestrator,
    ):
        """If the wire ``timestamp`` is absent, empty, or not a valid
        RFC 3339 string, the fetcher MUST drop the row — not ingest it
        with a boot-time fallback.

        This contract supersedes the earlier S2 "ingest with fallback"
        behaviour once PR-265 review L1 (second pass) wired
        ``validate_channel_message_dict`` into the catch-up path. The
        live ``ReceiveChannelMessage`` path rejects malformed
        timestamps via ``validate_channel_message_event``; symmetric
        defense-in-depth on the REST/JSON catch-up seam means the
        catch-up path rejects them too.

        The fallback in ``_build_replay_event`` is now pure
        defense-in-depth for an impossible case — the validator
        prevents malformed timestamps from ever reaching it.
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

        assert agent.events == []


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


class TestCatchupValidationParity:
    """PR-265 L1 (second-pass): catch-up rows are validated with the same
    bounds as the live ``ReceiveChannelMessage`` path.

    The orchestrator REST surface rides the same TLS-deferred trust
    boundary as the cleartext gRPC port (see ``agents/server.py`` TLS
    TODO). Without symmetric validation, an attacker MITM'ing the REST
    connection — or a future writer that bypasses the router validator
    when persisting to the channel store — could drive malformed
    payloads through the catch-up seam.

    The fetcher rejects malformed rows individually (logs WARN) and
    keeps replaying the rest of the channel; the contract is
    "best-effort ingest with bounds-checking", not "all-or-nothing".
    """

    async def test_skips_row_with_invalid_sender_id(self, orchestrator):
        """A row whose ``sender_id`` violates the participant-id pattern
        must be dropped without aborting the rest of the channel.

        Mirrors ``validate_channel_message_event`` rejecting
        ``sender_id`` patterns that don't match
        ``^[a-z0-9][a-z0-9-]*[a-z0-9]$`` — same trust boundary on both
        ingest seams (PR #248 trust-boundary asymmetry).
        """
        base_url, state = orchestrator
        state["channels"] = [_channel(channel_id="group:planning")]
        state["members"]["group:planning"] = [
            {"id": "ember-owl", "respond": "when_mentioned",
             "joined_at": "2026-05-01T00:00:00+00:00"},
        ]
        state["history"]["group:planning"] = [
            _msg(msg_id="m1", channel_id="group:planning",
                 sender_id="iron-fox", content="ok"),
            # Malformed: capitalised, contains an invalid character.
            _msg(msg_id="m2", channel_id="group:planning",
                 sender_id="BAD-Sender", content="injected"),
            _msg(msg_id="m3", channel_id="group:planning",
                 sender_id="iron-fox", content="also ok"),
        ]

        agent = _SpyAgent("ember-owl")
        async with aiohttp.ClientSession() as session:
            await replay_channel_history(
                agent=agent, orchestrator_url=base_url, session=session,
            )

        # The two well-formed rows make it through; the malformed one
        # is dropped (best-effort, log + continue).
        senders = [e.sender_id for e in agent.events]
        assert "iron-fox" in senders
        assert "BAD-Sender" not in senders
        assert len(agent.events) == 2

    async def test_skips_row_with_oversize_content(self, orchestrator):
        """Content bound (4000 chars) is mirrored from
        ``_CHANNEL_CONTENT_MAX_CHARS``. Catch-up should not happily
        ingest a 5000-char row that the live path would reject.
        """
        base_url, state = orchestrator
        state["channels"] = [_channel(channel_id="group:planning")]
        state["members"]["group:planning"] = [
            {"id": "ember-owl", "respond": "when_mentioned",
             "joined_at": "2026-05-01T00:00:00+00:00"},
        ]
        state["history"]["group:planning"] = [
            _msg(msg_id="m1", channel_id="group:planning",
                 sender_id="iron-fox",
                 content="x" * 4001),  # one over the cap
        ]

        agent = _SpyAgent("ember-owl")
        async with aiohttp.ClientSession() as session:
            await replay_channel_history(
                agent=agent, orchestrator_url=base_url, session=session,
            )

        assert agent.events == []

    async def test_skips_row_when_channel_type_prefix_disagrees(
        self, orchestrator,
    ):
        """A history row whose ``channel_id`` doesn't match the parent
        channel's ``channel_type`` prefix is malformed routing.
        Identical to the live-path check (RFC 0011 §B).
        """
        base_url, state = orchestrator
        # Parent channel says ``group``; row carries a ``dm:``-prefixed
        # channel_id. A future bug in the orchestrator's history filter
        # could produce this — defense-in-depth.
        state["channels"] = [_channel(channel_id="group:planning")]
        state["members"]["group:planning"] = [
            {"id": "ember-owl", "respond": "when_mentioned",
             "joined_at": "2026-05-01T00:00:00+00:00"},
        ]
        state["history"]["group:planning"] = [
            _msg(msg_id="m1", channel_id="dm:ember-owl:iron-fox",
                 sender_id="iron-fox", content="leaked dm"),
        ]

        agent = _SpyAgent("ember-owl")
        async with aiohttp.ClientSession() as session:
            await replay_channel_history(
                agent=agent, orchestrator_url=base_url, session=session,
            )

        assert agent.events == []

    async def test_well_formed_rows_still_pass(self, orchestrator):
        """Sanity: validation parity must NOT regress the happy path —
        well-formed rows continue to replay end-to-end.
        """
        base_url, state = orchestrator
        state["channels"] = [_channel(channel_id="group:planning")]
        state["members"]["group:planning"] = [
            {"id": "ember-owl", "respond": "when_mentioned",
             "joined_at": "2026-05-01T00:00:00+00:00"},
        ]
        state["history"]["group:planning"] = [
            _msg(msg_id="m1", channel_id="group:planning",
                 sender_id="iron-fox", content="hello",
                 mentions=["ember-owl"]),
        ]

        agent = _SpyAgent("ember-owl")
        async with aiohttp.ClientSession() as session:
            await replay_channel_history(
                agent=agent, orchestrator_url=base_url, session=session,
            )

        assert len(agent.events) == 1
        assert agent.events[0].payload["content"] == "hello"


class TestCatchupWallClockBudget:
    """PR-265 L3 (second-pass): the per-agent catch-up pass is bounded
    by a single wall-clock budget so a slow or hung orchestrator
    cannot block boot indefinitely.

    Each REST call is independently bounded by ``_REQUEST_TIMEOUT_SECONDS``
    (10s), but the multiplicative worst case (10s × 2N for N channels)
    grows unbounded with channel fanout. The budget caps the cumulative
    pass at a single wall-clock value; on overrun the helper logs WARN
    and returns, leaving in-flight requests cancelled.
    """

    async def test_budget_aborts_slow_pass(self, orchestrator, monkeypatch, caplog):
        """Force a tiny budget and a deliberately slow channel-list
        handler; the helper must abandon the pass without raising and
        without replaying any events.

        Pinning the slow path on ``list_channels`` (the first hop)
        proves the wait_for wraps the *whole* pass body, not just one
        per-channel iteration.
        """
        base_url, state = orchestrator

        # Wrap the existing list endpoint with a deliberate delay.
        original = channel_catchup._fetch_channel_list

        async def slow_fetch(session, base, timeout):
            await asyncio.sleep(0.5)
            return await original(session, base, timeout)

        monkeypatch.setattr(channel_catchup, "_fetch_channel_list", slow_fetch)
        # Tighten the budget far below the artificial delay.
        monkeypatch.setattr(channel_catchup, "_CATCHUP_BUDGET_SECONDS", 0.05)

        agent = _SpyAgent("ember-owl")
        with caplog.at_level("WARNING"):
            async with aiohttp.ClientSession() as session:
                await replay_channel_history(
                    agent=agent, orchestrator_url=base_url, session=session,
                )

        assert agent.events == []
        # The WARN line surfaces the budget-overrun to operators.
        assert any(
            "budget" in rec.message.lower() and "catch" in rec.message.lower()
            for rec in caplog.records
        ), f"expected budget WARN; got {[r.message for r in caplog.records]!r}"

    async def test_budget_does_not_fire_on_fast_pass(self, orchestrator):
        """A normal-speed run must complete well within the budget and
        replay successfully — the budget must not introduce false
        positives on the typical path."""
        base_url, state = orchestrator
        state["channels"] = [_channel(channel_id="group:planning")]
        state["members"]["group:planning"] = [
            {"id": "ember-owl", "respond": "when_mentioned",
             "joined_at": "2026-05-01T00:00:00+00:00"},
        ]
        state["history"]["group:planning"] = [
            _msg(msg_id="m1", channel_id="group:planning",
                 sender_id="iron-fox", content="ok"),
        ]

        agent = _SpyAgent("ember-owl")
        async with aiohttp.ClientSession() as session:
            await replay_channel_history(
                agent=agent, orchestrator_url=base_url, session=session,
            )

        assert len(agent.events) == 1


class TestCatchupSuccessLog:
    """PR-265 L7 (second-pass): one INFO log line at the end of a
    successful pass so operators can confirm catch-up actually ran
    without joining ``channel.messages.replayed`` counter values.
    """

    async def test_emits_summary_info_line(self, orchestrator, caplog):
        base_url, state = orchestrator
        state["channels"] = [_channel(channel_id="group:planning")]
        state["members"]["group:planning"] = [
            {"id": "ember-owl", "respond": "when_mentioned",
             "joined_at": "2026-05-01T00:00:00+00:00"},
        ]
        state["history"]["group:planning"] = [
            _msg(msg_id="m1", channel_id="group:planning",
                 sender_id="iron-fox", content="hello"),
            _msg(msg_id="m2", channel_id="group:planning",
                 sender_id="iron-fox", content="world"),
        ]

        agent = _SpyAgent("ember-owl")
        with caplog.at_level("INFO", logger="agents.channel_catchup"):
            async with aiohttp.ClientSession() as session:
                await replay_channel_history(
                    agent=agent, orchestrator_url=base_url, session=session,
                )

        info_records = [
            r for r in caplog.records
            if r.levelname == "INFO" and r.name == "agents.channel_catchup"
        ]
        assert info_records, "expected at least one INFO log on success"
        # Pin the structure: message must surface (a) the agent id,
        # (b) replayed event count, (c) channel count. Operators
        # paging through boot logs need all three to triage.
        joined = "\n".join(r.getMessage() for r in info_records)
        assert "ember-owl" in joined
        assert "2" in joined  # event count
        assert "1" in joined  # channel count

    async def test_emits_summary_even_on_empty_replay(self, orchestrator, caplog):
        """Empty channel list still emits an INFO line — the run
        boundary should be observable even on a fresh deployment.
        """
        base_url, state = orchestrator
        agent = _SpyAgent("ember-owl")
        with caplog.at_level("INFO", logger="agents.channel_catchup"):
            async with aiohttp.ClientSession() as session:
                await replay_channel_history(
                    agent=agent, orchestrator_url=base_url, session=session,
                )

        info_records = [
            r for r in caplog.records
            if r.levelname == "INFO" and r.name == "agents.channel_catchup"
        ]
        assert info_records, "expected INFO line even on empty replay"


# pytest-asyncio plugin auto-detects ``async def`` tests via
# ``asyncio_mode = "auto"`` in ``pyproject.toml``; no marker needed.
