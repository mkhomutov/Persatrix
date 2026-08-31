"""ISSUE-0130 (b) — the boot-stable identity of a replayed span.

Narrowing the shape-(a) derivation skip gives catch-up its derivation
back; this is what stops it giving back the *growth* with it.  Catch-up
has no watermark (RFC 0011 OQ #8), so every boot re-reads the same
last-N page — and every field that normally identifies an interaction is
boot-derived (``interaction_id`` is a ``uuid4``, ``started_at`` is boot
time, not wire time).  The identity here is the span's own content
instead, so the same messages hash the same on the next boot and the
close path can decline to summarise them twice.

What must NOT drift is which inputs it takes: dropping the principal or
the speaker would let one record's derivation suppress a sibling's in the
same room, which is the ISSUE-0123 boundary re-broken from the other end.
The same argument extends to every axis the STORED row is partitioned by,
which is why the epoch is in here too (PR B2 review): it is not part of
the record key, but ``store_episode`` stamps it and recall filters it with
no carve-out, so an epoch-free identity would let one epoch's row suppress
another's derivation of a span it can never read.
"""

from __future__ import annotations

import pytest

from agents.memory.interactions import InteractionTracker
from agents.persona_runtime.replay_identity import (
    REPLAY_INTERACTION_ID_PREFIX,
    replay_span_already_derived,
    replay_span_identity,
)


def _record(*, principal: str = "alice-person", speaker: str = "alice",
            scope: str = "group:planning"):
    tracker = InteractionTracker()
    return tracker.add_turn(
        scope, payload={"text": "hi"}, replayed=True, replay_attributed=True,
        principal_id=principal, speaker_id=speaker,
    )


class TestIdentityIsStableAndDiscriminating:
    def test_the_same_span_hashes_the_same(self):
        a = replay_span_identity(_record(), "ember-owl", "live", "internal", ["m-1", "m-2"])
        b = replay_span_identity(_record(), "ember-owl", "live", "internal", ["m-1", "m-2"])
        assert a is not None and a == b

    def test_it_is_marked_as_a_replay_derivation(self):
        identity = replay_span_identity(_record(), "ember-owl", "live", "internal", ["m-1"])
        assert identity is not None
        assert identity.startswith(REPLAY_INTERACTION_ID_PREFIX)

    @pytest.mark.parametrize(
        ("field", "value"),
        [("principal", "bob-person"), ("speaker", "bob"), ("scope", "dm:bob")],
    )
    def test_every_key_axis_changes_the_identity(self, field, value):
        base = replay_span_identity(_record(), "ember-owl", "live", "internal", ["m-1"])
        other = replay_span_identity(
            _record(**{field: value}), "ember-owl", "live", "internal", ["m-1"],
        )
        assert base != other, (
            f"{field} must discriminate — one record's derivation would "
            "otherwise suppress a sibling's in the same room"
        )

    def test_the_agent_discriminates(self):
        # Two personas in the same room legitimately derive their own
        # episode from the same messages; ``episodes.interaction_id`` is
        # not constrained to one agent.
        assert (
            replay_span_identity(_record(), "ember-owl", "live", "internal", ["m-1"])
            != replay_span_identity(_record(), "slate-fox", "live", "internal", ["m-1"])
        )

    def test_a_grown_window_is_a_different_span(self):
        assert (
            replay_span_identity(_record(), "ember-owl", "live", "internal", ["m-1"])
            != replay_span_identity(_record(), "ember-owl", "live", "internal", ["m-1", "m-2"])
        )

    def test_message_order_is_part_of_the_span(self):
        assert (
            replay_span_identity(_record(), "ember-owl", "live", "internal", ["m-1", "m-2"])
            != replay_span_identity(_record(), "ember-owl", "live", "internal", ["m-2", "m-1"])
        )

    def test_the_epoch_discriminates(self):
        # ``store_episode`` stamps the row with the active epoch and every
        # recall filters it with unconditional strict equality, so a shared
        # identity across epochs would have one epoch's row suppress
        # another's derivation — permanently, and of a row it cannot read.
        assert (
            replay_span_identity(_record(), "ember-owl", "live", "internal", ["m-1"])
            != replay_span_identity(_record(), "ember-owl", "eval-7", "internal", ["m-1"])
        )

    def test_no_message_ids_means_no_identity(self):
        assert replay_span_identity(_record(), "ember-owl", "live", "internal", []) is None

    @pytest.mark.parametrize(
        "ids",
        [["m-1", ""], ["", "m-2"], ["m-1", "", "m-3"]],
        ids=["trailing", "leading", "middle"],
    )
    def test_any_unidentified_turn_means_no_identity(self, ids):
        # Not just "no ids at all".  A digest over the identified SUBSET is
        # stable across two spans that differ only in their unidentified
        # turns, so the second would match the first and be skipped — its
        # content never derived and never retried.  ``validate_channel_
        # message_dict`` accepts a row whose ``id`` is missing or empty
        # (it only type- and length-checks), so this is reachable, and the
        # honest answer is to refuse the whole span and derive unguarded.
        assert replay_span_identity(_record(), "ember-owl", "live", "internal", ids) is None

    def test_a_partial_span_does_not_collide_with_its_identified_subset(self):
        subset = replay_span_identity(_record(), "ember-owl", "live", "internal", ["m-1"])
        partial = replay_span_identity(
            _record(), "ember-owl", "live", "internal", ["m-1", ""],
        )
        assert subset is not None
        assert partial is None, (
            "a span holding an unnamed turn must not silently take the "
            "identity of the span that holds only the named one"
        )


