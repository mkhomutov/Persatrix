"""Unit tests for ``agents.persona_runtime.summarize_close`` helpers.

Pins the PR 6 review follow-ups against the close-path helpers:

* :class:`TestMaybeRunJanitorCooldown` — review #29: two ``on_tick``
  calls within :data:`JANITOR_INTERVAL_SEC` run the cleanup once, not
  twice (the cooldown is exercised directly rather than via the
  persona event loop).
* :class:`TestJanitorFailedCounter` — review #24: a transient cleanup
  failure increments ``agent.interactions.janitor.failed`` so
  operators can SLO-alert on persistent janitor outages instead of
  silently accumulating stuck rows.
* :class:`TestEmptySummaryFieldFallsBack` — PR #340 deep-review S2:
  a well-formed JSON envelope with an empty ``summary`` field must
  fall back to :data:`SUMMARY_UNAVAILABLE_TEXT` and *not* return the
  serialised facts payload — committing ``""`` to
  :meth:`EpisodicMemory.update_episode_summary` (the pre-fix state)
  silently writes an empty summary and lets the facts dispatch fire
  against a missing prose half, violating the §G audit ordering "the
  summary always exists before any facts.store row pointing back at
  this ``interaction_id``."
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from _otel_test_helpers import counter_total

from agents.llm_client import LLMClient, LLMResponse, StopReason, Usage
from agents.memory.interactions import SUMMARY_UNAVAILABLE_TEXT, Interaction, Turn
from agents.persona_runtime.summarize_close import (
    JANITOR_INTERVAL_SEC,
    _interaction_to_entries,
    maybe_run_janitor,
    summarize_closed_interaction,
)


def _build_meter():
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader

    from agents.observability import metrics as metrics_mod

    reader = InMemoryMetricReader()
    metrics_mod.init_metrics(reader=reader)
    return reader, metrics_mod


@pytest.mark.asyncio
class TestMaybeRunJanitorCooldown:
    """PR 6 review #29 — cooldown semantics under :data:`JANITOR_INTERVAL_SEC`.

    Two calls inside the cooldown window must invoke the cleanup
    callable exactly once.  Pins the contract so a future refactor that
    drops the monotonic guard surfaces immediately.
    """

    async def test_two_calls_within_interval_runs_cleanup_once(self):
        calls = 0

        async def _cleanup() -> int:
            nonlocal calls
            calls += 1
            return 0

        last = await maybe_run_janitor(
            _cleanup, last_monotonic=None,
            now_monotonic=1000.0, interval_sec=JANITOR_INTERVAL_SEC,
            agent_id="a",
        )
        assert calls == 1

        # Second call inside the cooldown window — must NOT run.
        last = await maybe_run_janitor(
            _cleanup, last_monotonic=last,
            now_monotonic=1000.0 + JANITOR_INTERVAL_SEC / 2,
            interval_sec=JANITOR_INTERVAL_SEC, agent_id="a",
        )
        assert calls == 1, "cleanup must not re-run inside the cooldown window"

        # Third call past the cooldown window — must run.
        await maybe_run_janitor(
            _cleanup, last_monotonic=last,
            now_monotonic=1000.0 + JANITOR_INTERVAL_SEC + 1.0,
            interval_sec=JANITOR_INTERVAL_SEC, agent_id="a",
        )
        assert calls == 2


@pytest.mark.asyncio
class TestJanitorFailedCounter:
    """PR 6 review #24 — failed sweeps increment a dedicated counter.

    Without this, a persistent DB outage stalls the sweep silently for
    :data:`JANITOR_INTERVAL_SEC` per failure (5 min default) and stuck
    ``[summary pending]`` rows accumulate without any operator signal.
    """

    async def test_cleanup_exception_increments_failed_counter(self):
        reader, metrics_mod = _build_meter()
        try:
            async def _boom() -> int:
                raise RuntimeError("simulated DB hiccup")

            # The helper must swallow the failure (best-effort contract).
            new_last = await maybe_run_janitor(
                _boom, last_monotonic=None,
                now_monotonic=1000.0, interval_sec=JANITOR_INTERVAL_SEC,
                agent_id="janitor-test-agent",
            )
            assert new_last == 1000.0, (
                "cooldown must advance even on failure so the next call "
                "does not hammer a struggling DB"
            )
            assert counter_total(
                reader, "agent.interactions.janitor.failed",
            ) == 1
        finally:
            await metrics_mod.shutdown()

    async def test_successful_sweep_does_not_tick_failed_counter(self):
        reader, metrics_mod = _build_meter()
        try:
            async def _ok() -> int:
                return 0

            await maybe_run_janitor(
                _ok, last_monotonic=None,
                now_monotonic=1000.0, interval_sec=JANITOR_INTERVAL_SEC,
                agent_id="janitor-ok-agent",
            )
            assert counter_total(
                reader, "agent.interactions.janitor.failed",
            ) == 0
        finally:
            await metrics_mod.shutdown()


def _make_envelope_client(envelope_text: str) -> LLMClient:
    """Mock LLM that returns ``envelope_text`` verbatim on the summariser call.

    The summariser pins a ``max_tokens`` ceiling; the integration
    tests use that same gate to route real summariser traffic away
    from the persona event-loop path.  Here we only ever take the
    summariser branch (the unit test never invokes the persona loop)
    so the routing collapses to one return statement.
    """
    mock_provider = AsyncMock()

    async def _route(*, model, messages, system, tools, max_tokens, temperature):
        return LLMResponse(
            text=envelope_text,
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
    """Build a two-turn Interaction so :func:`summarize_closed_interaction`
    takes the LLM path (a single-turn interaction with no inbound
    message body short-circuits to the deterministic placeholder
    summary)."""
    return Interaction(
        interaction_id="ix-empty-summary",
        scope="dm:test-agent:bob",
        started_at=0.0,
        closed_at=10.0,
        close_reason="structural",
        turns=[
            Turn(at=0.0, payload={"sender": "bob", "summary": "hi"}),
            Turn(at=5.0, payload={"sender": "test-agent", "summary": "hey"}),
        ],
    )


@pytest.mark.asyncio
class TestEmptySummaryFieldFallsBack:
    """PR #340 deep-review S2 — a well-formed JSON envelope with an
    empty ``summary`` field must fall back to the placeholder, not
    commit the empty string and let facts dispatch fire.

    Why the fix lives at the :func:`summarize_closed_interaction`
    layer rather than in :func:`split_combined_response`: raising
    ``FactsParseError`` for empty summary would be caught by the
    caller's broad ``except FactsParseError: return (text, False, None)``
    backward-compat branch — which then commits the raw JSON envelope
    as the summary text.  That is worse than today's empty-string
    write.  The post-parse check at the caller treats an empty
    ``summary`` field the same way the helper already treats an
    empty LLM response (``_emit_summary_failed`` + return
    :data:`SUMMARY_UNAVAILABLE_TEXT`), and the distinct ``empty_field``
    reason lets operators disambiguate "model returned nothing" from
    "model returned valid JSON envelope with empty summary."
    """

    async def test_empty_string_summary_returns_unavailable_and_drops_facts(
        self,
    ):
        envelope = json.dumps({
            "summary": "",
            "facts": [
                {
                    "subject": "bob",
                    "predicate": "has_name",
                    "object": "Bob",
                },
            ],
        })
        result = await summarize_closed_interaction(
            _make_envelope_client(envelope),
            "test-agent",
            _multi_turn_interaction(),
        )
        # Pre-fix: result was ("", False, "[...facts...]") — the empty
        # string committed to update_episode_summary and facts_raw was
        # returned so dispatch_facts_from_response ran against an
        # empty prose half.  Post-fix: summary failed, facts not
        # dispatched.
        assert result == (SUMMARY_UNAVAILABLE_TEXT, True, None)

    async def test_whitespace_only_summary_returns_unavailable_and_drops_facts(
        self,
    ):
        """Whitespace-only is the same shape: the LLM emitted "summary"
        but it carries no prose.  :func:`split_combined_response`'s
        type check accepts it (a string is a string); the caller
        treats it as a summary failure."""
        envelope = json.dumps({
            "summary": "   \n  ",
            "facts": [
                {
                    "subject": "bob",
                    "predicate": "has_name",
                    "object": "Bob",
                },
            ],
        })
        result = await summarize_closed_interaction(
            _make_envelope_client(envelope),
            "test-agent",
            _multi_turn_interaction(),
        )
        assert result == (SUMMARY_UNAVAILABLE_TEXT, True, None)

    async def test_empty_summary_bumps_failed_counter_with_empty_field_reason(
        self,
    ):
        """The counter increment goes to ``agent.interactions.summary.failed``
        with a distinct ``reason`` attribute so operators can split
        the "model returned nothing" path from the "model returned
        valid envelope with empty summary" path in dashboards."""
        reader, metrics_mod = _build_meter()
        try:
            envelope = json.dumps({"summary": "", "facts": []})
            await summarize_closed_interaction(
                _make_envelope_client(envelope),
                "test-agent",
                _multi_turn_interaction(),
            )
            assert counter_total(
                reader, "agent.interactions.summary.failed",
            ) == 1
        finally:
            await metrics_mod.shutdown()

    async def test_non_empty_summary_returns_envelope_facts_unchanged(self):
        """Regression guard — the new empty-field branch must NOT
        catch a well-formed envelope with a non-empty summary.  This
        is the happy path the existing integration tests already
        cover; pinning it here too keeps the unit-level seam against
        a future refactor that broadens the empty-check predicate."""
        facts_payload = [
            {
                "subject": "bob",
                "predicate": "has_name",
                "object": "Bob",
            },
        ]
        envelope = json.dumps({
            "summary": "Bob said hi.",
            "facts": facts_payload,
        })
        summary, failed, facts_raw = await summarize_closed_interaction(
            _make_envelope_client(envelope),
            "test-agent",
            _multi_turn_interaction(),
        )
        assert summary == "Bob said hi."
        assert failed is False
        assert facts_raw is not None
        assert json.loads(facts_raw) == facts_payload


