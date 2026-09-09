"""Shared fixtures for the channel-roster test modules.

The roster's resolve/inject seam is covered by two modules
(``test_channel_roster_resolution.py`` and
``test_channel_roster_injection.py``); the room payloads and the fetcher
double they both drive live here so the two cannot drift apart.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

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
#: What the orchestrator actually sends for a room with nobody in it: the
#: Go response tags `members` `omitempty`, so an empty list is omitted.
_MEMBERLESS = {"id": "group:ghost", "name": "ghost", "description": "nobody"}


def _event(channel_id: str | None) -> MagicMock:
    """A stand-in AgentEvent — resolution only reads ``channel_id``."""
    event = MagicMock()
    event.channel_id = channel_id
    return event


class _FakeFetcher:
    """One double for every case the resolver has to survive.

    Pass an ``Exception`` instance as either half to make that half raise
    instead of return; pass ``order`` to record when the members call ran
    relative to another event in the same turn. Records each half
    separately, so a test can assert *that* a turn resolved a roster,
    *which* room it resolved, and whether it spent the authenticated
    directory round trip.
    """

    def __init__(
        self,
        members: dict[str, Any] | Exception | None = None,
        agents: list[dict[str, Any]] | Exception | None = None,
        *,
        order: list[str] | None = None,
    ) -> None:
        self._members = members
        self._agents = agents
        self._order = order
        self.calls: list[str] = []
        self.directory_calls: int = 0

    async def fetch_members(self, channel_id: str):  # noqa: ANN201
        if self._order is not None:
            self._order.append("roster")
        self.calls.append(channel_id)
        if isinstance(self._members, Exception):
            raise self._members
        return self._members

    async def fetch_directory(self):  # noqa: ANN201
        self.directory_calls += 1
        if isinstance(self._agents, Exception):
            raise self._agents
        return self._agents


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
