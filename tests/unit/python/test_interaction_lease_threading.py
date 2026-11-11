"""Layer 1 lease attribution — `interaction_id` threading (producer plan PR 2).

The wallet has accepted ``interaction_id`` on ``AcquireLease`` since the
governance-layers PR 2, but no channel-path call site passed it — the cost
ceiling could enforce a per-interaction total it could never attribute.
This matrix (TDD-first, written red) pins the links of the chain:

* :meth:`agents.llm_client.LLMClient.create_message` forwards a caller
  ``interaction_id`` into ``WalletClient.lease`` (and omits nothing when
  the caller has none — the default stays the untracked empty string).
* :func:`agents.salience_bid.evaluate_salience` threads it through to its
  leased ``fast``-model call, so the Tier B bid carries the same
  interaction attribution as the quality turn it gates (TB3's attribution
  contract, extended to the interaction dimension).
* :func:`agents.persona_runtime.wallet_cause.lease_interaction_id_for_event`
  is the loop-side read: the resolver-stamped id off the inbound event
  metadata, empty for untracked/legacy events and malformed values.
* The persona's action loop passes that id into the lease of its main model
  call. This link was added later (ISSUE-0221): until then its only guard was
  a search of ``action_loop.py`` for the argument's text, which the later
  revise call in the same file also satisfies.
* A revise round bills the same interaction: the loop hands the id to the
  reflexion module, whose critic and rewrite calls each take a lease under
  it. The reflexion module's own tests pass an id in and never read it back,
  so this link is pinned here too, through the loop.
"""

from __future__ import annotations

import copy
import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agents.generated import wallet_pb2 as walletpb
from agents.llm_client import LLMClient, LLMResponse
from agents.model_aliases import use_alias_map
from agents.persona import create_persona_agent
from agents.persona_runtime.deliberation_plan import CompositionPlan
from agents.persona_runtime.salience_gate import SalienceOutcome
from agents.persona_runtime.wallet_cause import lease_interaction_id_for_event
from agents.persona_types import ActionType, AgentEvent, EventType

from ._persona_test_helpers import _PERSONA_CONFIG


class _RecordingWallet:
    """A wallet double exposing the async-contextmanager ``lease`` shape;
    records every acquisition's kwargs."""

    def __init__(self) -> None:
        self.lease_kwargs: list[dict] = []

    @asynccontextmanager
    async def lease(self, **kwargs):
        self.lease_kwargs.append(kwargs)
        lease = AsyncMock()
        lease.settle = AsyncMock(return_value=None)
        yield lease


def _provider(text: str = "ok"):
    provider = AsyncMock()
    provider.create_message = AsyncMock(return_value=LLMResponse(text=text))
    return provider


class TestLLMClientForwardsInteractionID:
    @pytest.mark.asyncio
    async def test_interaction_id_reaches_the_lease(self):
        wallet = _RecordingWallet()
        client = LLMClient(provider=_provider(), wallet=wallet)

        await client.create_message(
            model="claude-test",
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=16,
            cause=walletpb.CAUSE_CHANNEL_MESSAGE,
            agent_id="ember-owl",
            interaction_id="itx-123",
        )

        assert len(wallet.lease_kwargs) == 1
        assert wallet.lease_kwargs[0]["interaction_id"] == "itx-123"

    @pytest.mark.asyncio
    async def test_default_stays_untracked(self):
        """Omitting the kwarg keeps the pre-producer wire shape — the wallet
        sees the empty string (untracked) and every ceiling stays at its
        uncapped default."""
        wallet = _RecordingWallet()
        client = LLMClient(provider=_provider(), wallet=wallet)

        await client.create_message(
            model="claude-test",
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=16,
            cause=walletpb.CAUSE_CHANNEL_MESSAGE,
            agent_id="ember-owl",
        )

        assert wallet.lease_kwargs[0]["interaction_id"] == ""


