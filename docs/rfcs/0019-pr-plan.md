# RFC 0019 — PR Implementation Plan

**RFC**: [0019-opentelemetry-completion.md](0019-opentelemetry-completion.md)
**Paired RFC**: [0018-structured-logging-framework.md](0018-structured-logging-framework.md) — joint v0.2.3 "Observability Foundation" delivery (paired plan: [0018-pr-plan.md](0018-pr-plan.md))
**Created**: 2026-04-22
**Branch prefix**: `feature/v023-`
**Target**: `main`
**Merge strategy**: Squash merge per [BRANCHING.md](../BRANCHING.md)

---

## Overview

RFC 0019 lands the **traces + metrics + correlation half** of the v0.2.3 Observability Foundation:

1. Python OTEL initialisation that mirrors the existing Go setup, with W3C TraceContext + W3C Baggage propagation across the gRPC boundary.
2. Semantic spans on the agent side: tick loop, persona event dispatch, memory ops, LLM calls (using OTEL **Gen-AI semantic conventions**), tool execution. Span Links for A2A and sub-agent causality.
3. OTLP **metrics** (counters, histograms, gauges) on the same OTLP transport with histogram exemplars.
4. A documented OTEL **Collector tail-sampling** pipeline — the canonical operator deployment from day 1.
5. Log↔trace correlation: coordination only — the structlog/zap enricher implementation is owned by RFC 0018 Phase 3, but RFC 0019's Phase 2 must land before so the OTEL context (provider + propagator + active span) exists.
6. The `internal/telemetry/` → `internal/observability/` Go-side **rename** (Phase 1) — eliminates the provisional two-root split and unblocks RFC 0018's `internal/observability/` packages.

The RFC spans 3 substantive phases plus a wrap-up. This plan splits the work into **5 PRs**.

> **Estimate calibration**: RFC 0005, 0006, 0016, and 0017 PRs landed within a 1.7× calibration factor relative to initial estimates. This plan applies the same factor.

**Prerequisite**: RFC 0017 fully merged (7/7 PRs — done as of v0.2.2). No code dependency on prior RFCs at landing time, but see Cross-RFC sequencing below for ordering against RFC 0018.

---

## Cross-RFC Sequencing (verbatim from [RFC 0019 Decision / Next Steps](0019-opentelemetry-completion.md#decision--next-steps) and [RFC 0018 Decision / Next Steps](0018-structured-logging-framework.md#decision--next-steps))

The two RFCs share namespace and code paths, so PR landing order matters:

1. **This RFC's Phase 1 lands before any RFC 0018 PR that adds packages under `internal/observability/`.** Phase 1 performs the `internal/telemetry/` → `internal/observability/` rename; landing it second would turn the rename PR into a rename-plus-merge-conflict-resolution PR.
2. **This RFC's Phase 1 lands before RFC 0018 Phase 3.** RFC 0018 Phase 3 declares `RFC 0019 Phase 1 (OTEL initialised on Python side)` as a prerequisite; the cross-process correlation work needs the OTEL context already established.
3. **RFC 0018 Phase 1 lands before this RFC's Phase 2** (so the redaction hook surface and the structlog/zap configuration the enricher attaches to exist before the Phase 2 spans need to call into them).

---

## Joint Merge Order (RFCs 0018 + 0019)

The two plans interleave. The combined order across both RFCs is:

> **Maintenance note**: this ASCII diagram is intentionally duplicated **verbatim** in [0018-pr-plan.md](0018-pr-plan.md). If you edit the order here, update the paired plan in the same commit. <!-- Callout added per PR #161 review nice-to-have #3: verbatim duplication is the deliberate single-source-of-truth choice (both reviewers see the same order from either entry point), but it carries a drift hazard. -->

```
0019 PR 1 (Phase 1 — telemetry→observability rename + Python OTEL init + gRPC + Baggage)
  ↓
0018 PR 1 (Phase 1 — Python structlog + schema doc + redactor surface)
  ↓
0018 PR 2 (Phase 2 — Go zap rename + pretty + redactor wired + source)
  ↓
0019 PR 2 (Phase 2 — semantic spans + Span Links; reads redactor for tool-payload capture)
  ↓
0018 PR 3 (Phase 3 — cross-process correlation + OTEL trace IDs on logs)
  ↓
0019 PR 3 (Phase 3a — metrics)
  ↓
0019 PR 4 (Phase 3b — Collector + docker-compose + E2E + schema-parity test)
  ↓
0018 PR 4 (Phase 4a — proto/log_service.proto + ring buffer + disk store)
  ↓
0018 PR 5 (Phase 4b — LogService server + agent shipper + REST + SSE)
  ↓
0018 PR 6 (Phase 4c — CLI rewrite + E2E)
  ↓
0018 PR 7 + 0019 PR 5 (review follow-ups + RFC close, opened together as a paired closeout)
```

