"""ISSUE-0132 — who is in the room, as an input to the RFC 0037 §D gate.

The §D gate asks one question: is this entry's protection level at or
below the acting channel's classification?  It never asks **who is
listening**.  So a fact Alice taught the persona in a DM — stamped
``internal``, entirely correctly — is admissible in any other
``internal`` room, including one Bob is in.  Classification says *how
secret*; it does not say *whose*.

This module supplies the missing half: an entry's **audience** is the
current member set of the room it came from, and the check is that the
acting room adds nobody that room did not already hold.  It is an
AND-condition on §D, not a replacement for it — an entry must clear both.

Read the vocabulary carefully, because the four verdicts and the two
modes are not the same axis:

* :class:`AudienceVerdict` is what the *check concluded* — the
  measurement scope lock 1 exists to produce.  It is recorded for every
  §D-admitted, ``internal``-and-above entry, in every mode but ``off``.
* :data:`ENFORCED_VERDICTS` is what the check *does* — and in ``live``
  mode that is **disjoint only**.  Both *unknown* causes are recorded
  and admitted: withhold-unknown is the safe reading, but it silently
  degrades a persona that has been useful for four releases, and the
  amendment's risk row makes the measurement, not the caution, the
  guard.  The names say "withhold" because they name the strict
  reading's answer; the policy below is what ships.

The three modes mirror ``memory.{facts,episodic}.cross_room``
(:mod:`.cross_room`) on purpose — same vocabulary, same shadow → verdict
→ flip pattern, same documented rollback lever.  ``shadow`` is the
v0.3.16 default and the whole cycle's posture: the verdict is recorded,
the entry still injects, and every prompt is byte-identical to v0.3.15.

*Cost* (scope lock 2): K distinct source rooms among a turn's candidates
cost K round trips — the N+1 shape :mod:`.channel_roster` was written to
avoid — bounded by the tier recall limits, deduplicated by the per-turn
cache below, and pre-seeded with the acting room the A1 rail already
resolved, so the overwhelmingly common same-room turn costs nothing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Final

from .channel_roster import member_ids_from_meta
from .classification import CLASSIFICATION_PUBLIC

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from .channel_roster import ChannelRoster, ChannelRosterFetcher

logger = logging.getLogger(__name__)

__all__ = [
    "AUDIENCE_LIVE",
    "AUDIENCE_TIERS",
    "AUDIENCE_MODES",
    "AUDIENCE_OFF",
    "AUDIENCE_SHADOW",
    "DEFAULT_MEMORY_AUDIENCE",
    "ENFORCED_VERDICTS",
    "AudienceRecord",
    "AudienceVerdict",
    "TurnAudience",
    "resolve_memory_audience",
    "resolve_turn_audience",
]

AUDIENCE_OFF: Final[str] = "off"
AUDIENCE_SHADOW: Final[str] = "shadow"
AUDIENCE_LIVE: Final[str] = "live"
AUDIENCE_MODES: Final[frozenset[str]] = frozenset(
    {AUDIENCE_OFF, AUDIENCE_SHADOW, AUDIENCE_LIVE},
)

#: The gated tiers whose entries can carry RFC 0037 §C provenance, and
#: so the only ones the audience check judges.  ``notes`` is deliberately
#: absent: a note is *authored* during a turn rather than derived from
#: one channel, so its ``source_channel_id`` is NULL **by design** (see
#: :meth:`agents.memory.notes.NoteStore.store_note`) — judging the tier
#: would stamp every recalled note *withhold-unknown-no-provenance* on
#: every turn, forever, and swamp the measured delta with a denominator
#: that can never move.  Scope lock 2 says it in one clause: episodes
#: and facts carry provenance, notes do not and stay out.
AUDIENCE_TIERS: Final[frozenset[str]] = frozenset(
    {"channel_history", "facts", "episodic"},
)

#: Scope lock 1: shadow for the whole v0.3.16 cycle.  The flip to
#: ``live`` is its own PR (A3), lands only on a green verdict, and only
#: before release-prep PR 0 — never scheduled.
DEFAULT_MEMORY_AUDIENCE: Final[str] = AUDIENCE_SHADOW


class AudienceVerdict(Enum):
    """What the audience check concluded for one §D-admitted entry.

    Four, because "we could not tell" is two different facts with two
    different fixes: a roster call that missed is transient and retried
    next turn, while a NULL ``source_channel_id`` is permanent by design
    (pre-migration rows and tick/task-scoped records have no room).
    Collapsing them would make the shadow measurement unable to say
    whether the unknowns are a bug or the schema.
    """

    #: Every member of the acting room was in the entry's source room.
    ADMIT = "admit"
    #: The acting room holds a member the source room does not.
    WITHHOLD_DISJOINT = "withhold-disjoint"
    #: A roster call missed — transient; retried on the next turn.
    WITHHOLD_UNKNOWN_FETCH_FAILED = "withhold-unknown-fetch-failed"
    #: ``source_channel_id`` is NULL by design — permanent.
    WITHHOLD_UNKNOWN_NO_PROVENANCE = "withhold-unknown-no-provenance"


#: The verdicts ``live`` mode actually acts on — see the module
#: docstring.  ``A3`` flips the knob's *default*, not this set.
ENFORCED_VERDICTS: Final[frozenset[AudienceVerdict]] = frozenset(
    {AudienceVerdict.WITHHOLD_DISJOINT},
)


@dataclass(frozen=True)
class AudienceRecord:
    """One entry's audience decision — the row the §G watch, the shadow
    trace and the measurement all read from the gate's decision record.

    Recorded for §D-admitted entries only: the delta scope lock 1 asks
    for is the share of *gate-admitted* entries the audience check would
    withhold, so an entry classification already withheld never enters
    the denominator.
    """

    tier: str
    entry_id: str
    protection_level: str
    source_channel_id: str | None
    verdict: AudienceVerdict


def resolve_memory_audience(config: dict) -> str:
    """Resolve ``memory.egress.audience`` from a persona config.

    Absent / ``None`` → :data:`DEFAULT_MEMORY_AUDIENCE`.  An unknown
    value raises at agent construction, the ``cross_room`` precedent:
    silently degrading a requested mode misreports what the deployment
    is doing, and this knob's whole point is that an operator can state
    the posture and be believed.
    """
    egress = (config.get("memory") or {}).get("egress") or {}
    raw = egress.get("audience")
    if raw is None:
        return DEFAULT_MEMORY_AUDIENCE
    if not isinstance(raw, str) or raw not in AUDIENCE_MODES:
        raise ValueError(
            f"memory.egress.audience must be one of {sorted(AUDIENCE_MODES)}, "
            f"got {raw!r}",
        )
    return raw


@dataclass(frozen=True)
class TurnAudience:
    """One turn's audience view: the acting room, plus every source room
    its candidates named, resolved once each.

    ``None`` in :attr:`rooms` means *unknown* (the fetch missed), never
    *empty*: an empty acting room would spuriously admit everything, and
    ``channel_roster`` already collapses "no member list" and "empty
    room" into the same absence.
    """

    mode: str
    acting_channel_id: str
    #: Room id → its current member ids, or ``None`` when the fetch
    #: missed.  Pre-seeded with the acting room from the A1 rail.
    rooms: dict[str, frozenset[str] | None]
    #: Roster fetches this turn actually issued (the scope-lock-2 bound).
    fetches: int = 0
    #: Distinct non-NULL source rooms the candidates named.
    source_rooms: int = 0
    _acting: frozenset[str] | None = field(init=False, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "_acting", self.rooms.get(self.acting_channel_id),
        )

    @property
    def enforcing(self) -> bool:
        """Whether a :data:`ENFORCED_VERDICTS` verdict actually withholds."""
        return self.mode == AUDIENCE_LIVE

    def verdict(
        self,
        *,
        tier: str,
        protection_level: str,
        source_channel_id: str | None,
    ) -> AudienceVerdict | None:
        """The audience answer for one §D-admitted entry.

        ``None`` means *out of scope*, and there are two ways to be:
        a ``public`` entry is shareable by definition (scope lock 1),
        and a tier outside :data:`AUDIENCE_TIERS` carries no provenance
        to judge.  Neither carries a verdict, so neither dilutes the
        measured delta.
        """
        if tier not in AUDIENCE_TIERS:
            return None
        if protection_level == CLASSIFICATION_PUBLIC:
            return None
        if not source_channel_id:
            return AudienceVerdict.WITHHOLD_UNKNOWN_NO_PROVENANCE
        source = self.rooms.get(source_channel_id)
        if source is None or self._acting is None:
            return AudienceVerdict.WITHHOLD_UNKNOWN_FETCH_FAILED
        if self._acting <= source:
            return AudienceVerdict.ADMIT
        return AudienceVerdict.WITHHOLD_DISJOINT


def _distinct_source_rooms(
    candidates: Iterable[Sequence[object]],
) -> list[str]:
    """Every non-NULL ``source_channel_id`` a turn's candidates named,
    deduplicated, in first-seen order (stable fetch order for the tests
    and for a reader following a live log)."""
    seen: dict[str, None] = {}
    for tier_entries in candidates:
        for entry in tier_entries:
            room = getattr(entry, "source_channel_id", None)
            if isinstance(room, str) and room:
                seen.setdefault(room, None)
    return list(seen)


async def resolve_turn_audience(
    fetcher: ChannelRosterFetcher | None,
    roster: ChannelRoster | None,
    *,
    mode: str,
    acting_channel_id: str | None,
    candidates: Sequence[Sequence[object]],
    agent_id: str,
) -> TurnAudience | None:
    """Resolve the turn's audience view, or ``None`` when there is none.

    ``None`` — the check does not run — for ``mode="off"`` and for a
    turn with no acting channel.  The second is not a gap: the §D scope
    rule (b) floors a channel-less turn to ``public``, so nothing above
    ``public`` is in its context to leak.

    ``roster`` is the A1 rail's already-resolved acting roster; it seeds
    the cache so a same-room recall — the overwhelmingly common turn —
    costs no round trip.  A missing roster on a channel turn is recorded
    as *unknown*, which is why the acting channel id is passed
    separately rather than read off the roster that may not exist.

    Never raises: a lost roster is an unknown audience, never a failed
    turn (``_inject_memory_context``'s never-fail contract).
    """
    if mode == AUDIENCE_OFF or not acting_channel_id:
        return None
    acting_members = (
        roster.member_ids
        if roster is not None and roster.channel_id == acting_channel_id
        else None
    )
    rooms: dict[str, frozenset[str] | None] = {acting_channel_id: acting_members}
    fetches = 0
    source_rooms = _distinct_source_rooms(candidates)
    for room in source_rooms:
        if room in rooms:
            continue
        rooms[room] = await _fetch_member_ids(fetcher, room, agent_id=agent_id)
        fetches += 1
    return TurnAudience(
        mode=mode, acting_channel_id=acting_channel_id, rooms=rooms,
        fetches=fetches, source_rooms=len(source_rooms),
    )


async def _fetch_member_ids(
    fetcher: ChannelRosterFetcher | None, channel_id: str, *, agent_id: str,
) -> frozenset[str] | None:
    """One source room's current membership, or ``None`` on any miss.

    Only the **public** members half is asked for: the audience is a set
    of ids, and the authenticated directory that ``401``s for the fleet
    under auth ([ISSUE-0140]) supplies display names this check never
    reads.  That is what keeps ISSUE-0140 off this release's path.
    """
    if fetcher is None:
        return None
    try:
        meta = await fetcher.fetch_members(channel_id)
    except Exception:
        logger.warning(
            "Agent %s: audience roster fetch for %s failed; audience unknown",
            agent_id, channel_id, exc_info=True,
        )
        return None
    return member_ids_from_meta(meta)
