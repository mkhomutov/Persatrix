"""A COMPROMISED replay window derives nothing, at every door.

Added by the v0.3.15 PR B2 review.  ISSUE-0130 (b) bounds re-derivation
with a content digest, which only works while the record the digest covers
holds a whole window.  Three separate defects let a record that did NOT
hold one derive anyway, and this suite pins all three plus the rule they
share:

* the truncation bookkeeping was keyed per CHANNEL while records are keyed
  per ``(principal, speaker, scope)``, so one live turn racing catch-up in
  a busy room refused every OTHER speaker's complete window there;
* the ingest-time segmentation door
  (:func:`~agents.persona_runtime.close_path.close_stale_records`) marked
  records derivable without consulting any of that bookkeeping — so it
  derived the remainder of an already-cut window, derived segments with a
  hole where a row had raised, and fired on the ISSUE-0130 ATTRIBUTION
  split as well as on the wire rotation its comment described; and
* the sweep's own "the record must name a channel at all" condition was
  documented but never written, holding only by accident of ``"" in
  derive_channels`` being false.

The shared rule: a window is compromised by ``(channel, speaker)``, both
doors ask the same question, and the answer defaults to "not derivable".
"""

from __future__ import annotations

import pytest

from agents.memory.interaction_tracker import InteractionTracker
from agents.memory.interaction_types import Interaction
from agents.persona_runtime.close_path import close_stale_records
from agents.persona_runtime.replay_sweep import close_replayed_scopes
from agents.persona_types import AgentEvent, EventType

CHANNEL = "group:planning"
SCOPE = "group:planning"


def _event(
    sender: str, wire: str, *, replayed: bool, attributed: bool = True,
) -> AgentEvent:
    """One CHANNEL_MESSAGE, live or replayed, carrying ``wire``."""
    metadata: dict[str, object] = {"interaction_id": wire}
    if replayed:
        metadata["replay_mode"] = True
        if attributed:
            metadata["persatrix_principal"] = "local"
    return AgentEvent(
        event_type=EventType.CHANNEL_MESSAGE,
        payload={"content": "x", "channel_type": "group"},
        channel_id=CHANNEL, sender_id=sender,
        message_id=f"m-{sender}-{wire}", metadata=metadata,
    )


def _replayed_record(
    tracker: InteractionTracker, speaker: str, *, wire: str = "wire-A",
    channel: str | None = CHANNEL,
) -> Interaction:
    record = tracker.add_turn(
        SCOPE, payload={"message_id": f"m-{speaker}"}, replayed=True,
        replay_attributed=True, speaker_id=speaker,
        source_channel_id=channel,
    )
    record.wire_interaction_id = wire
    return record


async def _sweep(tracker: InteractionTracker, **kwargs) -> dict[str, bool]:
    """Run the pass-end sweep; return ``speaker -> may derive``."""
    seen: dict[str, bool] = {}

    async def _persist(interaction: Interaction) -> None:
        seen[interaction.speaker_id] = interaction.replay_window_complete

    await close_replayed_scopes(tracker, _persist, **kwargs)
    return seen


@pytest.mark.asyncio
class TestOneSpeakersCutIsNotTheRoomsCut:
    """Truncation is per ``(channel, speaker)``, not per channel.

    Dispatch self-registers BEFORE catch-up runs (``AgentServer.start``),
    so a live turn landing mid-pass is ordinary rather than an edge case.
    Charging that one speaker's split to the whole channel meant every
    other speaker in the room lost their derivation — and in a room that
    always has traffic at boot, lost it on EVERY boot, which is the
    v0.3.14 cost the release exists to remove.  It is also the identical
    defect the review had already fixed one field over for
    ``speaker_gaps``.
    """

    async def test_a_live_split_refuses_only_that_speakers_window(
        self,
    ) -> None:
        tracker = InteractionTracker()
        for speaker in ("alice", "bob", "carol"):
            _replayed_record(tracker, speaker)

        # One LIVE turn from alice on the SAME wire id: the ISSUE-0130
        # catch-up boundary splits alice's record and no one else's
        # (``is_target_record``), truncating exactly one window.
        await close_stale_records(
            tracker, SCOPE, _event("alice", "wire-A", replayed=False),
            wire_id="wire-A", persist=_noop,
        )
        assert {r.speaker_id for r in tracker.records_for_scope(SCOPE)} == {
            "bob", "carol",
        }, "precondition: only alice's record was split"

        assert await _sweep(
            tracker, derive_channels=frozenset({CHANNEL}),
        ) == {"bob": True, "carol": True}, (
            "bob and carol hold their whole windows untouched; alice's cut "
            "is hers alone"
        )

    async def test_the_cut_speakers_own_remainder_is_still_refused(
        self,
    ) -> None:
        """The narrowing must not become a hole.

        What is still open for the speaker whose window WAS cut is the
        remainder of that window, and it is no more derivable than the
        prefix that was cut off it.
        """
        tracker = InteractionTracker()
        _replayed_record(tracker, "alice")
        await close_stale_records(
            tracker, SCOPE, _event("alice", "wire-A", replayed=False),
            wire_id="wire-A", persist=_noop,
        )
        # Catch-up resumes and reopens alice's record with the rest.
        _replayed_record(tracker, "alice")

        assert await _sweep(
            tracker, derive_channels=frozenset({CHANNEL}),
        ) == {"alice": False}


