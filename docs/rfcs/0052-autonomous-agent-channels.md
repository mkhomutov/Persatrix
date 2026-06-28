---
# Allowed values are documented in README.md. The YAML front-matter is the
# source of truth read by `scripts/rfcs.py` to regenerate INDEX.md — keep
# it in sync with the bold-markdown header below (which is what GitHub
# renders for human readers).
id: RFC-0052
title: "Autonomous Agent-Only Channels"
summary: "A channel that convenes a set of personas on a topic and runs a productive, human-free discussion that converges, terminates, and yields a readable synthesis — the human-out-of-the-loop capstone of the v0.3.x realism arc. Reuses the shipped channel/governance/scheduling/reasoning seams; the genuinely new content is self-convening, anti-collapse cadence (counter-pressure to the bias-to-silence defaults), and a mandatory cost cap."
type: feature
status: proposed
author: Maksim Khomutov
created: 2026-06-28
target: "v0.3.x (next realism rung — candidate; see docs/v0.3.x-sequencing.md Amendment 2026-06-28)"
depends_on:
  - RFC-0011
  - RFC-0030
  - RFC-0024
  - RFC-0050
  - RFC-0051
  - RFC-0020
  - RFC-0023
  - RFC-0033
---

# RFC 0052 — Autonomous Agent-Only Channels