Each PR in this plan lists its position in this combined order so reviewers do not have to derive it.

---

## RFC 0019 Dependency Graph

```
PR 1 (telemetry→observability rename + Python OTEL init + gRPC propagation + Baggage — joint order #1)
  ↓
0018 PR 1 + 0018 PR 2 (RFC 0018 Phase 1+2 — redactor surface + Go encoder need to exist for Phase 2)
  ↓
PR 2 (semantic spans + Span Links + log↔trace coordination — joint order #4)
  ↓
0018 PR 3 (RFC 0018 Phase 3 wires the enricher this PR coordinates with — joint order #5)
  ↓
PR 3 (metrics — Python + Go + InMemoryMetricReader fixture — joint order #6)
  ↓
PR 4 (Collector + docker-compose + E2E + schema-parity test — joint order #7)
  ↓
PR 5 (review follow-ups + RFC close — joint order #11, opened with 0018 PR 7)
```

---

## PR Sequence

### PR 1: `feature/v023-otel-rename-and-python-init` — Phase 1: Rename + Python OTEL Init + gRPC Propagation + Baggage

**Joint order position**: #1 — must land first per Cross-RFC sequencing constraint #1.
**Depends on**: Nothing (builds on the existing Go OTEL setup at `internal/telemetry/telemetry.go`).
**Branch**: `feature/v023-otel-rename-and-python-init`
**Estimated size**: ~400–500 lines (rename + new module + gRPC wire-up + tests)

#### Scope

| File | Change |
|------|--------|
| `internal/telemetry/` → `internal/observability/` | **Rename** the entire directory and update all importers. The Go orchestrator's existing OTEL setup (`telemetry.go`, related tests) moves verbatim; package name updates from `telemetry` to `observability`. |
| `cmd/orchestrator/main.go`, every `internal/...` importer | Update import paths from `…/internal/telemetry` to `…/internal/observability/otel` (or `…/observability` if the file is the only one in the package — pinned during PR 1 review). |
| `agents/observability/__init__.py` | **New** — package marker (the same package RFC 0018 PR 1 will populate further). |
| `agents/observability/tracing.py` | **New** — `init_tracing()` / `shutdown()` with: Resource attributes (`service.name`, `service.version`, `service.kind`, `service.instance.id`, OTEL Resource detectors); `schema_url=https://persatrix.dev/schemas/observability/1.0.0`; tuned `BatchSpanProcessor` (queue cap, max-export batch); a `CompositePropagator(TraceContext + Baggage)` registered as the global propagator. |
| `agents/pyproject.toml` | Replace `opentelemetry-exporter-otlp-proto-grpc` with `opentelemetry-exporter-otlp-proto-http`; add `opentelemetry-instrumentation-grpc`, `opentelemetry-instrumentation-system-metrics`. |
| `go.mod`, `go.sum` | Add `go.opentelemetry.io/contrib/instrumentation/google.golang.org/grpc/otelgrpc` and `go.opentelemetry.io/contrib/instrumentation/net/http/otelhttp`. |
| `cmd/orchestrator/main.go` | Inject `otelgrpc.NewClientHandler()` into the executor's `WithDialOptions` / `WithChatDialOptions` slices (covers both pinned `grpc.NewClient` sites in `internal/executor/dispatch.go` and `internal/executor/chat.go`). Wrap the orchestrator HTTP handler with `otelhttp.NewHandler`. Configure the global propagator to `propagation.NewCompositeTextMapPropagator(propagation.TraceContext{}, propagation.Baggage{})`. |
| `agents/server.py` | Call `init_tracing()` at startup; register `GrpcInstrumentorServer` on the gRPC server; await `shutdown()` on graceful shutdown. |
| `agents/tests/conftest.py` | Add an `InMemorySpanExporter` fixture that replaces the Batch processor for the duration of a test. |
| `agents/tests/test_observability_tracing.py` | **New** — `init_tracing` returns a working tracer; Resource carries `schema_url` and detector keys; `shutdown` flushes pending spans; missing env vars fall back consistently with the Go side; `BatchSpanProcessor` overflow drops rather than blocks (and a future metric counter is reserved — implementation in PR 3). |
| `tests/integration/test_trace_propagation.py` | **New** — invoke an `AgentService` RPC with a synthetic parent span context **and baggage** in metadata; assert agent-side span tree's root parent matches and baggage entries are accessible inside the handler. |
| [CHANGELOG.md](../../CHANGELOG.md) | Under v0.2.3, add an entry noting: (a) Python OTLP exporter package swap (`opentelemetry-exporter-otlp-proto-grpc` → `opentelemetry-exporter-otlp-proto-http`) — operator-visible because anyone running a custom OTEL collector on the gRPC port `:4317` will need to switch to the HTTP port `:4318`; (b) Go package rename `internal/telemetry` → `internal/observability`. |

