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

> 2026-07-31 — **CUT from v0.3.12 → v0.3.13** (maintainer call, exercised at
> [release-prep PR 2](../v0.3.12-release-prep-plan.md#pr-2--docs--release-checklist)
> per the master plan's "closed (or explicitly cut)" acceptance wording): the
> fold-in was never implemented during the v0.3.12 window and neither
> workstream depends on it. Listed in the
> [v0.3.12 release checklist Known Gaps](../v0.3.12-release-checklist.md#6-known-gaps-to-document-in-release-notes);
> the design fork in step 4 (default: option (c)) is unchanged and still the
> first decision the implementing PR must take.

> 2026-08-02 — **v0.3.12 shipped without it, as the cut anticipated**
> ([v0.3.12 — Memory that travels](https://github.com/mkhomutov/Persatrix/releases/tag/v0.3.12),
> tagged on `c833da34`). The cut is confirmed by the release rather than
> merely planned: the fleet-wide `max_cascade_depth` remains the de facto
> discussion-length knob in the shipped binary, and the issue carries to
> **v0.3.13** as the first of that line's three deferred calls (with
> [ISSUE-0118](ISSUE-0118-tool-recall-bypasses-epoch-session-scopes.md) and
> [ISSUE-0121](ISSUE-0121-crossroom-person-identity-legs-never-run-live.md)).
> Documented in the published release body's Known Gaps section.

> 2026-08-03 — **v0.3.13 plan opened**
> ([v0.3.13-plan.md](../v0.3.13-plan.md)): rides as a named scope item
> (`feature/v0313-issue0114-cascade-depth`, parallel to the ISSUE-0118 fix).
> The step-4 default — **option (c)**, per-channel caps validated ≤ the
> Python per-process backstop — is reconfirmed as the plan-opening posture
> and remains the first decision of the implementing PR, revisitable there;
> the RFC 0050 PATCH-vs-config-as-code call is made in that PR too.

> 2026-08-03 — **Implemented** (`feature/v0313-issue0114-cascade-depth`,
> v0.3.13 PR 2). The five steps landed as scoped, with the two in-PR
> decisions taken as follows:
>
> - **Step 4 (Go/Python alignment): option (c), as defaulted** — the Python
>   dispatcher cap stays a per-process global backstop
>   (`agents/cascade_depth_defaults.py` now says so explicitly), and the
>   ≤-fleet requirement is enforced where each write path can honor it:
>   the **YAML loader rejects** a per-channel cap above the resolved fleet
>   cap (`Config.Validate` — config-as-code can always be fixed before
>   boot), while a **live RFC 0050 edit warns and applies**
>   (`SetChannelMaxCascadeDepth`, mirroring the `SetEndVoteParams` k>w
>   posture: the fleet cap is startup-only, so a reject would force a
>   restart into a live edit loop; the warning also covers boot replay of
>   a store written before a fleet lowering, since it lives in the setter
>   both callers funnel through). The failure mode above the fleet cap is
>   degraded-not-runaway (the backstop suppresses first; stall/idle/cost
>   still terminate), which is what makes warn-don't-reject safe live.
> - **Step 3 (RFC 0050 surface): PATCHable.** `max_cascade_depth` is the
>   ninth flat knob on `ChannelConfigOverrides` — merge case, apply-path
>   stamp, GET provenance, web-console row (the knob registry moved to
>   `web/src/lib/channelKnobs.js` at the panel's 500-line cap). Two
>   capture seams are **conditional** (the chair precedent, not the
>   unconditional flat knobs): the adopt freeze (`toConfigOverrides`)
>   captures only a declared knob — both so adopted channels keep
>   tracking the fleet cap and so pre-v0.3.13 store rows keep hashing
>   identically to their re-resolved YAML (no spurious equal-revision
>   drift warning at the first post-upgrade boot) — and the ISSUE-0103
>   first-edit baseline freezes only an explicit router entry, keying on
>   `MaxCascadeDepthFor`'s set flag.
>
> Mechanics: `channelCascadeCaps` (own `cascadeMu`, the
> `endVoteThresholds` map pattern) read at the publish clamp + fanout
> suppression (`publishCommit` resolves once per publish), the
> continuation's terminal-bound check, and `closeOnCascadeBound`'s log;
> `ResolveChannelCascadeCaps` seeds declared channels at startup (the
> `ResolveEndVotes` no-store-enumeration posture — everyone else falls
> back to the fleet cap at read time, so DMs/threads are unchanged).
> Deterministic CI pins the precedence, both loader rejections, the
> above-fleet warn, the per-channel clamp/suppression binding beside an
> untouched sibling channel, the **autonomous structural close at the
> per-channel cap with the fleet cap untouched** (the headline shape),
> the apply/inherit round-trip, the REST PATCH surface (including the
> 400-mapping for `ErrInvalidMaxCascadeDepth`, which the new tests
> caught missing), both conditional captures, and the schema (Go + the
> Python `make validate` suite). Closure (status → resolved) rides the
> Phase 3 release-prep arc per the plan: a per-channel cascade-depth arc
> on a live autonomous roster verifies the knob binds where the fleet
> value used to.
