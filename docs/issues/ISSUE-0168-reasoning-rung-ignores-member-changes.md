---
id: ISSUE-0168
summary: "A channel's reasoning rung (`off`, `bid` or `plan`) is worked out from its members only at startup, on a config change and when a group is created. Adding or promoting a member through the member API leaves it alone, so a channel that becomes governed keeps the old score gate (`off`) instead of the `bid` default, and GET /config calls that `off` the default. The next unrelated config edit then saves the stale `off` as an explicit kill switch that survives restarts. The same logic has three relatives: a revision-0 YAML channel is re-stamped from its YAML roster on every boot; an explicit YAML `mode: off` on a roster that starts ungoverned is never saved, so it turns into `bid` after a promotion and a restart; and dropping a drifted mode keeps `revise >= 1`, so an unrelated first edit fails with 400."
status: open
severity: medium
area: channels
created: 2026-09-24
refs:
  - https://github.com/mkhomutov/Persatrix/pull/990
  - https://github.com/mkhomutov/Persatrix/pull/697
  - internal/channels/router_reasoning.go
  - internal/channels/config_apply.go
  - internal/channels/config_reasoning.go
  - internal/channels/dispatch_to.go
  - internal/server/channel_governance.go
  - internal/server/channel_handlers.go
  - internal/server/channel_member_handlers.go
  - internal/server/channel_delete_handlers.go
  - internal/server/channel_config_reasoning.go
  - docs/rfcs/0051-reasoning-before-posting.md
---

# ISSUE-0168: The reasoning rung does not follow membership changes

## Summary

A channel's `reasoning.mode` picks how its personas decide whether to speak.
When a channel sets no `mode`, its members decide the default. If at least one
member runs the salience bid (a `participant` or `chair`, or a legacy `always`
member given an explicit `threshold`), the channel is *governed* and gets `bid`: the persona privately asks whether a reply adds anything.
Otherwise the channel gets `off`, the older numeric score gate. The orchestrator
works this out only at startup, when a config change is applied, and when a
group is created. A member added or promoted through the member API does not
trigger it. So a channel that gains a `participant` keeps `off`, and a later,
unrelated config edit can make that `off` permanent.

## Context

**Where the rung is set.** `ChannelRouter.SetReasoning` has three callers:

- `ResolveReasoning` at startup
  ([`router_reasoning.go`](../../internal/channels/router_reasoning.go)). A
  channel declared in `config/channels.yaml` gets its load-time block, worked
  out from the YAML roster; a store-only group gets
  `governedReasoningBase(channelGoverned(...))`.
- `applyOverridesToRouter` on a runtime config change and on boot replay
  (`ResolveFromStore`, for revision > 0)
  ([`config_apply.go`](../../internal/channels/config_apply.go)).
- `applyRuntimeGroupGovernance` when a group is created at runtime
  ([`channel_governance.go`](../../internal/server/channel_governance.go)).

The member handlers write only to the store: `handleAddChannelMember`
([`channel_handlers.go`](../../internal/server/channel_handlers.go)),
`handleUpdateChannelMember`
([`channel_member_handlers.go`](../../internal/server/channel_member_handlers.go))
and `handleDeleteChannelMember`
([`channel_delete_handlers.go`](../../internal/server/channel_delete_handlers.go)).
Fanout reads each member's `salience_gated` flag live from the store but sends
the router's saved rung (`ReasoningMode: reasoning.Mode` in
[`dispatch_to.go`](../../internal/channels/dispatch_to.go)). The saved rung goes
stale in both directions: a channel that gains a gated member keeps `off`, and
one that loses its last gated member keeps `bid` or `plan`. The second is inert
at dispatch, since no recipient runs the salience gate, but case 5 below trips
on it.

A config change re-derives the rung only when it can: a PATCH that clears
`mode`, or any change on a channel already at revision > 0. The first unrelated
edit at revision 0 instead freezes whatever the router holds (case 2).

