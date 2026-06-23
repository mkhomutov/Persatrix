"""RFC 0051 Phase 1a (v0.3.10) — the structured silence verdict, **dark**.

PR 1 of the RFC 0051 PR plan (``docs/rfcs/0051-pr-plan.md``). These pin the
*structured* deliberation grammar the bid emits under ``mode: bid|plan`` —
``{ should_post, reason_code, reason_note }`` — which **supersedes the numeric
score gate** ([RFC 0051 §C](../../docs/rfcs/0051-reasoning-before-posting.md)):
``should_post`` *is* the silence decision and the per-member ``threshold`` is
inert under reasoning (no ``score`` is emitted). The scalar ``mode: off`` path
is unchanged — its byte-for-byte contract is pinned by the existing
``test_salience_bid.py`` suite (every test there calls ``evaluate_salience``
with no ``mode``, i.e. the ``off`` default), so this file covers only the new
structured rung.

Load-bearing invariants:

* **Fail-closed to silence.** An unparseable structured verdict stays silent
  *and* increments a first-class ``deliberation.parse_failures`` counter (the
  mandatory, never-gated safety net) so a silent parser break is alertable, not
  buried in the suppression totals.
* **No score under reasoning.** A ``score:`` line in a structured response is
  ignored — ``should_post`` alone gates, so a high score cannot rescue a
  ``should_post: no`` and a low score cannot veto a ``should_post: yes``.
* **Dark.** The structured path is reachable only when a caller passes
  ``mode="bid"``/``"plan"``; the seam does not yet (PR 4 wires the config knob).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from agents.llm_client import LLMClient, LLMResponse
from agents.model_aliases import use_alias_map
from agents.salience_bid import SalienceDecision, evaluate_salience
from agents.salience_deliberation import (
    MODE_BID,
    MODE_OFF,
    MODE_PLAN,
    REASON_ADDS_SUBSTANCE,
    REASON_ALREADY_ANSWERED,
    REASON_NOTHING_TO_ADD,
    REASON_ONLY_AGREEING,
    is_structured,
    max_output_tokens_for,
)

_FAST_ALIAS_MAP: dict[str, dict[str, Any]] = {
    "fast": {
        "provider": "mock",
        "model": "mock-fast",
        "input_per_1m_tokens": 0.0,
        "output_per_1m_tokens": 0.0,
    },
}

_TRANSCRIPT: list[dict[str, Any]] = [
    {"role": "user", "content": "[iron-fox]: We should pick a database for the cache."},
    {"role": "assistant", "content": "Redis is the obvious fit for a cache layer."},
]


def _client(text: str | None = None, *, raises: Exception | None = None) -> LLMClient:
    provider = AsyncMock()
    if raises is not None:
        provider.create_message = AsyncMock(side_effect=raises)
    else:
        provider.create_message = AsyncMock(return_value=LLMResponse(text=text))
    return LLMClient(provider)


async def _bid(
    *,
    client: LLMClient,
    mode: str,
    content: str = "What database should we use for the cache?",
    threshold: float | None = 0.4,
) -> SalienceDecision:
    with use_alias_map(_FAST_ALIAS_MAP):
        return await evaluate_salience(
            llm_client=client,
            content=content,
            transcript=_TRANSCRIPT,
            agent_id="ember-owl",
            persona_name="Ember Owl",
            persona_role="VP of Engineering",
            threshold=threshold,
            mode=mode,
        )


class TestStructuredVerdict:
    @pytest.mark.parametrize("mode", [MODE_BID, MODE_PLAN])
    async def test_should_post_yes_speaks_with_a_reason_code(self, mode: str):
        """A well-formed ``should_post: yes`` speaks, carries the speak-side
        ``reason_code``, and — crucially — emits **no score** (the threshold
        machinery is superseded under reasoning)."""
        decision = await _bid(
            client=_client("should_post: yes\nreason_code: adds_substance"),
            mode=mode,
        )
        assert decision.speak is True
        assert decision.reason == REASON_ADDS_SUBSTANCE
        assert decision.score is None

    @pytest.mark.parametrize(
        "code",
        [REASON_ALREADY_ANSWERED, REASON_ONLY_AGREEING, REASON_NOTHING_TO_ADD],
    )
    async def test_should_post_no_stays_silent_with_its_reason_code(self, code: str):
        decision = await _bid(
            client=_client(f"should_post: no\nreason_code: {code}"),
            mode=MODE_BID,
        )
        assert decision.speak is False
        assert decision.reason == code
        assert decision.score is None

    async def test_reason_note_is_captured_debug_only(self):
        """``reason_note`` is the one genuinely new field — an optional free
        clause captured on the decision (its only egress is the operator-debug
        path, wired in a later PR). It never becomes the low-cardinality
        ``reason`` label."""
        decision = await _bid(
            client=_client(
                "should_post: no\nreason_code: only_agreeing\n"
                "reason_note: iron-fox already covered the cache trade-off",
            ),
            mode=MODE_BID,
        )
        assert decision.speak is False
        assert decision.reason == REASON_ONLY_AGREEING
        assert decision.reason_note == "iron-fox already covered the cache trade-off"

    async def test_missing_reason_note_is_none(self):
        decision = await _bid(
            client=_client("should_post: yes\nreason_code: adds_substance"),
            mode=MODE_BID,
        )
        assert decision.reason_note is None

    async def test_reason_note_placeholder_echo_is_dropped(self):
        """A model with nothing to justify may echo the user snippet's literal
        ``<one short clause on why — optional>`` placeholder verbatim. That is
        template noise, not a justification — an angle-bracket-wrapped capture
        must not leak into the operator-debug egress (RFC 0051 §E, wired in a
        later PR); it drops to ``None`` like a missing note."""
        decision = await _bid(
            client=_client(
                "should_post: no\nreason_code: only_agreeing\n"
                "reason_note: <one short clause on why — optional>",
            ),
            mode=MODE_BID,
        )
        assert decision.speak is False
        assert decision.reason == REASON_ONLY_AGREEING
        assert decision.reason_note is None

    async def test_unknown_reason_code_falls_to_a_safe_default(self):
        """An off-enum ``reason_code`` must not reach the metric verbatim
        (cardinality blow-up). It collapses to the mode-appropriate default —
        a speak verdict to ``adds_substance``, a silence verdict to
        ``nothing_to_add`` — never the raw string."""
        speak = await _bid(
            client=_client("should_post: yes\nreason_code: i_just_feel_like_it"),
            mode=MODE_BID,
        )
        assert speak.speak is True
        assert speak.reason == REASON_ADDS_SUBSTANCE

        silence = await _bid(
            client=_client("should_post: no\nreason_code: vibes"),
            mode=MODE_BID,
        )
        assert silence.speak is False
        assert silence.reason == REASON_NOTHING_TO_ADD

    async def test_missing_reason_code_uses_the_default(self):
        decision = await _bid(
            client=_client("should_post: yes"),
            mode=MODE_BID,
        )
        assert decision.speak is True
        assert decision.reason == REASON_ADDS_SUBSTANCE

    async def test_missing_reason_code_on_a_no_defaults_to_nothing_to_add(self):
        """The silence-side mirror of the test above: ``should_post: no`` with no
        ``reason_code`` line collapses to the silence default ``nothing_to_add``,
        never an empty or raw label (so the metric stays bounded)."""
        decision = await _bid(
            client=_client("should_post: no"),
            mode=MODE_BID,
        )
        assert decision.speak is False
        assert decision.reason == REASON_NOTHING_TO_ADD


class TestNoScoreUnderReasoning:
    """[RFC 0051 OQ 7] — the structured verdict supersedes the score gate:
    ``should_post`` alone decides, so a ``score:`` line is inert."""

    async def test_high_score_cannot_rescue_a_should_post_no(self):
        decision = await _bid(
            client=_client("should_post: no\nreason_code: only_agreeing\nscore: 0.99"),
            mode=MODE_BID,
            threshold=0.4,
        )
        assert decision.speak is False
        assert decision.score is None

    async def test_low_score_cannot_veto_a_should_post_yes(self):
        decision = await _bid(
            client=_client("should_post: yes\nreason_code: adds_substance\nscore: 0.01"),
            mode=MODE_BID,
            threshold=0.4,
        )
        assert decision.speak is True
        assert decision.score is None

    async def test_unset_threshold_does_not_gate_under_reasoning(self):
        """An unset ``threshold`` forces a decisive score under ``mode: off``;
        under reasoning it is irrelevant — ``should_post: yes`` speaks."""
        decision = await _bid(
            client=_client("should_post: yes\nreason_code: adds_substance"),
            mode=MODE_BID,
            threshold=None,
        )
        assert decision.speak is True


class TestFailClosedAndCounter:
    async def test_unparseable_structured_verdict_is_silence(self):
        """No ``should_post:`` line → fail-closed silence with the shared
        ``parse_failure`` label."""
        decision = await _bid(
            client=_client("I think we should probably consider Postgres."),
            mode=MODE_BID,
        )
        assert decision.speak is False
        assert decision.reason == "parse_failure"
        assert decision.score is None

    async def test_empty_structured_response_is_silence(self):
        decision = await _bid(client=_client(None), mode=MODE_BID)
        assert decision.speak is False
        assert decision.reason == "parse_failure"

    @pytest.mark.parametrize("mode", [MODE_BID, MODE_PLAN])
    async def test_parse_failure_increments_the_deliberation_counter(self, mode: str):
        """The mandatory, never-gated safety net: a fail-closed structured
        parse error increments ``deliberation.parse_failures`` (asserted, not
        incidental) so a silent parser break is alertable — distinct from the
        Tier-B ``channel.messages.gated`` suppression rows."""
        import sys

        sys.path.insert(0, "tests")
        from _otel_test_helpers import build_meter, counter_total  # noqa: PLC0415

        reader, metrics_mod = build_meter()
        try:
            assert counter_total(reader, "deliberation.parse_failures") == 0
            await _bid(client=_client("not a verdict at all"), mode=mode)
            assert counter_total(reader, "deliberation.parse_failures") == 1
        finally:
            await metrics_mod.shutdown()

    async def test_off_mode_parse_failure_does_not_touch_the_counter(self):
        """The counter is specifically the *deliberation* safety net — a
        scalar (``mode: off``) parse failure flows through the existing
        seam-side ``channel.messages.gated`` path, not this counter, so
        ``mode: off`` stays byte-for-byte today's behaviour."""
        import sys

        sys.path.insert(0, "tests")
        from _otel_test_helpers import build_meter, counter_total  # noqa: PLC0415

        reader, metrics_mod = build_meter()
        try:
            await _bid(client=_client("no parseable score here"), mode=MODE_OFF)
            assert counter_total(reader, "deliberation.parse_failures") == 0
        finally:
            await metrics_mod.shutdown()


