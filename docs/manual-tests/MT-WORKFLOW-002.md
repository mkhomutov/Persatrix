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

```bash
curl -s -w "\nHTTP %{http_code}\n" \
  -X POST http://127.0.0.1:8080/api/v1/workflows/run \
  -H "Content-Type: application/json" \
  -d '{"workflow_id":"does-not-exist","inputs":{}}'
```

**Expected Result**: HTTP 4xx (expected `404` or `400`) with a JSON body matching the
`errorResponse` envelope:

```json
{"error":"<human-readable message>","code":"<code>"}
```

**Verification**:
- [ ] HTTP status is 4xx (not 2xx, not 5xx)
- [ ] Response `Content-Type` is `application/json`
- [ ] Body is valid JSON
- [ ] Body contains an `"error"` key with a non-empty string value
- [ ] No stack trace or Go panic text in the body

---

### Step 2: Submit Malformed JSON

**Action**:

```bash
curl -s -w "\nHTTP %{http_code}\n" \
  -X POST http://127.0.0.1:8080/api/v1/workflows/run \
  -H "Content-Type: application/json" \
  -d '{"workflow_id": NOTJSON'
```

**Expected Result**: HTTP 400 with a JSON error body.

**Verification**:
- [ ] HTTP status is `400`
- [ ] Response body is valid JSON with an `"error"` key
- [ ] No 500 Internal Server Error
- [ ] Server continues to serve subsequent requests (not crashed)

---

### Step 3: Submit Empty Body

**Action**:

```bash
curl -s -w "\nHTTP %{http_code}\n" \
  -X POST http://127.0.0.1:8080/api/v1/workflows/run \
  -H "Content-Type: application/json" \
  -d ''
```

**Expected Result**: HTTP 400 with a JSON error body indicating a missing or empty `workflow_id`.

**Verification**:
- [ ] HTTP status is `400`
- [ ] Response body is valid JSON with an `"error"` key
- [ ] Error message references a missing or invalid field (not a generic 500)

---

### Step 4: Submit Missing `workflow_id` Field

**Action**:

```bash
curl -s -w "\nHTTP %{http_code}\n" \
  -X POST http://127.0.0.1:8080/api/v1/workflows/run \
  -H "Content-Type: application/json" \
  -d '{"inputs":{"user_request":"test"}}'
```

**Expected Result**: HTTP 400 with a JSON error body.

**Verification**:
- [ ] HTTP status is `400`
- [ ] Response body is valid JSON with an `"error"` key
- [ ] No panic, no stack trace in response body

---

### Step 5: Confirm Server Remains Healthy After All Error Cases

**Action**:

```bash
curl -s http://127.0.0.1:8080/healthz
```

**Expected Result**: HTTP 200 — the server did not crash during the error-case steps above.

**Verification**:
- [ ] `curl` exits 0
- [ ] Response confirms server is healthy

---

## Expected Results Summary

| Step | Expected Outcome | Pass/Fail |
|------|-----------------|-----------|
| 1 | Unknown workflow ID → 4xx JSON error | ☐ |
| 2 | Malformed JSON → 400 JSON error | ☐ |
| 3 | Empty body → 400 JSON error | ☐ |
| 4 | Missing `workflow_id` → 400 JSON error | ☐ |
| 5 | Server still healthy after all error cases | ☐ |

---

## Edge Cases & Error Scenarios

### Edge Case 1: Workflow File Present but Fails Schema Validation

**Scenario**: A workflow YAML exists on disk but has an invalid schema (e.g., circular
dependency between steps).

**Expected Behavior**: The orchestrator should detect the cycle at planning time and return a
4xx response with a descriptive error rather than hanging or panicking.

---

## Test Results

| Date | Tester | OS | Result | Notes |
|------|--------|----|--------|-------|
| | | | | |

---

## Notes

- The error envelope is defined in `internal/server/types.go` as `{"error":"...","code":"..."}`.
  Both fields should be present in all 4xx responses.
- This test intentionally does not test 5xx paths — those indicate server bugs, not expected
  behavior.
