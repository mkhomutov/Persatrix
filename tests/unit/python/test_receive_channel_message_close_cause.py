"""``ReceiveChannelMessage`` seeding of the OQ 5 close-cause pair.

RFC 0030 interaction-id producer plan OQ 5: the gRPC receive path seeds
``previous_interaction_id`` + ``previous_interaction_close_trigger``
from the typed proto fields as a validated PAIR (id within the byte
cap, trigger in the resolver's ``idle``/``end_votes`` vocabulary) so
the rotation-close seam can label the boundary truthfully.  Anything
else seeds nothing — the rotation close then keeps its legacy
structural label (the mixed-version contract for an old orchestrator
and the post-restart re-mint).

Split out of :mod:`test_receive_channel_message` for the 500-line cap;
the servicer/event builders live in
:mod:`_receive_channel_message_helpers`.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import grpc

from ._receive_channel_message_helpers import (
    channel_event,
    enqueued_event,
    make_servicer,
)


class TestReceiveChannelMessagePreviousInteractionCloseCause:
    async def test_valid_pair_seeds_event_metadata(self):
        servicer, dispatcher = make_servicer()
        await servicer.ReceiveChannelMessage(
            channel_event(
                interaction_id="int-B",
                previous_interaction_id="int-A",
                previous_interaction_close_trigger="idle",
            ),
            MagicMock(spec=grpc.aio.ServicerContext),
        )
        event = enqueued_event(dispatcher)
        assert event.metadata.get("previous_interaction_id") == "int-A"
        assert event.metadata.get("previous_interaction_close_trigger") == "idle"

    async def test_end_votes_trigger_seeds(self):
        servicer, dispatcher = make_servicer()
        await servicer.ReceiveChannelMessage(
            channel_event(
                previous_interaction_id="int-A",
                previous_interaction_close_trigger="end_votes",
            ),
            MagicMock(spec=grpc.aio.ServicerContext),
        )
        event = enqueued_event(dispatcher)
        assert event.metadata.get("previous_interaction_close_trigger") == "end_votes"

    async def test_absent_pair_not_seeded(self):
        """Old orchestrator / fresh channel / post-restart re-mint: both
        fields empty — neither key appears, the pre-OQ5 wire shape."""
        servicer, dispatcher = make_servicer()
        await servicer.ReceiveChannelMessage(
            channel_event(interaction_id="int-B"),
            MagicMock(spec=grpc.aio.ServicerContext),
        )
        event = enqueued_event(dispatcher)
        assert "previous_interaction_id" not in event.metadata
        assert "previous_interaction_close_trigger" not in event.metadata

    async def test_unrecognised_trigger_drops_pair(self):
        """The trigger drives the persisted ``close_reason``, so an
        out-of-vocabulary value from a non-Go producer must not ride in —
        the whole pair degrades to absent."""
        servicer, dispatcher = make_servicer()
        await servicer.ReceiveChannelMessage(
            channel_event(
                previous_interaction_id="int-A",
                previous_interaction_close_trigger="cosmic-rays",
            ),
            MagicMock(spec=grpc.aio.ServicerContext),
        )
        event = enqueued_event(dispatcher)
        assert "previous_interaction_id" not in event.metadata
        assert "previous_interaction_close_trigger" not in event.metadata

    async def test_trigger_without_id_not_seeded(self):
        """A lone trigger attributes nothing (the seam matches on the id);
        seeding it could mislabel a mismatched generation."""
        servicer, dispatcher = make_servicer()
        await servicer.ReceiveChannelMessage(
            channel_event(previous_interaction_close_trigger="idle"),
            MagicMock(spec=grpc.aio.ServicerContext),
        )
        event = enqueued_event(dispatcher)
        assert "previous_interaction_close_trigger" not in event.metadata

    async def test_overlong_previous_id_drops_pair(self):
        """Same byte bound as ``interaction_id`` — absent, not truncated."""
        servicer, dispatcher = make_servicer()
        await servicer.ReceiveChannelMessage(
            channel_event(
                previous_interaction_id="x" * 129,
                previous_interaction_close_trigger="idle",
            ),
            MagicMock(spec=grpc.aio.ServicerContext),
        )
        event = enqueued_event(dispatcher)
        assert "previous_interaction_id" not in event.metadata
        assert "previous_interaction_close_trigger" not in event.metadata