class TestTokenScaling:
    """The output-token cap scales with ``mode``: ``_SCALAR_MAX_OUTPUT_TOKENS``
    (64) → ``_BID_MAX_OUTPUT_TOKENS`` (128) for the structured verdict →
    ``_PLAN_MAX_OUTPUT_TOKENS`` (320) for the eventual ``plan``
    ``CompositionPlan`` (PR 3)."""

    def test_token_budget_is_monotonic_in_mode(self):
        off = max_output_tokens_for(MODE_OFF)
        bid = max_output_tokens_for(MODE_BID)
        plan = max_output_tokens_for(MODE_PLAN)
        assert off == 64
        assert off < bid < plan

    def test_unknown_mode_is_treated_as_off(self):
        assert max_output_tokens_for("garbage") == max_output_tokens_for(MODE_OFF)
        assert is_structured("garbage") is False
        assert is_structured(MODE_BID) is True
        assert is_structured(MODE_PLAN) is True
        assert is_structured(MODE_OFF) is False

    async def test_structured_bid_requests_the_scaled_budget(self):
        """The provider call under ``mode: bid`` carries the scaled output
        budget, not the scalar 64 — proving the bigger structured verdict is
        not truncated into a parse failure."""
        provider = AsyncMock()
        provider.create_message = AsyncMock(
            return_value=LLMResponse(text="should_post: no\nreason_code: nothing_to_add"),
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
                mode=MODE_BID,
            )
        kwargs = provider.create_message.await_args.kwargs
        assert kwargs["max_tokens"] == max_output_tokens_for(MODE_BID)


