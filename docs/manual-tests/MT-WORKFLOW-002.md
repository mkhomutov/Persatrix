# Manual Test MT-WORKFLOW-002: Submit Invalid Workflow, Verify Clean Error Response

**Test ID**: `MT-WORKFLOW-002`
**Feature Area**: Workflow
**Version**: 1.0
**Created**: 2026-04-18
**Last Updated**: 2026-04-18
**Status**: Active

---

## Overview

**Purpose**: Verify that the orchestrator returns a well-formed JSON error response (not a panic or
HTML error page) when a workflow submission is invalid.

**Scope**: Error handling for `POST /api/v1/workflows/run` — unknown workflow ID, malformed JSON
body, and missing required fields.

**Out of Scope**: Agent behavior; workflow execution; budget enforcement.

---

## Related Documentation

**Feature Documentation**:
- [docs/ai-agents-orchestration-spec.md](../ai-agents-orchestration-spec.md) — REST API error envelope
- [internal/server/server.go](../../internal/server/server.go) — handler implementations

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
- `curl` available in PATH: `curl --version`

### Application State

**Orchestrator Setup**:
- ☐ Orchestrator built: `make build`
- ☐ Orchestrator running: `make run` (binds to `127.0.0.1:8080`)
- ☐ `make validate` exits 0 (config is known-good before this test)

### Test Data

No external fixtures required. All payloads are inline in the test steps.

---

## Test Procedure

### Step 1: Submit an Unknown Workflow ID

**Action**:

**Windows PowerShell**:

```powershell
curl.exe -s -D - -o - -w "`nHTTP %{http_code}`n" `
  -X POST "http://127.0.0.1:8080/api/v1/workflows/run" `
  -H "Content-Type: application/json" `
  -d '{"workflow_id":"does-not-exist","inputs":{}}'
```

**macOS/Linux (bash/zsh)**:

```bash
curl -s -D - -o - -w "\nHTTP %{http_code}\n" \
  -X POST http://127.0.0.1:8080/api/v1/workflows/run \
  -H "Content-Type: application/json" \
  -d '{"workflow_id":"does-not-exist","inputs":{}}'
```

**Expected Result**: HTTP `404` with a JSON body matching the
`errorResponse` envelope:

```json
{"error":"<human-readable message>","code":"<code>"}
```

**Verification**:
- [x] HTTP status is `404`
- [x] Response `Content-Type` is `application/json`
- [x] Body is valid JSON
- [x] Body contains an `"error"` key with a non-empty string value
- [x] No stack trace or Go panic text in the body

Note: For this payload, `workflow_id` format is valid and the workflow file is missing,
so the expected code path is `NOT_FOUND` (`404`).

---

### Step 2: Submit Malformed JSON

**Action**:

**Windows PowerShell**:

```powershell
curl.exe -s -D - -o - -w "`nHTTP %{http_code}`n" `
  -X POST "http://127.0.0.1:8080/api/v1/workflows/run" `
  -H "Content-Type: application/json" `
  -d '{"workflow_id": NOTJSON'
```

**macOS/Linux (bash/zsh)**:

```bash
curl -s -D - -o - -w "\nHTTP %{http_code}\n" \
  -X POST http://127.0.0.1:8080/api/v1/workflows/run \
  -H "Content-Type: application/json" \
  -d '{"workflow_id": NOTJSON'
```

**Expected Result**: HTTP 400 with a JSON error body.

**Verification**:
- [x] HTTP status is `400`
- [x] Response body is valid JSON with an `"error"` key
- [x] No 500 Internal Server Error
- [x] Server continues to serve subsequent requests (not crashed)

---

### Step 3: Submit Empty Body

**Action**:

**Windows PowerShell**:

```powershell
curl.exe -s -D - -o - -w "`nHTTP %{http_code}`n" `
  -X POST "http://127.0.0.1:8080/api/v1/workflows/run" `
  -H "Content-Type: application/json" `
  -d ''
```

**macOS/Linux (bash/zsh)**:

```bash
curl -s -D - -o - -w "\nHTTP %{http_code}\n" \
  -X POST http://127.0.0.1:8080/api/v1/workflows/run \
  -H "Content-Type: application/json" \
  -d ''
