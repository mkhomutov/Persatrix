# Manual Test MT-COST-004: TICK Budget Exhaustion Records Idle Without Provider Spend

**Test ID**: `MT-COST-004`
**Feature Area**: Cost
**Version**: 1.0
**Created**: 2026-05-20
**Last Updated**: 2026-05-20
**Status**: Active

---

## Overview

**Purpose**: Verify that when an autonomous persona agent's `per_agent` budget is exhausted,
every subsequent TICK is recorded as idle (with `idle_reason=budget_denied`) instead of
crashing the tick loop or silently leaking provider calls. Exercises the RFC 0023 PR 5
autonomous-TICK lease wiring and the budget-denied → idle short-circuit that distinguishes
TICK from chat (chat raises to the caller; TICK has no caller so it short-circuits).

**Scope**: `WalletService.AcquireLease` denial on the autonomous-TICK origin
(`cause=CAUSE_AUTONOMOUS_TICK`), `BudgetExceededError` short-circuit in
`persona_runtime.action_loop._on_event_inner`, `idle_count` increments in
`TickScheduler._run`, `agent.persona.tick.idle` counter with `idle_reason=budget_denied`.

**Out of Scope**: Chat-path budget denial (MT-COST-003); workflow-task budget denial
(MT-COST-002); sub-agent attribution; channel-message origin (RFC 0023 PR 6); per-cause
budget policies (RFC 0023 Phase 7+).

---

## Related Documentation

**Feature Documentation**:
- [docs/rfcs/0023-llm-call-leasing.md](../rfcs/0023-llm-call-leasing.md) — §E (Python client
  integration), §F (failure modes), Phase 5 (this test).
- [docs/rfcs/0023-pr-plan.md](../rfcs/0023-pr-plan.md) — PR 5 (this test).
- [agents/persona_runtime/action_loop.py](../../agents/persona_runtime/action_loop.py) —
  TICK call site, budget-denied short-circuit, idle-reason metric increment.
- [agents/persona_runtime/wallet_cause.py](../../agents/persona_runtime/wallet_cause.py) —
  `cause_for_event` (TICK → `CAUSE_AUTONOMOUS_TICK`).
- [agents/tick.py](../../agents/tick.py) — `TickScheduler._run` `idle_count` plumbing.
- [agents/observability/metrics.py](../../agents/observability/metrics.py) —
  `persona_tick_idle` counter, `tick_idle_attrs`.
- [docs/observability.md](../observability.md) — `agent.persona.tick.idle` attribute schema.

**Related Automated Tests**:
- Unit: `agents/tests/test_action_loop_tick_lease.py`
- Integration: `tests/integration/test_tick_budget_denied_idle.py`

**Related Manual Tests**:
- [MT-COST-002](MT-COST-002.md) — workflow-step budget abort (synchronous-call analogue).
- [MT-COST-003](MT-COST-003.md) — chat budget exceed (propagating analogue;
  TICK short-circuits because it has no caller).

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
- `curl` available in PATH
- `jq` available in PATH (response inspection)
- `ANTHROPIC_API_KEY` set

### Application State

- ☐ Orchestrator running: `make run`
- ☐ One autonomous persona agent registered (`autonomy.enabled: true` so its tick loop fires)
- ☐ Config valid: `make validate` exits 0

### Test Data

The orchestrator's wallet must enforce a `per_agent` budget low enough to deny on the first
TICK. Edit `config/optimization.yaml` so the persona agent's `per_agent` budget is small
(e.g. `0.01` USD), then restart the orchestrator. Note the pre-edit value so Step 5 can
restore it. The TICK interval should be short enough that several ticks fire within the test
window — e.g. `agents.<persona>.tick_interval_seconds: 10`.

