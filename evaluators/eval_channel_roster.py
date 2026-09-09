"""ISSUE-0132 (v0.3.16 PR A2) — an in-process channel-roster fetcher.

The persona runtime's audience check
(``agents/persona_runtime/audience.py``) resolves who is in a room from
a ``ChannelRosterFetcher`` — in production two HTTP calls to the
orchestrator. The RFC 0044 eval driver has no orchestrator to fetch
from, so without a fetcher every source room resolves *unknown* and the
audience seed could only ever record ``withhold-unknown-fetch-failed``:
a recipe that can never exercise the case it exists for. Scope lock 3
names this seam as the precondition for the audience seed existing at
all.

:class:`InProcessChannelRoster` is that fetcher, backed by a static
``channel_id -> [member ids]`` map the recipe declares
(``setup.rosters``). It conforms structurally to the runtime's
``ChannelRosterFetcher`` Protocol — ``fetch_members`` returns the
channel-response shape the runtime parses, ``fetch_directory`` returns
an empty directory — so the driver can wire it with
``agent.set_roster_fetcher(...)``.

An **undeclared** channel returns ``None``, the production
members-half-missed shape: that is how a recipe exercises the
fetch-failed cause deliberately rather than by omission.

Kept **pure** (no ``agents`` import), like the assertion core and the
history seam beside it, so it is unit-testable in isolation; the driver
owns the wiring.

Determinism (RFC 0044 §D): the map is static for the whole run, so
membership cannot drift between a record and its replays. The
assertion-time membership snapshot RFC 0035 would need is out of scope
for the whole release (scope lock 2), so a static map is not a
simplification here — it is the shipped semantics.

The directory half returns ``None``, not ``[]``, and the distinction is
load-bearing: the runtime maps ``None`` to ``DirectoryStatus.MISSED``
(no roster section) and a list — ``[]`` included — to ``RESOLVED``,
which renders the section with bare ids. ``None`` is therefore what
reproduces the fleet's posture under auth today ([ISSUE-0140] — the
authenticated ``/api/v1/agents`` half ``401``s, and the HTTP fetcher
returns ``None``): a group turn in an eval renders no roster section and
the audience check still resolves, which is the composition PR A1 split
the fetcher to make possible. Returning ``[]`` would model the opposite
branch and, worse for the audience seed, name every member of the room
— Bob included — in the prompt whose whole subject is whether the
persona knows who is listening.
"""

from __future__ import annotations

from typing import Any

__all__ = ["InProcessChannelRoster"]


class InProcessChannelRoster:
    """A ``ChannelRosterFetcher`` over a static membership map."""

    def __init__(self, rosters: dict[str, list[str]] | None = None) -> None:
        self._rosters = {
            channel_id: list(members)
            for channel_id, members in (rosters or {}).items()
        }
        #: Every ``fetch_members`` call, in order — the driver reports the
        #: count so a recipe can pin the scope-lock-2 fetch bound.
        self.calls: list[str] = []

    async def fetch_members(self, channel_id: str) -> dict[str, Any] | None:
        """The public half: the channel's declared membership, or ``None``
        for a channel the recipe did not declare."""
        self.calls.append(channel_id)
        members = self._rosters.get(channel_id)
        if members is None:
            return None
        return {
            "id": channel_id,
            "name": channel_id,
            "members": [{"id": member} for member in members],
        }

    async def fetch_directory(self) -> list[dict[str, Any]] | None:
        """The authenticated half: MISSED, as the fleet sees it under auth.

        ``None``, not ``[]`` — see the module docstring: only ``None``
        maps to ``DirectoryStatus.MISSED`` and suppresses the section.
        """
        return None
