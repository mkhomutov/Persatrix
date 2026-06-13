"""RFC 0030 Tier B (v0.3.8) — the leased ``fast``-model salience bid.

PR 2 of the Tier B PR plan
(``docs/rfcs/0030-amendment-relevance-gated-response-tierb-pr-plan.md``).
These are the **red** half of the TDD pair for
:mod:`agents.salience_bid`: they pin the bid's bias-to-silence
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
separately in ``test_salience_action_loop.py``.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import grpc
import grpc.aio
import pytest

from agents.generated import wallet_pb2 as walletpb
from agents.llm_client import LLMClient, LLMResponse
from agents.model_aliases import use_alias_map
from agents.salience_bid import (
    DEFAULT_SALIENCE_MAX_CHANNEL_MEMBERS,
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

    @pytest.mark.parametrize("text", ["score: 10", "speak: yes\nscore: 100"])
    async def test_out_of_grammar_integer_score_is_silence(self, text: str):
        """An out-of-grammar integer (e.g. a model answering on a 0-10 or
        0-100 scale) must *not* be truncated to a clearing ``1.0`` and
        admitted — the one place the parser would otherwise fail *toward*
        speech, against bias-to-silence (TB2). A score the grammar cannot
        read as a ``[0, 1]`` value is a parse failure → silence."""
        decision = await _bid(client=_client(text), threshold=0.4)
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

    async def test_resource_exhausted_lease_cap_is_lease_denied(self):
        """TB3: the wallet's per-agent active-lease cap surfaces as a raw
        ``AioRpcError(RESOURCE_EXHAUSTED)`` — ``WalletClient._acquire``
        re-raises it *unwrapped* after exhausting its retry budget, so it
        never becomes a ``BudgetExceededError``. It is wallet back-pressure
        (a lease that could not be acquired), so it must fail closed and be
        labelled ``lease_denied``, not the generic ``llm_error`` — matching
        how the action loop's ``handle_llm_call_exception`` treats it."""
        err = grpc.aio.AioRpcError(
            grpc.StatusCode.RESOURCE_EXHAUSTED,
            grpc.aio.Metadata(), grpc.aio.Metadata(),
            details="active-lease cap",
        )
        decision = await _bid(client=_client(raises=err), threshold=0.4)
        assert decision.speak is False
        assert decision.reason == "lease_denied"

    async def test_other_grpc_error_is_llm_error(self):
        """A non-``RESOURCE_EXHAUSTED`` gRPC failure is a real provider/server
        problem, not back-pressure — it degrades to ``llm_error`` so the two
        operational signals stay distinct (mirrors the action loop)."""
        err = grpc.aio.AioRpcError(
            grpc.StatusCode.UNAVAILABLE,
            grpc.aio.Metadata(), grpc.aio.Metadata(),
            details="provider down",
        )
        decision = await _bid(client=_client(raises=err), threshold=0.4)
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
    """TB6 — above ``salience_max_channel_members`` the bid is skipped and the
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
        assert DEFAULT_SALIENCE_MAX_CHANNEL_MEMBERS > 0


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
        """The cause is asserted at the LLMClient seam: the bid defaults to
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

    async def test_bid_threads_the_caller_supplied_cause(self):
        """The caller (the action-loop seam) derives the wallet ``cause`` from
        the event and passes it in, so the bid bills the same cause as the
        quality turn (e.g. ``CAUSE_CHAT`` for a chat-shaped message). The bid
        must thread the supplied value, not override it with a constant."""
        client = _client("speak: no\nscore: 0.0")
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
                cause=walletpb.CAUSE_CHAT,
            )
        assert seen["cause"] == walletpb.CAUSE_CHAT


