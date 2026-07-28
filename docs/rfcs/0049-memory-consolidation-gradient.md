---
id: RFC-0049
title: Memory Consolidation Gradient & Scope Reconciliation
summary: Add the vertical "consolidation level" axis (working → episodic → semantic → procedural → experiential) on top of the horizontal scope axes already settled in memory-scope-axes.md, and state one law — a memory's recall scope is a function of how consolidated it is, not of which subsystem stores it. Raw episodic memory stays room-scoped; consolidated knowledge and learned experience cross rooms, made safe by the RFC 0037 egress gate rather than by walling recall. This is the model the "personas behave like real people" goal and the v0.4.0 experience/decision work both require.
type: architecture
status: partially_implemented
author: Maksim Khomutov
created: 2026-06-06
target: v0.3.12 (Phases 0–1) + v0.4.0 (Phases 2–4); design ratified v0.3.7
depends_on:
  - RFC-0031
  - RFC-0037
  - RFC-0027
  - RFC-0028
  - RFC-0029
---

# RFC 0049 — Memory Consolidation Gradient & Scope Reconciliation

**Type**: architecture (memory model — meta-RFC over the memory tier RFCs)
**Status**: ⚠️ Partially Implemented — **Phases 0–1 ✅ v0.3.12, LIVE** (closeout 2026-07-28; [PR plan](0049-pr-plan.md) PRs 1–5 = [#781](https://github.com/mkhomutov/Persatrix/pull/781)/[#782](https://github.com/mkhomutov/Persatrix/pull/782)/[#783](https://github.com/mkhomutov/Persatrix/pull/783)/[#784](https://github.com/mkhomutov/Persatrix/pull/784) + the closeout: the `topic.*` capture path + both widenings, shadow→live on the green measurement verdict — see the amendments' Promotion sections); **Phases 2–4 stay v0.4.0** (they need the unimplemented RFC 0027/0028 engines). The gradient, the one law, and the §D re-rooting (cross-room topic knowledge) are **ratified** (2026-06-06). The 2026-07-15 lock also reversed ratified Non-Goal #1 — applied via the [L1 amendment](0049-amendment-l1-cross-room-availability.md) (raw episodic recall cross-room *available* behind the RFC 0037 gate, room-first-ranked).
**Author**: Maksim Khomutov
**Date**: 2026-06-06
**Target**: v0.3.12 (Phases 0–1) + v0.4.0 (Phases 2–4); **design ratified in v0.3.7** (docs-only, no code before v0.3.12 opens)
**Depends on**: RFC 0031 (Per-Session Namespacing — the `session = room` axis this RFC keeps and reframes), RFC 0037 (Memory Confidentiality — the egress gate that is this RFC's keystone), RFC 0027 (Reflection-Driven Consolidation — the pump that moves memory up the gradient), RFC 0028 (Agent Decision Policy Engine — the source of the experiential tier), RFC 0029 (Personal/Society Storage Split — the physical-storage axis this RFC is orthogonal to)
**Relates to**: RFC 0026 (Declarative Facts Tier — the tier whose scope this RFC re-decides), RFC 0038 (Concurrent-Context Awareness & Cross-Channel Relay — the runtime surface of cross-room knowledge), RFC 0042 (State Namespacing by Scope — the runtime-state vocabulary this RFC's memory vocabulary must agree with), RFC 0014/0015 (Skill Registry / Pattern Extraction — the procedural tier, already cross-scope)
**Spawned from**: [memory-scope-axes.md](../memory-scope-axes.md) (which settled the *horizontal* axes and asked to be promoted to an RFC) + the v0.3.7 cross-room behaviour review (the "personas should behave like real people" finding)

---

## Table of Contents

- [Summary](#summary)
- [Motivation](#motivation)
- [Goals](#goals)
- [Non-Goals](#non-goals)
- [Design](#design)
  - [A. The two planes](#a-the-two-planes)
  - [B. The consolidation gradient](#b-the-consolidation-gradient)
  - [C. The one law: scope is a function of consolidation level](#c-the-one-law-scope-is-a-function-of-consolidation-level)
  - [D. Reconciliation with memory-scope-axes.md (and the one decision reopened)](#d-reconciliation-with-memory-scope-axesmd-and-the-one-decision-reopened)
  - [E. Confidentiality is the keystone, not an add-on](#e-confidentiality-is-the-keystone-not-an-add-on)
  - [F. The consolidation pump](#f-the-consolidation-pump)
  - [G. The experiential tier — decisions become memory](#g-the-experiential-tier--decisions-become-memory)
- [Worked example: the two test scenarios](#worked-example-the-two-test-scenarios)
- [Sequencing & Phased Plan](#sequencing--phased-plan)
- [Amendments this RFC implies](#amendments-this-rfc-implies)
- [Risks](#risks)
- [Open Questions](#open-questions)
- [Decision / Next Steps](#decision--next-steps)
- [Related Documentation](#related-documentation)

---

## Summary

[memory-scope-axes.md](../memory-scope-axes.md) separated the word "session" into five **horizontal** axes — persona, session (= room), relationship, epoch, principal — and answered *who and where may see a memory*. It is correct and this RFC keeps it whole.

What it does not have is a **vertical** axis: *how consolidated a memory is*. A raw turn, a conversation summary, a decontextualised fact, a learned skill, and a decision-with-outcome are not the same kind of thing, and they do not want the same scope. This RFC adds that axis — the **consolidation gradient** — and states one law:

> **A memory's recall scope is a function of its consolidation level, not of which tier happens to store it.** Raw, context-rich memory (episodic) is room-*ranked*; consolidated, decontextualised memory (semantic facts, learned procedure, distilled experience) crosses rooms. Cross-room safety is enforced at *egress* by the RFC 0037 confidentiality gate — not by walling recall. *(Amended 2026-07-19: L1 was originally room-****scoped****; the [L1 amendment](0049-amendment-l1-cross-room-availability.md) makes raw episodic recall cross-room available behind the gate, with room-first ranking as the continuity default.)*

This is the model both of the project's stated goals need:

1. **"Personas behave like real people."** A person carries what they *know* across every room while what they *experienced* stays tied to where it happened. The gradient is exactly that distinction.
2. **The v0.4.0 experience/decision layer** (RFC 0027 consolidation, RFC 0028 decisions). Experience is *built* by consolidating episodic memory upward across contexts. If the lower tiers are permanently room-walled, the pump that turns experience into judgment has nothing to lift.

The change is small in surface and large in consequence: it does **not** unify episodic recall (that would contradict the dementia-test continuity memory-scope-axes.md protects), and it does **not** introduce a new store. It reframes one tier's scope (topic-subject knowledge), names a vertical axis the system was already half-using (identity and skills already cross rooms; episodes do not), and fixes the v0.4.0 build order so the safe pieces land before the cross-room ones.

## Motivation

### M-1. The current model half-implements the gradient already — without naming it

Look at what already crosses rooms and what does not, today:

| Tier | Crosses rooms today? | Why |
|------|----------------------|-----|
| Episodic (narrative) | no | room-scoped by RFC 0031 §D |
| Person **identity** (name/role/prefs) | **yes** | RFC 0031 identity amendment — "scope is a property of the tier, not a topic prefix" |
| Relationship (trust/opinion) | **hybrid** | one cross-room row at storage (`relationships` PK omits `session_id`), but the §D session filter walls *recall* — only the identity field reads cross-room today |
| Topic/project facts | no | memory-scope-axes.md: "belongs to the room" |
| Skills / learned patterns | **no (today)** | RFC 0014/0015 (cross-agent skills) are *proposed, unimplemented*; the shipped procedural tier (RFC 0008) reuses the episodes table and is per-agent + room-scoped by the RFC 0031 session filter |

The system is *already* sorting memory by how consolidated it is — raw episodes stay put; a distilled fact about a person travels; a learned skill travels furthest. It just made that decision one tier at a time, with no stated axis. The result is the drift the RFC 0031 identity amendment had to retrofit: scope kept getting decided locally (by a topic prefix, by a PK, by a tier) instead of by a principle. **Naming the axis is what stops the next drift.**

### M-2. The one gap that breaks the "real person" goal: topic knowledge is room-walled

The live v0.3.7 review found that an agent told a project fact in a DM does not know it in a group channel — and cannot be made to, because memory-scope-axes.md deliberately keeps **topic-subject facts room-scoped** ("knowledge about a person travels; knowledge about a topic belongs to the room"). A real colleague does the opposite: tell them "Atlas ships Friday" in a hallway and they know it in the standup. Topic-knowledge room-scoping is the single least human-like decision in the current model, and it is the direct cause of the scenario-2 failure (an agent learning project facts in one room and recalling them in another).

That decision was made for a good reason — *leakage*: a fact from a private room must not surface in a public one. But leakage is RFC 0037's job (a classification gate at egress), not the scope axis's job. memory-scope-axes.md predates 0037's gate, so it controlled leakage with the only tool it had: a recall wall. With 0037 in place, topic knowledge can be cross-room (human-like) **and** safe (gated at egress). This RFC reopens exactly that one decision and re-roots it on the gradient + confidentiality.

### M-3. Experience cannot be built on walled episodes

RFC 0027 (consolidation) *as designed* reflects **per active scope** only — it reads the top-N episodes of the *current room* (RFC 0027 is proposed, not yet implemented). RFC 0028 (decisions) writes `DecisionRecord`s to an audit log read **offline**, never back to the agent at decision time. So an agent can never notice "I keep hitting this same failure across projects" or "last time I made this call it went badly" — the cross-context signal is severed at both ends. Genuine experience and judgment require lifting episodic memory *across* rooms into a consolidated tier and feeding decisions *back* as memory. Neither is possible while the gradient's lower rungs are permanently room-walled and its top rung (decisions) is write-only.

## Goals

- Define the **consolidation gradient** (L0–L4) as a first-class axis, orthogonal to the horizontal scope axes.
- State the **one law** (scope = f(consolidation level)) so no future tier re-decides scope locally.
- **Promote** memory-scope-axes.md's horizontal-axis decisions to ratified RFC status (the doc asks for this explicitly) — unchanged except where noted.
- Reopen and re-root **one** decision: topic-subject knowledge scope.
- Establish **RFC 0037 as the keystone** and fix the v0.4.0 build order around it.
- Give RFC 0027, RFC 0028, and RFC 0038 a shared model to build against, instead of each inventing scope.

## Non-Goals

- ~~**Unifying episodic recall.**~~ **Superseded 2026-07-19 by the [L1 amendment](0049-amendment-l1-cross-room-availability.md):** episodes become cross-room *available* behind the RFC 0037 gate. The dementia-test continuity memory-scope-axes.md protects is preserved by **room-first ranking** (the boost, not the wall); the old `sessions=[…]/"*"` path was CLI/debug-only and is not the mechanism.
- **A new store or backend.** The gradient is a property of *existing* tiers (RFC 0005/0026/0027). Physical storage (personal vs society, SQLite vs Postgres) is RFC 0029's orthogonal axis and is untouched.
- **Removing the capture-time classification judgment.** Something still decides "is this worth consolidating?" — that stays a persona judgment, as the identity amendment already accepted.
- **Shipping code before v0.3.12 opens.** v0.3.11 is in release-prep; this RFC is docs-only until v0.3.12 opens. Phases 0–1 land in v0.3.12; Phases 2–4 (which need the unimplemented RFC 0027/0028 engines) stay v0.4.0, behind the sequencing in [§ Sequencing](#sequencing--phased-plan).

## Design

### A. The two planes

Memory is positioned on **two independent planes**. The bug being fixed is that "room" was placed on the wrong one.

**Plane 1 — Consolidation level (vertical).** How decontextualised a memory is. Memory flows *up* this plane and sheds context as it rises. This is the plane this RFC adds.

**Plane 2 — Scope / access (horizontal).** Who and where may see a memory. This is memory-scope-axes.md's plane, kept whole: `persona` (the mind itself) · `session = room` · `relationship` (per-person, cross-room) · `epoch` (run/test isolation, hard wall) · `principal` (tenant, hard wall).

Room is a **tag on Plane 1 and a ranking cue**, never a hard wall on Plane 2. Isolation belongs to `epoch`; tenancy to `principal`; leakage to RFC 0037. The room's only Plane-2 role is the *default* scope of the lowest, rawest rung — and only because raw episodic memory is the one rung whose meaning is inseparable from where it happened.

### B. The consolidation gradient

| Level | Tier | What it holds | Context retained | Default scope | RFC |
|------|------|---------------|------------------|---------------|-----|
| **L0 Working** | working memory | this turn's assembled context | full, transient | the turn | 0005 / 0034 |
| **L1 Episodic** | episodes, raw notes | what happened, verbatim-ish | room + time (a *source tag*) | **room-ranked**; cross-room *available* behind the 0037 gate (*amended 2026-07-19 — [L1 amendment](0049-amendment-l1-cross-room-availability.md)*) | 0005 / 0031 |
| **L2 Semantic** | facts, identity, relationship | what is *true* — decontextualised | provenance only | **cross-room** (`persona`) | 0026 / 0031-id |
| **L3 Procedural** | skills, learned patterns | how to *do* things | none | **cross-agent** *(aspirational — 0014/0015 unimplemented; today's RFC 0008 procedural rows are room-scoped)* | 0008 (today) / 0014–0015 (planned) |
| **L4 Experiential** | decision records → heuristics | what *worked* — choices + outcomes | provenance + outcome | **cross-room → society** | 0028 / 0029 |

The gradient is monotonic in two things at once: as memory rises, it **loses room-context** and **widens scope**. L1 is where "where/when" lives; by L2 it is metadata; by L3 it is gone. This is exactly human memory — you forget *where* you learned to ride a bike (L3) long before you forget *how* (the skill survives, the episode fades).

### C. The one law: scope is a function of consolidation level

> A tier's default recall scope is determined by its rung on the gradient, not by which subsystem owns it and not by a string the model picked at write time.

Corollaries:
- **L1 → room-first.** Raw experience is room-*ranked* because its meaning *is* its context — but per the [L1 amendment](0049-amendment-l1-cross-room-availability.md) (2026-07-19) it is cross-room *available* behind the 0037 gate, not walled. *(Originally "room-scoped, unchanged".)*
- **L2+ → cross-room.** Consolidated knowledge is cross-room because consolidation is precisely the act of stripping the context that bound it to a room. Identity (L2) already proves this; topic facts (L2) should follow.
- **Scope is intrinsic to the rung, never a query carve-out.** This is the identity amendment's principle, generalised: the F-7 seam recurred because scope was decided at the *query* layer; the cure is to decide it at the *tier/rung* layer. Same cure, applied to the whole gradient.

### D. Reconciliation with memory-scope-axes.md (and the one decision reopened)

memory-scope-axes.md's six "decisions taken" are **kept** by this RFC, with one re-rooted:

| memory-scope-axes.md decision | This RFC |
|---|---|
| 1. Session = room-continuity, `(agent, channel)` | **Kept as the ranking default.** It is L1's continuity unit; since the [L1 amendment](0049-amendment-l1-cross-room-availability.md) (2026-07-19) it ranks rather than walls. |
| 2. Drop sender axis | **Kept** (already shipped, ISSUE-0083). |
| 3. Relationship cross-room | **Kept.** It is L2. |
| 4. **Fact scope follows *subject*** (person→cross, topic→room) | **Re-rooted.** Scope follows *consolidation level*, not subject. See below. |
| 5. Epoch axis for isolation | **Kept.** It is the Plane-2 isolation wall. |
| 6. Principal = tenant/deletion | **Kept.** Plane-2 tenancy wall. |

**The re-rooting (decision 4 / [ISSUE-0084](../issues/ISSUE-0084-fact-scope-by-subject-not-uniform-session.md)).** memory-scope-axes.md scopes facts by their *subject*: a fact about a person crosses rooms; a fact about a topic stays put. This RFC argues the right discriminator is *consolidation level + confidentiality*, not subject:

- **Subject is a proxy that breaks.** "The Atlas project ships Friday" is a topic fact an agent absolutely should carry across rooms (it is decontextualised knowledge — L2). "Alice said something embarrassing in this DM" is a person fact that absolutely should *not* leak to a group. Subject predicts neither case. *Consolidation level* (is this a distilled fact or a raw episode?) plus *confidentiality* (what was the source channel's classification?) predicts both.
- **The reason topic facts were walled was leakage, and leakage is now 0037's job.** Once the egress gate exists, a topic fact can be L2 (cross-room) and still never reach a lower-classified channel. The wall becomes redundant — and redundant walls are what severed scenario 2.

So **ISSUE-0084 is reframed**: not "classify the fact's subject to choose person-scope vs room-scope," but "an L2 fact is cross-room; its *visibility* is the 0037 protection level inherited from its source." This is strictly simpler (no subject classifier) and it is what makes an agent carry project knowledge like a colleague.

This is the **only** decision this RFC reopens. Everything else in memory-scope-axes.md stands.

**Ratified 2026-06-06.** The re-rooting is adopted: an L2 fact is cross-room, its visibility is the RFC 0037 protection level inherited from its source, and memory-scope-axes.md decision 4 (fact scope by subject) is superseded. The default is cross-room topic knowledge; a per-persona `compartmentalised` posture is the named exception (see [OQ-1](#open-questions)). This re-roots [ISSUE-0084](../issues/ISSUE-0084-fact-scope-by-subject-not-uniform-session.md) from "classify the fact's subject" to "L2 = cross-room, gated by 0037".

### E. Confidentiality is the keystone, not an add-on

The moment topic knowledge becomes cross-room, the recall wall stops doing the leakage job it was quietly doing. Therefore **RFC 0037 must land before any L2 scope widening** — it is the load-bearing replacement, not a parallel feature.

RFC 0037 already has the right shape for this:
- a **deterministic egress gate** at injection/recall: an entry whose protection level outranks the acting channel's classification is withheld — verbatim, server-side, non-optional;
- **declassification projections**: an LLM-abstracted, lower-classified restatement, so an agent can *use* what it learned in a restricted room without leaking the verbatim text — the machine analogue of "I can't share the details, but the gist is…".

That projection mechanism is also a **consolidation primitive** (it produces a lower-fidelity, wider-scope memory from a higher one) — so this RFC ties 0037's projection to 0027's consolidation rather than letting them be two unrelated abstractors.

**Hard sequencing rule:** widening any tier from room to cross-room without 0037's gate in place is a confidentiality regression. 0037 is Phase 0 of everything below.

### F. The consolidation pump

Memory rises the gradient via **consolidation** — the act that strips context and widens scope. No consolidation *pump* exists today. The nearest precedent is the identity write-through — a synchronous *capture-time router* (`store_note` → identity tier) that proves the cross-room L2 destination tier works, but is not itself an L1→L2 *lift* of accumulated episodes. This RFC names the general lifting mechanism and points it at RFC 0027:

- **L1 → L2 (episodic → semantic).** RFC 0027 reflection, extended with a **cross-scope mode**: in addition to per-room reflection, a bounded periodic pass reads across the agent's rooms and distils recurring, decontextualised knowledge into L2 facts — stamped per **[RFC 0037 §C "Synthesized (multi-source) entries"](0037-memory-confidentiality-channel-classification.md#c-memory-provenance-and-protection-level)**, which owns the multi-source rule (`max` over sources, enforced at the memory write API, `provenance_json` provenance). This is the pump that makes "I learned this across several conversations" possible — and it is where experience genuinely forms.
- **L2 → L4 (semantic → experiential).** Decisions + outcomes (RFC 0028) consolidate into reusable heuristics (see §G).
- **L1/telemetry → L3 (episodic → procedural).** *Planned*, not shipped: RFC 0015 pattern extraction → RFC 0014 skills (both proposed). Today's procedural rows (RFC 0008) live in the episodes table and are room-scoped — so the top of the pump is a design target, not yet a working proof.

Consolidation is **append-only and non-destructive** (RFC 0027's rule): raw L1 episodes are never rewritten; a consolidation note points back at its sources and demotes them in ranking. Rising the gradient adds a higher-level memory; it never erases the lower one.

### G. The experiential tier — decisions become memory

RFC 0028 produces rich `DecisionRecord`s (candidates, scores, rationale, outcome) but writes them only to an audit log for *offline* replay. This RFC adds the missing return path:

- **Decisions are a readable L4 memory tier.** At the RFC 0028 `pre-act` checkpoint, the agent retrieves *similar past decisions and their outcomes* as a memory tier (subject to the 0037 gate and the injection budget).
- **Heuristics are consolidated decisions.** RFC 0027's pump distils recurring decision→outcome pairs into L4 heuristics ("when X, Y tends to fail") that bias future candidate scoring.
- **Storage is RFC 0029's society tier.** RFC 0029 already plans decision records for the society backend — so L4 is cross-room *and* cross-agent-ready by construction. No new storage decision.

This is what turns "an agent that decides" into "an agent that decides *from experience*" — the v0.4.0 deliberative-reasoning rung on the usefulness ladder.

## Worked example: the two test scenarios

The scenarios from the v0.3.7 review, traced through the model:

**Scenario 1 — introduce yourself in a group, asked in a DM.** Your name is L2 (identity). It already crosses rooms *if the agent consolidates it* (`store_note(contact:<you>)` → identity write-through). The failure was a missing pump firing, not a scope wall. Under this RFC the pump is general and reflection-backed, so identity capture stops depending on the model spelling one topic prefix correctly. ✅ works.

**Scenario 2 — teach project facts in DMs, share in a group, re-learn in DMs.** "Atlas ships Friday" is L2 topic knowledge. Today it fails **twice over**: it is room-walled (memory-scope-axes.md decision 4), and it is **never captured at all** — the facts tier's frozen predicate allowlist is person-centric, the extractor proposes only self/counterparty subjects, and recall seeds only self + sender. The capture half is the [RFC 0026 topic-predicate amendment](0026-amendment-topic-subject-predicates.md) (v0.3.12, lands with Phase 1 — without it the L2 widening reads an empty tier). → ✗ fails. Under this RFC: the fact consolidates to the cross-room L2 tier (§D re-rooting), the cross-scope pump (§F) lifts what was learned in the group into each agent's semantic memory, the 0037 gate (§E) ensures a fact learned in a `restricted` channel never surfaces in a `public` one — and the DM re-ask now recalls it. ✅ works, *and* stays safe. This is also the persisted-memory complement to RFC 0038's live cross-channel relay.

## Sequencing & Phased Plan

Phases 0–1 target **v0.3.12**; Phases 2–4 stay **v0.4.0** (Phase 2 needs the unimplemented RFC 0027 reflection engine; Phase 3 needs the unimplemented RFC 0028 decision engine). The order is forced by §E.

- **Phase 0 — RFC 0037 confidentiality gate (keystone).** Classification on channels; protection level stamped on memory rows (fail-closed `internal`); the deterministic egress gate at injection + recall. Nothing below may ship first. *(The roadmap now places 0037 in v0.3.12 alongside this RFC's Phases 0–1 — both pulled forward per the 2026-07-15 decision.)*
- **Phase 1 — Name the gradient + re-root ISSUE-0084 + capture topic facts.** Promote this RFC's law; reframe ISSUE-0084 from subject-classification to "L2 = cross-room, visibility = 0037 level." Widen topic-fact recall to cross-room *behind the Phase-0 gate* — **and land the capture path** ([RFC 0026 topic-predicate amendment](0026-amendment-topic-subject-predicates.md): `topic.*` predicate namespace + extractor-prompt + recall-seeding, behind the allowlist blast-radius re-review), without which the widening reads an empty tier.
- **Phase 2 — Cross-scope consolidation (RFC 0027 amendment).** Add the bounded cross-room reflection pass (L1→L2 pump). This is where the relevance-budget risk is measured (see Risks) before it is trusted.
- **Phase 3 — Decisions as memory (RFC 0028 amendment).** Readable L4 tier at `pre-act`; heuristic consolidation; society storage via RFC 0029.
- **Phase 4 — Reconcile with RFC 0038 / RFC 0042.** Cross-channel relay (live) and state-scope vocabulary aligned to the same gradient + axes.

**Measurement gate between Phase 1 and Phase 2:** ship cross-room L2 recall in *shadow* first (evaluated against RFC 0044 golden traces) and only promote it to the live prompt once it does not degrade prompt quality under the RFC 0017 injection budget.

## Amendments this RFC implies

Amendment files (created; the stubs expand into full implementation amendments when the corresponding release's PR plan opens — the first three ride v0.3.12 with Phases 0–1, the RFC 0027/0028 amendments ride v0.4.0 with Phases 2–3):

- [**RFC 0049 amendment — L1 cross-room availability**](0049-amendment-l1-cross-room-availability.md) — raw episodic recall becomes cross-room available behind the 0037 gate, room-first-ranked (reverses Non-Goal #1 per the 2026-07-15 v0.3.12 lock). ✍️ Authored 2026-07-19.
- [**RFC 0026 amendment — topic-subject predicate vocabulary**](0026-amendment-topic-subject-predicates.md) — the scenario-2 *capture* path: `topic.*` predicates + extractor/recall widening, gated on the allowlist blast-radius re-review (v0.3.12, with Phase 1). ✍️ Stub authored 2026-07-19.
- [**RFC 0031 amendment — fact scope by consolidation level**](0031-amendment-fact-scope-by-consolidation-level.md) — fact scope is consolidation-level, not subject; L2 facts cross rooms, gated by 0037. Re-roots [ISSUE-0084](../issues/ISSUE-0084-fact-scope-by-subject-not-uniform-session.md); supersedes memory-scope-axes.md decision 4.
- [**RFC 0027 amendment — cross-scope consolidation**](0027-amendment-cross-scope-consolidation.md) — the bounded agent-wide reflection pass that distils L1→L2 across rooms; ties the declassification projection to it.
- [**RFC 0028 amendment — decisions as readable memory**](0028-amendment-decisions-as-readable-memory.md) — the `pre-act` retrieval of past decision→outcome records and their heuristic consolidation; storage in the RFC 0029 society tier.
- **(doc) memory-scope-axes.md** — decision 4 annotated as superseded by this RFC; decisions 1–3, 5–6 unchanged. ✅ Done (PR [#559](https://github.com/mkhomutov/Persatrix/pull/559)).

## Risks

- **Sequencing is non-negotiable.** L2 widening before the 0037 gate = leak. Phase 0 gates everything.
- **Relevance becomes load-bearing.** Cross-room L2 recall puts more candidates in front of the RFC 0017 budget; if RFC 0030 relevance ranking is weak, prompts get noisier, not smarter. Mitigation: room-as-ranking-cue (same-room boosted) + the Phase-1→2 shadow measurement gate.
- **Cross-scope consolidation is the cost sink.** Agent-wide reflection is expensive; it must be bounded (top-N, decay-weighted, scheduled, budget-capped per RFC 0008) or it dominates cost.
- **Spine blast radius.** L2 recall is shared by every tier. Land tier-by-tier behind the gate, with the recall-latency regression gate (RFC 0029 Phase 1 already added one) watched.

## Open Questions

- **OQ-1. ✅ Resolved 2026-06-06.** Is "topic knowledge crosses rooms" the right *default*? **Yes** — cross-room is the default, made safe by the RFC 0037 classification gate, with an optional per-persona `compartmentalised` flag as the named exception. Continuity is the default; isolation is the named exception (the memory-scope-axes.md grounding principle). The exception's exact shape (per-persona flag vs per-channel posture vs both) is a v0.4.0 implementation detail, not a blocker for the model.
- **OQ-2.** Does the L4 experiential tier belong to the persona (personal) or the society (cross-agent) by default? RFC 0029 leans society; a persona's *private* lessons may want personal. Likely both, split by classification.
- **OQ-3.** How much does cross-scope consolidation actually cost at realistic room counts, and what is the bound? Must be answered by the Phase-2 measurement before trusting the pump.
- **OQ-4.** Does the relationship "contextual facet" layer (memory-scope-axes.md open follow-on) become an L1-vs-L2 distinction under this model (room-scoped behavioural facet = L1; cross-room core = L2)? Probably yes — fold it in when the facet layer is specified.

## Decision / Next Steps

**Status 2026-07-28 (Phase 0–1 closeout)**: items 1–4 are ✅ done — the law is ratified, the amendments are expanded and **implemented live**, RFC 0037 Phase 1 shipped as the v0.3.12 keystone (its own PRs 1–5), and the shadow-measurement gate ran **green** (`evaluators/shadow_measurement.py`, re-executed in CI). What remains is the v0.4.0 slice: Phases 2–4 on the RFC 0027/0028 engines.

1. Ratify the gradient + the one law as the memory model of record (v0.3.7 docs).
2. Expand the four amendment stubs into their own files.
3. Confirm RFC 0037 as v0.4.0 Phase 0 (already roadmapped) and bind the sequencing rule above to it. *(0037 was subsequently pulled forward with this RFC — it shipped as the v0.3.12 keystone.)*
4. Add the Phase-1→2 shadow-measurement gate to the RFC 0044 golden-trace suite as the promotion criterion for cross-room L2 recall.

## Related Documentation

- [memory-scope-axes.md](../memory-scope-axes.md) — the horizontal-axis planning doc this RFC promotes and extends
- [RFC 0031 — Per-Session Namespacing](0031-per-session-namespacing-channels.md) + [identity amendment](0031-amendment-person-identity-cross-room-tier.md) — the `session` axis and the identity precedent for "scope is intrinsic to the tier"
- [RFC 0037 — Memory Confidentiality & Channel Classification](0037-memory-confidentiality-channel-classification.md) — the keystone egress gate
- [RFC 0027 — Reflection-Driven Consolidation](0027-reflection-driven-consolidation.md) — the consolidation pump
- [RFC 0028 — Agent Decision Policy Engine](0028-agent-decision-policy-engine.md) — the experiential-tier source
- [RFC 0029 — Personal/Society Storage Split](0029-personal-society-storage-split.md) — the orthogonal physical-storage axis
- [RFC 0026 — Declarative Facts Tier](0026-declarative-facts-tier.md) — the L2 tier whose scope is re-rooted
- [RFC 0038 — Concurrent-Context Awareness & Cross-Channel Relay](0038-concurrent-context-awareness-relay.md) — the live counterpart to persisted cross-room knowledge
- [ISSUE-0084 — Fact scope by subject](../issues/ISSUE-0084-fact-scope-by-subject-not-uniform-session.md) — the issue this RFC re-roots
- [Memory Quality Roadmap](../memory-quality-roadmap.md) — the dementia-test bar this model serves
- [v0.3.x Sequencing](../v0.3.x-sequencing.md) — why memory/org work follows conversation realism
