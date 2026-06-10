# RFC 0030 Interaction-ID Producer — PR Implementation Plan (v0.3.8 scope: activate Layers 1/2/4)

**Type**: PR implementation plan — the `interaction_id` producer the [governance-layers plan](0030-governance-layers-pr-plan.md) deliberately scoped out ("wired and tested ahead of the producer, not yet load-bearing")
**Status**: 📋 Proposed
**Author**: Maksim Khomutov
**Date**: 2026-06-10
**Target**: v0.3.8
**Depends on**: governance-layers PRs 1–5 (landed v0.3.8, inert); RFC 0020 §B/§G (lifecycle + scope rules); RFC 0023 leasing (`AcquireLease.interaction_id`, landed)

---

## Overview

Layers 1/2/4 (per-interaction cost ceiling, reply budget, end-of-interaction vote) are built, composed, and tested — and inert, because no producer writes `interaction_id` onto real publish traffic ([`readInteractionID`](../../internal/channels/interaction_id.go) returns `""` on every publish; the Python [`END_INTERACTION_VOTE`](../../agents/persona_types.py) action returns `not_implemented`). The practical consequence is the convergence gap the floor-capable-directedness amendment's field report surfaced: with no semantic terminator live, the Layer 0 depth cap is the *de facto* conversation terminator — it ends discussions by length, not completion. This plan ships the producer and the vote path, making "two participants say we're done" the normal close and demoting depth-5 drops to the regression signal [RFC 0030 §D](0030-multi-agent-conversation-governance.md#d-layer-0--cascade-depth-backstop-shipped) intends them to be.

Three PRs: the Go resolver (the producer proper), the Python vote action + lease threading, and the activation closeout.

## Decisions locked at plan-authoring time

- **IP1 — the producer is the orchestrator, not the agent.** `ChannelRouter` resolves the open interaction for a channel on the publish path and stamps `msg.Metadata["interaction_id"]` before persistence — the same site and posture as cascade-depth clamping. Rationale: the orchestrator sits on the trust boundary (RFC 0030 §C names it the cross-process keyer); a single resolver covers *every* publisher (persona, console, CLI, programmatic REST) with no agent-echo dependency; and agents already *read* the id off the wire ([`seed_wire_metadata`](../../agents/channel_wire_metadata.py)) without producing it. RFC 0020's agent-side `InteractionTracker` keeps minting its own per-agent ids for memory scoping — unification is OQ 1, not a blocker.
- **IP2 — the router's resolution is authoritative; inbound claims never key governance state.** Today `readInteractionID` trusts the publish metadata bag, which is exactly the "attacker-influenceable 128-byte token keys a per-interaction map for the orchestrator's lifetime" hazard the [reply-budget hardening notes](../../internal/channels/reply_budget.go) flag. The resolver replaces any inbound `interaction_id` claim on channel publishes with its own resolved value (logged at debug when they differ, for diagnosis); only router-minted ids ever reach `replyCounts` / `endVotes` / the wallet. `readInteractionID` survives as the read helper for the router's own stamped value downstream of resolution.
- **IP3 — scope per RFC 0020 §G, keyed by `channel_id`.** `group` / `dm`: one open interaction per channel per idle window. `thread`: the thread *is* the interaction — idle rotation disabled (a per-channel-type rule, not a config knob). Idle window: `channels.yaml: interaction_idle_timeout_seconds`, fleet default + per-channel override, default **600** — the same value as the agent-side `interaction_idle_timeout_sec` ([`persona_runtime/__init__.py`](../../agents/persona_runtime/__init__.py)) so the two trackers' boundaries roughly coincide by default.
- **IP4 — lazy idle rotation on the publish path; no background janitor.** Resolution at publish: if the channel has an open id and `now − last_activity ≤ window`, reuse it and bump the timer; past the window, close the old id — fire **both** discard seams (`DiscardInteractionReplyBudget`, `DiscardInteractionEndVotes`) and `interaction_closed{trigger=idle}` — and mint a fresh uuid. The open-interaction table is bounded by the channel count (`max_channels`), so idle entries lingering between publishes are bounded state, not a leak. Reopen rule inherits RFC 0020 §C verbatim: never reopen. A janitor (prompt idle-close telemetry between publishes) is additive later if wanted (OQ 3).
- **IP5 — restart loses the open-interaction table; the next publish mints fresh.** RFC 0020 §C inheritance, stated for governance explicitly: the maps keyed by the lost ids (`replyCounts`, `endVotes`, `closedInteractions`) died with the process too, so nothing leaks and nothing needs recovery. Durable open-interaction state remains the larger change RFC 0020 defers.
- **IP6 — the vote producer is the agent action, riding the existing metadata bag.** `END_INTERACTION_VOTE` publishes a real channel message (the vote *is* a message — the landed `processEndVote` runs post-persistence) with `end_interaction_vote: true` merged into the publish metadata ([`ChannelPublisher.publish`](../../agents/channel_publisher.py) already accepts a caller metadata map). The router scopes the vote to **its own resolved id** (IP2), never to a publisher-claimed one, so a spoofed vote can at worst vote once in the interaction the spoofer is genuinely publishing into — the per-(participant, interaction) dedupe and W-turn recency window (landed, PR 4) bound the rest.
- **IP7 — the ordering constraint is discharged in-PR.** [`reply_budget.go`](../../internal/channels/reply_budget.go) and [`end_vote.go`](../../internal/channels/end_vote.go) both pin "the producer MUST NOT be enabled before the close-path discards are wired". PR 1 lands the resolver and the idle-rotation discard wiring in the same change; the vote-close discards are already wired (PR 4). There is no commit at which ids flow and a discard seam is unreachable.

