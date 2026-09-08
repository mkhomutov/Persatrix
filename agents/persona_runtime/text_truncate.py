"""Text truncation for the persona memory sections.

One helper, :func:`_truncate_with_ellipsis`, that shortens a piece of
recalled text so it fits the space a memory section is allowed, cutting
at a word (or token) boundary and marking the cut with an ellipsis.

Moved out of :mod:`agents.persona_runtime.memory_context` (v0.3.16
Workstream D, ISSUE-0143 PR D1) so the injector keeps headroom for the
audience gate and the allocate-loop can import the helper directly
instead of having it threaded through as a parameter.  ``memory_context``
still re-exports the name, so existing imports keep working.
"""

from __future__ import annotations

from typing import Literal

from .memory_budget import _truncate_to_token_limit

__all__ = ["_truncate_with_ellipsis"]


def _truncate_with_ellipsis(
    text: str,
    limit: int,
    *,
    mode: Literal["chars", "tokens"] = "chars",
) -> str:
    """Truncate *text* to *limit* with word-boundary or token-boundary awareness.

    If *text* fits within *limit* (measured in chars or tokens, depending on
    *mode*), it is returned unchanged.

    In ``"chars"`` mode (default): slices to *limit* chars, cuts at the
    last space so the LLM sees a complete word, and appends ``"..."``;
    a space-free slice is used whole (3-char worst-case overage).

    In ``"tokens"`` mode: truncates at a token boundary via tiktoken
    ``cl100k_base``, falling back to char-proportional slicing when
    tiktoken is absent (never panics).  The ellipsis ``"…"``
    counts toward the token budget.

    Extracted from _inject_memory_context() where the pattern was
    copy-pasted three times.  (PR #60 review.)
    """
    if mode == "tokens":
        # PR 1 review finding 4: ``_truncate_with_ellipsis_tokens`` was a
        # one-liner wrapper around ``_truncate_to_token_limit``.  Inlined
        # here to remove indirection now that the
        # ``memory_context → memory_budget`` import direction is known to
        # be safe (no cycle).
        return _truncate_to_token_limit(text, limit)

    # Original char mode (unchanged).
    if len(text) <= limit:
        return text
    sliced = text[:limit]
    truncated = sliced.rsplit(" ", 1)[0]
    # Zero-space guard: if the slice has no space, rsplit returns it
    # unchanged (len(truncated) == len(sliced)), so we use the full slice.
    if len(truncated) == len(sliced):
        truncated = sliced
    return truncated + "..."
