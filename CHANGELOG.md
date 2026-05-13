# Changelog

All notable changes to this project will be documented in this file.

## [Unreleased]

### Upgrade Notes

| Notable change | Detail |
|----------------|--------|
| **[Breaking]** Chat wire-field renamed `session_id` → `chat_session_id` | RFC 0031 Phase 1 introduces an operator-namespace `session_id` on channels, messages, episodes, and relationships. To disambiguate it from RFC 0016's chat-conversation token, `ChatRequest.session_id` (proto field 4) and `ChatResponse.session_id` (proto field 2) are renamed to `chat_session_id`. **Field numbers are preserved**, so binary-proto consumers are unaffected. **JSON / proto-text consumers must migrate**: REST callers of `POST /api/v1/agents/{id}/chat` sending the legacy `"session_id"` JSON key now receive `400 BAD_REQUEST "invalid or malformed JSON body"` — the Go handler decodes with `DisallowUnknownFields`, so unknown keys fail loud rather than degrading to a fresh-session mint. Migrate by switching the JSON key to `"chat_session_id"`. Proto3-JSON callers using `google.protobuf.json_format` raise `ParseError` unless `ignore_unknown_fields=True` is set (and even then `chat_session_id` parses to its zero value — the legacy value is discarded). Manual-test `curl` recipes (`MT-CHAT-001`, `MT-CHAT-003`) are updated. The `ChannelMessage.Metadata` map key also moves: `"session_id"` → `"chat_session_id"`. Resolves [RFC 0031 OQ #8](docs/rfcs/0031-per-session-namespacing-channels.md#open-questions). |

## [0.3.0] - 2026-05-12

> **Codename:** Agent Conversations

### Highlights

- **Internal channels — agents and humans share a typed conversation surface.** Group channels, DMs, threads, and the chat-as-DM façade ship as the v0.3.0 user-facing promise (RFC 0011 internal scope). `POST /api/v1/channels`, `POST /api/v1/channels/{id}/messages`, `GET /api/v1/channels/{id}/messages/{msg_id}/thread`, plus a publish → fanout → response-gate → LLM action → publish-back loop wired end-to-end across the Go orchestrator, the Python persona runtime, and a new `persatrix channel` CLI subcommand (`list` / `join` / `send` / `reply` / `history` / `watch`). The legacy `SendChatMessage` gRPC path is dead code — every `/chat` REST call now flows through the channels publish-and-await façade ([RFC 0011 amendment](docs/rfcs/0011-amendment-chat-as-dm.md)).
- **Interaction-bounded episodic memory** (RFC 0020). A multi-turn dialogue is now one episode (open → multi-turn → close → summarize), not one per inbound event. The close path is two-phase + async — synchronous `[summary pending]` sentinel inside the per-agent lock, then a background LLM summariser updates the row — so a follow-up message no longer queues head-of-line behind the summariser. Janitor cleanup wired into `on_tick` recovers crash-stuck `[summary pending]` rows.
- **Persona temporal awareness, Phase 1** (RFC 0021). Persona prompts now emit a `now-anchor` line and render episode + relationship timestamps as relative time ("yesterday", "3 days ago") instead of raw epoch seconds. Structured commitment tracking + scheduled callbacks are deferred to v0.4.0 (Phases 2–4).
- **Memory facade + per-step context-budget allocation** (RFC 0008). `MemoryFacade` becomes the single boundary between persona-runtime + memory tiers (working / relationship / facts / notes / episodic); `attachContextPackage` allocates a typed budget per workflow step and routes it through cross-language wire shape pinned by [`tests/fixtures/context_package_v1.json`](tests/fixtures/context_package_v1.json). Procedural tier ships read-time exponential confidence decay (default 69-day half-life) + revalidation; shared-memory pools with deny-by-default ACL + provenance ship behind a new `shared_memory_pools:` config block.
- **Security Phases 1–2** (RFC 0009). Audit logger with checksum-chained tamper evidence (JSONL append-only sink, per-event fsync for security-class events, three-state startup recovery), `SecretRedactor` with five default patterns + cycle-safe reflective walk, per-agent sliding-window REST + gRPC rate limiter with circuit-breaker quarantine, `<external_data>` envelope wrapping at the LLM-content boundary, Go↔Python sanitizer-pattern parity enforced at build time. Sandbox isolation + token auth (Phases 3–4) deferred to v0.4.0.
- **Externally inspectable persona prompt sections** (RFC 0022). Every persona prompt fragment lives under [`prompts/runtime/persona/sections/`](prompts/runtime/persona/sections/) as a separate markdown file — assembly order pinned by golden tests, forks and out-of-tree tooling that override prompt assembly can pin against this directory shape. New safety snippets `reply-discretion.md` + `conversational-pacing.md` shape the persona's channel-reply behaviour from the prompt layer rather than the executor.

