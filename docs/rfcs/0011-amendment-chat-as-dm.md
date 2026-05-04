# RFC 0011 Amendment — Chat as DM Channel (RFC 0016 Unification)

**Type**: amendment to [RFC 0011](0011-channels-bridges.md) §F
**Status**: ✅ Adopted
**Date**: 2026-05-04
**Trigger**: PR #246 (RFC 0011 PR 3) review surfaced collision with RFC 0016 chat
**Supersedes**:
- RFC 0011 §"Relationship to Existing Scaffolding" assumption that the renamed event/action types had no producer.
- RFC 0016 §Non-Goals → "Channel routing for user messages" (now in scope for v0.3.0+).
- RFC 0016 status flipped to "Implemented (partially superseded by this amendment)".

---

## Context

RFC 0011's [§Relationship to Existing Scaffolding](0011-channels-bridges.md#design--implementation) renamed `EventType.MESSAGE_RECEIVED` → `CHANNEL_MESSAGE` and `ActionType.SEND_MESSAGE` → `SEND_CHANNEL_MESSAGE` on the stated premise that *"pre-existing types had no producer beyond partial scaffolding."* That premise was true at RFC authoring time but is now stale: RFC 0016 (shipped in v0.2.1) made these the heavy producer/consumer for the chat ingest/reply path. Concrete v0.2.1 producers on `main` include [agents/server_servicers.py](../../agents/server_servicers.py) `SendChatMessage` (builds `MESSAGE_RECEIVED`) and [agents/persona.py](../../agents/persona.py) (emits `SEND_MESSAGE` for chat replies).

A blind rename across PR 4a/4b would create a window where chat REST/gRPC and the `persatrix chat` REPL are broken on `main`. This amendment pins a unified design so the rename + chat-path migration land atomically in PR 4a.

## The unified model

A user–agent chat is a `dm` channel between a `UserParticipant` and a persona agent. The unification is structural, not cosmetic: one event/action pair on the wire (`CHANNEL_MESSAGE` / `SEND_CHANNEL_MESSAGE`), one ingest path (channel publish), one reply path (channel publish back), one history store (RFC 0011 §B `channels` + `messages` tables). The chat REST endpoint and `persatrix chat` REPL are preserved as synchronous-reply *façades* over this DM channel, not as a parallel transport.

### Mapping

| RFC 0016 (v0.2.1) | Unified v0.3.0+ model |
|-------------------|------------------------|
| `POST /api/v1/agents/{id}/chat` (synchronous) | Same endpoint; handler resolves the DM via `ChannelStore.GetOrCreateDM(user_id, agent_id)`, calls `ChannelRouter.Publish`, awaits one `SEND_CHANNEL_MESSAGE` reply on the same DM channel, returns it as the HTTP response. |
| `SendChatMessage` gRPC RPC | Retained on `AgentService` as the synchronous-reply wrapper; internally builds a `ChannelMessageEvent` (channel_type=`dm`) and routes through the same `ReceiveChannelMessage` handler PR 4a implements for channel-side delivery. |
| `EventType.MESSAGE_RECEIVED` (chat ingest) | `EventType.CHANNEL_MESSAGE` with `channel_type=dm`; `metadata["participant_type"]` retained for sender-type discrimination. |
| `ActionType.SEND_MESSAGE` (chat reply) | `ActionType.SEND_CHANNEL_MESSAGE`; reply-extraction (RFC 0016 OQ 5) reformulated as: `SEND_CHANNEL_MESSAGE` whose `channel_id` matches the inbound DM and whose `mentions` includes the user → any `SEND_CHANNEL_MESSAGE` on the same DM → `COMPLETE_TASK` → empty string. |
| `RelationshipMemory.record_interaction(other_participant_type="user")` | Unchanged — runs on the same code path; the unification does not alter when relationship updates fire. |

### DM gate-bypass

The RFC 0011 §D response gate is implicitly `always` for the non-sender on DM channels: the user implicitly mentions the agent by addressing the DM, and the chat caller is blocked waiting for a reply. The `respond` policy on a DM-channel membership is therefore not consulted. Group-channel gating semantics in §D are unchanged.

### What stays in RFC 0016

RFC 0016 remains authoritative for:
- The `Participant` abstraction.
- The `UserParticipant` storage model (SQLite schema, `last_seen` semantics).
- The `persatrix chat` REPL UX (commands, prompts, error messaging).
- The chat REST/gRPC surface *shape* (request/response field names, status codes, timeout knob `timeout_seconds`).

Only the wire types and the underlying transport are unified.

## Migration sequencing

PR 4a (`feature/v030-rfc0011-agent-delivery`) carries:
1. The `EventType` / `ActionType` rename.
2. The chat ingest path migration (`SendChatMessage` servicer → publish-and-await on DM channel).
3. The DM gate-bypass rule.
4. The reply-extraction reformulation.
5. The atomic flip of all RFC 0016 chat tests (`tests/unit/python/test_send_chat_message.py`, `tests/integration/test_chat_endpoint.py`, the `persatrix chat` Rust integration tests).

Splitting any of these from the rename would create a window where chat is broken on `main` — explicitly rejected. PR 4b carries the non-DM response gate, DELETE endpoints, and the two-agent integration test.

## Glossary

See [`docs/ai-glossary.md`](../ai-glossary.md) → `Chat-as-DM` and the updated `Respond Policy` entry.
