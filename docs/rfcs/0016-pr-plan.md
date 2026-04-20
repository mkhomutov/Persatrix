# RFC 0016 — PR Implementation Plan

**RFC**: [0016-human-participant-chat-interface.md](0016-human-participant-chat-interface.md)
**Created**: 2026-04-20
**Branch prefix**: `feature/v021-`
**Target**: `main`
**Merge strategy**: Squash merge per [BRANCHING.md](../BRANCHING.md)

---

## Overview

RFC 0016 introduces a `Participant` Protocol abstracting agents, users, and future system actors; a `UserParticipant` concrete type with SQLite persistence; generalizes `RelationshipMemory` to participant pairs; and adds a chat interface spanning proto, gRPC, REST, and CLI layers. The RFC spans 3 implementation phases across Python agents, Go orchestrator, Rust CLI, and proto.

This plan splits the work into **7 PRs**: Phase 1 is split into PR 1 (Participant Protocol + UserParticipant + UserStore + BaseAgent properties) and PR 2 (Migration 4 + RelationshipMemory generalization + prompt injection). Phase 2 is split into PR 3 (proto extension + Python gRPC servicer + EventDispatcher flag) and PR 4 (Go REST endpoint + gRPC dispatch + integration test). Phase 3 is PR 5 (CLI chat command). PR 6 addresses review follow-ups. PR 7 closes the RFC.

Each PR is independently mergeable and leaves the codebase in a passing-tests, lint-clean state.

> **Estimate calibration**: RFC 0005/0006 PRs used a 1.7× calibration factor based on v0.1 actuals. This plan applies the same factor. Sizes below are calibrated estimates.

**Prerequisite**: RFC 0005 fully merged (20/20 PRs), RFC 0006 fully merged (12/12 PRs). The persona agent, memory system, event dispatch, and execution limit infrastructure are the foundation for this work.

**Recommended merge order**: **PR 1** → **PR 2** → **PR 3** → **PR 4** → **PR 5** → **PR 6** → **PR 7**.

All PRs are sequential — each depends on the previous due to the layered nature of the feature (data model → proto/gRPC → REST → CLI).

---

## Dependency Graph

```
PR 1 (Participant Protocol + UserParticipant + UserStore + BaseAgent)
  ↓
PR 2 (Migration 4 + RelationshipMemory generalization + prompt injection)
  ↓
PR 3 (Proto extension + Python gRPC servicer + EventDispatcher flag)
  ↓
PR 4 (Go REST endpoint + gRPC dispatch + integration test)
  ↓
PR 5 (CLI chat command)
  ↓
PR 6 (Review follow-ups)
  ↓
PR 7 (RFC close)
```

---

## PR Sequence

### PR 1: `feature/v021-participant-protocol` — Participant Protocol + UserParticipant + UserStore

**Depends on**: Nothing (builds on v0.2.0 infrastructure)
**Branch**: `feature/v021-participant-protocol`
**Estimated size**: ~250–425 lines (implementation + tests)

#### Scope

| File | Change |
|------|--------|
| `agents/participant.py` | **New** — `Participant` Protocol, `VALID_PARTICIPANT_TYPES` frozenset, `UserParticipant` dataclass, `UserStore` CRUD class |
| `agents/base.py` | Add three read-only `Participant` Protocol properties: `participant_id`, `participant_type`, `display_name` (OQ 10) |
| `agents/__init__.py` | Export `Participant`, `UserParticipant`, `UserStore`, `VALID_PARTICIPANT_TYPES` |
| `tests/unit/python/test_participant.py` | **New** — `UserParticipant` CRUD, `UserStore.get_or_create()`, `UserStore.update_last_seen()`, `BaseAgent` satisfies `Participant` Protocol, `VALID_PARTICIPANT_TYPES` validation |

#### Key implementation details

