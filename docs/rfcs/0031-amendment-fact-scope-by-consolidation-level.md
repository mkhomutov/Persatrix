# RFC 0031 Amendment — Fact Scope Follows Consolidation Level, Not Subject

**Type**: amendment to [RFC 0031](0031-per-session-namespacing-channels.md) §C (Storage Model) + §D (Recall Semantics), and to [RFC 0026](0026-declarative-facts-tier.md) (facts-tier scope boundary)
**Status**: 📋 Proposed — **stub**. Decision ratified ([RFC 0049 §D](0049-memory-consolidation-gradient.md#d-reconciliation-with-memory-scope-axesmd-and-the-one-decision-reopened), 2026-06-06); this file records the binding change and is to be expanded into a full implementation amendment when the v0.3.12 PR plan opens.
**Author**: Maksim Khomutov
**Date**: 2026-06-06
**Target**: v0.3.12 — lands with [RFC 0049](0049-memory-consolidation-gradient.md) Phase 1 (this amendment *is* the Phase-1 L2 widening), **behind the RFC 0037 keystone** (see [Sequencing](#sequencing--dependencies)). *(Retargeted from v0.4.0 2026-07-20, following the 2026-07-15 pull-forward of RFC 0049 Phases 0–1; its capture-half companion is the [RFC 0026 topic-predicate amendment](0026-amendment-topic-subject-predicates.md).)*
**Authoritative model**: [RFC 0049 — Memory Consolidation Gradient & Scope Reconciliation](0049-memory-consolidation-gradient.md)
**Supersedes**: [memory-scope-axes.md](../memory-scope-axes.md) decision 4 ("fact scope follows subject"). Re-roots [ISSUE-0084](../issues/ISSUE-0084-fact-scope-by-subject-not-uniform-session.md).

---

## Context

[memory-scope-axes.md](../memory-scope-axes.md) decision 4 scoped declarative facts by their **subject**: a fact *about a person* is person-scoped (cross-room); a fact *about a topic/room* stays room-scoped. [ISSUE-0084](../issues/ISSUE-0084-fact-scope-by-subject-not-uniform-session.md) tracked the implementation of that subject classifier.

[RFC 0049](0049-memory-consolidation-gradient.md) re-roots this: scope follows a fact's **consolidation level**, not its subject. A consolidated, decontextualised fact (gradient level **L2 — semantic**) is **cross-room regardless of subject** — topic facts included. The reason topic facts were walled was *leakage* (a fact from a private room surfacing in a public one), and leakage is now [RFC 0037](0037-memory-confidentiality-channel-classification.md)'s job — a deterministic classification gate at *egress*, not a recall wall.

This is what makes a persona carry project knowledge like a colleague: "Atlas ships Friday" learned in a DM is recallable in the standup (the [scenario-2 failure](0049-memory-consolidation-gradient.md#worked-example-the-two-test-scenarios) RFC 0049 traces).

## Decision

1. **An L2 (semantic) fact is cross-room.** The RFC 0026 facts tier's default recall stops applying the `session_id` room filter to consolidated facts; the relationship/identity tier's existing cross-room-by-construction shape ([RFC 0031 identity amendment](0031-amendment-person-identity-cross-room-tier.md)) is the precedent, generalised from "person identity" to "any L2 fact".
2. **Subject classification is dropped.** No person-vs-topic classifier is built (ISSUE-0084's original mechanism is *not* implemented). Scope is intrinsic to the tier/rung, never a query-time discriminator — the same cure the identity amendment applied to the F-7 seam.
3. **Visibility is the RFC 0037 protection level**, inherited from the fact's source channel(s) at consolidation time. Cross-room never means cross-classification: a fact distilled from a `restricted` channel cannot be recalled into a lower-classified channel's prompt.
4. **Cross-room is still never cross-`epoch` or cross-`principal`.** Those PK axes are unchanged (memory-scope-axes.md decisions 5–6 stand).

## What changes

- **RFC 0031 §D (Recall Semantics)** — the blanket room-scoping default no longer applies to the L2 facts tier. Episodes (L1) are out of this amendment's scope; their room posture is owned by the [RFC 0049 L1 amendment](0049-amendment-l1-cross-room-availability.md) (cross-room *available* behind the 0037 gate, room-first-ranked — *updated 2026-07-20; originally "room-scoped default recall unchanged"*).
- **RFC 0031 §C (Storage Model) / RFC 0026** — facts carry provenance (source session(s)) as a tag, not a filter, plus an RFC 0037 protection level.
- **memory-scope-axes.md** — decision 4 already annotated superseded (PR #559).

## Sequencing / dependencies

- **Hard dependency: RFC 0037 lands first.** Widening fact recall to cross-room without the egress gate is a confidentiality regression. RFC 0037 is RFC 0049's Phase 0, v0.3.12.
- **Measurement gate.** Ship cross-room L2 recall in *shadow* (evaluated against [RFC 0044](0044-eval-set-golden-traces.md) golden traces) and promote to the live prompt only when it does not degrade quality under the [RFC 0017](0017-persona-memory-injection-budget.md) injection budget.
- Pairs with the [RFC 0027 cross-scope consolidation amendment](0027-amendment-cross-scope-consolidation.md) (the pump that *produces* cross-room L2 facts).

## Non-goals (stub — to expand in the implementation amendment)

- Unifying **episodic** (L1) recall *here* — the L1 axis is owned by the [RFC 0049 L1 amendment](0049-amendment-l1-cross-room-availability.md); this amendment touches only the L2 facts tier. *(Reworded 2026-07-20 — originally "episodes stay room-scoped", which the L1 amendment reverses.)*
- Removing the capture-time "is this worth consolidating?" judgment.
- The exact provenance/classification column shape and migration — deferred to the implementation amendment.

## Related documentation

- [RFC 0049 — Memory Consolidation Gradient](0049-memory-consolidation-gradient.md) — the authoritative model
- [RFC 0037 — Memory Confidentiality & Channel Classification](0037-memory-confidentiality-channel-classification.md) — the egress keystone
- [RFC 0031 identity amendment](0031-amendment-person-identity-cross-room-tier.md) — the cross-room-by-tier precedent
- [RFC 0026 — Declarative Facts Tier](0026-declarative-facts-tier.md) — the tier amended
- [ISSUE-0084](../issues/ISSUE-0084-fact-scope-by-subject-not-uniform-session.md) — re-rooted by this amendment
- [memory-scope-axes.md](../memory-scope-axes.md) — decision 4, superseded
