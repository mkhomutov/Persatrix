# Manual Test MT-WORKFLOW-001: Submit YAML Workflow via REST, Poll to Completion

**Test ID**: `MT-WORKFLOW-001`
**Feature Area**: Workflow
**Version**: 1.0
**Created**: 2026-04-18
**Last Updated**: 2026-04-18
**Status**: Active

---

## Overview

**Purpose**: Verify that a valid YAML workflow can be submitted via the REST API and polled until it
reaches a terminal status (`completed` or `failed`).

This test supports two execution modes:
- API terminal-state mode: validates REST + state transitions only. A final `failed` status is acceptable.
- End-to-end success mode: validates full workflow execution and requires all workflow agents to be registered.

**Scope**: `POST /api/v1/workflows/run` submission, `GET /api/v1/workflows/{id}/status` polling,
response envelope shape, and workflow state transitions.

**Out of Scope**: Agent LLM correctness; step output content; budget enforcement.

---

## Related Documentation

**Feature Documentation**:
- [docs/ai-agents-orchestration-spec.md](../ai-agents-orchestration-spec.md)
- [internal/server/server.go](../../internal/server/server.go) — route registrations

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
- `curl` available in PATH: `curl --version`

### Application State

**Orchestrator Setup**:
- ☐ Orchestrator built: `make build`
- ☐ Orchestrator running: `make run` (binds to `127.0.0.1:8080`)
- ☐ Config valid: `make validate` exits 0

**Agent Requirements (choose one mode)**:
- ☐ API terminal-state mode: no agents required (a terminal `failed` run is valid for this test)
- ☐ End-to-end success mode: all workflow agents are registered: `planner`, `code-writer`, `code-reviewer`

### Test Data

**Fixtures Used**:
- `workflows/feature-builder.yaml` — pre-existing workflow definition loaded by the orchestrator
  from `workflows/` at startup (`--workflows-dir`)
- Input payload: `{"workflow_id": "feature-builder", "inputs": {"user_request": "Add hello-world endpoint"}}`

---

## Test Procedure

### Step 1: Start the Orchestrator and Verify Health

**Action**: In one terminal, start the orchestrator:

```bash
make run
```

In a second terminal, confirm it is healthy:

**Windows PowerShell**:

```powershell
curl.exe -s http://127.0.0.1:8080/healthz
```

**macOS/Linux (bash/zsh)**:

```bash
curl -s http://127.0.0.1:8080/healthz
```

**Expected Result**: HTTP 200 with body `{"status":"ok"}` (or equivalent).

**Verification**:
- [ ] `curl` exits 0
- [ ] Response body confirms server is healthy

Optional agent check:

```powershell
curl.exe -s http://127.0.0.1:8080/api/v1/agents | python -m json.tool
```

Expected result for this check:
- API terminal-state mode: an empty array (`[]`) is valid and does not fail this test.
- End-to-end success mode: an empty array (`[]`) means required agents are not registered yet.

For end-to-end success mode, ensure this list includes all of:
- `planner`
- `code-writer`
- `code-reviewer`

If one or more required agents are missing, start/register them first, then rerun the
agent check before continuing.

---

### Step 2: Submit the Workflow

**Action**: POST the workflow run request:

**Windows PowerShell**:

```bash
curl.exe -s -w "`nHTTP %{http_code}`n" `
  -X POST "http://127.0.0.1:8080/api/v1/workflows/run" `
  -H "Content-Type: application/json" `
  -d '{"workflow_id":"feature-builder","inputs":{"user_request":"Add hello-world endpoint"}}'
```

**macOS/Linux (bash/zsh)**:

```bash
curl -s -w "\nHTTP %{http_code}\n" \
  -X POST http://127.0.0.1:8080/api/v1/workflows/run \
  -H "Content-Type: application/json" \
  -d '{"workflow_id":"feature-builder","inputs":{"user_request":"Add hello-world endpoint"}}'
