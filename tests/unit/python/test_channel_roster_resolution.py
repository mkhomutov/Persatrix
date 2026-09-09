"""Roster resolution as an audience rail (v0.3.16 PR A1, ISSUE-0132).

The RFC 0037 §D gate decides what a persona may say from the acting
channel's classification alone; it cannot ask who is in the room, because
the roster is fetched *after* the gate has already run and only for group
channels. PR A1 lays the rail the audience check (PR A2) will read:

* the fetcher's two GETs are split, so a directory ``401`` (the fleet's
  authenticated ``/api/v1/agents``) no longer throws away the public
  channel-members half — the member set survives;
* resolution runs for every channel-anchored turn, DMs included — a DM
  with Bob is an audience;
* an audience that could not be established resolves to ``None`` rather
  than to a roster nobody is in.

This module covers resolution and the resolved record. What the prompt
does with it — which is nothing, on every turn but a group one — lives in
``test_channel_roster_injection.py``.
"""

from __future__ import annotations

import logging
from typing import Any

from agents.persona_runtime.channel_roster import (
    ChannelRoster,
    DirectoryStatus,
    build_roster,
    resolve_channel_roster,
)

from ._channel_roster_helpers import (
    _AGENTS,
    _CHANNEL,
    _DM,
    _MEMBERLESS,
    _event,
    _FakeFetcher,
)

# ─── resolve_channel_roster ───────────────────────────────────


class TestResolveChannelRoster:
    async def test_group_turn_resolves_the_member_set(self) -> None:
        roster = await resolve_channel_roster(
            _FakeFetcher(_CHANNEL, _AGENTS),
            _event("group:planning"), "iron-fox",
        )
        assert roster is not None
        assert roster.channel_id == "group:planning"
        assert roster.member_ids == {"ember-owl", "iron-fox", "nova-sparrow"}
        assert roster.directory is DirectoryStatus.RESOLVED

    async def test_dm_turn_resolves_a_roster(self) -> None:
        """A DM with Bob is an audience — today's group-only fetch is why
        the gate has nothing to read on the very turn ISSUE-0132 is about."""
        fetcher = _FakeFetcher(_DM, _AGENTS)
        roster = await resolve_channel_roster(
            fetcher, _event("dm:alice:iron-fox"), "iron-fox",
        )
        assert fetcher.calls == ["dm:alice:iron-fox"]
        assert roster is not None
        assert roster.member_ids == {"alice", "iron-fox"}

    async def test_directory_miss_still_yields_members(self) -> None:
        """The audience is member ids; the directory only supplies display
        names. A ``401`` on the authenticated half must not cost the ids."""
        roster = await resolve_channel_roster(
            _FakeFetcher(_CHANNEL, None),
            _event("group:planning"), "iron-fox",
        )
        assert roster is not None
        assert roster.member_ids == {"ember-owl", "iron-fox", "nova-sparrow"}
        assert roster.directory is DirectoryStatus.MISSED

    async def test_turn_without_a_channel_resolves_nothing(self) -> None:
        """A tick-shaped turn names no channel, so there is no audience to
        resolve and no round trip to spend."""
        fetcher = _FakeFetcher(_CHANNEL, _AGENTS)
        assert await resolve_channel_roster(fetcher, _event(None), "iron-fox") is None
        assert fetcher.calls == []

    async def test_dm_turn_does_not_spend_the_directory_round_trip(self) -> None:
        """A DM renders no roster section, so its display names can never be
        used — and under auth the directory half is a guaranteed ``401``
        plus a warning line. The audience is ids; do not pay for names."""
        fetcher = _FakeFetcher(_DM, _AGENTS)
        roster = await resolve_channel_roster(
            fetcher, _event("dm:alice:iron-fox"), "iron-fox",
        )
        assert roster is not None
        assert roster.member_ids == {"alice", "iron-fox"}
        assert fetcher.directory_calls == 0
        assert roster.directory is DirectoryStatus.NOT_REQUESTED

    async def test_group_turn_spends_both_halves(self) -> None:
        fetcher = _FakeFetcher(_CHANNEL, _AGENTS)
        await resolve_channel_roster(
            fetcher, _event("group:planning"), "iron-fox",
        )
        assert fetcher.calls == ["group:planning"]
        assert fetcher.directory_calls == 1

    async def test_thread_turn_has_no_channel_row_to_resolve(self) -> None:
        """``thread:`` is the third channel prefix
        (``internal/channels/identifiers.go``), but no production path
        creates a thread channel ROW — replies are messages in the parent
        channel. So the members GET 404s and resolution yields nothing;
        the directory round trip is not spent on the way."""
        fetcher = _FakeFetcher(None, _AGENTS)
        roster = await resolve_channel_roster(
            fetcher, _event("thread:planning:abc"), "iron-fox",
        )
        assert roster is None
        assert fetcher.calls == ["thread:planning:abc"]
        assert fetcher.directory_calls == 0

    async def test_raising_directory_still_yields_the_audience(self) -> None:
        """The directory half is best-effort: a raise inside it must cost
        display names, not the member set."""
        roster = await resolve_channel_roster(
            _FakeFetcher(_CHANNEL, RuntimeError("directory unreachable")),
            _event("group:planning"), "iron-fox",
        )
        assert roster is not None
        assert roster.member_ids == {"ember-owl", "iron-fox", "nova-sparrow"}
        assert roster.directory is DirectoryStatus.MISSED

    async def test_no_fetcher_resolves_nothing(self) -> None:
        assert await resolve_channel_roster(
            None, _event("group:planning"), "iron-fox",
        ) is None

    async def test_fetch_failure_resolves_nothing(self) -> None:
        assert await resolve_channel_roster(
            _FakeFetcher(None), _event("group:planning"), "iron-fox",
        ) is None

    async def test_raising_fetch_is_non_fatal(self, caplog: Any) -> None:
        with caplog.at_level(logging.WARNING):
            roster = await resolve_channel_roster(
                _FakeFetcher(RuntimeError("orchestrator unreachable")),
                _event("group:planning"), "iron-fox",
            )
        assert roster is None
        assert any(
            "roster resolution failed" in r.getMessage() for r in caplog.records
        )

    async def test_self_is_marked_in_the_resolved_members(self) -> None:
        roster = await resolve_channel_roster(
            _FakeFetcher(_CHANNEL, _AGENTS),
            _event("group:planning"), "iron-fox",
        )
        assert roster is not None
        assert [m.id for m in roster.members if m.is_self] == ["iron-fox"]

    async def test_memberless_channel_is_not_an_empty_audience(self) -> None:
        """A room the orchestrator returns with no member list is UNKNOWN
        membership, not a room where nobody is listening: the Go response
        tags `members` `omitempty`, so the two are the same bytes. The gate
        must not read that as a resolved, authoritative empty audience."""
        roster = await resolve_channel_roster(
            _FakeFetcher(_MEMBERLESS, _AGENTS),
            _event("group:ghost"), "iron-fox",
        )
        assert roster is None

    async def test_malformed_member_list_resolves_nothing(self) -> None:
        """A members value that is not a list must not raise across the
        seam — the resolver promises it never does — and must not present
        itself as a resolved audience either."""
        roster = await resolve_channel_roster(
            _FakeFetcher({"id": "group:x", "members": "nope"}, _AGENTS),
            _event("group:x"), "iron-fox",
        )
        assert roster is None


