# Manual Test MT-AUTONOMOUS-003: Standing / scheduled convening — a channel re-convenes itself on a timer, unattended, and stops at the aggregate bound

**Test ID**: `MT-AUTONOMOUS-003`
**Feature Area**: Channels (autonomous agent-only channels — RFC 0052 Phase 3: standing / scheduled convening via the config-round-trip timer seam + the mandatory aggregate bound; RFC 0024 `agents.yaml`-canonical timers; RFC 0050 config)
**Version**: 1.0
**Created**: 2026-07-09
**Last Updated**: 2026-07-09
**Status**: Active — authored at RFC 0052 PR 7c-ii-b; **live execution is a v0.3.11 release-prep (Phase 3) deliverable** (run against a real provider per [v0.3.11-plan §Acceptance](../v0.3.11-plan.md#acceptance-for-v0311)).

---

## Overview

**Purpose**: Verify the RFC 0052 [§E](../rfcs/0052-autonomous-agent-channels.md#e-standing-and-scheduled-discussions) contract — a **standing** channel convenes a **fresh interaction on a schedule**, with **no human turn and no manual convene**, and the recurrence is **bounded**: it **stops at the aggregate bound** (`max_convenings` count and/or the `standing_budget_tokens` spend ceiling), because the per-interaction cap alone does not bound a recurring schedule. A further §E safety property is asserted: the timer reaches the convener **only through a config round-trip** into its `agents.yaml` (no new runtime `RegisterTimer` API — [OQ #4](../rfcs/0052-autonomous-agent-channels.md#open-questions)). The wallet `interactionTokens` footprint is checked too, but as a **known limitation, not a bound**: per-convening eviction is **not wired** ([`EvictInteraction`](../../internal/wallet/synthesis_reserve.go) has no production caller), so the map grows one entry per convening — bounded only *within a process* by the aggregate bound, and cleared by a restart. See [§Wallet footprint](#wallet-footprint-a-bounded-leak-not-a-flat-footprint).

MT-AUTONOMOUS-001 proved one convening runs and terminates with artifacts; this MT proves the **recurring** case is both **alive** (fires unattended on schedule) and **safe** (the aggregate bound is a hard stop, not a suggestion).

**Scope**: a group channel armed exactly as in MT-AUTONOMOUS-001 (mandatory cap + convener + chair) **plus** the standing sub-knobs (`autonomous.schedule_interval_seconds` + a mandatory aggregate bound); the **config-round-trip writer** step that lands the convene timer in the convener's `agents.yaml` (`autonomy.timers`) — the PR 7c-ii-b [level-bump + tick-carry-forward](#the-config-round-trip-writer) contract; the convener's EventLoop firing `ScheduledWake(callback_kind="convene")` on schedule; the `/convene` dispatch through the **same** bounded `ChannelRouter.ConveneChannel` path a human hits; and the `max_convenings` / `standing_budget_tokens` aggregate stop.

**Out of Scope** — deferred, **not asserted** here:

- **The one-shot convene→converge→terminate→synthesize backbone** and the `1 + N` reserve — [MT-AUTONOMOUS-001](MT-AUTONOMOUS-001.md); the per-convening close is the same path and is not re-proved here.
- **Anti-collapse cadence** (Phase 2) — [MT-AUTONOMOUS-002](MT-AUTONOMOUS-002.md).
- **An operator-supplied topic queue.** Phase 3 ships a fixed/rotating topic ([OQ #4](../rfcs/0052-autonomous-agent-channels.md#open-questions)); each scheduled convening opens on the same `autonomous.topic`/`agenda`. A per-convening topic queue is a follow-up.
- **A runtime `RegisterTimer` API** and a **durable (cross-restart) aggregate count.** The bound is per-process (a restart refills it — [PR-plan §7c-ii-b residual](../rfcs/0052-pr-plan.md)); persisting it needs a store migration RFC 0052 rules out. Both are follow-ups.

---

## Related Documentation

- [RFC 0052 §E — Standing and scheduled discussions](../rfcs/0052-autonomous-agent-channels.md#e-standing-and-scheduled-discussions); [OQ #4](../rfcs/0052-autonomous-agent-channels.md#open-questions) (the config-round-trip resolution).
- [RFC 0052 PR plan §PR 7](../rfcs/0052-pr-plan.md#pr-7-featurev0311-rfc0052-standing--phase-3-standing--scheduled-convening--aggregate-bound) — this MT is the Phase-3 acceptance artifact.
- [Channels guide §Autonomous channels](../guides/channels.md#13-autonomous-channels-rfc-0052) — the operator arming/convening how-to.
- [MT-AUTONOMOUS-001](MT-AUTONOMOUS-001.md) — the one-shot backbone each scheduled convening reuses.

**Related Automated Tests** — the deterministic CI backbone of this MT (no provider needed; the timer/id/bound machinery is provider-agnostic):

- [`internal/channels/standing_schedule_test.go`](../../internal/channels/standing_schedule_test.go) — the config-round-trip timer **producer**: only an armed, aggregate-bounded standing channel yields a `ConveneTimerSpec`, and the derived timer id is agent.schema-valid **and** reverses to the channel id (a fired wake carries only `timer_id`).
- [`tests/unit/python/test_convene_timer.py`](../../tests/unit/python/test_convene_timer.py) + [`test_convene_timer_writer.py`](../../tests/unit/python/test_convene_timer_writer.py) — the Python id codec (forward encode + strict-inverse parse) and the **writer** contracts: level bump (a reactive convener is raised to `semi-autonomous` or its timer never arms) and tick carry-forward (adding a `timers` block does not silently drop the legacy heartbeat).
- [`tests/unit/python/test_convene_wake_dispatch.py`](../../tests/unit/python/test_convene_wake_dispatch.py) — a fired `ScheduledWake(callback_kind="convene")` routes to the convene client (not an LLM tick) and log-and-drops an expected decline (429/409/503) without crashing the loop.
- [`internal/channels/convening_counter_test.go`](../../internal/channels/convening_counter_test.go) + [`standing_budget_test.go`](../../internal/channels/standing_budget_test.go) — the aggregate bounds themselves: `ConveneChannel` returns 429 once `max_convenings` / `standing_budget_tokens` is reached, whether the convene came from a human or a timer.

This live MT confirms the *operator-observable* behaviour — the timer firing unattended and the bound halting it — on a real provider; the wiring and the bound arithmetic are pinned in CI.

---

## Preconditions

Identical to [MT-AUTONOMOUS-001 §Preconditions](MT-AUTONOMOUS-001.md#preconditions) — a real-provider compose stack, a clean store, the three demo personas as `respond: always` members, the CLI on `PATH`, and orchestrator log / metrics access. Two additions specific to the standing leg:

1. **Editable convener config.** You will apply the config-round-trip writer to the convener's `agents.yaml` (`config/agents.yaml`, or the convener container's mounted config) and **restart that persona** so its EventLoop picks up the new timer — the timer seam is config-canonical, so it takes effect on the persona's next boot, not live.
2. **A short schedule.** Use a **short** `schedule_interval_seconds` (≈120 s below) and a **small** `max_convenings` (3) so the whole window — multiple convenings, then the aggregate stop — is observable in a few minutes rather than across days.

---

## Test Procedure

### Step 1: Arm as a STANDING channel — and confirm the aggregate-bound gate holds

Arm the channel with the one-shot knobs **plus** a schedule **and** a mandatory aggregate bound:

```bash
persatrix channel config set group:planning \
  autonomous.enabled=true \
  autonomous.topic="Standing daily sync — what changed since yesterday and what needs attention?" \
  autonomous.convener=nova-sparrow \
  autonomous.goal="A short synthesized standup with the one thing that needs a decision." \
  autonomous.max_rounds=6 \
  autonomous.schedule_interval_seconds=120 \
  autonomous.max_convenings=3 \
  interaction_budget_tokens=150000 \
  escalation_chair_id=iron-fox
```

**Before** the successful set, confirm the §E gate fires (the recurring-runaway safety contract):

- Arming a **standing** channel (`schedule_interval_seconds` > 0) with **no** aggregate bound (`max_convenings` unset **and** `standing_budget_tokens` unset) → rejected at validate (`400`, `ErrAutonomousStandingBoundRequired`). A recurring schedule with no aggregate bound is exactly the runaway §E exists to prevent.

**Pass**: the unbounded-standing shape is rejected; the full set above round-trips (`persatrix channel config get group:planning` shows `schedule_interval_seconds=120`, `max_convenings=3`).

### Step 2: Round-trip the timer into the convener's `agents.yaml` (the config-round-trip writer)

The schedule reaches the convener as an RFC 0024 timer in its own config — there is **no** runtime timer API ([OQ #4](../rfcs/0052-autonomous-agent-channels.md#open-questions)). Register the convene timer in `nova-sparrow`'s `autonomy` block, applying the PR 7c-ii-b writer's two rules (see [§The config-round-trip writer](#the-config-round-trip-writer) for why each is load-bearing):

```yaml
# nova-sparrow's autonomy block in config/agents.yaml, after the writer:
autonomy:
  level: semi-autonomous          # (a) LEVEL BUMP — a reactive convener runs no
                                  #     scheduler, so its timer would never fire.
  timers:
    - id: convene-planning        # standing_convene_timer_id("group:planning")
      interval_seconds: 120       # = autonomous.schedule_interval_seconds
      kind: convene               # StandingConveneKind — routes to _handle_convene_wake
    # (b) TICK CARRY-FORWARD — include this ONLY if nova-sparrow was ALREADY
    #     semi-autonomous/autonomous with NO timers block (an implicit legacy tick);
    #     omit it for a convener bumped from reactive (it had no tick to keep).
    # - {id: legacy_tick, interval_seconds: 60, kind: tick}
```

The derived timer id (`convene-<name>`) is what a fired wake carries — it reverses to `group:planning`, so the convener recovers the channel from the id alone (the wake carries no `channel_id`). **Restart** `agent-nova-sparrow` so its EventLoop registers the timer.

**Pass**: after restart the convener log shows the timer armed — `COST: … timers=[convene-planning@120s …]` (the `summarize_autonomy_cadence` breadcrumb) — and, if the tick was carried forward, `legacy_tick@60s` alongside it. A convener left at `reactive` shows **no** scheduler line (the level-bump omission would silently swallow the schedule — the failure this step guards).

### Step 3: The channel convenes itself on schedule — no human, no manual convene

Watch the timeline (`GET /api/v1/channels/group:planning/messages`) and the convener log (`docker compose logs -f agent-nova-sparrow | grep -E 'convened standing channel|convene'`) **without touching anything**:

- Roughly every 120 s the convener fires a `ScheduledWake(callback_kind="convene")`; the handler POSTs `/convene`; a **fresh interaction** opens with `nova-sparrow`'s opening turn on the topic — **no operator ran `channel convene`**.
- Each convening runs the ordinary MT-AUTONOMOUS-001 arc to a bounded close (`interaction_closed{trigger=structural|cost}`), then the channel goes idle and the **next** tick re-convenes it.

**Pass**: **≥ 2** distinct interactions open back-to-back on the schedule with zero human turns and zero manual convenes; each is a fresh interaction id.

### Step 4: The aggregate bound is a hard stop — recurrence halts at `max_convenings`

Keep watching past the third convening:

- The convening counter reaches `max_convenings=3`; the **4th** scheduled fire is **declined at the bound** — `ChannelRouter.ConveneChannel` returns `429`, and the convener log records the expected decline: `scheduled convening of group:planning declined with HTTP 429 — dropping this cycle (expected on an unattended channel: a §E aggregate bound (429) …)`. The timer keeps firing on schedule but **no new interaction opens**.
- (Equivalent leg, if bounding on spend instead: set `standing_budget_tokens` rather than `max_convenings` and confirm the recurrence halts once cumulative standing spend crosses the ceiling — also `429`.)

**Pass**: exactly `max_convenings` interactions ran; every convening after the bound is refused with `429` and dispatches nothing; the schedule never re-opens past the bound.

### Step 5: The wallet footprint is a *bounded leak* across the window (known limitation)

Per-convening eviction ([`EvictInteraction`](../../internal/wallet/synthesis_reserve.go)) exists but is **not wired into the close path** — [`bounded_close.go`](../../internal/channels/bounded_close.go) defers it pending the settle barrier its precondition needs. So the map **does** grow one settled entry per convening. A **tracked residual**, not a bound this MT proves ([§Wallet footprint](#wallet-footprint-a-bounded-leak-not-a-flat-footprint)).

- Across the three convenings, confirm the `interactionTokens` map grows to **at most `max_convenings` entries** and then stops: the count bound (Step 4) caps the number of convenings, so the leak is bounded *by that bound*, not by eviction. (Bounding on spend instead, the entry count is capped by `standing_budget_tokens ÷ min-per-convening-spend` — the looser leg.)

**Pass**: the map grows one entry per convening and stops at the aggregate bound; a restart clears it. **Fail** (a real regression): entries exceed the convening count, or keep growing after the bound halts recurrence.

---

## Expected Results Summary

| # | Check | Expected |
|---|-------|----------|
| 1 | Standing gate + arming | unbounded-standing arming rejected at validate (`ErrAutonomousStandingBoundRequired`); the schedule + bound round-trip |
| 2 | Config-round-trip writer | the convene timer lands in the convener's `agents.yaml` with the level bumped (+ legacy tick carried when present); the scheduler arms it on restart |
| 3 | Unattended recurrence | ≥2 fresh interactions open on the ~120 s schedule with **zero** human turns and **zero** manual convenes |
| 4 | Aggregate bound is a hard stop | exactly `max_convenings` run; the next scheduled fire is declined `429` and opens nothing |
| 5 | Wallet footprint (known limitation) | `interactionTokens` grows one entry per convening (eviction **not yet wired**) but is bounded *within the process* by the aggregate bound (≈`max_convenings` entries), not flat; a restart clears it |

---

## Edge Cases & Error Scenarios

### Edge Case 1: a reactive convener silently swallows the schedule (the level-bump failure)

Land the `convene-planning` timer but leave `nova-sparrow` at `level: reactive` and restart.
**Expect**: **no** scheduler starts (`reactive` never enters the tick branch), so the timer never fires and the channel never self-convenes — the exact silent failure the writer's level bump prevents. This is a **negative control** for Step 2, not a supported configuration.

### Edge Case 2: the convener loses its heartbeat (the tick-carry-forward failure)

Take a convener that was `semi-autonomous` ticking on `tick_interval_seconds` with **no** `timers` block, add **only** the `convene-planning` entry (omit the carried `legacy_tick`), and restart.
**Expect**: the convene timer fires, but the convener's ordinary autonomy tick is **gone** (`register_legacy_timer = timers is None` is now `False`) — its non-convene heartbeat silently stops. The writer's tick carry-forward is what avoids this; this edge documents why it is mandatory, not optional.

### Edge Case 3: an unaddressable (non-group) standing channel

A DM/thread cannot be armed autonomous (group-only), so the writer refuses to encode a timer id for a non-`group:` address.
**Expect**: no timer is produced for a non-group id; the standing path is group-only end-to-end (the producer and the writer both reject it).

### Edge Case 4: a restart silently *resumes* an unattended channel that had hit its bound

After the bound halts recurrence (Step 4), restart the orchestrator and let the schedule fire again.
**Expect**: the count refills and convening **resumes**. This is more than "the count refills": the bound **never disarms the timer** (see Notes), so a restart actively re-opens a channel the operator watched stop — on an *unattended* channel, exactly where the bound is meant to protect. The aggregate bound therefore gives **no cross-restart protection**; it bounds spend within one process only. A durable count needs persistence RFC 0052 defers ("no new store migration"). Mitigation until then: disarm the channel (`autonomous.enabled=false`) rather than relying on the count, and re-run the writer so its convene timer is dropped (Edge Case 5).

### Edge Case 5: disarming a channel drops its convene timer on the next writer run

Arm two standing channels the same convener drives, disarm one, and re-run the writer with the producer's now-smaller `StandingConveneTimers` set.
**Expect**: the disarmed channel's `convene-<name>` entry is **removed** (the writer reconciles convene timers to the producer's current set), while the surviving channel's entry and any non-convene timers (`legacy_tick`, `reflection`) are kept. Otherwise a stale timer keeps firing a wake every interval into a channel that only `409`-declines it. Pinned by `test_convene_timer_writer.py::TestReconciliation`.

---

## The config-round-trip writer

The Phase-3 timer seam is a **config round-trip**, not a runtime API ([OQ #4](../rfcs/0052-autonomous-agent-channels.md#open-questions)): the orchestrator derives, from each armed standing channel, the `autonomy.timers` entry the convener needs (`ChannelRouter.StandingConveneTimers` — [`standing_schedule.go`](../../internal/channels/standing_schedule.go)), and that entry is written into the convener's `agents.yaml`, where RFC 0024 timers are canonical. The writer ([`agents/convene_timer_writer.py`](../../agents/convene_timer_writer.py)) is not a naive append — it enforces two contracts a hand-edit easily gets wrong:

- **Level bump.** `server_persona.initialize_persona_agents` builds the tick scheduler (the EventLoop a timer arms on) **only** at `level` `semi-autonomous`/`autonomous`. A `reactive`/`passive` convener silently ignores a `timers` entry, so the writer raises a below-scheduler level to `semi-autonomous` (Edge Case 1 is the failure it prevents).
- **Tick carry-forward.** `register_legacy_timer = timers is None`, so introducing a `timers` block drops a convener's implicit legacy heartbeat. The writer materializes that tick as an explicit `{id: legacy_tick, kind: tick}` entry — but **only** when the convener was already scheduling with no `timers` block; a just-bumped `reactive` convener had no tick to carry, so it does not gain unbudgeted idle spend (Edge Case 2 is the failure it prevents).

Because the seam is config-canonical, the round-trip takes effect on the convener's next **restart** — Step 2's restart is intrinsic to the mechanism, not a workaround.

**First-fire timing caveat (init→wire window).** `wire_convene_clients` injects the convene client *after* `initialize_persona_agents` has started the schedulers and armed their configured timers. A convene timer's **first** fire landing in that window hits a client-less scheduler and is **log-and-dropped with no re-arm** until the next interval — on a daily schedule, a lost day. Normally the window is sub-first-fire, but a saved cache anchor clamped to `_MIN_INTERVAL` plus slow init can close it. A **tracked runtime residual**; when running this MT, treat a *first* missed fire right after restart as this known window, not a Step-3 failure.

---

## Wallet footprint: a bounded leak, not a flat footprint

Step 5's check is a **known limitation**, not a clean bound — the honest state of the code at PR 7c-ii-b:

- **Eviction is not wired.** [`EvictInteraction`](../../internal/wallet/synthesis_reserve.go) exists and is unit-tested but has **no production caller**: [`bounded_close.go`](../../internal/channels/bounded_close.go) closes *without* evicting (`// NOTE: no wallet EvictInteraction here — deferred to PR 7`), so each convening leaves one settled entry. Earlier RFC 0052 docs said this landed in Phase 1/PR 4; it did not — PR 4 deferred it.
- **Why it cannot simply be called.** Its precondition is that *every* lease — including the close path's own per-persona RFC 0020 summaries — settled first; a lease granting *after* the evict re-creates the entry from zero and lets that spend **evade the cost ceiling for the rest of the interaction's life**. Those summaries are fire-and-forget tasks in N cross-process runtimes, so nothing at the close signals "all settled." A settle/refcount barrier is needed and does not exist — [PR-plan residual](../rfcs/0052-pr-plan.md).
- **What bounds it today.** The aggregate bound caps the *number* of convenings per process, so the leak is bounded to ≈`max_convenings` entries and cleared by restart. Full eviction and a durable bound are the remaining §E hardening.

---

## Test Results

| Date | Tester | OS | Result | Notes |
|------|--------|----|--------|-------|
| — | — | — | ⬜ Pending | Live execution scheduled for v0.3.11 release-prep (master-plan Phase 3). Dry-run: the CI backbone (see §Related Automated Tests) is green at PR 7c-ii-b — the producer, the id codec + writer, the wake dispatch, and both aggregate bounds. |

---

## Notes

- **The timer fires on the convener, the bound is enforced on the orchestrator.** The convener's EventLoop only *decides to convene*; the `/convene` POST runs the **same** `ChannelRouter.ConveneChannel` gate a human convene hits, so `max_convenings` / `standing_budget_tokens` bound a timer-driven convening **identically** — a scheduled convening can never bypass a bound a manual one obeys ([convene_client.py](../../agents/convene_client.py) header).
- **Every convening is separately capped.** Each scheduled convening opens a *fresh* `interaction_id` under the per-interaction `interaction_budget_tokens`; the aggregate bound is what caps the *recurring total* the per-interaction cap cannot (§E). The two are independent safety layers.
- **The schedule keeps firing after the bound.** Reaching `max_convenings` does not disarm the timer — it keeps ticking and keeps being declined `429`. That is intended: the bound is a *convening* ceiling, not a timer teardown; an operator raises `max_convenings` (or clears the count via restart) to resume.
- **`120 s` is a test cadence, not a recommendation.** A real standing panel uses a daily/weekly interval; the short interval here only makes the multi-convening window observable in one sitting.

[MT-AUTONOMOUS-001]: MT-AUTONOMOUS-001.md
[MT-AUTONOMOUS-002]: MT-AUTONOMOUS-002.md
