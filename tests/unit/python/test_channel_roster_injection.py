"""The resolved roster reaching (and not reaching) the prompt.

PR A1 moved the roster fetch ahead of the RFC 0037 §D gate without moving
the prompt: only a group turn whose agent directory answered injects a
section, and it is byte-identical to v0.3.15. These tests pin that, and
pin the wiring order the audience check (PR A2) depends on — the roster
resolves before the gate runs, concurrently with the tier recalls.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from agents.memory.working import WorkingMemory
from agents.persona import create_persona_agent
from agents.persona_runtime.channel_roster import (
    ROSTER_SECTION_NAME,
    DirectoryStatus,
    inject_channel_roster,
    resolve_channel_roster,
)
from agents.persona_types import AgentEvent, EventType

from ._channel_roster_helpers import (
    _AGENTS,
    _CHANNEL,
    _DM,
    _EXPECTED_GROUP_SECTION,
    _event,
    _FakeFetcher,
)
from ._persona_test_helpers import _PERSONA_CONFIG, _make_client

# ─── inject_channel_roster (now fed the resolved roster) ──────




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
        needs the directory — rendering bare ids under a ``401`` would be a
        prompt change, and ISSUE-0140 is off this release's path."""
        wm = WorkingMemory(max_tokens=8192)
        roster = await resolve_channel_roster(
            _FakeFetcher(_CHANNEL, None),
            _event("group:planning"), "iron-fox",
        )
        assert roster is not None
        inject_channel_roster(wm, roster)
        assert wm.get_section(ROSTER_SECTION_NAME) is None

    async def test_empty_directory_renders_bare_ids(self) -> None:
        """A fleet with no registered agents answers ``[]`` — a RESOLVED
        directory that supplies no names. It renders the section with bare
        ids, exactly as it did before PR A1: the guard is 'the directory
        answered', not 'the members have display names'."""
        wm = WorkingMemory(max_tokens=8192)
        roster = await resolve_channel_roster(
            _FakeFetcher(_CHANNEL, []),
            _event("group:planning"), "iron-fox",
        )
        assert roster is not None
        assert roster.directory is DirectoryStatus.RESOLVED
        inject_channel_roster(wm, roster)
        section = wm.get_section(ROSTER_SECTION_NAME)
        assert section is not None
        assert section.content == (
            "Channel #planning — engineering + product planning discussion\n"
            "Participants:\n"
            "- ember-owl\n"
            "- iron-fox (you)\n"
            "- nova-sparrow"
        )

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


class TestInjectionPathWiring:
    async def test_roster_resolution_precedes_the_gate(self) -> None:
        """The gate cannot consult a roster the turn resolves after it —
        the hard edge the plan's dependency graph draws from A1 to A2.
        The fetch is issued concurrently with the tier recalls, so this
        pins the ORDER (roster before gate), not the issue point."""
        from agents.persona_runtime import memory_context

        order: list[str] = []
        agent = create_persona_agent(
            agent_id="iron-fox", config=_PERSONA_CONFIG,
            llm_client=_make_client(),
        )
        await agent.initialize_memory()
        agent.set_roster_fetcher(_FakeFetcher(_CHANNEL, _AGENTS, order=order))
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

    async def test_a_failing_recall_does_not_orphan_the_roster_fetch(self) -> None:
        """The fetch is started before the recalls, so a recall that raises
        must still leave the task awaited — an orphaned task logs "Task was
        destroyed but it is pending" at GC. The recall here raises before
        ever yielding, so the task only runs at all if something awaits it."""
        from agents.persona_runtime import memory_context

        agent = create_persona_agent(
            agent_id="iron-fox", config=_PERSONA_CONFIG,
            llm_client=_make_client(),
        )
        await agent.initialize_memory()
        fetcher = _FakeFetcher(_CHANNEL, _AGENTS)
        agent.set_roster_fetcher(fetcher)
        event = AgentEvent(
            event_type=EventType.CHANNEL_MESSAGE,
            payload={"content": "hello"},
            sender_id="alice",
            channel_id="group:planning",
        )
        with patch.object(
            memory_context, "recall_relationship_summary",
            side_effect=RuntimeError("relationship backend down"),
        ), pytest.raises(RuntimeError):
            await agent._inject_memory_context(event)
        # Asserted BEFORE any further await: a task merely left pending
        # would not have run yet, and a later await would hide that by
        # letting it run anyway.
        assert fetcher.calls == ["group:planning"]
        await agent.close_memory()
