---
id: ISSUE-0103
summary: "The first live config edit on a YAML-seeded channel silently resets every OTHER non-default per-channel knob to the fleet default — most visibly it detaches the escalation chair. ApplyChannelConfig is store-canonical: it merges the sparse patch onto the channel's STORE overrides (empty at revision 0, because YAML config-as-code is never persisted there) and re-stamps all six router-held knobs from that merged set, so any knob the patch omits reverts to default rather than its YAML value. The cross-field escalation-chair/floor-control validator cannot warn because the merged set carries no chair."
status: resolved
severity: medium
area: internal/channels
created: 2026-06-14
closed: 2026-06-15
closed_pr:
refs:
  - docs/rfcs/0050-extensible-channel-configuration.md
  - docs/manual-tests/MT-CHANNEL-CONFIG-001.md
  - docs/issues/ISSUE-0095-idle-rotation-no-fire-observability.md
---

## Summary

On a channel whose governance config comes from `config/channels.yaml`
(revision 0 — config-as-code owned, never written to the store), the **first**
runtime config edit through the RFC 0050 apply path silently resets **every
other non-default per-channel knob to its fleet/package default**. The most
visible casualty is `escalation_chair_id`: editing an unrelated knob (e.g.
`interaction_idle_timeout_seconds`) detaches the channel's escalation chair with
no validation error and no warning. The change is store-canonical, so it
persists and survives restart.

## Context

Found live during the first run of
[MT-CHANNEL-CONFIG-001](../manual-tests/MT-CHANNEL-CONFIG-001.md) (2026-06-14,
HEAD `3402f0e`). On the default `planning` channel:

```
$ ./bin/persatrix channel config get planning        # before
  escalation_chair_id               nova-sparrow  [default]
  interaction_idle_timeout_seconds  600           [default]
$ ./bin/persatrix channel config set planning interaction_idle_timeout_seconds=60
  escalation_chair_id               (none)        [default]   # ← silently detached
  interaction_idle_timeout_seconds  60            [channel]
```

The mechanism is the store-canonical apply contract, working as documented but
with an unflagged consequence:

- `ChannelRouter.ApplyChannelConfig`
  ([`config_apply.go`](../../internal/channels/config_apply.go)) treats `patch`
  as the COMPLETE desired override set and, after persisting, re-seeds **all six**
  router-held knobs via `applyOverridesToRouter` — "present → value, absent →
  inherited default."
- The REST layer merges the sparse `{knob: value}` patch onto the channel's
  **stored** overrides
  ([`channel_config_handlers.go`](../../internal/server/channel_config_handlers.go)
  `mergeConfigPatch`). For a revision-0 channel the store has **no** overrides —
  the YAML-declared `escalation_chair_id` lives only on the router (seeded by
  `ResolveEscalationChairs`), never in the store
  ([`config_reconcile.go`](../../internal/channels/config_reconcile.go): "revision
  == 0 … leave the store untouched").
- So the merged set carries only the one edited knob; the re-stamp drops the
  chair (`SetEscalationChair(_, "")`).
- The cross-field rule `validateEscalationChair` (a chair requires floor control)
  is structurally unable to warn: it validates the merged set, which has a nil
  chair, so it returns early.

Pinned by `TestApplyChannelConfig_FirstEditDetachesYAMLSeededChair` and
`TestApplyChannelConfig_LoneFloorControlFalseDoesNotSeeYAMLSeededChair`
([`config_apply_test.go`](../../internal/channels/config_apply_test.go)).

## Impact

An operator who changes **one** governance knob on a YAML-configured channel
silently resets that channel's **entire** non-default governance profile to
fleet defaults except the knob they touched: the escalation chair is unset
(stall-escalation disabled), and any non-default salience cap, end-vote
quorum/window, or reply budget reverts too. No error, no warning; the loss
persists across restart. The operator believes they made a one-knob change.

Today the blast radius is bounded — the feature ships dark behind
`config_edit_enabled` (default off) and only `planning` carries a non-default
chair — but it becomes a routine footgun the moment the Phase 2 web settings
panel lands and operators edit channels that were configured in YAML.

## Proposed fix / investigation path

This is an RFC 0050 design call; options, roughly in order of principle:

1. **Seed YAML per-channel config into the store as overrides at boot** so
   config-as-code IS the initial store state. Then the first edit merges onto a
   full override set and nothing is silently dropped. Changes the revision-gate
   story (YAML-configured channels no longer start at a bare revision 0) — the
   biggest change, but the only one that makes "store is the single source of
   truth" literally true from boot.
2. **Make the cross-field validator consult effective state**: when a patch
   omits the chair but the router currently holds one, either preserve it or
   reject a chair-dropping apply. Narrow (chair-only), but closes the most
   dangerous case cheaply.
3. **Warn loudly on lossy first edits**: when an apply on a revision-0 channel
   would reset a currently-non-default resolved knob absent from the patch,
   surface a warning (CLI + audit log) so the silent reset becomes visible.

Recommend (1) as the real fix and (2)/(3) as cheap interim guards.

## Resolution

Fixed via option (3 of the proposed list, refined): **seed the merge base from
the channel's resolved governance on the first edit** — the lazy, on-first-edit
form of option 1, not the eager seed-at-boot form (which would push every
YAML channel off revision 0, break the byte-identical-boot property, and trigger
the FREEZE CONSEQUENCE / false drift for channels nobody ever edited).

`handlePatchChannelConfig`
([`channel_config_handlers.go`](../../internal/server/channel_config_handlers.go))
now chooses the merge base: for a channel already at revision > 0 the store is
canonical (base = stored overrides, unchanged); for a revision-0 channel with a
non-empty patch the base is `Server.resolvedConfigBaseline(id)` — a complete
snapshot of the channel's six router-held knobs read from the same getters
`buildChannelConfigResponse` uses. The sparse patch then layers over that
baseline, so the apply path receives the channel's full resolved set plus the
edit; nothing un-edited is dropped, the chair survives, and the channel becomes
store-canonical with a faithful snapshot — the same transition the YAML *adopt*
path makes via `ChannelConfig.toConfigOverrides`.

Deliberate, documented consequence (the FREEZE CONSEQUENCE, already accepted on
the adopt path): a first edit flips the previously-inherited knobs' provenance
from `default` to `channel` and detaches them from fleet-default tracking. This
is strictly better than the old reset-to-package-default data loss and is
consistent with how adoption already works. True sparse-layering over the live
YAML baseline (keeping inherited knobs tracking the fleet default) remains RFC
0050 Phase 3.

`ApplyChannelConfig`'s wholesale-replace contract is unchanged — the fix is one
layer up, at the REST merge. The two characterization tests still pin that
contract; the end-to-end regression is
`TestChannelConfig_FirstEditPreservesYAMLSeededChair` (+
`TestChannelConfig_FirstEditFreezesDefaultsAsChannel`) in
[`channel_config_handlers_test.go`](../../internal/server/channel_config_handlers_test.go).
This unblocks flipping `config_edit_enabled` on (RFC 0050 Phase 2 prerequisite).

## Notes

> 2026-06-15 — resolved (this PR). See Resolution above.
> 2026-06-14 — captured during the MT-CHANNEL-CONFIG-001 first live run; the MT
> documents the behavior (Step 2 side-effect + Edge Case 2) and the two
> characterization tests pin it. This issue tracks the underlying fix, which is
> deliberately out of scope of the docs work that surfaced it.
