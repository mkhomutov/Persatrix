"""RFC 0037 §D — the hard gate at memory injection (v0.3.12 PR 4).

The one deterministic filter this RFC exists to ship: when a turn's prompt
is assembled for an acting classification ``L``, no memory entry with a
protection level above ``L`` is injected in verbatim form.  Runs in
:meth:`~agents.persona_runtime.memory_context._MemoryContextMixin
._inject_memory_context` over every channel-derived tier (channel-history,
episodic recall, facts, notes) **before** the RFC 0017 token budget, so a
withheld entry never competes for tokens and never reaches the prompt.
The declassification-projection branch (§E) arrives in PR 6; until then a
withheld entry is withheld entirely.

Two deliberately ungated surfaces, recorded here so the review trail does
not re-litigate them:

* the **relationship** tier — its numeric trust score is unclassified by
  the RFC's Non-Goals, and its cross-room *identity* fields are protected
  at the WRITE side by the §C ≤-``internal`` write-through rule
  (:mod:`agents.tools.identity_write_through`), not by a read gate;
* the **conversation window** — §H: it reconstructs only the turn's own
  channel transcript, which is by definition at the acting level.

Acting-level resolution (§D "total coverage", v0.3.12 review item 5) is
by acting-context CLASS over a positive list of event types, not by event
name at the call site: a channel-anchored event resolves its level off
the wire stamp (rule (a) of the §D scope), and every other member of
:class:`~agents.persona_types.EventType` — the tick-shaped class that can
publish anywhere — takes the rule-(b) ``public`` floor.  The two
frozensets below must jointly cover the enum; the positive-list unit test
(``test_injection_gate.py``) forces a conscious choice for every future
event type, the ``episode_routing`` precedent.

Rule (c)'s "and logged" half lives HERE (the lattice helpers are pinned
pure): the gate is the only layer holding the entry's identity, so its
WARNING names each unknown-labeled entry, its source channel, and the
acting level — aggregated once per turn rather than emitted per entry, so
a corrupted batch cannot flood the log exactly when an operator is trying
to read the gate's decisions.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from ..channel_event_classification import wire_channel_classification
from ..persona_types import EventType
from .classification import acting_rank, entry_rank_or_withhold

if TYPE_CHECKING:
    from collections.abc import Sequence

    from ..persona_types import AgentEvent
    from .memory_budget import MemoryBudget

logger = logging.getLogger(__name__)

__all__ = [
    "CHANNEL_ACTING_EVENT_TYPES",
    "PUBLIC_FLOOR_EVENT_TYPES",
    "InjectionManifestEntry",
    "TurnInjectionGate",
    "acting_classification_for_event",
]


#: Event types whose turn is anchored to a channel: the acting level is read
#: off the §B wire stamp (and the §B single-channel-turn guard restricts
#: their ``SEND_CHANNEL_MESSAGE`` targets to the inbound channel).  Matches
#: the multi-turn routing set in ``episode_routing`` by construction — a
#: channel-anchored turn is exactly one the tracker scopes to a channel.
CHANNEL_ACTING_EVENT_TYPES: Final[frozenset[EventType]] = frozenset({
    EventType.CHANNEL_MESSAGE,
    EventType.MENTION,
})

#: The tick-shaped class (§D scope rule (b)): turns without a classified
#: acting channel.  Their injection is floored ``public``, which is also
#: why the §B guard exempts them — nothing above ``public`` can be in
#: their context, so any publish target is safe.
PUBLIC_FLOOR_EVENT_TYPES: Final[frozenset[EventType]] = frozenset({
    EventType.TICK,
    EventType.TASK_ASSIGNED,
    EventType.SUB_AGENT_COMPLETED,
    EventType.APPROVAL_REQUESTED,
    EventType.APPROVAL_RESPONSE,
    EventType.AGENT_JOINED,
    EventType.AGENT_LEFT,
})


def acting_classification_for_event(event: AgentEvent) -> str | None:
    """Resolve the turn's acting classification from the trusted event.

    Channel-anchored types read the verbatim §B wire stamp (``None`` when
    the producer predates v0.3.12 or the stamp failed — rule (b) floors it
    downstream); every floor-class type resolves ``None`` unconditionally,
    ignoring any metadata a malformed producer might have attached.  Never
    reads LLM output — this and the task-local
    :func:`agents.acting_classification.current_acting_classification`
    (the tool-boundary seam, bound from the same metadata) are the only
    two resolution paths, both fed from the ingress seed.
    """
    if event.event_type in CHANNEL_ACTING_EVENT_TYPES:
        return wire_channel_classification(event)
    return None


@dataclass(frozen=True)
class InjectionManifestEntry:
    """One injected (budget-admitted) memory entry — the §G tripwire's
    future per-turn input (RFC 0037 PR 7 threads it to ``ActionExecutor``
    and adds the normalized-span hashes; dark until then)."""

    tier: str
    entry_id: str
    protection_level: str


#: Manifest tier order — the gated tiers in canonical priority order.
_MANIFEST_TIERS: Final[tuple[str, ...]] = (
    "channel_history", "facts", "episodic", "notes",
)


class TurnInjectionGate:
    """The §D filter for one turn, at acting classification ``acting``.

    ``admit`` applies the rank comparison per entry; ``filter_entries``
    maps it over a tier's candidate list.  Withheld/unknown entries are
    tallied for the one aggregated log emission (:meth:`emit_log`), and
    gate-passed entries' levels are retained so :meth:`manifest` can label
    the budget-admitted subset afterwards.
    """

    def __init__(self, *, acting: str | None, agent_id: str) -> None:
        self._acting = acting
        self._acting_rank = acting_rank(acting)
        self._agent_id = agent_id
        self._withheld = 0
        # Rule-(c) casualties: (tier, entry_id, raw_level, source_channel).
        self._unknown: list[tuple[str, str, str | None, str | None]] = []
        # (tier, entry_id) → protection level for gate-PASSED entries only.
        self._passed_levels: dict[tuple[str, str], str] = {}

    def admit(
        self,
        *,
        tier: str,
        entry_id: str,
        protection_level: str | None,
        source_channel_id: str | None = None,
    ) -> bool:
        """§D per-entry decision: ``rank(P) <= rank(L)`` → inject."""
        rank = entry_rank_or_withhold(protection_level)
        if rank is None:
            # Rule (c): unknown/unparseable → withheld, logged (aggregated).
            self._unknown.append(
                (tier, entry_id, protection_level, source_channel_id),
            )
            return False
        if rank > self._acting_rank:
            self._withheld += 1
            return False
        self._passed_levels[(tier, entry_id)] = protection_level  # type: ignore[assignment]
        return True

    def filter_entries(
        self,
        tier: str,
        entries: Sequence[object],
        *,
        id_attr: str = "id",
    ) -> list:
        """Filter one tier's candidates through :meth:`admit`.

        Entries expose ``protection_level`` and (nullable)
        ``source_channel_id`` — the RFC 0037 §C columns projected onto the
        ``Episode`` / ``Fact`` / ``Note`` dataclasses in this PR.
        """
        return [
            entry for entry in entries
            if self.admit(
                tier=tier,
                entry_id=getattr(entry, id_attr),
                protection_level=getattr(entry, "protection_level", None),
                source_channel_id=getattr(entry, "source_channel_id", None),
            )
        ]

    @property
    def withheld_count(self) -> int:
        """Clean above-rank withholds so far — rule-(c) casualties are
        counted separately (:attr:`unknown_label_count`).  Read by the
        RFC 0049 shadow trace, which reports the split instead of firing
        :meth:`emit_log`."""
        return self._withheld

    @property
    def unknown_label_count(self) -> int:
        """Rule-(c) casualties so far: entries withheld because their
        stored protection label failed to parse."""
        return len(self._unknown)

    def emit_log(self) -> None:
        """One aggregated emission per turn (never per entry — §A volume
        rationale): WARNING naming every rule-(c) unknown-label casualty,
        DEBUG for the count of clean above-rank withholds."""
        if self._unknown:
            described = "; ".join(
                f"{tier}:{entry_id} level={raw!r} channel={channel or '-'}"
                for tier, entry_id, raw, channel in self._unknown
            )
            logger.warning(
                "Agent %s: §D gate withheld %d entr%s with unknown "
                "protection level (acting=%r): %s",
                self._agent_id, len(self._unknown),
                "y" if len(self._unknown) == 1 else "ies",
                self._acting, described,
            )
        if self._withheld:
            logger.debug(
                "Agent %s: §D gate withheld %d entries above acting "
                "classification %r",
                self._agent_id, self._withheld, self._acting,
            )

    def manifest(self, budget: MemoryBudget) -> tuple[InjectionManifestEntry, ...]:
        """The per-turn injection manifest: every gate-passed entry the
        budget actually admitted, labeled with its protection level.

        Reads the RFC 0026 MQ-11 admission registry so the manifest names
        what reached the prompt, not what recall returned; the
        relationship tier records no admissions and is not §D-gated, so
        it is absent by construction.
        """
        entries: list[InjectionManifestEntry] = []
        for tier in _MANIFEST_TIERS:
            for entry_id in budget.admissions_by_tier(tier):
                level = self._passed_levels.get((tier, entry_id))
                if level is not None:
                    entries.append(InjectionManifestEntry(
                        tier=tier, entry_id=entry_id, protection_level=level,
                    ))
        return tuple(entries)
