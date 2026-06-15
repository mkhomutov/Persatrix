# Manual Test MT-CHANNEL-CONFIG-002: Edit a governance knob from the web console — the running channel honors it, and the CLI reads back the same value

**Test ID**: `MT-CHANNEL-CONFIG-002`
**Feature Area**: Channels (operator-editable channel configuration — RFC 0050 Phase 2, the web-console settings panel over the Phase 1 apply path)
**Version**: 1.0
**Created**: 2026-06-15
**Last Updated**: 2026-06-15
**Status**: Active

---

## Overview

**Purpose**: Verify RFC 0050 Phase 2's web slice end-to-end with the real
orchestrator: an operator changes a live channel's governance knob **from the
browser** — no CLI, no YAML edit, no restart — the change takes effect on the
running channel immediately, and the **CLI `channel config get` reads back the
exact same value**. That last cross-surface check is the live acceptance of goal
**G4 (single source of truth)**: the web panel and the CLI ride the *same*
`GET`/`PATCH /api/v1/channels/{id}/config` endpoint and the *same* per-channel
revision, so neither is a second copy of the truth — both render the store.

This is the web sibling of the Phase 1 CLI arc
[MT-CHANNEL-CONFIG-001](MT-CHANNEL-CONFIG-001.md): same knob, same store-canonical
apply path, same live-honor and revision semantics — driven through the
`ChannelSettings.svelte` panel instead of the `channel config` CLI verbs. The
deterministic half of the web slice is pinned by the vitest suites named below;
this MT is the live, cross-surface half.

The knob under test is **`interaction_idle_timeout_seconds`** — chosen for the
same reasons as in MT-CHANNEL-CONFIG-001:

- It is **router-held** (seeded at boot by `SetInteractionIdleTimeout`), so a
  browser edit exercises the full `validate → PutChannelConfig → router setter`
  apply path, not just storage.
- Its effect is **directly observable in wall-clock timing**: a stalled floor
  round idles out after the configured timeout, so lowering it from the fleet
  default (600 s) to 60 s is visible without instrumentation.

**Why not a `null`-reading knob like `interaction_budget_tokens`**: it is
store-persisted but **not** router-wired in Phase 1 (Open item 4), so it reads
back `value: null` when inherited and never takes effect mid-run. The panel
renders it correctly (empty, "inherited", never coerced to `0`), but it is the
wrong knob for a live-honor step — use idle timeout, exactly as the CLI arc does.

**Scope**: the default `planning` group channel; the toggle-gated **Channel
settings** panel nested in the Channels tab; the browser override → store → router
apply, the live behavioral honor, and the cross-surface CLI read-back (G4). The
panel's inherit/override control, sparse-PATCH-of-touched-knobs-only, `If-Match`
revision guard, and `409` reload-and-replay are exercised.