```

**Expected Result**: HTTP 400 with a JSON error body indicating malformed JSON.

**Verification**:
- [x] HTTP status is `400`
- [x] Response body is valid JSON with an `"error"` key
- [x] Error message indicates malformed JSON (for example: `invalid or malformed JSON body`)

---

### Step 4: Submit Missing `workflow_id` Field

**Action**:

**Windows PowerShell**:

```powershell
curl.exe -s -D - -o - -w "`nHTTP %{http_code}`n" `
  -X POST "http://127.0.0.1:8080/api/v1/workflows/run" `
  -H "Content-Type: application/json" `
  -d '{"inputs":{"user_request":"test"}}'
```

**macOS/Linux (bash/zsh)**:

```bash
curl -s -D - -o - -w "\nHTTP %{http_code}\n" \
  -X POST http://127.0.0.1:8080/api/v1/workflows/run \
  -H "Content-Type: application/json" \
  -d '{"inputs":{"user_request":"test"}}'
```

**Expected Result**: HTTP 400 with a JSON error body.

**Verification**:
- [x] HTTP status is `400`
- [x] Response body is valid JSON with an `"error"` key
- [x] No panic, no stack trace in response body

---

### Step 5: Confirm Server Remains Healthy After All Error Cases

**Action**:

**Windows PowerShell**:

```powershell
curl.exe -s -w "`nHTTP %{http_code}`n" "http://127.0.0.1:8080/healthz"
```

**macOS/Linux (bash/zsh)**:

```bash
curl -s -w "\nHTTP %{http_code}\n" http://127.0.0.1:8080/healthz
```

**Expected Result**: HTTP 200 — the server did not crash during the error-case steps above.

**Verification**:
- [x] `curl` exits 0
- [x] Response confirms server is healthy

---

## Expected Results Summary

| Step | Expected Outcome | Pass/Fail |
|------|-----------------|-----------|
| 1 | Unknown workflow ID → 404 JSON error | ☑ |
| 2 | Malformed JSON → 400 JSON error | ☑ |
| 3 | Empty body → 400 JSON error | ☑ |
| 4 | Missing `workflow_id` → 400 JSON error | ☑ |
| 5 | Server still healthy after all error cases | ☑ |

---

## Edge Cases & Error Scenarios

### Edge Case 1: Workflow File Present but Fails Schema Validation (Optional Setup)

**Scenario**: A workflow YAML exists on disk but has an invalid schema (e.g., circular
dependency between steps).

This edge case requires additional setup (creating or modifying a workflow fixture) and is
outside the core "inline payload only" path above.

**Expected Behavior**: The orchestrator should detect the cycle at planning time and return a
4xx response with a descriptive error rather than hanging or panicking.

---

## Test Results

| Date | Tester | OS | Result | Notes |
|------|--------|----|--------|-------|
| 2026-04-18 | mkhomutov | Windows 11 | Pass | All negative-path checks passed: unknown workflow returned 404 JSON error, malformed/empty/missing-field payloads returned 400 JSON errors, and `/healthz` remained 200 afterward. |
| 2026-04-18 | Copilot | Windows 11 | Pass | Re-verified. Step 1: `404 {"error":"workflow not found","code":"NOT_FOUND"}`. Step 2: `400 {"error":"invalid or malformed JSON body","code":"BAD_REQUEST"}`. Step 3 (empty body): same 400 error. Step 4 (missing field): `400 {"error":"workflow_id is required","code":"BAD_REQUEST"}`. Step 5: `/healthz` returned 200. |
| 2026-04-18 | mkhomutov | Windows 11 | Pass | Re-verified all 5 steps. All responses match expected envelopes with `"error"` and `"code"` fields. Server remained healthy throughout. |
| 2026-04-18 | mkhomutov | Windows 11 | Pass | Retest — all 5 steps pass. 404 unknown workflow, 400 malformed/empty/missing-field, `/healthz` 200. No issues. |

---

## Notes

- The error envelope is defined in `internal/server/types.go` as `{"error":"...","code":"..."}`.
  Both fields should be present in all 4xx responses.
- This test intentionally does not test 5xx paths — those indicate server bugs, not expected
  behavior.
