# RFC 0030 Amendment — Floor Control / Speaker Serialization

**Type**: amendment to [RFC 0030](0030-multi-agent-conversation-governance.md) §A / §B / §F
**Status**: 📋 Proposed
**Author**: Maksim Khomutov
**Date**: 2026-06-03
**Target**: v0.3.6 (release blocker — brought forward ahead of RFC 0030's other Phase-1 layers)
**Trigger**: Manual end-to-end testing of multi-persona group channels: when a single message lands in a channel with two or more responding personas, every responder is dispatched **concurrently** and composes its reply against a transcript snapshot that does **not** contain any peer's reply (none exist yet). The result is N overlapping, mutually-blind replies to the same stimulus — a "mess" — plus the re-fanout amplification that motivated the [cascade-depth amendment](0011-amendment-cascade-depth-wire-propagation.md) (F-1). Cascade depth and reply budgets bound *volume*; neither bounds *order*, so the conversation is incoherent even when it is bounded.
**Supersedes**: RFC 0030 §A sub-problem **(c) "Fair turn-taking"** → *"Per-participant counter, deterministic — reply budget."* That row conflates two mechanisms. A reply **budget** answers "how many turns may a participant take"; it does **not** answer "in what order do concurrent responders speak, and does each see the others." This amendment splits (c) into its two halves and delivers the missing half — **floor control** — as Layer 2.5.

---

## Table of Contents

- [Context](#context)
- [The gap RFC 0030 left](#the-gap-rfc-0030-left)
- [The invariant this amendment establishes](#the-invariant-this-amendment-establishes)
- [Layer 2.5 — Floor control / speaker serialization](#layer-25--floor-control--speaker-serialization)
- [The floor loop](#the-floor-loop)
- [The turn-completion edge (reuse, not invent)](#the-turn-completion-edge-reuse-not-invent)
- [Nested-reply handling — the central decision](#nested-reply-handling--the-central-decision)
- [Ordering policy](#ordering-policy)
- [Composition with the existing layers](#composition-with-the-existing-layers)
- [Why this ships ahead of RFC 0030's other layers](#why-this-ships-ahead-of-rfc-0030s-other-layers)
- [Scope for v0.3.6 — in and out](#scope-for-v036--in-and-out)
- [Implementation plan (PR sequence)](#implementation-plan-pr-sequence)
- [Files touched (estimated)](#files-touched-estimated)
- [Test strategy](#test-strategy)
- [Security considerations](#security-considerations)
- [Open questions](#open-questions)
- [Related documentation](#related-documentation)

---

## Context

A publish to a channel is delivered by [`ChannelRouter.fanout`](../../internal/channels/fanout.go). For every member that is not the sender and is not `respond: never`, `fanout` spawns a goroutine (bounded by a 16-way semaphore) and dispatches the message **fire-and-forget** — it does not await the recipient's reply:

```go
// internal/channels/fanout.go
sem := make(chan struct{}, channelFanoutMaxConcurrency) // 16
for _, m := range members {
    // ... skip sender, skip RespondNever ...
    go func() { r.dispatcher.Dispatch(dispatchCtx, ...) }() // fire-and-forget
}
```

Each persona then composes its reply against the channel transcript **as it stood at dispatch time** ([`conversation_window.py`](../../agents/persona_runtime/conversation_window.py) reconstructs that transcript). Because all responders are dispatched at once, none of their replies exist yet when any of them reads — so every responder answers the *original* stimulus, blind to its peers.

Two consequences:

1. **Incoherence.** Four `always` personas produce four first-round replies that neither acknowledge nor build on one another. There is no "you go first, then I'll add to it" — only a simultaneous shout.
2. **Amplification.** Each of those replies re-enters [`ChannelRouter.Publish`](../../internal/channels/router.go) and fans out again, so round 2 is N replies to N replies. The [cascade-depth cap](0011-amendment-cascade-depth-wire-propagation.md) (Layer 0) eventually halts this, but only after the mess has already been written to history.

## The gap RFC 0030 left

RFC 0030 decomposes "conversation governance" into five sub-problems (§A) and lays them on six layers (§B). Every shipped and proposed layer answers a **volume / termination** question:

| Layer | Answers |
|-------|---------|
| 0 — cascade depth | "Is this a runaway loop?" (hard cap) |
| 1 — cost ceiling | "Has this cost too much?" (budget) |
| 2 — reply budget | "Has one participant taken too many turns?" (counter) |
| 3 — response gate | "Is this participant allowed to respond at all?" (policy) |
| 4 — end-vote | "Is everyone done?" (consensus) |
| 5 — moderator | "Is this still productive?" (judgement) |

**None of them answers "who speaks next, and does each responder see the previous one."** That is sub-problem (c) — but RFC 0030 reduced (c) to a *reply budget*, which is purely a counter ("at most N replies each"). A budget caps how often Morgan may speak; it does nothing to stop Alex, Jordan, and Morgan from all speaking *at the same instant, each blind to the other two*. Ordering and mutual visibility are a distinct mechanism — **floor control** — and RFC 0030 never specified it. This amendment supplies it.

## The invariant this amendment establishes

> **Within a channel, the responders to a given stimulus speak one at a time, in a deterministic order, and each composes its reply against a transcript that already includes every earlier speaker in that round.**

This is the property the current runtime silently assumes is unnecessary and the property a coherent multi-persona conversation requires. Everything below is in service of making it a code-enforced invariant rather than an emergent accident of dispatch timing.

## Layer 2.5 — Floor control / speaker serialization

A new layer slots into RFC 0030 §B **between Layer 2 (reply budget) and Layer 3 (response gate)**:

| Layer | Mechanism | Failure mode | Cost per check | Status |
|-------|-----------|--------------|----------------|--------|
| 2 | Per-participant reply budget | None — counter compare | hashmap lookup | 📋 RFC 0030 Phase 1 |
| **2.5** | **Floor control / speaker serialization** | **Floor-holder stalls → per-turn timeout advances** | **one in-flight dispatch + a parked waiter** | **📋 This amendment — v0.3.6** |
| 3 | Per-membership response gate | None — config lookup | ~free | ✅ Shipped (RFC 0011 §D) |

Floor control changes **how** a multi-responder publish is delivered — from concurrent fan-out to a serialized speaker round. It does not change *whether* a given persona is eligible to respond (that stays Layer 3) or *how many* turns it gets (that stays Layer 2).

## The floor loop

When a publish has **two or more** eligible responders (a single responder needs no serialization, and a DM has exactly one), `fanout` runs a **speaker round** instead of concurrent dispatch:

```
publish arrives (eligible responders = R, |R| >= 2)
    │
    ▼
acquire the channel's floor (per-channel; one round at a time)
    │
    ▼
order R  → [mentioned-first, then existing member order]
    │
    ▼
for each responder r in R:                          ◄── the floor loop
    │   register reply waiter for (channel, r)
    │   dispatch ONLY to r
    │   await r's reply  ─── or per-turn timeout (r passed / stalled)
    │   r's reply is persisted before the next dispatch
    │   (so responder r+1 reads it)
    │   release r; advance
    ▼
release the channel's floor
```

Because the next responder is dispatched only **after** the previous one's reply is durable, every responder's [`conversation_window`](../../agents/persona_runtime/conversation_window.py) reconstruction now contains the earlier speakers. The existing `DO_NOTHING` action becomes meaningful for the first time: a persona that sees its point already made can decline, and the response gate / prompt already support that outcome.

## The turn-completion edge (reuse, not invent)

The floor loop needs to know when a responder has *finished* its turn. The orchestrator already has exactly this primitive: [`replyWaiter`](../../internal/channels/waiter.go), built for the chat-as-DM `PublishAndAwait` flow.

- `replyWaiter.Register(channelID, senderID)` parks a buffered, single-shot waiter for the next `SEND_CHANNEL_MESSAGE` published by `senderID` on `channelID`.
- [`ChannelRouter.Publish`](../../internal/channels/router.go) already calls `r.waiter.Notify(msg)` after the store commit, on every publish.

The floor loop reuses this verbatim: for responder `r` it `Register(channel, r)`, dispatches, then awaits the channel (or the per-turn timeout). The reply re-entering `Publish` satisfies the waiter and advances the loop.

**The re-parking question is resolved by the key shape.** `replyWaiter` keys on `(channelID, senderID)` and `Register` returns `ErrWaiterAlreadyRegistered` on a duplicate key. The floor loop registers **one waiter per responder, sequentially** — responder A's waiter is `Register`ed, awaited, and `cancel()`ed before responder B's is registered. Distinct keys, no overlap, no clobber. The waiter is reusable as-is; no change to `waiter.go` is required for the minimal cut.

Single-shot semantics (a persona emitting `tool_call → final_answer` as two publishes — `waiter.go` `Notify` docstring, ISSUE-0033) are acceptable here: the **first** reply is a sufficient turn-completion edge, and the timeout backstops the no-reply case.

## Nested-reply handling — the central decision

A responder's reply re-enters `Publish` → which today calls `fanout` again. If left unchanged, each floor-turn reply would spawn its **own** competing fanout *while the round is still running* — re-introducing the very concurrency this amendment removes.

The reply must be **persisted** (so later responders read it) but its fanout must be **driven by the round loop, not by a parallel re-entrant fanout.** Two candidate mechanisms:

- **(Recommended) Defer fanout for floor-turn replies.** The orchestrator knows it dispatched `r` under an active round on this channel. It marks `r`'s expected reply so that when that reply hits `Publish`, the store commit and `waiter.Notify` still run but `fanout` is **skipped** — the round loop is the sole dispatcher, and it advances to the next responder with `r`'s reply now in history. Cross-*round* cascade (a reply that warrants a *new* round) remains bounded by `cascade_depth` (Layer 0).
- **(Alternative) Per-channel floor mutex only.** Keep re-entrant fanout but guard all fanout with a per-channel floor lock so rounds cannot overlap. Simpler to reason about, but it serializes *rounds* rather than collapsing the first-round storm — replies still each trigger a fresh round, so the amplification is reduced, not eliminated.

The recommended option is the one that actually delivers the invariant; the alternative is the fallback if reply-tagging proves fragile. **This is the single most important implementation decision and is called out as [OQ-1](#open-questions).**

## Ordering policy

For v0.3.6, ordering is deterministic and cheap — no LLM pre-pass:

1. **Mentioned-first.** Personas explicitly `@`-mentioned by the stimulus take the floor before un-mentioned `always` responders. Mentions already ride the wire ([`ChannelMessageEvent.mentions`](../../proto/task.proto), persisted per [`sqlite_mentions_test.go`](../../internal/channels/sqlite_mentions_test.go)).
2. **Then existing member order.** [`GetMembers`](../../internal/channels/sqlite_query.go) already returns members `ORDER BY joined_at ASC, participant_id ASC` — a stable, deterministic order the fanout loop already iterates. The floor loop reuses it directly; no new ordering state.

Relevance / "intent-to-speak" bidding (let each eligible persona signal *how much* it wants the floor and grant it to the strongest bidder) is the quality upgrade — explicitly **out of scope** here and deferred to RFC 0030 proper.

## Composition with the existing layers

Floor control composes cleanly; it does not replace anything:

- **Layer 0 (cascade depth)** still bounds cross-round cascade and remains the unfailable backstop. A floor round does not reset depth.
- **Layer 2 (reply budget, when it lands)** still caps per-participant turns; the floor loop simply skips a responder that is over budget.
- **Layer 3 (response gate)** still decides eligibility — the floor loop's responder set *is* the set Layer 3 admits.
- **Layer 4/5 (end-vote / moderator)** still close a conversation; the floor loop checks "is the round/interaction closed" before granting the next floor and stops early if so.

## Why this ships ahead of RFC 0030's other layers

RFC 0030 is 📋 Proposed (Draft) and its layers are sequenced across v0.3.x → v0.5.0. This amendment brings **one** layer forward because it is not an enhancement — it is a **correctness/usability blocker for v0.3.6**. The precedent is exact: the [cascade-depth amendment](0011-amendment-cascade-depth-wire-propagation.md) was likewise a "manual testing found this is broken, ship the fix now" response to F-1, landed ahead of the RFC 0030 governance work it belongs to. Floor control is the same shape of finding: multi-persona channels are not *usable* without it, so the layer that fixes it ships with the release that exposes the problem.

## Scope for v0.3.6 — in and out

**In scope (the minimal cut that removes the mess):**

- Serialized floor loop for multi-responder publishes (orchestrator-side).
- Reuse of `replyWaiter` for the turn-completion edge; per-turn timeout backstop.
- Deferred-fanout (or floor-mutex) handling of floor-turn replies (OQ-1).
- Deterministic ordering (mentioned-first, then existing member order).
- A per-channel feature flag so the behaviour can be disabled if it regresses (default **on** for group channels, **n/a** for DMs).

**Out of scope (→ RFC 0030 proper / a later amendment):**

- Relevance / intent-to-speak **bidding** for floor ordering.
- An explicit turn-complete **ack** on the dispatch RPC (the v0.3.6 edge is reply-or-timeout).
- An explicit **pass** signal (until then a silent responder costs one per-turn timeout).
- `interaction_id` scoping — RFC 0020 interactions are **not yet wired on the Go channel side** (only [`cascade_depth.go`](../../internal/channels/cascade_depth.go) exists); floor control keys off `(channel_id, stimulus message)`, not `interaction_id`.
- The moderator role, per-interaction cost ceiling, and per-participant reply budget (RFC 0030 Phases 1–2).

## Implementation plan (PR sequence)

A focused, mostly-orchestrator workstream. Each PR is independently reviewable; the feature is dark until PR 3 flips the flag default.

**PR 1 — Floor registry + ordering (no behaviour change).**
New `internal/channels/floor_control.go`: a per-channel floor registry (acquire/release, one round at a time) and the deterministic responder-ordering helper (mentioned-first over the existing `GetMembers` order). Unit-tested in isolation. `fanout` not yet rewired. Flag plumbing added, default **off**.

**PR 2 — The floor loop, behind the flag.**
Rewire `fanout`: when the flag is on and `|responders| >= 2`, run the serialized floor loop (register waiter → dispatch one → await reply-or-timeout → advance) instead of concurrent dispatch. Implement the recommended deferred-fanout handling for floor-turn replies (OQ-1). Single-responder and DM paths unchanged. Integration tests assert ordering, mutual visibility, and no-concurrent-dispatch.

**PR 3 — Default the flag on for group channels + docs + manual test.**
Flip the default; add the `docs/guides/channels.md` "Floor control" subsection; record `docs/manual-tests/MT-CHANNEL-GOV-002.md` (re-run the multi-persona scenario, observe ordered, mutually-aware replies). Update RFC 0030 §B layer table to reference Layer 2.5 as implemented.

**PR 4 (optional, fast-follow) — per-turn timeout tuning + telemetry.**
`channel.conversation.floor_turn{outcome=replied|timeout}` counter and a floor-round-duration histogram (RFC 0019 naming), so the latency cost is observable and the timeout default is data-driven.

## Files touched (estimated)

Estimates, not commitments. A full per-PR breakdown lands in `docs/rfcs/0030-pr-plan.md` if RFC 0030's broader Phase 1 proceeds; this amendment's PRs can also stand alone.

| Component | Files | Change |
|-----------|-------|--------|
| Go orchestrator | new `internal/channels/floor_control.go` | Per-channel floor registry; responder ordering; the floor loop |
| Go orchestrator | `internal/channels/fanout.go` | Multi-responder path → serialized floor loop (flag-gated) |
| Go orchestrator | `internal/channels/router.go` | Defer fanout for floor-turn replies; wire the floor loop to `replyWaiter` |
| Go orchestrator | `internal/channels/waiter.go` | None expected for the minimal cut (reused as-is) |
| Config / schema | `config/channels.yaml`, `schemas/channel.schema.json` | Per-channel `floor_control` flag + per-turn timeout override |
| Observability | `internal/observability/metrics/channel_instruments.go` | PR 4: floor-turn / round-duration instruments |
| Docs | `docs/guides/channels.md`, `docs/manual-tests/MT-CHANNEL-GOV-002.md` | Operator note + manual-test record |
| Tests | `internal/channels/*_test.go` | Floor registry, ordering, loop, timeout, deferred-fanout |

## Test strategy

**Unit (Go).**
- Floor registry: acquire/release; a second acquire on a held channel blocks/queues; release is idempotent-safe.
- Ordering helper: mentioned-first; stable tie-break by existing member order; empty / single-responder degenerate cases.
- Waiter sequencing: register → notify → cancel for responder A, then the same for B on the same channel, with no `ErrWaiterAlreadyRegistered`.
- Timeout: a responder that never replies advances the loop after the per-turn timeout.

**Integration.**
- Three `always` responders + one stimulus: assert exactly one dispatch is in flight at a time; assert responder 2's reconstructed transcript contains responder 1's reply; assert responder 3's contains both.
- Mention ordering: a stimulus that `@`-mentions responder C grants C the floor first.
- Deferred fanout: a floor-turn reply is persisted (visible in `GET /messages`) but does **not** spawn a competing fanout during the round.
- DM / single-responder: floor loop is a no-op; behaviour identical to today.

**Manual.**
- `MT-CHANNEL-GOV-002`: a group channel with three personas; one user prompt. Expected: three replies in deterministic order, each acknowledging/building on the prior, with at least one persona declining (`DO_NOTHING`) when its point is already covered — contrasted against the pre-amendment simultaneous-shout baseline.

## Security considerations

- **Floor stall / DoS.** A responder that holds the floor (slow or wedged LLM) must not freeze the channel. The per-turn timeout (reusing the [`channelFanoutPerRecipientTimeout`](../../internal/channels/fanout.go) shape) fail-opens by *advancing* the loop — a stalled responder loses its turn, the round continues. The floor is per-channel, so one channel's stall cannot block another.
- **No new principal or permission.** Floor control is a delivery-ordering change inside the existing fanout path. It introduces no new role, capability, or trust boundary (contrast Layer 5's moderator).
- **Latency is the explicit trade.** Responders go serial: round wall-clock is the *sum* of turn latencies, not the *max*. This is the deliberate price of coherence, bounded by the per-turn timeout and (future) a responder-per-round cap. It is observable via the PR 4 histogram and reversible via the feature flag.
- **In-process floor state.** Like `replyWaiter` itself ([waiter.go scaling note](../../internal/channels/waiter.go)), the floor registry is in-process and single-replica — consistent with v0.3.x's single-orchestrator deployment. Horizontal scale would need a cross-process floor primitive, flagged for the same future rollout that `replyWaiter` already defers.

## Open questions

1. **OQ-1: Nested-reply handling — deferred-fanout vs floor-mutex.** The recommended deferred-fanout option collapses the first-round storm and delivers the invariant; the floor-mutex alternative is simpler but only reduces amplification. *Lean: deferred-fanout,* with floor-mutex as the fallback if reply-tagging proves fragile across the REST publish boundary. **Gates PR 2.**
2. **OQ-2: Per-turn timeout default.** The existing per-recipient timeout is 5s (a stuck-dial guard). A floor turn includes a full LLM round-trip, so 5s is too tight. *Lean: a separate, larger floor-turn timeout (e.g. 30–60s), config-overridable;* calibrate from the PR 4 histogram.
3. **OQ-3: Mid-round mention reordering.** If responder A's reply `@`-mentions a not-yet-spoken member D who is already later in the queue, should D be promoted? *Lean: no for v0.3.6* — keep the round's order fixed at round start; mention-driven promotion is a bidding-adjacent refinement for RFC 0030 proper.
4. **OQ-4: Responder-per-round cap.** Should a round dispatch *all* eligible responders or cap at K to bound latency on large channels? *Lean: no hard cap in v0.3.6* (typical channels are small; Layer 0/2 still bound the worst case); revisit with the latency histogram.
5. **OQ-5: Interaction scoping.** Floor control keys off `(channel_id, stimulus)` because RFC 0020 interactions are not wired on the Go side. When they land (RFC 0030 Phase 1 / OQ-10), floor state should re-key to `interaction_id` for consistency with Layers 1/2/4 — a clean follow-up, not a v0.3.6 dependency.

## Related documentation

- [RFC 0030 — Multi-Agent Conversation Governance](0030-multi-agent-conversation-governance.md) — the parent RFC; this amendment adds its Layer 2.5 and splits sub-problem (c).
- [RFC 0011 Amendment — Cascade-Depth Wire Propagation](0011-amendment-cascade-depth-wire-propagation.md) — Layer 0; the precedent for a manual-testing-driven blocker amendment landed ahead of its parent RFC.
- [RFC 0011 — Channels & Internal Agent Messaging](0011-channels-bridges.md) — the channels stack and the response gate (Layer 3).
- [RFC 0020 — Interaction Lifecycle](0020-interaction-lifecycle.md) — the scope floor state will re-key to once it is wired on the Go side (OQ-5).
- [`internal/channels/fanout.go`](../../internal/channels/fanout.go) — the concurrent dispatch this amendment serializes.
- [`internal/channels/waiter.go`](../../internal/channels/waiter.go) — the `replyWaiter` reused as the turn-completion edge.
- [`internal/channels/sqlite_query.go`](../../internal/channels/sqlite_query.go) — `GetMembers`, the deterministic member order the floor loop reuses.
- [v0.3.6 Plan](../v0.3.6-plan.md) — the release this amendment is folded into as a blocker.