class _Episodic:
    """Minimal stand-in for the one method the guard calls."""

    def __init__(self, *, known: bool = False, raises: bool = False,
                 epoch: str = "live"):
        self.known = known
        self.raises = raises
        self.epoch = epoch
        self.asked: list[str] = []
        self.stored: list[str] = []

    async def store_episode(self, **kwargs: object) -> None:
        self.stored.append(str(kwargs.get("interaction_id", "")))

    async def update_episode_summary(self, *a: object, **k: object) -> int:
        # Present so a REGRESSION (the gate removed) fails on this file's
        # own assertions rather than on a missing attribute three frames
        # down in Phase 2.
        return 1

    def active_epoch_id(self) -> str:
        return self.epoch

    async def has_episode_for_interaction(self, interaction_id: str) -> bool:
        self.asked.append(interaction_id)
        if self.raises:
            raise RuntimeError("database is locked")
        return self.known


@pytest.mark.asyncio
class TestTheGuard:
    async def test_it_claims_the_deterministic_id(self):
        record = _record()
        minted_at_open = record.interaction_id
        episodic = _Episodic()

        assert await replay_span_already_derived(
            episodic=episodic, interaction=record, agent_id="ember-owl",
            protection_level="internal",
            message_ids=["m-1"],
        ) is False
        assert record.interaction_id != minted_at_open, (
            "the uuid4 minted at open is boot-derived — the row must be "
            "written under the id the NEXT boot will compute"
        )
        assert episodic.asked == [record.interaction_id]

    async def test_a_known_span_is_reported_as_derived(self):
        assert await replay_span_already_derived(
            episodic=_Episodic(known=True), interaction=_record(),
            agent_id="ember-owl", protection_level="internal",
            message_ids=["m-1"],
        ) is True

    async def test_an_unidentifiable_span_derives_unguarded(self):
        record = _record()
        minted_at_open = record.interaction_id
        episodic = _Episodic(known=True)

        assert await replay_span_already_derived(
            episodic=episodic, interaction=record, agent_id="ember-owl",
            protection_level="internal",
            message_ids=[],
        ) is False
        assert episodic.asked == [], "nothing to ask about"
        assert record.interaction_id == minted_at_open, (
            "with no identity to claim the record keeps its own id"
        )

    async def test_a_failing_lookup_derives_rather_than_skips(self):
        # The guard bounds duplication; a transient read error must not
        # cost a span its memory, and the close path gets one attempt.
        record = _record()
        minted_at_open = record.interaction_id

        assert await replay_span_already_derived(
            episodic=_Episodic(raises=True), interaction=record,
            agent_id="ember-owl", protection_level="internal",
            message_ids=["m-1"],
        ) is False
        assert record.interaction_id == minted_at_open, (
            "a failed lookup must NOT claim the digest: deriving twice is "
            "the accepted residual, but two rows under one interaction_id "
            "would have Phase 2's unbounded UPDATE rewrite both"
        )

    async def test_the_guard_asks_under_the_records_frozen_epoch(self):
        """The epoch comes off the record, not off the ambient context.

        The digest must be built from the epoch the row will be stamped
        with, or the guard asks about an id the write never uses — and
        ``close_path._record_write_scopes`` binds exactly
        ``interaction.epoch_id`` around ``store_episode``.  Reading the
        AMBIENT epoch instead (the first cut, via
        ``episodic.active_epoch_id()``) made the digest depend on WHICH
        close path fired rather than on the span: the pass-end sweep runs
        with no request scope bound, the ingest-time split runs inside a
        live event's ``on_event`` with that request's epoch bound, so the
        same window hashed two ways and the guard missed (PR B2 review).

        The tier is deliberately handed the SAME epoch in both calls
        here: if the guard were still reading it, the two digests would
        match and this test would fail.
        """
        live_record = _record()
        live_record.epoch_id = "live"
        eval_record = _record()
        eval_record.epoch_id = "eval-7"

        tier_a, tier_b = _Episodic(epoch="live"), _Episodic(epoch="live")
        await replay_span_already_derived(
            episodic=tier_a, interaction=live_record, agent_id="ember-owl",
            protection_level="internal",
            message_ids=["m-1"],
        )
        await replay_span_already_derived(
            episodic=tier_b, interaction=eval_record, agent_id="ember-owl",
            protection_level="internal",
            message_ids=["m-1"],
        )
        assert tier_a.asked != tier_b.asked, (
            "two epochs must derive and read their own: a row written "
            "under one epoch cannot be allowed to suppress derivation "
            "under another, where strict-equality recall can never see it"
        )

    async def test_the_tier_answers_only_when_the_record_froze_no_epoch(self):
        """The fallback, and it has to match what the write does.

        A record minted by a site that captures no epoch (a bare
        ``Interaction(...)``) leaves resolution ambient on BOTH sides —
        ``_record_write_scopes`` binds nothing for it either — so the two
        still agree.
        """
        record = _record()
        record.epoch_id = ""
        live, evaluated = _Episodic(epoch="live"), _Episodic(epoch="eval-7")

        await replay_span_already_derived(
            episodic=live, interaction=record, agent_id="ember-owl",
            protection_level="internal",
            message_ids=["m-1"],
        )
        record.epoch_id = ""
        await replay_span_already_derived(
            episodic=evaluated, interaction=record, agent_id="ember-owl",
            protection_level="internal",
            message_ids=["m-1"],
        )
        assert live.asked != evaluated.asked

    async def test_the_tracker_freezes_an_epoch_on_every_record(self):
        """The premise the two tests above rest on.

        If ``InteractionTracker`` stopped capturing it, every record would
        fall to the ambient branch and the instability would be back with
        both tests still green.
        """
        assert _record().epoch_id, (
            "a tracker-opened record must carry its own epoch — the close "
            "path re-binds this value, so a blank one silently restores "
            "ambient resolution at close time"
        )


