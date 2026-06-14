# Manual Test MT-CHANNEL-CONFIG-001: Live-edit a governance knob from the CLI — the running channel honors it without restart, and it survives one

**Test ID**: `MT-CHANNEL-CONFIG-001`
**Feature Area**: Channels (operator-editable channel configuration — RFC 0050 Phase 1, the store-canonical apply path + CLI surface)
**Version**: 1.0
**Created**: 2026-06-14
**Last Updated**: 2026-06-14
**Status**: Active

---

## Overview

**Purpose**: Verify RFC 0050 Phase 1's primary goal **G1 (operator
self-service)** end-to-end with the real orchestrator: an operator changes a
live channel's governance knob from the CLI — **no YAML edit, no restart** —
the change takes effect on the running channel immediately, and it **survives a
restart** (the store, not the YAML seed, is the source of truth, gated by the
per-channel revision). This is the live half of the
[PR plan](../rfcs/0050-phase1-pr-plan.md) *Test strategy*'s
`MT-CHANNEL-CONFIG-*` arc; the deterministic half is pinned by the Go/Rust unit
and integration suites named below.

The knob under test is **`interaction_idle_timeout_seconds`** — chosen
deliberately:

- It is **router-held** (seeded at boot by `SetInteractionIdleTimeout`), so it
  is one of the six knobs RFC 0050 Phase 1 PR 2 wires into the live apply path
  ([`config_apply.go`](../../internal/channels/config_apply.go)). Flipping it
  exercises the full `validate → PutChannelConfig → router setter` path, not
  just storage.
- Its effect is **directly observable in wall-clock timing**: a stalled floor
  round rotates / idles out after the configured timeout, so lowering it from
  the fleet default (600 s) to e.g. 60 s is visible without instrumentation.
