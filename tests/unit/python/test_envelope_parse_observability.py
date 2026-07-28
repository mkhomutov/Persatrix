"""Envelope parse-failure observability (RFC 0026 PR 5b).

PR 2 review deferred — the catch in
:func:`agents.persona_runtime.summarize_close.summarize_closed_interaction`
collapses three distinct response shapes into one outcome
``(text, False, None)``:

1. **Plain prose** — desired backward-compat path (older mock clients,
   legacy LLM responses without the JSON envelope).
2. **Truncated JSON envelope** — a multi-fact response that overran
   the combined-call output-token ceiling and truncated mid-array,
   yielding invalid JSON.
3. **Valid JSON object missing the ``summary`` key** — parses as a
   mapping but :func:`split_combined_response` rejects it.

Paths (2) and (3) used to be indistinguishable from path (1) at the
caller — no counter, no log, and the raw broken JSON committed as the
episode summary.  This file pins two signals: a
``agent.facts.envelope_parse_failed`` counter with a ``reason``
attribute fires for paths (2) and (3) but **not** for path (1); and
(ISSUE-0054) paths (2) and (3) now resolve to a summary failure
(:data:`SUMMARY_UNAVAILABLE_TEXT`) instead of committing the broken
JSON to the episode summary column.

Why a *new* counter rather than re-using ``agent.facts.extraction_failed``:
envelope-parse failures lose the **entire** facts batch silently;
per-tuple failures (allowlist miss / shape errors) lose individual
rows and already increment ``extraction_failed``.  Splitting them
keeps the two failure shapes separable in dashboards — an operator
debugging "the persona stopped remembering anything overnight" can
distinguish "model started emitting truncated envelopes" (one
``envelope_parse_failed`` spike) from "model started emitting bad
tuples" (sustained ``extraction_failed`` rise).

The plan for PR 5b is in
[`docs/rfcs/0026-pr-plan.md` §"From PR 2 review"](../../../docs/rfcs/0026-pr-plan.md);
the option-selection rationale is captured there.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from agents.llm_client import LLMClient, LLMResponse, StopReason, Usage
from agents.memory.interactions import SUMMARY_UNAVAILABLE_TEXT, Interaction, Turn
from agents.persona_runtime.fact_extractor import (
    FactsParseError,
    split_combined_response,
)
from agents.persona_runtime.summarize_close import summarize_closed_interaction

# ─── Test helpers ──────────────────────────────────────────────


def _build_meter():
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader

    from agents.observability import metrics as metrics_mod

    reader = InMemoryMetricReader()
    metrics_mod.init_metrics(reader=reader)
    return reader, metrics_mod


def _envelope_parse_failed_points(
    reader: Any,
) -> list[tuple[int, dict[str, Any]]]:
    """Collect every data point for ``agent.facts.envelope_parse_failed``.

    Returns a list of ``(value, attributes_dict)`` so tests can assert
    both the count *and* the ``reason`` attribute partition.
    """
    data = reader.get_metrics_data()
    if data is None:
        return []
    out: list[tuple[int, dict[str, Any]]] = []
    for rm in data.resource_metrics:
        for sm in rm.scope_metrics:
            for m in sm.metrics:
                if m.name != "agent.facts.envelope_parse_failed":
                    continue
                for dp in getattr(m.data, "data_points", []):
                    out.append((int(dp.value), dict(dp.attributes)))
    return out


def _make_text_client(response_text: str) -> LLMClient:
    """Return an :class:`LLMClient` that yields ``response_text`` verbatim.

    The summariser's ``max_tokens`` cap is irrelevant here — the
    mock provider returns whatever we hand it, so tests can pin
    truncation, missing-key, and plain-prose shapes deterministically
    without driving the real model.
    """
    mock_provider = AsyncMock()

    async def _route(*, model, messages, system, tools, max_tokens, temperature):
        return LLMResponse(
            text=response_text,
            stop_reason=StopReason.END_TURN,
            usage=Usage(120, 30),
        )

    mock_provider.create_message = AsyncMock(side_effect=_route)
    mock_provider.format_tool_definitions = MagicMock(return_value=[])
    mock_provider.append_tool_round = MagicMock(
        side_effect=lambda msgs, resp, results: msgs,
    )
    return LLMClient(mock_provider)


def _multi_turn_interaction() -> Interaction:
    """Two-turn interaction so the LLM path runs.

    Single-turn interactions short-circuit to the deterministic
    placeholder summary in :func:`summarize_closed_interaction`; the
    envelope-parse branches we are pinning live downstream of the
    LLM call, so the fixture must drive multi-turn.
    """
    return Interaction(
        interaction_id="ix-envelope-test",
        scope="dm:test-agent:bob",
        started_at=0.0,
        closed_at=10.0,
        close_reason="structural",
        turns=[
            Turn(at=0.0, payload={"sender": "bob", "summary": "hi"}),
            Turn(at=5.0, payload={"sender": "test-agent", "summary": "hey"}),
        ],
    )


# ─── split_combined_response: reason attribute ────────────────


class TestSplitCombinedResponseReason:
    """``FactsParseError.reason`` is the structured signal the caller
    dispatches on.

    Three values fire the counter:

    * ``"truncated"`` — JSON-shaped text (starts with ``{``) that fails
      to parse.  Distinguishable from plain prose because plain prose
      does not open with a JSON brace.
    * ``"missing_summary"`` — parses as an object but lacks the
      load-bearing ``summary`` key (or the value is the wrong type).
    * ``"invalid_envelope"`` — parses as JSON but the top level is not
      an object (e.g., the model emitted a bare list).

    The fourth value — ``None`` — is the backward-compat path: text
    that does not look like a JSON envelope at all (plain prose from
    a mock client or a legacy LLM).  The caller skips the counter and
    commits the text as the summary.
    """

    def test_truncated_envelope_sets_reason_truncated(self) -> None:
        """Mid-array truncation under the combined-call output-token
        cap is the motivating case from the PR 2 review."""
        truncated = '{"summary": "Bob said hi.", "facts": [{"subject": "bob",'
        with pytest.raises(FactsParseError) as exc_info:
            split_combined_response(truncated)
        assert exc_info.value.reason == "truncated"

    def test_missing_summary_key_sets_reason_missing_summary(self) -> None:
        """Valid JSON object that omits the ``summary`` key is path (3)
        from the review."""
        raw = json.dumps({"facts": []})
        with pytest.raises(FactsParseError) as exc_info:
            split_combined_response(raw)
        assert exc_info.value.reason == "missing_summary"

    def test_summary_non_string_sets_reason_missing_summary(self) -> None:
        """Same observability bucket as missing key — the load-bearing
        prose half is absent in either shape, so operators see one
        signal."""
        raw = json.dumps({"summary": 42, "facts": []})
        with pytest.raises(FactsParseError) as exc_info:
            split_combined_response(raw)
        assert exc_info.value.reason == "missing_summary"

    def test_top_level_list_sets_reason_invalid_envelope(self) -> None:
        """The model emitted ``[...]`` instead of ``{...}``.  Distinct
        bucket so a regression in the prompt that nudges the model
        toward list-shaped responses surfaces separately."""
        with pytest.raises(FactsParseError) as exc_info:
            split_combined_response("[]")
        assert exc_info.value.reason == "invalid_envelope"

    def test_plain_prose_leaves_reason_none(self) -> None:
        """Backward-compat path: a string that does not look like a
        JSON envelope (does not start with ``{`` or ``[``) is plain
        prose from a legacy mock client.  ``reason=None`` tells the
        caller to skip the counter and commit the text as the summary."""
        with pytest.raises(FactsParseError) as exc_info:
            split_combined_response("Bob said hi.")
        assert exc_info.value.reason is None

    def test_empty_response_leaves_reason_none(self) -> None:
        """Production callers strip + early-return on empty text
        before reaching :func:`split_combined_response`; the helper's
        own empty branch is a defensive fence for direct callers.
        Leave ``reason=None`` so the empty-response signal stays on
        ``agent.interactions.summary.failed{reason=empty}`` (emitted
        upstream) without double-counting on the new counter."""
        with pytest.raises(FactsParseError) as exc_info:
            split_combined_response("")
        assert exc_info.value.reason is None

    def test_fenced_truncated_envelope_sets_reason_truncated(self) -> None:
        """ISSUE-0054 — once the markdown fence is unwrapped, a fenced
        response truncated mid-array is still recognised as an
        envelope-shaped failure (``reason=truncated``).  Before the
        unwrap the leading backtick made ``looks_like_envelope`` false,
        so the truncation was silently mis-routed to the plain-prose
        backward-compat path and never counted."""
        truncated = (
            '```json\n{"summary": "Bob said hi.", "facts": [{"subject": "bob",'
        )
        with pytest.raises(FactsParseError) as exc_info:
            split_combined_response(truncated)
        assert exc_info.value.reason == "truncated"


# ─── Caller-site counter emission ─────────────────────────────


@pytest.mark.asyncio
class TestCallerSiteCounter:
    """:func:`summarize_closed_interaction` is the catch site.

    Each test feeds the LLM mock a different response shape and asserts
    that ``agent.facts.envelope_parse_failed`` either does or does not
    increment, with the expected ``reason`` attribute.
    """

    async def test_truncated_envelope_fires_counter_with_truncated_reason(
        self,
    ):
        reader, metrics_mod = _build_meter()
        try:
            truncated = '{"summary": "Bob said hi.", "facts": [{"subject": "bob",'
            await summarize_closed_interaction(
                _make_text_client(truncated),
                "test-agent",
                _multi_turn_interaction(),
            )
            points = _envelope_parse_failed_points(reader)
            truncated_points = [
                (v, a) for v, a in points if a.get("reason") == "truncated"
            ]
            assert truncated_points, (
                f"truncated-envelope path must fire the counter with "
                f"reason=truncated; got: {points!r}"
            )
            assert sum(v for v, _ in truncated_points) == 1
        finally:
            await metrics_mod.shutdown()

    async def test_truncated_envelope_returns_summary_failure_not_raw_text(
        self,
    ):
        """ISSUE-0054 — a truncated (envelope-shaped) response must not
        commit its raw broken JSON as the episode summary.

        Pre-fix the caller returned ``(raw_text, False, None)``: the
        malformed, half-written JSON landed in ``episodes.summary`` and
        degraded episodic recall (the recall path indexes/ranks the
        garbled text).  An envelope the model *intended* but could not
        finish is a summary failure, not a usable summary — the result
        must be ``(SUMMARY_UNAVAILABLE_TEXT, True, None)`` so the
        janitor owns the row, exactly like the empty-``summary``-field
        path.  ``reason=None`` plain prose still keeps the
        backward-compat commit (see ``test_plain_prose_*``)."""
        reader, metrics_mod = _build_meter()
        try:
            truncated = (
                '{"summary": "Bob said hi.", "facts": [{"subject": "bob",'
            )
            result = await summarize_closed_interaction(
                _make_text_client(truncated),
                "test-agent",
                _multi_turn_interaction(),
            )
            assert result == (SUMMARY_UNAVAILABLE_TEXT, True, None, {}), (
                "a truncated envelope must resolve to a summary failure, "
                "not commit the raw broken JSON as the episode summary"
            )
        finally:
            await metrics_mod.shutdown()

    async def test_missing_summary_envelope_fires_counter_with_missing_reason(
        self,
    ):
        reader, metrics_mod = _build_meter()
        try:
            await summarize_closed_interaction(
                _make_text_client(json.dumps({"facts": []})),
                "test-agent",
                _multi_turn_interaction(),
            )
            points = _envelope_parse_failed_points(reader)
            missing_points = [
                (v, a) for v, a in points
                if a.get("reason") == "missing_summary"
            ]
            assert missing_points, (
                f"missing-summary-key path must fire the counter with "
                f"reason=missing_summary; got: {points!r}"
            )
            assert sum(v for v, _ in missing_points) == 1
        finally:
            await metrics_mod.shutdown()

    async def test_plain_prose_does_not_fire_counter(self):
        """Backward-compat path — older mock clients and legacy LLM
        responses without the envelope return plain prose; that is
        not a parse failure, it is the intentional pre-RFC-0026
        contract, and the counter must stay quiet so dashboards do
        not light up on green traffic."""
        reader, metrics_mod = _build_meter()
        try:
            result = await summarize_closed_interaction(
                _make_text_client("Bob said hi."),
                "test-agent",
                _multi_turn_interaction(),
            )
            # Backward-compat behaviour is unchanged: prose commits
            # as the summary; facts half is None.
            assert result == ("Bob said hi.", False, None, {})
            assert _envelope_parse_failed_points(reader) == [], (
                "plain-prose backward-compat path must NOT increment "
                "envelope_parse_failed; that bucket is for shapes the "
                "LLM intended as the JSON envelope"
            )
        finally:
            await metrics_mod.shutdown()

    async def test_empty_summary_field_does_not_fire_envelope_counter(self):
        """The empty-``summary``-field case is path-S2 from PR #340
        deep-review and is already signalled on
        ``agent.interactions.summary.failed{reason=empty_field}``.
        The new envelope counter must not double-fire on it — the
        check belongs at the caller after a successful split, not
        inside :func:`split_combined_response`."""
        reader, metrics_mod = _build_meter()
        try:
            result = await summarize_closed_interaction(
                _make_text_client(json.dumps({
                    "summary": "",
                    "facts": [],
                })),
                "test-agent",
                _multi_turn_interaction(),
            )
            # Pre-existing PR 2 fix: empty-field → fallback summary.
            assert result == (SUMMARY_UNAVAILABLE_TEXT, True, None, {})
            assert _envelope_parse_failed_points(reader) == [], (
                "empty-summary-field already increments "
                "interactions.summary.failed{reason=empty_field}; the "
                "envelope counter must not also fire"
            )
        finally:
            await metrics_mod.shutdown()

    async def test_well_formed_envelope_does_not_fire_counter(self):
        """Regression guard: a clean envelope on the green path must
        not trip the counter — only the four failure shapes do."""
        reader, metrics_mod = _build_meter()
        try:
            envelope = json.dumps({
                "summary": "Bob said hi.",
                "facts": [
                    {
                        "subject": "bob",
                        "predicate": "has_name",
                        "object": "Bob",
                    },
                ],
            })
            result = await summarize_closed_interaction(
                _make_text_client(envelope),
                "test-agent",
                _multi_turn_interaction(),
            )
            assert result[0] == "Bob said hi."
            assert result[1] is False
            assert _envelope_parse_failed_points(reader) == []
        finally:
            await metrics_mod.shutdown()

    async def test_fenced_envelope_extracts_facts_and_stays_quiet(self):
        """ISSUE-0054 headline regression — a well-formed envelope the
        model wrapped in a ```` ```json ```` fence.

        Pre-fix: :func:`split_combined_response` saw the leading
        backtick, failed ``json.loads``, raised ``FactsParseError``
        with ``reason=None``, and the caller committed the raw fenced
        blob as the summary and returned ``facts_raw=None`` — so
        :func:`finalize_closed_interaction` never dispatched the facts
        half and the ``facts`` table stayed empty.

        Post-fix: the fence is unwrapped, the caller returns the parsed
        prose summary plus the serialised facts list, and the envelope
        counter stays quiet (a fenced *well-formed* envelope is a green
        response, not a parse failure)."""
        reader, metrics_mod = _build_meter()
        try:
            envelope = (
                "```json\n"
                + json.dumps({
                    "summary": "Bob said hi.",
                    "facts": [
                        {
                            "subject": "bob",
                            "predicate": "has_name",
                            "object": "Bob",
                        },
                    ],
                })
                + "\n```"
            )
            summary, failed, facts_raw, _projections = await summarize_closed_interaction(
                _make_text_client(envelope),
                "test-agent",
                _multi_turn_interaction(),
            )
            assert summary == "Bob said hi."
            assert failed is False
            assert facts_raw is not None, (
                "fenced envelope must yield a non-None facts payload so "
                "finalize_closed_interaction dispatches the facts half"
            )
            assert json.loads(facts_raw) == [
                {"subject": "bob", "predicate": "has_name", "object": "Bob"},
            ]
            assert _envelope_parse_failed_points(reader) == []
        finally:
            await metrics_mod.shutdown()

    async def test_top_level_list_fires_counter_with_invalid_envelope_reason(
        self,
    ):
        """The model emitted a bare JSON list instead of the
        ``{summary, facts}`` envelope.  Distinct bucket so a prompt
        regression that drifts the model toward list-shaped responses
        surfaces separately from the truncation / missing-summary
        signals."""
        reader, metrics_mod = _build_meter()
        try:
            await summarize_closed_interaction(
                _make_text_client("[]"),
                "test-agent",
                _multi_turn_interaction(),
            )
            points = _envelope_parse_failed_points(reader)
            invalid_points = [
                (v, a) for v, a in points
                if a.get("reason") == "invalid_envelope"
            ]
            assert invalid_points, (
                f"top-level-non-object path must fire the counter with "
                f"reason=invalid_envelope; got: {points!r}"
            )
            assert sum(v for v, _ in invalid_points) == 1
        finally:
            await metrics_mod.shutdown()
