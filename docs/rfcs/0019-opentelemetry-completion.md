# RFC 0019 — OpenTelemetry Completion

**Type**: architecture
**Status**: 📋 Proposed
**Author**: Maksim Khomutov
**Date**: 2026-04-21
**Target**: v0.2.3
**Depends on**: none (paired with RFC 0018)
**Feeds into**: log↔trace correlation follow-up (post-v0.2.3)

---

## Table of Contents

- [Summary](#summary)
- [Motivation](#motivation)
- [Goals](#goals)
- [Non-Goals](#non-goals)
- [Design / Implementation](#design--implementation)
  - [A. Current State](#a-current-state)
  - [B. Python OTEL Initialisation](#b-python-otel-initialisation)
  - [C. gRPC Trace Context Propagation](#c-grpc-trace-context-propagation)
  - [D. Semantic Spans on the Python Side](#d-semantic-spans-on-the-python-side)
  - [E. Span Naming and Attribute Conventions](#e-span-naming-and-attribute-conventions)
- [Security Considerations](#security-considerations)
- [Phased Implementation Plan](#phased-implementation-plan)
- [Files Touched (Estimated)](#files-touched-estimated)
- [Test Strategy](#test-strategy)
- [Open Questions](#open-questions)
- [Decision / Next Steps](#decision--next-steps)
- [Related Documentation](#related-documentation)

---

## Summary

This RFC closes the cross-language gap in Persatrix's OpenTelemetry implementation: traces start in the Go orchestrator, cross the gRPC boundary into Python agents, and continue as child spans for memory operations, persona event dispatch, LLM calls, and tool executions. Coverage gaps that are not on the cross-language critical path (full automatic HTTP instrumentation, REST endpoint span uniformity, YAML observability config) are explicitly deferred. The result: an end-to-end trace tree from `POST /api/v1/workflows` to LLM call appears in Jaeger; the README's OTEL story stops being a half-truth.

## Motivation

Persatrix's OTEL implementation is roughly 50–60% of a full setup. The Go side ([`internal/telemetry`](../../internal/telemetry/telemetry.go)) is correct and complete: tracer provider, OTLP HTTP exporter, sampler, W3C TraceContext propagator, manual spans on workflow submit / run / step / gRPC dispatch. Local infra (`docker-compose.yaml`, Jaeger on `:16686`) is OTEL-ready. The README tells operators a tracing story.

What is missing today defeats the primary purpose of having OTEL in a polyglot system:

1. **Python agent runtime never initialises OTEL.** [`agents/pyproject.toml`](../../agents/pyproject.toml) declares `opentelemetry-api`, `opentelemetry-sdk`, and `opentelemetry-exporter-otlp-proto-grpc`, but [`agents/server.py`](../../agents/server.py) never imports any of them. No tracer provider is installed; no exporter is wired.
2. **No explicit gRPC trace context inject/extract beyond the global propagator.** Even if the Python side initialised OTEL, incoming gRPC requests from Go would land without a parent context. There are no `otelgrpc` interceptors on either side.
3. **No semantic spans on the Python side.** No spans for memory operations, persona event dispatch, LLM calls, or tool executions. The `# TODO: OTEL span creation` at [`agents/tools/registry.py:138`](../../agents/tools/registry.py) reflects this.

Operators today see Jaeger and expect end-to-end traces. They get traces that stop at the gRPC dispatch span. That is worse than no tracing — it suggests the feature is broken rather than incomplete.

What happens if we do nothing: every multi-agent debugging story remains a manual timestamp-correlation exercise. RFC 0008's context optimisation work in v0.3 cannot easily be measured against baseline because there is no end-to-end span tree to attribute LLM-call latency to. The README OTEL claim quietly diverges from reality.

## Goals

1. Python agent runtime initialises OTEL on startup, mirroring the Go orchestrator's pattern (OTLP exporter, same endpoint via `OTEL_EXPORTER_OTLP_ENDPOINT`, resource attributes identifying the agent service).
2. gRPC requests from orchestrator carry W3C TraceContext into Python agents; agents resume the trace as child spans.
3. Python agents emit semantic spans for: agent tick cycle, persona event dispatch, memory operations (episodic recall, episodic write, relationship lookup, relationship update), LLM calls (with model, token counts, cache hit/miss attributes), tool executions (with tool name, success/failure, duration).
4. Span naming convention is documented and applied: `<service>.<component>.<operation>` (e.g., `agent.memory.episodic.query`).
5. End-to-end verification: a workflow submitted to `POST /api/v1/workflows` produces a single trace tree in Jaeger spanning orchestrator HTTP handler → workflow run → step dispatch → gRPC client span → gRPC server span → tick / event handler → memory query → LLM call.
6. The `# TODO: OTEL span creation` in [`agents/tools/registry.py`](../../agents/tools/registry.py) is removed and replaced with a real span.

## Non-Goals

- **Automatic HTTP / gRPC instrumentation via `otelhttp` and `otelgrpc` middleware.** Manual instrumentation already covers the critical paths. Adding the ecosystem auto-instrumentation removes a polish gap (some handlers untraced) without adding new capability. Deferred to a follow-up.
- **Span coverage on every REST endpoint.** Health, version, list-style endpoints can stay uninstrumented for v0.2.3.
- **YAML-based OTEL configuration.** `config/environments/development.yaml` has an `observability` section that isn't wired in. Env vars are adequate for v0.2.3; YAML wiring is a polish task.
- **Log↔trace correlation fields (`trace_id` / `span_id` on log lines).** Depends on RFC 0018 landing structured logging first; both RFCs target v0.2.3 but the correlation work is a follow-up after both ship and is intentionally not on the v0.2.3 critical path.
- **Metrics (counters, histograms, gauges).** Separate RFC if/when needed.
- **Custom OTEL exporters beyond OTLP.**
- **Production observability stack beyond Jaeger for local dev.**
- **Performance benchmarking of OTEL overhead.**
- **Trace-based alerting.**
- **Custom OTEL processors or samplers.** The orchestrator's existing sampler config applies; agents inherit the same sampling discipline (see [Open Question 4](#open-questions)).

---

## Design / Implementation

### A. Current State

| Concern | Go orchestrator | Python agents |
|---------|-----------------|---------------|
| Tracer provider | ✅ [`internal/telemetry/telemetry.go`](../../internal/telemetry/telemetry.go) | ❌ Not initialised |
| OTLP exporter | ✅ HTTP via `otlptracehttp` | Deps present, not wired |
| Propagator | ✅ W3C TraceContext | ❌ Not installed |
| Resource attributes | ✅ Service name, version | ❌ N/A |
| Manual spans (HTTP) | ⚠️ Partial — submit + chat | N/A |
| Manual spans (workflow run / step) | ✅ | N/A |
| Manual spans (gRPC client) | ✅ Dispatch + chat | N/A |
| gRPC server-side context extraction | N/A | ❌ Missing |
| Semantic spans (memory / LLM / tools) | N/A | ❌ Missing |

### B. Python OTEL Initialisation

A new module `agents/observability/tracing.py` mirrors `internal/telemetry/telemetry.go`:

- Reads `OTEL_EXPORTER_OTLP_ENDPOINT` (default `http://localhost:4318` for parity with Go), `OTEL_EXPORTER_OTLP_INSECURE`, and standard sampling env vars.
- Constructs a `TracerProvider` with a `Resource` carrying `service.name=agent-<id>`, `service.version`, `service.instance.id`.
- Installs an OTLP span exporter and `BatchSpanProcessor`.
- Sets the global propagator to W3C TraceContext (matching Go).
- Exposes `init_tracing(agent_id: str) -> trace.Tracer` and a `shutdown()` for clean exit.

**Exporter protocol decision.** The Go side uses OTLP-HTTP. Python deps currently include `opentelemetry-exporter-otlp-proto-grpc` (gRPC variant). For parity with the Go endpoint and to keep `:4318` as the single OTLP target, this RFC switches Python to `opentelemetry-exporter-otlp-proto-http`. Dependency change captured in [Files Touched](#files-touched-estimated).

`agents/server.py` calls `init_tracing(agent_id)` early in startup, before the gRPC server is constructed, and registers `shutdown()` with the existing graceful-shutdown sequence.

### C. gRPC Trace Context Propagation

**Approach: `otelgrpc` interceptors on both sides.** Manual inject/extract was the alternative; interceptors are the ecosystem standard, less code, less to maintain, and behave consistently with future contributions.

**Go side.** Add `go.opentelemetry.io/contrib/instrumentation/google.golang.org/grpc/otelgrpc` to `go.mod`. Wire the client interceptor when constructing the gRPC client connection in `internal/executor/`:

```go
conn, err := grpc.NewClient(addr,
    grpc.WithStatsHandler(otelgrpc.NewClientHandler()),
    // ...existing options...
)
```

The existing manual `executor.dispatch` and `executor.chat.dispatch` spans become parents of the otelgrpc-emitted client span and continue to work unchanged.

**Python side.** Add `opentelemetry-instrumentation-grpc` to `agents/pyproject.toml`. In `agents/server.py`:

```python
from opentelemetry.instrumentation.grpc import GrpcInstrumentorServer
GrpcInstrumentorServer().instrument()
```

Once instrumented, every incoming RPC handler runs inside an OTEL context with the parent span from the orchestrator.

**Note on overlap with RFC 0018.** RFC 0018 installs a Python gRPC server interceptor for logging context (`execution_id` / `step_id` / `agent_id`). This RFC installs `GrpcInstrumentorServer` for OTEL. Both interceptors coexist; both should be initialised in `agents/server.py` and the order is documented (OTEL first so the logging interceptor can see `trace_id` once log↔trace correlation is implemented as a follow-up).

### D. Semantic Spans on the Python Side

New spans, organised by component:

| Site | Span name | Key attributes |
|------|-----------|----------------|
| `agents/persona_runtime/` tick loop | `agent.persona.tick` | `agent.id`, `tick.reason` |
| `agents/persona_behavior.py` event dispatch | `agent.persona.event` | `agent.id`, `event.type`, `event.id` |
| `agents/memory/episodic.py` recall / recall_notes | `agent.memory.episodic.query` | `agent.id`, `query.kind` (recall \| recall_notes), `result.count`, `min_score` |
| `agents/memory/episodic.py` record | `agent.memory.episodic.write` | `agent.id`, `episode.kind` |
| `agents/memory/relationship.py` lookups | `agent.memory.relationship.lookup` | `agent.id`, `participant.id` |
| `agents/memory/relationship.py` updates | `agent.memory.relationship.update` | `agent.id`, `participant.id`, `delta.kind` |
| `agents/llm_client.py` LLM call | `agent.llm.call` | `agent.id`, `llm.model`, `llm.tokens.prompt`, `llm.tokens.completion`, `llm.cache.hit` |
| `agents/tools/registry.py` execute | `agent.tool.execute` | `agent.id`, `tool.name`, `tool.success`, `tool.duration_ms` |

The `# TODO: OTEL span creation` at [`agents/tools/registry.py:138`](../../agents/tools/registry.py) is replaced by the `agent.tool.execute` span in this RFC's scope.

*Semantics of `tool.success` (clarified per PR #142 review).* `tool.success=true` if and only if the wrapper returned a `ToolResult` with `success=True`. Both `ToolResult(success=False)` (controlled tool failure) and an unhandled exception escaping the tool body produce `tool.success=false`; in the exception case, the span additionally carries `record_exception(exc)` and `set_status(Status(StatusCode.ERROR))`. This collapses the two failure modes into one boolean for dashboards while preserving the distinction in the recorded exception event.

Spans use the `tracer = trace.get_tracer(__name__)` pattern; instrumentation uses `with tracer.start_as_current_span(name, attributes={...}) as span:`. Errors are recorded via `span.record_exception(exc)` and `span.set_status(Status(StatusCode.ERROR))`.

### E. Span Naming and Attribute Conventions

Naming: `<service>.<component>.<operation>` — lowercase, dot-separated, no plurals.

| Service prefix | Use |
|----------------|-----|
| `orchestrator.*` | Go orchestrator spans |
| `agent.*` | Python agent spans |

Attribute keys: snake_case, dot-separated for namespacing. Reserved keys mirror OTEL semantic conventions where one exists (e.g., `rpc.system`, `rpc.method` are set automatically by `otelgrpc`). Persatrix-specific attributes use a flat keying scheme with the prefix `persatrix.` reserved for cross-cutting fields:

| Key | Type | Notes |
|-----|------|-------|
| `agent.id` | string | Source agent for the span |
| `persatrix.execution_id` | string | Set on spans inside a workflow execution |
| `persatrix.step_id` | string | Set on spans inside a workflow step |
| `persatrix.workflow_id` | string | Workflow definition ID |

The convention is documented in `docs/observability.md` (created by RFC 0018; this RFC adds a span-conventions section).

---

## Security Considerations

- **Trace data may include sensitive information.** Span attributes can leak prompts, tool inputs, or user content if instrumentation copies payloads verbatim. This RFC's attribute schema deliberately avoids payload-bearing fields (`llm.tokens.prompt` is a count, not the prompt text). Tool execution span captures `tool.name` and duration, not arguments.
- **OTLP exporter endpoint is an outbound network call.** Default `http://localhost:4318` is local Jaeger. Operators pointing to remote collectors need to apply their own auth; OTLP collector security is operator responsibility, consistent with how Go currently handles it.
- **No new attack surfaces on the orchestrator.** The new `otelgrpc` client interceptor is outbound-only.
- **Python `GrpcInstrumentorServer` is server-side only and inert without an inbound RPC.** No new listening sockets.
- **Ring buffer (RFC 0018) does not interact with span data.** Logs and traces remain separate streams in v0.2.3; correlation is a future follow-up.

---

## Phased Implementation Plan

### Phase 1: Python OTEL Init + gRPC Context Propagation

**Summary.** Get traces crossing the language boundary. Minimum viable cross-process tracing.

**Deliverables.**

1. `agents/observability/tracing.py` (new) implementing `init_tracing` / `shutdown`.
2. Switch Python OTLP exporter dependency from `opentelemetry-exporter-otlp-proto-grpc` to `opentelemetry-exporter-otlp-proto-http`.
3. Add `opentelemetry-instrumentation-grpc` to `agents/pyproject.toml`.
4. Add `go.opentelemetry.io/contrib/instrumentation/google.golang.org/grpc/otelgrpc` to `go.mod`.
5. Wire `otelgrpc` client handler in `internal/executor/`.
6. `agents/server.py` initialises tracing and `GrpcInstrumentorServer` before serving.
7. Integration test: send a request with a synthetic parent span context; assert agent-side span has matching trace ID.

**Dependencies.** None.

### Phase 2: Semantic Spans on the Python Side

**Summary.** Add the spans listed in [Section D](#d-semantic-spans-on-the-python-side).

**Deliverables.**

1. Tick-loop span in `agents/persona_runtime/`.
2. Event-dispatch span in `agents/persona_behavior.py`.
3. Memory spans in `agents/memory/episodic.py` and `agents/memory/relationship.py`.
4. LLM-call span in `agents/llm_client.py` with token-count attributes.
5. Tool-execute span in `agents/tools/registry.py`; remove the TODO at line 138.
6. Span-conventions section appended to `docs/observability.md`.

**Dependencies.** Phase 1.

### Phase 3: End-to-End Verification + Documentation

**Summary.** Prove the trace tree works; document the operator workflow.

**Deliverables.**

1. E2E test: submit a workflow, query the local Jaeger API for the resulting trace, assert the expected span tree shape.
2. Operator-facing section in `docs/observability.md` covering "viewing traces in Jaeger".
3. README OTEL paragraph updated to reflect end-to-end coverage.

**Dependencies.** Phase 2.

### Phase 4 (reserved): Review Follow-Ups + RFC Close

Per [development-workflow.md](../development-workflow.md) Phase 5–8.

---

## Files Touched (Estimated)

| Component | Files | Change |
|-----------|-------|--------|
| Python agents | `agents/observability/tracing.py` | Add (new module) |
| Python agents | `agents/pyproject.toml` | Swap OTLP exporter to HTTP variant; add `opentelemetry-instrumentation-grpc` |
| Python agents | `agents/server.py` | Initialise tracing + gRPC instrumentor at startup; register shutdown |
| Python agents | `agents/persona_runtime/` (tick loop site) | Add `agent.persona.tick` span |
| Python agents | `agents/persona_behavior.py` | Add `agent.persona.event` span |
| Python agents | `agents/memory/episodic.py` | Add query / write spans |
| Python agents | `agents/memory/relationship.py` | Add lookup / update spans |
| Python agents | `agents/llm_client.py` | Add `agent.llm.call` span with token attributes |
| Python agents | `agents/tools/registry.py` | Add `agent.tool.execute` span; remove L138 TODO |
| Go orchestrator | `go.mod`, `go.sum` | Add `otelgrpc` |
| Go orchestrator | `internal/executor/` (gRPC client construction site) | Wire `otelgrpc` client handler |
| Tests | `agents/tests/test_observability_tracing.py` (new) | Init + propagation unit tests |
| Tests | `tests/integration/test_trace_propagation.py` (new) | Cross-language propagation integration test |
| Tests | `tests/integration/test_trace_e2e.py` (new) | E2E span-tree shape test against local Jaeger |
| Docs | `docs/observability.md` | Append span-conventions and Jaeger-usage sections (file created by RFC 0018) |
| Docs | [README.md](../../README.md) | Update OTEL paragraph |
| Docs | [CHANGELOG.md](../../CHANGELOG.md) | Add an entry under v0.2.3 noting the Python OTLP exporter package swap (`opentelemetry-exporter-otlp-proto-grpc` → `opentelemetry-exporter-otlp-proto-http`); operator-visible because anyone running a custom OTEL collector on the gRPC port (`:4317`) will need to switch to the HTTP port (`:4318`). |
| Docs | [ROADMAP.md](../../ROADMAP.md) | Add RFC 0019 to tracker; update v0.2.3 entry |

No changes to: protos, schemas, JSON schemas, blueprints, workflows, Rust CLI.

*Namespace rationale (added per PR #142 review).* The new Python module lives under `agents/observability/` (paired with `internal/observability/` from RFC 0018) rather than under a hypothetical `agents/telemetry/`. Persatrix-Python today has no `telemetry` package, so the choice is between introducing one and introducing `observability/`. `observability/` is preferred because (a) it pairs symmetrically with the Go `internal/observability/` namespace introduced by RFC 0018, (b) it is the umbrella term most operators recognise, and (c) it leaves room for the log↔trace correlation follow-up to live alongside both subsystems without further reorganisation. The Go-side `internal/telemetry/` package keeps its current scope and is not renamed by this RFC.

*Consolidation follow-up commitment (added per PR #142 review, second pass).* Mirroring the commitment in [RFC 0018](0018-structured-logging-framework.md#e-persatrix-logs-endpoint-and-storage), the RFC closure checklist for RFC 0019 (Phase 4) **must** include opening (or linking to the RFC 0018-spawned) tracking issue for consolidating `internal/telemetry/` and `internal/observability/` (and their Python counterparts if the same split exists by then). This prevents the provisional split from becoming a permanent two-root convention by inertia.

---

## Test Strategy

- **Unit tests — `agents/observability/tracing.py`**: `init_tracing` returns a working tracer; resource attributes set correctly; `shutdown` flushes pending spans; missing env vars fall back to defaults consistently with Go.
- **Integration test — propagation**: invoke an `AgentService` RPC with a synthetic parent context in metadata; assert the agent-side span tree's root parent matches.
- **Integration test — semantic span emission**: drive an agent through a tick + event + memory query + LLM call; assert spans with the expected names appear in an in-process span exporter.
- **E2E test — span tree shape**: requires the docker-compose stack up. Submit a workflow, poll the Jaeger HTTP API for the resulting trace ID, assert the parent/child relationships listed in [Section D](#d-semantic-spans-on-the-python-side).
- **Manual smoke**: `make docker-up`, submit a workflow, open `http://localhost:16686`, find the trace, eyeball it.

---

## Open Questions

1. **`otelgrpc` interceptors vs manual context propagation.** Recommendation: interceptors. Manual is more code with no benefit at this scope. Resolved unless review surfaces a concrete reason against.
2. **Should agent span attributes use `agent.id` or `service.instance.id`?** OTEL semantic conventions suggest the latter; Persatrix-internal queries are easier with the former. Recommendation: set both — `service.instance.id` on the resource, `agent.id` on each span. Cheap.
3. **Span attribute schema for tool arguments.** Today the schema deliberately omits arguments to avoid leaking secrets. Should we offer an opt-in `PERSATRIX_TRACE_TOOL_ARGS=1` for development debugging? Recommendation: defer; arguments are visible in the structured logs (RFC 0018) where the operator already has them.
4. **Sampling strategy for agent-side traces.** Autonomous tick loops can produce high-volume traces. Recommendation: keep parity with the orchestrator's sampler for v0.2.3 (parent-based). If volume becomes a problem, a tail-based sampler can be introduced as a polish item.
5. **Where to construct the gRPC client connection in Go for `otelgrpc` wiring.** Confirm during Phase 1 implementation; one of `internal/executor/dispatch.go` or its constructor site.

   *Pinned (added per PR #142 review, second pass).* Verified against the live tree: there are **two** `grpc.NewClient` call sites in `internal/executor/`, both of which must be wired with `grpc.WithStatsHandler(otelgrpc.NewClientHandler())`:
   - [internal/executor/dispatch.go](../../internal/executor/dispatch.go) line 321 (workflow-step task dispatch)
   - [internal/executor/chat.go](../../internal/executor/chat.go) line 105 (chat-message dispatch added by RFC 0016)

   Both sites already accept caller-provided `grpc.DialOption` slices via `WithDialOptions` / `WithChatDialOptions`, so the cleanest install is to inject the otelgrpc stats handler into those defaults from `cmd/orchestrator/main.go` (where the executor is constructed) rather than hard-coding it inside the dispatch sites. This keeps the executor package free of an OTEL import and matches the existing dependency-injection style.

---

## Decision / Next Steps

**To accept this RFC:**

1. Confirm v0.2.3 as the target milestone (this RFC pairs with RFC 0018 on the same release).
2. Sign off on `otelgrpc` interceptors over manual propagation ([Open Question 1](#open-questions)).
3. Sign off on the span-naming convention in [Section E](#e-span-naming-and-attribute-conventions) as the cross-codebase contract.

**Once accepted:**

1. Author `docs/rfcs/0019-pr-plan.md` per [development-workflow.md](../development-workflow.md) Phase 3.
2. Status → 🚧 Implementing; ROADMAP updated.
3. Begin Phase 1 implementation.

---

## Related Documentation

- [RFC 0018 — Structured Logging Framework](0018-structured-logging-framework.md) (paired RFC; targets the same release)
- [Development Workflow](../development-workflow.md)
- [Branching Strategy](../BRANCHING.md)
- [internal/telemetry/telemetry.go](../../internal/telemetry/telemetry.go) (Go OTEL setup that the Python module mirrors)
- [agents/server.py](../../agents/server.py) (entry point gaining `init_tracing` call)
- [agents/tools/registry.py](../../agents/tools/registry.py) (the OTEL TODO this RFC closes)
- [docker-compose.yaml](../../docker-compose.yaml) (Jaeger + OTLP infrastructure already in place)
