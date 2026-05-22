# Manual Test MT-COST-002: Workflow Exceeding Budget Is Aborted with the Expected Reason

**Test ID**: `MT-COST-002`
**Feature Area**: Cost
**Version**: 1.0
**Created**: 2026-04-18
**Last Updated**: 2026-04-18
**Status**: Active

---

## Overview

**Purpose**: Verify that when a workflow step exceeds its configured `max_llm_calls` or
`max_tokens` budget, the scheduler aborts the run and surfaces a meaningful error reason via the
status endpoint — exercising the RFC 0006 budget-enforcement path.

**Scope**: `TaskConfig.max_llm_calls`, `TaskConfig.max_tokens`, scheduler budget enforcement,
`status` → `"failed"` transition, `error` field content.

**Out of Scope**: Token pricing accuracy; cost-summary aggregation (see MT-COST-001).

> **Known gap ([ISSUE-0067](../issues/ISSUE-0067-workflow-budget-abort-no-attributable-reason.md))**:
> this test carries ⚠️ Accepted-with-known-gap. Budget enforcement itself works (per-call wallet
> leases deny over-budget LLM calls — RFC 0023), but a budget-driven workflow abort does not surface
> a *budget-attributable* terminal reason (the fixture trips agent-side `max_tokens` truncation
> first), and the scheduler pre-dispatch `ErrBudgetExceeded` check is an optimistic early-fail, not
> the enforcement point. See the issue for the fixture-rewrite + reason-attribution follow-up.

---

## Related Documentation

**Feature Documentation**:
- [docs/rfcs/0006-efficiency-execution-limits.md](../rfcs/0006-efficiency-execution-limits.md)
- [internal/scheduler/scheduler.go](../../internal/scheduler/scheduler.go)
- [internal/scheduler/budget.go](../../internal/scheduler/budget.go)

**Related Automated Tests**:
- Integration tests: `tests/integration/test_workflow.py`

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
- `ANTHROPIC_API_KEY` set

### Application State

- ☐ Orchestrator running: `make run`
- ☐ At least one Python task agent registered and connected
- ☐ Config valid: `make validate` exits 0

### Test Data

Create a budget-constrained workflow fixture at `workflows/budget-test.yaml`:

```yaml
schema_version: "0.1"

workflow:
  id: budget-test
  name: Budget Abort Test
  steps:
    - id: constrained-step
      agent: planner
      input: "Write a detailed 10-page report on the history of computing."
      max_llm_calls: 1     # forces abort after the very first LLM call
      max_tokens: 50       # extremely low output cap
      timeout_seconds: 60
```

> **Fix (2026-04-18)**: The original fixture used `id:`, `name:`, and `steps:` at the top level,
> with `task:` instead of `input:` and a nested `config:` wrapper. The actual schema requires a
> `workflow:` envelope and flat step-level fields (`input:`, `max_llm_calls`, `max_tokens`).
> `make validate` will fail on the old format.

Run `make validate` to confirm the fixture is valid before proceeding.

---

## Test Procedure

### Step 1: Submit the Budget-Constrained Workflow

**Action**:

```bash
curl -s -w "\nHTTP %{http_code}\n" \
  -X POST http://127.0.0.1:8080/api/v1/workflows/run \
  -H "Content-Type: application/json" \
  -d '{"workflow_id":"budget-test","inputs":{}}'
```

**Expected Result**: HTTP 201 (`Created`) with a `run_id`.

> **Fix (2026-04-18)**: The workflow submission endpoint returns HTTP **201**, not 200.
> The `POST /api/v1/workflows/run` endpoint always responds 201 on success (confirmed in
> MT-WORKFLOW-001 and live testing).

**Verification**:
- [ ] HTTP status 201
- [ ] `run_id` present in response body

Note the `run_id` for the following steps.

---

### Step 2: Poll Until the Run Reaches `"failed"`

**Action**:

```bash
RUN_ID=<run_id from Step 1>
TIMEOUT=120; ELAPSED=0
while [ $ELAPSED -lt $TIMEOUT ]; do
  RESP=$(curl -s http://127.0.0.1:8080/api/v1/workflows/${RUN_ID}/status)
  STATUS=$(echo "$RESP" | python3 -c "import sys,json; print(json.load(sys.stdin)['status'])")
  echo "$(date +%T) status=$STATUS"
  case "$STATUS" in completed|failed) echo "$RESP"; break;; esac
  sleep 3; ELAPSED=$((ELAPSED+3))
done
```

**Expected Result**: Run transitions to `"failed"` within 120 s.

**Verification**:
- [ ] `status` field equals `"failed"` (not `"completed"`)
- [ ] Run does **not** remain stuck at `"running"` past the timeout

---

### Step 3: Verify the Error Field Contains a Budget Reason

**Action**: Inspect the final status response from Step 2:

```bash
curl -s http://127.0.0.1:8080/api/v1/workflows/${RUN_ID}/status | python3 -m json.tool
```

**Expected Result**: The `error` field is present and non-empty. It should reference the budget
limit that was exceeded. The current sentinel from `internal/scheduler/budget.go` is:
- `"budget exceeded"`