- `Participant` is a `@runtime_checkable` Protocol with three attributes: `participant_id`, `participant_type`, `display_name`.
- `VALID_PARTICIPANT_TYPES` is `frozenset({"agent", "user"})` — validated at `UserStore` write boundary.
- `UserParticipant` is a dataclass with `participant_id`, `display_name`, `participant_type` (default `"user"`), `created_at`, `last_seen_at`.
- `UserStore` provides `get_or_create()`, `update_last_seen()`, and `get()` methods using `aiosqlite`.
- `participant_id` validation enforces the agent ID regex (`^[a-z0-9][a-z0-9-]*[a-z0-9]$`) at the `get_or_create()` boundary (OQ 1).
- `BaseAgent` gains three properties that delegate to existing attributes: `participant_id → agent_id`, `participant_type → "agent"`, `display_name → self.name` (OQ 10). No `__init__` signature changes.
- `UserStore` creates the `users` table if it does not exist (DDL in `get_or_create()` or via an `initialize()` method). The full migration (including relationship table rebuild) is deferred to PR 2.

#### Tests

- `UserParticipant` dataclass creation with all fields.
- `UserStore.get_or_create()` creates a new record on first call and returns existing on subsequent calls.
- `UserStore.update_last_seen()` updates timestamp without modifying other fields.
- `UserStore.get()` returns `None` for unknown `participant_id`.
- `participant_id` validation: rejects empty, rejects whitespace, rejects unicode, rejects uppercase, accepts `"local"`, accepts `"alice-01"`.
- `VALID_PARTICIPANT_TYPES` rejects invalid types at the store boundary.
- `BaseAgent` subclass (e.g., `TaskAgent`) satisfies `Participant` Protocol via `isinstance()` check.
- `PersonaAgent` satisfies `Participant` Protocol.

#### PR checklist

- [ ] `pytest tests/unit/python/ -v` passes
- [ ] `ruff check agents/` clean
- [ ] `agents/participant.py` exports `Participant`, `UserParticipant`, `UserStore`, `VALID_PARTICIPANT_TYPES`
- [ ] `BaseAgent` has `participant_id`, `participant_type`, `display_name` properties
- [ ] `participant_id` regex validation enforced at `UserStore.get_or_create()`

---

### PR 2: `feature/v021-memory-generalization` — Migration 4 + RelationshipMemory Generalization + Prompt Injection

**Depends on**: PR 1 merged (Participant types and UserStore exist)
**Branch**: `feature/v021-memory-generalization`
**Estimated size**: ~300–500 lines (implementation + tests + migration SQL)

#### Scope

| File | Change |
|------|--------|
| `agents/memory/migrations.py` | Migration 4: `users` table DDL + rebuild `relationships` and `interactions` tables with composite PK including `participant_type`/`other_participant_type` (12-step ALTER TABLE; OQ 12) |
| `agents/memory/relationship.py` | Rename `agent_id`/`other_agent_id` → `participant_id`/`other_participant_id`; add `participant_type`/`other_participant_type` parameters with `"agent"` defaults; update `Interaction` and `RelationshipSummary` dataclasses |
| `agents/persona_runtime/memory_context.py` | Label user participants distinctly in `_format_relationship_summary()`; add XML-style `<\|user_message\|>` delimiter wrapping in `_format_event()` (OQ 4, OQ 14); add system prompt instruction in `_build_system_prompt()` |
| `tests/unit/python/test_relationship_memory_user.py` | **New** — `RelationshipMemory.record_interaction()` with `participant_type="user"`, trust decay for user participants, PK collision test (user and agent with same ID; OQ 12) |
| Existing relationship memory tests | Update to use new parameter names (`participant_id` instead of `agent_id`); verify backfill correctness |

#### Key implementation details

