"""RFC 0052 PR 4b-ii — the close-notification redelivery marker + the
truthful bounded-close cause, receiver half.

TDD-first: written against the planned API, pinning the two wire fields
PR 4b-ii adds to ``ChannelMessageEvent`` (``docs/rfcs/0052-pr-plan.md``):

* ``close_notification_redelivery = 28`` — resolves the PR 4b-i KNOWN
  LIMIT documented in ``close_notification.py``: on a FLOOR-path bounded
  close the closing message already reached every member live inside its
  floor round, so the marked notification's ingest was a RE-delivery —
  one duplicate final turn + ``turn_count`` inflated by one per
  non-sender member per close. A marked redelivery now closes the scope
  WITHOUT the ingest; an unmarked notification (end-vote, concurrent
  path, every old producer) keeps the ingest-then-close behaviour
  byte-for-byte.
* ``close_notification_close_trigger = 29`` — the truthful
  ``structural`` | ``cost`` cause of a bounded close, stamped only by
  the RFC 0052 bounded close. ``cost`` now closes the local record with
  the truthful :data:`REASON_COST` instead of the documented 4b-i
  :data:`REASON_STRUCTURAL` fallback, and the field's PRESENCE is the
  OQ #6 metering key: it marks the closed interaction so its RFC 0020
  close summary draws a wallet lease (``meter_close_summary`` →
  ``summarize_close.py``). Absent — every end-vote/idle notification,
  every human channel, every old producer — keeps the structural label
  and the unleased summary, so both fields are additive across a
  mixed-version deployment.

Both consumers follow the strict-marker discipline of
``interaction_close_notification`` (typed-field-only lift, strict-bool /
allowlisted-string reads).
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import grpc

from agents.memory.boundary_detectors import REASON_COST, REASON_STRUCTURAL
from agents.memory.interactions import InteractionTracker
from agents.persona_types import AgentEvent, EventType

from ._receive_channel_message_helpers import (
    channel_event,
    enqueued_event,
    make_servicer,
)
from .test_interaction_close_notification import _CloseNotificationAgent


def _notification_event(
    *,
    redelivery: object = None,
    close_trigger: object = None,
    sender_id: str = "iron-fox",
    channel_id: str = "group:planning",
) -> AgentEvent:
    """A bounded-close notification as the runtime sees it post-lift —
    the ``_notification_event`` builder shape with the 4b-ii keys
    seeded onto the payload port only when given (typed-field-only)."""
    payload: dict[str, object] = {
        "content": "Round bound reached; synthesis follows.",
        "channel_type": "group",
        "mentions": [],
        "respond_policy": "always",
        "thread_parent_sender_id": "",
        "interaction_close_notification": True,
    }
    if redelivery is not None:
        payload["close_notification_redelivery"] = redelivery
    if close_trigger is not None:
        payload["close_notification_close_trigger"] = close_trigger
    return AgentEvent(
        event_type=EventType.CHANNEL_MESSAGE,
        payload=payload,
        channel_id=channel_id,
        sender_id=sender_id,
    )


class TestRedeliveryWireLift:
    """The servicer lift: both 4b-ii fields ride into the payload from
    the typed proto fields only (the ``interaction_close_notification``
    posture — key-ABSENCE on ordinary traffic is load-bearing)."""

    async def test_marked_redelivery_lifts_into_payload(self):
        servicer, dispatcher = make_servicer()
        await servicer.ReceiveChannelMessage(
            channel_event(
                interaction_id="int-A",
                interaction_close_notification=True,
                close_notification_redelivery=True,
                close_notification_close_trigger="structural",
            ),
            MagicMock(spec=grpc.aio.ServicerContext),
        )
        event = enqueued_event(dispatcher)
        assert event.payload.get("close_notification_redelivery") is True
        assert event.payload.get("close_notification_close_trigger") == "structural"

    async def test_unmarked_event_does_not_carry_the_keys(self):
        """Typed-field-only, the negative half: ordinary traffic (and an
        end-vote notification, whose producer stamps neither field) must
        never grow the keys."""
        servicer, dispatcher = make_servicer()
        await servicer.ReceiveChannelMessage(
            channel_event(
                interaction_id="int-A",
                interaction_close_notification=True,
            ),
            MagicMock(spec=grpc.aio.ServicerContext),
        )
        event = enqueued_event(dispatcher)
        assert "close_notification_redelivery" not in event.payload
        assert "close_notification_close_trigger" not in event.payload

    async def test_unrecognised_trigger_degrades_to_absent(self):
        """The field-21 allowlist posture: only the two causes the
        bounded close actually stamps (``structural`` / ``cost``) ride
        the lift — a non-Go (or compromised) producer's garbage value,
        and even a legal field-21 vocabulary member the bounded close
        never stamps here (``idle`` / ``end_votes``), seed nothing, so
        the metering key can never be widened from the wire."""
        for impostor in ("idle", "end_votes", "zz-spoofed"):
            servicer, dispatcher = make_servicer()
            await servicer.ReceiveChannelMessage(
                channel_event(
                    interaction_id="int-A",
                    interaction_close_notification=True,
                    close_notification_close_trigger=impostor,
                ),
                MagicMock(spec=grpc.aio.ServicerContext),
            )
            event = enqueued_event(dispatcher)
            assert "close_notification_close_trigger" not in event.payload, impostor


class TestRedeliverySkipsTheIngest:
    """The KNOWN-LIMIT fix: a marked redelivery closes the scope without
    re-ingesting the closing message the member already holds."""

    async def test_redelivery_closes_without_ingesting_the_turn(self):
        from agents.persona_runtime.close_notification import (
            close_interaction_on_notification,
        )

        tracker = InteractionTracker()
        tracker.add_turn("group:planning", now=time.time())
        agent = _CloseNotificationAgent(tracker)  # non-sender recipient

        await close_interaction_on_notification(
            agent,
            _notification_event(redelivery=True, close_trigger="structural"),
        )

        assert tracker.get("group:planning") is None, "the close still fires"
        assert agent.ingested == [], (
            "the bounding stimulus was already ingested live inside its "
            "floor round — the notification must not append it again"
        )
        assert len(agent.persisted) == 1
        assert agent.persisted[0].turn_count == 1, (
            "turn_count no longer inflated by the duplicate final turn"
        )

    async def test_sole_delivery_still_ingests(self):
        """The unmarked notification (end-vote, concurrent-path bounded
        close, old producers) keeps ingest-then-close byte-for-byte —
        skipping there would LOSE the closing message."""
        from agents.persona_runtime.close_notification import (
            close_interaction_on_notification,
        )

        tracker = InteractionTracker()
        tracker.add_turn("group:planning", now=time.time())
        agent = _CloseNotificationAgent(tracker)

        await close_interaction_on_notification(agent, _notification_event())

        assert agent.persisted[0].turn_count == 2, (
            "the closing message landed as the record's final turn (the "
            "v0.3.15 room fan ingests via a direct per-record append, so "
            "the turn itself — not a ``_store_event_episode`` call — is "
            "the ingest evidence)"
        )
        assert agent.persisted[0].turns[-1].payload["sender"] == "iron-fox"


    async def test_redelivery_without_bounded_trigger_still_ingests(self):
        """PR #718 review — the redelivery ingest-skip is co-gated on
        ``bounded``. A ``redelivery=True`` marker WITHOUT a recognized
        bounded trigger is a SOLE delivery: the message did not reach the
        member live (only the FLOOR-path bounded close redelivers), so
        skipping its ingest would LOSE the closing turn. This is the
        post-lift shape of a non-Go (or compromised) producer sending
        ``redelivery`` beside an ``idle``/``end_votes``/garbage cause —
        the trigger is allowlisted away at the lift, leaving ``bounded``
        False. Only a genuine bounded-close redelivery skips."""
        from agents.persona_runtime.close_notification import (
            close_interaction_on_notification,
        )

        tracker = InteractionTracker()
        tracker.add_turn("group:planning", now=time.time())
        agent = _CloseNotificationAgent(tracker)  # non-sender recipient

        await close_interaction_on_notification(
            agent, _notification_event(redelivery=True),  # no bounded trigger
        )

        assert tracker.get("group:planning") is None, "the close still fires"
        assert agent.persisted[0].turn_count == 2, (
            "redelivery without a bounded trigger is a sole delivery — the "
            "closing turn must still be ingested, not silently dropped"
        )
        assert agent.persisted[0].turns[-1].payload["sender"] == "iron-fox"

    async def test_redelivery_marker_is_strictly_boolean(self):
        """A truthy non-bool on the cleartext port must not suppress the
        ingest — dropping a sole-delivered closing turn is exactly the
        loss the strict-bool rule prevents."""
        from agents.persona_runtime.close_notification import (
            close_interaction_on_notification,
        )

        tracker = InteractionTracker()
        tracker.add_turn("group:planning", now=time.time())
        agent = _CloseNotificationAgent(tracker)

        await close_interaction_on_notification(
            agent, _notification_event(redelivery="true"),
        )

        assert agent.persisted[0].turn_count == 2, (
            "impostor marker: ingest stands"
        )
        assert agent.persisted[0].turns[-1].payload["sender"] == "iron-fox"


class TestTruthfulCloseCause:
    """The truthful ``structural`` / ``cost`` cause replaces the 4b-i
    REASON_STRUCTURAL fallback on the close-notification path."""

    async def test_cost_trigger_closes_with_reason_cost(self):
        from agents.persona_runtime.close_notification import (
            close_interaction_on_notification,
        )

        tracker = InteractionTracker()
        tracker.add_turn("group:planning", now=time.time())
        agent = _CloseNotificationAgent(tracker)

        await close_interaction_on_notification(
            agent, _notification_event(close_trigger="cost"),
        )

        assert [i.close_reason for i in agent.persisted] == [REASON_COST]

    async def test_structural_trigger_keeps_reason_structural(self):
        from agents.persona_runtime.close_notification import (
            close_interaction_on_notification,
        )

        tracker = InteractionTracker()
        tracker.add_turn("group:planning", now=time.time())
        agent = _CloseNotificationAgent(tracker)

        await close_interaction_on_notification(
            agent, _notification_event(close_trigger="structural"),
        )

        assert [i.close_reason for i in agent.persisted] == [REASON_STRUCTURAL]

    async def test_absent_trigger_keeps_reason_structural(self):
        """The end-vote notification (and every old producer) closes
        exactly as before — the mixed-version contract."""
        from agents.persona_runtime.close_notification import (
            close_interaction_on_notification,
        )

        tracker = InteractionTracker()
        tracker.add_turn("group:planning", now=time.time())
        agent = _CloseNotificationAgent(tracker)

        await close_interaction_on_notification(agent, _notification_event())

        assert [i.close_reason for i in agent.persisted] == [REASON_STRUCTURAL]

    async def test_garbage_trigger_degrades_to_structural_and_unmetered(self):
        """Defence-in-depth at the consumer (the lift already allowlists):
        a garbage value on the cleartext payload port neither relabels the
        close nor marks the summary for metering."""
        from agents.persona_runtime.close_notification import (
            close_interaction_on_notification,
        )

        tracker = InteractionTracker()
        tracker.add_turn("group:planning", now=time.time())
        agent = _CloseNotificationAgent(tracker)

        await close_interaction_on_notification(
            agent, _notification_event(close_trigger="zz-spoofed"),
        )

        assert [i.close_reason for i in agent.persisted] == [REASON_STRUCTURAL]
        assert agent.persisted[0].meter_close_summary is False


class TestBoundedCloseMetersTheSummary:
    """OQ #6, the marking half: a bounded-close notification (trigger
    present) marks the closed interaction so the close-path summary
    draws a wallet lease; everything else stays unmarked. The lease
    threading itself is pinned in ``test_summarize_close_metering.py``."""

    async def test_bounded_trigger_marks_the_interaction_for_metering(self):
        from agents.persona_runtime.close_notification import (
            close_interaction_on_notification,
        )

        for trigger in ("structural", "cost"):
            tracker = InteractionTracker()
            tracker.add_turn("group:planning", now=time.time())
            agent = _CloseNotificationAgent(tracker)

            await close_interaction_on_notification(
                agent, _notification_event(close_trigger=trigger),
            )

            assert agent.persisted[0].meter_close_summary is True, trigger

    async def test_self_echo_close_is_metered_too(self):
        """The bounded close fans to the round-triggering sender as well
        (``excludeSender=false``); its summary is one of the ``1 + N``
        reserve calls, so the self-echo close carries the mark too."""
        from agents.persona_runtime.close_notification import (
            close_interaction_on_notification,
        )

        tracker = InteractionTracker()
        tracker.add_turn("group:planning", now=time.time())
        agent = _CloseNotificationAgent(tracker, agent_id="iron-fox")  # == sender

        await close_interaction_on_notification(
            agent, _notification_event(close_trigger="cost"),
        )

        assert agent.persisted[0].meter_close_summary is True
        assert agent.persisted[0].turn_count == 1, "self-echo still not ingested"

    async def test_unmarked_close_stays_unmetered(self):
        """The human-channel regression: an end-vote notification (no
        trigger) leaves the flag at its default — the OQ #6 edit is
        autonomous-bounded-close-only."""
        from agents.persona_runtime.close_notification import (
            close_interaction_on_notification,
        )

        tracker = InteractionTracker()
        tracker.add_turn("group:planning", now=time.time())
        agent = _CloseNotificationAgent(tracker)

        await close_interaction_on_notification(agent, _notification_event())

        assert agent.persisted[0].meter_close_summary is False

    async def test_wire_id_mismatch_closes_nothing(self):
        """PR #718 review, the wire-id conjunct: the handler is scope-keyed,
        and the ingest-branch identity guard only catches rotations the
        ingest itself causes — a successor interaction's first publish
        overtaking the fire-and-forget notification leaves a DIFFERENT
        wire id open on the scope before the notification lands. Closing
        it would bury a live discussion's record under the bounded label
        and bill its metered summary to the successor's id against a
        reserve carved for the predecessor. Both ids known and disagreeing
        → the stale-straggler no-op: record untouched, nothing persisted."""
        from agents.persona_runtime.close_notification import (
            close_interaction_on_notification,
        )

        tracker = InteractionTracker()
        record = tracker.add_turn("group:planning", now=time.time())
        record.wire_interaction_id = "wire-successor"
        agent = _CloseNotificationAgent(tracker)

        event = _notification_event(close_trigger="cost", redelivery=True)
        event.metadata["interaction_id"] = "wire-predecessor"
        await close_interaction_on_notification(agent, event)

        assert agent.persisted == []
        still_open = tracker.get("group:planning")
        assert still_open is record and still_open.is_open
        assert still_open.meter_close_summary is False

    async def test_matching_wire_id_keeps_the_metered_close(self):
        """The conjunct's control arm: ids known and EQUAL — the ordinary
        bounded close (and the finding-1 self-echo close it now carries)
        is byte-for-byte unchanged: closed, truthful reason, metered."""
        from agents.persona_runtime.close_notification import (
            close_interaction_on_notification,
        )

        tracker = InteractionTracker()
        record = tracker.add_turn("group:planning", now=time.time())
        record.wire_interaction_id = "wire-ix-1"
        agent = _CloseNotificationAgent(tracker)

        event = _notification_event(close_trigger="cost")
        event.metadata["interaction_id"] = "wire-ix-1"
        await close_interaction_on_notification(agent, event)

        assert [i.close_reason for i in agent.persisted] == [REASON_COST]
        assert agent.persisted[0].meter_close_summary is True

    async def test_cap_crossing_final_turn_is_still_metered(self):
        """PR #718 review's metering concern, restated for the v0.3.15
        room fan: the metering mark must ride the record whichever way
        the close falls. The old shape — the ingest inline-closing at
        the max-turns cap past the post-close mark — retired with the
        ``_store_event_episode`` ingest path (the direct per-record
        append deliberately does not enforce the cap; the record closes
        in the same step with the truthful trigger). What must survive:
        a cap-crossing final turn yields exactly ONE persisted record,
        with the truthful cost cause, and it IS marked for the metered
        summary — the mark is set on the live record before the append,
        so no path can persist an unmetered bounded close."""
        from agents.memory.boundary_detectors import (
            MaxTurnsDetector,
            default_detectors,
        )
        from agents.persona_runtime.close_notification import (
            close_interaction_on_notification,
        )

        tracker = InteractionTracker(
            detectors=(
                *default_detectors(),
                MaxTurnsDetector(max_turns=2),
            ),
        )
        tracker.add_turn("group:planning", now=time.time())
        agent = _CloseNotificationAgent(tracker)  # non-sender recipient

        await close_interaction_on_notification(
            agent, _notification_event(close_trigger="cost"),
        )

        assert [i.close_reason for i in agent.persisted] == [REASON_COST]
        assert agent.persisted[0].turn_count == 2
        assert agent.persisted[0].meter_close_summary is True, (
            "the bounded-close summary must draw a lease even when the "
            "closing turn is the cap-crossing turn"
        )
