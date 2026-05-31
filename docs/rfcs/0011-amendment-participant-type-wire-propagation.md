# RFC 0011 Amendment — Participant-Type Wire Propagation

**Type**: amendment to [RFC 0011](0011-channels-bridges.md) §C (channel-message delivery) and the [chat-as-DM amendment](0011-amendment-chat-as-dm.md) §Mapping
**Status**: ✅ Adopted
**Date**: 2026-05-31
**Trigger**: [ISSUE-0068](../issues/ISSUE-0068-chat-peer-recorded-as-agent-participant-type.md) — found during the v0.3.3 release-prep dry-run of `MT-CHAT-004`: a human chatting with a persona over REST is recorded in the relationship tier with `other_participant_type = "agent"`, not `"user"`.
**Supersedes**: nothing — closes a wire gap the chat-as-DM amendment's §Mapping table assumed was already plumbed end-to-end.

---

## Context

REST chat is delivered to the agent as a **channel message**, not via the gRPC `SendChatMessage` servicer (chat-as-DM amendment). The peer type the caller supplies (`participant_type: "user"`) crosses two boundaries on the way to the agent's relationship tier:

1. **Caller → orchestrator (REST `POST /api/v1/agents/{id}/chat`)** — [`chat_handler.go`](../../internal/server/chat_handler.go) sets `ChannelMessage.Metadata["participant_type"]` and publishes via the channel router.
2. **Orchestrator → agent (gRPC `AgentService.ReceiveChannelMessage` carrying [`ChannelMessageEvent`](../../proto/task.proto))** — fields 1–11 are typed scalars; the message has **no metadata map.**

The second boundary drops `participant_type`: `ChannelMessageEvent` has no field to carry it. On the agent, [`episode_routing.py`](../../agents/persona_runtime/episode_routing.py) reads the sender type from `event.metadata.get("sender_participant_type", "agent")` — absent for a channel-delivered chat — so it **defaults to `"agent"`**, which flows to `other_participant_type` at interaction close ([`record_close.py`](../../agents/persona_runtime/record_close.py)).

There was also a **key-name mismatch**: the publish side wrote `participant_type` while the agent read side expected `sender_participant_type`. Even a metadata map on the event would not have closed the gap without reconciling the keys.

This is structurally the **same defect class as the [cascade-depth wire propagation amendment](0011-amendment-cascade-depth-wire-propagation.md)**: a field the in-process design assumed was present is silently dropped at the `ChannelMessageEvent` boundary because that message has no metadata map. The fix is the same shape — a typed scalar field — and the trade-off (typed field vs. adding a metadata map for one value) resolves the same way.

## The amended contract

### gRPC (orchestrator → agent fanout)

`ChannelMessageEvent` gains a new typed scalar:

```protobuf
string sender_participant_type = 12;  // proto/task.proto
```

`"user"` for a human chat peer, `"agent"` for an inter-agent sender. Typed rather than carried in an ad-hoc metadata map for the same reason as `cascade_depth = 11`: `ChannelMessageEvent` has no metadata map, and adding one purely for this value is a strictly larger surface than adding the value. **Empty (proto3 implicit presence)** is the genuine agent-to-agent case and resolves to `"agent"` downstream — ordinary channel fanout never fabricates a peer type.

### REST (orchestrator publish) — the default

The REST chat handler defaults an **omitted** `participant_type` request field to `"user"` before publish. REST chat is, by construction, a human talking to a persona; an empty field is the common case, not a missing one. This matches the gRPC `SendChatMessage` servicer's OQ-3 `"user"` default ([`server_servicers.py`](../../agents/server_servicers.py)), so the two chat entry points agree. An explicit request value is passed through verbatim (e.g. a future bridge integration tagging a non-human sender).

The default lives in the **REST handler**, not the dispatcher: the dispatcher translates *all* channel messages (including agent-to-agent fanout), so a default there would mislabel inter-agent traffic as `"user"`. Only the handler knows it is building a human→agent DM.

### Key reconciliation

The orchestrator populates `sender_participant_type` from `ChannelMessage.Metadata["participant_type"]`. The agent's servicer lifts the typed field onto `event.metadata["sender_participant_type"]` — the exact key the episode-routing close path already reads. The rename happens once, at the gRPC ingress boundary.

### Trust model

`sender_participant_type` is **advisory, low-stakes** input. Unlike `cascade_depth`, a wrong value does not enable a resource-exhaustion cascade — it degrades relationship-tier peer typing only. The agent-side read path already constrains the value to the `{"agent", "user"}` allowlist ([`record_close.py`](../../agents/persona_runtime/record_close.py) `extract_peer_from_interaction`), so an out-of-vocabulary value safely degrades to `"agent"` rather than corrupting the row. No inbound clamp or rejection is added at the boundary.

## What this amendment does NOT change

- The relationship-tier schema, the interaction-close mechanics (count / type / trust), and the DM-scope peer extraction are unchanged — only the peer *type* the chain was already trying to record is now delivered.
- The gRPC `SendChatMessage` path (which already defaults `participant_type` to `"user"` and sets `sender_participant_type`) is untouched; it was already correct and is simply no longer the only path that types its peer.
- Multi-agent-per-process channel routing remains deferred to RFC 0011 PR 4a-ii (`ChannelMessageEvent` still has no `recipient_id`); this amendment adds no recipient disambiguation.

## Implementation

Single PR (ISSUE-0068), test-driven:

1. **proto** — `string sender_participant_type = 12` on `ChannelMessageEvent`; regenerate Go + Python stubs. Wire-shape round-trip tests on both sides extended to pin field 12.
2. **Go orchestrator** — `channelMessageToProto` lifts `Metadata["participant_type"]` onto the field ([`grpc_dispatcher.go`](../../internal/channels/grpc_dispatcher.go) + `participant_type.go` reader, sibling of `cascade_depth.go`); `chat_handler.go` defaults an omitted request field to `"user"`.
3. **Python agent** — `ReceiveChannelMessage` seeds `event.metadata["sender_participant_type"]` from the typed field when non-empty ([`server_servicers.py`](../../agents/server_servicers.py)); the existing episode-routing → `record_close` path consumes it unchanged.

The agent-side consumer half (`event.metadata["sender_participant_type"]` → `other_participant_type`) predates this amendment and is already exercised by integration tests that inject the metadata key directly — this amendment delivers the value those tests assumed the wire carried.

## Glossary

See [`docs/ai-glossary.md`](../ai-glossary.md) → `Participant Type` (if absent, the relationship-tier `other_participant_type` entry).
