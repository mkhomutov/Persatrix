# RFC 0018 — PR Implementation Plan

**RFC**: [0018-structured-logging-framework.md](0018-structured-logging-framework.md)
**Paired RFC**: [0019-opentelemetry-completion.md](0019-opentelemetry-completion.md) — joint v0.2.3 "Observability Foundation" delivery (paired plan: [0019-pr-plan.md](0019-pr-plan.md))
**Created**: 2026-04-22
**Branch prefix**: `feature/v023-`
**Target**: `main`
**Merge strategy**: Squash merge per [BRANCHING.md](../BRANCHING.md)

---

## Overview

RFC 0018 lands the **structured logging half** of the v0.2.3 Observability Foundation:

1. A versioned, cross-language log schema ([RFC § B](0018-structured-logging-framework.md#b-common-log-schema)) with `schema_version: "1"` and a stable field-emission order.
2. Python `structlog` adoption replacing stdlib `logging.getLogger` across `agents/`.
3. Go `zap` field renames + a no-op redaction hook + a `pretty` console encoder + a `source` (file/line/function) field.
4. Cross-process correlation: `execution_id` / `step_id` / `agent_id` propagated over gRPC metadata; `trace_id` / `span_id` joined to every log line within an OTEL context.
5. A working `persatrix logs <id>` CLI: ring buffer + on-disk store + `LogService` streaming gRPC + REST endpoints (with `since` / `workflow` / `level` filters and `id=_` cross-execution) + SSE `--follow` endpoint + Rust CLI rewrite.

The RFC spans 4 substantive phases plus a wrap-up. This plan splits the work into **7 PRs** so each stays under the 500-line BRANCHING.md limit and each leaves the repo in a passing-tests, lint-clean state.

> **Estimate calibration**: RFC 0005, 0006, 0016, and 0017 PRs landed within a 1.7× calibration factor relative to initial estimates. This plan applies the same factor.

**Prerequisite**: RFC 0017 fully merged (7/7 PRs — done as of v0.2.2). No code dependency on prior RFCs at landing time, but see Cross-RFC sequencing below for ordering against RFC 0019.

---

## Cross-RFC Sequencing (verbatim from [RFC 0018 Decision / Next Steps](0018-structured-logging-framework.md#decision--next-steps) and [RFC 0019 Decision / Next Steps](0019-opentelemetry-completion.md#decision--next-steps))

The two RFCs share namespace and code paths, so PR landing order matters:

1. **RFC 0019 Phase 1 lands before any RFC 0018 PR that adds packages under `internal/observability/`.** RFC 0019 Phase 1 performs the `internal/telemetry/` → `internal/observability/` rename; landing RFC 0018 packages first would force the rename PR into a rename-plus-merge-conflict-resolution PR.
2. **RFC 0019 Phase 1 lands before RFC 0018 Phase 3.** RFC 0018 Phase 3 declares `RFC 0019 Phase 1 (OTEL initialised on Python side)` as a prerequisite; the cross-process correlation work needs the OTEL context already established.
3. **RFC 0018 Phase 1 lands before RFC 0019 Phase 2.** RFC 0019 Phase 2 spans need the redaction hook to exist for opt-in tool-payload capture, and the log↔trace enricher RFC 0019 Phase 2 cross-references is added in RFC 0018's Phase 3.

---

## Joint Merge Order (RFCs 0018 + 0019)

The two plans interleave. The combined order across both RFCs is:

> **Maintenance note**: this ASCII diagram is intentionally duplicated **verbatim** in [0019-pr-plan.md](0019-pr-plan.md). If you edit the order here, update the paired plan in the same commit. <!-- Callout added per PR #161 review nice-to-have #3: verbatim duplication is the deliberate single-source-of-truth choice (both reviewers see the same order from either entry point), but it carries a drift hazard. -->

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

## RFC 0018 Dependency Graph

```
0019 PR 1 (rename + Python OTEL — paired-plan prereq for any internal/observability/ package)
  ↓
PR 1 (Python structlog + schema doc + no-op redactor — joint order #2)
  ↓
PR 2 (Go zap rename + pretty + redactor wired + source — joint order #3)
  ↓
PR 3 (cross-process correlation; needs 0019 PR 1's OTEL context — joint order #5)
  ↓
PR 4 (proto + logbuffer ring + disk store — joint order #8)
  ↓
PR 5 (LogService server + agent shipper + REST + SSE endpoints — joint order #9)
  ↓
PR 6 (CLI rewrite + E2E — joint order #10)
  ↓
PR 7 (review follow-ups + RFC close — joint order #11, opened with 0019 PR 5)
```

All PRs are sequential.

---

## PR Sequence

### PR 1: `feature/v023-logging-python-structlog` — Phase 1: Schema Doc + Python `structlog` + Redactor Surface

**Joint order position**: #2 (after 0019 PR 1).
**Depends on**: 0019 PR 1 merged (the `internal/observability/` namespace exists; `agents/observability/` is the natural pair).
**Branch**: `feature/v023-logging-python-structlog`
**Estimated size**: ~350–500 lines (implementation + tests + docs)

#### Scope

| File | Change |
|------|--------|
| `docs/observability.md` | **New** — defines the schema in [RFC § B](0018-structured-logging-framework.md#b-common-log-schema), the field-ordering contract, `schema_version: "1"`, and the `PERSATRIX_LOG_FORMAT=pretty` env override. RFC 0019 will append span-conventions / metrics sections in its Phase 2 and 3. |
| `agents/observability/__init__.py` | Pre-existing after 0019 PR 1 (joint order #1 creates this package marker as part of the `agents/observability/` namespace setup) — no action needed in this PR. <!-- Clarified per PR #161 review: listing this as `**New**` would mislead implementers into treating a pre-existing file as a merge conflict. 0019 PR 1 owns the package creation; 0018 PR 1 adds modules inside it. --> |
| `agents/observability/logging.py` | **New** — `configure_logging()` builds the structlog chain (contextvars merge + OTEL processor placeholder + redactor hook + JSON renderer; pretty renderer when `PERSATRIX_LOG_FORMAT=pretty`). `get_logger(name)` returns a structlog `BoundLogger`. |
| `agents/observability/redact.py` | **New** — `Redactor` Protocol; `NoopRedactor` default. The Protocol surface is the same shape used by RFC 0019 Phase 2 for tool-payload capture, ensuring one redaction contract across both signals. |
| `agents/server.py`, `agents/base.py`, `agents/llm_client.py`, `agents/dispatch.py`, `agents/persona.py`, `agents/persona_behavior.py`, `agents/tick.py`, `agents/participant.py`, `agents/server_persona.py`, `agents/server_servicers.py`, `agents/task_agent.py`, `agents/validate.py`, `agents/persona_runtime/*.py`, `agents/sub_agents/*.py`, `agents/memory/*.py`, `agents/tools/*.py` | Replace `logging.getLogger(__name__)` with `from agents.observability.logging import get_logger; logger = get_logger(__name__)`. Replace any `logger.info("msg %s", x)` with structured `logger.info("msg", x=x)` form. No behavioural change to log content; this is a transport swap. |
| `agents/pyproject.toml` | Add `structlog` runtime dep. |
| `agents/tests/test_observability_logging.py` | **New** — asserts JSON output shape, `schema_version` presence, field ordering (timestamp → level → logger → message → service.* → schema_version → event-specific keys), `PERSATRIX_LOG_FORMAT=pretty` selects console renderer, redactor pass-through is invoked for every record. |

#### Key implementation details

- The OTEL processor in the structlog chain is added as a placeholder in this PR — it reads the active span via `opentelemetry.trace.get_current_span()` (already initialised by 0019 PR 1) and adds `trace_id` / `span_id` when present, else omits them. Real cross-process correlation work (orchestrator-side IDs in metadata; agent-side interceptor) is PR 3.
- The redactor is invoked on **every** log record (including those without sensitive fields) so future redactor implementations can rely on a single hook. `NoopRedactor.redact(record: dict) -> dict` returns `record` unchanged.
- `service.*` fields (`service.kind`, `service.instance`, optional `service.role`) are added as structlog **bound context** at `configure_logging()` time so every line carries them without per-call boilerplate.
- The schema doc in `docs/observability.md` is the single source of truth — RFC 0019's Phase 2/3 will append, never overwrite.

#### Tests

- JSON record contains the full required-field set in the documented order.
- `schema_version == "1"` on every record.
- `PERSATRIX_LOG_FORMAT=pretty` swaps to a console renderer and disables the JSON renderer.
- `NoopRedactor.redact()` is invoked exactly once per record (assert via spy).
- Contextvars set inside an `async with structlog.contextvars.bound_contextvars(...)` block appear in the emitted record and are cleared after.
- `get_logger("test").info("event", k=1)` round-trips through the chain without touching stdlib `logging` (assert by capturing on the structlog `WriteLoggerFactory`).

#### PR checklist

- [x] `pytest agents/tests/ -v` passes (1206 tests, 2 skipped — full suite green)
- [x] `ruff check agents/` clean
- [x] `mypy agents/observability/` clean (broader `mypy agents/` has pre-existing grpc-stub errors only)
- [ ] ~~All `logging.getLogger` call sites in `agents/` swapped to `get_logger`~~ — **descoped to follow-up PR 1b**. The `ProcessorFormatter.foreign_pre_chain` configured by `configure_logging()` already renders all stdlib `logging.getLogger`-emitted records through the schema chain, so log output conforms to `schema_version: "1"` immediately after this PR. The mechanical swap + `extra={...}` → kwargs migration ships in a small follow-up PR (1b) so this PR stays under the 500-line BRANCHING.md limit and avoids touching unrelated test scaffolding (`test_persona_tick_shortcircuit.py` depends on stdlib `LogRecord` attribute propagation that requires per-call-site rewrites).
- [x] `agents/observability/redact.py` exports a `Redactor` Protocol matching the surface RFC 0019 Phase 2 tool-payload capture will call
- [x] `docs/observability.md` documents the schema, field ordering, and `PERSATRIX_LOG_FORMAT=pretty`
- [x] ROADMAP.md RFC 0018 row: status → 🚧 Implementing on this PR opening (per RFC Decision/Next Steps step 2 — PR 1 is the first implementation PR)

#### Review follow-ups (from PR #164 deep review, 2026-04-22)

The deep review on the second-round commits classified all remaining items as **Should-Fix or Info, none blocking** (risk: Low–Medium). They are tracked here so PR 1 can land and the items can be handled in PR 1b or a dedicated follow-up:

- [ ] Add a unit test for `set_redactor()` invoked **after** `configure_logging()` (current tests only cover the before-configure ordering). — handle in PR 1b
- [ ] Extend `TestRaisingRedactor` to assert the out-of-band fallback warning actually reaches stderr (not just that the original record still emits). — handle in PR 1b
- [ ] Document the re-entry handler-leak corner case in `configure_logging()` docstring (symmetrical with the now-documented `PERSATRIX_LOG_FORMAT` first-call freeze). — handle in PR 1b
- [ ] Add a concurrency note to `agents/observability/logging.py` covering the module-global `_redactor` / `_configured` (single-process, configure-once-at-startup contract). — handle in PR 1b
- [ ] Verify the two `\ufffd` glyphs in the [ROADMAP.md](../../ROADMAP.md) RFC 0018 row diff hunks render as 🚧 on github.com after merge; if not, repair encoding in a follow-up commit. — verify post-merge
- [ ] File a tracking issue under RFC 0009 for the **real PII/secret scrubber** that replaces `NoopRedactor`, so the deferred work does not become "forever-deferred". — open issue before PR 1b merges
- [ ] Net diff (876 LOC) exceeds the [BRANCHING.md](../BRANCHING.md) 500-line soft limit; acknowledged in the PR body and accepted by reviewers because the schema doc + structlog chain + redactor surface are a single atomic boundary. No action — recorded for future estimate calibration.

Review report (local-only, not committed): `docs/pr-reviews/pr-164.md`.

**Merged**: PR [#164](https://github.com/mkhomutov/Persatrix/pull/164) — 2026-04-22

#### Follow-up: PR 1b — `feature/v023-logging-python-getlogger-swap` (descoped from this PR)

Mechanical migration of `logging.getLogger(__name__)` → `from .observability.logging import get_logger; logger = get_logger(__name__)` across `agents/` (~27 files), plus `logger.info("msg %s", x, extra={"k": v})` → `logger.info("msg", x=x, k=v)` for the ~25 printf-style call sites identified in the audit, plus targeted updates to `test_persona_tick_shortcircuit.py` to assert structlog event-dict fields rather than stdlib `LogRecord` attributes. Estimated size: ~250–400 lines. Joint order position remains #2 (1b is a sub-PR landing immediately after PR 1).

---

### PR 2: `feature/v023-logging-go-zap-rename` — Phase 2: Go Field Normalisation + `pretty` + Redactor Wire-Through + Source Field

**Joint order position**: #3 (after RFC 0018 PR 1).
**Depends on**: PR 1 merged (schema doc is the source of truth).
**Branch**: `feature/v023-logging-go-zap-rename`
**Estimated size**: ~350–500 lines (implementation + tests + CHANGELOG/README)

#### Scope

| File | Change |
|------|--------|
| `internal/observability/redact/redact.go` | **New** — `Redactor` interface; `NoopRedactor` default. Same shape as the Python `Redactor` Protocol from PR 1. |
| `internal/observability/zapenc/encoder.go` | **New** — zap `Encoder` wrapper that: emits `service.kind` / `service.instance` / optional `service.role`; emits `schema_version: "1"`; renames legacy zap field keys to schema names; reserves slots for `trace_id` / `span_id` (populated in PR 3); calls the registered `Redactor` on each entry; emits `source` (file/line/function) using zap's `AddCaller` + a custom `EncodeCaller`. <!-- Package name `zapenc` (not `zapcore`) avoids collision with upstream `go.uber.org/zap/zapcore` — would force every importer into aliased imports. Pinned by RFC 0018 § "Files Touched (Estimated)" per PR #160 review. --> |
| `internal/observability/zapenc/encoder_test.go` | **New** — table-driven: every legacy field key renames to its schema name; `service.*` group present; `schema_version` present; field ordering matches schema doc; redactor invoked exactly once per entry; `source` contains valid file/line/func. |
| `cmd/orchestrator/main.go` | Construct the zap logger with the new encoder wrapper; register `NoopRedactor`; honour `PERSATRIX_LOG_FORMAT=pretty` (selects zap dev console encoder). |
| Audit script-assisted | Find every `zap.String("execution_id", …)` / `zap.String("workflow_id", …)` / etc. across `internal/` and `cmd/` and rename to the schema names. The encoder wrapper provides a backstop, but call-site renames keep the code self-documenting. |
| `CHANGELOG.md` | Under v0.2.3, add a "Renamed zap field keys" table mapping every old key to its new schema name. |
| `README.md` | Quick-start: add `PERSATRIX_LOG_FORMAT=pretty` example for local development. |

#### Key implementation details

- Field ordering in the encoder follows the schema doc emission order verbatim (PR 1's `docs/observability.md` is the single source of truth). Tests assert this byte-for-byte.
- The redactor is invoked on every entry, regardless of fields. `NoopRedactor` returns the entry unchanged.
- `pretty` mode uses zap's built-in `NewDevelopmentEncoderConfig()` with caller info and colour; it is **not** wrapped by the schema encoder (pretty is a developer affordance, not a wire format).
- Compatibility shim: none. RFC 0018 § Resolved Decisions #3 commits to a clean break + CHANGELOG entry.
- Backwards compatibility: pre-1.0; field names are not in any public contract (no proto, no schema, no spec doc references them today).

#### Tests

- Encoder produces JSON containing `schema_version`, `service.kind`, `service.instance`, `source` for every entry.
- Every legacy key listed in the CHANGELOG table renames to its new schema name (one test per key, table-driven).
- Field emission order matches the schema doc (deterministic JSON serialisation; assert exact byte sequence on a fixed entry).
- Redactor invocation: spy `Redactor` increments a counter; one entry → one increment.
- `PERSATRIX_LOG_FORMAT=pretty` selects the dev encoder and disables the schema encoder (assert by writing a log entry and parsing the emitted bytes — JSON in default mode, ANSI-coloured key-value in pretty mode).
- `source` field carries the test file's path and a non-zero line number.

#### PR checklist

- [x] `go test ./internal/observability/zapenc/... -v -race` passes
- [x] `go test ./internal/... -v -race -cover` passes (no regressions in existing tests)
- [x] `golangci-lint run` clean
- [x] CHANGELOG v0.2.3 entry includes the full old→new field rename table
- [x] README quick-start mentions `PERSATRIX_LOG_FORMAT=pretty`
- [x] No `internal/observability/zapcore/` directory (collision-avoidance pin from PR #160 review)

**Merged**: PR [#165](https://github.com/mkhomutov/Persatrix/pull/165) — 2026-04-23

---

### PR 3: `feature/v023-logging-cross-process-correlation` — Phase 3: Correlation IDs + OTEL Trace IDs on Logs

**Joint order position**: #5 (after 0019 PR 2).
**Depends on**: PR 2 merged (Go encoder has the trace-ID slots) **and** 0019 PR 2 merged (semantic spans exist on the agent side, so the enricher has spans to read).
**Branch**: `feature/v023-logging-cross-process-correlation`
**Estimated size**: ~250–400 lines (implementation + integration test)

#### Scope

| File | Change |
|------|--------|
| `internal/executor/dispatch.go` | Inject `execution_id` / `step_id` / `agent_id` / `workflow_id` into outgoing gRPC metadata using a new helper from `internal/observability/grpcmeta/` (`InjectIDs(ctx, md)` / `ExtractIDs(md) → ids` pair). The helper is pinned now, not deferred to PR-time, because the same logic is called from two sites (`internal/executor/dispatch.go` and `internal/executor/chat.go`) — inlining would duplicate the metadata-key constants and the pre-condition checks. <!-- Pinned per PR #161 review: open-at-plan-time hedge removed in line with the RFC 0017 PR 5 precedent this plan cites elsewhere; helper is the better choice because it is the only de-duplication point for the four metadata-key constants shared with the Python-side interceptor. --> |
| `internal/executor/chat.go` | Same injection on the chat-message dispatch path added by RFC 0016. |
| `agents/observability/grpc_logging.py` | **New** — server-side gRPC interceptor that reads the four metadata keys and binds them to structlog's contextvars for the duration of the handler. Registered in `agents/server.py` **after** `GrpcInstrumentorServer` from RFC 0019 (otel interceptor first, so the OTEL context is established before the logging interceptor reads it). |
| `agents/server.py` | Wire `LoggingMetadataInterceptor` into the gRPC server's interceptor chain. |
| `internal/observability/zapenc/encoder.go` | Populate the `trace_id` / `span_id` slots reserved in PR 2 by reading `trace.SpanContextFromContext(entry.Context)`. The encoder wrapper now requires entries to carry a `context.Context` — call sites that do not yet pass one fall back to omitting the IDs (no panic). |
| `agents/observability/logging.py` | The OTEL processor placeholder added in PR 1 becomes load-bearing: read `trace.get_current_span().get_span_context()` and inject `trace_id` / `span_id` when valid. |
| `tests/integration/test_logs_correlation.py` | **New** — submit a workflow, capture orchestrator + agent log lines for the run, assert every line carries matching `execution_id`, `step_id`, `workflow_id`, `agent_id`, `trace_id`, `span_id` (where a span is active). Includes a "no active span" case where IDs are present but trace fields are omitted. |

#### Key implementation details

- Metadata keys follow the kebab-case convention defined in [RFC § D](0018-structured-logging-framework.md#d-cross-process-correlation-ids): `persatrix-execution-id`, `persatrix-step-id`, `persatrix-agent-id`. The `x-` prefix is **not used** — the `x-` convention was deprecated for HTTP headers (RFC 6648) and adds no value for gRPC metadata. RFC § D explicitly documents the rationale: the `persatrix-` form is lowercase-clean per gRPC spec and matches existing header conventions in the codebase. The interceptor strips the `persatrix-` prefix when binding to structlog contextvars. <!-- Corrected per PR #161 review: the earlier `x-persatrix-` form would silently break correlation (Go injects without `x-`; Python would extract with `x-` — no match). -->
- Order matters at server registration: `GrpcInstrumentorServer` (from RFC 0019 PR 1) → `LoggingMetadataInterceptor` (this PR). The OTEL interceptor establishes the trace context; the logging interceptor binds correlation IDs to that already-established context.
- `trace_id` and `span_id` are emitted in the schema's Optional-fields block defined in [RFC § B](0018-structured-logging-framework.md#b-common-log-schema). When no span is active they are omitted (not emitted as empty strings) — preserves the schema's "Optional" contract.
- The integration test runs against an in-process orchestrator + a single agent; no external services. Asserts via captured log streams (stdout) parsed as JSON.

#### Tests

- Unit (Go): encoder emits `trace_id` / `span_id` when entry context carries a valid SpanContext; omits them otherwise.
- Unit (Python): structlog OTEL processor adds the two fields when a span is active; absent fields when no span.
- Unit (Python): `LoggingMetadataInterceptor` extracts all four metadata keys; missing headers don't crash; contextvars cleared after handler returns (assert by emitting a log line outside the handler context post-call and verifying the IDs are absent).
- Integration: submit a workflow → all log lines for the run carry the four correlation IDs; agent-side spans inherit the orchestrator's `trace_id`.

#### PR checklist

- [x] `pytest agents/tests/test_grpc_logging_interceptor.py -v` passes
- [x] `pytest tests/integration/test_logs_correlation.py -v` passes
- [x] `go test ./internal/observability/zapenc/... -v -race` passes
- [x] `ruff check agents/` clean; `mypy agents/` clean
- [x] `golangci-lint run` clean
- [x] Interceptor registration order verified: OTEL (0019) → logging (this PR)
- [x] `trace_id` / `span_id` are omitted (not empty) when no span is active

**Merged**: PR [#168](https://github.com/mkhomutov/Persatrix/pull/168) — 2026-04-23

---

### PR 4: `feature/v023-logbuffer-proto-and-store` — Phase 4a: `proto/log_service.proto` + Ring Buffer + Disk Store

**Joint order position**: #8 (after 0019 PR 4).
**Depends on**: PR 3 merged. Independent of 0019 Phase 3 in code, but ordered after it so the v0.2.3 metrics/Collector pipeline lands first and operators have a complete observability trio before the CLI delivery PRs (5 + 6) start to ship operator-visible UX.
**Branch**: `feature/v023-logbuffer-proto-and-store`
**Estimated size**: ~400–500 lines (proto + Go package + tests)

#### Scope

| File | Change |
|------|--------|
| `proto/log_service.proto` | **New** — defines `LogService.StreamLogs(stream LogBatch) returns (stream LogAck)` per [RFC § E](0018-structured-logging-framework.md#e-persatrix-logs-endpoint-storage-and-streaming). `LogBatch` wraps `repeated LogEntry entries` + `agent_id` (the batch-level `agent_id` avoids per-entry repetition when all entries in a batch share the same agent, which is the common shipper case). `LogEntry` carries the schema's required + optional fields; `LogAck` advances the agent shipper's high-water mark. <!-- Corrected per PR #161 review: the plan previously said `StreamLogs(stream LogEntry)` while RFC § E defines `StreamLogs(stream LogBatch)`; citing "per RFC § E" while contradicting it was a spec drift hazard. --> |
| `internal/generated/log_service*.go`, `agents/generated/log_service*.py` | Regenerated stubs. |
| `internal/observability/logbuffer/buffer.go` | **New** — per-execution ring buffer (default 1000 entries) keyed by `execution_id`; LRU eviction across executions (default 50 executions); seal-on-terminal (when execution completes/fails, ring is sealed and protected from eviction until disk-flushed). |
| `internal/observability/logbuffer/disk.go` | **New** — append-only on-disk store under `PERSATRIX_LOGBUFFER_DIR` (default `data/logs/`) with a 512 MB cap (`PERSATRIX_LOGBUFFER_DISK_MB`); umask `0700` on directory creation per RFC § E. Warm-load on orchestrator start. |
| `internal/observability/logbuffer/ratelimit.go` | **New** — token-bucket rate limiter per execution (`PERSATRIX_LOGBUFFER_RATE_PER_EXEC`, default 1000 entries/sec); `PERSATRIX_LOGBUFFER_DROP_LEVEL` controls which severities are droppable. Drops are counted and surfaced via a future metric (RFC 0019). |
| `internal/observability/logbuffer/buffer_test.go`, `disk_test.go`, `ratelimit_test.go` | **New** — unit tests for capacity, LRU eviction, durability + warm-load, sealing, rate limiting, concurrency. |

#### Key implementation details

- Env-var knobs (defaults pinned in [RFC § Resolved Decisions #2](0018-structured-logging-framework.md#resolved-decisions)): `PERSATRIX_LOGBUFFER_PER_EXEC=1000`, `PERSATRIX_LOGBUFFER_MAX_EXEC=50`, `PERSATRIX_LOGBUFFER_DISK_MB=512`, `PERSATRIX_LOGBUFFER_DIR=data/logs`, `PERSATRIX_LOGBUFFER_DROP_LEVEL=DEBUG`, `PERSATRIX_LOGBUFFER_RATE_PER_EXEC=1000`.
- Disk store layout: `<DIR>/<execution_id>/<sequence>.jsonl` (directory per execution, monotonically-increasing sequence files, one JSON entry per line). **This supersedes the flat single-file layout described in [RFC § E](0018-structured-logging-framework.md#e-persatrix-logs-endpoint-storage-and-streaming)** (`data/logs/<execution_id>.jsonl`): the per-sequence-file approach keeps individual file sizes bounded for long-running executions and allows sealed-ring flush-in-full without in-place JSONL rewrites. RFC § E is the authority for the `LogService` RPC shape and env-var knobs; the disk layout is an implementation detail that this PR plan supersedes. Sealed rings are flushed in full before the in-memory ring is freed. <!-- Note added per PR #161 review to prevent implementers from conflicting against RFC § E's flat-file layout. -->
- LRU eviction never evicts a sealed ring with un-flushed entries (eviction is a no-op for such rings; oldest active ring is evicted instead).
- Concurrency: per-execution ring uses an internal `sync.Mutex`; cross-execution LRU uses a top-level `sync.RWMutex`. Tests run with `-race`.

#### Tests

- Per-execution capacity respected: 1001st entry evicts the oldest in the same execution's ring.
- LRU across executions: 51st execution evicts the least-recently-used.
- Disk persistence: write entries, restart (simulated by rebuilding the buffer from disk), assert all entries queryable.
- Rate limit: 2× `PERSATRIX_LOGBUFFER_RATE_PER_EXEC` entries/sec → ~half are dropped; drop counter incremented; severity ≥ `WARN` always admitted regardless of rate.
- Sealing: write entries, mark execution terminal, attempt eviction → sealed ring not evicted until disk-flushed.
- Warm-load resilience: simulate a truncated final JSONL line in a pre-existing execution file on disk → ring loads all well-formed entries successfully; exactly one `WARN` log line emitted per affected file; subsequent entries written and read back without error. <!-- Added per PR #161 review: the Risk/Mitigation table documents this behaviour but without a test assertion it is unverified. -->
- Concurrent writes: 10 goroutines × 100 entries each → no panics under `-race`; final entry count consistent with rate-limit drops.

#### PR checklist

- [x] `go test ./internal/observability/logbuffer/... -v -race -cover` passes
- [x] `make proto` regenerates Go + Python stubs without diffs left over
- [x] `proto/log_service.proto` matches [RFC § E](0018-structured-logging-framework.md#e-persatrix-logs-endpoint-storage-and-streaming)
- [ ] All six env-var knobs documented in `docs/observability.md` (operations section appended in PR 6)
- [x] Default `PERSATRIX_LOGBUFFER_DIR=data/logs` is created with `0700` umask

---

### PR 5: `feature/v023-logbuffer-service-and-endpoints` — Phase 4b: `LogService` Server + Agent Shipper + REST + SSE

**Joint order position**: #9.
**Depends on**: PR 4 merged.
**Branch**: `feature/v023-logbuffer-service-and-endpoints`
**Estimated size**: ~400–500 lines (server + shipper + endpoints + tests)

#### Scope

| File | Change |
|------|--------|
| `internal/server/logs_service.go` | **New** — orchestrator-side `LogServiceServer` implementation; receives streamed entries, writes them to the per-execution ring + disk store; sends `LogAck` to advance the agent's high-water mark. Registered on the existing agent gRPC server in `cmd/orchestrator/main.go`. |
| `agents/observability/log_shipper.py` | **New** — background task that drains a bounded `asyncio.Queue` and streams entries via `LogService.StreamLogs`; bounded-queue overflow drops the **oldest** entry (FIFO drop) and increments a counter; reconnect-with-backoff on stream errors (exponential, capped at 30s); flushes on shutdown. The structlog chain's terminal processor enqueues to this shipper. |
| `agents/server.py` | Start `log_shipper` at startup; await flush on graceful shutdown. |
| `internal/server/logs_handler.go` | **New** — `GET /api/v1/executions/{id}/logs` with `since` (RFC 3339 or duration like `5m`) / `workflow` / `level` / `limit` query filters; `id=_` returns merged cross-execution view. |
| `internal/server/logs_stream_handler.go` | **New** — `GET /api/v1/executions/{id}/logs/stream` — SSE endpoint feeding the CLI's `--follow` mode. Subscriber cap returns HTTP 429 when exceeded; client disconnect cleans up the subscription. |
| `internal/server/server.go` | Register the two new handlers; remove `handleGetLogs` from `stub_handlers.go`. |
| `internal/server/stub_handlers.go` | Remove `handleGetLogs`. |
| `internal/server/logs_handler_test.go`, `logs_stream_handler_test.go` | **New** — endpoint tests including 400-on-invalid-execution-id, filter behaviour, SSE framing, subscriber cap. |
| `agents/tests/test_log_shipper.py` | **New** — bounded queue overflow drops oldest + increments counter; reconnect-with-backoff on stream errors; flush on shutdown. |

#### Key implementation details

- The agent shipper uses `LogAck.high_water_mark` to free already-acked entries from its in-memory queue. On orchestrator restart, the shipper reconnects, the orchestrator replays from disk, and the high-water mark resumes from there.
- SSE framing follows the standard `data: <json>\n\n` form; heartbeats every 15s to keep proxies from killing idle connections.
- `since=5m` is parsed as a duration relative to the request time; `since=2026-04-22T12:00:00Z` is parsed as RFC 3339. Invalid `since` returns 400.
- `id=_` performs a chronological merge across all rings using the entries' timestamps; `limit` is enforced on the merged stream.
- The handlers do not authenticate today; auth lands in the future RFC 0009 work referenced in [RFC 0018 Related Documentation](0018-structured-logging-framework.md#related-documentation). A `// TODO(RFC-0009): authenticate` comment marks the gate.

#### Tests

- Empty execution → returns `[]` with HTTP 200.
- `since` / `workflow` / `level` filters applied correctly (one test per filter).
- `id=_` returns merged chronological view with `limit` enforced post-merge.
- Invalid `execution_id` (non-UUID where required) → HTTP 400.
- SSE: subscribe → emit one entry from the ring → assert `data:` frame received.
- SSE: subscriber cap exceeded → HTTP 429.
- SSE: client disconnect → subscription removed (assert via internal subscriber count).
- Shipper: queue overflow at `MAX_QUEUE` → oldest entries dropped; counter increments by exact overflow count.
- Shipper: stream error → reconnect with exponential backoff (assert via mocked stream).
- Shipper: graceful shutdown flushes the queue.

#### PR checklist

- [ ] `go test ./internal/server/... -v -race` passes
- [ ] `pytest agents/tests/test_log_shipper.py -v` passes
- [ ] `handleGetLogs` stub removed from `internal/server/stub_handlers.go`
- [ ] `LogService` registered in `cmd/orchestrator/main.go` on the existing agent gRPC server
- [ ] `// TODO(RFC-0009): authenticate` markers in place on both new HTTP handlers

---

### PR 6: `feature/v023-cli-logs-rewrite` — Phase 4c: CLI Rewrite + E2E

**Joint order position**: #10.
**Depends on**: PR 5 merged.
**Branch**: `feature/v023-cli-logs-rewrite`
**Estimated size**: ~300–450 lines (Rust rewrite + E2E test + ops doc append)

#### Scope

| File | Change |
|------|--------|
| `cli/src/commands/logs.rs` | Rewrite. Subcommand: `persatrix logs <execution_id>` with flags `--verbose`, `--follow`, `--since <dur-or-rfc3339>`, `--workflow <name>`, `--level <DEBUG\|INFO\|WARN\|ERROR>`, `--trace <trace_id>`. `--follow` consumes the SSE endpoint; non-`--follow` mode hits the REST endpoint and renders. `id=_` is a documented value (`persatrix logs _ --since 1h`). `--trace <trace_id>` filters to entries whose `trace_id` field matches, enabling the log↔trace correlation workflow described in [RFC 0019 § G](0019-opentelemetry-completion.md#g-logtrace-correlation). <!-- `--trace` added per PR #161 review: RFC 0019 § G and 0019 PR 4's E2E test both reference `persatrix logs --trace <trace_id>` as a first-class invocation; omitting it from the CLI rewrite scope would have left an undocumented gap. --> |
| `cli/Cargo.toml` | Add `eventsource-stream` (or equivalent SSE client crate) if not present. |
| `tests/integration/test_logs_e2e.py` | **New** — submit a small workflow; call `persatrix logs <id>` (via subprocess) → assert merged Go + Python entries, matching `execution_id`, valid `trace_id`. Run `persatrix logs --follow` against a long-running workflow → observe live entries within 2s. Restart the orchestrator mid-test → re-run `persatrix logs <id>` → assert pre-restart entries still present. |
| `docs/observability.md` | Append an "Operations" section: CLI usage, env-var knobs (the six from PR 4), on-disk store layout, the `data/logs/` umask note, and a `persatrix logs --follow` walkthrough. |

#### Key implementation details

- The CLI uses an exhaustive `match` on the new flag combinations (per Rust CLI convention in this repo).
- Display: default mode prints one line per entry with `<timestamp> <level> [<agent_id>] <message>`; `--verbose` adds `<execution_id>`, `<step_id>`, `<trace_id>`, and the full structured payload.
- `--follow` reconnects with backoff on connection loss and prints a single `[reconnected]` info line.
- The E2E test uses `make run` infrastructure to spin up the orchestrator + an agent in the test fixture; tears down on test exit.

#### Tests

- Unit (Rust): flag parsing — every combination of the four filter flags + `--follow` parses without conflict; invalid `--level` returns a parse error.
- Integration (Python E2E): the three scenarios above. Restart durability is asserted by stopping/starting the orchestrator subprocess between two `persatrix logs` invocations.

#### PR checklist

- [ ] `cargo test --manifest-path cli/Cargo.toml` passes
- [ ] `cargo clippy --manifest-path cli/Cargo.toml -- -D warnings` clean
- [ ] `pytest tests/integration/test_logs_e2e.py -v` passes
- [ ] `docs/observability.md` Operations section covers all six `PERSATRIX_LOGBUFFER_*` env vars and the `0700` umask
- [ ] Manual smoke documented in PR description: `make run`, `persatrix run <wf>`, `persatrix logs <id>`, `persatrix logs --follow <id>`, `persatrix logs --since 5m _`

---

### PR 7: `feature/v023-rfc0018-followups-close` — Review Follow-Ups + RFC Close

**Joint order position**: #11 (opened together with 0019 PR 5 as a paired closeout).
**Depends on**: PR 6 merged.
**Branch**: `feature/v023-rfc0018-followups-close`
**Estimated size**: ~150–350 lines (fixes + RFC status flip + ROADMAP + manual-test report append)

#### Scope

Per [.github/copilot-instructions.md](../../.github/copilot-instructions.md) ("PR review reports are local-only artifacts"), each follow-up entry must paraphrase the finding and **not** reference or link any `docs/pr-reviews/*.md` file. Items below are populated as PRs land and reviews complete.

<!-- Empty subsections below are intentional placeholders. Each is populated when the corresponding PR's review completes. The `<!-- TODO: populate after PR N review merges -->` markers below are added per PR #161 review so `git grep "TODO: populate after"` lists outstanding follow-up captures at any point. -->

##### From PR 1 review

<!-- TODO: populate after PR 1 review merges -->
*(populated during PR 1 review)*

##### From PR 2 review

- **Enforce required `Options.ServiceKind` / `ServiceInstance` at construction.** `internal/observability/zapenc.NewEncoder` documents both as required but accepts zero-value `Options`, silently emitting `"service.kind":""` / `"service.instance":""` lines that violate the schema's required-field group. Add a `Must`-style constructor (or panic) so misuse is caught at startup rather than on the first emitted line, and add a unit test covering both empty-string cases.
- **Strengthen the last-ditch fallback envelope to carry the full required-field group.** `encoder.encodeFallbackEnvelope`'s hand-crafted JSON literal (the branch reached only when `serialiseOrdered` itself fails) emits `schema_version` / `level` / `message` only, omitting `service.kind` and `service.instance`. Include all six required fields in the literal so the "every line carries the required-field group" invariant holds even on double failure, and add a test that forces this branch.
- **Lock in the reserved-key shadowing contract with a test.** Today the encoder injects `schema_version` / `service.*` / `source` after the inner JSON encoder runs, so a future call site emitting `zap.String("schema_version", "evil")` is silently overwritten — but the invariant is unasserted. Add an `encoder_test.go` case that emits each `fieldOrder` reserved key as a user field and asserts the wire line still carries the encoder-injected value.
- **Document Redactor mutate-then-panic semantics in the trust contract.** `applyRedactor`'s panic-recovery path returns `out = entry`, which shares the underlying map reference. A future real `Redactor` that mutates in place and then panics will leak partial mutations to the wire. Add one sentence to the `Redactor` interface doc comment requiring all mutations to complete before any operation that may panic, or shallow-copy `entry` before invoking.
- **Warn when `PERSATRIX_LOG_FORMAT=pretty` is combined with `--env=production`.** `cmd/orchestrator/main.go` `buildLogger` returns `zap.NewDevelopment()` for the pretty path, bypassing the encoder *and* the redactor and ignoring the production level filter. Pretty mode is documented as dev-only; emit a one-line stderr warning at startup when the combination is detected so an accidental production deployment is loud.
- **Benchmark `EncodeEntry` before v0.2.3 release.** The doc comment promises a benchmark of the deliberate `json.Unmarshal` → mutate → `json.Marshal` round-trip per log line, with the streaming-encoder alternative as the escape valve. Add `BenchmarkEncodeEntry` (representative entry shape, 100k iterations) and capture the baseline so the streaming-encoder follow-up has a target to beat.
- **Guard `legacyRenames` deterministic iteration with a fuzz/property test.** `TestEncoder_LegacyRenameCollisionIsDeterministic` covers the documented `executionID` > `runID` precedence over 50 runs; add a property-style test that permutes the rename map's input order and asserts the wire output is byte-identical, so a regression that re-introduces non-deterministic map iteration fails fast.
- **Move `stderrSink` / `jsonUnmarshal` package-level mutable globals behind an atomic or `_test.go`-only override.** Today both are safe because no test in `zapenc/` uses `t.Parallel()`; the package comment makes this explicit. A future test file in the same package using `t.Parallel()` would race against production `EncodeEntry` callers. Either build-tag the test-only writes into `*_test.go` files, expose them through a `sync/atomic.Value`, or add a CI lint that fails on `t.Parallel()` inside `internal/observability/zapenc/`.

##### From PR 3 review

- **Resolve `InjectIDs` "last-write-wins" doc vs `firstOrEmpty` first-wins behavior.** `internal/observability/grpcmeta.InjectIDs` documents that re-injection replaces prior values, but `metadata.AppendToOutgoingContext` actually appends and `ExtractIDs` → `firstOrEmpty` returns `vals[0]` (first-wins). No caller re-injects today, so the divergence is latent — but a future middleware that overrides (say) `WorkflowID` after a retry would silently see the original value win. Pick one semantic and pin it: either align the doc to "first-write-wins", or have `InjectIDs` deduplicate (filter prior keys from `metadata.FromOutgoingContextRaw` before re-appending), or have `ExtractIDs` use `vals[len-1]`. Add a regression test (`TestInjectIDs_*WriteWins`) so the choice can't drift.
- **Tighten `_unbind` to avoid stripping pre-bound contextvars.** `agents/observability/grpc_logging._unbind` calls `structlog.contextvars.unbind_contextvars(*bound.keys())` which fully removes the key — if anything outside the RPC scope had pre-bound (say) `agent_id` (a future `configure_logging(agent_id=...)` extension or a test fixture), the interceptor would silently strip it on every RPC return. Today only `service.kind` / `service.instance` are pre-bound and they aren't in the four-key set, so the behavior is benign. Capture prior values via `structlog.contextvars.get_contextvars()` before bind and restore them in `_unbind`, or at minimum add a docstring caveat: "callers must not pre-bind any of the four `_METADATA_TO_CONTEXTVAR` values".
- **Fix the unknown-handler-shape comment/code mismatch.** `agents/observability/grpc_logging._wrap_handler`'s fallback branch comment promises "use stderr as a last resort" but the code just `return handler` with no diagnostic. Either add the promised `print(..., file=sys.stderr)` line, or revise the comment to "we silently bypass an unrecognized shape; failure mode is missing correlation IDs, not an exception".
- **Document `LoggerWithContext(ctx, nil)` contract.** `internal/observability/zapenc/context.LoggerWithContext` returns `nil` when logger is nil (the caller's next `.Info()` panics), while the nil-ctx branch returns the original logger by design. The asymmetric nil-handling is acceptable but undocumented — add a docstring line: "`logger` must be non-nil at production call sites; the nil return is a contract for tests only".
- **Align `runID` local with the `ExecutionID` field name in stage_runner.** `internal/scheduler/stage_runner.go` populates `ExecutionID: runID` — pick one convention (rename the local to `executionID`, or rename the field to `RunID`).
- **Add a cross-language drift test for `_METADATA_TO_CONTEXTVAR` ↔ `grpcmeta.MD*` keys.** Today the four metadata keys are kept in sync only by comment ("The Go side defines the same constants in `internal/observability/grpcmeta`"). A drift test (e.g. parse the Go source and assert the four keys match the Python dict) would prevent silent skew when a fifth ID is added later in the RFC. Worth a follow-up issue, not necessarily this RFC's close PR.
- **Reduce `tests/integration/test_logs_correlation.py` coupling to module internals.** `_reset_logging_state` reaches into `logging_mod._configured` and `logging_mod._redactor` directly. Acceptable as the only practical full-reset path today; a `reset_for_tests()` public helper in `agents/observability/logging.py` would be cleaner and survive future internal renames.
- **Clarify the `_METADATA_TO_CONTEXTVAR` underscore-vs-`__all__` contradiction.** The dict is module-private (underscore prefix) but exported in `__all__` for tests. Either rename to `METADATA_TO_CONTEXTVAR` (public) or drop from `__all__` and have the test import it through the module namespace.

##### From PR 4 review

<!-- TODO: populate after PR 4 review merges -->
*(populated during PR 4 review)*

##### From PR 5 review

<!-- TODO: populate after PR 5 review merges -->
*(populated during PR 5 review)*

##### From PR 6 review

<!-- TODO: populate after PR 6 review merges -->
*(populated during PR 6 review)*

##### RFC close

- Flip [RFC 0018 status](0018-structured-logging-framework.md) → `✅ Implemented`; record merged-PR list.
- Update [ROADMAP.md](../../ROADMAP.md) RFC tracker row (0018) → `✅ Implemented`; ensure the v0.2.3 milestone row reflects the joint observability delivery alongside RFC 0019's close (see [0019-pr-plan.md](0019-pr-plan.md) PR 5).
- Add v0.2.3 manual-test report row(s) in `docs/manual-tests/` for any logging-specific manual checks (CLI `--follow`, restart durability, `pretty` mode toggle).
- Confirm [PR #161](https://github.com/mkhomutov/Persatrix/pull/161) (the RFC 0018 + 0019 PR plan document PR) appears in the [ROADMAP.md](../../ROADMAP.md) merged-PR table. <!-- Added per PR #161 review: development-workflow.md "Status Hygiene" requires every merged PR to appear in the ROADMAP table; the plan PR itself is subject to the same rule. -->

#### PR checklist

- [ ] All review follow-ups from PRs 1–6 addressed or explicitly deferred (with rationale)
- [ ] [RFC 0018 status](0018-structured-logging-framework.md) → `✅ Implemented`
- [ ] [ROADMAP.md](../../ROADMAP.md) RFC tracker row updated; v0.2.3 milestone row reflects logging-side close
- [ ] PR #161 appears in ROADMAP merged-PR table
- [ ] Manual-test report appended for v0.2.3 logging coverage
- [ ] `make test` passes; `make lint` clean

---

## Risk and Mitigations

| Risk | Mitigation |
|------|------------|
| zap field-rename PR (PR 2) breaks downstream log consumers | Pre-1.0; CHANGELOG entry with old→new table; no compatibility shim per [RFC § Resolved Decisions #3](0018-structured-logging-framework.md#resolved-decisions). |
| Cross-process correlation interceptor order regresses (PR 3) | Test asserts OTEL interceptor registers before logging interceptor; order is verified at startup. |
| Disk store corruption on crash (PR 4) | Append-only JSONL; warm-load skips malformed lines with a single startup warning per file. Unit test simulates a truncated final line. |
| Shipper queue saturation (PR 5) | Bounded queue; oldest-drop policy + counter; reconnect-with-backoff; flush on shutdown. |
| `--follow` SSE proxy timeouts (PR 6) | 15s heartbeat keepalive; CLI reconnects with backoff and prints a single `[reconnected]` line. |

---

## ROADMAP Hygiene

Per [.github/copilot-instructions.md](../../.github/copilot-instructions.md) "Status Hygiene":

- **PR 1 opens** → flip [ROADMAP.md](../../ROADMAP.md) RFC 0018 row to `🚧 Implementing`.
- **Each PR merges** → update the merged-PR table in ROADMAP and tick the corresponding checklist line in this plan.
- **PR 7 merges** → flip RFC 0018 row to `✅ Implemented`; ensure v0.2.3 milestone row reflects logging-side close (full v0.2.3 close depends on RFC 0019 PR 5 also merging — see [0019-pr-plan.md](0019-pr-plan.md)).
