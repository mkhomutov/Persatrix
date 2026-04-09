# RFC 0002 — REST API Server (HTTP Layer + Workflow Submission)

**Type**: architecture
**Status**: 📋 Proposed
**Author**: Orchestr8 team
**Date**: 2026-04-09
**Target**: v0.1 (MVP)
**Depends on**: RFC 0001
**Superseded by**: None

---

## Table of Contents

- [Summary](#summary)
- [Motivation](#motivation)
- [Goals](#goals)
- [Non-Goals](#non-goals)
- [Design / Implementation](#design--implementation)
- [Security Considerations](#security-considerations)
- [Phased Implementation Plan](#phased-implementation-plan)
- [Files Touched (Estimated)](#files-touched-estimated)
- [Test Strategy](#test-strategy)
- [Open Questions](#open-questions)
- [Decision / Next Steps](#decision--next-steps)
- [Related Documentation](#related-documentation)

---

## Summary

Implement the HTTP/REST API server for the Orchestr8 orchestrator. This RFC covers the v0.1 core endpoints: workflow run submission, run status polling, run deletion, agent registry CRUD, and skeleton endpoints for execution logs and cost summary. The server is wired into `cmd/orchestrator/main.go` as step 11 of the initialization sequence, completing the CLI-to-orchestrator communication path for manual workflow triggering.

## Motivation

RFC 0001 established the three foundational internal components (State, Registry, Planner) but deliberately deferred the HTTP layer. Without it, no external caller — including the Rust CLI — can submit a workflow run, query status, or manage agents. The orchestrator starts and shuts down cleanly but accepts no traffic.

The dependency chain now unblocks completely from the operator's perspective:

```
Rust CLI  ──REST──►  HTTP Server (this RFC)
                          │
                          ├─ POST /api/v1/workflows/run  ──►  YAMLPlanner + StateStore
                          ├─ GET  /api/v1/workflows/{id}/status  ──►  StateStore.GetRun
                          ├─ POST /api/v1/agents/register         ──►  Registry.Register
                          ├─ GET  /api/v1/agents                  ──►  Registry.List
                          ├─ GET  /api/v1/agents/{id}             ──►  Registry.Get
                          └─ DELETE /api/v1/agents/{id}          ──►  Registry.Unregister
```

If we do nothing, the orchestrator remains unreachable from the CLI or any HTTP client, and RFC 0003 (Scheduler/Executor) has nothing to integrate with for end-to-end testing.

## Goals

1. Implement an HTTP server in `internal/server/` using the Go standard library (`net/http`) with Go 1.22+ pattern routing (`http.NewServeMux`).
2. Expose the following v0.1 endpoints:
   - `POST   /api/v1/workflows/run` — submit a workflow run (parse YAML, create `WorkflowRun`, return run ID).
   - `GET    /api/v1/workflows/{id}/status` — poll status of a run by run ID.
   - `DELETE /api/v1/workflows/{id}` — delete a run; reject `Running`-status runs at the API layer.
   - `GET    /api/v1/workflows` — list all workflow runs.
   - `POST   /api/v1/agents/register` — register an agent into the in-memory registry.
   - `GET    /api/v1/agents` — list all registered agents.
   - `GET    /api/v1/agents/{id}` — get a single agent by ID.
   - `DELETE /api/v1/agents/{id}` — unregister an agent.
   - `GET    /api/v1/executions/{id}/logs` — stub returning `501 Not Implemented` (deferred to RFC 0003).
   - `GET    /api/v1/cost/summary` — stub returning `501 Not Implemented` (deferred to cost-tracker integration).
3. Enforce a consistent JSON error envelope across all endpoints.
4. Protect workflow file loading from path traversal attacks.
5. Wire the server into `cmd/orchestrator/main.go` (TODO step 11).
6. Achieve ≥ 80% test coverage for `internal/server/` (`go test -race -cover`).

## Non-Goals

- **Authentication / authorization.** No auth in v0.1. Document the gap explicitly; add a `// TODO(security): no auth in v0.1` marker at the handler registration site. Auth is deferred to a security RFC.
- **Rate limiting.** The `--http-port` flag and graceful shutdown are already wired; per-IP rate limiting is deferred (main.go TODO step 4 references `security` package initialization).
- **SSE streaming.** `GET /api/v1/stream/events` is a v0.2 endpoint. The server package must be designed to accommodate streaming handlers in future, but no streaming is implemented here.
- **TLS.** HTTP only for v0.1 (development/docker-compose use case). TLS termination is expected at the reverse-proxy layer in staging/production.
- **Workflow execution.** `POST /api/v1/workflows/run` creates the `WorkflowRun` and returns the run ID but does **not** execute it — the Scheduler (RFC 0003) owns execution. The run is left in `Pending` status for RFC 0003 to pick up.
- **gRPC server.** The gRPC server (main.go TODO step 10) is a separate concern and a separate RFC.
- **Persistent storage.** In-memory only; RFC 0001 established the `Store` interface for future SQLite migration.

## Design / Implementation

### Router and Server Structure

**Package:** `internal/server/`

Use Go 1.22+ `net/http` enhanced pattern routing (`{id}` wildcards) rather than a third-party router. This avoids new dependencies and is sufficient for the flat URL structure used in v0.1.

```go
type Server struct {
    addr     string
    store    state.Store
    registry registry.Registry
    planner  planner.Planner
    logger   *zap.Logger
    mux      *http.ServeMux
}

func New(addr string, store state.Store, reg registry.Registry, pl planner.Planner, logger *zap.Logger) *Server
func (s *Server) Handler() http.Handler
func (s *Server) Start(ctx context.Context) error
```

- `New` builds the `ServeMux` and registers all routes.
- `Handler()` returns the composed `http.Handler` for use in tests (avoids `httptest.NewServer` binding to a real port).
- `Start(ctx)` calls `http.ListenAndServe`, respects context cancellation for graceful shutdown via `http.Server.Shutdown`.
- The `addr` field is passed the `--http-port` flag value formatted as `":8080"`.

#### Graceful Shutdown

```go
func (s *Server) Start(ctx context.Context) error {
    srv := &http.Server{Addr: s.addr, Handler: s.Handler()}
    go func() {
        <-ctx.Done()
        shutdownCtx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
        defer cancel()
        _ = srv.Shutdown(shutdownCtx)
    }()
    if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
        return err
    }
    return nil
}
```

The existing `main.go` graceful-shutdown goroutine cancels the root context on SIGTERM/SIGINT, which propagates to `Start`'s context, triggering `Shutdown` with a 10-second drain window.

### JSON Error Envelope

All error responses use a consistent JSON structure:

```json
{
  "error": "run not found",
  "code":  "NOT_FOUND"
}
```

| `code` value        | HTTP status | Trigger |
|---------------------|-------------|---------|
| `BAD_REQUEST`       | 400         | Malformed JSON, missing required fields, invalid ID format |
| `NOT_FOUND`         | 404         | Run ID or agent ID absent from store / registry |
| `CONFLICT`          | 409         | Agent already registered; run in `Running` state (delete) |
| `UNPROCESSABLE`     | 422         | Workflow YAML parse error, DAG validation error |
| `NOT_IMPLEMENTED`   | 501         | Stub endpoints (logs, cost) |
| `INTERNAL`          | 500         | Unexpected server-side errors |

Helper:

```go
func writeError(w http.ResponseWriter, code string, msg string, status int)
func writeJSON(w http.ResponseWriter, v any, status int)
```

### Phase 1: Workflow Run Endpoints

#### `POST /api/v1/workflows/run`

**Request body:**

```json
{
  "workflow_id": "feature-builder",
  "inputs": {
    "user_request": "Add dark mode toggle to the settings page"
  }
}
```

- `workflow_id`: required, must match `^[a-z0-9][a-z0-9-]*[a-z0-9]$`.
- `inputs`: optional map of string key/value pairs; keys become `WorkflowRun.Inputs`.

**Handler logic:**

1. Decode and validate request body (reject unknown content types; require `Content-Type: application/json`).
2. Validate `workflow_id` format with the canonical regex.
3. **Path traversal protection** (see [Security Considerations](#security-considerations)).
4. Call `planner.Parse(resolvedPath)` → returns `*planner.Workflow` or error.
5. Call `planner.ValidateDAG(workflow)` → error on cycle.
6. Construct `state.WorkflowRun{WorkflowID: req.WorkflowID, Inputs: req.Inputs, Status: state.RunPending, StartedAt: time.Now()}`.
7. Generate a UUID run ID if not provided; call `store.CreateRun(ctx, &run)`.
8. Return `201 Created` with body `{"run_id": "<uuid>", "status": "pending"}`.

**Note:** The run is created in `Pending` state. RFC 0003 (Scheduler) will watch for pending runs and advance them through execution. In v0.1, before RFC 0003 is implemented, the run ID can be queried but the run will remain `Pending` indefinitely — this is expected and documented.

#### `GET /api/v1/workflows/{id}/status`

- Extract `{id}` from URL using `r.PathValue("id")` (Go 1.22+ `net/http` API).
- Call `store.GetRun(ctx, id)`.
- On `state.ErrRunNotFound`: return `404 NOT_FOUND`.
- Return `200` with:

```json
{
  "run_id":      "550e8400-...",
  "workflow_id": "feature-builder",
  "status":      "pending",
  "started_at":  "2026-04-09T12:00:00Z",
  "finished_at": null,
  "steps": {
    "plan": { "status": "pending", "output": "", "error": "" }
  }
}
```

`status` string values map from `state.RunStatus` constants: `"pending"`, `"running"`, `"completed"`, `"failed"`, `"cancelled"`.

#### `GET /api/v1/workflows`

- Call `store.ListRuns(ctx)`.
- Return `200` with a JSON array of run summaries (same shape as the status response).
- No pagination in v0.1; note the known scalability limitation with a `// TODO(v0.2): add pagination` comment.

#### `DELETE /api/v1/workflows/{id}`

- Call `store.GetRun(ctx, id)` to check current status.
- If `run.Status == state.RunRunning`: return `409 CONFLICT` with `"error": "cannot delete a running workflow run"`. This enforces the API-layer protection noted in RFC 0001 — the store permits deletion of any run regardless of status, but the HTTP layer must refuse running runs.
- Otherwise: call `store.DeleteRun(ctx, id)`.
- Return `204 No Content`.

### Phase 2: Agent Registry Endpoints

#### `POST /api/v1/agents/register`

**Request body:**

```json
{
  "id":           "code-writer",
  "address":      "localhost:50051",
  "capabilities": ["code_generation", "code_review"],
  "model":        "gpt-4o"
}
```

- `id`: required, validated with agent ID regex `^[a-z0-9][a-z0-9-]*[a-z0-9]$`.
- `address`: required, non-empty.
- `capabilities`: optional list of strings.
- `model`: optional.

Handler constructs `registry.AgentInfo` and calls `registry.Register(ctx, info)`.  
On `registry.ErrAgentAlreadyRegistered`: return `409 CONFLICT`.  
Return `201 Created` with the registered agent JSON.

#### `GET /api/v1/agents`

- Call `registry.List(ctx)`.
- Return `200` with JSON array of `AgentInfo`.

#### `GET /api/v1/agents/{id}`

- Call `registry.Get(ctx, id)`.
- On `registry.ErrAgentNotFound`: return `404 NOT_FOUND`.
- Return `200` with `AgentInfo` JSON.

#### `DELETE /api/v1/agents/{id}`

- Call `registry.Unregister(ctx, id)`.
- On `registry.ErrAgentNotFound`: return `404 NOT_FOUND`.
- Return `204 No Content`.

### Phase 3: Stub Endpoints

Both stubs are registered in the router and return a consistent `501 Not Implemented` JSON response:

```json
{ "error": "not implemented in v0.1", "code": "NOT_IMPLEMENTED" }
```

- `GET /api/v1/executions/{id}/logs` — deferred to RFC 0003 (Executor will capture step-level logs).
- `GET /api/v1/cost/summary` — deferred to cost-tracker integration (`internal/cost/cost.go` stub).

### Phase 4: Wire into main.go

Update `cmd/orchestrator/main.go` to:

1. Instantiate `server.New(fmt.Sprintf(":%d", *httpPort), store, reg, pl, logger)`.
2. Launch `s.Start(ctx)` in a goroutine, logging any non-`http.ErrServerClosed` errors.
3. Log `"HTTP server listening"` with the address field.
4. Add a `// TODO(security): no auth in v0.1` comment at the `server.New` call site.

This satisfies TODO step 11 ("Start HTTP server") in `main.go`. The existing graceful-shutdown context propagates to `Start`.

### Request ID Middleware

All requests receive a `X-Request-ID` response header with a UUID for log correlation:

```go
func requestIDMiddleware(next http.Handler) http.Handler
```

The UUID is also added to the `zap.Logger` context for that request using `logger.With(zap.String("request_id", id))` and passed via `context.WithValue` to downstream handlers. This is essential for correlating orchestrator logs with CLI output and agent logs once RFC 0003 is in place.

### Workflow Directory Configuration

A `--workflows-dir` flag (default: `"workflows/"`) is added to `cmd/orchestrator/main.go`. The server uses this directory as the root for loading workflow YAML files by ID. See [Security Considerations](#security-considerations) for the path traversal protection logic applied to this directory.

## Security Considerations

### Path Traversal in Workflow YAML Loading

When `POST /api/v1/workflows/run` receives a `workflow_id`, the server constructs a filesystem path from user input. This is the primary path-traversal risk in this RFC.

**Mitigation (mandatory):**

```go
func (s *Server) resolveWorkflowPath(workflowID string) (string, error) {
    // 1. Validate ID characters before any filesystem access.
    if !workflowIDRegex.MatchString(workflowID) {
        return "", ErrInvalidWorkflowID
    }
    // 2. Construct the candidate path.
    candidate := filepath.Join(s.workflowsDir, workflowID+".yaml")
    // 3. Canonicalize the workflows root (resolves symlinks).
    root, err := filepath.EvalSymlinks(s.workflowsDir)
    if err != nil {
        return "", err
    }
    // 4. Canonicalize the candidate path.
    resolved, err := filepath.EvalSymlinks(candidate)
    if err != nil {
        return "", ErrWorkflowNotFound
    }
    // 5. Prefix-check: resolved path must be inside root.
    if !strings.HasPrefix(resolved, root+string(filepath.Separator)) {
        return "", ErrWorkflowNotFound // do NOT reveal the directory traversal attempt
    }
    return resolved, nil
}
```

This approach supersedes the `filepath.Clean`-only defense noted in RFC 0001 §Security. `filepath.EvalSymlinks` resolves all symbolic links before the prefix check, so an attacker cannot escape the workflows directory via a symlink chain. Returning `ErrWorkflowNotFound` for a traversal attempt (rather than a distinct error) prevents information leakage about path structure.

### Running-Run Deletion Protection

As noted in RFC 0001, `state.Store.DeleteRun` permits deletion of any run. The `DELETE /api/v1/workflows/{id}` handler enforces the restriction at the API layer: a `409 CONFLICT` is returned when `run.Status == state.RunRunning`. This is the v0.1 mitigation; RFC 0003 will add lifecycle ownership at the Scheduler layer.

### No Authentication in v0.1

The HTTP server in v0.1 is unauthenticated. All endpoints are accessible to any client with network access. This is acceptable for the development/docker-compose deployment scenario where the orchestrator is not exposed to the internet.

**Residual risk:** Any process on the network can register/unregister agents, submit workflow runs, and query all run state.  
**Mitigation in v0.1:** Deployers should bind the HTTP server to `127.0.0.1` (loopback) by default in non-production environments. The `--http-port` flag currently binds to `0.0.0.0`; a `--http-bind` flag (default `127.0.0.1`) is added by this RFC to restrict the listen address in non-container environments.

A dedicated security RFC will add Bearer token authentication, per-agent API keys, or mTLS before any production deployment.

### JSON Input Size Limit

All handlers that read a request body wrap `r.Body` with `http.MaxBytesReader(w, r.Body, 1<<20)` (1 MiB) to prevent memory exhaustion from oversized payloads. Requests exceeding this limit receive `400 BAD_REQUEST`.

### JSON Decoder Strictness

Use `json.NewDecoder(r.Body).Decode(&req)` with `decoder.DisallowUnknownFields()` to reject unexpected fields. This prevents silent data loss when clients send misspelled field names and provides early feedback on API contract violations.

### Agent ID and Workflow ID Injection

Both are validated against their respective regexes before use in log fields, state-store keys, or filesystem paths. Validation occurs at the handler boundary (system boundary) before any downstream processing.

## Phased Implementation Plan

### Phase 1: Server scaffolding + workflow run endpoints (~200 LOC, ~1 day)

Summary: HTTP server setup, router, middleware, JSON envelope helpers, and workflow run CRUD.

**Deliverables:**
1. `internal/server/server.go` — `Server` struct, `New`, `Handler`, `Start`.
2. `internal/server/middleware.go` — `requestIDMiddleware`.
3. `internal/server/helpers.go` — `writeJSON`, `writeError`.
4. `internal/server/workflow_handlers.go` — POST run, GET status, GET list, DELETE run.
5. `internal/server/server_test.go` — handler tests using `httptest`.

**Dependencies:** RFC 0001 (state, registry, planner implementations).

### Phase 2: Agent registry endpoints (~120 LOC, ~0.5 day)

Summary: Agent CRUD endpoints backed by the in-memory registry.

**Deliverables:**
1. `internal/server/agent_handlers.go` — POST register, GET list, GET by ID, DELETE.
2. Extended `server_test.go` with agent handler tests.

### Phase 3: Stub endpoints + `--http-bind` flag (~50 LOC, ~0.5 day)

Summary: Register stub handlers; add `--http-bind` flag and `--workflows-dir` flag to main.

**Deliverables:**
1. `internal/server/stub_handlers.go` — logs and cost stubs.
2. Updated `cmd/orchestrator/main.go` — wire server, add `--http-bind`, `--workflows-dir`.

**Total estimated scope:** ~370 LOC implementation + tests. 2 days.

## Files Touched (Estimated)

| Component | Files | Change |
|-----------|-------|--------|
| Go orchestrator | `internal/server/server.go` | New — `Server` struct, `New`, `Handler`, `Start`, route registration |
| Go orchestrator | `internal/server/middleware.go` | New — `requestIDMiddleware` |
| Go orchestrator | `internal/server/helpers.go` | New — `writeJSON`, `writeError`, `resolveWorkflowPath` |
| Go orchestrator | `internal/server/workflow_handlers.go` | New — workflow run CRUD handlers |
| Go orchestrator | `internal/server/agent_handlers.go` | New — agent registry CRUD handlers |
| Go orchestrator | `internal/server/stub_handlers.go` | New — `501` stubs for logs and cost endpoints |
| Go orchestrator | `internal/server/server_test.go` | New — handler tests via `httptest.NewRecorder` |
| Go orchestrator | `cmd/orchestrator/main.go` | Add `--http-bind`, `--workflows-dir` flags; wire `server.New`; launch in goroutine |

## Test Strategy

- **Handler tests** using `httptest.NewRecorder` and `http.NewRequest` — no real TCP port required. All tests call `s.Handler()` directly.
- **Table-driven tests** for each endpoint covering: valid request → expected status code + body shape; missing required fields → `400`; invalid ID format → `400`; not-found IDs → `404`; duplicate agent registration → `409`; delete running run → `409`; valid delete → `204`.
- **Path traversal tests**: request with `workflow_id` containing `../`, URL-encoded traversal (`%2e%2e`), symlink pointing outside the workflows directory — all must return `404 NOT_FOUND`.
- **JSON body size limit**: send a 1 MiB + 1 byte body to `POST /api/v1/workflows/run` — must return `400 BAD_REQUEST`.
- **Unknown fields**: send a request body with an unrecognized field — must return `400 BAD_REQUEST` (strict decoder).
- **Content-Type enforcement**: send `POST /api/v1/workflows/run` with `Content-Type: text/plain` — must return `400 BAD_REQUEST`.
- **Request ID header**: every response must include `X-Request-ID` header with a non-empty UUID.
- **Graceful shutdown test**: start the server with `Start(ctx)`, cancel the context, verify `Start` returns without error within 1 second.
- **Workflow run lifecycle (v0.1)**: POST run → `201` with `run_id`; GET status with that ID → `200` with `status: "pending"`; DELETE → `204`; GET again → `404`.
- **Running-status delete protection**: manually set a run's status to `RunRunning` via `store.UpdateRunStatus`, then DELETE → `409 CONFLICT`.
- **Stub endpoints**: `GET /api/v1/executions/any-id/logs` → `501`; `GET /api/v1/cost/summary` → `501`.
- **Race detector**: all tests run with `-race` (already enforced in CI/Makefile).
- **Build smoke test**: `go build ./cmd/orchestrator` after wiring; `go vet ./internal/server/...`.

## Open Questions

1. **Workflow submission mode**: Should `POST /api/v1/workflows/run` accept inline YAML in the request body (for ad-hoc workflows) instead of referencing a pre-deployed file by ID? File-by-ID is simpler and safer for v0.1; inline submission is a future option.
   > *This RFC uses file-by-ID only. Inline submission requires additional sanitization and is deferred.*
2. **`RunStatus` string serialization**: `state.RunStatus` is a typed `int`. Should the API serialize it as an integer (compact but opaque) or a lowercase string (readable but requires a mapping function)?
   > *Use lowercase strings (`"pending"`, `"running"`, `"completed"`, `"failed"`, `"cancelled"`) in JSON responses. A `runStatusString` helper maps `RunStatus` → `string`. The integer representation is internal only.*
3. **Workflow list endpoint URL**: The spec defines `GET /api/v1/workflows/{id}/status` for a single run. Should the run list live at `GET /api/v1/workflows/runs` (resource-oriented) or `GET /api/v1/workflows` (flat)? The spec does not define a list endpoint explicitly.
   > *Use `GET /api/v1/workflows` returning an array of run status objects for v0.1. This can be revised when pagination is added.*

## Decision / Next Steps

Once this RFC is accepted:

1. Create feature branch `feature/v01-rest-api-server`.
2. Implement in phase order (scaffolding → workflow handlers → agent handlers → stubs + wiring).
3. PR < 500 lines per phase if needed; squash merge to `main`.
4. **Next RFC**: `0003-scheduler-executor.md` — parallel stage execution, gRPC task dispatch to agents, and step-level state transitions. Depends on RFC 0001 and RFC 0002 (uses the REST API's pending runs as the execution queue entry point).

## Related Documentation

- [ai-agents-orchestration-spec.md](../ai-agents-orchestration-spec.md) — §8.3 Orchestrator API (endpoint list)
- [orchestr8-extension-spec.md](../orchestr8-extension-spec.md) — v0.2+ streaming and channel endpoints
- [orchestr8-spec-audit.md](../orchestr8-spec-audit.md) — Spec gap audit
- [0001-core-orchestration-pipeline.md](0001-core-orchestration-pipeline.md) — State, Registry, Planner (this RFC's dependencies)
- [BRANCHING.md](../BRANCHING.md) — Branch naming and PR size guidelines
- Existing stubs: `internal/server/` (does not yet exist; this RFC creates it), `cmd/orchestrator/main.go` TODO step 11
- Workflow fixture: `workflows/feature-builder.yaml`
