---
id: RFC-0030
title: Multi-Agent Conversation Governance
summary: Deterministic anti-thrash governance for multi-persona channel conversations — Phase 1 cascade depth/quiet windows in v0.3.x, moderator role in v0.4.0, declarative conversation types in v0.5.0+.
type: architecture
status: proposed
author: Maksim Khomutov
created: 2026-05-11
target: v0.3.x (Phase 1); v0.4.0 (Phase 2); v0.5.0+ (Phase 3)
depends_on:
  - RFC-0011
  - RFC-0020
---

# RFC 0030 — Multi-Agent Conversation Governance

**Type**: architecture
**Status**: 📋 Proposed (Draft)
**Author**: Maksim Khomutov
**Date**: 2026-05-11
**Target**: v0.3.x (Phase 1 — deterministic layers); v0.4.0 (Phase 2 — moderator role); v0.5.0+ (Phase 3 — declarative conversation types + topic-drift)
**Depends on**: RFC 0011 (Channels), RFC 0011 amendment (Cascade-Depth Wire Propagation), RFC 0020 (Interaction Lifecycle)
**Integrates with**: RFC 0023 (LLM Call Leasing), RFC 0028 (Agent Decision Policy Engine), RFC 0024 (Event-Driven Scheduling) — composition, not hard gates

---

## Table of Contents

