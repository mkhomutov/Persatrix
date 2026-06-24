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
structured rung. This file holds the **grammar/verdict** half (parse, reason
codes, fail-closed, veto); the prompt/budget/mode-dispatch half lives in the
sibling ``test_salience_bid_reasoning_dispatch.py`` so each stays under the
500-line review cap (shared scaffold in ``tests/_salience_reasoning_helpers``).

Load-bearing invariants:

* **Fail-closed to silence.** An unparseable structured verdict stays silent
  *and* increments a first-class ``deliberation.parse_failures`` counter (the
  mandatory, never-gated safety net) so a silent parser break is alertable, not
  buried in the suppression totals.
* **No score under reasoning.** A ``score:`` line in a structured response is
  ignored — ``should_post`` alone gates, so a high score cannot rescue a
  ``should_post: no`` and a low score cannot veto a ``should_post: yes``.
* **Silence-side veto (TB2).** An explicit silence ``reason_code`` on a
  ``should_post: yes`` resolves to silence (parity with the scalar ``speak: no``
  one-way veto), and the silence code is preserved, not laundered.
* **Dark.** The structured path is reachable only when a caller passes
  ``mode="bid"``/``"plan"``; the seam does not yet (PR 4 wires the config knob).
"""

from __future__ import annotations

import pytest
from _salience_reasoning_helpers import _bid, _client

from agents.salience_deliberation import (
    MODE_BID,
    MODE_OFF,
    MODE_PLAN,
    REASON_ADDS_SUBSTANCE,
    REASON_ALREADY_ANSWERED,
    REASON_NOTHING_TO_ADD,
    REASON_ONLY_AGREEING,
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
        template noise, not a justification — it must not leak into the
        operator-debug egress (RFC 0051 §E, wired in a later PR); it drops to
        ``None`` like a missing note."""
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

    async def test_partial_placeholder_echo_is_dropped(self):
        """A *partially* echoed placeholder (template phrase + trailing filler)
        must also drop: detection is by the stable template phrase, not by a
        whole-string angle-bracket wrap, so ``<…> n/a`` no longer leaks the
        template into the debug egress."""
        decision = await _bid(
            client=_client(
                "should_post: no\nreason_code: only_agreeing\n"
                "reason_note: <one short clause on why — optional> n/a",
            ),
            mode=MODE_BID,
        )
        assert decision.reason_note is None

    async def test_legitimate_bracket_wrapped_note_is_kept(self):
        """The mirror guard: a genuine clause a model happens to wrap in angle
        brackets is **not** the placeholder, so it survives — the old
        bracket-only heuristic wrongly dropped it."""
        decision = await _bid(
            client=_client(
                "should_post: no\nreason_code: only_agreeing\n"
                "reason_note: <iron-fox already covered the trade-off>",
            ),
            mode=MODE_BID,
        )
        assert decision.reason_note == "<iron-fox already covered the trade-off>"

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


class TestSilenceReasonVetoesSpeak:
    """TB2 parity with the scalar ``speak: no`` one-way veto: under reasoning a
    silence-side ``reason_code`` vetoes a ``should_post: yes`` *toward silence*.
    A model that says "post" yet justifies it with an explicit no-substance code
    has not given a clean speak verdict; bias-to-silence resolves the
    contradiction to silence — and the silence code is preserved (not laundered
    into ``adds_substance``) so the contradiction stays visible downstream."""

    @pytest.mark.parametrize(
        "code",
        [REASON_ALREADY_ANSWERED, REASON_ONLY_AGREEING, REASON_NOTHING_TO_ADD],
    )
    async def test_yes_with_silence_reason_code_is_vetoed(self, code: str):
        decision = await _bid(
            client=_client(f"should_post: yes\nreason_code: {code}"),
            mode=MODE_BID,
        )
        assert decision.speak is False
        # The silence code survives — a dashboard/audit sees *why*, not a
        # speak-side ``adds_substance`` that hides the model's self-contradiction.
        assert decision.reason == code
        assert decision.score is None

    async def test_yes_with_garbage_reason_code_still_speaks(self):
        """Only an *explicit* silence code vetoes. An off-enum/garbage code on a
        yes is not a contradiction — it defaults to ``adds_substance`` and speaks,
        so a typo'd label never silences a genuine contribution."""
        decision = await _bid(
            client=_client("should_post: yes\nreason_code: i_just_feel_like_it"),
            mode=MODE_BID,
        )
        assert decision.speak is True
        assert decision.reason == REASON_ADDS_SUBSTANCE


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