### Upgrade Notes

| Notable change | Detail |
|----------------|--------|
| Channel-event enum hard-rename | `EventType.MESSAGE_RECEIVED` → `CHANNEL_MESSAGE` and `ActionType.SEND_MESSAGE` → `SEND_CHANNEL_MESSAGE` across all Python producers (chat ingest, persona-runtime response gate, dispatch executor, action validators, prompt assembly, state persistence, memory routing). v0.2 enum aliases dropped. `ActionExecutor` result dict now carries `"action_type": "send_channel_message"`. Out-of-tree consumers must update event-type filters and result-dict consumers. |
| Chat REST endpoint migrated to channels | `POST /api/v1/agents/{id}/chat` now goes through the channels publish-and-await façade ([RFC 0011 amendment](docs/rfcs/0011-amendment-chat-as-dm.md)). JSON contract on `chatRequest` / `chatResponse` is preserved — Rust CLI and existing REST clients are unaffected — but the legacy `SendChatMessage` gRPC path is dead code (cleanup tracked in ISSUE-0035). |
| New `SECURITY_RATE_LIMIT_*` env vars | RFC 0009 PR 2 ships a per-agent sliding-window rate limiter with circuit breaker. `SECURITY_RATE_LIMIT_ENABLED`, `SECURITY_RATE_LIMIT_CALLS`, `SECURITY_RATE_LIMIT_WINDOW_SECONDS`, `SECURITY_RATE_LIMIT_MAX_AGENTS` configure the limiter at startup (defaults: enabled, 60 calls / 60-s window). Opting out emits a one-shot `rate_limit.disabled` audit event so the choice is visible in the audit log. Operators see HTTP 429 + `Retry-After` on REST and `ResourceExhausted` / `PermissionDenied` on gRPC after threshold violations; `POST /api/v1/agents/{id}/unquarantine` clears a quarantined agent (call it with a non-`anonymous` `X-Agent-ID` header so the operator's own request is not rate-limited as `anonymous`). |
| `<external_data>` envelope wrapping | RFC 0009 PR 3 wraps `http_request` / `file_read` tool results in an `<external_data>…</external_data>` envelope at the LLM-content boundary, with close/open-tag escaping (`_EXTERNAL_DATA_TAG_RE`) so untrusted content cannot break out. Out-of-tree LLM evaluators or post-processors that grepped on raw tool-output strings must move to the wrapped form. The unconditional `external-data-handling` prompt fragment teaches the model the contract. |
| Channels REST surface is unauthenticated | A startup `WARN` notice fires whenever the channels subsystem is enabled. `sender_id` is body-trusted in v0.3.0 — firewall the port or front with an authenticating reverse proxy until [RFC 0009 Phase 4](docs/rfcs/0009-security-sandboxing.md) lands in v0.4.0. The notice is intentionally not suppressible from config. |
| Persona prompt-section directory is public surface | Every persona prompt fragment now lives under [`prompts/runtime/persona/sections/`](prompts/runtime/persona/sections/) ([RFC 0022](docs/rfcs/0022-persona-prompt-section-templating.md)). Forks and out-of-tree tooling that override prompt assembly should pin against this directory shape. |
| Now-anchor in persona prompts | Persona prompts emit a `now-anchor` line and render episode + relationship timestamps as relative time ("yesterday", "3 days ago") instead of raw epoch seconds (RFC 0021 Phase 1). Out-of-tree prompt evaluators that key on absolute timestamps must move to the rendered form, or read the underlying epoch from the episodic store directly. |
| Episodic write cadence changed | RFC 0020 collapses multi-turn dialogues to **one** episodic entry per interaction (open → multi-turn → close → summarize) instead of one entry per inbound event. Out-of-tree memory-inspection tools that counted episodes-per-message will see the count drop sharply on chatty channels — this is by design. The `interaction_id` + scope tag on each episode is the new lookup key. |
| `relationships.interaction_count` unit changed | `interaction_count` and `auto_reflect_after` are now per-closed-interaction, not per-message. A 10-message DM session now bumps `interaction_count` by 1 (previously by 10). Operators with bespoke trust thresholds calibrated against the per-message scale should consult the Migration Notes in [docs/rfcs/0020-interaction-lifecycle.md](docs/rfcs/0020-interaction-lifecycle.md). |
| `memory.min_score` schema default changed | `null` → `0.20` (RFC 0008 PR 2a). Operators with `memory.enabled: true` who did not previously set `memory.min_score` will see strictly fewer recall results — low-score entries are no longer concatenated into the system prompt. Restore the pre-PR-221 behaviour by setting `memory.min_score: null` in [`config/agents.yaml`](config/agents.yaml). |
| `MemoryFacade.store_procedure` key validation | Validates `key` against `^[A-Za-z0-9._-]+$` (max 256 chars) and raises `ValueError` on non-conforming keys. Callers persisting procedural keys with spaces, slashes, percent-signs, non-ASCII characters, or newlines must rename them before upgrading. |
| `pytest-timeout` test dependency added | Transitive dev dep added per ISSUE-0024 to stop the Python unit-suite from hanging on the full-suite invocation. Genuinely MIT-licensed; the `check-licenses-python` Makefile target carries an `--exception pytest-timeout` with justification (pip-licenses `--from=mixed` concatenates the legacy `License :: DFSG approved` Trove classifier producing a token the strict allow-list doesn't accept). |

### 🚀 Features

- *(memory)* RFC 0020 PR 1 - InteractionTracker + episodes schema v5 (#214)
- *(memory)* RFC 0020 PR 2 — route TICK + tool-only events through InteractionTracker (#215)
- *(memory)* RFC 0020 PR 3 - multi-turn aggregation for human-chat + DM (#216)
- *(rfc0008)* PR 1 - context budget allocator + packaging foundation (#218)
- *(rfc0008)* PR 1b — context metrics emission + remaining-budget persistence (#219)
- *(rfc0008)* PR 2 — MemoryFacade for task agents (#220)
- *(rfc0008)* PR 2a - episodic-tier eviction + PR 2 follow-up findings (#221)
- *(rfc0008)* PR 3 - delegation contract + merge engine (#222)
- *(rfc0008)* PR 4 - shared pool ACL + provenance (#223)
- *(rfc0008)* PR 3a - delegation metrics + PR 3 follow-up findings (#224)
- *(rfc0008)* PR 5 - confidence decay + procedural revalidation (#225)
- *(rfc0008)* PR 6a - Go scheduler hygiene + sampler bookkeeping (#227)
- *(rfc0008)* PR 6b - Python procedural memory + log-safety cleanup (#228)
- *(rfc0020)* PR 4 — summarization-on-close + janitor + record_interaction move (#229)
- *(rfc0011)* PR 1 - channel store + SQLite migration + schema rewrite (#231)
- *(rfc0009)* PR 1 - AuditLogger + SecretRedactor (security package + unit tests) (#233)
- *(rfc0009)* PR 1b — audit wiring + default redactor + chmod self-heal (#234)
- *(rfc0009)* PR 1c — RedactStruct hardening + audit metrics (#236)
- Externalize hardcoded literals to prompt snippets and config (#239)
- *(rfc0009)* PR 2 - RateLimiter + CircuitBreaker + REST/gRPC middleware (#244)
- *(rfc0011)* PR 2 — channels REST + router + config reconciliation (#245)
- *(rfc0011)* PR 3 — proto + RPC for ChannelMessageEvent (#246)
- *(rfc0011)* PR 4a-i — ReceiveChannelMessage real handler + additive enums (#248)
- *(rfc0011)* PR 4a-ii-α — hard rename CHANNEL_MESSAGE/SEND_CHANNEL_MESSAGE + SF-3 mentions validation (#249)
- *(rfc0011)* PR 4a-ii-β-1 — real Go gRPC MessageDispatcher + Python REST publish rewire (#250)
- *(rfc0011)* PR 4a-ii-β-2 — chat-as-DM rewrite (Go-side waiter + PublishAndAwait) (#251)
- *(rfc0011)* PR 4b — channels response gate + DELETE endpoints (#252)
- *(rfc0009)* PR 3 — InputSanitizer + ContextItem + external_data envelope (#253)
- *(rfc0021p1)* PR 1 — Clock seam + temporal rendering pure functions (#256)
- *(rfc0021p1)* PR 2 — now-anchor + episode/relationship recency rendering (#260)
- *(rfc0021p1)* PR 3 — review follow-ups + RFC Phase-1 close (#261)
- *(rfc0020)* PR 5 — per-channel scoping + closing-row recall filter (#262)
- *(rfc0011)* PR 5 — channel ingest sanitization + gate-suppress memory (#263)
- *(rfc0011)* PR 5 follow-up — channel-history tier in MemoryBudget (#264)
- *(rfc0011)* PR 5 follow-up — on-startup catch-up fetch (OQ #8) (#265)
- *(rfc0020)* PR 6 slice 1 — Phase-2/janitor race + PR 4 review follow-ups (#266)
- *(channels)* Close ISSUE-0015 — paginate ListChannels via keyset cursor (#280)
- *(channels)* ISSUE-0032 — emit channel.dispatch OTel span (Go side) (#286)
- *(agents)* Close ISSUE-0032 — emit channel.publish OTel span (Python side) (#287)
- *(rfc0020)* PR 6 slice 2 — typed CloseReason + table-driven _emit_closed dispatch (#296)
- *(rfc0011)* PR 6 — Rust CLI channel subcommands (list/join/send/reply/history/watch) (#302)
- *(rfc0009)* PR 4 — review follow-ups + Phases 1-2 close (#306)
- *(v030)* Demo personas + planning channel + walkthrough guide (#316)
- *(persona)* Reply-discretion + conversational-pacing prompt snippets (#327)

### 🐛 Bug Fixes

- *(agent)* Include prompts in image and configure audit log path (#235)
- *(security)* Dedupe ContextSource validation + codegen enum parity (#254) (#255)
- *(security)* Close ISSUE-0001 — CircuitBreaker rejects Window/Count <= 0; add Disabled flag (#270)
- *(security)* Close ISSUE-0007 — propagate request ctx through RateLimiter/CircuitBreaker audit emits (#272)
- *(channels)* Close ISSUE-0034 — demote chat-DM user to RespondNever (#276)
- *(agents)* Close ISSUE-0027 — symmetrize SEND_CHANNEL_MESSAGE result dicts (#277)
- *(docker)* Close ISSUE-0046 + ISSUE-0047 — get compose stack functional for v0.3.0 (#279)
- *(agents)* Close ISSUE-0026 — sticky-disable HTTPChannelPublisher on first 503 (#281)
- *(agents)* Close ISSUE-0048 — synthesise SEND_CHANNEL_MESSAGE for plain-text persona replies (#282)
- *(scripts)* Close ISSUE-0036 — switch doc_links collector to `git ls-files` (#284)
- *(channels)* Close ISSUE-0049 — buildDSN merges caller query params instead of double-? concatenation (#294)
- *(channels)* Close ISSUE-0050 — soft byte cap on msg.Content at the SQLite store boundary (#295)
- *(v030)* Channel cascade-depth wire propagation — amendment + schemas (PR 1) (#318)
- *(v030)* Channel cascade-depth Go orchestrator enforcement (PR 2) (#319)
- *(v030)* Channel cascade-depth Python round-trip (PR 3) (#321)
- *(v030)* Channel cascade-depth cross-process integration pin (PR 4) (#322)
- *(v030)* Channel persona impersonation — grounding clause (PR 5) (#323)
- *(v030)* Channel state-reset Make target + operator-guide notes (PR 6) (#324)

### 🔒 Security

- *(server)* Close ISSUE-0004 — hash bearer token before constant-time compare (#275)
- *(ratelimit)* Close ISSUE-0005 — emit rate_limit.reset audit event from RateLimiter.Reset (#285)

### ⚡ Performance

- *(security)* Close ISSUE-0003 — RateLimiter.evictOlderThan in-place compaction (#274)
- *(channels)* Close ISSUE-0014 — bounded-concurrency fanout in ChannelRouter (#283)

### 🔧 Refactoring

- *(tests)* Split test_persona_runtime.py into focused modules (#195)
- *(tests)* Split test_episodic_memory.py into focused modules (#196)
- *(tests)* Split test_event_dispatch_tick.py into focused modules (#197)
- *(tests)* Split scheduler_test.go into focused modules (#198)
- *(tests)* Split server_test.go into focused modules (#199)
- *(tests)* Split executor_test.go into focused modules (#200)
- *(tests)* Split test_validate.py and planner_test.go into focused modules (#201)
- *(tests)* Split state_test.go and test_server.py into focused modules (#202)
- *(tests)* Split test_chat_servicer.py, encoder_test.go, cost_test.go into focused modules (#203) (#203)
- *(tests)* Split oversized Python test files to comply with 500-line policy (#204)
- *(prompts)* Externalize task-agent instructions into prompts/runtime/ (#210)
- *(prompts)* Externalize safety snippets into prompts/runtime/safety/ (#211)
- *(prompts)* Externalize behavior-dimension descriptions into prompts/runtime/persona/sections/ (#212)
- *(prompts)* Externalize persona section composer (RFC 0022, PR C) (#213)
- *(orchestrator)* Close ISSUE-0008 — extract startup helpers, drop main.go below 500 lines (#292)
- *(memory)* Drop file-size grandfather entries — split memory_context, episodic + verify facade (#293)
- *(rfc0020)* PR 6 slice 3 — migration no-op cleanup + autouse metrics fixture (#297)
- *(rfc0020)* PR 6 slice 4 — PR-2 review #6/#7/#9/#10/#11 + episode-routing mixin extraction (#298)
- *(rfc0020)* PR 6 slice 5 — clock seam + cross-scope idle-flush attribution (#299)
- *(rfc0020)* PR 6 slice 6 — inline MaxTurns cap + multi-turn close-path coverage (#300)
- *(rfc0020)* PR 6 slice 7 — tighten _llm_client to LLMClient + drop dead silent-drop branches (#301)

### 📚 Documentation

- *(release)* Post-release follow-up for v0.2.3 (#192)
- Apply Priority 1 + 2.2 + 3.2 cleanup recommendations (#194)
- *(rfcs)* V0.3.0 planning kickoff — RFC 0011 (Channels) and roadmap corrections (#205)
- *(planning)* Add v0.3.0 master plan (#206)
- *(planning)* Scaffold the six v0.3.0 RFC PR plans (#207)
- *(ai)* Enforce brevity policy and trim prompt footprint (#208)
- *(ai)* Add canonical AI glossary and enforce it in assistant instructions (#209)
- *(rfc0008)* Flesh out RFC 0008 PR plan from scaffold (#217)
- *(rfc0008)* Triage accumulated PR 1-5 follow-ups before RFC close (#226)
- *(rfc0011)* Flesh out RFC 0011 PR plan from scaffold (#230)
- *(rfc0009)* Resolve in-scope open questions and flesh out PR plan (#232)
- Memory quality roadmap (assess draft RFCs 0023-0025, propose alternatives) (#237)
- *(instructions)* Adopt TDD from v0.3.0 onward (#240)
- Introduce docs/issues/ finding tracker with make issues target (#241)
- *(memory-quality)* Integrate roadmap into v0.3.x and v0.4.0 plans (#238)
- *(rfc)* Propose RFC 0028 agent decision policy engine (#242)
- *(storage)* Propose storage architecture roadmap discussion doc (#243)
- *(rfc)* Amend RFC 0011 with chat-as-DM unification (RFC 0016 reconciliation) (#247)
- *(rfc)* V0.3.0 readiness hygiene — status flips, stale Decision/Next Steps, OQ resolutions (#258)
- *(scope)* Retarget RFC 0007 from v0.3.0 to v0.4.0 (#259)
- *(security)* Close ISSUE-0002 — align GRPCRateLimitInterceptor godoc with grpc.SetHeader + add client-side contract test (#273)
- *(proto)* Close ISSUE-0019 + ISSUE-0022 — TaskAck reuse policy + timestamp format cross-reference (#291)
- *(rfc0011)* PR 7 — Phase 4b human participation MTs + channels guide + diagram (#303)
- *(rfc0011)* PR 8 — internal-scope close (NTH dispatch + status flips) (#304)
- *(rfc0020)* PR 7 — RFC close (status flips for v0.3.0 scope) (#305)
- *(rfc0023)* Introduce LLM call leasing RFC (#307)
- *(rfc0024)* Propose event-driven agent scheduling (#308)
- *(rfc0029)* Propose personal/society storage split (#309)
- *(rfc0023)* Review follow-ups — 3 correctness fixes + 8 clarifications (#310)
- *(v0.3.x)* Sequence RFCs 0023/0024/0026/0029 across v0.3.1-v0.3.3 (#311)
- *(v030)* Release-prep plan + walk back RFC 0008 OQ #12 calibration-window gate (#312)
- *(rfc0008)* PR 6 — review follow-ups absorbed + RFC close (#313)
- *(v030)* Release-prep PR 2 — README + ROADMAP + guide callouts + diagram refresh + release checklist (#315)
- *(v030)* PR plan for v0.3.0 channel test findings (#317)
- *(rfc0030)* Propose multi-agent conversation governance (#320)
- *(rfcs)* RFC 0031 — per-session namespacing for channels + persona memory (#325)
- *(rfcs)* YAML front-matter + auto-generated INDEX.md (#326)

### 🧪 Testing

- *(proto)* Close ISSUE-0021 — pin ChannelMessageEvent + TaskAck wire shape (#278)
- *(channels)* Close ISSUE-0025 — full-chain REST→fanout→gRPC integration test (#290)
- *(v030)* Release-prep PR 1 — manual test execution report + 3 release-prep regression fixes (#314)

### 🏗️ Build

- *(proto)* Close ISSUE-0017 — auto-generate agents/generated/*.pyi via mypy-protobuf (#288)
- *(proto)* Close ISSUE-0023 — gate proto/ source-of-truth (Python freshness + orphan detection) (#289)

### 📦 Miscellaneous

- *(deps)* Bump rustls-webpki from 0.103.12 to 0.103.13 in /cli (#193)
- *(deps)* Bump openssl from 0.10.78 to 0.10.79 in /cli (#257)
- *(rfc0021p1)* Close #261 review follow-ups (ISSUE-0042/0043/0044/0045) (#267)
- *(rfc0011)* Close ISSUE-0028/0030/0031 — channels dispatcher observability + test gaps (#268)
- *(rfc0011)* Close ISSUE-0010/0011/0013 — PR #245 review follow-ups (#269)
- *(rfc0009)* Close ISSUE-0006 — WARN on invalid SECURITY_RATE_LIMIT_* env values (#271)

[0.3.0]: https://github.com/mkhomutov/Persatrix/compare/v0.2.3...v0.3.0

## [0.2.3] - 2026-04-24

> **Codename:** Observability Foundation

### Highlights

- Structured JSON logs on a versioned schema across the Go orchestrator, Python
  agents, and the `persatrix` CLI — every entry carries `schema_version`,
  `service.kind`, `service.instance`, `source`, and the four reserved correlation
  IDs (`execution_id`, `step_id`, `agent_id`, `workflow_id`) (RFC 0018).
- Distributed OpenTelemetry traces end-to-end from REST handler to LLM call, with
  Gen-AI semantic conventions on every `agent.llm.call` span and Span Links for
  cross-tree causality (RFC 0019).
- OTLP metrics (counters + histograms) on both Go and Python runtimes, with
  histogram exemplars that point Prometheus click-throughs back to the
  originating Jaeger trace.
- W3C Baggage + the four reserved correlation IDs propagate across the Go →
  Python gRPC boundary via a dedicated `internal/observability/grpcmeta`
  surface and a Python `LoggingMetadataInterceptor`.
- Tail-sampling OpenTelemetry Collector pipeline
  (`config/observability/otel-collector.yaml`), with dev-stack `otel-collector`
  + `prometheus` + `loki` wired into `docker-compose.yaml`.
- `persatrix logs <execution_id>` CLI with REST query, `--follow` SSE stream,
  disk-store durability, filter flags, and `jq`-friendly JSON output.

### Upgrade Notes

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
  format. Pretty mode is **not** consumed by the `persatrix logs` endpoint —
  leave unset in production.

- **`PERSATRIX_SERVICE_INSTANCE`** overrides the orchestrator's
  `service.instance` log field (defaults to `os.Hostname()`). Useful in
  containerised deployments where the hostname is an ephemeral pod ID.

- **`PERSATRIX_TRACE_TOOL_PAYLOADS`** controls `agent.tool.execute` span
  detail. Defaults to `none` (only `tool.name`). `metadata` adds
  `tool.arguments.<arg>.type`; `full` emits redacted argument values via
  the same `Redactor` Protocol used for log redaction. Use `full` only
  with a configured redactor — the default `NoopRedactor` echoes values
  verbatim and may capture secrets.

### 🚀 Features

- *(logs)* Schema doc + Python structlog chain + redactor surface (RFC 0018 PR 1/7) (#164)
- *(logs)* Go zap rename + pretty + redactor wired + source (RFC 0018 PR 2/7) (#165)
- *(logs)* Cross-process correlation IDs + OTEL trace IDs on logs (RFC 0018 PR 3/7) (#168)
- *(logs)* `log_service.proto` + ring buffer + disk store + rate limiter (RFC 0018 PR 4/7) (#172)
- *(logs)* `LogService` server + agent shipper + REST + SSE (RFC 0018 PR 5/7) (#173)
- *(cli)* `persatrix logs` rewrite — filters + SSE follow + E2E (RFC 0018 PR 6/7) (#174)
- *(observability)* `internal/telemetry` → `internal/observability` rename + Python OTEL init + gRPC + Baggage (RFC 0019 PR 1/5) (#163)
- *(observability)* Semantic spans + Span Links + Gen-AI conventions (RFC 0019 PR 2/5) (#167)
- *(observability)* OTLP metrics (Python + Go) with exemplars (RFC 0019 PR 3/5) (#170)
- *(observability)* Collector pipeline + docker-compose + E2E + schema-parity test (RFC 0019 PR 4/5) (#171)
- *(docker)* Wire persona agent ember-owl into compose stack (#188)

### 🐛 Bug Fixes

- *(logs)* Zap encoder correctness cluster — Must-style ctor + reserved-key shadowing (issue #178) (#183)
- *(logs,observability)* Should-Fix correctness cluster — sentinel collision + timestamp policy + SSE write deadline (issue #179) (#182)
- *(logs)* Tee orchestrator zap entries into log buffer — MT-LOGS-001 follow-up (#184)
- *(observability)* MT-OTEL-001 walkthrough alignment + propagation-gap surfacing (#185)
- *(logs)* RFC 0018 closeout — review follow-ups + status flip (PR 7/7) (#180)
- *(observability)* RFC 0019 closeout — review follow-ups + status flip (PR 5/5) (#181)

### 🔧 Refactoring

- *(logs)* Log buffer + shipper polish (RFC 0018 PR 8, optional polish) (#177)
- *(observability)* Tracing/spans review follow-ups (RFC 0019 PR 6, optional polish) (#176)

### 📚 Documentation

- *(rfcs)* Joint PR plans for RFC 0018 + RFC 0019 (v0.2.3 Observability Foundation) (#161)
- *(rfcs)* Describe closeout PR scope in plans (#175)
- *(release)* v0.2.3 release preparation plan (#186)
- *(release)* v0.2.3 MT execution report + release-prep fixes (#187)
- *(release)* v0.2.3 README + ROADMAP + guide refresh + observability diagram + release checklist (#189)

### 🧪 Testing

- *(observability)* Schema-parity, log↔trace correlation, and compose-gated E2E (RFC 0019 PR 4) (#171)
- *(logs)* `logbuffer` ring + disk-store + rate-limiter unit tests (RFC 0018 PR 4) (#172)
- *(logs)* `LogService` server + agent shipper + REST + SSE tests (RFC 0018 PR 5) (#173)
- *(logs)* `persatrix logs` REST round-trip + SSE follow E2E (RFC 0018 PR 6) (#174)

### 📦 Miscellaneous

- *(deps)* Upgrade `tabled` 0.16 → 0.20, resolve RUSTSEC-2024-0370 (#162)

[0.2.3]: https://github.com/mkhomutov/Persatrix/compare/v0.2.2...v0.2.3

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