## What activates when

| Layer | Activation condition after this plan | Default outcome |
|---|---|---|
| 4 — end-vote | ids flow (PR 1) + agents can vote (PR 2) | **Live**: K=2 distinct votes within W=3 turns close the interaction and stop fanout — the semantic terminator |
| 2 — reply budget | ids flow (PR 1); enforcement needs `max_replies_per_participant_per_interaction > 0` | Inert by default (0 = uncapped) — opt-in unchanged |
| 1 — cost ceiling | ids flow (PR 1) + the agent threads the id into `AcquireLease` (PR 2); needs `interaction_budget_tokens > 0` | Inert by default (0 = uncapped) — opt-in unchanged |

Default-config behaviour change is therefore exactly one thing: **conversations can end because the participants said so.**

## PR Sequence

### PR 1: `feature/v038-rfc0030-interaction-id-resolver` — the orchestrator producer

| Area | Change |
|---|---|
| `internal/channels/interaction_resolver.go` (new, carved per the `reply_budget.go` pattern) | Per-channel open-interaction table on `ChannelRouter` (`map[channel_id]{id, lastActivity}` under its own mutex): resolve-or-mint, lazy idle rotation firing both discard seams + `interaction_closed{trigger=idle}`, thread-type exemption (IP3), authoritative override of inbound claims (IP2). |
| `internal/channels/router_publish_async.go` | Call the resolver in `publishCommit` next to the cascade-depth clamp; stamp the resolved id onto `msg.Metadata` so it persists and rides the existing fanout lift to `ChannelMessageEvent.interaction_id`. |
| `internal/channels/config.go` / `config_validate.go` / `schemas/channel.schema.json` / `config/channels.yaml` | `interaction_idle_timeout_seconds` (fleet default + per-channel), default 600, ≥ 0 validation (0 = idle rotation off — the documented thread posture, also usable per-channel). |
| Tests | Rotation matrix: reuse-within-window; rotate-after-idle (old id's budget/vote state discarded — the leak pin); thread channels never rotate; inbound spoofed id overridden and never keys a map; vote-close → next publish mints fresh; restart-fresh documented (in-memory table, not unit-testable beyond construction). |

TDD anchor (red first): a publish to a governed channel carries a non-empty `interaction_id` on the dispatched `ChannelMessageEvent`, and a second publish 601s later carries a *different* one with the first id's reply-budget state discarded.

### PR 2: `feature/v038-rfc0030-end-vote-action` — the Python vote + lease threading

| Area | Change |
|---|---|
| `agents/action_executor.py` | Implement `END_INTERACTION_VOTE`: publish a short channel message via the REST publisher with `end_interaction_vote: true` metadata (IP6); the legacy in-process dispatcher path keeps `not_implemented` (votes are a channels-governance concept; the chat path has no interaction router). |
| Persona prompt vocabulary | The action-vocabulary prompt section gains the vote: *emit when your contribution is complete and you have nothing further to add — two members voting closes the discussion*. This is the prompt half of Layer 4's social contract; the gate/router half is already live. |
| Lease threading | `WalletClient.lease()` already accepts `interaction_id` (governance PR 2) but no channel-path call site passes it: thread `event.metadata["interaction_id"]` into the Tier C turn lease and the Tier B salience-bid lease for `CAUSE_CHANNEL_MESSAGE`, activating Layer 1 attribution end-to-end. |
| Tests + drift pin | Action unit tests (publish carries the flag; metadata merge does not clobber reserved keys); a cross-language pin for the `end_interaction_vote` / `interaction_id` metadata key literals (Go readers ↔ Python writers — the `floor_mentions` drift-file pattern); lease-threading unit test. |

### PR 3: `feature/v038-rfc0030-convergence-closeout` — activation closeout

- **Convergence MT** (the governance master-plan's Phase 3 acceptance): N personas, an open question, discussion proceeds, two emit votes, the interaction closes with `trigger=end_votes`, fanout stops *before* the depth cap, and the interaction-summary surface hands back a readable outcome.
- Docs: channels guide §governance (how a conversation ends now), CHANGELOG, ROADMAP RFC 0030 row, this plan's status, the governance-layers plan's "inert until the producer" notes annotated as discharged.
- Telemetry sanity: `interaction_closed{trigger}` now emits on real traffic; `governance_drop{layer=depth}` becoming rare is the success signal.

## Mixed-version analysis

- **New orchestrator + old agent**: ids are stamped and ride the wire; an old agent ignores them and never votes — interactions close by idle only. No breakage; Layer 2 still counts its publishes server-side.
- **Old orchestrator + new agent**: the agent's vote flag and lease ids reference nothing; `readEndInteractionVote` doesn't exist pre-v0.3.8, the metadata key is inert ballast. No breakage.
- **The id on the wire**: already additive by the governance PR 1 contract (empty = untracked, every layer at its uncapped default).

## Open questions

1. **Agent-side tracker unification.** Should the RFC 0020 `InteractionTracker` adopt the orchestrator's wire id for channel scopes (one id per conversation across both trackers, aligning episodic summaries with governance closes)? Deferred to a follow-on — the summary surface currently keys the agent's own ids and works; unification is a memory-quality improvement, not a governance dependency.
2. **Cost-ceiling close.** Layer 1 exhaustion denies leases but does not *close* the interaction (`trigger=cost` is reserved on the instrument). Idle rotation eventually closes it; an explicit close-on-exhaustion needs the wallet→router signal path and is deferred until a real budget is configured anywhere.
3. **Idle-close janitor.** Lazy rotation closes an idle interaction only on the *next* publish, so `interaction_closed{trigger=idle}` lags. Add a timer sweep only if the lag bothers the summary surface or operators; state is bounded either way (IP4).
4. **Console surfacing.** Should the web console show the open interaction and its close events on the channel timeline? RFC 0048-era affordance; nothing here blocks it.

## Related documentation

- [RFC 0030 governance layers PR plan](0030-governance-layers-pr-plan.md) — the landed-inert Layers 1/2/4 this plan activates
- [RFC 0020 §B/§C/§G](0020-interaction-lifecycle.md) — lifecycle, restart semantics, and the scope rules IP3 reuses
- [RFC 0023](0023-llm-call-leasing.md) — the lease surface Layer 1 attribution rides
- [`internal/channels/interaction_id.go`](../../internal/channels/interaction_id.go) / [`reply_budget.go`](../../internal/channels/reply_budget.go) / [`end_vote.go`](../../internal/channels/end_vote.go) — the inert substrate and its hardening constraints
- [Floor-capable-directedness amendment](0030-amendment-floor-capable-directedness.md) — the sibling convergence fix; its field report motivates the terminator this plan makes live
