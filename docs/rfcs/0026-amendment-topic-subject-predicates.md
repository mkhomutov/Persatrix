# RFC 0026 Amendment — Topic-Subject Predicate Vocabulary

**Type**: amendment to [RFC 0026](0026-declarative-facts-tier.md) §B (predicate vocabulary), the extractor prompt, and fact-recall seeding
**Status**: ✅ Implemented — v0.3.12, [RFC 0049](0049-memory-consolidation-gradient.md) Phase 1 PR 1 ([0049-pr-plan.md](0049-pr-plan.md)). Stub authored 2026-07-19 (v0.3.12 review-prep decision item 4); expanded to this implementation amendment 2026-07-27.
**Author**: Maksim Khomutov
**Date**: 2026-07-19 (stub) / 2026-07-27 (implementation)
**Target**: v0.3.12 — lands with [RFC 0049](0049-memory-consolidation-gradient.md) Phase 1 (the L2 widening is vacuous without it — see Context)
**Authoritative model**: [RFC 0049 — Memory Consolidation Gradient](0049-memory-consolidation-gradient.md) (topic facts are L2) + [RFC 0037](0037-memory-confidentiality-channel-classification.md) §C (protection stamping)
**Related**: [RFC 0031 amendment — fact scope by consolidation level](0031-amendment-fact-scope-by-consolidation-level.md) (the *scope* half; this amendment is the *capture* half)

---

## Context

RFC 0049's headline scenario ("Atlas ships Friday", told in a DM, known in
the standup) has **no capture path today**. The v0.3.12 review verified the
gap is a three-legged conjunction
([`fact_predicates.py`](../../agents/memory/fact_predicates.py),
[`fact_extractor.py`](../../agents/persona_runtime/fact_extractor.py)):

1. **No predicate.** The frozen allowlist is person-centric
   (attribute / preference / commitment / relationship / `self.*`); no
   verb faithfully represents a topic fact, and `validate_predicate`
   rejects unknown verbs at the storage boundary.
2. **No extraction.** The extractor prompt restricts subjects to
   self/counterparty — a topic subject is never proposed.
3. **No recall.** Fact-recall seeds only self + sender — a topic fact, if
   stored, would never be retrieved. (RFC 0026 §D anticipated
   `(sender, *mentioned_entities)` seeding; the mentioned-entity half was
   never built.)

Widening L2 recall (RFC 0049 Phase 1) over a tier containing zero topic
facts delivers nothing. This amendment is the capture workstream that
makes the scenario reachable.

## The change (as implemented)

1. **`topic.*` predicate namespace** — mirrors the shipped `self.*` dotted
   convention (the dot separates subject-namespaces for a future predicate
   registry). Seed set, exported as `TOPIC_PREDICATES` and unioned into
   `PREDICATE_ALLOWLIST`:

   | Predicate | Example object |
   |---|---|
   | `topic.has_status` | `blocked on review` |
   | `topic.has_deadline` | `friday` |
   | `topic.decided` | `adopt the monorepo` |
   | `topic.owned_by` | `bob` |

   A **guarded, closed** addition — adding a verb remains a deliberate
   amendment + PR, exactly as the allowlist's own comment requires. The
   `topic.` prefix grants no free pass (`topic.is_root` rejects); a drift
   pin holds `TOPIC_PREDICATES` equal to the dotted `topic.` slice of the
   combined allowlist, because the recall-seeding SQL enumerates exactly
   that frozenset as an IN-list (closed set ⇒ equality filter, no `LIKE`).

2. **Extractor prompt** — `fact-extractor-suffix.md` gains one instruction:
   topic tuples use the canonical short name of the project / artifact /
   initiative as the subject (a few words, never a sentence or a quote),
   and every `object` stays a single short phrase. The `{predicate_list}`
   placeholder re-renders the widened vocabulary automatically; the byte
   pins in `test_extractor.py` / `test_prompt_loader.py` re-pin the new
   text.

3. **Recall seeding** — new [`topic_seeds.py`](../../agents/persona_runtime/topic_seeds.py)
   + `FactStore.topic_subjects()` ([`_facts_topics.py`](../../agents/memory/_facts_topics.py)):
   the store enumerates its **distinct live topic subjects**
   (most-recently-asserted first, bounded), and the persona seeds
   `FactStore.recall` for each subject the inbound stimulus **mentions**
   (word-boundary match on the canonical fold). Deterministic by
   construction — no LLM in the read path. Topic seeds join *behind* the
   person seeds (`self` first, sender second — the Leg-5 admit-priority
   invariant is untouched) and behind the sender-less short-circuit, so
   TICK events still issue zero DB round-trips.

