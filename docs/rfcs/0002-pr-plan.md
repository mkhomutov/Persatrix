# RFC 0002 — PR Implementation Plan

**RFC**: [0002-rest-api-server.md](0002-rest-api-server.md)
**Created**: 2026-04-09
**Branch prefix**: `feature/v01-`
**Target**: `main`
**Merge strategy**: Squash merge per [BRANCHING.md](../BRANCHING.md)

---

## Overview

RFC 0002 defines ~420 LOC across 4 phases. The project's PR size limit is <500 lines of meaningful change. Phases 1 and 2 together (~370 LOC implementation + ~400–600 LOC tests) will exceed the limit. This plan splits the work into **5 PRs**: Phase 1 is split into two PRs at the middleware/handler boundary (server scaffolding + middleware vs. workflow handlers), Phase 2 adds agent handlers, Phase 3+4 are combined into a single PR (stubs + wiring are both small), and a final cleanup PR sweeps accumulated review findings.

Each PR is independently mergeable and leaves the codebase in a compilable, test-passing state.

**Prerequisite**: All RFC 0001 PRs merged (state, registry, planner). ✅ Confirmed — `internal/state/`, `internal/registry/`, `internal/planner/` have full implementations. `github.com/google/uuid` already in `go.mod`.

> **Plan deviation**: PRs 1 and 2 were combined into a single PR #14 (`feature/v01-rest-api-phase1`). The scaffold, middleware, helpers, and workflow handlers were developed together because testing the server scaffold in isolation with 501 placeholder handlers provided minimal value — the real coverage comes from handler integration tests. Combined size (1,453 lines, 55% tests) is documented below with waiver rationale.

---

## PR Sequence

### ~~PR 1~~ + ~~PR 2~~ → PR #14: `feature/v01-rest-api-phase1` — Server Scaffolding + Middleware + Workflow Handlers

**Depends on**: RFC 0001 fully merged (all 5 PRs)
**Branch**: `feature/v01-rest-api-phase1` (combined PRs 1+2 from original plan)
**Actual size**: 1,453 lines (654 implementation + 799 tests)

> **Estimate calibration (F-03)**: RFC 0001 PRs consistently exceeded estimates by 73–138% (e.g. state: 350–450 est → 781 actual, planner: 350–450 est → 1,071 actual). Sizes in this plan are calibrated to ~1.7× of naive estimates. If PR 1 still exceeds 500 lines, apply the escape valve in the Risk Mitigation table.

> **Dependencies note (F-09)**: No `go.mod` changes expected — `github.com/google/uuid` already present from RFC 0001. All new imports (`runtime/debug`, `net/http`, etc.) are stdlib.

#### Scope

| File | Change |
|------|--------|
| `internal/server/server.go` | New — `Server` struct, `New` constructor (validates + canonicalizes `workflowsDir`), `Handler()` (returns composed `http.Handler`), `Start(ctx)` with graceful shutdown, `resolveWorkflowPath` (path traversal protection), route registration, minimal `GET /healthz` handler |
| `internal/server/types.go` | New — request/response DTOs: `SubmitRunRequest`, `SubmitRunResponse`, `RunStatusResponse`, `RegisterAgentRequest`, `RegisterAgentResponse`, `AgentResponse`, `ErrorResponse`; `*time.Time` for nullable timestamps (M-07); `runStatusString` helper mapping `state.RunStatus` → lowercase string. Agent response DTOs (`RegisterAgentResponse`, `AgentResponse`) provide snake_case `json:` tags for consistency — `registry.AgentInfo` has no tags, so direct serialization would produce PascalCase JSON (F-15). |
| `internal/server/middleware.go` | New — `recoveryMiddleware` (with `runtime/debug.Stack()`), `requestIDMiddleware` (server-generated UUID only), `loggingMiddleware` (status capture wrapper), `contextKey` type for `SA1029` compliance |
| `internal/server/helpers.go` | New — `writeJSON`, `writeError`, `requireJSON` (Content-Type enforcement), `decodeBody` (wraps `MaxBytesReader` + strict decoder + `MaxBytesError` check) |
| `internal/server/server_test.go` | New — middleware tests, helper tests, `/healthz` test, `resolveWorkflowPath` tests (path traversal, symlink, valid ID) |

#### Key implementation details

