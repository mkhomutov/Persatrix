"""v0.3.15 residuals PR 3 — the close notification as a ROOM fan.

Split out of ``test_interaction_close_notification.py`` when these pins
pushed it past the 500-line cap (``scripts/checks/file_size.py
--strict``); the harness and event builder are imported from there, the
``test_close_notification_redelivery`` precedent.

Since the ``(principal, speaker, scope)`` tracker re-key (ISSUE-0123 /
ISSUE-0131) the notified scope holds one record per speaker per tenant,
and the notification is a room event: the closing message lands as the
final turn of EACH record the wire-id conjunct admits (a direct
per-record ``append_turn``, never the per-event ingest path), and every
one of them closes with the truthful cause.  This suite carries the
residuals-plan PR 3 test — "the close-notification turn lands as the
final turn of each" — plus the two corners the old per-event ingest's
identity guard used to pin, restated for the fan.
"""

from __future__ import annotations

import time

from agents.memory.boundary_detectors import (
    REASON_STRUCTURAL,
    MaxTurnsDetector,
    default_detectors,
)
from agents.memory.interactions import InteractionTracker
from agents.persona_runtime.close_notification import (
    close_interaction_on_notification,
)

from .test_interaction_close_notification import (
    _CloseNotificationAgent,
    _notification_event,
)


