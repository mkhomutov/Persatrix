"""Tests for :mod:`agents.persona_runtime.fact_extractor` (RFC 0026 PR 2).

PR 2 wires the declarative-fact extractor into the existing RFC 0020 PR 4
summarize-on-close LLM call.  One LLM round-trip emits both the
interaction summary (prose) and a JSON list of fact tuples; the helpers
here parse the JSON, apply the predicate allowlist + subject
canonicalization, and dispatch ``FactStore.store`` per tuple.

Contracts pinned by this file:

* :func:`parse_facts_payload` returns ``[]`` for the empty-list case
  (short interaction → zero tuples — the common path); raises
  :class:`FactsParseError` for malformed JSON / non-list payloads;
  silently coerces dict-of-dict shapes the model occasionally returns.
* :func:`store_extracted_facts` iterates the parsed list, calls
  :meth:`FactStore.store` per tuple, and increments
  ``agent.facts.extraction_failed`` on each rejected row (predicate
  allowlist miss / shape error) — one bad tuple does not drop the rest.
* The combined summarize + extract prompt is structured-output friendly:
  the model is told to return *exactly* one JSON document with two
  keys (``summary`` + ``facts``); :func:`split_combined_response`
  recovers each side and reports parse failures granularly so the
  caller can apply the RFC 0026 §Phase 1 step 4 "summary commits even
  when facts parsing fails" rollback policy.
"""

from __future__ import annotations

import json

import pytest
from _otel_test_helpers import counter_total

from agents.memory.fact_predicates import PREDICATE_ALLOWLIST
from agents.memory.facts import FactStore
from agents.persona_runtime.fact_extractor import (
    FactsParseError,
    build_combined_prompt_suffix,
    parse_facts_payload,
    split_combined_response,
    store_extracted_facts,
)

# ─── Fixtures ───────────────────────────────────────────────


@pytest.fixture
async def fact_store():
    store = FactStore(agent_id="test-agent", db_path=":memory:")
    await store.initialize()
    yield store
    await store.close()


def _build_meter():
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader

    from agents.observability import metrics as metrics_mod

    reader = InMemoryMetricReader()
    metrics_mod.init_metrics(reader=reader)
    return reader, metrics_mod


# ─── build_combined_prompt_suffix ───────────────────────────


class TestBuildCombinedPromptSuffix:
    """Pin the rendered output of the combined-prompt suffix.

    The prompt body lives at
    ``prompts/runtime/safety/fact-extractor-suffix.md`` (loaded via
    :func:`agents.prompt_loader.load_snippet`).  The markdown contains
    ``{{ }}`` brace escapes — ``.format()`` collapses them to literal
    single braces in the JSON-shape example — and a
    ``{predicate_list}`` placeholder which is substituted with the
    sorted vocabulary at call time.

    Pinning the rendered bytes here catches drift in either the
    markdown file OR the call-site ``.format()`` invocation that the
    raw byte-identity guard in ``test_prompt_loader.py`` would miss.
    """

    def test_renders_to_expected_bytes(self) -> None:
        predicate_list = ", ".join(sorted(PREDICATE_ALLOWLIST))
        expected = (
            "\n\n"
            "Reply with EXACTLY one JSON object — no prose outside it — "
            "with two top-level keys:\n"
            "  * `summary` (string): the prose summary described above.\n"
            "  * `facts` (list): zero or more declarative-fact tuples "
            "extracted from the interaction.  Each tuple is an object "
            '{"subject": str, "predicate": str, "object": str, '
            '"certainty": float in [0, 1]}.\n'
            "\n"
            "Return `\"facts\": []` when the interaction yields no "
            "extractable declarative facts (short turns, pleasantries, "
            "and tool-only exchanges typically yield nothing — this is "
            "the expected common case; do not invent tuples).\n"
            "\n"
            f"Valid predicates (use ONLY these verbs): {predicate_list}.\n"
            "Use `self` as the subject for introspective tuples about the "
            "agent itself (paired with a `self.*` predicate); use the "
            "counterparty's display name for tuples about them.\n"
        )
        assert build_combined_prompt_suffix() == expected

    def test_includes_every_allowlisted_predicate(self) -> None:
        """A new predicate added to :data:`PREDICATE_ALLOWLIST` must
        surface in the prompt automatically — otherwise the model
        cannot emit tuples using the new verb on the first turn after
        the constant is updated.
        """
        rendered = build_combined_prompt_suffix()
        for predicate in PREDICATE_ALLOWLIST:
            assert predicate in rendered, (
                f"predicate {predicate!r} missing from rendered prompt"
            )


# ─── parse_facts_payload ────────────────────────────────────