class TestStructuredPromptShape:
    """The structured rung swaps in a ``should_post``/``reason_code`` prompt;
    the scalar two-line speak/score form is reserved for ``mode: off``."""

    def test_structured_mode_uses_the_reasoning_grammar(self):
        from agents.salience_addressing import NLAddressing  # noqa: PLC0415
        from agents.salience_bid import _build_bid_messages  # noqa: PLC0415

        body = _build_bid_messages(
            content="hi",
            transcript=[],
            addressing=NLAddressing(False, False),
            mode=MODE_BID,
        )[0]["content"]
        assert "should_post: yes|no" in body
        assert "reason_code:" in body
        assert "speak: yes|no" not in body

    def test_off_mode_keeps_the_scalar_grammar(self):
        from agents.salience_addressing import NLAddressing  # noqa: PLC0415
        from agents.salience_bid import _build_bid_messages  # noqa: PLC0415

        body = _build_bid_messages(
            content="hi",
            transcript=[],
            addressing=NLAddressing(False, False),
            mode=MODE_OFF,
        )[0]["content"]
        assert "speak: yes|no" in body
        assert "should_post:" not in body


class TestStructuredParserRigor:
    """The structured ``should_post`` parser keeps the scalar parser's
    fail-*toward-silence* rigor (cf. ``_SCORE_RE``'s ``(?!\\d)`` guard): a token
    that merely *starts* with ``yes`` must never partial-match into a speak
    verdict — the one direction the parser could fail toward speech."""

    async def test_yes_prefixed_word_does_not_leak_to_speak(self):
        """``should_post: yesterday`` must not clip to ``yes`` and speak; it
        falls through to ``parse_failure`` → silence (fail-closed)."""
        decision = await _bid(
            client=_client("should_post: yesterday\nreason_code: adds_substance"),
            mode=MODE_BID,
        )
        assert decision.speak is False
        assert decision.reason == "parse_failure"

    @pytest.mark.parametrize(
        "verdict", ["should_post: yes.", "should_post: yes,", "should_post: yes"],
    )
    async def test_trailing_punctuation_after_yes_still_speaks(self, verdict: str):
        """The word-boundary guard must not over-reject: a sentence-final or
        comma-trailed ``yes`` still parses as a speak verdict."""
        decision = await _bid(
            client=_client(f"{verdict}\nreason_code: adds_substance"), mode=MODE_BID,
        )
        assert decision.speak is True

    async def test_no_prefixed_word_falls_closed_to_silence(self):
        """``should_post: nope`` is outside the asked grammar; whichever way it
        is read the direction is *silence*, never speech."""
        decision = await _bid(client=_client("should_post: nope"), mode=MODE_BID)
        assert decision.speak is False