- `New` returns `(*Server, error)`: validates `workflowsDir` via `os.Stat` + `IsDir()`, canonicalizes via `filepath.Abs` + `filepath.EvalSymlinks` (CS-02). Stores the canonical path so `resolveWorkflowPath` avoids repeated syscalls.
- `resolveWorkflowPath`: regex-validates `workflow_id` first, then `filepath.Join` + `filepath.EvalSymlinks` + `strings.HasPrefix` prefix check against canonical root. Returns `ErrWorkflowNotFound` for both traversal attempts and missing files (no information leakage).
- `Handler()` composes middleware in order: `recoveryMiddleware` → `requestIDMiddleware` → `loggingMiddleware` → `mux`. (F-12: reordered from RFC 0002's stated `recovery → logging → requestID → mux` so that `loggingMiddleware` can read the request ID from context when its deferred log fires. With the original order, `requestIDMiddleware` has not yet set the ID when `loggingMiddleware` begins processing. RFC 0002 should be updated to match.)
- `Start(ctx)` spawns a goroutine that waits on `ctx.Done()` and calls `srv.Shutdown` with a 10-second timeout. Logs shutdown errors via zap (CS-01).
- `decodeBody` helper: wraps body with `http.MaxBytesReader(w, r.Body, 1<<20)`, creates strict decoder (`DisallowUnknownFields`), checks for `*http.MaxBytesError` before generic decode errors.
- `GET /healthz` is registered in this PR to satisfy `docker-compose.yaml` healthcheck. Returns `200` with `{"status": "ok"}`.
- Nil-logger guard: `if logger == nil { logger = zap.NewNop() }` in `New`.
- Route registration: all routes registered in `New` but workflow/agent handler methods are not yet implemented — they return `501 Not Implemented` as temporary placeholders until PR 2 and PR 3 land. This keeps the mux complete and testable from PR 1.
- `// TODO(security): no auth in v0.1` comment at route registration site.
- `// TODO(v0.2): rename /api/v1/workflows to /api/v1/workflows/runs when definition endpoints are added` comment.
- `// TODO(v0.2): per-request timeout middleware — see RFC 0002 H3` comment.

#### Tests

- **Middleware**: `requestIDMiddleware` generates UUID, does not echo client-provided header; `recoveryMiddleware` catches panic and returns `500 INTERNAL` JSON; `loggingMiddleware` captures status code.
- **Helpers**: `writeJSON` produces correct Content-Type and body; `writeError` produces correct error envelope; `requireJSON` rejects non-JSON Content-Type; `decodeBody` rejects oversized body (`400`), unknown fields (`400`), malformed JSON (`400`). Note: `requireJSON` is tested at the helper level in this PR; handler-level integration tests for Content-Type enforcement land in PRs 2 and 3 (F-05).
- **Middleware ordering (F-13)**: Panic in handler returns `500` JSON error **with** `X-Request-ID` header — validates that recovery wraps requestID wraps logging correctly.
- **Path traversal**: `resolveWorkflowPath` rejects `../etc/passwd`, URL-encoded traversal, empty ID, single-char ID; accepts valid `feature-builder` ID pointing to existing file.
- **Healthz**: `GET /healthz` returns `200` with `{"status": "ok"}`.
- **Server construction**: `New` rejects missing directory, non-directory path, nil logger fallback.
- Race detector (`-race` flag).

#### PR checklist

- [x] `go test ./internal/server/... -v -cover` passes (48/48 tests, 86.5% coverage)
- [x] Coverage ≥ 80% (achieved: 86.5%)
- [x] `go vet ./internal/server/...` clean
- [x] `go build ./cmd/orchestrator` succeeds
- [x] Path traversal tests cover `../`, symlink escape, valid resolution
- [x] `X-Request-ID` always server-generated (never echoed from client)
- [x] JSON error envelope consistent across all error responses
- [x] Transport-level timeouts configured (ReadHeader 10s, Read 30s, Idle 120s)
- [x] Run ID UUID format validation on GET/DELETE endpoints
- [x] Error messages sanitized (no filesystem paths, step IDs, or struct names leaked)

#### Post-merge review findings (PR #14)

PR #14 was submitted as 1,453 lines (654 implementation + 799 tests), exceeding the 500-line limit. Size waiver justified: PRs 1+2 were combined because handler integration tests require the scaffold, and 55% of lines are tests. Single-package, single-author scope. Full review: `docs/pr-reviews/pr-14-deep-review-round2.md` (not committed).

The PR went through 5 rounds of review fixes (commits `3783703` through `efae02d`) addressing all must-fix and should-fix findings from the initial deep review.

**Should Fix findings (carry-forward):**

| Finding | Severity | Description | Disposition |
|---------|----------|-------------|-------------|
| F-01: Store internal-error paths untested | Medium | `handleGetWorkflowStatus` (75%), `handleListWorkflows` (66.7%), `handleDeleteWorkflow` (59.1%) have `500 INTERNAL` branches for non-sentinel store errors. In-memory store never returns these, but they represent v0.2 SQLite failure mode. | Address in PR 3 or PR 4 — introduce a `failingStore` wrapper to test 500 paths |
| F-02: No mixed concurrent stress test | Medium | `TestConcurrentAccess` only exercises POST (20 goroutines). No concurrent submit+read+delete test. | Address in PR 4 (wiring) — add mixed-operation concurrent test |
| F-03: Duplicate log field construction | Low | `loggingMiddleware` has 12 lines of near-identical code in Warn/Info branches. | Address in PR 4 (wiring) or follow-up |

**Consider findings (no immediate action required):**

| Finding | Severity | Description | Disposition |
|---------|----------|-------------|-------------|
| F-04: Empty Steps map allocation per response | Low | `make(map[string]any)` always empty in v0.1 | No action — becomes necessary with RFC 0003 |
| F-05: Sleep-based sync in shutdown test | Low | 100ms sleep is documented flake risk | Track for v0.2 (`Start()` → accept `net.Listener`) |
| F-06: No post-Shutdown timeout on `<-errCh` | Low | Blocks forever if `ListenAndServe` doesn't return | Non-issue in practice — `Shutdown` closes listener |
| F-07: No `created_at` field in DTOs | Low | `started_at` carries "submitted at" semantics | Documented in RFC 0002 I-03; defer to RFC 0003 |
| F-08: Regex pattern exposed in error message | Low | Validation rule visible to API callers | Acceptable for v0.1 developer experience |
| F-09: Redundant regex check in `resolveWorkflowPath` | Low | Defense-in-depth (handler already validates) | Correct — keep for security-critical code |
| F-10: No Flush() test with non-Flusher writer | Low | Only tested with `httptest.NewRecorder` | Address in PR 3 or follow-up |
| F-11: Phases 2–4 follow-up tracking | Low | Ensure remaining phases tracked | ✅ Tracked in this plan |

---

### ~~PR 2~~ (merged into PR #14 above)

Original plan for workflow handlers was combined into PR #14. See above.

> **Note**: If this PR exceeds 500 lines during implementation, the `DELETE` handler and its running-status protection tests can be split into a follow-up PR within the same branch.

#### Scope

| File | Change |
|------|--------|
| `internal/server/workflow_handlers.go` | New — `handleSubmitRun`, `handleGetRunStatus`, `handleListRuns`, `handleDeleteRun` handler methods |
| `internal/server/server.go` | Update route registration to point to real workflow handler methods (replace temporary 501 placeholders) |
| `internal/server/server_test.go` | Extended — workflow handler tests using `httptest` |
| `internal/server/testdata/` | Test workflow YAML fixtures (valid workflow, invalid YAML for parse-error tests) |

#### Key implementation details

- **`POST /api/v1/workflows/run`**: Decode via `decodeBody`, validate `workflow_id` regex (`^[a-z0-9][a-z0-9-]*[a-z0-9]$`), call `resolveWorkflowPath`, call `planner.Parse` → `422` on failure, call `planner.ValidateDAG` → `422` on cycle, construct `state.WorkflowRun{Status: state.RunPending, StartedAt: time.Now()}`, call `store.CreateRun` → return `201` with `run_id`, `workflow_id`, `status`.
- **Input type enforcement**: `WorkflowRun.Inputs` is `map[string]string`; `DisallowUnknownFields` + strict decoder rejects non-string values automatically.
- **`GET /api/v1/workflows/{id}/status`**: Extract `{id}` via `r.PathValue("id")` (Go 1.22+), `store.GetRun` → `404` on `ErrRunNotFound`, return `200` with DTO (snake_case fields, `*time.Time` for `finished_at`).
- **`GET /api/v1/workflows`**: `store.ListRuns` → `200` with JSON array. Empty list returns `[]`, not `null`. `// TODO(v0.2): add pagination` comment.
- **`DELETE /api/v1/workflows/{id}`**: `store.GetRun` → `404`, check `RunRunning` → `409 CONFLICT`, `store.DeleteRun` → `204`. `// TODO(v0.3): atomic check-and-delete or store-level status guard` comment for TOCTOU race (H1). `// TODO(spec-sync): update ai-agents-orchestration-spec.md §8.3` comment.
- **`runStatusString`**: Maps `state.RunPending` → `"pending"`, `RunRunning` → `"running"`, `RunCompleted` → `"completed"`, `RunFailed` → `"failed"`, `RunCancelled` → `"cancelled"`.
- Note: `planner.Plan()` is NOT called at submission time — deferred to RFC 0003.

#### Tests

- **Submit run**: valid request → `201` with `run_id`/`workflow_id`/`status:"pending"`; missing `workflow_id` → `400`; empty `workflow_id` → `400` (T-01); invalid format (uppercase, special chars) → `400`; non-existent workflow file → `404` (T-04); non-string input value → `400`; oversized body → `400`.
- **Get status**: valid run ID → `200` with correct fields; non-existent ID → `404`; `steps` is empty object `{}` for v0.1.
- **List runs**: empty → `200` with `[]` (T-06); after submitting runs → array of run objects.
- **Delete run**: valid pending run → `204`; non-existent → `404`; running run → `409 CONFLICT`.
- **Lifecycle integration**: POST → `201`; GET status → `200`/`"pending"`; DELETE → `204`; GET again → `404`.
- **Content-Type enforcement**: POST with `text/plain` → `400`.
- **Unknown fields**: POST with extra field → `400`.
- **Malformed JSON**: POST with `{invalid}` → `400`.
- **Empty body**: POST with empty body → `400`.
- Race detector (`-race` flag).

#### PR checklist

✅ All items completed — merged into PR #14 (see above).

---

### PR 3: `feature/v01-agent-handlers` — Agent Registry Endpoints

**Depends on**: PR #14 merged. Uses `helpers.go` and agent response DTOs from PR #14.
**Branch**: `feature/v01-agent-handlers`
**Estimated size**: ~400–550 lines (implementation + tests)

> **PR #14 carry-forward items for PR 3**: (1) Add `failingStore` test wrapper for 500-path coverage on workflow handlers (F-01, optional — can defer to PR 4). (2) Add test for `statusCapture.Flush()` with non-Flusher writer (F-10, optional).

#### Scope

| File | Change |
|------|--------|
| `internal/server/agent_handlers.go` | New — `handleRegisterAgent`, `handleListAgents`, `handleGetAgent`, `handleDeleteAgent` handler methods |
| `internal/server/server.go` | Update route registration to point to real agent handler methods (replace temporary 501 placeholders) |
| `internal/server/server_test.go` | Extended — agent handler tests using `httptest` |

#### Key implementation details

- **`POST /api/v1/agents/register`**: Decode via `decodeBody` + `requireJSON`, validate `id` against agent ID regex, validate `address` non-empty (T-02), construct `registry.AgentInfo{ID: req.ID, Address: req.Address, Capabilities: req.Capabilities, Status: registry.StatusHealthy}`, call `registry.Register` → `409` on `ErrAgentAlreadyRegistered`, return `201` with agent JSON.
- **`GET /api/v1/agents`**: `registry.List` → `200` with JSON array.
- **`GET /api/v1/agents/{id}`**: `registry.Get` → `404` on `ErrAgentNotFound`, return `200`.
- **`DELETE /api/v1/agents/{id}`**: `registry.Unregister` → `404` on `ErrAgentNotFound`, return `204`.
- No `model`, `name`, or `role` in registration request (intentional v0.1 divergence — runtime agents provide minimum for gRPC dispatch).

#### Tests

- **Register**: valid agent → `201` with status `"healthy"`; missing `id` → `400`; invalid `id` format → `400`; empty `address` → `400` (T-02); duplicate registration → `409 CONFLICT`.
- **List agents**: empty → `200` with `[]`; after registering → array of agent objects.
- **Get agent**: valid → `200`; non-existent → `404`.
- **Delete agent**: valid → `204`; non-existent → `404`.
- **Lifecycle integration**: register → `201`; get → `200`; delete → `204`; get again → `404`.
- **Re-registration**: register → delete → register same ID → `201`.
- **Content-Type enforcement**: POST with wrong Content-Type → `400`.
- **Unknown fields**: POST with extra field → `400`.
- Race detector (`-race` flag).

#### PR checklist

- [x] `go test ./internal/server/... -v -cover` passes (75/75 tests, 90.5% coverage)
- [x] Coverage ≥ 80% (achieved: 90.5%)
- [x] Agent ID validated against `^[a-z0-9][a-z0-9-]*[a-z0-9]$`
- [x] Registered agent status is `StatusHealthy` (not zero-value `StatusUnknown`)
- [x] `go vet ./internal/server/...` clean
- [x] `go build ./cmd/orchestrator` succeeds
- [x] Defense-in-depth ID validation on GET/DELETE path parameters
- [x] `failingRegistry` test wrapper covers all 4 handler 500 paths (carry-forward from PR #14 F-01)
- [x] 458 lines (within 500-line limit)

#### Post-merge review findings (PR #16)

PR #16 was submitted as ~550 lines (141 implementation + 50 types + 10 routes + 350 tests). Actual size: 458 lines of meaningful change. Full review: `docs/pr-reviews/pr-016-deep-review.md` (not committed).

The PR went through 1 round of review fixes (commit `address PR #16 review findings`) addressing all medium-severity findings from the initial deep review.

**Should Fix findings (carry-forward to PR 4 or follow-up):**

| Finding | Severity | Description | Disposition |
|---------|----------|-------------|-------------|
| F-01: No address max-length validation | Medium | `address` field accepts arbitrary non-empty strings up to ~1 MiB (bounded by `decodeJSON`). Could pollute registry with nonsensical addresses and bloat logs. Recommend `len(req.Address) > 253` check. | Address in PR 4 or follow-up |
| F-02: Weak list assertion in `TestListAgentsWithRegistrations` | Low | Test asserts `Len(t, list, 2)` but doesn't verify returned entries contain expected IDs. Set-based assertion would be more thorough. | Address in PR 4 or follow-up |
| F-03: No `TestRegisterAgentContentTypeWithCharset` | Low | Workflow endpoint has `TestSubmitWorkflowRunContentTypeWithCharset` but no equivalent for agent registration. Code path covered through shared `requireJSON`, but symmetry preferred. | Address in PR 4 or follow-up |
| F-04: `workflowIDRegex` name misleading for agent ID validation | Low | Shared regex validates both workflow and agent IDs — name suggests workflow-only. Consider renaming to `resourceIDRegex` or `entityIDRegex`. | Separate follow-up PR (touches planner package + all references) |

**Nice to Have findings (no immediate action required):**

| Finding | Severity | Description | Disposition |
|---------|----------|-------------|-------------|
| F-05: No mixed concurrent read/write test | Low | `TestConcurrentAgentAccess` only does concurrent registrations. A mixed register+get+list+delete test would be more realistic. | Combine with PR #14 F-02 in PR 4 |
| F-06: No capability string validation | Low | Individual capability strings not validated (no regex, max length, max count). Acceptable since capabilities aren't used for authorization in v0.1. | Defer to v0.2 |
| F-07: No `TestRegisterAgentMalformedJSON` | Informational | Code path covered via shared `decodeJSON` helper; test symmetry would be nice. | Optional follow-up |
| F-08: No dedicated API reference documentation | Informational | RFC serves as current reference; OpenAPI spec would help CLI/agent implementors. | Defer to v0.2 |

**PR #14 carry-forward status:**

| Item | Source | Status |
|------|--------|--------|
| F-01: `failingStore` for 500-path coverage | PR #14 | ✅ Addressed as `failingRegistry` pattern — extend to `failingStore` in PR 4 |
| F-10: `statusCapture.Flush()` with non-Flusher writer | PR #14 | Deferred to PR 4 or follow-up |

---

### PR 4 = PR #17: `feature/v01-server-wiring` — Stub Endpoints + Wire into main.go + Docker Fix

**Depends on**: PR #14 and PR #16 merged
**Branch**: `feature/v01-server-wiring`
**Estimated size**: ~250–400 lines (implementation + tests)
**Actual size**: 440 lines (77 implementation + 363 tests)

> **PR #14 carry-forward items for PR 4**: (1) Add `failingStore` wrapper and test 500 paths for `handleGetWorkflowStatus`, `handleListWorkflows`, `handleDeleteWorkflow` (F-01, Medium — biggest coverage gap, raises `handleDeleteWorkflow` from 59.1% to ~90%+). (2) Add mixed concurrent stress test: submit + read + delete simultaneously with `-race` (F-02, Medium). (3) Deduplicate log field construction in `loggingMiddleware` (F-03, Low).
>
> **PR #16 carry-forward items for PR 4**: (1) Add address max-length validation (`len(req.Address) > 253`) in `handleRegisterAgent` (F-01, Medium). (2) Add set-based ID assertion in `TestListAgentsWithRegistrations` (F-02, Low). (3) Add `TestRegisterAgentContentTypeWithCharset` for parity with workflow test suite (F-03, Low). (4) Add mixed concurrent agent test: register + get + list + delete simultaneously (F-05, Low — combine with PR #14 F-02).

#### Scope

| File | Change |
|------|--------|
| `internal/server/stub_handlers.go` | New — `handleGetLogs`, `handleGetCostSummary` returning `501 NOT_IMPLEMENTED` JSON |
| `internal/server/server.go` | Update route registration to point to real stub handlers (replace temporary 501 placeholders for logs/cost) |
| `internal/server/server_test.go` | Extended — stub endpoint tests, start/shutdown integration tests |
| `cmd/orchestrator/main.go` | Add `--http-bind` flag (default `"127.0.0.1"`), `--workflows-dir` flag (default `"workflows/"`). Import `internal/server`. **Also instantiate** `state.NewInMemoryStore()`, `registry.NewInMemoryRegistry()`, and planner as local variables — these do not yet exist in `main.go` (F-01: the TODO block at steps 3/6/8 is still unimplemented). Instantiate `server.New(listenAddr, *workflowsDir, store, reg, pl, logger)`. Launch `srv.Start(ctx)` in goroutine with error → `cancel()` propagation. Log `"HTTP server listening"` with address. |
| `docker-compose.yaml` | Add `command: ["--http-bind", "0.0.0.0"]` to orchestrator service so it's reachable from agent containers over the Docker network (M3) |

#### Key implementation details

- **Stub handlers**: `stub_handlers.go` provides **named** handler functions that return `501` with `{"error": "not implemented in v0.1", "code": "NOT_IMPLEMENTED"}`. These replace the **inline** anonymous 501 returns registered as placeholders in PR 1 — behavior is unchanged; this is a code-organization refactor (F-04).
- **main.go wiring**: Uses structured zap logger (not sugar) for new code. Does NOT migrate existing sugar logger calls (separate cleanup PR per MI-04). `server.New` error → `logger.Fatal`. Start goroutine error → `logger.Error` + `cancel()` (not `logger.Fatal` to avoid bypassing deferred cleanup). `// TODO(v0.2): propagate Start error via errCh for non-zero exit code` comment (D-01). `// TODO(cleanup): migrate main.go sugar logger to structured zap` comment.
- **Flag wiring**: `--http-bind` + `--http-port` (existing) formatted as `fmt.Sprintf("%s:%d", *httpBind, *httpPort)` for `listenAddr`. Satisfies TODO step 11 in main.go.
- **Docker fix**: Pass `--http-bind 0.0.0.0` so the orchestrator is reachable across the Docker network. The default `127.0.0.1` bind only accepts loopback connections, making the orchestrator invisible to agent containers.

#### Tests

- **Stubs**: `GET /api/v1/executions/any-id/logs` → `501`; `GET /api/v1/cost/summary` → `501`.
- **Build smoke**: `go build ./cmd/orchestrator` succeeds after wiring.
- **Start error propagation**: `Start(ctx)` on already-bound port → goroutine logs error and cancels context.
- **Graceful shutdown**: start server, cancel context, verify `Start` returns within 1 second.
- **Method Not Allowed**: `PUT /api/v1/workflows/run` → `405` (Go 1.22+ automatic). Note: plain-text body, not JSON envelope (I-02).
- **Concurrent access**: multiple goroutines hitting endpoints simultaneously; run with `-race`.
- Race detector (`-race` flag).

#### PR checklist

- [x] `go test ./internal/server/... -v -cover` passes (90/90 tests, 96.7% coverage)
- [x] Coverage ≥ 80% (achieved: 96.7%)
- [x] `go build ./cmd/orchestrator` succeeds
- [x] `go vet ./cmd/orchestrator/... ./internal/server/...` clean
- [x] Binary starts, logs `"HTTP server starting"`, and shuts down cleanly with SIGINT
- [x] `docker-compose.yaml` passes `--http-bind 0.0.0.0` to orchestrator
- [x] All `// TODO` markers present per RFC 0002 requirements
- [x] `failingStore` wrapper covers 5 workflow handler 500-paths (PR #14 carry-forward F-01)
- [x] Mixed concurrent stress tests for workflows and agents (PR #14 F-02, PR #16 F-05)
- [x] Logging field deduplication in `loggingMiddleware` (PR #14 F-03)
- [x] Address max-length validation 253 chars (PR #16 F-01)
- [x] Set-based list assertion (PR #16 F-02)
- [x] Charset=utf-8 Content-Type test for agents (PR #16 F-03)
- [x] 440 lines (within 500-line limit)

#### Post-merge review findings (PR #17)

PR #17 was submitted as 440 lines (77 implementation + 363 tests). Full review: `docs/pr-reviews/pr-17-deep-review.md` (not committed).

The PR went through 1 round of review fixes (commit `5691916`) addressing high and medium findings from the initial deep review.

**Should Fix findings (carry-forward to follow-up PR):**

| Finding | Severity | Description | Disposition |
|---------|----------|-------------|-------------|
| F-01: `panic()` for `--env` validation | Medium | Produces a full goroutine stack trace on stderr for a mistyped flag. Logger isn't initialized yet, so `logger.Fatal()` unavailable. Use `fmt.Fprintln(os.Stderr, msg)` + `os.Exit(1)` instead. | Follow-up PR |
| F-02: No readiness signal from HTTP server | Medium | `logger.Info("HTTP server starting")` fires before `net.Listen` completes. No mechanism to confirm port is ready. Matters for integration tests and Docker healthcheck timing. | v0.2 — add `readyCh chan struct{}` to `Start()` |
| F-03: No `--http-bind` format validation | Medium | Accepts arbitrary strings; invalid values produce cryptic `net.Listen` errors. Not a security concern (CLI flags are trusted), but a usability gap. | Consider — `net.Listen` already fails descriptively |
| F-04: Mixed concurrent tests lack DELETE goroutines | Medium | `TestMixedConcurrentWorkflowAccess` and `TestMixedConcurrentAgentAccess` exercise create/read/list but not delete. TOCTOU delete path untested under concurrency. | Follow-up PR |

**Nice to Have findings (no immediate action required):**

| Finding | Severity | Description | Disposition |
|---------|----------|-------------|-------------|
| F-05: `ENVIRONMENT=development` env var unused | Low | `docker-compose.yaml` sets env var but `main.go` reads `--env` flag only. Misleading for operators. | Document or remove in follow-up |
| F-06: Docker `command` relies on flag defaults for ports | Low | `--http-port 8080` and `--port 9090` not explicit in command; implicit coupling with defaults. | Consider making explicit |
| F-07: Stub handlers ignore `{id}` path parameter | Low | No format validation on execution ID. Add TODO comment for RFC 0003 implementation. | Optional |
| F-08: Non-deterministic log ordering | Low | "HTTP server starting" may log after Start goroutine errors. Extremely unlikely in practice. | Accepted |
| F-09: Logging `fields` slice escapes to heap | Nice to Have | `fields...` spread in refactored middleware causes heap allocation. Negligible perf impact. | No action |
| F-10: `failingStore` uses string matching for method selection | Nice to Have | `failOn == "GetRun"` is brittle if method renamed. Consistent with `failingRegistry` pattern. | No action — consistency wins |
| F-11: Sugar vs structured logger inconsistency in main.go | Nice to Have | `log.Infow` (sugar) and `logger.Info` (structured) mixed in same file. Both write to same sink. | No action — separate cleanup per MI-04 |

**PR #14 + #16 carry-forward status:**

| Item | Source | Status |
|------|--------|--------|
| F-01: `failingStore` for 500-path coverage | PR #14 | ✅ Addressed — 5 tests cover GetRun, ListRuns, DeleteRun, CreateRun, GetRun-in-delete |
| F-02: Mixed concurrent stress test | PR #14 | ✅ Addressed — `TestMixedConcurrentWorkflowAccess` (30 goroutines) |
| F-03: Duplicate log field construction | PR #14 | ✅ Addressed — shared `fields` slice in `loggingMiddleware` |
| F-01: Address max-length validation | PR #16 | ✅ Addressed — `len(req.Address) > 253` check |
| F-02: Set-based list assertion | PR #16 | ✅ Addressed — `TestListAgentsWithRegistrationsSetBased` |
| F-03: Charset Content-Type test | PR #16 | ✅ Addressed — `TestRegisterAgentContentTypeWithCharset` |
| F-05: Mixed concurrent agent test | PR #16 | ✅ Addressed — `TestMixedConcurrentAgentAccess` (30 goroutines) |
| F-10: `statusCapture.Flush()` with non-Flusher writer | PR #14 | Deferred — not addressed in PR #17 (low priority) |

---

### PR 5: `feature/v01-rfc0002-followup` — Review Findings Cleanup

**Depends on**: PR #17 merged
**Branch**: `feature/v01-rfc0002-followup`
**Estimated size**: ~80–120 lines (implementation + tests)

This is the final cleanup PR that addresses all accumulated should-fix and low-severity findings from PRs #14, #16, and #17. Closes out RFC 0002 with no carry-forward items remaining.

#### Scope

| File | Change |
|------|--------|
| `cmd/orchestrator/main.go` | Replace `panic()` with `fmt.Fprintln(os.Stderr, msg)` + `os.Exit(1)` for `--env` validation (PR #17 F-01) |
| `internal/server/server_test.go` | Add DELETE goroutine groups to `TestMixedConcurrentWorkflowAccess` and `TestMixedConcurrentAgentAccess` (PR #17 F-04) |
| `internal/planner/planner.go` | Rename `WorkflowIDRegex` → `ResourceIDRegex` (PR #16 F-04) |
| `internal/server/agent_handlers.go` | Update `WorkflowIDRegex` → `ResourceIDRegex` import reference |
| `internal/server/server.go` | Update `WorkflowIDRegex` → `ResourceIDRegex` import reference |
| `internal/planner/planner_test.go` | Update test references if any |
| `docker-compose.yaml` | Remove unused `ENVIRONMENT=development` env var; add explicit `--http-port 8080` and `--port 9090` to command (PR #17 F-05, F-06) |
| `internal/server/stub_handlers.go` | Add `// TODO(v0.3): validate execution ID format before querying` comment (PR #17 F-07) |

#### Key implementation details

- **`panic()` → clean exit**: At the point of `--env` validation, the zap logger hasn't been constructed yet, so `logger.Fatal()` is not an option. `fmt.Fprintln(os.Stderr, ...)` + `os.Exit(1)` produces a clean single-line error matching `flag.Parse()` behavior.
- **DELETE in concurrent tests**: Add ~5 goroutines per test that delete pre-created runs/agents. Accept `204` (success) or `404` (already deleted by another goroutine) as valid outcomes — this exercises the TOCTOU race path in `handleDeleteWorkflow` and concurrent unregister in agent handlers.
- **`ResourceIDRegex` rename**: The regex `^[a-z0-9][a-z0-9-]*[a-z0-9]$` validates both workflow IDs and agent IDs. The current name `WorkflowIDRegex` is misleading since PR #16 reused it for agent ID validation. Rename to `ResourceIDRegex` with an explanatory comment.
- **Docker command cleanup**: Make port flags explicit (`--http-port 8080`, `--port 9090`) so the Docker deployment is self-documenting and decoupled from Go flag defaults.

#### Tests

- **`--env` validation**: Manual smoke test — `go run ./cmd/orchestrator --env invalid` prints clean error and exits with code 1 (no stack trace).
- **Concurrent DELETE workflows**: ~5 goroutines delete pre-seeded runs while 10 goroutines create and 10 read/list concurrently. Assert `204` or `404` for deletes. Run with `-race`.
- **Concurrent DELETE agents**: Same pattern — ~5 goroutines unregister while others register/get/list. Assert `204` or `404`. Run with `-race`.
- **Existing tests pass**: All 90+ server tests continue to pass after regex rename.
- Race detector (`-race` flag).

#### PR checklist

- [ ] `go test ./internal/... -v -cover` passes
- [ ] `go vet ./... ` clean
- [ ] `go build ./cmd/orchestrator` succeeds
- [ ] `--env invalid` produces clean single-line error (no stack trace)
- [ ] Mixed concurrent tests include DELETE goroutines
- [ ] `WorkflowIDRegex` → `ResourceIDRegex` rename compiles across all packages
- [ ] `docker-compose.yaml` has explicit port flags and no unused env vars
- [ ] Branch: `feature/v01-rfc0002-followup`
- [ ] Squash-merge ready

#### Findings addressed

| Source | Finding | Severity | Action |
|--------|---------|----------|--------|
| PR #17 F-01 | `panic()` for `--env` validation | Medium | Replace with `fmt.Fprintln` + `os.Exit(1)` |
| PR #17 F-04 | Mixed concurrent tests lack DELETE | Medium | Add DELETE goroutine groups |
| PR #16 F-04 | `workflowIDRegex` name misleading | Low | Rename to `ResourceIDRegex` |
| PR #17 F-05 | `ENVIRONMENT` env var unused | Low | Remove from docker-compose |
| PR #17 F-06 | Docker port flags implicit | Low | Add explicit `--http-port`/`--port` |
| PR #17 F-07 | Stub handlers ignore `{id}` format | Low | Add TODO comment for v0.3 |
| PR #14 F-10 | `statusCapture.Flush()` non-Flusher | Low | Deferred — defense-in-depth; `httptest.NewRecorder` implements `Flusher` |

---

## Dependency Graph

```
Original plan:
  PR 1 (scaffold + middleware)
      ├──→ PR 2 (workflow handlers) ──┐
      └──→ PR 3 (agent handlers)   ──┼──→ PR 4 (stubs + wiring + docker)

Actual execution:
  PR #14 (scaffold + middleware + workflow handlers)  ──┐
                                                        ├──→ PR #17 (stubs + wiring + docker)  ✅
  PR #16 (agent handlers)  ─────────────────────────────┘
                                                              │
                                                              └──→ PR 5 (review findings cleanup)
```

PR #14 (combined PRs 1+2) landed first, creating the `internal/server/` package with all shared infrastructure and workflow handlers. PR #16 (agent handlers) builds on PR #14's helpers, middleware, and test patterns. PR #17 depends on both PR #14 and PR #16 being merged. PR 5 sweeps all remaining review findings to close out RFC 0002.

**RFC 0002 endpoint implementation is complete.** All 11 endpoints registered, `main.go` TODO step 11 satisfied, Docker networking fixed. Total: 90 server tests, 96.7% coverage. PR 5 addresses accumulated code quality findings only — no new functionality.

---

## Carry-Forward Items from RFC 0001

These findings from RFC 0001 PR reviews are addressed or relevant within RFC 0002:

| RFC 0001 Finding | Source | RFC 0002 Action |
|------------------|--------|-----------------|
| F-01: No agent ID validation in `Register` | PR #7 (registry) | Validated at HTTP handler boundary in PR 3 (`handleRegisterAgent`) |
| F-03: nil vs empty slice in `FindByCapability` vs `List` | PR #7 (registry) | ✅ Already addressed — `List()` uses `make([]T, 0, len(map))` which produces `[]` in JSON. No RFC 0002 endpoint exposes `FindByCapability`. Agent/workflow list handlers must likewise initialize response slices with `make` (F-08). |
| F-04: No `RunStatus.String()` | PR #6 (state) | `runStatusString` helper in `types.go` maps `RunStatus` → lowercase string for JSON |
| F-05: No `AgentStatus.String()` | PR #7 (registry) | Not directly needed for RFC 0002 (agent JSON uses raw struct); defer to follow-up |
| F-05: No timestamp management in `UpdateRunStatus` | PR #6 (state) | `StartedAt` set at submission time (I-03); document `CreatedAt` gap for RFC 0003 |
| F-01: No nil-logger guard in `ResolveInputs` | PR #9 (resolve) | Not called by RFC 0002 handlers; defer to RFC 0003 |

---

## Risk Mitigation

| Risk | Mitigation | Status |
|------|------------|--------|
| PR 1 (scaffold) exceeds 500 lines with middleware + helper tests | Split: middleware + helpers + server core in PR 1; all handler logic in PRs 2–3. If still over, move `resolveWorkflowPath` + tests to PR 2, or split into PR 1a (server + types + middleware) and PR 1b (helpers + path resolution + healthz + tests). RFC 0001 showed 73–138% overrun on PRs with tests (F-03); PR 1 is the highest-risk PR for size. | ✅ Resolved — PRs 1+2 combined into PR #14 (1,453 lines; waiver accepted — 55% tests) |
| PR 2 (workflow handlers) exceeds 500 lines | Split DELETE handler + running-status tests into a follow-up PR 2b if needed. RFC 0001 pattern suggests this PR will likely reach 550–800 lines (F-03). | ✅ Resolved — combined into PR #14 |
| Path traversal logic requires filesystem fixtures in tests | Use `t.TempDir()` for test workflow directories — clean, hermetic, no repo-path dependency. | ✅ Done in PR #14 |
| `resolveWorkflowPath` symlink test requires OS support | Use `os.Symlink` in test; skip on platforms where symlinks are unsupported (`t.Skip`). | ✅ Done in PR #14 |
| `docker-compose.yaml` change in PR 4 requires coordination | Change is additive (`command:` field); no conflict with parallel work. | ✅ Done in PR #17 |
| PRs 2 and 3 both modify `server_test.go` | Merge PR 2 before PR 3 (or vice versa); second PR rebases. Minimal conflict since test functions are independent (workflow tests vs agent tests). | ✅ Resolved — PR 2 merged into PR #14; PR 3 rebases on PR #14 |
| `main.go` has sugar logger but RFC convention requires structured zap | New code in PR 4 uses structured zap; existing sugar calls left untouched per MI-04. No mixing within the same log call. | ✅ Done in PR #17 — structured zap used for new code; sugar retained for startup message |
| Empty `ListRuns` / `List` returns `null` JSON instead of `[]` | DTO conversion must initialize slices: `result := make([]RunStatusResponse, 0, len(runs))` to produce `[]` in JSON. Test explicitly. | ✅ Done in PR #14 |
| `go.mod` conflicts if RFC 0002 PRs interleave with other work | No new `go.mod` dependencies expected — `google/uuid` already present from RFC 0001. Minimal conflict risk. | ✅ Confirmed — no new deps in PR #14 |
| PR #14 F-01: Store internal-error paths (500) untested | Introduce a `failingStore` wrapper that returns `errors.New("db error")` for specific methods; verify 500 JSON envelope | ✅ Addressed in PR #17 — `failingStore` covers 5 handler 500-paths |
| PR #14 F-02: No mixed concurrent stress test | Add test with ~30 goroutines: submit + read + delete concurrently with `-race` | ✅ Addressed in PR #17 — `TestMixedConcurrentWorkflowAccess` (30 goroutines, create+read+list) |
| PR #16 F-01: Address max-length validation missing | `handleRegisterAgent` accepts arbitrarily long address strings. Add `len(req.Address) > 253` check. | ✅ Addressed in PR #17 |
| PR #16 F-04: `workflowIDRegex` name misleading | Shared regex used for both workflow and agent IDs; name suggests workflow-only. Rename to `ResourceIDRegex`. | PR 5 (follow-up) |
| PR #17 F-01: `panic()` for `--env` validation | `panic()` produces stack trace. Use `fmt.Fprintln + os.Exit(1)` for clean error output. | PR 5 (follow-up) |
| PR #17 F-04: Mixed concurrent tests lack DELETE | TOCTOU delete path untested under concurrency. Add DELETE goroutine group. | PR 5 (follow-up) |

---

## CI Validation

Each PR must pass the full CI pipeline (`.github/workflows/ci.yml`):

- `go build ./cmd/orchestrator` — binary compiles
- `go vet ./internal/... ./cmd/orchestrator/...` — static analysis (F-16)
- `go test ./internal/... -v -race -cover` — unit tests with race detector

No Python or Rust changes in RFC 0002 — only Go CI gates apply.

Run `make lint` locally before opening each PR (not yet enforced in CI).