@pytest.mark.asyncio
class TestTheSegmentationDoorAsksTheSameQuestion:
    """``close_stale_records`` may mark a segment complete — conditionally.

    A wire ROTATION between two replayed rows is a genuine conversation
    boundary inside the window: the segment it closes holds a whole wire
    conversation, so its digest is boot-stable.  That is the only case,
    and the first cut checked none of it.
    """

    async def test_a_replayed_rotation_marks_a_clean_segment_complete(
        self,
    ) -> None:
        tracker = InteractionTracker()
        record = _replayed_record(tracker, "alice")
        await close_stale_records(
            tracker, SCOPE, _event("alice", "wire-B", replayed=True),
            wire_id="wire-B", persist=_noop,
        )
        assert record.replay_window_complete is True, (
            "an uncompromised replay-internal rotation still derives — "
            "otherwise a window with N conversations derives only its last"
        )

    async def test_a_rotation_after_a_cut_derives_nothing(self) -> None:
        """The ``already_cut`` bypass.

        A live turn truncates alice's record; catch-up resumes into a new
        one; a later replayed row rotates and closes it.  That record is
        the REMAINDER of a cut window, and deriving it claims a digest no
        later boot recomputes — the next complete boot then derives the
        whole window on top of it, which is the growth curve shape (b)
        exists to bound, reached through the one door that did not ask.
        """
        tracker = InteractionTracker()
        _replayed_record(tracker, "alice")
        await close_stale_records(
            tracker, SCOPE, _event("alice", "wire-A", replayed=False),
            wire_id="wire-A", persist=_noop,
        )
        remainder = _replayed_record(tracker, "alice")

        await close_stale_records(
            tracker, SCOPE, _event("alice", "wire-B", replayed=True),
            wire_id="wire-B", persist=_noop,
        )
        assert remainder.replay_window_complete is False

    async def test_a_rotation_after_a_raised_row_derives_nothing(
        self,
    ) -> None:
        """The ``speaker_gaps`` bypass.

        A row that raised inside ``on_event`` leaves a hole this boot
        invented, so the segment's digest is not one a later boot
        recomputes.  The catch-up loop records that on
        ``ReplayPassOutcome``, which only the pass-END sweep reads — so the
        fact is routed into the tracker as well, where this door can see it.
        """
        tracker = InteractionTracker()
        record = _replayed_record(tracker, "alice")
        tracker.note_replay_gap(CHANNEL, "alice")

        await close_stale_records(
            tracker, SCOPE, _event("alice", "wire-B", replayed=True),
            wire_id="wire-B", persist=_noop,
        )
        assert record.replay_window_complete is False

    async def test_an_unnameable_senders_gap_takes_the_whole_channel(
        self,
    ) -> None:
        """A gap that cannot be attributed could be in ANY record."""
        tracker = InteractionTracker()
        record = _replayed_record(tracker, "bob")
        tracker.note_replay_gap(CHANNEL, "")

        await close_stale_records(
            tracker, SCOPE, _event("bob", "wire-B", replayed=True),
            wire_id="wire-B", persist=_noop,
        )
        assert record.replay_window_complete is False

    async def test_an_attribution_split_is_not_a_rotation(self) -> None:
        """``REASON_CATCHUP_COMPLETE`` closes a PREFIX, not a conversation.

        ``stale_close_reason`` answers it for a ``replay_attributed``
        disagreement too — a seeded ``"local"`` row meeting an unseeded one
        under the same record key.  That record holds however much replay
        had ingested when the disagreement arrived, so it must not be
        marked complete.
        """
        tracker = InteractionTracker()
        record = _replayed_record(tracker, "alice")
        # Same wire id, so nothing rotates; only the attribution differs.
        await close_stale_records(
            tracker, SCOPE,
            _event("alice", "wire-A", replayed=True, attributed=False),
            wire_id="wire-A", persist=_noop,
        )
        assert record.close_reason == "catchup_complete", (
            "precondition: this is the attribution split, not a rotation"
        )
        assert record.replay_window_complete is False

    async def test_a_live_event_never_marks_a_replayed_record_complete(
        self,
    ) -> None:
        tracker = InteractionTracker()
        record = _replayed_record(tracker, "alice")
        await close_stale_records(
            tracker, SCOPE, _event("alice", "wire-B", replayed=False),
            wire_id="wire-B", persist=_noop,
        )
        assert record.replay_window_complete is False


@pytest.mark.asyncio
class TestTheSweepsOwnConditions:
    async def test_a_channel_less_record_never_derives(self) -> None:
        """The documented fourth condition, now written down in code.

        It used to hold only because ``"" in derive_channels`` is false,
        which said nothing at all on the documented ``derive_channels=None``
        path — the default every direct caller and test takes.
        """
        tracker = InteractionTracker()
        _replayed_record(tracker, "alice", channel=None)
        assert await _sweep(tracker) == {"alice": False}

    async def test_the_sweeps_own_closes_do_not_disqualify_siblings(
        self,
    ) -> None:
        """Each record decides BEFORE it is closed.

        The decision is handed to ``close_record`` rather than assigned
        after it returns, so a record cannot self-register as a truncation
        on the way out.  That is what lets the compromised set be read live
        instead of snapshotted before the loop — a snapshot could not see a
        truncation landing during the loop's own ``await persist(...)``,
        which runs unlocked while dispatch is serving.
        """
        tracker = InteractionTracker()
        for speaker in ("alice", "bob", "carol"):
            _replayed_record(tracker, speaker)

        assert await _sweep(
            tracker, derive_channels=frozenset({CHANNEL}),
        ) == {"alice": True, "bob": True, "carol": True}


async def _noop(interaction: Interaction) -> None:
    """Persist stand-in — these tests assert on the flag, not the write."""
