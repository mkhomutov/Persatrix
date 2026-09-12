"""RFC 0037 §D — the hard gate at memory injection (v0.3.12 PR 4).

The one deterministic filter this RFC exists to ship: when a turn's prompt
is assembled for an acting classification ``L``, no memory entry with a
protection level above ``L`` is injected in verbatim form.  Runs in
:meth:`~agents.persona_runtime.memory_context._MemoryContextMixin
._inject_memory_context` over every channel-derived tier (channel-history,
episodic recall, facts, notes) **before** the RFC 0017 token budget, so a
withheld entry never competes for tokens and never reaches the prompt.
The declassification-projection branch (§E, PR 6) lives in
:mod:`agents.persona_runtime.projection_branch`, fed by this gate's
per-tier decision record; an entry with no admissible projection is
withheld entirely.

Since v0.3.16 PR A2 the rank comparison is one of **two** conditions.
The second — audience, :mod:`agents.persona_runtime.audience` — asks
whether the acting room adds anyone the entry's source room did not
hold, and is applied here rather than in front of the gate on purpose:
the §G watch, the §G manifest and the shadow trace all read this one
decision record, and an entry filtered out upstream would be invisible
to all three ([ISSUE-0132] scope lock 3).  It ships in ``shadow``, where
the verdict is recorded and the entry still injects.

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
from .audience import ENFORCED_VERDICTS, AudienceRecord
from .classification import acting_rank, entry_rank_or_withhold

if TYPE_CHECKING:
    from collections.abc import Sequence

    from ..persona_types import AgentEvent
    from .audience import TurnAudience
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
    """One injected (budget-admitted) memory entry — the record of what
    reached the prompt.  The §G tripwire (PR 7) watches the WITHHELD
    complement instead (see ``tripwire_watch``): an admitted entry's
    level is ≤ the acting level = the §B-guarded publish target, so it
    can never satisfy §G's above-target condition."""

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

    def __init__(
        self,
        *,
        acting: str | None,
        agent_id: str,
        audience: TurnAudience | None = None,
    ) -> None:
        self._acting = acting
        self._acting_rank = acting_rank(acting)
        self._agent_id = agent_id
        # ISSUE-0132: the audience AND-condition.  ``None`` — the mode is
        # ``off``, or the turn has no acting channel — restores the pure
        # v0.3.15 §D gate exactly, which is what every pre-A2 caller
        # (the two shadow passes among them) keeps getting.
        self._audience = audience
        self._audience_records: list[AudienceRecord] = []
        self._audience_terminal: set[tuple[str, str]] = set()
        self._withheld = 0
        # Rule-(c) casualties: (tier, entry_id, raw_level, source_channel).
        self._unknown: list[tuple[str, str, str | None, str | None]] = []
        # (tier, entry_id) → protection level for gate-PASSED entries only.
        self._passed_levels: dict[tuple[str, str], str] = {}
        # Per-tier candidate decisions in arrival order, recorded by
        # :meth:`filter_entries` for the §E projection branch (PR 6) —
        # the order-preserving merge that reinserts a projected
        # replacement where the withheld original stood.
        self._decisions: dict[str, list[tuple[object, bool]]] = {}

    @property
    def acting(self) -> str | None:
        """The turn's acting classification as resolved at construction —
        read by the §E projection branch to build its admissible-level
        IN-set (rule (b) flooring happens inside ``injectable_levels``)."""
        return self._acting

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
        # ── ISSUE-0132: the audience AND-condition ──────────────────
        # Reached only by entries §D has already admitted, which is what
        # makes the recorded delta answer scope lock 1's question: the
        # share of GATE-ADMITTED entries the audience check would
        # withhold.  A classification withhold above is a different
        # story and stays in its own tally.
        if not self._judge_audience(
            tier=tier, entry_id=entry_id,
            protection_level=protection_level,  # type: ignore[arg-type]
            source_channel_id=source_channel_id,
        ):
            return False
        self._passed_levels[(tier, entry_id)] = protection_level  # type: ignore[assignment]
        return True

    def _judge_audience(
        self,
        *,
        tier: str,
        entry_id: str,
        protection_level: str,
        source_channel_id: str | None,
    ) -> bool:
        """The ISSUE-0132 check for one entry, recording its verdict.

        Returns whether the entry may still reach the prompt: ``True``
        when the check is not running, the entry is out of its scope, or
        the mode is not enforcing.  The verdict is recorded either way —
        that recording IS the shadow measurement.

        Shared by the two ways an entry reaches the prompt (scope lock 3
        composition): :meth:`admit` for a verbatim §D-admitted entry and
        :meth:`audience_admits_projection` for a §E declassified stand-in.
        """
        if self._audience is None:
            return True
        verdict = self._audience.verdict(
            tier=tier,
            protection_level=protection_level,
            source_channel_id=source_channel_id,
        )
        if verdict is None:
            return True
        self._audience_records.append(AudienceRecord(
            tier=tier, entry_id=entry_id,
            protection_level=protection_level,
            source_channel_id=source_channel_id, verdict=verdict,
        ))
        if not (self._audience.enforcing and verdict in ENFORCED_VERDICTS):
            return True
        # §E composition (scope lock 3): a projection lowers an entry's
        # CLASSIFICATION, not its audience, so an audience withhold is
        # terminal — no projection substitutes for it.
        self._audience_terminal.add((tier, entry_id))
        return False

    def audience_admits_projection(
        self,
        *,
        tier: str,
        entry_id: str,
        protection_level: str,
        source_channel_id: str | None,
    ) -> bool:
        """Whether §E may serve a projection at ``protection_level``.

        A §D **rank** withhold returns from :meth:`admit` before the
        audience clause, so the entry carries no verdict — and the §E
        branch then re-admits exactly those entries as declassified
        stand-ins.  Without this call the AND-condition would be open in
        that direction: abstracting Alice's DM fact to ``internal`` and
        serving it in Bob's room still tells Bob there is such a fact.

        The **projection's** level is judged (a ``public`` stand-in is
        shareable by definition, the same exemption verbatim entries
        get) against the **original's** provenance, which a projection
        does not change.  Recording it here is also what keeps the
        measured denominator honest: a projected entry reaches the
        prompt, so it belongs in the share the flip is argued from.
        """
        return self._judge_audience(
            tier=tier, entry_id=entry_id,
            protection_level=protection_level,
            source_channel_id=source_channel_id,
        )

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

        Every candidate's decision is also recorded (in order) on the
        per-tier decision list :meth:`decisions` so the §E projection
        branch can rebuild the tier with declassified replacements in
        the withheld originals' positions.
        """
        admitted: list = []
        decisions = self._decisions.setdefault(tier, [])
        for entry in entries:
            ok = self.admit(
                tier=tier,
                entry_id=getattr(entry, id_attr),
                protection_level=getattr(entry, "protection_level", None),
                source_channel_id=getattr(entry, "source_channel_id", None),
            )
            decisions.append((entry, ok))
            if ok:
                admitted.append(entry)
        return admitted

    def decisions(self, tier: str) -> tuple[tuple[object, bool], ...]:
        """This tier's ``(entry, admitted)`` pairs in candidate order —
        the §E projection branch's input.  Empty for a tier never
        filtered through this gate."""
        return tuple(self._decisions.get(tier, ()))

    def record_projection(self, *, tier: str, entry_id: str, level: str) -> None:
        """Label a §E projection served in place of a withheld entry.

        Registers the PROJECTION's own (lower) level under the entry's id
        so :meth:`manifest` reports what actually reached the prompt —
        the abstraction at ``level``, not the verbatim entry at its
        protection level.  The verbatim withhold tallies
        (:attr:`withheld_count` / :attr:`unknown_label_count`) are
        deliberately NOT decremented: the verbatim entry WAS withheld —
        that is the §D guarantee — and the RFC 0049 shadow traces that
        read the split predate (and must not shift under) the
        projection affordance.
        """
        self._passed_levels[(tier, entry_id)] = level

    @property
    def audience_records(self) -> tuple[AudienceRecord, ...]:
        """Every §D-admitted entry's audience verdict, in candidate order.

        One decision record, three readers (scope lock 3): the §G watch
        reads :meth:`decisions`, the shadow trace reads this, and the
        measurement reads the trace.  A pre-filtered entry would be
        invisible to all three — which is why the check is a gate input
        and not a filter in front of it.
        """
        return tuple(self._audience_records)

    @property
    def audience_withheld_count(self) -> int:
        """Entries this turn withheld for audience, not classification.

        Non-zero only in ``live`` mode: in ``shadow`` the verdict is
        recorded and the entry still injects.

        Derived from the terminal set rather than counted alongside it:
        the two are the same fact — an entry withheld for audience is
        exactly an entry §E must not stand in for — and keeping a
        separate tally would let them drift the day a second verdict
        joins :data:`ENFORCED_VERDICTS`.
        """
        return len(self._audience_terminal)

    def audience_terminal(self, tier: str, entry_id: str) -> bool:
        """Whether this entry was withheld for audience, so §E must not
        serve a projection in its place (scope lock 3)."""
        return (tier, entry_id) in self._audience_terminal

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
        what reached the prompt, not what recall returned.  The
        relationship tier is not §D-gated (RFC 0037 Non-Goals) and is not
        in :data:`_MANIFEST_TIERS`, so it is absent by construction even
        though it records admissions since ISSUE-0122 (v0.3.16 PR B1) —
        its ``tier_admitted`` line is the provenance log's, not the
        manifest's.
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
