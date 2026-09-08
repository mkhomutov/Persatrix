"""Channel-roster context tier (v0.3.7 conversation test-findings, F-4).

In a group channel with no shared world-state, personas confabulate who
is present and what each other does. This module builds a **channel
roster** — the channel description plus each member's name and role — so
the per-event context carries a shared, consistent view of the room.

Sourced from the orchestrator, never per member (no N+1): `GET
/api/v1/channels/{id}` for membership and `GET /api/v1/agents` for the
id→name/role directory.

Since v0.3.16 PR A1 the module also carries the **audience rail** that
ISSUE-0132's egress check reads. Two things changed, both structural:

* the two GETs are **independent halves of the seam**, requested and
  failing separately. Membership is public and load-bearing — the member
  set *is* the audience. The agent directory is authenticated, answers
  `401` to the fleet under auth ([ISSUE-0140]), and supplies display
  names for one thing only: the group-channel prompt section. So a turn
  that renders no section never asks for it, and a directory that misses
  costs names, not membership.
* resolution (:func:`resolve_channel_roster`) is separate from injection
  (:func:`inject_channel_roster`) and runs for **every turn that names a
  channel** — group, DM (`dm:`) and thread (`thread:`), the three
  prefixes `internal/channels/identifiers.go` defines. A DM with Bob is
  an audience, and it is the turn ISSUE-0132 is actually about. Only a
  group turn with a live directory injects a section, so no prompt moves.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol
from urllib.parse import quote

import aiohttp

from ..channel_history_fetcher import DEFAULT_REQUEST_TIMEOUT_SECONDS
from ..memory.working import ContextSection, estimate_tokens

if TYPE_CHECKING:
    from ..memory.working import WorkingMemory
    from ..persona_types import AgentEvent

logger = logging.getLogger(__name__)

#: Working-memory section name (cleared + re-added per event).
ROSTER_SECTION_NAME: str = "channel_roster"
#: Priority above the relationship tier (8): "who is in this room" is
#: foundational context the other tiers build on. Pinned here so the
#: injection point and this constant cannot drift apart.
ROSTER_SECTION_PRIORITY: int = 9

#: The one channel prefix that renders a roster *section*
#: (``internal/channels/identifiers.go`` also defines ``dm:`` and
#: ``thread:``, which resolve an audience and show it to nobody). Read by
#: both :func:`resolve_channel_roster` — which skips the directory half
#: when no section can render — and :func:`inject_channel_roster`, so the
#: fetch and the render cannot disagree about what needs display names.
GROUP_CHANNEL_PREFIX: str = "group:"


@dataclass(frozen=True)
class RosterMember:
    """One channel member, joined with its agent-directory identity."""

    id: str
    name: str
    role: str
    is_self: bool


@dataclass(frozen=True)
class ChannelRoster:
    """One turn's resolved view of the acting room (v0.3.16 PR A1).

    The audience [ISSUE-0132] checks is :attr:`member_ids` — a
    type-agnostic id set, because a peer persona in the acting room that
    was not in the source room is audience too (scope lock 3). Frozen:
    it is a per-turn fact, read by the gate and the prompt section, owned
    by neither.

    Attributes:
        channel_id: The acting channel this roster describes.
        channel_meta: The raw channel object, for the prompt section's
            name and description.
        members: The joined membership, in the room's declared order.
        directory_ok: Whether the members carry directory display names.
            ``False`` when the directory half missed **or was never
            requested** — a DM or thread turn renders no section, so it
            does not spend the authenticated round trip. Either way the
            names fell back to ids: fine for an audience, not for a
            prompt section (see :func:`inject_channel_roster`).
    """

    channel_id: str
    channel_meta: dict[str, Any]
    members: tuple[RosterMember, ...]
    directory_ok: bool

    @property
    def member_ids(self) -> frozenset[str]:
        """The audience: every member id, self included."""
        return frozenset(m.id for m in self.members)


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
        # Non-compressible: the roster is a structured membership list, not
        # recalled prose. Summarizing it under budget pressure could drop
        # members or mangle roles — reintroducing the F-4 confabulation this
        # tier exists to prevent. The lower conversation/history tiers absorb
        # budget pressure first.
        compressible=False,
    )


class HttpChannelRosterFetcher:
    """Fetch a channel's roster inputs from the orchestrator over aiohttp.

    Two GETs, exposed as two seam methods so the caller can request —
    and lose — them independently (v0.3.16 PR A1):

    * :meth:`fetch_members` — ``/api/v1/channels/{id}``. Public, and
      load-bearing: the member set is the audience.
    * :meth:`fetch_directory` — ``/api/v1/agents``. Authenticated, so it
      is the half that ``401``s for the fleet under auth
      ([ISSUE-0140]); it supplies display names for the group-channel
      prompt section and nothing else.

    Each returns ``None`` on any HTTP error / transport failure /
    unusable body, and never raises across the seam. An empty directory
    (``[]``) is a *success* and stays distinguishable from a miss: it
    still renders a roster (every member falls back to its id) where a
    miss renders none. The caller owns the ``aiohttp`` session.
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

    async def fetch_members(self, channel_id: str) -> dict[str, Any] | None:
        """The public half: the channel's declared membership."""
        cid = quote(channel_id, safe="")
        channel_meta = await self._get_json(
            f"{self._base}/api/v1/channels/{cid}",
        )
        return channel_meta if isinstance(channel_meta, dict) else None

    async def fetch_directory(self) -> list[dict[str, Any]] | None:
        """The authenticated half: the id→name/role agent directory."""
        agents = await self._get_json(f"{self._base}/api/v1/agents")
        return agents if isinstance(agents, list) else None


