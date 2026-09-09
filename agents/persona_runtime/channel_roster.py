"""Channel-roster context tier (v0.3.7 conversation test-findings, F-4).

In a group channel with no shared world-state, personas confabulate who
is present and what each other does. This module builds a **channel
roster** — the channel description plus each member's name and role — so
the per-event context carries a shared, consistent view of the room.

Sourced from the orchestrator, never per member (no N+1): `GET
/api/v1/channels/{id}` for membership and `GET /api/v1/agents` for the
id→name/role directory.

Since v0.3.16 PR A1 the module also carries the **audience rail** that
ISSUE-0132's egress check reads. The rationale lives here once; the
functions below point back rather than restate it.

* The two GETs are **independent halves of the seam**. Membership is
  public and load-bearing — the member set *is* the audience. The
  directory is authenticated, `401`s for the fleet under auth
  ([ISSUE-0140]), and supplies display names for the group-channel
  section alone. A turn that renders no section never asks for it, and a
  directory that misses costs names, not membership.
* Resolution (:func:`resolve_channel_roster`) is separate from injection
  (:func:`inject_channel_roster`) and runs *ahead of* the RFC 0037 §D
  gate, for **every turn that names a channel**: the gate cannot ask who
  is listening if the roster arrives after it has decided. A DM with Bob
  is an audience, and is the turn ISSUE-0132 is about. Only a group turn
  with a resolved directory injects a section, so no prompt moves.
* An **unknown** audience is never presented as an empty one. A channel
  returned without a usable member list resolves to ``None``, not to a
  roster nobody is in: the Go response tags `members` `omitempty`, so
  "empty room" and "no member list" are the same bytes.
* Nothing here raises across the seam — both halves and the join are
  guarded. A malformed body must cost a roster, never a turn.

*Cost*: one round trip per channel turn, two on a group turn, where
before only group turns paid anything. ``_inject_memory_context`` issues
it concurrently with the tier recalls, off the critical path.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
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
#: ``thread:``, which resolve an audience and show it to nobody).
GROUP_CHANNEL_PREFIX: str = "group:"


class DirectoryStatus(Enum):
    """What became of the authenticated ``/api/v1/agents`` half.

    Three states, not a boolean: "never asked" and "asked and missed" are
    different facts, and PR A2 owes scope lock 1 a withhold cause it
    cannot name from one bit.
    """

    #: DM or thread turn — renders no section, so no names were wanted.
    NOT_REQUESTED = "not_requested"
    #: Asked and lost it: ``401`` under auth, an error, or a bad shape.
    MISSED = "missed"
    #: Answered — possibly ``[]``, which still renders the section with
    #: bare ids exactly as it did before PR A1.
    RESOLVED = "resolved"


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
    was not in the source room is audience too (scope lock 3). A resolved
    roster always has at least one member (see the module docstring).

    ``channel_meta`` is the raw response dict, kept for the section's name
    and description and excluded from equality/hashing: PR A2 caches these
    per turn, and a dict field would make the frozen record unhashable.
    """

    channel_id: str
    channel_meta: dict[str, Any] = field(compare=False, repr=False)
    members: tuple[RosterMember, ...]
    directory: DirectoryStatus
    #: Computed once: the gate reads the audience per entry per turn.
    _member_ids: frozenset[str] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "_member_ids", frozenset(m.id for m in self.members),
        )

    @property
    def member_ids(self) -> frozenset[str]:
        """The audience: every member id, self included."""
        return self._member_ids


def build_roster(
    channel_meta: dict[str, Any],
    agents: list[dict[str, Any]],
    *,
    self_agent_id: str,
) -> list[RosterMember]:
    """Join a channel's members with the agent directory.

    Membership order is preserved (it is the room's declared order).
    Members absent from ``agents`` fall back to ``name=id, role=""`` so an
    unregistered or task-only participant still appears. A missing or
    non-list ``members`` value, and malformed entries within it, yield no
    members rather than raising — this feeds an LLM prompt.
    """
    declared = channel_meta.get("members")
    if not isinstance(declared, list):
        return []
    directory = {
        a["id"]: a
        for a in agents
        if isinstance(a, dict) and isinstance(a.get("id"), str)
    }
    roster: list[RosterMember] = []
    for member in declared:
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

    Two GETs as two seam methods, so the caller can request — and lose —
    them independently (see the module docstring): :meth:`fetch_members`
    (``/api/v1/channels/{id}``, public) and :meth:`fetch_directory`
    (``/api/v1/agents``, authenticated). Each returns ``None`` on any HTTP
    error / transport failure / unusable body and never raises across the
    seam; an empty directory (``[]``) is a *success*, distinct from a
    miss. The caller owns the ``aiohttp`` session.
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

    Two halves, because they are wanted at different times: every channel
    turn needs the membership, only a group turn needs the names. Each
    returns ``None`` when its half missed."""

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
    one — see the module docstring for why (ISSUE-0132 scope lock 3).

    The predicate is the event's ``channel_id``, not the gate's
    ``CHANNEL_ACTING_EVENT_TYPES``: before PR A1 *any* event carrying a
    ``group:`` channel id rendered a roster whatever its event type, so
    narrowing to the gate's two types would delete the section from every
    other one — a prompt change this PR must not make.

    Returns ``None`` — never raises — when there is no channel, no wired
    fetcher, the members half missed, or the channel declared no usable
    membership.
    """
    channel_id = getattr(event, "channel_id", None)
    if not isinstance(channel_id, str) or not channel_id:
        return None
    if fetcher is None:
        return None
    # Each half is guarded on its own: losing the membership loses the
    # audience, losing the names loses only the section's cosmetics.
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
    directory = DirectoryStatus.NOT_REQUESTED
    agents: list[dict[str, Any]] | None = None
    if channel_id.startswith(GROUP_CHANNEL_PREFIX):
        try:
            agents = await fetcher.fetch_directory()
        except Exception:
            logger.warning(
                "channels: roster directory fetch failed for %s; "
                "falling back to member ids", channel_id, exc_info=True,
            )
        directory = (
            DirectoryStatus.MISSED if agents is None
            else DirectoryStatus.RESOLVED
        )
    members = build_roster(channel_meta, agents or [], self_agent_id=agent_id)
    if not members:
        # Unknown membership, not an empty room — see the module docstring.
        logger.debug(
            "channels: %s declared no usable membership; no roster",
            channel_id,
        )
        return None
    return ChannelRoster(
        channel_id=channel_id,
        channel_meta=channel_meta,
        members=tuple(members),
        directory=directory,
    )


def inject_channel_roster(
    working_memory: WorkingMemory,
    roster: ChannelRoster | None,
) -> None:
    """Inject the resolved roster as a **group**-channel prompt section (F-4).

    Clears any stale roster section first (so a prior group event's roster
    does not linger on a later DM turn), then renders only for a ``group:``
    channel whose directory answered — both conditions hold the prompt
    exactly where it stood before PR A1 moved the fetch. The two guards
    overlap by construction (the directory is only requested for group
    channels) and are kept apart so a hand-built roster cannot smuggle a
    section onto a DM turn.

    Not charged against ``MemoryBudget``: the roster is structural room
    context, not recalled memory, and rides ``WorkingMemory``'s own
    priority-weighted retention (priority 9, non-compressible).
    """
    working_memory.remove_section(ROSTER_SECTION_NAME)
    if roster is None or not roster.channel_id.startswith(GROUP_CHANNEL_PREFIX):
        return
    if roster.directory is not DirectoryStatus.RESOLVED:
        return
    section = render_roster_section(roster.channel_meta, list(roster.members))
    if section is not None:
        working_memory.add_section(section)
