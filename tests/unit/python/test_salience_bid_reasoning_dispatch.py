"""RFC 0051 Phase 1a (v0.3.10) — structured verdict **dispatch**, dark.

The prompt/budget/mode-dispatch half of the RFC 0051 structured-verdict tests;
the grammar/verdict half (parse, reason codes, fail-closed, veto) lives in the
sibling ``test_salience_bid_reasoning.py``. Split so each file stays under the
500-line review cap — the same discipline that split ``salience_deliberation``
out of ``salience_bid``. Shared scaffold (``_bid`` / ``_client`` / the mock
``fast`` alias map + transcript) lives in ``tests/_salience_reasoning_helpers``.

Covers: the ``mode``-scaled output-token budget wired through to the wire call,
the structured-vs-scalar prompt swap, NL-addressing reduced to an advisory prose
nudge under reasoning, and the loud-but-deduped unknown-``mode`` fallback.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from _salience_reasoning_helpers import _FAST_ALIAS_MAP, _TRANSCRIPT, _bid, _client

from agents.llm_client import LLMClient, LLMResponse
from agents.model_aliases import use_alias_map
from agents.salience_bid import evaluate_salience
from agents.salience_deliberation import (
    MODE_BID,
    MODE_OFF,
    MODE_PLAN,
    REASON_ADDS_SUBSTANCE,
    REASON_ONLY_AGREEING,
    _parse_failure_mode_label,
    is_structured,
    max_output_tokens_for,
)


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

    async def test_structured_plan_requests_the_plan_budget(self):
        """``mode: plan`` carries the larger plan ceiling end-to-end (320, not the
        bid 128) — the forward-headroom budget is wired through
        ``evaluate_salience``, not just the pure ``max_output_tokens_for``
        helper. Pins the previously-untested plan rung of the dispatch."""
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
                mode=MODE_PLAN,
            )
        kwargs = provider.create_message.await_args.kwargs
        assert kwargs["max_tokens"] == max_output_tokens_for(MODE_PLAN)

    def test_parse_failure_mode_label_is_bounded(self):
        """``parse_verdict`` is module-public, so the parse-failure counter's
        ``mode`` label must not echo a rogue caller's string (cardinality). The
        structured rungs pass through; anything else clamps to a bounded ``off``
        — the same closed-set discipline ``reason_code`` gets."""
        assert _parse_failure_mode_label(MODE_BID) == MODE_BID
        assert _parse_failure_mode_label(MODE_PLAN) == MODE_PLAN
        assert _parse_failure_mode_label(MODE_OFF) == MODE_OFF
        assert _parse_failure_mode_label("rogue-value") == MODE_OFF


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
    ``mode`` outright is a later PR (PR 4), so the bid logs the fallback now.

    The warning deduplicates on the value (one warning per distinct bad mode, so
    a config typo cannot spam the hot path), so each warn-asserting test clears
    the process-local cache first to stay independent of run order."""

    @pytest.fixture(autouse=True)
    def _reset_unknown_mode_warn_cache(self):
        from agents.salience_deliberation import (  # noqa: PLC0415
            _WARNED_UNKNOWN_MODES,
        )

        _WARNED_UNKNOWN_MODES.clear()
        yield
        _WARNED_UNKNOWN_MODES.clear()

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

    async def test_repeated_unknown_mode_warns_only_once(self, caplog):
        """The fix for the hot-path spam: a typo is a standing config fact, not a
        per-message one, so the warning fires **once per distinct bad value**, not
        on every governed admit. Without the dedup a single mistyped config line
        logged a WARNING for every open-floor message on every channel."""
        import logging  # noqa: PLC0415

        from agents.salience_deliberation import (  # noqa: PLC0415
            warn_if_unknown_mode,
        )

        with caplog.at_level(logging.WARNING):
            for _ in range(5):
                warn_if_unknown_mode("typo-mode", agent_id="ember-owl")
        assert caplog.text.count("unrecognised reasoning mode") == 1

    def test_known_mode_never_warns(self, caplog):
        """A recognised mode is silent — the dedup must not have made *known*
        modes warn (regression guard on the early-return ordering)."""
        import logging  # noqa: PLC0415

        from agents.salience_deliberation import (  # noqa: PLC0415
            warn_if_unknown_mode,
        )

        with caplog.at_level(logging.WARNING):
            for known in (MODE_OFF, MODE_BID, MODE_PLAN):
                warn_if_unknown_mode(known, agent_id="ember-owl")
        assert "unrecognised reasoning mode" not in caplog.text
