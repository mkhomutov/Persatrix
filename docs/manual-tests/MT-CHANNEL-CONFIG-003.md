# Manual Test MT-CHANNEL-CONFIG-003: Edit a channel's interaction budget at runtime — the wallet enforces the new ceiling server-side, and raising it relieves the next interaction

**Test ID**: `MT-CHANNEL-CONFIG-003`
**Feature Area**: Channels (operator-editable channel configuration — RFC 0050 + the [interaction-budget amendment](../rfcs/0050-amendment-interaction-budget-enforcement.md): the seventh knob, `interaction_budget_tokens`, made router-held and enforced server-side)
**Version**: 1.0
**Created**: 2026-06-16
**Last Updated**: 2026-06-16
**Status**: Active

---

## Overview

**Purpose**: Verify the RFC 0050
[interaction-budget amendment](../rfcs/0050-amendment-interaction-budget-enforcement.md)
end-to-end with the real orchestrator. The amendment closes RFC 0050 "Open
item 4" by giving `interaction_budget_tokens` — the RFC 0030 Layer 1
per-interaction cost ceiling — an enforcement path it never had: the budget is
now **router-held** (seeded at boot, store-canonical post-Phase 1), the wallet
resolves it **server-side** at `AcquireLease` from the interaction's snapshot
(not from the agent-supplied request field), and an over-budget lease is denied
fail-closed with `LEASE_DENIED_REASON_INTERACTION_BUDGET_EXHAUSTED`. This MT is
the live half; the deterministic half is pinned by the Go suites named below.

It exercises three things the amendment claims and the other two MTs do not:

1. **The GET `/config` `null` gap is closed.** Before the amendment an inherited
   `interaction_budget_tokens` read back `value: null` (Open item 4 — the knob
   was store-persisted but never router-resolved). Now `GET …/config` returns the
   effective value (the fleet default, `0` = uncapped), so the CLI and the panel
   show a resolvable number, not `null`.
2. **Server-side enforcement.** An operator sets a tight budget; the wallet
   denies the next interaction's agent leases at the **resolved** ceiling, with
   **no agent-supplied value** in play (the trust-model tightening — an agent can
   no longer widen its own ceiling by under-reporting it).
3. **Snapshot-at-open / raise-relieves-next.** The ceiling is snapshotted when an
   interaction opens (its first committed publish) and is stable for that
   interaction's life; a live edit applies to interactions opened *after* it.
   Raising the budget therefore relieves the **next** interaction, demonstrated by
   driving a fresh opener after the raise.

