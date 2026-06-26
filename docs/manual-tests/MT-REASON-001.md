# Manual Test MT-REASON-001: Reasoning before posting — semantic silence with a reason, a plan-threaded considered post, and the walled private trace

**Test ID**: `MT-REASON-001`
**Feature Area**: Channels (conversation governance — RFC 0051 reasoning before posting, on top of RFC 0030 Tier B / RFC 0050 config / RFC 0034 working memory)
**Version**: 1.0
**Created**: 2026-06-25
**Last Updated**: 2026-06-25
**Status**: Active — authored at RFC 0051 PR 9 closeout; **live execution is a v0.3.10 release-prep (Phase 3) deliverable** (run against a real provider per [v0.3.10-plan §Phase 3](../v0.3.10-plan.md#phase-3--v0310-release-prep-execution)).

---

## Overview

**Purpose**: Verify the v0.3.10 user-facing promise — **personas think before they speak**. On an open-floor turn a persona privately decides *whether* the turn is worth a post; when it would only be agreeing, restating, or piling on it **stays silent with a stated reason** instead of posting (semantic silence, on by default once a channel is governed), and when it does post under `mode: plan` it composes under a **private plan** (intent / key points / addressed-to / avoid-restating) so the post reads as *considered* rather than reflexive. The most context-revealing artifacts a persona produces — the plan, the silence `reason_note`, and (under reflexion) the discarded draft + critic note — are **walled**: never a channel message, never persisted, never visible to another persona ([RFC 0051 §E](../rfcs/0051-reasoning-before-posting.md#e-privacy-boundary--the-trace-is-walled)).

**Scope**: the default `planning` group channel (three personas — `ember-owl` `addressed`, `iron-fox` and `nova-sparrow` `participant`), the `reasoning` knob on the RFC 0050 config surface (`persatrix channel config` / web `ChannelSettings`), the operator-debug **agent log** (where the verbatim silence `reason_note` egresses per §E), the channel-timeline web panel + the REST recall cross-check (`GET /api/v1/channels/{id}/messages?as_participant=…`), and the go-live telemetry (`deliberation.*`, `reflexion.*`).

**Out of Scope** — explicitly deferred, **not asserted** here:

- **Native extended-thinking `reasoning.depth: deep`** (RFC 0051 Phase 4) — `validate` rejects it as unbacked; covered only as the rejection edge case below.
- **An operator reasoning-reveal web panel** (OQ 6(a), PR 7) — **cut from v0.3.10**. The silence-with-reason leg is read from the operator-debug **agent log**, not a web reveal, so this MT does not depend on PR 7.
- **An end-user "watch them think" surface** (OQ 6(b)) — the §E wall is held inviolate; there is no end-user egress to assert.

---

## Related Documentation

- [RFC 0051 — Reasoning Before Posting](../rfcs/0051-reasoning-before-posting.md) — §C verdict/plan types, §D mechanism, §E privacy wall, §F idle/cost, §G the `reasoning` knob.
- [RFC 0051 PR plan](../rfcs/0051-pr-plan.md) — the 9-PR breakdown; this MT is the PR 9 acceptance artifact.
- [v0.3.10-plan §Acceptance](../v0.3.10-plan.md#acceptance-for-v0310) — the release gate this MT anchors.
- [MT-CHANNEL-RELEVANCE-001](MT-CHANNEL-RELEVANCE-001.md) — the Tier A directedness probe this builds on (the same channel + web-console surface).
- [MT-CHANNEL-CONFIG-001](MT-CHANNEL-CONFIG-001.md) — the live-edit config-knob mechanics the `reasoning.*` legs reuse.
- [persona-agents guide §Reasoning before posting](../guides/persona-agents.md) — the operator-facing description of the knob.

---

## Preconditions

1. The compose stack is up against a **real provider** (`make demo-anthropic` / `make demo-openai` / equivalent — *not* the offline echo provider, which cannot produce a meaningful silence verdict or plan). Per [persona-agents guide §Starting the stack](../guides/persona-agents.md).
2. A **clean store** (`make reset`) so reply counts and recall are unambiguous.
3. The default `planning` channel exists with the three personas above and **at least one `participant`/`chair` member**, so the channel is *governed* — the precondition for the `off → bid` default flip.
4. Operator access to the **agent container logs** (the debug agent log — `docker compose logs -f agent` or the configured log sink), where the verbatim `reason_note` egresses (§E). The count-only `agent.deliberated` audit and the `deliberation.*` metrics are also observable.
5. `persatrix` CLI on `PATH` and pointed at the running orchestrator (as in MT-CHANNEL-CONFIG-001).

---

## Test Procedure

### Step 1: The governed default is `bid` — semantic silence is on out of the box

Read the effective config and confirm the governed channel resolved to `reasoning.mode: bid` with no operator edit (the PR 6 `off → bid` flip):

```bash
persatrix channel config get group:planning
# Expect: reasoning.mode = bid   (provenance: governed-default, not an explicit override)
#         reasoning.model = fast, reasoning.depth = shallow, reasoning.revise = 0
```

**Pass**: a governed channel shows `reasoning.mode: bid` by default; an **ungoverned** channel (no `participant`/`chair` member) shows `off` and rejects a non-`off` mode (the knob is inert without the gate).

### Step 2: Silence with a stated reason — the no-pile-on half

Drive an open-floor turn that is **already answered** (or addressed elsewhere) so a `participant` persona has nothing to add. For example, after one persona has clearly resolved the question, post an open-floor "Sounds right." that invites only agreement.

- **Observe the channel**: the un-addressed `participant` persona that would only be agreeing posts **no message** for this turn (compare per-sender reply counts against MT-CHANNEL-RELEVANCE-001's open-floor baseline — the pile-on turn is suppressed).
- **Observe the reason** in the operator-debug **agent log**: that persona's turn ends in `DO_NOTHING` carrying a deliberation `reason_code` (e.g. `only_agreeing` / `already_answered` / `nothing_to_add`) and a one-clause `reason_note`. The `agent.deliberated` audit records the decision + `reason_code` **count-only** (never the verbatim `reason_note`).

**Pass**: the persona stays silent *with a stated reason* readable in the agent log; the channel transcript shows the suppressed turn never produced a message; `deliberation.suppressed{reason_code=…,mode=bid}` increments.

### Step 3: A plan-threaded considered post — the considered half (opt-in `mode: plan`)

Promote the channel one rung (a deliberate operator opt-in), then drive a turn the persona *can* add to:

```bash
persatrix channel config set group:planning reasoning.mode=plan
```

Drive an open-floor turn that invites a substantive contribution (e.g. "What datastore should we pick for the cache, and why?").

- The persona **posts** a contribution.
- **No-leak (load-bearing)** — the private plan threaded the compose but never escaped:
  - It is **absent from the channel transcript** (web timeline + `GET /api/v1/channels/group:planning/messages`): no `intent` / `key_points` / `avoid_restating` text appears in any published message.
  - It is **absent from a second persona's recall**: `GET /api/v1/channels/group:planning/messages?as_participant=iron-fox` (an RFC 0034/0036 reconstruction) contains no plan text.

**Pass**: a plan-shaped post ships; the private plan appears in **zero** published messages and **zero** of a peer's reconstructed messages. (The structural wall is also pinned in CI by `tests/integration/test_deliberation_no_leak.py`.)

### Step 4: Reflexion opt-in — a weak draft is sharpened before it ships (default off)

Opt the channel up one further rung (meaningful only under `mode: plan`):

```bash
persatrix channel config set group:planning reasoning.revise=1
```

Drive a turn whose first-pass draft is weak enough for the critic to flag. Only the **revised** message is published; the **discarded draft** and the **critic note** never appear in the transcript or a peer's recall. `reflexion.runs{outcome=revised}` and `reflexion.rounds` increment; a strong draft instead charts `reflexion.runs{outcome=noop}` with no rounds, and a fail-soft hiccup (a starved critic lease / provider error) charts `reflexion.runs{outcome=degraded}` — kept separable from `noop` so a silent outage that disables the loop stays alertable.

**Pass**: the published message is the revised text; the discarded draft + critic note never surface; the loop is fail-soft (a starved lease keeps the last good draft — the post is never blocked — and charts `outcome=degraded`, not `noop`). Reset with `persatrix channel config set group:planning reasoning.revise=0`.

### Step 5: The kill switch is one flip

```bash
persatrix channel config set group:planning reasoning.mode=off
```

Re-drive the Step 2 turn. The channel reverts to **byte-for-byte the prior RFC 0030 scalar score gate** at runtime (no restart): the per-member `threshold` governs again, no `deliberation.*` telemetry emits, and an explicit `off` is preserved across a restart (re-confirm with `persatrix channel config get` after a stack bounce, as in MT-CHANNEL-CONFIG-001 Step 4).

**Pass**: `off` restores the scalar gate at runtime; no deliberation/reflexion telemetry on the `off` rung; the explicit `off` survives boot.

---

## Expected Results Summary

| # | Check | Expected |
|---|-------|----------|
| 1 | Governed default | `reasoning.mode: bid` by default on a governed channel; `off` + non-off rejected on an ungoverned one |
| 2 | Semantic silence | a would-be-pile-on `participant` posts nothing; the agent log carries the `reason_code` + `reason_note`; `deliberation.suppressed{reason_code,mode}` fires |
| 3 | Plan-threaded post | a plan-shaped post ships; the private plan is in **zero** messages and **zero** of a peer's `?as_participant` recall |
| 4 | Reflexion (opt-in) | only the revised message ships; discarded draft + critic note never surface; `reflexion.runs` (`revised`/`noop`/`degraded`) + `reflexion.rounds` chart the loop; fail-soft never blocks the post (and charts `degraded`, not `noop`) |
| 5 | Kill switch | `mode: off` restores the scalar score gate at runtime, emits no `deliberation.*`, and survives boot |

---

## Telemetry to capture (release-prep deliverable)

- A **countable pile-on-suppression delta**: a 3-persona `mode: plan` brainstorm shows fewer pile-on turns than the `off` baseline, charted by `deliberation.suppressed / deliberation.total` (the acceptance metric in [v0.3.10-plan §Acceptance](../v0.3.10-plan.md#acceptance-for-v0310)).
- The `deliberation.duration` latency histogram, `deliberation.budget_starved`, and the parse-failure safety-net counter emit on their respective paths.

---

## Edge Cases & Error Scenarios

### Edge Case 1: `reasoning.depth: deep` is rejected at validate (capability gate)

```bash
persatrix channel config set group:planning reasoning.depth=deep
# Expect: rejected (400) — Phase 4 native extended-thinking is not deployed; no silent downgrade.
```

### Edge Case 2: `reasoning.revise≥1` without `mode: plan` is rejected

```bash
persatrix channel config set group:planning reasoning.mode=bid reasoning.revise=1
# Expect: rejected (400) — the critic checks the draft against the plan, so revise requires mode: plan.
```

### Edge Case 3: a non-`off` mode on an ungoverned channel is rejected

Setting `reasoning.mode: bid|plan` on a channel with no `participant`/`chair` member is rejected — the knob does not by itself arm the gate.

---

## Test Results

**✅ Executed live (Anthropic) — Pass.** v0.3.10 release-prep PR 1, 2026-06-26 — full record in [v0.3.10-execution-report.md](v0.3.10-execution-report.md#mt-reason-001--reasoning-before-posting-live-anthropic).

- **Step 1** (governed default `bid`): ✅ — after fixing **F-1** (the demo `config/channels.yaml` had pinned `planning` to `mode: "off"` with a stale dark-phase comment; fixed to inherit the governed `bid` default in the same PR). A newly `salience_gated` runtime channel also resolved to `bid`; an ungoverned channel showed `off` and rejected a non-`off` mode.
- **Step 2** (semantic silence): ✅ at **reason_code** granularity — `deliberation.suppressed{reason_code=nothing_to_add, mode=bid}` = 8 of `deliberation.total{bid}` = 10 (**80% suppress-rate**), suppressed turns produced no message. **Caveat (F-2 / [ISSUE-0108](../issues/ISSUE-0108-reasoning-reason-note-no-operator-egress.md)):** the verbatim `reason_note` has **no operator egress** in v0.3.10 (the agent-log egress was deferred with the cut PR 7, and the `agent.deliberated` audit's `reason_code` is dropped by the structlog formatter) — the reason is read from the **suppression metric label**, not the agent log. The §E / Step 2 wording above should be corrected accordingly (tracked in ISSUE-0108).
- **Step 3** (plan-threaded post + no-leak): ✅ — considered single-item posts shipped; the private plan markers appear 0× in the transcript and 0× in both peers' `?as_participant` recall.
- **Step 4** (reflexion `revise=1`): ✅ — `reflexion.runs{outcome=revised}` = 2, `reflexion.rounds` = 2; discarded draft + critic note walled; fail-soft (`budget_starved`=0).
- **Step 5** (kill switch `off`): ✅ — `deliberation.*` flat under `off` (scalar `below_threshold` gate restored), explicit `off` preserved and survives a boot.
- **Edge cases**: ✅ — `depth: deep`, `revise≥1` without `mode: plan`, and `revise > 2` all rejected at validate (400 / client enum); a non-`off` mode on an ungoverned channel rejected.
- **Telemetry**: `deliberation.duration` histogram populated (~2.3 s `bid` / ~4.6 s `plan`), `budget_starved` = 0, `parse_failures` = 0.

---

## Notes

- The silence-with-reason leg reads the **agent log**, not a web reveal — the OQ 6(a) operator-reveal panel (PR 7) was cut from v0.3.10, and this MT is deliberately independent of it.
- The §E wall is the load-bearing privacy contract; its structural form is pinned in CI by `tests/integration/test_deliberation_no_leak.py` (plan + reflexion intermediates) so this live MT confirms the *operator-observable* behaviour rather than re-proving the structural invariant.
