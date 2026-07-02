"""The RFC 0052 no-reopen interaction claim — the agent-side echo (PR #716).

The Go resolver's post-close latch keys on the publish body's
``metadata.interaction_id`` claim. Before the echo, NO production agent
publish carried one — the latch tests hand-stamped it — so a straggler reply
landing after a bounded close minted a fresh interaction and re-fanned the
whole roster: the §D runaway the latch exists to stop, reachable on every
close. This file pins the whole echo contract:

* executor seam — a SAME-channel reply/vote claims the id it was dispatched
  under; cross-channel and origin-less publishes stamp nothing (IP8 keeps
  chain-origin publishes unstamped so a closed channel stays re-convenable);
* the fire-and-forget inbound path (``process_inbound_channel_event``, the
  DOMINANT channel-reply route) — threads the event's seeded id and channel
  into ``executor.execute``; ``EventDispatcher.dispatch``'s twin threading is
  pinned in ``test_dispatch_execute_actions.py``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from agents.chat_reply import process_inbound_channel_event
from agents.dispatch import ActionExecutor
from agents.persona_types import ActionType, AgentAction, AgentEvent, EventType


def _publisher() -> AsyncMock:
    publisher = AsyncMock()
    publisher.publish = AsyncMock(return_value=None)
    return publisher


def _stub_agent() -> MagicMock:
    agent = MagicMock()
    agent.agent_id = "ember-owl"
    agent.on_event = AsyncMock(return_value=[])
    return agent


def _stub_executor() -> MagicMock:
    executor = MagicMock()
    executor.execute = AsyncMock(return_value=[])
    executor.channel_publisher = None  # recovery wrapper's best-effort seam
    return executor


def _inbound_event(metadata: dict) -> AgentEvent:
    return AgentEvent(
        event_type=EventType.CHANNEL_MESSAGE,
        payload={"content": "hello"},
        channel_id="group:planning",
        sender_id="iron-fox",
        metadata=metadata,
    )


class TestExecutorInteractionClaim:

    async def test_claim_echoed_on_same_channel_publish(self):
        """A same-channel reply claims the interaction it was dispatched under."""
        publisher = _publisher()
        executor = ActionExecutor(channel_publisher=publisher)

        await executor.execute("ember-owl", [
            AgentAction(ActionType.SEND_CHANNEL_MESSAGE, {
                "channel_id": "group:planning", "content": "my take",
            }),
        ], cascade_depth=2,
            origin_channel_id="group:planning",
            origin_interaction_id="itx-1234",
        )

        kwargs = publisher.publish.await_args.kwargs
        assert kwargs["metadata"] == {"interaction_id": "itx-1234"}, (
            f"got metadata={kwargs.get('metadata')!r}"
        )

    async def test_claim_not_echoed_cross_channel(self):
        """The claim is channel-scoped: the target channel has its own open
        interaction, and a foreign id is at best resolver-override noise (IP2).
        ``metadata`` stays ``None`` so the POST body keeps the clean shape."""
        publisher = _publisher()
        executor = ActionExecutor(channel_publisher=publisher)

        await executor.execute("ember-owl", [
            AgentAction(ActionType.SEND_CHANNEL_MESSAGE, {
                "channel_id": "group:other-room", "content": "cross-post",
            }),
        ], cascade_depth=2,
            origin_channel_id="group:planning",
            origin_interaction_id="itx-1234",
        )

        assert publisher.publish.await_args.kwargs["metadata"] is None

    async def test_claim_absent_without_origin(self):
        """Callers with no inbound channel event (tick scheduler, chat surface)
        omit the origin pair — chain-origin publishes stay unstamped (IP8)."""
        publisher = _publisher()
        executor = ActionExecutor(channel_publisher=publisher)

        await executor.execute("ember-owl", [
            AgentAction(ActionType.SEND_CHANNEL_MESSAGE, {
                "channel_id": "group:planning", "content": "fresh convene",
            }),
        ], cascade_depth=0)

        assert publisher.publish.await_args.kwargs["metadata"] is None

    async def test_end_vote_echoes_claim_beside_vote_flag(self):
        """A vote is post-persistence channel traffic like any reply, so a
        vote straggling in after a bounded close must latch rather than mint
        fresh and re-fan. A live vote is unaffected: the resolver overrides
        the claim and scopes the quorum to its own resolved id (IP2)."""
        publisher = _publisher()
        executor = ActionExecutor(channel_publisher=publisher)

        await executor.execute("ember-owl", [
            AgentAction(ActionType.END_INTERACTION_VOTE, {
                "channel_id": "group:planning", "content": "nothing further",
            }),
        ], cascade_depth=2,
            origin_channel_id="group:planning",
            origin_interaction_id="itx-1234",
        )

        assert publisher.publish.await_args.kwargs["metadata"] == {
            "end_interaction_vote": True,
            "interaction_id": "itx-1234",
        }


class TestProcessInboundInteractionOrigin:

    async def test_seeded_interaction_id_threads_to_executor(self):
        executor = _stub_executor()
        event = _inbound_event({"cascade_depth": 1, "interaction_id": "itx-1234"})

        await process_inbound_channel_event(
            agent=_stub_agent(), executor=executor,
            event=event, max_cascade_depth=5,
        )

        kwargs = executor.execute.await_args.kwargs
        assert kwargs["origin_channel_id"] == "group:planning"
        assert kwargs["origin_interaction_id"] == "itx-1234"

    async def test_unseeded_event_threads_empty_origin(self):
        """No seeded id → empty pair, so the executor stamps nothing (IP8)."""
        executor = _stub_executor()

        await process_inbound_channel_event(
            agent=_stub_agent(), executor=executor,
            event=_inbound_event({"cascade_depth": 1}), max_cascade_depth=5,
        )

        assert executor.execute.await_args.kwargs["origin_interaction_id"] == ""

    async def test_non_string_seeded_id_reads_as_absent(self):
        """A non-string metadata value (replay anomaly) degrades to untracked
        rather than stamping a junk claim — the ``seed_wire_metadata``
        validation posture applied at this seam too."""
        executor = _stub_executor()
        event = _inbound_event({"cascade_depth": 1, "interaction_id": 42})

        await process_inbound_channel_event(
            agent=_stub_agent(), executor=executor,
            event=event, max_cascade_depth=5,
        )

        assert executor.execute.await_args.kwargs["origin_interaction_id"] == ""