4. **Scope discipline (PR 1 vs PR 2)** — `topic_subjects()` applies the
   SAME agent / session-§D-default / principal / epoch filters as
   `FactStore.recall`. This amendment widens *capture* only; the
   cross-room L2 *scope* widening is RFC 0049 PR 2 and stays behind the
   RFC 0037 gate. Until PR 2, a topic taught in another room does not
   seed — the recall wall is not bypassed pre-gate.

5. **Protection stamping** — unchanged: a topic fact is stamped from its
   source interaction per RFC 0037 §C (`store_extracted_facts` stamps the
   batch uniformly), and crosses rooms per the RFC 0031 fact-scope
   amendment, gated at egress. A topic fact extracted from a `restricted`
   DM is stamped `restricted`; pinned by
   `test_topic_seeds.py::TestTopicFactStampingInheritance`.

## Security — the allowlist blast-radius re-review (the named gate)

The frozen allowlist is a prompt-injection control (a hostile channel
cannot mint arbitrary predicates). Expanding it — and especially widening
extractor *subjects* to free-text topic names — re-opens that analysis.
The re-review ran with the implementation (2026-07-27) and is recorded
here; the bounds it pinned shipped in the same PR, all enforced at the
storage boundary (`FactStore.store`) so every write path — extractor,
operator-seeded, fixture — is covered:

* **Predicate surface: unchanged in kind.** The vocabulary stays a
  closed frozenset; `topic.*` adds four verbs, not a namespace wildcard.
  An adversarial verb still rejects at the storage boundary and feeds the
  existing rejected-predicate discovery log.
* **Subject canonicalization limits.** Free-text topic subjects are the
  new surface: an induced tuple's subject is stored and later re-rendered
  into prompts via the `Known facts about <subject>:` header.
  `canonicalize_subject` now enforces `MAX_SUBJECT_CHARS = 120`
  **post-normalization** (padding cannot dodge the bound) on every
  subject — person and topic share one column and are indistinguishable
  at canonicalization time. The read side fails closed: an over-bound
  query subject raises and the existing defensive branches drop the seed.
* **Object length bounds.** New `validate_object`: `MAX_OBJECT_CHARS =
  400`, empty/whitespace rejected. The prompt-side cost was already
  budget-bounded (RFC 0017 admission); this bounds the at-rest payload of
  a single induced tuple and the rejected-tuple log surface. The
  extractor's per-tuple try-block absorbs a rejection as one
  `agent.facts.extraction_failed` count without dropping the batch.
* **RFC 0009 delimiter escape.** Fact lines render OUTSIDE the
  `<external_data>` quarantine envelope, so a stored subject/object
  embedding `</external_data>` (or an opening tag) could forge an
  envelope boundary for adjacent wrapped content. Both fields now reject
  any value matching `</?external_data` (case-insensitive) at the
  storage boundary.
* **Seeding surface.** The read path adds **no** LLM step: matching is a
  bounded string scan (`TOPIC_SUBJECT_SCAN_LIMIT = 200` recent subjects,
  ≤ `TOPIC_SEED_LIMIT = 3` seeds/event, word-boundary so `atlas` does
  not fire inside `atlases`). A hostile stimulus can at most trigger
  recall of facts the store already holds for this agent in this scope —
  and every recalled row still passes the RFC 0037 §D injection gate.
* **Considered and deferred: subject–predicate namespace pairing.** A
  tuple like `("self", "topic.decided", …)` is semantically odd but
  harmless; the shipped `self.*` class has the same property (prompt
  instructs the pairing, storage does not enforce it). Enforcing pairing
  is a predicate-registry concern (the future registry the dotted
  convention reserves), not a blast-radius hole — no new injection power
  derives from a mismatched pairing.

## Test strategy (as landed)

`test_fact_predicates.py` (vocabulary + bounds + drift pin),
`test_topic_seeds.py` (store query scoping/determinism, pure matching,
seed wiring incl. the preserved TICK short-circuit, §C stamping
inheritance), byte re-pins for the prompt. The two RFC 0044 goldens
(`EVAL-MEMORY-001`/`EVAL-WORKING-001`) were re-recorded offline — the
widened `{predicate_list}` shifts every close-path request hash — and
replay green, preserving the dementia-continuity tripwire.
