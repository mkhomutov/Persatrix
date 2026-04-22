# RFC 0018 — Structured Logging Framework

**Type**: architecture
**Status**: 📋 Proposed
**Author**: Maksim Khomutov
**Date**: 2026-04-21 (rev 2026-04-22)
**Target**: v0.2.3
**Schema version**: 1
**Depends on**: none
**Pairs with**: RFC 0019 (OTEL completion — log↔trace correlation lands jointly in v0.2.3)

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
  - [E. `persatrix logs` Endpoint, Storage, and Streaming](#e-persatrix-logs-endpoint-storage-and-streaming)
  - [F. Redaction Hook](#f-redaction-hook)
- [Security Considerations](#security-considerations)
- [Phased Implementation Plan](#phased-implementation-plan)
- [Files Touched (Estimated)](#files-touched-estimated)
- [Test Strategy](#test-strategy)
- [Resolved Decisions](#resolved-decisions)
- [Decision / Next Steps](#decision--next-steps)
- [Related Documentation](#related-documentation)

---

## Summary

This RFC defines Persatrix's observability foundation for logs: a single versioned JSON line schema emitted by the Go orchestrator, Python agents, and the Rust CLI display path; correlation IDs (`execution_id`, `step_id`, `agent_id`) and OTEL trace context (`trace_id`, `span_id`) on every log line emitted within a workflow execution; a streaming `LogService` gRPC RPC carrying agent log entries to the orchestrator; a bounded ring buffer with on-disk durability so `persatrix logs <execution-id>` survives orchestrator restarts; server-side filters (`--since`, `--workflow`, `--level`) and a `--follow` mode over Server-Sent Events; and a redaction hook surface ready for a future security RFC to plug into without revisiting every call site.

The scope is intentionally larger than "operability hygiene." The goal is to land the observability foundation **once**, designed for the project's full lifetime, rather than ship the same surface across three or four releases with breaking schema changes between them.

## Motivation

Persatrix already has structured logging on the Go side (`go.uber.org/zap`) but Python uses stdlib `logging` with free-form messages, Rust uses `println!`/`eprintln!`, and there is no shared schema across the three. A workflow execution crosses Go → Python via gRPC; correlating a single run today requires manually matching timestamps across processes.

Concrete gaps:

1. **No shared schema.** Three formats, none parseable as a unit.
2. **No cross-process correlation.** `request_id` is added by `internal/server/middleware.go` but is not propagated into agent-side logs. `execution_id` and `step_id` are not in any log line on the Python side.
3. **No log↔trace bridge.** RFC 0019 stands up end-to-end traces, but a log line and its corresponding span have nothing in common to join on. Operators land in Jaeger from a log line by manual timestamp scrolling.
4. **`logs` CLI endpoint is a stub.** [`internal/server/stub_handlers.go`](../../internal/server/stub_handlers.go) returns 501 for `GET /api/v1/executions/{id}/logs`. The Rust CLI [`cli/src/commands/logs.rs`](../../cli/src/commands/logs.rs) is wired and discoverable in `--help`, so users find it, run it, and hit the stub. Worse than not having the command.
5. **Logs are lost on orchestrator restart.** The most common moment an operator wants logs is right after the thing that crashed.

What happens if we do nothing: every operator-facing issue requires the author to debug it for them. Every contributor PR review that depends on understanding cross-process behaviour requires running the code themselves rather than reading log output. The stubbed endpoint stays in `--help` indefinitely, and the v0.3.0 surface area expands on top of an observability story that doesn't work.

## Goals

1. A single versioned JSON-line log schema (`schema_version: 1`) is documented in `docs/observability.md` (new file created by this RFC) and emitted by all three processes.
2. Python agents emit structured logs via `structlog` conforming to the schema in [Section B](#b-common-log-schema).
3. Go orchestrator zap output conforms to the same schema (field renames where needed; the structured emit is already in place).
4. `execution_id`, `step_id`, and `agent_id` are propagated from orchestrator into Python agents via gRPC metadata, bound to logger context on entry, and present on every log line emitted while handling that task.
5. **Log↔trace correlation.** When an OTEL context is active in the emitting goroutine/coroutine, `trace_id` and `span_id` are emitted on the log line automatically. This is in scope for v0.2.3 and lands jointly with RFC 0019.
6. `GET /api/v1/executions/{id}/logs` returns `{"execution_id": "...", "entries": [...]}` with a merged stream of orchestrator-side and agent-side entries for that execution. Server-side filters (`--since`, `--workflow`, `--level`) are supported as query parameters.
7. `GET /api/v1/executions/{id}/logs/stream` is a Server-Sent Events endpoint backing `persatrix logs --follow`.
8. `persatrix logs <execution-id>` displays entries with colourised level, timestamp, service, message; `--verbose` shows the full attribute and source maps; `--follow` tails live entries; `--since`, `--workflow`, `--level` apply server-side filters.
9. Ring buffer entries are persisted to a bounded on-disk append-only store; `persatrix logs <id>` works across orchestrator restarts.
10. A redaction hook surface (no-op default) is wired into both Go zap and Python structlog so a future security RFC can plug in real PII/secret scrubbing without touching every call site.
11. `PERSATRIX_LOG_FORMAT=pretty` toggles a human-friendly renderer for local development; default remains JSON across `make run`, CI, and production.
12. Rust CLI logging is in scope only for the `logs` command's display formatting. CLI-internal logging migration to `tracing` is deferred.

## Non-Goals

- **Centralised log aggregation.** Loki / ELK / Splunk / Datadog integration. Operators pipe the JSON stream wherever they want; the on-disk store gives them a stable format to ship from.
- **Search / query DSL in the CLI.** `persatrix logs` exposes a small fixed set of server-side filters; arbitrary queries pipe to `jq`, `grep`, or `rg`.
- **Log shipping or forwarding built into Persatrix.**
- **Log-based alerting or monitoring.** Logs are for diagnosis; alerting belongs to traces and metrics.
- **A real PII / secret scrubber.** This RFC ships the **hook surface only** (no-op default). The actual redactor lives in a future security RFC under RFC 0009's umbrella.
- **Migrating Rust CLI internals to `tracing`.** Out of scope; the CLI is a thin client.

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

One JSON object per line, one event per object. Field emission order is **stable** (defined below) for diffability of captured logs across runs.

#### Required fields (in emission order)

| Order | Field | Type | Notes |
|-------|-------|------|-------|
| 1 | `schema_version` | string | `"1"` for this RFC. Future breaking changes increment this. |
| 2 | `timestamp` | string | RFC 3339 with timezone; UTC by default. |
| 3 | `level` | string | One of `DEBUG`, `INFO`, `WARN`, `ERROR`. |
| 4 | `service.kind` | string | One of `orchestrator`, `agent`, `cli`. |
| 5 | `service.instance` | string | Process instance identity (orchestrator node ID, agent ID, CLI invocation ID). |
| 6 | `message` | string | Human-readable; should not include the structured fields. |

#### Optional fields (in emission order, present when applicable)

| Order | Field | Type | Notes |
|-------|-------|------|-------|
| 7 | `service.role` | string | For agents: `coder`, `reviewer`, `persona`, etc. Omitted otherwise. |
| 8 | `execution_id` | string | Workflow run ID. |
| 9 | `step_id` | string | Step within a workflow. |
| 10 | `agent_id` | string | Source or target agent (depending on log site). For `service.kind=agent`, equals `service.instance`. |
| 11 | `request_id` | string | HTTP request ID (orchestrator only, set by middleware). |
| 12 | `trace_id` | string | OTEL trace ID when an OTEL context is active. |
| 13 | `span_id` | string | OTEL span ID when an OTEL context is active. |
| 14 | `attributes` | object | Free-form key/value bag for site-specific context. |
| 15 | `source` | object | `{file, line, function}` of the call site. |

`service.kind` / `service.instance` / `service.role` are emitted as a flat-keyed group (e.g., `"service.kind": "agent"`, `"service.instance": "ember-owl"`) rather than nested, to keep `jq` / `grep` workflows simple.

#### Versioning

The schema is **versioned**. Adding optional fields is non-breaking and does not bump `schema_version`. Removing fields, renaming fields, changing field types, or changing the meaning of an existing field bumps `schema_version` to `"2"` and is called out in CHANGELOG. Consumers can branch on the version field cleanly.

### C. Per-Language Adoption

**Go orchestrator.** Already structured via zap. Work consists of:

- Audit existing `logger.Info/Error/Debug/Warn` call sites, normalise field names to match the schema (`execution_id` not `executionID`, `agent_id` not `agentID`, `service.kind` / `service.instance` replacing the existing implicit service identification).
- Add a zap encoder wrapper that emits `schema_version`, the `service.*` group, the OTEL `trace_id` / `span_id` (read from the goroutine's OTEL context via `trace.SpanContextFromContext`), and `source` (using zap's `WithCaller`).
- Add a zap encoder pass that calls the redaction hook ([Section F](#f-redaction-hook)) before serialisation.
- Wire `PERSATRIX_LOG_FORMAT=pretty` to the zap dev encoder; default stays JSON production encoder.
- No new external dependency.

**Python agents.** Adopt `structlog` with a JSON renderer:

- Add `structlog>=24.1` to `agents/pyproject.toml` runtime deps.
- New module `agents/observability/logging.py` configures the processor chain in this order: timestamp → level → contextvars merger → caller (`structlog.processors.CallsiteParameterAdder`) → OTEL context merger (reads `opentelemetry.trace.get_current_span()`, emits `trace_id` / `span_id` when valid) → redaction hook → key reorderer → JSON renderer. Exposes `get_logger(name)`.
- Replace `logging.getLogger(__name__)` with `structlog.get_logger(__name__)` across `agents/`. Existing `logger.info("text")` call sites work unchanged; structured fields move to keyword args (`logger.info("text", key=value)`).
- `PERSATRIX_LOG_FORMAT=pretty` swaps the JSON renderer for `structlog.dev.ConsoleRenderer`.

**Rust CLI.** Out of scope for internal logs. The `logs` command formats incoming JSON entries from the server for terminal display using existing CLI patterns (no `tracing` adoption).

### D. Cross-Process Correlation

Three IDs travel from orchestrator to agent and into the agent's log context: `execution_id`, `step_id`, `agent_id`. OTEL `trace_id` / `span_id` ride the W3C TraceContext channel installed by RFC 0019.

**Outbound from Go.** The executor already has these in-scope at gRPC dispatch sites (`internal/executor/dispatch.go`, `internal/executor/chat.go`). Inject them into outgoing gRPC metadata using the keys `persatrix-execution-id`, `persatrix-step-id`, `persatrix-agent-id`.

**Inbound on Python.** A new gRPC server interceptor in `agents/observability/grpc_logging.py` extracts those headers from every incoming RPC and binds them to `structlog.contextvars` for the duration of the handler. Every log line emitted during that RPC then automatically carries them.

**Key conventions.** Outbound gRPC metadata uses kebab-case keys (`persatrix-execution-id`) because gRPC HTTP/2 metadata keys are required to be lowercase and the kebab form matches existing HTTP-style header conventions used elsewhere in the codebase. OTEL span attribute keys in RFC 0019 use dotted notation (`persatrix.execution_id`) because that is the OTEL semantic-conventions style. The two namespaces are independent; the divergence is intentional.

**OTEL interceptor coexistence.** RFC 0019's `GrpcInstrumentorServer` is installed first so the logging-context interceptor can see `trace_id` / `span_id` on the active span when binding contextvars. Order is documented in `agents/server.py`.

### E. `persatrix logs` Endpoint, Storage, and Streaming

#### Storage

A new `internal/observability/logbuffer` package implements a tiered store keyed by `execution_id`:

- **In-memory ring per execution.** Capacity defaults `PERSATRIX_LOGBUFFER_PER_EXEC=1000`; total executions retained `PERSATRIX_LOGBUFFER_MAX_EXEC=50`. Worked memory bound: ~25 MB steady-state at 500 B average entry, ~40 MB ceiling at 800 B/entry — well within the orchestrator's existing envelope.
- **On-disk durability.** Each execution's entries are append-written to `data/logs/<execution_id>.jsonl` (path overridable via `PERSATRIX_LOGBUFFER_DIR`). Total disk cap defaults `PERSATRIX_LOGBUFFER_DISK_MB=512`; oldest completed-execution files are evicted first. On startup, the ring buffer warm-loads the last N completed executions from disk so `persatrix logs <id>` works across restarts.
- **Drop-level filter.** `PERSATRIX_LOGBUFFER_DROP_LEVEL` (default `DEBUG`) excludes high-volume entries from the buffer without disabling stdout emission.
- **Per-execution rate limit.** Token-bucket of 1000 entries/s per (execution, level), tunable via `PERSATRIX_LOGBUFFER_RATE_PER_EXEC`. Drops are counted in `logs_buffer_dropped_total{reason="rate_limit"}` and surfaced as a single throttled `WARN` log per execution.
- **LRU policy.** Per-execution overflow drops oldest-in-execution. Total-cap overflow LRU-evicts whole executions (memory + disk together).
- **Ring lifecycle.** Each execution's ring is created on first write (lazy) and **sealed** when the executor records a terminal state (`completed`, `failed`, `cancelled`). A sealed ring is immutable and remains queryable until LRU-evicted. The Phase 4 PR plan pins the exact hook site (state-store transition or executor terminal handler).

#### Cross-process delivery: `LogService` streaming gRPC

Agent log entries reach the orchestrator's buffer over a long-lived bidirectional gRPC stream:

```protobuf
// proto/log_service.proto (new)
service LogService {
  // Agent → Orchestrator: stream log entries in batches.
  // Orchestrator responds with periodic acks for backpressure.
  rpc StreamLogs(stream LogBatch) returns (stream LogAck);
}

message LogBatch {
  repeated LogEntry entries = 1;
  string agent_id = 2;
}

message LogEntry {
  string schema_version = 1;
  google.protobuf.Timestamp timestamp = 2;
  string level = 3;
  string service_kind = 4;
  string service_instance = 5;
  string service_role = 6;
  string message = 7;
  string execution_id = 8;
  string step_id = 9;
  string agent_id = 10;
  string request_id = 11;
  string trace_id = 12;
  string span_id = 13;
  google.protobuf.Struct attributes = 14;
  Source source = 15;
  message Source { string file = 1; uint32 line = 2; string function = 3; }
}

message LogAck {
  uint64 received_through_seq = 1;
}
```

Why streaming gRPC over an internal HTTP loopback endpoint:

- **One transport, one auth model.** Persatrix already commits to gRPC as the orchestrator↔agent contract. Adding a sidecar HTTP loopback creates a second transport with its own auth story (shared secret, loopback binding, header validation) — permanent technical debt to dodge a one-time proto change.
- **Backpressure for free.** HTTP/2 flow control end-to-end. Unary HTTP would require reinventing batching + bounded queues + retry on top, and getting it subtly wrong.
- **Distributed-deployment ready.** v0.3 RFCs (mesh, A2A, multi-node) assume agents may not be co-located with the orchestrator. A loopback HTTP endpoint hard-codes the "same host" assumption; a streaming RPC works the same locally and across the network.
- **Lower per-line overhead.** A long-lived stream amortises connection cost; batching window of 100 ms or 50 entries (whichever first) on the agent side keeps stream chatter low.

**Agent-side shipper.** `agents/observability/log_shipper.py` runs a background task that consumes a bounded queue (default 1000 entries; overflow drops oldest, increments `logs_shipped_dropped_total`), batches, and writes to the `StreamLogs` client stream. On stream errors, exponential backoff with jitter; on reconnect, resumes from the next entry (no replay — durability is the orchestrator's job once entries arrive).

**Authentication.** The stream piggybacks on the existing agent gRPC channel auth (RFC 0009 will tighten this; today both channels are unauthenticated, consistent with the rest of the v0.2.x REST/gRPC surface).

**Namespace rationale.** The new package lives under `internal/observability/` rather than extending `internal/telemetry/` because `telemetry` is currently scoped to OTEL traces (and, in RFC 0019, metrics); logs follow a different lifecycle (per-execution ring + on-disk store + HTTP/SSE endpoint surface). A future RFC may consolidate `telemetry` and `observability` under one root once the boundaries stabilise — a tracking issue is opened at RFC closure (mirrored in [RFC 0019](0019-opentelemetry-completion.md)).

#### REST endpoints

`GET /api/v1/executions/{id}/logs` — non-streaming, JSON response:

- Query params: `since=<RFC3339>`, `workflow=<id>`, `level=<DEBUG|INFO|WARN|ERROR>`, `limit=<int>` (default 1000).
- When `id` is the literal `_`, filters apply across all retained executions.
- Returns `{"execution_id": "...", "entries": [...]}`. Each entry conforms to [Section B](#b-common-log-schema).

`GET /api/v1/executions/{id}/logs/stream` — Server-Sent Events:

- Same query params as above (applied as initial filter; entries arriving after subscription are also filtered).
- Each event is a single JSON entry on a `data:` line.
- Backs `persatrix logs --follow`. SSE is chosen over WebSocket because it composes with the existing HTTP server, requires no protocol negotiation, and matches the unidirectional server→client semantics.

#### CLI

`cli/src/commands/logs.rs` parses entries and prints:

- `<timestamp> <LEVEL> <service.kind>/<service.instance> [<step_id>] <message>` by default.
- `--verbose` adds `attributes` and `source` pretty-printed below each line.
- Level colourisation via the existing `colored` crate.
- `--follow` consumes the SSE endpoint.
- `--since <duration>`, `--workflow <id>`, `--level <level>` map to query params.
- Multi-execution mode: `persatrix logs --since 10m` invokes `id=_` with the `since` filter.

### F. Redaction Hook

A small interface both loggers call before serialisation. Default implementation is a no-op pass-through.

**Go:**

```go
// internal/observability/redact/redact.go
type Redactor interface {
    Redact(entry map[string]any) map[string]any
}
type NoopRedactor struct{}
func (NoopRedactor) Redact(e map[string]any) map[string]any { return e }
```

The orchestrator's zap setup wires the configured `Redactor` (default `NoopRedactor`) into a custom `zapcore.Encoder` wrapper. A future security RFC implements a real `Redactor` and registers it via DI in `cmd/orchestrator/main.go` — no log call site changes.

**Python:**

```python
# agents/observability/redact.py
class Redactor(Protocol):
    def redact(self, event_dict: dict) -> dict: ...

class NoopRedactor:
    def redact(self, event_dict: dict) -> dict: return event_dict
```

Wired into the structlog processor chain immediately before the JSON renderer. Same swap mechanism.

This RFC ships the hook surface, default no-op, and the wiring; it does **not** ship a real redactor.

---

## Security Considerations

- **Log content can leak secrets.** Existing call sites occasionally log argument summaries that may include LLM prompts, tool inputs, or user chat content. This RFC introduces a CLI surface that exposes those logs over HTTP and a streaming gRPC channel that ships them between processes. The endpoint is currently unauthenticated, consistent with the rest of the v0.2.x REST API. The redaction hook ([Section F](#f-redaction-hook)) is in place but ships with a no-op default. A future security RFC under RFC 0009's umbrella plugs in real redaction without touching call sites.
- **Ring buffer / disk DoS.** Per-execution rate limiting and per-execution capacity contain blast radius for high-volume agents. The total-execution cap and disk cap prevent one runaway run from exhausting orchestrator resources.
- **Cross-process delivery.** `LogService.StreamLogs` rides the existing agent gRPC channel — no new authenticated surface, no new listening sockets. Authn piggybacks on whatever the agent gRPC channel uses today; tightened by RFC 0009.
- **On-disk store.** Files in `data/logs/` inherit the orchestrator process's umask. Default file mode is `0o600` (owner read/write only). Documented in operations guide.
- **No new external dependencies for Go.** Python adds `structlog` (well-maintained, MIT licensed, no transitive C deps).
- **`pretty` output is opt-in.** Pretty-printed logs may include ANSI escapes; JSON default is safe to pipe into machine consumers.
- **SSE endpoint resource usage.** A long-lived `--follow` consumer holds a goroutine and a buffer-side subscription. The orchestrator caps concurrent SSE subscribers per execution at 16 (configurable via `PERSATRIX_LOGS_SSE_MAX_SUBS_PER_EXEC`); excess connections receive `429 Too Many Requests`.

---

## Phased Implementation Plan

### Phase 1: Schema + Python `structlog` Adoption + Redaction Hook

**Summary.** Document the schema, add `structlog`, replace stdlib logging in `agents/`, ship the no-op redactor surface. No cross-process work yet.

**Deliverables.**

1. `docs/observability.md` defining the schema in [Section B](#b-common-log-schema) including the field ordering contract.
2. `agents/observability/__init__.py`, `agents/observability/logging.py`, `agents/observability/redact.py` (no-op default).
3. Replace `logging.getLogger` call sites in `agents/`.
4. `PERSATRIX_LOG_FORMAT=pretty` env override.
5. Unit tests asserting JSON output shape, `schema_version`, field ordering, redactor pass-through.

**Dependencies.** None.

### Phase 2: Go Field Normalisation + `pretty` Encoder + Redaction Hook + Source Field

**Summary.** Make zap output conform to the schema; add the `pretty` mode; wire the no-op redactor; emit `source`.

**Deliverables.**

1. Audit and rename zap field keys (script-assisted) to schema names; emit `service.kind` / `service.instance` / optional `service.role`; add `schema_version`.
2. `internal/observability/redact/` package with `NoopRedactor`; wired into a `zapcore.Encoder` wrapper.
3. `PERSATRIX_LOG_FORMAT=pretty` wired to zap dev encoder; default JSON.
4. CHANGELOG entry under v0.2.3 enumerating renamed zap field keys (old → new table).
5. README quick-start updated with `PERSATRIX_LOG_FORMAT=pretty` example.
6. Tests asserting field name presence, ordering, and redactor invocation.

**Dependencies.** Phase 1 (schema document is the source of truth).

### Phase 3: Cross-Process Correlation + OTEL Trace IDs on Logs

**Summary.** `execution_id` / `step_id` / `agent_id` cross the gRPC boundary into agent-side logs; `trace_id` / `span_id` appear on every log line within an OTEL context.

**Deliverables.**

1. Outbound metadata injection in `internal/executor/dispatch.go` and `internal/executor/chat.go`.
2. Inbound gRPC server interceptor in `agents/observability/grpc_logging.py`, registered in `agents/server.py` (after `GrpcInstrumentorServer` from RFC 0019).
3. OTEL context reader in the Go zap encoder wrapper (`trace.SpanContextFromContext`).
4. OTEL context processor in the Python structlog chain.
5. Integration test: submit a workflow, assert agent-side log lines for that workflow contain matching `execution_id`, `step_id`, `trace_id`, `span_id`.

**Dependencies.** Phase 1, Phase 2, RFC 0019 Phase 1 (OTEL initialised on Python side).

### Phase 4: Ring Buffer + On-Disk Store + `LogService` Streaming + Endpoints + CLI

**Summary.** Make `persatrix logs <id>` actually work, including `--follow`, filters, and durability.

**Deliverables.**

1. `proto/log_service.proto` (new); regenerated Go (`internal/generated/`) and Python (`agents/generated/`) stubs.
2. `internal/observability/logbuffer` package: in-memory ring + disk store + LRU + rate limiting + ring lifecycle (seal-on-terminal).
3. Custom zap core writing to the buffer alongside stdout.
4. Agent-side `agents/observability/log_shipper.py` streaming via `LogService.StreamLogs` with bounded queue and reconnect-with-backoff.
5. Orchestrator-side `LogService` server registered on the existing agent gRPC server.
6. `internal/server/logs_handler.go`: `GET /api/v1/executions/{id}/logs` with `since` / `workflow` / `level` / `limit` filters; `id=_` for cross-execution. Remove from `stub_handlers.go`.
7. `internal/server/logs_stream_handler.go`: SSE endpoint for `--follow`.
8. `cli/src/commands/logs.rs`: rewrite display + `--verbose` + `--follow` + `--since` + `--workflow` + `--level`.
9. E2E test: submit a workflow, call `persatrix logs <id>`, assert merged Go + Python entries; restart orchestrator, assert entries still queryable.
10. Operations guide section in `docs/observability.md` covering CLI usage, env-var knobs, on-disk store layout, and the `data/logs/` umask note.

**Dependencies.** Phase 3.

### Phase 5: Review Follow-Ups + RFC Close

Per [development-workflow.md](../development-workflow.md) Phase 5–8. Closure checklist must include opening a tracking issue titled "Consolidate `internal/telemetry/` and `internal/observability/`" with a v0.3.x or later target (mirrored in RFC 0019).

---

## Files Touched (Estimated)

| Component | Files | Change |
|-----------|-------|--------|
| Docs | `docs/observability.md` | Add (new — schema + operations guide) |
| Docs | [README.md](../../README.md) | Add `PERSATRIX_LOG_FORMAT=pretty` to quick-start |
| Docs | [CHANGELOG.md](../../CHANGELOG.md) | v0.2.3 entry: zap field renames table; new endpoints; new env vars |
| Docs | [ROADMAP.md](../../ROADMAP.md) | Add v0.2.3 milestone; add RFC 0018 to tracker |
| Protos | `proto/log_service.proto` | Add (new) |
| Generated | `internal/generated/log_service*.go`, `agents/generated/log_service*.py` | Regenerated |
| Python agents | `agents/observability/__init__.py`, `agents/observability/logging.py`, `agents/observability/grpc_logging.py`, `agents/observability/log_shipper.py`, `agents/observability/redact.py` | Add (new modules) |
| Python agents | `agents/pyproject.toml` | Add `structlog` runtime dep |
| Python agents | `agents/server.py`, `agents/base.py`, `agents/llm_client.py`, `agents/dispatch.py`, `agents/persona.py`, `agents/persona_behavior.py`, others | Replace `logging.getLogger` with `structlog.get_logger` |
| Go orchestrator | `internal/observability/logbuffer/` | Add (new package) — ring + disk + LRU + rate limit |
| Go orchestrator | `internal/observability/redact/` | Add (new package) — no-op redactor surface |
| Go orchestrator | `internal/observability/zapcore/` | Add (new) — encoder wrapper for `service.*`, OTEL IDs, source, redaction hook |
| Go orchestrator | `internal/executor/dispatch.go`, `internal/executor/chat.go` | Inject correlation IDs into outgoing gRPC metadata |
| Go orchestrator | `cmd/orchestrator/main.go` | Wire encoder wrapper, register `LogService` server, register redactor (no-op) |
| Go orchestrator | `internal/server/logs_handler.go` (new), `internal/server/logs_stream_handler.go` (new), `internal/server/server.go` | Implement endpoints; remove `handleGetLogs` from `stub_handlers.go` |
| Go orchestrator | `internal/server/stub_handlers.go` | Remove `handleGetLogs` stub |
| Rust CLI | `cli/src/commands/logs.rs` | Rewrite: display, filters, `--follow` (SSE consumer) |
| Rust CLI | `cli/Cargo.toml` | Add SSE / `eventsource-stream` dep if not present |
| Tests | `agents/tests/test_observability_logging.py`, `agents/tests/test_grpc_logging_interceptor.py`, `agents/tests/test_log_shipper.py` (new) | Unit + integration |
| Tests | `internal/observability/logbuffer/buffer_test.go`, `internal/observability/logbuffer/disk_test.go` (new) | Unit tests for eviction, durability, rate limit |
| Tests | `internal/server/logs_handler_test.go`, `internal/server/logs_stream_handler_test.go` (new) | Endpoint tests |
| Tests | `tests/integration/test_logs_e2e.py` (new) | E2E: correlation, filters, restart durability, follow |

No changes to: agent / channel / workflow JSON schemas, blueprints, workflow YAML.

---

## Test Strategy

- **Unit — `agents/observability/logging.py`**: schema fields present and ordered; `schema_version` emitted; `PERSATRIX_LOG_FORMAT=pretty` selects console renderer; contextvars merge; OTEL processor adds `trace_id`/`span_id` when a span is active and omits them when not; redactor is invoked.
- **Unit — `agents/observability/grpc_logging.py`**: interceptor extracts the three metadata keys; missing headers don't crash; context is cleared after handler returns.
- **Unit — `agents/observability/log_shipper.py`**: bounded queue overflow drops oldest and increments counter; reconnect-with-backoff on stream errors; flush on shutdown.
- **Unit — `internal/observability/logbuffer`**: per-execution capacity respected; LRU eviction across executions; disk persistence + warm-load on startup; rate limiter drops with reason; ring sealing on terminal state; concurrent writes safe.
- **Unit — `internal/server/logs_handler.go`**: empty execution returns empty array; filters (`since`, `workflow`, `level`) applied correctly; `id=_` cross-execution; invalid execution ID format returns 400.
- **Unit — `internal/server/logs_stream_handler.go`**: SSE framing; subscriber cap returns 429; client disconnect cleans up subscription.
- **Integration — agent-side correlation**: invoke `AgentService.HandleTask` with metadata + active OTEL context; assert all log lines emitted during the call carry IDs and trace IDs.
- **Integration — `LogService` streaming**: agent ships entries; orchestrator buffer receives them; ack mechanism advances; agent reconnects after orchestrator restart.
- **E2E — `persatrix logs`**: submit a small workflow, run `persatrix logs <execution_id>`, assert merged Go + Python entries, matching `execution_id`, valid `trace_id`. Run `persatrix logs --follow` against a long-running workflow; observe live entries. Restart orchestrator mid-test; re-run `persatrix logs <id>`; assert pre-restart entries still present.
- **Manual smoke**: `make run`, submit a workflow via `persatrix run`, then `persatrix logs <id>`, `--follow`, `--since 5m`. Toggle `PERSATRIX_LOG_FORMAT=pretty` and re-run.

---

## Resolved Decisions

The following decisions are part of the spec; they are recorded here for traceability and not deferred.

1. **Agent-side log shipping mechanism: streaming gRPC `LogService.StreamLogs`.** Chosen over loopback HTTP and over per-process buffers. Rationale in [Section E](#e-persatrix-logs-endpoint-storage-and-streaming). Adds a one-time proto change; pays back in unified transport, free backpressure, distributed-deployment readiness, and lower per-line overhead.
2. **Ring buffer defaults: 1000 entries × 50 executions; 512 MB on-disk cap.** Worked memory bound ~25 MB steady-state. All knobs exposed as env vars (`PERSATRIX_LOGBUFFER_PER_EXEC`, `PERSATRIX_LOGBUFFER_MAX_EXEC`, `PERSATRIX_LOGBUFFER_DISK_MB`, `PERSATRIX_LOGBUFFER_DIR`, `PERSATRIX_LOGBUFFER_DROP_LEVEL`, `PERSATRIX_LOGBUFFER_RATE_PER_EXEC`).
3. **Backward compatibility of zap field names: clean break, CHANGELOG entry only.** No compatibility shim. Pre-1.0; field names are not in any public contract (no proto, no schema, no spec doc references them).
4. **`pretty` default: JSON everywhere, including `make run` and CI.** `PERSATRIX_LOG_FORMAT=pretty` documented as the standard local-dev override. Avoids the "works locally, looks different in CI" footgun.
5. **Schema is versioned from day one.** `schema_version: "1"`. Adding optional fields is non-breaking; all other changes bump the version.
6. **`service` is a structured tuple, not a free string.** `service.kind` / `service.instance` / `service.role`. Aligns with future multi-node deployments and OTEL `service.*` semantic conventions.
7. **`source` (file/line/function) is in scope.** Cheap on both sides; significant debugging win.
8. **Field emission order is stable.** Improves diffability of captured logs.
9. **`--follow` is in scope via SSE.** WebSocket rejected (protocol negotiation overhead, bidirectional semantics not needed).
10. **Server-side filters (`--since`, `--workflow`, `--level`, `id=_`) are in scope.** Covers the 80% operator workflow without becoming a query DSL.
11. **Redaction hook surface ships in v0.2.3, no-op default.** Real redactor is a future security RFC; the surface is in place so that RFC does not require touching every call site.
12. **Log↔trace correlation lands in v0.2.3.** Not deferred. The whole point of standing up structured logs and OTEL together is operator pivot from a log line to a trace and back.

---

## Decision / Next Steps

**To accept this RFC:**

1. Confirm v0.2.3 as the target milestone (this RFC adds v0.2.3 to the ROADMAP version map alongside RFC 0019).
2. Sign off on the schema in [Section B](#b-common-log-schema) as the cross-language contract (including `schema_version: "1"` and the structured `service.*` group).
3. Sign off on `LogService` as a new public proto surface in `proto/log_service.proto`.

**Once accepted:**

1. Author `docs/rfcs/0018-pr-plan.md` per [development-workflow.md](../development-workflow.md) Phase 3.
2. Status → 🚧 Implementing; ROADMAP updated.
3. Begin Phase 1 implementation.

---

## Related Documentation

- [RFC 0019 — OpenTelemetry Completion](0019-opentelemetry-completion.md) (paired RFC; targets the same release; provides the OTEL context the log↔trace processor reads from)
- [RFC 0009 — Agent Identity, Security & Sandboxing](0009-security-sandboxing.md) (auth layer that will eventually gate `logs` endpoints and `LogService`; future home of the real redactor)
- [Development Workflow](../development-workflow.md)
- [Branching Strategy](../BRANCHING.md)
- [internal/server/stub_handlers.go](../../internal/server/stub_handlers.go) (the stub being replaced)
- [cli/src/commands/logs.rs](../../cli/src/commands/logs.rs) (CLI command being completed)
- [internal/server/middleware.go](../../internal/server/middleware.go) (`request_id` precedent)
