"""Tests for :mod:`agents.memory.fact_predicates` (RFC 0026 PR 2).

PR 2 introduces two extractor-facing helpers in a sibling module to
:mod:`agents.memory.facts`:

* ``validate_predicate(predicate)`` — enumerated allowlist (≈25 verbs
  across attribute / preference / commitment / relationship + ``self.*``
  classes per RFC 0026 §B + OQ #10).  Raises ``ValueError`` on unknown
  verbs so :meth:`agents.memory.facts.FactStore.store` can count the
  rejection under ``agent.facts.extraction_failed`` (PR plan §PR 2).
* ``canonicalize_subject(raw)`` — folds case + whitespace so a counterparty
  named ``"Bob"`` / ``"bob"`` / ``"  Bob  "`` collapses to the same
  canonical row across writes (RFC 0026 §C).  The literal ``"self"``
  subject is preserved verbatim per §C.4.

The seam is co-located with :mod:`agents.memory.facts` (same package)
so PR 3's recall path and PR 4's retraction policy can reach the
allowlist without crossing a layer.
"""

from __future__ import annotations

import pytest

from agents.memory.fact_predicates import (
    PREDICATE_ALLOWLIST,
    canonicalize_subject,
    validate_predicate,
)


# ─── Predicate allowlist ────────────────────────────────────


class TestPredicateAllowlistShape:
    """Allowlist content — verbs grouped by RFC 0026 §B class."""

    @pytest.mark.parametrize(
        "predicate",
        ["has_name", "lives_in", "works_at", "has_age", "speaks_language"],
    )
    def test_attribute_class(self, predicate: str) -> None:
        assert predicate in PREDICATE_ALLOWLIST

    @pytest.mark.parametrize(
        "predicate", ["prefers", "dislikes", "loves", "avoids"],
    )
    def test_preference_class(self, predicate: str) -> None:
        assert predicate in PREDICATE_ALLOWLIST

    @pytest.mark.parametrize(
        "predicate", ["committed_to", "plans_to", "agreed_to"],
    )
    def test_commitment_class(self, predicate: str) -> None:
        assert predicate in PREDICATE_ALLOWLIST

    @pytest.mark.parametrize(
        "predicate",
        [
            "has_child_named",
            "has_partner_named",
            "has_parent_named",
            "works_with",
            "knows",
        ],
    )
    def test_relationship_class(self, predicate: str) -> None:
        assert predicate in PREDICATE_ALLOWLIST

    @pytest.mark.parametrize(
        "predicate", ["has_daughter_named", "has_son_named"],
    )
    def test_gendered_child_verbs_collapsed_to_has_child_named(
        self, predicate: str,
    ) -> None:
        """RFC 0026 PR 2 review decision — the gendered relationship
        verbs collapse into the single ``has_child_named`` predicate.

        Reason: the flat ``(subject, predicate, object)`` schema cannot
        carry the gender of the relationship without leaking schema gap
        into the vocabulary (5 → ∞ predicates as the family tree
        widens).  The salient fact for memory is the relationship + the
        named entity; gender, when load-bearing, lives in the prose
        summary that ships in the same close-path round-trip."""
        assert predicate not in PREDICATE_ALLOWLIST

    @pytest.mark.parametrize(
        "predicate",
        [
            "self.has_preference",
            "self.holds_value",
            "self.committed_to",
            "self.has_attribute",
        ],
    )
    def test_self_class_oq_10(self, predicate: str) -> None:
        """RFC 0026 OQ #10 — ``self.*`` predicates are first-class."""
        assert predicate in PREDICATE_ALLOWLIST

    def test_allowlist_is_immutable(self) -> None:
        """Frozen set so a caller cannot widen the vocabulary at runtime
        (the load-bearing guarantee for the prompt-injection blast-radius
        bound in RFC 0026 §Security)."""
        assert isinstance(PREDICATE_ALLOWLIST, frozenset)


