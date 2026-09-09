"""Roster resolution as an audience rail (v0.3.16 PR A1, ISSUE-0132).

The RFC 0037 §D gate decides what a persona may say from the acting
channel's classification alone; it cannot ask who is in the room, because
the roster is fetched *after* the gate has already run and only for group
channels. PR A1 lays the rail the audience check (PR A2) will read:

* the fetcher's two GETs are split, so a directory ``401`` (the fleet's
  authenticated ``/api/v1/agents``) no longer throws away the public
  channel-members half — the member set survives;
* resolution moves **ahead** of the gate and runs for every
  channel-anchored turn, DMs included — a DM with Bob is an audience;
* on a DM turn the roster is resolved **for the gate only**: no roster
  section is injected, or PR A2's byte-identity claim fails on the first
  DM (scope lock 3, review F-4).

Nothing reads the resolved roster yet — the gate is unchanged and no
prompt moves. These tests pin that: the rail exists, and the prompt does
not know it.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

from agents.memory.working import WorkingMemory
from agents.persona import create_persona_agent
from agents.persona_runtime.channel_roster import (
    ROSTER_SECTION_NAME,
    ChannelRoster,
    inject_channel_roster,
    resolve_channel_roster,
)
from agents.persona_types import AgentEvent, EventType

from ._persona_test_helpers import _PERSONA_CONFIG, _make_client

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
_DM = {
    "id": "dm:alice:iron-fox",
    "name": "alice ↔ iron-fox",
    "members": [{"id": "alice"}, {"id": "iron-fox"}],
}


def _event(channel_id: str | None) -> MagicMock:
    """A stand-in AgentEvent — resolution only reads ``channel_id``."""
    event = MagicMock()
    event.channel_id = channel_id
    return event


class _FakeFetcher:
    """Records each half separately, so a test can assert *that* a turn
    resolved a roster, *which* room it resolved, and whether it spent the
    authenticated directory round trip."""

    def __init__(
        self,
        members: dict[str, Any] | None,
        agents: list[dict[str, Any]] | None = None,
    ) -> None:
        self._members = members
        self._agents = agents
        self.calls: list[str] = []
        self.directory_calls: int = 0

    async def fetch_members(self, channel_id: str):  # noqa: ANN201
        self.calls.append(channel_id)
        return self._members

    async def fetch_directory(self):  # noqa: ANN201
        self.directory_calls += 1
        return self._agents


class _RaisingFetcher:
    async def fetch_members(self, channel_id: str):  # noqa: ANN201
        raise RuntimeError("orchestrator unreachable")

    async def fetch_directory(self):  # noqa: ANN201
        raise RuntimeError("orchestrator unreachable")


class _RaisingDirectoryFetcher:
    """Members fine, directory blows up — the audience must survive it."""

    async def fetch_members(self, channel_id: str):  # noqa: ANN201
        return _CHANNEL

    async def fetch_directory(self):  # noqa: ANN201
        raise RuntimeError("directory unreachable")


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
        assert roster.has_display_names is True

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
        assert roster.has_display_names is False

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

    async def test_group_turn_spends_both_halves(self) -> None:
        fetcher = _FakeFetcher(_CHANNEL, _AGENTS)
        await resolve_channel_roster(
            fetcher, _event("group:planning"), "iron-fox",
        )
        assert fetcher.calls == ["group:planning"]
        assert fetcher.directory_calls == 1

    async def test_thread_turn_resolves_members_only(self) -> None:
        """``thread:`` is the third channel prefix
        (``internal/channels/identifiers.go``). It has an audience like any
        other room and, like a DM, no roster section."""
        fetcher = _FakeFetcher(_DM, _AGENTS)
        roster = await resolve_channel_roster(
            fetcher, _event("thread:planning:abc"), "iron-fox",
        )
        assert roster is not None
        assert fetcher.directory_calls == 0

    async def test_raising_directory_still_yields_the_audience(self) -> None:
        """The directory half is best-effort: a raise inside it must cost
        display names, not the member set."""
        roster = await resolve_channel_roster(
            _RaisingDirectoryFetcher(), _event("group:planning"), "iron-fox",
        )
        assert roster is not None
        assert roster.member_ids == {"ember-owl", "iron-fox", "nova-sparrow"}
        assert roster.has_display_names is False

    async def test_no_fetcher_resolves_nothing(self) -> None:
        assert await resolve_channel_roster(
            None, _event("group:planning"), "iron-fox",
        ) is None

    async def test_fetch_failure_resolves_nothing(self) -> None:
        assert await resolve_channel_roster(
            _FakeFetcher(None), _event("group:planning"), "iron-fox",
        ) is None

    async def test_raising_fetch_is_non_fatal(self, caplog: Any) -> None:
        import logging

        with caplog.at_level(logging.WARNING):
            roster = await resolve_channel_roster(
                _RaisingFetcher(), _event("group:planning"), "iron-fox",
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


# ─── inject_channel_roster (now fed the resolved roster) ──────


#: The group-roster section, verbatim. PR A1 only *moves* the resolution;
#: this string is the guard that it did not also rewrite the prompt (the
#: plan's risk row: "A1 changes roster prompt text while 'only moving' it").
_EXPECTED_GROUP_SECTION = (
    "Channel #planning — engineering + product planning discussion\n"
    "Participants:\n"
    "- Ember Owl — Engineering leadership\n"
    "- Iron Fox — Staff engineering (you)\n"
    "- Nova Sparrow — Product management"
)


class TestInjectChannelRoster:
    async def test_group_section_is_byte_identical(self) -> None:
        wm = WorkingMemory(max_tokens=8192)
        roster = await resolve_channel_roster(
            _FakeFetcher(_CHANNEL, _AGENTS),
            _event("group:planning"), "iron-fox",
        )
        inject_channel_roster(wm, roster)
        section = wm.get_section(ROSTER_SECTION_NAME)
        assert section is not None
        assert section.content == _EXPECTED_GROUP_SECTION

    async def test_dm_roster_injects_no_section(self) -> None:
        """Resolved for the gate, invisible to the prompt (review F-4)."""
        wm = WorkingMemory(max_tokens=8192)
        roster = await resolve_channel_roster(
            _FakeFetcher(_DM, _AGENTS),
            _event("dm:alice:iron-fox"), "iron-fox",
        )
        assert roster is not None  # the gate has an audience …
        inject_channel_roster(wm, roster)
        assert wm.get_section(ROSTER_SECTION_NAME) is None  # … the prompt does not

    async def test_directory_miss_injects_no_section(self) -> None:
        """The members half survives for the audience, but the section still
        needs display names — rendering bare ids would be a prompt change,
        and ISSUE-0140 is off this release's path (scope lock 3)."""
        wm = WorkingMemory(max_tokens=8192)
        roster = await resolve_channel_roster(
            _FakeFetcher(_CHANNEL, None),
            _event("group:planning"), "iron-fox",
        )
        assert roster is not None
        inject_channel_roster(wm, roster)
        assert wm.get_section(ROSTER_SECTION_NAME) is None

    async def test_unresolved_roster_injects_no_section(self) -> None:
        wm = WorkingMemory(max_tokens=8192)
        inject_channel_roster(wm, None)
        assert wm.get_section(ROSTER_SECTION_NAME) is None

    async def test_stale_section_cleared_on_a_later_dm_turn(self) -> None:
        wm = WorkingMemory(max_tokens=8192)
        group = await resolve_channel_roster(
            _FakeFetcher(_CHANNEL, _AGENTS),
            _event("group:planning"), "iron-fox",
        )
        inject_channel_roster(wm, group)
        assert wm.get_section(ROSTER_SECTION_NAME) is not None
        dm = await resolve_channel_roster(
            _FakeFetcher(_DM, _AGENTS),
            _event("dm:alice:iron-fox"), "iron-fox",
        )
        inject_channel_roster(wm, dm)
        assert wm.get_section(ROSTER_SECTION_NAME) is None

    async def test_stale_section_cleared_when_resolution_fails(self) -> None:
        wm = WorkingMemory(max_tokens=8192)
        inject_channel_roster(wm, await resolve_channel_roster(
            _FakeFetcher(_CHANNEL, _AGENTS),
            _event("group:planning"), "iron-fox",
        ))
        assert wm.get_section(ROSTER_SECTION_NAME) is not None
        inject_channel_roster(wm, None)
        assert wm.get_section(ROSTER_SECTION_NAME) is None


