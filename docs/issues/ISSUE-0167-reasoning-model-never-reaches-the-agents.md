---
id: ISSUE-0167
summary: "`reasoning.model` never reaches the agents. The orchestrator validates it, stores it, reports it and warns about `quality`, but each dispatch carries only the channel's reasoning `mode` and `revise`, and the agents run the bid and the reflexion critic on the `fast` model alias no matter what is set. So `model: quality` changes nothing, while the channel config comment, the schema, the Go docs, the persona-agents guide and the load-time warning all say it moves the deliberation onto the expensive model. RFC 0051 §G's own rule is to reject a value that is not backed rather than quietly run a lesser one, and this value is quietly run as `fast`."
status: open
severity: medium
area: channels
created: 2026-09-24
refs:
  - https://github.com/mkhomutov/Persatrix/pull/989
  - https://github.com/mkhomutov/Persatrix/pull/990
  - internal/channels/config_reasoning.go
  - internal/channels/dispatch_to.go
  - internal/channels/router_reasoning.go
  - internal/channels/config_apply.go
  - proto/task.proto
  - agents/salience_bid.py
  - agents/persona_runtime/reflexion.py
  - config/channels.yaml
  - schemas/channel.schema.json
  - docs/guides/persona-agents.md
  - docs/rfcs/0051-reasoning-before-posting.md
  - docs/rfcs/0051-pr-plan.md
---

# ISSUE-0167: `reasoning.model` never reaches the agents, so `model: quality` changes nothing

## Summary

A channel's `reasoning` block has a `model` setting. It is meant to choose which
model runs the persona's private "should I post, and what should I say"
deliberation: `fast` (cheap, the default) or `quality` (expensive). The setting
is checked, saved, shown in the console and the CLI, and logged with a warning
when it is `quality`. But nothing ever sends it to the agents, which always run
the deliberation on `fast`. An operator who picks `quality` gets `fast`, with a
warning that says the opposite.

## Context

**What the orchestrator sends.** Fanout copies the channel's resolved reasoning
block onto each dispatch in `dispatchTo`
([`dispatch_to.go`](../../internal/channels/dispatch_to.go)), and it copies two
fields only: `ReasoningMode` and `ReasoningRevise`. The wire message has the same
two: `reasoning_mode = 25` and `reasoning_revise = 26` on `ChannelMessageEvent`
([`task.proto`](../../proto/task.proto)). There is no field for the model.
Outside the dispatch, `ChannelRouter.ReasoningFor` is read only by the REST
config GET and by the first-edit freeze, neither of which reaches an agent.

**What the agents run.** The deliberation is the RFC 0030 Tier B salience bid in
its structured form, and `evaluate_salience`
([`salience_bid.py`](../../agents/salience_bid.py)) has no model parameter. It
resolves `_BID_MODEL_ALIAS`, which is the constant `"fast"`, described in the
code as the alias "the bid always runs on, regardless of the persona's quality
model". The reflexion critic added in RFC 0051 Phase 5 is fixed the same way:
`_CRITIC_MODEL_ALIAS = "fast"`
([`reflexion.py`](../../agents/persona_runtime/reflexion.py)). No agent code
reads a reasoning model.

**What the orchestrator does with the value.** It validates it (`fast` or
`quality`), stores it, returns it with provenance on
`GET /api/v1/channels/{id}/config`, and freezes it into the first-edit baseline.
When it is `quality`, two places log
`reasoning.model=quality defeats the cheap-pass economics (RFC 0051 §F); prefer fast`:
`ResolveReasoning` at startup
([`router_reasoning.go`](../../internal/channels/router_reasoning.go)) and
`ApplyChannelConfig` on a runtime edit
([`config_apply.go`](../../internal/channels/config_apply.go)). Nothing else
reads it.

**What the docs promise.** Each of these says the value picks the deliberation
model:

- the `planning` channel's comment in [`config/channels.yaml`](../../config/channels.yaml):
  "`model: fast` keeps the deliberation on the cheap leased model (quality is
  accepted but defeats the cheap-pass economics)";
- `ReasoningModelQuality` and the `ReasoningConfig.Model` field in
  [`config_reasoning.go`](../../internal/channels/config_reasoning.go): "runs the
  deliberation on the expensive model" and "the leased model the deliberation
  pass runs on";
