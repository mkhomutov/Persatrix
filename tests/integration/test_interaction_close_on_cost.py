"""v0.3.8 summary-surface PR 1 (SS2) — integration: the RFC 0030 Layer 1
per-interaction cost ceiling closes *and summarises* the interaction.

The governance-layers work shipped the wallet-side enforcement: once an
interaction's running spend crosses ``interaction_budget_tokens`` the
wallet denies further leases with
``LEASE_DENIED_REASON_INTERACTION_BUDGET_EXHAUSTED`` →
``BudgetExceededError(reason="interaction_budget_exhausted")``. Before
this PR that denial merely *stopped fanout*: the in-memory interaction
stayed open and no episode row was ever written, so a cost-bounded
brainstorm ended with nothing to read.

PR 1 routes that denial through the RFC 0020 summarising close path
(``_close_interaction_on_cost`` → ``InteractionTracker.close`` with
``REASON_COST`` → ``_persist_closed_interaction``). This test pins the
behaviour at the loopback boundary — only the Go ``WalletService`` is
mocked; the persona runtime, action loop, response gate, wallet client,
and close-path summariser all run the production code.

Two behaviours:

1. **interaction-budget denial → close + summarise.** After two admitted
   channel turns open a multi-turn interaction, a third turn is denied
   for the interaction budget; the interaction is closed (``cost``) and
   summarised — a single ``closed`` / ``summarized`` episode row carries
   the LLM summary.
2. **per-agent denial → interaction left open (negative control).** A
   generic per-agent ``budget_exceeded`` denial is the agent's own RFC
   0023 wallet, not the shared cost ceiling; it must *not* close the
   interaction, so no episode row is written.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import grpc
import grpc.aio
import pytest

from agents.generated import wallet_pb2 as walletpb
from agents.generated import wallet_pb2_grpc as walletgrpc
from agents.llm_client import LLMClient, LLMResponse, StopReason, Usage
from agents.persona import create_persona_agent
from agents.persona_runtime import _LLMPersonaAgent
from agents.persona_runtime.summarize_close import SUMMARIZATION_MAX_OUTPUT_TOKENS
from agents.persona_types import AgentEvent, EventType
from agents.tools.registry import clear_registry
from agents.wallet_client import BudgetExceededError, WalletClient

LLM_SUMMARY_TEXT = (
    "The channel debated the launch date and converged on shipping "
    "Thursday once the cost ceiling was reached."
)


@pytest.fixture(autouse=True)
def _clean_registry() -> Iterator[None]:
    clear_registry()
    yield
    clear_registry()


# ─── In-process wallet: admit N calls, then deny with a chosen reason ──────────


class _CeilingWalletServicer(walletgrpc.WalletServiceServicer):
    """Grant ``admit`` leases, then deny every later lease.

    ``deny_reason`` selects the wire ``LeaseDeniedReason`` so the test can
    exercise both the interaction-budget ceiling and a generic per-agent
    denial through the same fixture. A real wallet reverses charges on
    release; this one does not, so every lease after the admit budget is
    denied.
    """

    def __init__(
        self,
        *,
        admit: int,
        deny_reason: walletpb.LeaseDeniedReason.ValueType,
    ) -> None:
        self._admit = admit
        self._deny_reason = deny_reason
        self._granted = 0
        self._next_id = 0
        self.acquired = 0
        self.denied = 0

    async def AcquireLease(  # noqa: N802
        self, request: walletpb.LeaseRequest, context: object,
    ) -> walletpb.LeaseResponse:
        self.acquired += 1
        if self._granted >= self._admit:
            self.denied += 1
            scope = (
                "interaction"
                if self._deny_reason
                == walletpb.LeaseDeniedReason.LEASE_DENIED_REASON_INTERACTION_BUDGET_EXHAUSTED
                else "per_agent"
            )
            return walletpb.LeaseResponse(
                denied=walletpb.LeaseDenied(
                    scope=scope,
                    spent_usd=1.0,
                    limit_usd=1.0,
                    estimated_usd=0.4,
                    message=f"{scope} budget exceeded",
                    reason=self._deny_reason,
                ),
            )
        self._granted += 1
        self._next_id += 1
        return walletpb.LeaseResponse(
            grant=walletpb.LeaseGrant(
                lease_id=f"lease-{self._next_id}",
                granted_input_tokens=request.estimated_input_tokens,
                granted_output_tokens=request.estimated_max_output_tokens,
                ttl_seconds=60,
            ),
        )

    async def SettleLease(  # noqa: N802
        self, request: walletpb.SettlementRequest, context: object,
    ) -> walletpb.SettlementAck:
        return walletpb.SettlementAck(success=True)

    async def ReleaseLease(  # noqa: N802
        self, request: walletpb.ReleaseRequest, context: object,
    ) -> walletpb.SettlementAck:
        return walletpb.SettlementAck(success=True)


async def _wallet_admitting(
    *, admit: int, deny_reason: walletpb.LeaseDeniedReason.ValueType,
) -> AsyncGenerator[WalletClient, None]:
    servicer = _CeilingWalletServicer(admit=admit, deny_reason=deny_reason)
    server = grpc.aio.server()
    walletgrpc.add_WalletServiceServicer_to_server(servicer, server)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    channel = grpc.aio.insecure_channel(f"127.0.0.1:{port}")
    try:
        yield WalletClient.from_channel(channel, backoff_base=0.0)
    finally:
        await channel.close()
        await server.stop(grace=0.5)


# ─── Persona fixture ──────────────────────────────────────────────────────────


_PERSONA_CONFIG: dict[str, Any] = {
    "id": "cost-close-persona",
    "type": "persona",
    "name": "Cost Close Persona",
    "role": "Integration-test persona for the RFC 0030 Layer 1 cost-close",
    "model": "claude-sonnet-4-6",
    "temperature": 0.3,
    "max_llm_calls": 3,
    "max_tokens": 128,
    "persona": {
        "background": "Test fixture.",
        "behavior": {"directness": "balanced"},
    },
    "permissions": {"memory": {"read": True, "write": True}},
    "memory": {"db_path": ":memory:"},
}


def _route_llm(text: str):
    """LLM provider router: summariser call → prose, persona loop → action.

    The summariser pins ``max_tokens=SUMMARIZATION_MAX_OUTPUT_TOKENS`` and
    passes no wallet cause, so it bypasses the (exhausted) wallet and
    reaches the provider — the cost-closed interaction still summarises.
    """
    async def _impl(*, model, model_alias=None, messages, system, tools,
                    max_tokens, temperature, **_kw):
        if max_tokens == SUMMARIZATION_MAX_OUTPUT_TOKENS:
            return LLMResponse(
                text=text, stop_reason=StopReason.END_TURN,
                usage=Usage(120, 30),
            )
        return LLMResponse(
            text=(
                '```json\n[{"action_type": "complete_task", '
                '"payload": {"result": "ack"}}]\n```'
            ),
            stop_reason=StopReason.END_TURN,
            usage=Usage(input_tokens=20, output_tokens=12),
        )
    return _impl


def _make_agent(wallet: WalletClient) -> _LLMPersonaAgent:
    provider = AsyncMock()
    provider.name = "anthropic"
    provider.create_message = AsyncMock(side_effect=_route_llm(LLM_SUMMARY_TEXT))
    provider.format_tool_definitions = MagicMock(return_value=[])
    provider.append_tool_round = MagicMock(side_effect=lambda m, r, x: m)
    llm_client = LLMClient(provider, wallet=wallet)
    return create_persona_agent(
        agent_id=_PERSONA_CONFIG["id"],
        config=_PERSONA_CONFIG,
        llm_client=llm_client,
    )


def _channel_event(*, channel_id: str = "group:room-7", i: int = 0) -> AgentEvent:
    return AgentEvent(
        event_type=EventType.CHANNEL_MESSAGE,
        payload={
            "content": f"turn {i}",
            "channel_type": "group",
            "mentions": [],
            "respond_policy": "always",
        },
        channel_id=channel_id,
        sender_id=f"peer-{i}",
        message_id=f"msg-{i}",
    )


async def _episode_rows(agent: _LLMPersonaAgent) -> list[Any]:
    db = agent._episodic_memory._ensure_db()
    async with db.execute(
        "SELECT summary, closed_at, turn_count, scope FROM episodes "
        "WHERE agent_id = ? ORDER BY created_at",
        (agent.agent_id,),
    ) as cursor:
        return list(await cursor.fetchall())


# ─── Tests ────────────────────────────────────────────────────────────────────


async def test_interaction_budget_denial_closes_and_summarises() -> None:
    """SS2: the cost ceiling closes the interaction and writes a summary."""
    agen = _wallet_admitting(
        admit=2,
        deny_reason=walletpb.LeaseDeniedReason.LEASE_DENIED_REASON_INTERACTION_BUDGET_EXHAUSTED,
    )
    wallet = await agen.__anext__()
    agent = _make_agent(wallet)
    await agent.initialize_memory()
    try:
        # Two admitted turns open a multi-turn interaction in the channel
        # scope (multi-turn rows persist only at close, so episodes is
        # still empty here).
        await agent.on_event(_channel_event(i=0))
        await agent.on_event(_channel_event(i=1))
        assert await _episode_rows(agent) == []

        # Third turn: the interaction budget is exhausted. The denial
        # re-raises to the caller (chat/channel contract) *after* the
        # cost-close has run.
        with pytest.raises(BudgetExceededError) as excinfo:
            await agent.on_event(_channel_event(i=2))
        assert excinfo.value.reason == "interaction_budget_exhausted"

        # Drain the background summariser, then assert a single closed +
        # summarised episode row carrying the LLM summary.
        await agent.drain_pending_summaries()
        rows = await _episode_rows(agent)
        # v0.3.15 residuals PR 3 (ISSUE-0123/0131): the tracker keys
        # ``(principal, speaker, scope)``, so the two speakers' turns form
        # TWO records, and the cost close is a ROOM event that fans over
        # both — one closed, summarised episode per speaker.
        assert len(rows) == 2, (
            f"expected one closed episode per speaker, got {len(rows)}"
        )
        for summary, closed_at, turn_count, scope in rows:
            assert summary == LLM_SUMMARY_TEXT
            assert closed_at is not None
            assert turn_count == 1
            assert scope == "group:room-7"
    finally:
        await agent.close_memory()
        await agen.aclose()


async def test_per_agent_denial_leaves_interaction_open() -> None:
    """Negative control: a generic per-agent denial does not close."""
    agen = _wallet_admitting(
        admit=2,
        deny_reason=walletpb.LeaseDeniedReason.LEASE_DENIED_REASON_UNSPECIFIED,
    )
    wallet = await agen.__anext__()
    agent = _make_agent(wallet)
    await agent.initialize_memory()
    try:
        await agent.on_event(_channel_event(i=0))
        await agent.on_event(_channel_event(i=1))

        with pytest.raises(BudgetExceededError) as excinfo:
            await agent.on_event(_channel_event(i=2))
        assert excinfo.value.reason == "budget_exceeded"

        # The interaction stays open — a per-agent wallet denial is not a
        # cost-ceiling close. Assert the tracker still holds the OPEN
        # interaction (both admitted turns), pinning the
        # ``reason == "interaction_budget_exhausted"`` gate in
        # ``handle_llm_call_exception_with_cost_close`` directly. The
        # empty-episodes check alone is too weak: an empty table is also
        # consistent with a wrongly-closed interaction whose no-turn /
        # failed persist simply wrote nothing.
        open_records = agent._interaction_tracker.records_for_scope(
            "group:room-7",
        )
        # One record per speaker since the v0.3.15 re-key; both stay open.
        assert len(open_records) == 2, (
            "per-agent denial must not close the interaction"
        )
        assert all(r.is_open for r in open_records)
        assert [r.turn_count for r in open_records] == [1, 1]

        # And no episode row is written (the close path never ran).
        await agent.drain_pending_summaries()
        assert await _episode_rows(agent) == []
    finally:
        await agent.close_memory()
        await agen.aclose()


async def test_cost_fan_skips_record_stamped_with_other_wire() -> None:
    """PR #846 review: the exhausted budget belongs to the EVENT's wire
    interaction — a sibling record positively stamped with a DIFFERENT id
    (a successor conversation with a fresh budget) survives the cost fan
    instead of being buried under ``REASON_COST``."""
    agen = _wallet_admitting(
        admit=2,
        deny_reason=walletpb.LeaseDeniedReason.LEASE_DENIED_REASON_INTERACTION_BUDGET_EXHAUSTED,
    )
    wallet = await agen.__anext__()
    agent = _make_agent(wallet)
    await agent.initialize_memory()
    try:
        for i in range(2):
            ev = _channel_event(i=i)
            ev.metadata["interaction_id"] = "wire-A"
            await agent.on_event(ev)
        successor = agent._interaction_tracker.add_turn(
            "group:room-7", speaker_id="peer-9",
        )
        successor.wire_interaction_id = "wire-B"

        denied = _channel_event(i=2)
        denied.metadata["interaction_id"] = "wire-A"
        with pytest.raises(BudgetExceededError):
            await agent.on_event(denied)

        open_records = agent._interaction_tracker.records_for_scope(
            "group:room-7",
        )
        assert [r.wire_interaction_id for r in open_records] == ["wire-B"], (
            "the successor conversation's record must survive the cost fan"
        )
    finally:
        await agent.close_memory()
        await agen.aclose()
