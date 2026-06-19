"""RFC 0036 PR 4 — the shared ``<|…|>`` delimiter-escape primitive.

``escape_prompt_delimiters`` is the single source of truth for the
RFC 0034 §D / PR #120 F-2 delimiter-injection escape: the transformation
that neutralises the ``<|user_message|>`` block delimiters so untrusted
peer text cannot close the block early and impersonate a system
instruction.

It was extracted (behaviour-preserving) from the inline
``_format_event`` ``CHANNEL_MESSAGE`` branch in
:mod:`agents.persona_runtime.prompt_assembly` so a second consumer — the
RFC 0036 ``recall_channel_messages`` tool (:mod:`agents.tools.recall`),
which per-row-escapes recalled verbatim content — reuses the *same*
escape rather than re-deriving it, while staying free of any
``persona_runtime`` import (mirroring the decoupling
:mod:`agents.channel_history_fetcher` documents).
"""

from __future__ import annotations

import pytest

from agents.prompt_safety import escape_prompt_delimiters


class TestEscapePromptDelimiters:
    def test_open_delimiter_is_escaped(self):
        assert escape_prompt_delimiters("<|") == "\\<|"

    def test_close_delimiter_is_escaped(self):
        assert escape_prompt_delimiters("|>") == "\\|>"

    def test_user_message_literal_round_trips_inert(self):
        """A literal ``<|user_message|>`` in the input cannot survive as a
        live block delimiter — both halves are escaped."""
        escaped = escape_prompt_delimiters("<|user_message|>")
        assert escaped == "\\<|user_message\\|>"
        # The escaped form contains neither a live opening nor closing
        # delimiter sequence (every ``<|`` / ``|>`` is backslash-prefixed).
        assert "<|" not in escaped.replace("\\<|", "")
        assert "|>" not in escaped.replace("\\|>", "")

    def test_matches_format_event_inline_escape(self):
        """The extracted helper is byte-identical to the inline
        ``content.replace("<|", "\\<|").replace("|>", "\\|>")`` it
        replaced in ``prompt_assembly._format_event`` — the extraction is
        a refactor, not a behaviour change."""
        for raw in ("<|", "|>", "<|user_message|>", "a<|b|>c", "plain"):
            assert escape_prompt_delimiters(raw) == raw.replace(
                "<|", "\\<|"
            ).replace("|>", "\\|>")

    @pytest.mark.parametrize(
        "benign",
        ["", "plain text", "no delimiters here", "pipe | and angle < >"],
    )
    def test_text_without_delimiters_is_unchanged(self, benign):
        """Lone ``|``, ``<``, or ``>`` characters are not delimiter
        sequences and must pass through untouched — the escape is
        surgical, not a blanket sanitiser."""
        assert escape_prompt_delimiters(benign) == benign

    def test_multiple_occurrences_all_escaped(self):
        # "<|a|><|b|>": every "<|" and every "|>" is escaped independently.
        assert escape_prompt_delimiters("<|a|><|b|>") == "\\<|a\\|>\\<|b\\|>"
