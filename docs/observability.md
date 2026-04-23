# Persatrix Observability — Log Schema

> **Owner**: [RFC 0018 — Structured Logging Framework](rfcs/0018-structured-logging-framework.md)
> **Companion**: [RFC 0019 — OpenTelemetry Completion](rfcs/0019-opentelemetry-completion.md) (will append span / metric sections in its Phase 2 + 3 PRs)
> **Status**: 🚧 In progress (RFC 0018 PR 1 — Phase 1)
> **Schema version**: `1`

This document is the **single source of truth** for the Persatrix structured-log
schema. Both the Go orchestrator (`go.uber.org/zap`) and the Python agents
(`structlog`) emit records conforming to this schema. Future RFCs append to this
document — they never overwrite a published schema field. Breaking changes (field
removal, rename, type change, semantic change) bump `schema_version` to `"2"` and
are called out in `CHANGELOG.md`.

Refer to the RFC for the design rationale; this doc is the operational reference.

---

## 1. Wire format

One JSON object per line, one event per object. UTF-8. No trailing whitespace.
Field emission order is **stable and documented** below for diffability of
captured logs across runs.

```json
{"schema_version":"1","timestamp":"2026-04-22T18:30:00.123456Z","level":"INFO","service.kind":"agent","service.instance":"ember-owl","message":"task accepted","execution_id":"exec-42","step_id":"step-1","agent_id":"ember-owl","trace_id":"4bf92f3577b34da6a3ce929d0e0e4736","span_id":"00f067aa0ba902b7"}
```

The `service.*` group is emitted as **flat keys** (`"service.kind": "agent"`),
not nested objects — this keeps `jq` / `grep` / `rg` workflows simple and
matches the Go-side `zap.Field` flat emission model.

---

## 2. Required fields (in emission order)

| # | Field | Type | Notes |
|---|-------|------|-------|
| 1 | `schema_version` | string | `"1"` for this RFC. Future breaking changes increment this. |
| 2 | `timestamp` | string | RFC 3339 with timezone; UTC by default. |
| 3 | `level` | string | One of `DEBUG`, `INFO`, `WARN`, `ERROR`. Note: `WARN` not `WARNING`. |
| 4 | `service.kind` | string | One of `orchestrator`, `agent`, `cli`. |
| 5 | `service.instance` | string | Process instance identity (orchestrator node ID, agent ID, CLI invocation ID). |
| 6 | `message` | string | Human-readable; should not include the structured fields. |

`service.kind` / `service.instance` are set at process start by
`configure_logging()` (Python) or the zap encoder (Go) and **never rewritten on
ingest**. Records shipped from agents into the orchestrator's ring buffer
preserve their original `service.kind=agent` provenance.

---

## 3. Optional fields (in emission order, present when applicable)

| # | Field | Type | Notes |
|---|-------|------|-------|
| 7 | `service.role` | string | For agents: `coder`, `reviewer`, `persona`, etc. Omitted otherwise. |
| 8 | `execution_id` | string | Workflow run ID. |
| 9 | `step_id` | string | Step within a workflow. |
| 10 | `agent_id` | string | Source or target agent (depending on log site). For `service.kind=agent`, equals `service.instance`. |
| 11 | `request_id` | string | HTTP request ID (orchestrator only, set by middleware). |
| 12 | `trace_id` | string | OTEL trace ID when an OTEL context is active. **Omitted (not empty)** when no span is in scope. |
| 13 | `span_id` | string | OTEL span ID when an OTEL context is active. **Omitted (not empty)** when no span is in scope. |
| 14 | `attributes` | object | Free-form key/value bag for site-specific context. |
| 15 | `source` | object | `{file, line, function}` of the call site (added by Go zap `WithCaller`; future PR for Python). |

Any keys emitted by a call site that are not in this table are appended **after**
the documented fields in insertion order. This preserves the diffability of
known fields while leaving room for site-local context.

---

## 4. Field-emission order contract

Both runtimes emit known fields in the order shown in §2 + §3 above. Unknown
keys are appended in insertion order after the known set.

* **Python (`structlog`)** — enforced by the `_reorder_keys` processor in
  [`agents/observability/logging.py`](../agents/observability/logging.py).
* **Go (`zap`)** — will be enforced by the schema encoder wrapper landing in
  RFC 0018 PR 2 (`internal/observability/zapenc/encoder.go`).

The order is asserted byte-for-byte in unit tests on both sides
(`agents/tests/test_observability_logging.py`,
`internal/observability/zapenc/encoder_test.go` — added in PR 2).

---

## 5. Versioning

* **Non-breaking**: adding new optional fields. `schema_version` does **not** bump.
* **Breaking**: removing a field, renaming a field, changing its type, or
  changing its meaning. `schema_version` bumps to `"2"` and the change is called
  out in `CHANGELOG.md` under the release that lands the change.

Consumers branch on `schema_version` to handle multi-version log streams cleanly.

---

## 6. Local-development renderer (`PERSATRIX_LOG_FORMAT=pretty`)

Setting `PERSATRIX_LOG_FORMAT=pretty` in the environment swaps the JSON
renderer for a human-readable console renderer:

