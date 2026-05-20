"""Unit tests for ISSUE-0066 — chat REST surface under wallet back-pressure.

The wallet's per-agent active-lease cap
([`internal/wallet/wallet.go:206-214`](../../internal/wallet/wallet.go)) and
the orchestrator's gRPC rate-limit interceptor
([`internal/security/middleware.go:172`](../../internal/security/middleware.go))
both deny with ``codes.ResourceExhausted``. The agent-side wallet client
retries with backoff and, on exhausting its retry budget, re-raises the
raw :class:`grpc.aio.AioRpcError` (see
[`agents/wallet_client.py::_acquire`](../../agents/wallet_client.py)) rather
than wrapping it in :class:`BudgetExceededError` — these are transient
infra signals, not budget violations.

Pre-fix the exception fell through ``_dispatch_channel_event``'s generic
``except Exception`` arm with a log line only: no reply was published on
the originating channel, the orchestrator's ``replyWaiter`` timed out,
and the REST chat caller saw HTTP 504 ``DEADLINE_EXCEEDED`` instead of
the MT-COST-003 contract HTTP 200 + ``reply_status="error"`` (same
operator-visible surface bug ISSUE-0065 fixed for ``BudgetExceededError``,
different error class).

This module pins the new gated ``grpc.aio.AioRpcError`` arm: only
``RESOURCE_EXHAUSTED`` is converted to a published error reply (other
gRPC codes still fall through to the generic arm so genuine agent bugs
are not masked as fake chat replies — same rationale as
``test_generic_exception_does_not_publish_error_reply`` in
[test_chat_path_budget_denial.py](test_chat_path_budget_denial.py)).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import grpc
import grpc.aio

from agents.base import BaseAgent, TaskInput, TaskOutput, TaskStatus
from agents.dispatch import EventDispatcher
from agents.persona_types import AgentEvent, EventType
from agents.server_servicers import AgentServiceServicer


class _StubAgent(BaseAgent):
    async def handle(self, task: TaskInput) -> TaskOutput:
        return TaskOutput(status=TaskStatus.COMPLETED, result="ok")


def _rpc_error(
    code: grpc.StatusCode, *, details: str = "",
) -> grpc.aio.AioRpcError:
    """Construct an ``AioRpcError`` carrying *code* for stub side-effects."""
    return grpc.aio.AioRpcError(
        code, grpc.aio.Metadata(), grpc.aio.Metadata(),
        details=details or f"simulated {code}",
    )


def _make_channel_event(
    *, channel_id: str = "dm:alice:chat-agent",
    sender_id: str = "alice",
    message_id: str = "msg-1",
) -> AgentEvent:
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


def _make_servicer_with_publisher(
    *, dispatch_side_effect: Exception | None = None,
) -> tuple[AgentServiceServicer, AsyncMock]:
    agent = _StubAgent(agent_id="chat-agent", config={"model": "test"})
    dispatcher = MagicMock(spec=EventDispatcher)
    dispatcher.dispatch = AsyncMock(side_effect=dispatch_side_effect)
    publisher = AsyncMock()
    publisher.publish = AsyncMock(return_value=None)
    executor = MagicMock()
    executor.channel_publisher = publisher
    dispatcher.executor = executor
    return AgentServiceServicer({"chat-agent": agent}, dispatcher), publisher


class TestDispatchChannelEventResourceExhausted:
    """ISSUE-0066 — ``_dispatch_channel_event`` must publish a structured-error
    reply on the originating channel when dispatch raises
    :class:`grpc.aio.AioRpcError` with ``code == RESOURCE_EXHAUSTED``.

    Same publish shape as the ISSUE-0065 ``BudgetExceededError`` arm so the
    Go-side discriminator (``metadata["reply_status"]="error"``, see
    ``internal/server/chat_handler.go``) needs no change.  Distinct
    ``error_reason`` lets operator dashboards split back-pressure from
    budget denials.
    """

    async def test_resource_exhausted_publishes_error_reply_on_channel(
        self,
    ) -> None:
        exhausted = _rpc_error(
            grpc.StatusCode.RESOURCE_EXHAUSTED,
            details="agent already holds the maximum 3 active leases",
        )
        servicer, publisher = _make_servicer_with_publisher(
            dispatch_side_effect=exhausted,
        )
        event = _make_channel_event()

        await servicer._dispatch_channel_event("chat-agent", event)

        assert publisher.publish.await_count == 1, (
            f"ISSUE-0066: AioRpcError(RESOURCE_EXHAUSTED) must trigger a "
            f"structured-error publish on event.channel_id; "
            f"publish.await_count={publisher.publish.await_count}"
        )

        call_kwargs = publisher.publish.await_args.kwargs
        # Wakes the orchestrator's reply waiter (keyed on
        # (channelID, awaitFromAgentID)).  sender_id must be the target
        # agent — anything else and the waiter does not resolve.
        assert call_kwargs["sender_id"] == "chat-agent", (
            f"publish.sender_id must equal target_agent_id so the "
            f"orchestrator's replyWaiter wakes; "
            f"got {call_kwargs['sender_id']!r}"
        )
        assert call_kwargs["channel_id"] == event.channel_id
        # Discriminator the REST chat handler reads to set
        # reply_status="error" in the JSON envelope.
        metadata = call_kwargs.get("metadata") or {}
        assert metadata.get("reply_status") == "error", (
            f"publish.metadata must carry reply_status='error'; "
            f"got metadata={metadata!r}"
        )
        # cascade_depth=0 — chat reply, not a fanout.
        assert call_kwargs.get("cascade_depth", -1) == 0

    async def test_resource_exhausted_uses_distinct_error_reason(self) -> None:
        """``error_reason`` must distinguish back-pressure from budget denial.

        Operator dashboards split on ``error_reason``; a single bucket
        with ``budget_exceeded`` would conflate user-visible end-of-conversation
        budget denials with transient infra back-pressure, defeating the
        whole point of cutting a separate issue from ISSUE-0065.  Pin the
        value so a future refactor cannot collapse the two.
        """
        exhausted = _rpc_error(grpc.StatusCode.RESOURCE_EXHAUSTED)
        servicer, publisher = _make_servicer_with_publisher(
            dispatch_side_effect=exhausted,
        )

        await servicer._dispatch_channel_event(
            "chat-agent", _make_channel_event(),
        )

        metadata = publisher.publish.await_args.kwargs.get("metadata") or {}
        assert metadata.get("error_reason") == "resource_exhausted", (
            f"error_reason must be 'resource_exhausted' to split from "
            f"budget_exceeded / wallet_unreachable on dashboards; "
            f"got {metadata.get('error_reason')!r}"
        )

    async def test_resource_exhausted_reply_is_user_facing_not_raw_details(
        self,
    ) -> None:
        """The reply body must be operator-friendly, not the raw gRPC details.

        ``exc.details()`` for the lease-cap path reads like
        ``"agent already holds the maximum 3 active leases"`` and for the
        rate-limiter path like ``"rate limit exceeded"`` — both leak
        internal-mechanics jargon to an end user who just needs to know
        "try again in a moment". Pin a stable, user-readable string so
        the surface contract does not regress to leaking raw gRPC text.
        """
        exhausted = _rpc_error(
            grpc.StatusCode.RESOURCE_EXHAUSTED,
            details="agent already holds the maximum 3 active leases",
        )
        servicer, publisher = _make_servicer_with_publisher(
            dispatch_side_effect=exhausted,
        )

        await servicer._dispatch_channel_event(
            "chat-agent", _make_channel_event(),
        )

        content = publisher.publish.await_args.kwargs["content"]
        # The reply must NOT be the raw gRPC details string.
        assert "maximum 3 active leases" not in content, (
            f"reply must not echo raw exc.details(); got {content!r}"
        )
        # And must convey "retry" semantics to the user.
        assert "capacity" in content.lower() or "retry" in content.lower(), (
            f"reply must convey transient back-pressure / retry intent; "
            f"got {content!r}"
        )

    async def test_other_grpc_codes_fall_through_to_generic_arm(self) -> None:
        """Only ``RESOURCE_EXHAUSTED`` is converted to a published reply.

        Other gRPC codes (``INTERNAL``, ``UNAVAILABLE``, ``DEADLINE_EXCEEDED``,
        …) are NOT modelled as known back-pressure / denial classes; they
        still fall through to the generic ``except Exception`` arm with a
        log line only.  Silently turning every gRPC error into a fake chat
        reply would mask the very bugs that arm is there to surface — same
        rationale as the ``test_generic_exception_does_not_publish_error_reply``
        guard in test_chat_path_budget_denial.py.
        """
        for code in (
            grpc.StatusCode.INTERNAL,
            grpc.StatusCode.UNAVAILABLE,
            grpc.StatusCode.DEADLINE_EXCEEDED,
            grpc.StatusCode.UNAUTHENTICATED,
        ):
            servicer, publisher = _make_servicer_with_publisher(
                dispatch_side_effect=_rpc_error(code),
            )

            await servicer._dispatch_channel_event(
                "chat-agent", _make_channel_event(),
            )

            assert publisher.publish.await_count == 0, (
                f"AioRpcError({code}) must NOT trigger a published reply — "
                f"only RESOURCE_EXHAUSTED is converted; the 504 surface "
                f"remains for everything else"
            )

    async def test_resource_exhausted_with_no_publisher_falls_back_to_log_only(
        self,
    ) -> None:
        """When no channel publisher is wired, log-only is the safe fallback.

        Mirrors the equivalent guard for the ``BudgetExceededError`` arm —
        test fixtures and session-less ``EventDispatcher`` instances do
        not always inject a publisher.  The wrapper must not crash.
        """
        agent = _StubAgent(agent_id="chat-agent", config={"model": "test"})
        dispatcher = MagicMock(spec=EventDispatcher)
        dispatcher.dispatch = AsyncMock(
            side_effect=_rpc_error(grpc.StatusCode.RESOURCE_EXHAUSTED),
        )
        executor = MagicMock()
        executor.channel_publisher = None
        dispatcher.executor = executor
        servicer = AgentServiceServicer({"chat-agent": agent}, dispatcher)

        # Must not raise.
        await servicer._dispatch_channel_event(
            "chat-agent", _make_channel_event(),
        )
