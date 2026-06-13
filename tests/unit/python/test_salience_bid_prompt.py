"""RFC 0030 Tier B (v0.3.8) — bid *prompt construction* tests.

Split out of ``test_salience_bid.py`` (which pins the bid's bias-to-silence
*decision* contract) to keep both files under the 500-line code cap. These
pin the rendered prompt text: the ISSUE-0097 opening-round calibration, the
note-slot spacing (review finding #4), and the brace-safety of the
externalised template (untrusted inbound content is concatenated, never
``str.format``-substituted).
"""

from __future__ import annotations

import pytest

from agents.salience_addressing import NLAddressing
from agents.salience_bid import _build_bid_messages


class TestOpeningQuestionCalibration:
    """ISSUE-0097 (defect 1) — the opening-round bid prompt must name the case
    the "something genuinely new" novelty framing misses: an unanswered direct
    question put to the room is itself salient (answering it IS the new
    content), so the discussion does not stall before it exists. The unit test
    pins the *steer* in the prompt; the behavioural close is the live MT run."""

    def test_prompt_names_the_unanswered_direct_question_as_salient(self):
        """The clause is present and instructs a high score for an unanswered
        direct question to the room — even with no other novel point."""
        body = _build_bid_messages(
            content="Name exactly one risk each.",
            transcript=[],
            addressing=NLAddressing(False, False),
        )[0]["content"]
        assert "A direct question put to the room" in body
        assert "answering it IS the new content" in body
        assert "score it high" in body

    def test_prompt_preserves_bias_to_silence_for_the_redundant_case(self):
        """The calibration must not loosen the bias-to-silence posture: it
        only lifts the *unanswered* question, and still routes an
        already-answered one back to silence."""
        body = _build_bid_messages(
            content="Name exactly one risk each.",
            transcript=[],
            addressing=NLAddressing(False, False),
        )[0]["content"]
        assert "unless someone has already given that answer" in body
        assert "Bias toward staying silent." in body


class TestBidPromptShape:
    """Review finding #4 — with no addressing cue the bid prompt's note slot
    collapses to a single space (not a paragraph break), so it never forces a
    blank line between the instruction and the answer form; a present note
    rides its own paragraph."""

    def test_no_addressing_prompt_keeps_the_pre_pr3_spacing(self):
        body = _build_bid_messages(
            content="hi", transcript=[], addressing=NLAddressing(False, False),
        )[0]["content"]
        assert "Bias toward staying silent. Answer on exactly two lines:" in body
        assert "staying silent.\n\nAnswer" not in body

    def test_brace_in_inbound_content_does_not_break_rendering(self):
        """The instruction prose is externalised and rendered with
        ``str.format(note_tail=…)``; the untrusted inbound ``content`` (and the
        reconstructed transcript) must be *concatenated*, never formatted in,
        so a literal ``{`` / ``}`` in a user message cannot raise KeyError /
        IndexError nor be silently consumed."""
        body = _build_bid_messages(
            content="use {redis} for the {cache}?",
            transcript=[{"role": "user", "content": "what about {pg}?"}],
            addressing=NLAddressing(False, False),
        )[0]["content"]
        assert "use {redis} for the {cache}?" in body
        assert "what about {pg}?" in body

    @pytest.mark.parametrize("addressing,marker", [
        (NLAddressing(True, False), "invited by name"),
        (NLAddressing(False, True), "someone else appears to be invited"),
    ])
    def test_addressing_note_rides_its_own_paragraph(
        self, addressing: NLAddressing, marker: str,
    ):
        body = _build_bid_messages(
            content="hi", transcript=[], addressing=addressing,
        )[0]["content"]
        assert marker in body
        assert "staying silent.\n\nNote:" in body
        assert "\n\nAnswer on exactly two lines:" in body
