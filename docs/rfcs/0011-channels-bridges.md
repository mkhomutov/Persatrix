# RFC 0011 — Channels & Internal Agent Messaging

**Type**: feature  
**Status**: � Implementing  
**Author**: Maksim Khomutov  
**Date**: 2026-04-25  
**Target**: v0.3.0 (internal channels) + v0.5.0 (external bridges)  
**Depends on**: RFC 0005; RFC 0008 (Phase 1 for action plumbing, Phase 2 for memory integration in RFC 0011 Phase 3); RFC 0009 Phases 1–2 (Phase 1 rate limiting at REST endpoints; Phase 1 input sanitization on stored channel content); RFC 0020 (Phase 3 jointly delivered — channel messages route through `InteractionTracker.add_turn` rather than per-event episodic writes; see §E)

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
  - [H. Channel Patterns (non-normative)](#h-channel-patterns-non-normative)
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

### Relationship to Existing Scaffolding

A v0.2-era design intent for inter-agent messaging left several stubs and partial implementations in the tree. This RFC is the canonical v0.3.0 design and supersedes them. To avoid ambiguity for implementers and future readers, the disposition of each pre-existing artifact is fixed here.

| Artifact | Disposition | Notes |
|----------|-------------|-------|
| `proto/agent_message.proto` (deleted in PR 3) — `ChannelService` (`SendMessage`, `Subscribe(stream)`), `AgentMessage`, `MessageType`, `Visibility`, `Attachment` | **Superseded.** Deleted in Phase 2 (PR 3). | Streaming `Subscribe` differs from the orchestrator-mediated dispatch in §C; `MessageType`/`Visibility` were never wired. New `ChannelMessageEvent` lives on `proto/task.proto`. |
| [internal/channels/channels.go](../../internal/channels/channels.go) — 7-line stub with `ChannelManager`/`MessageRouter`/`HistorySummarizer` TODOs | **Filled in.** Existing file; not new. | Files-Touched marks this `(rewritten)` rather than `(new)`. |
| [internal/bridges/bridges.go](../../internal/bridges/bridges.go) — stub for `BridgeManager`, per-platform bridges | **Untouched in v0.3.0.** Reserved for v0.5.0 (see Non-Goals). | Stub remains as a roadmap marker. |
| [schemas/channel.schema.json](../../schemas/channel.schema.json) — types `group \| direct \| broadcast \| meeting`, `id`/`type`/`name` per channel, `members: "all"`, `history_visible`, `max_history_messages` | **Superseded; rewritten in place** (same path, singular `channel.schema.json`). | Vocabulary: `direct`→`dm`; `broadcast`/`meeting` dropped (reduced to membership policies in §H — broadcast = `respond: never` listeners; meeting = transient `group` with explicit membership). The JSON Schema's redundant `id` field is removed — canonical address is derived from `channel_type` + `name` (e.g., `group:planning`). The SQL `channels.id` PK column (§B) is a separate concern and is unaffected. `members: "all"` dropped (keep membership explicit/auditable). `history_visible` → per-channel default; `max_history_messages` → global per-channel cap (§B). |
| [config/channels.yaml](../../config/channels.yaml) — placeholder using the old schema | **Rewritten to match the new schema.** | `schema_version` dropped — schema owned by the RFC, not the config file. |
| [agents/persona_types.py](../../agents/persona_types.py) — `EventType.MESSAGE_RECEIVED`, `EventType.MENTION`, `ActionType.SEND_MESSAGE` | **Renamed/superseded.** `SEND_MESSAGE`→`SEND_CHANNEL_MESSAGE`; `MESSAGE_RECEIVED`→`CHANNEL_MESSAGE`. `MENTION` retained as a derived convenience event the gate may also emit on self-mentions; personas wanting a separate handler can register on it. | Pre-existing types had no producer beyond partial scaffolding; renaming a breaking change to unused code, accepted in v0.3. |
| [agents/dispatch.py:212-326](../../agents/dispatch.py#L212) — `_handle_send_message` already pulls `channel_id`/`mentions`, applies `_MAX_MENTIONS_PER_ACTION` cap, logs `"channel routing not yet implemented"` | **Completed, not duplicated.** Phase 2 finishes the channel-routing branch through the new `ChannelRouter` rather than dispatching `MESSAGE_RECEIVED` directly. | Existing mentions cap, dispatcher timeout, and `no_targets` status taxonomy are preserved and apply to the new `SEND_CHANNEL_MESSAGE` handler. |
| [agents/dispatch.py:332-411](../../agents/dispatch.py#L332) — `EventDispatcher` with `max_cascade_depth=5` and per-event `metadata["cascade_depth"]` | **Reused as a backstop.** | See §D's "Composition with `cascade_depth`". |

Single rule: the RFC is canonical; scaffolding is either filled in (channels) or removed/renamed (proto, action/event types, schema). RFC wins on disagreement.

### A. Channel Model

Three channel types. The canonical vocabulary across this RFC, the SQL schema, the proto field, the JSON Schema, and the Python `ChannelType` enum is **`group | dm | thread`** — chosen for symmetry with the existing SQL/proto literals and consistency between value names and address-scheme prefixes:

| Type (canonical) | Description | Addressing |
|------------------|-------------|------------|
| `group` | Group channel with a declared name (e.g., `#planning`) | `group:<name>` |
| `dm` | Point-to-point between two participants | `dm:<participant_a>:<participant_b>` (participants lexicographically sorted — see below) |
| `thread` | Reply chain anchored to a specific message | `thread:<message_id>` |

User-facing surfaces (CLI prompts, log lines, the `#planning` shorthand in operator docs) may still display the friendly form `#<name>` for `group` channels — this is presentation only, not protocol.

**DM channel ID canonicalization**: A DM between A and B has exactly one ID regardless of initiator — the orchestrator **lexicographically sorts** the participant IDs before joining with `:`, so `dm:agent-a:agent-b` and `dm:agent-b:agent-a` both normalize to `dm:agent-a:agent-b`. Publish and history endpoints accept either ordering on input; stored `channel_id` is always sorted. Enforced in `ChannelStore.GetOrCreateDM(a, b string)` as the single source of truth — callers never concat DM IDs.

**Participant-ID syntax constraint (v0.3.0).** The `:`-joined DM address shape requires `:` to never appear inside a participant ID. v0.3.0 IDs are kebab-case slugs registered in `agents.yaml` / `users.yaml` (e.g., `planner-agent`, `alice`), so the assumption holds. No schema-level CHECK constraint is added: it would have to be replicated on every table storing a participant ID (`memberships`, RFC 0021's `commitments.target_party`, RFC 0020's `scope`), and a tight regex like `[a-z0-9-]+` would foreclose future ULID identifiers. Validation lives at the registration boundary — loaders reject IDs containing `:`, whitespace, or non-ASCII at startup, and `ChannelStore.GetOrCreateDM` re-checks at runtime. v0.5.0 external bridges may need an escape rule or alternative join character; deferred to the bridge RFC.

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

Membership is config-declared. Agents not in `members` cannot receive and get 403 on publish. DMs open on demand without pre-declaration; both participants must be registered.

Each `members` entry is either a participant ID string (defaulting to `respond: when_mentioned`) or an object with `id` and `respond` fields. The `respond` policy controls the per-membership response gate defined in §D and is the primary mechanism for preventing N²-fanout feedback loops in multi-agent channels. Distinct from the v0.2-era *list-level* `members: "all"` shorthand (dropped per the disposition table to keep membership explicit and auditable) — this entry-level shorthand names participants individually.

**Message schema**:

```python
@dataclass
class ChannelMessage:
    id: str                        # UUID — see ID-format note below
    channel_id: str                # canonical address: "group:planning", "dm:agent-a:agent-b", or "thread:<message_id>"
    channel_type: ChannelType      # GROUP | DM | THREAD
    sender_id: str                 # agent or user participant ID
    content: str                   # message text
    timestamp: datetime
    thread_id: str | None          # parent message ID for threaded replies; None for top-level
    mentions: list[str]            # participant IDs explicitly @-mentioned
    metadata: dict[str, Any]       # extensible; reserved for future attachment types
```

**ID format note.** `ChannelMessage.id` is a UUID for wire compatibility with the existing `agent_message.proto` surface. Newer greenfield tables introduced by RFC 0020 (`interaction_id`) and RFC 0021 (commitment `id`, duration record `id`) use ULIDs (time-sortable, cheaper to scan chronologically). The mixed scheme is intentional — messages stay UUID for proto compatibility, greenfield tables adopt ULID. Unifying would require re-versioning the proto.

### B. Channel Store

`internal/channels/` — Go-side channel storage.

**Storage**: SQLite at `data/channels.db` (configurable), separate from agent-memory DBs to avoid coupling agent state with channel state. WAL mode, consistent with all other SQLite usage.

**Schema**:

```sql
CREATE TABLE channels (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    channel_type TEXT NOT NULL CHECK (channel_type IN ('group', 'dm', 'thread')),
    description TEXT,
    created_at  DATETIME NOT NULL
);

CREATE TABLE memberships (
    channel_id     TEXT NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    participant_id TEXT NOT NULL,
    respond_policy TEXT NOT NULL DEFAULT 'when_mentioned'
        CHECK (respond_policy IN ('when_mentioned', 'always', 'never')),
    joined_at      DATETIME NOT NULL,
    PRIMARY KEY (channel_id, participant_id)
);

-- CHECK constraints mirror the canonical vocabularies in §A (channel types)
-- and §D (respond policies). Deliberately redundant with app-level validation:
-- a config typo or a future migration that misses an enum site fails fast at
-- write time rather than silently storing an unrecognized value the gate/router
-- would treat as default. RFC 0021 §F uses the same pattern on commitments.state.

CREATE TABLE messages (
    id         TEXT PRIMARY KEY,
    channel_id TEXT NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    sender_id  TEXT NOT NULL,
    content    TEXT NOT NULL,
    timestamp  DATETIME NOT NULL,
    thread_id  TEXT REFERENCES messages(id) ON DELETE CASCADE,  -- pruning a parent prunes its thread
    mentions   TEXT NOT NULL DEFAULT '[]',   -- JSON array of participant IDs
    metadata   TEXT NOT NULL DEFAULT '{}'    -- JSON object
);

CREATE INDEX idx_messages_channel_ts ON messages(channel_id, timestamp DESC);
CREATE INDEX idx_messages_thread ON messages(thread_id) WHERE thread_id IS NOT NULL;
```

**Timestamp type.** `DATETIME` here vs. `REAL` (epoch seconds) on RFC 0020 §D's `episodes` columns is an ownership split, not a missed unification: this table is Go-owned (`time.Time` + `database/sql/driver` map cleanly to `DATETIME`); `episodes` is Python-owned and already uses `time.time()` epoch seconds throughout ([agents/memory/episodic.py:203](../../agents/memory/episodic.py#L203)) — switching would impose a converter cost on every read. SQLite stores both as the same underlying value, so the only visible difference is `sqlite3` rendering (ISO-8601 text vs. `1714056720.123`). Unifying would require refactoring Python's existing timestamp call sites; v0.3.0 keeps each side native.

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

A per-channel message cap (default: 10,000) prevents unbounded growth — oldest messages are pruned on each new write past the cap, consistent with RFC 0008's episodic memory cap approach.

**Pruning interaction with thread FK.** The `thread_id` self-reference makes naive oldest-first pruning fail with FK violations whenever a pruned message still has live replies. `ON DELETE CASCADE` is chosen: pruning a thread root prunes its replies in the same transaction. Preferred over `ON DELETE SET NULL` (would orphan replies as top-level messages, polluting history) and over a thread-aware retention policy (would let a long-lived thread block pruning indefinitely). Phase 1's migration test exercises the cap-boundary case (publishes 10,001 messages including a straddling thread, asserts zero orphans). SQLite requires `PRAGMA foreign_keys = ON` for cascade enforcement; the channel store sets it at connection time per existing project convention.

**Channel-deletion cascade.** Both `memberships.channel_id` and `messages.channel_id` declare `ON DELETE CASCADE` on their FK to `channels(id)`. Without this, `DELETE FROM channels WHERE id = ?` fails as soon as any membership or message row exists, and `DELETE /api/v1/channels/{id}` (§C, Phase 2) becomes undeliverable. Cascade is preferred over `ON DELETE RESTRICT` (forces every endpoint and operator script to replicate the same transactional cleanup) and `SET NULL` (orphaned messages with no addressable channel). Cascade composes correctly with the `messages.thread_id` self-cascade — when a channel is deleted, its messages are deleted as a set, so thread self-references inside that set resolve transitively. The `PRAGMA foreign_keys = ON` requirement called out above applies here.

A global channel-count cap (default: 50, `channels.max_channels` in `config/channels.yaml`) bounds membership-table size, observability cardinality, and operator-visible namespace. Applies to named group channels only; DMs and threads are addressed implicitly and not counted. Startup overflow is a config validation error; REST creation past the cap returns 409.

**Config + REST coexistence rules.** Both `config/channels.yaml` (startup) and `POST /api/v1/channels` (runtime) write the same `channels`/`memberships` tables. Without an explicit policy three failure modes surface: (a) REST-created channel name-collides with a config-declared one on restart, (b) operator removes a config channel that has REST-added members and history, (c) a REST-added participant isn't in the config `members` block. v0.3.0 policy:

| Case | Behavior |
|------|----------|
| Channel in config, not in store | Inserted at startup with config-declared memberships. |
| Channel in store but not in config (REST-created earlier) | Preserved as-is. Removal requires `DELETE /api/v1/channels/{id}` (Phase 2) or DB surgery. Config is *not* the source of truth for REST-created channels. |
| Channel in both, memberships disagree | **Startup fails loudly**, listing divergent participant IDs. Operator edits `channels.yaml` to match live state or removes the channel from config. "Config wins" was rejected (silently deletes REST-added members and their interaction summaries); "REST wins" was rejected (renders `channels.yaml` non-authoritative). Loud failure is the only option preserving both sources without surprise data loss. |
| Channel in store, name removed from `channels.yaml` between restarts | Treated as "in store but not in config": channel and history persist; logged at `INFO` with channel ID and last-message timestamp. Removing a config name does not imply intent to drop history. |
| REST `POST /api/v1/channels/{id}/members` against a config-declared channel | Allowed; merged into `memberships`. Next restart triggers the loud-failure case above — runtime additions are valid for this process but need a `channels.yaml` edit for durability. Intentional: declared channels deserve a stable membership story, ad-hoc additions should surface rather than be silently consumed. |

Silent merging was rejected because every silent-merge policy hides one source from the operator and we have no telemetry to surface the drift. If dogfood shows conflicts trigger too easily, a follow-up RFC may add `channels.merge_policy` with `strict | config_wins | rest_wins`; v0.3.0 does not pre-commit.

### C. Message Routing and Delivery

**Architecture**: The orchestrator owns message routing — agents publish as actions, the orchestrator stores and dispatches.

**Publish flow**:

```
Agent A publishes to #planning
    ↓
ActionType.SEND_CHANNEL_MESSAGE (Python persona action)
    ↓
ActionExecutor → POST /api/v1/channels/{id}/messages   (HTTP, agent → orchestrator)
    ↓
ChannelRouter (Go): store message → look up members → filter to registered agents
    ↓
For each registered subscriber: DispatchChannelMessage → ReceiveChannelMessage gRPC call
    ↓
Agent B servicer: create AgentEvent(event_type=CHANNEL_MESSAGE) → dispatch to on_event()
```

The publish hop crosses agent → orchestrator as **HTTP/REST**, not gRPC, even though every other agent ↔ orchestrator call uses gRPC. Two reasons: (1) the same `POST /api/v1/channels/{id}/messages` backs `persatrix channel send` and `curl` testing — one REST surface for both agent and human publishers avoids two code paths to the same store and lets a single RFC 0009 rate-limit middleware cover everything; (2) publish is fire-and-forget — no streaming, no long-lived connection, no per-call protobuf marshaling needed agent-side. Downstream fanout to subscribers stays gRPC because that path *is* per-agent typed RPC with retries and timeouts. Different ergonomics, different transports.

**Cost of the asymmetry.** Agent publishers pay HTTP/1.1 framing overhead per message on the publish hop. Keep-alive amortizes TCP setup, but each publish still incurs request-line + header serialization that gRPC avoids; in a §H "Tight-loop pair" channel where both agents publish on every turn, this is the cost-dominant hop. Acceptable in v0.3.0 — a duplicate gRPC publish surface would force two rate-limit middleware integrations, two auth paths, and two CLI client SDKs to save ~hundreds of bytes per message. Revisit in v0.3.x if dogfood telemetry on `channel.delivery.latency_ms` (§G) shows the publish hop dominating end-to-end latency in tight-loop pair workloads.

**New REST endpoints**:

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/v1/channels` | Create a channel |
| `GET` | `/api/v1/channels` | List channels |
| `GET` | `/api/v1/channels/{id}` | Channel info and member list |
| `DELETE` | `/api/v1/channels/{id}` | Delete a channel; cascades to memberships and messages per §B "Channel-deletion cascade". 404 on unknown id; 409 reserved for future "channel pinned by config" guard if §B coexistence rules grow one. Phase 2. |
| `POST` | `/api/v1/channels/{id}/messages` | Publish a message |
| `GET` | `/api/v1/channels/{id}/messages` | Channel history (paginated, newest-first) |
| `GET` | `/api/v1/channels/{id}/messages/{msg_id}/thread` | Thread replies |
| `POST` | `/api/v1/channels/{id}/members` | Add a participant to a channel |
| `DELETE` | `/api/v1/channels/{id}/members/{participant_id}` | Remove a participant from a channel. 404 on unknown channel or membership. Does not delete the participant's prior messages — those persist under the same `sender_id`. Phase 2. |

**Query parameters** (the GET endpoints above accept the following query parameters; absent or unrecognized parameters apply the listed default):

| Endpoint | Parameter | Type | Default | Notes |
|----------|-----------|------|---------|-------|
| `GET /api/v1/channels` | `limit` | int | 100 | Caps the response page size; bounded above by the global cap (`max_channels`, default 50). |
| `GET /api/v1/channels/{id}/messages` | `limit` | int | 50 | Caps the page size; the on-startup catch-up fetch in OQ #8 also defaults to this value. |
| `GET /api/v1/channels/{id}/messages` | `before` | RFC 3339 timestamp | now | Cursor for backscroll. Returns messages strictly older than the supplied time, newest-first. Mirrors the `before time.Time` argument on `ChannelStore.GetHistory` so the REST surface and the Go interface have a 1:1 mapping. |
| `GET /api/v1/channels/{id}/messages/{msg_id}/thread` | `limit` | int | 100 | Caps thread depth surfaced in one response; threading is single-level in v0.3.0 (OQ #3) so this is the full reply count for typical use. |

A future `?since=<message_id>` watermark parameter (OQ #8) is intentionally omitted — adding it now would commit to the watermark protocol before the OQ is resolved. The `before` shape covers the v0.3.0 user stories (CLI backscroll, on-startup last-N fetch).

**New proto definitions**:

```protobuf
// Additions to proto/task.proto

message ChannelMessageEvent {
    string message_id  = 1;
    string channel_id  = 2;
    string channel_type = 3;   // "group" | "dm" | "thread" — MUST agree with the prefix on channel_id
    string sender_id   = 4;
    string content     = 5;
    string timestamp   = 6;    // RFC 3339
    string thread_id   = 7;    // empty string if not a reply
    repeated string mentions = 8;
}

// `channel_type` duplicates the `channel_id` prefix (§A). Carried separately
// for log/observability ergonomics — counters and span attributes use it
// without parsing. Orchestrator MUST validate agreement with the prefix on
// publish (Phase 2 `ChannelRouter`, unit-tested); receivers SHOULD drop on
// mismatch as malformed rather than pick one source.

service AgentService {
    // existing RPCs ...
    rpc ReceiveChannelMessage(ChannelMessageEvent) returns (TaskAck);
}
```

**Delivery guarantees**: At-most-once in v0.3.0 — best-effort dispatch to each subscriber. Agents offline at delivery time miss the message and recover via the history endpoint. Exactly-once with durable queuing is deferred.

**Agent-initiated publish** — Python action type:

```python
@dataclass
class SendChannelMessageAction:
    action_type: ActionType = ActionType.SEND_CHANNEL_MESSAGE
    channel_id: str           # canonical address: "group:planning", "dm:agent-a:agent-b", or "thread:<message_id>"
    content: str
    thread_id: str | None     # None for a new top-level message
    mentions: list[str]       # participant IDs to @-mention
```

`ActionExecutor` handles `SEND_CHANNEL_MESSAGE` by calling the REST endpoint. The framework injects `sender_id` from the agent's registered ID — agents cannot spoof another sender.

### D. Agent Integration

**Receiving messages** — new `EventType` (added to the existing [`EventType`](../../agents/persona_types.py#L32)):

```python
# Addition to agents/persona_types.py — existing class, not a redefinition:
class EventType(Enum):
    # existing values ...
    CHANNEL_MESSAGE = "channel_message"  # supersedes MESSAGE_RECEIVED for channel-routed events
```

When `ReceiveChannelMessage` arrives, the agent servicer creates an `AgentEvent` and dispatches to `on_event()`. The persona runtime handles it through the existing event loop — no special casing.

**`AgentEvent` extension (additive only).** Existing [`AgentEvent`](../../agents/persona_types.py#L44) already carries `event_type`, `payload: dict`, `channel_id`, `sender_id`, `message_id`, `timestamp: float`, `metadata: dict`. This RFC adds one optional field for threading and reuses `payload` for channel-message-specific fields rather than promoting them — preserves backward compatibility, keeps `timestamp` as `float` (existing Unix-epoch-seconds convention), avoids a breaking rename:

```python
# Diff against existing AgentEvent (agents/persona_types.py):
@dataclass
class AgentEvent:
    event_type: EventType
    payload: dict[str, Any] = field(default_factory=dict)   # carries content, mentions, respond_policy
    channel_id: str | None = None                            # already exists
    sender_id: str | None = None                             # already exists
    message_id: str | None = None                            # already exists
    timestamp: float = field(default_factory=time.time)      # already exists; stays float
    metadata: dict[str, Any] = field(default_factory=dict)   # already exists
    thread_id: str | None = None                             # NEW — promoted from payload because the response gate
                                                             #       branches on it (see §D thread-reply rule)
```

For `CHANNEL_MESSAGE` events, the `payload` dict contains:

| Key | Type | Notes |
|-----|------|-------|
| `content` | `str` | message text |
| `mentions` | `list[str]` | participant IDs explicitly @-mentioned |
| `respond_policy` | `str` | the receiving agent's `respond_policy` for this channel, copied in by the dispatcher so the gate doesn't re-query the store |
| `thread_parent_sender_id` | `str \| None` | for thread replies (`thread_id != None`), the orchestrator pre-resolves the parent message's sender during fanout (one indexed lookup against `messages` keyed on `thread_id`, identical for all recipients of the same publish) and writes it here so the gate's `when_mentioned` thread-reply branch can compare against `self.id` without a per-event store lookup. `None` for top-level messages and for reply-to-deleted-parent edge cases (gate treats `None` as "no thread parent in scope" and falls through to the mention check). |

`thread_id` is promoted because the gate's thread-reply rule reads it on every `CHANNEL_MESSAGE` — a misspelled `payload["thread_id"]` lookup would silently fail the rule. Top-level placement makes the contract type-checked.

**Why pre-resolve `thread_parent_sender_id` at the orchestrator.** The `when_mentioned` policy fires for thread replies to a message *this agent* authored, so the gate needs `parent_message.sender_id` on the hot path before any LLM cost. Options considered: (a) per-recipient `thread_parent_authored_by_recipient: bool` — asymmetric, burns N booleans for one fact; (b) shared `thread_parent_sender_id` — one orchestrator-side lookup amortized across recipients; (c) agent-side lookup — best case hits working memory, worst case adds remote-fetch latency to the gate. Option (b) chosen: same data-shaping pass that copies `respond_policy`, no agent-side store dependency, identical value for all recipients. The lookup is `SELECT sender_id FROM messages WHERE id = ?` against the PK index, ~microseconds; runs once per `ChannelRouter.Publish` regardless of subscriber count.

**Why `respond_policy` stays in `payload`.** Same hot path as `thread_id`, but different sourcing: `respond_policy` is not a property of the event — it lives on the receiving agent's `memberships` row, copied into `payload` today only as an optimization to skip a per-event store lookup at the gate. Promoting it to `AgentEvent` would commit the dispatcher to that copy forever; if the gate ever reads memberships directly (e.g., moves into the channel store), the field becomes stale denormalization. Keeping it in `payload` flags it as transient cargo. `thread_id`, by contrast, is intrinsic to the message. See OQ #12 for the typo-resistance alternative if it matters in practice.

**Agent behavior on channel messages**: `on_event()` handles `CHANNEL_MESSAGE` without core-loop changes — RFC 0008 memory injection pulls relevant channel history and episodic memory, the LLM decides whether and how to respond, and the resulting action may be a `SEND_CHANNEL_MESSAGE` reply to the same channel or thread. Agents may also initiate channel messages autonomously during their tick loop.

**Response gating (loop prevention)**: A naive "every `CHANNEL_MESSAGE` invokes the LLM" model produces O(N²) feedback amplification per turn in multi-agent channels — every message generates up to N inferences, each of which is itself a message that re-triggers the others. The persona runtime applies a **pre-LLM response gate** evaluated on the agent process before memory recall or LLM call, using the per-membership `respond` policy from §A:

| Policy | Triggers a response cycle when ... |
|--------|------------------------------------|
| `when_mentioned` (default) | the agent's ID appears in `event.mentions`, **or** the message is a thread reply to a message this agent authored |
| `always` | every channel message except the agent's own |
| `never` | no message — the agent observes the channel for memory ingestion only (read-only participant) |

Suppressed events are still stored in episodic memory (per §E) so the agent retains channel context for later recall, but no LLM call or action is dispatched. This bounds inference cost and is the primary structural defense against runaway exchanges.

**Composition with `cascade_depth` and rate limiting.** [`EventDispatcher`](../../agents/dispatch.py#L332) already enforces `max_cascade_depth=5` via `metadata["cascade_depth"]`. Defense-in-depth ordering (gate is primary, cascade is backstop, rate limiter is last line):

1. **Response gate (this RFC, agent-side, pre-LLM)** — primary structural defense. Suppresses by membership policy before inference cost. A correctly-tuned channel stops most fanout at depth 1.
2. **`cascade_depth` (existing, dispatcher-side)** — runtime backstop for misconfigured `always` memberships, accidental loops from future feature work, or any path the gate misses. Applied regardless of `respond` policy: an `always` member at `cascade_depth=5` does **not** receive the message; the drop is upstream of the gate.
3. **RFC 0009 Phase 1 rate limiting (REST-side)** — caps per-agent publish rate. Catches loops that somehow defeat both the gate and cascade limit (e.g., agents publishing on tick rather than in reaction to events).

Outbound `SEND_CHANNEL_MESSAGE` actions carry `cascade_depth + 1`, exactly as the dispatcher already does for `MESSAGE_RECEIVED`. No change to the cascade mechanism is required.

Additional invariants:

- An agent never receives its own `CHANNEL_MESSAGE` event — the orchestrator filters by `sender_id != subscriber_id` during fanout. This is enforced regardless of policy.
- The `when_mentioned` thread-reply branch fires only when *another participant* replies to a message the agent authored. An agent's own thread continuation does not retrigger itself.
- The autonomous tick loop is unaffected: agents may initiate channel messages on their own schedule. The gate only governs *reactive* replies to incoming events.
- Agents that publish via `SEND_CHANNEL_MESSAGE` set `mentions` explicitly. The Rust CLI offers a `--mention <id>` repeatable flag and a `--mention-all` shorthand that expands to every channel member, addressing the human-in-the-loop case where a human wants every agent in a channel to react.

### E. Memory Integration

Channel history is integrated with agent memory via RFC 0008's `MemoryFacade` — the hard dependency that makes RFC 0008 a prerequisite for Phase 3.

**`MemoryFacade` API extension (additive).** RFC 0008 §B specifies `retrieve_relevant(query, limit, scope)` and `store_observation(entry, scope, ttl)`. This RFC extends both additively:

- `store_observation(content, *, scope, tags=None, importance=None, ttl=None)` — adds optional `tags: list[str]` and `importance: float | None`. Both are forwarded to `EpisodicMemory.store_episode`, which already accepts them ([agents/memory/episodic.py:171](../../agents/memory/episodic.py#L171)).
- `retrieve_relevant(query, *, scope, tags=None, limit)` — adds optional `tags: list[str]` filter, intersection semantics (entry must include all listed tags).

Safe defaults preserve RFC 0008 §B behavior for callers that don't pass `tags`/`importance`. RFC 0008 itself is **not** amended (Accepted status, additive parameters are forward-compatible with its specified contract). This §E is the canonical reference for the extension; future facade extensions should follow the same additive pattern.

**Storing channel messages**: Not per-event. Each `CHANNEL_MESSAGE` is appended to the open `Interaction` for the channel via [`InteractionTracker.add_turn`](0020-interaction-lifecycle.md#g-per-channel-scoping) (RFC 0020 §G). On interaction close (structural boundary or idle gap, RFC 0020 §B) exactly one episodic entry is written, carrying the summary and channel-scoped tags:

```python
# Inside InteractionTracker.close_interaction() — runs once per closed interaction,
# not per CHANNEL_MESSAGE event. See RFC 0020 §C for the close pipeline.
await self.memory.store_observation(
    content=interaction.summary,  # placeholder text in Phase 1; LLM summary in Phase 2 (RFC 0020)
    scope="channel",
    tags=["channel", interaction.channel_id, *interaction.participant_ids],
    importance=self._compute_interaction_importance(interaction),
    ttl=None,  # channel history persists until evicted by capacity policy
)
```

This replaces earlier drafts' per-event storage shape; the change was introduced when RFC 0020 joined the v0.3.0 chain to eliminate the duplicate-summary problem (a ten-turn negotiation producing ten near-identical episodes — RFC 0020 §Motivation). RFC 0011 Phase 3 and RFC 0020 Phase 3 land jointly per ROADMAP.

**Retrieving channel history** during context assembly:

```python
channel_history = await self.memory.retrieve_relevant(
    query=event.content,
    scope="channel",
    tags=[event.channel_id],
    limit=_CHANNEL_RECALL_LIMIT,  # caps recall layer output; budget loop admits up to remaining tokens
)
```

`_CHANNEL_RECALL_LIMIT` (default: 20) caps the recall layer output before `MemoryBudget.try_add` runs. Actual admission depends on remaining budget per RFC 0017 §B greedy-fill — the cap only bounds allocator CPU cost on busy channels. Exposed as `optimization.yaml → channels.recall_limit` (Phase 3); hard-coding would replicate the regression that motivated RFC 0017's per-event-budget tunability. The Python module keeps the `_CHANNEL_RECALL_LIMIT` variable name (resolved once at startup) so call sites read identically.

**Budget allocation**: Channel history competes with episodic and relationship memory for `budget_memory_tokens` (RFC 0008) under RFC 0017's `MemoryBudget` contract — single greedy pool with priority-ordered fill, no per-tier fairness (RFC 0017 §B: *"No fairness across tiers. The budget is greedy by design."*). Admitted via the same `budget.try_add(...)` loop, with per-tier *priority* (not *ceiling*) determining order.

The recommended priority order in the persona-runtime caller — the **canonical cross-RFC sequence**, also referenced from RFC 0021 §J — is:

1. **relationship summary** (always)
2. **open commitments** (RFC 0021 §F, when present and within the proximity horizon)
3. **channel history** (this RFC, only when the triggering event is a `CHANNEL_MESSAGE`)
4. **episodic recall** (RFC 0008 / RFC 0020)
5. **recent notes**
6. **duration priors** (RFC 0021 §I, only when the task category has calibration data)

Pinned here so RFC 0011 and RFC 0021 cannot drift on whether notes come before or after episodes (an earlier RFC 0011 draft said `episodes → notes`; an earlier RFC 0021 §J said commitments admit `after notes, before episodes` — mutually inconsistent). The order above resolves it as `episodes → notes` (episodes are load-bearing; notes are reflective and tolerate truncation) and slots RFC 0021's tiers in by action-relevance. Channel history sits right after open commitments on channel-triggered events so recent in-channel turns are admitted before broader episodic recall fills the budget. Greedy allocation is preserved: if relationship + commitments + channel history fill the budget, no episodic recall is admitted — the same trade-off RFC 0017 already makes. RFC 0017 itself is unchanged (allocator only, not per-tier order); this RFC and RFC 0021 are the two sources for the order, kept in lockstep here.

An earlier RFC 0011 draft proposed a 25/50/25 per-tier ceiling; rejected during review because it would have changed RFC 0017's allocator from greedy-single-pool to bounded-tier-with-ceiling — a redesign disguised as an extension. Greedy-with-priority reaches the same operational goal without amending RFC 0017.

**Relationship updates**: Receiving a channel message updates the sender's trust score per RFC 0005; channel-interaction frequency feeds the trust decay/growth algorithm alongside RFC 0016 direct-chat interactions.

### F. Human Participant Channels

Humans join channels via the RFC 0016 `Participant` abstraction. No new infrastructure — `UserParticipant` already conforms to the membership model.

**CLI integration** — new Rust CLI subcommand group:

```
persatrix channel list                                # list available channels and member counts
persatrix channel join <name>                         # add current user to channel membership
persatrix channel send <name> <message>               # publish a message (top-level)
persatrix channel reply <name> <message_id> <message> # reply to a specific message (threaded)
persatrix channel history <name> [--limit N]          # display recent history (newest-first)
persatrix channel watch <name> [--interval N]         # poll for new messages (default: 5s interval)
```

`persatrix channel watch` uses polling in v0.3.0 (consistent with `persatrix logs --follow`); SSE streaming via RFC 0018's LogService infrastructure is a future extension.

**Output formats** for the non-`watch` subcommands (`watch` is covered by OQ #4):

| Subcommand | Default (human) | `--json` |
|------------|-----------------|----------|
| `list` | One row per channel: `<id>  <type>  <member_count>  <last_message_at>` | Array of channel objects matching `schemas/channel.schema.json` plus `last_message_at` and `member_count`. |
| `join` | `Joined #<channel_id> as <user_id>` on success; nonzero exit + stderr on failure | `{"channel_id": "...", "user_id": "...", "joined_at": "..."}` |
| `send` | `Sent <message_id> to #<channel_id>` | `{"message_id": "...", "channel_id": "...", "timestamp": "..."}` |
| `reply` | Same shape as `send`, plus `(reply to <thread_id>)` annotation | Send object with `thread_id` populated |
| `history` | One line per message: `<timestamp>  <sender_id>: <content>` (newest-first); `--limit` defaults to 50 | Array of message objects matching the §A `ChannelMessage` schema |

Conventions follow `persatrix logs` and `persatrix chat` precedents (plain text for humans, structured JSON for piping). PR-plan time may refine field names; this table pins the *shape*.

### G. Channel Observability

**OTEL metrics** (via RFC 0019 pipeline):

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| `channel.messages.published` | counter | `channel_id`, `sender_type` | Messages published per channel |
| `channel.messages.delivered` | counter | `channel_id`, `status` | Delivery attempts (ok / failed) |
| `channel.messages.gated` | counter | `channel_id`, `policy` | Events suppressed by the response gate (agent-side); primary signal for an under-/over-tuned `respond` policy. `subscriber_id` is deliberately **not** a label — cardinality scales as `members × channels × policies` (~3,000 series at N=20 over 50 channels × 3 policies; ~30,000 at N=200; excluded at N=2 for consistency). Per-subscriber drill-down lives in OTEL spans (`subscriber.id` attribute). |
| `channel.delivery.latency_ms` | histogram | `channel_id` | Publish-to-delivery latency per subscriber |
| `channel.members.active` | gauge | `channel_id` | Current registered subscriber count |
| `channel.history.recall_tokens` | histogram | `channel_id` | Tokens consumed by channel history injection |

**Spans**: Publish and per-subscriber delivery each emit an OTEL span with `channel.id`, `message.id`, `sender.id` attributes; delivery spans link to publish via Span Link per RFC 0019's A2A causality model.

### H. Channel Patterns (non-normative)

Guidance, not protocol. The three `respond` policies compose into channel-level patterns covering common urgency and attention scenarios — documented so operators reach for the right config before reaching for new schema. Message-level priority, urgency tags, and "circular" delivery modes are out of scope for v0.3.0; the channel itself is the unit of urgency.

| Pattern | Membership policy | When to use |
|---------|-------------------|-------------|
| **Quiet group** | all members `when_mentioned` | Default for general-purpose channels with 3+ agents (e.g., `#planning`, `#general`). The channel acts as a shared log; messages cut through only via explicit `@`-mention. Lowest fanout cost. |
| **Tight-loop pair** | both members `always` | Two agents in continuous collaboration (e.g., `#code-review` between a writer and reviewer). Every message triggers a reply cycle by design. **Bounded by `cascade_depth`** (see §D Composition with `cascade_depth`): any single chain of reactive replies terminates at `max_cascade_depth` (default 5) — the dispatcher drops the next event silently with only a debug log. Longer dialogues require an external re-trigger (a tick, a human message, a non-channel event), which resets `cascade_depth` to 0. Avoid extending to N > 2 — every additional `always` member multiplies fanout. |
| **Broadcast / announcements** | senders publish, all listeners `never` | One-way log channel (e.g., `#announcements`, `#deploy-events`). Listeners ingest into episodic memory but never reply, so the channel cannot loop regardless of write volume. |
| **Always-respond / incident** | all members `always` | Multi-agent channel where every message genuinely warrants attention from everyone (e.g., `#incident`). Fanout cost scales as `members × messages`; reserve for situations where that cost is the point. |

**Mixed-policy channels** are legitimate — e.g., `#planning` with two `always` collaborators plus a `when_mentioned` advisor (stays quiet until mentioned, still ingests history). Avoid more than two `always` members in one channel unless the multiplicative reply traffic is deliberate.

**Express urgency through destination, not content**: route urgent messages to tight-loop or always-respond channels, status updates to announcements channels. Same text, different channel = different urgency. Keeps the message schema small and avoids the failure mode where every sender flags every message "high priority".

**When to revisit**: if operational experience shows recurring need for selective urgency *within* a channel (e.g., `#planning` is 90% FYI, 10% blocking), the cleanest extension is a free-form `labels` array on `ChannelMessage` that the gate keys off in addition to mentions. Deferred until usage data justifies it; a fixed urgency enum now would foreclose better options.

---

## Security Considerations

- **Sender spoofing**: The `sender_id` is set by the orchestrator from the publishing agent's registration record, not from the request body. Agents cannot forge another sender's identity.
- **Membership enforcement**: Only agents listed in a channel's membership can publish or receive. The orchestrator checks membership on every publish attempt and returns 403 on violation. Delivery to non-registered subscribers is skipped silently (agent is offline, not unauthorized).
- **Rate limiting**: RFC 0009 Phase 1 rate limiting applies to channel publish calls. An agent spamming a channel hits its per-agent-per-window call limit and is circuit-broken.
- **Content injection**: Channel message content is stored in episodic memory and later injected into agent context. Adversarial content in channel messages represents the same prompt injection risk as tool output. Mitigation: RFC 0009 Phase 1 input sanitization applies to channel message content before it is stored.
- **History amplification**: A busy channel with large messages could dominate the memory injection budget. Mitigation: channel history is admitted via the existing `MemoryBudget.try_add` greedy-priority loop (see §E) — when the budget fills, lower-priority items are simply not admitted. The per-channel recall `limit` (derived from `budget_memory_tokens // AVG_MESSAGE_TOKENS`) caps how many items the recall layer returns before the budget loop runs, so a single mega-channel cannot starve the allocator with thousands of candidates. There is deliberately no per-tier ceiling — the priority order is the contract.
- **DM privacy**: DM channels are accessible only to the two declared participants. Membership is enforced at the store layer. DM content is stored in each participant's isolated episodic memory, not in a shared channel store visible to other agents.
- **Store growth**: Per-channel message cap (default 10,000), global channel-count cap (default 50 named channels), and the SQLite WAL checkpoint policy prevent unbounded disk growth and bound observability cardinality.

---

## Phased Implementation Plan

### Phase 1: Channel Store and REST Routing

**Summary**: Build the Go-side channel store, REST endpoints, and fanout routing engine. Testable end-to-end via REST with `curl` — no agent-side changes yet.

**Deliverables**:
1. `internal/channels/` package: `Channel`, `ChannelMessage`, `ChannelStore` interface, SQLite implementation with migration runner.
2. `ChannelRouter`: publish-and-fanout logic; dispatches to registered agent gRPC addresses (uses existing registry lookup).
3. REST endpoints: create channel, list channels, publish message, get history, get thread, add member. (DELETE endpoints land in Phase 2 per the §C table — deletion is only useful once agent delivery exists, and the §B cascade only matters once those endpoints are callable.)
4. `config/channels.yaml` loading at orchestrator startup; channel and membership initialization.
5. `schemas/channel.schema.json` rewritten in place for config validation against the new schema (see Relationship to Existing Scaffolding for vocabulary changes).
6. Unit tests: channel store CRUD, membership enforcement, history pagination, message cap pruning.

**Dependencies**: RFC 0009 Phase 1 (rate-limit middleware) for `POST /api/v1/channels/{id}/messages`. **Hard dependency for any production deployment** — the publish endpoint is reachable by any client on the orchestrator network well before agents are wired up, so an unrate-limited publish surface is a DoS vector (curl loop fills the `messages` table and exhausts the per-channel pruning loop) regardless of Phase 2 agent shipping. Demo/dev builds may run without the middleware, but the orchestrator MUST log `WARN` at startup ("channel publish endpoints running without rate limiting; not safe for production") gated by an explicit opt-out — config field, CLI flag, or both — rather than implicit absence. The precise surface (likely `security.rate_limit_enforced: false` in the orchestrator config, optionally with a CLI mirror) is deferred to the Phase 1 PR-plan so the RFC does not foreclose the implementation's choice between a config knob and a flag. "Soft dependency, defer to Phase 2" was rejected: REST is open at Phase 1, so DoS protection matters from the moment the surface exists, not from the moment Phase 2 wires up agents. RFC 0008 is not required here — the channel store is standalone; memory injection is wired in Phase 3.

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
8. DELETE endpoints from §C: `DELETE /api/v1/channels/{id}` (cascades memberships + messages per §B "Channel-deletion cascade") and `DELETE /api/v1/channels/{id}/members/{participant_id}`. Unit-tested for cascade correctness alongside the Phase 1 thread-FK cascade test (no message-orphan rows after channel delete; participant removal preserves the participant's prior messages).

**Dependencies**: Phase 1.

### Phase 3: Memory Integration

**Summary**: Wire channel history into RFC 0008's memory injection pipeline and update relationship memory.

**Deliverables**:
1. `CHANNEL_MESSAGE` event handler routes each event to `InteractionTracker.add_turn` (RFC 0020 §G); on interaction close, exactly one episodic entry is written per interaction with `tags=[event.channel_id, sender_ids…]` (see §E). No per-event episodic writes.
2. Persona-runtime memory-injection caller updated to query channel history (via `MemoryFacade.retrieve_relevant` with `tags=[event.channel_id]`) and feed it into the existing `MemoryBudget.try_add` loop in priority order (see §E). No change to `MemoryBudget` itself.
3. `MemoryFacade.retrieve_relevant()` supports channel-scoped recall via tag filter.
4. Relationship memory: channel interactions increment interaction count and influence trust score.
5. Integration test: agent B's reply to agent A's channel message demonstrates awareness of channel history (verifiable via `persatrix logs` trace).

**Dependencies**: Phase 2; RFC 0008 Phase 2 (`MemoryFacade` for task agents — provides the `store_observation` and `retrieve_relevant` API used here); RFC 0020 Phase 3 (joint delivery — `InteractionTracker.add_turn` is the entry point for deliverable 1).

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
| Go orchestrator | `internal/channels/` (rewritten) | Existing 7-line stub replaced by channel store, router, SQLite migration |
| Go orchestrator | `internal/server/handlers.go` | Channel REST endpoints |
| Go orchestrator | `internal/executor/dispatch.go` | `DispatchChannelMessage` |
| Go orchestrator | `cmd/main.go` | Channel config loading, router initialization |
| Proto | `proto/task.proto` | `ChannelMessageEvent`, `ReceiveChannelMessage` RPC added to `AgentService` |
| Proto | `proto/agent_message.proto` | Phase 2: delete the v0.2-era `ChannelService` (`SendMessage` + `Subscribe`) per the Relationship to Existing Scaffolding disposition. The new agent-side delivery path is on `AgentService.ReceiveChannelMessage` (`proto/task.proto`). |
| Proto (generated) | `internal/generated/`, `agents/generated/` | Regenerate from updated proto |
| Python agents | `agents/server.py` | Phase 2: remove `ChannelServiceServicer` import (L41) and gRPC registration (L133–134). New `ReceiveChannelMessage` handler joins the existing `AgentServiceServicer`. |
| Python agents | `agents/server_servicers.py` | Phase 2: delete v0.2 `ChannelServiceServicer` (L419); add `ReceiveChannelMessage` to `AgentServiceServicer`. |
| Python agents | `tests/unit/python/test_server_channel.py` | Phase 2: delete/rewrite — targets the deleted `ChannelServiceServicer.SendMessage`/`Subscribe` surface, will not survive proto regen. New coverage in Test Strategy integration tests. |
| Python agents | `agents/dispatch.py` | `SEND_CHANNEL_MESSAGE` action executor |
| Python agents | `agents/persona_types.py` | `EventType.CHANNEL_MESSAGE`, `SendChannelMessageAction` |
| Python agents | `agents/memory/episodic.py` | Channel-scoped recall tag filter (already accepts `tags`/`importance` per RFC 0008 §B; this RFC wires them through the facade — see §E) |
| Python agents | `agents/persona_runtime/memory_context.py` | Persona-runtime caller adds channel-history tier into existing `MemoryBudget` greedy fill on `CHANNEL_MESSAGE` events. No change to `MemoryBudget` itself. |
| Config | `config/channels.yaml` (rewritten) | Existing placeholder rewritten to match new schema (see Relationship to Existing Scaffolding) |
| Config | `schemas/channel.schema.json` (rewritten) | Existing v0.2 schema rewritten in place; same path (singular `channel.schema.json`), same `$id` URL |
| Rust CLI | `cli/src/commands/channel.rs` (new) | Channel CLI subcommands |
| Rust CLI | `cli/src/main.rs` | Register `channel` subcommand group |
| Tests | `tests/unit/`, `tests/integration/` | Channel store, routing, delivery, memory integration |

---

## Test Strategy

- **Unit tests**: `ChannelStore` CRUD, membership enforcement, message ordering, history pagination, per-channel cap pruning.
- **Thread FK cascade test** (Phase 1, called out separately because it spans the cap boundary and is easy to miss): publish 10,001 messages including a thread root that straddles the per-channel cap, force the prune step, and assert `(orphaned-reply count == 0)` and that no FK constraint violation surfaces. This is the test referenced in §B's "Pruning interaction with thread FK" paragraph; listing it here so it doesn't disappear into general CRUD coverage.
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

6. **`persatrix channel watch` SSE reuse**: The RFC 0018 LogService SSE infrastructure (`/api/v1/logs/stream`) uses the same pattern. Should Phase 4 implement `GET /api/v1/channels/{id}/messages/stream` instead of polling? Recommendation: **defer to v0.3.x** unless Phase 4 reveals the port is a one-day task. 5s polling is fine for the v0.3.0 user story (human spectator on a low-volume channel); SSE adds connection-lifetime handling and a new test surface competing with the higher-priority Phase 3 memory work.

7. **Human message gate-bypass**: Under default `when_mentioned`, a casual human message in a multi-agent channel produces no reply unless explicitly @-mentioned. Right default for busy channels, feels wrong in a 1-human-1-agent DM-like channel. Options: (a) human messages implicitly mention every member; (b) humans subject to the same gate, must @-mention; (c) per-channel `human_addresses_all: true`. Recommendation: **(b) for v0.3.0** with `--mention-all` on `persatrix channel send` covering broadcast. Defer (a)/(c) until usage data justifies it — silent agents in a DM-like channel is recoverable; flooding every agent on every casual message is not.

8. **Missed-message recovery protocol**: At-most-once delivery means offline agents miss messages. The history endpoint exposes the catch-up data, but the RFC does not specify *who* calls it or *how* an agent identifies "messages I missed". Sub-questions: (a) recovery trigger — startup, every tick, or first `CHANNEL_MESSAGE` after coming back? (per-tick is expensive at scale; on-startup misses mid-session gaps); (b) watermark vs. time — `before time.Time` is good for backscroll, weak for "since I was last seen"; a per-(channel, subscriber) high-watermark (last-seen `message_id`) would enable `?since=<watermark>`. Recommendation: **defer to v0.3.x**. v0.3.0 ships at-most-once with on-startup history fetch (last 50 per channel) as best-effort recovery; watermark and per-tick land once operational data justifies. `channel.delivery.missed` per OQ #2 provides the signal.

9. **`channel.schema.json` $id stability across the v0.2 → v0.3.0 rewrite.** The disposition rewrites the schema in place at the same path with the same `$id`. External tooling caching the document will silently break against v0.3.0 configs — vocabulary changed (`direct`→`dm`; `broadcast`/`meeting` dropped) and the `schema_version` field that would have signaled the break is also removed. For an internal-only v0.3.0 the cost is zero; missing is a public stance on whether `channel.schema.json` is stable public API. Recommendation: **declare it not-yet-public** in v0.3.0 release notes (RFC-owned, may break each v0.x bump until v1.0). If we change our mind, the right path is an `$id` bump on the next breaking change, not silent edits. **Implementation requirement carried into Phase 1**: the rewrite includes a top-level `description` *"Internal-only schema until v1.0; `$id` may break across v0.x bumps without notice."* — release notes are easy to miss; embedding the disclaimer in the schema itself surfaces it at validation time.

10. **`channel_type` proto-field redundancy with the `channel_id` prefix.** §C carries `channel_type` as a separate string on `ChannelMessageEvent` despite the prefix on `channel_id` already encoding it. Justified for log/observability ergonomics (see proto-block comment); also a drift risk if a call site updates one without the other. Phase 2's `ChannelRouter` validates on publish; receivers should drop on mismatch rather than trust one source. Should we typed-enum it at the proto level? Probably not for v0.3.0 — documentation-and-validation, revisit if drift bugs surface.

11. **Per-channel `cascade_depth` budget vs. global default.** §H notes a tight-loop pair channel saturates `max_cascade_depth=5` within five reactive turns. Operators wanting longer chains have only the global knob today, which also raises runaway-loop risk in misconfigured `always` channels. A per-channel override (e.g., `channels[].cascade_depth_max: 20`) would give granularity without weakening the default backstop. Cost: schema field + dispatcher plumbing + docs. Recommendation: **defer to v0.3.x** — collect dogfood data on whether cascade saturation is actually the user-visible failure mode, then decide.

12. **Typo-resistance on `payload["respond_policy"]`.** §D promotes `thread_id` to top-level on misspell-resistance grounds; the same argument seemingly applies to `respond_policy`, but §D keeps it in `payload` because policy is denormalized cargo from `memberships`, not intrinsic to the event. Open: cement that distinction with a typed accessor (`event.policy_for(agent_id)` that reads memberships directly and ignores `payload`), or accept the typo risk on grounds that the value is set in one place and read in one place? Recommendation: **accept the risk for v0.3.0**; revisit if a typo bug surfaces or the dispatcher gains a second policy-setting code path.

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
- [RFC 0017](0017-persona-memory-injection-budget.md) — `MemoryBudget` allocator (reused as-is; channel history admitted via the existing greedy-priority pool, see §E)
- [RFC 0019](0019-opentelemetry-completion.md) — OTEL spans and metrics pipeline (reused for channel observability)
- [RFC 0020](0020-interaction-lifecycle.md) — Interaction lifecycle (Phase 3 jointly delivered with this RFC's Phase 3; channel messages flow through `InteractionTracker.add_turn` rather than per-event episodic writes — see §E)
- [Roadmap](../../ROADMAP.md)
