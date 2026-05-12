---
id: RFC-0002
title: REST API Server (HTTP Layer + Workflow Submission)
summary: HTTP/REST surface for workflow run submission, status polling, and agent registry CRUD — closes the CLI-to-orchestrator path.
type: architecture
status: implemented
author: Maksim Khomutov
created: 2026-04-09
target: v0.1 (MVP)
depends_on:
  - RFC-0001
---

# RFC 0002 — REST API Server (HTTP Layer + Workflow Submission)

**Type**: architecture
**Status**: ✅ Implemented
**Author**: Maksim Khomutov
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

Implement the HTTP/REST API server for the Persatrix orchestrator. This RFC covers the v0.1 core endpoints: workflow run submission, run status polling, run deletion, agent registry CRUD, and skeleton endpoints for execution logs and cost summary. The server is wired into `cmd/orchestrator/main.go` as step 11 of the initialization sequence, completing the CLI-to-orchestrator communication path for manual workflow triggering.

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
- **Health/readiness endpoints.** Full `GET /healthz` (with dependency checks) and `GET /readyz` are not defined in this RFC. They are tracked as TODO step 12 in `main.go` and should be expanded in a follow-up PR or as part of Docker/Kubernetes deployment hardening. However, a **minimal `GET /healthz`** returning `200 OK` with `{"status": "ok"}` is included in Phase 1 to satisfy the existing `docker-compose.yaml` healthcheck (which already references `http://localhost:8080/healthz`). Without this, deploying via `docker compose` after this RFC would result in the orchestrator being marked `unhealthy` and potentially restart-looped. The minimal endpoint performs no dependency checks and is intentionally trivial.
- **CORS.** No CORS headers are set in v0.1. If the REST API is called from a browser in the future (e.g. admin dashboard, local dev tooling), CORS middleware will need to be added. Deferred.
- **Persistent storage.** In-memory only; RFC 0001 established the `Store` interface for future SQLite migration.

### Spec Deviations

The core spec (`ai-agents-orchestration-spec.md` §8.3) defines exactly eight v0.1 endpoints. This RFC adds two endpoints not present in the spec:

- `GET /api/v1/workflows` — list all workflow runs.
- `DELETE /api/v1/workflows/{id}` — delete a workflow run.

Both are deliberate additions to support the operational lifecycle of runs. They are not unintentional omissions from the spec; the spec should be updated in a follow-up documentation PR to reflect these additions. Track this via a `// TODO(spec-sync): update ai-agents-orchestration-spec.md §8.3 to include GET /api/v1/workflows and DELETE /api/v1/workflows/{id}` comment in the router registration.

A third addition not in the spec is:

- `GET /healthz` — minimal health endpoint returning `{"status": "ok"}`, required by the existing `docker-compose.yaml` healthcheck (see Non-Goals, C-02). This performs no dependency checks and is intentionally trivial.

