---
id: ISSUE-0103
summary: "The first live config edit on a YAML-seeded channel silently resets every OTHER non-default per-channel knob to the fleet default — most visibly it detaches the escalation chair. ApplyChannelConfig is store-canonical: it merges the sparse patch onto the channel's STORE overrides (empty at revision 0, because YAML config-as-code is never persisted there) and re-stamps all six router-held knobs from that merged set, so any knob the patch omits reverts to default rather than its YAML value. The cross-field escalation-chair/floor-control validator cannot warn because the merged set carries no chair."
status: open
severity: medium
area: internal/channels
created: 2026-06-14
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

## Notes

> 2026-06-14 — captured during the MT-CHANNEL-CONFIG-001 first live run; the MT
> documents the behavior (Step 2 side-effect + Edge Case 2) and the two
> characterization tests pin it. This issue tracks the underlying fix, which is
> deliberately out of scope of the docs work that surfaced it.