```yaml
cost:
  budgets:
    global:
      max_daily_usd: 100.00
    per_workflow:
      default_max_usd: 10.00
    per_agent:
      default_max_usd: 0.01      # tight cap — denies every TICK

# Top-level — sibling of `cost:`. RFC 0023 wallet lease lifecycle tuning.
wallet:
  ttl_seconds: 60
  reaper_interval_seconds: 5
  max_active_leases: 16
```

Run `make validate` after the edit and confirm the cap is in effect via
`GET /api/v1/cost/summary`.

---

## Test Procedure

### Step 1: Pre-Exhaust the Budget

**Action**: With the tight `per_agent` cap from Preconditions, drive one chat turn (or any
LLM-touching action) so the wallet records the spend that puts the agent at the cap. If the
configured cap is small enough that *any* LLM call exceeds it, the first TICK after agent
start-up will already be denied — skip the chat turn and proceed directly to Step 2.

```bash
AGENT_ID=<the registered persona agent id>
curl -s -X POST "http://127.0.0.1:8080/api/v1/agents/${AGENT_ID}/chat" \
  -H "Content-Type: application/json" \
  -d '{"message":"hello","user_id":"local"}' | jq '{reply_status, reply}'
```

**Expected Result**: `reply_status="ok"` on the first turn (if the cap admits it) or
`reply_status="error"` (cap denies it). Either is acceptable — the test target is the *next*
several TICKs, which must be denied.

**Verification**:
- [ ] `GET /api/v1/cost/summary` shows `per_agent` spend at or above the cap

---

### Step 2: Observe Autonomous TICKs

**Action**: Wait for at least three tick intervals to elapse (e.g. 30 s with a 10 s interval),
then tail the orchestrator logs and grep for the TICK structured log line emitted by the
persona action loop:

```bash
# Adjust the log path / filter to your deployment's log surface.
journalctl -u persatrix-orchestrator -f \
  | grep -E "agent.persona.tick|autonomous tick denied"
```

**Expected Result**: Each tick logs the `WARN`-level "autonomous tick denied by wallet"
record carrying the wallet's `LeaseDenied.message`. No `Tick error` lines appear (the
short-circuit caught the denial before `TickScheduler._run`'s generic-exception arm could
log one).

**Verification**:
- [ ] Multiple `autonomous tick denied by wallet` WARN lines per minute
- [ ] **No** `Tick error for agent` lines (would indicate the short-circuit failed)
- [ ] **No** unhandled-exception traces with `BudgetExceededError` in them

---

### Step 3: Verify the `idle_reason=budget_denied` Counter Increments

**Action**: Inspect the OTEL metrics surface (Prometheus, OTLP collector, or
`GET /api/v1/metrics` if the orchestrator exposes a debug endpoint) for the
`agent.persona.tick.idle` counter filtered on `idle_reason=budget_denied`.

```bash
curl -s http://127.0.0.1:9464/metrics \
  | grep -E '^agent_persona_tick_idle.*idle_reason="budget_denied"'
```

**Expected Result**: The counter has a non-zero value for the autonomous persona, attributed
to `idle_reason=budget_denied` and the persona's `agent.id`. The value increments roughly
once per tick interval.

**Verification**:
- [ ] `agent.persona.tick.idle{agent.id="<persona>",idle_reason="budget_denied"}` ≥ 3 after
      three intervals
- [ ] The counter is **distinct** from `idle_reason="empty_context_tick"` — querying with
      the empty-context reason returns a separate timeseries

---

### Step 4: Verify No Provider Spend Was Charged for Denied TICKs

**Action**: Inspect the orchestrator's cost summary across the observation window; denied
TICKs must not have advanced either the input- or output-token counters beyond the Step 1
baseline.

```bash
curl -s http://127.0.0.1:8080/api/v1/cost/summary | python3 -m json.tool
```

**Expected Result**: Token counts after Step 3's observation window match the Step 1
baseline. Every TICK was denied *before* the provider was contacted (RFC 0023 § F),
so the wallet's provisional charge was reversed on each denied-lease exit and no provider
spend occurred.

