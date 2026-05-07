"""Token-aware memory budget allocator for persona memory injection.

Bounds memory injection at the event layer via a greedy token allocator.
Items that exceed the remaining budget are truncated to the remaining
tokens when the truncated form is at least ``min_tokens`` long; otherwise
they are dropped.

RFC 0017 §B — stable API surface for RFC 0008 scheduler-budget composition.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

__all__ = [
    "MemoryBudget",
    # Budget and per-tier constants consumed by memory_context.py.
    # Defined here because they govern MemoryBudget.try_add() call sites
    # — keeping them co-located makes tuning self-contained.
    "MEMORY_BUDGET_TOKENS",
    "MIN_TOKENS_RELATIONSHIP",
    "MIN_TOKENS_EPISODIC",
    "MIN_TOKENS_NOTES",
    "MIN_TOKENS_CHANNEL_HISTORY",
    "CHANNEL_RECALL_LIMIT",
    "REL_NOTES_INTERIM_CHARS",
    "MAX_EPISODE_SUMMARY_CHARS",
    "MAX_NOTE_CONTENT_CHARS",
]


# ─── Budget and per-tier token limits ─────────────────────

# Total token budget for all memory tiers injected per event.
# RFC 0017 §B / OQ1 resolution: 1500 tokens balances detail vs. prompt size.
# Retune by changing this single constant; no API changes required.
MEMORY_BUDGET_TOKENS: int = 1500

# Per-call min_tokens floors for the MemoryBudget allocator.
# Each tier specifies the minimum token count a truncated item must have
# to be admitted rather than dropped.  Relationship context uses a higher
# floor (64) because a partially-truncated header line without notes is
# nearly useless; notes use a lower floor (24) to allow even short snippets.
MIN_TOKENS_RELATIONSHIP: int = 64
MIN_TOKENS_EPISODIC: int = 32
MIN_TOKENS_NOTES: int = 24
# Channel-history tier (RFC 0011 PR 5 follow-up).  Same render shape as
# episodic recall ([recency-tag] summary), so the floor matches.
MIN_TOKENS_CHANNEL_HISTORY: int = 32

# Maximum number of channel-scoped episodes pulled per CHANNEL_MESSAGE
# event before the budget allocator decides which fit.  Default 20 per
# RFC 0011 §E and RFC 0021 §J.  Hardcoded for v0.3.0; the RFC describes
# this as ``optimization.yaml → channels.recall_limit``, but the
# persona-runtime does not load runtime tuning from YAML today (every
# other tier constant in this file is also hardcoded).  Promote to
# config when a runtime YAML loader exists.
CHANNEL_RECALL_LIMIT: int = 20

# Interim per-field cap on ``rel.notes`` (chars).  The pre-RFC-0017 code
# used 300 chars as an interim mitigation against prompt injection from
# peer-authored relationship notes.  After the allocate-loop rewrite the
# only remaining bound is the per-block budget, which is far larger than
# the original cap.  Restore a per-field bound here so the prompt-injection
# surface for ``rel.notes`` does not silently expand.
# 400 chars (~100 tokens) matches the original cap with mild headroom.
# (PR #146 review finding: prompt-injection surface regression.)
REL_NOTES_INTERIM_CHARS: int = 400

# Per-field char caps applied before the budget loop.  Prevents individual
# items from dominating the prompt even when the token budget is generous.
MAX_EPISODE_SUMMARY_CHARS: int = 200
MAX_NOTE_CONTENT_CHARS: int = 500


# ─── Internal token helpers ────────────────────────────────


def _count_tokens(text: str) -> int:
    """Count tokens in *text* using tiktoken ``cl100k_base``, falling back to chars/4.

    Identical to the ``accurate=True`` path in :func:`~agents.memory.working.estimate_tokens`
    but kept local to avoid an import that would create a circular dependency
    once ``memory_context.py`` imports ``MemoryBudget`` in PR 2.
    """
    try:
        import tiktoken  # type: ignore[import-untyped]

        enc = tiktoken.get_encoding("cl100k_base")
        return len(enc.encode(text))
    except ImportError:
        logger.debug("tiktoken not available, using chars/4 token estimate")
    except Exception:
        logger.warning(
            "tiktoken encoding failed, falling back to chars/4", exc_info=True
        )
    # ``max(0, …)`` (not ``max(1, …)``) so empty input returns 0, matching
    # the tiktoken path (``enc.encode("") == []``).  ``try_add`` short-
    # circuits on ``if not text:`` before reaching this helper, so no caller
    # currently observes the difference, but the contracts now agree.
    # (PR 6 — RFC 0017 PR 1 review finding 1.)
    return max(0, len(text) // 4)


def _truncate_to_token_limit(text: str, token_limit: int) -> str:
    """Truncate *text* to fit within *token_limit* tokens, including the ellipsis ``…``.

    Uses tiktoken for exact token-boundary truncation when available.  Falls
    back to char-proportional slicing when tiktoken is absent.  Never panics
    on missing tiktoken.

    The ellipsis character ``…`` (U+2026) counts toward the token budget.
    """
    ellipsis = "…"

    if token_limit <= 0:
        return ellipsis

    try:
        import tiktoken  # type: ignore[import-untyped]

        enc = tiktoken.get_encoding("cl100k_base")
        tokens = enc.encode(text)
        if len(tokens) <= token_limit:
            return text
        ellipsis_tokens = len(enc.encode(ellipsis))
        content_budget = token_limit - ellipsis_tokens
        if content_budget <= 0:
            return ellipsis
        truncated = enc.decode(tokens[:content_budget])
        return truncated + ellipsis
    except ImportError:
        pass
    except Exception:
        logger.warning(
            "tiktoken truncation failed, falling back to char-proportional",
            exc_info=True,
        )

    # Char-proportional fallback: approximate ``token_limit`` via chars/4.
    # The ellipsis counts as ~1 token → 4 chars.
    total_approx = len(text) // 4
    if total_approx <= token_limit:
        return text
    content_budget = token_limit - 1  # reserve 1 token for ellipsis
    if content_budget <= 0:
        return ellipsis
    return text[: content_budget * 4] + ellipsis


# ─── MemoryBudget ──────────────────────────────────────────


class MemoryBudget:
    """Token-aware greedy allocator for persona memory injection.

    Admits memory items one at a time, consuming tokens from a fixed total
    budget.  Items that exceed the remaining budget are truncated to the
    remaining tokens when the truncated form contains at least *min_tokens*
    tokens; otherwise they are dropped entirely.

    The admitted-token count for a single :meth:`try_add` call equals
    ``remaining_before - remaining_after``.  The per-event total is the sum
    over all calls in the allocation loop.

    RFC 0017 §B — stable API surface for RFC 0008 scheduler-budget
    composition.  The :meth:`try_add` signature is pinned; do not change it.

    Example::

        budget = MemoryBudget(total_tokens=1500)
        for formatted_item in items:
            admitted = budget.try_add(formatted_item)
            if admitted is not None:
                section_lines.append(admitted)
    """

    def __init__(self, total_tokens: int) -> None:
        self._remaining: int = max(0, total_tokens)

    @property
    def remaining(self) -> int:
        """Tokens remaining in this budget."""
        return self._remaining

    def try_add(self, text: str, *, min_tokens: int = 32) -> str | None:
        """Try to admit *text* into the budget.

        Returns the admitted string (original or truncated) on success,
        or ``None`` when the item is dropped.

        Behaviour:

        - Empty *text* → always dropped (returns ``None``).
        - Budget exhausted (``remaining <= 0``) → always dropped.
        - Item fits whole → admit; ``remaining`` decremented by exact token count.
        - Item exceeds remaining **and** truncated form ≥ *min_tokens* tokens
          → admit truncated; ``remaining`` decremented by truncated item's
          token count.
        - Otherwise → dropped; ``remaining`` unchanged.

        Args:
            text: Formatted text to admit.  May be truncated before admission.
            min_tokens: Minimum token count a truncated item must have to be
                admitted rather than dropped.  Each tier may specify its own
                floor via this kwarg (default 32).
        """
        if not text:
            return None
        if self._remaining <= 0:
            return None

        count = _count_tokens(text)
        if count <= self._remaining:
            self._remaining -= count
            return text

        # Item exceeds remaining budget; try a truncated form.
        # Note: enc.encode() is invoked 3× for oversized items — once in
        # _count_tokens(text) above, once inside _truncate_to_token_limit
        # (encode + decode), and once in _count_tokens(truncated) below.
        # Acceptable for typical memory-snippet sizes; profile here first
        # if allocation becomes a hotspot.
        truncated = _truncate_to_token_limit(text, self._remaining)
        truncated_count = _count_tokens(truncated)
        if truncated_count >= min_tokens:
            self._remaining -= truncated_count
            return truncated

        # Truncated form is smaller than min_tokens — drop entirely.
        return None
