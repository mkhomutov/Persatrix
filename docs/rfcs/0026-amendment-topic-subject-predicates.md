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
   `FactStore.recall`, and adds no scope of its own. Stated precisely,
   because the obvious phrasing would be wrong: the facts tier has
   never carried a *room* filter on either path (`source_channel_id` is
   provenance, not a predicate), so same-level cross-room fact
   visibility is pre-existing behaviour governed by the RFC 0037 §D
   egress gate — this amendment neither opens nor closes it. What it
   does not do is add the explicit L2 widening or its shadow-mode
   measurement plumbing; those are RFC 0049 PR 2.

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
* **The seed set is the real widening — topic seeds are predicate-scoped.**
  The review's sharpest finding, and it reverses this amendment's first
  draft (which dismissed subject–predicate pairing as cosmetic). Seeding
  makes the *subject* an attacker-reachable key: one induced
  `("bob", "topic.owned_by", "atlas")` tuple — phrasable as "the bob
  workstream is owned by atlas" — permanently turns the string `bob`
  into a recall seed, and `FactStore.recall` is not predicate-scoped, so
  **every** private fact about Bob would then load into any later turn
  merely mentioning him, from any sender. Pre-amendment those rows
  entered a turn only when Bob was the counterparty. Fix: a topic seed
  recalls only `TOPIC_PREDICATES` rows (`FactStore.recall(predicates=…)`);
  the person-seed path stays unscoped, which is correct — a counterparty's
  own turn should surface everything about them.
* **Subject canonicalization limits.** Free-text topic subjects are the
  next surface: an induced subject is stored and later re-rendered into
  prompts via the `Known facts about <subject>:` header. New
  `validate_subject` enforces `MAX_SUBJECT_CHARS = 120` on the canonical
  form (padding cannot dodge the bound) for every subject — person and
  topic share one column and are indistinguishable at write time.
  Enforced at the **write** boundary only, deliberately: read paths
  canonicalize too (recall queries, seed derivation, the RFC 0013
  erasure traversal), and a bound that raised there would make a
  pre-amendment over-bound row unreadable *and unerasable*, and would
  drop the persona's `self` seed on the way past. Writes fail closed;
  reads stay total.
* **Object length + control characters.** New `validate_object`:
  `MAX_OBJECT_CHARS = 400`, empty/whitespace rejected, and **no control
  characters**. The last is not cosmetic — fact lines render as
  `- subj pred obj` under a per-subject header, so an object carrying a
  newline forges a second, fabricated `Known facts about self:` block
  inside the tier's own framing: exactly the persona-inversion footgun
  the subject-templated header exists to prevent. Subjects are immune by
  construction (`canonicalize_subject` collapses all Unicode whitespace);
  this is the object-side twin. The extractor's per-tuple try-block
  absorbs a rejection as one `agent.facts.extraction_failed` count
  without dropping the batch.
* **RFC 0009 delimiter escape.** Fact lines render OUTSIDE the
  `<external_data>` quarantine envelope, so a stored subject/object
  embedding `</external_data>` (or an opening tag) could forge an
  envelope boundary for adjacent wrapped content. Both fields reject it
  at the storage boundary, **whitespace-tolerantly**
  (`<\s*/?\s*external_data`), mirroring `agents.security`'s canonical
  pattern: a strict pattern leaves a covert-bypass channel for any
  tokeniser more permissive than `re` (PR #253 deep-review L1), and the
  subject path makes that worse — canonicalization folds
  `<\t/external_data>` into a space-separated variant a strict pattern
  would miss. A drift test pins the local pattern against the canonical
  one.
* **Seeding surface.** The read path adds **no** LLM step: matching is a
  bounded string scan (`TOPIC_SUBJECT_SCAN_LIMIT = 200` recent subjects,
  ≤ `TOPIC_SEED_LIMIT = 3` seeds/event, word-boundary so `atlas` does
  not fire inside `atlases`). Two eligibility rules close a slot-stealing
  hole the review found: because the scan is most-recently-asserted-first
  and matching stops at the cap, whoever wrote the newest topic row gets
  first claim on the seeds — so a subject below `TOPIC_SEED_MIN_CHARS`
  or in the function-word set (`the`, `you`, …) never seeds, since one
  induced tuple named `the` would otherwise occupy every slot on every
  subsequent turn. With those in place a hostile stimulus can at most
  trigger recall of topic rows the store already holds for this agent in
  this scope — and every recalled row still passes the RFC 0037 §D
  injection gate (pinned end-to-end, not just structurally).
* **Named residual: subjects render unquarantined.** A stored subject
  reaches the prompt verbatim in its header, so a 120-char subject is
  120 chars of attacker-chosen text in the system prompt. This
  **pre-dates** the amendment (the extractor has always stored
  LLM-proposed person subjects) and is bounded by the length cap, the
  control-character and delimiter rejections, and the RFC 0009 framing
  that instructs the persona to treat memory as data. Recorded as a
  known residual rather than silently inherited — filed as
  [ISSUE-0116](../issues/ISSUE-0116-fact-subject-renders-unquarantined.md);
  a subject-side grammar belongs with the future predicate registry.

## Test strategy (as landed)

`test_fact_predicates.py` (vocabulary + bounds + drift pin),
`test_topic_seeds.py` (store query scoping/determinism, pure matching,
seed wiring incl. the preserved TICK short-circuit, §C stamping
inheritance), plus the facts leg of the §D gate end-to-end in
`tests/integration/test_confidentiality_gate.py` (a `restricted` topic
fact named from an `internal` room is withheld; the same turn at
`restricted` sees it — so the withhold is the gate acting, not the
seeding silently failing) and byte re-pins for the prompt. Both RFC
0044 goldens were re-recorded offline and replay green (only
`EVAL-MEMORY-001` actually shifted — the widened `{predicate_list}`
moves close-path request hashes, and it is the recipe with close-path
calls), preserving the dementia-continuity tripwire.