# ─── What the CLOSE PATH feeds it (PR B2 review finding 11) ────────────


@pytest.mark.asyncio
async def test_the_close_path_digests_every_turn_not_the_filtered_view(
) -> None:
    """``persist_closed_interaction`` must hand over ``interaction.turns``.

    It used to read the ids off ``own_turn_items`` — the RFC 0020 §G
    POST-EXCLUSION view — which is a digest over a subset by
    construction, the exact shape :func:`replay_span_identity` refuses
    two paragraphs into its own docstring.  The two agree today only
    because a different module holds the invariant (``admitted_records``
    keeps the room-close fan off replayed records), which is not where
    this rule is stated; a change there would silently make two different
    spans hash the same and cost the second its memory.
    """
    from agents.memory.interaction_types import ROOM_CLOSE_TURN_KEY
    from agents.persona_runtime.close_path import persist_closed_interaction

    tracker = InteractionTracker()
    record = tracker.add_turn(
        "group:planning",
        payload={"summary": "s", "text": "hi", "message_id": "m-1"},
        replayed=True, replay_attributed=True,
        principal_id="alice-person", speaker_id="alice",
    )
    # A turn the §G chokepoint EXCLUDES from the derivation input: a
    # room-close message somebody else spoke.
    tracker.add_turn(
        "group:planning",
        payload={
            "summary": "closing", "sender": "robin", "message_id": "m-close",
            ROOM_CLOSE_TURN_KEY: True,
        },
        principal_id="alice-person", speaker_id="alice",
    )
    closed = tracker.close_record(record, reason="structural")
    assert closed is not None
    # This close is a NON-sweep door, so the completeness gate would refuse
    # it outright (that is its own test below).  Mark the record derivable
    # so this test isolates WHICH TURNS the digest is taken over.
    closed.replay_window_complete = True

    episodic = _Episodic(known=True)  # skip before any real write
    await persist_closed_interaction(
        episodic=episodic,  # type: ignore[arg-type]
        llm_client=None,  # type: ignore[arg-type]
        memory_ns=None,  # type: ignore[arg-type]
        agent_id="ember-owl", interaction=closed,
        pending_tasks=set(),
        on_finalized=_noop,
    )

    expected = replay_span_identity(
        closed, "ember-owl", closed.epoch_id, "internal", ["m-1", "m-close"],
    )
    assert episodic.asked == [expected], (
        "the excluded turn is still part of WHICH SPAN this is, even "
        "though it is not part of what the span derives"
    )


