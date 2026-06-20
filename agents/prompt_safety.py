"""Prompt-delimiter safety primitives.

A single, dependency-free home for the ``<|…|>`` delimiter-injection
escape so every consumer applies the *same* transformation rather than
re-deriving it:

* the persona prompt assembler wraps user-participant channel messages in
  ``<|user_message|>`` block delimiters
  (:mod:`agents.persona_runtime.prompt_assembly`, RFC 0034 §D / PR #120
  F-2), and
* the RFC 0036 ``recall_channel_messages`` tool
  (:mod:`agents.tools.recall`) escapes each recalled verbatim message so
  arbitrary historical text pulled on demand cannot inject the prompt
  (RFC 0036 §F).

This module imports nothing from :mod:`agents.persona_runtime` (or any
heavyweight runtime module), so the tool layer can reuse the escape
without coupling to the persona runtime — the same decoupling
:mod:`agents.channel_history_fetcher` documents.
"""

from __future__ import annotations

__all__ = ["escape_prompt_delimiters"]


def escape_prompt_delimiters(text: str) -> str:
    """Neutralise ``<|`` / ``|>`` block-delimiter sequences in ``text``.

    Backslash-prefixes each delimiter half (``"<|" → "\\<|"``,
    ``"|>" → "\\|>"``) so untrusted text embedded inside a
    ``<|user_message|>`` block cannot close the block early and inject
    text that appears to come from the system. Lone ``<``, ``>``, or ``|``
    characters are not delimiter sequences and pass through untouched —
    the escape is surgical, not a blanket sanitiser.
    """
    return text.replace("<|", "\\<|").replace("|>", "\\|>")
