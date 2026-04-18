# Manual Test MT-COST-001: `GET /api/v1/cost/summary` Reports Token Usage for a Completed Run

**Test ID**: `MT-COST-001`
**Feature Area**: Cost
**Version**: 1.0
**Created**: 2026-04-18
**Last Updated**: 2026-04-18
**Status**: Active

---

## Overview

**Purpose**: Verify that after a workflow run completes, `GET /api/v1/cost/summary` returns a
well-formed JSON response containing token usage data for that run.

**Scope**: Cost-summary REST endpoint response shape, cost tracking enablement.

**Out of Scope**: Cost calculation accuracy; per-model pricing; budget enforcement (see MT-COST-002).

---

## Related Documentation

**Feature Documentation**:
- [docs/rfcs/0006-efficiency-execution-limits.md](../rfcs/0006-efficiency-execution-limits.md)
- [internal/server/server.go](../../internal/server/server.go) — route registration
- [internal/server/cost_handlers.go](../../internal/server/cost_handlers.go)

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
- `ANTHROPIC_API_KEY` set (agents must complete at least one LLM call for cost data to accumulate)

### Application State

- ☐ Orchestrator running: `make run`
- ☐ At least one Python task agent registered and connected
- ☐ Config valid: `make validate` exits 0

---

## Test Procedure

### Step 1: Confirm Cost Tracking is Enabled

**Action**:

```bash
curl -s http://127.0.0.1:8080/api/v1/cost/summary | python3 -m json.tool
```

**Expected Result**: Either a cost-summary JSON object (tracking enabled) or a 503 with:
```json
{"code": "SERVICE_UNAVAILABLE", "message": "cost tracking is not configured"}
```

**Verification**:
- [ ] HTTP 200 → cost tracking enabled; proceed to Step 2.
- [ ] HTTP 503 → cost tracking not configured; check orchestrator startup config and re-run.

---

### Step 2: Run a Workflow to Generate Cost Data

**Action**: Submit the `feature-builder` workflow (same as MT-WORKFLOW-001):

```bash
curl -s -X POST http://127.0.0.1:8080/api/v1/workflows/run \
  -H "Content-Type: application/json" \
  -d '{"workflow_id":"feature-builder","inputs":{"user_request":"Add hello-world endpoint"}}' \
| python3 -c "import sys,json; d=json.load(sys.stdin); print('run_id:', d['run_id'])"
```

Note the `run_id`. Then poll until terminal (see MT-WORKFLOW-001 Step 3) or wait 60 s.

**Verification**:
- [ ] `run_id` obtained
- [ ] Run reaches `"completed"` or `"failed"` terminal status

---

### Step 3: Query the Cost Summary

**Action**:

```bash
curl -s http://127.0.0.1:8080/api/v1/cost/summary | python3 -m json.tool
```

**Expected Result**: HTTP 200 with a JSON object containing cost/token data. The exact schema is
defined by `costReporter.GlobalSummary()` in `internal/cost/`. Expected top-level fields include
aggregated token counts and cost figures.

**Verification**:
- [ ] HTTP 200
- [ ] Response is valid JSON (no parse error from `python3 -m json.tool`)
- [ ] Response is not an empty object `{}`
- [ ] At least one numeric field representing tokens or cost is present and non-zero

---

### Step 4: Verify Run-Level Cost is Tracked

**Action**: If the response includes a per-run breakdown, locate the entry for the `run_id` from
Step 2:

```bash
RUN_ID=<run_id from Step 2>
curl -s http://127.0.0.1:8080/api/v1/cost/summary \
  | python3 -c "
import sys, json
d = json.load(sys.stdin)
print(json.dumps(d, indent=2))
"
```

**Expected Result**: The cost data includes entries attributable to the workflow run in Step 2
(either directly keyed by `run_id` or aggregated into global totals).

**Verification**:
- [ ] Token usage is non-zero (confirms at least one LLM call was tracked)
- [ ] Response structure is consistent between repeated calls to the endpoint

---

## Expected Results Summary

| Step | Expected Outcome | Pass/Fail |
|------|-----------------|-----------|
| 1 | Cost tracking enabled; 200 OK | ☐ |
| 2 | Workflow run completes; `run_id` obtained | ☐ |
| 3 | Cost summary returns non-empty JSON with token/cost fields | ☐ |
| 4 | Token usage is non-zero after a completed run | ☐ |

---

## Edge Cases & Error Scenarios

### Edge Case 1: Cost Tracking Not Configured

**Scenario**: Orchestrator started without cost-tracking configuration.

**Expected Behavior**: `GET /api/v1/cost/summary` returns HTTP 503 with
`{"code": "SERVICE_UNAVAILABLE", "message": "cost tracking is not configured"}`.
No panic or 500 error.

---

## Test Results

| Date | Tester | OS | Result | Notes |
|------|--------|----|--------|-------|
| | | | | |