class TestSalienceBidForwardsInteractionID:
    @pytest.mark.asyncio
    async def test_bid_lease_bills_the_same_interaction(self):
        """TB3 extended: the bid's leased call carries the inbound event's
        interaction. Substrate, not yet enforcement — the wallet acts on
        the id only when a positive ``interaction_budget_tokens`` rides the
        same request, which the config-stamping follow-up adds; this pin
        guarantees the id half is already in place so that follow-up can
        deny exhausted-interaction bids fail-closed."""
        from agents.model_aliases import use_alias_map
        from agents.salience_bid import evaluate_salience

        captured: dict = {}

        async def _capture(**kwargs):
            captured.update(kwargs)
            return LLMResponse(text="speak: no\nscore: 0.1")

        llm_client = AsyncMock()
        llm_client.create_message = AsyncMock(side_effect=_capture)

        # The same `fast`-alias override the bid suite uses, so the bid's
        # internal resolve("fast") does not hit the shipped fail-loud default.
        fast_alias = {"fast": {
            "provider": "mock", "model": "mock-fast",
            "input_per_1m_tokens": 0.0, "output_per_1m_tokens": 0.0,
        }}
        with use_alias_map(fast_alias):
            await evaluate_salience(
                llm_client=llm_client,
                content="open floor question",
                transcript=[],
                agent_id="ember-owl",
                persona_name="Ember Owl",
                persona_role="advisor",
                threshold=0.5,
                interaction_id="itx-123",
            )

        assert captured["interaction_id"] == "itx-123"


