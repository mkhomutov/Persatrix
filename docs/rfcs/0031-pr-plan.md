# RFC 0031 — PR Implementation Plan (Phase 1 — v0.3.1 scope)

**RFC**: [0031-per-session-namespacing-channels.md](0031-per-session-namespacing-channels.md)
**Created**: 2026-05-12
**Branch prefix**: `feature/v031-rfc0031p1-`
**Target**: `main`
**Merge strategy**: Squash merge per [BRANCHING.md](../BRANCHING.md)
**Master plan**: [v0.3.1-plan.md Phase 1 (combined plans PR)](../v0.3.1-plan.md#phase-1--author-the-two-rfc-pr-plans)

---

## Overview

RFC 0031 introduces **Session** as a first-class operator-visible namespace and tags channels + persona-memory rows with it. The RFC spans four phases; **only Phase 1 lands in v0.3.1** (sessions table, `session_id` columns on four tables, env-var plumbing — no CLI yet, no recall filtering yet). Phases 2–4 are reserved for v0.3.x patches — see [§Future Phases](#future-phases).

Phase 1 splits into **5 PRs**. The first carries an RFC 0016 wire-field rename that resolves [RFC 0031 OQ #8](0031-per-session-namespacing-channels.md#open-questions); the next two land orchestrator-side and persona-side migrations; the last two are review follow-ups + Phase 1 closeout.

**Prerequisite**: v0.3.0 merged (✅ — released 2026-05-12).

### Open-question resolutions locked at plan-authoring time

[v0.3.1-plan Phase 0 acceptance](../v0.3.1-plan.md#phase-0--this-pr) names these as hard gates before Phase 1 can ship its column / env-var names.

- **[OQ #1](0031-per-session-namespacing-channels.md#open-questions) — default-recall semantics: resolution 1a.** Single-session default. Dementia-test author pins one `session_id` across the arc via `PERSATRIX_SESSION_ID` (Phase 1) or `persatrix session use <arc>` (Phase 3). Matches §D pseudocode (`sessions = [self._active_session_id]`). Phase 1 lands no recall changes; the resolution is informational here and load-bearing in Phase 2.
- **[OQ #8](0031-per-session-namespacing-channels.md#open-questions) — wire-level name collision against RFC 0016 `ChatRequest.session_id`: resolution 8b.** Rename RFC 0016's wire field to `chat_session_id`. PR 1 below carries the rename + RFC 0016 amendment. After PR 1, every `session_id` in the codebase refers to RFC 0031's operator-namespace; RFC 0016's chat-conversation id is explicitly `chat_session_id`.

### Sequencing

**Recommended merge order**: **PR 1 → PR 2 → PR 3 → PR 4 → PR 5**. PR 2 (Go-side migrations) must merge before PR 3 (Python-side) so the cross-process integration test in PR 3 exercises a coherent env-var contract end-to-end.

This plan is the load-bearing sequencing input for the RFC 0026 PR plan — see [RFC 0026 PR plan dependency graph](0026-pr-plan.md#dependency-graph). RFC 0026 PR 1 cannot open until PR 3 below has merged.

---

## Dependency Graph

```
PR 1 (RFC 0016 wire rename: ChatRequest/ChatResponse.session_id → chat_session_id)
  ↓
PR 2 (Go: sessions table + session_id columns on channels/messages + orchestrator env-var threading)
  ↓
PR 3 (Python: session_id columns on episodes/relationships + persona-runtime env-var + cross-process integration)
  ↓
PR 4 (Review follow-ups)
  ↓
PR 5 (Phase 1 closeout — status: ⚠️ Partially Implemented)
```

PR 1 is logically independent of PRs 2–3 (renaming `ChatRequest.session_id` does not require Phase 1's sessions table), but it ships first so every later PR uses `session_id` to mean exactly one thing.

---

## PR Sequence

### PR 1: `feature/v031-rfc0031p1-chat-session-rename` — RFC 0016 Wire-Field Rename

**Depends on**: Nothing (v0.3.0 baseline).
**Purpose**: Resolve [RFC 0031 OQ #8](0031-per-session-namespacing-channels.md#open-questions) by renaming RFC 0016's wire field to `chat_session_id`, so the operator-namespace `session_id` introduced by Phase 1 has an unambiguous identifier across protos, structured logs (RFC 0018), OTEL spans (RFC 0019), and the future `MemoryStore` facade signature (RFC 0029 P1).

#### Scope

| File | Change |
|------|--------|
| [`proto/task.proto`](../../proto/task.proto) | Rename `ChatRequest.session_id` (field 4) → `chat_session_id`; rename `ChatResponse.session_id` (field 2) → `chat_session_id`. Field *numbers* unchanged (binary-compatible); field *names* change (proto3 JSON / proto-text break). Update the inline comment referring to `ChatRequest.session_id` bound (line 150 area). |
| `agents/generated/task_pb2.py`, `agents/generated/task_pb2.pyi` | Auto-regenerated via `make proto`. Verify the regen produces only field-name changes; no manual edits. |
| [`agents/server_servicers.py`](../../agents/server_servicers.py) | Rename every `request.session_id` / `session_id=` on the `ChatRequest` / `ChatResponse` boundary to `chat_session_id` (lines 221–338). The local `session_id` variable name stays unprefixed — it holds RFC 0016's chat-session id within the servicer's scope; no ambiguity at the function level. The interaction-metadata write at line 276 also renames the metadata key from `"session_id"` to `"chat_session_id"`. |
| [`cli/src/types.rs`](../../cli/src/types.rs) | Rename `session_id` field on the `ChatRequest` / `ChatResponse` struct definitions (lines 74, 81). Update every `"session_id"` JSON literal in the test fixtures (lines 270, 276, 284, 292, 301, 319, 344, 360, 373). |
| [`cli/src/commands/chat.rs`](../../cli/src/commands/chat.rs) | Rename `chat_resp.session_id` accesses (lines 170–171, 91). The local `session_id: String` variable (line 57) stays unprefixed for the same reason as the Python servicer. This is the **only** Rust call site that builds `ChatRequest` — [`cli/src/main.rs`](../../cli/src/main.rs) is pure clap dispatch and does not touch the chat wire fields. |
| [`docs/rfcs/0016-human-participant-chat-interface.md`](0016-human-participant-chat-interface.md) | **Amendment block** at the bottom: "2026-05-12 — wire-field rename to `chat_session_id` (carried by RFC 0031 PR 1). Resolves RFC 0031 OQ #8." Cite RFC 0031 §A "Distinct from RFC 0016" table for context. RFC 0016 status row stays `implemented`; the amendment is additive context. |
| [`docs/rfcs/0011-channels-bridges.md`](0011-channels-bridges.md) | Update the reference to `ChatRequest.session_id` cited from `task.proto:150` (M1 deep-review comment). |
| `CHANGELOG.md` (Unreleased) | `[Breaking]` entry under `Upgrade Notes`: chat REST/JSON consumers sending `"session_id"` must switch to `"chat_session_id"` as of v0.3.1. Binary proto consumers are unaffected (field numbers preserved). |
| `tests/integration/test_chat_endpoint.py`, `tests/unit/python/test_grpc_server.py`, `cli/src/types.rs::tests` | Update fixtures and assertions to the new field name. |

#### Key implementation details

- Wire-binary compatibility holds (proto field numbers are unchanged); JSON / proto-text consumers break. CHANGELOG `[Breaking]` Upgrade Note ships in the v0.3.1 release notes under [v0.3.1 release-prep Phase 3](../v0.3.1-plan.md#phase-3--v031-release-prep-plan).
- Local variable names inside servicer / chat-command bodies stay `session_id` — they hold RFC 0016's chat-session id within their function scope. The discipline is: any *boundary* (wire field, JSON key, log key, span attribute, metadata key, public function kwarg) for RFC 0016's chat session uses `chat_session_id`. Function-local variables remain idiomatic.
- The interaction-metadata key rename at `agents/server_servicers.py:276` keeps the rule simple. Audit during PR review whether any read site consumes the old `"session_id"` key — current grep shows the key is observability-only, so renaming is safe; if a reader is found, the read path takes both keys with `chat_session_id` preferred and the old key as fallback (deprecation breadcrumb, removed in v0.4.0).

#### Tests

- `ChatRequest` with `chat_session_id="abc"` → `ChatResponse` carries `chat_session_id="abc"` (or a server-generated UUID on empty input).
- `ChatRequest` sent with legacy `session_id` JSON key — confirm proto3 JSON parse produces an `INVALID_ARGUMENT` or silent zero-value default, depending on parser config. Whichever it is, pin it in a regression test so a future parser upgrade does not change the failure mode silently.
- Existing chat end-to-end tests pass after fixture rename.

#### PR checklist

- [ ] `proto/task.proto` renames applied; field numbers unchanged.
- [ ] `make proto` regen committed; diff is field-name-only.
- [ ] `make test` + `cargo test --manifest-path cli/Cargo.toml` pass.
- [ ] `ruff check agents/` clean; `mypy agents/` clean; `cargo clippy` clean.
- [ ] [RFC 0016 row in ROADMAP](../../ROADMAP.md#rfc-master-index) carries an "Amended 2026-05-12 (RFC 0031 §OQ8)" footnote.
- [ ] CHANGELOG `Unreleased` has the `[Breaking]` Upgrade Note.
- [ ] Grep audit: every `session_id` in code post-rename falls into exactly one of three buckets — (a) RFC 0031's operator-namespace identifier (introduced in PR 2 / PR 3), (b) a function-local variable inside a chat-handler scope, or (c) an unrelated identifier whose semantics are neither chat nor RFC-0031 (e.g. [`cli/src/main.rs::Commands::Replay { session_id }`](../../cli/src/main.rs) is the workflow-replay session id, a stub command at this writing — it MUST NOT be renamed to `chat_session_id`). PR 1 reviewer confirms no chat-wire boundary still carries the unprefixed name.

---

### PR 2: `feature/v031-rfc0031p1-sessions-table-go` — Sessions Table + Orchestrator Migrations + Env-Var Threading

**Depends on**: PR 1 merged.
**Purpose**: Land the `sessions` table and add `session_id` columns to `channels` and `messages` on the Go (orchestrator) side. Thread `PERSATRIX_SESSION_ID` from boot through `ChannelStore.CreateChannel` and `PublishMessage`.

#### Scope

| File | Change |
|------|--------|
| [`internal/channels/sqlite_schema.go`](../../internal/channels/sqlite_schema.go) | New migration step (v2 → v3 under `channelStoreSchemaVersion`): `CREATE TABLE sessions (id TEXT PRIMARY KEY, label TEXT, created_at REAL NOT NULL, archived_at REAL, metadata_json TEXT)`. `ALTER TABLE channels ADD COLUMN session_id TEXT NOT NULL DEFAULT 'legacy'`. Same for `messages`. Replace `idx_messages_channel_ts` with covering `idx_messages_channel_session(channel_id, session_id, timestamp DESC)` per §C. Add `idx_channels_session`. |
| [`internal/channels/sqlite.go`](../../internal/channels/sqlite.go), [`internal/channels/channels.go`](../../internal/channels/channels.go) | `ChannelStore.CreateChannel` and `PublishMessage` accept and persist `session_id`. Default to `"legacy"` if caller passes empty. |
| [`internal/channels/router.go`](../../internal/channels/router.go), [`internal/server/channel_handlers.go`](../../internal/server/channel_handlers.go) | Thread `session_id` from request context to the store. The orchestrator reads the env var at boot and uses it as the per-process default; per-request override (Phase 3 `--session` flag) is out of scope here. |
| [`cmd/orchestrator/main.go`](../../cmd/orchestrator/main.go) (boot site) | `os.Getenv("PERSATRIX_SESSION_ID")` at boot — co-located with the existing `os.Getenv(zapenc.PrettyEnvVar)` read at `main.go:80` (the established orchestrator env-var read site; `internal/server` has no `main.go` — the binary entry point is here under `cmd/orchestrator/`). If empty, log `INFO "PERSATRIX_SESSION_ID unset, defaulting to 'legacy'"` and use `"legacy"`. |
| `internal/channels/*_test.go` | Unit: `CreateChannel(session_id="abc")` persists the value; default `"legacy"` applies on empty; `PublishMessage` inherits from the channel row; covering-index regression (chronological scans still cheap). |
| [`internal/observability/metrics/metrics.go`](../../internal/observability/metrics/metrics.go) | New counter `sessions.writes` with `session_id` attribute. Lands in the existing `metrics` subpackage alongside the per-domain `audit_instruments.go` / `channel_instruments.go` files (a `session_instruments.go` companion is acceptable if the cluster grows). Low-cardinality only — Phase 1 inserts the env-var value verbatim, so cardinality is bounded by operator-controlled session count. |

#### Key implementation details

- The new `sessions` table is created empty in PR 2 — no seed row for `legacy`. §D's WHERE clause treats `session_id = 'legacy'` as a synthetic carve-out (per [OQ #2](0031-per-session-namespacing-channels.md#open-questions) default proposal (a) — reserve `legacy` as an identifier at session-creation time, deferred to Phase 3 CLI).
- Migration is forward-only and idempotent. SQLite supports `ALTER TABLE ... ADD COLUMN session_id TEXT NOT NULL DEFAULT 'legacy'` with a constant default since 3.20.0 (no backfill UPDATE).
- The covering-index replacement preserves the chronological-scan shape (`channel_id` prefix, `timestamp DESC` tail). Migration drops the old index and creates the new one in the same step.
- An env-var value containing characters outside `[A-Za-z0-9_-]` emits a `WARN` log at boot. No rejection — Phase 3 CLI's `persatrix session new` owns validation; Phase 1 plumbing treats the value as opaque storage.

#### Tests

- Schema migration on a fresh DB: table + columns + indexes present.
- Idempotence: running the migration twice produces no diff.
- `CreateChannel` writes `session_id`; `PublishMessage` carries it.
- Legacy carve-out shape (the read Phase 2 will run): existing rows with `session_id='legacy'` are returned by a query with `WHERE session_id IN ('run-a') OR session_id='legacy'`.
- Counter `sessions.writes{session_id="abc"}` increments on `CreateChannel` and `PublishMessage`.

#### PR checklist

- [ ] `go test ./internal/channels/... ./internal/server/...` passes.
- [ ] `go vet ./...` clean.
- [ ] Schema version bumped; migration tested against a legacy v2 DB fixture.
- [ ] Boot log emits the default-fallback line when env unset.
- [ ] [RFC 0031 row in ROADMAP](../../ROADMAP.md#rfc-master-index) → `🚧 Implementing` on this PR opening.
- [ ] [v0.3.1-plan Master Progress Overview](../v0.3.1-plan.md#master-progress-overview) row 3 → 🔄 In progress.

---

### PR 3: `feature/v031-rfc0031p1-sessions-py` — Python Migrations + Persona-Runtime Threading + Cross-Process Integration

**Depends on**: PR 2 merged.
**Purpose**: Mirror PR 2 on the Python side. Add `session_id` columns to `episodes` and `relationships`; thread `PERSATRIX_SESSION_ID` through `EpisodicMemory.store_episode` and `RelationshipMemory.record_interaction`. Pin the cross-process contract.

#### Scope

| File | Change |
|------|--------|
| [`agents/memory/_migration_handlers.py`](../../agents/memory/_migration_handlers.py) | New handler `_apply_migration_<N>` (version assigned at PR-author time): `ALTER TABLE episodes ADD COLUMN session_id TEXT NOT NULL DEFAULT 'legacy'`; `CREATE INDEX idx_episodes_session ON episodes(session_id)`. Same for `relationships` with `idx_rel_session`. No-op early-return guard if either table is missing (RFC 0020 PR 6 finding #4 precedent). |
| [`agents/memory/migrations.py`](../../agents/memory/migrations.py) | Wire the new handler into the umbrella runner. |
| [`agents/memory/episodic.py`](../../agents/memory/episodic.py) | `store_episode` accepts `session_id: str = "legacy"` kwarg, persists it. No recall-side filtering yet (Phase 2). |
| [`agents/memory/relationship_mutations.py`](../../agents/memory/relationship_mutations.py) | `record_interaction` accepts `session_id: str = "legacy"` kwarg, persists it. |
| [`agents/persona_runtime/__init__.py`](../../agents/persona_runtime/__init__.py) | Read `os.environ.get("PERSATRIX_SESSION_ID", "legacy")` at agent construction; thread to `EpisodicMemory.store_episode` and `RelationshipMemory.record_interaction` at every call site. Same boot-log message as PR 2. |
| `tests/unit/python/test_session_id_migration.py` | **New** — fresh + upgrade migration paths; idempotence; no-op early-return. |
| `tests/unit/python/test_episodic_memory.py`, `tests/unit/python/test_relationship_memory.py` | Add `session_id` round-trip cases on store paths; default `"legacy"` semantics. |
| `tests/integration/test_session_id_cross_process.py` | **New** — orchestrator + persona runtime started under `PERSATRIX_SESSION_ID=run-a`, write a channel + message + persona episode; restart both under `PERSATRIX_SESSION_ID=run-b`; verify at the storage layer that both sets of rows exist, each tagged with its own session_id. (Recall-side filtering ships in Phase 2 — this test asserts the *write* contract only.) |
| `docs/manual-tests/MT-SESSION-001.md` | **New** (deliverable of this PR) — per [v0.3.1-plan Phase 2 cross-cutting acceptance](../v0.3.1-plan.md#phase-2--implement-the-two-rfcs): two stack starts under different `PERSATRIX_SESSION_ID` values; assert via raw SQLite query that the second run's rows carry the new session_id and the first run's are untouched at the storage layer. Execution lives in [Phase 4 PR 1](../v0.3.1-plan.md#phase-4--v031-release-prep-execution). |

#### Key implementation details

- `session_id` flows as a kwarg, not as a thread-local / context-var. The persona runtime reads the env at construction and threads through. Reasoning: cross-async-task safety, no surprising rebind on container restart, matches `agent_id` plumbing precedent.
- The migration handler version is assigned at PR-author time so it linearises cleanly after any unrelated parallel RFC's handler. Umbrella `schema_version` table catches collision as a CI failure.
- Phase 1 ships no facade or recall changes. The kwarg additions are purely additive — existing call sites that omit the kwarg get the `"legacy"` default. No call-site sweep is required at storage-API consumers.

#### Tests

- Migration idempotence (fresh + upgrade matrix); no-op early-return on missing table.
- Default `"legacy"` round-trip on both stores.
- Cross-process: row counts at the storage layer match per-session writes; no carryover at the write path.
- Boot log emits the same default-fallback line as PR 2 when env unset.
- Telemetry counter `agents.sessions.writes{session_id}` increments on store / record paths.

#### PR checklist

- [ ] `make test` passes; `make lint` clean.
- [ ] Migration tested against a legacy DB fixture frozen at the pre-RFC-0031 schema version.
- [ ] Cross-process integration test runs in CI under the existing `make integration-test` target.
- [ ] `docs/manual-tests/MT-SESSION-001.md` authored as part of this PR; execution deferred to Phase 4 PR 1.
- [ ] Boot log emits default-fallback line on env unset.
- [ ] [RFC 0026 PR plan PR 1](0026-pr-plan.md#pr-1-featurev031-rfc0026-facts-schema-store--facts-schema--factstore--erasure-primitive) is now unblocked (the column-convention is pinned).

---

### PR 4: `feature/v031-rfc0031p1-followups` — Review Follow-Ups

**Depends on**: PR 3 merged.
**Purpose**: Address review findings surfaced during PRs 1–3 review. Follows the [RFC 0017 PR plan §PR 6 precedent](0017-pr-plan.md) — "From PR N review" subsections, each finding paraphrased inline (no link to local review reports per [.github/copilot-instructions.md](../../.github/copilot-instructions.md)).

#### Scope

Items below are populated as PRs are reviewed. Per [.github/copilot-instructions.md](../../.github/copilot-instructions.md) ("PR review reports are local-only artifacts"), each entry paraphrases the finding inline and does **not** reference or link any local PR review report.

##### From PR 1 review

_None recorded at plan-update time. Add findings here if surfaced post-merge._

##### From PR 2 review

_None recorded at plan-update time. Add findings here if surfaced post-merge._

##### From PR 3 review

1. **Three persona-reachable `store_episode` / write surfaces bypass `_session_id` threading**
   ([agents/memory/facade.py:361](../../agents/memory/facade.py#L361),
   [agents/memory/facade_procedural.py:155](../../agents/memory/facade_procedural.py#L155),
   [agents/memory/shared_pool.py:328](../../agents/memory/shared_pool.py#L328)). PR 3 threads
   `_session_id` through the two persona-runtime entry points
   ([episode_routing.py](../../agents/persona_runtime/episode_routing.py),
   [summarize_close.py](../../agents/persona_runtime/summarize_close.py)) but three additional
   surfaces reach `EpisodicMemory.store_episode` without a `session_id` kwarg —
   `MemoryFacade.publish` (RFC 0008 §B / RFC 0029 P1 write path), `ProceduralFacadeMixin.publish_procedural`
   (RFC 0008 PR 5 procedural tier), and `SharedMemoryPool.write` (RFC 0023 shared pool). Each defaults
   to `"legacy"`, so a persona running under `PERSATRIX_SESSION_ID=run-a` that publishes via the
   facade or shared pool lands its rows tagged `legacy`, not `run-a`. PR 3's cross-process
   integration test does not catch this because it calls `EpisodicMemory.store_episode` directly.
   Invisible at write time (Phase 1 ships no recall filter); becomes a silent recall miss the day
   Phase 2's filter lands — the exact dementia-test failure mode the carve-out exists to prevent.
   Fix: add `session_id` pass-through on each of the three surfaces and thread the persona's
   `_session_id` through. Audit during this PR whether `BaseAgent`-derived task agents constructing
   their own `MemoryFacade` at [agents/base.py:184](../../agents/base.py#L184) are intentionally
   out-of-Phase-1-scope; if so, pin that deferral in this PR or in the RFC, not in silence.

2. **`seed_trust` writes the legacy carve-out, then `record_interaction` cannot overwrite it**
   ([agents/memory/relationship_mutations.py:264](../../agents/memory/relationship_mutations.py#L264-L304)).
   The YAML-config seed path uses `INSERT OR IGNORE` without `session_id`, so seeded rows take the
   `'legacy'` column default. The first real `record_interaction` under `PERSATRIX_SESSION_ID=run-a`
   then hits the `ON CONFLICT DO UPDATE` branch — which deliberately omits `session_id` to preserve
   the first-seen-wins contract verified at
   [relationship_mutations.py:236](../../agents/memory/relationship_mutations.py#L236) — and the
   tag stays `'legacy'`. The
   [MT-SESSION-001 Step 7 expectation](../manual-tests/MT-SESSION-001.md) ("Relationships row
   `session_id` is `run-a`") would fail in this seed-first scenario for any persona that
   pre-declares the peer in its `relationships:` config block. PR 3's
   `test_second_interaction_does_not_overwrite_session_id` catches the deliberate-preservation
   contract but does not probe the seed-before-record sequence. Fix: either (a) thread
   `session_id` through `seed_trust` and the `RelationshipMemory.initialize(config_relationships=...)`
   boundary — the persona knows its `_session_id` at the same construction time it runs seeding;
   or (b) explicitly amend RFC 0031 / MT-SESSION-001 to document that config-seeded rows always
   carry `'legacy'` as the intended first-seen baseline. Current behaviour is underspecified.

3. **Python side does not WARN on non-canonical `PERSATRIX_SESSION_ID`; Go side does**
   ([agents/persona_runtime/session_id.py](../../agents/persona_runtime/session_id.py)). The
   Go-side [cmd/orchestrator/startup.go::resolveSessionID](../../cmd/orchestrator/startup.go) emits
   a `WARN` log when the env var contains characters outside `[A-Za-z0-9_-]` (regression test:
   `cmd/orchestrator/session_env_test.go::TestResolveSessionID_InvalidCharsWarnsButAccepts`); the
   Python `resolve_session_id_and_log` accepts any non-empty value verbatim and stays silent.
   [MT-SESSION-001 Edge Case 1](../manual-tests/MT-SESSION-001.md) acknowledges the gap as
   "intentional, since the canonical surface for operator-facing validation is the CLI (Phase 3)" —
   defensible, but the asymmetry is operator-visible today: setting
   `PERSATRIX_SESSION_ID="my session"` across both shells produces one binary that warns and one
   that does not, and an operator may reasonably conclude the persona side is happy with the
   value. Fix: add a `WARN` log to `resolve_session_id_and_log` using the same `[A-Za-z0-9_-]`
   regex and the same canonical message string as the Go side, so operators grep for one phrase
   across both logs.

4. **`agents.sessions.writes{session_id}` telemetry counter promised in PR 3 plan was not shipped**
   (no symbol in `agents/`). PR 3's `#### Tests` last bullet committed to "Telemetry counter
   `agents.sessions.writes{session_id}` increments on store / record paths" (line 171 of this
   plan); grep shows zero matches for any `sessions.writes` / `SessionsWrites` identifier across
   `agents/`. The Go side ships an equivalent counter at
   [internal/observability/metrics/channel_instruments.go](../../internal/observability/metrics/channel_instruments.go).
   Without the Python half, an operator cannot validate end-to-end that the env var flows through
   to disk without running the manual MT-SESSION-001 raw-SQLite asserts. Fix: add the counter via
   [agents/observability/metrics.py](../../agents/observability/metrics.py) and increment once per
   `store_episode` and once per `record_interaction`. Cardinality is bounded by the
   operator-controlled session count — the same argument PR 2 §Key implementation details makes
   for the Go counter.

5. **Doc-string drift on the Python env reader's location** (three sites). The PR 3 docs name
   the env reader at the wrong place in three places:
   (a) [agents/persona_runtime/episode_routing.py:86](../../agents/persona_runtime/episode_routing.py#L86)
   cites `_LLMPersonaAgent.__init__` — the read is actually in `PersonaAgent.__init__` at
   [agents/persona.py:125](../../agents/persona.py#L125);
   (b) [docs/manual-tests/MT-SESSION-001.md:69](../manual-tests/MT-SESSION-001.md#L69) cites
   `agents/persona_runtime/__init__.py — _resolve_session_id` — the module is
   [agents/persona_runtime/session_id.py](../../agents/persona_runtime/session_id.py) and the
   function is `resolve_session_id_and_log`;
   (c) [docs/manual-tests/MT-SESSION-001.md:111](../manual-tests/MT-SESSION-001.md#L111) invokes
   `python -m agents.persona_runtime_main` — no such module exists; the actual persona entry
   point is `python -m persatrix_agents.server` (per
   [Dockerfile.agent:32](../../Dockerfile.agent#L32) and
   [.github/CLAUDE.md:57](../../.github/CLAUDE.md#L57)). An operator following MT-SESSION-001
   step-by-step today gets `ModuleNotFoundError`. Fix: three one-line doc edits.

#### PR checklist

- [ ] All deferred review findings addressed or downgraded to tracked issues with rationale.
- [ ] `make test` + `make lint` clean.
- [ ] MT-SESSION-001 re-run after items 2 and 5 land; Step 7 and the entry-point invocation succeed without operator intervention.

---

### PR 5: `feature/v031-rfc0031p1-close` — Phase 1 Closeout

**Depends on**: PR 4 merged.
**Purpose**: Mark Phase 1 implemented and hand off to v0.3.x for Phases 2–4.

#### Scope

| File | Change |
|------|--------|
| [`docs/rfcs/0031-per-session-namespacing-channels.md`](0031-per-session-namespacing-channels.md) | Status → `⚠️ Partially Implemented (Phase 1)`. Append "Phase 1 implemented in v0.3.1" note to Decision/Next Steps. |
| [`ROADMAP.md`](../../ROADMAP.md) | RFC 0031 row → `⚠️ Partially Implemented`; target column `v0.3.1 (Phase 1) + v0.3.x (Phases 2–4)`. `Last updated` refresh. |
| [`docs/rfcs/0031-pr-plan.md`](0031-pr-plan.md) | [Progress Overview](#progress-overview-phase-1) rows filled with merged-PR numbers and dates. |

No code changes; doc-only.

#### PR checklist

- [ ] RFC 0031 status = `⚠️ Partially Implemented`.
- [ ] [ROADMAP RFC Master Index](../../ROADMAP.md#rfc-master-index) updated.
- [ ] [v0.3.1-plan Master Progress Overview](../v0.3.1-plan.md#master-progress-overview) row 3 → ✅.

---

## Risk and Mitigations

| Risk | Mitigation |
|------|------------|
| PR 1's RFC 0016 rename breaks an undocumented JSON consumer (third-party chat client) post-tag. | CHANGELOG `[Breaking]` Upgrade Note ships in the v0.3.1 release notes. Field numbers preserved so binary-proto consumers are unaffected. Internal CLI / REST audits land in PR 1. |
| PR 2 and PR 3 land out of order — a persona-runtime container under `PERSATRIX_SESSION_ID=run-a` writes against an orchestrator still defaulting to `legacy`. | Strict merge ordering enforced by the dependency graph. PR 3's cross-process integration test fails-closed against partial deployment because the assertion converges on a shared row. |
| Migration version collision with an unrelated parallel RFC's migration. | The version number is assigned at PR-author time. The umbrella `schema_version` table linearises ordering; collision surfaces as a CI failure on `make test`. |
| `session_id = 'legacy'` synthetic carve-out hides downstream recall bugs because every read sees it by default. | Phase 1 ships no recall changes — the risk surfaces in Phase 2, where the carve-out becomes a tested invariant. Flagged in [v0.3.1-plan §Risk and mitigations](../v0.3.1-plan.md#risk-and-mitigations). |
| Operator misconfiguration: `PERSATRIX_SESSION_ID=my session with spaces` propagates to logs / counters. | PR 2's WARN-log at boot for characters outside `[A-Za-z0-9_-]`. Hard validation lives in Phase 3 CLI's `persatrix session new`. |
| RFC 0026 PR plan PR 1 starts before this plan's PR 3 merges. | Hard cross-RFC sequencing pinned at the top of [RFC 0026 PR plan dependency graph](0026-pr-plan.md#dependency-graph). Reviewers reject RFC 0026 PR 1 if this plan's PR 3 is not on `main`. |
| OQ #1's "1a" resolution becomes load-bearing in Phase 2 but only informational here — a future plan author might re-litigate. | This plan's [§Open-question resolutions](#open-question-resolutions-locked-at-plan-authoring-time) records the resolution; Phase 2's plan (out of scope here) inherits it via the [§Future Phases](#future-phases) §Phase 2 entry. |

---

## ROADMAP Hygiene

Per [.github/copilot-instructions.md](../../.github/copilot-instructions.md) "Status Hygiene" and [v0.3.1-plan §ROADMAP hygiene](../v0.3.1-plan.md#roadmap-hygiene):

- **PR 1 opens** → no RFC 0031 status change; RFC 0016 row carries an "Amended" footnote.
- **PR 2 opens** → RFC 0031 row → `🚧 Implementing`; [v0.3.1-plan Master Progress Overview](../v0.3.1-plan.md#master-progress-overview) row 3 → 🔄 In progress.
- **Each PR merges** → fill the [Progress Overview](#progress-overview-phase-1) row.
- **PR 5 merges** → RFC 0031 row → `⚠️ Partially Implemented`; master-plan row 3 → ✅; `Last updated` refresh.

---

## Future Phases

Reserved for v0.3.x patches beyond v0.3.1. Out of scope for this plan; tracking notes only.

### Phase 2 — Recall Filtering + Dementia-Test Bridge

Default recall becomes session-scoped per OQ #1 resolution 1a. Cross-session recall lands as an explicit `sessions=[…]` parameter. Dementia-test ([MT-MEMORY-005](../manual-tests/MT-MEMORY-005-dementia-test.md)) updated to exercise multi-session continuity. Phase 2 PR plan opens only after this Phase 1 plan closes and the v0.3.1 release tag cuts.

### Phase 3 — Operator CLI

`persatrix session new / use / list / archive / current`. Active-session pointer at `~/.persatrix/active-session` plus `PERSATRIX_ACTIVE_SESSION_FILE` override. `--session` flag on `persatrix chat` / `persatrix channel publish` / `persatrix channel list`. `persatrix session new --label legacy` rejected per OQ #2 resolution.

### Phase 4 — Operator Documentation Pass

New `docs/guides/sessions.md`. Close [ISSUE-0051](../issues/ISSUE-0051-per-session-memory-namespacing-channels.md). Scope (not implement) `persatrix memory legacy-prune`.

---

## Progress Overview (Phase 1)

| # | Title | Branch | Status | GitHub PR | Merged |
|---|-------|--------|--------|-----------|--------|
| 1 | RFC 0016 wire-field rename | `feature/v031-rfc0031p1-chat-session-rename` | ✅ Merged | [#333](https://github.com/mkhomutov/Persatrix/pull/333) | 2026-05-13 |
| 2 | Sessions table + Go migrations + orchestrator env-var | `feature/v031-rfc0031p1-sessions-table-go` | ✅ Merged | [#335](https://github.com/mkhomutov/Persatrix/pull/335) | 2026-05-13 |
| 3 | Python migrations + persona-runtime env-var + cross-process integration | `feature/v031-rfc0031p1-sessions-py` | 🔀 PR open | — | — |
| 4 | Review follow-ups | `feature/v031-rfc0031p1-followups` | ⬜ Not started | — | — |
| 5 | Phase 1 closeout | `feature/v031-rfc0031p1-close` | ⬜ Not started | — | — |

---

## Related Documentation

- [RFC 0031 — Per-Session Namespacing for Channels and Persona Memory](0031-per-session-namespacing-channels.md) — canonical spec.
- [RFC 0016 — Human Participant & Chat Interface](0016-human-participant-chat-interface.md) — amended by PR 1 (wire-field rename).
- [RFC 0029 — Personal/Society Storage Split](0029-personal-society-storage-split.md) — Phase 1 of this plan must land before RFC 0029 Phase 1's facade signature freezes (OQ #4); RFC 0029 P1 targets v0.3.2.
- [RFC 0020 — Interaction Lifecycle](0020-interaction-lifecycle.md) §G `scope` — separate orthogonal dimension; not modified by this plan.
- [RFC 0026 PR plan](0026-pr-plan.md) — paired Phase-1 workstream in v0.3.1.
- [v0.3.1-plan.md](../v0.3.1-plan.md) — master plan.
- [ISSUE-0051](../issues/ISSUE-0051-per-session-memory-namespacing-channels.md) — root issue (closes after Phase 4).
- [v0.3.0 channel test-findings plan](../v0.3.0-test-findings-pr-plan.md) — F-3 origin.
- [MT-MEMORY-005 — Dementia test](../manual-tests/MT-MEMORY-005-dementia-test.md) — Phase 2 acceptance gate.
