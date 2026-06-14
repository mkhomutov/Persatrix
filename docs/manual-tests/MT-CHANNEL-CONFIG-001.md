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
sketch): it is the wrong knob for a single-channel happy path on `planning`, for
a subtler reason than the RFC sketch assumes. `planning` carries
`escalation_chair_id: nova-sparrow`
([`config/channels.yaml`](../../config/channels.yaml) ~line 152), and a chair
requires floor control on. But that cross-field rule is enforced against the
**merged override set** (the channel's *stored* overrides + the patch), **not**
the running channel's effective state — and on a freshly seeded `planning` the
chair is **YAML-seeded / router-held, never a store override** (revision 0 ⇒ the
reconcile leaves the store to config-as-code). So a lone
`channel config set planning floor_control=false` is **not** rejected: the rule
sees no chair in the merged blob, the apply commits, and — because the override
blob omits the chair — the store-canonical re-stamp **silently detaches** the
YAML chair (absent knob → inherit; see
[`config_apply.go`](../../internal/channels/config_apply.go) `applyOverridesToRouter`,
pinned by `TestApplyChannelConfig_LoneFloorControlFalseDoesNotSeeYAMLSeededChair`).
That makes `floor_control` both undemonstrative (no clean rejection to show) and
quietly destructive on `planning`. A *legitimate* cross-field rejection needs the
chair to ride the same patch — covered as
[Edge Case 2](#edge-case-2-flip-floor_control-and-the-yaml-chair-limit) rather
than the main arc, so the happy path stays single-channel and dependency-free.
(Idle timeout sidesteps all of this — but note it triggers the *same* chair
detachment on its first edit; see [Step 2](#step-2-live-edit-the-knob--no-yaml-no-restart).)

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
- [`config_apply_test.go`](../../internal/channels/config_apply_test.go) — apply persists + reflected by router getters; invalid patch rejected pre-write; restart simulation; the cross-field chair/floor-control rule and **its limit** (`TestApplyChannelConfig_LoneFloorControlFalseDoesNotSeeYAMLSeededChair`, `TestApplyChannelConfig_FirstEditDetachesYAMLSeededChair` — a YAML-seeded chair is not in the merged blob, so a lone `floor_control`/idle edit is accepted and detaches it) (PR 2)
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

> **⚠️ Side effect — this first edit also detaches `planning`'s escalation
> chair.** The apply re-stamps **all six** router-held knobs from the merged
> override blob, and that blob carries only the knob you set. `planning`'s
> `escalation_chair_id: nova-sparrow` is YAML-seeded (never a store override), so
> it is *not* in the blob and the re-stamp drops it back to "inherit" (no chair).
> The second `get` will show `escalation_chair_id` unset where Step 1 showed
> `nova-sparrow`. This is the store-canonical model working as designed (RFC 0050
> — a channel goes store-canonical on its first edit); it is **not** specific to
> idle timeout. Pinned by `TestApplyChannelConfig_FirstEditDetachesYAMLSeededChair`.
> `make reset` restores the seed after the run.

**Verification**:
- [ ] `set` returns the bumped revision (1) with `interaction_idle_timeout_seconds=60`, source `channel`.
- [ ] An independent `get` reflects the same value — the change is persisted, not in-memory-only.
- [ ] The same `get` shows `escalation_chair_id` now unset (the documented first-edit side effect), confirming the store-canonical re-stamp.

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

> **⚠️ Sequencing hazard — undo the Step 3 `join` before restarting.** The boot
> path runs a **strict** config-vs-store membership reconcile (`ReconcileConfig`,
> v0.3.0 §B): if a config-declared channel's stored member set diverges from
> `config/channels.yaml`, the orchestrator **FATAL-exits and crash-loops**
> (`channels: config-vs-store membership divergence: channel=group:planning
> divergent_participants=[+alex]`). Step 3 joined `alex` (undeclared in YAML), so
> a restart now will **not boot**. Before restarting, remove the runtime-joined
> driver — `DELETE /api/v1/channels/group:planning` membership for `alex`, or
> `make reset` and re-run Steps 1–2 *without* the Step 3 join if you only need to
> verify config survival. (This hazard is orthogonal to RFC 0050 — it is the
> pre-existing membership gate — but it bites this arc, so it is called out here.
> Pinned by `router_reconcile` tests.)

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
  `revision` still **1**. Two boot mechanisms combine: (1) the per-knob YAML
  `Resolve*` calls seed the router with the 600 s default, then `ResolveFromStore`
  **overlays** `planning` (now at revision > 0, so store-canonical) and re-stamps
  it to 60; and (2) the revision-gated `ReconcileFromYAML` leaves the store
  untouched because the YAML block's absent revision (= 0) is **not** strictly
  greater than the store's 1 (higher revision wins) — so the 600 s seed never
  overwrites the live edit in either direction. G1 holds: the change survived
  restart. (The first-edit chair detachment from Step 2 also persists — `get`
  still shows `escalation_chair_id` unset.)

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

**Expected**: the `PATCH` is gated server-side and returns **403** with a clean
message (`channel config editing is disabled (set
panels.channel_timeline.config_edit_enabled: true …)`), no partial write. The
gate covers the **whole `/config` endpoint, not just writes**: with the toggle
off, `channel config get` **also returns 403** — you cannot read the config
surface while it is dark, so there is no "read-only fallback" view. This is the
uniform gate that covers CLI and web identically (PR 4 / PR plan Open item 1).
Pinned deterministically by `channel_config_handlers_test.go` (toggle-off → 403).

### Edge Case 2: Flip `floor_control` and the YAML-chair limit

`floor_control` is the most visceral behavioral knob (turn-by-turn dispatch vs
open floor). The cross-field rule "a chair requires floor control on" guards the
**merged override set**, not the channel's effective state — so its behavior
depends on whether the chair is in the patch/store, not on whether the channel
*appears* to have a chair. Three cases confirm where it does and does not fire:

1. **Open-floor flip on a chair-less channel succeeds.** Create one and flip it:

   ```bash
   # `channel create` takes a bare NAME and requires ≥1 --member; it is always a
   # group channel (no --type flag) and declares no escalation_chair_id:
   ./bin/persatrix channel create scratch --member alex --member nova-sparrow
   ./bin/persatrix channel config set scratch floor_control=false
   ```

   The apply succeeds (no chair to conflict) and `scratch`'s dispatch switches to
   open-floor on the next round.

2. **A chair set *alongside* `floor_control=false` is rejected.** The chair is in
   the merged blob, so the rule fires:

   ```bash
   ./bin/persatrix channel config set scratch escalation_chair_id=nova-sparrow floor_control=false
   ```

   returns a clean validation error naming the chair/floor-control conflict, and
   nothing persists. Pinned deterministically by `config_apply_test.go`
   (`TestApplyChannelConfig_EscalationChairRequiresFloorControl`).

3. **⚠️ A lone `floor_control=false` on `planning` is *accepted* — and silently
   detaches the chair.** This is the gotcha the RFC's E2E sketch missed:

   ```bash
   ./bin/persatrix channel config set planning floor_control=false
   ```

   does **not** error. `planning`'s `nova-sparrow` chair lives in YAML, not the
   store, so it is invisible to the cross-field rule (which sees only the merged
   store overrides). The edit commits and the store-canonical re-stamp drops the
   chair — **no validation warning**. Pinned by
   `TestApplyChannelConfig_LoneFloorControlFalseDoesNotSeeYAMLSeededChair`. Run
   `make reset` afterward to restore the YAML-seeded chair.

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
| 2026-06-14 | Maksim Khomutov | `3402f0e` (Anthropic overlay; orchestrator built via host-asset workaround — see note) | **PASS (with 3 procedure fixes)** | First live exercise. Steps 1, 2, 3, 5 ✅; Step 4 ✅ via clean re-run (see ⚠️ below); Edge Cases 1, 2, 3 ✅. **Step 1**: rev 0, idle 600 `[default]`, chair `nova-sparrow` `[default]` (inherited, *not* a store override — confirms the chair is YAML-seeded). **Step 2**: live set → idle 60 `[channel]`, rev 1, **and `escalation_chair_id` flipped `nova-sparrow → (none)`** — the first-edit chair detachment, confirmed live exactly as documented. **Step 3**: stalled round (last turn 15:02:09Z) closed by idle rotation at 15:03:48Z (~99 s) — only possible under the 60 s window, *not* the 600 s default; recorded lazily on the next event (the capture-timing note). **Step 4**: the original Step 3 `join alex` then crash-looped the restart on membership divergence (now documented as a hazard); re-ran clean (set→restart, no join) → 60 `[channel]` rev 1 survived. **Step 5**: unset → 600 `[default]` rev 2. **EC1**: toggle off → 403 on both PATCH *and* GET. **EC2**: lone `floor_control=false` accepted; chair+`floor_control=false` in one patch rejected with the cross-field error. **EC3**: stale `If-Match` → 409. Fixed during this run: Step 4 join/restart hazard, EC1 GET-also-gated, EC2 `channel create` syntax (`--member`, no `--type`). Build caveat: the canonical arm64 Docker UI build is broken (npm rollup optional-dep, npm/cli#4828 — likely #644 fallout); ran the orchestrator from an image baked with host-built `internal/ui/assets`. |

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