class TestInteractionToEntriesCarriesMessageText:
    """ISSUE-0054 root cause — :func:`_interaction_to_entries` must
    project the inbound message body (``payload["text"]``) into the
    entry content fed to the combined summarise + extract LLM call.

    Pre-fix the helper read only the deterministic action-envelope
    ``summary`` (``"Event: channel_message → Actions: [...]"``) and the
    ``sender`` annotation, so the extractor's LLM input never carried a
    real message — it correctly returned ``facts: []`` because the
    input it saw genuinely contained no extractable facts.  The body
    must reach the entry content or RFC 0026's facts tier stays inert.
    """

    def test_message_text_lands_in_entry_content(self) -> None:
        interaction = Interaction(
            interaction_id="ix-text",
            scope="dm:test-agent:bob",
            started_at=0.0,
            closed_at=10.0,
            close_reason="structural",
            turns=[
                Turn(at=0.0, payload={
                    "sender": "bob",
                    "summary": (
                        "Event: channel_message → Actions: ['do_nothing']"
                    ),
                    "text": "I'm picking up my daughter Mira from school",
                }),
                Turn(at=5.0, payload={
                    "sender": "bob",
                    "summary": (
                        "Event: channel_message → Actions: ['do_nothing']"
                    ),
                    "text": "She dislikes loud phone calls",
                }),
            ],
        )
        joined = " ".join(e.content for e in _interaction_to_entries(interaction))
        assert "daughter Mira" in joined
        assert "dislikes loud phone calls" in joined

    def test_missing_text_key_still_projects_envelope(self) -> None:
        """Backward-compat — a turn with no ``text`` key (single-turn
        rows, legacy payloads) still yields a non-empty entry from the
        action-envelope ``summary``."""
        interaction = Interaction(
            interaction_id="ix-no-text",
            scope="dm:test-agent:bob",
            started_at=0.0,
            closed_at=10.0,
            close_reason="structural",
            turns=[
                Turn(at=0.0, payload={
                    "sender": "bob",
                    "summary": (
                        "Event: channel_message → Actions: ['do_nothing']"
                    ),
                }),
            ],
        )
        entries = _interaction_to_entries(interaction)
        assert len(entries) == 1
        assert entries[0].content.strip()


