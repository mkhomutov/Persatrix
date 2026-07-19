# RFC 0026 Amendment — Topic-Subject Predicate Vocabulary

**Type**: amendment to [RFC 0026](0026-declarative-facts-tier.md) §B (predicate vocabulary), the extractor prompt, and fact-recall seeding
**Status**: 📋 Proposed — **stub**. Capture-path workstream named by the v0.3.12 review-prep (decision item 4); to be expanded into a full implementation amendment when the v0.3.12 PR plan opens.
**Author**: Maksim Khomutov
**Date**: 2026-07-19
**Target**: v0.3.12 — lands with [RFC 0049](0049-memory-consolidation-gradient.md) Phase 1 (the L2 widening is vacuous without it — see Context)
**Authoritative model**: [RFC 0049 — Memory Consolidation Gradient](0049-memory-consolidation-gradient.md) (topic facts are L2) + [RFC 0037](0037-memory-confidentiality-channel-classification.md) §C (protection stamping)
**Related**: [RFC 0031 amendment — fact scope by consolidation level](0031-amendment-fact-scope-by-consolidation-level.md) (the *scope* half; this stub is the *capture* half)

---

## Context

RFC 0049's headline scenario ("Atlas ships Friday", told in a DM, known in
the standup) has **no capture path today**. The v0.3.12 review verified the
gap is a three-legged conjunction
([`fact_predicates.py`](../../agents/memory/fact_predicates.py) 41–80,
[`fact_extractor.py`](../../agents/persona_runtime/fact_extractor.py)):

1. **No predicate.** The frozen allowlist is person-centric
   (attribute / preference / commitment / relationship / `self.*`); no
   verb faithfully represents a topic fact, and `validate_predicate`
   rejects unknown verbs at the storage boundary.
2. **No extraction.** The extractor prompt restricts subjects to
   self/counterparty — a topic subject is never proposed.
3. **No recall.** Fact-recall seeds only self + sender — a topic fact, if
   stored, would never be retrieved.

Widening L2 recall (RFC 0049 Phase 1) over a tier containing zero topic
facts delivers nothing. This amendment is the capture workstream that
makes the scenario reachable.

## The change (to be expanded)

1. **`topic.*` predicate namespace** — mirror the shipped `self.*` dotted
   convention (the dot separates subject-namespaces for a future predicate
   registry): e.g. `topic.has_status`, `topic.has_deadline`,
   `topic.decided`, `topic.owned_by`. A **guarded, closed** addition to the
   allowlist — adding a verb remains a deliberate amendment + PR, exactly
   as the allowlist's own comment requires.
2. **Extractor prompt** — widened to propose salient topic subjects
   (canonicalized project/artifact names) alongside self/counterparty.
3. **Recall seeding** — widened to extract candidate topic subjects from
   the inbound stimulus so stored topic facts are retrievable.
4. **Protection stamping** — unchanged: a topic fact is stamped from its
   source interaction per RFC 0037 §C, and crosses rooms per the RFC 0031
   fact-scope amendment, gated at egress.

## Security note

The frozen allowlist is a prompt-injection control (a hostile channel
cannot mint arbitrary predicates). Expanding it — and especially widening
extractor *subjects* to free-text topic names — re-opens that analysis:
the expansion must re-run the allowlist blast-radius review (subject
canonicalization limits, object length bounds, the RFC 0009 delimiter
escape) before the extractor prompt ships. This is a named gate of the
implementation amendment, not a footnote.
