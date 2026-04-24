# Changelog

All notable changes to this project will be documented in this file.

## [Unreleased] — v0.2.3

> **Codename:** Observability Foundation

### ⚠️ Operator-Visible Changes

- **Jaeger OTLP host ports unpublished** (RFC 0019 PR 4): the `jaeger`
  service in `docker-compose.yaml` no longer publishes `4317`/`4318` on
  the host. The OTEL Collector now owns the host-facing OTLP ingress
  (also on `4317`/`4318`) and forwards traces to Jaeger over the
  internal compose network. Dev tooling that previously sent OTLP
  directly to `localhost:4317` against Jaeger must either be retargeted
  at the Collector (no other change required — the host ports are the
  same) or pin its Jaeger endpoint to the in-network `jaeger:4317`. See
  [`docs/observability.md` § 11.1](docs/observability.md).

- **Python OTLP exporter transport changed** (`grpc` → `http`):
  `opentelemetry-exporter-otlp-proto-grpc` is replaced with
  `opentelemetry-exporter-otlp-proto-http`. Collector configs pointing the
  Python exporter at `:4317` must switch to `:4318`. The Go exporter was
  already HTTP; both runtimes now use the same endpoint.
- **Go package rename** `internal/telemetry` → `internal/observability`:
  internal only; forks importing it directly must update import paths.
- **Go zap log field keys renamed** to the RFC 0018 schema (`docs/observability.md`).
  The **reserved correlation IDs** (`execution_id`, `agent_id`, `workflow_id`,
  `step_id`) are renamed at every Go call site, with the encoder's
  `legacyRenames` map as a defence-in-depth backstop. Site-local attributes
  (`inputTokens` / `outputTokens`, `retryCount`, `wallTimeMs`, `estimatedCost`,
  `serviceName`, …) **remain camelCase on the wire** pending a future PR that
  nests them under the schema's `attributes` slot. Downstream consumers (log
  shippers, `jq` queries, dashboards) filtering on the renamed correlation IDs
  must switch to the new keys.

  | Old (legacy) | New (RFC 0018 § B) |
  |--------------|--------------------|
  | `runID` | `execution_id` |
  | `executionID` | `execution_id` |
  | `agentID` | `agent_id` |
  | `workflowID` | `workflow_id` |
  | `stepID` | `step_id` |

  Every Go log line now also carries the RFC 0018 required-field group:
  `schema_version: "1"`, `service.kind: "orchestrator"`,
  `service.instance: <hostname>`, and a `source: {file, line, function}`
  object from `zap.AddCaller`. Custom forks constructing their own zap logger
  should switch to
  [`internal/observability/zapenc.NewEncoder`](internal/observability/zapenc/encoder.go)
  for schema-conformant output.

- **`PERSATRIX_LOG_FORMAT=pretty`** selects a human-readable console encoder
  for local debugging. Default (unset or `json`) emits the RFC 0018 wire
  format. Pretty mode is **not** consumed by the future `persatrix logs`
  endpoint — leave unset in production.

- **`PERSATRIX_SERVICE_INSTANCE`** overrides the orchestrator's
  `service.instance` log field (defaults to `os.Hostname()`). Useful in
  containerised deployments where the hostname is an ephemeral pod ID.

- **`PERSATRIX_TRACE_TOOL_PAYLOADS`** controls `agent.tool.execute` span
  detail. Defaults to `none` (only `tool.name`). `metadata` adds
  `tool.arguments.<arg>.type`; `full` emits redacted argument values via
  the same `Redactor` Protocol used for log redaction. Use `full` only
  with a configured redactor — the default `NoopRedactor` echoes values
  verbatim and may capture secrets.

### Added (RFC 0019 PR 2 — Phase 2 spans + Span Links)

- **Semantic OTEL spans** at every Persatrix decision boundary in the Python
  agent runtime: `agent.persona.event`, `agent.persona.tick`,
  `agent.memory.episodic.recall` / `.remember`,
  `agent.memory.relationship.lookup` / `.update`, `agent.llm.call`,
  `agent.tool.execute`, `agent.subagent.spawn`.  Span names follow
  `<service>.<component>.<operation>`; Persatrix attributes use the
  `persatrix.*` namespace.