class TestValidatePredicate:
    def test_accepts_allowlisted_verb(self) -> None:
        # No exception.
        validate_predicate("has_name")
        validate_predicate("self.has_preference")

    def test_rejects_unknown_verb(self) -> None:
        with pytest.raises(ValueError, match="not in allowlist"):
            validate_predicate("knows_a_secret_handshake")

    def test_rejects_empty(self) -> None:
        with pytest.raises(ValueError):
            validate_predicate("")

    def test_rejects_whitespace_only(self) -> None:
        with pytest.raises(ValueError):
            validate_predicate("   ")

    def test_case_sensitive(self) -> None:
        """Predicates are normalised lowercase / snake_case by RFC §B.

        A capitalised variant from the LLM is still rejected so the
        extractor surfaces ``facts.extraction_failed`` rather than
        silently coercing.  The extractor will downcase on its own
        (PR 2 implementation detail) before reaching the validator;
        the validator itself remains strict.
        """
        with pytest.raises(ValueError):
            validate_predicate("Has_Name")

    def test_self_namespace_rejects_unknown(self) -> None:
        """Unknown ``self.*`` verbs are still rejected — the prefix
        does not grant a free pass.  Prevents a prompt-injection
        attacker from manufacturing ``self.is_root`` etc."""
        with pytest.raises(ValueError, match="not in allowlist"):
            validate_predicate("self.is_root")


# ─── Subject canonicalization ───────────────────────────────


class TestCanonicalizeSubject:
    """RFC 0026 §C subject normalization.

    Counterparty resolution (sender-id reuse) is the extractor's job —
    this helper only normalizes a raw string into the canonical form
    that goes into the ``facts.subject`` column.
    """

    def test_lowercases(self) -> None:
        assert canonicalize_subject("Bob") == "bob"

    def test_trims_whitespace(self) -> None:
        assert canonicalize_subject("  Bob  ") == "bob"

    def test_uppercase_normalises(self) -> None:
        assert canonicalize_subject("BOB") == "bob"

    def test_collapses_internal_whitespace(self) -> None:
        """``"Bob   Smith"`` and ``"Bob Smith"`` collapse together so a
        future write under either spelling lands on the same row."""
        assert canonicalize_subject("Bob   Smith") == "bob smith"

    def test_preserves_self_literal(self) -> None:
        """RFC 0026 §C.4 — ``self`` is the literal subject for
        introspective facts.  It must round-trip unchanged so the
        ``self.*`` predicates have a stable subject column to join on."""
        assert canonicalize_subject("self") == "self"

    def test_self_with_surrounding_whitespace_normalises(self) -> None:
        assert canonicalize_subject("  self  ") == "self"

    def test_self_uppercase_normalises(self) -> None:
        assert canonicalize_subject("Self") == "self"

    def test_empty_raises(self) -> None:
        with pytest.raises(ValueError):
            canonicalize_subject("")

    def test_whitespace_only_raises(self) -> None:
        with pytest.raises(ValueError):
            canonicalize_subject("   ")

    def test_idempotent(self) -> None:
        """Applying canonicalize twice must equal applying it once.

        Load-bearing for write-then-read round-trips: callers that
        canonicalize on the way in and on the way out (defensive
        normalization at both ends) cannot drift."""
        once = canonicalize_subject("  Bob  ")
        twice = canonicalize_subject(once)
        assert once == twice

    def test_casefolds_non_ascii_eszett(self) -> None:
        """RFC 0026 PR 2 review — case folding uses Unicode-aware
        :py:meth:`str.casefold`, not ASCII-only ``str.lower``.  The
        German ``ß`` (eszett) folds to ``"ss"`` so a counterparty
        spelled ``"Straße"`` and another spelled ``"Strasse"`` land
        on the same canonical row — the whole point of subject
        normalization.

        Why this matters: ``.lower()`` is locale-insensitive but
        leaves ``ß`` unchanged, which silently splits one
        counterparty across two memory rows.  The dementia-test
        happy path uses ASCII names so this is latent for English
        workloads, but the seam should be Unicode-correct from the
        first international subject."""
        assert canonicalize_subject("Straße") == "strasse"

    def test_eszett_collapses_to_double_s_spelling(self) -> None:
        """The two spellings of the same name collapse to one row.

        This is the load-bearing assertion behind the casefold
        choice: a user writing ``"Straße"`` once and ``"STRASSE"``
        the next session must hit the same ``facts.subject`` key."""
        assert canonicalize_subject("Straße") == canonicalize_subject("STRASSE")

    def test_idempotent_on_non_ascii(self) -> None:
        """Idempotence holds across the casefold transformation.

        ``canonicalize_subject("Straße")`` returns ``"strasse"`` —
        re-running on that result must be a no-op so callers that
        defensively normalize at both write and read sites cannot
        drift even for non-ASCII subjects."""
        once = canonicalize_subject("Straße")
        twice = canonicalize_subject(once)
        assert once == twice


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
