---
id: ISSUE-0114
summary: "No per-channel cascade-depth override: the ISSUE-0109 calibration documented the fleet-wide `max_cascade_depth` (default 5) as the DE FACTO discussion-length knob on a productive autonomous roster (the ISSUE-0110 continuation advances round tally and reply depth together, so the depth cap binds before max_rounds on every productive chain), yet the cap is a single top-level channels.yaml value on an unsynchronised router-global field — lengthening or shortening ONE channel's productive chain means retuning the whole fleet. A per-channel override is a real feature, not a default: per-channel config + schema + validation, a synchronized hot-path read, an RFC 0050 runtime-override decision, and the Go/Python depth-cap alignment (the Python EventDispatcher defense-in-depth cap is per-process and must not fire below a legitimately raised channel cap). Deliberately scoped OUT of the ISSUE-0109 tuning PR (#769); filed for explicit slotting."
status: open
severity: low
area: channels
created: 2026-07-25
refs:
  - docs/issues/ISSUE-0109-rfc0052-autonomous-defaults-calibration.md
  - docs/rfcs/0011-amendment-cascade-depth-wire-propagation.md
  - internal/channels/cascade_depth.go
  - internal/channels/config.go
  - internal/channels/autonomous_continuation.go
  - agents/dispatch.py
  - schemas/channel.schema.json
  - docs/guides/channels.md
---

## Summary

The cascade-depth cap is fleet-wide only, but post-ISSUE-0109 it is documented
as the de facto length knob for productive autonomous discussions — so the one
knob an operator most plausibly wants to tune per channel cannot be tuned per
channel.

## Context

Recorded as the first "residual deliberately NOT taken" in the
[ISSUE-0109 Resolution](ISSUE-0109-rfc0052-autonomous-defaults-calibration.md)
(calibration PR #769, merged 2026-07-24). The v0.3.11 soak's finding 1: all
five productive arcs closed on the depth bound, and `max_rounds` never fired
at 6/8/12 — because the ISSUE-0110 productive-round continuation re-fans the
round's last reply, advancing the round tally and the reply's cascade depth
*together*. `max_rounds` is the stall-arc net; the depth cap is the
productive-chain length knob (guide §"Tuning an autonomous roster").

Today's mechanics (one conceptual cap, two enforcement points):

- **Go (primary):** top-level `max_cascade_depth:` in `channels.yaml` →
  `Config.MaxCascadeDepth` (`internal/channels/config.go`) →
  `ChannelRouter.SetMaxCascadeDepth` at startup → the **unsynchronised**
  router-global `maxCascadeDepth` (`internal/channels/cascade_depth.go`),
  read on the hot publish path (fanout suppression + inbound clamp) and by
  `closeOnCascadeBound` (`internal/channels/autonomous_continuation.go`).
  Zero/absent means "use the default" (the cap cannot be config-disabled);
  the schema rejects negatives at `make validate`.
- **Python (defense-in-depth):** `EventDispatcher.max_cascade_depth`
  (`agents/dispatch.py`, `DEFAULT_MAX_CASCADE_DEPTH`), a per-process value
  aligned by convention with the Go cap per the
  [RFC 0011 cascade-depth amendment](../rfcs/0011-amendment-cascade-depth-wire-propagation.md).

`ChannelConfig` carries no depth field, and the RFC 0050 runtime-override
surface (`ChannelConfigOverrides`) does not either.

## Impact

Nothing breaks — this is a tunability gap. An operator who wants one
autonomous channel's productive discussions longer (a deliberative roster) or
shorter (a cheap triage room) must move the fleet-wide cap, changing every
channel's fanout-suppression bound at once. The shipped per-channel knobs that
*look* like length controls do not bind on a productive chain: `max_rounds`
(structurally unreachable above the depth cap there) and the cost cap (the
soak peaked at 0.59 utilization). The practical workaround today is topic and
roster design, not configuration.

## Proposed fix / investigation path

A real feature, sized accordingly (this is why it was not folded into the
ISSUE-0109 defaults PR):

1. **Config + schema:** optional `max_cascade_depth` on `ChannelConfig`,
   resolved like `ResolveInteractionBudgetTokens` (zero/absent → fleet
   value); schema + `config_validate.go` reject negatives; keep the
   "cannot be silently disabled" posture (zero means inherit, not infinite).
2. **Hot path:** `maxCascadeDepth` is unsynchronised by design (set once
   pre-traffic). A per-channel value needs a synchronized map keyed by
   channel id — the `endVoteThresholds`/`endVoteWindows` mutex pattern is
   the in-package precedent — read at the fanout-suppression and clamp
   sites, which then need the channel id in hand.
3. **RFC 0050 surface:** decide whether the override is runtime-PATCHable
   (`ChannelConfigOverrides`) or config-as-code only. If PATCHable, mirror
   the `SetEndVoteParams` posture: warn loudly on foot-guns rather than
   rejecting a live edit.
4. **Go/Python alignment (the hard part):** the Python dispatcher cap is
   per-process, not per-channel. Options: (a) set the Python cap to the
   fleet **maximum** of per-channel caps so defense-in-depth never fires
   below a legitimately raised channel cap (weakens the backstop for
   low-cap channels); (b) teach the dispatcher per-channel awareness
   (couples it to channel config it deliberately does not read today);
   (c) declare the Python cap a global backstop only and require
   per-channel caps ≤ the global value (cheapest; makes "raise one
   channel above 5" require raising the global backstop too — which may
   be acceptable). The choice is the design decision this issue exists to
   force before code is written.
5. **Autonomous coupling:** `closeOnCascadeBound` and the arming validation
   read the same cap; a per-channel value flows through the continuation's
   close decision — the guide's tuning section and RFC 0052's structural
   close description need the "per channel" qualifier once this lands.

## Notes

> 2026-07-25 — filed from the ISSUE-0109 Resolution residuals list, at the
> maintainer's request, after the post-merge verification of PR #769.
> Candidate for the v0.3.12 scope lock (alongside RFC 0039 and the proposed
> ISSUE-0106 slot); not slated to any train yet.

> 2026-07-25 (later) — **Slotted into v0.3.12 as a cuttable fold-in** at the
> [v0.3.12 plan opening](../v0.3.12-plan.md#scope-decisions-locked-at-plan-authoring-time-2026-07-25).
> Plan-opening default for step 4 (Go/Python alignment): **option (c)** — the
> Python dispatcher cap stays a per-process global backstop and per-channel
> caps must be ≤ it (raising one channel above the fleet default means raising
> the backstop too) — revisitable in the fold-in PR with the alternatives
> above. Droppable to v0.3.13 without touching either v0.3.12 workstream.
