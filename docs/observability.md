# Persatrix Observability — Log Schema

> **Owner**: [RFC 0018 — Structured Logging Framework](rfcs/0018-structured-logging-framework.md)
> **Companion**: [RFC 0019 — OpenTelemetry Completion](rfcs/0019-opentelemetry-completion.md) (will append span / metric sections in its Phase 2 + 3 PRs)
> **Status**: 🚧 In progress (RFC 0018 PR 1 — Phase 1)
> **Schema version**: `1`

This document is the **single source of truth** for the Persatrix structured-log
schema. Both the Go orchestrator (`go.uber.org/zap`) and the Python agents
(`structlog`) emit records conforming to this schema. Future RFCs append; they
never overwrite a published field. Breaking changes (removal, rename, type or
semantic change) bump `schema_version` to `"2"` and are called out in
`CHANGELOG.md`.

See the RFC for design rationale; this doc is the operational reference.

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

Future RFCs append sections here (single-source-of-truth discipline);
never overwrite a section owned by another PR.

| Section | Owning RFC + PR |
|---------|-----------------|
| Span semantic conventions (`persatrix.*` attribute namespace) | RFC 0019 PR 2 (✅ § 10 below) |
| Metric inventory + dimensions | RFC 0019 PR 3 |
| Operator pipeline (Collector + tail sampling + Jaeger / Prometheus / Loki) | RFC 0019 PR 4 (✅ § 11 below) |
| Persisted log layout (`data/logs/<execution_id>/...`) + env knobs | RFC 0018 PR 4 |
| `LogService` gRPC + REST + SSE endpoint shapes | RFC 0018 PR 5 |
| `persatrix logs` CLI flags + colour scheme | RFC 0018 PR 6 |

---

## 10. Span conventions (RFC 0019 PR 2)

