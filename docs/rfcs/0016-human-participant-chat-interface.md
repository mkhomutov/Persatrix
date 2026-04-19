# RFC 0016 — Human Participant & Chat Interface

**Type**: feature
**Status**: 📋 Proposed
**Author**: Maksim Khomutov
**Date**: 2026-04-19
**Target**: v0.2.1
**Depends on**: RFC 0005

---

## Table of Contents

- [Summary](#summary)
- [Motivation](#motivation)
- [Goals](#goals)
- [Non-Goals](#non-goals)
- [Design / Implementation](#design--implementation)
  - [A. The `Participant` Abstraction](#a-the-participant-abstraction)
  - [B. `UserParticipant` Concrete Type](#b-userparticipant-concrete-type)
  - [C. Memory Generalization](#c-memory-generalization)
  - [D. Event Routing for User Messages](#d-event-routing-for-user-messages)
  - [E. Proto Extension](#e-proto-extension)
  - [F. REST API for Chat](#f-rest-api-for-chat)
  - [G. `persatrix chat` CLI Command](#g-persatrix-chat-cli-command)
  - [H. Prompt Injection for User Participants](#h-prompt-injection-for-user-participants)
- [Security Considerations](#security-considerations)
- [Phased Implementation Plan](#phased-implementation-plan)
- [Files Touched (Estimated)](#files-touched-estimated)
- [Test Strategy](#test-strategy)
- [Open Questions](#open-questions)
- [Decision / Next Steps](#decision--next-steps)
- [Related Documentation](#related-documentation)

---

## Summary

This RFC introduces a first-class `Participant` abstraction to the Persatrix type system, making humans and agents symmetrical actors in the agent society. It adds a `UserParticipant` concrete type with SQLite-persisted identity, generalizes the existing `EpisodicMemory` and `RelationshipMemory` primitives to treat human users as recognized participants, and exposes a `persatrix chat <agent_id>` CLI command backed by a new `POST /api/v1/agents/{id}/chat` REST endpoint and `SendChatMessage` gRPC RPC. The result: a user can talk to a persona agent from the terminal, and the agent remembers them and builds a relationship with them over time.

## Motivation

v0.2.0 ships persona agents with memory, personality, and an autonomous tick loop. A developer can start an agent, observe its behavior, and inspect its memory state. What they cannot do is talk to it.

The current system treats the human as an observer sitting outside the agent society. `RelationshipMemory` tracks trust between agents. `EpisodicMemory` records interactions between agents. `EventDispatcher` routes events between agents. All of these primitives assume the participant is an agent, and they are correct — v0.2.0 never promised otherwise.

The gap this creates is commercial, not technical. Anyone who encounters Persatrix for the first time will ask within minutes: *how do I talk to one of these agents?* The answer today is: you cannot, not until v0.5.0 external bridges land. That is too long to wait for something this foundational.

**Why this is tractable now:**

Almost everything the feature needs already exists from RFC 0005:

- `EpisodicMemory` records "entity said thing at time" — it does not constrain entity type.
- `RelationshipMemory` tracks trust between entity pairs — same generalization applies.
- `EventDispatcher` routes `AgentEvent` objects where `sender_id` is a free-form string — human sender IDs already work.
- `EventType.MESSAGE_RECEIVED` is exactly the event type a persona agent uses when receiving a message from another agent.

The architectural work is generalizing three Python types from "agent-only" to "participant" and adding the thin REST + gRPC + CLI interface layer. This is a few hundred lines of code, not a new subsystem.

**What happens if we do nothing:**

- Every v0.2.0 user experiences Persatrix as a demo, not a product.
- Real-world feedback on persona behavior cannot be collected until v0.5.0 bridges.
- v0.3.0 channels (agent-to-agent) ship without a human-in-the-loop primitive, making it harder to build anything interactive on top.
- The `Participant` generalization deferred here has to happen eventually — every future RFC that touches memory or messaging will be cleaner if the abstraction already exists.

## Goals

1. Define a `Participant` Protocol in `agents/participant.py` with `participant_id: str`, `participant_type: str`, and `display_name: str`.
2. Implement `UserParticipant` as a concrete `Participant` with identity persisted to a new `users` table in the agent SQLite database.
3. Generalize `RelationshipMemory` to store relationships between any participant pair, not just agent–agent pairs. Schema migration adds `participant_type` columns and renames entity ID columns.
4. Generalize episodic memory interaction recording to accept any participant ID as sender — no schema change required (field is already a free-form string).
5. Add `SendChatMessage(ChatRequest) returns (ChatResponse)` RPC to `AgentService` in `task.proto`.
6. Add `POST /api/v1/agents/{id}/chat` REST endpoint to the Go orchestrator that accepts a user message and returns the agent's reply synchronously.
7. Route chat messages to the target agent via the new gRPC RPC. The agent processes them as `MESSAGE_RECEIVED` events with `participant_type: "user"` in `AgentEvent.metadata`.
8. Add `persatrix chat <agent_id>` as a new CLI subcommand that opens an interactive REPL session.
9. Persist the `UserParticipant` record in SQLite so the agent recognises the same user identity across sessions.

## Non-Goals

- **Multi-user concurrent sessions.** A single `UserParticipant` per session for v0.2.1. Multi-user support is RFC 0011 territory (v0.3.0).
- **Authentication or access control for user identity.** Sessions are local and caller-supplied for v0.2.1. Auth is RFC 0009 (v0.3.0).
- **Agent-initiated messages to users outside a session.** Notification infrastructure is deferred. Agents cannot push messages to a user who is not in an active chat session.
- **Channel routing for user messages.** Channels are RFC 0011 (v0.3.0). This RFC delivers point-to-point user–agent conversation only.
- **Web or GUI interfaces.** CLI only for v0.2.1.
- **Streaming chat responses.** v0.2.1 uses synchronous request-response. SSE streaming is a natural follow-up when channels arrive.
- **User memory management commands** (e.g., `persatrix memory clear --user <id>`).
- **Cross-session conversation threading.** Messages within a session are sequential. Threading is RFC 0011.

---

## Design / Implementation

### A. The `Participant` Abstraction

A new `agents/participant.py` module introduces a structural Protocol covering the minimal surface shared by agents, users, and any future participant type (system actors, external services):

```python
# agents/participant.py
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable
import time


@runtime_checkable
class Participant(Protocol):
    """Structural type for any entity that can participate in agent events."""

    participant_id: str
    participant_type: str  # "agent" | "user" | "system"
    display_name: str
```

`PersonaAgent` and `TaskAgent` satisfy `Participant` implicitly once they expose these three attributes. No changes to existing agent classes beyond adding the three fields to their `__init__` (they can default to existing `agent_id` / `"agent"` / configured name).

Using `Protocol` (structural subtyping) means existing agents do not need to inherit from a new base class. The abstraction is opt-in at the type-checking level and transparent at runtime.

### B. `UserParticipant` Concrete Type

```python
@dataclass
class UserParticipant:
    """A human user participating in the agent society."""

    participant_id: str
    display_name: str
    participant_type: str = "user"
    created_at: float = field(default_factory=time.time)
    last_seen_at: float = field(default_factory=time.time)
```

Users are created lazily on first chat session and retrieved by `participant_id` on subsequent sessions. For single-user local installs, `participant_id` defaults to `"local"` unless overridden via `--user <id>`. The underlying SQLite row uses `participant_id` as the primary key.

```python
class UserStore:
    """CRUD for UserParticipant records in the agent SQLite database."""

    async def get_or_create(self, db: aiosqlite.Connection, participant_id: str, display_name: str) -> UserParticipant: ...
    async def update_last_seen(self, db: aiosqlite.Connection, participant_id: str) -> None: ...
    async def get(self, db: aiosqlite.Connection, participant_id: str) -> UserParticipant | None: ...
```

`UserStore` shares the same database connection and migration infrastructure as `EpisodicMemory` and `RelationshipMemory`.

### C. Memory Generalization

**`RelationshipMemory` schema change:**

The `relationships` and `interactions` tables currently use `agent_id` / `other_agent_id`. Migration 3 renames these to `participant_id` / `other_participant_id` and adds a `participant_type` column to both tables. Existing rows are backfilled with `participant_type = "agent"`.

```sql
-- Migration 3 (excerpt)
ALTER TABLE relationships RENAME COLUMN agent_id TO participant_id;
ALTER TABLE relationships RENAME COLUMN other_agent_id TO other_participant_id;
ALTER TABLE relationships ADD COLUMN participant_type TEXT NOT NULL DEFAULT 'agent';
ALTER TABLE interactions RENAME COLUMN agent_id TO participant_id;
ALTER TABLE interactions RENAME COLUMN other_agent_id TO other_participant_id;
ALTER TABLE interactions ADD COLUMN participant_type TEXT NOT NULL DEFAULT 'agent';
```

The `RelationshipMemory` API surface changes minimally: `agent_id` parameters become `participant_id`, with `participant_type` added where needed. Callers that pass their own `agent_id` are unaffected in behavior; they now pass `participant_type="agent"` explicitly or via the updated default.

**`EpisodicMemory` — no schema change needed:**

Episodes already store `sender_id` as a free-form `TEXT` column. When a user sends a message, the episode is recorded with `sender_id = user_participant_id`. The recall and summarization paths are unaffected.

**New `users` table:**

```sql
-- Migration 3 (continued)
CREATE TABLE IF NOT EXISTS users (
    participant_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    participant_type TEXT NOT NULL DEFAULT 'user',
    created_at REAL NOT NULL,
    last_seen_at REAL NOT NULL
);
```

### D. Event Routing for User Messages

The full call chain for a chat message:

```
persatrix chat <agent_id>
    │  (interactive REPL, maintains session_id)
    │
    ▼ POST /api/v1/agents/{agent_id}/chat
      { "user_id": "local", "message": "Hello!", "session_id": "" }
    │
    ▼  Go orchestrator  ─── gRPC SendChatMessage(ChatRequest) ───▶  Python agent server
                                                                         │
                                                                         ▼
                                                                AgentServiceServicer.SendChatMessage()
                                                                         │
                                                                         ▼
                                                                EventDispatcher.dispatch(
                                                                    target_id=agent_id,
                                                                    event=AgentEvent(
                                                                        event_type=MESSAGE_RECEIVED,
                                                                        sender_id=user_id,
                                                                        metadata={"participant_type": "user"},
                                                                        payload={"content": message},
                                                                    )
                                                                )
                                                                         │
                                                                         ▼
                                                                _LLMPersonaAgent.on_event()
                                                                         │
                                                                         ▼
                                                                RelationshipMemory.record_interaction(
                                                                    participant_id=agent_id,
                                                                    other_participant_id=user_id,
                                                                    participant_type="user",
                                                                    ...
                                                                )
                                                                ChatResponse(reply=agent_reply)
    ◀──────────────────────────────────────────────────────────────────────────────────
    │
    ▼ 200 OK  { "reply": "...", "session_id": "uuid" }
    │
    ▼  printed to terminal
```

No changes to `EventDispatcher.dispatch()` or `_LLMPersonaAgent.on_event()` are required. The event shape is identical to an agent-to-agent `MESSAGE_RECEIVED`; only `sender_id` and `metadata["participant_type"]` differ.

The `SendChatMessage` gRPC call is synchronous (unary RPC). The agent's reply is extracted from the `SEND_MESSAGE` or `COMPLETE_TASK` action produced by `on_event()` and returned in `ChatResponse.reply`.

### E. Proto Extension

Add to `proto/task.proto` (existing `AgentService`):

```proto
service AgentService {
  // Existing RPCs...
  rpc ExecuteTask(TaskRequest) returns (TaskResponse);
  rpc ExecuteTaskStream(TaskRequest) returns (stream TaskStreamEvent);
  rpc HealthCheck(HealthCheckRequest) returns (HealthCheckResponse);

  // New in RFC 0016
  rpc SendChatMessage(ChatRequest) returns (ChatResponse);
}

message ChatRequest {
  string agent_id   = 1;
  string user_id    = 2;
  string message    = 3;
  string session_id = 4;  // opaque per-session UUID; empty = create new session
}

message ChatResponse {
  string reply      = 1;
  string session_id = 2;
  string agent_id   = 3;
  int64  timestamp  = 4;
}
```

No changes to `ChannelService` or `AgentMessage` in `agent_message.proto` — those are reserved for RFC 0011 channel routing.

### F. REST API for Chat

New endpoint on the Go orchestrator:

```
POST /api/v1/agents/{agent_id}/chat
Content-Type: application/json

{
  "user_id":    "local",    // optional; defaults to "local"
  "message":    "Hello!",
  "session_id": ""          // optional; empty = new session
}

HTTP 200 OK
{
  "reply":      "Hey there — I've been waiting for something interesting...",
  "session_id": "a3f7e2b1-...",
  "agent_id":   "nexus-7",
  "timestamp":  1745030400
}

HTTP 400  agent_id missing or message empty or message exceeds length limit
HTTP 404  agent not found in registry
HTTP 503  agent gRPC call failed
```

The orchestrator looks up the agent gRPC address via `Registry`, calls `SendChatMessage`, and returns the response. No session state is stored in the orchestrator — session continuity lives in the agent's episodic and relationship memory.

**Message length limit**: The orchestrator rejects messages longer than `chat_max_message_length` (default: 4000 characters, configurable). This prevents token exhaustion attacks and is the only input validation applied at this layer.

### G. `persatrix chat` CLI Command

New Rust subcommand:

```
persatrix chat <agent_id> [--user <user_id>]
```

Behavior:

1. Connects to the orchestrator REST API (uses the same `--orchestrator` / env-var config as other commands).
2. Prints a connection banner: `Connected to <agent_id>. Type 'exit' or Ctrl-C to quit.`
3. Enters a REPL loop:
   - Prompts `You: `, reads a line from stdin.
   - On `exit` or empty EOF, exits cleanly.
   - POSTs to `POST /api/v1/agents/{agent_id}/chat` with the message.
   - Prints `<agent_id>: <reply>`.
   - Repeats.
4. The `session_id` received from the first response is reused for all subsequent messages in the same REPL process.
5. History is not persisted client-side — it lives in the agent's memory.

```
$ persatrix chat nexus-7
Connected to nexus-7. Type 'exit' or Ctrl-C to quit.
You: Hello, who are you?
nexus-7: I'm nexus-7, a research agent with a particular interest in...
You: What have you been thinking about lately?
nexus-7: Honestly? I've been turning over a problem one of the other agents raised...
You: exit
$
```

### H. Prompt Injection for User Participants

The existing `_inject_memory_context()` in `agents/persona_runtime/memory_context.py` injects relationship summaries into the LLM prompt. When `sender_id` corresponds to a `UserParticipant` (`participant_type == "user"`), the prompt section should label the sender as "a human user" rather than "an agent", so the persona responds with appropriate social registers.

This is a targeted change to `_format_relationship_summary()` — one conditional branch:

```python
if relationship.participant_type == "user":
    label = f"Human user '{relationship.other_participant_id}'"
else:
    label = f"Agent '{relationship.other_participant_id}'"
```

No other changes to the LLM loop or prompt construction are required.

---

## Security Considerations

1. **Prompt injection via user input.** User messages are injected into the LLM system prompt as conversation context. The existing `_sanitize_tool_input()` path (RFC 0005) applies only to tool call arguments, not to natural language conversation. For v0.2.1 (single local user, no auth), this risk is self-inflicted. A follow-up mitigation for multi-user scenarios is tracked as Open Question 5.

2. **User identity without authentication.** `user_id` is caller-supplied with no verification. Any caller can impersonate any `user_id`. For v0.2.1 (local CLI, single trusted user) this is acceptable. When network-accessible multi-user deployments arrive, RFC 0009 identity tokens will gate this endpoint; `user_id` will become a claim inside a verified token, not a raw parameter.

3. **Memory isolation.** User relationship records and episodic interactions are physically co-located with agent-agent records in the same SQLite database, separated only by `participant_type`. No isolation boundary exists for v0.2.1. Revisit when multi-user support is added.

4. **Message length as a token exhaustion vector.** An unbounded message length allows a caller to force the agent into an arbitrarily long LLM context. The REST layer enforces a configurable ceiling (default: 4000 characters). The orchestrator returns HTTP 400 for oversized messages before the gRPC call is made.

5. **Session ID reuse.** Session IDs are UUIDs generated server-side on the first `ChatRequest` with an empty `session_id`. They are opaque tokens with no associated state in the orchestrator. Replaying a session ID from a previous process simply continues the conversation — there is no session invalidation mechanism in v0.2.1. For a local CLI this is acceptable behavior.

6. **No rate limiting on the chat endpoint.** RFC 0009 rate limiting (v0.3.0) does not yet apply to the chat endpoint. For v0.2.1, the existing RFC 0006 budget gating (token caps and LLM call limits) bounds runaway spending per agent.

---

## Phased Implementation Plan

### Phase 1 — Participant Abstraction & Memory Generalization

**Summary**: Introduce the `Participant` Protocol and `UserParticipant` type, apply the SQLite schema migration, and generalize `RelationshipMemory` to participant pairs. No user-facing change; this phase prepares the data layer.

**Deliverables**:
1. `agents/participant.py` — `Participant` Protocol + `UserParticipant` dataclass + `UserStore`
2. `agents/memory/migrations.py` — Migration 3: `users` table + `participant_type` columns on `relationships` and `interactions` tables
3. `agents/memory/relationship.py` — rename `agent_id`/`other_agent_id` → `participant_id`/`other_participant_id`, add `participant_type` parameter with `"agent"` default
4. `agents/persona_runtime/memory_context.py` — label user participants distinctly in prompt injection
5. Unit tests: `UserParticipant` CRUD, relationship memory with user participant type, migration idempotency

**Dependencies**: RFC 0005 ✅ Implemented.

### Phase 2 — Proto, gRPC & REST Wiring

**Summary**: Extend the proto contract, implement the gRPC servicer method, and add the REST endpoint. After this phase, a curl command can deliver a message to an agent and receive a reply.

**Deliverables**:
1. `proto/task.proto` — add `SendChatMessage` RPC + `ChatRequest` / `ChatResponse` messages
2. `make proto` — regenerate Go and Python stubs
3. `agents/server.py` — implement `SendChatMessage` servicer: build `AgentEvent`, call `EventDispatcher.dispatch()`, extract reply from actions
4. `internal/server/` — `POST /api/v1/agents/{id}/chat` handler with message length validation
5. `internal/executor/` or `internal/chat/` — gRPC `SendChatMessage` call with the existing agent connection pool
6. Integration test: full round-trip via REST + gRPC

**Dependencies**: Phase 1 complete. `make proto` available.

### Phase 3 — CLI Chat Command

**Summary**: Implement the interactive `persatrix chat` REPL. After this phase, `persatrix chat <agent_id>` works end-to-end from a terminal.

**Deliverables**:
1. `cli/src/commands/chat.rs` — REPL loop: read line, POST to REST API, print reply, maintain `session_id`
2. `cli/src/main.rs` — wire `chat` subcommand into the command enum
3. Manual test: `persatrix chat <agent_id>` end-to-end with a running persona agent

**Dependencies**: Phase 2 complete.

---

## Files Touched (Estimated)

**Python agents (`agents/`)**:
- `agents/participant.py` — new file (~80 lines)
- `agents/memory/migrations.py` — Migration 3 SQL (~30 lines)
- `agents/memory/relationship.py` — entity ID field renaming, `participant_type` parameter (~40 lines changed)
- `agents/server.py` — `SendChatMessage` servicer method (~50 lines)
- `agents/persona_runtime/memory_context.py` — user participant label branch (~10 lines)

**Go orchestrator (`internal/`)**:
- `internal/server/` — `POST /api/v1/agents/{id}/chat` handler + routing (~80 lines)
- `internal/executor/` or `internal/chat/` — `SendChatMessage` gRPC call (~60 lines)
- `internal/generated/` — regenerated stubs (generated, do not edit)

**Rust CLI (`cli/`)**:
- `cli/src/commands/chat.rs` — new file (~100 lines)
- `cli/src/main.rs` — subcommand wiring (~10 lines changed)

**Proto (`proto/`)**:
- `proto/task.proto` — `SendChatMessage` RPC + 2 messages (~20 lines)

**Tests**:
- `tests/unit/python/test_participant.py` — new
- `tests/unit/python/test_relationship_memory_user.py` — new
- `tests/integration/test_chat_endpoint.py` — new

**Config**:
- `config/agents.yaml` — no schema changes required for v0.2.1

---

## Test Strategy

**Unit tests (Python)**:
- `UserParticipant.get_or_create()` creates a new record on first call and returns the existing record on subsequent calls.
- `UserStore.update_last_seen()` updates `last_seen_at` without modifying other fields.
- `RelationshipMemory.record_interaction()` with `participant_type="user"` stores a row with the correct type and is retrieved by `get_relationship_summary()`.
- `RelationshipMemory` trust decay behaves identically for user and agent participant types.
- Migration 3 is idempotent (run twice, no error, no duplicate rows).
- All existing relationship memory tests pass after the entity ID rename (backfill correctness).

**Unit tests (Go)**:
- `POST /api/v1/agents/{id}/chat` returns HTTP 400 for empty `message`.
- `POST /api/v1/agents/{id}/chat` returns HTTP 400 for `message` exceeding the length limit.
- `POST /api/v1/agents/{id}/chat` returns HTTP 404 for an unknown `agent_id`.
- `SendChatMessage` gRPC call returns the agent reply extracted from the `COMPLETE_TASK` action.

**Integration tests**:
- Full round-trip: REST POST → orchestrator → gRPC → agent `on_event()` → relationship memory updated → reply returned.
- Second message in the same session: agent relationship memory contains an interaction record from the first message.

**Manual tests**:
- `persatrix chat <agent_id>`: banner printed, messages exchanged, agent stays in character.
- `exit` and Ctrl-C both terminate the REPL cleanly.
- Reconnecting to the same agent with the same `--user` ID: agent's response references the prior session (confirms relationship memory persistence).

---

## Open Questions

**1. Free-form vs. UUID-backed `user_id`?**
Free-form IDs (e.g., `"local"`, `"alice"`) are ergonomic for local use. UUID-backed IDs are better for future multi-user disambiguation. *Proposed default*: free-form for v0.2.1 with no validation beyond non-empty check. Migration to UUID-backed IDs can be done in the same pass as multi-user support (v0.3.0).

**2. Synchronous vs. streaming chat response?**
Synchronous (unary gRPC, blocking REST) is simpler for v0.2.1 and sufficient for short persona responses. Streaming (SSE on REST side, server-streaming gRPC) is better for long-running agentic responses. *Proposed default*: synchronous for v0.2.1. Streaming is a follow-up in v0.3.0, likely as part of channel message delivery.

**3. How to handle the `RelationshipMemory` column rename in production databases?**
SQLite supports `ALTER TABLE RENAME COLUMN` since version 3.25.0 (2018). Persatrix requires Python 3.11+ and the `aiosqlite` package, which bundles an adequate SQLite version on all supported platforms. The migration runs `RENAME COLUMN` with `UPDATE ... SET participant_type = 'agent' WHERE participant_type IS NULL` to backfill existing rows. *Decision*: use `ALTER TABLE RENAME COLUMN` in Migration 3 with backfill.

**4. Should `participant_type` be an enum or a free-form string?**
Free-form (`"agent"`, `"user"`) is extensible to future types (`"system"`, `"service"`, `"role"`). An enum enforces the allowed set at the application layer. *Proposed default*: free-form string for v0.2.1, validated against an allowlist (`{"agent", "user"}`) at the storage boundary. New types are added to the allowlist when a defining RFC lands.

**5. Prompt injection risk for multi-user scenarios?**
For v0.2.1 (single local user) the risk is self-inflicted and acceptable. For future multi-user scenarios, user-supplied message content must be clearly delimited in the LLM prompt to prevent injection attacks. *Proposed default*: add a `[User message]` / `[End user message]` wrapper to the prompt injection site in Phase 1, even though it is not strictly required for v0.2.1. This is a low-cost future-proofing measure.

**6. How does the agent produce a `reply` string from `on_event()` actions?**
`on_event()` returns a list of `AgentAction` objects. The `SendChatMessage` servicer extracts the reply by looking for the first `COMPLETE_TASK` action (`payload["result"]`) or `SEND_MESSAGE` action (`payload["content"]`). If neither is present (e.g., agent returned `DO_NOTHING`), the servicer returns an empty string. *Proposed default*: check `SEND_MESSAGE` first (more natural for conversational responses), then fall back to `COMPLETE_TASK`. Log a warning on `DO_NOTHING` so operators can tune the agent's conversational behavior.

---

## Decision / Next Steps

**Status**: 📋 Proposed — open for review.

**Prerequisites for implementation:**
- RFC 0005 ✅ Implemented — all memory and event dispatch primitives are in place.
- No other blocking RFC dependencies.

**Estimated scope**: 3 PRs, one per phase. Each PR is independently deployable. Total estimated line count: ~480 lines new/changed across Python, Go, Rust, and proto.

**Recommended next step**: Accept this RFC and begin Phase 1 (participant abstraction and memory generalization) as the first PR after v0.2.0 release.

---

## Related Documentation

- [RFC 0005 — Persona Agent & Memory System](0005-persona-agent-memory.md) — episodic memory, relationship memory, and event dispatch that this RFC extends
- [RFC 0009 — Agent Identity, Security & Sandboxing](0009-security-sandboxing.md) — future dependency for user authentication and identity tokens
- [RFC 0011 — Channels + Bridges](../rfcs/README.md) — future dependency for multi-user channel routing and external bridges
- [Architecture overview](../../.github/copilot-instructions.md)
- [Persatrix Roadmap](../../ROADMAP.md)