**Out of scope**: the CLI-only YAML follow-up verbs (`export`/`import`/`diff` —
covered by MT-CHANNEL-CONFIG-001); restart-survival of the override (the
store-vs-YAML revision gate is identical to and covered by
[MT-CHANNEL-CONFIG-001 Step 4](MT-CHANNEL-CONFIG-001.md#step-4-restart--the-live-edit-survives-the-yaml-seed-does-not-clobber-it),
since the web panel writes through the *same* store path — this MT does not
re-verify it); editable member thresholds and the effective-policy preview (both
**deferred** — require backend work, RFC 0050 Phase 2 PR plan
[Deferred](../rfcs/0050-phase2-pr-plan.md#deferred-requires-backend-work)).

---

## Related Documentation

- [RFC 0050 — Extensible Channel Configuration](../rfcs/0050-extensible-channel-configuration.md) — the truth model and goals (G1/G4) this MT accepts
- [RFC 0050 Phase 2 PR plan](../rfcs/0050-phase2-pr-plan.md) — the 3-PR web-only breakdown; this MT is PR 3's live arc
- [Web Console guide § Channel settings](../guides/web-console.md#channel-settings--edit-governance-from-the-browser) — operator-facing panel walkthrough
- [Channels guide § Editing governance config at runtime](../guides/channels.md#editing-governance-config-at-runtime--channel-config-rfc-0050-phase-1) — the shared knob semantics and CLI counterpart
- [MT-CHANNEL-CONFIG-001](MT-CHANNEL-CONFIG-001.md) — the Phase 1 CLI sibling (restart survival, YAML verbs, the cross-field chair rule)
- [ISSUE-0103](../issues/ISSUE-0103-first-config-edit-detaches-yaml-seeded-knobs.md) — first-edit detachment of YAML-seeded knobs (the prerequisite to shipping this panel non-dark)

**Related Automated Tests** (the deterministic half — Phase 2 ships zero Go; all web/vitest):
- [`bootstrap.test.js`](../../web/src/lib/bootstrap.test.js) — `selectPanels()` threads `panels[].config_edit` onto the descriptor with the same parity as `create` (PR 1)
- [`api.config.test.js`](../../web/src/lib/api.config.test.js) — `getChannelConfig`/`patchChannelConfig`: the sparse body + `If-Match` header, and `403`/`409`/`428`/`400`/`404`/`503` mapping onto `ApiError` (PR 1)
- [`ChannelSettings.test.js`](../../web/src/panels/ChannelSettings.test.js) — renders knobs + provenance from a mocked response; single-key sparse PATCH with `If-Match`; revert sends `null`; `409` reloads rather than overwrites; inherited `interaction_budget_tokens` renders empty not `0` (PR 2)
- [`ChannelTimeline.settings.test.js`](../../web/src/panels/ChannelTimeline.settings.test.js) — the panel mounts only under `canConfigEdit && selectedChannel && !isDM` (PR 2)

---

## Preconditions

Same base as [MT-CONSOLE-001 § Preconditions](MT-CONSOLE-001.md) (the console
running with channels wired, a provider overlay so personas can actually reply),
**plus the feature toggle must be turned on** — it ships dark.

1. Enable the config-edit surface. In [`config/ui.yaml`](../../config/ui.yaml),
   under `panels.channel_timeline`, set:

   ```yaml
   panels:
     channel_timeline:
       enabled: true
       config_edit_enabled: true   # default false — gates BOTH the web panel and CLI uniformly
   ```

   This is read at boot and surfaced via `GET /api/v1/ui/config` as
   `panels.channel_timeline.config_edit = { enabled, available }`; the panel
   renders only when **both** are true (the same `enabled && available` rule every
   panel follows). **Revert this edit after the run** (keep `git diff` clean, like
   the `MT-CHANNEL-GOV-*` test-profile overrides).

2. Bring the fleet up with the UI and a provider overlay (the base ships
   UNCONFIGURED by design — RFC 0033):

   ```bash
   make reset
   ENABLE_UI=1 docker compose -f docker-compose.yaml -f docker-compose.anthropic.yaml up --build
   # (make demo-anthropic is the one-step equivalent)
   ```

   > **arm64 build caveat.** The canonical arm64 Docker UI build was broken by a
   > `.dockerignore` nested-`node_modules` leak; it is **resolved** as of #650
   > (see [ISSUE-0104](../issues/ISSUE-0104-arm64-orchestrator-docker-ui-build-broken.md)),
   > so `up --build` / `make demo-anthropic` work on arm64 again. If you are on a
   > build older than #650, fall back to the host-built-assets workaround noted in
   > MT-CHANNEL-CONFIG-001's Test Results.

3. Open the console at <http://localhost:8080/ui> and go to the **Channels** tab.

---

## Test Procedure

### Step 1: Open Channel settings — confirm the panel renders, with effective values and provenance

In the **Channels** tab, select the **planning** group channel, then expand the
**Channel settings** disclosure (beside the member roster).

**Expected**:
- The panel renders (it is a **group** channel and both `config_edit.enabled` and
  `config_edit.available` are true). It does **not** render for a DM.
- Every governed knob is listed with its effective value and a provenance badge:
  **Inherited default** (the fleet/group default) or **Overridden on this
  channel**.
- `Interaction idle timeout (seconds)` shows **600**, badge **Inherited default**
  (`planning` carries no per-channel override yet), with **Inherit fleet default**
  ticked and the value input disabled.
- The **Escalation chair** picker resolves `planning`'s YAML-seeded chair
  (`nova-sparrow`) as its effective value, badged **Inherited default** — because
  a YAML-seeded knob is *not* a store override. Provenance reflects the store's
  `source`: `default` = inherited (including YAML-seeded knobs); `channel` = an
  explicit store override.
- **Save settings** is disabled (nothing is dirty yet).

**Verification**:
- [ ] The panel renders for the group channel, lists knobs with effective values + provenance, and idle timeout reads 600 / Inherited default.

### Step 2: Override the knob in the browser — Save

In the **Interaction idle timeout (seconds)** row, untick **Inherit fleet
default**, set the value to **60**, and click **Save settings**.

**Expected**:
- The save succeeds (a "Settings saved." confirmation). Under the hood the panel
  sent a **single-key sparse PATCH** (`{ "interaction_idle_timeout_seconds": 60 }`)
  with the loaded revision as the `If-Match` header — no other knob is touched.
- The row now reads **60**, badge **Overridden on this channel**.
- The orchestrator logs the apply landing on the router
  (`SetInteractionIdleTimeout` after `PutChannelConfig`) — no restart occurred.

> **⚠️ Side effect — this first edit also detaches `planning`'s escalation
> chair.** The store-canonical apply re-stamps all router-held knobs from the
> merged override blob, which carries only the knob you set; `planning`'s
> YAML-seeded `escalation_chair_id: nova-sparrow` is *not* in the blob, so the
> re-stamp drops it to "inherit" (no chair). The panel's chair picker will show
> no selection after a refresh. This is the store-canonical model working as
> designed (a channel goes store-canonical on its first edit); it is **not**
> specific to idle timeout and **not** the panel's bug. Tracked as
> [ISSUE-0103](../issues/ISSUE-0103-first-config-edit-detaches-yaml-seeded-knobs.md)
> — **the prerequisite to shipping this panel non-dark.** `make reset` restores
> the seed after the run.

**Verification**:
- [ ] Save succeeds; the row reads 60 / Overridden on this channel; no restart occurred.

### Step 3: Cross-surface read-back — the CLI sees the browser's edit (G4)

From a shell (the CLI's `channel config` verbs are gated behind the same toggle,
already on):

```bash
./bin/persatrix channel config get planning
```

**Expected**:
- `interaction_idle_timeout_seconds` = **60**, source **`channel`** — the exact
  value set in the browser, read back from the store through a *different*
  surface. The browser did not write a private copy; it wrote the one store the
  CLI reads.
- `revision` is bumped to **1** (the apply the browser triggered) — the same
  revision the panel now holds for its next `If-Match`.
- `escalation_chair_id` shows unset (the documented first-edit detachment from
  Step 2), confirming both surfaces see the same post-apply state.

**Verification**:
- [ ] The CLI `config get` reflects 60 / `channel` / revision 1 — identical to the browser. (G4: one source of truth.)

### Step 4: The running channel honors the new value — drive a stall and time the rotation

With the orchestrator still running (never restarted), drive a stalled floor
round the way [MT-CHANNEL-GOV-004 Step 1](MT-CHANNEL-GOV-004.md#step-1-engineer-an-honest-stall)
does, and observe that idle rotation now fires on the **60 s** timeout rather than
the 600 s default:

```bash
./bin/persatrix channel join planning --as alex --respond never
./bin/persatrix channel send planning \
  "Name exactly one risk each for shipping v1 next Friday. One sentence per person, no repeats." \
  --as alex --mention iron-fox --mention nova-sparrow --mention ember-owl
# …after the round lands, nudge so the round stalls:
./bin/persatrix channel send planning "Anything else on this?" --as alex
```

**Expected**:
- The stalled round's idle rotation / timeout-driven advance fires at **~60 s**
  after the last turn, not ~600 s — the browser edit governs the running channel.
  (Watch the orchestrator's idle-rotation log line, made observable by
  [ISSUE-0095](../issues/ISSUE-0095-idle-rotation-no-fire-observability.md);
  compare the elapsed time against the prior 600 s arcs in MT-CHANNEL-GOV-004.)

**Verification**:
- [ ] The running channel's idle behavior reflects 60 s, with no restart between the browser Save and the observed rotation.

> **Note on capture timing.** If your build resolves the idle timeout once at
> interaction start rather than per-round, an in-flight interaction may finish on
> its captured value; drive a *fresh* opener after the Save so the new interaction
> picks up 60 s. Either way the contract under test — "the running orchestrator
> honors the browser edit without a restart" — holds. Record which you observed in
> Test Results.

### Step 5: Revert from the browser — back to inherit, and the CLI agrees

> **⚠️ Undo the Step 4 `join` first if you intend to restart.** Step 4 joined
> `alex` (undeclared in YAML); a restart would crash-loop on the strict
> membership reconcile (see
> [MT-CHANNEL-CONFIG-001 Step 4](MT-CHANNEL-CONFIG-001.md#step-4-restart--the-live-edit-survives-the-yaml-seed-does-not-clobber-it)).
> This step does **not** restart, so it is only a hazard if you go on to verify
> restart survival; if so, remove `alex` first.

Back in the **Channel settings** panel, re-tick **Inherit fleet default** on the
idle-timeout row and click **Save settings**.

**Expected**:
- The panel sends `{ "interaction_idle_timeout_seconds": null }` (revert →
  inherit) with the current revision as `If-Match`. The row reverts to **600**,
  badge back to **Inherited default**.
- An independent `./bin/persatrix channel config get planning` confirms
  `interaction_idle_timeout_seconds` = **600**, source **`default`**, `revision`
  bumped to **2** (revision only ever increases — a revert is a new higher
  revision, never a decrement).

**Verification**:
- [ ] The browser revert returns the knob to 600 / Inherited default; the CLI reads back 600 / `default` / revision 2.

---

## Expected Results Summary

| Step | Expected Outcome | Pass/Fail |
|------|-----------------|-----------|
| 1 | Channel settings panel renders for the group channel; knobs show effective values + provenance; idle timeout 600 / Inherited default | ☐ |
| 2 | Browser override → single-key sparse PATCH with `If-Match`; row reads 60 / Overridden; no restart (first-edit chair detachment observed) | ☐ |
| 3 | CLI `config get` reads back 60 / `channel` / revision 1 — identical to the browser (G4) | ☐ |
| 4 | The running channel honors 60 s idle timeout — no restart between browser Save and observed rotation | ☐ |
| 5 | Browser revert → 600 / Inherited default; CLI reads 600 / `default` / revision 2 | ☐ |

---

## Edge Cases & Error Scenarios

### Edge Case 1: Toggle off hides the panel entirely

With `config_edit_enabled: false` (the shipped default), restart and reopen the
console.

**Expected**: the **Channel settings** disclosure does **not** render — there is
no read-only fallback. `GET /api/v1/ui/config` reports
`panels.channel_timeline.config_edit.enabled = false`, so the panel's
`canConfigEdit` gate is false and `ChannelTimeline` never mounts `ChannelSettings`
(the same uniform gate that returns `403` to the CLI verbs and to a hand-crafted
`PATCH`). Pinned deterministically by `ChannelTimeline.settings.test.js`
(no-mount when `canConfigEdit` is false) and the server-side
`channel_config_handlers_test.go` (toggle-off → 403).

### Edge Case 2: Concurrent edit elsewhere → 409 reload-and-replay (not blind overwrite)

Open the panel, untick **Inherit** and set idle timeout to **120**, but **do not
save yet**. From a shell, race a different edit through the CLI:

```bash
./bin/persatrix channel config set planning end_vote_window=5
```

Now click **Save settings** in the browser.

**Expected**: the save's `If-Match` is now stale, so the server returns **409**.
The panel does **not** clobber the CLI's `end_vote_window` change: it **reloads
the latest config** (picking up `end_vote_window=5` and the bumped revision),
**replays your pending idle-timeout edit on top**, and shows a warning ("changed
elsewhere — reloaded with the latest. Review your edits and save again."). Your
120 is still pending against the fresh revision; clicking **Save settings** again
lands it. Pinned deterministically by `ChannelSettings.test.js` (`409` → reload,
edits preserved, no silent overwrite).

### Edge Case 3: Chair + open-floor in one save → 400 surfaced, not silently mis-set

The escalation chair picker constrains you to members that can hold the floor, but
it **cannot** prevent the cross-field rule "a chair requires `floor_control` on."
On a chair-capable channel, in one Save, pick an escalation chair **and** untick
**Inherit** on **Floor control** setting it **off**.

**Expected**: the PATCH round-trips to a **400** (the
`validateEscalationChair` cross-field rejection from Phase 1), and the panel
surfaces the error rather than assuming the picker guaranteed a valid chair —
nothing persists. (The picker satisfies "must be a non-observer member"; it cannot
satisfy "resolves under floor control on" when the same patch turns floor control
off.) The server stays the authority on value validity, exactly as the panel's
client-side `min="0"` int bounds are advisory only.

---

## Test Results

| Date | Tester | Build | Result | Notes |
|------|--------|-------|--------|-------|
| _pending_ | | | ☐ Not yet run | First live web exercise of the RFC 0050 Phase 2 settings panel. Run after the panel lands (PR 2, #653) and ISSUE-0103 is understood as the non-dark prerequisite. |

## Notes

- **Keep `git diff` clean.** The `config_edit_enabled: true` toggle and any
  test-profile timing are config edits; revert them after the run, as the
  `MT-CHANNEL-GOV-*` arcs do, so the recorded build is honest about what shipped.
- **This MT does not re-verify restart survival.** The web panel writes through
  the *same* store path as the CLI, so the store-vs-YAML revision gate
  ([MT-CHANNEL-CONFIG-001 Step 4](MT-CHANNEL-CONFIG-001.md#step-4-restart--the-live-edit-survives-the-yaml-seed-does-not-clobber-it))
  is already covered surface-agnostically. The new ground this MT breaks is the
  **browser → store → router** apply and the **cross-surface read-back** (G4).
- **Interaction budget is the wrong knob for the live-honor step.**
  `interaction_budget_tokens` persists through the same apply path but is **not**
  router-wired in Phase 1 (Open item 4), so an edit takes effect only on the next
  restart. The panel renders its inherited value as empty ("inherited"), never
  `0` — verify that rendering separately, but use `interaction_idle_timeout_seconds`
  for Step 4.
</content>
</invoke>