* **Python** — `structlog.dev.ConsoleRenderer` (colours when stderr is a TTY).
* **Go** — zap's development encoder config (added in RFC 0018 PR 2).

Default is `json` (or unset, which is treated as `json`) for `make run`, CI,
production, and the future `persatrix logs` endpoint. The pretty mode is a
developer affordance; it is **not** a stable wire format and is **not**
consumed by the ring buffer or the streaming endpoint.

```shell
# Pretty console output for local debugging:
PERSATRIX_LOG_FORMAT=pretty make run

# Default (JSON) — unchanged:
make run
```

---

## 7. Redaction hook

Both runtimes route every record through a `Redactor` interface before
serialisation. The default implementation is a no-op pass-through; a real
PII / secret scrubber is the responsibility of a future security RFC under the
RFC 0009 umbrella.

The same `Redactor` interface shape is used by RFC 0019 Phase 2 for opt-in
tool-payload capture as span attributes — one redaction contract across both
observability signals.

* **Python** — [`agents.observability.redact.Redactor`](../agents/observability/redact.py)
  Protocol; install via `agents.observability.logging.set_redactor(impl)`.
* **Go** — `internal/observability/redact.Redactor` interface (added in
  RFC 0018 PR 2).

---

## 8. Cross-process correlation

Three IDs travel from orchestrator to agent and into the agent's log context:
`execution_id`, `step_id`, `agent_id`. OTEL `trace_id` / `span_id` ride the
W3C TraceContext channel installed by RFC 0019.

The injection helpers (Go side) and the gRPC server interceptor (Python side)
land in **RFC 0018 PR 3**. PR 1 (this PR) wires the OTEL processor placeholder
that already reads `trace.get_current_span()` so that any code path running
inside an OTEL span (for example, tests that wrap an operation in a span) gets
the trace IDs on its log lines today; the orchestrator-to-agent metadata
plumbing arrives in PR 3.

