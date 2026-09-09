"""ISSUE-0132 (v0.3.16 PR A2) — audience as an AND-condition on the §D gate.

The RFC 0037 §D gate ranks an entry's ``protection_level`` against the
acting channel's classification and nothing else, so a fact Alice taught
in a DM is admissible in any equally-classified room — including one Bob
is in.  This module pins the audience check that closes that: the
**three-case regression** scope lock 1 names, the four verdicts, the
``public`` exemption, the NULL-provenance rule, and the fetch bound the
lock's cost paragraph promises.

Shadow is the shipped posture: every case below is asserted twice where
the two modes differ — in ``shadow`` the verdict is *recorded* and the
entry still injects (the byte-identity claim), in ``live`` the
disjoint verdict withholds.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from agents.persona_runtime.audience import (
    AUDIENCE_LIVE,
    AUDIENCE_MODES,
    AUDIENCE_OFF,
    AUDIENCE_SHADOW,
    AUDIENCE_TIERS,
    DEFAULT_MEMORY_AUDIENCE,
    AudienceVerdict,
    resolve_memory_audience,
    resolve_turn_audience,
)
from agents.persona_runtime.channel_roster import ChannelRoster, DirectoryStatus, RosterMember
from agents.persona_runtime.injection_gate import TurnInjectionGate

DM = "dm:alice:iron-fox"
WITH_BOB = "group:standup"
WITHOUT_BOB = "group:pair"

#: The DM Alice taught in: Alice and the persona, nobody else.
_ROOMS: dict[str, list[str]] = {
    DM: ["alice", "iron-fox"],
    # Adds Bob — a member the source room does not hold.
    WITH_BOB: ["alice", "iron-fox", "bob"],
    # Every member was in the DM (a strict subset is still admissible:
    # nobody here failed to hear it the first time).
    WITHOUT_BOB: ["alice", "iron-fox"],
    # A peer PERSONA is audience too (scope lock 3: an agent→agent leak
    # is a leak), so this room is disjoint despite holding no human Bob.
    "group:personas": ["alice", "iron-fox", "nova-sparrow"],
    # The third regression case: a DM with the *wrong* person in it.
    "dm:bob:iron-fox": ["bob", "iron-fox"],
}


@dataclass(frozen=True)
class _Entry:
    """The three §C columns the gate reads off every tier's candidates."""

    id: str
    protection_level: str | None
    source_channel_id: str | None


class _RoomFetcher:
    """A ``ChannelRosterFetcher`` over :data:`_ROOMS`, counting fetches.

    ``missing`` names rooms whose members half returns ``None`` (the
    live ``401`` / transport-failure shape); ``raising`` names rooms
    whose fetch blows up, which must never cross the seam.
    """

    def __init__(
        self, *, missing: set[str] | None = None, raising: set[str] | None = None,
    ) -> None:
        self.calls: list[str] = []
        self._missing = missing or set()
        self._raising = raising or set()

    async def fetch_members(self, channel_id: str) -> dict[str, Any] | None:
        self.calls.append(channel_id)
        if channel_id in self._raising:
            raise RuntimeError(f"boom: {channel_id}")
        if channel_id in self._missing or channel_id not in _ROOMS:
            return None
        return {
            "id": channel_id,
            "members": [{"id": m} for m in _ROOMS[channel_id]],
        }

    async def fetch_directory(self) -> list[dict[str, Any]] | None:
        return []


def _roster(channel_id: str) -> ChannelRoster:
    """The A1 roster the turn already resolved for its acting room."""
    return ChannelRoster(
        channel_id=channel_id,
        channel_meta={"id": channel_id},
        members=tuple(
            RosterMember(id=m, name=m, role="", is_self=(m == "iron-fox"))
            for m in _ROOMS[channel_id]
        ),
        directory=DirectoryStatus.RESOLVED,
    )


async def _audience(
    acting: str,
    candidates: list[_Entry],
    *,
    mode: str = AUDIENCE_SHADOW,
    fetcher: _RoomFetcher | None = None,
    roster: ChannelRoster | None = ...,  # type: ignore[assignment]
):
    """Resolve one turn's audience the way ``_inject_memory_context`` does."""
    return await resolve_turn_audience(
        fetcher if fetcher is not None else _RoomFetcher(),
        _roster(acting) if roster is ... else roster,
        mode=mode,
        acting_channel_id=acting,
        candidates=(candidates,),
        agent_id="iron-fox",
    )


def _verdicts(gate: TurnInjectionGate) -> list[AudienceVerdict]:
    return [record.verdict for record in gate.audience_records]