**Verification**:
- [ ] `daily_output_tokens` did not advance during the Step 2/3 observation window
- [ ] `per_agent` spend for the autonomous persona did not advance

---

### Step 5: Restore the Original Budget Config

**Action**: Revert the `config/optimization.yaml` edit from Preconditions, restart the
orchestrator, and confirm `GET /api/v1/cost/summary` reflects the restored cap. Verify the
autonomous persona's next TICK reaches the provider (no more `autonomous tick denied`
log lines).

**Verification**:
- [ ] `config/optimization.yaml` matches the pre-test value
- [ ] `make validate` exits 0
- [ ] Orchestrator restart cleanly registers the persona agent
- [ ] First post-restore TICK reaches the provider (no denial WARN)

---

## Expected Results Summary

| Step | Expected Outcome | Pass/Fail |
|------|------------------|-----------|
| 1 | Wallet records `per_agent` spend at or above the configured cap | ☐ |
| 2 | Every observed TICK logs `autonomous tick denied by wallet`; no `Tick error` lines | ☐ |
| 3 | `agent.persona.tick.idle{idle_reason="budget_denied"}` increments per tick | ☐ |
| 4 | Cost summary shows no provider-spend trace for denied TICKs | ☐ |
| 5 | Original budget config restored; TICKs resume reaching the provider | ☐ |

---

## Edge Cases & Error Scenarios

### Edge Case 1: Wallet Unreachable

**Scenario**: The orchestrator's gRPC listener is down when a TICK fires. The agent's
`WalletClient` raises `BudgetExceededError(reason="wallet_unreachable")`.

**Expected Behavior**: TICK short-circuits to `DO_NOTHING` exactly as it does for a hard
budget denial — same WARN log shape, same `idle_reason=budget_denied` counter increment
(the counter does not distinguish denial-vs-unreachable; the WARN log message does). The
tick loop continues without crashing.

### Edge Case 2: Empty-Context TICK Coincides With Budget Pressure

**Scenario**: The persona has nothing to do (no memory admitted, no active goal, no pending
turn) *and* the budget is exhausted.

**Expected Behavior**: The RFC 0017 §F empty-context short-circuit fires *first* (it runs
before the LLM call is attempted), so the tick records `idle_reason=empty_context_tick`,
not `budget_denied`. This is correct — no provider call was attempted, so no budget
pressure was actually observed on this tick. The two reasons are disjoint by construction.

### Edge Case 3: Chat Turn Mid-Test Hits the Same Cap

**Scenario**: A user issues a chat turn during the observation window; the chat path's
LLM call hits the same `per_agent` cap.

**Expected Behavior**: The chat handler surfaces the denial as `reply_status="error"` per
MT-COST-003 — chat propagates, TICK short-circuits. The two paths are independent;
neither short-circuit affects the other's metric attribution.

---

## Test Results

| Date | Tester | OS | Result | Notes |
|------|--------|----|--------|-------|

(Execution is deferred to the v0.3.2 release-prep Phase 4 PR 1 manual-test report per the
v0.3.2 master plan.)

---

## Notes

- This test exercises the autonomous-TICK lease wiring introduced in RFC 0023 PR 5. The
  asymmetry with MT-COST-003 (chat) is deliberate: chat propagates the denial to its
  caller, while a TICK has no caller and must short-circuit to idle. Re-raising on a TICK
  would surface as `Tick error` in `TickScheduler._run` and lose the tick instead of
  recording it as idle, blinding dashboards to the budget pressure the wallet is actually
  suppressing.
- The `idle_reason` attribute is the dashboard discriminator — without it, sustained
  budget pressure is indistinguishable from organic quiet periods (the empty-context
  short-circuit also records to the same counter, tagged `empty_context_tick`).
- The wallet's provisional charge is reversed on the denied-lease exit, so a denied
  TICK leaves no per-token spend trace. The wallet *attempt* is still recorded in the
  orchestrator's wallet metrics (`acquire_total`), separate from this Python-side counter.