Metadata key conventions are documented in
[RFC 0018 § D](rfcs/0018-structured-logging-framework.md#d-cross-process-correlation).

---

## 9. Roadmap for this document

Future RFCs append the following sections to this file (single-source-of-truth
discipline):

| Section | Owning RFC + PR |
|---------|-----------------|
| Span semantic conventions (`persatrix.*` attribute namespace) | RFC 0019 PR 2 (✅ § 10 below) |
| Metric inventory + dimensions | RFC 0019 PR 3 |
| Persisted log layout (`data/logs/<execution_id>/...`) + env knobs | RFC 0018 PR 4 |
| `LogService` gRPC + REST + SSE endpoint shapes | RFC 0018 PR 5 |
| `persatrix logs` CLI flags + colour scheme | RFC 0018 PR 6 |

When editing this doc, **never overwrite a section owned by another PR**;
append instead.

---

## 10. Span conventions (RFC 0019 PR 2)

This section is the operational reference for trace spans emitted by the
Python agent runtime. The naming and attribute conventions come from
[RFC 0019 § D](rfcs/0019-opentelemetry-completion.md#d-semantic-spans-on-the-python-side)
and [§ E](rfcs/0019-opentelemetry-completion.md#e-span-naming-and-attribute-conventions).

### 10.1 Naming

`<service>.<component>.<operation>` — lowercase, dot-separated, no plurals.
`agent.*` is the Python runtime; `orchestrator.*` is the Go orchestrator.

### 10.2 Span inventory

| Span name | Emitted from | Key attributes |
|-----------|--------------|----------------|
| `agent.persona.event` | `_LLMPersonaAgent.on_event()` | `agent.id`, `event.type`, `event.id`. Sub-millisecond phases recorded as **span events** (`received` → `queued` → `handled` → `completed`), not nested spans. |
| `agent.persona.tick` | `_LLMPersonaAgent.on_tick()` | `agent.id`, `tick.reason`. Carries `Link(link.kind="trigger")` back to the event span when an event woke the tick scheduler (RFC 0019 § I). |
| `agent.memory.episodic.recall` | `EpisodicMemory.recall()` | `agent.id`, `query.kind`, `query.empty`, `min_score`, `result.count` |
| `agent.memory.episodic.remember` | `EpisodicMemory.store_episode()` | `agent.id`, `episode.kind` |
| `agent.memory.relationship.lookup` | `RelationshipMemory.get_trust()` | `agent.id`, `participant.id` |
| `agent.memory.relationship.update` | `RelationshipMemory.update_trust()` | `agent.id`, `participant.id`, `delta.kind`, `delta.value`, `trust.new` |
| `agent.llm.call` | `LLMClient.create_message()` | OTEL **Gen-AI semantic conventions**: `gen_ai.system`, `gen_ai.request.model`, `gen_ai.operation.name`, `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`, `gen_ai.response.finish_reasons` |
| `agent.tool.execute` | `@tool` decorator wrapper in `tools/registry.py` | `tool.name`, `tool.success` (+ optional payload — see § 10.4) |
| `agent.subagent.spawn` | `ActionExecutor` SPAWN_SUB_AGENT case (stub for RFC 0009) | `agent.id`, `subagent.role`, `subagent.status`. The sub-agent's root span will emit `Link(link.kind="spawn")` back here when the spawner ships in RFC 0009. |

### 10.3 Span Links and A2A causality

Per [RFC 0019 § I](rfcs/0019-opentelemetry-completion.md#i-span-links-and-a2a-causality),
when one span causes work in another span tree (rather than being a synchronous
parent), an OTEL `Link` records the relationship:

| Causality | Link source | Link target | Status |
|-----------|-------------|-------------|--------|
| Persona event triggers a tick | `agent.persona.tick` | `agent.persona.event` that woke the scheduler | ✅ Wired (PR 2) |
| Parent agent spawns sub-agent | sub-agent root span | parent's `agent.subagent.spawn` | ⏳ Span exists; Link wires when RFC 0009 ships the spawner |
| Bridged message crosses a channel | receiving handler | producing dispatch | ⏳ RFC 0006 follow-up |
| Mesh A2A call (v0.3) | receiving node | originating node | ⏳ v0.3 mesh |

Links carry minimal attributes — `link.kind` distinguishes `trigger` /
`spawn` / `bridge`. The load-bearing piece is the target's
`trace_id` / `span_id`.

### 10.4 Tool-payload capture (`PERSATRIX_TRACE_TOOL_PAYLOADS`)

Tool argument and result capture is **opt-in** to keep secrets out of trace
backends by default. The env var has three modes:

| Mode | Behaviour | Span attributes added |
|------|-----------|-----------------------|
| `none` (default) | No payload data captured. | (none) |
| `metadata` | Argument names and types only, no values. | `tool.arguments.<arg>.type` |
| `full` | Argument values captured, **passed through the RFC 0018 redactor** (single secrets-policy code path for both logs and spans). | `tool.arguments.<arg>` |

The redactor today is the no-op `NoopRedactor` shipped in RFC 0018 PR 1.
A real redactor lands with the future security RFC under the RFC 0009
umbrella; both signals will pick it up automatically because they share
the same `agents.observability.spans.set_redactor()` hook.

### 10.5 Persatrix-specific attribute namespace

Persatrix uses **two** attribute conventions side-by-side, mirroring
[RFC 0019 § E](rfcs/0019-opentelemetry-completion.md#e-span-naming-and-attribute-conventions):

1. **Bare component-namespaced keys** — `agent.id`, `event.type`,
   `episode.kind`, `tool.name`, `tool.success`, `participant.id`,
   `delta.kind`, `delta.value`, `trust.new`, `subagent.role`,
   `subagent.status`, `tick.reason`, `link.kind`, `query.kind`,
   `query.empty`, `result.count`, `min_score`, `actions.count`,
   `tool.arguments.<name>` / `tool.arguments.<name>.type`. These describe
   the *local* span subject (the agent, the tool call, the relationship
   row) and follow the OTEL convention of using a component prefix
   (`agent.`, `tool.`, `event.`) without a vendor namespace. Collision
   with future upstream OTEL keys is mitigated by the schema-version
   pin (`schema_url=https://persatrix.dev/schemas/observability/1.0.0`)
   — a future OTEL key under one of these prefixes can be migrated in
   one schema bump rather than spreading vendor noise across every span
   today.

2. **`persatrix.*`-prefixed cross-cutting keys** — reserved for
   workflow-context fields propagated via W3C Baggage across process
   boundaries, where the prefix prevents collision with arbitrary
   third-party span producers in the same trace:

| Key | Type | Notes |
|-----|------|-------|
| `persatrix.execution_id` | string | Set on spans inside a workflow execution (via Baggage from RFC 0019 PR 1) |
| `persatrix.step_id` | string | Set on spans inside a workflow step |
| `persatrix.workflow_id` | string | Workflow definition ID |

`gen_ai.*` attributes use the upstream OTEL Gen-AI semantic-convention
namespace verbatim — no Persatrix-private renames — so vendor backends
render Persatrix LLM traces correctly out of the box. The
`gen_ai.response.finish_reasons` attribute emits the canonical OTEL
vocabulary (`stop` / `length` / `tool_calls` / `content_filter` /
`error`); Persatrix's internal `StopReason` enum is translated at the
span emission site (see `agents.observability.spans.STOP_REASON_TO_GEN_AI`).

### 10.6 Correlated debugging walkthrough

Once RFC 0018 PR 3 lands the log↔trace enricher, the operator workflow is:

1. Operator opens Jaeger and finds a slow `agent.llm.call` span.
2. Copies the `trace_id` from the span detail panel.
3. Runs `persatrix logs --trace <trace_id>` (RFC 0018 PR 6) — gets every
   structured log line emitted under that trace, ordered by timestamp,
   across all participating processes.
4. The same `trace_id` keys an exemplar on the histogram metric (RFC 0019
   PR 3), so a p99-latency spike on the dashboard is one click to the
   trace and to the logs.

The `agent.persona.event` → `agent.persona.tick` Link (§ 10.3) means an
autonomous tick that produced the slow LLM call is already linked back to
the originating user event without the operator having to follow timestamps.
