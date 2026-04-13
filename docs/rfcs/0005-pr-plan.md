# RFC 0005 — PR Implementation Plan

**RFC**: [0005-persona-agent-memory.md](0005-persona-agent-memory.md)
**Created**: 2026-04-12
**Branch prefix**: `feature/v02-`
**Target**: `main`
**Merge strategy**: Squash merge per [BRANCHING.md](../BRANCHING.md)

---

## Overview

RFC 0005 defines the PersonaAgent runtime, three-tier memory system (working, episodic, relationship), agent-initiated memory tools, behavioral dimensions, dynamic persona state, and data-driven TaskAgent consolidation. The RFC spans 6 implementation phases with an estimated ~3,500–4,850 LOC (calibrated at 1.7×) across Python agents, Rust CLI, YAML config, and JSON schemas.

The project's PR size limit is <500 lines of meaningful change. This plan splits the work into **12 PRs**: Phase 1 is split into 1a (TaskAgent consolidation + agent type system) and 1b (CLI wiring to v0.1 endpoints), Phase 2 is one PR, Phase 3 is split into 3a (schema migration + episodic memory core), 3b (agent-initiated memory tools), and 3c (episode auto-summarization), Phase 4 is one PR, Phase 5 is split into 5a (persona runtime core) and 5b (event dispatch + tick loop integration), Phase 6 is split into 6a (config validation + schema updates) and 6b (CLI persona commands), plus a final PR 7 for accumulated review follow-ups and RFC close.

Each PR is independently mergeable and leaves the codebase in a passing-tests, lint-clean state.

> **Estimate calibration**: RFC 0001–0004 PRs consistently exceeded naive estimates by 73–138%. Sizes in this plan are calibrated to ~1.7× of naive estimates.

**Prerequisite**: RFC 0001–0004 fully merged (v0.1 complete). The v0.1 agent infrastructure (`BaseAgent`, `_run_llm_loop()`, `LLMClient`, `server.py` agent loader, permission gate, tool registry) is the foundation for all v0.2 work.

**Recommended merge order:** **PR 1a** → **PR 1b** (independent, can parallel with PR 1a and PR 2) → **PR 2** → **PR 3a** → **PR 3b** / **PR 3c** / **PR 4** (all three can parallel; each depends only on PR 3a) → **PR 5a** → **PR 5b** → **PR 6a** → **PR 6b** → **PR 7**.

---

## PR Sequence

### PR 1a: `feature/v02-task-agent-type` — Data-Driven TaskAgent + Agent Type System

**Depends on**: Nothing (builds on v0.1 infrastructure)
**Branch**: `feature/v02-task-agent-type`
**Estimated size**: ~350–500 lines (implementation + tests + config migration)

#### Scope

| File | Change |
|------|--------|
| `agents/task_agent.py` | **New** — `TaskAgent` class with YAML `instructions` support, delegates to `_run_llm_loop()` |
| `agents/server.py` | Update `load_agent()` to dispatch on `type` field instead of `_resolve_agent_type()` capability heuristic |
| `agents/coder.py` | **Remove** — instructions move to `config/agents.yaml` |
| `agents/reviewer.py` | **Remove** — instructions move to `config/agents.yaml` |
| `agents/planner_agent.py` | **Remove** — instructions move to `config/agents.yaml` |
| `agents/__init__.py` | Update exports — remove old agents, add `TaskAgent` |
| `config/agents.yaml` | Add `type: task` and `instructions` field to all three agents |
| `schemas/agent.schema.json` | Wire validation for `type` enum and `instructions` field (note: `type`, `persona`, `behavior`, `autonomy`, `memory`, and `relationships` definitions already exist in the schema from prior work — PR 1a focuses on wiring the `type`-based dispatch and `instructions` requirement, not creating schema from scratch) |
| `tests/unit/python/test_task_agent.py` | **New** — parametrized TaskAgent tests |
| `tests/unit/python/test_agents.py` | Update — replace class-specific tests with `TaskAgent` parametrization |

#### Key implementation details

- `TaskAgent` is a single class that reads `instructions` from agent config and prepends `"Role: {self.role}"` to match existing agent behavior.
- `server.py` dispatcher replaces `_resolve_agent_type()` with a `match` on `agent_config.get("type", "task")`. Unknown types raise `ValueError`.
- System prompts from `CoderAgent`, `ReviewerAgent`, `PlannerAgent` are moved verbatim to `config/agents.yaml` `instructions` fields.
- Agent schema updated to support `type` enum with conditional `instructions` requirement for task agents.
- Backward compatibility: agents without a `type` field default to `"task"`.

#### Tests

- TaskAgent with `instructions` field → correct system prompt composition.
- TaskAgent without `instructions` → empty instructions, still functions.
- Agent loader dispatches `type: task` → `TaskAgent`, `type: persona` → error (not yet implemented), unknown type → `ValueError`.
- Parametrized tests for coder/reviewer/planner behavior via different `instructions` configs.
- Existing `test_agents.py` cross-agent tests adapted to `TaskAgent`.

#### PR checklist

- [x] `pytest tests/unit/python/ -v` passes
- [x] `ruff check agents/` clean
- [x] `make validate` passes with updated `agents.yaml`
- [x] `CoderAgent`, `ReviewerAgent`, `PlannerAgent` files removed
- [x] Agent loader uses `type` field dispatch
- [x] `TaskAgent` preserves `"Role: ..."` prefix in system prompt

---

### PR 1b: `feature/v02-cli-v1-wiring` — CLI Wiring to v0.1 REST Endpoints

**Depends on**: None — fully independent of PR 1a (CLI commands hit existing v0.1 REST endpoints, no agent type changes needed). Can parallel with PR 1a and PR 2.
**Branch**: `feature/v02-cli-v1-wiring`
**Estimated size**: ~250–400 lines (Rust implementation)

#### Scope

| File | Change |
|------|--------|
| `cli/src/main.rs` | Wire `run`, `status`, `agent list`, `agent info`, `logs` to existing REST endpoints |
| `cli/Cargo.toml` | No changes needed — `reqwest` (with `json` feature), `serde`, `serde_json`, and `tokio` are already present from prior PRs |

#### Key implementation details

- Each command follows the pattern: build URL from `--server` flag, construct request body, `reqwest` HTTP call, deserialize JSON, format output.
- `orch run <workflow>` → `POST /api/v1/workflows/run` with `workflow_file`, `input`, `profile`.
- `orch status [id]` → `GET /api/v1/workflows/{id}/status` or list all runs.
- `orch agent list` → `GET /api/v1/agents`.
- `orch agent info <id>` → `GET /api/v1/agents/{id}`.
- `orch logs <id>` → `GET /api/v1/executions/{id}/logs`.
- Error handling: HTTP errors → user-friendly messages with status code.
- Exhaustive `match` on commands — existing stubs for unimplemented commands preserved with `todo!()` or status messages.

#### Tests

- Build verification: `cargo build --release` succeeds.
- `cargo clippy` clean.
- Manual smoke test: commands produce correct HTTP calls (unit tests deferred — Rust CLI is a thin client).

#### PR checklist

- [x] `cargo build --release` succeeds
- [x] `cargo clippy -- -D warnings` clean
- [x] All 5 v0.1 commands produce HTTP calls to correct endpoints
- [x] Error handling for connection refused, 404, 500

#### Review findings (PR #48 → deferred to PR 7)

| ID | Severity | Finding | Action |
|----|----------|---------|--------|
| F-1b-1 | Medium | `validate_path_param()` not called for workflow ID in `cmd_run()` — workflow ID goes into JSON body (safe), but client-side validation would catch malformed IDs earlier and give clearer error messages | Add `validate_resource_id()` matching server's `resourceIDRegex` `^[a-z0-9][a-z0-9-]*[a-z0-9]$` |
| F-1b-2 | Low | No serde serialization test for `SubmitWorkflowRequest` — field rename or serde attribute change would silently break the API contract | Add `#[test] fn submit_workflow_request_serializes_correctly()` |
| F-1b-3 | Low | `AgentCommands::Reload` stub discards `agent_id` with `_` pattern — generic "not yet implemented" message | Capture `agent_id` and include in message: `"Agent reload for '{agent_id}' not yet implemented"` |
| F-1b-4 | Low | Server URL scheme check is case-sensitive — `HTTP://` would be rejected | Add `.to_lowercase()` before `starts_with` check |
| F-1b-5 | Info | `WorkflowRunResponse` missing `steps` field — safe due to forward-compatible serde, but needed when step-level display is added | Add `steps: Option<HashMap<String, serde_json::Value>>` when `--steps` flag is needed |
| F-1b-6 | Info | `--server` flag could support `env = "ORCHESTR8_SERVER"` via clap `#[arg(env)]` | Wire env var fallback when auth headers are also added |