# ─── The knob ──────────────────────────────────────────────


def test_default_mode_is_shadow_for_the_whole_cycle() -> None:
    """Scope lock 1: the audience check ships recording, not withholding."""
    assert DEFAULT_MEMORY_AUDIENCE == AUDIENCE_SHADOW
    assert resolve_memory_audience({}) == AUDIENCE_SHADOW
    assert AUDIENCE_MODES == {AUDIENCE_OFF, AUDIENCE_SHADOW, AUDIENCE_LIVE}


def test_knob_reads_memory_egress_audience() -> None:
    cfg = {"memory": {"egress": {"audience": "live"}}}
    assert resolve_memory_audience(cfg) == AUDIENCE_LIVE
    assert resolve_memory_audience({"memory": {"egress": {"audience": None}}}) == (
        AUDIENCE_SHADOW
    )


def test_unknown_mode_is_loud_not_floored() -> None:
    """The ``cross_room`` precedent: silently degrading a requested mode
    misreports what the deployment is doing."""
    with pytest.raises(ValueError, match="memory.egress.audience"):
        resolve_memory_audience({"memory": {"egress": {"audience": "on"}}})
    with pytest.raises(ValueError, match="memory.egress.audience"):
        resolve_memory_audience({"memory": {"egress": {"audience": 1}}})


# ─── The three-case regression (scope lock 1 / the plan's acceptance) ───


@pytest.mark.asyncio
async def test_room_adding_anyone_not_in_the_dm_is_withhold_disjoint() -> None:
    entry = _Entry("e1", "internal", DM)
    audience = await _audience(WITH_BOB, [entry])
    gate = TurnInjectionGate(acting="internal", agent_id="iron-fox", audience=audience)
    admitted = gate.filter_entries("facts", [entry])

    assert _verdicts(gate) == [AudienceVerdict.WITHHOLD_DISJOINT]
    # Shadow: recorded, still injected — the byte-identity claim.
    assert admitted == [entry]


@pytest.mark.asyncio
async def test_a_peer_persona_makes_the_room_disjoint_too() -> None:
    """Scope lock 3: audience is the type-agnostic member-id set."""
    entry = _Entry("e1", "internal", DM)
    audience = await _audience("group:personas", [entry])
    gate = TurnInjectionGate(acting="internal", agent_id="iron-fox", audience=audience)
    gate.filter_entries("facts", [entry])
    assert _verdicts(gate) == [AudienceVerdict.WITHHOLD_DISJOINT]


@pytest.mark.asyncio
async def test_room_whose_every_member_was_in_the_dm_is_admit() -> None:
    entry = _Entry("e1", "internal", DM)
    audience = await _audience(WITHOUT_BOB, [entry])
    gate = TurnInjectionGate(acting="internal", agent_id="iron-fox", audience=audience)
    admitted = gate.filter_entries("facts", [entry])

    assert _verdicts(gate) == [AudienceVerdict.ADMIT]
    assert admitted == [entry]


@pytest.mark.asyncio
async def test_a_dm_with_bob_alone_is_withhold_disjoint() -> None:
    """The third case: the acting room is itself a DM, with the wrong
    person in it.  A DM is an audience — that is the turn the issue is
    about — and Bob was not in Alice's."""
    entry = _Entry("e1", "internal", DM)
    audience = await _audience("dm:bob:iron-fox", [entry])
    gate = TurnInjectionGate(acting="internal", agent_id="iron-fox", audience=audience)
    gate.filter_entries("facts", [entry])
    assert _verdicts(gate) == [AudienceVerdict.WITHHOLD_DISJOINT]


@pytest.mark.asyncio
async def test_same_room_recall_always_admits() -> None:
    """The overwhelmingly common turn: the entry came from the room the
    persona is acting in.  It must cost no fetch and never withhold."""
    entry = _Entry("e1", "internal", WITH_BOB)
    fetcher = _RoomFetcher()
    audience = await _audience(WITH_BOB, [entry], fetcher=fetcher)
    gate = TurnInjectionGate(acting="internal", agent_id="iron-fox", audience=audience)
    gate.filter_entries("facts", [entry])

    assert _verdicts(gate) == [AudienceVerdict.ADMIT]
    assert fetcher.calls == []


# ─── live: the disjoint verdict is the one that bites ───────


