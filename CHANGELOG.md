# Changelog

All notable changes to this project will be documented in this file.

## [Unreleased]

### Added

- **RFC 0011 PR 4a-ii-α — hard rename
  `MESSAGE_RECEIVED`/`SEND_MESSAGE` → `CHANNEL_MESSAGE`/`SEND_CHANNEL_MESSAGE`
  + SF-3 mentions validation.** Drops the v0.2 enum aliases now that
  PR 4a-ii-α has migrated every Python producer (chat ingest,
  persona-runtime response gate, dispatch executor, action validators,
  prompt assembly, state persistence, memory routing, all unit and
  integration tests, glossary + spec docs) onto the canonical channel
  vocabulary. The cross-process REST/gRPC rewire lands in PR 4a-ii-β
  per the α/β split documented in `docs/rfcs/0011-pr-plan.md`.

  **Behaviour change visible to downstream consumers:** the
  `ActionExecutor` result dict for channel sends now carries
  `"action_type": "send_channel_message"` (previously
  `"send_message"`). Any log scraper, evaluator, or telemetry pipeline
  that grepped on the v0.2 literal must be updated. The
  `EventType.MESSAGE_RECEIVED` and `ActionType.SEND_MESSAGE` enum names
  no longer resolve and any out-of-tree producer must move to the new
  members. (PR #249.)

  Also closes PR #231 review SF-3: `sqliteStore.PublishMessage` now
  validates every entry in `msg.Mentions` through the same
  `validateParticipantID` check the sender goes through, before
  `BeginTx`. The error wraps the offending index
  (`mentions[%d]: %w`) so callers can identify the bad value while
  preserving `errors.Is(err, ErrInvalidParticipantID)` for the
  Router's 422 mapping.

- **PR #248 deep-review follow-ups — `CHANNEL_MESSAGE` runtime
  integration + receiver hardening.** Closes the High/Medium/Low
  findings on top of the PR 4a-i scope:

  - **Persona-runtime routing for the new enum** (PR #248 deep review
    High + Medium): `EventType.CHANNEL_MESSAGE` now sits in
    `_StatePersistenceMixin._MULTI_TURN_EVENT_TYPES` (alongside
    `MESSAGE_RECEIVED` / `MENTION`) so dispatched channel events take
    the multi-turn episode path instead of falling through the legacy
    "not classified" warning fallback PR-215 added; `_format_event`
    treats `CHANNEL_MESSAGE` exactly like `MESSAGE_RECEIVED`
    (`<|user_message|>` delimiter wrap with PR #120 F-2 sanitisation)
    so the LLM sees a sender-attributed prompt rather than a raw
    `json.dumps(payload)` blob; `action_loop` extracts
    `payload["content"]` (not the wrapped form) for the FTS5
    `memory_query` so recall is not contaminated with delimiter tokens.
    Dormant until a producer wires `ReceiveChannelMessage` end-to-end
    (PR 4a-ii / PR 4b), but pinned by tests now so the path is correct
    the moment it activates.
  - **Bounded `_pending_dispatches` queue** (PR #248 deep review Low):
    new `_MAX_PENDING_DISPATCHES = 1000` cap on the strong-ref
    fire-and-forget set in `AgentServiceServicer`. Once full,
    `ReceiveChannelMessage` returns
    `TaskAck(success=False, error_message="receiver overloaded …")`
    so the orchestrator's existing per-ack failure path becomes the
    backpressure signal — closes a slow-burn DoS surface on the
    cleartext gRPC port symmetric with the validator's other bounds
    work.
  - **Naive RFC 3339 timestamp rejection** (PR #248 deep review NTH):
    `parse_channel_timestamp` now returns `None` for inputs lacking a
    `time-offset` (e.g. `"2026-05-04T00:00:00"`). `datetime.fromisoformat`
    parses such values as *naive* datetimes and the subsequent
    `.timestamp()` then converts via the *host* timezone, silently
    shifting the publish time by however many hours the receiver is
    offset from UTC. RFC 3339 §5.6 mandates the offset; the validator
    now enforces the contract.
  - **`__all__` hygiene**: removes `_extract_chat_reply` (a
    single-leading-underscore, module-private name) from
    `agents.server_servicers.__all__`. Direct imports continue to work
    (the back-compat shim is unchanged); only `from … import *` no
    longer pulls a name that advertises itself as private.
  - **Unicode content-cap pinning** (PR #248 deep review NTH): new
    tests pin that the validator's "4000 character" content cap means
    4000 *codepoints*, not 4000 wire bytes — accepts `"🦊" * 4000`
    (16 KB UTF-8) and rejects `"🦊" * 4001`, so a future "switch to
    bytes" refactor surfaces as a hard test failure rather than
    silently halving the effective limit for non-ASCII traffic.

- **RFC 0011 PR 4a — `ReceiveChannelMessage` real handler + additive
  enums.** Replaces the PR 3 `TaskAck(success=False)` stub on
  `AgentServiceServicer.ReceiveChannelMessage` with a real receiver-side
  handler: validates the wire-side `ChannelMessageEvent` (mentions cap,
  participant-id pattern, content/thread-id length, channel_type ↔
  channel_id prefix agreement) defensively against the cleartext gRPC
  transport, resolves the target agent on the single-agent-per-process
  server, builds an `AgentEvent(event_type=CHANNEL_MESSAGE)`, and
  schedules dispatch via `asyncio.create_task` with a strong-ref task
  set (`self._pending_dispatches`) so Python 3.11+ does not GC the task
  mid-flight (PR #246 deep review Should-Fix #2). Returns
  `TaskAck(success=True)` on enqueue (at-most-once contract; the
  orchestrator does not retry). Adds two new enum members **additively**
  alongside the v0.2 names: `EventType.CHANNEL_MESSAGE` and
  `ActionType.SEND_CHANNEL_MESSAGE`. Promotes `thread_id` to a top-level
  `AgentEvent` field per RFC 0011 §D so the response gate (PR 4b) can
  branch on thread context without a payload lookup.

  The hard renames `EventType.MESSAGE_RECEIVED` → `CHANNEL_MESSAGE` and
  `ActionType.SEND_MESSAGE` → `SEND_CHANNEL_MESSAGE`, the
  `SEND_CHANNEL_MESSAGE` dispatch executor in `agents/dispatch.py`, the
  orchestrator-side `internal/executor/dispatch.go::DispatchChannelMessage`,
  the persona-runtime response gate, the DELETE endpoints, and the
  chat-path migration per the RFC 0011 chat-as-DM amendment land in
  follow-up PRs (chat is the heavy producer of the old enum names;
  renaming without migrating chat would leave `main` broken).

- **RFC 0011 PR 3 — Proto + RPC for channel-message delivery.** Adds
  `ChannelMessageEvent` + `ReceiveChannelMessage` (returning a new minimal
  `TaskAck`) to `proto/task.proto` per RFC 0011 §C. The `channel_type`
  string field on the event duplicates the prefix encoded in `channel_id`
  ("group:" / "dm:" / "thread:") so log/metric attributes do not have to
  re-parse the address; the orchestrator's `ChannelRouter` (PR 2) already
  validates agreement on publish, and receivers should drop on mismatch
  as malformed. The Python `AgentServiceServicer` gains a stub
  `ReceiveChannelMessage` that returns
  `TaskAck(success=false, error_message="…RFC 0011 PR 4")` — fail-closed
  so the eventual orchestrator dispatcher cannot mistake the stub
  response for a real ack (PR #246 deep review H1). The real handler
  (build `AgentEvent(event_type=CHANNEL_MESSAGE)` and dispatch through
  `EventDispatcher`) lands in PR 4 alongside the orchestrator-side
  `DispatchChannelMessage` action and the `MESSAGE_RECEIVED` →
  `CHANNEL_MESSAGE` event-type rename.

### Removed

- **v0.2-era `ChannelService` proto surface.** Deletes
  `proto/agent_message.proto` (`ChannelService.SendMessage` +
  `ChannelService.Subscribe(stream)`, `AgentMessage`, `MessageType`,
  `Visibility`, `Attachment`), the matching `ChannelServiceServicer` in
  `agents/server_servicers.py`, its registration in `agents/server.py`,
  and `tests/unit/python/test_server_channel.py`. The surface had no
  producer wired anywhere in the codebase — server-streaming `Subscribe`
  was incompatible with the orchestrator-mediated dispatch model adopted
  in RFC 0011 §C, and `MessageType` / `Visibility` were never read by a
  consumer. The new agent-side delivery path is `AgentService.ReceiveChannelMessage`
  on `proto/task.proto` (this PR). Generated stubs `agents/generated/agent_message_pb2*`
  and `internal/generated/msgpb/` are removed in the same commit so CI
  never sees a missing-import window.

- **RFC 0011 PR 2 — Channels REST surface + router + config reconciliation.**
  Wires `internal/channels` (PR 1) into the orchestrator: new `ChannelRouter`
  publishes through the store with `channel_type` cross-validation and
  per-recipient fanout (the gRPC dispatcher remains a `NoopDispatcher` until
  PR 4); REST endpoints land at `POST /api/v1/channels`,
  `GET /api/v1/channels`, `GET /api/v1/channels/{id}`,
  `POST /api/v1/channels/{id}/messages`,
  `GET /api/v1/channels/{id}/messages`,
  `GET /api/v1/channels/{id}/messages/{msg_id}/thread`, and
  `POST /api/v1/channels/{id}/members`; startup reconciles
  `config/channels.yaml` against the live store under §B coexistence rules
  (loud-fail on membership divergence). New env-overridable flag
  `--channels-db` (default `data/channels.db`) selects the SQLite path; a
  new `cmd/orchestrator/channels.go` extraction keeps `main.go` from
  growing past the 500-line review-friendly cap (ISSUE-0008). Schema
  migrates to **v2** automatically on first open: `channels.name` becomes
  nullable (DM/thread channels store NULL) and a partial unique index
  `ux_channels_name_group` enforces uniqueness only over group rows; the
  rebuild runs the SQLite "12-step" sequence with `PRAGMA foreign_keys=OFF`
  so existing membership and message rows survive intact. `ChannelStore`
  grows one method (`CreateChannelWithMembers`) so handler-side create
  bundles are atomic at the store boundary; the new
  `internal/channels/sqlite_pr2_review_test.go` pins both the migration
  child-row contract and the rollback contract. PR #245 re-review applied:
  the `ReconcileConfig` missing-channel arm now uses the same atomic
  `CreateChannelWithMembers` helper (no more orphan rows on partial
  failure); the store's `name`-required and `name`-pattern errors are
  wrapped with `ErrInvalidChannelType` so REST callers see 400 instead of
  500 on bad input.

  **Security — UNAUTHENTICATED in v0.3.0.** The channels REST endpoints
  ship without authentication this release. `sender_id` is body-trusted,
  and any HTTP-reachable client can publish as any registered participant
  or add themselves to any channel (including `group:`-prefixed channels
  declared in `config/channels.yaml`). Token-based auth lands in RFC 0009
  Phase 4. Until then operators MUST: (1) bind the orchestrator listener
  to `127.0.0.1`, (2) front it with an authenticating reverse proxy, or
  (3) firewall the port. The orchestrator emits a one-shot
  `channels: REST surface is UNAUTHENTICATED in v0.3.0 …` Warn at startup
  whenever the channels subsystem is enabled so the trust boundary is
  surfaced in the first log scrape (PR #245 re-review Must-Fix #1).

  PR #245 deep review (round 3) applied: (a) the publish handler's
  router-nil fallback path now emits a once-per-process `Warn`
  signposting that channel_type cross-validation and the
  `channel.messages.delivered` metric are skipped — production
  callers always wire the router via `WithChannels(store, router)`,
  but a forgotten wiring is now observable on first publish instead
  of silently degrading (Should-Fix #3); (b) the default REST list
  page size (`channelDefaultListLimit`) is aligned with
  `channels.DefaultMaxChannels` so the page size never exceeds the
  global channel cap (Nice-to-Have #3).

- **RFC 0009 PR 1b — Audit wiring + default redactor + chmod self-heal.**
  Wires the PR 1 `AuditLogger` + `SecretRedactor` into orchestrator hot
  paths so security-relevant lifecycle events become forensically
  observable on disk. New env var `OBSERVABILITY_AUDIT_PATH` (default
  `data/logs/audit.jsonl`, resolved to absolute at startup; literal
  `=off` disables, case-insensitive) selects the JSONL sink; parent
  directory is created `0o700` and the file is opened `0o600` with
  per-open chmod self-heal that re-tightens pre-existing files
  (POSIX-only — on Windows the call is a no-op against ACLs; see
  `docs/observability.md` §13). The orchestrator REST `register`
  handler now emits `agent.registered` (success) and
  `capability.violation` (rejection) and the gRPC executor emits
  `tool.invoked` on every successful dispatch. Both wiring points are
  nil-safe and opt-in via `WithAuditLogger`. Default `Redactor`
  installed at constructor (closes prior plaintext-leak window from
  PR #233 review SF-3); pass `WithRedactor(nil)` only for tests that
  need to write plaintext fixtures. New `WithLogger(*zap.Logger)`
  option routes audit-logger self-diagnostics (e.g. chmod warnings)
  through the structured pipeline instead of raw stderr. PR #234
  review applied: `tool.invoked` and `agent.registered` emits use
  `context.WithoutCancel` so cancellation racing a committed side
  effect cannot drop the forensic record; `validateCapabilities` caps
  per-request capability count at 64 and per-value echo at 256 chars
  to bound audit-log fan-out from hostile registrations; `Resource`
  on `agent.registered` is the agent ID (consistent with
  `tool.invoked`) with the rotating address moved to
  `Detail["address"]`. Doc-only `config/observability/audit.yaml`
  ships as a forward-looking knob template (no loader reads it in
  this PR — runtime knobs are env vars).

- **RFC 0009 PR 1 — `internal/security/` package: AuditLogger +
  SecretRedactor.** First implementation PR for RFC 0009 Phase 1a ships
  the `internal/security/` package surface with no orchestrator wiring
  (PR 1b carries the wiring): `AuditLogger` interface +
  `NewFileAuditLogger` JSONL append-only sink with checksum-chained
  tamper evidence (length-tagged `prev=<len>:<sum>|` prefix per RFC
  0009 §G), per-event fsync for security-class events, batched
  flush for telemetry-class events, and three-state startup recovery
  (`chain.bootstrap` / `chain.restart` / `chain.recovered`) addressing
  PR #232 review SF-3; `Redactor` interface + `NewSecretRedactor` with
  five default patterns (anthropic, openai, bearer, aws, generic) and
  a cycle-safe + depth-bounded reflective struct walk (PR #232 review
  SF-2); closed-set `AuditEventType` enum (20 constants, including the
  reserved chain / token / HITL types) with a CI guard
  (`TestEveryAuditEventType_HasSeverityClassification`) that fails the
  build if a new event type lands without a severity classification.
  PR #233 review applied: openai-key regex now matches real-world
  shapes (`sk-proj-AbCd_…`), generic-secret value class is bounded
  (`[^\s,"'}\]]+`) so JSON payloads no longer over-match, and the
  truncated-tail recovery path now persists the partial tail bytes as
  `prior_tail_raw_truncated`.

- **RFC 0011 PR 1 — Channel store + SQLite migration + schema rewrite.**
  `internal/channels/` is filled in (was a 7-line stub) with the
  canonical `group | dm | thread` model, a SQLite-backed
  `ChannelStore` interface (CRUD, idempotent membership, history
  pagination, `GetOrCreateDM` canonicalisation, `DeleteChannel`),
  per-channel oldest-first message-cap pruning with `thread_id`
  cascade, a global `max_channels` cap on declared group channels,
  and a YAML config loader (`config.go`) backed by a strict YAML
  decoder so legacy `direct`/`broadcast`/`meeting` schemas fail
  loudly. `schemas/channel.schema.json` is rewritten in place with
  the new vocabulary plus the "internal-only until v1.0" disclaimer
  per RFC 0011 OQ #9; `config/channels.yaml` is rewritten as a
  commented-out template against the new schema. New dependency:
  `modernc.org/sqlite` (pure-Go driver — preserves the
  `CGO_ENABLED=0` orchestrator build). REST endpoints, fanout, and
  the response gate land in subsequent RFC 0011 PRs.

### Changed

- **RFC 0020 PR 4 — Interaction summarisation is two-phase and
  asynchronous.** The persona runtime's close-path
  (`_StatePersistenceMixin._persist_closed_interaction`) now writes the
  closed-interaction episode row twice: once synchronously with the
  `[summary pending]` sentinel inside the per-agent lock, then again
  from a background `asyncio` task that runs the LLM summariser
  (≤30 s, bounded by `MemoryFacade.compress`), updates the `summary`
  column to the final text, calls `record_interaction`, and ticks the
  auto-reflect counter.  This change addresses two findings from the
  PR #229 deep review:
    - **Must-Fix #1:** the prior single-INSERT-with-final-summary path
      never wrote the `[summary pending]` sentinel from production
      code, so RFC 0020 §C's crash-recovery contract (the
      `cleanup_closing_interactions` janitor) was effectively dead
      code.  The two-phase write makes the sentinel reachable on every
      close, so the janitor now has real rows to sweep.
    - **Should-Fix #1:** the LLM round-trip no longer holds the
      per-agent `_lock`, so a second inbound event for the same agent
      no longer queues head-of-line behind the summariser.
  The runtime exposes `_LLMPersonaAgent.drain_pending_summaries()` so
  callers (tests, shutdown paths) can synchronise against the
  background tail.  `close_memory()` drains pending tasks before
  closing the underlying memory tiers.
- **RFC 0020 PR 4 — Interaction janitor wired into `on_tick`.** The
  closing-state janitor (`cleanup_closing_interactions`) is now
  invoked opportunistically from `_LLMPersonaAgent.on_tick` at most
  once per `_JANITOR_INTERVAL_SEC` (300 s by default).  Operators no
  longer need an out-of-band cron path to recover crash-stuck
  `[summary pending]` rows.  Closes PR #229 review Should-Fix #2.
- **`relationships.interaction_count` and `auto_reflect_after` units
  changed from per-message to per-closed-interaction.** A 10-message
  DM session now bumps `interaction_count` by 1 (previously by 10).
  Operators with bespoke trust thresholds calibrated against the
  per-message scale should consult the Migration Notes appendix in
  [docs/rfcs/0020-interaction-lifecycle.md](docs/rfcs/0020-interaction-lifecycle.md)
  before upgrading; RFC 0008 PR 6's 30-day calibration window is the
  canonical recovery path for production deployments.
- **BREAKING — `MemoryFacade.store_procedure` now validates `key`**
  against `^[A-Za-z0-9._-]+$` (max 256 chars) and raises `ValueError`
  on non-conforming keys (RFC 0008 PR 6b, closes PR 5 review M1).
  Previously the facade only rejected empty / whitespace-only keys,
  letting in payloads with spaces, slashes, percent-signs, non-ASCII
  characters, or newlines that could (a) confuse downstream FTS5
  tokenisation and (b) silently widen `LIKE` matches inside the
  refresh-confidence path.  Existing callers persisting procedural
  keys with disallowed characters must rename them before upgrading.
  Pinned by `tests/unit/python/test_procedural_key_validation.py`.
- **RFC 0008 PR 6b — Python procedural memory + log-safety cleanup.**
  Internal-only follow-up consolidating deferred review findings from
  PR 3a (delegation log-safety) and PR 5 (procedural decay) into the
  Python agent + memory surfaces.  No wire-shape changes.  Highlights:
  log-safety helpers (`bounded`, `_CTRL_TRANSLATION`,
  `_CTRL_REPLACEMENT`, `_DELEGATION_FAILURE_MESSAGE_CAP`) lifted from
  [`agents/sub_agents/spawner.py`](agents/sub_agents/spawner.py) into
  the new [`agents/sub_agents/_log_safety.py`](agents/sub_agents/_log_safety.py)
  single-source-of-truth module (PR 3a R4 L4 / R5 S1) — `task_agent.py`
  now imports from `_log_safety` directly; `spawner.py` re-imports
  `bounded` as the module-local `_bounded` alias so its existing call
  sites are unchanged. The underscore-prefixed names
  (`_bounded`, `_CTRL_TRANSLATION`, `_CTRL_REPLACEMENT`,
  `_DELEGATION_FAILURE_MESSAGE_CAP`) remain importable from
  `agents.sub_agents._log_safety` (listed in that module's `__all__`)
  for any out-of-tree caller pinned to the old names; they are *not*
  re-exported from `agents.sub_agents.spawner` and that import path is
  removed.
  Delegation test harness consolidated: `_ScriptedSubAgent`,
  `_FailedSubAgent`, `_MalformedSubAgent`, `boom_delete` now live in
  shared [`tests/integration/_delegation_helpers.py`](tests/integration/_delegation_helpers.py)
  (PR 3a R2 L2 / R4 L5).  `MemoryFacade.retrieve_procedures` exposes a
  new `now: float | None` parameter (deterministic-time test injection;
  PR 5 R1 L4).  `recall_procedures` now pushes the
  `t_max = -ln(c_min) / lambda_per_day` decay cutoff into the SQL
  `WHERE` clause and adds a `LIMIT` over-fetch (PR 5 R1 S3 + Info-3).
  Promoted `_resolve_base_confidence` → public
  `resolve_base_confidence` (PR 5 R1 L2 / R2 M2; legacy alias kept for
  v0.3.x).  `stale_memory_injection` warn-log relocated into
  `recall_procedures` so it fires regardless of caller (PR 5 R2 Mi2).
  `EvictionStats.procedural_evicted` no longer has a default value
  (PR 5 R2 N2 — forces explicit construction at every call site).
  New attribute-schema pin
  [`tests/integration/test_shared_pool_metrics.py`](tests/integration/test_shared_pool_metrics.py)
  closes PR 4 N4 (verifies `agent.shared_pool.{reads,writes,denied}`
  emit the documented `{pool, agent.id[, operation]}` keys).
- **RFC 0008 PR 6a — Go scheduler hygiene + sampler bookkeeping.**
  Internal-only follow-up consolidating ~22 deferred review findings from
  PR 1 / PR 1b / PR 3 / PR 4 / PR 5 into the Go scheduler & packaging
  surfaces.  No wire-shape or schema-shape changes.  Highlights:
  deterministic candidate ordering in `attachContextPackage` (sorts map
  keys before building `packaging.Candidate`s); per-run warning-sampler
  bookkeeping with `pruneRun(execID)` invoked from `executeRun`
  (eliminates unbounded growth on long-running orchestrators); `noCopy`
  sentinel on the sampler (verified by `go vet -copylocks`); rune-count
  token estimate for multibyte payloads; cross-language wire-shape
  contract pinned via Go-produced fixture
  (`tests/fixtures/context_package_v1.json`, regenerated with
  `PERSATRIX_REGEN_FIXTURES=1`) consumed by the Python wire-shape test.
  See `internal/scheduler/context_package_pr6a_pins_test.go` for the
  contract pin tests.
  - **Operator-visible behaviour change (planner-tighten, M7).** The
    planner now rejects workflows where the sum of per-step
    `context_budget` overrides plus the count of non-overridden steps
    exceeds `workflow.context_budget_total` (each non-overridden step
    must receive at least one token). Previously such workflows parsed
    and silently dispatched non-overridden steps with zero-budget legacy
    passthrough — masking author intent. Operators with tightly-budgeted
    workflows that previously parsed will see a new parse-time error of
    the form `workflow %q: per-step context_budget overrides (%d) plus
    %d non-overridden step(s) requires at least %d tokens but
    context_budget_total is %d`. Fix: raise `context_budget_total`, drop
    one or more per-step overrides, or remove `context_budget_total` to
    fall back to legacy passthrough end-to-end. Pinned by
    `TestParse_AllOverridesEqualTotal_NonOverriddenStepRejected` in
    `internal/planner/planner_context_budget_test.go`.
- **`memory.min_score` schema default `null` → `0.20`** (RFC 0008 PR 2a).
  Operators with `memory.enabled: true` who did not previously set
  `memory.min_score` will see strictly fewer recall results after this
  release: low-score entries are no longer concatenated into the system
  prompt by `BaseAgent._inject_memories`.  Rationale: closes the
  recall-side trust-boundary leak flagged by OWASP LLM01 / memory
  poisoning (PR #220 deep-review M-1).  To restore the pre-PR-221
  behaviour explicitly, set `memory.min_score: null` in
  [`config/agents.yaml`](config/agents.yaml).

### Removed

- **Deprecated underscore aliases `_DEFAULT_EPISODIC_MIN_SCORE` /
  `_DEFAULT_NOTES_MIN_SCORE` in `agents.memory.episodic`.**  The
  shim was introduced in v0.2 (RFC 0017 PR 6) with an explicit
  "remove in v0.3" deprecation banner; v0.3 is the current
  development cycle and no internal caller still references the
  underscore form.  The public names `DEFAULT_EPISODIC_MIN_SCORE` /
  `DEFAULT_NOTES_MIN_SCORE` are unchanged.  External consumers
  pinned to the underscore alias must rename to the public form.
  The corresponding back-compat test
  (`test_underscore_aliases_back_compat`) was removed.

### Added

- **`EpisodicMemory.update_episode_summary(interaction_id, summary)`**
  (RFC 0020 PR 4).  Replaces the `summary` column on a single episode
  row keyed by `(agent_id, interaction_id)`.  Used by the close-path
  two-phase write to swap the `[summary pending]` sentinel for the
  final LLM-generated text; agent-scoped `WHERE` clause prevents a
  malformed caller from rewriting a different agent's summary.
- **`_LLMPersonaAgent.drain_pending_summaries()`** (RFC 0020 PR 4).
  Awaits every in-flight background summarisation task spawned by the
  two-phase close path.  Called from `close_memory()` on shutdown;
  exposed publicly so integration tests can synchronise against the
  background tail before asserting on the final episode `summary`
  column.
- **Confidence decay + procedural revalidation** (RFC 0008 PR 5,
  Phase 4b).  The procedural memory tier now applies read-time
  exponential confidence decay (`c_t = c_0 * exp(-lambda_per_day * age_days)`,
  default `lambda = 0.01/day` ≈ 69-day half-life) so stale
  procedural knowledge naturally fades.  New surface:
  [`agents/memory/decay.py`](agents/memory/decay.py) (pure-stdlib
  formula), [`agents/memory/episodic_procedural.py`](agents/memory/episodic_procedural.py)
  (`recall_procedures` + `refresh_confidence` SQL helpers), facade
  mixin [`agents/memory/facade_procedural.py`](agents/memory/facade_procedural.py)
  exposing `MemoryFacade.store_procedure` (refreshes confidence on
  existing keys) + `MemoryFacade.retrieve_procedures` (filters below
  `c_min` and emits a `stale_memory_injection` structured log when
  decayed confidence falls in `[c_min, stale_confidence_alert_threshold)`).
  Procedural rows below `c_min` are also evicted by the periodic loop
  ([`agents/memory/eviction.py`](agents/memory/eviction.py)) via a new
  `_evict_procedural_decay` pass.  Schema migration v6 adds the
  `confidence` and `last_validated_at` columns to `episodes` (non-destructive,
  `DEFAULT 1.0` / NULL).  Operators tune the knobs via the new
  `memory.procedural_memory: {lambda_per_day, c_min, stale_confidence_alert_threshold}`
  block in [`config/agents.yaml`](config/agents.yaml) (schema:
  [`schemas/agent.schema.json`](schemas/agent.schema.json)).
  Orchestrator-side observability: seven new instruments under the
  `orchestrator.memory.*` namespace registered in
  [`internal/observability/metrics/metrics.go`](internal/observability/metrics/metrics.go)
  (`evictions_count`, `average_confidence_at_eviction`,
  `average_importance_at_eviction`, `memory_utilization_ratio`,
  `oldest_surviving_entry_age_days`, `entries_below_stale_threshold`,
  `stale_memory_injection`).  The 30-day post-merge calibration review
  required by RFC 0008 Open Question 12 is scheduled in
  [`docs/rfcs/0008-calibration-review.md`](docs/rfcs/0008-calibration-review.md);
  PR 6 (RFC close) replaces the placeholder with the actual review
  summary before flipping the RFC to `✅ Implemented`.
  Round-1 review follow-up (in-PR fix commit): escape SQLite LIKE
  meta-characters (`%`, `_`, `\`) in the procedural `recall_procedures`
  query and `refresh_confidence` UPDATE paths and align the eviction
  pass with the same legacy-row base-confidence shim
  (`_resolve_base_confidence`) the recall path uses, so a pre-PR-5
  row's eviction disposition cannot disagree with its recall
  disposition.  Closes PR #225 round-1 deep-review S1 / S2 /
  S4-doc.

- **Sub-agent delegation Go-side metrics + spawner hardening** (RFC 0008
  PR 3a, follow-up to PR 3).  Four new Go counters land in
  [`internal/observability/metrics/metrics.go`](internal/observability/metrics/metrics.go)
  under the `orchestrator.delegation.` namespace (per RFC 0019 OTEL
  naming): `merge_outcome`, `memory_writes_admitted`,
  `memory_writes_rejected`, `memory_writes_downscaled`.  These mirror the
  Python-side structured-log metrics the merge engine already emits
  (a future log→counter bridge needs a fixed-prefix translation, not
  a 1-for-1 lookup; see [`internal/observability/metrics/metrics.go`](internal/observability/metrics/metrics.go)
  comment).  Spawner-side hardening lands alongside the counters:
  (a) **S1** — `output_schema` is no longer advisory; the spawner now
  runs Draft-7 validation against `DelegationResult.artifacts` before
  the merge engine sees it (OWASP A04); (b) **S6** —
  `DelegationResult.from_metadata_value` re-runs `validate()` on
  deserialisation; (c) **N5** — `FacadeBoundSpawner._persist_admitted`
  rolls back partial-batch failure via `episodic.delete_episode` so a
  mid-batch crash cannot leave orphaned writes; (d) **N6** —
  `_parse_or_synthesise` collapsed into a single contract-parser call;
  (e) **N7** — request payload serialised exactly once (the per-field
  `output_schema` size check is subsumed by the whole-payload cap).
  All `DelegationFailure` raise sites that interpolate
  attacker-influenceable text now funnel through a module-private
  `_bounded` helper (200-char cap, control-character strip to U+2424
  sentinel) to neutralise CWE-117 log injection / OWASP A09.  No proto
  / wire change.
- **Shared memory pools — config-driven cross-agent pools with ACL +
  provenance** (RFC 0008 §H, PR 4).  New `agents.memory.shared_pool`
  module ships `SharedMemoryPool`, `SharedPoolEntry`, `SharedPoolConfig`,
  `SharedPoolRegistry`, and `SharedMemoryPermissionError` (with
  structured `reason` taxonomy).  `MemoryFacade` gains
  `publish_to_pool` / `read_from_pool` for the curated isolated→shared
  publish path; deny-by-default reader/writer ACLs declared under a new
  top-level `shared_memory_pools:` section in
  [`config/agents.yaml`](config/agents.yaml) (schema:
  [`schemas/agent.schema.json`](schemas/agent.schema.json) `shared_memory_pool`).
  Provenance (`source_agent`, `created_at`, `confidence`) is
  framework-injected and bound 1-for-1 to the calling `agent_id` so an
  in-process caller cannot spoof it.  `min_confidence` consumer-side
  trust filter, FIFO eviction at `max_entries`, sensitive-pool
  publish-isolation per RFC §H safety constraint #3, and OTEL counters
  (`agent.shared_pool.{reads,writes,denied,evictions}`) round out the
  surface.  No proto / wire change.  Persona-side wiring is partial in
  Phase 4a — the `agents/persona_runtime/state_persistence.py` path
  accepts a `shared_pools` arg but does not yet expose
  `publish_to_pool` / `read_from_pool` to persona prompts (follow-on PR).
- **Sub-agent delegation contract + merge engine** (RFC 0008 §E, PR 3).
  New `agents.sub_agents.delegation` module ships
  `DelegationRequest` / `DelegationResult` / `MemoryWriteEntry` /
  `BudgetEnvelope` frozen dataclasses on the reserved
  `_delegation_request` / `_delegation_result` `TaskInput.context` /
  `TaskOutput.metadata` keys (no proto changes).  New
  `agents.sub_agents.merge.MergeEngine` applies the deterministic 6-step
  pipeline (schema → source-agent inject → cap → trust-ceiling
  downscale → per-entry strategy → metrics) with strategies `replace`,
  `append`, `patch` (RFC 7396 JSON Merge Patch on objects, tag-list
  union for arrays, replace-for-strings on scalars), and
  `reject_on_conflict`.  Procedural tier is intentionally excluded from
  delegated writes (dedicated `procedural_tier_rejected` reason) — see
  [RFC 0008 PR 3 plan](docs/rfcs/0008-pr-plan.md) Key implementation
  details.  In-process `SubAgentSpawner` + `FacadeBoundSpawner` exercise
  the contract end-to-end without sub-process isolation (full RFC 0009
  isolation lands later).  `TaskAgent` now auto-emits a synthesised
  `DelegationResult` envelope when invoked under a delegation request.
  Per-rejection structured-log metric emission (`delegation_merge_outcome`,
  `delegation_memory_writes_admitted`, `delegation_memory_writes_rejected`);
  Go counter back-fill ships in the follow-on
  `feature/v030-rfc0008-delegation-metrics` PR per the PR 3 sizing-risk
  note.
- **Episodic-tier eviction** (RFC 0008 §G, PR 2a).  TTL eviction of
  low-importance entries (`importance < 0.3`, default 30-day window) and
  hybrid-score size-cap eviction (`importance · 0.6 + recency · 0.3 +
  access · 0.1`, default cap 1000) run on a per-agent background loop
  (default cadence 1 hour).  Procedural-tier rows are excluded so
  PR 5 owns confidence decay end-to-end.  New config keys:
  `memory.episodic_cap`, `memory.ttl_low_importance_days`,
  `memory.eviction_cadence_seconds`.

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