---

### PR 2: `feature/v02-working-memory` — Working Memory + Token Estimation

**Depends on**: PR 1a merged (soft dependency — `WorkingMemory` and `estimate_tokens()` are added to `BaseAgent`, not `TaskAgent`, so there is no hard code dependency; however, merging PR 1a first ensures the agent type system is stable before layering memory on top. If critical-path pressure arises, PR 2 can safely parallel with PR 1a.)
**Branch**: `feature/v02-working-memory`
**Estimated size**: ~350–500 lines (implementation + tests)

#### Scope

| File | Change |
|------|--------|
| `agents/memory/working.py` | **Implement** — `ContextSection` dataclass, `WorkingMemory` class with `add_section()`, `build_context()`, `compress_if_needed()`, `total_tokens()`, `close()` |
| `agents/memory/__init__.py` | Export `WorkingMemory`, `ContextSection`, `estimate_tokens` |
| `agents/base.py` | Add `estimate_tokens()` utility function (chars/4 MVP, optional tiktoken) |
| `agents/pyproject.toml` | Add `tiktoken` as optional dependency |
| `tests/unit/python/test_working_memory.py` | **New** — working memory management tests |

#### Key implementation details

- `WorkingMemory` tracks `_sections: list[ContextSection]` with `max_tokens` budget (default 100,000).
- `add_section()` replaces existing section with same `name` or appends new.
- `build_context()` returns sections ordered by priority (highest first), excluding overflow sections.
- `compress_if_needed()` is an async method that summarizes lowest-priority compressible sections via `LLMClient`. Spawned as fire-and-forget `asyncio.create_task()` with concurrency guard (`_compression_task` attribute).
- `estimate_tokens(text, *, accurate=False)`: chars/4 for MVP, tiktoken `cl100k_base` when `accurate=True`.
- `close()` awaits any outstanding compression task.

#### Tests

- Add sections, verify `total_tokens()` accumulates correctly.
- `build_context()` returns sections in priority order.
- Adding section with same name replaces existing.
- `compress_if_needed()` triggers when over budget — mock LLM returns summary, sections replaced.
- Concurrency guard: second `compress_if_needed()` is no-op while first is running.
- `estimate_tokens()` chars/4 accuracy for ASCII and mixed content.
- `estimate_tokens(accurate=True)` uses tiktoken when available.
- `close()` awaits outstanding compression task.

#### PR checklist

- [x] `pytest tests/unit/python/test_working_memory.py -v` passes
- [x] Coverage ≥ 80% for `agents/memory/working.py`
- [x] `ruff check agents/memory/` clean
- [x] Non-compressible sections are never summarized
- [x] Compression guard prevents double-compression

#### Review findings (PR #49 → deferred to PR 7)

| ID | Severity | Finding | Action |
|----|----------|---------|--------|
| F-2-1 | Medium | `compress_if_needed()` uses `estimate_tokens(summary)` with default `accurate=False` for post-compression re-estimation. Short summaries have proportionally larger chars/4 error (20-char summary → 5 est. tokens vs 8–10 actual). Drift accumulates over multiple compressions. | Use `estimate_tokens(summary, accurate=True)` for post-compression token re-estimation |
| F-2-2 | Low | No logging of total tokens before/after compression pass. Only individual section compression is logged. | Add `logger.info("Compression pass: %d → %d total tokens", original_total, self.total_tokens())` after the compression loop |
| F-2-3 | Low | `build_context()` has no log of total context tokens used vs budget after constructing included sections. | Add `logger.debug("Context built: %d/%d tokens, %d/%d sections included", ...)` |
| F-2-4 | Low | No test for `estimate_tokens(accurate=True)` when tiktoken IS available. `test_accurate_true_fallback` passes regardless of tiktoken installation. | Add conditional test: `pytest.importorskip("tiktoken")` → verify accurate path produces different count |
| F-2-5 | Low | No test for `initialize()` — it's a no-op, but documenting it ensures `MemoryLifecycle` protocol contract is verified. | Add `async def test_initialize_is_noop()` |
| F-2-6 | Info | No test for `build_context()` with zero-token sections or `add_section()` with empty content. | Add edge case tests for `token_count=0` and `content=""` |
| F-2-7 | Info | No `__repr__` on `WorkingMemory` — REPL/log debugging shows default object repr. | Add `__repr__` returning section count and token usage |
| F-2-8 | Info | No thread-safety docstring note. `WorkingMemory` uses plain `list` with no locks — correct for asyncio single-event-loop model but could mislead future contributors. | Add class docstring note: "Not thread-safe — designed for single-event-loop use" |

> Items F-2-6 through F-2-8 are deferred beyond PR 7 — they are nice-to-have improvements, not review fixes.

---

### PR 3a: `feature/v02-schema-migration-episodic` — Schema Migration + Episodic Memory Core

**Depends on**: PR 2 merged (uses token estimation)
**Branch**: `feature/v02-schema-migration-episodic`
**Estimated size**: ~400–500 lines (implementation + tests)

#### Scope

| File | Change |
|------|--------|
| `agents/memory/episodic.py` | **Implement** — `EpisodicMemory` class: `initialize()`, `store_episode()`, `recall()`, FTS5 setup, LIKE fallback, `MemoryLifecycle` protocol, migration infrastructure |
| `agents/memory/__init__.py` | Export `EpisodicMemory`, `Episode`, `MemoryLifecycle` |
| `agents/pyproject.toml` | Add `aiosqlite>=0.19.0,<1` dependency |
| `tests/unit/python/test_episodic_memory.py` | **New** — episodic memory CRUD + retrieval tests |

#### Key implementation details

- **Schema migration infrastructure**: `schema_version` table, ordered `MIGRATIONS` list, `_apply_migrations()` function. Forward-only (no rollback).
- **Initial migration (v1)**: `episodes` table, `agent_state` table, `episodes_fts` FTS5 virtual table, FTS5 sync triggers (INSERT/UPDATE/DELETE).
- **FTS5 availability check**: `initialize()` tests FTS5 with a throwaway `CREATE VIRTUAL TABLE`, falls back to `LIKE`-based queries with warning log.
- **`store_episode()`**: generates UUID, inserts into `episodes`, FTS5 auto-synced via trigger.
- **`recall()`**: FTS5 `MATCH` query with composite scoring: `BM25 × importance × (1 + ln(1 + access_count)) × 1/(1 + age_days)`. Falls back to `LIKE` matching when FTS5 unavailable. Increments `access_count` and updates `last_accessed_at` on returned episodes.
- **WAL mode**: Set `PRAGMA journal_mode=WAL` on connection open.
- **Agent-scoped**: All queries filter by `agent_id` (fixed at construction, not per-call).
- Default `db_path`: `data/memory.db`.

#### Tests

- `store_episode()` → `recall()` round-trip.
- FTS5 ranking: more relevant episodes score higher.
- `recall()` with `min_importance` filter.
- `access_count` incremented on recall.
- Agent isolation: agent A cannot retrieve agent B's episodes.
- Schema migration: version table created, migrations applied in order, re-run is idempotent.
- FTS5 trigger sync: insert/update/delete on `episodes` → FTS5 index updated.
- LIKE fallback: when FTS5 unavailable, recall still works (with warning).
- Empty query → recency × importance fallback.

#### PR checklist

- [x] `pytest tests/unit/python/test_episodic_memory.py -v` passes
- [x] Coverage ≥ 80% for `agents/memory/episodic.py`
- [x] `ruff check agents/memory/` clean
- [x] Agent-scoped isolation verified by cross-agent test
- [x] WAL mode enabled
- [x] FTS5 fallback tested
- [x] Migration infrastructure tested for idempotency

#### Review findings (PR #50 → deferred to PR 7)

