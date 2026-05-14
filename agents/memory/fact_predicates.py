"""Declarative-fact predicate allowlist + subject canonicalization
(RFC 0026 PR 2).

PR 1 shipped :class:`agents.memory.facts.FactStore` with a callable
``predicate_validator`` injection seam and a permissive default; PR 2
swaps the default in for the enumerated allowlist pinned by RFC 0026
§B (attribute / preference / commitment / relationship) plus the
``self.*`` predicate class added by OQ #10.

The allowlist is the **prompt-injection blast-radius bound** for the
combined summarize + extract LLM call: a model that emits a malformed
or adversarial tuple cannot widen the predicate vocabulary, because
:meth:`FactStore.store` rejects unknown predicates at the storage
boundary.  The vocabulary lives here (not co-located with the LLM
prompt) so PR 3's recall path and PR 4's retraction policy can reach
the same constants without crossing a layer.

Subject canonicalization (RFC 0026 §C) folds whitespace + case so a
counterparty named ``"Bob"`` / ``"bob"`` / ``"  Bob  "`` collapses to
the same canonical row across writes.  The literal subject ``"self"``
is preserved verbatim per §C.4 — introspective ``self.*`` facts join
on the same ``subject`` column as user-facing ones.
"""

from __future__ import annotations

__all__ = [
    "PREDICATE_ALLOWLIST",
    "canonicalize_subject",
    "validate_predicate",
]


# RFC 0026 §B predicate vocabulary, frozen so a caller cannot widen the
# allowlist at runtime — the load-bearing guarantee for the
# §Security-Considerations prompt-injection blast-radius bound.
#
# Adding a verb is a deliberate RFC amendment + PR; the LLM extractor
# prompt enumerates these inline so the model's output and the
# storage-side check stay in sync.
PREDICATE_ALLOWLIST: frozenset[str] = frozenset({
    # Attribute (§B.1).
    "has_name",
    "lives_in",
    "works_at",
    "has_age",
    "speaks_language",
    # Preference (§B.2).
    "prefers",
    "dislikes",
    "loves",
    "avoids",
    # Commitment (§B.3).
    "committed_to",
    "plans_to",
    "agreed_to",
    # Relationship (§B.4).
    "has_daughter_named",
    "has_son_named",
    "has_partner_named",
    "has_parent_named",
    "works_with",
    "knows",
    # Self-introspection (OQ #10).  Same dotted-namespace convention
    # the RFC's example uses; the dot is significant — it separates
    # the "self" subject namespace from user-facing predicates so a
    # future predicate registry can treat the two as distinct vocabs.
    "self.has_preference",
    "self.holds_value",
    "self.committed_to",
    "self.has_attribute",
})


def validate_predicate(predicate: str) -> None:
    """Reject any predicate not in :data:`PREDICATE_ALLOWLIST`.

    Raises ``ValueError`` on:

    * empty / whitespace-only input (mirrors the PR 1 permissive
      validator's only constraint so the swap is monotone — anything
      that PR 1 rejected, PR 2 still rejects),
    * any predicate not in the allowlist (case-sensitive; the
      extractor downcases on its own before reaching this validator).

    Case sensitivity is deliberate: predicates are normalised
    snake_case per RFC §B.  A capitalised variant from the LLM means
    the prompt has drifted — surfacing
    ``agent.facts.extraction_failed`` is more useful than silently
    coercing.
    """
    if not predicate or not predicate.strip():
        raise ValueError("predicate must not be empty")
    if predicate not in PREDICATE_ALLOWLIST:
        raise ValueError(
            f"predicate {predicate!r} not in allowlist (RFC 0026 §B)",
        )


def canonicalize_subject(raw: str) -> str:
    """Fold case + whitespace into the canonical ``facts.subject`` form.

    Rules:

    * Empty / whitespace-only input raises ``ValueError`` (subject is
      a non-nullable column).
    * Outer whitespace stripped; internal whitespace runs collapsed to
      a single space so ``"Bob   Smith"`` and ``"Bob Smith"`` land on
      the same row.
    * Lowercased.
    * The literal ``"self"`` (any case, any surrounding whitespace)
      collapses to ``"self"`` so introspective facts join on a stable
      subject column (RFC 0026 §C.4).

    Idempotent — applying twice equals applying once.  Load-bearing
    for callers that defensively normalize at both write and read
    sites.
    """
    if not raw or not raw.strip():
        raise ValueError("subject must not be empty")
    normalised = " ".join(raw.strip().lower().split())
    return normalised