class TestParseFactsPayload:
    def test_empty_list_round_trips(self) -> None:
        """Short interactions return ``[]`` — the common case the
        prompt explicitly asks the model to emit when no extractable
        facts are present."""
        assert parse_facts_payload("[]") == []

    def test_well_formed_list(self) -> None:
        raw = json.dumps([
            {
                "subject": "bob",
                "predicate": "has_child_named",
                "object": "Mira",
                "certainty": 0.9,
            },
        ])
        result = parse_facts_payload(raw)
        assert len(result) == 1
        assert result[0]["subject"] == "bob"
        assert result[0]["predicate"] == "has_child_named"
        assert result[0]["object"] == "Mira"
        assert result[0]["certainty"] == 0.9

    def test_strips_surrounding_whitespace(self) -> None:
        """LLMs occasionally pad JSON output with trailing newlines."""
        raw = "  \n[]\n  "
        assert parse_facts_payload(raw) == []

    def test_malformed_json_raises_facts_parse_error(self) -> None:
        with pytest.raises(FactsParseError):
            parse_facts_payload("not-json")

    def test_non_list_payload_raises(self) -> None:
        """A scalar / mapping at the top level is not a list of tuples."""
        with pytest.raises(FactsParseError):
            parse_facts_payload('{"subject": "bob"}')

    def test_non_dict_element_raises(self) -> None:
        with pytest.raises(FactsParseError):
            parse_facts_payload('["bob"]')

    def test_empty_string_raises(self) -> None:
        with pytest.raises(FactsParseError):
            parse_facts_payload("")


# ─── split_combined_response ────────────────────────────────


class TestSplitCombinedResponse:
    """The combined summarize + extract prompt asks the model for a
    single JSON object ``{"summary": "...", "facts": [...]}``.

    The split helper extracts both halves and surfaces granular parse
    errors so the caller can commit the summary even when facts JSON
    is malformed (RFC 0026 §Phase 1 step 4 rollback policy).
    """

    def test_returns_summary_and_facts_text(self) -> None:
        raw = json.dumps({
            "summary": "Bob mentioned his daughter Mira.",
            "facts": [
                {
                    "subject": "bob",
                    "predicate": "has_child_named",
                    "object": "Mira",
                },
            ],
        })
        summary, facts_text = split_combined_response(raw)
        assert summary == "Bob mentioned his daughter Mira."
        # facts_text is a serialised JSON list so the downstream parser
        # is the single point of truth for fact-tuple validation.
        assert json.loads(facts_text) == [
            {
                "subject": "bob",
                "predicate": "has_child_named",
                "object": "Mira",
            },
        ]

    def test_missing_facts_key_defaults_empty(self) -> None:
        """``facts`` is optional — if the model omits the key (short
        interaction, nothing to extract) the helper treats it as ``[]``
        so the caller does not have to special-case missing keys."""
        raw = json.dumps({"summary": "Hello."})
        summary, facts_text = split_combined_response(raw)
        assert summary == "Hello."
        assert json.loads(facts_text) == []

    def test_missing_summary_raises(self) -> None:
        """Summary is the load-bearing output — its absence is an LLM
        failure, not a recoverable facts-parse failure."""
        with pytest.raises(FactsParseError):
            split_combined_response(json.dumps({"facts": []}))

    def test_top_level_non_object_raises(self) -> None:
        with pytest.raises(FactsParseError):
            split_combined_response("[]")

    def test_malformed_json_raises(self) -> None:
        with pytest.raises(FactsParseError):
            split_combined_response("oh no")

    def test_strips_json_code_fence(self) -> None:
        """ISSUE-0054 — the model wraps the envelope in a ```` ```json ````
        markdown fence even though the prompt asks for a bare object.
        An unstripped fence makes the whole envelope unparseable: the
        raw fenced blob commits as the summary and zero facts extract.
        The fence must be unwrapped before parsing."""
        raw = (
            "```json\n"
            + json.dumps({
                "summary": "Bob mentioned his daughter Mira.",
                "facts": [
                    {
                        "subject": "bob",
                        "predicate": "has_child_named",
                        "object": "Mira",
                    },
                ],
            })
            + "\n```"
        )
        summary, facts_text = split_combined_response(raw)
        assert summary == "Bob mentioned his daughter Mira."
        assert json.loads(facts_text) == [
            {
                "subject": "bob",
                "predicate": "has_child_named",
                "object": "Mira",
            },
        ]

    def test_strips_bare_code_fence(self) -> None:
        """A fence with no language tag (```` ``` ```` then a newline)
        is unwrapped the same way as a ```` ```json ```` fence."""
        raw = "```\n" + json.dumps({"summary": "Hello."}) + "\n```"
        summary, facts_text = split_combined_response(raw)
        assert summary == "Hello."
        assert json.loads(facts_text) == []

    def test_strips_fence_with_trailing_whitespace(self) -> None:
        """LLMs occasionally pad the closing fence with trailing
        newlines / spaces — the unwrap tolerates it."""
        raw = (
            "```json\n"
            + json.dumps({"summary": "Padded."})
            + "\n```\n  \n"
        )
        summary, facts_text = split_combined_response(raw)
        assert summary == "Padded."
        assert json.loads(facts_text) == []


# ─── store_extracted_facts ──────────────────────────────────