class TestChannelRosterRecord:
    async def test_roster_is_hashable(self) -> None:
        """Frozen means usable as a value: PR A2 caches these per turn, and
        a record carrying a raw dict field cannot be a key or a set member."""
        roster = await resolve_channel_roster(
            _FakeFetcher(_CHANNEL, _AGENTS),
            _event("group:planning"), "iron-fox",
        )
        assert roster is not None
        assert hash(roster) == hash(roster)
        assert len({roster, roster}) == 1

    async def test_member_ids_is_computed_once(self) -> None:
        """The gate reads the audience per entry per turn; rebuilding the
        frozenset on each access makes that one allocation per entry."""
        roster = await resolve_channel_roster(
            _FakeFetcher(_CHANNEL, _AGENTS),
            _event("group:planning"), "iron-fox",
        )
        assert roster is not None
        assert roster.member_ids is roster.member_ids

    def test_roster_is_a_frozen_record(self) -> None:
        """The audience PR A2 reads must not be mutable per-turn state."""
        roster = ChannelRoster(
            channel_id="group:planning", channel_meta=_CHANNEL,
            members=(), directory=DirectoryStatus.RESOLVED,
        )
        assert roster.member_ids == frozenset()


class TestBuildRosterHardening:
    def test_non_list_members_yields_no_members(self) -> None:
        """``build_roster`` feeds an LLM prompt and promises never to raise
        across it — a members value of the wrong type is skipped, not
        iterated."""
        assert build_roster(
            {"id": "group:x", "members": "nope"}, _AGENTS,
            self_agent_id="iron-fox",
        ) == []

    def test_missing_members_key_yields_no_members(self) -> None:
        assert build_roster(
            _MEMBERLESS, _AGENTS, self_agent_id="iron-fox",
        ) == []

