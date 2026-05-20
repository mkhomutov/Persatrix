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
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import grpc
import grpc.aio
import pytest

from agents.base import BaseAgent, TaskInput, TaskOutput, TaskStatus
from agents.dispatch import EventDispatcher
from agents.generated import task_pb2, task_pb2_grpc
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
