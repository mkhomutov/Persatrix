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
0018 PR 8 + 0019 PR 6 (post-merge polish: logbuffer/shipper hot path + tracing/spans cluster — land before the paired closeout so the closeout can reference them as "addressed in PR #X" rather than carrying the diff)
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
PR 6 (post-merge polish: tracing/spans cluster — Nice-to-Have / Nit / Minor items from PRs 1+2 — joint order #11a, optional, lands before PR 5)
  ↓
PR 5 (review follow-ups + RFC close — joint order #11b, opened with 0018 PR 7)
```

PR 6 is **optional**: if any of its captured Nice-to-Have items are deferred (per the closeout PR's Description block exclusions), they become tracked issues against RFC 0009 (security/auth surface — natural home for the redactor `ContextVar` and baggage-key allowlist lint) or a generic `observability-polish` issue, and PR 5 closes the RFC without them.

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
| `agents/pyproject.toml` | Replace `opentelemetry-exporter-otlp-proto-grpc` with `opentelemetry-exporter-otlp-proto-http`; add `opentelemetry-instrumentation-grpc`. (`opentelemetry-instrumentation-system-metrics` is deferred to PR 3 alongside its first consumer — see PR #163 review round 2 Nit #3.) |
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

- [x] `go test ./internal/observability/... -v -race -cover` passes (rename preserves all existing tests)
- [x] `pytest agents/tests/test_observability_tracing.py -v` passes
- [x] `pytest tests/integration/test_trace_propagation.py -v` passes
- [x] No `internal/telemetry/` directory remains
- [x] Pre-merge import-path audit: `grep -r 'internal/telemetry' --include='*.go' .` returns no matches <!-- Added per PR #161 review: the rename touches ~57 Go files; a grep verification is a low-cost safety net for any imports the automated update missed. -->
- [x] All `internal/telemetry` import paths in `internal/...` and `cmd/...` updated
- [x] `agents/pyproject.toml` Python OTLP exporter dep is the HTTP variant
- [x] CHANGELOG v0.2.3 entry covers the OTLP exporter swap (operator-visible) and the Go package rename
- [x] Both `grpc.NewClient` sites carry the `otelgrpc` client handler via the executor's dial-options slices
- [x] HTTP handler is wrapped with `otelhttp.NewHandler` in `cmd/orchestrator/main.go`
- [x] `CompositePropagator(TraceContext + Baggage)` configured globally on both sides
- [x] ROADMAP.md RFC 0019 row: status → 🚧 Implementing on this PR opening (per RFC Decision/Next Steps step 2)

**Merged**: PR [#163](https://github.com/mkhomutov/Persatrix/pull/163) — 2026-04-22

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

- [x] `pytest agents/tests/test_observability_spans.py -v` passes
- [x] `pytest tests/integration/test_span_links.py -v` passes
- [x] `ruff check agents/` clean; `mypy agents/` clean (pre-existing grpc stubs errors only)
- [x] OTEL TODO at `agents/tools/registry.py:138` removed (search the file post-merge)
- [x] `PERSATRIX_TRACE_TOOL_PAYLOADS` documented in `docs/observability.md` Span Conventions section
- [x] Tool-payload capture routes through `agents.observability.redact.Redactor` (the same Protocol RFC 0018 PR 1 introduced)
- [x] TICK span wraps `_LLMPersonaAgent.on_tick()` in `agents/persona_runtime/__init__.py` (module pinned in the plan above; this checkbox just re-asserts the implementation site)

**Merged**: PR [#167](https://github.com/mkhomutov/Persatrix/pull/167) — 2026-04-23

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

- [x] `pytest agents/tests/test_observability_metrics.py -v` passes
- [x] `go test ./internal/observability/metrics/... -v -race` passes
- [x] Every instrument in [RFC § F](0019-opentelemetry-completion.md#f-metrics) is implemented
- [x] Histogram exemplars verified in unit test (not deferred to PR 4's E2E test)
- [x] `agents/observability/metrics.py:init_metrics()` exposes a `shutdown()` callable that flushes pending exports

**Merged**: PR [#170](https://github.com/mkhomutov/Persatrix/pull/170) — 2026-04-23

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

- [x] `pytest tests/integration/test_observability_schema_parity.py -v` passes (no compose dep)
- [x] `pytest tests/integration/test_log_trace_correlation.py -v` passes (no compose dep)
- [ ] `pytest -m requires_compose tests/integration/test_observability_e2e.py -v` passes against the local compose stack (manual gate; not in default CI)
- [x] `config/observability/otel-collector.yaml` documents every processor + exporter in the pipeline
- [x] README OTEL paragraph mentions logs + traces + metrics + Collector
- [x] CHANGELOG v0.2.3 entry mentions the new Collector + Prometheus + Loki services in `docker-compose.yaml`

**Merged**: PR [#171](https://github.com/mkhomutov/Persatrix/pull/171) — 2026-04-23

---

### PR 5: `feature/v023-rfc0019-followups-close` — Review Follow-Ups + RFC Close

**Joint order position**: #11 (opened together with 0018 PR 7 as a paired closeout).
**Depends on**: PR 4 merged **and** RFC 0018 PR 6 merged (so the v0.2.3 logging delivery is also in main).
**Branch**: `feature/v023-rfc0019-followups-close`
**Estimated size**: ~150–300 lines (fixes + RFC status flip + ROADMAP + manual-test report append)

#### Description

The closeout PR ships three classes of work and nothing else. Anything not in one of these buckets is out of scope and should land in its own PR (or be opened as a tracked follow-up issue under the next RFC):

1. **Apply or explicitly defer the per-PR review follow-ups captured below** for PRs 1–4. Each item is either implemented in this PR with a one-line `Review-fix (PR #N)` marker on the touching diff, or moved to a tracked issue with a link from the entry; "silently dropped" is not an option.
2. **Status hygiene flip.** Per [development-workflow.md "Status Hygiene"](../development-workflow.md#status-hygiene): RFC 0019 status → `✅ Implemented`; [ROADMAP.md](../../ROADMAP.md) RFC tracker row updated; the v0.2.3 milestone row flips to `✅ Released` jointly with RFC 0018 (see [0018-pr-plan.md](0018-pr-plan.md) PR 7 — the two close PRs are opened together as a paired closeout, joint order #11); merged-PR table contains every RFC 0019 PR (1–4 plus this PR and the [PR #161](https://github.com/mkhomutov/Persatrix/pull/161) plan PR).
3. **Manual-test report rows for the operator-visible v0.2.3 traces / metrics surface** appended under `docs/manual-tests/`: at minimum a Jaeger trace lookup by `persatrix.workflow_id`, a Prometheus exemplar click-through to the same trace, and a Collector tail-sampling spot-check (one error-tagged trace retained, untagged ticks sampled at the configured rate).

**Hard exclusions**: any new tracing / metrics feature, any structural refactor whose driver is not a captured review item, and any code touching the v0.3 mesh / A2A surface. The PR is sized to stay under the 500-line BRANCHING.md soft limit; if the follow-up workload exceeds that, split *deferred items* into a tracked issue rather than carrying them into the closeout.

#### Scope

Per [.github/copilot-instructions.md](../../.github/copilot-instructions.md) ("PR review reports are local-only artifacts"), each follow-up entry must paraphrase the finding and **not** reference or link any `docs/pr-reviews/*.md` file. Items below are populated as PRs land and reviews complete.

<!-- Empty subsections below are intentional placeholders. Each is populated when the corresponding PR's review completes. The `<!-- TODO: populate after PR N review -->` markers below are added per PR #161 review so `git grep "TODO: populate after"` lists outstanding follow-up captures at any point. -->

##### From PR 1 review

Captured from the PR #163 review rounds 1–3. All round-1 and round-2 *Must Fix* items were addressed in-PR; the items below are the residual *Should Fix* / *Nice to Have* findings deferred here.

- **Should Fix — resolve the unused `agents/tests/conftest.py::span_exporter` fixture.** The fixture is defined but no test consumes it; `agents/tests/test_observability_tracing.py` uses a local `_exporter()` helper and `tests/integration/test_trace_propagation.py` defines its own `mem_exporter`. Either delete the fixture or migrate the 13 unit tests in `test_observability_tracing.py` to consume it (replacing the local helper). Leaving the unused fixture invites PR 2/3 contributors to introduce a third pattern. → **#176**.
- **Nice to Have — collapse duplicate `agents/server.py` imports.** Lines 23–24 import `init_tracing` and `tracing_shutdown` from `.observability.tracing` on two separate lines; combine into a single `from .observability.tracing import init_tracing, shutdown as tracing_shutdown`. → **#176**.
- **Nice to Have — add a Go-side Baggage round-trip test.** Symmetric with the Python `test_baggage_propagator_round_trip`. A single test in `internal/observability/telemetry_test.go` using `propagation.NewCompositeTextMapPropagator(propagation.TraceContext{}, propagation.Baggage{})` and an in-memory carrier closes the cross-runtime gap. → **#176**.
- **Nice to Have — update `cmd/orchestrator/main.go` startup-warning string.** Line 107 still reads `"failed to initialize telemetry, continuing without tracing"`; the variable rename (`obsCfg` / `obsShutdown`) was applied in PR 1 but this operator-visible log string was missed. Change `telemetry` → `observability` for consistency with the renamed package. → **#176**.
- **Nice to Have — add a regression test for `init_tracing()` re-call behaviour.** The PR 1 docstring now correctly documents OTEL's one-way `set_tracer_provider`; lock the documented behaviour in with a test that calls `init_tracing()` twice and asserts (a) the returned tracer is fresh, and (b) a warning is logged on the second call. → **#176**.
- **Nice to Have — add a unit test for `OTEL_EXPORTER_OTLP_ENDPOINT` path-normalisation.** The `/v1/traces` double-suffix guard added during PR 1 review has no explicit test; one `init_tracing()` call with a fake exporter and an inspection of the exporter endpoint is enough. → **#176**.
- **Nice to Have — track the baggage-key allowlist as a real lint.** The PR 1 module docstring (`agents/observability/tracing.py` lines 22–32) mandates the `persatrix.*` namespace and forbids PII / secrets / credentials in baggage values, but enforcement is contributor-vigilance only. A lint or runtime guard that rejects unknown baggage keys would close the residual *Low* security finding from the round-3 review.

##### From PR 2 review

Captured from the PR #167 deep review. All round-1 *Must Fix* items (canonical `gen_ai.response.finish_reasons` mapping, ERROR status on episodic `remember` exceptions, `trust.new` on both relationship update paths, `provider.name` Protocol contract, NoopRedactor warn-once latch, namespace doc clarification) were addressed in-PR. The items below are the residual *Medium* / *Minor* / *Nit* findings deferred here.

- **Should Fix — wrap `EpisodicMemory.recall()` in the same span-error try/except as `store_episode`.** `agents/memory/episodic.py:255` opens `agent.memory.episodic.recall` but does not record `StatusCode.ERROR` / `record_exception()` if the underlying SQLite call raises; the `remember` path was fixed in PR 2 follow-ups but `recall` is now inconsistent. Apply the same pattern (try/except → `record_exception` + set `StatusCode.ERROR` + re-raise) and add a regression test that injects a connection failure and asserts the span's status code. → **#176**.
- **Should Fix — cap `_pending_tick_links` to a bounded ring buffer.** `agents/persona_runtime/__init__.py:199` stores a `Link` per dispatched event awaiting the next tick, with no upper bound. A high event rate combined with a slow or paused tick consumer accumulates links indefinitely (memory growth + every tick span eventually carries a pathological link list). Cap to ~32 entries with oldest-drop semantics and add a unit test that pushes 100 events without a tick and asserts the buffer length stays at the cap. → **#176**.
- **Nice to Have — derive `tick.reason` from the link list.** `agents/persona_runtime/__init__.py:456` hardcodes `tick.reason="scheduled"` even when the tick span carries event-trigger links. Compute `"woke-on-event"` when `_pending_tick_links` is non-empty at tick start; otherwise `"scheduled"`. Prevents doc-vs-code drift while the attribute key is still being introduced. → **#176**.
- **Nice to Have — skip empty-string span attributes.** `event.id` and `subagent.role` are emitted as empty strings on paths where the source value is unset, polluting backend cardinality and making attribute filters noisier. Set the attribute only when the value is a non-empty string. → **#176**.
- **Nice to Have — promote `add_pending_tick_link` to a `Linkable` Protocol.** The dispatcher in `agents/persona_behavior.py` calls `getattr(agent, "add_pending_tick_link", None)` to forward the link, so the contract is enforced at runtime only. Declaring a `Linkable(Protocol)` in `agents/persona_runtime/__init__.py` (or a shared types module) and typing the dispatcher parameter as `Linkable | BaseAgent` lets mypy validate the contract end-to-end. → **#176**.
- **Nice to Have — assert canonical `finish_reasons` shape in tests.** The new `STOP_REASON_TO_GEN_AI` mapping has parametrised tests; add one assertion that the LLM span's `gen_ai.response.finish_reasons` is always a list (per OTEL spec) even when only one reason is present, so a future change to a scalar string is caught. → **#176**.
- **Nice to Have — document the `PERSATRIX_TRACE_TOOL_PAYLOADS=full` + NoopRedactor warning in `docs/observability.md`.** The warn-once latch was added in PR 2 follow-ups but the operator-facing doc only describes the modes; mention that operators selecting `full` without configuring a real redactor will see a one-time warning and that this is the intentional safe-default escape hatch. → **#176**.
- **Should Fix (new — round-2 review) — break the `llm_client` ↔ `llm_providers` import cycle.** Extract shared dataclasses (`LLMRequest`, `LLMResponse`, `StopReason`, `STOP_REASON_TO_GEN_AI`, etc.) into a leaf `agents/llm_types.py` that both modules import from; remove the `# noqa: E402` deferred re-export from `llm_client.py`. → **#176**.
- **Minor (new) — `tool.success` attribute is brittle to non-bool truthy values.** The tool span sets `tool.success` from the raw return; a tool that returns a non-empty string or dict will record `success=True` even on a logical failure. Coerce to `bool(result is not None and not isinstance(result, Exception))` or require tools to return a typed `ToolResult`. → **#176**.
- **Minor (new) — `min_score=-1.0` is a sentinel masquerading as a score.** `EpisodicMemory.recall(min_score=-1.0)` uses `-1.0` to mean "no filter". Use `min_score: float | None = None` and treat `None` as unfiltered; emits a cleaner span attribute too (skip-when-None pairs with the empty-string nice-to-have above). → **#176**.
- **Minor (new) — fragile aiosqlite private-attribute patch in test infra.** `tests/_test_infra.py` reaches into `aiosqlite.Connection._connection` / `._tx`; a library upgrade can silently re-introduce the pytest exit-hang. Pin `aiosqlite` in `pyproject.toml` with a comment pointing at the patch, or replace with a `pytest_sessionfinish` hook. → **#176**.
- **Nit (new) — `_redactor` is a mutable module-level global.** `agents/observability/tracing.py` stores the active redactor at module scope; tests that need a per-test redactor have to monkeypatch and restore. Consider a `ContextVar` so concurrent tests (and future per-request redactor overrides) compose cleanly.
- **Nit (new) — provider-name fallback silently masks misconfigured real providers.** When a provider lacks `name`, the code falls back to the class name. That is correct for shims but hides genuine misconfigurations of real providers. Log a single `warning` the first time the fallback fires for a non-test provider class.

###### Nits

- **Nit — single-source the `tick.reason` enum.** Define the allowed values (`"scheduled"`, `"woke-on-event"`) as a module-level `Final` constant referenced by both the emitter and the test, rather than as bare string literals. → **#176**.
- **Nit — collapse repeated `tracer.start_as_current_span("agent.memory.…")` boilerplate.** Each memory op repeats the same span-context-manager + attributes-dict pattern; consider a small helper in `agents/observability/tracing.py` (`@traced_memory_op(name)`) once the third call site lands. → **#176**.
- **Nit — add a CHANGELOG line for `gen_ai.system` provider contract.** The provider `name` Protocol is a public contract for downstream provider plugins; a one-line note under v0.2.3 unreleased ("Providers must now expose a `name` attribute …") gives plugin authors warning before the close PR ships.

##### From PR 3 review

###### Should-Fix

- **Should Fix — extract duplicated `_env` / `_int_env` helpers.** Both [agents/observability/metrics.py](../../agents/observability/metrics.py) and `agents/observability/tracing.py` carry their own copy of the env-var parser helpers. Extract into a shared `agents/observability/_env.py` module to prevent silent parser drift (e.g., one module trimming whitespace and the other not). → **#176**.

###### Nice-to-Have

- **Nice-to-Have — add a parametrised test for `_classify_llm_error()`.** The classifier in `agents/llm_client.py` powers the `llm.error_type` metric label; today it has no direct unit test, so cardinality changes there can ship undetected. → **#176**.
- **Nice-to-Have — cover the Go `Init()` full path.** Existing tests exercise `NewInstruments`, `NewConfigFromEnv`, and recording helpers in isolation but skip the top-level `Init()` wiring (provider creation + meter registration + shutdown). → **#176**.
- **Nice-to-Have — add lightweight recording-site wiring tests in both languages.** Per-call-site emission is currently deferred entirely to the PR 4 E2E suite; a few unit assertions ("calling `executeRun` with a fake meter increments `workflow.runs.total`") would catch regressions without waiting for the compose stack. → **#176**.
- **Nice-to-Have — test the `runSucceeded=false` failure-defer cleanup path in `executeRun`.** The defer-based terminal emission has both branches; only the success branch is currently covered. → **#176**.
- **Nice-to-Have — use the public `pmetrics.shutdown()` in the Python test fixture.** The current fixture pokes private state (`_meter_provider`) to reset between tests; switching to the public shutdown API keeps the fixture stable across SDK upgrades. → **#176**.

###### Nits

- **Nit — drop the redundant `+1/-1` in `_touch_all` for `agent_active`.** Now that `TestAgentActiveLifecycle` covers the gauge transitions, the touch-all helper can record a no-op `0` add instead of a paired increment/decrement. → **#176**.
- **Nit — document the span-vs-metric attribute key convention divergence.** Spans use `persatrix.workflow_id` while metrics use `workflow.id` (deliberate, to keep metric cardinality keys aligned with OTEL semconv). Add a one-liner to [docs/observability.md](../../docs/observability.md) when PR 4 lands so operators searching for "workflow_id" in metrics aren't surprised. → **#176**.
- **Nit — keep prep refactors out of feature PRs.** The `agents/persona_runtime/prompt_assembly.py` extraction was unrelated to OTEL metrics and was bundled in only to satisfy the 500-line file-size CI cap. Future PRs should land such prep in a separate refactor PR ahead of the feature work.

##### From PR 4 review

Captured from the PR #171 deep re-review (round 2, against the post-fix local branch state). Round-1 *High-severity correctness bugs* (E2E request shape `workflow` vs `workflow_id`, response field `execution_id` vs `run_id`, Jaeger tag `persatrix.execution_id` vs `persatrix.run_id`) and the round-1 *Should-Fix* readiness/CHANGELOG/markexpr/file-size items were addressed in-PR with explicit `Review-fix (PR #171, …)` markers and are not re-listed below.

###### Should-Fix

- **Should Fix — verify and likely correct the Loki query path in the E2E.** [tests/integration/test_observability_e2e.py](../../tests/integration/test_observability_e2e.py) `_loki_has_correlated_log` queries `{trace_id="<id>"}` LogQL. With OTLP HTTP into Loki 3.x's OTLP receiver, `trace_id` is mapped to a **structured-metadata field** by default, not a stream label, so the label-selector query may return no matches even when the correlation is intact end-to-end. Either ship `config/observability/loki-config.yaml` that promotes `trace_id` to a label, or rewrite the query as `{service_name=~"persatrix-.*"} | trace_id="<id>"` with a parser stage. Highest-value follow-up because it gates whether the round-trip is actually validated.
- **Should Fix — pin a `loki-config.yaml` next to `prometheus.yaml`.** [docker-compose.yaml](../../docker-compose.yaml) mounts no config into the `loki` service; behaviour depends entirely on the `grafana/loki:3.1.0` image default for OTLP receiver enablement. A future image bump that flips that default would silently break the logs pipeline. Pinning an explicit config also gives a natural home for the `trace_id` label-promotion fix above.
- **Should Fix — promote `_LOG_TO_SPAN_KEY` to a production constant.** [tests/integration/test_observability_schema_parity.py](../../tests/integration/test_observability_schema_parity.py) keeps the log → span attribute mapping table as a test-local literal; the parity assertion only catches drift between two literals in the same file. Move to e.g. `agents/observability/_schema.py` and assert against it. Turns the test from "two literals in one file stay in sync" into a real contract check.
- **Should Fix — drop the `debug` exporter from steady-state Collector pipelines.** [config/observability/otel-collector.yaml](../../config/observability/otel-collector.yaml) wires `debug` into all three pipelines at `verbosity: basic`; in long-running dev sessions it floods `docker compose logs otel-collector` and dilutes signal. Either remove from steady-state pipelines, gate behind a Compose profile, or move to a stderr-only minimal exporter.
- **Should Fix — readiness gating for the new compose observability services.** Applied for `prometheus` (`/-/ready`) and `loki` (`/ready`) in [docker-compose.yaml](../../docker-compose.yaml). **Deferred for `otel-collector`**: the upstream `otel/opentelemetry-collector-contrib` image is distroless (no shell / `wget` / `nc` / `curl`), so a Docker `CMD` healthcheck cannot be authored without rebuilding the image or adding a probe sidecar. Without a Collector healthcheck, depending services cannot use `condition: service_healthy`, and a cold-start `docker compose up -d` may briefly race the OTLP receiver bind and drop the first batch (visible as a one-off non-zero `agent.observability.{spans,logs}.dropped` on first boot). Steady-state operation is unaffected. Follow-up options: (a) ship a thin Collector image that adds `wget`, (b) add a sidecar probe container, or (c) enable the `health_check` extension and accept the image extension cost.
- **Should Fix — document the Jaeger OTLP host-port unpublish as a breaking dev change.** Already landed in [CHANGELOG.md](../../CHANGELOG.md) "⚠️ Operator-Visible Changes" and [docs/observability.md](../../docs/observability.md) § 11.1. No further action.

###### Nice-to-Have

- **Nice-to-Have — assert tail-sampling policies in the E2E.** Submit one error trace and verify Jaeger has it; submit N untagged ticks and verify roughly 1 % retention over the run. Current E2E would pass even if `tail_sampling` were misconfigured to keep everything.
- **Nice-to-Have — assert `agent.observability.{spans,logs}.dropped` stay at zero across an E2E round-trip.** Locks down the back-pressure invariant from [docs/observability.md](../../docs/observability.md) § 11.5 and would catch the cold-start race noted in the deferred Collector-healthcheck item above.
- **Nice-to-Have — build a thin wrapper image around `otel/opentelemetry-collector-contrib`** that adds `wget`/busybox so `otel-collector` can declare a Docker healthcheck and dependent services can use `condition: service_healthy`. Closes the cold-start race without sidecar gymnastics.
- **Nice-to-Have — wire `requires_compose` into a separate optional CI job** (e.g. `workflow_dispatch`) so the suite does not bit-rot.
- **Nice-to-Have — switch `captured_stderr` to a `WriteLoggerFactory(file=buf)` rewire** in [tests/integration/test_log_trace_correlation.py](../../tests/integration/test_log_trace_correlation.py) so the fixture does not depend on `agents/observability/logging.py`'s `sys` import style. Today the `_SysShim` would silently no-op (and the test pass vacuously) if `logging.py` switched to `import sys as _sys` internally.
- **Nice-to-Have — add a one-line OTLP gRPC :4317 reachability probe** to [tests/integration/test_observability_e2e.py](../../tests/integration/test_observability_e2e.py). Today only the HTTP port (4318) is probed. Matches actual production traffic but a gRPC reachability check would close the loop for operators using gRPC exporters.
- **Nice-to-Have — switch the tail-sampling workflow-tagged policy to exact-list mode.** [config/observability/otel-collector.yaml](../../config/observability/otel-collector.yaml) uses `enabled_regex_matching: true` with `values: [".+"]` against `persatrix.workflow_id`. The `string_attribute` processor also supports an `invert_match: false` exact-list mode that avoids the regex engine on the hot path. Cosmetic; current shape is clear.
- **Nice-to-Have — note in [docs/observability.md](../../docs/observability.md) § 11.5 that `enable_open_metrics: true` causes `trace_id`/`span_id` exemplars to be persisted in Prometheus.** Trace IDs are not PII, but operators with PII-sensitive Prometheus retention should know they are now stored.

###### Nits

- **Nit — fix the `SS H` artifact in the ASCII topology diagram** in [docs/observability.md](../../docs/observability.md) § 11. Should read `§ H`; likely a unicode-escape artifact from the file generator.
- **Nit — move the four backend-reachability probes out of the `requires_compose` marker** (or add an import smoke check elsewhere) so a syntax / import regression in [tests/integration/test_observability_e2e.py](../../tests/integration/test_observability_e2e.py) shows up in the default `make test-integration` run instead of bit-rotting until someone opts in. The four probes already `pytest.skip` on transport errors, so they are safe to run by default.
- **Nit — soften the schema-parity mapping-test docstring** in [tests/integration/test_observability_schema_parity.py](../../tests/integration/test_observability_schema_parity.py) to match its actual scope (two literals in the same module) until `_LOG_TO_SPAN_KEY` is promoted out of the test module per the Should-Fix above.
- **Nit — rename local var `trace = _poll_until(...)`** in [tests/integration/test_observability_e2e.py](../../tests/integration/test_observability_e2e.py) to `trace_data` to avoid shadowing the `opentelemetry.trace` module name imported by the sibling `test_log_trace_correlation.py`. Cosmetic.
- **Nit — tighten the `CHANGELOG.md` file-size exemption comment** in [scripts/checks/file_size.py](../../scripts/checks/file_size.py) to reference `cliff.toml` / the release process so future readers know where the trim happens. Applied in this PR.
- **Nit — note the upstream-Prometheus exemplar-storage requirement** next to the `prometheus` exporter in [config/observability/otel-collector.yaml](../../config/observability/otel-collector.yaml). Already enabled in compose; closes the loop for forkers. Applied in this PR.

##### RFC close

- Flip [RFC 0019 status](0019-opentelemetry-completion.md) → `✅ Implemented`; record merged-PR list.
- Update [ROADMAP.md](../../ROADMAP.md) RFC tracker row (0019) → `✅ Implemented`; flip the v0.2.3 milestone row to `✅ Released` (joint with RFC 0018 — see [0018-pr-plan.md](0018-pr-plan.md) PR 7).
- Add v0.2.3 manual-test report row(s) in `docs/manual-tests/` for any traces/metrics-specific manual checks (Jaeger trace lookup, Prometheus exemplar click-through, Collector tail-sampling spot-check).
- Confirm [PR #161](https://github.com/mkhomutov/Persatrix/pull/161) (the RFC 0018 + 0019 PR plan document PR) appears in the [ROADMAP.md](../../ROADMAP.md) merged-PR table. <!-- Added per PR #161 review: development-workflow.md "Status Hygiene" requires every merged PR to appear in the ROADMAP table; the plan PR itself is subject to the same rule. -->

#### PR checklist

- [x] All review follow-ups from PRs 1–4 addressed or explicitly deferred (with rationale)
- [x] [RFC 0019 status](0019-opentelemetry-completion.md) → `✅ Implemented`
- [x] [ROADMAP.md](../../ROADMAP.md) RFC tracker row updated; v0.2.3 milestone row reflects joint observability close
- [x] PR #161 appears in ROADMAP merged-PR table
- [x] Manual-test report appended for v0.2.3 traces/metrics coverage
- [x] `make test` passes; `make lint` clean

**Merged**: [#181](https://github.com/mkhomutov/Persatrix/pull/181) — 2026-04-24

#### Disposition of review follow-ups

Every per-PR-review bullet ending in "→ **#176**" landed in polish PR [#176](https://github.com/mkhomutov/Persatrix/pull/176). The remaining items are dispositioned below.

##### Applied in this closeout PR

- **PR 4 review Should-Fix #4** — [`config/observability/otel-collector.yaml`](../../config/observability/otel-collector.yaml) drops the `debug` exporter from all three steady-state pipelines; the exporter definition is retained with a comment for opt-back-in during incident triage. Stops `docker compose logs otel-collector` flooding under routine traffic.

##### Deferred follow-ups

The remaining items from [PR 4 review](#from-pr-4-review) (Should-Fix: Loki LogQL query / `loki-config.yaml` pin, `_LOG_TO_SPAN_KEY` promotion, Collector readiness; Nice-to-Have: tail-sampling assertions, dropped-counter invariant, Collector wrapper image, optional CI job, `captured_stderr` rewire, OTLP-gRPC reachability probe, exact-list tail-sampling, exemplar PII note; Nits: ASCII-diagram artifact, backend-probe scope, schema-parity docstring, `trace`→`trace_data` rename) and the four PR 1–3 residuals not in #176 (baggage-key allowlist lint, `_redactor` ContextVar, provider-name fallback warn, `gen_ai.system` CHANGELOG note) are deferred to a single tracked issue, filed at PR open.

---

### PR 6: `polish/v023-tracing-spans` — Post-Merge Polish: Tracing + Spans Cluster

**Joint order position**: #11a (lands before the paired closeout #11b so PR 5 can mark its captured items as "addressed in PR #X" rather than carrying the diff; opened together with 0018 PR 8 as the polish-pair).
**Depends on**: PR 4 merged.
**Branch**: `polish/v023-tracing-spans`
**Estimated size**: ~200–400 lines (implementation + targeted regression tests + one extracted module)
**Status**: **Optional.** If skipped, every item below converts to a tracked issue and PR 5 lists each as "deferred to issue #X with rationale".

#### Why this PR exists (justification)

Three forces converge to make a dedicated polish PR the lowest-friction option for this cluster, instead of either bundling into PR 5 or shipping individual one-line PRs:

1. **Single-surface cohesion.** All captured items touch [`agents/observability/tracing.py`](../../agents/observability/tracing.py), [`agents/persona_runtime/`](../../agents/persona_runtime), and [`agents/llm_client.py`](../../agents/llm_client.py) / [`agents/llm_providers.py`](../../agents/llm_providers.py) — one reviewer with the OTEL spans context loaded handles them in a single pass.
2. **One non-trivial extraction (`agents/llm_types.py`).** The `llm_client` ↔ `llm_providers` import-cycle Should-Fix (round-2 finding from [PR 2 review](#from-pr-2-review)) requires extracting `LLMRequest` / `LLMResponse` / `StopReason` / `STOP_REASON_TO_GEN_AI` into a leaf module. That is structurally meaningful enough to deserve its own commit and review pass; bundling into PR 5 buries the diff under status-flip noise.
3. **PR 5 size budget.** [PR 5](#pr-5-feature-v023-rfc0019-followups-close---review-follow-ups--rfc-close) targets 150–300 lines under the [BRANCHING.md](../BRANCHING.md) 500-line soft cap. Folding `_pending_tick_links` cap + Linkable Protocol + `llm_types.py` extraction + `_env.py` consolidation would push past that cap.

#### Scope

Apply the following items captured in [PR 1 review](#from-pr-1-review), [PR 2 review](#from-pr-2-review), and [PR 3 review](#from-pr-3-review) above. Each maps to one item already enumerated in the closeout PR; this PR becomes the implementation PR for them, and PR 5 cross-references back here.

**From PR 1 review (tracing init):**

- Resolve unused `agents/tests/conftest.py::span_exporter` fixture.
- Collapse duplicate [`agents/server.py`](../../agents/server.py) `init_tracing` / `tracing_shutdown` imports.
- Go-side Baggage round-trip test in `internal/observability/telemetry_test.go`.
- Update [`cmd/orchestrator/main.go`](../../cmd/orchestrator/main.go) line 107 startup-warning string `telemetry` → `observability`.
- `init_tracing()` re-call regression test (warning-on-second-call).
- `OTEL_EXPORTER_OTLP_ENDPOINT` `/v1/traces` double-suffix-guard unit test.

**From PR 2 review (semantic spans):**

- Bound `_pending_tick_links` to ~32 entries with oldest-drop semantics.
- Derive `tick.reason` from link list (`woke-on-event` vs `scheduled`); single-source the enum as `Final`.
- Skip empty-string span attributes (`event.id`, `subagent.role`).
- Promote `add_pending_tick_link` to a `Linkable(Protocol)`.
- Assert canonical `gen_ai.response.finish_reasons` list-shape in tests.
- **Round-2 Should-Fix:** extract `agents/llm_types.py` to break the `llm_client` ↔ `llm_providers` import cycle; remove the `# noqa: E402` deferred re-export.
- Coerce `tool.success` to `bool(result is not None and not isinstance(result, Exception))`.
- Replace `min_score=-1.0` sentinel with `min_score: float | None = None`.
- Pin `aiosqlite` in [`agents/pyproject.toml`](../../agents/pyproject.toml) with a comment pointing at the `tests/_test_infra.py` private-attribute patch (or replace with `pytest_sessionfinish` hook).

**From PR 3 review (metrics):**

- Extract duplicated `_env` / `_int_env` helpers from [`agents/observability/metrics.py`](../../agents/observability/metrics.py) and [`agents/observability/tracing.py`](../../agents/observability/tracing.py) into a shared `agents/observability/_env.py`.
- Parametrised test for `_classify_llm_error()`.
- Go `Init()` full-path test (provider creation + meter registration + shutdown).
- Recording-site wiring tests in both languages.
- `runSucceeded=false` failure-defer cleanup-path coverage in `executeRun`.
- Switch Python test fixture to public `pmetrics.shutdown()` instead of `_meter_provider` poke.

#### Out of scope for this PR (deferred to tracked issues)

- The Collector / E2E Nice-to-Have cluster from [PR 4 review](#from-pr-4-review) (Loki query path / `loki-config.yaml`, Collector wrapper image with healthcheck, tail-sampling assertions in E2E, `_LOG_TO_SPAN_KEY` promotion, `requires_compose` CI job). Lives in `config/observability/`, [docker-compose.yaml](../../docker-compose.yaml), and [`tests/integration/test_observability_e2e.py`](../../tests/integration/test_observability_e2e.py) — ops-runbook surface that can land in v0.2.x patches without delaying v0.2.3 release. Opens as `observability-polish` tracked issue.
- `_redactor` `ContextVar` migration and baggage-key allowlist lint — both belong in RFC 0009 (security/auth) per the existing comment in [`agents/observability/tracing.py`](../../agents/observability/tracing.py); link from the RFC 0009 tracker.
- Provider-name fallback `warning` for non-test classes — needs a stable provider-classification taxonomy first; opens as RFC 0019 follow-up issue rather than a one-line behavioural change.

#### PR checklist

- [x] Items above either applied (with `Polish-fix (PR #N)` marker on the diff) or downgraded to tracked issue with a link from PR 5's [From PR 1 review](#from-pr-1-review) / [From PR 2 review](#from-pr-2-review) / [From PR 3 review](#from-pr-3-review) entries.
- [x] `agents/llm_types.py` extraction passes the existing import-path compat tests (`from agents.llm_client import AnthropicProvider` still resolves).
- [x] No new public API surface beyond the `agents/llm_types.py` extraction and the `Linkable(Protocol)` declaration (polish only).
- [x] `make test` passes; `make lint` clean; `mypy agents/` clean for any newly typed surface.

**Merged**: PR [#176](https://github.com/mkhomutov/Persatrix/pull/176) — 2026-04-23

#### From PR 6 review (round-2 polish)

**Applied:**

- **Should-Fix #1** — `_pending_tick_links` now uses `collections.deque(maxlen=...)` for native O(1) drop.
- **Should-Fix #2** — OTLP endpoint test monkeypatches `OTLPSpanExporter` and asserts ctor kwargs (public contract).
- **Should-Fix #3** — added `test_episodic_recall_span_records_backend_failure` for `Status.ERROR` + exception event on backend failure.
- **Nice-to-Have #2** — fallback test for `gen_ai.system` when provider `name` is missing.
- **Nice-to-Have #4** — narrowed `caplog` in `TestInitTracingRecall` to `opentelemetry`.
- **Nice-to-Have #5** — `LLMProvider` Protocol marked `@runtime_checkable`.
- **Nice-to-Have #6** — `_FakeAgent` buffer init moved into `__init__`.
- **Nice-to-Have #7** — added `isinstance(agent, Linkable)` guard tests.
- **Nit #1** — documented `__all__` ordering convention.

**Deferred:**

- **NTH #1** (hoist `Linkable` import) — keeps lazy import; avoids eager `persona_runtime` load.
- **NTH #3** (`_BoundedLinkBuffer` extraction) — deque conversion already covers behaviour.
- **Nit #3** (`_env.py` docstring) — no-op: already documented.

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
- **PR 6 (optional polish) merges** → add to merged-PR table; cross-reference each addressed item from the corresponding [PR 1 review](#from-pr-1-review) / [PR 2 review](#from-pr-2-review) / [PR 3 review](#from-pr-3-review) bullet in PR 5's section.
- **PR 5 merges** (joint with RFC 0018 PR 7) → flip RFC 0019 row to `✅ Implemented`; flip v0.2.3 milestone row to `✅ Released` jointly with RFC 0018.
