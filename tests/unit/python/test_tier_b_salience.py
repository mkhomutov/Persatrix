"""RFC 0030 Tier B (v0.3.8) — the leased ``fast``-model salience bid.

PR 2 of the Tier B PR plan
(``docs/rfcs/0030-amendment-relevance-gated-response-tierb-pr-plan.md``).
These are the **red** half of the TDD pair for
:mod:`agents.tier_b_salience`: they pin the bid's bias-to-silence
contract before the module exists.

The bid is the no-pile-on decision. On the open-floor remainder Tier A
leaves (a ``participant`` admitted with ``reason="policy_always"``), it
asks a cheap ``fast``-model call "do I have something worth adding that
hasn't already been said?" and stays silent unless the answer clears the
member's ``threshold``. Load-bearing invariants (amendment OQs + master
plan §Open-question status):

* **TB2 — bias-to-silence.** An unset (``None``) ``threshold`` requires a
  *decisively* high score; a parse failure, a lease denial, an
  unresolvable model, or ``score < threshold`` all resolve to silence.
* **TB3 — every bid is leased + attributable.** The call carries
  ``cause=CAUSE_CHANNEL_MESSAGE`` and runs on the ``fast`` alias.

The bid is pure of runtime wiring: it takes the inbound content, the
in-round transcript, and the resolved ``threshold`` and returns a
:class:`SalienceDecision`. The action-loop seam (when it fires) is pinned
separately in ``test_tier_b_action_loop.py``.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from agents.generated import wallet_pb2 as walletpb
from agents.llm_client import LLMClient, LLMResponse
from agents.model_aliases import use_alias_map
from agents.tier_b_salience import (
    DEFAULT_TIER_B_MAX_CHANNEL_MEMBERS,
    SalienceDecision,
    evaluate_salience,
    skip_bid_for_channel_size,
)
from agents.wallet_client import BudgetExceededError

# A `fast` alias that resolves to the mock provider for the duration of a
# test, so the bid's internal `resolve("fast")` does not hit the shipped
# `unconfigured` provider (which fails loud by design).
_FAST_ALIAS_MAP: dict[str, dict[str, Any]] = {
    "fast": {
        "provider": "mock",
        "model": "mock-fast",
        "input_per_1m_tokens": 0.0,
        "output_per_1m_tokens": 0.0,
    },
}


def _client(text: str | None = None, *, raises: Exception | None = None) -> LLMClient:
    """Mock :class:`LLMClient` whose single provider call returns ``text``
    (or raises ``raises``)."""
    provider = AsyncMock()
    if raises is not None:
        provider.create_message = AsyncMock(side_effect=raises)
    else:
        provider.create_message = AsyncMock(return_value=LLMResponse(text=text))
    return LLMClient(provider)


_TRANSCRIPT: list[dict[str, Any]] = [
    {"role": "user", "content": "[iron-fox]: We should pick a database for the cache."},
    {"role": "assistant", "content": "Redis is the obvious fit for a cache layer."},
]


async def _bid(
    *,
    client: LLMClient,
    threshold: float | None,
    content: str = "What database should we use for the cache?",
    transcript: list[dict[str, Any]] | None = None,
) -> SalienceDecision:
    with use_alias_map(_FAST_ALIAS_MAP):
        return await evaluate_salience(
            llm_client=client,
            content=content,
            transcript=_TRANSCRIPT if transcript is None else transcript,
            agent_id="ember-owl",
            persona_name="Ember Owl",
            persona_role="VP of Engineering",
            threshold=threshold,
        )


class TestSpeakWhenSalient:
    async def test_in_domain_unaddressed_clears_an_explicit_threshold(self):
        """A decisively-scored bid clears a configured threshold → speak."""
        decision = await _bid(
            client=_client("speak: yes\nscore: 0.9"),
            threshold=0.4,
        )
        assert decision.speak is True
        assert decision.reason == "salient"
        assert decision.score == pytest.approx(0.9)

    async def test_score_at_threshold_speaks(self):
        """The threshold is an inclusive floor (``score >= threshold``)."""
        decision = await _bid(
            client=_client("speak: yes\nscore: 0.40"),
            threshold=0.4,
        )
        assert decision.speak is True

    async def test_missing_speak_line_falls_through_to_score(self):
        """The grammar is forgiving: a bare ``score:`` (no ``speak:`` line)
        is governed by the score alone — a clearing score speaks."""
        decision = await _bid(
            client=_client("score: 0.9"),
            threshold=0.4,
        )
        assert decision.speak is True
        assert decision.reason == "salient"


class TestBiasToSilence:
    async def test_redundant_followup_stays_silent(self):
        """A low score (the point was already made) → silence."""
        decision = await _bid(
            client=_client("speak: no\nscore: 0.1"),
            threshold=0.4,
        )
        assert decision.speak is False
        assert decision.reason == "below_threshold"

    async def test_unset_threshold_requires_a_decisive_score(self):
        """TB2: an unset threshold biases to silence — a middling score
        that *would* clear a configured bar still stays silent."""
        decision = await _bid(
            client=_client("speak: yes\nscore: 0.5"),
            threshold=None,
        )
        assert decision.speak is False
        assert decision.reason == "below_threshold"

    async def test_unset_threshold_speaks_only_when_decisive(self):
        decision = await _bid(
            client=_client("speak: yes\nscore: 0.95"),
            threshold=None,
        )
        assert decision.speak is True

    async def test_speak_no_vetoes_a_clearing_score(self):
        """TB2: an explicit ``speak: no`` is a one-way veto toward silence —
        even a score that clears the threshold stays silent. The veto only
        ever *adds* silence, so it cannot cause pile-on."""
        decision = await _bid(
            client=_client("speak: no\nscore: 0.95"),
            threshold=0.4,
        )
        assert decision.speak is False
        assert decision.reason == "declined"

    async def test_parse_failure_is_silence(self):
        """Unparseable bid output → fail-closed silence (TB2)."""
        decision = await _bid(
            client=_client("I think we should probably consider Postgres."),
            threshold=0.4,
        )
        assert decision.speak is False
        assert decision.reason == "parse_failure"

    async def test_empty_response_is_silence(self):
        decision = await _bid(client=_client(None), threshold=0.4)
        assert decision.speak is False
        assert decision.reason == "parse_failure"

    async def test_lease_denial_is_silence(self):
        """TB3: a denied lease fails closed → no bid → silence."""
        decision = await _bid(
            client=_client(raises=BudgetExceededError("denied")),
            threshold=0.4,
        )
        assert decision.speak is False
        assert decision.reason == "lease_denied"

    async def test_provider_error_is_silence(self):
        decision = await _bid(
            client=_client(raises=RuntimeError("boom")),
            threshold=0.4,
        )
        assert decision.speak is False
        assert decision.reason == "llm_error"

    async def test_unresolvable_model_is_silence(self):
        """No `fast` alias configured → resolve() SystemExit → silence,
        never an uncaught crash on the hot path."""
        client = _client("speak: yes\nscore: 0.9")
        # No `use_alias_map`: the shipped `fast` alias is `unconfigured`,
        # so resolve() raises SystemExit, which the bid must swallow.
        decision = await evaluate_salience(
            llm_client=client,
            content="anything",
            transcript=_TRANSCRIPT,
            agent_id="ember-owl",
            persona_name="Ember Owl",
            persona_role="VP of Engineering",
            threshold=0.4,
        )
        assert decision.speak is False
        assert decision.reason == "model_unresolvable"


class TestChannelSizeCap:
    """TB6 — above ``tier_b_max_channel_members`` the bid is skipped and the
    channel falls back to ``addressed``-only, so bid fan-out stays small."""

    def test_under_cap_runs_the_bid(self):
        assert skip_bid_for_channel_size(channel_size=4, max_members=20) is False

    def test_at_cap_runs_the_bid(self):
        assert skip_bid_for_channel_size(channel_size=20, max_members=20) is False

    def test_over_cap_skips_the_bid(self):
        assert skip_bid_for_channel_size(channel_size=21, max_members=20) is True

    def test_unknown_channel_size_runs_the_bid(self):
        """A missing/zero channel size cannot trigger the cap — fall through
        to the bid rather than silently suppressing on no information."""
        assert skip_bid_for_channel_size(channel_size=0, max_members=20) is False
        assert skip_bid_for_channel_size(channel_size=None, max_members=20) is False

    def test_nonpositive_cap_disables_the_cap(self):
        """A zero/absent cap means 'no cap' — a large channel still bids."""
        assert skip_bid_for_channel_size(channel_size=999, max_members=0) is False

    def test_default_cap_constant_is_positive(self):
        assert DEFAULT_TIER_B_MAX_CHANNEL_MEMBERS > 0


class TestLeasedAndFast:
    async def test_bid_is_leased_and_uses_the_fast_alias(self):
        """TB3: the bid call carries the channel-message cause and the
        ``fast`` alias so it is leased + attributable on the wallet."""
        provider = AsyncMock()
        provider.create_message = AsyncMock(
            return_value=LLMResponse(text="speak: no\nscore: 0.0"),
        )
        client = LLMClient(provider)
        with use_alias_map(_FAST_ALIAS_MAP):
            await evaluate_salience(
                llm_client=client,
                content="q",
                transcript=_TRANSCRIPT,
                agent_id="ember-owl",
                persona_name="Ember Owl",
                persona_role="VP of Engineering",
                threshold=0.4,
            )
        # One provider call, with the fast physical model, no tools, and a
        # small output budget.
        provider.create_message.assert_awaited_once()
        kwargs = provider.create_message.await_args.kwargs
        assert kwargs["model"] == "mock-fast"
        assert kwargs["tools"] == []

    async def test_bid_passes_channel_message_cause(self):
        """The cause is asserted at the LLMClient seam: the bid threads
        ``CAUSE_CHANNEL_MESSAGE`` so the wallet attributes it."""
        client = _client("speak: no\nscore: 0.0")
        # Spy on the LLMClient.create_message wrapper to capture `cause`.
        seen: dict[str, Any] = {}
        original = client.create_message

        async def _spy(**kwargs: Any) -> LLMResponse:
            seen.update(kwargs)
            return await original(**kwargs)

        client.create_message = _spy  # type: ignore[method-assign]
        with use_alias_map(_FAST_ALIAS_MAP):
            await evaluate_salience(
                llm_client=client,
                content="q",
                transcript=_TRANSCRIPT,
                agent_id="ember-owl",
                persona_name="Ember Owl",
                persona_role="VP of Engineering",
                threshold=0.4,
            )
        assert seen["cause"] == walletpb.CAUSE_CHANNEL_MESSAGE
        assert seen["agent_id"] == "ember-owl"
        assert seen["model_alias"] == "fast"