- **OTEL Gen-AI semantic conventions** on `agent.llm.call`
  (`gen_ai.system`, `gen_ai.request.model`, `gen_ai.usage.input_tokens` /
  `output_tokens`, `gen_ai.response.finish_reasons`) so vendor backends
  render Persatrix LLM traces without project-specific configuration.
- **Span Links** carrying `link.kind` for cross-tree causality: persona
  event → triggered tick wires today; sub-agent spawn → sub-agent root
  ships with RFC 0009; channel/bridge and mesh links are documented and
  reserved for their owning RFCs.
- **Sub-millisecond event phases** (`received` / `queued` / `handled` /
  `completed`) recorded as span events on the `agent.persona.event` span
  rather than as nested spans, keeping trace trees navigable.
- **Tool-payload capture** is opt-in through `PERSATRIX_TRACE_TOOL_PAYLOADS`
  and routes through the redactor surface introduced by RFC 0018 PR 1 —
  one secrets-policy code path serves both logs and span attributes.
- New documentation section `docs/observability.md § 10 — Span conventions`
  inventorying every Persatrix span, its attributes, the Span Link table,
  payload-capture modes, and a correlated debugging walkthrough.

### Added (RFC 0018 PR 3 — cross-process correlation IDs + OTEL trace IDs on logs)

- **Cross-process correlation IDs** propagate from the Go orchestrator
  to Python agents via gRPC metadata (`persatrix-execution-id`,
  `persatrix-step-id`, `persatrix-agent-id`, `persatrix-workflow-id`).
  New `internal/observability/grpcmeta` package owns the constants and
  `InjectIDs` / `ExtractIDs` helpers.
- **Python `LoggingMetadataInterceptor`** binds the four keys to
  structlog contextvars per-RPC and cleans up on success + exception.
  Registered after `GrpcAioInstrumentorServer` so OTEL context exists
  before logging contextvars bind.
- **OTEL trace IDs on Go log records** via new
  `internal/observability/zapenc.LoggerWithContext(ctx, logger)`,
  wired at the executor dispatch boundary. Emits `trace_id` / `span_id`
  when a span is active, omits them otherwise. (Per-call-site binding
  is the otelzap convention; `zapcore.Entry` has no `Context` field.)
- `workflow_id` added to the Python structlog `_FIELD_ORDER`;
  `ExecuteRequest` gains `ExecutionID` / `StepID` populated by the
  scheduler.

### Added (RFC 0019 PR 3 — Phase 3a OTEL metrics, Python + Go)

