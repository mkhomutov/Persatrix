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
    "validate_subject",
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
# blast-radius review and pinned these bounds.  They are enforced at
# the **write** boundary only (:meth:`FactStore.store` calls
# :func:`validate_subject` / :func:`validate_object`) — deliberately
# NOT inside :func:`canonicalize_subject`, which stays a pure
# normalizer.  Read paths canonicalize too (recall queries, seed
# derivation, the RFC 0013 erasure traversal), and a bound that raised
# there would turn a pre-amendment over-bound row into an unreadable —
# and unerasable — row, and would drop the persona's ``self`` seed on
# the way past.  Writes fail closed; reads stay total.
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
# * _CONTROL_CHAR_RE — a stored value may not carry a line break or
#   other control character.  Fact lines render as ``- subj pred obj``
#   inside a section whose per-subject ``Known facts about …:`` header
#   is the persona-inversion guard (facts_section §M-2); an embedded
#   newline lets one stored object forge a second, fabricated header
#   block (e.g. a ``self`` block) inside the tier's own framing.
#   Enforced on BOTH fields.  For subjects, ``canonicalize_subject``
#   already folds the header-forgery subset — every Unicode
#   *whitespace* control (LF, CR, NEL, LS/PS) collapses via
#   ``str.split()`` — but the non-whitespace controls (NUL, ESC,
#   backspace, DEL) survive the fold and would render verbatim in the
#   ``Known facts about <subject>:`` header and any operator
#   prompt-dump surface, so ``validate_subject`` rejects the class
#   too (PR #781 review M-1).
# * _DELIMITER_ESCAPE_RE — no stored subject/object may open or close
#   an RFC 0009 ``<external_data>`` envelope when re-rendered into a
#   prompt (fact lines render OUTSIDE the quarantine envelope, so an
#   embedded closing tag could forge an envelope boundary for adjacent
#   wrapped content).  Whitespace-tolerant on purpose, mirroring
#   ``agents.security._EXTERNAL_DATA_TAG_RE`` (PR #253 deep-review L1):
#   strict matching leaves a covert-bypass channel for any tokeniser
#   more permissive than ``re``, and the subject path makes that worse
#   — canonicalization folds ``<\t/external_data>`` into a
#   space-separated variant that a strict pattern would miss.  Defined
#   locally rather than imported so ``agents.memory`` keeps no
#   dependency on ``agents.security``; the tag-prefix shape is pinned
#   against the canonical pattern by a drift test.
MAX_SUBJECT_CHARS: int = 120
MAX_OBJECT_CHARS: int = 400
_CONTROL_CHAR_RE = re.compile("[\\x00-\\x1f\\x7f\\x85\\u2028\\u2029]")
_DELIMITER_ESCAPE_RE = re.compile(r"<\s*/?\s*external_data", re.IGNORECASE)


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
    return normalised


def validate_subject(subject: str) -> None:
    """Reject an unsafe fact ``subject`` at the **write** boundary.

    Topic-amendment blast-radius bounds (see the constants block):
    over-:data:`MAX_SUBJECT_CHARS`, carrying a control character, or
    carrying an RFC 0009 ``<external_data>`` delimiter.  Expects the
    canonical form — :meth:`FactStore.store` canonicalizes first, so
    the length check is post-normalization and padding cannot dodge
    it.  Canonicalization also collapses every Unicode *whitespace*
    control, so the control-character check can only fire on the
    non-whitespace class (NUL, ESC, backspace, DEL) — which survives
    ``str.split()`` and would otherwise ride verbatim into the
    ``Known facts about <subject>:`` header (PR #781 review M-1).

    Deliberately NOT called from :func:`canonicalize_subject`: read
    paths canonicalize too, and raising there would make a
    pre-amendment over-bound row unreadable (and unerasable via the
    RFC 0013 subject traversal) while also dropping the persona's
    ``self`` seed on the way past.  Writes fail closed; reads stay
    total, and no row that passes here can be written over the bound.
    """
    if len(subject) > MAX_SUBJECT_CHARS:
        raise ValueError(
            f"subject exceeds {MAX_SUBJECT_CHARS} chars "
            "(topic amendment blast-radius bound)",
        )
    if _CONTROL_CHAR_RE.search(subject):
        raise ValueError(
            "subject must not contain control characters "
            "(topic amendment blast-radius bound)",
        )
    if _DELIMITER_ESCAPE_RE.search(subject):
        raise ValueError(
            "subject must not contain an external_data delimiter "
            "(RFC 0009 envelope escape)",
        )


def validate_object(value: str) -> None:
    """Reject an unsafe fact ``object`` at the **write** boundary.

    Topic-amendment blast-radius bounds (see the constants block):
    empty / whitespace-only, over-:data:`MAX_OBJECT_CHARS`, any
    control character (a newline would let one stored object forge a
    second ``Known facts about …:`` header block inside the facts
    tier's own framing), and any RFC 0009 ``<external_data>``
    delimiter.  Enforced by :meth:`FactStore.store` so every write
    path — extractor, operator-seeded, test fixture — is bound; the
    extractor's per-tuple try-block absorbs a rejection as one
    ``agent.facts.extraction_failed`` count without dropping the batch.
    """
    if not value or not value.strip():
        raise ValueError("object must not be empty")
    if len(value) > MAX_OBJECT_CHARS:
        raise ValueError(
            f"object exceeds {MAX_OBJECT_CHARS} chars "
            "(topic amendment blast-radius bound)",
        )
    if _CONTROL_CHAR_RE.search(value):
        raise ValueError(
            "object must not contain control characters "
            "(fabricated-section-header escape)",
        )
    if _DELIMITER_ESCAPE_RE.search(value):
        raise ValueError(
            "object must not contain an external_data delimiter "
            "(RFC 0009 envelope escape)",
        )