- It is the **exact lever** the `MT-CHANNEL-GOV-*` arcs reach for today by
  hand-editing `config/channels.yaml` and restarting (the "test-profile idle
  window" note in
  [MT-CHANNEL-GOV-004 Test Results](MT-CHANNEL-GOV-004.md#test-results)). RFC
  0050 is precisely what removes that edit-and-restart loop — so this MT also
  demonstrates the operator ergonomics win, not just the contract.

**Why not `floor_control`** (the knob the RFC/PR plan name in their E2E
sketch): on the default `planning` channel `floor_control: false` is **rejected
at apply** by the cross-field rule — `escalation_chair_id: nova-sparrow` ships
on `planning` and requires floor control on
([`config/channels.yaml`](../../config/channels.yaml) line ~149,
[`config_apply.go`](../../internal/channels/config_apply.go) validation). A
live `floor_control` flip therefore needs a chair-less channel — covered as
[Edge Case 2](#edge-case-2-flip-floor_control-on-a-chair-less-channel) rather
than the main arc, so the happy path stays single-channel and dependency-free.

**Scope**: the default `planning` group channel; the toggle-gated
`channel config get`/`set` CLI verbs against a running orchestrator; the
store → router apply, the live behavioral honor, and restart survival under the
revision gate (store revision > absent-YAML-revision = 0 ⇒ the YAML seed does
**not** clobber the live override).

**Out of scope**: the YAML follow-up verbs (`export`/`import`/`diff`) beyond a
single `diff`/drift confirmation (pinned by
[`config_reconcile_test.go`](../../internal/channels/config_reconcile_test.go)
and the CLI tests); interaction-budget live application (deferred — RFC 0050
Phase 1 Open item 4: `interaction_budget_tokens` persists but is **not**
router-wired, so it must **not** be the live-flip knob); the web settings panel
(Phase 2).

---

## Related Documentation

- [RFC 0050 — Extensible Channel Configuration](../rfcs/0050-extensible-channel-configuration.md) — the truth model and goals (G1/G4) this MT accepts
- [RFC 0050 Phase 1 PR plan](../rfcs/0050-phase1-pr-plan.md) — the 5-PR breakdown; this MT is its *Test strategy* manual arc
- [Channels guide §Conversation governance](../guides/channels.md#conversation-governance-rfc-0030-layers-124--v038) — operator-facing knob semantics

**Related Automated Tests**:
- [`channel_config_store_test.go`](../../internal/channels/channel_config_store_test.go) — round-trip persistence, revision monotonicity, stale-revision conflict (PR 1)
- [`config_apply_test.go`](../../internal/channels/config_apply_test.go) — apply persists + reflected by router getters; invalid patch rejected pre-write; restart simulation (PR 2)
- [`config_reconcile_test.go`](../../internal/channels/config_reconcile_test.go) — revision-gating decision table + drift detection (PR 3)
- [`channel_config_handlers_test.go`](../../internal/server/channel_config_handlers_test.go) — `PATCH/GET …/config` happy path, stale-revision 409, toggle-off 403 (PR 4)
- [`channel_config_tests.rs`](../../cli/src/commands/channel_config_tests.rs) — CLI set→get round-trip, conflict surfacing (PR 5)

---

## Preconditions

Same base as [MT-CHANNEL-GOV-004 § Preconditions](MT-CHANNEL-GOV-004.md#preconditions)
(valid API key; clean state; the default `config/channels.yaml`), **plus the
feature toggle must be turned on** — it ships dark.

1. Enable the config-edit surface. In [`config/ui.yaml`](../../config/ui.yaml),
   under `panels.channel_timeline`, set:

   ```yaml
   panels:
     channel_timeline:
       config_edit_enabled: true   # default false — gates BOTH CLI and web uniformly
   ```

   This is read at boot and surfaced via `GET /api/v1/ui/config`; the `PATCH
   …/config` endpoint the CLI rides is gated server-side on it (toggle off →
   `403`, see [Edge Case 1](#edge-case-1-toggle-off-rejects-the-edit-403)).
   **Revert this edit after the run** (keep `git diff` clean, like the
   `MT-CHANNEL-GOV-*` test-profile overrides).

2. Bring the fleet up with a provider overlay (the base ships UNCONFIGURED by
   design — RFC 0033):

   ```bash
   make reset
   ENABLE_UI=1 docker compose -f docker-compose.yaml -f docker-compose.anthropic.yaml up --build
   # (make demo-anthropic is the one-step equivalent)
   ```

---

## Test Procedure

### Step 1: Read the effective config — confirm the seed value and provenance

```bash
./bin/persatrix channel config get planning
```

**Expected**:
- The render lists every governed knob with its effective value, a `source`
  provenance label (`default` = inherited fleet/group default; `channel` =
  explicit per-channel override), and the channel's current `revision`.
- `interaction_idle_timeout_seconds` shows the fleet default (**600**, source
  `default`) — `planning` carries no per-channel override yet.
- `revision` is **0** on a freshly seeded store (no apply has bumped it; the
  absent-YAML-revision migration leaves seed-only channels at 0).

**Verification**:
- [ ] `get` renders effective values + provenance + revision; idle timeout = 600 / `default`; revision = 0.

### Step 2: Live-edit the knob — no YAML, no restart

```bash
./bin/persatrix channel config set planning interaction_idle_timeout_seconds=60
```

The CLI is read-then-write: it `GET`s the current revision and sends it back as
the `If-Match` optimistic-concurrency guard on the `PATCH`, then renders the
post-apply state from the response (no second round-trip).

**Expected**:
- The command succeeds and renders the new effective config:
  `interaction_idle_timeout_seconds = 60`, source now **`channel`**, and
  `revision` bumped to **1**.
- The orchestrator logs the apply landing on the router (the apply path calls
  `SetInteractionIdleTimeout` after `PutChannelConfig`) — no restart occurred.
- A second `./bin/persatrix channel config get planning` confirms the same
  (the value came back from the store round-trip, not just the writer's local
  echo).

**Verification**:
- [ ] `set` returns the bumped revision (1) with `interaction_idle_timeout_seconds=60`, source `channel`.
- [ ] An independent `get` reflects the same value — the change is persisted, not in-memory-only.

### Step 3: The running channel honors the new value — drive a stall and time the rotation

With the orchestrator still running (never restarted), drive a stalled floor
round the way [MT-CHANNEL-GOV-004 Step 1](MT-CHANNEL-GOV-004.md#step-1-engineer-an-honest-stall)
does, and observe that idle rotation now fires on the **60 s** timeout rather
than the 600 s default:

```bash
./bin/persatrix channel join planning --as alex --respond never
./bin/persatrix channel send planning \
  "Name exactly one risk each for shipping v1 next Friday. One sentence per person, no repeats." \
  --as alex --mention iron-fox --mention nova-sparrow --mention ember-owl
# …after the round lands, nudge un-mentioned so the round stalls:
./bin/persatrix channel send planning "Anything else on this?" --as alex
```

**Expected**:
- The stalled round's idle rotation / timeout-driven advance fires at
  **~60 s** after the last turn, not ~600 s — the live edit governs the running
  channel. (Watch the orchestrator's idle-rotation log line, made observable by
  [ISSUE-0095](../issues/ISSUE-0095-idle-rotation-no-fire-observability.md); compare
  the elapsed time against the prior 600 s arcs in MT-CHANNEL-GOV-004.)

**Verification**:
- [ ] The running channel's idle behavior reflects 60 s, with no restart between the `set` and the observed rotation.

> **Note on capture timing.** If your build resolves the idle timeout once at
> interaction start rather than per-round, an in-flight interaction may finish
> on its captured value; drive a *fresh* opener after the `set` so the new
> interaction picks up 60 s. Either way the contract under test — "the running
> orchestrator honors the new value without a restart" — holds. Record which
> you observed in Test Results.

### Step 4: Restart — the live edit survives, the YAML seed does not clobber it

Restart the orchestrator **without** touching `config/channels.yaml` (it still
declares no `interaction_idle_timeout_seconds` on `planning`, and no
`revision:`):

```bash
# Ctrl-C the compose stack, then bring it back up (NO make reset — keep the store):
ENABLE_UI=1 docker compose -f docker-compose.yaml -f docker-compose.anthropic.yaml up
./bin/persatrix channel config get planning
```

**Expected**:
- `interaction_idle_timeout_seconds` is still **60**, source **`channel`**,
  `revision` still **1**. The boot reconcile (`ResolveFromStore` →
  revision-gated YAML reconcile) seeds the router from the **store** override;
  the YAML block carries an absent revision (= 0), which is **not** strictly
  greater than the store's 1, so the 600 s seed does **not** overwrite the live
  edit. G1 holds: the change survived restart.

**Verification**:
- [ ] After restart `get` still shows 60 / `channel` / revision 1 — the store won over the YAML seed under the revision gate.

### Step 5: Unset returns the knob to inherit

```bash
./bin/persatrix channel config unset planning interaction_idle_timeout_seconds
./bin/persatrix channel config get planning
```

**Expected**:
- `unset` sends `interaction_idle_timeout_seconds: null` (clear → inherit),
  bumps `revision` to **2**, and the effective value reverts to **600**, source
  back to **`default`**. (Revision only ever increases — rollback is a new
  higher revision, never a decrement.)

**Verification**:
- [ ] `unset` reverts the knob to the inherited 600 / `default` and bumps revision to 2.

---

## Expected Results Summary

| Step | Expected Outcome | Pass/Fail |
|------|-----------------|-----------|
| 1 | `config get` renders effective values + provenance + revision; idle timeout 600/`default`, revision 0 | ☐ |
| 2 | `config set` applies live (no restart): idle timeout 60/`channel`, revision 1; independent `get` agrees | ☐ |
| 3 | The running channel honors 60 s idle timeout — no restart between edit and observed behavior | ☐ |
| 4 | After restart the store override (60/`channel`/rev 1) survives; absent-revision YAML seed does not clobber it | ☐ |
| 5 | `unset` reverts to inherited 600/`default`, revision bumped to 2 | ☐ |

---

## Edge Cases & Error Scenarios

### Edge Case 1: Toggle off rejects the edit (403)

With `config_edit_enabled: false` (the shipped default), restart and retry:

```bash
./bin/persatrix channel config set planning interaction_idle_timeout_seconds=60
```

**Expected**: the `PATCH` is gated server-side and returns **403**; the CLI
surfaces it cleanly (no partial write — `config get` still shows the prior
value/revision). This is the uniform gate that covers CLI and web identically
(PR 4 / PR plan Open item 1). Pinned deterministically by
`channel_config_handlers_test.go` (toggle-off → 403).

### Edge Case 2: Flip `floor_control` on a chair-less channel

`floor_control` is the most visceral behavioral knob (turn-by-turn dispatch vs
open floor) but cannot be set `false` on `planning` (its `escalation_chair_id`
requires floor control — the cross-field rule rejects it at apply, surfacing a
clean validation error, **not** a silent no-op). To exercise a live
`floor_control` flip, create a chair-less group channel first:

```bash
./bin/persatrix channel create scratch --type group   # no escalation_chair_id
./bin/persatrix channel config set scratch floor_control=false
```

**Expected**: the apply succeeds (no chair to conflict), the router's dispatch
for `scratch` switches to open-floor on the next round, and a `floor_control=false`
attempt on `planning` instead returns a validation error naming the
chair/floor-control conflict.

### Edge Case 3: Stale-revision conflict (409)

The CLI auto-reads the current revision before each write, so a stale `If-Match`
does not arise in single-operator use. To observe the optimistic-concurrency
guard, race two writers (or hand-craft a `PATCH` with an old `If-Match` via
`curl`): the second writer's stale revision returns **409 Conflict** and the CLI
surfaces it without clobbering. Pinned deterministically by
`channel_config_store_test.go` (stale-revision conflict) and
`channel_config_handlers_test.go` (409).

---

## Test Results

| Date | Tester | Build | Result | Notes |
|------|--------|-------|--------|-------|
| _pending_ | | | | First live exercise of the RFC 0050 Phase 1 G1 arc. |

## Notes

- **Keep `git diff` clean.** Both levers this MT touches — the
  `config_edit_enabled: true` toggle and any test-profile timing — are config
  edits; revert them after the run, as the `MT-CHANNEL-GOV-*` arcs do, so the
  recorded build is honest about what shipped.
- **Interaction budget is the wrong knob here.** `interaction_budget_tokens`
  persists through the same apply path but is **not** router-wired in Phase 1
  (Open item 4 — it is resolved on the wallet path, not held by the router), so
  an edit to it takes effect only on the *next restart*, not mid-run. The
  live-honor step (Step 3) must use a router-held knob; this MT uses
  `interaction_idle_timeout_seconds`.
</content>
</invoke>
