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
        a = replay_span_identity(_record(), "ember-owl", ["m-1", "m-2"])
        b = replay_span_identity(_record(), "ember-owl", ["m-1", "m-2"])
        assert a is not None and a == b

    def test_it_is_marked_as_a_replay_derivation(self):
        identity = replay_span_identity(_record(), "ember-owl", ["m-1"])
        assert identity is not None
        assert identity.startswith(REPLAY_INTERACTION_ID_PREFIX)

    @pytest.mark.parametrize(
        ("field", "value"),
        [("principal", "bob-person"), ("speaker", "bob"), ("scope", "dm:bob")],
    )
    def test_every_key_axis_changes_the_identity(self, field, value):
        base = replay_span_identity(_record(), "ember-owl", ["m-1"])
        other = replay_span_identity(
            _record(**{field: value}), "ember-owl", ["m-1"],
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
            replay_span_identity(_record(), "ember-owl", ["m-1"])
            != replay_span_identity(_record(), "slate-fox", ["m-1"])
        )

    def test_a_grown_window_is_a_different_span(self):
        assert (
            replay_span_identity(_record(), "ember-owl", ["m-1"])
            != replay_span_identity(_record(), "ember-owl", ["m-1", "m-2"])
        )

    def test_message_order_is_part_of_the_span(self):
        assert (
            replay_span_identity(_record(), "ember-owl", ["m-1", "m-2"])
            != replay_span_identity(_record(), "ember-owl", ["m-2", "m-1"])
        )

    def test_no_message_ids_means_no_identity(self):
        assert replay_span_identity(_record(), "ember-owl", []) is None


class _Episodic:
    """Minimal stand-in for the one method the guard calls."""

    def __init__(self, *, known: bool = False, raises: bool = False):
        self.known = known
        self.raises = raises
        self.asked: list[str] = []

    async def has_episode_for_interaction(self, interaction_id: str) -> bool:
        self.asked.append(interaction_id)
        if self.raises:
            raise RuntimeError("database is locked")
        return self.known


@pytest.mark.asyncio
class TestTheGuard:
    async def test_it_claims_the_deterministic_id_before_asking(self):
        record = _record()
        minted_at_open = record.interaction_id
        episodic = _Episodic()

        assert await replay_span_already_derived(
            episodic=episodic, interaction=record, agent_id="ember-owl",
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
            agent_id="ember-owl", message_ids=["m-1"],
        ) is True

    async def test_an_unidentifiable_span_derives_unguarded(self):
        record = _record()
        minted_at_open = record.interaction_id
        episodic = _Episodic(known=True)

        assert await replay_span_already_derived(
            episodic=episodic, interaction=record, agent_id="ember-owl",
            message_ids=[],
        ) is False
        assert episodic.asked == [], "nothing to ask about"
        assert record.interaction_id == minted_at_open, (
            "with no identity to claim the record keeps its own id"
        )

    async def test_a_failing_lookup_derives_rather_than_skips(self):
        # The guard bounds duplication; a transient read error must not
        # cost a span its memory, and the close path gets one attempt.
        assert await replay_span_already_derived(
            episodic=_Episodic(raises=True), interaction=_record(),
            agent_id="ember-owl", message_ids=["m-1"],
        ) is False
