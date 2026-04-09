# RFC 0002 — PR Implementation Plan

**RFC**: [0002-rest-api-server.md](0002-rest-api-server.md)
**Created**: 2026-04-09
**Branch prefix**: `feature/v01-`
**Target**: `main`
**Merge strategy**: Squash merge per [BRANCHING.md](../BRANCHING.md)

---

## Overview

RFC 0002 defines ~420 LOC across 4 phases. The project's PR size limit is <500 lines of meaningful change. Phases 1 and 2 together (~370 LOC implementation + ~400–600 LOC tests) will exceed the limit. This plan splits the work into **4 PRs**: Phase 1 is split into two PRs at the middleware/handler boundary (server scaffolding + middleware vs. workflow handlers), Phase 2 adds agent handlers, and Phase 3+4 are combined into a single PR (stubs + wiring are both small).

Each PR is independently mergeable and leaves the codebase in a compilable, test-passing state.

**Prerequisite**: All RFC 0001 PRs merged (state, registry, planner). ✅ Confirmed — `internal/state/`, `internal/registry/`, `internal/planner/` have full implementations. `github.com/google/uuid` already in `go.mod`.

---

## PR Sequence

### PR 1: `feature/v01-server-scaffold` — Server Scaffolding + Middleware + Helpers

**Depends on**: RFC 0001 fully merged (all 5 PRs)
**Branch**: `feature/v01-server-scaffold`
**Estimated size**: ~350–450 lines (implementation + tests)

#### Scope

| File | Change |
|------|--------|
| `internal/server/server.go` | New — `Server` struct, `New` constructor (validates + canonicalizes `workflowsDir`), `Handler()` (returns composed `http.Handler`), `Start(ctx)` with graceful shutdown, `resolveWorkflowPath` (path traversal protection), route registration, minimal `GET /healthz` handler |
| `internal/server/types.go` | New — request/response DTOs: `SubmitRunRequest`, `SubmitRunResponse`, `RunStatusResponse`, `RegisterAgentRequest`, `ErrorResponse`; `*time.Time` for nullable timestamps (M-07); `runStatusString` helper mapping `state.RunStatus` → lowercase string |
| `internal/server/middleware.go` | New — `recoveryMiddleware` (with `runtime/debug.Stack()`), `requestIDMiddleware` (server-generated UUID only), `loggingMiddleware` (status capture wrapper), `contextKey` type for `SA1029` compliance |
| `internal/server/helpers.go` | New — `writeJSON`, `writeError`, `requireJSON` (Content-Type enforcement), `decodeBody` (wraps `MaxBytesReader` + strict decoder + `MaxBytesError` check) |
| `internal/server/server_test.go` | New — middleware tests, helper tests, `/healthz` test, `resolveWorkflowPath` tests (path traversal, symlink, valid ID) |

#### Key implementation details

- `New` returns `(*Server, error)`: validates `workflowsDir` via `os.Stat` + `IsDir()`, canonicalizes via `filepath.Abs` + `filepath.EvalSymlinks` (CS-02). Stores the canonical path so `resolveWorkflowPath` avoids repeated syscalls.
- `resolveWorkflowPath`: regex-validates `workflow_id` first, then `filepath.Join` + `filepath.EvalSymlinks` + `strings.HasPrefix` prefix check against canonical root. Returns `ErrWorkflowNotFound` for both traversal attempts and missing files (no information leakage).
- `Handler()` composes middleware in order: `recoveryMiddleware` → `loggingMiddleware` → `requestIDMiddleware` → `mux`.
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
- **Helpers**: `writeJSON` produces correct Content-Type and body; `writeError` produces correct error envelope; `requireJSON` rejects non-JSON Content-Type; `decodeBody` rejects oversized body (`400`), unknown fields (`400`), malformed JSON (`400`).
- **Path traversal**: `resolveWorkflowPath` rejects `../etc/passwd`, URL-encoded traversal, empty ID, single-char ID; accepts valid `feature-builder` ID pointing to existing file.
- **Healthz**: `GET /healthz` returns `200` with `{"status": "ok"}`.
- **Server construction**: `New` rejects missing directory, non-directory path, nil logger fallback.
- Race detector (`-race` flag).

