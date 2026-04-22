# RFC 0019 — OpenTelemetry Completion (Traces, Metrics, Correlation)

**Type**: architecture
**Status**: 📋 Proposed
**Author**: Maksim Khomutov
**Date**: 2026-04-21 (rev 2026-04-22 — future-focused expansion; rev 2026-04-22b — apply PR #160 review)
**Target**: v0.2.3
**Depends on**: none
**Pairs with**: RFC 0018 (Structured Logging Framework — both ship together in v0.2.3 with shared schema, redaction hook, and OTLP export; see [Relationship to RFC 0018](#relationship-to-rfc-0018))

<!--
Review note (PR #160): the `Schema version` field was removed from the frontmatter
and folded into Section A ("Current State") below, where the OTEL Resource
`schema_url` is the canonical home for the version. The RFC template does not
define a `Schema version` frontmatter field; introducing one here would set an
ad-hoc convention that diverges from the template.
-->

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
  - [F. Metrics](#f-metrics)
  - [G. Log↔Trace Correlation](#g-logtrace-correlation)
  - [H. Sampling, Back-Pressure, and the Collector Pipeline](#h-sampling-back-pressure-and-the-collector-pipeline)
  - [I. Span Links and A2A Causality](#i-span-links-and-a2a-causality)
- [Security Considerations](#security-considerations)
- [Phased Implementation Plan](#phased-implementation-plan)
- [Files Touched (Estimated)](#files-touched-estimated)
- [Test Strategy](#test-strategy)
- [Resolved Decisions](#resolved-decisions)
- [Open Questions](#open-questions)
- [Decision / Next Steps](#decision--next-steps)
- [Related Documentation](#related-documentation)

---

## Summary

This RFC closes the cross-language gap in Persatrix's OpenTelemetry implementation and lands the **observability foundation** for the project's full lifetime: traces start in the Go orchestrator, cross the gRPC boundary into Python agents (with W3C TraceContext **and** W3C Baggage), and continue as child spans for memory operations, persona event dispatch, LLM calls (annotated with the OTEL Gen-AI semantic conventions), and tool executions. **Metrics** (counters, histograms, gauges) ship on the same OTLP transport. **Log↔trace correlation** (`trace_id` / `span_id` injected into every structured log line emitted by RFC 0018's logger configuration) lands in the same release rather than as a follow-up. **Span Links** capture A2A and sub-agent causality so v0.3 mesh traces are not a forest of disconnected trees. A documented **OTEL Collector tail-sampling pipeline** is the canonical operator deployment from day 1.

The scope is intentionally larger than "traces work end-to-end." The goal is to ship traces + metrics + correlation alongside RFC 0018's logs, with a single redaction hook shared between the two, once — rather than re-litigate naming, sampling, and transport across three or four releases. (Logs themselves remain RFC 0018's deliverable; this RFC owns trace and metric signals plus the enrichers that join them to log records.)

### Relationship to RFC 0018

RFC 0018 (Structured Logging) and this RFC are deliberately kept as separate documents for review tractability, but they share a single design contract:

<!--
Review note (PR #160): the previous "Wire transport | OTLP (HTTP, port 4318)" row
conflicted with RFC 0018 Section E, which specifies a `LogService` streaming gRPC
for the agent→orchestrator log ingest path (with documented rationale: one
orchestrator↔agent transport, free HTTP/2 backpressure, distributed-deployment
ready). The contract is now broken out per signal so the two RFCs no longer
contradict each other. The PR description text claiming "Logs ship via the OTEL
Logs SDK over OTLP" is inconsistent with both RFC bodies and should be updated
in the PR description before merge — that is a metadata fix, not an RFC change.
-->

| Concern | Single source of truth |
|---------|------------------------|
| Trace + metric export | OTLP HTTP (port 4318) — this RFC |
| Log ingest (agent → orchestrator) | `LogService` streaming gRPC over the existing agent gRPC channel — RFC 0018 Section E |
| Log export to external backends (optional) | OTLP HTTP via the same Collector — operator-side, out of scope for v0.2.3 |
| Schema version field | `schema_version: 1` (logs); OTEL `schema_url=https://persatrix.dev/schemas/observability/1.0.0` (traces + metrics) |
| Correlation IDs | `execution_id`, `step_id`, `agent_id`, `workflow_id`, `trace_id`, `span_id` |
| Cross-process propagation | W3C TraceContext + W3C Baggage |
| Redaction hook | Shared interface, used by both log records and span attributes |
| Namespace | Go `internal/observability/`, Python `agents/observability/` (no separate `telemetry/` tree) |
| Sampling discipline | Parent-based head sampling + Collector tail sampling |
| Log↔trace enricher ownership | RFC 0018 (owns the structlog chain and zap encoder where the enrichment lives); this RFC only requires that the OTEL context is established before the enricher PR lands |

A merger of the two RFCs into one "Observability Foundation" document is recorded as [Open Question A](#open-questions). Pending that decision, both RFCs cross-reference this contract.

## Motivation

Persatrix's OTEL implementation is roughly 50–60% of a full setup. The Go side ([`internal/telemetry`](../../internal/telemetry/telemetry.go)) is correct and complete: tracer provider, OTLP HTTP exporter, sampler, W3C TraceContext propagator, manual spans on workflow submit / run / step / gRPC dispatch. Local infra (`docker-compose.yaml`, Jaeger on `:16686`) is OTEL-ready. The README tells operators a tracing story.

What is missing today defeats the primary purpose of having OTEL in a polyglot system:

1. **Python agent runtime never initialises OTEL.** [`agents/pyproject.toml`](../../agents/pyproject.toml) declares `opentelemetry-api`, `opentelemetry-sdk`, and `opentelemetry-exporter-otlp-proto-grpc`, but [`agents/server.py`](../../agents/server.py) never imports any of them. No tracer provider is installed; no exporter is wired.
2. **No explicit gRPC trace context inject/extract beyond the global propagator.** Even if the Python side initialised OTEL, incoming gRPC requests from Go would land without a parent context. There are no `otelgrpc` interceptors on either side.
3. **No semantic spans on the Python side.** No spans for memory operations, persona event dispatch, LLM calls, or tool executions. The `# TODO: OTEL span creation` at [`agents/tools/registry.py:138`](../../agents/tools/registry.py) reflects this.

Operators today see Jaeger and expect end-to-end traces. They get traces that stop at the gRPC dispatch span. That is worse than no tracing — it suggests the feature is broken rather than incomplete.

What happens if we do nothing: every multi-agent debugging story remains a manual timestamp-correlation exercise. RFC 0008's context optimisation work in v0.3 cannot easily be measured against baseline because there is no end-to-end span tree to attribute LLM-call latency to. The README OTEL claim quietly diverges from reality.

## Goals

1. Python agent runtime initialises OTEL on startup (traces **and** metrics), mirroring the Go orchestrator's pattern (OTLP HTTP exporter, same endpoint via `OTEL_EXPORTER_OTLP_ENDPOINT`, resource attributes identifying the agent service, OTEL Resource detectors for process / host / container / k8s metadata, `schema_url` set to a versioned Persatrix URL).
2. gRPC requests from orchestrator carry W3C TraceContext **and** W3C Baggage into Python agents; agents resume the trace as child spans and inherit baggage (`persatrix.execution_id`, `persatrix.step_id`, `persatrix.workflow_id`, plus future `persatrix.tenant_id` / `persatrix.organization_id` slots).
3. Python agents emit semantic spans for: agent tick cycle, persona event dispatch (with sub-millisecond phases as **span events**, not separate spans), memory operations (episodic recall, episodic write, relationship lookup, relationship update), LLM calls (using **OTEL Gen-AI semantic conventions** — `gen_ai.system`, `gen_ai.request.model`, `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`, `gen_ai.response.finish_reasons`, `gen_ai.operation.name`), tool executions (with tool name, success/failure, duration, and optional argument capture routed through the RFC 0018 redaction hook).
4. Span naming convention is documented and applied: `<service>.<component>.<operation>` (e.g., `agent.memory.episodic.query`). Persatrix-specific attributes use the reserved `persatrix.*` prefix; Gen-AI attributes use the upstream `gen_ai.*` prefix verbatim.
5. **Span Links** capture A2A causality (event → triggered tick, parent agent → spawned sub-agent, dispatched message → handler).
6. **Metrics** ship on the same OTLP exporter: counters (`agent.tool.invocations`, `agent.llm.calls`, `agent.llm.tokens`, `agent.event.dispatched`), histograms (`agent.llm.duration`, `agent.tool.duration`, `agent.persona.tick.interval`), gauges (`agent.active`, `workflow.active`). Histograms emit **exemplars** linking to trace samples.
7. **Log↔trace correlation lands in v0.2.3, not as a follow-up.** Every structured log line from RFC 0018 emitted inside an active span carries `trace_id` and `span_id`; the Python logging interceptor and the Go zap field enricher both pull from the active OTEL context.
8. **Tail sampling pipeline.** A documented OTEL Collector configuration ships under `docker-compose.yaml` (or a sibling override) implementing rules: keep all error spans, keep slow LLM calls (>p95), sample 1% of healthy autonomous tick traces.
9. End-to-end verification: a workflow submitted to `POST /api/v1/workflows` produces a single trace tree in Jaeger spanning orchestrator HTTP handler → workflow run → step dispatch → gRPC client span → gRPC server span → tick / event handler → memory query → LLM call, with correlated log lines queryable by `trace_id` and a metrics dashboard showing the same execution.
10. The `# TODO: OTEL span creation` in [`agents/tools/registry.py`](../../agents/tools/registry.py) is removed and replaced with a real span.
11. A reusable `InMemorySpanExporter` / `InMemoryMetricReader` `pytest` fixture lives in `agents/tests/conftest.py` so every future agent feature is span- and metric-testable without docker-compose.

## Non-Goals

- **Span coverage on every REST endpoint.** Health, version, list-style endpoints can stay uninstrumented for v0.2.3. (`otelhttp` middleware on the public REST handler **is** in scope as a one-line install — see [Section F](#f-metrics).)
- **YAML-based OTEL configuration.** `config/environments/development.yaml` has an `observability` section that isn't wired in. Env vars remain primary for v0.2.3; YAML wiring is a polish task tracked separately.
- **Custom OTEL exporters beyond OTLP.** OTLP HTTP is the only supported transport.
- **Production observability stack beyond Jaeger + Prometheus + Loki for local dev.** A reference Collector pipeline is documented; choosing the operator's production backend is out of scope.
- **Performance benchmarking of OTEL overhead.** Recommended `BatchSpanProcessor` / `BatchLogRecordProcessor` settings are documented; formal benchmarking deferred.
- **Trace-based alerting.**
- **Profiling signal (4th OTEL signal).** Namespace is left unblocked (`agents/observability/` and `internal/observability/` can host a future `profiling.py`) but no implementation in v0.2.3.
- **Tool/function-call OTEL semantic conventions.** Adopt them when the upstream spec stabilises; current attribute set is forward-compatible (flat `tool.*` keys easily aliased).
- **Multi-tenant attribute enforcement.** `persatrix.tenant_id` / `persatrix.organization_id` baggage slots are defined, but enforcement (RFC 0009) is out of scope here.

---

## Design / Implementation

### A. Current State

*Schema version.* Trace + metric signals carry the OTEL Resource attribute `schema_url="https://persatrix.dev/schemas/observability/1.0.0"` (Section B). The structured-log schema is versioned independently as `schema_version: "1"` (RFC 0018 Section B). Both versions are tracked in CHANGELOG; bumping either is a release-notes event.

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

**Resource attributes.** The `Resource` carries:

- `service.name=agent-<id>`, `service.version`, `service.instance.id`
- `schema_url="https://persatrix.dev/schemas/observability/1.0.0"` (versioned, lets future consumers do schema-aware migrations)
- Auto-detected attributes via OTEL Resource detectors: `ProcessResourceDetector`, `OTELResourceDetector` (env-var bridge), and — when running under containers/k8s — `ContainerResourceDetector` and `OTResourceDetector` for k8s metadata. One-line installs each; trivial now, painful to retrofit when Persatrix runs under k8s.

**BatchSpanProcessor tuning.** Defaults (queue 2048, schedule 5s, max export batch 512) are wrong for autonomous tick loops. The RFC ships explicit values (`max_queue_size=8192`, `schedule_delay_millis=2000`, `max_export_batch_size=1024`) and configures the processor to **drop on overflow rather than block**. A `agent.observability.spans.dropped` counter records the drop rate so operators see exporter back-pressure on a dashboard.

`agents/server.py` calls `init_tracing(agent_id)` and `init_metrics(agent_id)` early in startup, before the gRPC server is constructed, and registers `shutdown()` with the existing graceful-shutdown sequence.

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

**W3C Baggage propagation.** In addition to TraceContext, the global propagator is configured as a `CompositePropagator([TraceContextTextMapPropagator(), W3CBaggagePropagator()])` on both sides. The Go orchestrator sets baggage entries (`persatrix.execution_id`, `persatrix.step_id`, `persatrix.workflow_id`) before the gRPC client call; Python agents read them via `baggage.get_all()` and use them to enrich both spans and structured log records (RFC 0018) without manual context plumbing. The same mechanism carries forward to v0.3 mesh and A2A calls — sub-agent spawns and downstream RPCs auto-tag every signal with the originating workflow context.

**Note on overlap with RFC 0018.** RFC 0018 installs a Python gRPC server interceptor for logging context. This RFC installs `GrpcInstrumentorServer` for OTEL. Both interceptors coexist; OTEL is initialised first so the logging interceptor sees `trace_id` / `span_id` from the active context (see [Section G](#g-logtrace-correlation)).

### D. Semantic Spans on the Python Side

New spans, organised by component:

| Site | Span name | Key attributes |
|------|-----------|----------------|
| `agents/persona_runtime/` tick loop | `agent.persona.tick` | `agent.id`, `tick.reason` |
| `agents/persona_behavior.py` event dispatch | `agent.persona.event` | `agent.id`, `event.type`, `event.id` — sub-millisecond phases (`received` → `queued` → `handled` → `completed`) emitted as **span events** on this single span, not nested spans |
| `agents/memory/episodic.py` recall / recall_notes | `agent.memory.episodic.query` | `agent.id`, `query.kind` (recall \| recall_notes), `result.count`, `min_score` |
| `agents/memory/episodic.py` record | `agent.memory.episodic.write` | `agent.id`, `episode.kind` |
| `agents/memory/relationship.py` lookups | `agent.memory.relationship.lookup` | `agent.id`, `participant.id` |
| `agents/memory/relationship.py` updates | `agent.memory.relationship.update` | `agent.id`, `participant.id`, `delta.kind` |
| `agents/llm_client.py` LLM call | `agent.llm.call` | OTEL Gen-AI semantic conventions: `gen_ai.system` (e.g., `openai`, `anthropic`), `gen_ai.request.model`, `gen_ai.response.model`, `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`, `gen_ai.response.finish_reasons`, `gen_ai.operation.name` (e.g., `chat`, `text_completion`); plus Persatrix-specific `agent.id`, `persatrix.llm.cache.hit`. Vendor observability backends (Jaeger, Tempo, Honeycomb, Datadog) already render Gen-AI views keyed on these. |
| `agents/tools/registry.py` execute | `agent.tool.execute` | `agent.id`, `tool.name`, `tool.success`, `tool.duration_ms`; optional `tool.arguments` / `tool.result` controlled by `PERSATRIX_TRACE_TOOL_PAYLOADS=none\|metadata\|full` and **routed through the RFC 0018 redaction hook** so traces and logs share one secrets-policy code path |

The `# TODO: OTEL span creation` at [`agents/tools/registry.py:138`](../../agents/tools/registry.py) is replaced by the `agent.tool.execute` span in this RFC's scope.

*Semantics of `tool.success` (clarified per PR #142 review).* `tool.success=true` if and only if the wrapper returned a `ToolResult` with `success=True`. Both `ToolResult(success=False)` (controlled tool failure) and an unhandled exception escaping the tool body produce `tool.success=false`; in the exception case, the span additionally carries `record_exception(exc)` and `set_status(Status(StatusCode.ERROR))`. This collapses the two failure modes into one boolean for dashboards while preserving the distinction in the recorded exception event.

*Forward-compatibility note for tool spans.* When the upstream OTEL spec stabilises tool/function-call semantic conventions (currently in flight as part of Gen-AI), the `tool.*` attribute set will be aliased to the upstream namespace in a follow-up. The flat keying chosen here makes the alias mechanical (a `RENAME` view in the Collector or a one-shot rename in instrumentation).

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

The convention is documented in `docs/observability.md` (created by RFC 0018; this RFC adds a span-conventions section, a metrics section, and a Collector pipeline section).

### F. Metrics

Metrics ship on the same OTLP HTTP exporter as traces. A new module `agents/observability/metrics.py` mirrors the tracing module: `init_metrics(agent_id) -> Meter`, `shutdown()`, same Resource (so traces and metrics share `service.instance.id`).

**Instrument inventory.**

| Instrument | Type | Unit | Attributes |
|------------|------|------|------------|
| `agent.tool.invocations` | Counter | `{invocation}` | `agent.id`, `tool.name`, `tool.success` |
| `agent.tool.duration` | Histogram | `ms` | `agent.id`, `tool.name`, `tool.success` |
| `agent.llm.calls` | Counter | `{call}` | `agent.id`, `gen_ai.system`, `gen_ai.request.model`, `persatrix.llm.cache.hit` |
| `agent.llm.tokens` | Counter | `{token}` | `agent.id`, `gen_ai.request.model`, `gen_ai.token.type` (`input` \| `output`) |
| `agent.llm.duration` | Histogram | `ms` | `agent.id`, `gen_ai.request.model` |
| `agent.event.dispatched` | Counter | `{event}` | `agent.id`, `event.type` |
| `agent.persona.tick.interval` | Histogram | `ms` | `agent.id` |
| `agent.active` | UpDownCounter | `{agent}` | (resource-only) |
| `workflow.active` | UpDownCounter | `{workflow}` | (orchestrator side; resource-only) |
| `agent.observability.spans.dropped` | Counter | `{span}` | `agent.id`, `reason` (`queue_full` \| `export_error`) |
| `agent.observability.logs.dropped` | Counter | `{record}` | `agent.id`, `reason` |

**Exemplars.** Histogram instruments emit exemplars by default (OTEL SDK feature); each histogram bucket sample carries the `trace_id` / `span_id` of the call that produced it. A p99 LLM-latency spike on the dashboard becomes one click to the actual slow trace in Jaeger.

**Go-side metrics.** The orchestrator gains `internal/observability/metrics.go` with: `workflow.submitted`, `workflow.completed`, `workflow.duration`, `workflow.steps.dispatched`, `workflow.active`. Same OTLP exporter, same Resource conventions.

**`otelhttp` on the REST surface.** The orchestrator's main HTTP handler is wrapped with `otelhttp.NewHandler` so route-level latency histograms (`http.server.request.duration`) come for free. Per-handler manual spans remain on the workflow-submit and chat paths; trivial endpoints (health, version) get the `otelhttp` span only.

### G. Log↔Trace Correlation

**In scope for v0.2.3.** Both interceptors land in the same release; the marginal cost over RFC 0018's logging interceptor is one field-enrichment line. Without correlation, RFC 0018's structured logs and this RFC's traces are two parallel, manually-correlated streams — defeating the point of shipping them together.

**Mechanism.**

- **Python.** RFC 0018's logging interceptor (and the standalone `agents/observability/logging.py` setup) reads the active span context via `trace.get_current_span().get_span_context()` and adds `trace_id` (hex) and `span_id` (hex) to every log record's structured attributes when valid.
- **Go.** The orchestrator's zap field enricher (RFC 0018) pulls the span context from the request context (`trace.SpanFromContext(ctx).SpanContext()`) and adds the same two fields.
- **Baggage enrichment.** The same enrichers also copy known baggage entries (`persatrix.execution_id`, `persatrix.step_id`, `persatrix.workflow_id`) onto log records — meaning a log line emitted three calls deep in a sub-agent automatically carries the originating workflow's IDs without any caller doing manual context plumbing.

The `docs/observability.md` doc gains a "correlated debugging" walkthrough: from a Jaeger trace ID → `persatrix logs --trace <trace_id>` → the structured log lines for that trace, ordered by timestamp, across all participating processes.

### H. Sampling, Back-Pressure, and the Collector Pipeline

**Head sampling: parent-based.** The Python SDK uses `ParentBased(TraceIdRatioBased(<rate>))` matching the Go orchestrator's existing sampler. Default sampling rate is `1.0` for v0.2.3 (sample everything; tail sampler downstream decides what to keep).

**Tail sampling: OTEL Collector.** Autonomous tick loops in v0.3 mesh will dwarf workflow-driven traces. Cheap to set up the Collector pipeline now; painful to retrofit during a v0.3 incident. A reference Collector configuration ships under `config/observability/otel-collector.yaml` and is referenced from `docker-compose.yaml` (path chosen per PR #160 review to align with the existing `config/` directory convention; the repository has no top-level `deploy/` directory today, and creating one for a single file would be a structural decision out of scope for this RFC):

```yaml
processors:
  tail_sampling:
    decision_wait: 10s
    num_traces: 100000
    expected_new_traces_per_sec: 1000
    policies:
      - name: errors
        type: status_code
        status_code: { status_codes: [ERROR] }
      - name: slow-llm
        type: latency
        latency: { threshold_ms: 5000 }
      - name: workflow-traces
        type: string_attribute
        string_attribute: { key: persatrix.workflow_id, values: [".+"], enabled_regex_matching: true, invert_match: false }
      - name: healthy-tick-sample
        type: probabilistic
        probabilistic: { sampling_percentage: 1 }
```

**Back-pressure.** Both `BatchSpanProcessor` and `BatchLogRecordProcessor` are configured to drop on overflow rather than block (queues sized in [Section B](#b-python-otel-initialisation) for spans; mirrored for logs in RFC 0018). Drop counters (`agent.observability.spans.dropped`, `agent.observability.logs.dropped`) are emitted as metrics so operators see exporter or collector unavailability on a dashboard immediately.

### I. Span Links and A2A Causality

When one span causes work in another span tree (rather than being a synchronous parent), OTEL `Link`s record the relationship without forcing a parent/child:

| Causality | Link source | Link target |
|-----------|-------------|-------------|
| Persona event triggers a tick | the `agent.persona.tick` span | the `agent.persona.event` span that scheduled it |
| Parent agent spawns sub-agent | the sub-agent's root span | the parent's `agent.subagent.spawn` span |
| Bridged message crosses a channel | the receiving handler span | the producing dispatch span |
| Mesh A2A call (v0.3) | the receiving node's span | the originating node's span |

Without links, v0.3 mesh traces will be a forest of disconnected trees. Adding the Link API call when an event/spawn/dispatch is enqueued is one line and forward-compatible with v0.3.

---

## Security Considerations

- **Trace data may include sensitive information.** Span attributes can leak prompts, tool inputs, or user content if instrumentation copies payloads verbatim. The default attribute schema avoids payload-bearing fields (`gen_ai.usage.input_tokens` is a count, not the prompt text). The opt-in `PERSATRIX_TRACE_TOOL_PAYLOADS=full` mode routes tool arguments and results through the **shared RFC 0018 redaction hook** — there is one secrets-policy code path covering both logs and traces.
- **`PERSATRIX_TRACE_TOOL_PAYLOADS=full` is gated on a non-noop redactor** (added per PR #160 review). At startup, if the configured value is `full` *and* the registered `Redactor` is the default `NoopRedactor`, the agent runtime logs a `WARN` (`tool payload capture forced down to 'metadata' — register a non-noop Redactor to enable 'full'`) and force-downgrades the effective mode to `metadata`. This closes the most likely accidental data-leakage vector in the window between this RFC landing and the future security RFC (under RFC 0009) that ships a real redactor.
- **Baggage entries are propagated downstream.** Baggage values appear on every span and log line in the trace tree, including across A2A boundaries. The schema deliberately reserves `persatrix.*` for non-sensitive correlation IDs; user content must never be placed in baggage. This is documented in `docs/observability.md`.
- **OTLP exporter endpoint is an outbound network call.** Default `http://localhost:4318` is local Collector / Jaeger. Operators pointing to remote collectors need to apply their own auth; OTLP collector security is operator responsibility, consistent with how Go currently handles it.
- **No new attack surfaces on the orchestrator.** The new `otelgrpc` client interceptor and `otelhttp` handler wrap are non-listening / outbound-only respectively.
- **Python `GrpcInstrumentorServer` is server-side only and inert without an inbound RPC.** No new listening sockets.
- **Metrics carry attribute cardinality risk.** Instruments are deliberately bounded — `tool.name`, `gen_ai.request.model`, `event.type` are low-cardinality enumerations. No instrument carries a per-execution ID as a metric attribute (that is what traces and logs are for).

---

## Phased Implementation Plan

### Phase 1: Python OTEL Init + gRPC Context Propagation + Baggage

**Summary.** Get traces and baggage crossing the language boundary. Minimum viable cross-process tracing.

**Deliverables.**

1. `agents/observability/tracing.py` (new) implementing `init_tracing` / `shutdown` (with Resource detectors, `schema_url`, tuned `BatchSpanProcessor`).
2. Switch Python OTLP exporter dependency from `opentelemetry-exporter-otlp-proto-grpc` to `opentelemetry-exporter-otlp-proto-http`.
3. Add `opentelemetry-instrumentation-grpc` and `opentelemetry-instrumentation-system-metrics` to `agents/pyproject.toml`.
4. Add `go.opentelemetry.io/contrib/instrumentation/google.golang.org/grpc/otelgrpc` and `go.opentelemetry.io/contrib/instrumentation/net/http/otelhttp` to `go.mod`.
5. Wire `otelgrpc` client handler into the executor's dial options from `cmd/orchestrator/main.go` (covers both pinned `grpc.NewClient` sites).
6. Wire `otelhttp.NewHandler` around the orchestrator's main HTTP handler.
7. Configure `CompositePropagator(TraceContext + Baggage)` on both sides.
8. `agents/server.py` initialises tracing and `GrpcInstrumentorServer` before serving.
9. `agents/tests/conftest.py` ships an `InMemorySpanExporter` fixture.
10. Integration test: send a request with a synthetic parent span context **and baggage**; assert agent-side span has matching trace ID and inherits baggage.

**Dependencies.** None.

### Phase 2: Semantic Spans + Span Links + Log↔Trace Correlation

**Summary.** Add the spans listed in [Section D](#d-semantic-spans-on-the-python-side), the Span Links from [Section I](#i-span-links-and-a2a-causality), and the log↔trace enricher from [Section G](#g-logtrace-correlation).

**Deliverables.**

1. Tick-loop span in `agents/persona_runtime/`.
2. Event-dispatch span in `agents/persona_behavior.py` with sub-millisecond phases as **span events** (not nested spans).
3. Memory spans in `agents/memory/episodic.py` and `agents/memory/relationship.py`.
4. LLM-call span in `agents/llm_client.py` using OTEL Gen-AI semantic conventions.
5. Tool-execute span in `agents/tools/registry.py`; remove the TODO at line 138; opt-in payload capture via `PERSATRIX_TRACE_TOOL_PAYLOADS` routed through the RFC 0018 redaction hook.
6. Span Links wired at: persona event → triggered tick, parent agent → spawned sub-agent, bridged-message dispatch → receiving handler.
7. **Log↔trace enricher coordination only — implementation owned by RFC 0018 Phase 3.** This RFC's Phase 2 must land before RFC 0018 Phase 3 so that the OTEL context (provider + propagator + active span) is available when RFC 0018's structlog/zap enricher reads `trace_id` / `span_id` and known baggage entries. See [Section G](#g-logtrace-correlation) for the contract; the actual interceptor / encoder code ships in RFC 0018. <!-- Review note (PR #160): previously this deliverable claimed the enricher implementation, duplicating RFC 0018 Phase 3 deliverables 3–4. Reduced to a cross-reference to give PR-plan authors a single source of truth. -->
8. Span-conventions and "correlated debugging" walkthrough sections appended to `docs/observability.md`.

**Dependencies.** Phase 1; coordinated with RFC 0018 Phase 3 (which owns the log↔trace enricher; see deliverable 7 above).

### Phase 3: Metrics + Collector Pipeline + End-to-End Verification

**Summary.** Land metrics on the same OTLP exporter, ship the documented Collector tail-sampling pipeline, and prove the trace + log + metric trio works end-to-end.

**Deliverables.**

1. `agents/observability/metrics.py` (new) implementing `init_metrics` / `shutdown` with the instrument inventory in [Section F](#f-metrics).
2. `internal/observability/metrics.go` (new) for orchestrator metrics; instrumentation at workflow submit / complete / step dispatch sites.
3. Histograms emit exemplars (default OTEL SDK behaviour, verified in test).
4. `deploy/observability/otel-collector.yaml` (new) with the tail-sampling pipeline from [Section H](#h-sampling-back-pressure-and-the-collector-pipeline).
5. `docker-compose.yaml` updated to add the Collector service in front of Jaeger; Prometheus added as the metrics backend; Loki added as the logs backend (development only).
6. `agents/tests/conftest.py` ships an `InMemoryMetricReader` fixture.
7. **Schema parity contract test** (added per PR #160 review): asserts that every Persatrix correlation ID listed in [RFC 0018 Section B](0018-structured-logging-framework.md#b-common-log-schema) Optional fields (`execution_id`, `step_id`, `agent_id`, `request_id`, `trace_id`, `span_id`) appears with a matching key in this RFC's [Section E](#e-span-naming-and-attribute-conventions) attribute conventions (under the `persatrix.*` prefix where applicable), and that the schema-version values declared in both RFCs (`schema_version: "1"` for logs; `schema_url=…/1.0.0` for traces/metrics) are pinned in code. Prevents silent drift between the two schemas across future revisions.
8. E2E test: submit a workflow, query Jaeger for the trace, query Prometheus for the resulting metrics, query Loki (or `persatrix logs --trace <trace_id>`) for the correlated log lines; assert the expected shape across all three signals.
9. Operator-facing section in `docs/observability.md` covering "viewing traces in Jaeger", "querying metrics in Prometheus", "correlated debugging from a trace ID".
10. README OTEL paragraph updated to reflect logs + traces + metrics end-to-end coverage.

**Dependencies.** Phase 2.

### Phase 4 (reserved): Review Follow-Ups + RFC Close

Per [development-workflow.md](../development-workflow.md) Phase 5–8.

---

## Files Touched (Estimated)

| Component | Files | Change |
|-----------|-------|--------|
| Python agents | `agents/observability/tracing.py` | Add (new module) |
| Python agents | `agents/observability/metrics.py` | Add (new module) |
| Python agents | `agents/pyproject.toml` | Swap OTLP exporter to HTTP variant; add `opentelemetry-instrumentation-grpc`, `opentelemetry-instrumentation-system-metrics` |
| Python agents | `agents/server.py` | Initialise tracing, metrics, gRPC instrumentor at startup; register shutdown |
| Python agents | `agents/persona_runtime/` (tick loop site) | Add `agent.persona.tick` span; record `agent.persona.tick.interval` histogram |
| Python agents | `agents/persona_behavior.py` | Add `agent.persona.event` span (with phase span events); record `agent.event.dispatched` counter; emit Span Link to triggered tick |
| Python agents | `agents/memory/episodic.py` | Add query / write spans |
| Python agents | `agents/memory/relationship.py` | Add lookup / update spans |
| Python agents | `agents/llm_client.py` | Add `agent.llm.call` span with **Gen-AI semantic-convention attributes**; record `agent.llm.calls`, `agent.llm.tokens`, `agent.llm.duration` |
| Python agents | `agents/tools/registry.py` | Add `agent.tool.execute` span (with optional payload capture via redaction hook); record `agent.tool.invocations`, `agent.tool.duration`; remove L138 TODO |
| Python agents | `agents/sub_agents/` (spawn site) | Add `agent.subagent.spawn` span; emit Span Link from sub-agent root span back to spawn span |
| Go orchestrator | `go.mod`, `go.sum` | Add `otelgrpc`, `otelhttp` |
| Go orchestrator | `internal/observability/metrics.go` | Add (new module) |
| Go orchestrator | `cmd/orchestrator/main.go` | Inject `otelgrpc` client handler into executor dial options; wrap HTTP handler with `otelhttp`; init metrics |
| Go orchestrator | `internal/executor/dispatch.go`, `internal/executor/chat.go` | (No code change required — dial options injected from caller) |
| Tests | `agents/tests/conftest.py` | Add `InMemorySpanExporter` and `InMemoryMetricReader` fixtures |
| Tests | `agents/tests/test_observability_tracing.py` (new) | Init + propagation + baggage unit tests |
| Tests | `agents/tests/test_observability_metrics.py` (new) | Instrument inventory + exemplar emission tests |
| Tests | `tests/integration/test_trace_propagation.py` (new) | Cross-language propagation integration test |
| Tests | `tests/integration/test_log_trace_correlation.py` (new) | Asserts every structured log emitted inside a span carries `trace_id` / `span_id` |
| Tests | `tests/integration/test_observability_e2e.py` (new) | E2E shape test: trace tree + correlated logs + metric exemplars against the local Collector + Jaeger + Prometheus stack |
| Deploy | `config/observability/otel-collector.yaml` (new) | Reference Collector config with `tail_sampling` processor (path chosen per PR #160 review to align with `config/` convention) |
| Deploy | `docker-compose.yaml` | Add Collector, Prometheus, Loki services; route OTLP through Collector |
| Docs | `docs/observability.md` | Append span-conventions, metrics inventory, sampling/Collector, correlated-debugging, and Jaeger/Prometheus usage sections (file created by RFC 0018) |
| Docs | [README.md](../../README.md) | Update OTEL paragraph to cover logs + traces + metrics |
| Docs | [CHANGELOG.md](../../CHANGELOG.md) | Add an entry under v0.2.3 noting the Python OTLP exporter package swap (`opentelemetry-exporter-otlp-proto-grpc` → `opentelemetry-exporter-otlp-proto-http`); operator-visible because anyone running a custom OTEL collector on the gRPC port (`:4317`) will need to switch to the HTTP port (`:4318`). Also note the new Collector + Prometheus + Loki services in `docker-compose.yaml`. |
| Docs | [ROADMAP.md](../../ROADMAP.md) | Group RFC 0018 + RFC 0019 as the v0.2.3 "Observability Foundation" delivery |

No changes to: protos, schemas, JSON schemas, blueprints, workflows, Rust CLI.

*Namespace rationale (added per PR #142 review).* The new Python modules live under `agents/observability/` (paired with `internal/observability/` from RFC 0018). Persatrix-Python today has no `telemetry` package, so the choice is between introducing one and introducing `observability/`. `observability/` is preferred because (a) it pairs symmetrically with the Go `internal/observability/` namespace introduced by RFC 0018, (b) it is the umbrella term most operators recognise, and (c) it leaves room for the future profiling signal to live alongside both subsystems without further reorganisation.

*On the Go-side `internal/telemetry/` package.* Under the future-focused framing (and the shared namespace contract in [Relationship to RFC 0018](#relationship-to-rfc-0018)), `internal/telemetry/` is **renamed to `internal/observability/`** as part of this RFC's Phase 1, eliminating the provisional two-root split. The previously-recorded namespace consolidation follow-up is therefore dropped.

---

## Test Strategy

- **Unit tests — `agents/observability/tracing.py`**: `init_tracing` returns a working tracer; resource attributes set correctly (including `schema_url` and detector-supplied keys); `shutdown` flushes pending spans; missing env vars fall back to defaults consistently with Go; `BatchSpanProcessor` overflow drops rather than blocks and increments `agent.observability.spans.dropped`.
- **Unit tests — `agents/observability/metrics.py`**: instrument inventory matches [Section F](#f-metrics); units and attribute keys are correct; histograms emit exemplars carrying valid `trace_id` / `span_id`.
- **Integration test — propagation**: invoke an `AgentService` RPC with a synthetic parent context **and baggage** in metadata; assert the agent-side span tree's root parent matches and baggage entries are accessible inside the handler.
- **Integration test — semantic span emission**: drive an agent through a tick + event + memory query + LLM call; assert spans with the expected names appear in the in-process `InMemorySpanExporter` and the LLM span carries `gen_ai.*` attributes.
- **Integration test — log↔trace correlation**: emit log lines from inside a span on both Go and Python sides; assert every record carries the active span's `trace_id` and `span_id`, and known baggage entries.
- **Integration test — Span Links**: trigger a tick from an event and a sub-agent spawn from a parent agent; assert the resulting spans carry the expected `Link`s.
- **E2E test — observability end-to-end**: requires the docker-compose stack up (orchestrator + agents + Collector + Jaeger + Prometheus + Loki). Submit a workflow, poll Jaeger for the resulting trace ID, poll Prometheus for the matching metrics (with exemplars), query Loki for the correlated log lines; assert the parent/child relationships, the metric counts, and that log lines link back to the same trace.
- **Manual smoke**: `make docker-up`, submit a workflow, open `http://localhost:16686`, find the trace, click an exemplar in Prometheus to jump back to a span, run `persatrix logs --trace <trace_id>`.

---

## Resolved Decisions

Under the future-focused framing applied in revision 2026-04-22, the original open questions are resolved as follows:

1. **`otelgrpc` interceptors vs manual context propagation.** ✅ Resolved: **interceptors**. Ecosystem standard; manual is more code with no benefit.
2. **`agent.id` vs `service.instance.id`.** ✅ Resolved: **set both**. `service.instance.id` on the Resource (OTEL convention), `agent.id` on every span (Persatrix query ergonomics). Additionally adopt the **OTEL Gen-AI semantic conventions** verbatim for LLM spans (see [Section D](#d-semantic-spans-on-the-python-side)) so vendor observability tools render Persatrix LLM traces correctly out of the box.
3. **Tool argument capture.** ✅ Resolved: **opt-in `PERSATRIX_TRACE_TOOL_PAYLOADS=none\|metadata\|full`**, routed through the shared RFC 0018 redaction hook. One secrets-policy code path for both signals; production observability stacks expect payloads on traces, not just logs.
4. **Sampling strategy.** ✅ Resolved: **parent-based head sampling (rate 1.0) + Collector tail sampling from day 1**. See [Section H](#h-sampling-back-pressure-and-the-collector-pipeline). Cheap to set up now, painful to retrofit during a v0.3 mesh incident.
5. **gRPC client wire site.** ✅ Pinned to two `grpc.NewClient` call sites:
   - [internal/executor/dispatch.go](../../internal/executor/dispatch.go) line 321 (workflow-step task dispatch)
   - [internal/executor/chat.go](../../internal/executor/chat.go) line 105 (chat-message dispatch added by RFC 0016)

   Both sites accept caller-provided `grpc.DialOption` slices via `WithDialOptions` / `WithChatDialOptions`; the otelgrpc stats handler is injected from `cmd/orchestrator/main.go` (where the executor is constructed). The executor package stays free of an OTEL import.

## Open Questions

<!--
Review note (PR #160): the prior single open question was numbered "6" because
it continued the Resolved Decisions list. Renumbered to start at A inside this
section to make the discontinuity intentional and avoid reader confusion when
landing on the section directly.
-->

**OQ-A.** **Merge RFC 0018 and RFC 0019 into one "Observability Foundation" RFC?** Both share OTLP export (for traces/metrics), schema-version fields, redaction hook, namespace, sampling discipline, and ship in the same release. The case for merging: one document, one decision tree, no consolidation follow-up. The case for keeping split: review tractability — each RFC is large enough that a merged document would be hard to review in one pass. **Recommendation:** keep split for review, but record both RFCs as a single "Observability Foundation" delivery in [ROADMAP.md](../../ROADMAP.md), drop the namespace consolidation follow-up (already aligned via the [Relationship to RFC 0018](#relationship-to-rfc-0018) contract), and treat them as a unit in release notes. Decide before authoring the PR plan.

---

## Decision / Next Steps

**To accept this RFC:**

1. Confirm v0.2.3 as the target milestone (this RFC pairs with RFC 0018 on the same release).
2. Sign off on the resolved decisions in [Resolved Decisions](#resolved-decisions) (interceptors, dual `agent.id` + `service.instance.id` with Gen-AI conventions, opt-in payload capture through redaction hook, head + tail sampling, pinned gRPC client sites).
3. Sign off on the span-naming and Persatrix attribute conventions in [Section E](#e-span-naming-and-attribute-conventions), the metrics inventory in [Section F](#f-metrics), and the log↔trace correlation contract in [Section G](#g-logtrace-correlation) as the cross-codebase contract.
4. Decide [Open Question A](#open-questions) (merge with RFC 0018 vs keep split).

**Cross-RFC sequencing (added per PR #160 review).** The two RFCs share namespace and code paths, so PR landing order matters:

1. **This RFC's Phase 1 lands before any RFC 0018 PR that adds packages under `internal/observability/`.** Phase 1 performs the `internal/telemetry/` → `internal/observability/` rename; landing it second would turn the rename PR into a rename-plus-merge-conflict-resolution PR.
2. **This RFC's Phase 1 lands before RFC 0018 Phase 3.** RFC 0018 Phase 3 declares `RFC 0019 Phase 1 (OTEL initialised on Python side)` as a prerequisite; the cross-process correlation work needs the OTEL context already established.
3. **RFC 0018 Phase 1 lands before this RFC's Phase 2** (so the redaction hook surface and the structlog/zap configuration the enricher attaches to exist before the Phase 2 spans need to call into them).

These constraints should be reflected in both RFCs' joint PR plan and in the v0.2.3 ROADMAP entry.

**Once accepted:**

1. Author `docs/rfcs/0019-pr-plan.md` per [development-workflow.md](../development-workflow.md) Phase 3, sequenced against the RFC 0018 PR plan so shared deliverables (OTLP transport, redaction hook, `agents/observability/` namespace, log↔trace enrichment) land once.
2. Status → 🚧 Implementing; ROADMAP updated; both RFCs grouped as "Observability Foundation" in the v0.2.3 entry.
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