**Type**: feature
**Status**: 📋 Proposed
**Author**: Maksim Khomutov
**Date**: 2026-06-28
**Target**: v0.3.x (next realism rung — candidate; sequenced by [v0.3.x-sequencing Amendment 2026-06-28](../v0.3.x-sequencing.md#amendment-2026-06-28--add-the-autonomous-agent-only-channel-as-the-v03x-realism-capstone))
**Depends on**: RFC 0011 (channels), RFC 0030 (conversation governance), RFC 0024 (event-driven scheduling), RFC 0050 (channel configuration), RFC 0051 (reasoning before posting), RFC 0020 (interaction lifecycle), RFC 0023 (LLM call leasing — the mandatory cost cap is enforced as a lease), RFC 0033 (provider-agnostic alias layer — per-seat vendor selection). The headline four-vendor [Phase 4](#phase-4-flagship-demo) demo additionally requires [RFC 0053](0053-gemini-watsonx-providers.md) (bundled, not a hard dependency — RFC 0052 ships on any single provider).

---

## Table of Contents

- [Summary](#summary)
- [Motivation](#motivation)
- [Goals](#goals)
- [Non-Goals](#non-goals)
- [Design / Implementation](#design--implementation)
  - [A. What already exists (the substrate)](#a-what-already-exists-the-substrate)
  - [B. Self-convening — starting without a human turn](#b-self-convening--starting-without-a-human-turn)
  - [C. The central tension — anti-collapse cadence](#c-the-central-tension--anti-collapse-cadence)
  - [D. Termination and synthesis — always produce an artifact](#d-termination-and-synthesis--always-produce-an-artifact)
  - [E. Standing and scheduled discussions](#e-standing-and-scheduled-discussions)
  - [F. Scope line — brainstorm now, decision later](#f-scope-line--brainstorm-now-decision-later)
- [Security Considerations](#security-considerations)
- [Phased Implementation Plan](#phased-implementation-plan)
- [Files Touched (Estimated)](#files-touched-estimated)
- [Test Strategy](#test-strategy)
- [Open Questions](#open-questions)
- [Decision / Next Steps](#decision--next-steps)
- [Related Documentation](#related-documentation)

---

## Summary

An **autonomous agent-only channel** is a channel that convenes a roster of personas on a stated topic and runs a productive discussion **with no human in the loop** — no human seeds it, no human keeps it alive, no human ends it. The discussion **converges, terminates, and yields a readable synthesized outcome**, exactly as the v0.3.8 *brainstorm* rung does, but with the human removed from every step.

This is the human-out-of-the-loop **capstone of the v0.3.x realism arc** (v0.3.7→v0.3.10). That arc proved personas can converse like colleagues, converge on a result, quote each other, and think before they speak — but every rung was validated *with a human seeding the topic and implicitly steering*. Removing the human is both the honest proof that the arc works and the single best adoption demo: *give five personas a topic and watch them discuss it and produce something worth reading, untouched.*

Most of the machinery already ships. The genuinely new design content is small but **not zero** — and each piece *extends a shipped governance invariant rather than merely toggling it*, so it is new mechanism, not config: **self-convening** (an opening without a human turn), **anti-collapse cadence** (counter-pressure to the bias-to-silence defaults the realism arc deliberately installed — concretely, a generalization of the RFC 0030 *one-escalation-per-interaction* chair ration to a *per-agenda-item* ration, so the discussion advances rather than dying on round one), an optional **standing/scheduled** convening trigger, and a **mandatory cost cap with a reserved synthesis allowance and a new deterministic bounded close** (there is no human to notice a runaway loop, and — because the shipped wallet fails leases closed and the chair cannot close an interaction — neither the synthesis nor the close comes for free).

## Motivation

The [usefulness ladder](../v0.3.x-sequencing.md#the-usefulness-ladder) defines minimal usefulness as a *brainstorm* that converges, terminates, and yields a readable synthesis — shipped in v0.3.8. But the whole v0.3.x realism arc was exercised through a human participant:

1. **A human seeds the topic.** Today a discussion starts because a human (or operator) posts the first message into a channel. There is no first-class "convene these agents on this topic and let them run" primitive that begins without a human turn.
2. **A human implicitly keeps it alive.** The realism arc optimized hard for *bias-to-silence and converge-then-terminate*: the RFC 0030 relevance gate suppresses off-target turns, the RFC 0051 semantic-silence verdict is **on by default** once a channel is governed, and the RFC 0030 chair stall-escalation forces one chair turn whose job is to **close** a stalled interaction. With a human present, the human re-prompts and the channel stays productive. Remove the human and those same forces can collapse the discussion to silence almost immediately — there is no one left to revive it.
3. **A human watches for runaway cost.** The v0.3.2 lease and the v0.3.8 cost ceiling make spend *attributable and capped per interaction*, but the human is still the backstop who notices a misbehaving channel and stops it. An unattended agent-only channel has no such backstop.

Doing nothing leaves Persatrix able to demonstrate good conversation only when a human is in the chair — which undersells the realism work and gives a prospective user no "watch it run by itself" moment before the project pivots to organizations (v0.4.0). The substrate to close this is almost entirely shipped (see [§A](#a-what-already-exists-the-substrate)); this RFC assembles it and adds the three missing pieces.

## Goals

1. **Self-convening.** A channel can be marked autonomous with a topic/agenda and a convener; the discussion opens on a seeded first turn with **no human message** required.
2. **Human-free liveness.** Once opened, the discussion sustains itself through agent-to-agent wakes and an **anti-collapse cadence** that supplies counter-pressure to the bias-to-silence defaults, so it advances rather than dying on the first quiet round.
3. **Converge, terminate, synthesize.** The discussion reaches a goal/round bound, closes through the existing RFC 0030 end-of-interaction path, and **always emits a readable synthesis** (the artifact) — never an open-ended, never-terminating loop.
4. **Mandatory cost cap.** An autonomous channel cannot be created without an enforced per-interaction spend ceiling (RFC 0023 lease + RFC 0030 Layer 1 + RFC 0050 interaction budget). Uncapped autonomy is rejected at config-validation time.
5. **Optional standing/scheduled discussions.** A channel can be convened (or re-convened, or have its agenda advanced) on an RFC 0024 timer — "the panel deliberates topic X every morning."
6. **A one-command demo.** `make demo-autonomous` boots a curated roster on a topic and shows the whole arc — convene → discuss → converge → synthesize — with zero human input.

## Non-Goals

- **Reasoning toward a *decision*.** The deliberation-to-a-justified-recommendation rung is the v0.4.x [decision engine (RFC 0028)](0028-agent-decision-policy-engine.md). This RFC is scoped to the **brainstorm** rung (discuss → converge → synthesize), achievable on today's substrate. See [§F](#f-scope-line--brainstorm-now-decision-later).
- **Changing the human-channel defaults.** The bias-to-silence behavior (RFC 0030 relevance gate, RFC 0051 semantic silence) stays exactly as shipped for ordinary human-in-the-loop channels. Anti-collapse pressure applies only when a channel is marked autonomous.
- **Organizational roles, authority, or clearance** (RFC 0012, v0.4.0) — an autonomous channel is a flat roster, not an org.
- **External bridges** (Slack/Discord/email — RFC 0011 external, v0.5.0). Autonomous channels are internal.
- **A new moderator subsystem.** This reuses the v0.3.8 `chair` disposition and the minimal Layer-5 stall slice; the full RFC 0030 Phase 2 moderator stays v0.4.0.

## Design / Implementation

### A. What already exists (the substrate)

The point worth making first: this is **assembly plus three small new pieces**, not a new subsystem — but two of those pieces *modify shipped governance invariants* (spelled out in [§C](#c-the-central-tension--anti-collapse-cadence) and [§D](#d-termination-and-synthesis--always-produce-an-artifact)), so they are genuinely new mechanism, not configuration over an untouched substrate. The following already ship and are directly reusable:

| Capability | Source | Reuse here |
|------------|--------|------------|
| Agent-to-agent channels with **no human participant** work at the session/memory layer | RFC 0011 + the [RFC 0031 scope-axes review](../memory-scope-axes.md) (the "channel with no human participant" case is explicitly handled) | The channel itself needs no new isolation work |
| One persona's post **wakes the others** (no poll) | RFC 0024 `InboundEventWake`; scheduled `timers` | Free agent-to-agent turn propagation; timers drive [§E](#e-standing-and-scheduled-discussions) |
| Floor control / speaker serialization | RFC 0030 Layer 2.5 (v0.3.6) | One speaker at a time, no stampede |
| Relevance gating, `chair` disposition, end-of-interaction vote, **stall escalation** | RFC 0030 Tier A/B + chair + the [chair-stall-escalation amendment](0030-amendment-chair-stall-escalation.md) (v0.3.7–0.3.8) | Convergence; [§C](#c-the-central-tension--anti-collapse-cadence) **generalizes** the shipped one-escalation-*per-interaction* ration (CE5) to per-agenda-item; [§D](#d-termination-and-synthesis--always-produce-an-artifact) works around CE4 ("a chair cannot close") |
| Reply budget, cost ceiling, interaction budget | RFC 0030 Layers 1/2 + RFC 0050 | The [§D](#d-termination-and-synthesis--always-produce-an-artifact) bound and the [Goal #4](#goals) cost cap |
| Reasoning before posting (semantic silence + considered compose) | RFC 0051 (v0.3.10) | Quality of each turn; also the *cause* of collapse risk |
| Interaction summary surface | RFC 0020 + v0.3.8 summary surface | The synthesized artifact [§D](#d-termination-and-synthesis--always-produce-an-artifact) |
| Per-channel, operator-editable config | RFC 0050 | Where the `autonomous` block lives |
| **Provider-agnostic personas** (each persona picks its model by alias) | RFC 0033 alias layer + [RFC 0053](0053-gemini-watsonx-providers.md) (Gemini + watsonx.ai) | Lets each seat run on a *different vendor* — the cross-vendor demo [§Phase 4](#phase-4-flagship-demo) |

The three things **not** covered by any shipped seam — and where the genuinely new mechanism lives: self-convening ([§B](#b-self-convening--starting-without-a-human-turn)); anti-collapse cadence ([§C](#c-the-central-tension--anti-collapse-cadence)), which generalizes the shipped CE5 one-escalation-per-interaction ration to a per-agenda-item ration **while preserving its anti-runaway loop guard**; and the mandatory-cap + reserved-synthesis + deterministic-bounded-close contract ([§D](#d-termination-and-synthesis--always-produce-an-artifact)) that makes unattended running safe (the shipped wallet fails leases closed, and per RFC 0030 CE4 *a chair cannot close an interaction* — so neither the synthesis nor the close is free reuse).

### B. Self-convening — starting without a human turn

An autonomous channel carries an `autonomous` block on its RFC 0050 config surface:

```yaml
# channel config (RFC 0050 config_overrides / config-as-code)
autonomous:
  enabled: true
  topic: "Should we adopt a monorepo? Lay out the tradeoffs."
  agenda:                       # optional ordered sub-topics for the chair to advance through
    - "Build tooling and CI cost"
    - "Cross-team coupling risk"
    - "Migration effort"
  convener: persona://chair      # the persona that authors the opening + advances the agenda
  goal: "A synthesized recommendation with the strongest argument on each side."
  max_rounds: 12                 # hard bound (also see budget cap, Goal #4)
  interaction_budget_tokens: 200000   # MANDATORY — config-validation rejects autonomy without a cap
```

**Convening** = the convener persona authors the **opening turn** (topic + first agenda item) as a normal channel publish, stamped with a fresh `interaction_id` (RFC 0030 producer). From that publish onward the existing `InboundEventWake` chain carries the discussion with no further human input. Convening is triggered three ways, all reusing existing surfaces:

- **CLI** — a new `persatrix channel convene <id>` verb (thin wrapper over the existing publish path).
- **Web console** — a "Convene" action in the Channel settings panel (RFC 0050 Phase 2 surface).
- **Timer** — an RFC 0024 scheduled wake on the convener ([§E](#e-standing-and-scheduled-discussions)).

No new transport, no new wake type, no new store table — convening is "author the seed turn under a fresh interaction id."

### C. The central tension — anti-collapse cadence

**This is the design heart of the RFC.** The v0.3.x realism arc deliberately installed *bias-to-silence and converge-then-terminate* pressure:

- RFC 0051 semantic silence is **on by default** (`mode: bid`) — a persona with "nothing to add" stays silent *with a reason*.
- The RFC 0030 [chair stall escalation](0030-amendment-chair-stall-escalation.md) (v0.3.8) forces **one** chair turn on a zero-reply round, whose prompt is *"state the synthesis and cast your end-vote, **or** name the member best placed to resolve what remains and ask them directly."* Two properties of the *shipped* mechanism matter here, and the naïve framing gets both wrong: it already *can* hand off (it is **not** close-only — outcome (b) is a directed re-invite), but it fires **at most once per interaction** — the CE5 loop guard, which exists precisely because "a synthesis that draws no reaction re-triggers detection every round: chair speaks to silence, forever."

With a human in the chair these are exactly right. **With no human they compose into premature death:** every persona reasons "the others can cover this," all stay silent, the single escalation fires, its handoff also draws silence (CE5 is now spent), and an unattended channel converges to a near-empty transcript before idle rotation buries it with no synthesis. Anti-collapse cadence is the counter-pressure that makes a human-free channel productive without un-doing the realism work — and it is **new mechanism, not a behavior toggle**, because making a multi-item agenda workable requires generalizing the shipped CE5 ration:

1. **The chair advances the *agenda*, not just the *question*.** The shipped escalation hands off *within* the open question ("who can resolve what remains"); an autonomous channel additionally needs the chair, on a stall with the agenda **not exhausted**, to **advance to the next agenda item** — pose the next sub-topic or a pointed question directed at a specific persona (NL addressing, RFC 0030 Tier B). This requires lifting CE5's **one-escalation-per-interaction** ration to a **per-agenda-item** ration: each item gets its own escalation budget, where today the whole interaction gets exactly one.
2. **The per-item loop guard is preserved, not removed.** CE5's anti-runaway purpose is non-negotiable in an unattended channel, so the per-item ration carries the same guard: a stall on item *N* escalates **once**; if that escalation also draws silence, the chair does not re-invite item *N* forever — it advances to item *N+1* (or, if the agenda is exhausted, proposes synthesis-and-close, [§D](#d-termination-and-synthesis--always-produce-an-artifact)). The chair never speaks twice into silence on the same item. This bounds total chair turns at **one per agenda item** — a hard, agenda-length-bounded ceiling, not an open loop. Generalizing CE5 this way is the load-bearing new work; "a behavior switch on the existing chair" understates it.
3. **A liveness floor.** A `min_substantive_turns_per_agenda_item` keeps the discussion from skipping an item on the first quiet round — the chair re-invites before the item's escalation ration is spent and it advances. The floor sets a per-item *minimum*; the per-item ration (item 2) sets the per-item *maximum*, so the two together bound each item between a known floor and ceiling.
4. **Silence stays semantic, not suppressed.** Anti-collapse does **not** lower the RFC 0051 silence threshold globally (that would bring back the pile-on the arc removed). It works by *giving the chair something concrete to ask*, which raises individual personas' salience honestly rather than forcing low-value "I agree" turns.

The explicit invariant: **anti-collapse pressure is scoped to `autonomous.enabled` channels.** Ordinary channels keep the shipped CE5 one-shot ration and bias-to-silence defaults untouched. This keeps the two opposing forces (bias-to-silence for human channels, keep-alive for autonomous ones) cleanly separated rather than globally re-tuned.

### D. Termination and synthesis — always produce an artifact

An autonomous channel **must** terminate and **must** leave a readable artifact. Two shipped invariants make this harder than "reuse the close path," and the RFC must respect both:

- **A chair cannot close an interaction.** RFC 0030 CE4 pins termination to the Layer-4 quorum end-vote (the chair *proposes* a synthesis; a second member's concurring vote *disposes*). Outside that, the only shipped terminator is **idle rotation** — a ~10-minute timeout that closes with **no** synthesis. Critically, hitting the cost ceiling does **not** close an interaction: per RFC 0030 it "stays open but useful work cannot continue" once leases are denied. So "two hard bounds the chair owns" is not achievable as stated — the chair owns neither.
- **The wallet fails leases closed.** The mandatory `interaction_budget_tokens` cap is enforced in the wallet ([RFC 0050 interaction-budget amendment](0050-amendment-interaction-budget-enforcement.md)): once the interaction is over budget, `AcquireLease` denies the next call. The chair's synthesis turn is itself a leased call (RFC 0030 CE6: "the lease denial fails the forced turn closed, the same as any turn"). So a budget-exhausted close would deny the very synthesis this RFC requires — the cap would eat the artifact.

The autonomous path therefore adds two **new, small, orchestrator-side** mechanisms — the part of [§D](#d-termination-and-synthesis--always-produce-an-artifact) that is *not* pure reuse:

1. **A deterministic bounded close.** A new orchestrator-side close trigger fires when the agenda is exhausted, *or* `max_rounds` is reached, *or* a **soft** budget threshold is crossed — distinct from the shipped quorum/idle paths, and distinct from the chair (which still cannot close, CE4 intact). On fire it dispatches the chair's synthesis turn and *then* closes the interaction, yielding the same artifact-bearing close the quorum path produces. `max_rounds` has no shipped enforcement today; this trigger is what adds it.
2. **A reserved synthesis allowance.** The cap is split: `interaction_budget_tokens` bounds the *discussion*, and a separate reserve (a fraction of the cap) is held back so the closing synthesis turn always has a lease, even when the discussion budget is spent. The **soft** threshold in (1) trips synthesize-and-close *before* the hard cap denies leases, and the reserve guarantees headroom for the synthesis call. Without this, the close-by-budget path degrades to the artifact-less idle close.

With both in place: **Termination** is bounded by agenda exhaustion, `max_rounds`, the soft budget threshold, or the natural quorum end-vote — whichever fires first — and there is no path to an unbounded loop (the per-item escalation ration of [§C](#c-the-central-tension--anti-collapse-cadence) bounds chair turns; these triggers cap the rest). **Synthesis** reuses the RFC 0020 / v0.3.8 interaction-summary surface but is **mandatory**: on every close path the chair emits a synthesis turn (against the `goal`) from the reserved allowance, and the interaction summary is the artifact a human reads later. An autonomous interaction that closes without a synthesis is a failure the acceptance MT checks for — explicitly including the **close-by-budget** path, which is exactly where the reserve earns its keep.

### E. Standing and scheduled discussions

Reusing RFC 0024 `timers`, the convener can be woken on a schedule to **open** a fresh interaction (a standing panel that deliberates a new topic each morning) or to **advance** a long-running one. The timer fires a convener wake; the convener authors the opening/advancing turn exactly as in [§B](#b-self-convening--starting-without-a-human-turn). Two caveats keep this honest:

- **Not quite "no new scheduling primitive."** RFC 0024 timers are **`agents.yaml`-canonical** (the per-agent `scheduled_wakes` SQLite table is a *derived cache* rebuilt from config), and RFC 0024 explicitly **deferred runtime timer mutation** ("a `RegisterTimer()` API … defer until a use case appears"). `autonomous.schedule` lives on the *runtime-editable* RFC 0050 **channel** surface, so wiring it to a convener wake needs either (a) a config round-trip into the convener's `agents.yaml` timer set, or (b) the deferred runtime-registration API — a small but genuinely new seam, not free reuse. Phase 3 picks one (see [OQ #4](#open-questions)).
- **The per-interaction cap does not bound a standing schedule.** The mandatory `interaction_budget_tokens` caps each *interaction*; a recurring convener with no human re-opens a fresh (separately capped) interaction indefinitely, so total spend over a standing schedule is **unbounded by the per-interaction cap alone**. A standing channel therefore additionally requires an **aggregate bound** — a `max_convenings` count, a standing-window cost budget, or both — validated at config time the same way the per-interaction cap is ([§Security](#security-considerations), [Goal #4](#goals)). Phase 3 ships this *with* the schedule, not after it.

No new wake *type* — `autonomous.schedule` is an RFC 0024 timer spec plus a topic source plus the standing aggregate bound.

### F. Scope line — brainstorm now, decision later

The autonomous channel is scoped to the **brainstorm** rung: discuss → converge → synthesize a readable outcome. The **reason-toward-a-justified-decision** version — where the roster weighs tradeoffs and emits an auditable recommendation — is gated on the [RFC 0028 decision engine](0028-agent-decision-policy-engine.md) and stays **v0.4.x**, consistent with the [usefulness ladder](../v0.3.x-sequencing.md#the-usefulness-ladder). When RFC 0028 lands, an autonomous channel becomes the natural host for an autonomous *deliberation*; this RFC is forward-compatible with that (the `goal` field and the chair synthesis turn are the seam RFC 0028 plugs into).

## Security Considerations

- **Runaway cost is the headline risk.** There is no human circuit-breaker on an unattended channel, so the mandatory `interaction_budget_tokens` cap ([Goal #4](#goals)) is a **safety requirement, not a tuning knob** — `validate` rejects an `autonomous.enabled` channel without an enforced cap, and the cap is server-side fail-closed in the wallet ([RFC 0050 interaction-budget amendment](0050-amendment-interaction-budget-enforcement.md)). `max_rounds` is a second independent bound. Two corollaries follow from the cap being *fail-closed*: (i) the cap must **reserve a synthesis allowance** ([§D](#d-termination-and-synthesis--always-produce-an-artifact)) so the artifact survives a close-by-budget — a fail-closed cap that swallows the closing synthesis is *itself* a failure mode; and (ii) a **standing/scheduled** channel needs an **aggregate** bound on top of the per-interaction cap ([§E](#e-standing-and-scheduled-discussions)), since per-interaction capping leaves the recurring total unbounded.
- **Loop amplification.** Agent-to-agent wakes could in principle self-amplify (A's post wakes B, whose post wakes A…). The existing RFC 0024 loop-back guard and RFC 0030 reply budget bound this; the autonomous path must not bypass them. A *second* potential loop is the anti-collapse chair (chair → silence → chair …); [§C](#c-the-central-tension--anti-collapse-cadence) bounds it at one escalation **per agenda item** (the per-item generalization of the shipped CE5 guard), so chair turns are bounded by agenda length, never open-ended. The acceptance MT includes a "no runaway" leg measuring turns and spend against the cap.
- **Prompt injection inside the agent-only loop.** Persona-authored content already flows through the RFC 0009 `<external_data>` envelope and the RFC 0036 §F per-row escaping; autonomous channels add no new ingestion path, but the `topic`/`agenda`/`goal` strings are operator-supplied config and must be escaped on injection into the convener prompt the same way.
- **Unattended drift.** Without a human, an autonomous channel could drift off-topic indefinitely; `max_rounds` + agenda-exhaustion termination bound this. Topic-drift *detection* (RFC 0030 Phase 3) is out of scope and not required for the bounded brainstorm.

## Phased Implementation Plan

### Phase 1: Convene + bounded one-shot brainstorm (MVP)

The smallest shippable, demoable unit: a channel that opens itself on a topic, runs to a bound, and synthesizes — with the mandatory cap enforced.

1. `autonomous` config block on the RFC 0050 surface + `validate` rejecting autonomy without `interaction_budget_tokens`.
2. Convener opening-turn authoring under a fresh `interaction_id`; `persatrix channel convene` CLI verb.
3. The **deterministic bounded close** ([§D](#d-termination-and-synthesis--always-produce-an-artifact)): a new orchestrator-side close trigger (`max_rounds` / soft-budget threshold / agenda-exhausted) that dispatches the chair synthesis turn and closes — respecting CE4 (the chair still cannot close itself).
4. The **reserved synthesis allowance** in the wallet so the mandatory synthesis turn always has a lease, even on a close-by-budget.
5. Mandatory synthesis-on-close (chair synthesis turn + interaction-summary artifact), drawn from the reserve.
6. Acceptance MT: convene → converges → terminates → synthesis present (**including the close-by-budget path**) → spend ≤ cap, with zero human turns.

### Phase 2: Anti-collapse cadence

The [§C](#c-the-central-tension--anti-collapse-cadence) chair mechanism — the **per-agenda-item escalation ration** (generalizing the shipped CE5 one-shot ration) with the per-item loop guard preserved, plus the liveness floor — scoped to `autonomous.enabled`. MT: a roster that would collapse to silence under the default bias instead works through a multi-item agenda, *and the chair never speaks twice into silence on the same item*.

### Phase 3: Standing / scheduled convening

`autonomous.schedule` over RFC 0024 timers ([§E](#e-standing-and-scheduled-discussions)) — open/advance on a timer, including the channel-config→convener-timer wiring (RFC 0024 deferred runtime timer mutation) and the **aggregate** standing bound (`max_convenings` / standing cost budget) validated at config time. MT: a standing channel convenes a fresh interaction on schedule, unattended, across a window, *and stops at the aggregate bound*.

### Phase 4: Flagship demo

The four-vendor human-free brainstorm. `make demo-autonomous` — a curated roster + topic that shows the whole arc in one command, with **two faces**:

- **Cross-vendor (headline).** Four personas, each pinned by alias to a *different cloud vendor* — **Anthropic + OpenAI + Gemini + watsonx.ai** — brainstorm one topic in one channel **with no human**, then converge and synthesize. This is the most vivid possible proof that the conversation layer is provider-agnostic (RFC 0033) and the single best adoption demo before v0.4.0. It depends on [RFC 0053](0053-gemini-watsonx-providers.md) landing both new providers, and needs all four vendors' credentials (the headline manual test runs live; see [§Test Strategy](#test-strategy)).
- **Offline (CI / no-keys).** The same roster mapped to the `mock` provider so the demo and the no-runaway smoke run with zero keys and zero spend.

Doubles as the adoption showcase; no separate showcase scenario is built.

## Files Touched (Estimated)

| Component | Files | Change |
|-----------|-------|--------|
| Python agents | `agents/` channel/governance + chair path | Convener opening-turn authoring; chair **per-agenda-item escalation ration** (generalizing the shipped CE5 one-shot ration, autonomous-scoped, [§C](#c-the-central-tension--anti-collapse-cadence)); mandatory synthesis-on-close drawn from the reserve |
| Go orchestrator | `internal/` channel config + validation + wallet | `autonomous` config block; `validate` cap-required + standing aggregate-bound gate; convene endpoint over the existing publish path; **new** deterministic bounded-close trigger (max_rounds / soft-budget / agenda-exhausted) and a wallet **synthesis-allowance reserve** ([§D](#d-termination-and-synthesis--always-produce-an-artifact)) |
| Rust CLI | `cli/src/` | `persatrix channel convene` verb |
| Config | `config/` channel schema | `autonomous` block schema; example autonomous channel |
| Demo | `Makefile`, `blueprints/` | `make demo-autonomous` + a curated roster blueprint |
| Docs | `docs/manual-tests/` | New `MT-AUTONOMOUS-*` acceptance tests |

No new proto, no new store migration, no new wake *type*. The estimate is small because the substrate is shipped — but it is **not zero new mechanism**: the per-agenda-item escalation ration ([§C](#c-the-central-tension--anti-collapse-cadence)), the deterministic bounded close + wallet synthesis reserve ([§D](#d-termination-and-synthesis--always-produce-an-artifact)), and the standing aggregate bound + schedule-wiring seam ([§E](#e-standing-and-scheduled-discussions)) are each new, each autonomous-scoped, and each respects a shipped invariant rather than re-tuning it globally.

## Test Strategy

- **Unit tests**: `validate` rejects `autonomous.enabled` without a cap; convener authors exactly one opening turn under a fresh `interaction_id`; chair advances vs. closes correctly given agenda state.
- **Integration tests**: full convene→converge→terminate→synthesis cycle on the mock provider with zero human turns; spend ≤ cap; no-runaway leg (turns + tokens bounded under an adversarial "everyone wants to talk" roster); a **close-by-budget** leg asserting a synthesis is *still* produced (the reserve is honored — the regression the fail-closed wallet would otherwise cause); a **chair-loop** leg asserting at most one escalation per agenda item; a **standing** leg asserting the aggregate bound stops re-convening.
- **E2E / smoke tests**: `make demo-autonomous` runs offline and produces a non-empty synthesis.
- **Manual tests**: `MT-AUTONOMOUS-001` (one-shot brainstorm, live provider — converges + synthesizes, no human); `MT-AUTONOMOUS-002` (anti-collapse — a roster that collapses under default bias works the agenda under autonomous pressure); `MT-AUTONOMOUS-003` (standing/scheduled convene across a window); **`MT-AUTONOMOUS-MULTIPROVIDER-001`** (the headline cross-vendor demo — Anthropic + OpenAI + Gemini + watsonx.ai personas brainstorm one topic in one channel with no human, converge, and synthesize; live, all four vendors keyed; spend ≤ cap on every seat). The last depends on [RFC 0053](0053-gemini-watsonx-providers.md) landing the two new providers.

## Open Questions

1. **Where does the convener role live?** Reuse the v0.3.8 `chair` disposition as-is (the convener *is* the chair), or introduce a distinct `convener` so a channel can have a separate opener and facilitator? Lean toward **chair = convener** for v0.3.x simplicity; revisit if a roster wants them split.
2. **Is anti-collapse a chair-path generalization or a channel-level mode?** [§C](#c-the-central-tension--anti-collapse-cadence) assumes the per-agenda-item escalation ration on the existing chair path (generalizing CE5), scoped by `autonomous.enabled`. The alternative — a general `liveness` knob on the RFC 0050 surface usable on human channels too — is more general but risks re-introducing pile-on and re-opening the CE5 runaway loop on human channels. Default to the scoped generalization.
3. **Should the brainstorm `goal` be free-text only, or a small typed schema** (question / compare-options / produce-list) that shapes the synthesis? Free-text for v0.3.x; a typed goal is the natural RFC 0028 seam (v0.4.x).
4. **Topic source *and* timer wiring for standing channels** — fixed `topic`, a rotating `agenda`, or an operator-supplied queue? Phase 3 ships fixed/rotating; a queue is a follow-up. Relatedly ([§E](#e-standing-and-scheduled-discussions)): does `autonomous.schedule` reach the RFC 0024 timer registry via a config round-trip into the convener's `agents.yaml`, or via a new runtime `RegisterTimer` seam (which RFC 0024 explicitly deferred)? Phase 3 picks the cheaper; lean config round-trip for v0.3.x.
5. **Default `max_rounds`, cap, synthesis-reserve fraction, and standing aggregate bound** — calibration needs a soak on real rosters. Ship conservative defaults (low rounds, modest cap, a synthesis reserve generous enough that a full synthesis turn never gets denied, a low `max_convenings`) and a tracked-issue follow-up to tune after observed runs.

## Decision / Next Steps

**Status**: 📋 Proposed. This RFC is gated on ratification of the [v0.3.x-sequencing Amendment 2026-06-28](../v0.3.x-sequencing.md#amendment-2026-06-28--add-the-autonomous-agent-only-channel-as-the-v03x-realism-capstone), which sequences it as the next realism rung (candidate) ahead of v0.4.0.

On ratification:

1. Open `docs/v0.3.x-plan.md` for the assigned patch (modeled on the prior per-version plans), or fold Phase 1 into the next opening patch plan.
2. Open `docs/rfcs/0052-pr-plan.md` (the per-PR breakdown), Phase 1 first.
3. Resolve [OQ #1](#open-questions) (convener = chair?) and [OQ #2](#open-questions) (anti-collapse scope) before Phase 2 begins — both shape the chair path.
4. Pair with the [RFC 0044 eval/golden-trace harness](0044-eval-set-golden-traces.md) as the safety net: autonomous = no human to catch a bad conversation, so automated conversation-quality gates matter more here than anywhere prior.

## Related Documentation

- [v0.3.x Sequencing — Amendment 2026-06-28](../v0.3.x-sequencing.md#amendment-2026-06-28--add-the-autonomous-agent-only-channel-as-the-v03x-realism-capstone) — the sequencing decision this RFC implements; the [usefulness ladder](../v0.3.x-sequencing.md#the-usefulness-ladder) framing.
- [RFC 0030 — Multi-Agent Conversation Governance](0030-multi-agent-conversation-governance.md) — relevance gate, chair, stall escalation, end-of-interaction (the convergence + the re-pointed stall behavior).
- [RFC 0051 — Reasoning Before Posting](0051-reasoning-before-posting.md) — semantic silence (the collapse-risk source) and considered compose.
- [RFC 0024 — Event-Driven Agent Scheduling](0024-event-driven-scheduling.md) — `InboundEventWake` (agent-to-agent propagation) + timers (standing/scheduled).
- [RFC 0050 — Extensible Channel Configuration](0050-extensible-channel-configuration.md) — where the `autonomous` block lives; the interaction-budget cap.
- [RFC 0028 — Agent Decision Policy Engine](0028-agent-decision-policy-engine.md) — the v0.4.x decision rung the brainstorm is forward-compatible with.
- [RFC 0044 — Eval-Set Shape with Golden Traces](0044-eval-set-golden-traces.md) — the conversation-quality safety net to pair with this.
- [RFC 0053 — Gemini and watsonx.ai LLM Providers](0053-gemini-watsonx-providers.md) — bundled into the same version; provides the two new vendors the [Phase 4](#phase-4-flagship-demo) four-vendor brainstorm demo needs.
- [RFC 0033 — Provider-Agnostic Model Alias Layer](0033-model-alias-layer.md) — why each persona can run on a different vendor with no conversation-layer change.
- [memory-scope-axes.md](../memory-scope-axes.md) — the "channel with no human participant" case that already works at the session layer.