@pytest.mark.asyncio
class TestStoreExtractedFacts:
    async def test_empty_list_writes_nothing(self, fact_store: FactStore):
        n = await store_extracted_facts(
            fact_store,
            facts=[],
            source_interaction_id="ix-1",
            asserted_at=1000.0,
            session_id="legacy",
        )
        assert n == 0
        assert await fact_store.recall(subject="bob") == []

    async def test_stores_each_tuple(self, fact_store: FactStore):
        n = await store_extracted_facts(
            fact_store,
            facts=[
                {
                    "subject": "Bob",
                    "predicate": "has_child_named",
                    "object": "Mira",
                    "certainty": 0.9,
                },
                {
                    "subject": "bob",
                    "predicate": "lives_in",
                    "object": "Berlin",
                },
            ],
            source_interaction_id="ix-1",
            asserted_at=1000.0,
            session_id="run-a",
        )
        assert n == 2
        results = await fact_store.recall(subject="bob")
        # Subject was canonicalised — both rows live under the same key.
        assert len(results) == 2
        predicates = {f.predicate for f in results}
        assert predicates == {"has_child_named", "lives_in"}
        # Default certainty for the second tuple is 1.0 per FactStore.
        certainty_by_pred = {f.predicate: f.certainty for f in results}
        assert certainty_by_pred["has_child_named"] == pytest.approx(0.9)
        assert certainty_by_pred["lives_in"] == pytest.approx(1.0)
        # session_id threads through.
        assert all(f.session_id == "run-a" for f in results)

    async def test_unknown_predicate_increments_extraction_failed(
        self, fact_store: FactStore,
    ):
        """RFC 0026 §B + PR plan: an allowlist miss counts under
        ``agent.facts.extraction_failed`` and the row is skipped — one
        bad tuple does not drop the rest of the batch."""
        reader, metrics_mod = _build_meter()
        try:
            n = await store_extracted_facts(
                fact_store,
                facts=[
                    {
                        "subject": "bob",
                        "predicate": "manifests_unauthorised_powers",
                        "object": "telekinesis",
                    },
                    {
                        "subject": "bob",
                        "predicate": "has_name",
                        "object": "Bob",
                    },
                ],
                source_interaction_id="ix-1",
                asserted_at=1000.0,
                session_id="legacy",
            )
            assert n == 1, "bad tuple skipped; good tuple stored"
            assert counter_total(
                reader, "agent.facts.extraction_failed",
            ) == 1
        finally:
            await metrics_mod.shutdown()

    async def test_missing_required_field_increments_extraction_failed(
        self, fact_store: FactStore,
    ):
        """A fact dict that omits a required key is rejected the same
        way an unknown predicate is — the counter increment surfaces
        the failure to operators without breaking the batch."""
        reader, metrics_mod = _build_meter()
        try:
            n = await store_extracted_facts(
                fact_store,
                facts=[
                    {"subject": "bob", "predicate": "has_name"},  # no object
                    {
                        "subject": "bob",
                        "predicate": "has_name",
                        "object": "Bob",
                    },
                ],
                source_interaction_id="ix-1",
                asserted_at=1000.0,
                session_id="legacy",
            )
            assert n == 1
            assert counter_total(
                reader, "agent.facts.extraction_failed",
            ) == 1
        finally:
            await metrics_mod.shutdown()

    async def test_subject_canonicalised_so_supersede_chain_fires(
        self, fact_store: FactStore,
    ):
        """``"Bob"`` and ``"bob"`` collapse to one canonical row; a
        later write with the same ``(subject, predicate)`` supersedes
        the earlier one per RFC 0026 §F."""
        await store_extracted_facts(
            fact_store,
            facts=[
                {
                    "subject": "Bob",
                    "predicate": "prefers",
                    "object": "coffee",
                },
            ],
            source_interaction_id="ix-1",
            asserted_at=1000.0,
            session_id="legacy",
        )
        await store_extracted_facts(
            fact_store,
            facts=[
                {
                    "subject": "bob",
                    "predicate": "prefers",
                    "object": "tea",
                },
            ],
            source_interaction_id="ix-2",
            asserted_at=2000.0,
            session_id="legacy",
        )
        live = await fact_store.recall(subject="bob")
        assert len(live) == 1
        assert live[0].object == "tea"

    async def test_self_subject_round_trips(self, fact_store: FactStore):
        """RFC 0026 §C.4 + OQ #10 — ``self`` is the canonical subject
        for introspective facts; the ``self.*`` predicate class lands
        on the same row."""
        await store_extracted_facts(
            fact_store,
            facts=[
                {
                    "subject": "self",
                    "predicate": "self.has_preference",
                    "object": "morning walks",
                },
            ],
            source_interaction_id="ix-1",
            asserted_at=1000.0,
            session_id="legacy",
        )
        live = await fact_store.recall(subject="self")
        assert len(live) == 1
        assert live[0].predicate == "self.has_preference"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
