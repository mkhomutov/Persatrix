"""Layer 1 lease attribution — `interaction_id` threading (producer plan PR 2).

The wallet has accepted ``interaction_id`` on ``AcquireLease`` since the
governance-layers PR 2, but no channel-path call site passed it — the cost
ceiling could enforce a per-interaction total it could never attribute.
This matrix (TDD-first, written red) pins the three links of the chain:

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
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest

from agents.generated import wallet_pb2 as walletpb
from agents.llm_client import LLMClient, LLMResponse
from agents.persona_runtime.wallet_cause import lease_interaction_id_for_event
from agents.persona_types import AgentEvent, EventType


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