# ─── the wiring: resolution runs ahead of the §D gate ─────────


class _OrderRecordingFetcher:
    def __init__(self, order: list[str]) -> None:
        self._order = order
        self.calls: list[str] = []

    async def fetch_members(self, channel_id: str):  # noqa: ANN201
        self._order.append("roster")
        self.calls.append(channel_id)
        return _CHANNEL

    async def fetch_directory(self):  # noqa: ANN201
        return _AGENTS


class TestInjectionPathWiring:
    async def test_roster_resolution_precedes_the_gate(self) -> None:
        """The gate cannot consult a roster the turn resolves after it —
        the hard edge the plan's dependency graph draws from A1 to A2."""
        from agents.persona_runtime import memory_context

        order: list[str] = []
        agent = create_persona_agent(
            agent_id="iron-fox", config=_PERSONA_CONFIG,
            llm_client=_make_client(),
        )
        await agent.initialize_memory()
        agent.set_roster_fetcher(_OrderRecordingFetcher(order))
        real_gate = memory_context.TurnInjectionGate

        def _spy(*args: Any, **kwargs: Any):  # noqa: ANN202
            order.append("gate")
            return real_gate(*args, **kwargs)

        event = AgentEvent(
            event_type=EventType.CHANNEL_MESSAGE,
            payload={"content": "hello"},
            sender_id="alice",
            channel_id="group:planning",
        )
        with patch.object(memory_context, "TurnInjectionGate", _spy):
            await agent._inject_memory_context(event)
        await agent.close_memory()
        assert order == ["roster", "gate"]

    async def test_dm_turn_resolves_a_roster_and_injects_no_section(self) -> None:
        agent = create_persona_agent(
            agent_id="iron-fox", config=_PERSONA_CONFIG,
            llm_client=_make_client(),
        )
        await agent.initialize_memory()
        fetcher = _FakeFetcher(_DM, _AGENTS)
        agent.set_roster_fetcher(fetcher)

        event = AgentEvent(
            event_type=EventType.CHANNEL_MESSAGE,
            payload={"content": "hello"},
            sender_id="alice",
            channel_id="dm:alice:iron-fox",
        )
        await agent._inject_memory_context(event)
        await agent.close_memory()

        assert fetcher.calls == ["dm:alice:iron-fox"]
        assert agent._working_memory.get_section(ROSTER_SECTION_NAME) is None

    async def test_group_turn_still_injects_the_same_section(self) -> None:
        agent = create_persona_agent(
            agent_id="iron-fox", config=_PERSONA_CONFIG,
            llm_client=_make_client(),
        )
        await agent.initialize_memory()
        agent.set_roster_fetcher(_FakeFetcher(_CHANNEL, _AGENTS))

        event = AgentEvent(
            event_type=EventType.CHANNEL_MESSAGE,
            payload={"content": "hello"},
            sender_id="alice",
            channel_id="group:planning",
        )
        await agent._inject_memory_context(event)
        section = agent._working_memory.get_section(ROSTER_SECTION_NAME)
        await agent.close_memory()
        assert section is not None
        assert section.content == _EXPECTED_GROUP_SECTION

    async def test_roster_is_a_frozen_record(self) -> None:
        """The audience PR A2 reads must not be mutable per-turn state."""
        roster = ChannelRoster(
            channel_id="group:planning", channel_meta=_CHANNEL,
            members=(), has_display_names=True,
        )
        assert roster.member_ids == frozenset()