class TestCloseNotificationRoomFan:
    async def test_room_fan_lands_final_turn_on_each_record(self):
        """The notified scope holds one record per ``(principal,
        speaker)`` pair — the closing message must land as the final
        turn of EACH and every record must close with the truthful
        cause, or the siblings leak open until idle relabels the ended
        conversation (ISSUE-0123 part 3)."""
        tracker = InteractionTracker()
        for speaker in ("iron-fox", "nova-sparrow", "ember-persona"):
            tracker.add_turn(
                "group:planning", speaker_id=speaker, now=time.time(),
            )
        agent = _CloseNotificationAgent(tracker)

        await close_interaction_on_notification(agent, _notification_event())

        assert tracker.records_for_scope("group:planning") == [], (
            "the room fan closes every record, not just the sender's"
        )
        assert len(agent.persisted) == 3
        assert {i.speaker_id for i in agent.persisted} == {
            "iron-fox", "nova-sparrow", "ember-persona",
        }
        for record in agent.persisted:
            assert record.close_reason == REASON_STRUCTURAL
            assert record.turns[-1].payload["sender"] == "iron-fox", (
                "the closing message is the final turn of EACH record"
            )
            assert record.turn_count == 2

    async def test_room_fan_stamps_one_instant_across_records(self):
        """One room event, one timestamp (v0.3.15 PR 3 review fix): the
        fan reads the clock seam ONCE and hands every ``append_turn`` /
        ``close_record`` the same instant — per-call reads gave the one
        closing message N different ``Turn.at`` values (and N different
        ``closed_at``) across sibling records."""
        ticks = iter(range(1_000, 2_000))
        tracker = InteractionTracker(clock=lambda: float(next(ticks)))
        for speaker in ("iron-fox", "nova-sparrow", "ember-persona"):
            tracker.add_turn("group:planning", speaker_id=speaker)
        agent = _CloseNotificationAgent(tracker)

        await close_interaction_on_notification(agent, _notification_event())

        assert len(agent.persisted) == 3
        assert len({i.turns[-1].at for i in agent.persisted}) == 1, (
            "the one closing message must carry ONE timestamp on every record"
        )
        assert len({i.closed_at for i in agent.persisted}) == 1, (
            "one room event, one close instant"
        )

    async def test_cap_crossing_final_turn_closes_once_with_truthful_cause(self):
        """The cap corner under the room fan: the notification's final
        turn lands via a DIRECT per-record append (``append_turn``),
        which deliberately does not enforce the max-turns cap — the
        record closes in the same step, and the notification's truthful
        trigger outranks the cap label (``append_turn``'s contract; the
        old ``_store_event_episode`` ingest, whose inline cap-close the
        pre-fan pin exercised, is no longer driven by this path). What
        that pin actually protected still holds and is asserted:
        exactly ONE persisted record, no second close layered on it."""
        tracker = InteractionTracker(
            detectors=(
                *default_detectors(),
                MaxTurnsDetector(max_turns=2),
            ),
        )
        tracker.add_turn("group:planning", now=time.time())
        agent = _CloseNotificationAgent(tracker)

        await close_interaction_on_notification(agent, _notification_event())

        assert [i.close_reason for i in agent.persisted] == [REASON_STRUCTURAL], (
            "exactly one close, labelled by the notification's truthful "
            "trigger — the cap-th turn being the closing message does not "
            "relabel an imminent room close as max_turns"
        )
        assert agent.persisted[0].turn_count == 2
        assert tracker.get("group:planning") is None

    async def test_mismatched_wire_id_fabricates_no_successor(self):
        """The rotation corner under the room fan: a notification whose
        wire id no longer matches the open record is a stale straggler
        — the wire-id conjunct SKIPS the record (nothing closed, nothing
        persisted), and because the ingest is a direct per-record append
        rather than a ``_store_event_episode`` pass, no 1-turn successor
        is fabricated to hold the straggler's message. This replaces the
        old identity-guard pin, whose ingest-driven rotation path
        retired with the per-event ingest; the contract it protected —
        never mint a fabricated "ended" record — is now structural and
        pinned here from the other side."""
        tracker = InteractionTracker()
        record = tracker.add_turn("group:planning", now=time.time())
        record.wire_interaction_id = "wire-SUCCESSOR"
        agent = _CloseNotificationAgent(tracker)

        event = _notification_event()
        event.metadata["interaction_id"] = "wire-RETIRED"
        await close_interaction_on_notification(agent, event)

        assert agent.persisted == [], (
            "the straggler closes nothing — the notified close stands "
            "recorded orchestrator-side"
        )
        survivors = tracker.records_for_scope("group:planning")
        assert survivors == [record], (
            "no successor record fabricated to hold the straggler's message"
        )
        assert record.is_open
        assert record.turn_count == 1, "the straggler's turn is not ingested"

    async def test_replayed_record_is_left_to_the_catchup_sweep(self):
        """PR #846 review: a replay-opened record belongs to the pass-end
        ``REASON_CATCHUP_COMPLETE`` sweep — a live notification arriving
        mid catch-up must neither land its turn in the flagged span
        (derivation skips it wholesale, silently discarding the turn) nor
        relabel the close.  The live sibling still closes normally."""
        tracker = InteractionTracker()
        live = tracker.add_turn(
            "group:planning", speaker_id="nova-sparrow", now=time.time(),
        )
        replayed = tracker.add_turn(
            "group:planning", speaker_id="iron-fox", replayed=True,
            now=time.time(),
        )
        agent = _CloseNotificationAgent(tracker)

        await close_interaction_on_notification(agent, _notification_event())

        assert replayed.is_open, (
            "the replay-opened record is the catch-up sweep's to close"
        )
        assert replayed.turn_count == 1, (
            "the live closing turn must not land inside a flagged span"
        )
        assert [i.speaker_id for i in agent.persisted] == ["nova-sparrow"]
        assert live.close_reason == REASON_STRUCTURAL

    async def test_blank_stamped_record_is_backfilled_with_notified_id(self):
        """PR #846 review: a blank-stamped record the tolerant conjunct
        admitted closes AS the notified conversation — stamp it, so the
        metered summary leases against the id (``summarize_close`` skips
        the lease on a blank one) and the persisted episode keeps the
        governance cross-reference the retired ingest path used to
        backfill."""
        tracker = InteractionTracker()
        record = tracker.add_turn("group:planning", now=time.time())
        agent = _CloseNotificationAgent(tracker)

        event = _notification_event()
        event.metadata["interaction_id"] = "wire-NOTIFIED"
        await close_interaction_on_notification(agent, event)

        assert agent.persisted == [record]
        assert record.wire_interaction_id == "wire-NOTIFIED"