| ID | Severity | Finding | Action |
|----|----------|---------|--------|
| F-3a-1 | Medium | Zero-importance episodes invisible in scoring: multiplicative formula produces `score=0.0` when `importance=0.0`, making stored episodes effectively invisible in ranked recall | Change `e.importance` to `(0.1 + e.importance * 0.9)` for non-zero baseline, or document that `importance=0.0` suppresses episodes from ranked recall |
| F-3a-2 | Medium | Scoring formula duplicated across `_recall_fts5()`, `_recall_like()`, `_recall_recency()` — tuning change requires updating 3 SQL strings in sync | Extract shared scoring expression into a module-level constant `_SCORE_EXPR` |
| F-3a-3 | Medium | No future migration forward-compatibility test — migration system is a critical building block for PRs 3b and 4 | Add test that patches `MIGRATIONS` with a hypothetical v2 entry and verifies both v1 and v2 are applied |
| F-3a-4 | Low | `MemoryLifecycle` protocol not defined or exported per PR plan scope. `EpisodicMemory` and `WorkingMemory` duck-type-match but no formal protocol class exists | Add `MemoryLifecycle(Protocol)` with `initialize()` and `close()` to `agents/memory/__init__.py` |
| F-3a-5 | Low | Relative default `db_path` `"data/memory.db"` depends on process working directory — unpredictable on deployment | Add docstring note that callers should resolve to absolute path |
| F-3a-6 | Low | No async context manager support (`__aenter__`/`__aexit__`) — callers must remember to call `close()` explicitly | Add `__aenter__`/`__aexit__` for `async with` cleanup pattern |
| F-3a-7 | Low | Aggressive recency decay `1/(1 + age_days)` halves score at 1 day, ~3% at 30 days — too aggressive for 90-day retention window | Consider softer decay like `1/(1 + age_days/7)` or `1/(1 + sqrt(age_days))` |
| F-3a-8 | Info | No scoring formula docstring explaining component rationale and edge case behavior | Add inline comment block in `_recall_fts5()` |

---

### PR 3b: `feature/v02-memory-tools` — Agent-Initiated Memory Tools

**Depends on**: PR 3a merged (shares SQLite infrastructure and migration system)
**Branch**: `feature/v02-memory-tools`
**Estimated size**: ~350–500 lines (implementation + tests)

#### Scope

| File | Change |
|------|--------|
| `agents/memory/episodic.py` | Add `notes` table migration (v2), `store_note()`, `recall_notes()`, `update_note()`, `delete_note()` methods, `notes_fts` FTS5 index + sync triggers |
| `agents/tools/builtin.py` | Add `store_note`, `recall_notes`, `update_note`, `delete_note` tool functions |
| `tests/unit/python/test_memory_tools.py` | **New** — memory tool CRUD, FTS5 search, pruning tests |

#### Key implementation details

- **Migration v2**: `notes` table + `notes_fts` FTS5 virtual table + sync triggers.
- **Closure-based factory**: `create_memory_tools(agent_id, db)` returns tool instances with `agent_id` captured in closure — not LLM-controllable.
- **`store_note()`**: generates UUID, inserts into `notes`, enforces `max_notes` cap (prunes oldest low-access notes).
- **`recall_notes()`**: FTS5 search on `topic`, `content`, `tags_json`. Returns ranked results.
- **`update_note()`**: replaces content, updates `updated_at`. Topic and tags preserved.
- **`delete_note()`**: hard delete by `note_id` + `agent_id` scope.
- **Permission gating**: all tools require `memory:read` or `memory:write` permissions.
- **`auto_reflect_after` nudge**: counter stored in `agent_state` table, incremented per interaction, nudge injected into system prompt when threshold reached.

#### Tests

- `store_note` → `recall_notes` round-trip.
- `update_note` → content replaced, topic/tags preserved.
- `delete_note` → note removed, subsequent recall returns nothing.
- Note pruning: exceed `max_notes` → oldest low-access notes removed.
- FTS5 search ranking for notes.
- Agent isolation: agent A cannot access agent B's notes.
- Permission gating: tool without `memory:write` → denied.
- `auto_reflect_after` counter: increments per interaction, fires nudge at threshold, resets after nudge.
- Counter persistence: survives simulated restart (close + reopen DB).

#### PR checklist

- [x] `pytest tests/unit/python/test_memory_tools.py -v` passes
- [x] Coverage ≥ 80% for new code in `episodic.py` and `builtin.py`
- [x] `ruff check agents/` clean
- [x] Agent-scoped isolation verified
- [x] `max_notes` pruning tested
- [x] `auto_reflect_after` counter persistence tested
- [x] Note content size bounded (10KB default)

#### Review findings (PR #51 → deferred to PR 7)

| ID | Severity | Finding | Action |
|----|----------|---------|--------|
| F-3b-1 | Medium | `_prune_notes()` has TOCTOU race: SELECT count then DELETE is non-atomic across concurrent connections — stale overflow calculation possible when multiple `EpisodicMemory` instances share a DB file | Use atomic DELETE with subquery count: `DELETE FROM notes WHERE id IN (SELECT id FROM notes WHERE agent_id = ? ORDER BY access_count ASC, created_at ASC LIMIT MAX(0, (SELECT COUNT(*) FROM notes WHERE agent_id = ?) - ? + 1))`, or use `BEGIN IMMEDIATE` to acquire write lock before count |
| F-3b-2 | Medium | `increment_interaction_count()` read-after-write race: separate SELECT after UPSERT+COMMIT could read stale/advanced count under concurrent access. SQLite 3.35+ supports `RETURNING` clause which would be both cleaner and atomic | Use `INSERT ... ON CONFLICT ... SET interaction_count = interaction_count + 1 RETURNING interaction_count` — eliminates the separate SELECT |
| F-3b-3 | Medium | `note_id` from LLM passed to `update_note`/`delete_note` tool closures without format validation. Parameterized (no injection), but malformed IDs (extremely long strings, null bytes) cause wasted DB round-trips and unclear error messages | Add `UUID(note_id)` parse or regex check before calling memory layer |
| F-3b-4 | Low | FTS5 fallback warning in `_recall_notes_fts5()` does not include the exception message — harder to diagnose specific FTS5 syntax issues | Capture `except sqlite3.OperationalError as exc` and log `"Notes FTS5 query failed for %r, falling back to LIKE: %s", query, exc` |
| F-3b-5 | Low | No test for `recall_notes` with FTS5 malformed query fallback (e.g., `"NOT"` or `"*"`) — episodes have this tested in PR 3a, but notes FTS5 fallback path is not explicitly tested | Add `test_recall_fts5_malformed_query_fallback` to `TestRecallNotes` |

> Items deferred beyond PR 7 — nice-to-have improvements: split notes into separate `NoteStore` class (~800-line `EpisodicMemory` growing), cap `limit` parameter at tool layer, test `_prune_notes` with `max_notes=1`, negative test for `check_auto_reflect(auto_reflect_after=-1)`, `test_migration_idempotent` should use same DB file (not fresh `:memory:`).

---

### PR 3c: `feature/v02-episode-summarization` — Episode Auto-Summarization

**Depends on**: PR 3a merged (uses `agent_state` table, episode infrastructure from PR 3a; can parallel with PR 3b — no dependency on notes or memory tools)
**Branch**: `feature/v02-episode-summarization`
**Estimated size**: ~200–350 lines (implementation + tests)

> **RFC divergence note**: RFC Phase 3c ("Phase 3c: Episode Auto-Summarization", line ~1602 of `0005-persona-agent-memory.md`) lists "Dependencies: Phase 3b" and includes deliverable #3: "`auto_reflect_after` counter persistence across restarts (round-trip test)". In this PR plan, the counter persistence test is moved to PR 3b (where the counter logic and `agent_state` table writes are implemented), allowing PR 3c to depend only on PR 3a and run in parallel with PR 3b. This reordering is tracked here rather than in the RFC itself to keep the RFC as the canonical design document.

#### Scope

| File | Change |
|------|--------|
| `agents/memory/episodic.py` | Add `summarize_old_episodes()`, `delete_old_episodes()` methods |
| `tests/unit/python/test_episodic_memory.py` | Extended — summarization + retention tests |

#### Key implementation details

- **`summarize_old_episodes(older_than_days, llm_client)`**: Selects episodes with `compression_level < 1` older than threshold, calls LLM for summarization, updates `summary`, increments `compression_level`, sets `compressed_at`.
- **`delete_old_episodes(older_than_days)`**: Hard-deletes episodes with `compression_level >= 1` older than retention window. Uncompressed episodes are not deleted.
- **Compression levels**: 0 = raw, 1 = summarized, 2 = distilled.
- **Configurable**: `memory.episodic.retention_days` (default 90 per extension spec E7.2).