#### Key implementation details

- The rename is the load-bearing part of joint order #1: if it lands later, every subsequent RFC 0018 PR that adds an `internal/observability/...` package becomes a rename-plus-add merge conflict.
- The Python OTLP exporter swap to HTTP is documented as operator-visible in the CHANGELOG. The Collector pipeline shipped by PR 4 will accept HTTP on `:4318`; the swap aligns the Python side with the Go side's OTLP HTTP convention.
- Resource attributes are populated via OTEL's standard detectors (`OSResourceDetector`, `ProcessResourceDetector`, `HostResourceDetector`) plus Persatrix-specific `service.kind=agent`, `service.instance.id=<agent_id>`, optional `service.role`. This pairs with RFC 0018 Schema's `service.*` group — single source of truth.
- `BatchSpanProcessor` is configured with explicit queue cap and max-export batch so behaviour is deterministic across environments. Overflow drops increment a counter (whose `Counter` instrument is created in PR 3; the drop-counting hook is wired here as a no-op call site that PR 3 turns load-bearing — same pattern as RFC 0018 PR 1's redactor placeholder).
- Both `grpc.NewClient` sites (pinned in [RFC § Resolved Decisions #5](0019-opentelemetry-completion.md#resolved-decisions)) are wired via the executor's caller-provided dial-options slices; the executor package itself remains free of an OTEL import.

#### Tests

- Unit (Python): `init_tracing()` returns a tracer; Resource attributes include the documented keys + `schema_url`; `shutdown()` flushes a batch of pre-registered spans before returning.
- Unit (Python): `BatchSpanProcessor` overflow path returns immediately without blocking (assert via timing on a synthetic load).
- Integration: cross-language propagation — Go side initiates a span and dial, Python side receives the RPC, the agent-side root span's parent matches the Go span context. Same test asserts baggage entries are readable inside the handler via `baggage.get_all()`.
- Smoke: `make run` with `OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318` documented in PR description; visual confirmation in Jaeger is not gated (no Jaeger in CI yet — PR 4 adds the docker-compose stack).

#### PR checklist

- [ ] `go test ./internal/observability/... -v -race -cover` passes (rename preserves all existing tests)
- [ ] `pytest agents/tests/test_observability_tracing.py -v` passes
- [ ] `pytest tests/integration/test_trace_propagation.py -v` passes
- [ ] No `internal/telemetry/` directory remains
- [ ] Pre-merge import-path audit: `grep -r 'internal/telemetry' --include='*.go' .` returns no matches <!-- Added per PR #161 review: the rename touches ~57 Go files; a grep verification is a low-cost safety net for any imports the automated update missed. -->
- [ ] All `internal/telemetry` import paths in `internal/...` and `cmd/...` updated
- [ ] `agents/pyproject.toml` Python OTLP exporter dep is the HTTP variant
- [ ] CHANGELOG v0.2.3 entry covers the OTLP exporter swap (operator-visible) and the Go package rename
- [ ] Both `grpc.NewClient` sites carry the `otelgrpc` client handler via the executor's dial-options slices
- [ ] HTTP handler is wrapped with `otelhttp.NewHandler` in `cmd/orchestrator/main.go`
- [ ] `CompositePropagator(TraceContext + Baggage)` configured globally on both sides
- [ ] ROADMAP.md RFC 0019 row: status → 🚧 Implementing on this PR opening (per RFC Decision/Next Steps step 2)

---

### PR 2: `feature/v023-otel-semantic-spans` — Phase 2: Semantic Spans + Span Links + Log↔Trace Coordination

**Joint order position**: #4 (after RFC 0018 PR 2).
**Depends on**:
- **Hard**: PR 1 merged (OTEL provider + propagator are the substrate every new span needs) **and** RFC 0018 PR 1 merged (redactor `Protocol` exists for opt-in tool-payload capture).
- **Scheduling only**: ordered after RFC 0018 PR 2 to preserve the joint Go/Python alternation in [Joint Merge Order](#joint-merge-order-rfcs-0018--0019); 0019 PR 2 touches only Python files, so there is no code-level conflict with 0018 PR 2's Go encoder work.
- **Coordinated with** RFC 0018 PR 3 (which owns the log↔trace enricher implementation that this PR's spans feed).
<!-- Hard vs. scheduling split added per PR #161 review: prose previously listed only the hard deps while the dep-graph block above listed `0018 PR 1 + 0018 PR 2`, leaving reviewers unable to tell whether 0018 PR 2 was mandatory or merely sequencing. -->
**Branch**: `feature/v023-otel-semantic-spans`
**Estimated size**: ~400–500 lines (spans across multiple agent modules + tests + doc append)

#### Scope

| File | Change |
|------|--------|
| `agents/persona_runtime/__init__.py` | Add `agent.persona.tick` span around the tick handler. <!-- Pinned per PR #161 review (was "or `agents/persona_runtime/action_loop.py` — pinned in PR 2's first commit"): `on_tick()` is defined in `agents/persona_runtime/__init__.py` (the `_LLMPersonaAgent` class); `action_loop.py` holds the multi-turn LLM loop (`_on_event_inner`) and is *not* the tick entry point. Pinning now removes a PR-time decision the plan can already make and aligns with the RFC 0017 PR 5 precedent this plan cites. --> |
| `agents/persona_behavior.py` | Add `agent.persona.event` span around event dispatch with sub-millisecond phases recorded as **span events** (not nested spans), per [RFC § D](0019-opentelemetry-completion.md#d-semantic-spans-on-the-python-side). |
| `agents/memory/episodic.py` | Add `agent.memory.episodic.recall` and `agent.memory.episodic.remember` spans (or the names pinned by [RFC § E](0019-opentelemetry-completion.md#e-span-naming-and-attribute-conventions) on PR 2 review). |
| `agents/memory/relationship.py` | Add `agent.memory.relationship.lookup` and `agent.memory.relationship.update` spans. |
| `agents/llm_client.py` | Add `agent.llm.call` span using the **OTEL Gen-AI semantic conventions** (`gen_ai.system`, `gen_ai.request.model`, `gen_ai.usage.prompt_tokens`, `gen_ai.usage.completion_tokens`, etc.). |
| `agents/tools/registry.py` | Add `agent.tool.execute` span; remove the OTEL TODO at line 138; wire opt-in payload capture via `PERSATRIX_TRACE_TOOL_PAYLOADS=none\|metadata\|full` routed through the **RFC 0018 Redactor Protocol** so the same secrets-policy code path serves both signals. |
| `agents/sub_agents/` (spawn site) | Add `agent.subagent.spawn` span; emit a Span Link from the sub-agent root span back to its spawn span. |
| `agents/persona_behavior.py` (event→tick), `agents/persona.py` or wherever bridged messages dispatch | Emit Span Links per [RFC § I](0019-opentelemetry-completion.md#i-span-links-and-a2a-causality): persona event → triggered tick; bridged-message dispatch → receiving handler. |
| `docs/observability.md` | Append "Span conventions" + "Correlated debugging walkthrough" sections (the file is created by RFC 0018 PR 1; this PR appends per Cross-RFC sequencing constraint). |
| `agents/tests/test_observability_spans.py` | **New** — drive an agent through tick + event + memory query + LLM call → assert spans with the expected names appear in the in-process `InMemorySpanExporter`; the LLM span carries `gen_ai.*` attributes; tool span emits payload attributes only when `PERSATRIX_TRACE_TOOL_PAYLOADS=full` and routes them through the redactor. |
| `tests/integration/test_span_links.py` | **New** — trigger a tick from an event and a sub-agent spawn from a parent agent → assert the resulting spans carry the expected `Link`s. |

#### Key implementation details

- **Log↔trace enricher coordination only — implementation owned by RFC 0018 Phase 3.** This RFC's Phase 2 must land before RFC 0018 Phase 3 so the OTEL context (provider + propagator + active span) is available when RFC 0018's structlog/zap enricher reads `trace_id` / `span_id` and known baggage entries. See [RFC 0019 § G](0019-opentelemetry-completion.md#g-logtrace-correlation) for the contract; the actual interceptor / encoder code ships in RFC 0018 PR 3. (This deliverable previously claimed the enricher implementation, duplicating RFC 0018 Phase 3 deliverables 3–4 — reduced to a cross-reference per PR #160 review.)
- Span events vs nested spans: the sub-millisecond phases inside event dispatch are recorded as `span.add_event(name, attributes=...)` rather than nested spans, per [RFC § D](0019-opentelemetry-completion.md#d-semantic-spans-on-the-python-side). Keeps trace trees navigable.
- Tool-payload capture goes through the RFC 0018 Redactor Protocol — single secrets-policy path. With `PERSATRIX_TRACE_TOOL_PAYLOADS=none` (default) no payload attributes are emitted; `metadata` emits only argument names + types; `full` emits redacted payload values.
- Span Links carry minimal attribute payload (`link.kind=spawn|trigger|bridge`); the link target's trace/span IDs are the load-bearing piece.
- The Gen-AI semantic-convention attributes are sourced from the OTEL spec verbatim — no Persatrix-private renames. This makes vendor observability tools render Persatrix LLM traces correctly out of the box ([RFC § Resolved Decisions #2](0019-opentelemetry-completion.md#resolved-decisions)).

#### Tests

- Unit: each new span name appears in the `InMemorySpanExporter` after the corresponding code path runs once.
- Unit: `gen_ai.*` attributes on the LLM span match the Gen-AI semantic-convention spec for the configured provider.
- Unit: `PERSATRIX_TRACE_TOOL_PAYLOADS=none` → no payload attributes; `=metadata` → arg names/types; `=full` → values pass through `Redactor.redact()` exactly once before being attached.
- Integration: event → tick produces two spans linked via `Link(trace_id=event_span.trace_id, span_id=event_span.span_id, attributes={"link.kind": "trigger"})`.
- Integration: parent agent spawning a sub-agent produces a sub-agent root span linked to the spawn span.

#### PR checklist

- [ ] `pytest agents/tests/test_observability_spans.py -v` passes
- [ ] `pytest tests/integration/test_span_links.py -v` passes
- [ ] `ruff check agents/` clean; `mypy agents/` clean (pre-existing grpc stubs errors only)
- [ ] OTEL TODO at `agents/tools/registry.py:138` removed (search the file post-merge)
- [ ] `PERSATRIX_TRACE_TOOL_PAYLOADS` documented in `docs/observability.md` Span Conventions section
- [ ] Tool-payload capture routes through `agents.observability.redact.Redactor` (the same Protocol RFC 0018 PR 1 introduced)
- [ ] TICK span wraps `_LLMPersonaAgent.on_tick()` in `agents/persona_runtime/__init__.py` (module pinned in the plan above; this checkbox just re-asserts the implementation site)

---

### PR 3: `feature/v023-otel-metrics` — Phase 3a: Metrics (Python + Go)

**Joint order position**: #6 (after RFC 0018 PR 3).
**Depends on**: PR 2 merged. Independent of RFC 0018 PR 3 in code; histogram exemplars only require the active OTEL span context established by PR 1, not the log↔trace enricher. The joint-order #6 placement is a **scheduling** choice: it lands logging correlation (operator-visible deliverables 0018 PRs 1–3) before introducing metrics + a visualisation backend (PR 4), keeping the user-facing deliverable cadence linear. <!-- Rationale corrected per PR #161 review: previous wording ("so the log↔trace correlation contract is in place before metrics-with-exemplars start citing trace IDs") implied a technical dependency on the enricher that does not exist — exemplars are emitted by the SDK from the active span context regardless of whether log records carry trace IDs. -->
**Branch**: `feature/v023-otel-metrics`
**Estimated size**: ~300–450 lines (Python metrics module + Go metrics module + instrumentation sites + tests)

#### Scope

| File | Change |
|------|--------|
| `agents/observability/metrics.py` | **New** — `init_metrics()` / `shutdown()`. Builds the instrument inventory in [RFC § F](0019-opentelemetry-completion.md#f-metrics): counters (`agent.event.dispatched`, `agent.llm.calls`, `agent.tool.invocations`, `agent.observability.spans.dropped`), histograms (`agent.persona.tick.interval`, `agent.llm.tokens`, `agent.llm.duration`, `agent.tool.duration`), gauges (instance count). Histograms emit exemplars (default OTEL SDK behaviour, verified in test). |
| `internal/observability/metrics/metrics.go` | **New** — orchestrator metrics: `orchestrator.workflow.submitted`, `orchestrator.workflow.completed`, `orchestrator.workflow.failed`, `orchestrator.step.dispatched`, `orchestrator.step.duration`. |
| `cmd/orchestrator/main.go` | Init the metrics provider at startup; instrument the workflow submit / complete / step dispatch sites. |
| `agents/server.py` | Init metrics alongside tracing; register shutdown. |
| `agents/persona_runtime/...`, `agents/llm_client.py`, `agents/tools/registry.py`, `agents/persona_behavior.py` | Record the documented metrics at the same call sites that emit the corresponding spans (PR 2). |
| `agents/tests/conftest.py` | Add an `InMemoryMetricReader` fixture. |
| `agents/tests/test_observability_metrics.py` | **New** — instrument inventory matches [RFC § F](0019-opentelemetry-completion.md#f-metrics); units and attribute keys are correct; histograms emit exemplars carrying valid `trace_id` / `span_id`. |
| `internal/observability/metrics/metrics_test.go` | **New** — orchestrator-side instruments emit at the documented call sites; counter increments are monotonic; histogram buckets sane. |

#### Key implementation details

- Metric names follow the `<service>.<area>.<measurement>` convention (`agent.llm.calls`, `orchestrator.workflow.submitted`). Persatrix-prefix attributes (e.g., `persatrix.agent.id`) match the schema-parity contract enforced in PR 4's parity test.
- Exemplars are emitted by the OTEL SDK when a span is active at the recording site. PR 4's E2E test confirms exemplars survive the round trip through the Collector and into Prometheus.
- The Python `BatchSpanProcessor` overflow drop counter (`agent.observability.spans.dropped`) becomes load-bearing here — PR 1's no-op hook now points at this counter.
- Same goes for the agent shipper's overflow counter from RFC 0018 PR 5 (if merged before PR 3 in joint order #6 vs #9 — joint order has 0018 PR 5 *after* 0019 PR 4, so this is *forward-looking* coordination, not a hard dep). The counter name is reserved here so RFC 0018 PR 5 can slot into it without renaming.

#### Tests

- Unit (Python): every documented instrument is registered with the correct name, kind, unit, and attribute set.
- Unit (Python): a histogram recording inside an active span produces an exemplar with the span's `trace_id` and `span_id`.
- Unit (Go): orchestrator instruments registered with correct names + units; counters emit on workflow lifecycle events.
- Smoke: PR description records a 1-minute `make run` + workflow submission with `OTEL_METRICS_EXPORTER=otlp` and `OTEL_EXPORTER_OTLP_ENDPOINT` pointed at a local debug exporter; output captured.

#### PR checklist

- [ ] `pytest agents/tests/test_observability_metrics.py -v` passes
- [ ] `go test ./internal/observability/metrics/... -v -race` passes
- [ ] Every instrument in [RFC § F](0019-opentelemetry-completion.md#f-metrics) is implemented
- [ ] Histogram exemplars verified in unit test (not deferred to PR 4's E2E test)
- [ ] `agents/observability/metrics.py:init_metrics()` exposes a `shutdown()` callable that flushes pending exports

---

### PR 4: `feature/v023-otel-collector-and-e2e` — Phase 3b: Collector Pipeline + docker-compose + E2E + Schema-Parity Test

**Joint order position**: #7 (after RFC 0019 PR 3).
**Depends on**: PR 3 merged.
**Branch**: `feature/v023-otel-collector-and-e2e`
**Estimated size**: ~350–500 lines (Collector config + compose changes + E2E + parity test + ops doc)

#### Scope

| File | Change |
|------|--------|
| `config/observability/otel-collector.yaml` | **New** — reference Collector config with the `tail_sampling` processor from [RFC § H](0019-opentelemetry-completion.md#h-sampling-back-pressure-and-the-collector-pipeline). Path chosen per PR #160 review to align with the `config/` convention. |
| `docker-compose.yaml` | Add the OTEL Collector service in front of Jaeger; add Prometheus as the metrics backend; add Loki as the logs backend (development only). Existing Jaeger service stays; OTLP traffic now flows orchestrator → Collector → Jaeger/Prometheus/Loki. |
| `tests/integration/test_observability_e2e.py` | **New** — requires the docker-compose stack up. Submit a workflow → poll Jaeger for the resulting trace ID → poll Prometheus for the matching metrics (with exemplars) → query Loki (or `persatrix logs --trace <trace_id>` if RFC 0018 PR 6 has merged) for the correlated log lines. Assert parent/child relationships, metric counts, and that log lines link back to the same trace. |
| `tests/integration/test_log_trace_correlation.py` | **New** — emit log lines from inside a span on both Go and Python sides; assert every record carries the active span's `trace_id` and `span_id`, plus known baggage entries. (This complements RFC 0018 PR 3's correlation test by exercising the joint signal flow.) |
| `tests/integration/test_observability_schema_parity.py` | **New** — **schema parity contract test** added per PR #160 review. Asserts that every Persatrix correlation ID listed in [RFC 0018 § B](0018-structured-logging-framework.md#b-common-log-schema) Optional fields (`execution_id`, `step_id`, `agent_id`, `request_id`, `trace_id`, `span_id`) appears with a matching key in this RFC's [§ E](0019-opentelemetry-completion.md#e-span-naming-and-attribute-conventions) attribute conventions (under the `persatrix.*` prefix where applicable), and that the schema-version values declared in both RFCs (`schema_version: "1"` for logs; `schema_url=…/1.0.0` for traces/metrics) are pinned in code. Prevents silent drift between the two schemas across future revisions. |
| `docs/observability.md` | Append: "Viewing traces in Jaeger", "Querying metrics in Prometheus", "Correlated debugging from a trace ID", and the Collector/sampling section. |
| [README.md](../../README.md) | Update the OTEL paragraph to reflect logs + traces + metrics end-to-end coverage. |
| [CHANGELOG.md](../../CHANGELOG.md) | Note the new Collector + Prometheus + Loki services in `docker-compose.yaml`. |

#### Key implementation details

- The Collector uses a tail-sampling processor with the policy set documented in [RFC § H](0019-opentelemetry-completion.md#h-sampling-back-pressure-and-the-collector-pipeline). Head sampling stays at rate 1.0 (parent-based); tail sampling is the cost-control lever.
- The docker-compose changes are dev-only — production operators run their own Collector. The README change makes this explicit.
- The E2E test is opt-in via a pytest marker (`@pytest.mark.requires_compose`) so it does not run in the default unit-test path. CI gates it separately.
- The schema-parity test is the safety net for "the two RFCs share a schema and a redactor — silent drift would be a slow-motion incident". It runs in the default test path (no compose dep).

#### Tests

- Unit: schema-parity test (no compose dep) — runs in CI default suite.
- Unit (Python): log↔trace correlation test using captured logs + the `InMemorySpanExporter` fixture (no compose dep).
- Integration (compose-gated): E2E shape test against Jaeger + Prometheus + Loki.
- Manual smoke documented in PR description: `make docker-up`, submit a workflow, open `http://localhost:16686` and find the trace, click an exemplar in Prometheus to jump back to a span, run `persatrix logs --trace <trace_id>` (if RFC 0018 PR 6 merged).

#### PR checklist

- [ ] `pytest tests/integration/test_observability_schema_parity.py -v` passes (no compose dep)
- [ ] `pytest tests/integration/test_log_trace_correlation.py -v` passes (no compose dep)
- [ ] `pytest -m requires_compose tests/integration/test_observability_e2e.py -v` passes against the local compose stack (manual gate; not in default CI)
- [ ] `config/observability/otel-collector.yaml` documents every processor + exporter in the pipeline
- [ ] README OTEL paragraph mentions logs + traces + metrics + Collector
- [ ] CHANGELOG v0.2.3 entry mentions the new Collector + Prometheus + Loki services in `docker-compose.yaml`

---

### PR 5: `feature/v023-rfc0019-followups-close` — Review Follow-Ups + RFC Close

**Joint order position**: #11 (opened together with 0018 PR 7 as a paired closeout).
**Depends on**: PR 4 merged **and** RFC 0018 PR 6 merged (so the v0.2.3 logging delivery is also in main).
**Branch**: `feature/v023-rfc0019-followups-close`
**Estimated size**: ~150–300 lines (fixes + RFC status flip + ROADMAP + manual-test report append)

#### Scope

Per [.github/copilot-instructions.md](../../.github/copilot-instructions.md) ("PR review reports are local-only artifacts"), each follow-up entry must paraphrase the finding and **not** reference or link any `docs/pr-reviews/*.md` file. Items below are populated as PRs land and reviews complete.

<!-- Empty subsections below are intentional placeholders. Each is populated when the corresponding PR's review completes. The `<!-- TODO: populate after PR N review -->` markers below are added per PR #161 review so `git grep "TODO: populate after"` lists outstanding follow-up captures at any point. -->

##### From PR 1 review

<!-- TODO: populate after PR 1 review merges -->
*(populated during PR 1 review)*

##### From PR 2 review

<!-- TODO: populate after PR 2 review merges -->
*(populated during PR 2 review)*

##### From PR 3 review

<!-- TODO: populate after PR 3 review merges -->
*(populated during PR 3 review)*

##### From PR 4 review

<!-- TODO: populate after PR 4 review merges -->
*(populated during PR 4 review)*

##### RFC close

- Flip [RFC 0019 status](0019-opentelemetry-completion.md) → `✅ Implemented`; record merged-PR list.
- Update [ROADMAP.md](../../ROADMAP.md) RFC tracker row (0019) → `✅ Implemented`; flip the v0.2.3 milestone row to `✅ Released` (joint with RFC 0018 — see [0018-pr-plan.md](0018-pr-plan.md) PR 7).
- Add v0.2.3 manual-test report row(s) in `docs/manual-tests/` for any traces/metrics-specific manual checks (Jaeger trace lookup, Prometheus exemplar click-through, Collector tail-sampling spot-check).
- Confirm [PR #161](https://github.com/mkhomutov/Persatrix/pull/161) (the RFC 0018 + 0019 PR plan document PR) appears in the [ROADMAP.md](../../ROADMAP.md) merged-PR table. <!-- Added per PR #161 review: development-workflow.md "Status Hygiene" requires every merged PR to appear in the ROADMAP table; the plan PR itself is subject to the same rule. -->

#### PR checklist

- [ ] All review follow-ups from PRs 1–4 addressed or explicitly deferred (with rationale)
- [ ] [RFC 0019 status](0019-opentelemetry-completion.md) → `✅ Implemented`
- [ ] [ROADMAP.md](../../ROADMAP.md) RFC tracker row updated; v0.2.3 milestone row reflects joint observability close
- [ ] PR #161 appears in ROADMAP merged-PR table
- [ ] Manual-test report appended for v0.2.3 traces/metrics coverage
- [ ] `make test` passes; `make lint` clean

---

## Risk and Mitigations

| Risk | Mitigation |
|------|------------|
| `internal/telemetry` → `internal/observability` rename (PR 1) breaks unrelated importers | Single PR scoped to the rename + Python OTEL init; `go build ./...` and full test suite run before merge; CHANGELOG entry. |
| Python OTLP exporter swap (PR 1) breaks operators with custom Collectors on the gRPC port | CHANGELOG entry calls out the port change (`:4317` → `:4318`); README OTEL paragraph updated in PR 4. |
| Span attribute drift between RFC 0018 schema and RFC 0019 conventions | Schema-parity contract test (PR 4) runs in the default CI suite. |
| Tool-payload capture leaks secrets (PR 2) | Routed through the same `Redactor` Protocol as RFC 0018; default mode is `none`; `full` mode passes every value through the redactor. |
| Tail-sampling policy too aggressive in production | Collector config is shipped as a reference, not a managed deployment; ops doc covers per-policy tuning. |
| E2E test (PR 4) flakes against compose stack | Marked `@pytest.mark.requires_compose`; not in default CI; runs as a manual gate before release. |

---

## ROADMAP Hygiene

Per [.github/copilot-instructions.md](../../.github/copilot-instructions.md) "Status Hygiene":

- **PR 1 opens** → flip [ROADMAP.md](../../ROADMAP.md) RFC 0019 row to `🚧 Implementing`.
- **Each PR merges** → update the merged-PR table in ROADMAP and tick the corresponding checklist line in this plan.
- **PR 5 merges** (joint with RFC 0018 PR 7) → flip RFC 0019 row to `✅ Implemented`; flip v0.2.3 milestone row to `✅ Released` jointly with RFC 0018.