This section is the operational reference for trace spans emitted by the
Python agent runtime. The naming and attribute conventions come from
[RFC 0019 § D](rfcs/0019-opentelemetry-completion.md#d-semantic-spans-on-the-python-side)
and [§ E](rfcs/0019-opentelemetry-completion.md#e-span-naming-and-attribute-conventions).

### 10.1 Naming

`<service>.<component>.<operation>` — lowercase, dot-separated, no plurals.
`agent.*` is the Python runtime; `orchestrator.*` is the Go orchestrator.

**Cross-process exception — `channel.*`.** Spans on the channels publish
path (`channel.publish` on the Python publisher and `channel.dispatch` on
the Go dispatcher) deliberately drop the service prefix so both halves of
a single publish-then-fanout trace live in one query namespace. An
operator querying `name =~ "^channel\\."` finds the full publish path
without having to know which language runs which side; an operator
querying for a specific channel id pivots from either span via the shared
`channel.id` / `channel.message_id` attributes.

### 10.2 Span inventory

| Span name | Emitted from | Key attributes |
|-----------|--------------|----------------|
| `agent.persona.event` | `_LLMPersonaAgent.on_event()` | `agent.id`, `event.type`, `event.id`. Sub-millisecond phases recorded as **span events** (`received` → `queued` → `handled` → `completed`), not nested spans. |
| `agent.persona.tick` | `_LLMPersonaAgent.on_tick()` | `agent.id`, `tick.reason`. Carries `Link(link.kind="trigger")` back to the event span when an event woke the tick scheduler (RFC 0019 § I). |
| `agent.memory.episodic.recall` | `EpisodicMemory.recall()` | `agent.id`, `query.kind`, `query.empty`, `min_score`, `result.count` |
| `agent.memory.episodic.remember` | `EpisodicMemory.store_episode()` | `agent.id`, `episode.kind` |
| `agent.memory.relationship.lookup` | `RelationshipMemory.get_trust()` | `agent.id`, `participant.id` |
| `agent.memory.relationship.update` | `RelationshipMemory.update_trust()` | `agent.id`, `participant.id`, `delta.kind`, `delta.value`, `trust.new` |
| `agent.llm.call` | `LLMClient.create_message()` | OTEL **Gen-AI semantic conventions**: `gen_ai.system`, `gen_ai.request.model`, `gen_ai.operation.name`, `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`, `gen_ai.response.finish_reasons`. Plus `persatrix.lease_id` when the call acquired an RFC 0023 wallet lease, and `persatrix.llm.model_alias` when the model came in via a model alias (both § 10.5). |
| `agent.tool.execute` | `@tool` decorator wrapper in `tools/registry.py` | `tool.name`, `tool.success` (+ optional payload — see § 10.4) |
| `agent.subagent.spawn` | `ActionExecutor` SPAWN_SUB_AGENT case (stub for RFC 0009) | `agent.id`, `subagent.role`, `subagent.status`. The sub-agent's root span will emit `Link(link.kind="spawn")` back here when the spawner ships in RFC 0009. |
| `channel.publish` | `HTTPChannelPublisher.publish()` (Python REST publisher; ISSUE-0032) | `channel.id`, `channel.sender_id`, `channel.mentions_count`, `channel.message_id` (set from the orchestrator's 201 response). Joins the Go-side `channel.dispatch` span by `channel.message_id` for end-to-end publish-path traces. Status `UNSET` on the sticky `ChannelsDisabledError` (HTTP 503) branch — deployment signal, not an internal failure; mirrors the Go-side `channel.dispatch` discipline of leaving best-effort no-ops `Unset` so error-rate dashboards stay honest on channels-off runs. |

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

> **Operator warning — `full` + `NoopRedactor`.** Setting
> `PERSATRIX_TRACE_TOOL_PAYLOADS=full` with the default `NoopRedactor`
> writes raw tool arguments to spans; the runtime logs a one-time
> `WARNING` on first use. Install a real redactor via
> `set_redactor()` before enabling `full` in production.

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
| `persatrix.lease_id` | string | The server-issued ULID of the RFC 0023 wallet lease an LLM call holds. Set on the `agent.llm.call` span when the call was bracketed by a wallet lease (PR 3 wired workflow-task; PR 4 chat; PR 5 autonomous-TICK and sub-agent; PR 6 channel-message). Absent on un-leased calls **and on calls that were denied a lease** (the wallet refuses *before* the provider call, so no `agent.llm.call` span is emitted — see the prose block below). |
| `persatrix.llm.model_alias` | string | The logical [model alias](guides/model-providers.md) (`quality` / `fast` / `summarizer`) the call's model resolved from (RFC 0033 §G). Set on the `agent.llm.call` span **alongside** the physical `gen_ai.request.model`, never instead of it — so a dashboard can group spend by logical role while `gen_ai.request.model` still shows the vendor ID the alias resolved to. Telemetry-only: it is never forwarded to the provider. Omitted on the raw-vendor-ID fall-through path (which instead increments the `persatrix.llm.alias.raw_id_usage` counter — RFC 0033 §G / §I). |

> **RFC 0023 wallet leasing.** Every workflow-task LLM call acquires a
> server-issued lease from the orchestrator-side `WalletService` before
> issuing, and settles the provider-reported actual usage afterward
> (`docs/rfcs/0023-llm-call-leasing.md`). The `agent.llm.call` span
> carries `persatrix.lease_id` so a span can be joined to the wallet's
> lease-lifecycle logs (lease granted / settled / reaped — keyed on the
> same `lease_id`) and the `LeaseRequest.trace_id` the agent stamps from
> the active trace. A budget denial does not produce an `agent.llm.call`
> span at all — the lease is refused *before* the provider call, and the
> agent surfaces it as a structured failure (`error_type=budget_exceeded`
> on a workflow task). Wallet enforcement and the reaper run on the Go
> side; the orchestrator-side wallet metrics are covered by RFC 0023's
> server instrumentation.

> **Autonomous TICK idle reasons (RFC 0023 PR 5).** An autonomous
> persona TICK has no caller to render a budget denial to, so the
> action loop *short-circuits* a denied `cause=CAUSE_AUTONOMOUS_TICK`
> lease to `DO_NOTHING` instead of propagating
> `BudgetExceededError` (chat and workflow-task callers continue to
> see the error). To keep that suppression visible on dashboards, the
> Python agent emits the `agent.persona.tick.idle` counter on *every*
> TICK that returns `DO_NOTHING` via a known short-circuit, attributed
> by `idle_reason`:
>
> * `idle_reason=empty_context_tick` — the RFC 0017 §F empty-context
>   short-circuit fired (no memory admitted, no active goal, no
>   pending turn); no provider call was attempted.
> * `idle_reason=budget_denied` — the wallet refused the
>   `CAUSE_AUTONOMOUS_TICK` lease (or was unreachable, failing
>   closed); the provider was *not* contacted.
>
> The two reasons are disjoint by construction — the empty-context
> branch fires before lease acquisition. Filtering on
> `idle_reason=budget_denied` separates budget-throttled idle from
> organic quiet periods without joining traces. Sub-agent leases ride
> the `CAUSE_SUB_AGENT` cause attributed to the *parent* persona's
> `agent.id` so per-persona cost dashboards bill the originating
> persona for delegated work — the active-lease cap stays per-process
> per [RFC 0023 OQ §7](rfcs/0023-llm-call-leasing.md#open-questions).

`gen_ai.*` attributes use the upstream OTEL Gen-AI semantic-convention
namespace verbatim — no Persatrix-private renames — so vendor backends
render Persatrix LLM traces correctly out of the box. The
`gen_ai.response.finish_reasons` attribute emits the canonical OTEL
vocabulary (`stop` / `length` / `tool_calls` / `content_filter` /
`error`); Persatrix's internal `StopReason` enum is translated at the
span emission site (see `agents.observability.spans.STOP_REASON_TO_GEN_AI`).

> **Wake counters (RFC 0024 PR 3b).** The four `agent.wake.*` counters
> track every wake the `EventLoop` substrate observes, partitioned by
> the `wake.kind` attribute (`inbound` / `scheduled` / `salience` /
> `dropped`). PR 4's "bored persona" cost-regression CI gate asserts
> all four read zero over a 60-second observation window — that is the
> v0.3.3 "Idle Truly Idle" acceptance gate ([v0.3.3-plan
> Acceptance](v0.3.3-plan.md#acceptance-for-v033)).
>
> * `agent.wake.inbound{agent.id, wake.kind=inbound}` — every
>   `InboundEventWake` the supervisor dispatches (RPC, channel
>   message, chat). PR 4's channel-message dispatch is the dominant
>   producer once wired.
> * `agent.wake.scheduled{agent.id, wake.kind=scheduled, timer_id}` —
>   every `ScheduledWake` (the legacy `tick_interval_seconds` cadence
>   carries `timer_id="legacy_tick"`; `autonomy.timers` entries carry
>   their configured id).
> * `agent.wake.salience{agent.id, wake.kind=salience, tier,
>   suppressed_reason}` — every `MemoryWriteEvent` this agent's
>   subscriber observes. The `suppressed_reason` attribute is the
>   dashboard discriminator: the three suppression branches
>   (`below_threshold`, `loopback`, `rate_limit`) plus the admit
>   branch's substrate result — `none` (enqueued) or `queue_full`
>   (admitted by salience policy but the queue was full, so also
>   counted on `agent.wake.dropped`). `none` alone is the true-enqueue
>   count. Without this attribute "no salience wakes" is
>   indistinguishable from "wakes are working and the agent is quiet"
>   — every same-agent write increments exactly one data point so a
>   dashboard can attribute every write to one of these outcomes.
> * `agent.wake.dropped{agent.id, wake.kind=dropped}` — incremented
>   when `EventLoop.enqueue` rejects a wake because the queue is full
>   (discard-not-block per RFC 0024 Decided §1). The substrate's
>   `EventLoop.dropped_count` and this OTEL counter agree by
>   construction.
>
> Two configuration knobs control the salience subscriber, both on the
> `autonomy` block (`schemas/agent.schema.json`):
>
> * `autonomy.salience_threshold` (default `0.95`) — strict `>`
>   comparison; a write at exactly the threshold is suppressed. The
>   default is strictly above PR 3a's conservative-scoring maximum
>   (`REFLECTION_CONTRADICTION_SALIENCE = 0.6` in
>   `agents/memory/_salience.py`) so salience wakes stay off by
>   inequality under stock scoring. The calibration follow-up named in
>   [v0.3.x sequencing §OQ §3](v0.3.x-sequencing.md#open-questions)
>   flips this default after a salience-distribution data sample
>   exists.
> * `autonomy.salience_rate_max_per_sec` (default `10`) — the rolling
>   1-second cap on `SalienceWake` enqueues per agent. DoS guard per
>   RFC 0024 §Security Considerations.

> **Span-vs-metric key divergence (by design).** Spans use the
> `persatrix.workflow_id` Baggage key (round-trips via the Baggage
> propagator); metrics use bare `workflow.id` (matches OTEL semconv
> attribute style expected by Prometheus / OTLP dashboards). Query
> `workflow.id` in metric backends. The RFC 0019 PR 4 schema-parity
> test enumerates documented divergences so renames cannot silently
> break either signal.

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

### 10.7 Wallet lease lifecycle (RFC 0023)

The orchestrator-side `WalletService` is the cost gate every LLM call
passes through. The operator-visible surface is one config block, one
failure surface, and three lifecycle log messages keyed on a stable
`lease_id`.

**Configuration — `wallet:` block in [`config/optimization.yaml`](../config/optimization.yaml).**
Top-level, sibling of `cost:`. An absent block (or absent key) falls
back to the defaults below; no operator action is required.

| Key | Default | What it controls |
|-----|--------:|------------------|
| `wallet.ttl_seconds` | `60` | Lease time-to-live. The reaper settles a lease still unsettled this long after issue at its granted (worst-case) amount, so an agent crash mid-call neither leaks a provisional hold nor frees spend. Default is 2× the 30 s per-call timeout ([RFC 0023 OQ §2](rfcs/0023-llm-call-leasing.md#open-questions)). |
| `wallet.reaper_interval_seconds` | `5` | How often the reaper scans for expired leases. |
| `wallet.max_active_leases` | `16` | Per-agent cap on concurrently-held (unsettled) leases — a DoS ceiling ([RFC 0023 Security Considerations](rfcs/0023-llm-call-leasing.md#security-considerations)), keyed on the lease-issuing agent. |

**Failure surface — `BudgetExceededError`.** Raised by the Python
`WalletClient` when `WalletService.AcquireLease` returns
`LeaseResponse.Denied`. Five origins translate it to an
operator-visible outcome:

| Origin | Operator-visible surface on lease denial |
|--------|------------------------------------------|
| Chat (`SendChatMessage`) | HTTP 200 with `reply_status="error"` and `error_message=<LeaseDenied.message>`. Same envelope on the channel reply for the channel-event chat path. |
| Autonomous TICK | Action loop short-circuits to `DO_NOTHING`; `agent.persona.tick.idle{idle_reason="budget_denied"}` counter increments. No `agent.llm.call` span is emitted. |
| Workflow task | Step fails with `error_type=budget_exceeded`; the orchestrator surfaces it through the workflow-run APIs and OTEL spans. |
| Sub-agent spawn | Spawn aborts; the parent persona observes the failure on its turn. |
| Channel-message reply | Reply suppressed; the orchestrator publishes a chat-error envelope on the channel under the parent persona's `agent.id` ([ISSUE-0065](issues/ISSUE-0065-chat-rest-budget-denied-no-channel-reply.md), [ISSUE-0066](issues/ISSUE-0066-chat-rest-resource-exhausted-no-channel-reply.md)). |

**Lifecycle log messages — finalised by [RFC 0023 PR 7](https://github.com/mkhomutov/Persatrix/pull/391).**
Emitted by [`internal/wallet/wallet.go`](../internal/wallet/wallet.go);
field set is stable so downstream log consumers can pin field names. All
three messages key on the same `lease_id` so an operator can `grep
lease_id=<ULID>` to follow one call end-to-end.

| Event | Message | Level | Fields |
|-------|---------|-------|--------|
| Lease granted | `wallet: lease granted` | `Debug` | `lease_id`, `workflow_id`, `agent_id`, `model`, `cause` |
| Lease denied — budget | `wallet: lease denied — budget exceeded` | `Warn` | `workflow_id`, `agent_id`, `model`, `cause`, denial reason (no `lease_id` — never granted) |
| Lease denied — cap | `wallet: lease denied — per-agent active-lease cap reached` | `Warn` | `agent_id`, `active_leases`, `max_active_leases` |
| Lease settled / released | `wallet: lease finalized` | `Debug` | `op` (`settle` / `release`), `lease_id`, `actual_input_tokens`, `actual_output_tokens` |
| Lease reaped (TTL expiry) | `wallet: lease reaped — settled at granted amount on TTL expiry` | `Warn` | `lease_id`, `workflow_id`, `agent_id`, `model`, `cause` |
| Reaper pass | `wallet: reaper pass complete` | `Debug` | `reaped`, `purged` (emitted only when one is non-zero) |

The same `lease_id` rides on the `agent.llm.call` span as
`persatrix.lease_id` (§ 10.5), so a span can be joined to its
lifecycle logs without joining traces. A budget denial does **not**
produce an `agent.llm.call` span — the wallet refuses *before* the
provider call.

---

## 11. Operator pipeline (RFC 0019 PR 4)

See [diagrams/observability-stack.md](diagrams/observability-stack.md) for the full signal flow (log shipper + OTLP pipeline + gRPC-boundary propagation).

The reference operator stack ships in [docker-compose.yaml](../docker-compose.yaml) and routes every signal through an OpenTelemetry Collector before fanning out to per-signal backends:

```
orchestrator + agents
        |
        |  OTLP HTTP (4318)
        v
 otel-collector  ---> Jaeger        (traces)
       |       \ --> Prometheus    (metrics, scraped)
       |        \-> Loki           (logs, OTLP push)
       |
       |  tail_sampling processor (RFC 0019 SS H):
       |    - keep all ERROR traces
       |    - keep traces >= 5s
       |    - keep traces tagged persatrix.workflow_id
       |    - sample 1% of remaining (autonomous tick) traces
       v
```

Configuration lives at [config/observability/otel-collector.yaml](../config/observability/otel-collector.yaml). The Prometheus scrape config is at [config/observability/prometheus.yaml](../config/observability/prometheus.yaml). Loki uses its image default.

### 11.1 Local ports

| Service | URL | Purpose |
|---------|-----|---------|
| Jaeger UI | http://localhost:16686 | Browse traces, follow Span Links |
| Prometheus UI | http://localhost:9091 | Query metrics, click exemplars to jump to traces |
| Loki HTTP API | http://localhost:3100 | LogQL queries (e.g. `{trace_id="<id>"}`) |
| OTEL Collector | localhost:4317 (gRPC), 4318 (HTTP) | OTLP ingest |

The Prometheus host port is shifted to `9091` so it does not collide with the orchestrator gRPC port (`9090`).

> **Breaking dev-workflow change (v0.2.3):** Jaeger's OTLP ports
> (`4317`/`4318`) are no longer published on the host. The Collector now
> owns the host-facing OTLP ingress on the same port numbers and forwards
> traces to Jaeger over the internal compose network. Local scripts (e.g.
> under `data/`) that set `OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317`
> continue to work unchanged — they now talk to the Collector instead of
> Jaeger directly. Tooling that needs to bypass the Collector must use the
> in-network `jaeger:4317` from another compose service.

### 11.2 Viewing traces in Jaeger

Open http://localhost:16686, pick the `persatrix-server` or `persatrix-agent` service, and search by tag. The `persatrix.workflow_id` baggage attribute is set on every workflow-driven span (RFC 0019 SS E) so a tag query like `persatrix.workflow_id=feature-builder` returns every trace for that workflow across the full process tree.

### 11.3 Querying metrics in Prometheus

The orchestrator exposes `orchestrator.workflow.submitted/completed/failed` counters, `orchestrator.workflow.duration` and `orchestrator.step.duration` histograms, and `workflow.active` (an UpDownCounter). Agent-side metrics live under `agent.tool.*`, `agent.llm.*`, `agent.persona.*`, and the `agent.observability.{spans,logs}.dropped` back-pressure counters.

The channels subsystem (RFC 0011) emits `channel.messages.delivered{channel_type, status}` — one increment per per-subscriber dispatch attempt, with `channel_type ∈ {group, dm, thread}` and `status ∈ {ok, error}`. Sender filtering and `respond: never` skips happen *before* the increment, so the counter reflects effective delivery attempts. Pair `status="error"` with `rate(channel.messages.delivered{status="error"}[5m]) > 0` to alert on a wedged dispatcher.

The publish-side companion `channel.messages.published{channel_type}` increments once per accepted publish — after the store commit, before fanout (ISSUE-0013). Labelled by `channel_type` only. The delivered/published ratio per channel type is computable as `sum by (channel_type)(rate(channel.messages.delivered{status="ok"}[5m])) / sum by (channel_type)(rate(channel.messages.published[5m]))`; a low ratio surfaces "all members filtered" / `RespondNever`-only / sender-only channels that the delivered counter alone cannot distinguish from "no traffic".

The agent-side response gate (RFC 0011 PR 4b) emits `channel.messages.gated{channel_id, policy}` — one increment per `CHANNEL_MESSAGE` the gate suppresses before the LLM call. `policy ∈ {when_mentioned, always, defense_in_depth}` (`never` is filtered upstream of dispatch; `defense_in_depth` covers self-sender re-checks the router already filters — see RFC 0011 §D). The agent identity is carried by the OTLP resource attribute `service.instance.id` (set from `PERSATRIX_AGENT_ID` at startup), not duplicated on every record — `subscriber_id` is excluded from the label set for cardinality reasons (members × channels × policies, ~30,000 series at N=200; per-subscriber drill-down lives in OTEL spans). A high `policy="always"` count signals a wrong policy on the membership; the `when_mentioned` baseline tracks natural channel chatter the agent ignored; a non-zero `policy="defense_in_depth"` count flags a router regression that is handing self-messages back to the gate. Pair with `channel.messages.delivered` to compute the dispatched-vs-responded ratio per channel.

Histogram queries surface exemplars (`--enable-feature=exemplar-storage` is on by default in the dev compose). A p99 LLM-latency spike on the `agent_llm_duration_milliseconds` histogram exposes the originating `trace_id` next to the bucket sample; clicking it opens the trace in Jaeger.

### 11.4 Correlated debugging from a trace ID

1. Start with a slow span in Jaeger (or an exemplar in Prometheus).
2. Copy the `trace_id` from the span detail panel.
3. `persatrix logs --trace <trace_id>` (RFC 0018 PR 6) returns every log record emitted under that trace, ordered by timestamp, across all participating processes.
4. For ad-hoc queries before the CLI lands, query Loki directly: `{trace_id="<id>"}` returns the same record set.

The log-trace correlation contract is locked in by the `test_log_trace_correlation.py` and `test_observability_schema_parity.py` tests; the end-to-end shape against the live compose stack is locked in by the opt-in `test_observability_e2e.py` (run via `pytest -m requires_compose`).

### 11.5 Sampling and back-pressure

- **Head sampling**: parent-based `TraceIdRatioBased(1.0)` on both runtimes (sample everything; tail processor decides).
- **Tail sampling**: see the policy block in `config/observability/otel-collector.yaml`. Tune `num_traces` and `decision_wait` for production load profiles.
- **Back-pressure**: both `BatchSpanProcessor` and `BatchLogRecordProcessor` drop on overflow rather than block. Drop counters (`agent.observability.spans.dropped`, `agent.observability.logs.dropped`) are exported as metrics so a dashboard alert fires when an exporter or collector becomes unavailable.

### 11.6 Production deployment

The dev compose images are conveniences for local debugging and for the schema-parity / E2E tests in CI. Production operators are expected to:

- Run their own OpenTelemetry Collector (or accept Persatrix's reference config and pin the image tag).
- Point the Collector at their own Jaeger / Tempo / Datadog / etc. trace backend, their own Prometheus, and their own Loki / Elasticsearch / etc. log store.
- Apply auth at the OTLP exporter endpoint (Persatrix sends OTLP HTTP unauthenticated by default, consistent with how the Go orchestrator already behaves).
- Override the Collector's policies to match their own retention, sampling, and PII budgets.

Forking `config/observability/otel-collector.yaml` is encouraged; the file is checked in as a starting point, not as a binding contract.

## 12. Operations: `persatrix logs` (RFC 0018 PR 6)

Thin REST/SSE client over the PR 5 endpoints; server-side filtering lives in [`internal/observability/logbuffer`](../internal/observability/logbuffer).

### 12.1 Snapshot vs follow

```shell
persatrix logs <execution_id>                       # snapshot
persatrix logs <execution_id> --follow              # stream (SSE)
persatrix logs _ --trace <trace_id>                 # cross-execution by trace
persatrix logs <execution_id> --since 5m --level WARN
persatrix logs <execution_id> --workflow code-review --agent reviewer-1 --verbose
```

- `_` queries all in-buffer executions; pair with `--trace` for per-trace lookup.
- `--follow` reconnects with exponential backoff (500 ms → 15 s) and prints `[reconnected]` on resume.
- `--verbose` appends `execution_id=… step_id=… trace_id=… attributes={…}`; default render is `<ts> <LEVEL> [<agent>] <message>` with ANSI colour on TTYs.
- `--level` accepts `DEBUG`/`INFO`/`WARN`/`ERROR`, validated by clap before any network call.

### 12.2 Server-side env var knobs

Defaults pinned in [RFC 0018 § Resolved Decisions](rfcs/0018-structured-logging-framework.md#resolved-decisions); read at orchestrator startup:

| Variable | Default | Purpose |
|---|---|---|
| `PERSATRIX_LOGBUFFER_DIR` | `data/logs` | JSONL root (created `0700`). |
| `PERSATRIX_LOGBUFFER_DISK_MB` | `512` | On-disk cap; oldest sealed evicted first. |
| `PERSATRIX_LOGBUFFER_PER_EXEC` | `1000` | Per-execution ring capacity. |
| `PERSATRIX_LOGBUFFER_MAX_EXEC` | `50` | LRU cap on concurrent executions. |
| `PERSATRIX_LOGBUFFER_DROP_LEVEL` | `DEBUG` | Min level kept when over rate. |
| `PERSATRIX_LOGBUFFER_RATE_PER_EXEC` | `1000` | Per-execution admit rate (entries/sec). |

Layout: one dir per execution, one append-only `<sequence>.jsonl` per sealed flush; dir `0o700`, file `0o600`.

### 12.3 Tailing a live workflow

```shell
persatrix workflows submit code-review.yaml
persatrix logs exec-7f2a1 --follow --level INFO
persatrix logs _ --trace abc123 --verbose
```

Across an orchestrator restart, `--follow` prints `[reconnected]` and resumes from the warm-loaded ring; entries flushed to disk before the restart remain queryable via the snapshot path.

### 12.4 End-to-end coverage

Locked in by `tests/integration/test_logs_e2e.py` (opt-in via `pytest -m requires_orchestrator` after `make build-orchestrator build-cli`): snapshot, `--trace` filter, `--follow` latency, warm-load restart, `--level` parse rejection.


## 13. Security audit log (RFC 0009 PR 1b)

JSONL + SHA-256 chain at `OBSERVABILITY_AUDIT_PATH` (default
`data/logs/audit.jsonl`, `=off` disables). Knobs:
[audit.yaml](../config/observability/audit.yaml). Contracts: RFC 0009.

### 13.1. Metrics + SLO alerts (RFC 0009 PR 1c)

OTEL surface in
[audit_instruments.go](../internal/observability/metrics/audit_instruments.go):
`audit.events_total{event_type,class}` (latency early-warning),
`audit.chain_recovered_total` (integrity incidents),
`audit.emit_latency_seconds` (1 ms → 2.5 s buckets, PR #234 Medium-1).

```yaml
# Prometheus alert templates. Thresholds tuned for SSD-backed sinks.
- alert: AuditEmitLatencyP95High
  expr: |
    histogram_quantile(0.95,
      sum by (le) (rate(orchestrator_audit_emit_latency_seconds_bucket[5m]))
    ) > 0.1
  for: 5m
  labels: { severity: page }
  annotations:
    summary: AuditLogger Emit p95 latency exceeds 100 ms

- alert: AuditChainRecovered
  expr: increase(orchestrator_audit_chain_recovered_total[5m]) > 0
  labels: { severity: page }
  annotations:
    summary: Audit chain recovered — integrity incident or mid-write crash

- alert: AuditSecurityClassSilent
  expr: rate(orchestrator_audit_events_total{class="security"}[15m]) == 0
  for: 30m
  labels: { severity: warn }
  annotations:
    summary: No security-class audit events for 30 minutes

# PR #236 review L-5: AuditSecurityClassSilent fires only when the
# series exists. If the orchestrator boots with metrics-init failure,
# orchestrator_audit_events_total is never registered with Prometheus,
# `rate(...)` returns the empty vector, and `empty == 0` evaluates to
# nothing — the silence alert never fires even though every Emit is
# also missing from the metrics pipeline. Pair the silence alert with
# this absence alert so an init failure surfaces independently of
# log-based monitoring (cmd/orchestrator/main.go logs WARN on
# metrics-init failure but the §13 templates should not silently rely
# on log-based alerting for a metrics-stack outage).
- alert: AuditMetricsAbsent
  expr: absent(orchestrator_audit_events_total{class="security"}) == 1
  for: 1h
  labels: { severity: warn }
  annotations:
    summary: Audit metrics series absent for 1 hour — metrics init failure or sink down
```