#### Tests

- `summarize_old_episodes()` with mock LLM: old episodes get compressed summary, `compression_level` set to 1.
- `delete_old_episodes()`: only compressed episodes deleted, uncompressed preserved.
- Episodes newer than threshold untouched.
- `compression_level` transition: 0 → 1 → 2.
- Retention: episodes older than `retention_days` + compressed → deleted.

#### PR checklist

- [x] `pytest tests/unit/python/test_episodic_memory.py -v` passes
- [x] Coverage ≥ 80% for new methods
- [x] `ruff check agents/memory/` clean
- [x] Only compressed episodes eligible for deletion
- [x] LLM call mocked — no real API calls

#### Review findings (PR #52 → deferred to PR 7)

| ID | Severity | Finding | Action |
|----|----------|---------|--------|
| F-3c-1 | Low | `summarize_old_episodes()` stores raw `response.text` without stripping whitespace — emptiness check uses `.strip()` but the stored value retains leading/trailing whitespace from LLM | Store `summary.strip()` instead of raw `response.text` |
| F-3c-2 | Low | `logger.info("Summarized episode ...")` fires outside `if rowcount > 0` guard — misleading log in race conditions where episode was deleted between SELECT and UPDATE | Move `logger.info` inside the `if update_cursor.rowcount > 0:` block |
| F-3c-3 | Info | No concurrency guard for parallel invocations — concurrent callers can SELECT the same batch, causing duplicate LLM calls and wasted budget | Add docstring note: "Not concurrency-safe. External callers should ensure only one summarization run per agent at a time." |

> Items deferred beyond PR 7 — nice-to-have improvements: test FTS5 searchability after summarization UPDATE, composite index `(agent_id, compression_level, created_at)` for retention queries, `memory_episode_summarize_count` telemetry counter, document `older_than_days=0` edge case behavior.

---

### PR 4: `feature/v02-relationship-memory` — Relationship Memory

**Depends on**: PR 3a merged (shares SQLite infrastructure)
**Branch**: `feature/v02-relationship-memory`
**Estimated size**: ~350–500 lines (implementation + tests)

#### Scope

| File | Change |
|------|--------|
| `agents/memory/relationship.py` | **Implement** — `RelationshipMemory` class: `initialize()`, `get_trust()`, `update_trust()`, `record_interaction()`, `apply_decay()`, `get_relationship_summary()`, `close()`, trust bootstrapping from config |
| `agents/memory/__init__.py` | Export `RelationshipMemory` |
| `tests/unit/python/test_relationship_memory.py` | **New** — trust math, decay, interaction recording tests |

#### Key implementation details