```

**Expected Result**: HTTP 201 (`Created`) with a JSON body containing a non-empty `run_id` and `status` of
`"running"` or `"pending"`:

```json
{"run_id":"<uuid>","workflow_id":"feature-builder","status":"running"}
```

**Verification**:
- [ ] HTTP status code is `201`
- [ ] Response JSON contains `run_id` (non-empty string)
- [ ] Response JSON contains `workflow_id: "feature-builder"`
- [ ] `status` field is `"running"` or `"pending"` (not already `"completed"`)

Note the `run_id` value for the next steps.

---

### Step 3: Poll Status Until Terminal

**Action**: Replace `<RUN_ID>` with the value from Step 2, then poll at ~2-second intervals.
The loop aborts with a timeout message after 180 s so it cannot run indefinitely if the
orchestrator stalls.

**Quick status check (single request)**:

**Windows PowerShell**:

```powershell
$RUN_ID = "<RUN_ID>"
curl.exe -s "http://127.0.0.1:8080/api/v1/workflows/$RUN_ID/status" | ConvertFrom-Json | ConvertTo-Json -Depth 10
```

**macOS/Linux (bash/zsh)**:

```bash
RUN_ID=<RUN_ID>
curl -s http://127.0.0.1:8080/api/v1/workflows/${RUN_ID}/status | python3 -m json.tool
```

**Continuous polling until terminal**:

**Windows PowerShell**:

```powershell
$RUN_ID = "<RUN_ID>"
$TIMEOUT = 180
$ELAPSED = 0
$STATUS = ""
while ($ELAPSED -lt $TIMEOUT) {
  $RESP = curl.exe -s "http://127.0.0.1:8080/api/v1/workflows/$RUN_ID/status"
  $STATUS = ($RESP | ConvertFrom-Json).status
  Write-Host "$(Get-Date -Format HH:mm:ss) status=$STATUS"
  if ($STATUS -eq "completed" -or $STATUS -eq "failed") {
    $RESP
    break
  }
  Start-Sleep -Seconds 2
  $ELAPSED += 2
}
if ($STATUS -ne "completed" -and $STATUS -ne "failed") {
  Write-Error "TIMEOUT: run did not reach terminal state in ${TIMEOUT}s"
}
```

**macOS/Linux (bash/zsh)**:

```bash
RUN_ID=<RUN_ID>
TIMEOUT=180; ELAPSED=0
while [ $ELAPSED -lt $TIMEOUT ]; do
  RESP=$(curl -s http://127.0.0.1:8080/api/v1/workflows/${RUN_ID}/status)
  STATUS=$(echo "$RESP" | python3 -c "import sys,json; print(json.load(sys.stdin)['status'])")
  echo "$(date +%T) status=$STATUS"
  case "$STATUS" in completed|failed) echo "$RESP"; break;; esac
  sleep 2; ELAPSED=$((ELAPSED+2))
done
if [ "$STATUS" != "completed" ] && [ "$STATUS" != "failed" ]; then
  echo "TIMEOUT: run did not reach terminal state in ${TIMEOUT}s" >&2
fi
```

**Expected Result**: The loop terminates with `status` equal to `"completed"` or `"failed"` within
180 s. A `"failed"` status is acceptable for this test if agents are unavailable; only the
terminal transition itself is under test here.

**Verification**:
- [ ] Loop terminates (does not run indefinitely)
- [ ] Final `status` is `"completed"` or `"failed"` — never stuck at `"running"` or `"pending"`
- [ ] `started_at` field is a non-null ISO-8601 timestamp
- [ ] `finished_at` field is a non-null ISO-8601 timestamp
- [ ] `steps` field is present (may be empty map `{}` if run failed early)

---

### Step 4: Inspect the Status Response Shape

**Action**: Fetch the final status once more and pretty-print it:

**Windows PowerShell**:

```powershell
curl.exe -s "http://127.0.0.1:8080/api/v1/workflows/$RUN_ID/status" | python -m json.tool
```

**macOS/Linux (bash/zsh)**:

```bash
curl -s http://127.0.0.1:8080/api/v1/workflows/${RUN_ID}/status | python3 -m json.tool
```

**Expected Result**: All required fields present with correct types:

| Field | Type | Required |
|-------|------|----------|
| `run_id` | string | yes |
| `workflow_id` | string | yes |
| `status` | string | yes |
| `started_at` | string (ISO-8601) or null | yes |
| `finished_at` | string (ISO-8601) or null | yes |
| `steps` | object | yes |
| `error` | string | only if `status == "failed"` |

**Verification**:
- [ ] All required fields present
- [ ] No extra `500`-level or unhandled-exception text in the body
- [ ] If `status == "failed"`, the `error` field is a non-empty human-readable string

---

### Step 5: Verify the Run Appears in the List Endpoint

**Action**:

**Windows PowerShell**:

```powershell
curl.exe -s http://127.0.0.1:8080/api/v1/workflows | python -m json.tool
```

**macOS/Linux (bash/zsh)**:

```bash
curl -s http://127.0.0.1:8080/api/v1/workflows | python3 -m json.tool
```

**Expected Result**: The response is a JSON array. The array includes the run submitted in Step 2.

**Verification**:
- [ ] Response is a JSON array (not an error object)
- [ ] Array contains an element whose `run_id` matches the value from Step 2

---

## Expected Results Summary

| Step | Expected Outcome | Pass/Fail |
|------|-----------------|-----------|
| 1 | Orchestrator healthy, `/healthz` returns 200 | ☑ |
| 2 | Submission returns 201 with valid `run_id` | ☑ |
| 3 | Run reaches `completed` or `failed` terminal status | ☑ |
| 4 | Status response contains all required fields with correct types | ☑ |
| 5 | Run appears in list endpoint | ☑ |

---

## Edge Cases & Error Scenarios

### Edge Case 1: No Agents Connected

**Scenario**: Orchestrator is running but no Python agents have registered.

**Expected Behavior**: The workflow run transitions to `"failed"` with an `error` field explaining
that the agent could not be reached (gRPC connection refused or registry lookup failure). The REST
API still returns a well-formed JSON response — no 500 or panic.

---

## Test Results

| Date | Tester | OS | Result | Notes |
|------|--------|----|--------|-------|
| 2026-04-18 | mkhomutov | Windows 11 | Pass | End-to-end run completed successfully; submit returned HTTP 201 with valid `run_id`, terminal status reached, and run present in list endpoint. |

---

## Notes

- This test validates the REST plumbing and state-machine transitions, not agent LLM quality.
  A `"failed"` terminal status is acceptable when no real agent is connected — what matters is
  that the state machine advances to a terminal state cleanly.
- If running without live agents, expect `error` to mention the agent address or registry lookup.
