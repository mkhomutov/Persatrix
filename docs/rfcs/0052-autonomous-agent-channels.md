---
# Allowed values are documented in README.md. The YAML front-matter is the
# source of truth read by `scripts/rfcs.py` to regenerate INDEX.md — keep
# it in sync with the bold-markdown header below (which is what GitHub
# renders for human readers).
id: RFC-0052
title: "Autonomous Agent-Only Channels"
summary: "A channel that convenes a set of personas on a topic and runs a productive, human-free discussion that converges, terminates, and yields a readable synthesis — the human-out-of-the-loop capstone of the v0.3.x realism arc. Reuses the shipped channel/governance/scheduling/reasoning seams; the genuinely new content spans four pieces (§B–§E): self-convening (assembled from the publish path) plus three mechanisms that each extend a shipped governance invariant — anti-collapse cadence, a mandatory cost cap with a reserved synthesis allowance + deterministic bounded close, and a standing-schedule aggregate bound + timer-wiring seam."
type: feature
status: implemented
author: Maksim Khomutov
created: 2026-06-28
target: "v0.3.11"
depends_on:
  - RFC-0011
  - RFC-0030
  - RFC-0024
  - RFC-0050
  - RFC-0051
  - RFC-0020
  - RFC-0023
  - RFC-0033
  - RFC-0009
---

# RFC 0052 — Autonomous Agent-Only Channels