#### PR checklist

- [ ] `go test ./internal/server/... -v -cover` passes
- [ ] Coverage ≥ 80%
- [ ] `go vet ./internal/server/...` clean
- [ ] `go build ./cmd/orchestrator` succeeds (no import of `server` yet — that's PR 4)
- [ ] Path traversal tests cover `../`, symlink escape, valid resolution
- [ ] `X-Request-ID` always server-generated (never echoed from client)

---

### PR 2: `feature/v01-workflow-handlers` — Workflow Run Endpoints

**Depends on**: PR 1 merged
**Branch**: `feature/v01-workflow-handlers`
**Estimated size**: ~350–500 lines (implementation + tests)

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

- [ ] `go test ./internal/server/... -v -cover` passes
- [ ] Coverage ≥ 80%
- [ ] Path traversal protection active on `POST /api/v1/workflows/run`
- [ ] JSON error envelope consistent across all error responses
- [ ] `go vet ./internal/server/...` clean
- [ ] `go build ./cmd/orchestrator` succeeds
- [ ] Test fixtures in `internal/server/testdata/`

---

### PR 3: `feature/v01-agent-handlers` — Agent Registry Endpoints

**Depends on**: PR 1 merged (PR 2 not required — agent handlers are independent)
**Branch**: `feature/v01-agent-handlers`
**Estimated size**: ~250–350 lines (implementation + tests)

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

- [ ] `go test ./internal/server/... -v -cover` passes
- [ ] Coverage ≥ 80%
- [ ] Agent ID validated against `^[a-z0-9][a-z0-9-]*[a-z0-9]$`
- [ ] Registered agent status is `StatusHealthy` (not zero-value `StatusUnknown`)
- [ ] `go vet ./internal/server/...` clean
- [ ] `go build ./cmd/orchestrator` succeeds

---

### PR 4: `feature/v01-server-wiring` — Stub Endpoints + Wire into main.go + Docker Fix

**Depends on**: PRs 1, 2, 3 merged
**Branch**: `feature/v01-server-wiring`
**Estimated size**: ~150–250 lines (implementation + tests)

#### Scope

| File | Change |
|------|--------|
| `internal/server/stub_handlers.go` | New — `handleGetLogs`, `handleGetCostSummary` returning `501 NOT_IMPLEMENTED` JSON |
| `internal/server/server.go` | Update route registration to point to real stub handlers (replace temporary 501 placeholders for logs/cost) |
| `internal/server/server_test.go` | Extended — stub endpoint tests, start/shutdown integration tests |
| `cmd/orchestrator/main.go` | Add `--http-bind` flag (default `"127.0.0.1"`), `--workflows-dir` flag (default `"workflows/"`). Import `internal/server`. Instantiate `server.New(listenAddr, *workflowsDir, store, reg, pl, logger)`. Launch `srv.Start(ctx)` in goroutine with error → `cancel()` propagation. Log `"HTTP server listening"` with address. |
| `docker-compose.yaml` | Add `command: ["--http-bind", "0.0.0.0"]` to orchestrator service so it's reachable from agent containers over the Docker network (M3) |

#### Key implementation details

- **Stub handlers**: Both return `501` with `{"error": "not implemented in v0.1", "code": "NOT_IMPLEMENTED"}`.
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

- [ ] `go test ./internal/server/... -v -cover` passes
- [ ] Coverage ≥ 80% for `internal/server/`
- [ ] `go build ./cmd/orchestrator` succeeds
- [ ] `go vet ./cmd/orchestrator/... ./internal/server/...` clean
- [ ] Binary starts, logs `"HTTP server listening"`, and shuts down cleanly with SIGINT
- [ ] `docker-compose.yaml` passes `--http-bind 0.0.0.0` to orchestrator
- [ ] All `// TODO` markers present per RFC 0002 requirements

---

## Dependency Graph

```
PR 1 (scaffold + middleware)
    ├──→ PR 2 (workflow handlers) ──┐
    └──→ PR 3 (agent handlers)   ──┼──→ PR 4 (stubs + wiring + docker)
                                    │
```

PR 1 must land first — it creates the `internal/server/` package and all shared infrastructure. PRs 2 and 3 can proceed in parallel after PR 1 (workflow handlers and agent handlers have no dependencies on each other). PR 4 depends on all others being merged.

---

## Carry-Forward Items from RFC 0001

These findings from RFC 0001 PR reviews are addressed or relevant within RFC 0002:

| RFC 0001 Finding | Source | RFC 0002 Action |
|------------------|--------|-----------------|
| F-01: No agent ID validation in `Register` | PR #7 (registry) | Validated at HTTP handler boundary in PR 3 (`handleRegisterAgent`) |
| F-03: nil vs empty slice in `FindByCapability` vs `List` | PR #7 (registry) | Ensure agent list endpoint returns `[]` not `null` for empty results |
| F-04: No `RunStatus.String()` | PR #6 (state) | `runStatusString` helper in `types.go` maps `RunStatus` → lowercase string for JSON |
| F-05: No `AgentStatus.String()` | PR #7 (registry) | Not directly needed for RFC 0002 (agent JSON uses raw struct); defer to follow-up |
| F-05: No timestamp management in `UpdateRunStatus` | PR #6 (state) | `StartedAt` set at submission time (I-03); document `CreatedAt` gap for RFC 0003 |
| F-01: No nil-logger guard in `ResolveInputs` | PR #9 (resolve) | Not called by RFC 0002 handlers; defer to RFC 0003 |

---

## Risk Mitigation

| Risk | Mitigation |
|------|------------|
| PR 1 (scaffold) exceeds 500 lines with middleware + helper tests | Split: middleware + helpers + server core in PR 1; all handler logic in PRs 2–3. If still over, move `resolveWorkflowPath` tests to PR 2. |
| PR 2 (workflow handlers) exceeds 500 lines | Split DELETE handler + running-status tests into a follow-up PR 2b if needed. |
| Path traversal logic requires filesystem fixtures in tests | Use `t.TempDir()` for test workflow directories — clean, hermetic, no repo-path dependency. |
| `resolveWorkflowPath` symlink test requires OS support | Use `os.Symlink` in test; skip on platforms where symlinks are unsupported (`t.Skip`). |
| `docker-compose.yaml` change in PR 4 requires coordination | Change is additive (`command:` field); no conflict with parallel work. |
| PRs 2 and 3 both modify `server_test.go` | Merge PR 2 before PR 3 (or vice versa); second PR rebases. Minimal conflict since test functions are independent (workflow tests vs agent tests). |
| `main.go` has sugar logger but RFC convention requires structured zap | New code in PR 4 uses structured zap; existing sugar calls left untouched per MI-04. No mixing within the same log call. |
| Empty `ListRuns` / `List` returns `null` JSON instead of `[]` | DTO conversion must initialize slices: `result := make([]RunStatusResponse, 0, len(runs))` to produce `[]` in JSON. Test explicitly. |
| `go.mod` conflicts if RFC 0002 PRs interleave with other work | No new `go.mod` dependencies expected — `google/uuid` already present from RFC 0001. Minimal conflict risk. |

---

## CI Validation

Each PR must pass the full CI pipeline (`.github/workflows/ci.yml`):

- `go build ./cmd/orchestrator` — binary compiles
- `go test ./internal/... -v -race -cover` — unit tests with race detector

Run `make lint` locally before opening each PR (not yet enforced in CI).
