"""Unit tests for RFC 0023 PR 4 — chat handler's ``BudgetExceededError`` surface.

``AgentServiceServicer.SendChatMessage`` must catch
:class:`BudgetExceededError` raised by the dispatcher (via the persona
action loop's wallet-leased ``create_message`` call) and surface it as a
structured ``ChatResponse(reply_status="error", reply=<denied-message>)``
instead of letting it fall into the generic ``except Exception`` branch
that returns ``grpc.StatusCode.INTERNAL`` + ``reply_status="error"`` with
an empty ``reply``.

The shape is deliberately distinct from a transient internal error: the
gRPC status stays ``OK`` (the chat *call* succeeded; the LLM call inside
it was *denied*), the ``reply_status`` carries the structured ``"error"``,
and ``reply`` carries the wallet's ``LeaseDenied.message`` so an operator
reading the chat log can see *why* the call was refused.

ISSUE-0065 also pins the *channel-receive* arm of the chat path: the
production REST chat handler routes via
``ChannelRouter.PublishAndAwait`` → ``ReceiveChannelMessage``, NOT
``SendChatMessage``. Under RFC 0024 Phase 4 that handler enqueues
fire-and-forget and the event loop runs
``process_inbound_channel_event``, whose recovery arm must catch
:class:`BudgetExceededError` and publish a structured-error reply back on
the originating channel so the orchestrator's reply waiter resolves and
the REST chat handler returns HTTP 200 + ``reply_status="error"`` instead
of HTTP 504 ``DEADLINE_EXCEEDED``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import grpc
import grpc.aio
import pytest

from agents.base import BaseAgent, TaskInput, TaskOutput, TaskStatus
from agents.cascade_depth_defaults import DEFAULT_MAX_CASCADE_DEPTH
from agents.chat_reply import process_inbound_channel_event
from agents.dispatch import EventDispatcher
from agents.generated import task_pb2, task_pb2_grpc
from agents.persona_types import AgentEvent, EventType
from agents.server_servicers import AgentServiceServicer
from agents.tools.registry import clear_registry
from agents.wallet_client import BudgetExceededError


@pytest.fixture(autouse=True)
def _clean_registry():
    clear_registry()
    yield
    clear_registry()


class _StubAgent(BaseAgent):
    async def handle(self, task: TaskInput) -> TaskOutput:
        return TaskOutput(status=TaskStatus.COMPLETED, result="ok")


def _make_servicer_with_denial(exc: BudgetExceededError) -> AgentServiceServicer:
    """Build a servicer whose dispatcher raises *exc* on ``dispatch``."""
    agent = _StubAgent(agent_id="chat-agent", config={"model": "test"})
    dispatcher = MagicMock(spec=EventDispatcher)
    dispatcher.dispatch = AsyncMock(side_effect=exc)
    dispatcher.executor = MagicMock()
    dispatcher.executor.execute = AsyncMock(return_value=[])
    return AgentServiceServicer({"chat-agent": agent}, dispatcher)


class TestChatHandlerBudgetDenialSurface:
    async def test_budget_denied_returns_reply_status_error_with_message(self) -> None:
        denial = BudgetExceededError(
            "per_agent budget exceeded for chat-agent (spent=$10.0000 limit=$10.0000)",
            scope="per_agent",
            spent_usd=10.0,
            limit_usd=10.0,
            estimated_usd=0.01,
            reason="budget_exceeded",
        )
        servicer = _make_servicer_with_denial(denial)

        server = grpc.aio.server()
        task_pb2_grpc.add_AgentServiceServicer_to_server(servicer, server)
        port = server.add_insecure_port("127.0.0.1:0")
        await server.start()
        channel = grpc.aio.insecure_channel(f"127.0.0.1:{port}")
        try:
            stub = task_pb2_grpc.AgentServiceStub(channel)

            # The chat *call* must succeed at the gRPC layer (OK status);
            # the budget denial is conveyed in the reply_status field, not
            # via a gRPC error code — that's how the v0.3.2 chat client
            # tells "budget denied" apart from "agent crashed".
            resp = await stub.SendChatMessage(
                task_pb2.ChatRequest(
                    agent_id="chat-agent",
                    user_id="user-1",
                    message="please respond",
                ),
            )

            assert resp.reply_status == "error", (
                "PR 4: chat budget denial must surface as reply_status='error', "
                f"got {resp.reply_status!r}"
            )
            assert resp.reply == denial.message, (
                "PR 4: chat budget denial must carry the LeaseDenied.message "
                f"in reply, got {resp.reply!r}"
            )
            assert resp.agent_id == "chat-agent"
            # Server still echoes the session id so the client can correlate.
            assert resp.chat_session_id
        finally:
            await channel.close()
            await server.stop(grace=0)

    async def test_wallet_unreachable_returns_reply_status_error_with_message(self) -> None:
        """A wallet-unreachable failure is also surfaced as a structured error.

        RFC 0023 § F: an agent that cannot reach the wallet *fails closed*
        — :class:`BudgetExceededError` is raised with
        ``reason='wallet_unreachable'`` and an empty ``scope``. The chat
        handler must surface it the same way as an in-band budget denial:
        ``reply_status='error'`` with the error message in ``reply``.
        """
        unreachable = BudgetExceededError(
            "wallet unreachable — LLM call failing closed",
            reason="wallet_unreachable",
        )
        servicer = _make_servicer_with_denial(unreachable)

        server = grpc.aio.server()
        task_pb2_grpc.add_AgentServiceServicer_to_server(servicer, server)
        port = server.add_insecure_port("127.0.0.1:0")
        await server.start()
        channel = grpc.aio.insecure_channel(f"127.0.0.1:{port}")
        try:
            stub = task_pb2_grpc.AgentServiceStub(channel)

            resp = await stub.SendChatMessage(
                task_pb2.ChatRequest(
                    agent_id="chat-agent",
                    user_id="user-1",
                    message="please respond",
                ),
            )

            assert resp.reply_status == "error"
            assert resp.reply == unreachable.message
            assert "wallet unreachable" in resp.reply
        finally:
            await channel.close()
            await server.stop(grace=0)


# ─── ISSUE-0065: channel-receive arm of the chat path ────────────────────────


def _make_channel_event(
    *, channel_id: str = "dm:alice:chat-agent",
    sender_id: str = "alice",
    message_id: str = "msg-1",
) -> AgentEvent:
    """Build a minimal channel-message AgentEvent for dispatch tests."""
    return AgentEvent(
        event_type=EventType.CHANNEL_MESSAGE,
        payload={
            "content": "please respond",
            "channel_type": "dm",
            "mentions": ["chat-agent"],
            "respond_policy": "when_mentioned",
            "thread_parent_sender_id": "",
        },
        channel_id=channel_id,
        sender_id=sender_id,
        message_id=message_id,
        thread_id=None,
        timestamp=0.0,
        metadata={"cascade_depth": 0},
    )


def _make_agent_executor(
    *,
    on_event_side_effect: Exception | None = None,
    publisher: AsyncMock | None = None,
) -> tuple[MagicMock, MagicMock, AsyncMock]:
    """Build ``(agent, executor, publisher)`` for the processing coroutine.

    RFC 0024 Phase 4 moved channel-message processing onto the per-agent
    ``EventLoop``: the loop runs
    :func:`agents.chat_reply.process_inbound_channel_event`, which wraps
    the persona action loop in the ISSUE-0065/0066 recovery arm. A budget
    denial is therefore raised inside ``agent.on_event`` (the
    wallet-leased ``create_message`` call), not by the dispatcher; the
    executor's ``channel_publisher`` is where the structured-error reply
    lands. Passing ``publisher=None`` exercises the no-publisher fallback.
    """
    agent = MagicMock()
    agent.agent_id = "chat-agent"
    agent.on_event = AsyncMock(side_effect=on_event_side_effect, return_value=[])
    if publisher is None:
        publisher = AsyncMock()
        publisher.publish = AsyncMock(return_value=None)
    executor = MagicMock()
    executor.channel_publisher = publisher
    executor.execute = AsyncMock(return_value=[])
    return agent, executor, publisher


async def _process(agent: MagicMock, executor: MagicMock, event: AgentEvent) -> None:
    await process_inbound_channel_event(
        agent=agent,
        executor=executor,
        event=event,
        max_cascade_depth=DEFAULT_MAX_CASCADE_DEPTH,
    )


class TestProcessInboundBudgetDenial:
    """ISSUE-0065 — fire-and-forget inbound processing must publish a
    structured-error reply on the originating channel when the persona
    action loop raises :class:`BudgetExceededError`.

    The orchestrator's :class:`PublishAndAwait` reply waiter keys on
    ``(channelID, awaitFromAgentID)`` (see
    ``internal/channels/waiter.go``). A normal-shape publish from the
    target agent on the same DM channel wakes the waiter, which carries
    the reply to the REST chat handler. The handler reads
    ``metadata["reply_status"]`` and surfaces it in the JSON envelope —
    so the published reply must carry that discriminator.

    Without this arm, the channel-processing generic ``except Exception``
    branch only logs the denial; the waiter never sees a reply and times
    out → HTTP 504 instead of MT-COST-003's contracted HTTP 200 +
    ``reply_status="error"``.
    """

    async def test_budget_denied_publishes_error_reply_on_channel(self) -> None:
        denial = BudgetExceededError(
            "per_agent budget exceeded: spent=$0.0172 limit=$0.1000 estimated=$0.0846",
            scope="per_agent",
            spent_usd=0.0172,
            limit_usd=0.1000,
            estimated_usd=0.0846,
            reason="budget_exceeded",
        )
        agent, executor, publisher = _make_agent_executor(
            on_event_side_effect=denial,
        )
        event = _make_channel_event()

        await _process(agent, executor, event)

        # The wrapper must have published *exactly one* reply on the
        # originating channel — the wallet denial envelope.
        assert publisher.publish.await_count == 1, (
            f"ISSUE-0065: BudgetExceededError must trigger a structured-error "
            f"publish on event.channel_id; publish.await_count="
            f"{publisher.publish.await_count}"
        )

        call_kwargs = publisher.publish.await_args.kwargs
        # Wakes the orchestrator's reply waiter (keyed on
        # (channelID, awaitFromAgentID)). sender_id must be the target
        # agent — anything else and the waiter does not resolve.
        assert call_kwargs["sender_id"] == "chat-agent", (
            "publish.sender_id must equal target_agent_id so the "
            f"orchestrator's replyWaiter wakes; got {call_kwargs['sender_id']!r}"
        )
        assert call_kwargs["channel_id"] == event.channel_id, (
            "publish must land on event.channel_id (the DM the chat REST "
            "handler is awaiting on)"
        )
        # The wallet's LeaseDenied.message is what an operator sees in
        # the chat log — must round-trip verbatim.
        assert call_kwargs["content"] == denial.message
        # Discriminator the REST chat handler reads to set
        # reply_status="error" in the JSON envelope.
        metadata = call_kwargs.get("metadata") or {}
        assert metadata.get("reply_status") == "error", (
            f"publish.metadata must carry reply_status='error' so the REST "
            f"chat handler renders the error envelope; got metadata={metadata!r}"
        )
        # cascade_depth=0 — this is a chat-reply, not a fanout. Anything
        # else and the orchestrator's depth clamp would drop the reply.
        assert call_kwargs.get("cascade_depth", -1) == 0

    async def test_budget_denied_error_reply_echoes_interaction_claim(self) -> None:
        """The error reply carries the RFC 0052 no-reopen claim of the
        dispatch it recovers (PR #716 review).

        The recovery publish is same-channel post-persistence traffic like
        any reply, and it fires under exactly the spend pressure that trips
        the bounded close's cost trigger — so an unstamped error reply
        landing after the close bypassed the resolver's no-reopen latch
        (``latchClaim`` requires a non-empty inbound claim), minted a FRESH
        interaction, and re-fanned the roster into the terminated
        discussion. The claim must ride the same ``interaction_id``
        metadata key the reply and end-vote publishes echo.
        """
        denial = BudgetExceededError(
            "per_agent budget exceeded",
            scope="per_agent",
            reason="budget_exceeded",
        )
        agent, executor, publisher = _make_agent_executor(
            on_event_side_effect=denial,
        )
        event = _make_channel_event()
        event.metadata["interaction_id"] = "itx-9"  # seed_wire_metadata's key

        await _process(agent, executor, event)

        metadata = publisher.publish.await_args.kwargs.get("metadata") or {}
        assert metadata.get("interaction_id") == "itx-9", (
            "the error reply must echo the dispatched-under interaction id "
            "as its no-reopen claim, or a post-close budget-denial straggler "
            f"reopens the closed discussion; got metadata={metadata!r}"
        )
        # The error discriminator still rides beside the claim.
        assert metadata.get("reply_status") == "error"

    async def test_wallet_unreachable_also_publishes_error_reply(self) -> None:
        """``reason='wallet_unreachable'`` takes the same publish arm.

        RFC 0023 §F treats wallet-unreachable as fail-closed: same
        operator-visible surface as a budget denial.
        """
        unreachable = BudgetExceededError(
            "wallet unreachable — LLM call failing closed",
            reason="wallet_unreachable",
        )
        agent, executor, publisher = _make_agent_executor(
            on_event_side_effect=unreachable,
        )
        event = _make_channel_event()

        await _process(agent, executor, event)

        assert publisher.publish.await_count == 1
        call_kwargs = publisher.publish.await_args.kwargs
        assert call_kwargs["content"] == unreachable.message
        metadata = call_kwargs.get("metadata") or {}
        assert metadata.get("reply_status") == "error"

    async def test_generic_exception_does_not_publish_error_reply(self) -> None:
        """Only :class:`BudgetExceededError` triggers the published-error path.

        A bare :class:`RuntimeError` from the action loop is still logged
        via the generic ``except Exception`` arm but does NOT publish a
        reply — silently turning every processing crash into a fake chat
        response would mask agent bugs. The 504 surface is the correct
        one for unexpected errors today; only the explicitly-modelled
        wallet denial gets the structured envelope.
        """
        agent, executor, publisher = _make_agent_executor(
            on_event_side_effect=RuntimeError("boom"),
        )
        event = _make_channel_event()

        await _process(agent, executor, event)

        assert publisher.publish.await_count == 0, (
            "Generic exceptions must NOT trigger a published reply — only "
            "BudgetExceededError does; the 504 surface remains for crashes."
        )

    async def test_happy_path_does_not_publish_error_reply(self) -> None:
        """Successful processing must not trigger the error-reply path."""
        agent, executor, publisher = _make_agent_executor(
            on_event_side_effect=None,
        )
        event = _make_channel_event()

        await _process(agent, executor, event)

        assert publisher.publish.await_count == 0

    async def test_synthesis_turn_budget_denial_suppresses_error_reply(self) -> None:
        """PR #718 review finding 1: a §D synthesis turn whose dispatch is
        budget-denied must NOT publish the apology reply.

        The synthesis turn's reply is the closing artifact the fanout head
        claims by (sender == chair, armed interaction id). An error-recovery
        apology published under the chair's id with that same claim would be
        indistinguishable from the real synthesis and get fanned to every
        member as the goal-directed close — and this fires precisely under
        the spend pressure that triggered the cost close. The orchestrator's
        timeout net owns the no-reply branch instead, so the recovery arm
        must stay silent for a synthesis turn.
        """
        denial = BudgetExceededError(
            "interaction budget exceeded during synthesis turn",
            scope="per_interaction", reason="budget_exceeded",
        )
        agent, executor, publisher = _make_agent_executor(
            on_event_side_effect=denial,
        )
        event = _make_channel_event()
        event.payload["synthesis_turn"] = True  # the orchestrator's §D marker

        await _process(agent, executor, event)

        assert publisher.publish.await_count == 0, (
            "a synthesis turn's budget denial must not publish an apology the "
            "fanout head would mistake for the synthesis — the timeout net closes"
        )

    async def test_resource_exhausted_on_synthesis_turn_also_suppressed(self) -> None:
        """The ISSUE-0066 arm of finding 1: a RESOURCE_EXHAUSTED lease
        denial on a synthesis turn is likewise not published."""
        exc = grpc.aio.AioRpcError(
            grpc.StatusCode.RESOURCE_EXHAUSTED,
            grpc.aio.Metadata(), grpc.aio.Metadata(), details="max leases",
        )
        agent, executor, publisher = _make_agent_executor(on_event_side_effect=exc)
        event = _make_channel_event()
        event.payload["synthesis_turn"] = True

        await _process(agent, executor, event)

        assert publisher.publish.await_count == 0

    async def test_no_channel_publisher_falls_back_to_log_only(self) -> None:
        """When no channel publisher is wired, log-only is the safe fallback.

        Test fixtures and session-less ``EventDispatcher`` instances do
        not always inject a publisher. The wrapper must not crash — the
        REST chat surface times out at 504 (pre-fix behaviour) rather
        than the new structured-error envelope, but no exception
        escapes into the asyncio event loop.
        """
        denial = BudgetExceededError(
            "per_agent budget exceeded", scope="per_agent",
        )
        agent, executor, _ = _make_agent_executor(
            on_event_side_effect=denial, publisher=None,
        )
        executor.channel_publisher = None
        event = _make_channel_event()

        # Must not raise.
        await _process(agent, executor, event)
