"""RFC 0044 / ISSUE-0132 — the eval driver's in-process roster seam."""

from __future__ import annotations

import pytest

from agents.persona_runtime.audience import (
    AUDIENCE_SHADOW,
    AudienceVerdict,
    resolve_turn_audience,
)
from agents.persona_runtime.channel_roster import (
    DirectoryStatus,
    resolve_channel_roster,
)
from agents.persona_types import AgentEvent, EventType
from evaluators.eval_channel_roster import InProcessChannelRoster

pytestmark = pytest.mark.asyncio


async def test_declared_channel_returns_the_runtime_response_shape() -> None:
    roster = InProcessChannelRoster({"group:standup": ["alice", "bob"]})
    assert await roster.fetch_members("group:standup") == {
        "id": "group:standup",
        "name": "group:standup",
        "members": [{"id": "alice"}, {"id": "bob"}],
    }
    assert roster.calls == ["group:standup"]


async def test_undeclared_channel_is_the_members_half_missed_shape() -> None:
    """The deliberate fetch-failed lever: a recipe declares the rooms it
    means to resolve, and anything else is *unknown*, not empty."""
    assert await InProcessChannelRoster().fetch_members("group:ghost") is None


async def test_the_directory_is_missed_like_the_fleet_under_auth() -> None:
    """``None``, not ``[]`` — and the distinction is the whole point.
    Under auth the fleet's ``/api/v1/agents`` half ``401``s and the HTTP
    fetcher returns ``None``, which the runtime maps to
    ``DirectoryStatus.MISSED``: no roster section.  ``[]`` maps to
    ``RESOLVED``, which renders the section with bare ids — the opposite
    branch, and one that would name every member of the room in the
    prompt of the seed asking whether the persona knows who is
    listening."""
    assert await InProcessChannelRoster().fetch_directory() is None


async def test_the_seam_renders_no_roster_section() -> None:
    """The consequence, driven through the real resolver rather than
    asserted about the return value."""
    event = AgentEvent(
        event_type=EventType.CHANNEL_MESSAGE, payload={},
        channel_id="group:standup",
    )
    fetcher = InProcessChannelRoster({"group:standup": ["alice", "iron-fox"]})
    resolved = await resolve_channel_roster(fetcher, event, "iron-fox")

    assert resolved is not None
    assert resolved.directory is DirectoryStatus.MISSED


async def test_the_runtime_resolves_a_roster_through_this_seam() -> None:
    """Structural conformance, driven rather than asserted: the real
    resolver and the real audience resolution both accept it."""

    event = AgentEvent(
        event_type=EventType.CHANNEL_MESSAGE, payload={},
        channel_id="group:standup",
    )
    fetcher = InProcessChannelRoster({
        "group:standup": ["alice", "bob", "iron-fox"],
        "dm:alice:iron-fox": ["alice", "iron-fox"],
    })
    resolved = await resolve_channel_roster(fetcher, event, "iron-fox")
    assert resolved is not None
    assert resolved.member_ids == frozenset({"alice", "bob", "iron-fox"})

    class _Entry:
        protection_level = "internal"
        source_channel_id = "dm:alice:iron-fox"

    audience = await resolve_turn_audience(
        fetcher, resolved, mode=AUDIENCE_SHADOW,
        acting_channel_id="group:standup", acting_classification="internal",
        candidates=(("facts", [_Entry()]),), agent_id="iron-fox",
    )
    assert audience is not None
    assert audience.verdict(
        tier="facts", protection_level="internal",
        source_channel_id="dm:alice:iron-fox",
    ) is AudienceVerdict.WITHHOLD_DISJOINT
