"""Channel-roster context tier (v0.3.7 conversation test-findings, F-4).

In a group channel with no shared world-state, personas confabulate who
is present and what each other does. This module builds a **channel
roster** — the channel description plus each member's name and role — so
the per-event context carries a shared, consistent view of the room.

Sourced from the orchestrator in two calls (no N+1): `GET
/api/v1/channels/{id}` for membership and `GET /api/v1/agents` for the
id→name/role directory.

Slice A (this module) is pure + self-contained: the :func:`build_roster`
join, the :func:`render_roster_section` renderer, and
:class:`HttpChannelRosterFetcher`. Slice B wires
:func:`render_roster_section` into the budgeted ``_inject_memory_context``
path. Nothing here is imported by the runtime yet — landing it is a
zero-behaviour-change step.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import aiohttp

from ..channel_history_fetcher import DEFAULT_REQUEST_TIMEOUT_SECONDS
from ..memory.working import ContextSection, estimate_tokens

logger = logging.getLogger(__name__)

#: Working-memory section name (slice B clears + re-adds it per event).
ROSTER_SECTION_NAME = "channel_roster"
#: Priority above the relationship tier (8): "who is in this room" is
#: foundational context the other tiers build on. Pinned here so slice B's
#: allocate-loop placement and this constant cannot drift apart.
ROSTER_SECTION_PRIORITY = 9


@dataclass(frozen=True)
class RosterMember:
    """One channel member, joined with its agent-directory identity."""

    id: str
    name: str
    role: str
    is_self: bool


def build_roster(
    channel_meta: dict[str, Any],
    agents: list[dict[str, Any]],
    *,
    self_agent_id: str,
) -> list[RosterMember]:
    """Join a channel's members with the agent directory.

    Membership order is preserved (it is the room's declared order).
    Members absent from ``agents`` fall back to ``name=id, role=""`` so an
    unregistered or task-only participant still appears. Malformed member
    entries (non-dict, or missing ``id``) are skipped defensively — this
    feeds an LLM prompt, never raise across it.
    """
    directory = {
        a["id"]: a
        for a in agents
        if isinstance(a, dict) and isinstance(a.get("id"), str)
    }
    roster: list[RosterMember] = []
    for member in channel_meta.get("members", []):
        if not isinstance(member, dict):
            continue
        mid = member.get("id")
        if not isinstance(mid, str) or not mid:
            continue
        entry = directory.get(mid, {})
        roster.append(
            RosterMember(
                id=mid,
                name=str(entry.get("name") or mid),
                role=str(entry.get("role") or ""),
                is_self=(mid == self_agent_id),
            ),
        )
    return roster


def render_roster_section(
    channel_meta: dict[str, Any],
    members: list[RosterMember],
) -> ContextSection | None:
    """Render the roster as a :class:`ContextSection`, or ``None`` when the
    channel has no members.

    The viewing persona's line carries a ``(you)`` marker so the model does
    not refer to itself in the third person.
    """
    if not members:
        return None
    name = str(channel_meta.get("name") or channel_meta.get("id") or "")
    description = str(channel_meta.get("description") or "").strip()
    header = f"Channel #{name}" if name else "Channel"
    if description:
        header = f"{header} — {description}"
    lines = [header, "Participants:"]
    for m in members:
        line = f"- {m.name} — {m.role}" if m.role else f"- {m.name}"
        if m.is_self:
            line = f"{line} (you)"
        lines.append(line)
    content = "\n".join(lines)
    return ContextSection(
        name=ROSTER_SECTION_NAME,
        content=content,
        priority=ROSTER_SECTION_PRIORITY,
        token_count=estimate_tokens(content, accurate=True),
    )


class HttpChannelRosterFetcher:
    """Fetch a channel's roster inputs from the orchestrator over aiohttp.

    Two GETs — ``/api/v1/channels/{id}`` (membership) and
    ``/api/v1/agents`` (the id→name/role directory). Returns
    ``(channel_meta, agents)`` on success or ``None`` on any HTTP error /
    transport failure / unusable body — never raises across the seam (the
    slice-B injection caller degrades to no roster section). The caller
    owns the ``aiohttp`` session.
    """

    def __init__(
        self,
        *,
        session: aiohttp.ClientSession,
        orchestrator_url: str,
        timeout: aiohttp.ClientTimeout | None = None,
    ) -> None:
        self._session = session
        self._base = orchestrator_url.rstrip("/")
        self._timeout = timeout or aiohttp.ClientTimeout(
            total=DEFAULT_REQUEST_TIMEOUT_SECONDS,
        )

    async def _get_json(self, url: str) -> Any | None:
        try:
            async with self._session.get(url, timeout=self._timeout) as resp:
                if resp.status >= 400:
                    body = await resp.text()
                    logger.warning(
                        "channels: roster fetch %s returned HTTP %d: %s",
                        url, resp.status, body[:256],
                    )
                    return None
                return await resp.json()
        except Exception as exc:
            logger.warning("channels: roster fetch %s failed: %s", url, exc)
            return None

    async def fetch(
        self, channel_id: str,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]] | None:
        cid = quote(channel_id, safe="")
        channel_meta = await self._get_json(
            f"{self._base}/api/v1/channels/{cid}",
        )
        if not isinstance(channel_meta, dict):
            return None
        agents = await self._get_json(f"{self._base}/api/v1/agents")
        if not isinstance(agents, list):
            return None
        return channel_meta, agents
