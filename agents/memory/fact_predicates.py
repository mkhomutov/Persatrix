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

import re

__all__ = [
    "MAX_OBJECT_CHARS",
    "MAX_SUBJECT_CHARS",
    "PREDICATE_ALLOWLIST",
    "TOPIC_PREDICATES",
    "canonicalize_subject",
    "validate_object",
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
    #
    # ``has_child_named`` is deliberately gender-neutral: the flat
    # ``(subject, predicate, object)`` schema cannot carry the gender
    # axis without leaking schema gap into the vocabulary (one verb per
    # relation × gender × generation grows unbounded as the family tree
    # widens).  When the gender of the relationship is the load-bearing
    # detail, it surfaces in the prose summary that ships in the same
    # close-path round-trip — the fact records the relationship + the
    # named entity, the summary records the framing.
    "has_child_named",
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


# Topic-subject class (RFC 0026 topic-predicate amendment — RFC 0049
# Phase 1).  Kept as its own frozenset because the recall-seeding SQL
# (:meth:`FactStore.topic_subjects`) enumerates exactly this subset;
# the drift pin in ``test_fact_predicates.py`` asserts it equals the
# ``topic.``-prefixed slice of the combined allowlist.  Same closed-set
# discipline: a new topic verb is a deliberate amendment + PR.
TOPIC_PREDICATES: frozenset[str] = frozenset({
    "topic.has_status",
    "topic.has_deadline",
    "topic.decided",
    "topic.owned_by",
})

PREDICATE_ALLOWLIST = PREDICATE_ALLOWLIST | TOPIC_PREDICATES


# ─── Blast-radius bounds (topic amendment §Security gate) ───
#
# Free-text topic subjects widen what an adversarial channel can induce
# the extractor to persist, so the amendment re-ran the allowlist
# blast-radius review and pinned three storage-boundary bounds:
#
# * MAX_SUBJECT_CHARS — a subject is a short canonical name, never a
#   sentence.  120 chars (post-normalization) leaves generous headroom
#   for person display names, participant ids, and multi-word topic
#   names while capping what a stored subject can re-inject via the
#   ``Known facts about <subject>:`` prompt header.
# * MAX_OBJECT_CHARS — an object is one short phrase.  400 chars caps
#   the stored payload of a single induced tuple (the prompt-side cost
#   is already budget-bounded; this bounds the at-rest surface and the
#   rejected-tuple discovery log).
# * _DELIMITER_ESCAPE_RE — no stored subject/object may open or close
#   an RFC 0009 ``<external_data>`` envelope when re-rendered into a
#   prompt (fact lines render OUTSIDE the quarantine envelope, so an
#   embedded closing tag could forge an envelope boundary for adjacent
#   wrapped content).
MAX_SUBJECT_CHARS: int = 120
MAX_OBJECT_CHARS: int = 400
_DELIMITER_ESCAPE_RE = re.compile(r"</?external_data", re.IGNORECASE)


def validate_predicate(predicate: str) -> None:
    """Reject any predicate not in :data:`PREDICATE_ALLOWLIST`.

    Raises ``ValueError`` on:

    * empty / whitespace-only input (mirrors the PR 1 permissive
      validator's only constraint so the swap is monotone — anything
      that PR 1 rejected, PR 2 still rejects),
    * any predicate not in the allowlist (case-sensitive — see
      "Case-sensitivity layering" below).

    Case-sensitivity layering (PR #340 review N4)
    ---------------------------------------------
    The allowlist is lowercase / snake_case per RFC §B and this
    validator is **strict** — ``"Has_Name"`` is rejected.  But the
    *production* extractor path
    (:func:`agents.persona_runtime.fact_extractor._normalise_predicate`)
    downcases its input before calling here, so a capitalised verb
    from the LLM normalises rather than rejecting — this is a
    deliberate choice to absorb routine LLM casing drift without
    bumping ``agent.facts.extraction_failed``.  The strict policy is
    not unreachable: it covers callers that bypass the extractor's
    normalisation (operator-seeded facts via direct
    :meth:`FactStore.store` calls, the future RFC 0013 erasure
    backfill, and any test fixture that constructs a fact tuple by
    hand).  Such callers see the rejection if they emit a mis-cased
    verb, which is the right surface — they own the canonicalisation
    discipline at the call site.
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
    * Unicode-aware case-folded via :py:meth:`str.casefold` (not
      ``str.lower``) so locale-specific characters collapse to a
      single canonical spelling — ``"Straße"`` and ``"Strasse"``
      both fold to ``"strasse"``.  ``.lower()`` would leave the
      eszett (``ß``) intact and silently split one counterparty
      across two ``facts.subject`` rows; PR 2 review pinned the
      switch to ``.casefold()`` before any non-ASCII subject reaches
      the dementia-test path.
    * The literal ``"self"`` (any case, any surrounding whitespace)
      collapses to ``"self"`` so introspective facts join on a stable
      subject column (RFC 0026 §C.4).

    Idempotent — applying twice equals applying once.  Load-bearing
    for callers that defensively normalize at both write and read
    sites; the casefold operation is idempotent on its own output
    (``"ss".casefold() == "ss"``) so the property survives the
    non-ASCII path too.
    """
    if not raw or not raw.strip():
        raise ValueError("subject must not be empty")
    normalised = " ".join(raw.strip().casefold().split())
    # Topic-amendment blast-radius bounds — applied post-normalization
    # so padding cannot dodge the length check, and applied to EVERY
    # subject (person and topic share one column; the write path cannot
    # tell them apart at canonicalization time).  The recall side fails
    # closed: an over-bound query subject raises here and the callers'
    # existing defensive branches drop the seed (``_subject_seeds``) or
    # skip the tier (``recall_facts_for_event``).
    if len(normalised) > MAX_SUBJECT_CHARS:
        raise ValueError(
            f"subject exceeds {MAX_SUBJECT_CHARS} chars "
            "(topic amendment blast-radius bound)",
        )
    if _DELIMITER_ESCAPE_RE.search(normalised):
        raise ValueError(
            "subject must not contain an external_data delimiter "
            "(RFC 0009 envelope escape)",
        )
    return normalised


def validate_object(value: str) -> None:
    """Reject an unsafe fact ``object`` at the storage boundary.

    Topic-amendment blast-radius bounds (see the constants block):
    empty / whitespace-only, over-``MAX_OBJECT_CHARS``, and any value
    carrying an RFC 0009 ``<external_data>`` delimiter all raise
    ``ValueError``.  Enforced by :meth:`FactStore.store` so every
    write path — extractor, operator-seeded, test fixture — is bound;
    the extractor's per-tuple try-block absorbs the rejection as one
    ``agent.facts.extraction_failed`` count without dropping the batch.
    """
    if not value or not value.strip():
        raise ValueError("object must not be empty")
    if len(value) > MAX_OBJECT_CHARS:
        raise ValueError(
            f"object exceeds {MAX_OBJECT_CHARS} chars "
            "(topic amendment blast-radius bound)",
        )
    if _DELIMITER_ESCAPE_RE.search(value):
        raise ValueError(
            "object must not contain an external_data delimiter "
            "(RFC 0009 envelope escape)",
        )
