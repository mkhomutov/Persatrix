# RFC 0011 — Channels & Internal Agent Messaging

**Type**: feature  
**Status**: 📋 Proposed  
**Author**: Maksim Khomutov  
**Date**: 2026-04-25  
**Target**: v0.3.0 (internal channels) + v0.5.0 (external bridges)  
**Depends on**: RFC 0005, RFC 0008, RFC 0009 Phases 1–2

---

## Table of Contents

- [Summary](#summary)
- [Motivation](#motivation)
- [Goals](#goals)
- [Non-Goals](#non-goals)
- [Design / Implementation](#design--implementation)
  - [A. Channel Model](#a-channel-model)
  - [B. Channel Store](#b-channel-store)
  - [C. Message Routing and Delivery](#c-message-routing-and-delivery)
  - [D. Agent Integration](#d-agent-integration)
  - [E. Memory Integration](#e-memory-integration)
  - [F. Human Participant Channels](#f-human-participant-channels)
  - [G. Channel Observability](#g-channel-observability)
- [Security Considerations](#security-considerations)
- [Phased Implementation Plan](#phased-implementation-plan)
- [Files Touched (Estimated)](#files-touched-estimated)
- [Test Strategy](#test-strategy)
- [Open Questions](#open-questions)
- [Decision / Next Steps](#decision--next-steps)
- [Related Documentation](#related-documentation)

---

## Summary

This RFC introduces internal agent channels — named message buses that allow agents to participate in group conversations, send direct messages, and form threaded discussions. Agents subscribe to channels via configuration, publish messages as actions, and receive channel messages as events. Channel history is stored persistently and injected into agent context via the RFC 0008 memory framework.

The v0.3.0 scope is internal-only: channels operate within a single Persatrix deployment. External bridges (Slack, Discord, email) are deferred to v0.5.0.

## Motivation

v0.2.x established the building blocks: persona agents with memory and relationships (RFC 0005), bounded resource execution (RFC 0006), human chat (RFC 0016), and memory injection budgets (RFC 0017). But agents cannot yet communicate with each other directly. Every agent interaction is mediated by a human (via `persatrix chat`) or encoded into a pre-planned YAML workflow step.

This means:

1. **Agents cannot negotiate or collaborate autonomously.** A code reviewer cannot ask a code writer for clarification. A planner cannot get a second opinion from a domain expert without the interaction being pre-scripted into a workflow.

2. **Relationships exist but have no organic expression.** RFC 0005 tracks trust and interaction history, but agents have no channel through which those relationships can deepen naturally and unprompted.

3. **The v0.3.0 user-facing promise is undeliverable without this RFC.** "Give agents a shared channel and watch them talk, negotiate, and form opinions over time" requires infrastructure that does not yet exist.

## Goals

1. **Named group channels**: Agents subscribe to named channels and publish or receive messages within them.
2. **Direct messages**: Agents send point-to-point messages to specific other agents.
3. **Threaded replies**: Agents reply to specific messages, forming persistent discussion threads.
4. **Persistent history**: Channel messages are stored and retrievable across restarts.
5. **Memory integration**: Channel history is injected into agent context via RFC 0008's memory facade, budget-aware.
6. **Human participation**: Humans can join channels via the existing RFC 0016 `Participant` abstraction without new infrastructure.
7. **Observable**: Channel message counts, delivery latency, and dropped messages are tracked via the RFC 0019 telemetry pipeline.

## Non-Goals

- External bridges (Slack, Discord, Telegram, email) → v0.5.0.
- Cross-node channel federation → v0.6.0.
- Rich message types (file attachments, embeds, reactions) → future.
- Channel moderation or access control beyond RFC 0009 rate limiting and membership.
- Real-time streaming push to the CLI (SSE/WebSocket for continuous channel activity) → future; v0.3.0 uses polling.
- Guaranteed exactly-once delivery — at-most-once delivery is sufficient for v0.3.0.

---

## Design / Implementation

### A. Channel Model

Three channel types:

| Type | Description | Addressing |
|------|-------------|------------|
| Named channel | Group channel with a declared name (e.g., `#planning`) | `channel:<name>` |
| Direct message | Point-to-point between two participants | `dm:<participant_a>:<participant_b>` |
| Thread | Reply chain anchored to a specific message | `thread:<message_id>` |

**Channel configuration** (`config/channels.yaml`):

```yaml
max_channels: 50   # global cap on named group channels; DMs and threads not counted

channels:
  - name: planning
    description: "Strategy and planning discussions"
    members:
      - id: planner-agent
        respond: when_mentioned       # default; see §D Response Gating
      - id: code-writer
        respond: when_mentioned
      - code-reviewer                 # shorthand: equivalent to {id: code-reviewer, respond: when_mentioned}

  - name: code-review
    description: "Code review coordination"
    members:
      - id: code-writer
        respond: always               # tight-loop pair channel — every message triggers a reply cycle
      - id: code-reviewer
        respond: always
```

Agents declare channel membership via configuration. An agent not listed in a channel's `members` list cannot receive messages from that channel and receives a 403 error if it attempts to publish. DMs are opened on-demand and do not require pre-declaration; both participants must be registered agents or users.

Each `members` entry is either a participant ID string (defaulting to `respond: when_mentioned`) or an object with `id` and `respond` fields. The `respond` policy controls the per-membership response gate defined in §D and is the primary mechanism for preventing N²-fanout feedback loops in multi-agent channels.

**Message schema**:

```python
@dataclass
class ChannelMessage:
    id: str                        # UUID
    channel_id: str                # e.g., "planning" or "dm:agent-a:agent-b"
    channel_type: ChannelType      # GROUP | DM | THREAD
    sender_id: str                 # agent or user participant ID
    content: str                   # message text
    timestamp: datetime
    thread_id: str | None          # parent message ID for threaded replies; None for top-level
    mentions: list[str]            # participant IDs explicitly @-mentioned
    metadata: dict[str, Any]       # extensible; reserved for future attachment types
```

### B. Channel Store

`internal/channels/` — Go-side channel storage.

**Storage**: SQLite database at a configurable path (default: `data/channels.db`), separate from agent memory databases to avoid coupling agent state with channel state. WAL mode, consistent with all other SQLite usage in the project.

**Schema**:

```sql
CREATE TABLE channels (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    channel_type TEXT NOT NULL,   -- 'group' | 'dm' | 'thread'
    description TEXT,
    created_at  DATETIME NOT NULL
);

CREATE TABLE memberships (
    channel_id     TEXT NOT NULL REFERENCES channels(id),
    participant_id TEXT NOT NULL,
    respond_policy TEXT NOT NULL DEFAULT 'when_mentioned',  -- 'when_mentioned' | 'always' | 'never'
    joined_at      DATETIME NOT NULL,
    PRIMARY KEY (channel_id, participant_id)
);

CREATE TABLE messages (
    id         TEXT PRIMARY KEY,
    channel_id TEXT NOT NULL REFERENCES channels(id),
    sender_id  TEXT NOT NULL,
    content    TEXT NOT NULL,
    timestamp  DATETIME NOT NULL,
    thread_id  TEXT REFERENCES messages(id),
    mentions   TEXT NOT NULL DEFAULT '[]',   -- JSON array of participant IDs
    metadata   TEXT NOT NULL DEFAULT '{}'    -- JSON object
);

CREATE INDEX idx_messages_channel_ts ON messages(channel_id, timestamp DESC);
CREATE INDEX idx_messages_thread ON messages(thread_id) WHERE thread_id IS NOT NULL;
```

**`ChannelStore` interface** (Go):

```go
type ChannelStore interface {
    CreateChannel(ctx context.Context, ch Channel) error
    GetChannel(ctx context.Context, id string) (Channel, error)
    ListChannels(ctx context.Context) ([]Channel, error)
    AddMember(ctx context.Context, channelID, participantID, respondPolicy string) error
    GetMembers(ctx context.Context, channelID string) ([]Member, error)
    GetMember(ctx context.Context, channelID, participantID string) (Member, error)
    IsMember(ctx context.Context, channelID, participantID string) (bool, error)
    PublishMessage(ctx context.Context, msg ChannelMessage) error
    GetHistory(ctx context.Context, channelID string, limit int, before time.Time) ([]ChannelMessage, error)
    GetThread(ctx context.Context, threadID string) ([]ChannelMessage, error)
}

type Member struct {
    ParticipantID string
    RespondPolicy string    // "when_mentioned" | "always" | "never"
    JoinedAt      time.Time
}
```

A per-channel message cap (default: 10,000 messages) prevents unbounded store growth. When the cap is reached, oldest messages are pruned on each new write, consistent with RFC 0008's episodic memory cap approach.

A global channel-count cap (default: 50 named channels, configurable via `channels.max_channels` in `config/channels.yaml`) bounds the membership table size, observability label cardinality, and the operator-visible namespace. The cap applies to named group channels only — DMs and threads are addressed implicitly by participant pair or parent message ID and are not counted against this limit. Exceeding the cap at startup is a config validation error; channel creation via the REST endpoint returns 409 Conflict when the cap is reached.

### C. Message Routing and Delivery

**Architecture**: The orchestrator owns message routing. Agents publish to channels as actions; the orchestrator stores the message and dispatches it to subscribers.

**Publish flow**:

```
Agent A publishes to #planning
    ↓
ActionType.SEND_CHANNEL_MESSAGE (Python persona action)
    ↓
ActionExecutor → POST /api/v1/channels/{id}/messages
    ↓
ChannelRouter (Go): store message → look up members → filter to registered agents
    ↓
For each registered subscriber: DispatchChannelMessage → ReceiveChannelMessage gRPC call
    ↓
Agent B servicer: create AgentEvent(event_type=CHANNEL_MESSAGE) → dispatch to on_event()
```

**New REST endpoints**:

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/v1/channels` | Create a channel |
| `GET` | `/api/v1/channels` | List channels |
| `GET` | `/api/v1/channels/{id}` | Channel info and member list |
| `POST` | `/api/v1/channels/{id}/messages` | Publish a message |
| `GET` | `/api/v1/channels/{id}/messages` | Channel history (paginated, newest-first) |
| `GET` | `/api/v1/channels/{id}/messages/{msg_id}/thread` | Thread replies |
| `POST` | `/api/v1/channels/{id}/members` | Add a participant to a channel |

**New proto definitions**:

```protobuf
// Additions to proto/task.proto

message ChannelMessageEvent {
    string message_id  = 1;
    string channel_id  = 2;
    string channel_type = 3;   // "group" | "dm" | "thread"
    string sender_id   = 4;
    string content     = 5;
    string timestamp   = 6;    // RFC 3339
    string thread_id   = 7;    // empty string if not a reply
    repeated string mentions = 8;
}

service AgentService {
    // existing RPCs ...
    rpc ReceiveChannelMessage(ChannelMessageEvent) returns (TaskAck);
}
```

**Delivery guarantees**: At-most-once delivery in v0.3.0. The orchestrator dispatches to each registered subscriber in a best-effort manner. Agents offline at delivery time miss the message but can retrieve missed messages via the history endpoint. Exactly-once delivery with durable queuing is deferred to a future RFC.

**Agent-initiated publish** — Python action type:

```python
@dataclass
class SendChannelMessageAction:
    action_type: ActionType = ActionType.SEND_CHANNEL_MESSAGE
    channel_id: str           # e.g., "planning" or "dm:other-agent-id"
    content: str
    thread_id: str | None     # None for a new top-level message
    mentions: list[str]       # participant IDs to @-mention
```

`ActionExecutor` handles `SEND_CHANNEL_MESSAGE` by calling the REST endpoint. The sender_id is the publishing agent's registered ID, injected by the framework — agents cannot spoof another sender's identity.

### D. Agent Integration

**Receiving messages** — new `EventType`:

```python
class EventType(str, Enum):
    # existing types ...
    CHANNEL_MESSAGE = "channel_message"
```

When a `ReceiveChannelMessage` gRPC call arrives, the agent servicer creates an `AgentEvent` and dispatches it to `on_event()`. The persona runtime handles it via the existing event loop — no special casing required.

**Extended `AgentEvent` fields** (channel-specific):

```python
@dataclass
class AgentEvent:
    event_type: EventType
    sender_id: str
    content: str
    timestamp: datetime
    # Channel-specific fields (None for non-channel events):
    channel_id: str | None = None
    thread_id: str | None = None
    mentions: list[str] = field(default_factory=list)
```

**Agent behavior on channel messages**: The persona runtime's `on_event()` path handles `CHANNEL_MESSAGE` without modification to the core loop. The agent receives the event, the memory injection pipeline (RFC 0008) pulls relevant channel history and episodic memory, the LLM decides whether and how to respond, and the resulting action may be a `SEND_CHANNEL_MESSAGE` reply to the same channel or thread. Agents can also initiate channel messages autonomously during their tick loop without waiting for an incoming event.

**Response gating (loop prevention)**: A naive "every `CHANNEL_MESSAGE` event invokes the LLM" model is unworkable in multi-agent channels. With N agents in a channel, every message would generate up to N inferences, each of which is itself a message that re-triggers the others — O(N²) feedback amplification per turn, with token cost and latency blowups. The persona runtime therefore applies a **pre-LLM response gate** to incoming channel events. The gate is evaluated on the agent process before any memory recall or LLM call, using the per-membership `respond` policy from §A:

| Policy | Triggers a response cycle when ... |
|--------|------------------------------------|
| `when_mentioned` (default) | the agent's ID appears in `event.mentions`, **or** the message is a thread reply to a message this agent authored |
| `always` | every channel message except the agent's own |
| `never` | no message — the agent observes the channel for memory ingestion only (read-only participant) |

When the gate suppresses a message, the event is still stored in episodic memory (per §E) — the agent stays aware of channel context for later retrieval — but no LLM call is made and no action is dispatched. This bounds inference cost and is the primary structural defense against runaway exchanges; RFC 0009 Phase 1 rate limiting is a second-line defense if a misconfigured `always` membership produces a tight loop.

Additional invariants:

- An agent never receives its own `CHANNEL_MESSAGE` event — the orchestrator filters by `sender_id != subscriber_id` during fanout. This is enforced regardless of policy.
- The `when_mentioned` thread-reply branch fires only when *another participant* replies to a message the agent authored. An agent's own thread continuation does not retrigger itself.
- The autonomous tick loop is unaffected: agents may initiate channel messages on their own schedule. The gate only governs *reactive* replies to incoming events.
- Agents that publish via `SEND_CHANNEL_MESSAGE` set `mentions` explicitly. The Rust CLI offers a `--mention <id>` repeatable flag and a `--mention-all` shorthand that expands to every channel member, addressing the human-in-the-loop case where a human wants every agent in a channel to react.

### E. Memory Integration

Channel history is integrated with agent memory via RFC 0008's `MemoryFacade`. This is the hard dependency that makes RFC 0008 a prerequisite for RFC 0011 Phase 3.

**Storing channel messages**: When a `CHANNEL_MESSAGE` event is processed, the agent stores it in episodic memory with channel-scoped tags:

```python
# Inside on_event() for CHANNEL_MESSAGE
await self.memory.store_observation(
    content=f"[#{event.channel_id}] {event.sender_id}: {event.content}",
    scope="channel",
    tags=["channel", event.channel_id, event.sender_id],
    importance=self._compute_message_importance(event),
    ttl=None,  # channel history persists until evicted by capacity policy
)
```

**Retrieving channel history** during context assembly:

```python
channel_history = await self.memory.retrieve_relevant(
    query=event.content,
    scope="channel",
    tags=[event.channel_id],
    limit=budget_channel_tokens // AVG_MESSAGE_TOKENS,  # budget-aware
)
```

**Budget allocation**: Channel history competes with episodic and relationship memory for the `budget_memory_tokens` allocation from RFC 0008. RFC 0017's `MemoryBudget` allocator is extended to include a `channel_history` tier with a configurable token ceiling. The default split reserves 25% of `budget_memory_tokens` for channel history, 50% for episodic, and 25% for relationship memory — adjustable per agent in `config/agents.yaml`.

**Relationship updates**: Receiving a channel message from another agent updates the relationship trust score for that agent, consistent with RFC 0005. Channel interaction frequency feeds into the trust decay/growth algorithm alongside direct chat interactions from RFC 0016.

### F. Human Participant Channels

Humans can join channels via the RFC 0016 `Participant` abstraction. No new infrastructure is required — `UserParticipant` is already a `Participant` type compatible with the membership model.

**CLI integration** — new Rust CLI subcommand group:

```
persatrix channel list                           # list available channels and member counts
persatrix channel join <name>                    # add current user to channel membership
persatrix channel send <name> <message>          # publish a message (top-level)
persatrix channel reply <channel> <msg_id> <msg> # reply to a specific message (threaded)
persatrix channel history <name> [--limit N]     # display recent history (newest-first)
persatrix channel watch <name> [--interval N]    # poll for new messages (default: 5s interval)
```

`persatrix channel watch` uses polling in v0.3.0 (consistent with the existing `persatrix logs --follow` polling mode). SSE-based streaming reuses the RFC 0018 LogService SSE infrastructure as a future extension.

### G. Channel Observability

**OTEL metrics** (via RFC 0019 pipeline):

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| `channel.messages.published` | counter | `channel_id`, `sender_type` | Messages published per channel |
| `channel.messages.delivered` | counter | `channel_id`, `status` | Delivery attempts (ok / failed) |
| `channel.messages.gated` | counter | `channel_id`, `subscriber_id`, `policy` | Events suppressed by the response gate (incremented on the agent side); the primary signal for diagnosing an under- or over-tuned `respond` policy |
| `channel.delivery.latency_ms` | histogram | `channel_id` | Publish-to-delivery latency per subscriber |
| `channel.members.active` | gauge | `channel_id` | Current registered subscriber count |
| `channel.history.recall_tokens` | histogram | `channel_id` | Tokens consumed by channel history injection |

**Spans**: Each message publish and per-subscriber delivery is wrapped in an OTEL span with `channel.id`, `message.id`, and `sender.id` attributes. Delivery spans link to the publishing span via Span Link, consistent with RFC 0019's A2A causality model.

---

## Security Considerations

- **Sender spoofing**: The `sender_id` is set by the orchestrator from the publishing agent's registration record, not from the request body. Agents cannot forge another sender's identity.
- **Membership enforcement**: Only agents listed in a channel's membership can publish or receive. The orchestrator checks membership on every publish attempt and returns 403 on violation. Delivery to non-registered subscribers is skipped silently (agent is offline, not unauthorized).
- **Rate limiting**: RFC 0009 Phase 1 rate limiting applies to channel publish calls. An agent spamming a channel hits its per-agent-per-window call limit and is circuit-broken.
- **Content injection**: Channel message content is stored in episodic memory and later injected into agent context. Adversarial content in channel messages represents the same prompt injection risk as tool output. Mitigation: RFC 0009 Phase 1 input sanitization applies to channel message content before it is stored.
- **History amplification**: A busy channel with large messages could dominate the memory injection budget. Mitigation: RFC 0008's `budget_channel_tokens` allocation cap in `MemoryBudget` prevents channel history from crowding out other memory tiers.
- **DM privacy**: DM channels are accessible only to the two declared participants. Membership is enforced at the store layer. DM content is stored in each participant's isolated episodic memory, not in a shared channel store visible to other agents.
- **Store growth**: Per-channel message cap (default 10,000), global channel-count cap (default 50 named channels), and the SQLite WAL checkpoint policy prevent unbounded disk growth and bound observability cardinality.

---

## Phased Implementation Plan

### Phase 1: Channel Store and REST Routing

**Summary**: Build the Go-side channel store, REST endpoints, and fanout routing engine. Testable end-to-end via REST with `curl` — no agent-side changes yet.

**Deliverables**:
1. `internal/channels/` package: `Channel`, `ChannelMessage`, `ChannelStore` interface, SQLite implementation with migration runner.
2. `ChannelRouter`: publish-and-fanout logic; dispatches to registered agent gRPC addresses (uses existing registry lookup).
3. REST endpoints: create channel, list channels, publish message, get history, get thread, add member.
4. `config/channels.yaml` loading at orchestrator startup; channel and membership initialization.
5. `schemas/channels.schema.json` for config validation.
6. Unit tests: channel store CRUD, membership enforcement, history pagination, message cap pruning.

**Dependencies**: None — the channel store is standalone and has no RFC 0008/0009 dependency at this phase.

### Phase 2: Proto and Agent Delivery

**Summary**: Wire message delivery from orchestrator to agent gRPC servers and add agent-side publish action.

**Deliverables**:
1. `ChannelMessageEvent` proto message + `ReceiveChannelMessage` RPC added to `AgentService`.
2. Go executor: `DispatchChannelMessage` method that calls `ReceiveChannelMessage` on each subscriber using the existing gRPC connection pool.
3. Python servicer: handle `ReceiveChannelMessage`, construct `AgentEvent(event_type=CHANNEL_MESSAGE)`, dispatch to `on_event()`.
4. `ActionType.SEND_CHANNEL_MESSAGE` added to `persona_types.py`.
5. `ActionExecutor` handler: calls `POST /api/v1/channels/{id}/messages` with the agent's sender ID.
6. **Response gate** in the persona runtime: filters incoming `CHANNEL_MESSAGE` events by the per-membership `respond` policy (`when_mentioned` / `always` / `never`) before any memory recall or LLM invocation. Suppressed events are dropped at this phase and increment the `channel.messages.gated` counter; memory ingestion of suppressed events is added in Phase 3.
7. Integration test: two persona agents (`ember-owl` and a second agent) exchange one message via a named channel; a third agent in the channel with `respond: when_mentioned` and no mention sees `channel.messages.gated` increment and produces no reply.

**Dependencies**: Phase 1.

### Phase 3: Memory Integration

**Summary**: Wire channel history into RFC 0008's memory injection pipeline and update relationship memory.

**Deliverables**:
1. `CHANNEL_MESSAGE` event handler stores message to episodic memory with channel scope and tags.
2. `MemoryBudget` allocator extended with a `channel_history` tier; default 25% of `budget_memory_tokens`.
3. `MemoryFacade.retrieve_relevant()` supports channel-scoped recall via tag filter.
4. Relationship memory: channel interactions increment interaction count and influence trust score.
5. Integration test: agent B's reply to agent A's channel message demonstrates awareness of channel history (verifiable via `persatrix logs` trace).

**Dependencies**: Phase 2; RFC 0008 Phase 2 (`MemoryFacade` for task agents — provides the `store_observation` and `retrieve_relevant` API used here).

### Phase 4: CLI and Human Participation

**Summary**: Add `persatrix channel` subcommand group, human membership support, and manual test suite.

**Deliverables**:
1. Rust CLI: `channel` subcommand group with `list`, `join`, `send`, `reply`, `history`, `watch` commands.
2. `UserParticipant` channel membership wired through `POST /api/v1/channels/{id}/members`.
3. `persatrix channel watch` polling loop (5s default interval, configurable via `--interval`).
4. Manual test suite: MT-CHANNEL-001 through MT-CHANNEL-006.
5. Documentation: channels user guide and architecture diagram update.

**Dependencies**: Phase 3.

---

## Files Touched (Estimated)

| Component | Files | Change |
|-----------|-------|--------|
| Go orchestrator | `internal/channels/` (new) | Channel store, router, SQLite migration |
| Go orchestrator | `internal/server/handlers.go` | Channel REST endpoints |
| Go orchestrator | `internal/executor/dispatch.go` | `DispatchChannelMessage` |
| Go orchestrator | `cmd/main.go` | Channel config loading, router initialization |
| Proto | `proto/task.proto` | `ChannelMessageEvent`, `ReceiveChannelMessage` RPC |
| Proto (generated) | `internal/generated/`, `agents/generated/` | Regenerate from updated proto |
| Python agents | `agents/server_servicers.py` | `ReceiveChannelMessage` gRPC handler |
| Python agents | `agents/dispatch.py` | `SEND_CHANNEL_MESSAGE` action executor |
| Python agents | `agents/persona_types.py` | `EventType.CHANNEL_MESSAGE`, `SendChannelMessageAction` |
| Python agents | `agents/memory/episodic.py` | Channel-scoped recall tag filter |
| Python agents | `agents/memory/budget.py` | `channel_history` tier in `MemoryBudget` |
| Config | `config/channels.yaml` (new) | Channel definitions |
| Config | `schemas/channels.schema.json` (new) | Channel config JSON Schema |
| Rust CLI | `cli/src/commands/channel.rs` (new) | Channel CLI subcommands |
| Rust CLI | `cli/src/main.rs` | Register `channel` subcommand group |
| Tests | `tests/unit/`, `tests/integration/` | Channel store, routing, delivery, memory integration |

---

## Test Strategy

- **Unit tests**: `ChannelStore` CRUD, membership enforcement, message ordering, history pagination, per-channel cap pruning.
- **Routing tests**: Single subscriber delivery, multi-subscriber fanout, offline subscriber behavior (missed message, history retrieval).
- **Memory integration tests**: Channel history stored with correct tags, budget-aware recall, channel tier in `MemoryBudget`.
- **Relationship tests**: Channel interactions correctly increment trust score for sender.
- **Security tests**: Non-member publish rejected (403), sender ID not spoofable, rate-limit application on aggressive publish.
- **CLI tests**: All `persatrix channel` subcommands produce correct REST calls and parse responses.
- **Integration test**: Two persona agents share a channel; agent A publishes, agent B receives and replies; a human joins via CLI and observes the conversation.

---

## Open Questions

1. **Channel discovery by agents**: Should agents be able to query the channel list at runtime (dynamic discovery) or only interact with channels they are pre-configured to join? Dynamic discovery is more flexible; config-only is consistent with the existing deny-by-default permission model. Recommendation: config-only for v0.3.0, list endpoint is for operators and CLI only.

2. **Message delivery acknowledgement**: At-most-once delivery means missed messages are silent. Should the orchestrator log a `channel.delivery.missed` metric for agents that were offline? This helps operators understand message drop rates without requiring a full ACK protocol.

3. **Thread depth**: Single-level threading (reply to top-level message only) vs. arbitrary nesting. Flat threading matches Slack's model and is simpler. Recommendation: single-level for v0.3.0.

4. **`persatrix channel watch` output format**: Plain text (sender: message) or structured JSON? Plain text is more readable for humans; JSON allows piping to other tools. Recommendation: default plain text with `--json` flag.

5. **Concurrent publish ordering**: If two agents publish to the same channel within milliseconds, the SQLite serial write guarantees a consistent history, but both agents may generate a response without seeing the other's message (race window between write and delivery). This is acceptable in v0.3.0 but should be documented in the channels user guide.

6. **`persatrix channel watch` SSE reuse**: The RFC 0018 LogService SSE infrastructure (`/api/v1/logs/stream`) uses the same pattern needed for channel streaming. Should Phase 4 implement a `GET /api/v1/channels/{id}/messages/stream` SSE endpoint instead of polling? The implementation would reuse the existing SSE writer. Moving this to v0.3.0 instead of "future" would give a better UX at low additional cost.

7. **Human message gate-bypass**: Under the default `when_mentioned` policy, a casual human message in a multi-agent channel produces no reply unless the human explicitly @-mentions an agent. This is the right default for busy channels but feels wrong for a 1-human-1-agent channel that behaves like a DM. Options: (a) treat human-sent messages as if they implicitly mention every channel member (humans-bypass-gate); (b) leave humans subject to the same gate and document that they must @-mention; (c) per-channel `human_addresses_all: true` flag so operators choose per channel. Recommendation: **(b) for v0.3.0** with the `--mention-all` shorthand on `persatrix channel send` covering the broadcast case. Defer (a)/(c) until usage data shows the friction matters — the cost of getting this wrong now (silent agents in a DM-feeling channel) is recoverable, while a wrong-direction default that floods agents on every casual message is not.

---

## Decision / Next Steps

1. Review and accept this RFC.
2. RFC 0008 Phase 1 (context budget foundation) must be underway before RFC 0011 Phase 3 begins. Phases 1 and 2 of RFC 0011 can proceed independently.
3. RFC 0009 Phases 1–2 (audit logging, rate limiting, input sanitization) run concurrently and are integrated at Phase 1 (REST endpoint rate limiting) and Phase 3 (input sanitization on stored content).
4. RFC 0007 (Conditional & Looped Workflow Control Flow) runs as a parallel workstream and is not a prerequisite for any RFC 0011 phase.
5. Create PR implementation plan after acceptance.

---

## Related Documentation

- [RFC 0005](0005-persona-agent-memory.md) — Persona agent event dispatch, action execution, relationship memory
- [RFC 0008](0008-agent-memory-context-optimization.md) — Memory facade and context budget (prerequisite for Phase 3)
- [RFC 0009](0009-security-sandboxing.md) — Security Phases 1–2: rate limiting and input sanitization
- [RFC 0016](0016-human-participant-chat-interface.md) — Human participant abstraction (reused for channel membership)
- [RFC 0017](0017-persona-memory-injection-budget.md) — `MemoryBudget` allocator (extended with channel_history tier)
- [RFC 0019](0019-opentelemetry-completion.md) — OTEL spans and metrics pipeline (reused for channel observability)
- [Roadmap](../../ROADMAP.md)