**Why `interaction_budget_tokens` here and not in MT-CHANNEL-CONFIG-001/002**:
those two arcs deliberately avoid it — pre-amendment it was the one knob with no
live enforcement path (it read back `null` and took effect nowhere), so they use
`interaction_idle_timeout_seconds` for their live-honor step and call the budget
out as out of scope. This MT is the budget's dedicated arc, runnable only after
the amendment (#657, #658) landed.

**Scope**: the default `planning` group channel; the CLI `channel config
get`/`set`/`unset` verbs over `interaction_budget_tokens` (the same toggle-gated
apply path the other knobs use); the `GET …/config` effective-value resolution;
the wallet's server-side denial on a capped interaction; and the
raise-relieves-the-next-interaction semantic. The denial is observed in the
orchestrator log line emitted by `interactionCeilingDenialLocked`
([`internal/wallet/interaction_budget.go`](../../internal/wallet/interaction_budget.go)).

**Out of scope**: the web panel's *rendering* of the budget value (that the
inherited value shows as the effective `0`, not `null` and not coerced — covered
by [MT-CHANNEL-CONFIG-002](MT-CHANNEL-CONFIG-002.md)'s render note and
`ChannelSettings.test.js`); restart survival of the override (the store-vs-YAML
revision gate is surface- and knob-agnostic, covered by
[MT-CHANNEL-CONFIG-001 Step 4](MT-CHANNEL-CONFIG-001.md#step-4-restart--the-live-edit-survives-the-yaml-seed-does-not-clobber-it));
the opening-turn boundary (a fully agent-initiated opener escaping its own
ceiling for one turn — a documented, bounded
[semantic](../rfcs/0050-amendment-interaction-budget-enforcement.md#semantics-snapshot-at-interaction-open),
not naturally reachable in the inbound-driven flow this MT drives).

---

## Related Documentation

- [RFC 0050 — Extensible Channel Configuration](../rfcs/0050-extensible-channel-configuration.md) — the truth model; Open item 4 this amendment closes
- [RFC 0050 amendment — Interaction-Budget Enforcement](../rfcs/0050-amendment-interaction-budget-enforcement.md) — the server-side-resolution design this MT accepts (its *Test strategy* names this arc)
- [RFC 0030 — Multi-Agent Conversation Governance](../rfcs/0030-multi-agent-conversation-governance.md) — Layer 1, the per-interaction cost ceiling
- [MT-CHANNEL-CONFIG-001](MT-CHANNEL-CONFIG-001.md) / [MT-CHANNEL-CONFIG-002](MT-CHANNEL-CONFIG-002.md) — the CLI / web siblings (idle timeout; restart survival; G4 read-back)
- [Channels guide § Editing governance config at runtime](../guides/channels.md#editing-governance-config-at-runtime--channel-config-rfc-0050-phase-1) — the shared knob semantics

**Related Automated Tests** (the deterministic half — amendment is Go-only):
- [`wallet_interaction_budget_test.go`](../../internal/wallet/wallet_interaction_budget_test.go) — the ceiling denial + reason, running-total accrual
- [`wallet_interaction_budget_resolver_test.go`](../../internal/wallet/wallet_interaction_budget_resolver_test.go) — server-side resolution: resolved ceiling enforced, request field ignored when a resolver is wired, legacy fallback when absent
- `internal/channels` budget tests — `SetInteractionBudgetTokens`/`InteractionBudgetTokensFor`, `ResolveInteractionBudgets` boot seeding, the `applyOverridesToRouter` budget branch, snapshot-at-open + evict-on-close

---

## Preconditions

Same base as [MT-CHANNEL-CONFIG-001 § Preconditions](MT-CHANNEL-CONFIG-001.md)
(the console/orchestrator running with channels wired and a provider overlay so
personas actually reply and consume tokens), with the config-edit toggle on.

1. The config-edit surface is enabled. As of #660,
   `panels.channel_timeline.config_edit_enabled: true` is the shipped default in
   [`config/ui.yaml`](../../config/ui.yaml), so the CLI `channel config` verbs are
   ungated out of the box. (Pre-#660 builds must flip it on, as in
   MT-CHANNEL-CONFIG-001/002.)

2. Bring the fleet up with a **real provider overlay** — the budget can only be
   exercised by leases that actually estimate tokens, so the offline mock (which
   does not drive realistic estimates) is unsuitable:

   ```bash
   make reset
   ENABLE_UI=1 docker compose -f docker-compose.yaml -f docker-compose.anthropic.yaml up -d --build
   # (or the openai overlay)
   ```

3. Have the orchestrator log tailing in a shell so the denial is observable:

   ```bash
   docker compose logs -f orchestrator | grep -iE "interaction cost ceiling|interaction_budget|lease denied"
   ```

---

## Test Procedure

### Step 1: Baseline — the inherited budget resolves (no more `null`)

Before any edit, read the channel's effective config:

```bash
./bin/persatrix channel config get planning
```

**Expected**:
- `interaction_budget_tokens` = **0**, source **`default`** — the effective
  fleet default (`0` = uncapped), **not `null`**. This is the closed Open-item-4
  GET gap: pre-amendment an inherited budget read back `null`; now it resolves to
  the inherited effective value.
- `revision` = **0** (no override yet).

**Verification**:
- [ ] `interaction_budget_tokens` reads `0` / `default` (a resolved number, not `null`); revision 0.

### Step 2: Set a tight budget — the override lands and reads back

```bash
./bin/persatrix channel config set planning interaction_budget_tokens=500
./bin/persatrix channel config get planning
```

**Expected**:
- The set succeeds; `config get` now shows `interaction_budget_tokens` = **500**,
  source **`channel`**, `revision` bumped to **1**.
- The orchestrator logs the apply landing on the router
  (`SetInteractionBudgetTokens` after `PutChannelConfig`) — no restart.
- 500 is **below a single persona lease's estimate** (a quality turn's
  `estimated_input_tokens` alone — the flattened system prompt + message — is
  well over 500), so the next interaction's first agent lease will trip the
  ceiling. This is deliberate: a clean, deterministic denial rather than a
  fragile "allow N then deny" boundary.

**Verification**:
- [ ] `config get` reads back 500 / `channel` / revision 1; no restart.

### Step 3: Drive a fresh interaction — the wallet denies the agent leases server-side

Open a **new** interaction (a fresh inbound human publish — this becomes the
interaction's first commit, so the snapshot is taken at 500 before any agent
reply leases):

```bash
./bin/persatrix channel send planning \
  "Quick gut-check: what's the single biggest risk to shipping v1 on time?" \
  --as alex --mention iron-fox --mention nova-sparrow
```

**Expected**:
- In the orchestrator log tail, a denial fires for the mentioned personas' reply
  leases:

  ```
  wallet: lease denied — interaction cost ceiling exceeded
    layer=cost interaction_id=… agent_id=… spent_tokens=0 estimated_tokens=… interaction_budget_tokens=500
  ```

  i.e. `LEASE_DENIED_REASON_INTERACTION_BUDGET_EXHAUSTED`, with
  `interaction_budget_tokens=500` — the **resolved** ceiling, enforced
  server-side. No agent-supplied budget is in play (the resolver is authoritative;
  the request field is ignored).
- The personas **fail closed**: no quality-turn reply from the mentioned personas
  is persisted to the channel for this interaction (the LLM call does not happen —
  GL5). The opening human message stands; the agent replies it would normally
  draw do not land.

**Verification**:
- [ ] A `LEASE_DENIED_REASON_INTERACTION_BUDGET_EXHAUSTED` denial is logged for this interaction at `interaction_budget_tokens=500`; no over-budget persona reply is persisted.

### Step 4: Raise the budget — the next interaction is relieved (snapshot-at-open)

Raise the ceiling generously, then open **another fresh** interaction:

```bash
./bin/persatrix channel config set planning interaction_budget_tokens=1000000
./bin/persatrix channel config get planning        # → 1000000 / channel / revision 2
./bin/persatrix channel send planning \
  "Thanks — now name one concrete mitigation for that risk, one sentence." \
  --as alex --mention iron-fox --mention nova-sparrow
```

**Expected**:
- `config get` shows **1000000** / `channel` / `revision` **2** (revision only
  increases).
- The **new** interaction's agent leases are **admitted** (no
  `interaction_budget_tokens` denial in the log for this interaction's id), and the
  mentioned personas reply normally — relief on the next interaction.
- This is the snapshot-at-open semantic made visible: the raise governs
  interactions opened *after* the edit. (The Step 3 interaction, already opened at
  500, is not retroactively relieved — but it is also already over, so the
  observable contract here is "the next interaction picks up the new ceiling.")

**Verification**:
- [ ] After the raise, a freshly-opened interaction's persona leases are admitted (no ceiling denial) and replies land — the new budget governs the next interaction.

### Step 5: Unset — back to inherited uncapped, and the GET gap stays closed

```bash
./bin/persatrix channel config unset planning interaction_budget_tokens
./bin/persatrix channel config get planning
```

**Expected**:
- `interaction_budget_tokens` reverts to **0**, source **`default`** (inherited,
  uncapped), `revision` bumped to **3**. Still a resolved `0`, never `null`.
- A subsequent fresh interaction runs uncapped at this layer (the always-on Layer 0
  depth cap and the RFC 0023 dollar budget remain the nets).

**Verification**:
- [ ] Unset returns the knob to 0 / `default` (resolved, not `null`) at revision 3; the next interaction is uncapped at Layer 1.

---

## Expected Results Summary

| Step | Expected Outcome | Pass/Fail |
|------|-----------------|-----------|
| 1 | Inherited `interaction_budget_tokens` reads `0` / `default` — a resolved value, not `null` (Open-item-4 GET gap closed); revision 0 | ☐ |
| 2 | `set …=500` lands; `config get` reads 500 / `channel` / revision 1; no restart | ☐ |
| 3 | Fresh interaction → `LEASE_DENIED_REASON_INTERACTION_BUDGET_EXHAUSTED` at budget 500, enforced server-side; personas fail closed (no over-budget reply persisted) | ☐ |
| 4 | Raise to 1000000 (revision 2) → next fresh interaction's leases admitted, replies land (snapshot-at-open: raise relieves the next interaction) | ☐ |
| 5 | Unset → 0 / `default` (resolved, not `null`), revision 3; next interaction uncapped at Layer 1 | ☐ |

---

## Edge Cases & Error Scenarios

### Edge Case 1: Negative budget is rejected

```bash
./bin/persatrix channel config set planning interaction_budget_tokens=-1
```

**Expected**: rejected at validation (negative is invalid; zero is the meaningful
"uncapped", a positive int is a real ceiling) — nothing persists, revision
unchanged. The apply path validates against the schema before touching the router.

### Edge Case 2: Toggle off → the CLI verb is gated

With `config_edit_enabled: false` (pre-#660 default) and a restart, the same
`channel config set planning interaction_budget_tokens=…` returns **403** — the
budget knob rides the same uniform toggle gate as every other knob (pinned by the
server-side handler tests). Not re-exercised here if the shipped default (on) is
in effect; noted for parity with MT-CHANNEL-CONFIG-001 EC1.

### Edge Case 3: Mid-interaction edit does not move the in-flight ceiling

Open a capped interaction (budget 500), then — before it closes — raise the
budget. The **in-flight** interaction is still governed by its snapshot (500),
because the ceiling is fixed at open; only the **next** interaction sees the new
value (Step 4). This is the chosen
[snapshot-at-open semantic](../rfcs/0050-amendment-interaction-budget-enforcement.md#semantics-snapshot-at-interaction-open);
it is hard to observe deterministically live (the in-flight interaction is already
denied) and is pinned by the Go snapshot/evict tests — recorded here as the
defining property, verified by Step 4's "next interaction" framing.

---

## Test Results

| Date | Tester | Build | Result | Notes |
|------|--------|-------|--------|-------|
| _pending_ | | | ☐ Not yet run | First live exercise of the interaction-budget amendment (server-side enforcement). |
| 2026-06-16 | Maksim Khomutov | `015149a` (Anthropic overlay, `ENABLE_UI=1 up --build`) | **PASS (with 1 procedure refinement + 1 cosmetic bug filed)** | First live exercise; server-side enforcement confirmed. **Step 1**: inherited budget read `0` / `default` (resolved, not `null`) at revision 0 — Open-item-4 GET gap confirmed closed. **Step 2**: `set …=500` → 500 / `channel` / revision 1 (post-ISSUE-0103 the first edit seeds all knobs `[channel]`; chair `nova-sparrow` survived). **Step 3** ✅ the decisive step: a fresh opener (interaction `c576ca66`) → **both** mentioned personas denied server-side — `iron-fox estimated_tokens=5926`, `nova-sparrow estimated_tokens=5927`, each `> interaction_budget_tokens=500`, `spent_tokens=0`, `cause=CAUSE_CHANNEL_MESSAGE`, reason `INTERACTION_BUDGET_EXHAUSTED` (logged by `interactionCeilingDenialLocked`). The resolved ceiling (500) is the store value — no agent-supplied number in play. Personas **failed closed**: the only persisted "replies" were the fail-closed denial notices (`interaction "c576ca66" cost ceiling exceeded: 0 spent + 5926 estimated > 500 budget tokens`), **not** quality turns — the LLM call did not happen (GL5). **Step 4** ✅ raised to 1000000 (revision 2); drove a fresh opener — interaction **rotated** (`rotated:true`) and iron-fox posted a **real** reply ("From a reliability standpoint: the biggest risk is undiscovered data-integrity issues…"), **no** ceiling denial on the new interaction id. Relief on the next interaction (snapshot-at-open) confirmed. **Step 5** ✅ unset → 0 / `default` / revision 3 (revision only increases). **Procedure refinement**: to open a *fresh* interaction without waiting out the 600 s idle window, also set `interaction_idle_timeout_seconds=20` in Step 4 so the interaction rotates (confirmed live: `rotated:true, window:20`); a same-window re-send would have stayed on `c576ca66` (pinned at 500) and is the wrong test. **Cosmetic bug filed**: the CLI `channel config get/set` still prints `interaction_budget_tokens … ⚠ not yet enforced (RFC 0050 Open item 4)` ([`cli/src/commands/channel_config.rs:290`](../../cli/src/commands/channel_config.rs)) — stale post-amendment messaging; enforcement *is* wired (`cmd/orchestrator/channels.go:175` sets the resolver) and demonstrably works here. The Go side is correct; only the Rust CLI label lags. |

## Notes

- **Keep `git diff` clean.** Revert any `interaction_budget_tokens` override
  after the run (`channel config unset planning interaction_budget_tokens`, or
  `make reset`), as the `MT-CHANNEL-GOV-*` arcs do, so the recorded build is
  honest about what shipped.
- **Use a real provider.** The budget is only exercised by leases that estimate
  real tokens; the offline mock does not drive realistic estimates, so the
  denial would not reproduce faithfully.
- **The opening turn is ungoverned by design.** In this inbound-driven arc the
  opener is a human `channel send` carrying no lease, so the snapshot is in place
  before any agent reply leases — every persona lease in the interaction is
  governed. A fully agent-initiated opener (a TICK-driven first post) would escape
  its own ceiling for that one turn; that boundary is bounded by Layer 0 / RFC 0023
  and is out of scope here (see the amendment's
  [Boundary](../rfcs/0050-amendment-interaction-budget-enforcement.md#semantics-snapshot-at-interaction-open)
  note).
