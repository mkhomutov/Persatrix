# Autonomous Channels — User Guide

This guide shows how to run a channel discussion that no one has to start or
keep going: arming a channel, convening it by hand or on a schedule, tuning
the roster, and a free offline demo. It used to be §13 of the
[channels guide](channels.md), which still covers the basics it builds on:
declaring a channel, the
[response gate](channels.md#4-the-response-gate-who-replies-and-when), and
[`channel config`](channels.md#editing-governance-config-at-runtime--channel-config-rfc-0050-phase-1).

> **Spec-level detail** lives in [RFC 0052](../rfcs/0052-autonomous-agent-channels.md)
> and its [PR plan](../rfcs/0052-pr-plan.md). The persona side — why an
> unattended roster keeps talking instead of going quiet — is in the
> [persona agents guide](persona-agents.md#autonomous-channels--the-anti-collapse-cadence-v0311).

---

## Arming and convening

An **autonomous channel** runs a discussion with **no human in the loop**: no
human seeds the topic, no human keeps it alive. It is an ordinary group channel
carrying an `autonomous` block (configured exactly like the governance knobs in
[§4](channels.md#4-the-response-gate-who-replies-and-when) — YAML, `channel config`, or the
web panel) plus one operator action — **convene** — that opens the discussion.

```yaml
# config/channels.yaml — an armed channel
channels:
  - name: roundtable
    interaction_budget_tokens: 200000   # MANDATORY — validate rejects uncapped autonomy
    escalation_chair_id: ember-owl      # MANDATORY — the chair writes the closing synthesis
    autonomous:
      enabled: true
      topic: "Should we adopt a monorepo? Lay out the tradeoffs."
      agenda: ["Build tooling cost", "Cross-team coupling", "Migration effort"]
      convener: nova-sparrow          # authors the opening turn; a DISTINCT role from
                                      # escalation_chair_id (RFC 0052 OQ #1)
      goal: "A synthesized recommendation with the strongest argument on each side."
    members:
      - {id: nova-sparrow, respond: participant}
      - {id: ember-owl, respond: participant}
      - {id: iron-fox, respond: participant}
```

**The safety contract (enforced at config-validation).** An unattended channel
has no human circuit-breaker, so an `autonomous.enabled` channel is
**un-creatable** without a positive resolved `interaction_budget_tokens` cap and
an `escalation_chair_id` to write the closing synthesis; arming is **group-only**
(a DM/thread cannot be made autonomous); the `convener` must be a declared,
floor-capable member (not an `observer`) distinct from `escalation_chair_id`; and
a [standing channel](#standing-channels--convening-on-a-schedule) must also
declare an aggregate bound.

**Convening.** Convening = the convener authors the **opening turn** under a
fresh interaction, with no human message; from that publish the ordinary
[response gate](channels.md#4-the-response-gate-who-replies-and-when) + `InboundEventWake`
chain carries the discussion. Under the hood the orchestrator dispatches a
directed **convene forced turn** to the convener (the sibling of the chair-stall
escalation — same directed-lane admission, so the opener is never silenced by the
bias-to-silence salience bid). The operator-supplied `topic`/`agenda`/`goal` are
wrapped in the RFC 0009 `<external_data>` envelope before they reach the
convener's prompt — operator config is a distinct trust class, the one genuinely
new injection surface this opens. The opening turn resolves **uncapped** (the
wallet snapshots the per-interaction cap at the interaction's first commit, so
the lease that *produces* the opener predates its own snapshot); the always-on
RFC 0030 Layer-0 depth cap bounds that first call.

Convene is reachable on all three RFC 0050 surfaces, each gated behind the **same**
`config_edit_enabled` toggle as the config surface. Be aware of what that toggle
actually is: the bundled `config/ui.yaml` ships it **`true`** (and it is loaded
even without `--enable-ui`), so in a default deployment convene is reachable as
soon as a channel is armed — it is **not** a dark, dedicated convene opt-in.
Because convene shares the config-edit gate, the same `config_edit_enabled: false`
that lands the config surface dark also disables convene; the deliberate human
steps that gate an unattended discussion are *arming* the channel (a config edit)
and pressing convene. Convening does trigger real LLM spend on an unattended
channel, so treat enabling the operator surface as also enabling convene:

```bash
# CLI — POST /api/v1/channels/{id}/convene
persatrix channel convene group:planning
persatrix channel convene planning --json     # {channel_id, convener, status}
```

```text
# REST
POST /api/v1/channels/{id}/convene      → 202 {channel_id, convener, status:"convening"}
                                          403 toggle off · 404 no such channel
                                          409 not autonomous.enabled · 409 already has a
                                              live interaction · 409 no open-floor responder
                                              besides the convener · 409 no topic/agenda/goal
                                              to convene on
                                          400 convener or chair drifted out of the roster
                                          429 max_convenings or standing_budget_tokens reached
                                          503 convener not reachable right now (retryable)
```

> **The audience must answer an *open-floor* opener.** The convener's opening
> turn addresses the room as a whole (it names no one), and only `participant`
> (`always`) members reply to an open-floor message — a `when_mentioned` member
> stays silent until @-mentioned. Note an unspecified member defaults to
> `when_mentioned`, so give the intended discussants `respond: always` (the
> `participant` disposition), or convene 409s with *no open-floor responder
> besides the convener*.

Convening targets an **idle** channel: a channel that already has a live
interaction is refused (`409`) rather than silently joined — the convener opens
one discussion, not a second one over a running one (a timer-fired convene on a
[standing channel](#standing-channels--convening-on-a-schedule) gets the same
`409`). Note the convene ack is `202 Accepted` —
"the convener was woken", not "the discussion ran". Repeated convening is bounded
by the §E aggregate ceiling: once a channel has been convened `autonomous.max_convenings`
times, a further convene is refused with `429 Too Many Requests` (the count is
process-lifetime — a restart resets it — and cleared when the channel is deleted).
A channel with `max_convenings` unset (`0`) is not count-bounded, but a positive
`standing_budget_tokens` bounds it by *cost* instead: each interaction close folds
its settled discussion spend into a per-channel running total, and once that total
reaches `standing_budget_tokens` a further convene is likewise refused with `429`
— the aggregate-*spend* twin of the count ceiling, process-lifetime and
delete-cleared in the same way (the async per-persona close summaries settle after
the close, so the folded total tracks the discussion spend; the co-declared count
bound caps how far it can overrun). A
[standing channel](#standing-channels--convening-on-a-schedule) must declare at
least one of these two ceilings, and a convene fired by its timer meets them
exactly like a manual one.

How much of the *count* allowance is spent is visible on the config **read**
surface: `GET …/config` carries an `autonomous_runtime` block —
`convening_count` (openers dispatched this process lifetime) and
`convenings_remaining` (the `max_convenings` allowance left, or `null` when
unbounded; clamped at zero if a lowered bound sits below the spent count). The web
*Autonomous channel* panel renders it as a **Convenings: _N_ used, _M_ remaining**
line, and `persatrix channel config get` prints a trailing `convenings … (runtime)`
row. It is read-only observability — the count itself is enforced by the `429`
ceiling above. Nothing reports the `standing_budget_tokens` running total yet, so
a channel bounded only by spend reaches its `429` with no warning.

- **Web console** — a **Convene** button in the Channel-settings panel's
  *Autonomous channel* section, shown only when the channel is armed per the
  *saved* config and disabled while there are unsaved edits (convening reads the
  persisted block, so save first). See the
  [web console channel settings guide](web-console-channel-settings.md).

### Standing channels — convening on a schedule

A **standing** channel convenes itself on a timer instead of waiting for an
operator. Arming one takes two steps.

**1. On the channel** (armed as above), set `autonomous.schedule_interval_seconds`
to the timer's interval in seconds, plus an aggregate bound: `max_convenings`,
`standing_budget_tokens`, or both. The per-interaction cap limits each discussion,
not how many the timer starts, so a positive interval with no bound is refused:
`channel config set` answers `400`, and in `config/channels.yaml` the file fails
to load at boot, which leaves every channel endpoint answering `503`. `0`, the
default, means no schedule and needs no bound. The web panel does not edit these
knobs.

```bash
# one discussion a day, at most 30 in all
persatrix channel config set planning \
  autonomous.schedule_interval_seconds=86400 autonomous.max_convenings=30
```

**2. On the convener**, add the timer to its entry in `config/agents.yaml`, then
restart that persona. The channel setting does not start anything, and nothing
writes this timer for you yet.

```yaml
# config/agents.yaml — the convener's entry
agents:
  - id: nova-sparrow
    autonomy:
      level: semi-autonomous        # or autonomous; other levels run no timers
      timers:
        - id: convene-planning      # "convene-" + the channel name
          interval_seconds: 86400   # keep equal to schedule_interval_seconds
          kind: convene
        # Only if it already ran semi-autonomous or autonomous with no timers
        # block, keep its ordinary heartbeat (at its tick_interval_seconds):
        # - {id: legacy_tick, interval_seconds: 60, kind: tick}
```

The timer's `interval_seconds` is the schedule that actually runs; the channel
setting only makes `validate` demand a bound, so keep the two equal. Adding a
`timers` block switches off the implicit heartbeat, hence the commented `tick`
line ([MT-AUTONOMOUS-003](../manual-tests/MT-AUTONOMOUS-003.md) Step 2 explains
both rules). Do step 1 first: nothing checks a convene timer against its
channel's bound, so a timer aimed at a channel without one keeps opening
discussions, each capped but with no limit on how many.

Each fire calls the same convene endpoint as the CLI and meets the same
refusals: a fire during a running discussion gets `409` and is skipped, and once
the bound is spent every fire gets `429` and nothing opens. A new timer first
fires one full interval after the persona starts. Every fire opens on the
channel's current `topic`, `agenda` and `goal`, so to vary them per fire (see
the tuning notes below), edit the channel between fires. Both bounds reset when
the orchestrator restarts, but the timer keeps firing, so a restart **resumes** a
channel that had stopped at its bound. To stop a standing channel for good,
disarm it (`autonomous.enabled=false`) and remove its timer the same way you
added it.

### Tuning an autonomous roster — the ISSUE-0109 calibration

What the v0.3.11 live soak (7 arcs, single- and four-vendor rosters —
[ISSUE-0109](../issues/ISSUE-0109-rfc0052-autonomous-defaults-calibration.md))
taught about the knobs:

- **The cascade-depth cap is the de facto length knob** on a productive
  roster: the productive-round continuation advances the round tally and the
  reply's cascade depth *together*, so a `max_rounds` above the depth cap (5)
  can never fire on that chain — every productive soak arc closed on the depth
  bound. `max_rounds` (default now **8**, down from 12) is the net for
  *stall-driven* arcs, where convener cadence turns reset depth. The knob is
  **per-channel** since v0.3.13
  ([ISSUE-0114](../issues/ISSUE-0114-per-channel-cascade-depth-override.md)):
  to shorten one channel's discussions (a cheap triage room), set that
  channel's `max_cascade_depth` below the fleet cap — in `channels.yaml` or
  live via the RFC 0050 config surface; to lengthen discussions *past the
  fleet default*, raise the top-level `max_cascade_depth` (keeping it aligned
  with the Python dispatcher's equal pin, `agents/dispatch.py`) and then
  per-channel caps up to it.
- **Co-tune `end_vote_threshold` with discussion length**: at the K=2 default,
  2 votes closed a 3-seat roster in 4 of 7 arcs — sometimes as ~20 s
  confirmation stubs — and an end-vote close arms **no** chair synthesis. The
  shipped autonomous templates now pin K = the full roster, so convergence
  routes to the artifact-bearing bounded close unless the roster unanimously
  ends.
- **Standing channels need per-convening topic freshness**: re-convening an
  unchanged topic yields push-back ("we already worked through this") and a
  redundant synthesis, or record-confirmation stubs. Vary the topic/agenda per
  fire (a date, a delta, the previous synthesis as input).
- **Naming the personas in the topic is the fresh-store engagement lever** —
  collapse-proneness inversely tracks accumulated channel memory, so a fresh
  store needs the pointed opener a warmed store does not.
- **Size caps from telemetry, not logs**: live arcs spent 0.24–0.59 of the
  200k cap. Those arcs predate v0.3.15 and cleared the reserve of the day;
  **re-read them against the table below before reusing them**, because the
  v0.3.15 re-size lowered the threshold they were measured against. The
  `channel.conversation.interaction_cap_utilization{channel_type,trigger}`
  histogram records spend-at-close ÷ cap on every capped close — read it to
  right-size `interaction_budget_tokens`, and multiply a typical utilization
  by the cap to size `standing_budget_tokens` per expected convening.
- **The close reserve grew, so budget for a smaller discussion.** Since v0.3.15
  the reserve funds one closing summary per `(principal, speaker)` record rather
  than one per persona, and that record count grows with the **cube** of the
  room. The speaker axis is the members **plus two**: the orchestrator's convene
  and synthesis directives ride synthetic senders that hold no seat and key
  close records of their own. The reserve is held back from the cap, so what the
  discussion may spend before the bounded close fires — the *soft* threshold —
  falls as the roster grows. At a 200 000 cap:

  | seats | close records `R` | reserve | discussion budget | clamped? |
  |---|---|---|---|---|
  | 3 | 20 | 73 500 | 126 500 (63%) | no |
  | 4 | 36 | 100 000 | 100 000 (50%) | **yes** |
  | 5+ | 63+ | 100 000 | 100 000 (50%) | **yes** |

  **Check the 4-seat row first.** It is the shape the bundled
  `blueprints/autonomous-multivendor` roster ships, at the cap that blueprint
  ships — and it clamps, so its discussion budget fell from 182 500 to 100 000
  and the counter below fires on every close. An arc at the top of the
  historical 0.24–0.59 band (≈118 000 tokens) now crosses that threshold and
  closes on `cost` where it used to run on. Clearing the clamp on four seats
  needs `interaction_budget_tokens` ≥ **259 000**.

  The 3-seat row (the bundled `blueprints/autonomous-roundtable`) is the quiet
  one: it is **not** clamped, so nothing warns — but its discussion budget still
  fell from 186 000 to 126 500. That is the row to re-read the historical band
  against by hand.
- **Watch for a clamped close reserve.** When a cap cannot fund the sizing
  above, the reserve is capped at half the ceiling — the discussion keeps a
  working budget and the close fires earlier, but the tail of the close is
  under-funded and its late summaries land as `[interaction summary
  unavailable]`, which nothing retries.
  `channel.conversation.synthesis_reserve_clamped{channel_type, trigger}` fires
  once per bounded close that actually fired against a clamped reserve; a
  non-zero rate means raise `interaction_budget_tokens` or shrink the room. A
  warning naming the room size, the record count, the cap and the reserve
  accompanies it **once per channel per configuration**, not once per close —
  the clamp is a property of the room and the cap, so the counter is the
  per-close surface and the log line is the explanation. Neither says anything
  about the unclamped rows above, which is why they get their own table. On a
  fleet with no wallet (no cost config) nothing is reported at all: no
  close-path lease is drawn, so none can be denied.

### Try it offline — `make demo-autonomous`

To watch the whole arc with **no API key and zero spend**, run the offline demo:

```bash
make demo-autonomous
```

It boots the society on the `mock` provider (the RFC 0033 offline alias →
`provider: mock`, priced at $0), **arms** the bundled `roundtable` channel
(which ships *disarmed* for safety — see the `config/channels.yaml` template),
and **convenes** it: `nova-sparrow` opens the "Should we adopt a monorepo?"
topic, `ember-owl` and `iron-fox` discuss it through the governed wake chain,
and the chair `ember-owl` closes with a synthesized recommendation — **no human
types anything**. Watch it live in the web console (`http://localhost:8080/ui`
→ *Channels → roundtable*), or read the closing synthesis + each persona's
RFC 0020 summary with `persatrix agent interactions ember-owl` once it closes;
the channel is left re-convenable, so the web **Convene** button re-runs it.
Stop with `make docker-down`.

The convener/chair/participant turns come from the curated
`config/offline_responses.yaml` fixtures, so the offline discussion is
deterministic — it demonstrates the *shape* of a human-free brainstorm, not a
live model's reasoning. Swap `provider: mock` for a keyed vendor (`make
demo-anthropic`, `demo-openai`, `demo-gemini` or `demo-watsonx`; the four-vendor
roster in
[`blueprints/autonomous-multivendor`](../../blueprints/autonomous-multivendor/blueprint.yaml)
is assembled by hand, as in
[MT-AUTONOMOUS-MULTIPROVIDER-001](../manual-tests/MT-AUTONOMOUS-MULTIPROVIDER-001.md))
and convene the same channel for a real run. The deterministic pin that
the offline face yields a non-empty, on-topic synthesis at $0 is
[`tests/integration/test_autonomous_offline_smoke.py`](../../tests/integration/test_autonomous_offline_smoke.py);
the live acceptance is [MT-AUTONOMOUS-001](../manual-tests/MT-AUTONOMOUS-001.md).

> **Scope in v0.3.11.** PR 3 ships convening + the opening turn. PR 4 adds the
> mechanisms that make the discussion *bounded and artifact-bearing*: a
> deterministic **bounded close** terminates the interaction when it crosses
> `autonomous.max_rounds` or the wallet's soft budget (the cap minus the
> synthesis reserve), and — on a chaired channel — first asks the
> `escalation_chair_id` for a goal-directed **closing synthesis** against
> `autonomous.goal`: the chair's reply is delivered to every member as the
> discussion's final message, each member's RFC 0020 interaction summary is
> produced (and, on the autonomous close, metered against the cost cap), and
> the channel is left re-convenable. A chair that never replies falls back to
> an immediate close after a timeout, so termination never waits on a model.
> PR 6 adds the anti-collapse cadence and PR 7 standing convening
> ([above](#standing-channels--convening-on-a-schedule)); all of them shipped in
> v0.3.11 (see the [PR plan](../rfcs/0052-pr-plan.md)).

---

## Related documentation

- [RFC 0052](../rfcs/0052-autonomous-agent-channels.md) — the spec, and its
  [PR plan](../rfcs/0052-pr-plan.md) for what each PR shipped
- [Channels guide](channels.md) — the channel basics this guide builds on
- [Persona agents guide § Autonomous channels](persona-agents.md#autonomous-channels--the-anti-collapse-cadence-v0311)
  — the persona side: the anti-collapse cadence
- [Web console channel settings](web-console-channel-settings.md) — the
  *Autonomous channel* section and its **Convene** button
- Manual tests: [MT-AUTONOMOUS-001](../manual-tests/MT-AUTONOMOUS-001.md)
  (convene to synthesis), [MT-AUTONOMOUS-002](../manual-tests/MT-AUTONOMOUS-002.md)
  (the anti-collapse cadence), [MT-AUTONOMOUS-003](../manual-tests/MT-AUTONOMOUS-003.md)
  (a standing channel) and
  [MT-AUTONOMOUS-MULTIPROVIDER-001](../manual-tests/MT-AUTONOMOUS-MULTIPROVIDER-001.md)
  (a four-vendor roster)
- [ISSUE-0109](../issues/ISSUE-0109-rfc0052-autonomous-defaults-calibration.md)
  — the live soak behind the tuning notes
