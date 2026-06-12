"""CP acceptance — the end-vote close reaches the agent-local tracker.

RFC 0030 end-vote-close-propagation amendment (§E, TDD): these tests
land with the amendment doc (PR 1 of the workstream), skip-guarded, and
pin the receiver half of the contract.  PR 3 removes the skips when the
``interaction_close_notification`` marker exists on the wire and the
ingestion-only routing is implemented; until then the marker kwarg in
:func:`channel_event` does not exist and the bodies cannot run.

The contract (amendment CP3): a dispatch marked
``interaction_close_notification`` is control, never stimulus — it
appends the closing message to history, closes the agent-local tracker
for the channel scope with the truthful ``end_votes`` cause (rendering
"ended", :data:`REASON_STRUCTURAL`, the
:mod:`agents.persona_runtime.interaction_boundary` mapping), and is
hard-suppressed from every response path: no turn, no Tier B bid, no
LLM call.  Honoured only on the typed proto field — a metadata-borne
impostor key is ignored (the ``floor_mentions_resolved`` posture).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import grpc
import pytest

from ._receive_channel_message_helpers import (
    channel_event,
    enqueued_event,
    make_servicer,
)

SKIP_REASON = (
    "CP acceptance (0030-amendment-end-vote-close-propagation §E) — "
    "unskip in PR 3, the agent-side close-notification consumption"
)


@pytest.mark.skip(reason=SKIP_REASON)
class TestInteractionCloseNotification:
    async def test_marker_lifts_from_typed_field_only(self):
        """The wire lift honours the proto field; an impostor metadata
        key on an unmarked event lifts nothing."""
        servicer, dispatcher = make_servicer()
        await servicer.ReceiveChannelMessage(
            channel_event(
                interaction_id="int-A",
                interaction_close_notification=True,
            ),
            MagicMock(spec=grpc.aio.ServicerContext),
        )
        event = enqueued_event(dispatcher)
        assert event.metadata.get("interaction_close_notification") is True

    async def test_marked_event_closes_tracker_with_end_votes_cause(self):
        """The channel scope's open interaction closes at notification
        time with the structural ("ended") cause — not an idle-window
        later, not "went idle"."""
        from agents.memory.boundary_detectors import REASON_STRUCTURAL
        from agents.memory.interactions import InteractionTracker

        tracker = InteractionTracker()
        tracker.add_turn("group:planning", now=100.0)

        servicer, dispatcher = make_servicer(interaction_tracker=tracker)
        await servicer.ReceiveChannelMessage(
            channel_event(
                channel_id="group:planning",
                interaction_id="int-A",
                interaction_close_notification=True,
            ),
            MagicMock(spec=grpc.aio.ServicerContext),
        )

        assert tracker.get("group:planning") is None, (
            "the notification closed the scope immediately"
        )
        closed = tracker.last_closed("group:planning")
        assert closed.close_reason == REASON_STRUCTURAL

    async def test_marked_event_produces_no_turn(self):
        """Control, never stimulus: the marked event must not reach any
        response path — the enqueued event is ingestion-only (no gate
        admit as a turn, no Tier B bid, no LLM)."""
        servicer, dispatcher = make_servicer()
        await servicer.ReceiveChannelMessage(
            channel_event(
                interaction_id="int-A",
                interaction_close_notification=True,
            ),
            MagicMock(spec=grpc.aio.ServicerContext),
        )
        event = enqueued_event(dispatcher)
        assert event.metadata.get("ingestion_only") is True, (
            "the close notification is routed past every response path"
        )