class ChannelRosterFetcher(Protocol):
    """Seam the injection path depends on (``server_persona`` sets an
    :class:`HttpChannelRosterFetcher`; tests inject a fake).

    Two halves, because they are wanted at different times: every
    channel turn needs the membership, only a group turn needs the
    names. Each returns ``None`` when its half missed — see
    :class:`HttpChannelRosterFetcher`."""

    async def fetch_members(
        self, channel_id: str,
    ) -> dict[str, Any] | None: ...

    async def fetch_directory(self) -> list[dict[str, Any]] | None: ...


async def resolve_channel_roster(
    fetcher: ChannelRosterFetcher | None,
    event: AgentEvent,
    agent_id: str,
) -> ChannelRoster | None:
    """Resolve who is in the acting channel, for **any** turn that names
    one (v0.3.16 PR A1, [ISSUE-0132] scope lock 3).

    Runs *ahead* of the RFC 0037 §D gate, which is the whole point: the
    gate cannot ask who is listening if the roster arrives after it has
    already decided. DMs and threads resolve too — a DM with Bob is an
    audience, and the DM is the turn ISSUE-0132 is actually about — but
    they inject no section (see :func:`inject_channel_roster`).

    The predicate is the event's ``channel_id``, not the gate's
    ``CHANNEL_ACTING_EVENT_TYPES``. That is a requirement, not a
    preference: before PR A1 *any* event carrying a ``group:`` channel id
    resolved and rendered a roster, whatever its event type, so narrowing
    to the gate's two channel-anchored types would delete the section
    from every other one — a prompt change, which is exactly what this PR
    must not make. It also keeps "which room is this?" independent of
    "which classification do we act at?".

    The **directory** half is spent only where its display names can be
    used — a group channel, the one shape that renders a section. A DM or
    thread turn therefore costs one round trip, not two, and does not
    trip the fleet's authenticated ``401`` ([ISSUE-0140]) once per turn.

    Returns ``None`` — never raises — when there is no channel, no wired
    fetcher, or the members half missed. A resolved roster whose
    ``directory_ok`` is ``False`` still carries the member ids.

    *Cost*: one round trip per channel turn, two on a group turn, where
    before PR A1 only group turns paid anything. The per-source-room
    fetches the audience check adds are PR A2's, cached per turn.
    """
    channel_id = getattr(event, "channel_id", None)
    if not isinstance(channel_id, str) or not channel_id:
        return None
    if fetcher is None:
        return None
    # Each half is guarded on its own, which is the point of splitting
    # them: losing the membership loses the audience, losing the names
    # loses only the prompt section's cosmetics.
    try:
        channel_meta = await fetcher.fetch_members(channel_id)
    except Exception:
        logger.warning(
            "channels: roster resolution failed for %s; skipping",
            channel_id, exc_info=True,
        )
        return None
    if channel_meta is None:
        return None
    agents: list[dict[str, Any]] | None = None
    if channel_id.startswith(GROUP_CHANNEL_PREFIX):
        try:
            agents = await fetcher.fetch_directory()
        except Exception:
            logger.warning(
                "channels: roster directory fetch failed for %s; "
                "falling back to member ids", channel_id, exc_info=True,
            )
    return ChannelRoster(
        channel_id=channel_id,
        channel_meta=channel_meta,
        members=tuple(
            build_roster(channel_meta, agents or [], self_agent_id=agent_id),
        ),
        directory_ok=agents is not None,
    )


def inject_channel_roster(
    working_memory: WorkingMemory,
    roster: ChannelRoster | None,
) -> None:
    """Inject the resolved roster as a **group**-channel prompt section (F-4).

    Clears any stale roster section first (so a roster from a prior group
    event does not linger on a later DM turn), then adds the rendered
    section only when the turn is a ``group:`` channel *and* the agent
    directory resolved. Both conditions preserve the prompt exactly as it
    stood before PR A1 moved the fetch:

    * **group only** — a DM has two known participants and needs no
      roster; it resolves one for the gate and shows it to nobody, or
      PR A2's byte-identity claim fails on the first DM (review F-4);
    * **directory required** — a member set with no display names would
      render bare ids, which is a *different* prompt. Restoring the
      roster under a directory ``401`` is [ISSUE-0140]'s fix, off this
      release's path by scope lock 3.

    Non-fatal throughout: an unresolved roster simply leaves no section
    (the persona is no worse off than before F-4). Not charged against
    ``MemoryBudget`` — the roster is structural room context, not
    recalled memory; it rides ``WorkingMemory``'s own priority-weighted
    retention (priority 9, non-compressible) instead.
    """
    working_memory.remove_section(ROSTER_SECTION_NAME)
    if roster is None or not roster.channel_id.startswith(GROUP_CHANNEL_PREFIX):
        return
    if not roster.directory_ok:
        return
    section = render_roster_section(roster.channel_meta, list(roster.members))
    if section is not None:
        working_memory.add_section(section)