def _single_turn_interaction(*, with_text: bool) -> Interaction:
    """Build a one-turn conversational Interaction.

    ``with_text=True`` mirrors a real one-message DM interaction that
    idle-closed before any follow-up: the turn carries the inbound
    message body on ``payload["text"]`` (stashed by
    ``_handle_multi_turn_event``).  ``with_text=False`` is a
    content-less turn — only the deterministic action envelope, no
    extractable message body.
    """
    payload: dict[str, object] = {
        "sender": "bob",
        "summary": "Event: channel_message → Actions: ['do_nothing']",
    }
    if with_text:
        payload["text"] = "I'm picking up my daughter Mira from school"
    return Interaction(
        interaction_id="ix-single-turn",
        scope="dm:test-agent:bob",
        started_at=0.0,
        closed_at=10.0,
        close_reason="idle_gap",
        turns=[Turn(at=0.0, payload=payload)],
    )


@pytest.mark.asyncio
class TestSingleTurnInteractionFactExtraction:
    """F-6 (v0.3.1 MT-MEMORY-005 re-run) — a single-turn conversational
    interaction that carries an inbound message body must be routed
    through the LLM summarise + extract path so a fact stated in a
    one-message interaction reaches the RFC 0026 facts tier.

    Pre-fix :func:`summarize_closed_interaction` short-circuited every
    ``turn_count == 1`` interaction onto a deterministic placeholder
    summary with ``facts=None`` — so a user who stated a fact in a
    single message that then idle-closed had that fact silently
    dropped (observed in the MT-MEMORY-005 re-run: the I3 commitment
    never reached the ``facts`` table).  A content-less single turn
    (no ``text``) still keeps the cheap placeholder — its extractor
    input would carry no message body, so an LLM call would only ever
    (correctly) extract nothing.
    """

    async def test_single_turn_with_message_text_extracts_facts(self):
        facts_payload = [{
            "subject": "bob",
            "predicate": "has_child_named",
            "object": "Mira",
        }]
        envelope = json.dumps({
            "summary": "Bob is picking up his daughter Mira from school.",
            "facts": facts_payload,
        })
        summary, failed, facts_raw = await summarize_closed_interaction(
            _make_envelope_client(envelope),
            "test-agent",
            _single_turn_interaction(with_text=True),
        )
        assert summary == "Bob is picking up his daughter Mira from school."
        assert failed is False
        assert facts_raw is not None, (
            "single-turn interaction with message text dropped its facts "
            "— F-6 regression"
        )
        assert json.loads(facts_raw) == facts_payload

    async def test_single_turn_without_message_text_keeps_placeholder(self):
        """Regression guard — a content-less single turn keeps the
        cheap deterministic placeholder and never reaches the LLM.
        The exact placeholder tuple proves the short-circuit fired:
        an LLM round-trip would have returned the envelope summary,
        not this string."""
        interaction = _single_turn_interaction(with_text=False)
        envelope = json.dumps({
            "summary": "this summary must never be used",
            "facts": [{"subject": "bob", "predicate": "x", "object": "y"}],
        })
        summary, failed, facts_raw = await summarize_closed_interaction(
            _make_envelope_client(envelope),
            "test-agent",
            interaction,
        )
        single = interaction.turns[0].payload["summary"]
        expected = (
            f"Multi-turn interaction (scope={interaction.scope}, "
            f"turns=1, reason={interaction.close_reason}): "
            f"first[{single}] last[{single}]"
        )
        assert (summary, failed, facts_raw) == (expected, False, None)