**Five consequences.** Each was reproduced during the
[#990](https://github.com/mkhomutov/Persatrix/pull/990) review with a throwaway
test run through `go test -overlay`; no code was changed.

1. **A new participant does not switch the channel to `bid`.** A group created
   with no salience-gated member is stamped `off`. After
   `POST /api/v1/channels/{id}/members` with `"respond": "participant"`, or a
   member PATCH that promotes someone, the channel is governed but its
   dispatches still carry `off`. `GET /config` reports `mode: off` with source
   `default`. A restart fixes it, unless case 2 or 3 applies.
2. **The next config edit makes the stale `off` permanent.** In that state, the
   first unrelated config PATCH (for example `{"end_vote_threshold": 3}` with
   `If-Match: 0`) runs the first-edit freeze, `Server.reasoningBaseline`
   ([`channel_config_reasoning.go`](../../internal/server/channel_config_reasoning.go)).
   It reads governance live (governed) but the rung from the router (`off`).
   `ReasoningConfig.FreezeOverrides(true)` sees `off`, which is not the governed
   default, and stores it as an explicit override. The channel is now pinned to
   a kill switch nobody set, with source `channel`, across restarts.
3. **A YAML channel is re-stamped from its YAML roster on every boot.** A
   revision-0 channel whose YAML members are not salience-gated, one of whom was
   promoted through the member API, gets `off` at each startup:
   `ResolveReasoning` uses the block worked out from the YAML roster, revision 0
   skips the store overlay, and reconcile compares member IDs only (a
   disposition change is not divergence). A store-only group with the same
   roster boots to `bid`.
4. **An explicit YAML kill switch can be lost.** A YAML channel with
   `revision: 1`, `reasoning: {mode: "off"}` and an ungoverned YAML roster is
   frozen with `governed=false` when it is adopted. There `off` is the default,
   so nothing is stored. After a member promotion and a restart,
   `applyOverridesToRouter` sees a governed channel and resolves `bid`, with no
   drift warning. The same YAML with a governed roster keeps `off`, and so does
   a revision-0 channel.
5. **Dropping a drifted mode blocks the first edit.** A channel at
   `{mode: plan, revise: 1}` whose salience-gated member is demoted or removed:
   the first-edit freeze drops the drifted `mode` but keeps `revise: 1`.
   `ChannelRouter.validateReasoningGoverned` then rejects the merged set, since
   `revise >= 1` needs `plan` and the unset mode reads as `off`. An unrelated
   first config edit fails with 400. For a revision-0 YAML channel this repeats
   after every restart, because the YAML rung is stamped again at boot. The
   freeze's own comment names a narrower limitation (revision > 0 only).

**What the design promised.** RFC 0051
[§G](../rfcs/0051-reasoning-before-posting.md#g-configuration--an-rfc-0050-knob)
says the `bid` default takes effect when a channel becomes governed, and that
`mode: off` is a true one-flip kill switch. Cases 1–3 break the first promise
and case 4 the second. Case 5 breaks the freeze's own rule that a drifted mode
must not block an unrelated first edit.

## Impact

- **Operator-visible.** A governed channel can run the score gate (the
  per-member `threshold` in force, no semantic silence) without anyone asking
  for it, and the console calls that the default.
- **Case 2** turns a passing state into a permanent override that looks as if
  an operator set it.
- **Case 4** throws away an operator's explicit kill switch.
- **Case 5** blocks every first config edit on the channel until an operator
  clears `revise`.
- **Bounded.** Each case needs a member change after the rung was stamped. The
  wrong rung is `off` (the pre-v0.3.10 behaviour) or `bid` (the shipped
  default). There is no data loss, cost overrun or security exposure.

## Proposed fix / investigation path

Each step needs a failing test first; the overlay tests above are the shape.

1. **Record what was set, not what differs from today's default.** Keep, per
   channel, which reasoning sub-knobs an operator set (in YAML or through an
   override). Resolve an unset `mode` from live membership where it is used:
   fanout already has the roster, so `AnySalienceGated(members)` costs no extra
   read. Alternatively, re-stamp the rung from the member handlers. Either way,
   `FreezeOverrides` should freeze only what was set. That fixes cases 1, 2 and 4.
2. **Case 3:** at boot, work out a YAML channel's rung against the store roster,
   or treat a member's disposition change as divergence.
3. **Case 5:** drop `revise` together with the drifted `mode`, or validate the
   merged set against the resolved rung.
4. **Related:** `validateReasoningGoverned` reads an unset `mode` as `off`
   through `ReasoningOverrides.effectiveMode`. That gives the right answer for
   today's two rules, because on a governed channel `bid` passes the membership
   rule and fails the `revise` rule just as `off` does. A future rule that tells
   `off` from `bid` would get it wrong. Resolving the patch over
   `governedReasoningBase(governed)` and calling `ReasoningConfig.validate`
   would keep a single set of rules.

## Slot

Not slotted. No release plan is open: ruling (a) of the
[sequencing Amendment 2026-09-12](../v0.3.x-sequencing.md#amendment-2026-09-12--close-v0316-small-then-measure-before-any-train-opens)
opens none before EXP-001 reports and the first strategy review logs its result.
The [EXP-001](../experiments/EXP-001-preregistration.md) runs are not affected:
[`panel.yaml`](../../evaluators/experiments/EXP-001/panel.yaml) sets each
channel's reasoning mode explicitly (`bid`, and `off` for arm B), and its one
member change, at the memo turn, leaves the chair's disposition alone, so no
channel changes between governed and ungoverned.

## Notes

> 2026-09-24 — filed from the review of
> [#990](https://github.com/mkhomutov/Persatrix/pull/990), outside that PR's
> comments-only scope. Several reviewers found it independently, and each case
> was reproduced with a throwaway test and checked against a control. #990
> rewrote the comments that described the default as following membership
> live.