class TestAddressingUnderReasoning:
    """Under reasoning the NL-addressing signal is **advisory only**: it still
    rides the prompt as a prose nudge, but the deterministic ``_bar_for`` shift
    (TB4) is part of the score gate the structured verdict supersedes, so it can
    no longer change the outcome on its own — ``should_post`` governs. These pin
    that intended contract (previously untested)."""

    async def test_self_named_does_not_rescue_a_should_post_no(self):
        """Invited-by-name lowers the *scalar* bar; under reasoning there is no
        bar, so ``should_post: no`` still silences the named persona."""
        decision = await _bid(
            client=_client("should_post: no\nreason_code: only_agreeing"),
            mode=MODE_BID,
            content="Let's hear from Ember Owl on this.",
        )
        assert decision.speak is False
        assert decision.reason == REASON_ONLY_AGREEING

    async def test_other_named_does_not_veto_a_should_post_yes(self):
        """Someone-else-invited raises the *scalar* bar; under reasoning
        ``should_post: yes`` speaks regardless."""
        decision = await _bid(
            client=_client("should_post: yes\nreason_code: adds_substance"),
            mode=MODE_BID,
            content="Let's hear from Iron Fox on this.",
        )
        assert decision.speak is True
        assert decision.reason == REASON_ADDS_SUBSTANCE

    def test_addressing_prose_still_rides_under_reasoning(self):
        """Advisory, not ignored: the self-invite nudge is still rendered into
        the structured prompt body even though the deterministic shift is gone."""
        from agents.salience_addressing import NLAddressing  # noqa: PLC0415
        from agents.salience_bid import _build_bid_messages  # noqa: PLC0415

        body = _build_bid_messages(
            content="hi",
            transcript=[],
            addressing=NLAddressing(self_named=True, other_named=False),
            mode=MODE_BID,
        )[0]["content"]
        assert "invited by name" in body