- **Migration v3** (or added to existing migration, depending on ordering): `relationships` + `interactions` tables.
- **Trust math**: `update_trust()` clamps delta to ±0.2 per call, clamps result to [0.0, 1.0].
- **Bidirectional decay**: `apply_decay()` moves all trust scores toward 0.5 (neutral) by `decay_rate` (default 0.01). Trust > 0.5 decays down, trust < 0.5 decays up.
- **Trust bootstrapping**: `initialize()` seeds trust scores from agent config `relationships` entries. Only inserts if no existing row (doesn't overwrite runtime-evolved trust).
- **`record_interaction()`**: inserts into `interactions` table, increments `interaction_count` on `relationships`.
- **`get_relationship_summary()`**: returns dict with trust score, interaction count, recent interactions for LLM prompt injection.
- **Agent-scoped**: `agent_id` fixed at construction.

#### Tests

- `update_trust()` → trust changes by delta, clamped to [0.0, 1.0].
- Delta clamping: ±0.2 max per call.
- `apply_decay()`: trust 0.9 → moves toward 0.5; trust 0.1 → moves toward 0.5.
- Trust bootstrapping: config values seeded on first init, preserved on subsequent inits.
- `record_interaction()` → increments interaction count, stored in `interactions` table.
- Agent isolation: agent A's trust scores invisible to agent B.
- `get_relationship_summary()`: includes trust, interaction count, recent interactions.
- Default trust for unknown agents: 0.5.

#### PR checklist

- [x] `pytest tests/unit/python/test_relationship_memory.py -v` passes
- [x] Coverage ≥ 80% for `agents/memory/relationship.py`
- [x] `ruff check agents/memory/` clean
- [x] Bidirectional decay tested
- [x] Trust bootstrapping from config tested
- [x] Agent isolation verified
- [x] Delta clamping tested

---

### PR 5a: `feature/v02-persona-runtime` — PersonaAgent Runtime Core

**Depends on**: PRs 2, 3a, 3b, 4 merged (all memory tiers required). Note: PR 3c (Episode Auto-Summarization) is NOT required — summarization is invoked independently and is not needed for persona runtime core functionality. PR 3c can merge before or after PR 5a.
**Branch**: `feature/v02-persona-runtime`
**Estimated size**: ~400–500 lines (implementation + tests)

#### Scope

| File | Change |
|------|--------|
| `agents/persona.py` | Add `PersonaState` dataclass (`Mood` enum, energy, stress), `render_behavior()` function, `DIMENSION_DESCRIPTIONS` mapping, `_LLMPersonaAgent` concrete class with LLM-powered `on_event()`, `create_persona_agent()` factory, per-agent `asyncio.Lock`, `close_memory()`, PersonaState persistence |
| `agents/memory/__init__.py` | Verify all exports needed by persona runtime |
| `tests/unit/python/test_persona_runtime.py` | **New** — persona state, behavior rendering, on_event loop tests |

#### Key implementation details

- **`PersonaState`**: `mood: Mood` (6-value enum), `stress_level: float`, `energy: float`, `recent_context: list[str]`, `goal_progress: dict[str, float]`. `to_prompt_section()` renders for LLM injection.
- **Energy mechanics**: -0.05 per action, +0.1 per idle tick. Non-tick agents recover lazily. Clamped [0.0, 1.0].
- **`render_behavior()`**: maps 5 dimensions × 3 values to natural language from `DIMENSION_DESCRIPTIONS`. Defaults to middle values when omitted.
- **`_LLMPersonaAgent`**: concrete `PersonaAgent` subclass. `on_event()` implements multi-turn tool-use loop: build system prompt → format event → LLM call loop (with memory tools) → parse `AgentAction`s → store episode.
- **`create_persona_agent()`**: factory that instantiates `_LLMPersonaAgent` with config, wires memory tiers, creates memory tools via `create_memory_tools()`.
- **Per-agent `asyncio.Lock`**: serializes `on_event()` and `on_tick()`.
- **PersonaState persistence**: serialize/load from `agent_state` table. `recent_context` not persisted.
- **`close_memory()`**: acquires lock, awaits compression task, closes all memory tiers.

#### Tests

- `PersonaState.to_prompt_section()`: mood injected, stress only when > 0.3, energy warning when < 0.5.
- `Mood` enum: all 6 values serialize/deserialize correctly.
- Energy drain and recovery math.
- Lazy energy recovery for non-tick agents.
- `render_behavior()`: all 5 dimensions × 3 values produce expected descriptions.
- `render_behavior()` with omitted dimensions: defaults to middle values.
- `_LLMPersonaAgent.on_event()` with mock LLM: returns `AgentAction`s.
- Multi-turn tool-use loop: LLM calls tool → tool result fed back → final response parsed.
- `handle()` backward compatibility: wraps task as `TASK_ASSIGNED` event.
- PersonaState persistence: serialize → close → reopen → deserialize matches.
- `create_persona_agent()`: returns configured `_LLMPersonaAgent` with memory tiers.

#### PR checklist

- [x] `pytest tests/unit/python/test_persona_runtime.py -v` passes
- [x] Coverage ≥ 80% for new code in `agents/persona.py`
- [x] `ruff check agents/` clean
- [x] `Mood` enum constraints enforced
- [x] Energy clamped to [0.0, 1.0]
- [x] Behavioral dimension defaults applied for omitted dimensions
- [x] PersonaState persistence round-trip tested
- [x] `handle()` backward compatibility verified

---

### PR 5b: `feature/v02-event-dispatch-tick` — Event Dispatch + Tick Loop Integration

**Depends on**: PR 5a merged
**Branch**: `feature/v02-event-dispatch-tick`
**Estimated size**: ~400–500 lines (implementation + tests)

#### Scope

| File | Change |
|------|--------|
| `agents/persona.py` | Add `ActionExecutor`, `EventDispatcher` (with `_inject_memory_context()`, cascade depth limiting, idle wake-up), `TickScheduler` (with graceful shutdown, idle detection) |
| `agents/server.py` | Update agent loader: `type: persona` → `create_persona_agent()`. Add memory lifecycle calls (`initialize()` at startup, `close_memory()` at shutdown). Start `TickScheduler` for autonomous agents. Wire `EventDispatcher` |
| `config/agents.yaml` | Uncomment/add sample persona agent config for integration testing |
| `tests/unit/python/test_persona_runtime.py` | Extended — event dispatch, tick loop, action execution, concurrency tests |
| `tests/integration/test_persona_e2e.py` | **New** — persona agent end-to-end with mock LLM |

#### Key implementation details

- **`ActionExecutor`**: exhaustive `match` on `ActionType`. `COMPLETE_TASK` handled by return path, `SEND_MESSAGE` routes through `EventDispatcher.dispatch()` with cascade depth increment, `USE_TOOL` dispatches through tool registry. `DELEGATE`, `SPAWN_SUB_AGENT` are TODO stubs for future RFCs.
- **`EventDispatcher`**: holds dict of persona agents + tick schedulers. `dispatch()` acquires per-agent lock, injects memory context, calls `on_event()`, executes resulting actions. Cascade depth tracked in `event.metadata["cascade_depth"]` — reject at `max_cascade_depth` (default 5). Wakes idle tick scheduler on incoming event.
- **`TickScheduler`**: `asyncio.Task` loop with `asyncio.sleep(interval)`. Acquires agent lock before `on_tick()`. Idle detection: `idle_after_ticks` DO_NOTHING actions → skip LLM calls. `wake()` resets idle counter. Graceful shutdown: cancel task, wait up to 10s for in-flight actions.
- **`server.py` updates**: agent loader creates persona agents via `create_persona_agent()`. Startup sequence: construct agents → initialize memory → start gRPC → start tick schedulers. Shutdown sequence: stop tick schedulers → stop gRPC → close memory.
- **Memory lifecycle**: `initialize()` called after agent construction; `close_memory()` called during shutdown.

#### Tests

- **Event dispatch**: event → `on_event()` called → actions returned and executed.
- **Cascade depth**: events beyond `max_cascade_depth` → logged and dropped.
- **Idle detection**: repeated DO_NOTHING → `idle_count` increments → tick loop skips LLM calls.
- **Wake from idle**: incoming event → `wake()` → tick loop resumes.
- **Concurrency lock**: concurrent `on_event()` and `on_tick()` serialize on the agent lock.
- **Tick loop lifecycle**: start → ticks fire → stop → in-flight operations complete.
- **Memory lifecycle**: `initialize()` at startup, `close_memory()` at shutdown (in-flight ops complete).
- **Agent loader**: `type: persona` → `create_persona_agent()` called, memory initialized.
- **Integration**: full event → action → memory store cycle with mock LLM.
- **Cross-agent memory isolation**: agent A cannot retrieve agent B's data through shared DB.

#### PR checklist

- [x] `pytest tests/unit/python/ tests/integration/ -v` passes
- [x] Coverage ≥ 80% for new code
- [x] `ruff check agents/` clean
- [x] Cascade depth limiting tested
- [x] Idle detection and wake tested
- [x] Graceful shutdown tested (in-flight operations complete)
- [x] Memory lifecycle correct (initialize → use → close)
- [x] Cross-agent memory isolation verified
- [x] Sample persona agent in `agents.yaml`

#### Review findings (PR #55 → deferred to PR 7)

> **Already applied** (committed on branch before merge): `EventDispatcher.dispatch()` uses `copy.deepcopy()` for event payload (was shallow spread), `TickScheduler._run()` recovers energy during idle tick skips, integration test config fixed to use valid string enum behavior values under `persona` key, memory init failure prevents dispatch/scheduler registration, `exclusive()` public lock accessor added, cascade depth propagated through `SEND_MESSAGE`, `recover_idle_energy()` public API added, WARNING log for channel-only messages without mentions.

| ID | Severity | Finding | Action |
|----|----------|---------|--------|
| F-5b-1 | Medium | `_inject_memory_context()` deferred as TODO — persona agents rely solely on explicit memory tool calls for context retrieval, LLMs may not proactively recall context | Implement automatic episodic recall + relationship summary + recent notes injection into working memory before event handling |
| F-5b-4 | Medium | No per-dispatch timeout in `_handle_send_message()` — if a mentioned agent's `on_event()` hangs (despite event timeout), the sending agent blocks indefinitely. Cascade can stack 5 levels of blocking calls | Wrap each `self._dispatcher.dispatch()` call in `asyncio.wait_for()` with configurable timeout (e.g., 60s) |
| F-5b-5 | Low | `_MAX_MENTIONS_PER_ACTION` truncation untested — resource exhaustion mitigation (cap 10 mentions per SEND_MESSAGE) has no test coverage | Add test: LLM response with 15 mentions → verify only 10 dispatched + warning logged |
| F-5b-6 | Low | `_handle_send_message()` with `dispatcher=None` returns `"no_dispatcher"` status untested — misconfigured setup could silently lose messages | Add test: `ActionExecutor(dispatcher=None)` + SEND_MESSAGE → verify `"no_dispatcher"` status returned |
| F-5b-7 | Low | `TickScheduler._MIN_INTERVAL` clamping untested — safety clamp for zero/negative config not verified | Add test: `TickScheduler(agent, interval=0.0)` → verify `scheduler._interval == 0.01` |
| F-5b-8 | Low | Cascade depth termination only tested via integration test — no unit-level test for `EventDispatcher.dispatch()` rejecting events at `max_cascade_depth` | Add unit test: `dispatch()` with `metadata["cascade_depth"] >= max_cascade_depth` → empty actions + warning logged |
| F-5b-9 | Low | Three separate agent iteration loops in `server.py start()` (memory init, dispatcher registration, tick scheduler start) — code duplication | Consolidate into single loop with `if agent_id in failed_memory_init: continue` guard |

> Items deferred beyond PR 7 — nice-to-have improvements: decouple `ActionExecutor` ↔ `EventDispatcher` via event bus (v0.3 mesh networking), `_handle_send_message()` channel-based routing (v0.2 channels), `TickScheduler.stop()` forced-cancel test, module split (`persona/agent.py`, `persona/dispatch.py`, `persona/tick.py`, `persona/state.py`), config-driven energy thresholds, backpressure mechanism for event dispatch queue (bounded per-agent queue), `_wait_for_stop_or_wake()` task churn reduction (combined `asyncio.Event` or `asyncio.Condition`), typed `AgentEvent.cascade_depth` field replacing `metadata["cascade_depth"]`, `USE_TOOL` as final action path test, `ActionExecutor._execute_one()` catch-all branch test.

---

### PR 6a: `feature/v02-config-validation` — Config Validation + Schema Updates

**Depends on**: PR 5b merged (all persona config fields must exist)
**Branch**: `feature/v02-config-validation`
**Estimated size**: ~300–450 lines (implementation + tests)

#### Scope

| File | Change |
|------|--------|
| `agents/validate.py` | **Implement** — `validate_config_dir()` using `jsonschema` library |
| `schemas/agent.schema.json` | Validate and refine: most v0.2 definitions (`persona`, `behavior` dimensions, `autonomy`, `memory`, `relationships`) already exist from prior work. PR 6a focuses on ensuring conditional requirements (`type: task` → `instructions`, `type: persona` → `persona`), range validation, and wiring `make validate` — not creating the schema from scratch. Actual delta expected to be smaller than naive estimate. |
| `agents/pyproject.toml` | Add `jsonschema>=4.0,<5` dependency |
| `tests/unit/python/test_validate.py` | **New** — validation pass/fail tests |

#### Key implementation details

- **`validate_config_dir(path)`**: loads YAML files from directory, validates against JSON schemas in `schemas/`.
- **Schema updates**: `type` conditional — `type: task` requires `instructions`, `type: persona` requires `persona` object (with `behavior` sub-object). Behavioral dimensions: 5 enums with defaults. Memory config: `db_path`, `episodic.retention_days`, `notes.*`, `relationship.decay_rate`. Autonomy config: `level` enum, `tick_interval_seconds`, `max_actions_per_tick`, `idle_after_ticks`.
- **Behavioral dimension defaults**: each dimension defaults to its middle value (`balanced`, `moderate`) when not specified. `behavior` object required for persona agents; individual dimensions optional.
- **`make validate`**: existing Makefile target wired to Python validator.

#### Tests

- Valid task agent config → passes.
- Valid persona agent config → passes.
- Missing `instructions` for task agent → fails.
- Missing `persona` for persona agent → fails.
- Invalid behavioral dimension value → fails.
- Omitted behavioral dimension → defaults applied, passes.
- Invalid `autonomy.level` → fails.
- Memory config with out-of-range values → fails.
- Empty config dir → passes (no agents to validate).

#### PR checklist

- [x] `pytest tests/unit/python/test_validate.py -v` passes
- [x] `make validate` succeeds with updated configs
- [x] `ruff check agents/validate.py` clean
- [x] Behavioral dimension enum validation tested
- [x] Conditional requirements per agent type tested
- [x] All persona fields from RFC 0005 represented in schema

#### Review findings (PR #56 → deferred to PR 7)

> **Already applied** (committed on branch before merge): `_load_schema()` wrapped in try/except for FileNotFoundError/JSONDecodeError, `channels.yaml` wired to `channel.schema.json` in `_SCHEMA_MAP`, `additionalProperties: false` added to all sub-objects (permissions, autonomy, memory, persona, goals, knowledge, relationship), workflow schema `json.loads()` error handling added, TODO comment listing unmapped config files, duplicate test class removed.

| ID | Severity | Finding | Action |
|----|----------|---------|--------|
| F-6a-1 | Medium | `validate_config_dir()` uses `print()` and returns `bool` — callers (future `orch validate`, server startup) cannot inspect individual errors without parsing stdout | Return `tuple[bool, list[ValidationError]]` or a `ValidationResult` dataclass. Keep `print()` in `__main__` block for CLI usage |
| F-6a-2 | Medium | Agent ID regex `^[a-z0-9][a-z0-9-]*[a-z0-9]$` silently requires ≥3 characters — valid 1–2 char IDs like `"a1"` are rejected. The `[a-z0-9-]*` middle portion forces a minimum of start+middle+end | Either update pattern to `^[a-z0-9]([a-z0-9-]*[a-z0-9])?$` (allows 1-char) or add `"minLength": 3` with descriptive documentation |
| F-6a-3 | Low | Workflow schema load error handling pattern is asymmetric with config schema loading — both work but the different patterns (try/else vs try/except/continue) may confuse future maintainers | Align patterns when touching this code next |
| F-6a-4 | Low | `memory.db_path` has no schema-level validation — accepts empty strings, `..` traversal paths. Runtime must validate but schema could catch obvious errors | Add `"minLength": 1` to `db_path` |
| F-6a-5 | Low | No test for `channels.yaml` validation path — `_SCHEMA_MAP` entry exists but has zero test coverage | Add test writing `channels.yaml` in `config_dir` and validating against `channel.schema.json` |
| F-6a-6 | Low | Tests only check pass/fail boolean — a schema regression that fails for the *wrong* reason would be invisible | Add at least one test capturing stdout (via `capsys`) and asserting specific error message content |

> Items deferred beyond PR 7 — nice-to-have improvements: schema-level `db_path` path traversal pattern, duplicate agent ID detection (custom post-schema check), `goals.primary` as required for persona agents, replace `print()` with `logging.getLogger("orchestr8.validate")`, schema `$ref` splitting into `schemas/definitions/` for maintainability, validator result caching for repeated schema loads.

---

### PR 6b: `feature/v02-cli-persona` — CLI Persona Commands

**Depends on**: PR 5b merged (persona agent endpoints exist)
**Branch**: `feature/v02-cli-persona`
**Estimated size**: ~150–250 lines (Rust implementation)

#### Scope

| File | Change |
|------|--------|
| `cli/src/main.rs` | Wire `validate` and `test --persona` commands |

#### Key implementation details

- `orch validate <path>` → invoke Python validator subprocess or validate in-process (calls `agents/validate.py` via `reqwest` against a validation endpoint or subprocess).
- `orch test --persona <id>` → persona consistency check endpoint (or local persona config validation).
- Maintains exhaustive `match` pattern — no catch-all `_`.

#### Tests

- Build verification: `cargo build --release` succeeds.
- `cargo clippy` clean.

#### PR checklist

- [x] `cargo build --release` succeeds
- [x] `cargo clippy -- -D warnings` clean
- [x] `orch validate` command functional
- [x] `orch test --persona` command functional

---

### PR 7: `feature/v02-rfc0005-close` — Review Follow-ups + RFC Close

**Depends on**: All previous PRs merged
**Branch**: `feature/v02-rfc0005-close`
**Estimated size**: ~100–200 lines (targeted fixes + status updates)

#### Scope

| File | Change |
|------|--------|
| `docs/rfcs/0005-persona-agent-memory.md` | Update status: `📋 Proposed` → `✅ Implemented` |
| `ROADMAP.md` | Update RFC 0005 tracker: merged count, status. Update Component Status tables |
| Various | Accumulated review follow-up findings from PRs 1a–6b |

#### Accumulated review findings

**From PR 1b (PR #48 review):**

| ID | File | Change |
|----|------|--------|
| F-1b-1 | `cli/src/main.rs` | Add `validate_resource_id()` for workflow ID in `cmd_run()` — client-side regex `^[a-z0-9][a-z0-9-]*[a-z0-9]$` matching server's `resourceIDRegex` |
| F-1b-2 | `cli/src/main.rs` | Add `#[test] fn submit_workflow_request_serializes_correctly()` — serde contract test |
| F-1b-3 | `cli/src/main.rs` | `AgentCommands::Reload` — capture `agent_id`, include in stub message |
| F-1b-4 | `cli/src/main.rs` | Case-insensitive server URL scheme check (`.to_lowercase()`) |

> Items F-1b-5 and F-1b-6 are deferred beyond PR 7 — they require new features (`--steps` flag, env-based auth), not review fixes.

**From PR 2 (PR #49 review):**

| ID | File | Change |
|----|------|--------|
| F-2-1 | `agents/memory/working.py` | Use `estimate_tokens(summary, accurate=True)` in `compress_if_needed()` for post-compression re-estimation — short summaries have proportionally larger chars/4 error |
| F-2-2 | `agents/memory/working.py` | Add total tokens before/after log in `compress_if_needed()`: `"Compression pass: %d → %d total tokens"` |
| F-2-3 | `agents/memory/working.py` | Add debug log in `build_context()`: `"Context built: %d/%d tokens, %d/%d sections included"` |
| F-2-4 | `tests/unit/python/test_working_memory.py` | Add tiktoken-conditional test: `pytest.importorskip("tiktoken")` → verify accurate path produces different count than chars/4 |
| F-2-5 | `tests/unit/python/test_working_memory.py` | Add `test_initialize_is_noop()` to verify `MemoryLifecycle` protocol contract |

> Items F-2-6 through F-2-8 are deferred beyond PR 7 — nice-to-have improvements (zero-token edge case tests, `__repr__`, thread-safety docstring).

**From PR 3a (PR #50 review):**

| ID | File | Change |
|----|------|--------|
| F-3a-1 | `agents/memory/episodic.py` | Handle zero-importance episodes in scoring formula — `importance=0.0` produces `score=0.0` via multiplicative formula, making valid episodes invisible in all recall results. Change `e.importance` to `(0.1 + e.importance * 0.9)` or similar non-zero baseline in FTS5, LIKE, and recency scoring expressions |
| F-3a-2 | `agents/memory/episodic.py` | Extract shared scoring formula — the non-BM25 scoring components (`importance × access_boost × recency_decay`) are duplicated across `_recall_fts5()`, `_recall_like()`, and `_recall_recency()`. Extract into a module-level SQL fragment constant or helper |
| F-3a-3 | `tests/unit/python/test_episodic_memory.py` | Add future migration forward-compatibility test — patch `MIGRATIONS` to include a hypothetical v2 entry, verify both v1 and v2 are applied and recorded in `schema_version`. Core value proposition of the migration infrastructure |
| F-3a-4 | `agents/memory/__init__.py` | Define `MemoryLifecycle` protocol — PR plan scope specifies exporting `MemoryLifecycle` but it was not implemented. Add `class MemoryLifecycle(Protocol): async def initialize(self) -> None: ...; async def close(self) -> None: ...` |

> Items deferred beyond PR 7 — nice-to-have improvements: softer recency decay (tune `1/(1+age_days)` to `1/(1+age_days/7)` or `1/(1+sqrt(age_days))`), async context manager (`__aenter__`/`__aexit__`), Unicode/large payload tests, query length validation cap, scoring formula inline docstring.

**From PR 3b (PR #51 review):**

| ID | File | Change |
|----|------|--------|
| F-3b-1 | `agents/memory/episodic.py` | TOCTOU in `_prune_notes()` — SELECT count then DELETE is non-atomic. Use atomic DELETE with subquery count or `BEGIN IMMEDIATE` write lock before count |
| F-3b-2 | `agents/memory/episodic.py` | Use `RETURNING` clause in `increment_interaction_count()` — separate SELECT after UPSERT+COMMIT is a read-after-write race. `INSERT ... ON CONFLICT ... SET interaction_count = interaction_count + 1 RETURNING interaction_count` eliminates the separate SELECT |
| F-3b-3 | `agents/tools/builtin.py` | Add `note_id` format validation in `update_note`/`delete_note` tool closures — `UUID(note_id)` parse or regex check before calling memory layer |
| F-3b-4 | `agents/memory/episodic.py` | Add exception message to FTS5 fallback warning in `_recall_notes_fts5()` — capture `except sqlite3.OperationalError as exc` and include `exc` in log message |
| F-3b-5 | `tests/unit/python/test_memory_tools.py` | Add `test_recall_fts5_malformed_query_fallback` — test notes FTS5 fallback with malformed queries like `"NOT"` or `"*"` |

> Items deferred beyond PR 7 — nice-to-have improvements: split notes into separate `NoteStore` class, cap `limit` at tool layer, test `_prune_notes` with `max_notes=1`, negative test for `check_auto_reflect(auto_reflect_after=-1)`, `test_migration_idempotent` same-DB test.

**From PR 3c (PR #52 review):**

| ID | File | Change |
|----|------|--------|
| F-3c-1 | `agents/memory/episodic.py` | Strip LLM summary before storing — change `summary = response.text` to `summary = response.text.strip() if response.text else response.text` in `summarize_old_episodes()` |
| F-3c-2 | `agents/memory/episodic.py` | Move `logger.info("Summarized episode ...")` inside `if update_cursor.rowcount > 0:` block in `summarize_old_episodes()` |
| F-3c-3 | `agents/memory/episodic.py` | Add concurrency note to `summarize_old_episodes()` docstring: "Not concurrency-safe. External callers should ensure only one summarization run per agent at a time." |

> Items deferred beyond PR 7 — nice-to-have improvements: FTS5 searchability test after summarization, composite retention index, summarization telemetry counter, `older_than_days=0` edge case documentation.

**From PR 4 (PR #53 review):**

| ID | File | Change |
|----|------|--------|
| F-4-1 | `agents/memory/relationship.py` | Validate `other_agent_id` is non-empty in `update_trust()` and `record_interaction()` — add `if not other_agent_id or not other_agent_id.strip(): raise ValueError("other_agent_id must not be empty")` at top of both methods, consistent with `interaction_type` validation |
| F-4-2 | `tests/unit/python/test_relationship_memory.py` | Add concurrent `record_interaction()` test — fire two concurrent calls, verify `interaction_count == 2`. Matches pattern established by `test_concurrent_updates_both_applied` for `update_trust()` |
| F-4-3 | `agents/memory/relationship.py` | Cap `reason` string length in `update_trust()` — `reason = reason[:1024]` before storing. The `notes` field is injected into LLM prompts via `get_relationship_summary()`, unbounded strings waste context tokens |
| F-4-4 | `agents/memory/relationship.py` | Cap `outcome` string length in `record_interaction()` — `outcome = outcome[:1024] if outcome else outcome`. Same concern as F-4-3 |

> Items deferred beyond PR 7 — nice-to-have improvements: interaction retention/pruning (`prune_old_interactions(older_than_days)`), async context manager (`__aenter__`/`__aexit__` for all memory tiers), trust change audit trail table, `Sentiment = Annotated[float, Ge(-1.0), Le(1.0)]` type alias, file-based DB test for `apply_decay`, pagination for `get_all_relationships()`.

**From PR 5a (PR #54 review):**

> **Already applied** (committed on branch before merge): `close_memory()` individual tier try/except for resilient close, `_parse_actions()` regex hardened from `\s*` to `\n` anchors to prevent polynomial backtracking, `render_behavior()` warns on unknown dimension keys, module docstring trimmed of PR 5b scope references.

| ID | File | Change |
|----|------|--------|
| F-5a-1 | `agents/persona.py` | `on_tick()` missing per-event timeout — `on_event()` wraps `_on_event_inner()` in `asyncio.wait_for(timeout=...)`, but `on_tick()` calls `_on_event_inner()` directly. A slow LLM during a tick holds the lock indefinitely. Wrap the `_on_event_inner()` call in `on_tick()` with `asyncio.wait_for()` using the same configurable timeout. Add a test for tick timeout. |
| F-5a-2 | `agents/persona.py` | Add prompt injection trust boundary comment in `_format_event()` — when external bridges are added (future RFC), `sender_id` and `content` from `MESSAGE_RECEIVED` events will originate from untrusted sources. Add `# Security: content is from framework-internal agents. Sanitize if external bridges are added.` at the top of `_format_event()`. |
| F-5a-3 | `agents/persona.py` | Forward `llm_client` through `PersonaAgent.__init__` — currently `PersonaAgent.__init__` calls `super().__init__(agent_id, config)` without forwarding `llm_client`, and `_LLMPersonaAgent.__init__` sets `self._llm_client` directly. Either add `llm_client` to `PersonaAgent.__init__` signature and forward it, or document the override contract clearly. |
| F-5a-4 | `tests/unit/python/test_persona_runtime.py` | Add test for `_build_system_prompt()` with minimal persona config (empty `background`, no `quirks`, no `goals`) — all existing tests use the full `_PERSONA_CONFIG` fixture. Verify graceful degradation. |
| F-5a-5 | `tests/unit/python/test_persona_runtime.py` | Add test for `on_tick()` when energy is already at `1.0` — verify `recover_energy()` clamp (trivially correct but validates contract). |

> Items deferred beyond PR 7 — nice-to-have improvements: make energy thresholds configurable (`stress_level > 0.3`, `energy < 0.5` in `to_prompt_section()`), cap `recent_context` list size during accumulation (currently only capped on display via `[-5:]`), `SubAgentRequest.model` default value as a constant instead of hardcoded string, `APPROVAL_REQUESTED`/`APPROVAL_RESPONSE` event type explicit tests.

**From PR 5b (PR #55 review):**

> **Already applied** (committed on branch before merge): `EventDispatcher.dispatch()` uses `copy.deepcopy()` for event payload (was shallow spread), `TickScheduler._run()` recovers energy during idle tick skips, integration test config fixed to use valid string enum behavior values under `persona` key, memory init failure prevents dispatch/scheduler registration, `exclusive()` public lock accessor added, cascade depth propagated through `SEND_MESSAGE`, `recover_idle_energy()` public API added, WARNING log for channel-only messages without mentions.

| ID | File | Change |
|----|------|--------|
| F-5b-1 | `agents/persona.py` | `_inject_memory_context()` deferred as TODO — scope reduction from PR plan. Persona agents rely solely on explicit memory tool calls for context retrieval. Implement automatic episodic recall + relationship summary + recent notes injection into working memory before event handling. |
| F-5b-4 | `agents/persona.py` | No per-dispatch timeout in `_handle_send_message()` — if a mentioned agent's `on_event()` hangs, the sending agent blocks indefinitely. Cascade can stack 5 levels. Wrap each `self._dispatcher.dispatch()` call in `asyncio.wait_for()` with configurable timeout (e.g., 60s) |
| F-5b-5 | `tests/unit/python/test_event_dispatch_tick.py` | Add test for `_MAX_MENTIONS_PER_ACTION` truncation — LLM response with 15 mentions → verify only 10 dispatched + warning logged |
| F-5b-6 | `tests/unit/python/test_event_dispatch_tick.py` | Add test for `_handle_send_message()` with `dispatcher=None` → `ActionExecutor(dispatcher=None)` + SEND_MESSAGE → verify `"no_dispatcher"` status |
| F-5b-7 | `tests/unit/python/test_event_dispatch_tick.py` | Add test for `TickScheduler._MIN_INTERVAL` clamping — `TickScheduler(agent, interval=0.0)` → verify `_interval == 0.01` |
| F-5b-8 | `tests/unit/python/test_event_dispatch_tick.py` | Add unit test for cascade depth termination at dispatcher level — `dispatch()` with `metadata["cascade_depth"] >= max_cascade_depth` → empty actions + warning |
| F-5b-9 | `agents/server.py` | Consolidate three agent iteration loops in `start()` (memory init, dispatcher registration, tick scheduler start) into single loop with `if agent_id in failed_memory_init: continue` guard |

**From PR 6a (PR #56 review):**

> **Already applied** (committed on branch before merge): `_load_schema()` try/except for FileNotFoundError/JSONDecodeError, `channels.yaml` → `channel.schema.json` wired in `_SCHEMA_MAP`, `additionalProperties: false` on all sub-objects (permissions, autonomy, memory, persona, goals, knowledge, relationship), workflow schema `json.loads()` error handling, TODO comment listing unmapped config files, duplicate test class removed.

| ID | File | Change |
|----|------|--------|
| F-6a-1 | `agents/validate.py` | Return structured errors from `validate_config_dir()` — return `tuple[bool, list[ValidationError]]` or `ValidationResult` dataclass instead of `bool`. Keep `print()` in `__main__` block only. Enables `orch validate` (PR 6b) and programmatic server-startup validation without stdout parsing |
| F-6a-2 | `schemas/agent.schema.json` | Clarify agent ID minimum length — regex `^[a-z0-9][a-z0-9-]*[a-z0-9]$` silently requires ≥3 chars. Either fix pattern to `^[a-z0-9]([a-z0-9-]*[a-z0-9])?$` or add `"minLength": 3` with documentation |
| F-6a-4 | `schemas/agent.schema.json` | Add `"minLength": 1` to `memory.db_path` — currently accepts empty strings at schema level |
| F-6a-5 | `tests/unit/python/test_validate.py` | Add test for `channels.yaml` validation path — `_SCHEMA_MAP` entry has zero test coverage |
| F-6a-6 | `tests/unit/python/test_validate.py` | Add test asserting specific error message content (via `capsys`) — current tests only check pass/fail boolean, schema regressions that fail for the wrong reason are invisible |

> Items F-5b-2 and F-5b-3 already applied in review fix passes (memory init failure isolation, `exclusive()` lock accessor). No further action needed.

> Items deferred beyond PR 7 — nice-to-have improvements: decouple `ActionExecutor` ↔ `EventDispatcher` via event bus (v0.3 mesh networking), `_handle_send_message()` channel-based routing (v0.2 channels), `TickScheduler.stop()` forced-cancel test, module split (`persona/agent.py`, `persona/dispatch.py`, `persona/tick.py`, `persona/state.py`), config-driven energy thresholds, backpressure mechanism for event dispatch queue (bounded per-agent queue), `_wait_for_stop_or_wake()` task churn reduction (combined `asyncio.Event` or `asyncio.Condition`), typed `AgentEvent.cascade_depth` field replacing `metadata["cascade_depth"]`, `USE_TOOL` as final action path test, `ActionExecutor._execute_one()` catch-all branch test, schema-level `db_path` path traversal pattern, duplicate agent ID detection (custom post-schema check), `goals.primary` as required for persona agents, replace `print()` with `logging.getLogger("orchestr8.validate")`, schema `$ref` splitting into `schemas/definitions/`.

#### Key implementation details

- Bundle all "Should Fix" findings from previous PR reviews.
- Update RFC status and ROADMAP.
- Final lint and test pass.

#### PR checklist

- [ ] RFC 0005 status is `✅ Implemented`
- [ ] ROADMAP RFC tracker updated with correct merged count
- [ ] ROADMAP Component Status tables updated for v0.2 components
- [ ] All accumulated review findings addressed
- [ ] Full test suite passes: `make test`
- [ ] Full lint passes: `make lint`

---

## Summary

| PR | Phase | Branch | Est. Size | Depends On |
|----|-------|--------|-----------|------------|
| 1a | 1 | `feature/v02-task-agent-type` | 350–500 | — |
| 1b | 1 | `feature/v02-cli-v1-wiring` | 250–400 | — |
| 2 | 2 | `feature/v02-working-memory` | 350–500 | 1a |
| 3a | 3 | `feature/v02-schema-migration-episodic` | 400–500 | 2 |
| 3b | 3 | `feature/v02-memory-tools` | 350–500 | 3a |
| 3c | 3 | `feature/v02-episode-summarization` | 200–350 | 3a |
| 4 | 4 | `feature/v02-relationship-memory` | 350–500 | 3a |
| 5a | 5 | `feature/v02-persona-runtime` | 400–500 | 2, 3a, 3b, 4 |
| 5b | 5 | `feature/v02-event-dispatch-tick` | 400–500 | 5a |
| 6a | 6 | `feature/v02-config-validation` | 300–450 | 5b |
| 6b | 6 | `feature/v02-cli-persona` | 150–250 | 5b |
| 7 | — | `feature/v02-rfc0005-close` | 100–200 | All |

**Total estimated**: ~3,600–4,900 lines across 12 PRs (calibrated at 1.7×).

### Dependency Graph

```
PR 1a (TaskAgent + type system)
  ├── PR 1b (CLI v0.1 wiring) [independent — can parallel] ┐
  └── PR 2 (Working Memory)                                 │
        └── PR 3a (Schema Migration + Episodic Core)        │
              ├── PR 3b (Memory Tools) ─────────────────┐   │
              ├── PR 3c (Episode Summarization) [can ────┤   │
              │         parallel with PR 3b]             │   │
              └── PR 4 (Relationship Memory) ───────────┤   │
                                                        ↓   │
                                        PR 5a (Persona Runtime Core)
                                              └── PR 5b (Event Dispatch + Tick)
                                                    ├── PR 6a (Config Validation)
                                                    └── PR 6b (CLI Persona)
                                                          └── PR 7 (Close RFC)
```

### Risk Mitigation

| Risk | Impact | Mitigation |
|------|--------|------------|
| Memory integration complexity exceeds estimates | PR 5a/5b oversize | Pre-split into two PRs; can further split action executor if needed |
| FTS5 unavailable in test/CI environments | PR 3a test failures | LIKE fallback tested explicitly; CI uses standard CPython (FTS5 included) |
| SQLite concurrency under multi-agent load | PR 5b integration flaky | WAL mode + per-agent lock; single-process MVP limits contention |
| Behavioral dimension rendering quality | Persona agents behave poorly | All 15 dimension/value combos tested with expected output; LLM testing is manual |
| TaskAgent consolidation breaks existing tests | PR 1a regressions | Parametrized tests preserve existing coverage; old test patterns adapted |
| PRs 3a, 4, 5a, 5b at 500-line boundary | PRs exceed size limit, require mid-implementation splits | Calibrated at 1.7×; PR 5a/5b already pre-split. Monitor during implementation and split further if needed |

### Deferred to Follow-Up

The following RFC 0005 items are **out of scope** for this PR plan and will be addressed in separate follow-up work:

- **REST API Observability Endpoints** (Go orchestrator changes): `/api/v1/agents/{id}/state`, `/memory/episodes`, `/memory/notes`, `/memory/relationships`, `/{id}/tick`. These are Go-side changes outside the Python agent scope of this plan.
- **Memory Observability Metrics**: The RFC defines structured log metrics (`memory_episode_store_duration_ms`, `memory_notes_count`, `memory_trust_update`, etc.). These should be added incrementally in each memory PR (episode metrics in PR 3a, notes metrics in PR 3b, trust metrics in PR 4, working memory metrics in PR 2) but are not tracked as explicit deliverables in this plan. Implementers should include basic `logger.info()`-level observability when implementing each memory tier.
- **Go Orchestrator Impact**: Persona-aware scheduling, agent type routing, and orchestrator-side config changes are deferred to a separate Go-focused RFC/plan.