- **Python OTEL metrics**: new `agents/observability/metrics.py` providing
  `init_metrics()` / `shutdown()` and the instrument inventory documented in
  [RFC 0019 § F](docs/rfcs/0019-opentelemetry-completion.md#f-metrics):
  counters (`agent.tool.invocations`, `agent.llm.calls`, `agent.llm.tokens`,
  `agent.event.dispatched`, `agent.observability.spans.dropped`,
  `agent.observability.logs.dropped`), histograms (`agent.tool.duration`,
  `agent.llm.duration`, `agent.persona.tick.interval`), up/down counter
  (`agent.active`). Recording call sites added in
  [`tools/registry.py`](agents/tools/registry.py),
  [`llm_client.py`](agents/llm_client.py), and
  [`persona_runtime/__init__.py`](agents/persona_runtime/__init__.py);
  provider lifecycle wired from [`server.py`](agents/server.py).
- **Go OTEL metrics**: new `internal/observability/metrics` package providing
  orchestrator-side `Instruments` (`orchestrator.workflow.submitted/completed/failed/active/duration`,
  `orchestrator.step.dispatched/duration`), init/shutdown wired from
  [`cmd/orchestrator/main.go`](cmd/orchestrator/main.go), with recording sites
  in the scheduler (`executeRun`, stage runner) and the REST server's workflow
  submit handler.
- Exporter: OTLP HTTP via `otlpmetrichttp` (Go) and
  `opentelemetry-exporter-otlp-proto-http` (Python); same endpoint as tracing
  (`OTEL_EXPORTER_OTLP_ENDPOINT`, default `http://localhost:4318`). Histogram
  exemplars carry the active span's `trace_id` / `span_id` so dashboards can
  drill into the originating trace.
- Tests: `agents/tests/test_observability_metrics.py` (inventory/units/exemplars/idempotency/helpers)
  and `internal/observability/metrics/metrics_test.go` (inventory, counter
  monotonicity, up/down gauge, histogram bucket sanity, env-var config,
  endpoint helper).

### Added (RFC 0019 PR 4 — Phase 3b Collector pipeline + e2e + schema-parity test)

- [`config/observability/otel-collector.yaml`](config/observability/otel-collector.yaml)
  + [`prometheus.yaml`](config/observability/prometheus.yaml): tail-sampling
  pipeline per [RFC 0019 § H](docs/rfcs/0019-opentelemetry-completion.md#h-sampling-back-pressure-and-the-collector-pipeline).
- `docker-compose.yaml`: dev `otel-collector`, `prometheus` (`:9091`),
  `loki` (`:3100`); orchestrator + agents OTLP → Collector.
- Three new integration tests — schema-parity (pins log `SCHEMA_VERSION`,
  trace+metric `_SCHEMA_URL`, gRPC metadata surface, [RFC 0018 § B](docs/rfcs/0018-structured-logging-framework.md#b-common-log-schema)
  Optional correlation-field map), single-process log↔trace correlation,
  and a compose-gated e2e under the new `requires_compose` marker
  (opt in via `pytest -m requires_compose`).
- `docs/observability.md` § 11 (operator pipeline) + README OTEL bullet.

### Changed (RFC 0019 PR 6 — observability polish)

- LLM provider plugins are now expected to expose a stable `name`
  attribute (`"anthropic"` / `"openai"` / …) used as the OTEL
  `gen_ai.system` attribute on `agent.llm.call` spans. Plugins missing
  the attribute fall back to a class-name-derived value with a one-time
  warning. Declared as the `agents.llm_types.LLMProvider` Protocol.
- `agents.llm_client` no longer hosts the `LLMResponse` /
  `StopReason` / `Usage` / `LLMProvider` / `ToolCall` / `LLMToolResult`
  type definitions — they live in the new leaf module
  `agents.llm_types`. The historical `from agents.llm_client import …`
  paths still resolve (re-exported) so no consumer code needs to
  change; the move breaks the previous `llm_client` ↔ `llm_providers`
  import cycle that required a deferred re-export.
- `agent.persona.tick` spans now derive the `tick.reason` attribute
  (`"scheduled"` / `"woke-on-event"`) from the dispatcher-queued link
  list instead of always emitting `"scheduled"`. Pinned values exposed
  as `agents.persona_runtime.TICK_REASON_SCHEDULED` /
  `TICK_REASON_WOKE_ON_EVENT`.
- The pending-tick-link buffer is now bounded at 32 entries with
  oldest-drop semantics so a paused tick consumer cannot leak memory.
- `EpisodicMemory.recall()` now records exceptions on its span and sets
  `StatusCode.ERROR` before re-raising, matching `store_episode` and
  the LLM/tool spans.
- Empty `subagent.role` span attributes are no longer emitted (kept
  attribute set lean).

### Changed (RFC 0018 PR 8 — log buffer + shipper polish)

- `Buffer.Append` no longer takes the LRU write lock on the hot path:
  re-admitting an existing execution updates an atomic `lastTouch`
  stamp on the ring under an `RLock`, and only the cold path
  (first-admit + eviction sweep) takes the write lock. Eliminates a
  global serialisation point for high-frequency multi-execution
  ingest.
- `diskStore.flush` now releases the disk lock before fsync / parent
  fsync / rename / `evictIfOverCap`, so concurrent flushes for
  different executions no longer serialise on a single fsync.
- `evictIfOverCap` now reads candidate sizes from the maintained
  `totalMap` snapshot under a short critical section instead of
  re-walking `os.ReadDir` and re-stating every directory on each
  sweep; the recursive `os.RemoveAll` runs unlocked.
- The per-execution rate-limit `WARN`-once gate (`rateWarned`) is
  pruned when its execution is LRU-evicted so the gate map cannot
  grow unbounded over the orchestrator's lifetime.
- Disk flush opens the per-sequence `.tmp` file with `O_NOFOLLOW`
  (POSIX) as defence-in-depth against same-UID symlink-pre-creation
  attacks.
- `disk.scan` / `disk.read` now use `strconv.Atoi` instead of
  `fmt.Sscanf("%d", ...)` and reject non-positive sequence numbers,
  so externally-dropped junk filenames (whitespace-padded, signed)
  no longer skew `nextSeq`.
- Python log shipper's `record_to_proto` now filters bookkeeping keys
  (structlog's `_record` / `_from_structlog`, stdlib `logger` /
  `stack_info` / `exc_info`) plus any `_`-prefixed key from the
  outgoing `attributes` Struct.
- `_to_timestamp` preserves sub-second precision for numeric epoch
  inputs (`FromNanoseconds(int(ts * 1e9))` instead of the previous
  `FromSeconds(int(ts))`), so chronological merges across log sources
  no longer lose ordering within the same second.

### Fixed (RFC 0018 PR 2 hardening — issue #178 encoder correctness cluster)

- [`internal/observability/zapenc.NewEncoder`](internal/observability/zapenc/encoder.go)
  now panics at construction when `Options.ServiceKind` or
  `Options.ServiceInstance` is empty. Both are members of the RFC 0018
  § B required-field group; returning a usable encoder with empty values
  produced log lines that silently violated the required-field invariant
  — including the `encodeFallbackEnvelope` double-failure path, which
  re-emits the same service.* values. A panic at startup is the correct
  failure mode: no valid zero state exists for a production logger.
- `EncodeEntry` now re-stamps `timestamp`, `level`, `message` from the
  `zapcore.Entry` after the inner-encoder JSON is parsed. Previously, a
  caller that added a user field with one of those keys
  (e.g. `zap.String("timestamp", "fake")`) could shadow the encoder's
  value via `encoding/json`'s last-value-wins duplicate-key rule. The
  reserved-key shadowing contract now applies uniformly to the whole
  encoder-owned set (`schema_version`, `service.kind`, `service.instance`,
  `service.role`, `timestamp`, `level`, `message`, `source`). The
  re-stamped `timestamp` is now normalised to UTC (RFC 3339 Nano with a
  `Z` suffix) to match [`docs/observability.md` § 2](docs/observability.md)
  ("timestamp — RFC 3339 with timezone; UTC by default"); previously,
  zap's inner `RFC3339NanoTimeEncoder` passed through whatever zone
  `time.Now()` carried, so a non-UTC host emitted a local offset such as
  `+02:00`. Downstream consumers that parsed the offset as informational
  are unaffected (RFC 3339 Nano remains valid); those that string-matched
  a specific offset must switch to `Z`.
- `source` is now explicitly deleted from the parsed record when
  `entry.Caller.Defined` is false, so a user field `zap.Any("source", …)`
  cannot leak a forged provenance object onto the wire when
  `zap.AddCaller()` was not enabled.

## [0.2.2] - 2026-04-22

> **Codename:** Bounded Persona Memory Injection

### Highlights

- Persona-agent memory injection now enforces a per-event token budget. A new
  `MemoryBudget` allocator distributes available tokens across the three memory
  tiers (episodic, relationship, working) and truncates injected context to fit.
- Episodic and relationship `recall` / `recall_notes` calls now accept a
  `min_score` relevance threshold, reducing noise in injected memory.
- TICK events that admit zero memory items after budget allocation are
  short-circuited before reaching the LLM, eliminating spurious cost on
  persona agents with empty context windows.

### Upgrade Notes

- **No breaking changes.** All RFC 0017 changes are internal to the Python
  agent runtime. No proto changes, no new REST endpoints, no config schema
  changes.
- **Optional:** `min_score` defaults to `0.0` (matches previous behaviour).
  Set it in `recall`/`recall_notes` tool calls to filter low-relevance
  memories proactively.

### 🚀 Features

- *(agents)* `MemoryBudget` allocator + token-aware truncation (RFC 0017 PR 1/7) (#145)
- *(agents)* `_inject_memory_context` allocate-loop rewrite (RFC 0017 PR 2/7) (#146)
- *(memory)* `min_score` relevance threshold on `recall`/`recall_notes` (RFC 0017 PR 3/7) (#147)
- *(agents)* Wire `min_score` and remove legacy gates (RFC 0017 PR 4/7) (#148)

### 🐛 Bug Fixes

- *(agents)* Short-circuit empty-context TICKs (RFC 0017 PR 5/7) (#149)
- *(agents)* RFC 0017 PR 6 review follow-ups (#152)

### 📚 Documentation

- *(safety)* Add cost warning, responsible-use section, and runtime cost notice (#150)
- *(rfcs)* Close RFC 0017 — Persona Memory Injection Token Budget (#153)
- *(manual-tests)* Add MT-MEMORY-004 and MT-PERSONA-003 runbooks for RFC 0017 (#154)
- *(release)* v0.2.2 release checklist + prep plan + README/guide refresh (#156)

### 🧪 Testing

- *(manual)* v0.2.2 execution report — 18 pass, 1 accepted-with-known-gap (#155)

### 📦 Miscellaneous

- *(deps)* Bump `rustls-webpki` from 0.103.10 to 0.103.12 in `/cli` (#139)

[0.2.2]: https://github.com/mkhomutov/Persatrix/compare/v0.2.1...v0.2.2

## [0.2.1] - 2026-04-21

> **Codename:** Talk to Your Agents

### Highlights

- Human-agent chat is now part of the core surface. Open a terminal and run
  `persatrix chat <agent_id>` to start an interactive conversation with any persona agent.
- A new `Participant` protocol and `UserParticipant` implementation give the system a
  first-class model for human participants, with relationship-memory tracking per user-agent pair.
- The `POST /api/v1/agents/{id}/chat` REST endpoint and the `SendChatMessage` gRPC RPC
  are both live and tested (see MT-CHAT-001 through MT-CHAT-004 in the manual-test suite).
- Binary renamed from `orch` to `persatrix` — the CLI is now a single, coherent tool.

### Upgrade Notes

- **New gRPC RPC:** `SendChatMessage` added to `AgentService` (proto/task.proto). Regenerate
  gRPC stubs if you maintain a custom client.
- **New REST endpoint:** `POST /api/v1/agents/{id}/chat` — accepts `{message, user_id, session_id}`
  and returns `{reply, session_id, agent_display_name, reply_status}`.
- **Binary rename:** the CLI binary is now `persatrix` (previously `orch`). Update any scripts
  or CI steps that reference the old name.
- **RelationshipMemory generalised:** `RelationshipMemory` now models arbitrary participant pairs
  (agent↔agent or user↔agent). Existing agent-agent relationship data is unaffected.

### 🚀 Features

- *(agents)* Participant Protocol + UserParticipant + UserStore (RFC 0016 PR 1/7) (#119)
- *(agents)* Generalize RelationshipMemory to participant pairs (RFC 0016 PR 2/7) (#120)
- *(agents)* SendChatMessage gRPC servicer + EventDispatcher flag (RFC 0016 PR 3/7) (#121)
- *(server)* Add REST chat endpoint and gRPC chat executor (RFC 0016 PR 4) (#123)
- *(cli)* Add `persatrix chat` command and rename binary (RFC 0016 PR 5/7) (#125)

### 🐛 Bug Fixes

- *(agents,cli)* Address PR 1–5 review follow-ups (RFC 0016 PR 6/7) (#127)
- *(persona-runtime)* Apply PR #131 deep-review follow-ups (#133)

### 🔧 Refactoring

- *(executor)* Split executor.go into executor.go + dispatch.go (#124)

### 📚 Documentation

- *(rfcs)* Correct author attribution across all RFCs (#115)
- *(rfc)* RFC 0015 — Process Automation & Pattern Extraction (#114)
- *(rfc)* RFC 0016 — Human Participant & Chat Interface (#116)
- *(rfc)* Accept RFC 0016 and add PR implementation plan (#118)
- *(rfc)* Close RFC 0016 — Human Participant & Chat Interface (PR 7/7) (#128)
- *(diagrams)* Architecture diagram refresh for v0.2.1 chat surface (#132)
- *(guide)* Add chat walkthrough to persona-agents guide (#135)
- *(readme)* Refresh README for v0.2.1 chat surface (#136)
- *(release)* Add v0.2.1 release checklist (#137)

### 🧪 Testing

- Author manual tests — chat & participant surface (MT-CHAT-001..004) (#130)
- Execute manual test suite, record results (#131)

[0.2.1]: https://github.com/mkhomutov/Persatrix/compare/v0.2.0...v0.2.1

## [0.2.0] - 2026-04-18

> **Note:** Persatrix was previously developed internally under a different name.
> The project was renamed in April 2026 prior to this first public release.

### Highlights

- Persona-agent runtime is now part of the core surface for v0.2, including event-driven behavior,
  autonomous ticks, and integrated memory tools.
- Memory capabilities now include episodic, relationship, and working tiers with persistence,
  context-window management, and summarization paths.
- Workflow execution now includes execution limits, cost tracking, budget enforcement,
  response caching, and a cost summary API.
- Default `max_tokens` for task agents raised from **4096** to **8192**, improving out-of-box
  capacity for code review and generation workloads.

### Upgrade Notes

- **Behavior change:** task-agent default `max_llm_calls` is reduced from **10** to **5**.
  If your workflows relied on the previous default for long tool/LLM loops, set an explicit
  `max_llm_calls` override in workflow step config or agent config.

### 🚀 Features

- *(agents)* Data-driven TaskAgent + agent type system (#47)
- *(cli)* Wire v0.1 REST endpoints (RFC 0005, PR 1b) (#48)
- *(memory)* Working memory + token estimation (RFC 0005, PR 2) (#49)
- *(memory)* Schema migration + episodic memory core (RFC 0005, PR 3a) (#50)
- *(memory)* Agent-initiated memory tools (RFC 0005, PR 3b) (#51)
- *(memory)* Episode auto-summarization (RFC 0005, PR 3c) (#52)
- *(memory)* Relationship memory (RFC 0005, PR 4) (#53)
- *(agents)* PersonaAgent runtime core (#54)
- *(agents)* Event dispatch + tick loop integration (RFC 0005 PR 5b) (#55)
- *(agents)* Config validation + schema wiring (RFC 0005, PR 6a) (#56)
- *(cli)* Wire validate + test --persona commands (RFC 0005, PR 6b) (#57)
- *(persona,validate)* Persona + validation review fixes (PR 7b) (#60)
- Add defaults package, step limit fields, and schema updates (RFC 0006 PR 1a) (#79)
- Wire execution limits through executor and scheduler (RFC 0006 PR 1b) (#81)
- Implement Python defaults and limit validation (RFC 0006 PR 1c) (#83)
- *(executor)* Derived deadline mode with shared retry budget (RFC 0006 PR 2) (#84)
- *(cost)* Implement TokenCounter and BudgetEnforcer (RFC 0006 PR 3a) (#85)
- *(cost)* CostReporter + scheduler budget integration (RFC 0006 PR 3b) (#86)
- *(state)* StepExecutionMetadata + observability (RFC 0006 PR 4a) (#87)
- *(cost)* RFC 0006 PR 4b — Response Cache + Cost Summary Endpoint (#88)

### 🐛 Bug Fixes

- *(memory)* Memory tier review fixes (RFC 0005, PR 7a) (#59)
- *(cli)* Rust CLI review fixes (RFC 0005, PR 7c) (#62)
- Resolve Windows setup, Docker service discovery, and tool schema bugs (#71)
- *(executor,scheduler,state)* RFC 0006 PR 5a — execution follow-up fixes (#90)
- *(cost)* Atomic budget snapshot, BudgetError struct, config validation (RFC 0006 PR 5b) (#91)
- *(cost)* Remove dead rawPricing field, fix CacheKey non-deterministic hashing
- *(planner,agents)* RFC 0006 PR 5c — Planner/Schema + Python Fixes (#92)
- *(agents)* Surface invalid_fields in negative-limit error metadata (RFC 0006 PR 5c N-01, N-02) (#93)

### 🔧 Refactoring

- *(persona)* Split persona.py into focused modules (RFC 0005, PR 8a) (#64)
- *(persona)* Extract _LLMPersonaAgent to persona_runtime.py (RFC 0005, PR 8d) (#65)
- *(memory)* Split episodic.py into focused modules (RFC 0005, PR 8b) (#66)
- *(cli)* Split main.rs into modules (RFC 0005, PR 8c) (#67)
- Rename project to Persatrix (#70)
- *(agents)* Split persona_runtime.py into package (#95)
- *(scheduler)* Split scheduler.go into stage_runner.go and budget.go (#96)
- *(agents)* Split episodic.py and server.py (v0.2 release prep A-3) (#97)

### 📚 Documentation

- *(rfc)* RFC 0005 — Persona Agent & Memory System (v0.2 planning) (#45)
- *(rfc0005)* Add PR implementation plan for Persona Agent & Memory System (#46)
- *(rfc0005)* Add PR 3a review findings to PR plan
- *(roadmap)* Update episodic memory component status for PR 3c
- *(roadmap)* Add persona.py component status, fix PR #54 link
- Update ROADMAP last-updated date to 2026-04-13
- Fix PR #56 link in ROADMAP merged PR history
- *(rfc0005)* Split PR 7 into 4 sub-PRs (7a-7d) (#58)
- Add development workflow lifecycle guide (#61)
- Add documentation & diagrams phase to workflow and PR plan (RFC 0005, PR 9) (#68)
- Close RFC 0005 — Persona Agent & Memory System (PR 7d, 20/20) (#69)
- *(rfc)* Propose RFC 0006 (Efficiency & Execution Limits) and RFC 0007 (Conditional & Looped Control Flow) (#72)
- *(rfc)* Add RFC 0008 for agent memory and context optimization (#73)
- *(rfc)* Add RFC 0009 — Agent Identity, Security & Sandboxing (#74) (#74)
- *(rfc0006)* Resolve open questions, accept RFC (#75)
- *(rfc0008)* Resolve open questions and accept RFC (#76)
- *(rfc)* RFC 0013 — Legal, Ethical & Regulatory Compliance Framework (#77)
- *(rfc0006)* Add PR implementation plan for Efficiency & Execution Limits (#78)
- *(rfc)* RFC 0014 — Agent Skill Registry & Lifecycle (#80)
- *(roadmap)* Restructure versioning strategy for release velocity (#82)
- *(rfc0006)* Add detailed follow-up PR descriptions (5a-5c) and update status (#89)
- Add v0.2.0 release preparation plan (#94)
- *(tests)* Author manual tests for v0.1 surface (v0.2 release prep C-8) (#98)
- *(tests)* Author manual tests for v0.2 surface (PR 9) (#99)
- README overhaul for v0.2.0 (v0.2 release prep B-4) (#102)
- *(guides)* Persona & memory user guide (v0.2 release prep B-5) (#103)
- *(diagrams)* Phase-neutral architecture diagrams (v0.2 release prep B-7) (#104)

### 📦 Miscellaneous

- Ongoing manual test campaign and fixes (WIP) (#101)
- Move repository to BUSL 1.1 (#63)

[0.2.0]: https://github.com/mkhomutov/Persatrix/compare/v0.1.0...v0.2.0

## [0.1.0] - 2026-04-11

### 🚀 Features

- Scaffold initial project structure (#1)
- Adopt blueprint tooling for project governance and quality gates (#2)
- *(state)* Implement InMemoryStateStore (RFC 0001, PR 1/5) (#6)
- *(registry)* Implement InMemoryRegistry (RFC 0001, PR 2/5) (#7)
- *(planner)* Implement YAMLPlanner Parse+DAG+Plan (RFC 0001, PR 3a/5) (#8)
- *(planner)* Implement ResolveInputs template resolution (RFC 0001, PR 4/5) (#9)
- *(orchestrator)* Wire state, registry, planner into main.go (RFC 0001, PR 5/5) (#10)
- *(server)* HTTP server scaffolding + workflow handlers (RFC 0002, Phase 1) (#14)
- *(server)* Implement agent registry endpoints (RFC 0002, PR 3/4) (#16)
- *(server)* Stub endpoints + main.go wiring + Docker fix (RFC 0002, PR 4/4) (#17)
- *(proto)* Generate Go gRPC stubs from protobuf definitions (#21)
- *(executor)* GRPCExecutor core with retry logic (#22)
- *(state)* Add RunRetrying, SetRunTimestamps, SetRunError (RFC 0003, PR 4/7) (#24)
- *(scheduler)* WorkflowScheduler core with polling, parallel stages, dedup (RFC 0003, PR 3a/7) (#25)
- *(orchestrator)* Wire scheduler + executor into main.go (RFC 0003, PR 5/7) (#27)
- *(agents)* PermissionGate + PathValidator (RFC 0004, PR 2/7) (#36)
- *(agents)* Built-in tools + PR 2 follow-up fixes (RFC 0004, PR 3/7) (#37)
- *(agents)* LLM client + TaskInputConfig + base handle loop (RFC 0004, PR 4a/7) (#38)
- *(agents)* CoderAgent, ReviewerAgent, PlannerAgent (RFC 0004, PR 4b/7) (#39)
- *(agents)* GRPC server + agent loading + proto stubs (RFC 0004, PR 5a) (#40)
- *(agents)* Self-registration + integration tests + follow-up fixes (RFC 0004, PR 5b/7) (#41)

### 🐛 Bug Fixes

- Address accumulated review findings (RFC 0001, PR 6/6) (#12)
- Address accumulated review findings (RFC 0002, PR 5/5) (#18)
- *(state)* Replace rune-based test IDs with fmt.Sprintf (RFC 0001, F-06) (#30)
- *(executor)* Additive dial options, mid-dispatch cancel & retry stress tests (RFC 0003, PR 6) (#31)
- *(orchestrator)* Graceful shutdown drain + absolute workflowsDir (RFC 0003, PR 8) (#33)
- *(agents)* Registration follow-ups + RFC 0004 close (PR 6/7) (#42)
- *(lint)* Resolve all golangci-lint, ruff, mypy, clippy warnings (#44)
- *(agents)* Surface `invalid_fields` in `TaskOutput.metadata` when negative
  execution limits are rejected, to aid operator diagnosis of misconfigured
  `TaskConfig` values. Strengthen explicit-limit test to verify the loop is
  capped at the configured value (RFC 0006 PR 5c follow-ups N-01, N-02)

### 📚 Documentation

- RFC 0001 Core Orchestration Pipeline (#3)
- PR implementation plan for RFC 0001 (#5)
- *(plan)* Update PR plan with PR #8 review follow-ups
- RFC 0002 REST API Server (#4)
- PR implementation plan for RFC 0002 (#11)
- RFC 0003 Scheduler & Executor (#13)
- RFC 0004 Python Agent gRPC Server (#15)
- RFC 0004 PR implementation plan (#19)
- Add ROADMAP.md, status hygiene rules, fix pre-commit checks (#20)
- Update PR plan with PR #22 review findings (N-06..N-11)
- Add follow-up PRs 6-9 to RFC 0003 PR plan (#28)
- Close RFC 0002 PR plan — mark PR 5 as superseded
- *(rfc0001)* Complete PR 6 follow-up scope with all carry-forward findings (#29)
- RFC 0003/0004 status updates, multi-provider LLM design, v0.2 deferrals (#35)
- Update progress tracking for PR #39 merge (RFC 0004, 5/7)
- *(roadmap)* Add missing merged PRs #28, #29, #30, #35 to history table
- Add v0.1 release checklist (#43)

### 🧪 Testing

- *(executor)* IsTransient table-driven tests, retry edge cases, concurrent dispatch (#23)
- *(scheduler)* Step execution, template resolution, error path coverage (RFC 0003, PR 3b/7) (#26)
- Observability improvements — concurrent race tests, log assertions, zaptest logger (#32)

### 🏗️ Build

- *(proto)* Split make proto into go/python targets + CI staleness check (RFC 0003, PR 9) (#34)

### 📦 Miscellaneous

- Update FILEMAP.md

[0.1.0]: https://github.com/mkhomutov/Persatrix/releases/tag/v0.1.0


