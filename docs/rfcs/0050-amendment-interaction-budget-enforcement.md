# RFC 0050 Amendment — Interaction-Budget Enforcement (server-side resolution)

**Type**: amendment to [RFC 0050](0050-extensible-channel-configuration.md) — corrects the *"Open item 4"* framing and the [Phase 1 plan](0050-phase1-pr-plan.md)'s interaction-budget note
**Status**: ✅ Implemented — router-held budget + GET resolution (#657) and snapshot-at-open + wallet server-side enforcement (#658)
**Author**: Maksim Khomutov
**Date**: 2026-06-15
**Decision**: architecture resolved 2026-06-15 — **Option A (server-side resolution)**; see [Decision](#decision)
**Target**: v0.3.x (unscheduled)
**Trigger**: Implementing RFC 0050 "Open item 4" (make the per-channel `interaction_budget_tokens` override take effect live, like the other six knobs) surfaced that the framing — *"it is not router-held; add a setter or repoint the wallet"* — is incomplete. The knob is not merely *un-wired to the store*; it has **no end-to-end enforcement path at all**, from the store **or** from YAML. There is nothing live to wire an override into.
**Supersedes**: RFC 0050 *Open item 4* and the Phase 1 plan's "interaction budget is store-persisted but its live application lands later" note — both assume a latent live path that does not exist. This amendment defines that path.

---

## Table of Contents

- [Context](#context)
- [The gap RFC 0050 assumed away](#the-gap-rfc-0050-assumed-away)
- [Decision](#decision)
- [Design — server-side resolution](#design--server-side-resolution)
- [Why not Option B (agent-stamped via wire)](#why-not-option-b-agent-stamped-via-wire)
- [Semantics: snapshot at interaction open](#semantics-snapshot-at-interaction-open)
- [Implementation plan (PR sequence)](#implementation-plan-pr-sequence)
- [Files touched (estimated)](#files-touched-estimated)
- [Test strategy](#test-strategy)
- [Security considerations](#security-considerations)
- [Open questions](#open-questions)

---

## Context

RFC 0050 makes per-channel governance operator-editable, store-canonical, live.
Six of the seven governance knobs are router-held and enforced **entirely
orchestrator-side** in the router hot-path (`enforceReplyBudget`, `processEndVote`, floor control, …), so
the Phase 1 apply path wires a store override to them with a single setter call
(`applyOverridesToRouter`, `internal/channels/config_apply.go`). The seventh —
`interaction_budget_tokens`, the RFC 0030 Layer 1 per-interaction cost ceiling —
behaves differently, and the difference was under-appreciated when Open item 4 was
written.

## The gap RFC 0050 assumed away

Interaction-budget enforcement lives **agent-side**, and the enforcing number
**never leaves the orchestrator**. Traced end to end:

1. **Enforcement** is in the wallet: `WalletService.AcquireLease` denies an
   over-budget interaction (`internal/wallet/wallet.go:197` →
   `interactionCeilingDenialLocked`, `internal/wallet/interaction_budget.go`) and
   folds the running total (`wallet.go:255`) only when
   `req.GetInteractionBudgetTokens() > 0`. The ceiling and its tracking key
   (`interaction_id`) both arrive **on the `LeaseRequest`**, supplied by the
   **agent**.
2. **The wire carries no budget.** `ChannelMessageEvent` (`proto/task.proto`) has
   no metadata map and carries `interaction_id = 17` as a first-class field — but
   **no `interaction_budget_tokens` field**. So the orchestrator's resolved budget
   has no channel to reach the agent through.
3. **No call site stamps a ceiling.** The only code passing the field to a lease is
   `wallet_client.py:259` forwarding its own default-`0` parameter; **no caller
   passes a positive value**. The three lease call sites each note this in-line in
   their own words — `agents/salience_bid.py` is the explicit one: *"no call site
   stamps that ceiling yet … the config-stamping follow-up must thread the budget
   through here"*; `agents/llm_client.py` (*"until the config-stamping follow-up
   threads that ceiling, the id rides the wire and the wallet discards it"*) and
   `agents/persona_runtime/wallet_cause.py` (*"the wallet acts on the id only once a
   positive `interaction_budget_tokens` accompanies it … (the config-stamping
   follow-up)"*) corroborate.

**Consequence:** per-channel `interaction_budget_tokens` is enforced **nowhere
today** — not from the store, not from YAML. The wallet check, the
`LEASE_DENIED_REASON_INTERACTION_BUDGET_EXHAUSTED` reason, the agent-side
fail-closed handling, and the `interaction_id` wire attribution are all built;
only the *number* is missing. RFC 0050 Open item 4 is therefore not "wire the
override to a live path" — there is no live path. This amendment builds it, and
in doing so makes the override take effect (the original Open-item-4 goal) as a
direct consequence.

## Decision

**Option A — server-side resolution** (resolved 2026-06-15).

The wallet resolves the effective per-interaction budget **server-side**, from the
router (which already owns per-channel governance, store-canonical post-Phase 1),
rather than trusting an agent-supplied number. The agent's
`LeaseRequest.interaction_budget_tokens` becomes vestigial — the orchestrator no
longer depends on it.

Rationale:

- **G4 (single source of truth).** A cost ceiling is governance the *store* owns.
  The agent re-asserting it is the opposite of single-sourcing; an agent
  under-reporting its own ceiling (bug or otherwise) should not widen it. Moving
  resolution server-side makes the store authoritative at the enforcement point.
- **Smallest correct surface.** No proto change, no stub regen, no Python. The
  change is contained to `internal/channels` (a budget the router already could
  hold) and a thin resolver injected into `internal/wallet`.
- It corrects the trust model rather than extending it.

## Design — server-side resolution

Three pieces, no wire/agent changes.

**1. Make the budget router-held (mirrors the other six knobs).** Add to
`ChannelRouter`:

- `channelBudgets map[string]int64` (channel id → effective budget; `0` =
  uncapped) under a mutex, with `SetInteractionBudgetTokens(channelID, budget)` /
  `InteractionBudgetTokensFor(channelID) int64`, plus a `ResolveInteractionBudgets`
  boot method seeding it from `cfg` exactly like `ResolveReplyBudgets`. The
  Phase 1 apply path then wires the override: in `applyOverridesToRouter`, the
  branch that is *intentionally absent today* calls
  `SetInteractionBudgetTokens` (override present) or applies the captured fleet
  default (absent → inherit). `ResolveFromStore` re-overlays it on boot like the
  rest. **This alone makes the override live in the router and closes the
  GET `/config` `null` gap for an inherited budget** (`buildChannelConfigResponse`
  reads the new getter and returns the effective fleet-default value, instead of the
  `value: null` it emits today only when no per-channel override is present).

**2. Snapshot the budget onto the interaction at first commit.** When an
interaction first *commits* — its first persisted publish, in
`settleInteraction` (`internal/channels/interaction_resolver.go`), after
`interactionMu` is released so `budgetMu` never nests — both the channel id and
the resolved interaction id are in hand. Record
`interactionBudgetSnapshots[interactionID] = InteractionBudgetTokensFor(channelID)`
there, only for a capped channel (`> 0`). First-commit, not bare stamp-at-resolve,
mirrors how `idCommitted` / `lastActivity` already reconcile to persistence — a
rejected or never-committed publish leaves no snapshot. Evict one generation after
close via the same deferred discard seams as the end-vote tombstone
(`DiscardInteractionBudget` beside `DiscardInteractionEndVotes`), so a lease racing
the close still resolves. See [Semantics](#semantics-snapshot-at-interaction-open).

**3. Inject a resolver into the wallet; enforce server-side.** Give `WalletService` a
`SetInteractionBudgetResolver(func(interactionID string) (int64, bool))` injector
(closing over the router — the wallet gains a thin function dependency, not the whole
router). It is a **post-construction setter, not a `New…` option**: in
`cmd/orchestrator/main.go` the wallet is built (`NewWalletService`, ~L276) *before*
the channel router exists (`initChannels` → `NewChannelRouter`, ~L354), so there is
no router to close over at construction time. The resolver is wired in the window
after the router is built and before the gRPC server that serves `AcquireLease` is
registered (`newAgentGRPCServer`, ~L388). At `AcquireLease`, resolve the ceiling from
`req.GetInteractionId()` via the resolver; the resolved value is authoritative. The
agent-supplied `req.InteractionBudgetTokens` is ignored when a resolver is wired (kept
on the proto for back-compat / a no-resolver fallback so the change is additive).

```
operator edit ─▶ store (canonical) ─▶ router.channelBudgets[ch]
                                            │ (snapshot at interaction open)
                                            ▼
                              router.interactionBudgets[interaction_id]
                                            │ resolver(interaction_id)
                                            ▼
                         wallet.AcquireLease enforces  ◀── lease (interaction_id)
```

## Why not Option B (agent-stamped via wire)

Option B (add `interaction_budget_tokens` to `ChannelMessageEvent`, orchestrator
stamps it, agent lifts it onto **both** lease sites — quality turn + salience bid)
matches the existing agent-stamps-the-lease pattern and the proto comment's stated
"config-stamping follow-up." It was rejected because it is strictly more surface
(proto contract change + Go *and* Python stub regen + two agent stamping sites) for
a *weaker* trust model — the agent re-asserting a ceiling the orchestrator already
owns. The proto field it would add is exactly what Option A makes unnecessary.

## Semantics: snapshot at interaction open

The budget is snapshotted **when the interaction opens** (its first *committed*
publish) and is **stable for the life of that interaction**. A live edit
therefore applies to interactions opened *after* the edit, not retroactively to an
in-flight one. This is the chosen, defensible semantic: a per-interaction cost
ceiling that shifts mid-interaction is harder to reason about than one fixed at
open, and it matches how `interaction_id`-keyed Layer-1 tracking already
accumulates. (Resolving live per-lease is possible — read `channelBudgets` through
an `interaction_id → channel` association every acquire — but adds a second map and
a moving ceiling for no clear benefit; recorded as an [open question](#open-questions).)

**Boundary — the opening turn is ungoverned.** Because the snapshot is taken at
first *commit* and a lease is acquired *before* its message is published, the lease
that produces an interaction's opening message resolves before the snapshot exists
and is therefore not governed by that interaction's own ceiling; every lease after
the opening commit is. In the dominant flow this is a non-issue — the opening
message is an inbound (human / external) publish carrying no lease, so the snapshot
is in place before any agent reply leases. Only a fully agent-initiated opening
turn (a TICK-driven first post to a channel with no open interaction) escapes the
ceiling for that one turn — and because that lease resolves uncapped (budget 0) it
is not interaction-tracked either, so its tokens never accrue to the running total
the *next* lease is checked against. The effective ceiling for such an interaction
is therefore the configured budget plus the opening turn's (uncounted) spend; both
remain bounded by the always-on Layer 0 depth cap and the RFC 0023 dollar budget.
Closing it would require resolving the channel ceiling for
an interaction-less lease (the live-per-lease variant above), which this amendment
deliberately does not adopt.

## Implementation plan (PR sequence)

Two PRs, each < 500 lines, Go-only.

### PR 1 — Router-held interaction budget + apply-path wiring + GET gap

Make the budget router-held and store-wired (design piece 1). Deliverables:
`channelBudgets` map + setter/getter + `ResolveInteractionBudgets` boot seeding;
the `applyOverridesToRouter` budget branch (replacing the intentionally-absent
comment); `ResolveFromStore` coverage; `buildChannelConfigResponse` returns the
effective value (closing the `null` gap so the panel/CLI stop showing budget as
un-resolvable). **No enforcement behavior change yet** — the value is held and
surfaced but not yet consulted by the wallet. Tests: apply→getter reflects;
restart overlay; GET returns effective (override + inherited fleet default).

### PR 2 — Snapshot at stamp + wallet resolver + enforcement

Wire enforcement (design pieces 2–3). Deliverables: `interactionBudgets`
snapshot at the `interaction_resolver` stamp site + close-time eviction;
`SetInteractionBudgetResolver` on `WalletService` + server-side resolution at
`AcquireLease`; orchestrator wiring of the resolver in `cmd/orchestrator/main.go`
(set the router-backed resolver on the wallet after `initChannels`, before the gRPC
server is registered). Tests: an over-budget
interaction denies fail-closed via the *resolved* ceiling with no agent-supplied
value; a live edit applies to the next interaction; eviction on close; mixed-mode
(no resolver → legacy request-field behavior unchanged).

## Files touched (estimated)

| PR | Component | Files |
|----|-----------|-------|
| 1 | Go orchestrator | new `internal/channels/interaction_budget.go` (map+setter+getter+resolve), `config_apply.go` (apply branch + ResolveFromStore), `cmd/orchestrator/channels.go` (boot call), `internal/server/channel_config_handlers.go` (GET gap) |
| 2 | Go orchestrator + wallet | `internal/channels/interaction_resolver.go` (snapshot+evict), `internal/wallet/wallet.go` (resolver setter + AcquireLease), `cmd/orchestrator/main.go` (wire resolver post-`initChannels`) |

No `proto/`, no `agents/`, no stub regen.

## Test strategy

- **Unit (Go)**: budget setter/getter + boot resolve precedence; apply-path
  override vs inherit; snapshot-at-open + evict-on-close; wallet server-side
  resolution (resolved ceiling enforced; request field ignored when resolver
  present; legacy behavior when absent).
- **Integration**: operator edits budget via the Phase 1 `PATCH /config` →
  next interaction in that channel is enforced at the new ceiling; GET `/config`
  no longer returns `null` for an inherited budget.
- **Manual**: [MT-CHANNEL-CONFIG-003](../manual-tests/MT-CHANNEL-CONFIG-003.md) —
  set a tight budget on a channel, drive an interaction past it, confirm the
  `INTERACTION_BUDGET_EXHAUSTED` denial, then raise it and confirm relief on the
  next interaction. **Passed live 2026-06-16** (build `015149a`): a 500-token
  ceiling denied both mentioned personas' leases server-side (estimated ~5926 >
  500, `spent=0`, fail-closed); raising to 1,000,000 admitted the next
  interaction's leases (real reply landed). Note: a stale CLI warning
  (`channel_config.rs` "not yet enforced") was filed as a cosmetic follow-up
  ([ISSUE-0105](../issues/ISSUE-0105-stale-cli-interaction-budget-not-enforced-warning.md))
  — the Go enforcement is correct and verified.

## Security considerations

Tightens the trust boundary: the cost ceiling is no longer asserted by the agent
(which the wallet otherwise trusted), but resolved from store-canonical governance
at the enforcement point. An agent can no longer widen its own interaction budget
by under-reporting it — for every lease after the interaction's opening commit. The
opening turn itself is ungoverned by the per-interaction ceiling (see the
[Boundary](#semantics-snapshot-at-interaction-open) note); in the dominant
inbound-driven flow no agent lease falls in that turn, and where one can (a fully
agent-initiated opening post) the Layer 0 depth cap and the RFC 0023 dollar budget
still bound it. The resolver is a read-only function over in-memory router state —
no new external surface.

## Open questions

1. **Live-per-lease vs snapshot-at-open.** This amendment chooses snapshot-at-open
   ([Semantics](#semantics-snapshot-at-interaction-open)). Revisit only if an
   operator needs a mid-interaction ceiling change to bite immediately.
2. **Vestigial proto field.** `LeaseRequest.interaction_budget_tokens` stays for
   back-compat as a no-resolver fallback. Whether to later mark it deprecated (or
   remove the agent-side `interaction_budget_tokens` plumbing entirely) is a
   follow-up once Option A is the only path.
3. **TICK / non-channel leases.** Leases with no `interaction_id` (autonomous
   ticks, sub-agents) resolve to "no ceiling," unchanged from today. Confirm no
   channel-bound path reaches the wallet without a stamped `interaction_id`.
