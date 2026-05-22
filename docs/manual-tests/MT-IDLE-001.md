# Manual Test MT-IDLE-001: Idle Persona Costs Nothing ("Idle Truly Idle")

**Test ID**: `MT-IDLE-001`
**Feature Area**: Cost
**Version**: 1.0
**Created**: 2026-05-22
**Last Updated**: 2026-05-22
**Status**: Active (execution deferred to v0.3.3 release-prep)

---

## Overview

**Purpose**: Verify the v0.3.3 user-facing promise — *a persona with no scheduled
work and no inbound traffic costs nothing*. Started with `autonomy.timers: []` and
left alone, the persona's per-agent event loop parks on its queue and the agent is
never invoked: no LLM provider call, no memory-recall query, no wallet lease. This is
the human-driven companion to the automated `tests/integration/test_bored_persona_cost.py`
gate ([RFC 0024 §Test Strategy](../rfcs/0024-event-driven-scheduling.md#test-strategy)).

**Scope**: Event-driven scheduling under the RFC 0024 Phase 1–4 model — `EventLoop`
substrate, the `TickScheduler` thin adapter, and the absence of any synthetic tick when
no timers are configured. Observed via `persatrix logs` (no provider activity) and
`GET /api/v1/cost/summary` (no wallet/token activity).

**Out of Scope**: Personas that *do* configure `autonomy.timers` (they wake on schedule
by design); inbound chat / channel traffic (that legitimately wakes the loop — see
MT-COST-003 / MT-CHANNEL-*); the `tick_interval_seconds` legacy path (continues to work;
its Phase 5 deprecation warning is v0.4.0).

---

## Related Documentation

**Feature Documentation**:
- [docs/rfcs/0024-event-driven-scheduling.md](../rfcs/0024-event-driven-scheduling.md) — §B
  (event-loop inversion), §C (timer registry), §Test Strategy (this gate).
- [docs/rfcs/0024-pr-plan.md](../rfcs/0024-pr-plan.md) — PR 4 (authored this test).
- [agents/event_loop.py](../../agents/event_loop.py) — the per-agent `EventLoop` that parks
  on `queue.get()` when no timers and no events exist.
- [agents/tick.py](../../agents/tick.py) — `TickScheduler` adapter; `register_legacy_timer=False`
  for the no-timers (`autonomy.timers: []`) case.

**Related Automated Tests**:
- Integration: `tests/integration/test_bored_persona_cost.py` (the CI cost-regression gate).
- Unit: `agents/tests/test_event_loop.py`, `agents/tests/test_event_loop_wake_counters.py`.

**Related Manual Tests**:
- [MT-COST-004](MT-COST-004.md) — TICK budget-exhaustion (the woken-but-denied path; still
  passes under the event-driven model via the synthesised `legacy_tick`).
- [MT-COST-003](MT-COST-003.md) — chat budget-denial (a legitimately *woken* path).

---

## Preconditions

### System Requirements

**Operating Systems**:
- ☐ Windows 10/11 (x64)
- ☐ macOS 12.0+ (Intel/Apple Silicon)
- ☐ Linux (Ubuntu 22.04+)

**Dependencies Installed**:
- Go 1.24+: `go version`
- Python 3.11+: `python3 --version`
- `curl` + `jq` available in PATH
- `ANTHROPIC_API_KEY` set

### Application State

- ☐ Orchestrator running: `make run`
- ☐ Exactly one persona agent registered and connected
- ☐ Config valid: `make validate` exits 0

### Test Data

In `config/agents.yaml`, the persona under test must have **no scheduled timers** — the
v0.3.3 default. Set its `autonomy` block to an empty timer list and remove any
`tick_interval_seconds`:

```yaml
autonomy:
  level: "semi-autonomous"
  timers: []          # no scheduled work → the loop parks
```

Run `make validate` after the edit and restart the orchestrator so the persona reconnects
with no timers registered.

---

## Test Procedure

### Step 1: Confirm the Persona Started With No Timers

**Action**: Inspect the agent startup logs.

```bash
persatrix logs --agent <agent-id> | grep -iE "tick scheduler|cadence|timers"
```

**Expected Result**: The startup line reports an event-driven scheduler with **no timers**
(e.g. `Started tick scheduler for <id> (no timers — event-driven)`), not a periodic tick
cadence.

**Verification**:
- [ ] Startup log shows the scheduler started
- [ ] No periodic tick cadence is reported (no `tick_interval_seconds` line, no `legacy_tick`)

---

### Step 2: Capture a Cost Baseline

**Action**:

```bash
curl -s http://127.0.0.1:8080/api/v1/cost/summary | tee /tmp/cost-before.json | python3 -m json.tool
```

**Expected Result**: A cost summary snapshot. Note `daily_input_tokens`,
`daily_output_tokens`, and `daily_estimated_usd`.

**Verification**:
- [ ] Baseline snapshot captured to `/tmp/cost-before.json`

---

### Step 3: Observe for 60 Seconds With No Interaction

**Action**: Send the persona **no** chat and **no** channel traffic. Wait 60 seconds, then
tail the logs for the observation window.

```bash
sleep 60
persatrix logs --agent <agent-id> --since 60s
```

**Expected Result**: No provider activity during the window — no LLM-call span, no
`create_message`, no memory-recall (`_inject_memory_context`) line, no wallet lease
(`AcquireLease`). The agent log is quiet apart from health/liveness lines.

**Verification**:
- [ ] No LLM provider call logged in the 60 s window
- [ ] No memory-recall / context-injection line logged
- [ ] No wallet `AcquireLease` line logged
- [ ] No `agent.wake.*` events for this agent in the window

---

### Step 4: Confirm the Cost Summary Did Not Move

**Action**:

```bash
curl -s http://127.0.0.1:8080/api/v1/cost/summary | tee /tmp/cost-after.json | python3 -m json.tool
diff <(jq -S . /tmp/cost-before.json) <(jq -S . /tmp/cost-after.json) && echo "NO COST CHANGE"
```

**Expected Result**: `daily_input_tokens`, `daily_output_tokens`, and `daily_estimated_usd`
are unchanged from the Step 2 baseline. The idle persona advanced no counters.

**Verification**:
- [ ] `daily_input_tokens` unchanged
- [ ] `daily_output_tokens` unchanged
- [ ] `daily_estimated_usd` unchanged

---

### Step 5: Sanity Check — A Single Chat Turn Still Wakes the Persona

**Action**: Send one chat turn to prove the loop is *idle, not dead*.

```bash
curl -s -X POST "http://127.0.0.1:8080/api/v1/agents/<agent-id>/chat" \
  -H "Content-Type: application/json" \
  -d '{"message":"are you there?","user_id":"local"}' | jq '{reply_status, reply}'
```

**Expected Result**: HTTP 200 with `reply_status="ok"` and a non-empty `reply`. The inbound
event woke the parked loop and the persona responded — confirming the quiet in Steps 3–4 was
genuine idleness, not a hung agent.

**Verification**:
- [ ] Chat turn returns `reply_status="ok"` with a non-empty reply
- [ ] The cost summary now advances (the woken turn legitimately spends)

---

## Expected Results Summary

| Step | Expected Outcome | Pass/Fail |
|------|------------------|-----------|
| 1 | Persona started with no timers (event-driven, no periodic cadence) | ☐ |
| 2 | Cost baseline captured | ☐ |
| 3 | No provider / memory / wallet activity across the 60 s idle window | ☐ |
| 4 | Cost summary unchanged from the baseline | ☐ |
| 5 | A single chat turn still wakes the persona (idle, not dead) | ☐ |

---

## Edge Cases & Error Scenarios

### Edge Case 1: Persona Has a Configured Timer

**Scenario**: The persona under test has a non-empty `autonomy.timers` (e.g. a
`memory_consolidation` timer).

**Expected Behavior**: This test does **not** apply — a configured timer wakes the loop on
schedule by design. Pick a persona with `timers: []`, or temporarily clear the timers for the
observation window.

### Edge Case 2: Background Channel Membership

**Scenario**: The persona is a member of a channel that other agents are actively posting to.

**Expected Behavior**: Inbound channel messages legitimately wake the loop (RFC 0024 §E), so
the window will show activity. For a clean idle observation, use a persona with no inbound
channel traffic during the window.

---

## Test Results

Executed in v0.3.3 release-prep PR 1 — see [`v0.3.3-execution-report.md`](v0.3.3-execution-report.md#mt-idle-001--primary-gate-evidence-live) for per-step evidence.

| Date | Tester | OS | Result | Notes |
|------|--------|----|--------|-------|
| 2026-05-22 | Claude (Opus 4.7) | Windows 11 + Docker Desktop | ✅ Pass | `ea2b86d` RC tip, live stack. ember-owl (`timers: []`): event-driven start (no cadence); 60 s idle window → 0 tokens / $0, no `agent.wake.*`, no LLM/memory/wallet activity; cost unchanged; chat turn woke it (`reply_status="ok"`, cost advanced, `agent_wake_inbound_total` 0→1). |

---

## Notes

- The 60-second window mirrors the automated gate's observation budget. The automated test
  uses a short wall-clock window for speed; the manual run uses the full 60 s to match the
  user-facing "leave it running and it costs nothing" claim.
- Pre-v0.3.3, a persona on the polling `TickScheduler` would have fired at least one `on_tick`
  inside this window — a recall query + LLM call + wallet lease — even with nothing to do.
  That polling-cost class is what RFC 0024 closes structurally; this test is the human-visible
  confirmation.