async def _noop() -> None:
    return None


async def test_a_replayed_record_closed_off_the_sweep_never_derives():
    """The ISSUE-0130 (b) completeness gate, at the shared chokepoint.

    A replayed record reaches ``persist_closed_interaction`` through four
    doors, and only the pass-end sweep can know the record holds a WHOLE
    replay window.  The other three — the ingest-time replay/live split, a
    wire rotation reaching a non-target record, and ``idle_check`` — close
    it mid-pass, so it holds a PREFIX, and the span digest over a prefix is
    an id no later boot recomputes: the next complete boot derives the whole
    window on top of it, unbounded across boots.

    While the decision lived in the sweep's own loop body all three of those
    doors derived prefixes unguarded (PR B2 review).  Reading a frozen field
    here fixes every door at once and, more to the point, FAILS CLOSED — a
    future close path that has never heard of replay windows cannot derive
    one by omission.
    """
    from agents.persona_runtime.close_path import persist_closed_interaction

    tracker = InteractionTracker()
    record = tracker.add_turn(
        "group:planning",
        payload={"summary": "s", "text": "hi", "message_id": "m-1"},
        replayed=True, replay_attributed=True,
        source_channel_id="group:planning",
        principal_id="alice-person", speaker_id="alice",
    )
    # The finding-2 path verbatim: a live wire rotation reaching a replayed
    # record that is not the arriving event's own.
    closed = tracker.close_record(record, reason="structural")
    assert closed is not None
    assert not closed.replay_window_complete, (
        "only the pass-end sweep may mark a replayed record derivable"
    )

    episodic = _Episodic(known=False)
    await persist_closed_interaction(
        episodic=episodic,  # type: ignore[arg-type]
        llm_client=None,  # type: ignore[arg-type]
        memory_ns=None,  # type: ignore[arg-type]
        agent_id="ember-owl", interaction=closed,
        pending_tasks=set(),
        on_finalized=_noop,
    )

    assert episodic.asked == [], (
        "a prefix must not even ASK the guard — claiming the digest is "
        "what makes the duplicate permanent"
    )
    assert not episodic.stored, "and it must not write a row"