class TestActionLoopLeasesItsTurnUnderTheInteraction:
    @pytest.mark.asyncio
    async def test_main_turn_lease_carries_the_events_interaction(self):
        """One direct-message turn through the real action loop: the lease
        its model call takes names the interaction the message arrived
        under, so the wallet counts the turn against that interaction's
        cost ceiling.

        The turn makes exactly one model call. A direct message takes no
        salience bid, and the persona's config asks for no revise round, so
        the one lease the wallet double records is the main call's. Drop
        ``interaction_id`` from that call in ``action_loop.py`` and the lease
        carries the empty string, the untracked case, and this test fails.
        """
        wallet = _RecordingWallet()
        provider = AsyncMock()
        provider.create_message = AsyncMock(
            return_value=LLMResponse(text="Hi Alice, what's on your mind?"),
        )
        provider.format_tool_definitions = MagicMock(return_value=[])
        client = LLMClient(provider=provider, wallet=wallet)
        agent = create_persona_agent(
            agent_id="ember-owl", config=copy.deepcopy(_PERSONA_CONFIG), llm_client=client,
        )
        await agent.initialize_memory()
        event = AgentEvent(
            event_type=EventType.CHANNEL_MESSAGE,
            payload={
                "content": "say hi",
                "channel_type": "dm",
                "mentions": ["ember-owl"],
                "respond_policy": "always",
            },
            channel_id="dm:alice:ember-owl",
            sender_id="alice",
            message_id="msg-1",
            metadata={"sender_participant_type": "user", "interaction_id": "itx-123"},
        )
        try:
            actions = await agent.on_event(event)
        finally:
            await agent.close_memory()

        # The turn ran to a reply, on one model call.
        assert ActionType.SEND_CHANNEL_MESSAGE in [a.action_type for a in actions]
        assert provider.create_message.await_count == 1
        assert len(wallet.lease_kwargs) == 1
        lease = wallet.lease_kwargs[0]
        assert lease["cause"] == walletpb.CAUSE_CHANNEL_MESSAGE
        assert lease["agent_id"] == "ember-owl"
        assert lease["interaction_id"] == "itx-123"

    @pytest.mark.asyncio
    async def test_revise_round_leases_carry_the_events_interaction(self):
        """One group-channel turn with a revise round, through the real action
        loop: the turn takes three leases, for the compose call, the critic
        and the rewrite, and each names the interaction the message arrived
        under. A revise round that billed no interaction would spend outside
        that interaction's cost ceiling.

        The salience seam is replaced so the turn has a plan and one revise
        round without a bid of its own; everything after it is the real loop
        and the real reflexion module. The critic reads the draft as weak, so
        the rewrite runs. The compose and rewrite calls go to the persona's
        own model and the critic to the ``fast`` alias, which is how the
        three leases are told apart.
        """
        wallet = _RecordingWallet()
        compose_and_rewrite = iter([
            "```json\n" + json.dumps([{
                "action_type": "send_channel_message",
                "payload": {"channel_id": "group:planning", "content": "first draft"},
            }]) + "\n```",
            "revised reply",
        ])

        def _respond(**kwargs):
            if kwargs["model"] == "mock-fast":
                return LLMResponse(text="weak: yes\ncritique: buries the point")
            return LLMResponse(text=next(compose_and_rewrite))

        provider = AsyncMock()
        provider.create_message = AsyncMock(side_effect=_respond)
        provider.format_tool_definitions = MagicMock(return_value=[])
        client = LLMClient(provider=provider, wallet=wallet)
        agent = create_persona_agent(
            agent_id="ember-owl", config=copy.deepcopy(_PERSONA_CONFIG), llm_client=client,
        )
        await agent.initialize_memory()
        event = AgentEvent(
            event_type=EventType.CHANNEL_MESSAGE,
            payload={
                "content": "Which datastore?",
                "channel_type": "group",
                "mentions": [],
                "respond_policy": "always",
            },
            channel_id="group:planning",
            sender_id="alice",
            message_id="msg-1",
            metadata={"sender_participant_type": "user", "interaction_id": "itx-123"},
        )
        salience = SalienceOutcome(
            silence=False,
            user_message="Which datastore?",
            seed=[{"role": "user", "content": "Which datastore?"}],
            plan=CompositionPlan(
                intent="answer", key_points=("name one",), addressed_to="alice",
                avoid_restating=(),
            ),
            revise=1,
        )
        fast_alias = {"fast": {
            "provider": "mock", "model": "mock-fast",
            "input_per_1m_tokens": 0.0, "output_per_1m_tokens": 0.0,
        }}
        try:
            with use_alias_map(fast_alias), patch(
                "agents.persona_runtime.action_loop.run_salience_gate",
                new=AsyncMock(return_value=salience),
            ):
                actions = await agent.on_event(event)
        finally:
            await agent.close_memory()

        # The round ran to its end: the reply that leaves is the rewrite.
        sent = [a for a in actions if a.action_type is ActionType.SEND_CHANNEL_MESSAGE]
        assert [a.payload["content"] for a in sent] == ["revised reply"]
        # Compose, critic, rewrite, in that order, one lease each.
        assert [lease["model"] for lease in wallet.lease_kwargs] == [
            "test-model", "mock-fast", "test-model",
        ]
        assert [lease["interaction_id"] for lease in wallet.lease_kwargs] == ["itx-123"] * 3
        assert {lease["cause"] for lease in wallet.lease_kwargs} == {
            walletpb.CAUSE_CHANNEL_MESSAGE,
        }


class TestLeaseInteractionIDForEvent:
    def test_reads_resolver_stamped_metadata(self):
        event = AgentEvent(
            event_type=EventType.CHANNEL_MESSAGE,
            payload={},
            channel_id="group:planning",
            sender_id="alice",
            metadata={"interaction_id": "itx-123"},
        )
        assert lease_interaction_id_for_event(event) == "itx-123"

    def test_untracked_and_malformed_resolve_empty(self):
        """Absent (legacy / pre-producer) and non-string values resolve to
        the untracked empty string — the same tolerance as every other
        metadata read at this boundary."""
        untracked = AgentEvent(
            event_type=EventType.CHANNEL_MESSAGE, payload={},
            channel_id="group:planning", sender_id="alice",
        )
        assert lease_interaction_id_for_event(untracked) == ""

        malformed = AgentEvent(
            event_type=EventType.CHANNEL_MESSAGE, payload={},
            channel_id="group:planning", sender_id="alice",
            metadata={"interaction_id": 42},
        )
        assert lease_interaction_id_for_event(malformed) == ""
