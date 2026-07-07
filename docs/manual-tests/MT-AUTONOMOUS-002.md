# Manual Test MT-AUTONOMOUS-002: Anti-collapse cadence — a collapse-prone roster works a multi-item agenda under convener pressure

**Test ID**: `MT-AUTONOMOUS-002`
**Feature Area**: Channels (autonomous agent-only channels — RFC 0052 Phase 2 anti-collapse cadence, on top of the Phase-1 convene→bounded-close backbone; RFC 0030 chair-stall escalation; RFC 0051 semantic silence)
**Version**: 1.0
**Created**: 2026-07-07
**Last Updated**: 2026-07-07
**Status**: Active — authored at RFC 0052 PR 6; **live execution is a v0.3.11 release-prep (Phase 3) deliverable** (run against a real provider per [v0.3.11-plan §Acceptance](../v0.3.11-plan.md#acceptance-for-v0311)).

---

## Overview

**Purpose**: Verify the RFC 0052 [§C](../rfcs/0052-autonomous-agent-channels.md#c-the-central-tension--anti-collapse-cadence) design heart — the counter-pressure that keeps a **human-free** discussion alive **without** un-doing the v0.3.x realism arc. MT-AUTONOMOUS-001 proved a well-behaved roster converges and terminates; this MT proves the harder case: a roster that, left alone, would **collapse to silence** (every persona reasons "the others can cover this" and passes) is instead nudged **through a multi-item agenda** by the **convener**, one item at a time, and still terminates with the §D artifacts.

The three invariants under test:

1. **Advance, don't die.** On a stalled round with the agenda not exhausted, the **convener** (the `autonomous.convener` role — distinct from the chair, [OQ #1](../rfcs/0052-autonomous-agent-channels.md#open-questions)) authors a forced turn that **advances to the next agenda item** (or re-poses an under-discussed one), rather than the discussion converging to an empty transcript.
2. **Never twice into silence on the same item** — the CE5 loop guard, generalized per agenda item. Each item earns **at most one re-invite** and the agenda cursor is **monotonic**: an item is never re-posed once advanced past, so total convener turns are **linear in agenda length** — one introduction plus at most one re-invite per item (no open loop).
3. **Silence stays semantic; the human path is untouched.** The convener cadence does **not** lower the RFC 0051 silence threshold globally — it gives the convener something concrete to ask. It is scoped to `autonomous.enabled`; an ordinary channel keeps the shipped CE5 one-shot ration and bias-to-silence **byte-for-byte**.

**Scope**: a group channel armed exactly as in MT-AUTONOMOUS-001 (mandatory cap + convener + chair) but with a **multi-item agenda** and a roster/topic chosen to *provoke* early quiet (a broad, low-controversy topic where personas readily pass); the convener forced turns on the timeline; the `channel.conversation.convener_advance{outcome ∈ advance|reinvite|dispatch_error}` telemetry; and the deterministic bounded close that still fires at the end.

**Out of Scope** — deferred, **not asserted** here:

- **The Phase-1 convene→converge→terminate→synthesize backbone** and the `1 + N` reserve — [MT-AUTONOMOUS-001](MT-AUTONOMOUS-001.md).
- **Standing / scheduled convening + the aggregate bound** (Phase 3 / PR 7) — [MT-AUTONOMOUS-003].
- **A configurable liveness target.** The `min_substantive_turns_per_agenda_item` target ships at its default of **one** substantive turn per item (the value the silent-roster case collapses to anyway); promoting it to an operator-editable `autonomous.*` knob is a follow-up and is not exercised here.

---

## Related Documentation

- [RFC 0052 §C — the central tension: anti-collapse cadence](../rfcs/0052-autonomous-agent-channels.md#c-the-central-tension--anti-collapse-cadence).
- [RFC 0052 PR plan §PR 6](../rfcs/0052-pr-plan.md) — this MT is the PR 6 (Phase 2) acceptance artifact.
- [Persona-agents guide §Autonomous channels](../guides/persona-agents.md#autonomous-channels--the-anti-collapse-cadence-v0311) — the persona-side view (semantic silence still applies; the convener's agenda role).
- [MT-AUTONOMOUS-001](MT-AUTONOMOUS-001.md) — the Phase-1 backbone this MT layers the cadence onto.

**Related Automated Tests** — the deterministic CI backbone of this MT (no provider needed; the ration mechanism is provider-agnostic):

- [`internal/channels/convener_cadence_test.go`](../../internal/channels/convener_cadence_test.go) — the per-agenda-item ration state machine and the fanout-tail precedence: a fully-silent (collapse) roster is walked through a 2-item agenda (re-invite item 0 → advance to item 1 → re-invite item 1 → agenda exhausted → the chair escalation fires exactly once); the monotonic-cursor loop guard (`TestClaimConvenerCadence_MonotonicCursorNeverRepeats`); the convene-lane reuse (the turn carries the `convene` marker, the synthetic sender, and the open interaction id); and the **human-channel regression** (`TestConvenerCadence_HumanChannelUnchanged` — a stalled human round dispatches no convener turn and the shipped chair escalation fires unchanged).

This live MT confirms the *operator-observable* behaviour on a real provider; the ration invariants and the human-path regression are pinned in CI.

---

## Preconditions

Identical to [MT-AUTONOMOUS-001 §Preconditions](MT-AUTONOMOUS-001.md#preconditions) — a real-provider compose stack, a clean store, the three demo personas as `respond: always` members, the CLI on `PATH`, and log/metrics access. The one difference is the arming (Step 1), which uses a **multi-item agenda** and a deliberately **collapse-prone** topic.

---

## Test Procedure

### Step 1: Arm with a multi-item agenda and a collapse-prone topic

```bash
persatrix channel config set group:planning \
  autonomous.enabled=true \
  autonomous.topic="A quick retro on last sprint — anything worth noting?" \
  autonomous.agenda='What went well,What to improve,One experiment for next sprint' \
  autonomous.convener=nova-sparrow \
  autonomous.goal="A short synthesized retro with one concrete experiment." \
  autonomous.max_rounds=12 \
  interaction_budget_tokens=200000 \
  escalation_chair_id=iron-fox
```

The topic is broad and low-controversy on purpose: it is the shape where personas most readily pass ("nothing to add") and a human-free channel would otherwise die on item 1.

**Pass**: the block round-trips (`persatrix channel config get group:planning`); the three-item agenda is present.

### Step 2: Convene and watch the convener work the agenda

```bash
persatrix channel convene group:planning --json    # 202, convener nova-sparrow
```

Watch the timeline (web console, or `GET /api/v1/channels/group:planning/messages`) **and** the orchestrator log (`docker compose logs -f orchestrator | grep -E 'convener advanced|convener_advance|chair'`):

- The convener opens on the topic + **first** agenda item.
- Whenever a round goes quiet with the agenda not exhausted, the orchestrator logs `channels: convener advanced the agenda on a stall` (with `agenda_item` and `action=advance|reinvite`) and a **fresh convener turn** appears posing the next item (or re-posing the current one) — **not** an early chair "let's wrap up".
- Over the run the discussion visibly **moves through** *What went well → What to improve → One experiment*.

**Pass**: the convener authored **at least one** agenda-advancing turn; the transcript moved past item 1; no message came from a human.

### Step 3: The loop guard held — no item was flogged into silence twice

From the same log (`grep convener_advance`, or the `channel.conversation.convener_advance` metric):

- For any single agenda item, **at most one `reinvite`** fired against it, and the agenda cursor is **monotonic** — once the convener advances off an item it is never re-posed. (The `advance` turn poses the *next* item, not a second turn on the current one.)
- The total `convener_advance` count is **linear in the agenda length** — at most one `advance` per item transition (`len − 1`) plus at most one `reinvite` per item (`len`), i.e. **≤ `2·len − 1`** (here, 3 items ⇒ ≤ 5); the cadence never produced an unbounded stream of convener messages.

**Pass**: the per-item re-invite ceiling held; total cadence turns are linear in agenda length (bounded, no open loop).

### Step 4: The discussion still terminates with the §D artifacts

Exactly as [MT-AUTONOMOUS-001 §Step 3](MT-AUTONOMOUS-001.md#step-3-the-discussion-terminates-on-its-own-and-leaves-both-artifacts): once the agenda is exhausted (or `max_rounds` / the soft budget fires first), the bounded close runs — `interaction_closed{trigger=structural|cost}`, the chair's goal-directed synthesis as the final message, and a real RFC 0020 summary per persona.

**Pass**: the interaction closes without human help and leaves both artifacts; the channel is idle (re-convenable) afterwards.

### Step 5: The human path is unchanged (control)

On a **separate, non-autonomous** group channel (`autonomous.enabled` unset), run an ordinary multi-persona discussion until a round stalls.

**Pass**: **no** convener turn is dispatched (`convener_advance` stays at 0 for that channel); the shipped single chair-stall escalation fires exactly as before. Anti-collapse pressure never leaks onto a human channel.

---

## Expected Results Summary

| # | Check | Expected |
|---|-------|----------|
| 1 | Arm with agenda | multi-item agenda + collapse-prone topic round-trips |
| 2 | Convener advances | ≥1 `convener advanced the agenda` log / `convener_advance` increment; the transcript moves through the agenda; zero human turns |
| 3 | Loop guard | ≤1 `reinvite` per item (monotonic cursor); total `convener_advance` ≤ `2·len − 1`; no item re-posed twice into silence |
| 4 | Terminate + artifacts | bounded close fires; chair synthesis is the final message; every persona's summary is real |
| 5 | Human path (control) | a stalled human channel dispatches **no** convener turn; the shipped chair escalation is unchanged |

---

## Edge Cases & Error Scenarios

### Edge Case 1: single-topic (no agenda) autonomous channel

Arm with **no** `autonomous.agenda`. Convene and let a round stall.
**Expect**: no convener cadence (there is no agenda to advance) — the stall is handled by the shipped chair escalation exactly as on the Phase-1 backbone. The convener cadence is an *agenda* mechanism.

### Edge Case 2: a drifted / observer convener

Remove the convener from the roster (or set it `respond: never`) mid-run, then provoke a stall.
**Expect**: no convener turn (a drifted convener cannot author one) and **no ration is burned** — the stall falls through to the chair escalation and the discussion still terminates on the bounded close. (Convening a channel whose convener is already drifted is refused up front — the [MT-AUTONOMOUS-001 Edge Case 3](MT-AUTONOMOUS-001.md#edge-case-3-an-audience-that-cannot-answer-is-refused) family.)

### Edge Case 3: a genuinely silent roster (worst case)

If a roster passes on *every* item even after the convener re-invites it, each item yields zero substantive turns and advances after that one escalation — the liveness target and the ceiling **collapse to the same event** ([RFC §C 3](../rfcs/0052-autonomous-agent-channels.md#c-the-central-tension--anti-collapse-cadence)). The discussion still walks the agenda once and terminates with a (thin) synthesis. This is a **degraded pass**: the ceiling is the hard guarantee; the target only raises the odds an item gets discussed before it advances.

---

## Test Results

| Date | Tester | OS | Result | Notes |
|------|--------|----|--------|-------|
| — | — | — | ⬜ Pending | Live execution scheduled for v0.3.11 release-prep (master-plan Phase 3). Dry-run: the CI backbone (`internal/channels/convener_cadence_test.go`, incl. the human-channel regression) is green at PR 6. |

---

## Notes

- **The convener cadence reuses the §B convene lane** — the advance/re-invite forced turn carries the same `convene` wire marker, gate admission, and convener framing as the opening turn; it needs no new wire field or prompt. On the timeline it reads as the convener turning the room to the next item.
- **"One per agenda item" is the *escalation* bound, not the *turn* bound.** Introducing each new item is normal cadence; the rationed **re-invite** (the liveness second chance) is what is capped at one per item. Both are linear in agenda length, so total convener turns never open-loop.
- **Termination never depends on the cadence.** Even if every convener turn drew silence, `max_rounds` and the wallet soft budget (the deterministic bounded close, [MT-AUTONOMOUS-001](MT-AUTONOMOUS-001.md)) backstop termination — the cadence improves *liveness*, it does not gate *safety*.

[MT-AUTONOMOUS-003]: ../rfcs/0052-pr-plan.md#pr-7-featurev0311-rfc0052-standing--phase-3-standing--scheduled-convening--aggregate-bound