class TestUnknownModeIsLoud:
    """A typo'd ``mode`` falls back to the scalar gate (fail-safe), but must not
    do so *silently* — the config-layer ``validate`` that rejects an unbacked
    ``mode`` outright is a later PR (PR 4), so the bid logs the fallback now."""

    def test_is_known_mode(self):
        from agents.salience_deliberation import is_known_mode  # noqa: PLC0415

        assert is_known_mode(MODE_OFF) is True
        assert is_known_mode(MODE_BID) is True
        assert is_known_mode(MODE_PLAN) is True
        assert is_known_mode("garbage") is False

    async def test_unknown_mode_warns_and_falls_back_to_scalar(self, caplog):
        import logging  # noqa: PLC0415

        with caplog.at_level(logging.WARNING):
            decision = await _bid(
                client=_client("should_post: yes\nreason_code: adds_substance"),
                mode="garbage",
            )
        # Scalar fallback: a structured-looking text has no parseable ``score:``,
        # so the scalar gate silences it — proving the unknown mode degraded to
        # ``off`` rather than taking the structured path.
        assert decision.speak is False
        assert "unrecognised reasoning mode" in caplog.text

    async def test_unknown_mode_warns_even_when_model_unresolvable(self, caplog):
        """The unknown-mode diagnostic must not depend on alias resolution: with
        the shipped ``fast`` alias unconfigured (``resolve`` → ``SystemExit`` →
        ``model_unresolvable``), a typo'd ``mode`` is *still* logged. Regression
        guard for the warn-*before*-resolve ordering — otherwise an unresolvable
        model silently swallows the typo signal."""
        import logging  # noqa: PLC0415

        # No ``use_alias_map``: the shipped ``fast`` alias is unconfigured, so
        # ``resolve`` raises ``SystemExit`` and the bid returns model_unresolvable
        # before it would ever reach the (former) warn site.
        client = _client("should_post: yes\nreason_code: adds_substance")
        with caplog.at_level(logging.WARNING):
            decision = await evaluate_salience(
                llm_client=client,
                content="anything",
                transcript=_TRANSCRIPT,
                agent_id="ember-owl",
                persona_name="Ember Owl",
                persona_role="VP of Engineering",
                threshold=0.4,
                mode="garbage",
            )
        assert decision.reason == "model_unresolvable"
        assert "unrecognised reasoning mode" in caplog.text
