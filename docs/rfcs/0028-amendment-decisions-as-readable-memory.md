# RFC 0028 Amendment — Decisions Become Readable Memory (the L4 Experiential Tier)

**Type**: amendment to [RFC 0028](0028-agent-decision-policy-engine.md) §A (Decision checkpoint model) + §D (Persistence and replay)
**Status**: 📋 Proposed — **stub**. Authorised by [RFC 0049 §G](0049-memory-consolidation-gradient.md#g-the-experiential-tier--decisions-become-memory); to be expanded into a full amendment when its v0.4.0 PR plan opens.
**Author**: Maksim Khomutov
**Date**: 2026-06-06
**Target**: v0.4.0 — with the RFC 0028 base engine; behind the [RFC 0037 keystone](0037-memory-confidentiality-channel-classification.md); no v0.3.7 code.
**Authoritative model**: [RFC 0049 — Memory Consolidation Gradient & Scope Reconciliation](0049-memory-consolidation-gradient.md)

---

## Context

RFC 0028 produces a rich `DecisionRecord` at each checkpoint (candidates, scores, rejected reasons, rationale) but §D persists it to **two write-only sinks**: OTEL spans (sampled) and an audit-log stream "for forensic replay … the source of truth for the Phase 2 replay harness". The record is read **offline** for calibration — never fed back to the agent **at decision time**.

[RFC 0049](0049-memory-consolidation-gradient.md) places decisions-with-outcomes at the top of the consolidation gradient (**L4 — experiential**) and observes the missing return path: an agent that logs decisions but never reads its own past ones cannot decide *from experience* ("last time I made this call it went badly"). Closing that loop is what turns the v0.4.0 deliberative-reasoning rung from "decides" into "decides from experience".

## Decision

1. **Decisions are a readable L4 memory tier.** At the RFC 0028 **pre-act** checkpoint, the agent retrieves *similar past decision → outcome* records as a memory tier — subject to the [RFC 0037](0037-memory-confidentiality-channel-classification.md) egress gate and the [RFC 0017](0017-persona-memory-injection-budget.md) injection budget like any other tier.
2. **Heuristics are consolidated decisions.** The [RFC 0027 cross-scope pump](0027-amendment-cross-scope-consolidation.md) distils recurring decision→outcome pairs into L4 **heuristics** ("when X, Y tends to fail") that bias future candidate scoring (RFC 0028 §C guardrail/selection pipeline).
3. **Storage is the RFC 0029 society tier.** [RFC 0029](0029-personal-society-storage-split.md) already plans decision records for the society backend, so L4 is cross-room *and* cross-agent-ready by construction — no new storage decision. A persona's *private* lessons may stay personal; the split is by classification ([RFC 0049 OQ-2](0049-memory-consolidation-gradient.md#open-questions)).
4. **The offline replay/calibration path is unchanged.** This amendment *adds* a read path; it does not alter §D's audit-log source-of-truth role for replay.

## What changes

- **RFC 0028 §A** — pre-act gains a "retrieve similar past decisions" memory-read step before candidate scoring.
- **RFC 0028 §D** — the persisted `DecisionRecord` is additionally indexed for at-decision-time recall (not only offline replay).
- **RFC 0028 §C** — consolidated heuristics enter the selection pipeline as a scoring input.

## Sequencing / dependencies

- **Depends on the RFC 0028 base engine** (v0.4.0 Phases 1–3) and the **RFC 0037 keystone** (the L4 tier is egress-gated like any cross-room memory).
- **Depends on the [RFC 0027 cross-scope pump](0027-amendment-cross-scope-consolidation.md)** for the heuristic-consolidation half (decision records → heuristics).
- **Relates to RFC 0029** for society-tier storage of decision records.

## Non-goals (stub — to expand)

- Online production learning loops (RFC 0028 keeps offline calibration; this only adds *recall* of past decisions, not autonomous weight updates).
- Collective/quorum decisions (RFC 0028 Phase 4, v0.5.0+).
- The exact similarity-retrieval and heuristic-representation shapes — deferred to the implementation amendment.

## Related documentation

- [RFC 0049 — Memory Consolidation Gradient](0049-memory-consolidation-gradient.md) §G — the authoritative L4 model
- [RFC 0028 — Agent Decision Policy Engine](0028-agent-decision-policy-engine.md) — the RFC amended
- [RFC 0027 cross-scope consolidation amendment](0027-amendment-cross-scope-consolidation.md) — the pump that distils heuristics
- [RFC 0029 — Personal/Society Storage Split](0029-personal-society-storage-split.md) — society-tier storage for decision records
- [RFC 0037 — Memory Confidentiality](0037-memory-confidentiality-channel-classification.md) — the egress gate