**Type**: feature
**Status**: ✅ **Implemented** — v0.3.11 (Phases 1–4, PRs 1–9: convene + bounded brainstorm + mandatory cap → anti-collapse cadence → standing/scheduled + aggregate bound → flagship demo; PR 9 the four-vendor headline blueprint + `MT-AUTONOMOUS-MULTIPROVIDER-001` + this closeout). The four-vendor human-free brainstorm it headlines is enabled by [RFC 0053](0053-gemini-watsonx-providers.md) (Gemini + watsonx.ai — landed). [plan](../v0.3.11-plan.md), [PR plan](0052-pr-plan.md).
**Author**: Maksim Khomutov
**Date**: 2026-06-28
**Target**: v0.3.11 (the v0.3.x realism capstone; sequenced by [v0.3.x-sequencing Amendment 2026-06-28](../v0.3.x-sequencing.md#amendment-2026-06-28--add-the-autonomous-agent-only-channel-as-the-v03x-realism-capstone), pinned to v0.3.11 at [plan opening](../v0.3.11-plan.md#scope-decisions-locked-at-plan-authoring-time-2026-06-28))
**Depends on**: RFC 0011 (channels), RFC 0030 (conversation governance), RFC 0024 (event-driven scheduling), RFC 0050 (channel configuration), RFC 0051 (reasoning before posting), RFC 0020 (interaction lifecycle), RFC 0023 (LLM call leasing — the mandatory cost cap is enforced as a lease), RFC 0033 (provider-agnostic alias layer — per-seat vendor selection), RFC 0009 (security envelope — operator-supplied `topic`/`agenda`/`goal` are wrapped as `<external_data>` in the convener prompt). The headline four-vendor [Phase 4](#phase-4-flagship-demo) demo additionally requires [RFC 0053](0053-gemini-watsonx-providers.md) (bundled, not a hard dependency — RFC 0052 ships on any single provider).

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

Most of the machinery already ships. The genuinely new design content is small but **not zero**, and spans four pieces (§B–§E). One — **self-convening** (an opening without a human turn) — is new behavior assembled from the existing publish path. The other three each *extend a shipped governance invariant rather than merely toggling it*, so they are new mechanism, not config: **anti-collapse cadence** (counter-pressure to the bias-to-silence defaults the realism arc deliberately installed — concretely, a generalization of the RFC 0030 *one-escalation-per-interaction* chair ration to a *per-agenda-item* ration, so the discussion advances rather than dying on round one); a **mandatory cost cap with a reserved synthesis allowance and a new deterministic bounded close** (there is no human to notice a runaway loop, and — because the shipped wallet fails leases closed and the chair cannot close an interaction — neither the synthesis nor the close comes for free); and a **standing-schedule aggregate bound + timer-wiring seam** for recurring convening, which the per-interaction cap alone does not bound.

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
5. **Optional standing/scheduled discussions — with a mandatory aggregate bound.** A channel can be convened (or re-convened, or have its agenda advanced) on an RFC 0024 timer — "the panel deliberates topic X every morning." Because the per-interaction cap ([Goal #4](#goals)) does **not** bound a recurring schedule, a standing channel cannot be created without an enforced **aggregate** bound (`max_convenings` and/or a standing-window cost budget), rejected at config-validation time the same way an uncapped per-interaction channel is ([§E](#e-standing-and-scheduled-discussions)).
6. **A one-command demo.** `make demo-autonomous` boots a curated roster on a topic and shows the whole arc — convene → discuss → converge → synthesize — with zero human input.

## Non-Goals

- **Reasoning toward a *decision*.** The deliberation-to-a-justified-recommendation rung is the v0.4.x [decision engine (RFC 0028)](0028-agent-decision-policy-engine.md). This RFC is scoped to the **brainstorm** rung (discuss → converge → synthesize), achievable on today's substrate. See [§F](#f-scope-line--brainstorm-now-decision-later).
- **Changing the human-channel defaults.** The bias-to-silence behavior (RFC 0030 relevance gate, RFC 0051 semantic silence) stays exactly as shipped for ordinary human-in-the-loop channels. Anti-collapse pressure applies only when a channel is marked autonomous.
- **Organizational roles, authority, or clearance** (RFC 0012, v0.4.0) — an autonomous channel is a flat roster, not an org.
- **External bridges** (Slack/Discord/email — RFC 0011 external, v0.5.0). Autonomous channels are internal.
- **A new moderator subsystem.** This reuses the v0.3.8 `chair` disposition and the minimal Layer-5 stall slice; the full RFC 0030 Phase 2 moderator stays v0.4.0.

## Design / Implementation

### A. What already exists (the substrate)

The point worth making first: this is **assembly plus four pieces of new work (§B–§E)**, not a new subsystem. One — self-convening ([§B](#b-self-convening--starting-without-a-human-turn)) — is assembled from the existing publish path. The other three *modify shipped governance invariants* (spelled out in [§C](#c-the-central-tension--anti-collapse-cadence), [§D](#d-termination-and-synthesis--always-produce-an-artifact), and [§E](#e-standing-and-scheduled-discussions)), so they are genuinely new mechanism, not configuration over an untouched substrate. The following already ship and are directly reusable:

| Capability | Source | Reuse here |
|------------|--------|------------|
| Agent-to-agent channels with **no human participant** work at the session/memory layer | RFC 0011 + the [RFC 0031 scope-axes review](../memory-scope-axes.md) (the "channel with no human participant" case is explicitly handled) | The channel itself needs no new isolation work |
| One persona's post **wakes the others** (no poll) | RFC 0024 `InboundEventWake`; scheduled `timers` | Free agent-to-agent turn propagation; timers drive [§E](#e-standing-and-scheduled-discussions) |
| Floor control / speaker serialization | RFC 0030 Layer 2.5 (v0.3.6) | One speaker at a time, no stampede |
| Relevance gating, `chair` disposition, end-of-interaction vote, **stall escalation** | RFC 0030 Tier A/B + chair + the [chair-stall-escalation amendment](0030-amendment-chair-stall-escalation.md) (v0.3.7–0.3.8) | Convergence; [§C](#c-the-central-tension--anti-collapse-cadence) **generalizes** the shipped one-escalation-*per-interaction* ration (CE5) to per-agenda-item; [§D](#d-termination-and-synthesis--always-produce-an-artifact) works around CE4 ("a chair cannot close") |
| Cost ceiling (**Layer 1**), per-participant reply budget (**Layer 2**), interaction budget | RFC 0030 Layer 1 / Layer 2 + RFC 0050 | The [§D](#d-termination-and-synthesis--always-produce-an-artifact) bound and the [Goal #4](#goals) cost cap |
| Reasoning before posting (semantic silence + considered compose) | RFC 0051 (v0.3.10) | Quality of each turn; also the *cause* of collapse risk |
| Interaction summary surface | RFC 0020 + v0.3.8 summary surface | The synthesized artifact [§D](#d-termination-and-synthesis--always-produce-an-artifact) |
| Per-channel, operator-editable config | RFC 0050 | Where the `autonomous` block lives |
| **Provider-agnostic personas** (each persona picks its model by alias) | RFC 0033 alias layer + [RFC 0053](0053-gemini-watsonx-providers.md) (Gemini + watsonx.ai) | Lets each seat run on a *different vendor* — the cross-vendor demo [§Phase 4](#phase-4-flagship-demo) |

The four things **not** covered by any shipped seam — and where the genuinely new work lives: self-convening ([§B](#b-self-convening--starting-without-a-human-turn), assembled from the publish path); anti-collapse cadence ([§C](#c-the-central-tension--anti-collapse-cadence)), which generalizes the shipped CE5 one-escalation-per-interaction ration to a per-agenda-item ration **while preserving its anti-runaway loop guard**; the mandatory-cap + reserved-synthesis + deterministic-bounded-close contract ([§D](#d-termination-and-synthesis--always-produce-an-artifact)) that makes unattended running safe (the shipped wallet fails leases closed, and per RFC 0030 CE4 *a chair cannot close an interaction* — so neither the synthesis nor the close is free reuse, and the wallet reserve has **no shipped analog**); and the standing-schedule aggregate bound + timer-wiring seam ([§E](#e-standing-and-scheduled-discussions)), since the per-interaction cap leaves a recurring schedule unbounded and the RFC 0024 timer registry is `agents.yaml`-canonical.

### B. Self-convening — starting without a human turn

An autonomous channel carries an `autonomous` block on its RFC 0050 config surface:

```yaml
# channel config (RFC 0050 config_overrides / config-as-code)
# interaction_budget_tokens and escalation_chair_id are top-level channel knobs,
# siblings of `autonomous` — not nested under it (see mergeAutonomousPatch's closed
# sub-key set, which does not include either).
interaction_budget_tokens: 200000   # MANDATORY — config-validation rejects autonomy without a cap
escalation_chair_id: ember-owl      # MANDATORY on an armed channel (PR 4) — the role that
                                     # authors the mandatory synthesis turn on close (§D)
autonomous:
  enabled: true
  topic: "Should we adopt a monorepo? Lay out the tradeoffs."
  agenda:                       # optional ordered sub-topics for the chair to advance through
    - "Build tooling and CI cost"
    - "Cross-team coupling risk"
    - "Migration effort"
  convener: nova-sparrow         # agent id of the persona that authors the opening + advances the agenda
                                 # (plain agent id, as RFC 0050 escalation_chair_id uses; a DISTINCT role from the
                                 # chair per the resolved OQ #1 — validate rejects convener == escalation_chair_id)
  goal: "A synthesized recommendation with the strongest argument on each side."
  max_rounds: 8                  # hard bound (also see budget cap, Goal #4); default 8 since the
                                 # ISSUE-0109 calibration (was 12 — see OQ #5)
```

**Convening** = the convener persona authors the **opening turn** (topic + first agenda item) as a normal channel publish, stamped with a fresh `interaction_id` (RFC 0030 producer). From that publish onward the existing `InboundEventWake` chain carries the discussion with no further human input. Convening is triggered three ways, all reusing existing surfaces:

- **CLI** — a new `persatrix channel convene <id>` verb (thin wrapper over the existing publish path).
- **Web console** — a "Convene" action in the Channel settings panel (RFC 0050 Phase 2 surface).
- **Timer** — an RFC 0024 scheduled wake on the convener ([§E](#e-standing-and-scheduled-discussions)).

No new transport, no new wake type, no new store table — convening is "author the seed turn under a fresh interaction id."

**The opening turn is structurally outside the per-interaction cap.** The wallet snapshots `interaction_budget_tokens` at an interaction's *first commit* (router-side), so the lease that *produces* the opening message predates its own snapshot and resolves uncapped ([`interaction_budget.go`](../../internal/wallet/interaction_budget.go) — `resolveInteractionBudget` returns 0/uncapped for an interaction "whose first message has not yet committed"). The mandatory cap therefore bounds the discussion *after* convening, not the convener's opening turn; the always-on RFC 0030 Layer-0 depth cap is the only bound on that first call. For a one-shot brainstorm this is one turn of slack; for a **standing** channel it is one uncapped opening turn *per convening* — a further reason the [§E](#e-standing-and-scheduled-discussions) aggregate bound is mandatory. Phase 1 either accepts this (documented) or has the convener's opening turn carry the channel's resolved cap explicitly on its own lease.

### C. The central tension — anti-collapse cadence

**This is the design heart of the RFC.** The v0.3.x realism arc deliberately installed *bias-to-silence and converge-then-terminate* pressure:

- RFC 0051 semantic silence is **on by default** (`mode: bid`) — a persona with "nothing to add" stays silent *with a reason*.
- The RFC 0030 [chair stall escalation](0030-amendment-chair-stall-escalation.md) (v0.3.8) forces **one** chair turn on a zero-reply round, whose prompt is *"state the synthesis and cast your end-vote, **or** name the member best placed to resolve what remains and ask them directly."* Two properties of the *shipped* mechanism matter here, and the naïve framing gets both wrong: it already *can* hand off (it is **not** close-only — outcome (b) is a directed re-invite), but it fires **at most once per interaction** — the CE5 loop guard, which exists precisely because "a synthesis that draws no reaction re-triggers detection every round: chair speaks to silence, forever."

With a human in the chair these are exactly right. **With no human they compose into premature death:** every persona reasons "the others can cover this," all stay silent, the single escalation fires, its handoff also draws silence (CE5 is now spent), and an unattended channel converges to a near-empty transcript before idle rotation buries it with no synthesis. Anti-collapse cadence is the counter-pressure that makes a human-free channel productive without un-doing the realism work — and it is **new mechanism, not a behavior toggle**, because making a multi-item agenda workable requires generalizing the shipped CE5 ration:

1. **The chair advances the *agenda*, not just the *question*.** The shipped escalation hands off *within* the open question ("who can resolve what remains"); an autonomous channel additionally needs the chair, on a stall with the agenda **not exhausted**, to **advance to the next agenda item** — pose the next sub-topic or a pointed question directed at a specific persona (NL addressing, RFC 0030 Tier B). This requires lifting CE5's **one-escalation-per-interaction** ration to a **per-agenda-item** ration: each item gets its own escalation budget, where today the whole interaction gets exactly one.
2. **The per-item loop guard is preserved, not removed.** CE5's anti-runaway purpose is non-negotiable in an unattended channel, so the per-item ration carries the same guard: a stall on item *N* escalates **once**; if that escalation also draws silence, the chair does not re-invite item *N* forever — it advances to item *N+1* (or, if the agenda is exhausted, proposes synthesis-and-close, [§D](#d-termination-and-synthesis--always-produce-an-artifact)). The chair never speaks twice into silence on the same item. This bounds total chair turns at **one per agenda item** — a hard, agenda-length-bounded ceiling, not an open loop. Generalizing CE5 this way is the load-bearing new work; "a behavior switch on the existing chair" understates it.
3. **A liveness target (best-effort, not a guaranteed floor).** A `min_substantive_turns_per_agenda_item` makes the chair re-invite an item rather than skip it on the first quiet round. It is a *target*, not an enforceable floor: with RFC 0051 semantic silence on, nothing can *compel* personas to produce substantive content, so the chair's only lever is the single per-item re-invite (the escalation in item 2). In the worst case — a genuinely silent roster — an item yields zero substantive turns and advances after that one escalation, so the target and the ceiling **collapse to the same event**. The *ceiling* (one escalation per item) is the hard bound; the *target* only raises the odds an item gets discussed before it advances. Calling the two a symmetric floor-and-ceiling would overstate the guarantee.
4. **Silence stays semantic, not suppressed.** Anti-collapse does **not** lower the RFC 0051 silence threshold globally (that would bring back the pile-on the arc removed). It works by *giving the chair something concrete to ask*, which raises individual personas' salience honestly rather than forcing low-value "I agree" turns.

The explicit invariant: **anti-collapse pressure is scoped to `autonomous.enabled` channels.** Ordinary channels keep the shipped CE5 one-shot ration and bias-to-silence defaults untouched. This keeps the two opposing forces (bias-to-silence for human channels, keep-alive for autonomous ones) cleanly separated rather than globally re-tuned.

### D. Termination and synthesis — always produce an artifact

An autonomous channel **must** terminate and **must** leave a readable artifact. Two shipped invariants make this harder than "reuse the close path," and the RFC must respect both:

- **A chair cannot close an interaction.** RFC 0030 CE4 pins termination to the Layer-4 quorum end-vote (the chair *proposes* a synthesis; a second member's concurring vote *disposes*). Outside that, the shipped terminators are the **quorum end-vote** and **idle rotation** — the latter a ~10-minute timeout ([RFC 0020 `idle_timeout_sec`](0020-interaction-lifecycle.md), default 600s). Idle rotation is **not** artifact-less: every close — idle included — transitions the interaction through `closing` and enqueues an LLM-generated interaction summary ([RFC 0020 §C](0020-interaction-lifecycle.md)). What idle close *lacks* is a **goal-directed chair synthesis** — a turn that states the outcome against `autonomous.goal`, as opposed to a generic recap of whatever was said. Critically, hitting the cost ceiling does **not** close an interaction: per RFC 0030 it "stays open but useful work cannot continue" once leases are denied. So "two hard bounds the chair owns" is not achievable as stated — the chair owns neither.
- **The wallet fails leases closed — and the closing artifact is itself a leased call.** The mandatory `interaction_budget_tokens` cap is enforced in the wallet ([RFC 0050 interaction-budget amendment](0050-amendment-interaction-budget-enforcement.md)): once the interaction is over budget, `AcquireLease` denies the next call. The close path is **not two leased calls but `1 + N`**: (1) the chair's goal-directed synthesis *turn* — a channel publish (RFC 0030 CE6: "the lease denial fails the forced turn closed, the same as any turn"); and (2) the RFC 0020 closing **summarization** call (`context_management.summarization.model`) — which is authored **per participating persona, not once per interaction** ([`close_path.py`](../../agents/persona_runtime/close_path.py) spawns one `finalize_closed_interaction` per `agent_id`), so an N-persona roster issues **N** summary calls, all drawing on the *shared* per-interaction budget ([`interaction_budget.go`](../../internal/wallet/interaction_budget.go) keys the running total by `interaction_id` alone). On a budget-exhausted close the wallet denies the chair turn (CE6); and **if** the summaries are metered to the same interaction budget — which this RFC must confirm ([OQ #6](#open-questions)) — they are denied too, each falling through to the janitor's `"[interaction summary unavailable]"` placeholder ([RFC 0020 §C](0020-interaction-lifecycle.md)). Either way a budget-exhausted close puts the artifact a human reads at risk, not just the chair turn — and the allowance the close must reserve **scales with the roster**, not a fixed two.

The autonomous path therefore adds two new mechanisms — the part of [§D](#d-termination-and-synthesis--always-produce-an-artifact) that is *not* pure reuse, one orchestrator-side and one wallet-side:

1. **A deterministic bounded close (orchestrator-side, small).** A new orchestrator-side close trigger fires when the agenda is exhausted, *or* `max_rounds` is reached, *or* a **soft** budget threshold is crossed — distinct from the shipped quorum/idle paths, and distinct from the chair (which still cannot close, CE4 intact). On fire it dispatches the chair's synthesis turn and *then* closes the interaction, yielding the same artifact-bearing close the quorum path produces. `max_rounds` has no shipped enforcement today; this trigger is what adds it.
2. **A reserved synthesis allowance (wallet-side, genuinely new accounting).** The shipped wallet is a *single hard integer cap per interaction* — it has **no** concept of a reserved fraction, a soft threshold, or a partitioned sub-budget, so this is new wallet accounting, not a config knob over an existing mechanism. It is the one new piece with **no shipped analog** (the orchestrator close trigger and the §C ration extend shipped paths; this extends the wallet's core accounting — see [§Files Touched](#files-touched-estimated)). The cap is split: `interaction_budget_tokens` bounds the *discussion*, and a separate reserve is held back so the closing call(s) always have a lease even when the discussion budget is spent. The reserve must be sized for **every** leased call on the close path — the chair synthesis turn, *and* the RFC 0020 summarization call **for each participating persona** if it is metered to the interaction ([OQ #6](#open-questions)) — so the reserve is **roster-scaled (`1 + N`)**, not a fixed two; otherwise the close-by-budget path still degrades to the `"[interaction summary unavailable]"` placeholder for the personas whose summaries fall outside the held-back allowance. (If a roster-scaled reserve eats too large a fraction of the cap, the alternative is to scope the metering + reserve to a single designated close-summarizer and document the rest as best-effort — but the default is roster-scaled so every persona's memory carries a real summary.) The **soft** threshold in (1) trips synthesize-and-close *before* the hard cap denies leases, giving those reserved calls headroom.

With both in place: **Termination** is bounded by agenda exhaustion, `max_rounds`, the soft budget threshold, or the natural quorum end-vote — whichever fires first — and there is no path to an unbounded loop (the per-item escalation ration of [§C](#c-the-central-tension--anti-collapse-cadence) bounds chair turns; these triggers cap the rest). **Synthesis** is two coupled artifacts, both **mandatory** on every close path: the chair emits a goal-directed synthesis *turn* (against the `goal`) from the reserve, and the RFC 0020 interaction summary — the thing a human reads later via the v0.3.8 surface — is produced (also from the reserve, on the budget path). An autonomous interaction that closes with **either** missing (the placeholder counts as missing) is a failure the acceptance MT checks for — explicitly on the **close-by-budget** path, where the reserve earns its keep. One failure mode the reserve does *not* cover is a provider/transport error on the synthesis call itself (distinct from budget denial); on an unattended channel there is no human fallback, so [OQ #6](#open-questions) tracks hardening that path, and the MT asserts the artifact on the happy and budget paths.

### E. Standing and scheduled discussions

Reusing RFC 0024 `timers`, the convener can be woken on a schedule to **open** a fresh interaction (a standing panel that deliberates a new topic each morning) or to **advance** a long-running one. The timer fires a convener wake; the convener authors the opening/advancing turn exactly as in [§B](#b-self-convening--starting-without-a-human-turn). Two caveats keep this honest:

- **Not quite "no new scheduling primitive."** RFC 0024 timers are **`agents.yaml`-canonical** (the per-agent `scheduled_wakes` SQLite table is a *derived cache* rebuilt from config), and RFC 0024 explicitly **deferred runtime timer mutation** ("a `RegisterTimer()` API … defer until a use case appears"). `autonomous.schedule` lives on the *runtime-editable* RFC 0050 **channel** surface, so wiring it to a convener wake needs either (a) a config round-trip into the convener's `agents.yaml` timer set, or (b) the deferred runtime-registration API — a small but genuinely new seam, not free reuse. Phase 3 picks one (see [OQ #4](#open-questions)).
- **The per-interaction cap does not bound a standing schedule.** The mandatory `interaction_budget_tokens` caps each *interaction*; a recurring convener with no human re-opens a fresh (separately capped) interaction indefinitely, so total spend over a standing schedule is **unbounded by the per-interaction cap alone**. A standing channel therefore additionally requires an **aggregate bound** — a `max_convenings` count, a standing-window cost budget, or both — validated at config time the same way the per-interaction cap is ([§Security](#security-considerations), [Goal #4](#goals)). Phase 3 ships this *with* the schedule, not after it.
- **Each convening leaves a wallet residue that nothing currently evicts.** The wallet's per-interaction running total is a process-lifetime map entry that is **never pruned** for a capped interaction that settled non-zero spend ([`interaction_budget.go`](../../internal/wallet/interaction_budget.go) — "There is no 'interaction closed' signal at this layer … nothing currently evicts it"). A standing channel mints a fresh *capped* `interaction_id` per convening, so the wallet's `interactionTokens` map grows by one entry per convening — unbounded over a long-running schedule even though `max_convenings` bounds only the count of live convenings. The deterministic bounded close ([§D](#d-termination-and-synthesis--always-produce-an-artifact)) is the first orchestrator point with a real interaction-closed signal, so it is also where the wallet must **evict** the closed interaction's entry; Phase 1 wires that eviction with the close trigger and Phase 3 asserts a standing channel's wallet footprint stays bounded across a window.

No new wake *type* — `autonomous.schedule` is an RFC 0024 timer spec plus a topic source plus the standing aggregate bound.

### F. Scope line — brainstorm now, decision later

The autonomous channel is scoped to the **brainstorm** rung: discuss → converge → synthesize a readable outcome. The **reason-toward-a-justified-decision** version — where the roster weighs tradeoffs and emits an auditable recommendation — is gated on the [RFC 0028 decision engine](0028-agent-decision-policy-engine.md) and stays **v0.4.x**, consistent with the [usefulness ladder](../v0.3.x-sequencing.md#the-usefulness-ladder). When RFC 0028 lands, an autonomous channel becomes the natural host for an autonomous *deliberation*; this RFC is forward-compatible with that (the `goal` field and the chair synthesis turn are the seam RFC 0028 plugs into).

## Security Considerations

- **Runaway cost is the headline risk.** There is no human circuit-breaker on an unattended channel, so the mandatory `interaction_budget_tokens` cap ([Goal #4](#goals)) is a **safety requirement, not a tuning knob** — `validate` rejects an `autonomous.enabled` channel without an enforced cap, and the cap is server-side fail-closed in the wallet ([RFC 0050 interaction-budget amendment](0050-amendment-interaction-budget-enforcement.md)). `max_rounds` is a second independent bound. Two corollaries follow from the cap being *fail-closed*: (i) the cap must **reserve a synthesis allowance** ([§D](#d-termination-and-synthesis--always-produce-an-artifact)) sized for *every* leased call on the close path — the chair's goal-directed synthesis turn and the RFC 0020 summarization call **for each participating persona** (if metered to the interaction, [OQ #6](#open-questions); the close summary is authored per-agent, so this reserve is **roster-scaled `1 + N`**, not a fixed two) — so the artifact survives a close-by-budget; a fail-closed cap that swallows either is *itself* a failure mode; and (ii) a **standing/scheduled** channel needs an **aggregate** bound on top of the per-interaction cap ([§E](#e-standing-and-scheduled-discussions)), since per-interaction capping leaves the recurring total unbounded.
- **Loop amplification.** Agent-to-agent wakes could in principle self-amplify (A's post wakes B, whose post wakes A…). This channel-message fanout cycle is bounded by **RFC 0030 Layer 0 (cascade-depth cap)** and **Layer 2 (per-participant reply budget)**; the autonomous path must not bypass them. (The RFC 0024 `source_span_id` loop-back guard bounds a *different* cycle — the `SalienceWake` memory-write→wake path — not the channel-message fanout, so it is not the relevant bound here.) A *second* potential loop is the anti-collapse chair (chair → silence → chair …); [§C](#c-the-central-tension--anti-collapse-cadence) bounds it at one escalation **per agenda item** (the per-item generalization of the shipped CE5 guard), so chair turns are bounded by agenda length, never open-ended. The acceptance MT includes a "no runaway" leg measuring turns and spend against the cap.
- **Prompt injection inside the agent-only loop.** Two distinct trust classes are in play. *Persona-authored* content already flows through the RFC 0009 `<external_data>` envelope and the RFC 0036 §F per-row escaping, and autonomous channels add no new ingestion path there. *Operator-supplied* `topic`/`agenda`/`goal` strings are a **different** trust class — not persona-authored, but still untrusted input to the convener prompt, and the one genuinely new injection surface this RFC opens. They must be wrapped in the RFC 0009 `<external_data>` envelope when injected into the convener's opening/advancing prompt; the RFC 0036 §F per-row escaping is **not** the right control for them (it escapes recall rows, not config). Phase 1 owns this wrapping.
- **Unattended drift.** Without a human, an autonomous channel could drift off-topic indefinitely; `max_rounds` + agenda-exhaustion termination bound this. Topic-drift *detection* (RFC 0030 Phase 3) is out of scope and not required for the bounded brainstorm.

## Phased Implementation Plan

### Phase 1: Convene + bounded one-shot brainstorm (MVP)

The smallest shippable, demoable unit: a channel that opens itself on a topic, runs to a bound, and synthesizes — with the mandatory cap enforced.

1. `autonomous` config block on the RFC 0050 surface + `validate` rejecting autonomy without `interaction_budget_tokens`.
2. Convener opening-turn authoring under a fresh `interaction_id`, with operator-supplied `topic`/`agenda`/`goal` wrapped in the RFC 0009 `<external_data>` envelope ([§Security](#security-considerations)); `persatrix channel convene` CLI verb.
3. The **deterministic bounded close** ([§D](#d-termination-and-synthesis--always-produce-an-artifact)): a new orchestrator-side close trigger (`max_rounds` / soft-budget threshold / agenda-exhausted) that dispatches the chair synthesis turn and closes — respecting CE4 (the chair still cannot close itself).
4. The **reserved synthesis allowance** in the wallet (new accounting — the shipped wallet is a single hard cap) so **every** leased close-path call has a lease even on a close-by-budget: the chair synthesis turn, and the RFC 0020 summarization call — authored per participating persona — if metered to the interaction ([OQ #6](#open-questions) — resolve this first, since it decides whether the reserve covers one call or `1 + N`). The same close trigger **evicts** the interaction's wallet residue ([§E](#e-standing-and-scheduled-discussions)).
5. Mandatory synthesis-on-close (chair synthesis turn + interaction-summary artifact), drawn from the reserve.
6. Acceptance MT: convene → converges → terminates → **both** artifacts present (the chair synthesis turn — the user-facing readable synthesis — *and* a real RFC 0020 interaction summary for **every participating persona**, not the `"[interaction summary unavailable]"` placeholder) on every close path **including the close-by-budget path** (so the roster-scaled reserve is exercised, not just a one-summary reserve) → spend ≤ cap, with zero human turns.

### Phase 2: Anti-collapse cadence

The [§C](#c-the-central-tension--anti-collapse-cadence) chair mechanism — the **per-agenda-item escalation ration** (generalizing the shipped CE5 one-shot ration) with the per-item loop guard preserved, plus the best-effort liveness target — scoped to `autonomous.enabled`. MT: a roster that would collapse to silence under the default bias instead works through a multi-item agenda, *and the chair never speaks twice into silence on the same item*.

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
| Python agents | `agents/` convener + governance path | **Convener** opening-turn authoring (`convener.py`) + the **per-agenda-item escalation ration** on the convener path (generalizing the shipped CE5 one-shot ration, autonomous-scoped, [§C](#c-the-central-tension--anti-collapse-cadence); distinct convener role per [OQ #1](#open-questions)); mandatory synthesis-on-close drawn from the reserve; the [OQ #6](#open-questions) autonomous-close summary-metering edit in `summarize_close.py` |
| Go orchestrator | `internal/channels/` + `internal/wallet/` | `autonomous` config block + apply/persist; `validate` cap-required + standing aggregate-bound gate; convene publish logic over the existing path; **new** deterministic bounded-close trigger (max_rounds / soft-budget / agenda-exhausted) and a wallet **synthesis-allowance reserve** ([§D](#d-termination-and-synthesis--always-produce-an-artifact)) |
| Go orchestrator (REST) | `internal/server/` channel-config + convene handlers | The RFC 0050 PATCH/GET layer for the `autonomous` block (`channel_config_autonomous.go` merge/response, mirroring `channel_config_reasoning.go`) **+** a new `POST /api/v1/channels/{id}/convene` endpoint — the surfaces the CLI + web call |
| Rust CLI | `cli/src/commands/` | `persatrix channel config` **`autonomous.*` nested knobs** (new `channel_config_autonomous.rs`, mirroring the RFC 0051 `channel_config_reasoning.rs` split since `channel_config.rs` is at the 500 cap) **+** the `persatrix channel convene` verb (`channel_convene.rs`) |
| Web console | `web/src/panels/`, `web/src/lib/api.js` | An **Autonomous config section** (new `AutonomousSettings.svelte` child of the RFC 0050 Channel-settings panel — extracted because `ChannelSettings.svelte` is near the cap) editing the `autonomous.*` fields, **+** a **"Convene" action button** (the panel's first per-channel action) and a `conveneChannel()` API wrapper; a standing channel shows a convening-count / aggregate-bound readout |
| Config | `config/` channel schema | `autonomous` block schema; example autonomous channel |
| Demo | `Makefile`, `blueprints/` | `make demo-autonomous` + a curated roster blueprint |
| Docs | `docs/manual-tests/` | New `MT-AUTONOMOUS-*` acceptance tests |

The CLI + web surfaces are first-class operator affordances (the [v0.3.11 PR plan](0052-pr-plan.md) dedicates PR 2 to the config surfaces and PR 3's surface legs to the convene action), following the RFC 0050/0051 operator-editable precedent — an earlier draft of this table under-counted them (it omitted the web row and listed only the convene verb for CLI). No new proto, no new store migration, no new wake *type*. Much of the estimate is assembly because the substrate is shipped — but it is **not zero new mechanism**, and one piece has no shipped analog at all. Self-convening ([§B](#b-self-convening--starting-without-a-human-turn)) assembles the existing publish path. The three new mechanisms — the per-agenda-item escalation ration ([§C](#c-the-central-tension--anti-collapse-cadence)), the deterministic bounded close **+ wallet synthesis reserve** ([§D](#d-termination-and-synthesis--always-produce-an-artifact)), and the standing aggregate bound + schedule-wiring seam ([§E](#e-standing-and-scheduled-discussions)) — are each new and each autonomous-scoped. The §C ration and the §E bound extend shipped paths; the **wallet synthesis reserve is the exception** — the shipped wallet is a single hard integer cap with no reserved-fraction or soft-threshold concept, so the reserve is new core wallet accounting, not a tuning knob. Size the §D wallet work accordingly rather than as "small."

## Test Strategy

- **Unit tests**: `validate` rejects `autonomous.enabled` without a cap; convener authors exactly one opening turn under a fresh `interaction_id`; chair advances vs. closes correctly given agenda state.
- **Integration tests**: full convene→converge→terminate→synthesis cycle on the mock provider with zero human turns; spend ≤ cap; no-runaway leg (turns + tokens bounded under an adversarial "everyone wants to talk" roster); a **close-by-budget** leg (on a **≥2-persona** roster) asserting **both** closing artifacts are *still* produced — the chair synthesis turn *and* a real RFC 0020 interaction summary for **each persona** (not the `"[interaction summary unavailable]"` placeholder), i.e. the roster-scaled reserve is honored for every leased close-path call — the regression the fail-closed wallet would otherwise cause; a **chair-loop** leg asserting at most one escalation per agenda item; a **standing** leg asserting the aggregate bound stops re-convening.
- **E2E / smoke tests**: `make demo-autonomous` runs offline and produces a non-empty synthesis.
- **Manual tests**: `MT-AUTONOMOUS-001` (one-shot brainstorm, live provider — converges + synthesizes, no human); `MT-AUTONOMOUS-002` (anti-collapse — a roster that collapses under default bias works the agenda under autonomous pressure); `MT-AUTONOMOUS-003` (standing/scheduled convene across a window); **`MT-AUTONOMOUS-MULTIPROVIDER-001`** (the headline cross-vendor demo — Anthropic + OpenAI + Gemini + watsonx.ai personas brainstorm one topic in one channel with no human, converge, and synthesize; live, all four vendors keyed; total interaction spend ≤ the single shared per-interaction cap — **not** a per-seat cap). The last depends on [RFC 0053](0053-gemini-watsonx-providers.md) landing the two new providers.

## Open Questions

> **Status — resolved 2026-06-28 at [v0.3.11 plan opening](../v0.3.11-plan.md#scope-decisions-locked-at-plan-authoring-time-2026-06-28).** The load-bearing OQs were settled with the maintainer; **two reverse this RFC's original lean** (noted inline). Resolutions: **#1 → distinct `convener` role** (*reverses* "lean chair = convener" — the convener owns the agenda lifecycle, the chair keeps its shipped role, so the §C ration lives on the convener path); **#2 → scoped to `autonomous.enabled`** (the scoped generalization, not a general `liveness` knob); **#6 → meter the closing summary** (*takes the heavier path* — code-verified the summary is **currently unmetered**, so the autonomous close brings it under a lease and the reserve covers the chair turn **plus one summary per participating persona** — `1 + N`, roster-scaled, since the close summary is authored per-agent); **#3/#4/#5 → the documented defaults** (free-text `goal`; config-round-trip timer seam + fixed/rotating topic; conservative defaults + a calibration tracked-issue). Detail in the [v0.3.11 plan](../v0.3.11-plan.md#scope-decisions-locked-at-plan-authoring-time-2026-06-28) and the [RFC 0052 PR plan](0052-pr-plan.md#open-question-resolutions-locked-at-plan-authoring-time).

1. **Where does the convener role live?** Reuse the v0.3.8 `chair` disposition as-is (the convener *is* the chair), or introduce a distinct `convener` so a channel can have a separate opener and facilitator? Lean toward **chair = convener** for v0.3.x simplicity; revisit if a roster wants them split. — **Resolved (v0.3.11): distinct `convener` role** (reverses the lean). The convener authors the opening turn and advances the agenda; the chair keeps its shipped v0.3.8 stall-escalation → synthesis → end-vote role. The §C anti-collapse per-agenda-item ration therefore lives on the **convener** path, not the chair path.
2. **Is anti-collapse a chair-path generalization or a channel-level mode?** [§C](#c-the-central-tension--anti-collapse-cadence) assumes the per-agenda-item escalation ration on the existing chair path (generalizing CE5), scoped by `autonomous.enabled`. The alternative — a general `liveness` knob on the RFC 0050 surface usable on human channels too — is more general but risks re-introducing pile-on and re-opening the CE5 runaway loop on human channels. Default to the scoped generalization. — **Resolved (v0.3.11): scoped to `autonomous.enabled`** (the convener path; human channels keep the shipped bias-to-silence + CE5 one-escalation guard untouched).
3. **Should the brainstorm `goal` be free-text only, or a small typed schema** (question / compare-options / produce-list) that shapes the synthesis? Free-text for v0.3.x; a typed goal is the natural RFC 0028 seam (v0.4.x). — **Resolved (v0.3.11): free-text** (documented default).
4. **Topic source *and* timer wiring for standing channels** — fixed `topic`, a rotating `agenda`, or an operator-supplied queue? Phase 3 ships fixed/rotating; a queue is a follow-up. Relatedly ([§E](#e-standing-and-scheduled-discussions)): does `autonomous.schedule` reach the RFC 0024 timer registry via a config round-trip into the convener's `agents.yaml`, or via a new runtime `RegisterTimer` seam (which RFC 0024 explicitly deferred)? Phase 3 picks the cheaper; lean config round-trip for v0.3.x. — **Resolved (v0.3.11): fixed/rotating topic + the config-round-trip seam** (the queue + the runtime `RegisterTimer` API stay follow-ups).
5. **Default `max_rounds`, cap, synthesis-reserve fraction, and standing aggregate bound** — calibration needs a soak on real rosters. Ship conservative defaults (low rounds, modest cap, a synthesis reserve generous enough that a full synthesis turn never gets denied, a low `max_convenings`) and a tracked-issue follow-up to tune after observed runs. — **Resolved (v0.3.11): conservative defaults + a calibration tracked-issue** (filed at the PR 9 closeout as [ISSUE-0109](../issues/ISSUE-0109-rfc0052-autonomous-defaults-calibration.md) — tune after the live-MT soak). **Calibration applied 2026-07-24** from the 7-arc soak: `max_rounds` default 12→8, full-roster `end_vote_threshold` on the autonomous templates, the `interaction_cap_utilization` close histogram, the reserve unit validated unchanged — see the issue's Resolution.
6. **Is the RFC 0020 closing summarization call metered to the interaction budget, and how is the artifact guaranteed under non-budget failure?** Two coupled sub-questions that decide the [§D](#d-termination-and-synthesis--always-produce-an-artifact) reserve. (a) *Metering* — the closing summary is a leased LLM call (`context_management.summarization.model`); if it is attributed to the same `interaction_id` as the discussion, the fail-closed wallet denies it on a budget-exhausted close and the reserve must cover **two** calls (chair turn + summary), not one. Confirm the attribution before sizing the reserve. (b) *Non-budget failure* — the "always emits a readable synthesis" guarantee is only defended against budget denial; a provider/transport error or timeout on the synthesis turn (RFC 0020 janitor → placeholder) has no human fallback on an unattended channel. Decide whether Phase 1 hardens this (e.g. a retry/fallback-summarizer on the autonomous close path) or accepts the placeholder as the floor and documents it. — **Resolved (v0.3.11):** (a) *code-verified the summary is **currently unmetered*** — [`summarize_close.py`](../../agents/persona_runtime/summarize_close.py) passes no `cause`/`interaction_id`, so it bypasses the wallet lease ([`llm_client.py:212`](../../agents/llm_client.py)). On an unattended channel that is unbudgeted spend outside the mandatory cap, so the **autonomous close brings the summary under a lease** (stamped with the interaction's `interaction_id`); the reserve therefore covers the chair synthesis turn **plus one summary per participating persona** — `1 + N`, since [`close_path.py`](../../agents/persona_runtime/close_path.py) authors the close summary **per-agent**, not once per interaction (the question's "two calls" undercounted any roster larger than one). The reserve is therefore **roster-scaled**, feeding the OQ #5 calibration. (b) the existing timeout/exception → `[interaction summary unavailable]` placeholder is the documented floor for a non-budget transport error; the acceptance MT asserts both artifacts on the happy and close-by-budget paths.

## Decision / Next Steps

**Status**: ✅ **Implemented** (v0.3.11) — all four phases landed across PRs 1–9 (the [PR plan](0052-pr-plan.md) breakdown), closed out at PR 9 (the four-vendor headline blueprint + `MT-AUTONOMOUS-MULTIPROVIDER-001` + this flip). The [v0.3.x-sequencing Amendment 2026-06-28](../v0.3.x-sequencing.md#amendment-2026-06-28--add-the-autonomous-agent-only-channel-as-the-v03x-realism-capstone) is ratified ([#709](https://github.com/mkhomutov/Persatrix/pull/709)); the [v0.3.11 plan](../v0.3.11-plan.md) pins this RFC to v0.3.11. The live acceptance MTs (`MT-AUTONOMOUS-001/002/003` + the four-vendor `MT-AUTONOMOUS-MULTIPROVIDER-001`) run at release-prep, feeding the OQ #5 defaults calibration ([ISSUE-0109](../issues/ISSUE-0109-rfc0052-autonomous-defaults-calibration.md)).

Done at plan opening:

1. ✅ [`docs/v0.3.11-plan.md`](../v0.3.11-plan.md) — the master version plan; v0.3.11 anchors on RFC 0052 + 0053, RFC 0039 → v0.3.12 candidate.
2. ✅ [`docs/rfcs/0052-pr-plan.md`](0052-pr-plan.md) — the per-PR breakdown, Phase 1 first.
3. ✅ [OQ #1](#open-questions) (distinct convener), [OQ #2](#open-questions) (anti-collapse scoped to autonomous), and [OQ #6](#open-questions) (meter the summary → roster-scaled `1 + N` reserve) resolved — see the §Status note above.
4. Pair with the [RFC 0044 eval/golden-trace harness](0044-eval-set-golden-traces.md) as the safety net: a cuttable fold-in for v0.3.11 ([plan §Scope decisions](../v0.3.11-plan.md#scope-decisions-locked-at-plan-authoring-time-2026-06-28)) — autonomous = no human to catch a bad conversation, so automated conversation-quality gates matter more here than anywhere prior.

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
