"""Token-aware memory budget allocator for persona memory injection.

Bounds memory injection at the event layer via a greedy token allocator.
Items that exceed the remaining budget are truncated to the remaining
tokens when the truncated form is at least ``min_tokens`` long; otherwise
they are dropped.

RFC 0017 §B — stable API surface for RFC 0008 scheduler-budget composition.

RFC 0026 PR 4 extends the allocator with a tier-aware admission registry
(:meth:`MemoryBudget.record_admission`,
:meth:`MemoryBudget.admissions_by_tier`).  Two consumers ride on it:

* :doc:`MT-MEMORY-005 dementia test
  <../../docs/manual-tests/MT-MEMORY-005-dementia-test>` leg-failure
  analysis (MQ-11) — operators flip ``PERSATRIX_MEMORY_PROVENANCE=1``
  to log per-turn ``(tier, item_id, tokens_admitted)`` records and
  disambiguate recall miss (item absent from the admitted slice) from
  reasoning miss (LLM had the row and ignored it).
* :meth:`agents.memory.facts.FactStore.mark_recalled` — the facts
  tier reads the admitted ``fact_id`` list off the budget to write
  ``last_recalled_at`` on every reinforced row.

The :meth:`try_add` signature is pinned (RFC 0017 §B / OQ4); the
registry is a separate call so the allocator's hot path stays
side-effect-free for callers that do not need provenance.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)
# Provenance records emit through the persona-runtime namespace so
# operators filtering structured logs by source can scope to a single
# dotted prefix.  Splitting the logger from ``__name__`` keeps the
# allocator's own DEBUG output separate from MT-MEMORY-005 diagnostics.
_provenance_logger = logging.getLogger("agents.persona_runtime.memory_budget.provenance")


def _provenance_enabled() -> bool:
    """Return ``True`` when the ``PERSATRIX_MEMORY_PROVENANCE`` env gate is set.

    Re-read on every admission so tests can flip the env-var without a
    process restart; the cost is one ``os.environ`` lookup per admitted
    row, well below the LLM hot path.  Accepts ``1`` / ``true`` / ``yes``
    (case-insensitive) for ergonomic parity with other RFC 0018-style
    debug gates.
    """
    raw = os.environ.get("PERSATRIX_MEMORY_PROVENANCE", "").strip().lower()
    return raw in {"1", "true", "yes"}

__all__ = [
    "MemoryBudget",
    "KNOWN_TIERS",
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


# RFC 0026 PR 4 / PR #342 review N-4 — frozen tier-name allowlist for
# :meth:`MemoryBudget.record_admission`.  Two reasons the validation
# lives here at module scope:
#
# * Single source of truth for the canonical tier vocabulary.  A typo
#   at a future call site (``tier="fact"`` instead of ``"facts"``)
#   would silently populate an unread bucket — the facts-tier
#   reinforcement read at
#   :meth:`agents.memory.facts.FactStore.mark_recalled` looks up
#   ``admissions_by_tier("facts")``, sees ``[]``, and skips the
#   ``last_recalled_at`` write without surfacing anywhere.
# * Tests can re-use the constant instead of duplicating string
#   literals.  Same pattern :data:`agents.memory.fact_predicates.
#   PREDICATE_ALLOWLIST` establishes for the storage layer.
#
# The membership covers all five canonical tier names appearing in
# the RFC 0027 §F priority order, even tiers that do not currently
# call ``record_admission`` (``relationship``, ``channel_history``) —
# future wiring lands on a known name rather than coining a new one
# in a follow-up PR.  Adding a tier is a deliberate amendment + test
# update at :class:`TestKnownTierAllowlist`.
KNOWN_TIERS: frozenset[str] = frozenset({
    "facts",
    "episodic",
    "notes",
    "relationship",
    "channel_history",
})


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


def _encode_text(text: str) -> tuple[int, list[int] | None]:
    """Encode *text* once, returning ``(token_count, token_ids)``.

    ``token_ids`` is the tiktoken ``cl100k_base`` token list when
    tiktoken is available, and ``None`` in the chars/4 fallback.

    DR2-N-6 (PR #342 second-pass review): :meth:`MemoryBudget.try_add`
    caches the result of this single encode and threads ``token_ids``
    into :func:`_truncate_to_token_limit`, so an oversized item's full
    text is tokenised once per admission rather than re-encoded by the
    counter and the truncator separately.

    ``max(0, …)`` (not ``max(1, …)``) so empty input returns 0,
    matching the tiktoken path (``enc.encode("") == []``).
    """
    try:
        import tiktoken  # type: ignore[import-untyped]

        enc = tiktoken.get_encoding("cl100k_base")
        tokens = enc.encode(text)
        return len(tokens), tokens
    except ImportError:
        logger.debug("tiktoken not available, using chars/4 token estimate")
    except Exception:
        logger.warning(
            "tiktoken encoding failed, falling back to chars/4", exc_info=True
        )
    return max(0, len(text) // 4), None


def _count_tokens(text: str) -> int:
    """Count tokens in *text* using tiktoken ``cl100k_base``, falling back to chars/4.

    Identical to the ``accurate=True`` path in :func:`~agents.memory.working.estimate_tokens`
    but kept local to avoid an import that would create a circular dependency
    once ``memory_context.py`` imports ``MemoryBudget`` in PR 2.

    Thin wrapper over :func:`_encode_text` — callers that only need the
    count (and not the token list) keep the original one-int signature.
    """
    return _encode_text(text)[0]


def _truncate_to_token_limit(
    text: str, token_limit: int, *, _token_ids: list[int] | None = None,
) -> str:
    """Truncate *text* to fit within *token_limit* tokens, including the ellipsis ``…``.

    Uses tiktoken for exact token-boundary truncation when available.  Falls
    back to char-proportional slicing when tiktoken is absent.  Never panics
    on missing tiktoken.

    The ellipsis character ``…`` (U+2026) counts toward the token budget.

    ``_token_ids`` (internal) — when the caller has already encoded
    *text* (:meth:`MemoryBudget.try_add` does, on the oversized path),
    it passes the token list here so the truncator decodes against it
    instead of re-encoding the full text.  ``None`` means "encode
    here", the behaviour every external caller and the chars/4
    fallback rely on.  (PR #342 second-pass review DR2-N-6.)
    """
    ellipsis = "…"

    if token_limit <= 0:
        return ellipsis

    try:
        import tiktoken  # type: ignore[import-untyped]

        enc = tiktoken.get_encoding("cl100k_base")
        tokens = enc.encode(text) if _token_ids is None else _token_ids
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
        # RFC 0026 PR 4 — per-tier admission registry.  Keyed by tier
        # name so callers can pull every admitted item_id for one tier
        # (e.g. the facts tier reads the ``"facts"`` list to drive
        # ``FactStore.mark_recalled``).  Insertion-ordered so admit
        # order survives round-tripping through ``admissions_by_tier``.
        self._admissions: dict[str, list[str]] = {}

    @property
    def remaining(self) -> int:
        """Tokens remaining in this budget."""
        return self._remaining

    # ─── Tier-provenance registry (RFC 0026 PR 4 / MQ-11) ──────────

    def record_admission(
        self, *, tier: str, item_id: str, tokens_admitted: int,
    ) -> None:
        """Register an admitted item against the per-turn provenance log.

        Callers invoke this immediately after a successful
        :meth:`try_add` so the admission is captured for two downstream
        uses:

        * :meth:`admissions_by_tier` is read by the facts tier to drive
          :meth:`agents.memory.facts.FactStore.mark_recalled` — the
          ``last_recalled_at`` reinforcement write.
        * When the ``PERSATRIX_MEMORY_PROVENANCE`` env gate is set, the
          same call emits a structured ``persatrix.memory.tier_admitted``
          log record for MT-MEMORY-005 leg-failure analysis (MQ-11).

        Registry shape (PR #342 third-pass review L-1)
        ----------------------------------------------
        Only ``item_id`` lands on the in-memory registry — keyed by
        ``tier``, the per-tier list stores bare ID strings so the facts
        tier's reinforcement read is a flat ``list[str]``.
        ``tokens_admitted`` is consumed by the structured-log emission
        only.  A future caller that needs the per-item token count off
        the registry will need to widen :attr:`_admissions` to
        ``dict[str, list[tuple[str, int]]]``; today no caller does, and
        the bare-string shape keeps :meth:`admissions_by_tier`'s
        contract narrow.

        The env gate scopes only the structured-log emission; the
        in-memory registry is populated unconditionally and
        synchronously, because the facts-tier reinforcement read
        does not depend on the env var.  The log emission alone is
        best-effort — a custom log-handler hiccup must never corrupt
        the registry the caller is about to read.

        Tier-name validation (PR #342 review N-4)
        -----------------------------------------
        ``tier`` is validated against :data:`KNOWN_TIERS` so a typo at
        the call site fails loudly with a :class:`ValueError` rather
        than silently populating an unread bucket.  The reader side
        (:meth:`admissions_by_tier`) stays permissive — a lookup on a
        typo returns the empty-default list, because the bug lives on
        the writer side (an orphaned bucket) not the reader side
        (a harmless empty read).
        """
        if tier not in KNOWN_TIERS:
            raise ValueError(
                f"tier={tier!r} is not a known tier; "
                f"expected one of {sorted(KNOWN_TIERS)}",
            )
        self._admissions.setdefault(tier, []).append(item_id)
        if _provenance_enabled():
            try:
                # ``event`` lifts the event identifier off the message
                # field so downstream structured-log pipelines (Loki,
                # ELK) can index on a stable key instead of grepping
                # the human-readable message.  Message stays populated
                # for terminal-tailing.  (PR #342 review N-7.)
                _provenance_logger.info(
                    "persatrix.memory.tier_admitted",
                    extra={
                        "event": "persatrix.memory.tier_admitted",
                        "tier": tier,
                        "item_id": item_id,
                        "tokens_admitted": tokens_admitted,
                    },
                )
            except Exception:  # noqa: BLE001 — best-effort observability path.
                logger.debug(
                    "tier-provenance log emission failed",
                    exc_info=True,
                )

    def admissions_by_tier(self, tier: str) -> list[str]:
        """Return the admitted ``item_id`` list for ``tier`` in admit-order.

        Returns an empty list for tiers that have not recorded any
        admission this turn.  Callers receive a shallow copy so a
        downstream mutation cannot corrupt the registry mid-turn.
        """
        return list(self._admissions.get(tier, ()))

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

        count, token_ids = _encode_text(text)
        if count <= self._remaining:
            self._remaining -= count
            return text

        # Item exceeds remaining budget; try a truncated form.  The
        # token list cached from the encode above is threaded into
        # _truncate_to_token_limit so the full text is tokenised once,
        # not re-encoded by the truncator (PR #342 second-pass review
        # DR2-N-6).  The short truncated string is still counted below
        # and the one-char ellipsis encoded inside the truncator —
        # both cheap; only the full-text re-encode is eliminated.
        truncated = _truncate_to_token_limit(
            text, self._remaining, _token_ids=token_ids,
        )
        truncated_count = _count_tokens(truncated)
        if truncated_count >= min_tokens:
            self._remaining -= truncated_count
            return truncated

        # Truncated form is smaller than min_tokens — drop entirely.
        return None