@pytest.mark.asyncio
async def test_live_withholds_the_disjoint_entry() -> None:
    entry = _Entry("e1", "internal", DM)
    audience = await _audience(WITH_BOB, [entry], mode=AUDIENCE_LIVE)
    gate = TurnInjectionGate(acting="internal", agent_id="iron-fox", audience=audience)
    admitted = gate.filter_entries("facts", [entry])

    assert admitted == []
    assert _verdicts(gate) == [AudienceVerdict.WITHHOLD_DISJOINT]
    assert gate.audience_withheld_count == 1
    # The §D tallies are the *classification* story and must not move: an
    # audience withhold is not an above-rank withhold.
    assert gate.withheld_count == 0
    assert gate.unknown_label_count == 0


@pytest.mark.asyncio
async def test_live_admits_both_unknown_causes() -> None:
    """Scope lock 1: the flip is withhold-disjoint / **admit-unknown**,
    taken per cause — withhold-unknown is the safe reading but silently
    degrades a persona that has been useful for four releases."""
    no_provenance = _Entry("e1", "internal", None)
    fetch_failed = _Entry("e2", "internal", "group:gone")
    audience = await _audience(
        WITH_BOB, [no_provenance, fetch_failed],
        mode=AUDIENCE_LIVE, fetcher=_RoomFetcher(missing={"group:gone"}),
    )
    gate = TurnInjectionGate(acting="internal", agent_id="iron-fox", audience=audience)
    admitted = gate.filter_entries("facts", [no_provenance, fetch_failed])

    assert admitted == [no_provenance, fetch_failed]
    assert _verdicts(gate) == [
        AudienceVerdict.WITHHOLD_UNKNOWN_NO_PROVENANCE,
        AudienceVerdict.WITHHOLD_UNKNOWN_FETCH_FAILED,
    ]
    assert gate.audience_withheld_count == 0


@pytest.mark.asyncio
async def test_off_computes_no_audience_at_all() -> None:
    fetcher = _RoomFetcher()
    audience = await _audience(
        WITH_BOB, [_Entry("e1", "internal", DM)], mode=AUDIENCE_OFF, fetcher=fetcher,
    )
    assert audience is None
    assert fetcher.calls == []


# ─── unknown, split by cause (scope lock 1) ────────────────


@pytest.mark.asyncio
async def test_null_source_channel_never_acquires_an_audience() -> None:
    """Pre-migration rows and tick/task-scoped records carry no source
    room by design: they are never *fetched* for, and their cause is
    permanent, not transient."""
    entry = _Entry("e1", "internal", None)
    fetcher = _RoomFetcher()
    audience = await _audience(WITH_BOB, [entry], fetcher=fetcher)
    gate = TurnInjectionGate(acting="internal", agent_id="iron-fox", audience=audience)
    gate.filter_entries("facts", [entry])

    assert _verdicts(gate) == [AudienceVerdict.WITHHOLD_UNKNOWN_NO_PROVENANCE]
    assert fetcher.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["missing", "raising"])
async def test_a_lost_source_roster_is_fetch_failed_not_disjoint(kind: str) -> None:
    entry = _Entry("e1", "internal", DM)
    fetcher = _RoomFetcher(**{kind: {DM}})  # type: ignore[arg-type]
    audience = await _audience(WITH_BOB, [entry], fetcher=fetcher)
    gate = TurnInjectionGate(acting="internal", agent_id="iron-fox", audience=audience)
    gate.filter_entries("facts", [entry])

    assert _verdicts(gate) == [AudienceVerdict.WITHHOLD_UNKNOWN_FETCH_FAILED]


@pytest.mark.asyncio
async def test_a_lost_acting_roster_is_fetch_failed_for_every_entry() -> None:
    """The A1 rail resolved nothing for this turn (the members half
    missed).  The audience is unknown-transient, not empty — an empty
    acting room would spuriously admit everything."""
    entry = _Entry("e1", "internal", DM)
    audience = await _audience(WITH_BOB, [entry], roster=None)
    gate = TurnInjectionGate(acting="internal", agent_id="iron-fox", audience=audience)
    gate.filter_entries("facts", [entry])

    assert _verdicts(gate) == [AudienceVerdict.WITHHOLD_UNKNOWN_FETCH_FAILED]


@pytest.mark.asyncio
async def test_public_entries_are_exempt_and_carry_no_verdict() -> None:
    """Scope lock 1: entries at ``public`` are always shareable, so
    audience applies to ``internal`` and above — and a public entry must
    not land in the delta's denominator."""
    public = _Entry("e1", "public", DM)
    internal = _Entry("e2", "internal", DM)
    audience = await _audience(WITH_BOB, [public, internal], mode=AUDIENCE_LIVE)
    gate = TurnInjectionGate(acting="internal", agent_id="iron-fox", audience=audience)
    admitted = gate.filter_entries("facts", [public, internal])

    assert admitted == [public]
    assert [(r.entry_id, r.verdict) for r in gate.audience_records] == [
        ("e2", AudienceVerdict.WITHHOLD_DISJOINT),
    ]


