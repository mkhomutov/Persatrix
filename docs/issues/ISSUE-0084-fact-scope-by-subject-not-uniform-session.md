---
id: ISSUE-0084
summary: "RFC 0031 Phase 2 scopes the `facts` tier uniformly by session (default recall = active session + `legacy` carve-out). Per the scope-axes reframing, fact scope should follow the fact's *subject*: a fact whose subject is a **person/participant** (`Alice's daughter is Mira`) should be person-scoped and cross-room — knowledge about a person travels with the person, like relationship/trust does — while a fact whose subject is a **room/topic** (`this channel shipped Friday`) stays room-scoped. Uniform session-scoping means the persona trusts a person across rooms (relationship is cross-room) but cannot recall what it knows about them in another room — a one-level-down dementia symptom in the multi-room case. Refines, not reverts, the shipped Phase 2 default."
status: resolved
severity: low
area: agents/memory
created: 2026-05-30
closed: 2026-07-28
closed_pr: 785
refs:
  - docs/memory-scope-axes.md
  - docs/rfcs/0031-per-session-namespacing-channels.md
  - docs/rfcs/0026-declarative-facts-tier.md
  - agents/memory/facts.py
  - docs/manual-tests/MT-MEMORY-005-dementia-test.md
---

## Summary

RFC 0031 Phase 2 made default recall session-scoped across all four persona-memory tiers, including `facts` (active session + the always-visible `legacy` carve-out, [RFC §D](../rfcs/0031-per-session-namespacing-channels.md#d-recall-semantics)). The [scope-axes reframing](../memory-scope-axes.md) concludes that fact scope should **follow the fact's subject**, not be uniform:

- **Person-subject facts** (subject is a participant — `(Alice, has_child_named, "Mira")`) → **person-scoped, cross-room**. Knowledge about a person should travel with that person, exactly as relationship/trust already does (relationship keeps `session_id` out of its PK by design — [RFC §C amendment](../rfcs/0031-per-session-namespacing-channels.md#c-storage-model)).
- **Topic/room-subject facts** (`(group:planning, decided, "ship Friday")`) → **room-scoped**, as today.

## Context

This resolves a tension the reframing surfaced: the relationship tier is cross-room (you trust a *person*, not a person-in-a-room), but the facts that *justify* that trust are currently trapped in the room they were first stated in. The dementia test ([MT-MEMORY-005](../manual-tests/MT-MEMORY-005-dementia-test.md)) is authored single-room (one chat arc), so it passes today; the gap is the multi-room case — agent knows Alice across rooms (trust carries) but "forgets" her daughter's name when the venue changes.

Note the direction: uniform session-scoping is *conservative* (less sharing), so this is a **continuity limitation, not a confidentiality leak** — hence low severity and forward-looking, in contrast to the F-3 *over*-sharing leaks ([ISSUE-0079](ISSUE-0079-cross-session-supersede-not-scoped.md) / [ISSUE-0080](ISSUE-0080-relationship-recent-interactions-cross-session-leak.md)).

## Impact

- Multi-room persona continuity: a person-fact established in one room does not surface in another without explicit cross-room recall (`sessions=[…]`). Acceptable as an explicit opt-in; suboptimal as the default for *person* knowledge.
- No regression to the shipped single-room dementia-test path.
- Cross-room person-fact recall stays bounded by `(epoch, principal)` — it ranges over rooms, never across tenants or test worlds (see [ISSUE-0085](ISSUE-0085-epoch-axis-run-isolation.md)).

## Proposed fix / investigation path

1. **Subject classification** — the facts tier already canonicalizes a `subject` ([RFC 0026 §C](../rfcs/0026-declarative-facts-tier.md#c-subject-canonicalization)). Add a person-vs-topic classification (a participant-typed subject vs. a channel/topic subject) at write time, or derive it from the existing subject type.
2. **Scope by subject class** — person-subject facts: recall/write key on `(agent, subject)` cross-room (drop the session predicate, keep `epoch`/`principal`); topic-subject facts: keep the §D session predicate. The facts supersession chain ([ISSUE-0079](ISSUE-0079-cross-session-supersede-not-scoped.md) fix) must use the same subject-dependent scope so a person-fact retraction spans rooms while a topic-fact retraction does not.
3. **Notes tier** — apply the same subject rule to `notes` where a note is about a person (mixed today; [Memory Scope Axes §Where each tier rides](../memory-scope-axes.md#where-each-memory-tier-rides)).
4. **Tests** — multi-room person-fact recall (establish in room A, recall in room B by default); topic-fact stays room-scoped; extend the dementia test with a cross-room leg once this lands.

> Maintainer decision: whether this is a v0.3.x follow-up, a new RFC 0031 phase, or folds into the RFC 0026 facts-tier surface. It changes a v0.3.5-shipped default, so it wants its own PR + an MT update.

## Resolution (2026-07-28 — re-rooted, not implemented as filed)

Closed by the v0.3.12 RFC 0049 Phase 0–1 arc — **re-rooted per [RFC 0049 §D](../rfcs/0049-memory-consolidation-gradient.md#d-reconciliation-with-memory-scope-axesmd-and-the-one-decision-reopened)** (ratified 2026-06-06), which reopened exactly one memory-scope-axes decision: recall scope follows a fact's **consolidation level**, not its subject. The subject-classification machinery this issue proposed (steps 1–2: person-vs-topic classification at write time + subject-dependent scope predicates) was deliberately **never built**; the continuity symptom it named is fixed more generally:

- **All L2 facts — person *and* topic subjects — recall cross-room by default** ([0031 fact-scope amendment](../rfcs/0031-amendment-fact-scope-by-consolidation-level.md); shadow [#782](https://github.com/mkhomutov/Persatrix/pull/782), promoted live [#784](https://github.com/mkhomutov/Persatrix/pull/784)). The leakage concern that motivated room-scoping moved to egress: every cross-room candidate passes the [RFC 0037 §D classification gate](../rfcs/0037-memory-confidentiality-channel-classification.md#d-the-hard-gate-at-memory-injection).
- **Topic facts exist at all** via the `topic.*` capture path ([0026 amendment](../rfcs/0026-amendment-topic-subject-predicates.md), [#781](https://github.com/mkhomutov/Persatrix/pull/781)) — the filed dichotomy assumed a topic-fact tier the extractor could not yet populate.
- **The dementia-symptom fix generalizes past facts**: raw episodic recall is room-first *ranked* rather than walled ([0049-L1 amendment](../rfcs/0049-amendment-l1-cross-room-availability.md), [#783](https://github.com/mkhomutov/Persatrix/pull/783)/[#784](https://github.com/mkhomutov/Persatrix/pull/784)).
- **Notes** (step 3) stay room-scoped; person identity crosses via the RFC 0037 §C identity write-through at ≤ `internal`, with higher-classified turns routed to a gated room note.
- **Tests** (step 4): cross-room recall is pinned by `EVAL-MEMORY-002`/`003` + `tests/integration/test_cross_room_seed_replay.py` + `tests/unit/python/test_cross_room_live.py`; the multi-room dementia leg is [MT-MEMORY-CROSSROOM-001](../manual-tests/MT-MEMORY-CROSSROOM-001.md), and [MT-MEMORY-005 §V6](../manual-tests/MT-MEMORY-005-dementia-test.md#v6--post-rfc-0049-p01-cross-room-recall-live-v0312) re-anchors the no-bleed bar to the epoch axis.

Cross-room recall stays bounded by `(epoch, principal)` exactly as the Impact section required.