- [Summary](#summary)
- [Motivation](#motivation)
- [Goals](#goals)
- [Non-Goals](#non-goals)
- [Design / Implementation](#design--implementation)
  - [A. Problem decomposition](#a-problem-decomposition)
  - [B. Layered architecture](#b-layered-architecture)
  - [C. Conversation scope = Interaction](#c-conversation-scope--interaction)
  - [D. Layer 0 — Cascade-depth backstop (shipped)](#d-layer-0--cascade-depth-backstop-shipped)
  - [E. Layer 1 — Per-conversation cost ceiling](#e-layer-1--per-conversation-cost-ceiling)
  - [F. Layer 2 — Per-participant reply budget](#f-layer-2--per-participant-reply-budget)
  - [G. Layer 3 — Response gate (shipped)](#g-layer-3--response-gate-shipped)
  - [H. Layer 4 — End-of-interaction signal](#h-layer-4--end-of-interaction-signal)
  - [I. Layer 5 — Moderator role](#i-layer-5--moderator-role)
  - [J. Layer 6 — Declarative conversation types](#j-layer-6--declarative-conversation-types)
  - [K. Integration with RFC 0028 (Decision Engine)](#k-integration-with-rfc-0028-decision-engine)
  - [L. Telemetry and observability](#l-telemetry-and-observability)
  - [M. Wire and config surfaces](#m-wire-and-config-surfaces)
  - [N. Failure modes](#n-failure-modes)
- [Security Considerations](#security-considerations)
- [Phased Implementation Plan](#phased-implementation-plan)
- [Files Touched (Estimated)](#files-touched-estimated)
- [Test Strategy](#test-strategy)
- [Open Questions](#open-questions)
- [Decision / Next Steps](#decision--next-steps)
- [Related Documentation](#related-documentation)

---

## Summary

A flat `cascade_depth` cap is a safety backstop, not a conversation policy. Productive multi-agent discussions span anywhere from 2 to 20+ turns; a fixed depth either truncates real convergence or under-bounds runaway loops, depending on which one you tune for. This RFC proposes **layered conversation governance**: six complementary mechanisms ordered cheap-and-deterministic → expensive-and-judgement-based, each falling safely down to the next. No single mechanism is asked to be smart, fair, and fail-safe simultaneously.

The scope of a "conversation" is the **Interaction** from [RFC 0020](0020-interaction-lifecycle.md) — a bounded sequence of turns with a defined start, close, and one summary. Governance layers attach to that scope so cost, reply budgets, moderator decisions, and topic-drift detection all share a single identity. Most of the architecture composes existing primitives (RFC 0011 channels, RFC 0020 interactions, RFC 0023 leasing, RFC 0028 decision engine); the new primitives are a per-participant reply budget, an explicit end-of-interaction signal, an optional moderator role, and declarative conversation types.

## Motivation

The v0.3.0 channels stack ships with one mechanism for bounding agent-to-agent conversation: `cascade_depth=5` (RFC 0011 amendment, landed in PR 1+2 of the v0.3.0 test-findings plan). Manual end-to-end testing showed this is necessary but insufficient:

1. **The cap is unfailable and dumb.** It catches loops and runaway costs, but it cannot tell whether a conversation at depth 5 is a productive design discussion converging on a decision (cut off — bad) or two `always`-respond personas trading "great point!" / "thanks!" (cut off — good). Same mechanism, opposite outcomes.

2. **Cost still scales as `members × depth` per publish.** Even at cap=5 a fully populated 50-member channel pays up to 250 LLM calls per publish. F-1's tail (the "~160 calls per prompt" measurement) is *not* closed by depth-clamping alone — it needs an orthogonal cost-ceiling primitive. RFC 0011 amendment lists this as deferred to a follow-up; this RFC is that follow-up.

3. **Real conversation policy is not one number.** In real meetings — the closest non-technical analogy — facilitators apply several layered constraints: each person has roughly a fair share of speaking time; once everyone has agreed there's nothing left to add, the meeting ends; if it runs over an hour the chair calls it; if it drifts off-topic the chair re-anchors. No one of these alone is the policy. The current design forces one mechanism to do all four jobs and inevitably fails at three of them.

4. **The right home for "should I reply?" already exists, partly.** [RFC 0028](0028-agent-decision-policy-engine.md) (Proposed) defines a per-action decision checkpoint with explicit candidates and guardrails. "Reply to channel message" is exactly the kind of decision RFC 0028's pre-act checkpoint covers, with `publish_channel` already listed as a candidate class. RFC 0020's Interaction is already the right scope. RFC 0023's lease `cause` enum already has `CAUSE_CHANNEL_MESSAGE` reserved. The pieces are positioned; they have not been assembled into a coherent conversation-governance story.

**What happens if we do nothing.** The cascade cap continues to catch loops at the cost of cutting productive convergences. Operators tune `max_cascade_depth` upward to give discussions room — and reintroduce the runaway-cost class the cap was meant to prevent. Persona channels remain useful only for narrow "tight-loop pair" patterns; the more interesting "design review with four perspectives" pattern stays untenable.

## Goals

1. **Define conversation governance as a layered architecture**, not a single mechanism. Each layer has a clear responsibility, a known failure mode, and a known cost.
2. **Use [RFC 0020](0020-interaction-lifecycle.md) Interaction as the canonical scope.** Every layer scopes to `interaction_id`; no parallel "conversation" abstraction is introduced.
3. **Bound cost per conversation independently from depth.** Wire RFC 0023 leasing through the channel publish path so a per-`interaction_id` budget exists as a hard ceiling, not an aspiration.
4. **Surface a deterministic per-participant turn budget.** "Each agent gets at most N replies in this interaction" is the cheapest mechanism that catches the productive-conversation-but-one-agent-dominates failure mode.
5. **Provide a structured end-of-interaction signal** so agents can opt out of continued participation without external intervention.
6. **Define an optional moderator role** that reads transcripts and decides continue/wrap-up/terminate, but is itself bounded by budget and falls back safely.
7. **Support declarative conversation types** (design review, brainstorm, retro, incident) so common patterns ship with sensible per-type defaults rather than requiring per-channel tuning.
8. **Each layer fails safely down to the next.** A moderator timeout falls back to per-participant budget; a budget race falls back to depth; depth never fails.
9. **Maintain backward compatibility.** Existing v0.3.0 channels work unchanged; new layers opt in via config.
10. **Make decisions auditable** — moderator decisions and budget-exhaustion drops emit structured records compatible with RFC 0028's `DecisionRecord`.

## Non-Goals

- **Replacing `cascade_depth`.** It remains the unfailable safety backstop (Layer 0). The point of layering is that the cap is the floor, not the policy.
- **Replacing the response gate** (RFC 0011 §D). Per-membership `respond` policy stays as the structural primitive — Layer 3 in the stack.
- **Cross-channel coordination.** Each Interaction is bounded to one channel/thread/DM per [RFC 0020 §G](0020-interaction-lifecycle.md). Cross-channel "this discussion continues in #design" is out of scope; either it's a new Interaction or operators link manually.
- **Real-time topic-shift detection during an open interaction.** RFC 0020 §B already scaffolds `TopicShiftDetector`; this RFC composes with that scaffolding when it lights up, but does not deliver it.
- **Multi-agent voting / consensus** beyond the simple end-of-interaction-vote primitive. Voting protocols ([RFC 0028 Phase 4 collective scope](0028-agent-decision-policy-engine.md)) are a separate workstream.
- **Streaming-response handling.** Settlement semantics for streamed token counts are deferred to whichever RFC introduces streaming.
- **Auto-resuming closed interactions.** Once an Interaction is `closed` (RFC 0020 §C), a continuation starts a fresh interaction. Resume semantics across the close boundary are out of scope.
- **Replacing free-form prompt engineering** as the way personas decide what to say. This is about *whether and when* to reply, not *what* to say.

---

## Design / Implementation

### A. Problem decomposition

The umbrella term "conversation governance" actually covers five distinct sub-problems, each of which wants a different mechanism. Conflating them is the structural reason `cascade_depth` alone cannot solve the problem.

| Sub-problem | Question it answers | Right kind of mechanism |
|-------------|---------------------|--------------------------|
| **(a) Loop prevention** | "Are agents stuck in a trivial back-and-forth?" | Hard cap, deterministic — *cascade_depth* |
| **(b) Cost containment** | "Has this conversation already cost too much, regardless of quality?" | Hard budget, deterministic — *lease budget per interaction* |
| **(c) Fair turn-taking** | "Has one participant dominated past their share?" | Per-participant counter, deterministic — *reply budget* |
| **(d) Productive termination** | "Is there genuinely nothing more to add?" | LLM-quality judgement — *moderator + end-vote* |
| **(e) Topic anchoring** | "Has the discussion wandered away from its original question?" | Embedding/LLM judgement — *topic-drift detector* (RFC 0020 scaffolding) |

Sub-problems (a), (b), (c) are deterministic and unfailable in principle (modulo bugs). Sub-problems (d), (e) require reading the transcript — only an LLM can do that, and any LLM can be wrong, so they're failable and must layer above the deterministic mechanisms, not replace them.

### B. Layered architecture

Six layers, ordered by cheap-and-unfailable → expensive-and-judgement-based:

| Layer | Mechanism | Failure mode | Cost per check | Status |
|-------|-----------|--------------|----------------|--------|
| **0** | Cascade-depth cap | None — counter compare | ~free | ✅ Shipped (RFC 0011 amendment) |
| **1** | Per-interaction cost ceiling (lease budget) | None — wallet rejects | wallet RPC, p99 ≤ 5 ms (RFC 0023) | 📋 This RFC, Phase 1 |
| **2** | Per-participant reply budget | None — counter compare | hashmap lookup | 📋 This RFC, Phase 1 |
| **2.5** | Floor control / speaker serialization | Floor-holder stalls → per-turn timeout advances | one in-flight dispatch + a parked waiter | ✅ Shipped ([floor-control amendment](0030-amendment-floor-control-speaker-serialization.md), v0.3.6; telemetry PR 4 fast-follow) |
| **3** | Per-membership response gate (`respond_policy`) | None — config lookup | ~free | ✅ Shipped (RFC 0011 §D) |
| **4** | End-of-interaction signal (K consecutive votes) | Agent must opt in; falls back to lower layers | message accounting | 📋 This RFC, Phase 1 |
| **5** | Moderator role | Moderator can be wrong or time out; falls back to lower layers | ~1 LLM call per N turns | 📋 This RFC, Phase 2 (v0.4.0) |
| **6** | Declarative conversation type / phases | N/A for runtime termination — sets defaults for Layers 1–5 | trivial | 📋 This RFC, Phase 3 (v0.5.0+) |

> **Layer 2.5 note (added by the [floor-control amendment](0030-amendment-floor-control-speaker-serialization.md)).** Layers 0–2 and 4–6 all answer *volume / termination* questions ("how many," "how much," "are we done"). None answers *ordering* — "who speaks next, and does each responder see the previous one." Sub-problem §A(c) conflated that with the reply *budget*; the amendment splits it and adds **Layer 2.5**, which serializes concurrent responders into a deterministic, mutually-visible speaker round. It is brought forward to **v0.3.6** as a usability blocker, ahead of the rest of this RFC's Phase 1.

**Composition rule.** A publish proceeds only if every layer admits it. Any layer's drop terminates fanout for that publish and emits a `governance_drop{layer}` counter increment. Lower-layer drops short-circuit higher-layer evaluation (no point asking the moderator if the cost ceiling already said no).

**Failure-down rule.** Higher layers can opt out (moderator times out, declarative type is unset). Lower layers never opt out — Layer 0 is always on, Layer 1 is always on once leasing is wired, Layer 2 is always on with a sensible default cap.

```
publish arrives
    │
    ▼
Layer 0: depth >= cap?     ────yes──► drop, log, counter; END
    │ no
    ▼
Layer 1: lease available?  ────no──► drop, log, counter; END
    │ yes
    ▼
Layer 2: participant under reply budget?  ────no──► drop, log, counter; END
    │ yes
    ▼
Layer 3: respond policy admits? ─no──► drop (existing); END
    │ yes
    ▼
Layer 4: interaction has K end-votes?  ─yes──► close interaction, drop; END
    │ no
    ▼
Layer 5: moderator says continue?       ─no──► drop or close; END
    │ yes (or moderator opted out)
    ▼
Layer 6: (sets policy for above; no runtime check)
    │
    ▼
fanout proceeds
```

### C. Conversation scope = Interaction

[RFC 0020](0020-interaction-lifecycle.md) defines **Interaction** as a bounded sequence of turns with a precise lifecycle (`open → closing → closed → summarized`) and an `interaction_id` (ULID). The interaction scope per channel type ([RFC 0020 §G](0020-interaction-lifecycle.md)) is:

- **Group channel**: one interaction per channel per idle window (default 600s).
- **DM**: one interaction per DM pair per idle window.
- **Thread**: one interaction per thread (the thread *is* the conversation; no idle close until the thread is archived).

This RFC reuses that scope verbatim. **A "conversation" in this RFC is an open Interaction.** The Interaction's `interaction_id` is the key under which Layers 1–5 attach state:

- Layer 1: lease ledger keyed by `(channel_id, interaction_id)`.
- Layer 2: `(interaction_id, participant_id) → reply_count` counter.
- Layer 4: `interaction_id → end_vote_set` accumulator.
- Layer 5: moderator state keyed by `interaction_id`.
- Layer 6: declarative type attached to the channel; instantiates at interaction start.

**Wire implication.** `interaction_id` must traverse the publish→fanout→dispatch path so cross-process scope works. Today the orchestrator side (RFC 0020 implementation) keys interactions per-agent in-process. For governance to work cross-process, `metadata.interaction_id` joins `metadata.cascade_depth` on the publish payload — same wire-shape extension pattern as the RFC 0011 amendment. See [§M](#m-wire-and-config-surfaces).

**Restart behaviour** inherits RFC 0020 §C: an in-memory interaction is lost on restart; the next turn starts a fresh interaction. Governance state attached to the lost interaction is implicitly reset — acceptable, because the alternative (durable open-interaction state) is the larger architectural change RFC 0020 explicitly defers.

### D. Layer 0 — Cascade-depth backstop (shipped)

Already implemented per [RFC 0011 amendment 'Cascade-depth wire propagation'](0011-amendment-cascade-depth-wire-propagation.md) (PRs #318, #319). Unchanged by this RFC. It remains the unfailable safety backstop that catches anything the higher layers miss — including bugs in the higher layers themselves.

The default of `max_cascade_depth=5` is now the floor, not the policy. Layer 5 (moderator) is expected to terminate productive discussions well before depth 5 most of the time; depth-5 drops should be a regression signal once moderator is live.

### E. Layer 1 — Per-conversation cost ceiling

**Why.** Even at `cascade_depth=5`, an N-member channel with all-`always` membership pays `(N-1) × 4` LLM calls per publish at worst (4 = inbound depths 1–4; 5 is dropped). A 50-member channel publishes 196 calls per publish; cost compounds across rounds. The depth cap is not a cost cap.

**Mechanism.** Compose with [RFC 0023 LLM Call Leasing](0023-llm-call-leasing.md). The `Cause` enum already has `CAUSE_CHANNEL_MESSAGE = 5` reserved ([RFC 0023 §C](0023-llm-call-leasing.md#c-proto-surface)). This RFC extends the `AcquireLease` request with an optional `interaction_id` attribution field and an optional `interaction_budget_tokens` ceiling. The wallet tracks a per-`interaction_id` running total alongside the existing per-workflow / per-agent totals.

```protobuf
message AcquireLeaseRequest {
  // ... existing fields ...
  string interaction_id = 8;             // RFC 0020 interaction scope (optional;
                                         // absent for non-channel causes)
  int64 interaction_budget_tokens = 9;   // soft ceiling: tokens above this fail-closed
                                         // (0 = use default from config / declarative
                                         //  conversation type)
}
```

When `interaction_budget_tokens` is exhausted, the wallet returns `LeaseDenied{reason=INTERACTION_BUDGET_EXHAUSTED}`. The agent (per RFC 0023 §F) treats this exactly like a workflow-budget denial — the LLM call does not happen, the agent surfaces a `governance.cost_ceiling` event, and the channel publish chain terminates for that interaction.

**Default ceiling.** Configurable per channel via `channels.yaml: interaction_budget_tokens`. Recommended default: derived from the orchestrator's per-workflow token budget divided by an expected-interaction-count heuristic — left as an Open Question (§OQ-5) until v0.3.x usage data exists. Until then, ship `0` (uncapped) as the safe default so this layer is opt-in.

**Why on the lease, not on the publish.** A publish itself doesn't cost tokens — the LLM call that the dispatched event eventually triggers does. Putting the ceiling on the lease catches the actual spend at the exact point where it materialises, and reuses RFC 0023's fail-closed guarantee. Putting it on the publish would create a second budget abstraction that drifts from the wallet over time.

### F. Layer 2 — Per-participant reply budget

**Why.** Per-participant turn limits are the standard meeting-facilitation primitive: "each person gets at most 3 interventions before yielding the floor." It's a structural way to surface the "one agent dominates" failure mode without invoking an LLM judge. Deterministic, cheap, and easy to reason about.

**Mechanism.** A new in-memory counter on the orchestrator side:

```
type interactionReplyBudget struct {
    interactionID  string
    maxPerParticipant int
    counts         map[string]int  // participant_id → reply_count
}
```

On every publish to a channel with an open Interaction, the router:
1. Resolves the Interaction (RFC 0020 §G scope rule) — creates one if none open.
2. Increments `counts[msg.SenderID]`.
3. If `counts[msg.SenderID] > maxPerParticipant`, drops the publish *before* persistence with `ErrParticipantBudgetExhausted` → REST 429 + log + counter.

**Why pre-persistence.** A publish that exceeded its budget should not appear in channel history — that would let an over-budget participant pollute future memory recall with un-acted-on content. The store boundary is the right enforcement point.

**Defaults.** Configurable per channel via `channels.yaml: max_replies_per_participant_per_interaction`. Recommended default: **0 (uncapped)** for backward compatibility with v0.3.0; **10** when the operator declares a `conversation_type` (Layer 6). The orchestrator emits a startup Warn when a channel has all-`always` membership and uncapped reply budget — same shape as the existing unauthenticated-REST warning in [cmd/orchestrator/channels.go](../../cmd/orchestrator/channels.go).

**Reset semantics.** Counters live on the Interaction. When the Interaction closes ([RFC 0020 §B](0020-interaction-lifecycle.md)), the counter is discarded; the next Interaction starts fresh. This matches the human-meeting intuition — your budget resets at the next meeting.

**Mention amplification.** A reply that `@`-mentions another participant could be argued to "spend" the mentioner's budget *and* the mentioned participant's response budget; we deliberately don't model that. Mentions are routing, not accounting; conflating them would surprise operators when a quiet `when_mentioned` agent runs out of replies because someone else mentioned them.

### G. Layer 3 — Response gate (shipped)

Already implemented per [RFC 0011 §D](0011-channels-bridges.md). The per-membership `respond_policy` (`always` | `when_mentioned` | `never`) is the structural admission gate that fires *receiver-side* in the response gate before any LLM call. Unchanged by this RFC.

The gate is necessary but not sufficient — it admits every event for `always` members, and `when_mentioned` for any mentioned event. The higher layers exist precisely because Layer 3 cannot distinguish "good cascade" from "loop."

### H. Layer 4 — End-of-interaction signal

**Why.** The simplest way to terminate a productive discussion is to let participants signal "I'm done." Two consecutive distinct participants saying "I have nothing more to add" is strong enough evidence to close. This pattern is standard in human meetings — "any other business?" → silence → meeting adjourned.

**Mechanism.** Two compatible options; this RFC recommends Option A and reserves the decision per [§OQ-4](#open-questions):

**Option A — Explicit action type.** Add `END_INTERACTION_VOTE` to the agent action vocabulary alongside the existing `END_INTERACTION` structural trigger from [RFC 0020 §B](0020-interaction-lifecycle.md). An agent emits `END_INTERACTION_VOTE` when it judges its contribution complete; the orchestrator accumulates votes per Interaction. When K (default 2) **distinct** participants vote within W consecutive turns (default 3 — i.e., votes must be recent), the orchestrator triggers RFC 0020's structural close and stops fanning out new replies.

**Option B — Implicit no-action signal.** Reuse the existing "agent produced no outbound action" signal: K consecutive turns where the dispatched agents return zero actions counts as an implicit end-vote. Cheaper (no new action) but conflates "I'm done" with "I had nothing useful to say this turn." Loses the explicit-intent signal that matters for audit.

**Why K=2, W=3.** A single agent saying "done" is not consensus; two distinct agents in close succession is. Wider W lets the signal survive an intervening one-off comment. Both are configurable per channel.

**Composition with cascade_depth.** End-votes do *not* reset cascade_depth — they're orthogonal. A vote at depth 4 still closes the interaction even though depth 5 would have dropped fanout anyway. The two mechanisms are complementary: one says "cap reached," the other says "we're done."

**Vote tampering.** A misbehaving agent could spam `END_INTERACTION_VOTE` to silence channels. Mitigations: votes are per-(`participant_id`, `interaction_id`) — one agent voting twice counts as one vote; vote rate-limiting is inherent (one vote per turn); votes are logged with structured records so an adversarial pattern is visible in audit. Layer 5 (moderator) is the canonical defense if vote-spam becomes a real problem; until then the cheap mitigations are sufficient.

### I. Layer 5 — Moderator role

**Why.** Sub-problem (d) — "is the discussion productive?" — requires reading the transcript. Only an LLM can do that. A moderator is the right abstraction because the question is genuinely transcript-level, not per-message; it's a different judgement from "should I reply?"

**Mechanism.** A new logical role: a **moderator agent** registered against a channel via `channels.yaml: moderator_agent_id`. The moderator is not a participant — it doesn't send messages into the channel — but it does subscribe to events. Every N turns within an open Interaction (default `moderator_interval_turns=5`), the orchestrator wakes the moderator with a `MODERATE_INTERACTION` event containing the interaction transcript and asks for a decision:

```python
class ModerationDecision(StrEnum):
    CONTINUE = "continue"      # keep going
    WRAP_UP = "wrap_up"        # close after current depth completes
    TERMINATE = "terminate"    # close now; drop in-flight fanout
```

**Bounds on the moderator.**
- **Cost.** Lease (RFC 0023) with `cause=CAUSE_CHANNEL_MESSAGE` and a sub-budget separate from the conversation's main budget — the moderator's spend is attributed but does not eat the participants' budget.
- **Latency.** Hard deadline (default 5s). On timeout, decision defaults to `CONTINUE` (fail-open to keep traffic flowing; Layer 0/1/2 still bound the worst case).
- **Failure isolation.** A wedged moderator must not block the publish hot path. Moderator decisions are advisory until they complete; in-flight publishes proceed under Layers 0–4 alone.

**Trust model.** The moderator is a trusted principal — it can close interactions, but it cannot send messages, escalate permissions, or alter participant state. Cross-references [RFC 0009](0009-security-sandboxing.md) deny-by-default permissions: the moderator role gets a narrow capability set (`channels.read_history`, `channels.close_interaction`); nothing else.

**Wake mechanism.** Composes naturally with [RFC 0024 Event-Driven Scheduling](0024-event-driven-scheduling.md): "a moderator wake every N turns" is exactly the salience-triggered wake pattern RFC 0024 proposes. The moderator should land *after* RFC 0024 lands, not before — polling-based wake would re-introduce the cost class RFC 0024 closes.

**Decision auditability.** Each moderator decision emits a [RFC 0028 `DecisionRecord`](0028-agent-decision-policy-engine.md) with the decision class `moderate_interaction`, candidate set `{continue, wrap_up, terminate}`, the chosen action, and a one-sentence rationale. This makes moderator behaviour replayable and calibratable per RFC 0028's offline-tuning loop.

**Suggested moderator prompt shape** (illustrative, not normative):

> You are a meeting facilitator. Read the transcript. Decide:
> - `CONTINUE` if participants are still introducing new information or refining positions.
> - `WRAP_UP` if positions have converged but a summary hasn't been stated.
> - `TERMINATE` if the discussion is looping, off-topic, or producing no new content.
> Answer with one word and a one-sentence rationale.

### J. Layer 6 — Declarative conversation types

**Why.** Operators shouldn't have to discover good defaults for "max replies per participant in a brainstorm" vs "in an incident response" by trial and error. Known patterns ship with sensible defaults that can be overridden per channel.

**Mechanism.** A new optional field on the channel config:

```yaml
# config/channels.yaml
channels:
  - name: incident
    conversation_type: incident
    members:
      - {id: oncall, respond: always}
      - {id: postmortem, respond: when_mentioned}
```

Each `conversation_type` is a named bundle of defaults for Layers 1, 2, 5:

| Type | Reply budget / participant | Cost ceiling (tokens) | Moderator interval | End-vote threshold |
|------|----------------------------|-----------------------|--------------------|--------------------|
| `open` (default) | unlimited | uncapped | disabled | 2 / 3 turns |
| `brainstorm` | 5 | 50k | 5 turns | 2 / 3 turns |
| `design_review` | 8 | 100k | 5 turns | 3 / 5 turns |
| `incident` | unlimited | 200k | disabled (humans drive) | 3 / 5 turns |
| `retro` | 4 | 30k | 3 turns | 2 / 3 turns |

Numbers above are illustrative starting points, not normative — calibration is an Open Question ([§OQ-5](#open-questions)) gated on observed-workload data, not calendar dates.

Per-channel `channels.yaml` overrides take precedence; the type just sets defaults.

**Lifecycle phases (deferred).** A richer extension would attach explicit phases to a conversation type ("design_review: scoping → critique → decision → action items"), with budgets per phase and explicit phase transitions emitted by the moderator or by agent actions. This is intentionally out of scope for v0.5.0 — a flat conversation type captures 90% of the value without committing to the workflow shape.

### K. Integration with RFC 0028 (Decision Engine)

[RFC 0028](0028-agent-decision-policy-engine.md) defines `pre-act` as the decision checkpoint where the agent chooses among action classes including `publish_channel`. This RFC's Layers 1–5 slot into RFC 0028's **guardrail stage** ([RFC 0028 §C](0028-agent-decision-policy-engine.md)):

```
RFC 0028 pre-act pipeline:
  Build candidates                    ← agent generates {respond, defer, end_vote, abstain}
  Apply hard constraints              ← THIS RFC's Layers 1, 2 + RFC 0009 perms
  Score remaining candidates          ← RFC 0028 policy
  Route through approval gate         ← RFC 0028 §H if applicable
  Execute                             ← publish via RFC 0011 router
  Persist DecisionRecord              ← includes governance-layer attribution
```

**`end_interaction_vote` becomes a candidate action class.** When budget is low or no new information has emerged, the policy can rank `end_interaction_vote` ahead of `publish_channel` — the choice is policy, the budget is constraint, the cap is hard.

**No new decision-engine surface.** This RFC does not need RFC 0028 fully landed to ship Phase 1 — Layer 4's end-vote can be a plain action; Layer 5's moderator can hand-write its records pending the unified `DecisionRecord` shape. When RFC 0028 lands, the records consolidate without behaviour change. This RFC is therefore not gated on RFC 0028.

### L. Telemetry and observability

New metrics (RFC 0019 naming):

| Metric | Type | Labels | Meaning |
|--------|------|--------|---------|
| `channel.conversation.governance_drop` | Counter | `layer`, `channel_type` | Publishes dropped per layer. `layer ∈ {depth, cost, reply_budget, end_vote, moderator}`. |
| `channel.conversation.moderator_decision` | Counter | `decision`, `channel_type` | `decision ∈ {continue, wrap_up, terminate, timeout}`. Pairs with the `DecisionRecord` audit stream. |
| `channel.conversation.end_vote_emitted` | Counter | `channel_type` | One increment per vote action. |
| `channel.conversation.interaction_closed` | Counter | `trigger`, `channel_type` | `trigger ∈ {idle, structural, end_votes, moderator, cost}`. |
| `channel.conversation.reply_budget_remaining` | Histogram | `channel_type` | Per-participant remaining budget at interaction close — tail histogram diagnoses too-tight budgets. |
| `channel.conversation.cost_tokens_per_interaction` | Histogram | `channel_type` | Total lease tokens spent per closed interaction — informs Layer 1 calibration. |

Structured logs on every drop carry `channel_id`, `interaction_id`, `participant_id` (where applicable), and the layer-specific reason — same shape as the existing `channels: cascade limit reached` line.

Trace correlation: every governance drop emits a span attribute `conversation.governance.layer=<layer>` on the publish span, so an operator querying for "all publishes dropped by Layer 2 in #planning today" is one Jaeger/Tempo query, not a log grep.

### M. Wire and config surfaces

**Publish metadata bag** (REST `publishMessageRequest.metadata`):

Extending the existing `messageMetadata` definition in `schemas/channel.schema.json` (already used for `cascade_depth`):

| Key | Type | Direction | Purpose |
|-----|------|-----------|---------|
| `interaction_id` | string (ULID) | publisher → orchestrator | Scope binding (RFC 0020 — already implicit; pinning on the wire makes cross-process scope explicit) |
| `cascade_depth` | int | publisher → orchestrator | Layer 0 (shipped) |
| `end_interaction_vote` | bool | publisher → orchestrator | Layer 4 vote signal (alternative to a dedicated action type — Open Question §OQ-4) |

**Proto `ChannelMessageEvent`** (gRPC fanout):

Adds `string interaction_id = 12` (next free field number after `cascade_depth=11` from the amendment). Typed scalar — same rationale as cascade_depth (no metadata map on the event).

**Config — `channels.yaml`**:

```yaml
max_channels: 50
max_cascade_depth: 5

# NEW: per-channel governance defaults
default_conversation_type: open
default_max_replies_per_participant: 0       # 0 = uncapped
default_interaction_budget_tokens: 0          # 0 = uncapped
default_moderator_interval_turns: 0           # 0 = disabled

channels:
  - name: planning
    conversation_type: design_review          # picks up Layer 6 defaults
    moderator_agent_id: alex-facilitator      # optional Layer 5
    members:
      - {id: alex, respond: always}
      - {id: jordan, respond: always}
      - {id: morgan, respond: when_mentioned}
```

**Backward compatibility.** All new fields have defaults that disable the new layer (`0` / `unset`), so a v0.3.0 `channels.yaml` continues to work unchanged. Operators opt in per channel.

### N. Failure modes

| Failure | Layer | Behaviour |
|---------|-------|-----------|
| Wallet (Layer 1) RPC times out | 1 | Lease fail-closed per RFC 0023 §F — no LLM call happens; fanout terminates. The depth cap (Layer 0) is the still-running safety net. |
| Reply-budget counter lost on orchestrator restart | 2 | New interaction starts on next turn (RFC 0020 §C); budget resets. Acceptable. |
| Moderator agent unreachable | 5 | Hard deadline (5s) → default `CONTINUE`; structured log + `moderator_decision{decision=timeout}` counter increment. Lower layers still bound the worst case. |
| Moderator returns malformed decision | 5 | Treated as timeout → default `CONTINUE`; same log + counter. |
| End-vote spam from one participant | 4 | Votes are per-(participant_id, interaction_id) — duplicates collapse. Adversarial pattern visible in `end_vote_emitted{channel_type}` rate per participant. |
| Adversarial publisher exhausts cost ceiling to silence a channel | 1 | Documented in Security Considerations. Mitigation: lease attributes spend per-participant via `agent_id`; per-participant sub-caps are a follow-up. |
| Race: two publishes both pass the reply-budget check then both increment | 2 | Best-effort — the budget is a soft target, not a hard transaction. Worst case: budget off by one. Layer 0/1 still bound. |
| Interaction never closes (no idle, no votes, no moderator) | 0 | Depth cap fires every chain; cost ceiling fires eventually. The Interaction stays open but useful work cannot continue past Layer 0. Operator-visible via `interaction_closed{trigger}` *absence* alarming. |

---

## Security Considerations

- **Moderator authorization.** The moderator gains a `channels.close_interaction` capability — a new permission per [RFC 0009](0009-security-sandboxing.md). Deny-by-default applies: only the channel's declared `moderator_agent_id` may close the channel's open interactions; any other principal attempting `close_interaction` is rejected and audited.
- **Vote spam / denial-of-service.** A misbehaving participant could try to silence a channel by sending `END_INTERACTION_VOTE` every turn. Mitigations: votes deduplicate per (participant, interaction); a participant who emits votes with no message content has nothing to gate; per-participant vote rates are observable; the response gate already drops `respond: never` members so a non-participant cannot vote.
- **Cost-ceiling exhaustion as silencing.** A publisher who can issue many cheap publishes could exhaust the per-interaction budget so legitimate replies can't acquire leases. Mitigations: lease accounting is per-agent (per RFC 0023), so the spammer's own future leases are denied first; per-participant sub-caps are a follow-up; existing RFC 0009 Phase 1 rate limiting applies to publishes irrespective of budget.
- **Moderator prompt injection.** The moderator reads a transcript that may contain hostile content. Treated as the same attack surface as memory content per [RFC 0011 §Security](0011-channels-bridges.md): RFC 0009 input sanitisation applies before transcript is materialised; the moderator's output is constrained to a closed enum + rationale, so even a successful injection cannot trigger arbitrary actions.
- **Audit integrity.** Moderator `DecisionRecord` and governance drops feed [RFC 0009 audit logging](0009-security-sandboxing.md) — the audit chain integrity contract carries forward.
- **No new principal escalation.** The moderator is a narrowly-scoped role, not a privileged participant. It cannot send messages, change membership, or issue leases against another agent.

---

## Phased Implementation Plan

Phasing matches the user's request: Phase 1 in v0.3.x, Phase 2 in v0.4.0, Phase 3 in v0.5.0+. Phases ship independently; each is useful on its own.

### Phase 1 — Deterministic layers (v0.3.x)

**Summary.** Layers 1, 2, 4 — the cheap, deterministic, fail-safe-by-construction layers. No new LLM-judgement code paths; opt-in via config.

**Deliverables.**
1. `interaction_id` propagation on the wire (REST metadata bag + proto field #12). Mirror the cascade-depth amendment pattern.
2. Per-interaction lease budget on `WalletService.AcquireLease` (RFC 0023 extension): new `interaction_id` and `interaction_budget_tokens` request fields; new `INTERACTION_BUDGET_EXHAUSTED` denial reason. Gated on RFC 0023 Phase 1 landing; until then, Layer 1 is a no-op (proto fields reserved).
3. Per-participant reply budget on the orchestrator. New `interactionReplyBudget` in-memory tracker keyed by RFC 0020 `interaction_id`. Publish-time enforcement, pre-persistence rejection with HTTP 429 + `ErrParticipantBudgetExhausted`.
4. `END_INTERACTION_VOTE` agent action + per-Interaction vote accumulator. K and W configurable per channel; defaults K=2, W=3.
5. Telemetry: `channel.conversation.governance_drop{layer}`, `interaction_closed{trigger}`, `end_vote_emitted` counters.
6. Operator-facing doc updates: `docs/guides/channels.md` §"Conversation governance" — new subsection covering all of Phase 1.

**Dependencies.** RFC 0011 amendment (shipped); RFC 0020 Interaction (shipped). Layer 1 (lease budget) soft-depends on RFC 0023 landing — until then the field is wire-reserved and inert.

### Phase 2 — Moderator role (v0.4.0)

**Summary.** Layer 5 — a pluggable moderator agent reading the open-interaction transcript every N turns and emitting continue/wrap_up/terminate decisions.

**Deliverables.**
1. `moderator_agent_id` channel config field and orchestrator-side wake plumbing. Moderator wakes via RFC 0024's salience-triggered scheduler — Phase 2 is gated on RFC 0024 Phase 1.
2. `MODERATE_INTERACTION` event type carrying the interaction transcript.
3. Moderator-side capability whitelist (`channels.read_history`, `channels.close_interaction`) — gated on RFC 0009 Phase 4 capability framework.
4. Decision audit via RFC 0028 `DecisionRecord` — gated on RFC 0028 Phase 1.
5. Telemetry: `channel.conversation.moderator_decision{decision}` counter; histogram of moderator latency.
6. Manual test: re-run F-1 two-`always`-member scenario with moderator enabled; assert moderator terminates productive convergence by depth 3–4, well below the Layer 0 cap.

**Dependencies.** RFC 0024 (wake mechanism); RFC 0009 Phase 4 (capability framework); RFC 0028 Phase 1 (decision records). Each is a soft-dependency — Phase 2 can ship behind a feature flag without all of them, with degraded auditability.

### Phase 3 — Declarative types + drift detection (v0.5.0+)

**Summary.** Layer 6 — named conversation types with calibrated defaults; topic-drift detection wired to RFC 0020's scaffolded `TopicShiftDetector`.

**Deliverables.**
1. `conversation_type` field on channel config; type→defaults bundles (open / brainstorm / design_review / incident / retro).
2. Per-type recommended defaults committed once observed-workload data exists — calibration is gated on data, not dates.
3. Topic-drift detector implementation behind a feature flag (composes with RFC 0020 §B's existing scaffolding).
4. Per-conversation-type phase declaration is intentionally out of scope; revisit after Phase 3 ships.

**Dependencies.** Phase 1 (config surfaces); RFC 0020 topic-drift scaffolding (shipped).

---

## Files Touched (Estimated)

Estimates are scope sketches, not commitments. Final per-PR plans land in `docs/rfcs/0030-pr-plan.md` once this RFC is accepted.

| Component | Files | Phase | Change |
|-----------|-------|-------|--------|
| Go orchestrator | `internal/channels/router.go`, `internal/channels/cascade_depth.go`, new `internal/channels/conversation_governance.go` | 1 | Reply-budget tracker; vote accumulator; interaction-id propagation |
| Go orchestrator | `internal/server/channel_handlers.go`, `internal/server/channel_cascade_depth.go` (extend) | 1 | REST metadata for `interaction_id`, `end_interaction_vote` |
| Protos | `proto/task.proto` | 1 | `string interaction_id = 12` on `ChannelMessageEvent`; extend `AcquireLeaseRequest` with `interaction_id`, `interaction_budget_tokens` (gated on RFC 0023) |
| Schemas | `schemas/channel.schema.json` | 1 | New keys on `messageMetadata`: `interaction_id`, `end_interaction_vote` |
| Python agents | `agents/dispatch.py`, `agents/action_executor.py`, `agents/channel_publisher.py` | 1 | Emit `END_INTERACTION_VOTE`; consume `interaction_id` on inbound events |
| Python agents | New `agents/moderator/` package | 2 | Moderator runtime (separate from persona runtime) |
| Config | `config/channels.yaml` template | 1, 3 | New fields: `max_replies_per_participant_per_interaction`, `interaction_budget_tokens`, `moderator_agent_id`, `conversation_type` |
| Observability | `internal/observability/metrics/channel_instruments.go` | 1, 2 | New counter / histogram instruments |
| Docs | `docs/guides/channels.md` | 1, 2 | New "Conversation governance" section |
| Docs | `docs/rfcs/0030-pr-plan.md` | 1 | Phase 1 PR plan |

---

## Test Strategy

**Unit (Phase 1).**
- Reply-budget tracker: increment / over-cap / interaction-reset / sender-self counting.
- Vote accumulator: K distinct votes / vote dedup / W-window expiry.
- `interaction_id` propagation: REST → router → gRPC dispatch end-to-end shape, including `float64`-from-JSON parsing parity with the cascade_depth helper.
- Lease-budget extension: per-interaction balance maths; exhaustion produces `INTERACTION_BUDGET_EXHAUSTED` denial.

**Integration (Phase 1).**
- Two-`always`-member channel: declare `max_replies_per_participant=3`; publish a user prompt; assert the cascade terminates at total ≤ 6 dispatches (3 each).
- End-vote: simulate two agents each emitting `END_INTERACTION_VOTE` within 3 turns; assert interaction closes and no further fanout fires.
- Cost ceiling: declare `interaction_budget_tokens=1000`; assert later leases in the same interaction are denied once the running total crosses 1000.

**Integration (Phase 2).**
- Moderator scenario: 4-agent design-review channel; moderator wakes every 5 turns; assert moderator returns `TERMINATE` once transcript shows obvious convergence (test seeded with a script transcript).
- Moderator timeout: stub moderator that blocks 10s; assert publish-side default-`CONTINUE` fires within deadline; assert telemetry counter `moderator_decision{decision=timeout}` ticks.

**Manual (Phase 1).**
- Re-run the v0.3.0 F-1 scenario (two `always` personas, single user prompt) with `max_replies_per_participant_per_interaction=3` configured. Expected: cascade terminates within ~6 LLM calls instead of ~60. Record findings in `docs/manual-tests/MT-CHANNEL-GOV-001.md`.

**Manual (Phase 2).**
- Same scenario with moderator enabled and reply budget uncapped. Expected: moderator decides `TERMINATE` by turn ~3–4. Compare cost against Phase 1 manual baseline.

---

## Open Questions

1. **OQ-1: Where does the reply-budget tracker live?** In-process orchestrator map (simplest, lost on restart, matches RFC 0020's "restart loses open-interaction state" stance) vs durable store (survives restart, adds a write-side hot-path cost). *Lean: in-process.* The RFC 0020 precedent argues for accepting the restart gap.

2. **OQ-2: Moderator authorship — overlay or distinct principal?** Option A: a persona that has the moderator capability (lower setup cost, but persona has memory and could "remember" past conversations in ways that bias moderation). Option B: a dedicated stateless principal type (cleaner trust boundary, more wiring). *Lean: Option B,* but Option A is acceptable if Phase 2's first cut wants to compose with existing persona infrastructure.

3. **OQ-3: Cost-ceiling semantics — hard fail or soft warning?** Hard fail (no further LLM calls in the interaction) matches RFC 0023's fail-closed contract and is the safe default. Soft warning (continue but emit a counter) would let operators discover the ceiling without dropping conversations on the floor. *Lean: hard fail* in Phase 1; revisit if operators ask for soft mode.

4. **OQ-4: End-of-interaction signal — explicit action (`END_INTERACTION_VOTE`) or metadata bag (`metadata.end_interaction_vote=true`)?** Explicit action is more visible in audit trails and decision records (RFC 0028); metadata bag is cheaper to add and composes with existing `metadata.cascade_depth` shape. *Lean: explicit action* — the auditability win is worth the wire-shape cost.

5. **OQ-5: Default values for Layer 1 / 2 / 5 are not yet derivable.** "What's a reasonable per-participant reply budget for a `design_review`?" needs observed-workload data from real channel usage. The RFC commits to *uncapped* defaults at v0.3.x ship time so the layers are opt-in; the recommended values in [§J](#j-layer-6--declarative-conversation-types) are starting points to validate, not normative.

6. **OQ-6: Moderator wake cadence — fixed `every_N_turns` or salience-triggered via RFC 0024?** Fixed is simpler; salience composes better with the event-driven scheduler RFC 0024 introduces. *Lean: fixed in Phase 2 first cut, migrate to salience once RFC 0024 lands.* Avoids gating Phase 2 on Phase 1 of an unrelated RFC.

7. **OQ-7: Does chat-as-DM ([RFC 0011 amendment 'Chat as DM'](0011-amendment-chat-as-dm.md)) count as a governed conversation?** A DM with one user and one persona is technically a channel, has an Interaction, and could in principle be governed. But the human user is unbounded — capping the user's reply budget would be silly. *Lean: governance enabled on DMs but `max_replies_per_participant` is enforced only against non-human principals.* Surface this as an explicit config: `governance.exempt_principals: [human]`.

8. **OQ-8: Backward-compatibility — opt-in or opt-out for Layer 2?** Uncapped reply budget is the v0.3.0 status quo; opting in via config is the safest path. The trade-off is that operators who would benefit from Layer 2 must explicitly turn it on. *Lean: opt-in for v0.3.x, revisit for v0.4.0 once the moderator role gives operators a clearer mental model.*

9. **OQ-9: Should `END_INTERACTION_VOTE` count as one of the agent's reply-budget turns?** Voting "I'm done" without saying anything else is a turn for accounting purposes but produces no message content. Counting it consumes budget; not counting it lets a near-budget agent vote for free. *Lean: count it* — the budget is about agent time on the floor, not content volume.

10. **OQ-10: Cross-RFC coordination — does this RFC's `interaction_id` wire field overlap with anything RFC 0020 plans for Phase 2+?** RFC 0020 currently keys interactions in-process per agent; it has no wire surface for `interaction_id` of its own. This RFC is therefore the proposed introducer. If RFC 0020 grows a wire surface independently, the two must reconcile — flagged as a hard dependency for Phase 1 commit.

---

## Decision / Next Steps

This RFC is a **draft for discussion**. Before any PRs land:

1. Review and discuss the layered architecture and the cleanness of decomposition onto existing RFCs (0011, 0020, 0023, 0028).
2. Resolve OQ-1 (tracker durability), OQ-3 (cost-ceiling semantics), OQ-4 (vote shape), OQ-10 (RFC 0020 coordination) — these gate Phase 1's PR plan.
3. Open `docs/rfcs/0030-pr-plan.md` once the design is accepted.
4. Confirm Phase 2 scope after RFC 0024 (event-driven scheduling) lands enough infrastructure to host the moderator wake — Phase 2 should not race RFC 0024 Phase 1.

**Status flip path.** 📋 Proposed (Draft) → 📋 Proposed (Accepted) once OQs above are resolved → 🚧 In Progress on Phase 1 PR plan merge → ✅ Implemented per phase as PRs land.

---

## Related Documentation

- [RFC 0011 — Channels & Internal Agent Messaging](0011-channels-bridges.md) — the channels stack this RFC governs.
- [RFC 0011 Amendment — Cascade-Depth Wire Propagation](0011-amendment-cascade-depth-wire-propagation.md) — Layer 0 of this RFC.
- [RFC 0030 Amendment — Floor Control / Speaker Serialization](0030-amendment-floor-control-speaker-serialization.md) — Layer 2.5; the ordering half of sub-problem (c), brought forward to v0.3.6. Its [PR plan](0030-amendment-floor-control-pr-plan.md) is the implementation workstream.
- [RFC 0020 — Interaction Lifecycle](0020-interaction-lifecycle.md) — defines the scope ("conversation") this RFC governs.
- [RFC 0023 — LLM Call Leasing](0023-llm-call-leasing.md) — provides the cost-ceiling primitive (Layer 1).
- [RFC 0024 — Event-Driven Agent Scheduling](0024-event-driven-scheduling.md) — provides the moderator wake mechanism (Phase 2).
- [RFC 0028 — Agent Decision Policy Engine](0028-agent-decision-policy-engine.md) — defines the `pre-act` checkpoint this RFC's layers slot into.
- [v0.3.0 Channel Test-Findings PR Plan](../v0.3.0-test-findings-pr-plan.md) — F-1 (the empirical motivation) and its tail ("Cost-ceiling enforcement" follow-up).
- [Channels operator guide](../guides/channels.md) — operator-facing landing point for governance configuration.