Additionally, the runtime agent registration API (`POST /api/v1/agents/register`) diverges from `schemas/agent.schema.json`, which marks `name`, `role`, and `model` as **required** properties for statically configured agents. Runtime-registered agents provide only `id`, `address`, and `capabilities`. This is intentional: static config serves UI and scheduling metadata, while runtime registration provides the minimum needed for gRPC dispatch. See the [Phase 2 registration handler](#post-apiv1agentsregister) for details.

## Design / Implementation

### Router and Server Structure

**Package:** `internal/server/`

Use Go 1.22+ `net/http` enhanced pattern routing (`{id}` wildcards) rather than a third-party router. This avoids new dependencies and is sufficient for the flat URL structure used in v0.1.

```go
type Server struct {
    addr         string
    workflowsDir string // root directory for workflow YAML files; validated at construction
    store        state.Store
    registry     registry.Registry
    planner      planner.Planner
    logger       *zap.Logger
    mux          *http.ServeMux
}

// New validates that workflowsDir is accessible (via os.Stat) and returns an error if not,
// so misconfiguration is caught at startup rather than on the first workflow request.
func New(addr, workflowsDir string, store state.Store, reg registry.Registry, pl planner.Planner, logger *zap.Logger) (*Server, error)
func (s *Server) Handler() http.Handler
func (s *Server) Start(ctx context.Context) error
```

- `New` validates and **canonicalizes** `workflowsDir`, builds the `ServeMux`, and registers all routes. It returns `(*Server, error)` so that a missing, inaccessible, or non-directory workflows path is caught at startup rather than silently producing errors on every workflow submission:
  ```go
  fi, err := os.Stat(workflowsDir)
  if err != nil {
      return nil, fmt.Errorf("workflows directory %q not accessible: %w", workflowsDir, err)
  }
  if !fi.IsDir() {
      return nil, fmt.Errorf("workflows directory %q is not a directory", workflowsDir)
  }
  // Canonicalize once at startup: resolve to absolute path and follow symlinks.
  // This avoids repeated filepath.EvalSymlinks syscalls on every request in
  // resolveWorkflowPath, and eliminates a theoretical correctness issue where
  // a relative workflowsDir could resolve differently if the process cwd
  // changed between requests. (Review finding CS-02)
  absDir, err := filepath.Abs(workflowsDir)
  if err != nil {
      return nil, fmt.Errorf("workflows directory %q: failed to resolve absolute path: %w", workflowsDir, err)
  }
  canonicalDir, err := filepath.EvalSymlinks(absDir)
  if err != nil {
      return nil, fmt.Errorf("workflows directory %q: failed to resolve symlinks: %w", workflowsDir, err)
  }
  // Store canonicalDir as s.workflowsDir — all downstream path checks
  // use this pre-resolved value.
  ```
  The `fi.IsDir()` check is necessary because `os.Stat` succeeds for any path type (regular file, symlink target, device node). Without it, passing a file path (e.g. `--workflows-dir workflows/feature-builder.yaml`) would pass startup validation but fail at runtime when `filepath.Join` produces invalid paths.

  The `filepath.Abs` + `filepath.EvalSymlinks` canonicalization is performed once at construction time so that `resolveWorkflowPath` can compare against the stored canonical root without repeating the syscall on every request.
- `Handler()` returns the composed `http.Handler` for use in tests (avoids `httptest.NewServer` binding to a real port).
- `Start(ctx)` calls `http.ListenAndServe`, respects context cancellation for graceful shutdown via `http.Server.Shutdown`.
- The `addr` field is set from `--http-bind` and `--http-port` formatted as `"127.0.0.1:8080"` (see Phase 4).

#### Graceful Shutdown

```go
func (s *Server) Start(ctx context.Context) error {
    srv := &http.Server{Addr: s.addr, Handler: s.Handler()}
    go func() {
        <-ctx.Done()
        shutdownCtx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
        defer cancel()
        // Log shutdown errors (e.g. context deadline exceeded) rather than
        // discarding them — operators need visibility into unclean shutdowns.
        // (Review finding CS-01)
        if err := srv.Shutdown(shutdownCtx); err != nil {
            s.logger.Error("HTTP server shutdown error", zap.Error(err))
        }
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

> **URL semantics:** `{id}` in all `/api/v1/workflows/{id}/...` paths is a **run UUID** generated at submission time — it is not the `workflow_id` (the YAML filename). A `workflow_id` always matches `^[a-z0-9][a-z0-9-]*[a-z0-9]$`; a run `{id}` is a UUID e.g. `3f7a1c2d-...`. These are distinct values.

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

- `workflow_id`: required, must match `^[a-z0-9][a-z0-9-]*[a-z0-9]$`. Minimum two characters; single-character workflow IDs are invalid by design (consistent with the agent ID constraint defined in the project conventions).
- `inputs`: optional map of string key/value pairs; keys become `WorkflowRun.Inputs`. All input values **must** be JSON strings. Non-string values (numbers, booleans, objects, arrays) are rejected with `400 BAD_REQUEST` because `WorkflowRun.Inputs` is typed `map[string]string` (RFC 0001) and the strict JSON decoder (`DisallowUnknownFields`) will fail on type mismatch.

**Handler logic:**

1. Decode and validate request body. The `requireJSON` helper enforces `Content-Type: application/json` — this check applies only to handlers that expect a request body (POST endpoints). GET and DELETE handlers do not enforce Content-Type.
2. Validate `workflow_id` format with the canonical regex.
3. **Path traversal protection** (see [Security Considerations](#security-considerations)).
4. Call `s.planner.Parse(r.Context(), resolvedPath)` → returns `*planner.Workflow` or error; return `422 UNPROCESSABLE` on parse failure.
5. Call `s.planner.ValidateDAG(r.Context(), workflow)` → return `422 UNPROCESSABLE` on cycle. Note: `s.planner.Plan()` is **not** called at submission time — computing the `ExecutionPlan` (topological stages) is deferred to RFC 0003, where the Scheduler picks up pending runs and plans execution.
6. Construct `state.WorkflowRun{WorkflowID: req.WorkflowID, Inputs: req.Inputs, Status: state.RunPending, StartedAt: time.Now()}`.

   > **Note (I-03 — `StartedAt` semantics):** `StartedAt` is set at submission time even though the run is in `Pending` status. Semantically this is "submitted at," not "execution started." In v0.1 this is acceptable because no execution occurs (RFC 0003 deferred). The response DTO (P3 decision) maps this to `started_at` in the JSON wire format. A future RFC should add a dedicated `CreatedAt` field to `WorkflowRun` and reset `StartedAt` to zero until the Scheduler transitions the run to `Running`.
7. Generate a UUID run ID if not provided; call `store.CreateRun(ctx, &run)`.
8. Return `201 Created` with body `{"run_id": "<uuid>", "workflow_id": "<workflow_id>", "status": "pending"}`. Including `workflow_id` in the response saves clients a round-trip GET for display purposes (e.g., the Rust CLI showing "Submitted run abc123 for workflow feature-builder").

**Note:** The run is created in `Pending` state. RFC 0003 (Scheduler) will watch for pending runs and advance them through execution. In v0.1, before RFC 0003 is implemented, the run ID can be queried but the run will remain `Pending` indefinitely — this is expected and documented.

**Idempotency:** v0.1 has no client-side idempotency key. Retried submissions (e.g. a network timeout in the Rust CLI) create separate `WorkflowRun` entries with new UUIDs. This is acceptable for v0.1. A future RFC may add an optional `X-Idempotency-Key` header to deduplicate submissions.

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
  "steps":       {}
}
```

> **Note (L2 — v0.1 steps):** The `steps` map is empty at creation time because no step execution occurs until RFC 0003 (Scheduler/Executor). The example above reflects v0.1 reality. Once RFC 0003 populates `WorkflowRun.Steps`, responses will include per-step status objects.

`status` string values map from `state.RunStatus` constants: `"pending"`, `"running"`, `"completed"`, `"failed"`, `"cancelled"`.

> **Note (MI-01 — step timestamps):** RFC 0001's `StepState` struct includes `StartedAt time.Time` and `FinishedAt time.Time` fields. These are omitted from the v0.1 API response because no step execution occurs until RFC 0003 (Scheduler/Executor) is implemented — all steps remain in `"pending"` status with zero-value timestamps. When RFC 0003 adds step lifecycle transitions, the status response should be extended to include `started_at` and `finished_at` per step.

> **Decision (P3 — JSON serialization):** The snake_case field names above (`run_id`, `workflow_id`, `started_at`) require a mapping layer between domain types and wire format. **Option (b) — dedicated response DTOs in `internal/server/types.go`** — is adopted. This decouples the storage model from the wire format, avoids amending RFC 0001's domain types with HTTP-specific struct tags, and allows the API response shape to evolve independently. A `types.go` file containing request/response structs with explicit `json:` tags is added to Phase 1 deliverables and the Files Touched table.

> **Note (M-07 — `null` vs zero-value timestamps):** The example above shows `"finished_at": null`. Go’s `time.Time` zero value serializes to `"0001-01-01T00:00:00Z"`, not `null`. To produce `null` for unfinished runs, the response DTO must use `*time.Time` (pointer) for `FinishedAt` (and potentially `StartedAt` for future use when runs haven’t started yet). The `types.go` deliverable must use pointer types for nullable timestamp fields.

#### `GET /api/v1/workflows`

- Call `store.ListRuns(ctx)`.
- Return `200` with a JSON array of run summaries (same shape as the status response).
- No pagination in v0.1; note the known scalability limitation with a `// TODO(v0.2): add pagination` comment.

#### `DELETE /api/v1/workflows/{id}`

- Call `store.GetRun(ctx, id)` to check current status.
- On `state.ErrRunNotFound`: return `404 NOT_FOUND`.
- If `run.Status == state.RunRunning`: return `409 CONFLICT` with `"error": "cannot delete a running workflow run"`. This enforces the API-layer protection noted in RFC 0001 — the store permits deletion of any run regardless of status, but the HTTP layer must refuse running runs.
- Otherwise: call `store.DeleteRun(ctx, id)`.
- Return `204 No Content`.

> **Known issue (H1 — TOCTOU race):** The `GetRun` → status check → `DeleteRun` sequence is not atomic. A concurrent Scheduler (RFC 0003) could advance a run from `Pending` → `Running` between the check and the delete, allowing deletion of a running run. In v0.1, this race is dormant because no component transitions runs to `Running`. When RFC 0003 introduces the Scheduler, this must be addressed — either by adding a `DeleteRunIfNotRunning(ctx, runID)` atomic method to the `Store` interface or by acquiring a run-level lock. Implementation should include a `// TODO(v0.3): atomic check-and-delete or store-level status guard` comment.

### Phase 2: Agent Registry Endpoints

#### `POST /api/v1/agents/register`

**Request body:**

```json
{
  "id":           "code-writer",
  "address":      "localhost:50051",
  "capabilities": ["code_generation", "code_review"]
}
```

- `id`: required, validated with agent ID regex `^[a-z0-9][a-z0-9-]*[a-z0-9]$`.
- `address`: required, non-empty.
- `capabilities`: optional list of strings.

> **Note (B4 fix):** `model` is absent from this request body. `registry.AgentInfo` (defined in RFC 0001) has no `Model` field. Adding it here without amending `AgentInfo` would either silently drop the value (request DTO) or cause a compile error (direct struct mapping). Deferred: a future RFC may add `Model string` to `AgentInfo`.

> **Note (M-03 — Name/Role divergence):** `registry.AgentInfo` has `Name string` and `Role string` fields, and `schemas/agent.schema.json` marks `name`, `role`, and `model` as **required** for statically configured agents. Runtime-registered agents (via this endpoint) have empty `Name` and `Role` — gRPC agents self-identify by `ID` and `Capabilities` only. This is an intentional v0.1 divergence: static config provides rich metadata for UI and scheduling, while runtime registration provides the minimum needed for gRPC dispatch. See also [Spec Deviations](#spec-deviations).

Handler validates `Content-Type: application/json` (same `requireJSON` helper used in the workflow-run handler) before decoding the body. It constructs `registry.AgentInfo` with `Status: registry.StatusHealthy` (a freshly registered agent is assumed reachable until the first health check fails; the Go zero value `StatusUnknown` would be misleading) and calls `registry.Register(ctx, info)`:

```go
info := registry.AgentInfo{
    ID:           req.ID,
    Address:      req.Address,
    Capabilities: req.Capabilities,
    Status:       registry.StatusHealthy, // reachable until first health check fails
}
```  
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

1. Declare `--http-bind` and `--workflows-dir` flags alongside the existing `--http-port` flag:
   ```go
   httpBind     = flag.String("http-bind",      "127.0.0.1", "HTTP listen address (default: loopback only)")
   workflowsDir = flag.String("workflows-dir",  "workflows/", "Directory containing workflow YAML files")
   ```
   Using `--http-bind` (default `127.0.0.1`) instead of binding directly to `0.0.0.0` is the primary mitigation for the unauthenticated v0.1 server (see §No Authentication in v0.1).

   > **Note (M3 — Docker networking):** The `127.0.0.1` default is correct for host-native development but makes the orchestrator unreachable from other containers in a `docker-compose` network. The `Dockerfile.orchestrator` or `docker-compose.yaml` must pass `--http-bind 0.0.0.0` explicitly. Document this in `config/environments/development.yaml` as well.

2. Instantiate the server (see step 3 snippet for the combined `New` + goroutine code). `New` returns an error when `workflowsDir` is inaccessible or not a directory, surfacing misconfiguration at startup.

   > **Note (MI-04 — logger consistency):** The existing `main.go` uses the sugar logger (`log.Infow(...)`) while this RFC's Phase 4 snippets use structured zap (`logger.Info(..., zap.String(...))`) which is the project convention per `go-orchestrator.instructions.md`. Phase 4 should use structured zap for all **new** code added by this RFC but must **not** migrate existing sugar logger calls in the same PR — mixing refactoring with feature addition inflates the diff and makes the PR harder to review. The sugar-to-structured migration should be done in a separate cleanup PR tracked as `// TODO(cleanup): migrate main.go sugar logger to structured zap`.

3. Launch `srv.Start(ctx)` in a goroutine. The error path must call `cancel()` to propagate the failure to the root context so the orchestrator shuts down cleanly — without this, a failed HTTP server (e.g. port already bound) goes unnoticed while the rest of the orchestrator continues running with no HTTP endpoint:
   ```go
   listenAddr := fmt.Sprintf("%s:%d", *httpBind, *httpPort)
   srv, err := server.New(listenAddr, *workflowsDir, store, reg, pl, logger)
   if err != nil {
       logger.Fatal("failed to create HTTP server", zap.Error(err))
   }
   go func() {
       if err := srv.Start(ctx); err != nil {
           logger.Error("HTTP server terminated with error", zap.Error(err))
           cancel() // propagate to root context so orchestrator can shutdown cleanly
       }
   }()
   ```

4. Log `"HTTP server listening"` with the bound address:
   ```go
   logger.Info("HTTP server listening", zap.String("addr", listenAddr))
   ```
   Note: the `listenAddr` variable from step 3 eliminates the duplicated `fmt.Sprintf` call that would otherwise appear in both the `server.New` and `logger.Info` call sites.

> **Limitation (D-01 — exit code on async Start failure):** Because `Start` runs in a goroutine, a listen failure (e.g. port already bound) results in `logger.Error` + `cancel()` but the process still exits with code 0 (success). The existing `main.go` shutdown path only logs "Persatrix Server stopped" on `ctx.Done()` — it does not distinguish a clean shutdown from an error-triggered cancellation. For v0.1 this is acceptable. A future improvement is to propagate the error back to `main` via an `errCh chan error` and call `os.Exit(1)` on receive. Note: `logger.Fatal` is deliberately avoided in the goroutine because it calls `os.Exit(1)` immediately, bypassing deferred cleanup. Implementation should include a `// TODO(v0.2): propagate Start error via errCh for non-zero exit code` comment.

This satisfies TODO step 11 ("Start HTTP server") in `main.go`. The existing graceful-shutdown context propagates to `Start`.

### Request ID Middleware

All requests receive a `X-Request-ID` response header with a server-generated UUID for log correlation. The middleware **always generates a new UUID server-side** and **ignores** any `X-Request-ID` header present in the incoming request. Accepting client-provided IDs would enable log injection attacks — a malicious client could flood logs with forged request IDs, poisoning log aggregators or correlation systems.

```go
func requestIDMiddleware(next http.Handler) http.Handler {
    return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
        id := uuid.NewString() // always server-generated; never r.Header.Get("X-Request-ID")
        w.Header().Set("X-Request-ID", id)
        ctx := context.WithValue(r.Context(), requestIDKey, id)
        next.ServeHTTP(w, r.WithContext(ctx))
    })
}
```

The UUID is also added to the `zap.Logger` context for that request using `logger.With(zap.String("request_id", id))` and passed via `context.WithValue` to downstream handlers. This is essential for correlating orchestrator logs with CLI output and agent logs once RFC 0003 is in place.

To avoid the `staticcheck SA1029` warning (plain `string` context keys can be shadowed by any other package), declare an unexported type for the key:

```go
type contextKey string
const requestIDKey contextKey = "request_id"
```

Use `context.WithValue(r.Context(), requestIDKey, id)` to store and `r.Context().Value(requestIDKey).(string)` to retrieve.

### Panic Recovery Middleware

A `recoveryMiddleware` wraps all handlers to catch unhandled panics, log them via zap, and return a structured `500 INTERNAL` response instead of propagating the panic to the Go runtime (which closes the connection with plain text output):

```go
func recoveryMiddleware(logger *zap.Logger, next http.Handler) http.Handler {
    return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
        defer func() {
            if rec := recover(); rec != nil {
                logger.Error("handler panic",
                    zap.Any("panic", rec),
                    zap.String("stack", string(debug.Stack())),
                )
                writeError(w, "INTERNAL", "internal server error", http.StatusInternalServerError)
            }
        }()
        next.ServeHTTP(w, r)
    })
}
```

> **Limitation (M-05 — partial response write):** If a handler panics after already writing HTTP headers (via `w.WriteHeader()` or `w.Write()`), the `writeError` call in the recover block cannot send a `500` error response — Go's `http.ResponseWriter` silently ignores the second `WriteHeader` call, and the client receives a truncated or corrupt response body with no error indication. In v0.1, this is acceptable because all responses are short JSON objects written atomically (no streaming handlers). The panic is still logged via zap for operator visibility. Operators should monitor for abrupt connection resets in server logs.

All three (`recoveryMiddleware`, `requestIDMiddleware`, and `loggingMiddleware`) are composed in `Handler()`. Recovery must be the outermost wrapper so that panics in any inner middleware are also caught.

### Access Logging Middleware

> **Addition (M2 — access logging):** The RFC originally defined only `requestIDMiddleware` and `recoveryMiddleware` but omitted access logging. Without it, operators have no visibility into request patterns, error rates, or slow endpoints. A `loggingMiddleware` is added to Phase 1:
>
> ```go
> func loggingMiddleware(logger *zap.Logger, next http.Handler) http.Handler
> ```
>
> This wraps the `ResponseWriter` to capture the status code and logs method, path, status code, latency, and request ID on completion (~20 LOC). Composition order: recovery → logging → requestID → mux.

### Per-Request Timeout

> **TODO (H3 — request timeout):** Individual handler requests currently have no timeout. A pathological workflow YAML file (e.g. deeply nested, 1 MiB) could cause `planner.Parse` to block a handler goroutine for an extended period. `MaxBytesReader` limits the HTTP body size but does not protect against slow filesystem reads. A per-request timeout middleware (e.g., `http.TimeoutHandler` wrapping the mux, or `context.WithTimeout` in each handler) with a configurable default (e.g. 30s) is **deferred to a follow-up PR**. Implementation should add a `// TODO(v0.2): per-request timeout middleware — see RFC 0002 H3` marker. **Residual risk:** without a per-request timeout, slow filesystem I/O or a large YAML file can block a handler goroutine indefinitely. This is acceptable in v0.1 where the server is not exposed to untrusted clients.

### Workflow Directory Configuration

A `--workflows-dir` flag (default: `"workflows/"`) is added to `cmd/orchestrator/main.go`. The server uses this directory as the root for loading workflow YAML files by ID. See [Security Considerations](#security-considerations) for the path traversal protection logic applied to this directory.

> **Warning (P7 — relative default):** The default `"workflows/"` is a relative path resolved against the process working directory (`cwd`) at startup. If the binary is launched from a directory other than the repository root (e.g. `./bin/persatrix-server`), the path resolves to `./bin/workflows/` and all workflow submissions return `404`. Production and staging deployments should always pass an absolute `--workflows-dir` path. In development, run the binary from the repository root or set the flag explicitly.

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
    // 3. Use the pre-canonicalized workflows root stored by New().
    //    (s.workflowsDir was resolved via filepath.Abs + filepath.EvalSymlinks
    //    at construction time — see CS-02.)
    root := s.workflowsDir
    // 4. Canonicalize the candidate path.
    resolved, err := filepath.EvalSymlinks(candidate)
    if err != nil {
        // Log at Debug so operators can distinguish permission errors or I/O failures
        // from genuine not-found, without leaking path details to the HTTP response.
        s.logger.Debug("EvalSymlinks failed", zap.String("workflow_id", workflowID), zap.Error(err))
        return "", ErrWorkflowNotFound // do NOT reveal the reason to the caller
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

As noted in RFC 0001, `state.Store.DeleteRun` permits deletion of any run. The `DELETE /api/v1/workflows/{id}` handler enforces the restriction at the API layer: a `409 CONFLICT` is returned when `run.Status == state.RunRunning`. This is the v0.1 mitigation; RFC 0003 will add lifecycle ownership at the Scheduler layer. Note: this check-then-delete sequence has a TOCTOU race window — see the [H1 note in the DELETE handler](#delete-apiv1workflowsid) for details and the planned resolution.

### No Authentication in v0.1

The HTTP server in v0.1 is unauthenticated. All endpoints are accessible to any client with network access. This is acceptable for the development/docker-compose deployment scenario where the orchestrator is not exposed to the internet.

**Residual risk:** Any process on the network can register/unregister agents, submit workflow runs, and query all run state.  
**Mitigation in v0.1:** Deployers should bind the HTTP server to `127.0.0.1` (loopback) by default in non-production environments. The `--http-port` flag currently binds to `0.0.0.0`; a `--http-bind` flag (default `127.0.0.1`) is added by this RFC to restrict the listen address in non-container environments.

A dedicated security RFC will add Bearer token authentication, per-agent API keys, or mTLS before any production deployment.

### JSON Input Size Limit

All handlers that read a request body — specifically `POST /api/v1/workflows/run` and `POST /api/v1/agents/register` — wrap `r.Body` with `http.MaxBytesReader(w, r.Body, 1<<20)` (1 MiB) to prevent memory exhaustion from oversized payloads. Requests exceeding this limit must return `400 BAD_REQUEST`.

When the limit is exceeded, `json.Decode` returns an error that wraps `*http.MaxBytesError`, not a `*json.SyntaxError`. Without an explicit `errors.As` check, the error falls through to the generic `500 INTERNAL` path — the opposite of the documented behaviour. The shared body-decoding helper must check for this type **before** the generic decode-error path:

```go
var maxBytesErr *http.MaxBytesError
if errors.As(err, &maxBytesErr) {
    writeError(w, "BAD_REQUEST", "request body too large", http.StatusBadRequest)
    return
}
```

### JSON Decoder Strictness

Use `json.NewDecoder(r.Body).Decode(&req)` with `decoder.DisallowUnknownFields()` to reject unexpected fields. This prevents silent data loss when clients send misspelled field names and provides early feedback on API contract violations.

### Agent ID and Workflow ID Injection

Both are validated against their respective regexes before use in log fields, state-store keys, or filesystem paths. Validation occurs at the handler boundary (system boundary) before any downstream processing.

## Phased Implementation Plan

### Phase 1: Server scaffolding + workflow run endpoints (~250 LOC, ~1 day)

Summary: HTTP server setup, router, middleware, JSON envelope helpers, and workflow run CRUD.

**Deliverables:**
1. `internal/server/server.go` — `Server` struct, `New`, `Handler`, `Start`, minimal `GET /healthz` handler (C-02: satisfies existing `docker-compose.yaml` healthcheck).
2. `internal/server/types.go` — request/response DTOs with `json:` struct tags (P3 decision).
3. `internal/server/middleware.go` — `recoveryMiddleware` (imports `runtime/debug` for `debug.Stack()` — I-04), `requestIDMiddleware`, `loggingMiddleware` (M2).
4. `internal/server/helpers.go` — `writeJSON`, `writeError`, `requireJSON` (Content-Type enforcement helper used by POST handlers).
5. `internal/server/workflow_handlers.go` — POST run, GET status, GET list, DELETE run, `resolveWorkflowPath` (M-05).
6. `internal/server/server_test.go` — handler tests using `httptest`.

**Dependencies:** RFC 0001 (state, registry, planner implementations). **Existing dependency:** `github.com/google/uuid` (already in `go.mod` from RFC 0001) is reused for server-generated request IDs (`uuid.NewString()` in `requestIDMiddleware`). RFC 0001 must export the following sentinel errors for `errors.Is()` comparisons in RFC 0002 handlers:
- `state.ErrRunNotFound`
- `state.ErrRunAlreadyExists`
- `registry.ErrAgentAlreadyRegistered`
- `registry.ErrAgentNotFound`

If these sentinels are absent from the RFC 0001 merged implementation, RFC 0002 cannot be implemented without brittle string-matching. Track as a blocker on PR #3.

### Phase 2: Agent registry endpoints (~120 LOC, ~0.5 day)

Summary: Agent CRUD endpoints backed by the in-memory registry.

**Deliverables:**
1. `internal/server/agent_handlers.go` — POST register, GET list, GET by ID, DELETE.
2. Extended `server_test.go` with agent handler tests.

### Phase 3: Stub endpoints (~30 LOC, ~0.5 day)

Summary: Register stub handlers for the two deferred endpoints.

**Deliverables:**
1. `internal/server/stub_handlers.go` — logs and cost stubs.

### Phase 4: Wire into main.go + CLI flags + Docker fix (~40 LOC, ~0.5 day)

Summary: Add `--http-bind` and `--workflows-dir` flags; wire `server.New` into the orchestrator startup sequence (TODO step 11). Update `docker-compose.yaml` so the orchestrator is reachable from agent containers.

**Deliverables:**
1. Updated `cmd/orchestrator/main.go` — wire server, add `--http-bind`, `--workflows-dir` flags.
2. Updated `docker-compose.yaml` — pass `--http-bind 0.0.0.0` to the orchestrator service command so that agent containers can reach the REST API over the Docker network. Without this, the default `127.0.0.1` bind makes the orchestrator unreachable from other containers.

**Total estimated scope:** ~420 LOC implementation + tests. 2 days.

## Files Touched (Estimated)

| Component | Files | Change |
|-----------|-------|--------|
| Go orchestrator | `internal/server/server.go` | New — `Server` struct, `New`, `Handler`, `Start`, route registration, minimal `/healthz` |
| Go orchestrator | `internal/server/types.go` | New — request/response DTOs with `json:` struct tags (P3 decision); `*time.Time` for nullable timestamps (M-07) |
| Go orchestrator | `internal/server/middleware.go` | New — `recoveryMiddleware` (with `debug.Stack()`), `requestIDMiddleware`, `loggingMiddleware` |
| Go orchestrator | `internal/server/helpers.go` | New — `writeJSON`, `writeError` |
| Go orchestrator | `internal/server/workflow_handlers.go` | New — workflow run CRUD handlers, `resolveWorkflowPath` (security-critical path traversal logic kept with its only caller rather than in generic helpers — review finding M-05) |
| Go orchestrator | `internal/server/agent_handlers.go` | New — agent registry CRUD handlers |
| Go orchestrator | `internal/server/stub_handlers.go` | New — `501` stubs for logs and cost endpoints |
| Go orchestrator | `internal/server/server_test.go` | New — handler tests via `httptest.NewRecorder` |
| Go orchestrator | `cmd/orchestrator/main.go` | Add `--http-bind`, `--workflows-dir` flags; wire `server.New`; launch in goroutine |
| Go dependency | `go.mod`, `go.sum` | Uses existing `github.com/google/uuid` dependency (added by RFC 0001 for `state.CreateRun`) |
| Docker | `docker-compose.yaml` | Pass `--http-bind 0.0.0.0` to orchestrator service command (C-01) |

## Test Strategy

- **Handler tests** using `httptest.NewRecorder` and `http.NewRequest` — no real TCP port required. All tests call `s.Handler()` directly.
- **Table-driven tests** for each endpoint covering: valid request → expected status code + body shape; missing required fields → `400`; invalid ID format → `400`; not-found IDs → `404`; duplicate agent registration → `409`; delete running run → `409`; valid delete → `204`.
- **Path traversal tests**: request with `workflow_id` containing `../`, URL-encoded traversal (`%2e%2e`), symlink pointing outside the workflows directory — all must return `404 NOT_FOUND`.
- **JSON body size limit**: send a 1 MiB + 1 byte body to `POST /api/v1/workflows/run` — must return `400 BAD_REQUEST`.
- **Unknown fields**: send a request body with an unrecognized field — must return `400 BAD_REQUEST` (strict decoder).
- **Content-Type enforcement**: send `POST /api/v1/workflows/run` with `Content-Type: text/plain` — must return `400 BAD_REQUEST`.
- **Request ID header**: every response must include `X-Request-ID` header with a server-generated UUID. Sending a client-provided `X-Request-ID` header must **not** cause the response to echo it back — the server always generates a fresh UUID (see MA-01 security rationale).
- **Input type enforcement**: `POST /api/v1/workflows/run` with `inputs: {"key": 42}` (non-string value) → `400 BAD_REQUEST`.
- **Initial agent status**: after `POST /api/v1/agents/register`, the returned agent must have `status` corresponding to `StatusHealthy` (not `StatusUnknown`).
- **Start error propagation**: call `Start(ctx)` on an already-bound port → the goroutine must log the error and cancel the root context.
- **Graceful shutdown test**: start the server with `Start(ctx)`, cancel the context, verify `Start` returns without error within 1 second.
- **Workflow run lifecycle (v0.1)**: POST run → `201` with `run_id`; GET status with that ID → `200` with `status: "pending"`; DELETE → `204`; GET again → `404`.
- **Running-status delete protection**: manually set a run's status to `RunRunning` via `store.UpdateRunStatus`, then DELETE → `409 CONFLICT`.
- **Stub endpoints**: `GET /api/v1/executions/any-id/logs` → `501`; `GET /api/v1/cost/summary` → `501`.
- **Malformed JSON body**: send `{invalid}` to `POST /api/v1/workflows/run` → `400 BAD_REQUEST`.
- **Empty request body**: send empty body to POST endpoints → `400 BAD_REQUEST`.
- **Method Not Allowed**: send unsupported methods (e.g. `PUT /api/v1/workflows/run`) → `405`. Go 1.22+ `ServeMux` returns `405 Method Not Allowed` automatically for method-specific patterns. **Design decision (I-02):** `405` and `404` responses from the router use Go’s default plain-text body, not the JSON error envelope. The JSON envelope applies to application-level errors returned by handlers. This is the pragmatic choice for v0.1 — intercepting and converting router-level rejections adds complexity for minimal benefit. Document this in the handler code with a `// NOTE: 405/404 from ServeMux are plain text (see RFC 0002 I-02)` comment.
- **Concurrent access**: multiple goroutines hitting endpoints simultaneously to validate the `sync.RWMutex`-backed stores under contention. Run with `-race`.
- **Race detector**: all tests run with `-race` (already enforced in CI/Makefile).
- **Empty workflow ID**: `POST /api/v1/workflows/run` with `"workflow_id": ""` → `400 BAD_REQUEST`. The regex `^[a-z0-9][a-z0-9-]*[a-z0-9]$` rejects empty strings, but this should be an explicit test case for clarity. (Review finding T-01)
- **Empty agent address**: `POST /api/v1/agents/register` with `"address": ""` → `400 BAD_REQUEST`. The handler requires a non-empty address; verify this is enforced. (Review finding T-02)
- **Non-existent workflow file**: `POST /api/v1/workflows/run` with a validly-formatted `workflow_id` (e.g. `"no-such-workflow"`) where no corresponding YAML file exists on disk → `404 NOT_FOUND` (not `500`). `filepath.EvalSymlinks` returns an error for non-existent paths; the handler must map this to the workflow-not-found path. (Review finding T-04)
- **Empty workflow list**: `GET /api/v1/workflows` when no runs exist → `200` with `[]` (empty JSON array), not `404` or `null`. (Review finding T-06)
- **Build smoke test**: `go build ./cmd/orchestrator` after wiring; `go vet ./internal/server/...`.

## Open Questions

1. **Workflow submission mode**: Should `POST /api/v1/workflows/run` accept inline YAML in the request body (for ad-hoc workflows) instead of referencing a pre-deployed file by ID? File-by-ID is simpler and safer for v0.1; inline submission is a future option.
   > *This RFC uses file-by-ID only. Inline submission requires additional sanitization and is deferred.*
2. **`RunStatus` string serialization**: `state.RunStatus` is a typed `int`. Should the API serialize it as an integer (compact but opaque) or a lowercase string (readable but requires a mapping function)?
   > *Use lowercase strings (`"pending"`, `"running"`, `"completed"`, `"failed"`, `"cancelled"`) in JSON responses. A `runStatusString` helper maps `RunStatus` → `string`. The integer representation is internal only.*
3. **Workflow list endpoint URL**: The spec defines `GET /api/v1/workflows/{id}/status` for a single run. Should the run list live at `GET /api/v1/workflows/runs` (resource-oriented) or `GET /api/v1/workflows` (flat)? The spec does not define a list endpoint explicitly.
   > *Use `GET /api/v1/workflows` returning an array of run status objects for v0.1. This can be revised when pagination is added.*
   >
   > **Naming collision risk (H2):** `GET /api/v1/workflows` currently returns **workflow runs** (execution instances), not **workflow definitions**. When v0.2 adds workflow management endpoints (CRUD for workflow YAML files), this URL will collide. The cleanest migration path is to rename to `GET /api/v1/workflows/runs` (and `DELETE /api/v1/workflows/runs/{id}`) in the same RFC that introduces definition management. Accept the v0.1 naming as-is since no definition endpoints exist yet, but avoid building CLI UX that hard-codes the current path — the Rust CLI should use a named constant for the endpoint URL. Implementation should include a `// TODO(v0.2): rename /api/v1/workflows to /api/v1/workflows/runs when definition endpoints are added` comment in the router registration.

## Decision / Next Steps

Feature branch `feature/v01-rest-api-server` will be created for implementation.

1. Implement in phase order (scaffolding → workflow handlers → agent handlers → stubs + wiring).
2. PR < 500 lines per phase if needed; squash merge to `main`.
3. **Next RFC**: `0003-scheduler-executor.md` — parallel stage execution, gRPC task dispatch to agents, and step-level state transitions. Depends on RFC 0001 and RFC 0002 (uses the REST API's pending runs as the execution queue entry point).

## Related Documentation

- [ai-agents-orchestration-spec.md](../ai-agents-orchestration-spec.md) — §8.3 Orchestrator API (endpoint list)
- [persatrix-extension-spec.md](../persatrix-extension-spec.md) — v0.2+ streaming and channel endpoints
- [persatrix-spec-audit.md](../persatrix-spec-audit.md) — Spec gap audit
- [0001-core-orchestration-pipeline.md](0001-core-orchestration-pipeline.md) — State, Registry, Planner (this RFC's dependencies)
- [BRANCHING.md](../BRANCHING.md) — Branch naming and PR size guidelines
- Existing stubs: `internal/server/` (does not yet exist; this RFC creates it), `cmd/orchestrator/main.go` TODO step 11
- Workflow fixture: `workflows/feature-builder.yaml`
