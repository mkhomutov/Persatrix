"""RFC 0037 §E projection PRODUCER — unit tests (v0.3.12 PR 6).

Three surfaces of the close-consolidation projection request:

* :func:`agents.persona_runtime.fact_envelope.extract_projections` — the
  lenient, never-raising parse of the envelope's ``projections`` half.
* :func:`agents.persona_runtime.summarize_close._projection_levels` +
  ``_build_summarization_prompt`` — projections are requested ONLY for a
  protected (``restricted``/``secret``) interaction, and an unprotected
  close keeps the exact pre-PR-6 prompt bytes (the pin that holds every
  landed RFC 0044 golden and mocked close fixture stable).
* :func:`agents.persona_runtime.summarize_close.summarize_closed_interaction`
  — the returned ``projections`` dict end to end through the mock LLM.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from agents.llm_client import LLMClient, LLMResponse, StopReason, Usage
from agents.memory.interaction_types import Interaction, Turn
from agents.memory.store_types import CompressedView
from agents.persona_runtime.fact_envelope import extract_projections
from agents.persona_runtime.fact_extractor import build_combined_prompt_suffix
from agents.persona_runtime.summarize_close import (
    _build_summarization_prompt,
    _projection_levels,
    summarize_closed_interaction,
)

_LEVELS = ("public", "internal")


class TestExtractProjections:
    """The §E honest boundary: every malformation degrades to ``{}``."""

    def test_valid_projections_filtered_to_requested_levels(self) -> None:
        raw = json.dumps({
            "summary": "s",
            "projections": {
                "public": "  a decision landed  ",
                "internal": "the sunset question settled",
                "restricted": "hallucinated level at the entry's own rank",
            },
        })
        assert extract_projections(raw, levels=_LEVELS) == {
            "public": "a decision landed",
            "internal": "the sunset question settled",
        }

    def test_fenced_envelope_unwraps(self) -> None:
        raw = (
            "```json\n"
            + json.dumps({"summary": "s", "projections": {"public": "p"}})
            + "\n```"
        )
        assert extract_projections(raw, levels=_LEVELS) == {"public": "p"}

    @pytest.mark.parametrize("raw", [
        "",
        "plain prose, no envelope",
        '{"summary": "s"}',
        '{"summary": "s", "projections": []}',
        '{"summary": "s", "projections": "not a mapping"}',
        '{"summary": "s", "projections"',
        "[1, 2, 3]",
    ])
    def test_malformed_or_absent_degrades_to_empty(self, raw: str) -> None:
        assert extract_projections(raw, levels=_LEVELS) == {}

    def test_non_string_and_blank_values_dropped(self) -> None:
        raw = json.dumps({
            "summary": "s",
            "projections": {"public": "", "internal": 42},
        })
        assert extract_projections(raw, levels=_LEVELS) == {}


def _interaction(classification: str | None) -> Interaction:
    return Interaction(
        interaction_id="ix-proj",
        scope="dm:test-agent:bob",
        started_at=0.0,
        closed_at=10.0,
        close_reason="structural",
        classification=classification,
        turns=[
            Turn(at=0.0, payload={"sender": "bob", "summary": "hi",
                                  "text": "the plan is REDWOLF"}),
            Turn(at=5.0, payload={"sender": "test-agent", "summary": "hey"}),
        ],
    )


class TestProjectionLevels:
    """The PR 6 scope decision: only a protected interaction requests."""

    @pytest.mark.parametrize("classification", [None, "public", "internal",
                                                "clasified"])
    def test_unprotected_requests_nothing(self, classification) -> None:
        assert _projection_levels(_interaction(classification)) == ()

    def test_restricted_requests_the_two_lower_levels(self) -> None:
        assert _projection_levels(_interaction("restricted")) == _LEVELS

    def test_secret_requests_the_three_lower_levels(self) -> None:
        assert _projection_levels(_interaction("secret")) == (
            "public", "internal", "restricted",
        )


class TestPromptShape:
    _VIEW = CompressedView(
        summary="compressed", entries_dropped=0,
        tokens_before=10, tokens_after=10,
    )

    def test_unprotected_prompt_is_byte_identical_to_pre_pr6(self) -> None:
        """The golden-stability pin: an ``internal``-default close ends
        with the RFC 0026 facts suffix and never mentions projections."""
        prompt = _build_summarization_prompt(_interaction("internal"), self._VIEW)
        assert prompt.endswith(build_combined_prompt_suffix())
        assert "projections" not in prompt

    def test_protected_prompt_appends_the_projection_suffix(self) -> None:
        prompt = _build_summarization_prompt(
            _interaction("restricted"), self._VIEW,
        )
        assert "`projections`" in prompt
        assert "`public`, `internal`" in prompt
        assert "classified `restricted`" in prompt
        # The §E ask sits AFTER the facts suffix — the summary and facts
        # halves of the prompt keep their exact prior bytes.
        assert build_combined_prompt_suffix() in prompt


def _envelope_client(envelope: dict) -> LLMClient:
    provider = AsyncMock()
    provider.create_message = AsyncMock(return_value=LLMResponse(
        text=json.dumps(envelope),
        stop_reason=StopReason.END_TURN,
        usage=Usage(120, 30),
    ))
    provider.format_tool_definitions = MagicMock(return_value=[])
    provider.append_tool_round = MagicMock(
        side_effect=lambda msgs, resp, results: msgs,
    )
    return LLMClient(provider)


@pytest.mark.asyncio
class TestSummarizeReturnsProjections:
    async def test_protected_interaction_returns_the_parsed_dict(self) -> None:
        envelope = {
            "summary": "They discussed the sunset plan.",
            "facts": [],
            "projections": {
                "public": "A roadmap decision was made.",
                "internal": "The sunset plan advanced.",
                "restricted": "must be dropped — at the entry's own level",
            },
        }
        summary, failed, facts_raw, projections = (
            await summarize_closed_interaction(
                _envelope_client(envelope), "test-agent",
                _interaction("restricted"),
            )
        )
        assert (summary, failed, facts_raw) == (
            "They discussed the sunset plan.", False, "[]",
        )
        assert projections == {
            "public": "A roadmap decision was made.",
            "internal": "The sunset plan advanced.",
        }

    async def test_unprotected_interaction_ignores_a_spurious_key(self) -> None:
        """Nothing was requested, so nothing is parsed — a model that
        volunteers projections on an ``internal`` close writes no rows."""
        envelope = {
            "summary": "s",
            "facts": [],
            "projections": {"public": "volunteered"},
        }
        *_, projections = await summarize_closed_interaction(
            _envelope_client(envelope), "test-agent", _interaction("internal"),
        )
        assert projections == {}

    async def test_protected_interaction_without_the_key_degrades(self) -> None:
        envelope = {"summary": "s", "facts": []}
        *_, projections = await summarize_closed_interaction(
            _envelope_client(envelope), "test-agent",
            _interaction("restricted"),
        )
        assert projections == {}