@pytest.mark.asyncio
async def test_an_entry_the_d_gate_already_withheld_gets_no_verdict() -> None:
    """The audience check is an AND-condition, not a second opinion: the
    delta is the share of **gate-admitted** entries it would withhold, so
    a classification-withheld entry must not enter the denominator."""
    above_rank = _Entry("e1", "restricted", DM)
    unknown_label = _Entry("e2", "confidential-ish", DM)
    audience = await _audience(WITH_BOB, [above_rank, unknown_label])
    gate = TurnInjectionGate(acting="internal", agent_id="iron-fox", audience=audience)
    assert gate.filter_entries("facts", [above_rank, unknown_label]) == []
    assert gate.audience_records == ()


# ─── the fetch bound (scope lock 2 / review F-6) ────────────


@pytest.mark.asyncio
async def test_one_fetch_per_distinct_source_room() -> None:
    """The N+1 ``channel_roster`` was written to avoid.  The acting room
    is pre-seeded from the A1 roster, so it is never re-fetched."""
    entries = [
        _Entry("e1", "internal", DM),
        _Entry("e2", "internal", DM),          # same room again
        _Entry("e3", "internal", WITHOUT_BOB),
        _Entry("e4", "internal", WITH_BOB),    # the acting room
        _Entry("e5", "internal", None),        # no provenance
    ]
    fetcher = _RoomFetcher()
    audience = await _audience(WITH_BOB, entries, fetcher=fetcher)

    assert sorted(fetcher.calls) == sorted([DM, WITHOUT_BOB])
    assert audience is not None
    assert audience.fetches == 2
    assert audience.source_rooms == 3


@pytest.mark.asyncio
async def test_candidates_are_pooled_across_tiers_before_fetching() -> None:
    """All four gated tiers share one per-turn cache — the same source
    room named by an episode and a fact costs one round trip, not two."""
    fetcher = _RoomFetcher()
    audience = await resolve_turn_audience(
        fetcher, _roster(WITH_BOB), mode=AUDIENCE_SHADOW,
        acting_channel_id=WITH_BOB,
        candidates=(
            [_Entry("e1", "internal", DM)],
            [_Entry("f1", "internal", DM)],
            [_Entry("n1", "internal", WITHOUT_BOB)],
        ),
        agent_id="iron-fox",
    )
    assert sorted(fetcher.calls) == sorted([DM, WITHOUT_BOB])
    assert audience is not None and audience.fetches == 2


# ─── tier scope: notes carry no provenance BY DESIGN ───────


@pytest.mark.asyncio
async def test_the_notes_tier_is_never_judged() -> None:
    """A note is *authored* during a turn, not derived from one channel,
    so its ``source_channel_id`` is NULL by design.  Judging the tier
    would stamp every recalled note *no-provenance* on every turn and
    swamp the delta with a denominator that can never move — scope lock
    2's "notes do not and stay out" clause.
    """
    note = _Entry("n1", "internal", None)
    fact = _Entry("f1", "internal", None)
    audience = await _audience(WITH_BOB, [note, fact])
    gate = TurnInjectionGate(acting="internal", agent_id="iron-fox", audience=audience)
    gate.filter_entries("notes", [note])
    gate.filter_entries("facts", [fact])

    # Only the fact — whose NULL provenance IS a pre-migration/tick fact
    # about that row, not a property of its tier.
    assert [(r.tier, r.entry_id) for r in gate.audience_records] == [
        ("facts", "f1"),
    ]


@pytest.mark.asyncio
async def test_every_other_gated_tier_is_judged() -> None:
    """The complement, so the exclusion above cannot quietly grow."""
    entry = _Entry("e1", "internal", DM)
    audience = await _audience(WITH_BOB, [entry])
    gate = TurnInjectionGate(acting="internal", agent_id="iron-fox", audience=audience)
    for tier in ("channel_history", "facts", "episodic"):
        gate.filter_entries(tier, [entry])
    assert [r.tier for r in gate.audience_records] == [
        "channel_history", "facts", "episodic",
    ]
    assert AUDIENCE_TIERS == {"channel_history", "facts", "episodic"}