(The check matches any budget-related phrase, so a richer string produced by a future
refactor — e.g. `"budget exceeded: max_llm_calls (1) reached"` — also passes.)
<!-- Example strings in an earlier draft ("max_llm_calls limit (1) exceeded" / "budget exhausted:
     token limit reached") did not match ErrBudgetExceeded and were replaced with the real one. -->

**Verification**:
- [ ] `"error"` field is present in the JSON
- [ ] `"error"` value is a non-empty string
- [ ] `"error"` string references a budget/limit concept (not a generic internal error)
- [ ] No 500-level HTTP status returned; response is a well-formed JSON envelope

---

### Step 4: Verify Cost Summary Still Updated

**Action**: Check that the aborted run's token usage appears in the cost summary:

```bash
curl -s http://127.0.0.1:8080/api/v1/cost/summary | python3 -m json.tool
```

**Expected Result**: Cost data updated to reflect tokens consumed before the abort.

**Verification**:
- [ ] Response is HTTP 200 and valid JSON
- [ ] Token counts are non-zero (tokens consumed on the single allowed LLM call are recorded)

---

### Step 5: Clean Up Test Fixture

**Action**: Remove the test workflow fixture created in preconditions:

```bash
rm workflows/budget-test.yaml
```

**Verification**:
- [ ] File removed

---

## Expected Results Summary

| Step | Expected Outcome | Pass/Fail |
|------|-----------------|-----------|
| 1 | Workflow submitted; `run_id` returned | ☐ |
| 2 | Run reaches `"failed"`; does not hang | ☐ |
| 3 | `error` field references budget exhaustion | ☐ |
| 4 | Cost summary updated despite abort | ☐ |
| 5 | Test fixture cleaned up | ☐ |

---

## Edge Cases & Error Scenarios

### Edge Case 1: `max_tokens: 50` Causes Agent Crash

**Scenario**: The extremely low token cap causes the agent process to panic rather than return a
graceful error.

**Expected Behavior**: The orchestrator's executor catches the gRPC error and marks the step as
`FAILED`. The workflow transitions to `"failed"` with an error message. The agent process itself
should not crash permanently — it should recover for subsequent tasks.

---

## Test Results

| Date | Tester | OS | Result | Notes |
|------|--------|----|--------|-------|
| 2026-04-18 | mkhomutov | Windows 11 | Partial | Precondition fixture fixed (wrong YAML format); Step 1 HTTP code corrected (200→201). Steps 1–5 skipped — require live agents and `ANTHROPIC_API_KEY`. Fixture validated OK with corrected format. |
| 2026-04-18 | mkhomutov | Windows 11 | Partial | Retest — fixture created at `workflows/budget-test.yaml`, `make validate` passes (4 files). Step 1: HTTP 201 `run_id=1551cf89` on port 8081. Step 2: terminal `failed` (no `planner` registered — expected). Steps 3–5 require live agents + `ANTHROPIC_API_KEY`. |
| 2026-04-18 | mkhomutov | Windows 11 | Fail (Step 4) | Full live run with planner agent on Windows 11. `run_id=5c96ebcb`. Step 1: HTTP 201 ✓. Step 2: `"failed"` in ~3 s ✓. Step 3: error = `"LLM response truncated: max_tokens limit reached"` — references limit concept ✓ (enforcement via agent-side LLM truncation, not scheduler `ErrBudgetExceeded`; `max_llm_calls:1` path not triggered since step failed on first call). Step 4: FAIL — `/api/v1/cost/summary` token counts unchanged after run (746 output tokens from prior session; new run's tokens not recorded). Root cause: `recordStepUsage` requires a non-nil `ExecuteResult`; permanent gRPC failures return no result so tokens consumed pre-failure are silently dropped. Step 5: N/A — fixture committed to repo. |
| 2026-04-18 | mkhomutov | Windows 11 | Pass | Full live retest after fix `1232236` (`recordStepUsage` called on error path when `result != nil`). Orchestrator rebuilt and restarted on port 8081 with `--config config/`. Planner agent self-registered. `run_id=e624e00b`. Step 1: HTTP 201 ✓. Step 2: `"failed"` in ~7 s ✓. Step 3: error = `"LLM response truncated: max_tokens limit reached"` ✓. Step 4: PASS — cost summary shows `daily_output_tokens=246` (started from 0; 246 tokens from the aborted run recorded) ✓. Step 5: N/A — fixture committed to repo. All 4 steps pass. |

---

## Notes

- This test exercises the most recently shipped RFC 0006 budget-enforcement code. A `"failed"`
  status that does **not** reference the budget in the `error` field may indicate the enforcement
  path is not wired correctly.
- The `max_llm_calls` default change from 10 → 5 (breaking change noted in `CHANGELOG.md`) does
  not affect this test since `config.max_llm_calls: 1` is set explicitly.
- **Gap resolved (Step 4)**: Fixed in commit `1232236`. When a step fails with a permanent gRPC
  error (e.g. LLM truncation), the executor now propagates a partial `ExecuteResult` carrying
  the agent's metadata (including `tokens_used`) alongside the error. `stage_runner.go` calls
  `recordStepUsage` when `result != nil`, ensuring tokens consumed before failure are recorded.
- **Enforcement path note**: With `max_tokens: 50`, the `max_tokens` constraint is enforced by
  the LLM provider cutting off the response; the agent detects truncation and raises an error.
  The scheduler's `ErrBudgetExceeded` sentinel (from `budget.go`) covers the `max_llm_calls`
  counter path, which is not exercised here because the step fails before a second LLM call is
  attempted. A dedicated test with `max_llm_calls: 2` and a multi-turn agent would better target
  `ErrBudgetExceeded`.