class TestNLAddressingBiasesTheBid:
    """PR 3 / TB4 — NL addressing shifts the bid's *bar*, never a hard
    pre-filter: a non-named persona with a decisive contribution still clears."""

    async def test_baseline_middling_score_speaks(self):
        """Control: with no addressing, a score clearing the threshold speaks."""
        decision = await _bid(
            client=_client("speak: yes\nscore: 0.5"),
            threshold=0.4,
            content="What database should we use for the cache?",
        )
        assert decision.speak is True

    async def test_addressed_elsewhere_raises_the_bar_to_silence(self):
        """The same middling score that *would* clear the bar now stays silent
        when the message invites someone else by name — a shift, not a drop."""
        decision = await _bid(
            client=_client("speak: yes\nscore: 0.5"),
            threshold=0.4,
            content="let's hear from Iron Fox on this",
        )
        assert decision.speak is False
        assert decision.reason == "below_threshold"

    async def test_addressed_self_lowers_the_bar_to_speak(self):
        """A normally-silent score speaks when the persona is invited by name."""
        decision = await _bid(
            client=_client("speak: yes\nscore: 0.3"),
            threshold=0.4,
            content="let's hear from Ember Owl on this",
        )
        assert decision.speak is True
        assert decision.reason == "salient"

    async def test_addressed_elsewhere_is_not_a_hard_filter(self):
        """The invariant: a non-named persona with a *decisive* in-domain
        contribution still clears even when someone else was invited (TB4)."""
        decision = await _bid(
            client=_client("speak: yes\nscore: 0.95"),
            threshold=0.4,
            content="let's hear from Iron Fox on this",
        )
        assert decision.speak is True
        assert decision.reason == "salient"

    async def test_unset_threshold_addressed_elsewhere_still_clears_decisive(self):
        """Finding #1 — on the unset-threshold path the someone-else penalty is
        capped at ``_ADDRESSED_OTHER_CEILING`` (0.9, strictly below 1.0), so a
        near-certain 0.95 still clears (no hard drop)."""
        decision = await _bid(
            client=_client("speak: yes\nscore: 0.95"),
            threshold=None,
            content="let's hear from Iron Fox on this",
        )
        assert decision.speak is True
        assert decision.reason == "salient"

    async def test_unset_threshold_addressed_elsewhere_biases_away_from_others(self):
        """Finding #1 regression guard — on the unset-threshold path the penalty
        must still *bite* (the old ceiling collapsed to 0.8, going inert). A
        merely-decisive 0.85 now defers; a near-certain 0.95 (above) clears."""
        decision = await _bid(
            client=_client("speak: yes\nscore: 0.85"),
            threshold=None,
            content="let's hear from Iron Fox on this",
        )
        assert decision.speak is False
        assert decision.reason == "below_threshold"

    async def test_unset_threshold_addressed_elsewhere_still_biases_to_silence(self):
        """The companion: a middling score on the unset path still stays silent."""
        decision = await _bid(
            client=_client("speak: yes\nscore: 0.5"),
            threshold=None,
            content="let's hear from Iron Fox on this",
        )
        assert decision.speak is False
        assert decision.reason == "below_threshold"

    async def test_addressed_elsewhere_score_exactly_at_shifted_bar_clears(self):
        """The shifted bar stays an *inclusive* floor: with ``threshold=0.4`` the
        penalty lifts it to 0.6, and a float-naive ``0.4 + 0.2`` =
        0.6000000000000001 must not silence a score of exactly 0.6."""
        decision = await _bid(
            client=_client("speak: yes\nscore: 0.6"),
            threshold=0.4,
            content="let's hear from Iron Fox on this",
        )
        assert decision.speak is True
        assert decision.reason == "salient"

    async def test_addressed_elsewhere_still_runs_the_bid(self):
        """No pre-filter short-circuit: the leased bid is still issued for a
        non-named persona (the score decides, not a deterministic NL drop)."""
        provider = AsyncMock()
        provider.create_message = AsyncMock(
            return_value=LLMResponse(text="speak: no\nscore: 0.1"),
        )
        client = LLMClient(provider)
        with use_alias_map(_FAST_ALIAS_MAP):
            await evaluate_salience(
                llm_client=client,
                content="let's hear from Iron Fox on this",
                transcript=_TRANSCRIPT,
                agent_id="ember-owl",
                persona_name="Ember Owl",
                persona_role="VP of Engineering",
                threshold=0.4,
            )
        provider.create_message.assert_awaited_once()
