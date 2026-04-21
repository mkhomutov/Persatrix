# RFC 0018 — Structured Logging Framework

**Type**: architecture
**Status**: 📋 Proposed
**Author**: Maksim Khomutov
**Date**: 2026-04-21
**Target**: v0.2.3
**Depends on**: none
**Feeds into**: RFC 0019 (log↔trace correlation, deferred)

---

## Table of Contents

- [Summary](#summary)
- [Motivation](#motivation)
- [Goals](#goals)
- [Non-Goals](#non-goals)
- [Design / Implementation](#design--implementation)
  - [A. Current State](#a-current-state)
  - [B. Common Log Schema](#b-common-log-schema)
  - [C. Per-Language Adoption](#c-per-language-adoption)
  - [D. Cross-Process Correlation](#d-cross-process-correlation)
  - [E. `persatrix logs` Endpoint and Storage](#e-persatrix-logs-endpoint-and-storage)
- [Security Considerations](#security-considerations)
- [Phased Implementation Plan](#phased-implementation-plan)
- [Files Touched (Estimated)](#files-touched-estimated)
- [Test Strategy](#test-strategy)
- [Open Questions](#open-questions)
- [Decision / Next Steps](#decision--next-steps)
- [Related Documentation](#related-documentation)

---

## Summary

This RFC standardises log output across the three Persatrix processes (Go orchestrator, Python agents, Rust CLI) on a single JSON line schema, propagates `execution_id` / `step_id` / `agent_id` across the gRPC boundary so a single workflow run is traceable through logs, and replaces the stubbed `GET /api/v1/executions/{id}/logs` endpoint with a working implementation backed by a bounded in-memory ring buffer. The result: `persatrix logs <execution-id>` becomes useful, and operators can grep one stream instead of correlating timestamps across three terminals.

## Motivation

Persatrix already has structured logging on the Go side (`go.uber.org/zap`) but Python uses stdlib `logging` with free-form messages, Rust uses `println!`/`eprintln!`, and there is no shared schema across the three. A workflow execution crosses Go → Python via gRPC; correlating a single run today requires manually matching timestamps across processes.

Three concrete gaps make this visible:

1. **No shared schema.** Go zap fields, Python f-strings, Rust println — three formats, none parseable as a unit.
2. **No cross-process correlation.** `request_id` is added by `internal/server/middleware.go` but is not propagated into agent-side logs. `execution_id` and `step_id` are not in any log line on the Python side.
3. **`logs` CLI endpoint is a stub.** [`internal/server/stub_handlers.go`](../../internal/server/stub_handlers.go) returns 501 for `GET /api/v1/executions/{id}/logs`. The Rust CLI [`cli/src/commands/logs.rs`](../../cli/src/commands/logs.rs) is wired and discoverable in `--help`, so users find it, run it, and hit the stub. Worse than not having the command.

What happens if we do nothing: every operator-facing issue requires the author to debug it for them, because the operator cannot self-diagnose from logs. Every contributor PR review that depends on understanding cross-process behaviour requires running the code themselves rather than reading log output. The stubbed endpoint stays in `--help` indefinitely.

This is not a v0.2.x headline feature. It is operability hygiene that should land before the v0.3.0 surface area expands and makes the gap larger.

## Goals

1. A single JSON-line log schema is documented in `docs/observability.md` (new file created by this RFC) and emitted by all three processes.
2. Python agents emit structured logs via `structlog` with the schema in [Section B](#b-common-log-schema).
3. Go orchestrator zap output conforms to the same schema (field renames where needed; the structured emit is already in place).
4. `execution_id`, `step_id`, and `agent_id` are propagated from orchestrator into Python agents via gRPC metadata, bound to logger context on entry, and present on every log line emitted while handling that task.
5. `GET /api/v1/executions/{id}/logs` returns a JSON array of log entries for that execution, drawn from a bounded in-memory ring buffer keyed by `execution_id`.
6. `persatrix logs <execution-id>` displays those entries with colourised level, timestamp, service, message; `--verbose` shows the full attribute map.
7. `PERSATRIX_LOG_FORMAT=pretty` toggles a human-friendly renderer for local development; default remains JSON.
8. Rust CLI logging is in scope only for the `logs` command's display formatting. CLI-internal logging migration to `tracing` is deferred.

## Non-Goals

- **Centralised log aggregation.** Loki / ELK / Splunk / Datadog integration. Operators pipe the JSON stream wherever they want.
- **Persistent log retention.** The ring buffer is in-memory, bounded, and lost on restart. File-based log storage is a v0.3+ concern if it materialises at all.
- **Search / filter / query language in the CLI.** `persatrix logs` returns a flat list; users pipe to `jq`, `grep`, or `rg` for filtering.
- **Log shipping or forwarding built into Persatrix.**
- **Log-based alerting or monitoring.**
- **Sensitive-data scrubbing.** PII / secret redaction belongs in a dedicated security RFC (likely under RFC 0009's umbrella).
- **Migrating Rust CLI internals to `tracing`.** Out of scope; the CLI is a thin client and its internal logs are low-value relative to server-side logs.
- **Log-to-trace correlation fields (`trace_id` / `span_id` on every log line).** Depends on RFC 0019 landing; deferred to a follow-up that lands after both RFCs.

---

## Design / Implementation

### A. Current State

| Component | Logger | Format | Cross-process IDs |
|-----------|--------|--------|-------------------|
| Go orchestrator | `go.uber.org/zap` (production config) | JSON, structured fields | `request_id` in middleware; `execution_id` / `step_id` partial |
| Python agents | stdlib `logging`, free-form `%`-format messages | Plain text to stderr | None |
| Rust CLI | `println!` / `eprintln!` | Plain text | N/A |
| `logs` HTTP endpoint | [stub_handlers.go](../../internal/server/stub_handlers.go) | 501 Not Implemented | — |
| `persatrix logs` CLI | [logs.rs](../../cli/src/commands/logs.rs) | Wired but unusable end-to-end | — |

### B. Common Log Schema

One JSON object per line, one event per object. Required fields:

| Field | Type | Notes |
|-------|------|-------|
| `timestamp` | string (ISO 8601 with timezone, RFC 3339) | UTC unless overridden |
| `level` | string | One of `DEBUG`, `INFO`, `WARN`, `ERROR` |
| `service` | string | One of `orchestrator`, `agent-<id>`, `cli` |
| `message` | string | Human-readable; should not include the structured fields |

Optional fields, present when applicable:

| Field | Type | Notes |
|-------|------|-------|
| `execution_id` | string | Workflow run ID |
| `step_id` | string | Step within a workflow |
| `agent_id` | string | Source or target agent (depending on log site) |
| `request_id` | string | HTTP request ID (orchestrator only, set by middleware) |
| `attributes` | object | Free-form key/value bag for site-specific context |

Reserved for RFC 0019:

| Field | Type | Notes |
|-------|------|-------|
| `trace_id` | string | OTEL trace ID (added by the log↔trace correlation follow-up) |
| `span_id` | string | OTEL span ID (added by the same follow-up) |

The schema is versionless. If a breaking change is needed in the future, it will be called out in CHANGELOG and this RFC will be revised.

### C. Per-Language Adoption

**Go orchestrator.** Already structured via zap. Work consists of:

- Audit existing `logger.Info/Error/Debug/Warn` call sites, normalise field names to match the schema (`execution_id` not `executionID`, `agent_id` not `agentID`, etc.).
- Wire `PERSATRIX_LOG_FORMAT=pretty` to the zap dev encoder; default stays JSON production encoder.
- No new dependency.

**Python agents.** Adopt `structlog` with a JSON renderer:

- Add `structlog>=24.1` to `agents/pyproject.toml` runtime deps.
- New module `agents/observability/logging.py` configures the processor chain (timestamp, level, context vars merger, JSON renderer) and exposes `get_logger(name)`.
- Replace `logging.getLogger(__name__)` with `structlog.get_logger(__name__)` across `agents/`. Existing `logger.info("text")` call sites work unchanged; structured fields move to keyword args (`logger.info("text", key=value)`).
- `PERSATRIX_LOG_FORMAT=pretty` swaps the JSON renderer for `structlog.dev.ConsoleRenderer`.

**Rust CLI.** Out of scope for internal logs. The `logs` command formats incoming JSON entries from the server for terminal display using existing CLI patterns (no `tracing` adoption).

### D. Cross-Process Correlation

Three IDs need to travel from orchestrator to agent and back into the agent's log context: `execution_id`, `step_id`, `agent_id`.

**Outbound from Go.** The executor already has these in-scope at gRPC dispatch sites (`internal/executor/dispatch.go`, `internal/executor/chat.go`). Inject them into outgoing gRPC metadata using the keys `persatrix-execution-id`, `persatrix-step-id`, `persatrix-agent-id`.

**Inbound on Python.** A new gRPC server interceptor in `agents/observability/grpc_logging.py` extracts those headers from every incoming RPC and binds them to `structlog.contextvars` for the duration of the handler. Every log line emitted during that RPC then automatically carries them.

**Note on overlap with RFC 0019.** RFC 0019's gRPC interceptors propagate W3C TraceContext for OTEL. The two interceptor concerns are independent (header keys differ, lifecycle differs) but should be installed in the same place and reviewed together. This RFC ships the logging-context interceptor; RFC 0019 ships the OTEL one.

### E. `persatrix logs` Endpoint and Storage

**Storage.** A new `internal/observability/logbuffer` package implements a bounded ring buffer keyed by `execution_id`:

- Each execution gets its own ring (capacity TBD per [Open Question 2](#open-questions); proposal: 1000 entries).
- Total memory bound: `max_executions × per_execution_capacity` entries; LRU-evict whole executions when the total cap is reached.
- Buffer accepts entries via a write hook called from a custom zap core (Go) and via a `structlog` processor that pushes to the orchestrator over a small internal HTTP endpoint or a side-channel (see Open Question 1).
- Ring contents are lost on orchestrator restart. This is documented as a known limitation in the operations guide.

**Endpoint.** `handleGetLogs` is rewritten to:

1. Parse and validate `execution_id` from the path.
2. Fetch the ring's contents (or empty array if no such execution).
3. Return `{"execution_id": "...", "entries": [...]}` as JSON. Each entry is a log object conforming to [Section B](#b-common-log-schema).

**CLI display.** `cli/src/commands/logs.rs` parses the JSON array and prints each entry:

- `<timestamp> <LEVEL> <service> [<step_id>] <message>` by default
- `--verbose` adds the `attributes` object pretty-printed below each line
- Level colourisation via the existing `colored` crate (already in `Cargo.toml`)
- `--follow` flag: out of scope for this RFC; documented as "not yet implemented" in `--help`

---

## Security Considerations

- **Log content can leak secrets.** Existing call sites occasionally log argument summaries that may include LLM prompts, tool inputs, or user chat content. This RFC does not introduce redaction (explicit non-goal) but does introduce a CLI surface that exposes those logs over HTTP. The endpoint is currently unauthenticated, consistent with the rest of the v0.2.x REST API. Documented as a known limitation; resolved when RFC 0009's auth layer lands.
- **Ring buffer DoS.** A workflow that emits very high log volume could fill its ring before useful entries are read. The bound is per-execution, so other executions are unaffected. The total cap prevents one runaway run from exhausting orchestrator memory.
- **No new external dependencies for Go.** Python adds `structlog` (well-maintained, MIT licensed, no transitive C deps).
- **`pretty` output is opt-in.** Pretty-printed logs may include ANSI escapes; JSON default is safe to pipe into machine consumers.

---

## Phased Implementation Plan

### Phase 1: Schema + Python `structlog` Adoption

**Summary.** Document the schema, add `structlog`, replace stdlib logging in `agents/`, no cross-process work yet.

**Deliverables.**

1. `docs/observability.md` defining the schema in [Section B](#b-common-log-schema).
2. `agents/observability/__init__.py` and `agents/observability/logging.py` configuring `structlog`.
3. Replace `logging.getLogger` call sites in `agents/`. Existing `logger.info("text")` continues to work; structured fields opt-in via kwargs.
4. `PERSATRIX_LOG_FORMAT=pretty` env override implemented in `agents/observability/logging.py`.
5. Unit tests asserting JSON output shape and field presence.

**Dependencies.** None.

### Phase 2: Go Field Normalisation + `pretty` Encoder

**Summary.** Make zap output conform to the schema; add the `pretty` mode.

**Deliverables.**

1. Audit and rename zap field keys (script-assisted) to schema names.
2. `PERSATRIX_LOG_FORMAT=pretty` wired to zap dev encoder; default JSON.
3. Tests asserting field name presence on representative log lines.

**Dependencies.** Phase 1 (schema document is the source of truth).

### Phase 3: Cross-Process Context Propagation

**Summary.** `execution_id` / `step_id` / `agent_id` cross the gRPC boundary into agent-side logs.

**Deliverables.**

1. Outbound metadata injection in `internal/executor/dispatch.go` and `internal/executor/chat.go`.
2. Inbound gRPC server interceptor in `agents/observability/grpc_logging.py`, registered in `agents/server.py`.
3. Integration test: submit a workflow, assert agent-side log lines for that workflow contain matching `execution_id` and `step_id`.

**Dependencies.** Phase 1, Phase 2.

### Phase 4: Ring Buffer + `logs` Endpoint + CLI Display

**Summary.** Make `persatrix logs <id>` actually work.

**Deliverables.**

1. `internal/observability/logbuffer` package with `Buffer` type and bounded eviction.
2. Custom zap core writing to the buffer alongside stdout.
3. Agent-side log shipper (mechanism per [Open Question 1](#open-questions)) feeding the same buffer.
4. `handleGetLogs` rewritten; remove from `stub_handlers.go`.
5. `cli/src/commands/logs.rs` rewritten to parse and display the new response shape.
6. E2E test: submit a workflow, call `persatrix logs <id>`, assert output contains entries from both Go and Python sides with correct `execution_id`.
7. Operations guide section in `docs/observability.md` covering `persatrix logs` usage and the in-memory limitation.

**Dependencies.** Phase 3.

### Phase 5 (reserved): Review Follow-Ups + RFC Close

Per [development-workflow.md](../development-workflow.md) Phase 5–8.

---

## Files Touched (Estimated)

| Component | Files | Change |
|-----------|-------|--------|
| Docs | `docs/observability.md` | Add (new — schema + operations guide) |
| Python agents | `agents/observability/__init__.py`, `agents/observability/logging.py`, `agents/observability/grpc_logging.py` | Add (new modules) |
| Python agents | `agents/pyproject.toml` | Add `structlog` runtime dep |
| Python agents | `agents/server.py`, `agents/base.py`, `agents/llm_client.py`, `agents/dispatch.py`, `agents/persona.py`, `agents/persona_behavior.py`, others | Replace `logging.getLogger` with `structlog.get_logger` |
| Go orchestrator | `internal/executor/dispatch.go`, `internal/executor/chat.go` | Inject correlation IDs into outgoing gRPC metadata |
| Go orchestrator | `internal/observability/logbuffer/` (new package) | Bounded ring buffer keyed by execution ID |
| Go orchestrator | `cmd/orchestrator/main.go` | Wire custom zap core that fans out to ring buffer |
| Go orchestrator | `internal/server/server.go` (handler registration), new `internal/server/logs_handler.go` | Implement `handleGetLogs`; remove from `stub_handlers.go` |
| Go orchestrator | `internal/server/stub_handlers.go` | Remove `handleGetLogs` stub |
| Rust CLI | `cli/src/commands/logs.rs` | Rewrite display formatting for new response shape |
| Tests | `agents/tests/test_observability_logging.py`, `agents/tests/test_grpc_logging_interceptor.py` (new) | Unit + integration coverage |
| Tests | `internal/observability/logbuffer/buffer_test.go` (new) | Unit tests for buffer eviction |
| Tests | `internal/server/logs_handler_test.go` (new) | Endpoint tests |
| Tests | `tests/integration/test_logs_e2e.py` (new) | E2E correlation test |
| Docs | [ROADMAP.md](../../ROADMAP.md) | Add v0.2.3 milestone; add RFC 0018 to tracker |

No changes to: protos, schemas, JSON schemas, blueprints, workflows.

---

## Test Strategy

- **Unit tests — `agents/observability/logging.py`**: schema fields present on default JSON output; `PERSATRIX_LOG_FORMAT=pretty` selects console renderer; context vars merge into output.
- **Unit tests — `agents/observability/grpc_logging.py`**: interceptor extracts the three metadata keys; missing headers don't crash; context is cleared after handler returns.
- **Unit tests — `internal/observability/logbuffer`**: per-execution capacity respected; LRU eviction across executions; concurrent writes safe; reads return entries in insertion order.
- **Unit tests — `internal/server/logs_handler.go`**: empty execution returns empty array; populated execution returns entries; invalid execution ID format returns 400.
- **Integration test — agent-side correlation**: invoke an `AgentService.HandleTask` with metadata; assert all log lines emitted during the call carry the IDs.
- **E2E test — `persatrix logs`**: submit a small workflow, run `persatrix logs <execution_id>`, assert output has entries from both `orchestrator` and `agent-<id>` services for the same `execution_id`.
- **Manual smoke**: run `make run`, submit a workflow via `persatrix run`, then `persatrix logs <id>`; eyeball the output. Toggle `PERSATRIX_LOG_FORMAT=pretty` and re-run.

---

## Open Questions

1. **Agent-side log shipping mechanism.** How do Python log entries reach the orchestrator's ring buffer? Options:
   - **(a)** Agent posts each entry to a small internal HTTP endpoint on the orchestrator (`POST /internal/logs`). Simple, adds an HTTP hop per log line.
   - **(b)** Reuse the existing gRPC channel — a streaming RPC `StreamLogs` from agent to orchestrator. Lower overhead, requires proto change.
   - **(c)** Out of scope for v0.2.3 — orchestrator and agents each maintain their own buffer, `persatrix logs` queries both. More complex on the CLI side.
   Recommendation: **(a)** for v0.2.3; revisit if log volume becomes a problem.
2. **Ring buffer capacity defaults.** Per-execution: 1000 entries. Total executions retained: 50. Total memory cap implied: ~50k entries. Acceptable? Confirm before Phase 4.
3. **Backward compatibility of log field names.** Renaming `executionID` → `execution_id` in zap output is a breaking change for any operator currently parsing Persatrix logs. CHANGELOG entry is sufficient at v0.2.x; no compatibility shim needed.
4. **Should `pretty` mode be the default for `make run` (developer ergonomics)?** Recommendation: keep JSON default everywhere; document `PERSATRIX_LOG_FORMAT=pretty` as the standard local-dev override in the README quick-start.

---

## Decision / Next Steps

**To accept this RFC:**

1. Confirm v0.2.3 as the target milestone (this RFC adds v0.2.3 to the ROADMAP version map alongside RFC 0019).
2. Resolve Open Question 1 (log shipping mechanism) before Phase 4 starts.
3. Sign off on the schema in [Section B](#b-common-log-schema) as the cross-language contract.

**Once accepted:**

1. Author `docs/rfcs/0018-pr-plan.md` per [development-workflow.md](../development-workflow.md) Phase 3.
2. Status → 🚧 Implementing; ROADMAP updated.
3. Begin Phase 1 implementation.

---

## Related Documentation

- [RFC 0019 — OpenTelemetry Completion](0019-opentelemetry-completion.md) (paired RFC; targets the same release)
- [RFC 0009 — Agent Identity, Security & Sandboxing](0009-security-sandboxing.md) (auth layer that will eventually gate `logs` endpoint)
- [Development Workflow](../development-workflow.md)
- [Branching Strategy](../BRANCHING.md)
- [internal/server/stub_handlers.go](../../internal/server/stub_handlers.go) (the stub being replaced)
- [cli/src/commands/logs.rs](../../cli/src/commands/logs.rs) (CLI command being completed)
- [internal/server/middleware.go](../../internal/server/middleware.go) (`request_id` precedent)
