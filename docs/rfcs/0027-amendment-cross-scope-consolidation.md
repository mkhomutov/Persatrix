# RFC 0027 Amendment — Cross-Scope Consolidation (the L1 → L2 Pump)

**Type**: amendment to [RFC 0027](0027-reflection-driven-consolidation.md) §B (Reflection scope) + Non-Goals
**Status**: 📋 Proposed — **stub**. Authorised by [RFC 0049 §F](0049-memory-consolidation-gradient.md#f-the-consolidation-pump); to be expanded into a full amendment when its v0.4.0 PR plan opens.
**Author**: Maksim Khomutov
**Date**: 2026-06-06
**Target**: v0.4.0 — after the [RFC 0037 keystone](0037-memory-confidentiality-channel-classification.md) and alongside the [fact-scope re-root](0031-amendment-fact-scope-by-consolidation-level.md); no v0.3.7 code.
**Authoritative model**: [RFC 0049 — Memory Consolidation Gradient & Scope Reconciliation](0049-memory-consolidation-gradient.md)

---

## Context

RFC 0027 today reflects **per active scope only**: §B fixes the reflection input to "the top-N recent closed episodes for the active scope (DM partner, channel, thread)", and the Non-Goals explicitly exclude "LLM-driven theme detection across the whole episode corpus" and "cross-agent consolidation".

[RFC 0049](0049-memory-consolidation-gradient.md) names consolidation as the **pump** that moves memory up the gradient — stripping room-context and widening scope as it rises (L1 episodic → L2 semantic). Per-scope reflection only ever produces *within-room* consolidations; it can never distil knowledge that recurs *across* an agent's rooms. That is precisely the signal genuine experience is made of ("I keep seeing this across projects"), and per-scope reflection structurally cannot see it.

## Decision

Add a **bounded cross-scope reflection mode** to RFC 0027 — a second reflection trigger that reads across the agent's rooms (still **single-agent**; cross-*agent* consolidation stays a non-goal) and distils recurring, decontextualised knowledge into **L2 facts** rather than room-scoped consolidation notes.

1. **Output is cross-room L2**, carrying provenance (contributing sessions) and the **max source RFC 0037 protection level** for the egress gate — i.e. it feeds the [fact-scope re-root](0031-amendment-fact-scope-by-consolidation-level.md), it does not bypass it.
2. **Bounded by construction.** Top-N decay-weighted episodes across scopes, scheduled (not per-turn), capped under the [RFC 0008](0008-agent-memory-context-optimization.md) budget. Whole-corpus passes stay a non-goal; "bounded cross-scope" replaces "active-scope only", not "everything".
3. **Non-destructive.** RFC 0027's append-only / `consolidated_into` demotion rule is preserved; rising the gradient adds an L2 memory and demotes its L1 sources in ranking, never rewrites them.
4. **Declassification projection = a consolidation primitive.** RFC 0037's projection (an LLM-abstracted, lower-classified restatement) is unified with this pump rather than living as a separate abstractor — the same mechanism that produces a wider-scope memory from a higher-fidelity one.

## What changes

- **RFC 0027 §B** — add the cross-scope reflection input (bounded, scheduled) alongside the existing per-scope path.
- **RFC 0027 Non-Goals** — narrow "active-scope only" to "bounded cross-scope; whole-corpus still excluded"; "cross-agent consolidation" stays a non-goal.
- **RFC 0037** — note the projection mechanism is the L1/L2→wider-scope consolidation primitive shared with this RFC.

## Sequencing / dependencies

- **Depends on** the [fact-scope re-root](0031-amendment-fact-scope-by-consolidation-level.md) (the L2 tier this pump writes into) and therefore transitively on **RFC 0037** (the egress gate).
- **This is where the cost lives.** Cross-scope reflection is the RFC 0049 cost-sink risk; the v0.4.0 amendment must specify the bound (N, decay, cadence, budget cap) and a cost-regression gate, mirroring the [RFC 0024 "bored persona costs nothing"](0024-event-driven-scheduling.md) discipline.

## Non-goals (stub — to expand)

- Cross-**agent** consolidation (stays a non-goal; that is the [RFC 0029](0029-personal-society-storage-split.md) society axis).
- Whole-corpus theme detection.
- The exact trigger cadence and N — deferred to the implementation amendment + v0.4.0 PR plan.

## Related documentation

- [RFC 0049 — Memory Consolidation Gradient](0049-memory-consolidation-gradient.md) §F — the authoritative pump model
- [RFC 0027 — Reflection-Driven Consolidation](0027-reflection-driven-consolidation.md) — the RFC amended
- [RFC 0031 fact-scope re-root amendment](0031-amendment-fact-scope-by-consolidation-level.md) — the L2 tier this pump feeds
- [RFC 0037 — Memory Confidentiality](0037-memory-confidentiality-channel-classification.md) — projection as a consolidation primitive; the egress gate
- [RFC 0008 — Agent Memory & Context Optimization](0008-agent-memory-context-optimization.md) — the budget discipline that bounds the pump
