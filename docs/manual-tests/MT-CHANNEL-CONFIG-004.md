# Manual Test MT-CHANNEL-CONFIG-004: Edit a member's salience threshold from the web console — the store records it and the CLI/REST read back the same value

**Test ID**: `MT-CHANNEL-CONFIG-004`
**Feature Area**: Channels (operator-editable member configuration — RFC 0050 Phase 2, the deferred member-threshold web slice landed in #660: the per-row inline disposition + salience-threshold editor in `ChannelMembers.svelte` over the `PATCH …/members/{participant_id}` endpoint)
**Version**: 1.0
**Created**: 2026-06-16
**Last Updated**: 2026-06-16
**Status**: Active

---

## Overview

**Purpose**: Verify the last RFC 0050 Phase 2 slice end-to-end with the real
orchestrator: an operator edits a channel **member's** disposition + salience
threshold **from the browser** — no CLI, no YAML edit, no restart — the change
lands on the store through the member-config `PATCH …/members/{participant_id}`
endpoint, the roster re-renders with the new value, and an independent **REST
read-back of the channel shows the same `threshold` / `salience_gated`**. This is
the member-roster sibling of the channel-settings web arc
[MT-CHANNEL-CONFIG-002](MT-CHANNEL-CONFIG-002.md): same store-canonical path,
driven through the `ChannelMembers.svelte` inline editor instead of the
`ChannelSettings.svelte` knob panel.

This slice was **deferred** out of the original Phase 2 (no member-config
mutation endpoint existed when the settings panel shipped); #659 added the backend
endpoint and #660 added the web editor and flipped `config_edit_enabled` on,
closing RFC 0050. The deterministic half is pinned by the vitest suites named
below; this MT is the live, cross-surface half.

**What the editor does** ([`ChannelMembers.svelte`](../../web/src/panels/ChannelMembers.svelte),
[`api.members.js`](../../web/src/lib/api.members.js)):

- Per member row, an **Edit** control opens an inline editor: a **disposition**
  `<select>` (When mentioned / Participant (salience bid) / Chair (facilitator) /
  Addressed only / Observer / Always / Never) + a **salience threshold** number
  input (placeholder `unset`) + **Save** / **Cancel**.
- The PATCH is a **full REPLACE** of the member's editable config — the server
  **requires** `respond` (the disposition) because `salience_gated` is re-derived
  from the *declared* disposition and is unrecoverable from persisted state. A
  threshold-only body is a 400.
- `threshold` is a number in **[0, 1]** to set the salience bar, or **explicit
  `null`** (empty input) to unset it (bias-to-silence). A threshold on a
  non-open-floor disposition, or out of range, is a 400.
- **Edit is withheld for the acting user** (a human principal, like Remove) —
  you cannot edit your own membership from the console.

**The #660 follow-up regression this MT guards**: the persisted `respond` reads
back as the normalized legacy `"always"` for any salience-gated participant/chair
(the store collapses open-floor dispositions on write). If the editor echoed that
bare `"always"` on Save, the server would re-derive `salience_gated=false` and
**silently demote a salience-gated participant to reply-to-everything**. #660 fixes
`startEdit` to reconstruct an open-floor disposition (**Participant**) whenever
`salience_gated && respond === "always"`, so a threshold edit preserves gating.
Step 3 verifies the member stays salience-gated after the edit.

**Member under test**: **`iron-fox`** on the default `planning` group channel — it
seeds as a salience-gated open-floor member (`respond: participant` →
read-back `respond:"always"`, `salience_gated:true`, no threshold), so it is a
valid threshold-edit target. (`ember-owl` is `when_mentioned` / not gated — a
threshold on it is the 400 in Edge Case 3.)

**Scope**: the toggle-gated member roster in the Channels tab; the browser →
store member-config apply; the roster re-render; the cross-surface REST read-back;
the open-floor-disposition reconstruction (#660); empty→null unset; and the
range / disposition validation surfaced from the server.

**Out of scope**: the channel-settings knob panel (covered by
MT-CHANNEL-CONFIG-002); add/remove member (RFC 0011, covered by
MT-CONSOLE-001); a behavioral honor of the new threshold in live salience bidding
(the bid is probabilistic and provider-dependent — the *contract* under test is
"the browser edit reaches the store and is read back consistently"; the bid
mechanics are covered by the RFC 0030 salience suites).

---

## Related Documentation

- [RFC 0050 — Extensible Channel Configuration](../rfcs/0050-extensible-channel-configuration.md) — the truth model; the member-threshold slice it stayed open on
- [RFC 0050 Phase 2 PR plan](../rfcs/0050-phase2-pr-plan.md) — member thresholds were the deferred slice
- [MT-CHANNEL-CONFIG-002](MT-CHANNEL-CONFIG-002.md) — the channel-settings web sibling (panel render, sparse PATCH, G4 read-back)
- [Web Console guide § Channel settings](../guides/web-console.md#channel-settings--edit-governance-from-the-browser) — operator-facing walkthrough

**Related Automated Tests** (the deterministic half — #660 is web-only):
- [`api.members.test.js`](../../web/src/lib/api.members.test.js) — `updateChannelMember` wire: PATCH body `{respond, threshold}`, explicit `null` unset, error mapping
- [`ChannelMembers.test.js`](../../web/src/panels/ChannelMembers.test.js) — inline editor mount/Save/Cancel; Edit withheld for the acting user; open-floor disposition reconstruction (no silent un-gating); empty→null
- [`channel_member_update_test.go`](../../internal/server/channel_member_update_test.go) — the server-side PATCH happy path (204) + validation (400 on bad threshold / disposition, 404 on missing member)

---

## Preconditions

Same base as [MT-CHANNEL-CONFIG-002 § Preconditions](MT-CHANNEL-CONFIG-002.md):
the console + orchestrator up with a provider overlay, and the config-edit toggle
on (the shipped default as of #660).

1. `panels.channel_timeline.config_edit_enabled: true` (shipped default post-#660,
   in [`config/ui.yaml`](../../config/ui.yaml)) — gates the member editor (Edit
   controls) the same way it gates the settings panel and the CLI verbs.

2. Bring the fleet up with the UI and a provider overlay:

   ```bash
   make reset
   ENABLE_UI=1 docker compose -f docker-compose.yaml -f docker-compose.anthropic.yaml up -d --build
   ```

3. Open the console at <http://localhost:8080/ui>, go to the **Channels** tab,
   select the **planning** group channel, and expand the **Members** roster.

---

## Test Procedure

### Step 1: Baseline — the roster shows iron-fox salience-gated with no threshold

In the **Members** roster for `planning`, find the **iron-fox** row.

**Expected**:
- iron-fox is listed as **salience-gated** with **no threshold** (the row's
  effective-policy text reads salience-gated but shows no `threshold N`).
- An **Edit** control is present on the iron-fox row (config-edit is on, and
  iron-fox is not the acting user).

**Cross-check (REST baseline)**:
```bash
curl -s http://localhost:8080/api/v1/channels/group:planning | python3 -m json.tool
```
iron-fox shows `"salience_gated": true` and **no `threshold`** field (unset).

**Verification**:
- [ ] iron-fox renders salience-gated, no threshold; Edit control present.

### Step 2: Edit the salience threshold from the browser — Save

Click **Edit** on the iron-fox row. In the inline editor:

- The **disposition** select shows **Participant (salience bid)** — *not* a bare
  "Always" — confirming the #660 open-floor reconstruction (the persisted
  `"always"` + `salience_gated` was rebuilt to the open-floor disposition).
- The **salience threshold** input is empty (placeholder `unset`).

Set the threshold to **0.8**, leave the disposition on **Participant**, and click
**Save**.

**Expected**:
- The save succeeds (204). The panel sent
  `PATCH …/members/iron-fox` with body `{ "respond": "participant", "threshold": 0.8 }`.
- The iron-fox row re-renders showing **salience-gated · threshold 0.8**.
- No restart occurred.

**Verification**:
- [ ] Editor opens with disposition = Participant (not bare Always); Save lands; row reads salience-gated · threshold 0.8.

### Step 3: Cross-surface read-back — REST/CLI sees the browser's edit, and gating is preserved

```bash
curl -s http://localhost:8080/api/v1/channels/group:planning | python3 -m json.tool
```

**Expected**:
- iron-fox shows `"salience_gated": true` **and** `"threshold": 0.8` — the exact
  value set in the browser, read back from the store through a different surface.
- Critically, `salience_gated` is **still `true`** (not silently flipped to
  `false`) — the #660 reconstruction held: a threshold edit did **not** demote the
  participant to reply-to-everything. (Pre-#660, echoing the bare `"always"` would
  have re-derived `salience_gated=false` here.)

**Verification**:
- [ ] REST read-back shows iron-fox `threshold: 0.8`, `salience_gated: true` — identical to the browser, gating preserved.

### Step 4: Unset the threshold from the browser — empty sends explicit null

Click **Edit** on iron-fox again, clear the threshold input (leave it empty), keep
disposition on **Participant**, and click **Save**.

**Expected**:
- The panel sends `{ "respond": "participant", "threshold": null }` (empty →
  explicit `null` = unset / bias-to-silence). The row re-renders **salience-gated**
  with **no threshold** again.
- REST read-back: iron-fox `salience_gated: true`, **no `threshold`** field.

**Verification**:
- [ ] Empty threshold unsets the bar (explicit null); row + REST show salience-gated, no threshold; gating still true.

---

## Expected Results Summary

| Step | Expected Outcome | Pass/Fail |
|------|-----------------|-----------|
| 1 | iron-fox renders salience-gated, no threshold; Edit present; REST baseline matches | ☐ |
| 2 | Editor opens with disposition reconstructed to Participant; Save → PATCH `{respond:"participant", threshold:0.8}`; row reads threshold 0.8; no restart | ☐ |
| 3 | REST read-back shows threshold 0.8 / salience_gated true — identical to browser, gating preserved (#660 no-silent-un-gating) | ☐ |
| 4 | Empty threshold → explicit null unset; row + REST back to salience-gated, no threshold | ☐ |

---

## Edge Cases & Error Scenarios

### Edge Case 1: Edit withheld for the acting user

The acting console user (a human principal) cannot edit its own membership — the
**Edit** control is withheld on that row, exactly as **Remove** is. (On a default
`planning` with no human member, this is covered deterministically by
`ChannelMembers.test.js`; if a human principal is a member, confirm its row shows
no Edit control.)

### Edge Case 2: Out-of-range threshold → 400 surfaced

Edit iron-fox, set the threshold to **1.5** (outside [0, 1]), Save.

**Expected**: the PATCH round-trips to a **400** (`ErrInvalidThreshold`) and the
panel surfaces the server's error — nothing persists. The client-side `min`/`max`
bounds are advisory; the server stays the authority. (REST read-back unchanged.)

### Edge Case 3: Threshold on a non-open-floor disposition → 400

Edit **ember-owl** (a `when_mentioned` member): set the disposition to **When
mentioned** (or any non-open-floor option) **and** enter a threshold. Save.

**Expected**: **400** — a salience threshold is only meaningful on an open-floor
disposition (participant/chair); the server rejects it and the panel surfaces the
error. (Setting ember-owl to **Participant** with a threshold would be accepted —
that is a valid disposition change, not this rejection.)

---

## Test Results

| Date | Tester | Build | Result | Notes |
|------|--------|-------|--------|-------|
| _pending_ | | | ☐ Not yet run | First live exercise of the #660 member-threshold web editor (the last RFC 0050 Phase 2 slice). |
| 2026-06-16 | Maksim Khomutov | `015149a` (Anthropic overlay, `ENABLE_UI=1 up --build`; Chrome-driven) | **PASS (steps 1–4 + EC2/EC3 server-side; EC1 + panel-surfacing deferred to vitest)** | First live exercise. **Step 1** ✅ roster shows "Iron Fox always salience-gated" with **no threshold**, Edit present; REST baseline `salience_gated:true`, threshold unset. **Step 2** ✅ the decisive #660 check: clicking Edit opened the inline editor with disposition **"Participant (salience bid)"** — *not* a bare "Always" — confirming the open-floor reconstruction (`startEdit` rebuilds the disposition from `salience_gated && respond==="always"`). Set threshold 0.8 + Save → `PATCH …/members/iron-fox` **204**; row re-rendered **salience-gated · threshold 0.8**. **Step 3** ✅ G4 — REST read-back: iron-fox `threshold:0.8`, **`salience_gated:true`** (gating *preserved* — the edit did **not** silently demote to reply-to-everything, the exact regression #660 guards). **Step 4** ✅ re-Edit, clear the input (shows `unset`) + Save → `{respond:"participant", threshold:null}` (204); REST back to `threshold:<unset>`, `salience_gated:true`. **EC2** ✅ (server-side curl) threshold 1.5 → **400** `invalid member threshold: 1.5 (must be a finite value in [0, 1])`; iron-fox unchanged. **EC3** ✅ (server-side curl) threshold on `when_mentioned` (ember-owl) → **400** `threshold not applicable to disposition: … only an open-floor disposition (participant/chair/always) runs the salience bid`. **EC1 (Edit withheld for the acting user) not exercised live** — the acting principal was `local`, not a channel member, so no row matched; pinned by `ChannelMembers.test.js`. The panel-surfacing of the EC2/EC3 400s is likewise pinned by the vitest suite — the live curl confirms the server authority the panel relays. **UI note**: the member roster disclosure collapses when the message feed re-renders (personas replied to a nudge mid-test), so the editor must be re-opened after such a refresh — cosmetic, not a fault. |

## Notes

- **Keep `git diff` clean.** Revert any member-config edits after the run
  (re-unset the threshold, or `make reset`), so the recorded build is honest about
  what shipped.
- **The behavioral honor is out of scope.** A live salience bid is probabilistic
  and provider-dependent; this MT verifies the *config path* (browser → store →
  read-back) and the no-silent-un-gating guard, not that a higher bar measurably
  changes one bid. The bid mechanics are covered by the RFC 0030 salience suites.
- **`salience_gated` is the gating witness, not `respond`.** The persisted
  `respond` normalizes to the legacy triple (`"always"`), so it cannot tell an
  open-floor participant from an unconditional responder; `salience_gated` is the
  field that confirms the disposition took effect. Read it, not `respond`, when
  checking gating.