- Migration 4 uses the [12-step ALTER TABLE](https://www.sqlite.org/lang_altertable.html#otheralter) pattern: `CREATE TABLE new → INSERT...SELECT → DROP old → ALTER TABLE RENAME`. Executed in a single transaction.
- Composite PK: `PRIMARY KEY (participant_id, participant_type, other_participant_id, other_participant_type)` prevents ID collisions between user and agent participants.
- Existing data is backfilled with `participant_type = "agent"` and `other_participant_type = "agent"`.
- `RelationshipMemory` API: all methods gain `participant_type`/`other_participant_type` keyword parameters defaulting to `"agent"`. Existing callers are unaffected.
- `participant_type` validation at write boundary against `VALID_PARTICIPANT_TYPES` (imported from `agents/participant.py`; OQ 3).
- Prompt injection: `_format_event()` wraps user messages in `<|user_message user_id="..."|>` / `<|/user_message|>` delimiters. `_build_system_prompt()` adds ~20-token instruction unconditionally (OQ 14b).
- Memory context: `_format_relationship_summary()` labels `participant_type == "user"` as "Human user" vs "Agent".

#### Tests

- Migration 4 idempotency: run twice, no error, no duplicate rows.
- `RelationshipMemory.record_interaction()` with `other_participant_type="user"` stores and retrieves correctly.
- `RelationshipMemory.get_trust()` for user participant returns default trust score.
- Trust decay behaves identically for user and agent participant types.
- PK collision: user `"local"` and agent `"local"` both have distinct relationship rows with the same target (OQ 12).
- All existing relationship memory tests pass after entity ID rename (backfill correctness).
- Prompt injection delimiter wrapping applied to user messages, not agent messages.
- System prompt instruction present unconditionally.

#### PR checklist

- [ ] `pytest tests/unit/python/ -v` passes
- [ ] `ruff check agents/` clean
- [ ] Migration 4 executes cleanly on fresh DB and on DB with existing relationship data
- [ ] Composite PK prevents user/agent ID collision
- [ ] All existing relationship memory tests pass with renamed parameters
- [ ] `<|user_message|>` delimiters applied in `_format_event()` for user messages
- [ ] System prompt instruction in `_build_system_prompt()`

---

### PR 3: `feature/v021-proto-grpc-servicer` — Proto Extension + Python gRPC Servicer + EventDispatcher Flag

**Depends on**: PR 2 merged (memory generalization in place for `record_interaction()` calls)
**Branch**: `feature/v021-proto-grpc-servicer`
**Estimated size**: ~250–425 lines (implementation + tests + proto + generated stubs)

> **Size risk**: Regenerated proto stubs across two language targets (Python + Go) may push this PR beyond the 500-line limit. If so, split into PR 3a (proto definitions + regenerated stubs) and PR 3b (servicer implementation + tests), following the RFC 0005 sub-PR splitting precedent.

#### Scope

| File | Change |
|------|--------|
| `proto/task.proto` | Add `SendChatMessage(ChatRequest) returns (ChatResponse)` RPC; add `ChatRequest` and `ChatResponse` messages (OQ 8, OQ 16) |
| `agents/generated/` | Regenerated Python stubs via `make proto-python` |
| `internal/generated/` | Regenerated Go stubs via `make proto-go` |
| `agents/dispatch.py` | Add `execute_actions: bool = True` keyword argument to `EventDispatcher.dispatch()` (OQ 7) |
| `agents/server.py` | Implement `SendChatMessage` servicer: build `AgentEvent`, call `dispatch(execute_actions=False)`, extract reply (OQ 5 priority), execute remaining actions, call `record_interaction()` (OQ 11), timeout wrapping (OQ 6), `session_id` in metadata (OQ 9), validate `participant_type` against `VALID_PARTICIPANT_TYPES` |
| `tests/unit/python/test_dispatch_execute_actions.py` | **New** — `dispatch(execute_actions=False)` returns actions without executing |
| `tests/unit/python/test_chat_servicer.py` | **New** — reply extraction priority, timeout, session_id, empty reply, error handling |

#### Key implementation details

- `ChatRequest` fields: `agent_id`, `user_id`, `message`, `session_id`, `timeout_seconds`.
- `ChatResponse` fields: `reply`, `session_id`, `agent_id`, `timestamp`, `agent_display_name` (empty — orchestrator fills; OQ 8/15), `reply_status` (`"ok"`, `"empty"`, `"error"`; OQ 16).
- `EventDispatcher.dispatch()` gains `execute_actions` kwarg. When `False`, returns `list[AgentAction]` without calling `ActionExecutor.execute()`. This is a per-call override; child dispatches use the default `True` (OQ 7).
- Reply extraction priority (OQ 5): `SEND_MESSAGE` with `user_id in mentions` → any `SEND_MESSAGE` → `COMPLETE_TASK` → empty string.
- After extracting reply, remaining actions are fed into `ActionExecutor.execute()` so side-effects still fire.
- `asyncio.wait_for(timeout=clamped_timeout)` wraps `dispatch()` (OQ 6). Timeout clamped to `max(1, min(timeout_seconds, 300))` (OQ 13).
- `session_id` generated via `uuid.uuid4()` on first request (empty `session_id`); stored in `event.metadata["session_id"]` (OQ 9).
- `record_interaction(other_participant_type="user")` called after reply extraction (OQ 11).
- On exception: return gRPC `INTERNAL` status.

#### Tests

- `dispatch(execute_actions=False)` returns action list without executing.
- `dispatch(execute_actions=True)` (default) still executes actions.
- Reply extraction: `SEND_MESSAGE(mentions=["local"])` selected over `SEND_MESSAGE(mentions=["other-agent"])`.
- Reply extraction: fallback to first `SEND_MESSAGE` when no user-targeted action.
- Reply extraction: fallback to `COMPLETE_TASK` when no `SEND_MESSAGE`.
- Reply extraction: empty string + warning when no applicable action.
- Timeout: `asyncio.wait_for` cancellation returns `DEADLINE_EXCEEDED`.
- `session_id` generated when request has empty `session_id`.
- `session_id` reused when request provides one.
- `record_interaction()` called with `other_participant_type="user"`.
- Exception in `on_event()` → gRPC `INTERNAL`.

#### PR checklist

- [ ] `make proto` succeeds (Go + Python stubs regenerated)
- [ ] `pytest tests/unit/python/ -v` passes
- [ ] `ruff check agents/` clean
- [ ] `ChatRequest`/`ChatResponse` messages in `proto/task.proto`
- [ ] `SendChatMessage` servicer handles all OQ 5/6/7/9/11/13/16 requirements
- [ ] `execute_actions` flag on `EventDispatcher.dispatch()`
- [ ] `participant_type` validated at servicer boundary

---

### PR 4: `feature/v021-rest-chat-endpoint` — Go REST Endpoint + gRPC Dispatch + Integration Test

**Depends on**: PR 3 merged (proto stubs and Python servicer ready)
**Branch**: `feature/v021-rest-chat-endpoint`
**Estimated size**: ~300–500 lines (implementation + tests)

#### Scope

| File | Change |
|------|--------|
| `internal/server/chat_handler.go` | **New** — `handleChat()`: parse request, validate `agent_id` and `message`, enforce message length limit, call gRPC `SendChatMessage`, populate `agent_display_name` from Registry (OQ 8), set `reply_status` (OQ 16), map gRPC errors to HTTP status codes |
| `internal/server/server.go` | Register `POST /api/v1/agents/{id}/chat` route |
| `internal/server/chat_handler_test.go` | **New** — handler tests: 400 (empty message, oversized message), 404 (unknown agent), 503 (gRPC INTERNAL), 504 (DEADLINE_EXCEEDED), 200 with `agent_display_name` and `reply_status` |
| `internal/executor/chat.go` | **New** — `SendChatMessage()` gRPC call using existing agent connection pool |
| `internal/executor/chat_test.go` | **New** — gRPC call tests with mock server |
| `tests/integration/test_chat_endpoint.py` | **New** — full round-trip: REST → orchestrator → gRPC → agent → reply |

#### Key implementation details

- `handleChat()` validates: `agent_id` present (from URL path), `message` non-empty, `message` ≤ `chat_max_message_length` (default 4000 chars, configurable).
- Registry lookup for agent gRPC address — 404 if not found.
- gRPC `SendChatMessage` call with existing connection pool from executor.
- `agent_display_name` populated from Registry `Name` field (OQ 8/15). Falls back to `agent_id` if empty.
- gRPC error mapping: `INTERNAL` → HTTP 503, `DEADLINE_EXCEEDED` → HTTP 504.
- Response JSON: `reply`, `session_id`, `agent_id`, `timestamp`, `agent_display_name`, `reply_status`.
- Integration test: starts agent server + orchestrator, sends chat message via REST, verifies reply and relationship memory updated.

#### Tests

- HTTP 400 for empty `message`.
- HTTP 400 for `message` exceeding 4000 character limit.
- HTTP 404 for unknown `agent_id`.
- HTTP 503 when gRPC returns `INTERNAL`.
- HTTP 504 when gRPC returns `DEADLINE_EXCEEDED`.
- HTTP 200 with correct `agent_display_name` from Registry.
- HTTP 200 with `reply_status` set to `"ok"`, `"empty"`, or `"error"`.
- Integration: full round-trip REST → gRPC → agent → reply → relationship memory updated.
- Integration: second message in same session continues conversation.
- Integration: first request with empty `session_id` returns server-generated UUID.

#### PR checklist

- [ ] `go test ./internal/server/ -v -race` passes
- [ ] `go test ./internal/executor/ -v -race` passes
- [ ] `pytest tests/integration/ -v` passes
- [ ] `POST /api/v1/agents/{id}/chat` registered
- [ ] Message length validation enforced
- [ ] gRPC error → HTTP status mapping correct
- [ ] `agent_display_name` populated from Registry
- [ ] `reply_status` field set correctly

---

### PR 5: `feature/v021-cli-chat` — CLI Chat Command

**Depends on**: PR 4 merged (REST endpoint available)
**Branch**: `feature/v021-cli-chat`
**Estimated size**: ~150–250 lines (Rust implementation)

#### Scope

| File | Change |
|------|--------|
| `cli/src/commands/chat.rs` | **New** — `cmd_chat()`: REPL loop, REST POST, session tracking, `Waiting for...` spinner, display name rendering, empty reply handling (OQ 16) |
| `cli/src/main.rs` | Add `Chat` variant to command enum with `agent_id` and `--user` arguments; wire to `cmd_chat()` |
| `cli/src/types.rs` | Add `ChatRequest` and `ChatResponse` serde types |
| `cli/Cargo.toml` | Add `indicatif` dependency for spinner (if not already present) |

#### Key implementation details

- `Chat { agent_id: String, user: Option<String> }` added to the command enum. Exhaustive `match` ensures compile error if not handled.
- `cmd_chat()` REPL: prints banner, loops reading stdin, sends `POST /api/v1/agents/{agent_id}/chat`, prints response.
- `--user` flag defaults to `"local"` when not provided.
- `session_id` from first response is stored and reused for subsequent requests.
- `Waiting for <agent_id>...` spinner shown after ~2s of no response (OQ 6).
- Response display: `<display_name>: <reply>` using `agent_display_name` from response; falls back to `agent_id` (OQ 8).
- Empty reply: when `reply_status == "empty"`, prints `<display_name> did not respond.` in dimmed style (OQ 16).
- `exit` command or Ctrl-C/EOF terminates cleanly.
- No client-side history persistence — conversation lives in agent memory.

#### Tests

- `cargo build --release` succeeds.
- `cargo clippy -- -D warnings` clean.
- Exhaustive `match` — removing `Chat` variant causes compile error.
- Manual test: `persatrix chat <agent_id>` end-to-end with running agent.

#### PR checklist

- [ ] `cargo build --release` succeeds
- [ ] `cargo clippy -- -D warnings` clean
- [ ] `Chat` command variant in enum
- [ ] REPL loop reads stdin, sends POST, prints reply
- [ ] `--user` flag with `"local"` default
- [ ] `session_id` maintained across requests
- [ ] Spinner after ~2s
- [ ] Empty reply handling (`reply_status == "empty"`)
- [ ] `exit` and Ctrl-C terminate cleanly

---

### PR 6: `feature/v021-chat-followups` — Review Follow-Ups

**Depends on**: PR 5 merged (all core PRs complete)
**Branch**: `feature/v021-chat-followups`
**Estimated size**: ~150–300 lines (fixes + new tests)

#### Scope

Review findings from PRs 1–5, grouped by component. Items below are populated from review reports as PRs are reviewed.

##### From PR 1 review (PR #119)

**Should Fix:**

| # | File | Finding | Fix |
|---|------|---------|-----|
| 1 | `agents/participant.py` | `get_or_create()` TOCTOU race: SELECT-then-INSERT is not atomic. Two concurrent callers for the same ID could race to INSERT, causing `IntegrityError`. | Replace INSERT with `INSERT OR IGNORE INTO users (...) VALUES (?, ?, ?, ?, ?)`, then unconditionally re-SELECT to return the final state. |
| 2 | `agents/participant.py` | `get_or_create()` and `get()` use `execute_fetchall()` instead of the `async with db.execute() as cursor:` + `fetchone()` pattern used everywhere else in the memory subsystem. | Refactor to cursor context manager pattern for style consistency. |
| 3 | `agents/participant.py` | `update_last_seen()` does not validate `participant_id` (unlike `get_or_create()`). Silently succeeds on nonexistent participants. | Add `validate_participant_id(participant_id)` at the top of the method. |

**Test gaps to fill:**

| # | Test | Purpose |
|---|------|---------|
| 4 | Concurrent `get_or_create` calls via `asyncio.gather()` | Verify idempotency after `INSERT OR IGNORE` fix (exposes TOCTOU if unfixed). |
| 5 | `update_last_seen` on nonexistent participant | Document and verify the expected behavior (silent no-op or error after validation fix). |
| 6 | `UserStore.initialize()` called twice | Verify close-then-reopen re-initialization path works correctly. |
| 7 | `display_name` with very long string (10,000+ chars) | Verify no unbounded storage issues; consider adding a length limit (e.g., 255 chars) at write boundary. |
| 8 | `PersonaAgent` satisfies `Participant` Protocol | Explicit conformance test (currently only `BaseAgent` via `_StubAgent` and `UserParticipant` are tested). |

##### From PR 2 review

_TBD — populated after PR 2 review._

##### From PR 3 review

_TBD — populated after PR 3 review._

##### From PR 4 review

_TBD — populated after PR 4 review._

##### From PR 5 review

_TBD — populated after PR 5 review._

#### PR checklist

- [ ] All deferred review findings addressed
- [ ] PR 1 findings: `get_or_create` uses `INSERT OR IGNORE` (idempotent)
- [ ] PR 1 findings: query style aligned to cursor context manager pattern
- [ ] PR 1 findings: `update_last_seen` validates `participant_id`
- [ ] PR 1 findings: 5 new tests added (concurrent get_or_create, update_last_seen nonexistent, re-init, long display_name, PersonaAgent conformance)
- [ ] `make test` passes
- [ ] `make lint` clean
- [ ] `make validate` passes

---

### PR 7: `feature/v021-rfc0016-close` — RFC Close

**Depends on**: PR 6 merged (all follow-ups addressed)
**Branch**: `feature/v021-rfc0016-close`
**Estimated size**: ~50–100 lines (status updates only)

#### Scope

| File | Change |
|------|--------|
| `docs/rfcs/0016-human-participant-chat-interface.md` | Status → `✅ Implemented` |
| `ROADMAP.md` | RFC 0016 status → `✅ Implemented`, merged count = 7/7, component status updates |
| `docs/rfcs/0016-pr-plan.md` | All checklists complete |

#### PR checklist

- [ ] RFC 0016 status = `✅ Implemented`
- [ ] ROADMAP.md RFC Tracker updated
- [ ] ROADMAP.md Component Status tables updated
- [ ] ROADMAP.md Merged PR History includes all 7 PRs
- [ ] `make test` passes
- [ ] `make lint` clean
- [ ] `make validate` passes