- the `model` description in [`channel.schema.json`](../../schemas/channel.schema.json):
  "Which leased model runs the deliberation pass";
- the reasoning section of the [persona agents guide](../guides/persona-agents.md):
  "`reasoning.model` (the deliberation model …)";
- the configuration sample in [RFC 0051 §G](../rfcs/0051-reasoning-before-posting.md#g-configuration--an-rfc-0050-knob):
  `model: fast  # alias for the deliberation pass`.

The CLI and the web console offer `reasoning.model` as an editable setting too.

**How it happened.** The [RFC 0051 PR plan](../rfcs/0051-pr-plan.md) gave `mode`
and `model` "the full validate→apply→persist path" in PR 4, and the router holds
both. The go-live in PR 6 then put only `reasoning_mode` on the wire, because
that was what the dark rung needed. PR 8 added `reasoning_revise` for the
reflexion loop. No PR sent `model`, and no plan row, known gap or issue records
that it was left out.

## Impact

- **An operator's choice is silently ignored.** Setting `model: quality` to get
  a more careful decision about whether to speak does nothing. The console and
  `config get` report `quality` as the channel's setting, so nothing a reader can
  see says otherwise.
- **The one signal is wrong.** The warning says `quality` "defeats the cheap-pass
  economics". It cannot: the bid stays on `fast`, so the cost does not move.
- **It is the silent downgrade RFC 0051 rules out.** §G says validation rejects a
  value whose backing is not deployed "rather than silently degrading to the
  nearest implemented rung", and calls silent degradation "the classic
  feature-flag footgun". `depth: deep` is rejected for that reason; `model:
  quality` is accepted and run as `fast`.
- **No cost or safety exposure.** The effect is only ever cheaper than asked for,
  and the shipped config sets `fast`, so a default deployment behaves exactly as
  documented.

## Proposed fix / investigation path

Either send the value or stop promising it. In the order worth trying:

1. **Send it.** Add a reasoning-model field to `ChannelMessageEvent` and the
   dispatch envelope, stamp it in `dispatchTo` next to `mode` and `revise`, and
   have the salience seam hand it to `evaluate_salience` as the bid's alias,
   keeping `fast` as the default and for an empty value from an older
   orchestrator. Decide at the same time whether the reflexion critic follows it
   or stays on `fast`: RFC 0051 describes the critic as the cheap judgement, so
   keeping it on `fast` is defensible, but the docs must then say which call the
   setting governs. The proto change must use the CI-pinned toolchain
   (`make proto-check`). Once this lands, the warning becomes true.
2. **Stop promising it.** Either reject `model: quality` at validate as "not yet
   deployed", the way `depth: deep` is rejected, or keep accepting it and change
   the comment, the Go docs, the schema, the guide and the warning to say the
   value is recorded and reported but not yet used. Before rejecting, check how
   the boot replay (`ChannelRouter.ResolveFromStore`) treats a stored runtime
   override that no longer validates: a channel whose operator already saved
   `quality` must not fail to load.

## Slot

Not slotted. No release plan is open: ruling (a) of the
[sequencing Amendment 2026-09-12](../v0.3.x-sequencing.md#amendment-2026-09-12--close-v0316-small-then-measure-before-any-train-opens)
opens none before EXP-001 reports and the first strategy review logs its result.
The [EXP-001](../experiments/EXP-001-preregistration.md) harness sets each
channel's reasoning mode in [`panel.yaml`](../../evaluators/experiments/EXP-001/panel.yaml)
(`bid`, and `off` for arm B) but never `model`, and it points the `quality`,
`fast` and `summarizer` aliases at one model. So neither fix changes what a
frozen arm runs.

## Notes

> 2026-09-24 — filed from the review of
> [#989](https://github.com/mkhomutov/Persatrix/pull/989) as finding F-2,
> outside that PR's comments-only scope. [#990](https://github.com/mkhomutov/Persatrix/pull/990)
> noticed the same gap while checking what fanout sends and left it for an
> issue. Confirmed by reading the dispatch, the proto and every agent call site
> that resolves a model; no code was changed.
>
> 2026-09-24 — corrected the Slot paragraph, which said the EXP-001 harness
> sets no `reasoning` block. Its panel sets a reasoning mode per arm; it never
> sets `model`, so the conclusion stands.
