---
id: RFC-0032
title: Wire-Level Channel Interaction Layer and Chat-Façade Unification
summary: Promote RFC 0020's server-side Interaction concept to a wire-visible identifier carried on every channel message, and unify the RFC 0016 chat surface as a thin client over channel primitives.
type: architecture
status: draft
author: Maksim Khomutov
created: 2026-05-12
target: v0.4.0+
depends_on:
  - RFC-0011
  - RFC-0016
  - RFC-0020
  - RFC-0029
  - RFC-0031
---

# RFC 0032 — Wire-Level Channel Interaction Layer and Chat-Façade Unification

**Type**: architecture
**Status**: 🔨 Draft (stub)
**Author**: Maksim Khomutov
**Date**: 2026-05-12
**Target**: v0.4.0+
**Depends on**: RFC 0011 (Channels), RFC 0016 (Human-Participant Chat), RFC 0020 (Interaction Lifecycle), RFC 0029 (Personal/Society Storage Split), RFC 0031 (Per-Session Namespacing)
**Relates to**: RFC 0018 (Structured Logging), RFC 0019 (OpenTelemetry)

---

## Table of Contents

- [Summary](#summary)
- [Motivation](#motivation)
- [Goals](#goals)
- [Non-Goals](#non-goals)
- [Design / Implementation](#design--implementation)
  - [A. Vocabulary](#a-vocabulary)
  - [B. Hierarchy](#b-hierarchy)
  - [C. Wire Surface (Sketch)](#c-wire-surface-sketch)
  - [D. Chat-Façade Unification](#d-chat-façade-unification)
  - [E. Relationship to RFC 0020 Internal Interactions](#e-relationship-to-rfc-0020-internal-interactions)
- [Security Considerations](#security-considerations)
- [Phased Implementation Plan](#phased-implementation-plan)
- [Files Touched (Estimated)](#files-touched-estimated)
- [Test Strategy](#test-strategy)
- [Open Questions](#open-questions)
- [Decision / Next Steps](#decision--next-steps)
- [Related Documentation](#related-documentation)

---

## Summary

Today the orchestrator carries **three different identifiers** for closely-related concepts: `channel_id` (the transport, [RFC 0011](0011-channels-bridges.md)), `chat_session_id` (per-conversation token on the chat surface only, [RFC 0016](0016-human-participant-chat-interface.md) — renamed from `session_id` in v0.3.1 per [RFC 0031 OQ #8](0031-per-session-namespacing-channels.md#open-questions)), and `thread_id` (per-message reply pointer on `ChannelMessageEvent`). None of these carry a uniform "which conversation does this message belong to" semantic that works symmetrically across DMs and group channels. The chat surface invented `chat_session_id` to fill that gap for human-driven DMs only; agent-to-agent traffic on group channels has no equivalent and treats every channel as an unbounded stream.

This RFC proposes promoting [RFC 0020](0020-interaction-lifecycle.md)'s server-side `Interaction` concept to a **wire-visible identifier** carried on every channel message (`interaction_id`), so DMs and group channels share one model. The RFC 0016 chat REST/gRPC surface then collapses to a **thin synchronous-reply façade** over channel primitives — either retained for ergonomics or deprecated entirely (see Open Question 1). The [RFC 0011 chat-as-DM amendment](0011-amendment-chat-as-dm.md) unified the transport in v0.3.0; this RFC completes the API-level unification the amendment explicitly deferred.

This RFC is a **stub**: problem statement, vocabulary, and load-bearing open questions are in scope; field names, schema deltas, and migration sequencing are not committed until the OQs resolve.

## Motivation

Three problems compound today, and the v0.3.1 `chat_session_id` rename is a symptomatic fix that does not address any of them:

1. **No wire concept for "conversation within a channel."** [`ChannelMessageEvent.thread_id`](../../proto/task.proto#L155-L157) is documented as a per-message reply pointer ("empty string if not a reply"), not a conversation token. On group channels, a busy `#planning` channel that hosts several distinct conversations per day has no way to tag messages by conversation. [RFC 0020 §G](0020-interaction-lifecycle.md) calls this out: "topic-shift would be the natural improvement here — explicitly deferred."

2. **`chat_session_id` is a one-channel-type kludge.** The chat REST/gRPC handler ([`internal/server/chat_handler.go:281-296`](../../internal/server/chat_handler.go#L281-L296)) propagates the chat-conversation token as `ChannelMessage.Metadata["chat_session_id"]`. It exists only because the chat façade needed *something* to correlate request and reply across multiple HTTP turns. Group channels and bridges (future RFC 0011 work) have no equivalent and would each invent their own metadata key for the same concept.

3. **RFC 0020 already defines Interaction internally, but only per-agent.** Each agent's memory store closes its own interaction independently. Two agents on the same DM may disagree about where one interaction ended and the next began. A wire-level `interaction_id` published by the channel router gives all participants a shared frame of reference and lets the per-agent boundary detector consume the wire value instead of re-deriving it.

What happens if we do nothing: each new channel-shaped surface (group channels with topic-shift, bridges to Slack/Discord, multi-agent governance per [RFC 0030](0030-multi-agent-conversation-governance.md)) will either reinvent its own conversation-id metadata key or treat its channel as an unbounded stream. The chat façade's `chat_session_id` becomes a permanent special case rather than a temporary one. Persona-memory indexing under [RFC 0029](0029-personal-society-storage-split.md) inherits the asymmetry — `(operator_session, channel, ???)` works for chat and is `(operator_session, channel)` for everything else.

## Goals

1. **One identifier per concept across all channel types.** A uniform `interaction_id` (final name TBD — see OQ 2) on every `ChannelMessageEvent`, regardless of `channel_type`. After this RFC, `chat_session_id` either becomes an alias for the DM channel's `interaction_id` or is retired entirely.
2. **Compose with RFC 0020.** The wire-level `interaction_id` is *the* identifier that RFC 0020's per-agent `InteractionTracker` keys on. Per-agent boundary detection still runs (an agent can decide its local interaction has ended even if the wire-level one is still open), but the shared identifier provides the cross-participant anchor that today's per-agent model lacks.
3. **Symmetric A2A and human channels.** Agent-to-agent traffic on a group channel gets the same conversation-tagging semantics as a human-driven DM. Topic-shift on group channels (RFC 0020 §G's deferred item) becomes implementable because there is now a wire identifier to roll over.
4. **Bridges-ready.** The future RFC 0011 bridge work (Slack, Discord, etc.) inherits `interaction_id` automatically — a Slack thread maps cleanly onto a Persatrix interaction without bridge-specific metadata-key conventions.
5. **Non-disruptive for existing chat consumers** *during the deprecation arc.* The chat REST/gRPC surface continues to work; whether it remains long-term is OQ 1. If retired, deprecation follows the project's standard arc with the field aliased through the bridging release.

## Non-Goals

- **A new transport.** Channels remain the transport ([RFC 0011](0011-channels-bridges.md)). This RFC adds a field, not a protocol.
- **Replacing RFC 0020's internal Interaction model.** RFC 0020's per-agent `InteractionTracker`, lifecycle states, summarization granularity, and recall semantics all stand. This RFC adds a wire-level identifier *under* RFC 0020 — what changes is that the per-agent tracker reads a shared id off the wire instead of inventing its own.
- **Renaming `channel_id` or `thread_id`.** `channel_id` is the transport; `thread_id` keeps its existing per-message-reply-pointer semantic. The new `interaction_id` is a third, distinct field.
- **Operator-session ([RFC 0031](0031-per-session-namespacing-channels.md)) scope changes.** Operator-session remains the top-level namespace. `interaction_id` lives one level below `channel_id`, which lives under `operator_session_id`. The two RFCs compose orthogonally.
- **Cross-channel interactions.** An interaction belongs to exactly one channel. A multi-channel conversation (e.g. "discussion that started in DM and moved to a group channel") is out of scope; that's a future RFC if ever.
- **Implementation commitment in this draft.** Field names, schema deltas, proto field numbers, and migration sequencing are all TBD until OQs 1–6 resolve.

---

## Design / Implementation

### A. Vocabulary

| Term | Definition | Storage |
|------|------------|---------|
| **Interaction** (wire) | A bounded conversation on a single channel, identified by a stable `interaction_id` and carried on every `ChannelMessageEvent`. Generated by the channel router on the first message of a fresh interaction; rolled over by an explicit close signal or a structural/idle-gap boundary. | New column on the RFC 0011 `messages` table; new field on `ChannelMessageEvent`. |
| **interaction_id** (working name) | Short canonical identifier, scoped within a `channel_id`. UUIDv7-derived by default so creation-order sort is free. Final name pending **OQ 2**. | Column / proto field. |
| **Interaction** (per-agent, RFC 0020) | An agent's *local* view of the conversation: lifecycle state (`open` / `closing` / `closed` / `summarized`), turn buffer, summary at close. Unchanged by this RFC. | RFC 0020 §D `interactions` table per agent. |
| **Chat session** | Retired concept (post-RFC). Until retired, `ChatRequest.chat_session_id` aliases the DM channel's `interaction_id`. | None new; alias only. |

The bold structural point: **one concept ("conversation on a channel") with two views — a shared wire identifier (this RFC) and a per-agent local lifecycle (RFC 0020).** Today only the local view exists; the wire view is what this RFC adds.

### B. Hierarchy

```
operator session (RFC 0031)              ← top-level namespace
  └─ channel (RFC 0011)                  ← persistent transport
       └─ interaction (THIS RFC)         ← wire-level conversation tag
            └─ message
                 └─ optional reply pointer (thread_id, RFC 0011)
```

Compared to today, the **interaction** row is new. Every other layer already exists.

### C. Wire Surface (Sketch)

*Not committed in this draft — illustrative only.*

```proto
message ChannelMessageEvent {
  // ... existing fields ...

  // RFC 0032: stable id for the conversation on this channel that
  // this message belongs to. Empty only during the brief migration
  // window; required after Phase 2. See OQ 4 for who generates it
  // and OQ 5 for rollover semantics.
  string interaction_id = N;  // field number TBD
}
```

`ChatRequest.chat_session_id` becomes either:
- **OQ 1a (façade retained)** — a server-honored alias that the chat handler resolves to `interaction_id` on publish and echoes back on reply.
- **OQ 1b (façade deprecated)** — removed in the deprecation-end release; clients call channel primitives directly.

### D. Chat-Façade Unification

The [RFC 0011 chat-as-DM amendment](0011-amendment-chat-as-dm.md) preserved the chat REST/gRPC endpoint as a synchronous-reply *façade* over the DM channel. This RFC re-opens that decision. Three real costs gated the amendment's choice; they are still real and inform OQ 1:

1. **Synchronous-reply correlator.** The chat handler's `replyWaiter` (`channel_router.PublishAndAwait`) is keyed on `(channelID, awaitFromAgentID)` and produces an HTTP 409 on concurrent-chat collision. If clients do publish-and-await themselves, they need streaming channel reads, correct timeout semantics, and the collision case. Either every client reimplements this, or we ship a generic `AwaitChannelReply` RPC — which is almost exactly `SendChatMessage` minus the chat-specific name.

2. **Auth checkpoint.** [chat-as-DM §Security note](0011-amendment-chat-as-dm.md) — DM-channel creation is the only place that gates "is this user allowed to address this agent?", because per-publish gating is bypassed on DMs by design. This stays server-side regardless of OQ 1.

3. **Public-API deprecation.** `POST /api/v1/agents/{id}/chat` and `AgentService.SendChatMessage` have Rust CLI consumers, manual tests ([MT-CHAT-001](../manual-tests/MT-CHAT-001.md), [MT-CHAT-003](../manual-tests/MT-CHAT-003.md)), integration tests, and out-of-tree consumers. Removal follows the project's standard deprecation arc.

**Tentative framing** (subject to OQ 1): the cleanest decomposition is to keep a generic synchronous-reply primitive (`AwaitChannelReply` or equivalent) on `AgentService` for clients that want one round-trip ergonomics, and let `persatrix chat` become a thin client wrapper. That captures most of the unification gain without forcing every client to implement publish-and-await independently.

### E. Relationship to RFC 0020 Internal Interactions

RFC 0020 §A defines `Interaction` as the unit of summarization, episodic storage, and relationship-memory updates. Today each agent runs its own `InteractionTracker` and decides locally when one interaction ends. Two consequences this RFC addresses:

- **Cross-participant disagreement.** Two agents on the same DM may close their local interactions at different turns. The wire-level `interaction_id` does not force them to agree on lifecycle — agents may still close locally early — but it gives a shared anchor for cross-agent correlation (relationship memory, structured logs, OTEL spans).
- **Group-channel topic-shift (RFC 0020 §G's deferred item).** With a wire identifier, topic-shift becomes "router rolls the `interaction_id` over." Agents observe the change and close their local interactions in response, rather than each detecting topic-shift independently from their own message buffers.

Pending **OQ 3** resolution, the per-agent `interactions` table from [RFC 0020 §D](0020-interaction-lifecycle.md) either gains a cross-reference column to the wire `interaction_id` (two-distinct-ids branch — wire id and agent-local id linked) or simply carries the wire value in its existing `interaction_id` column (one-id branch — same value flows wire→agent). Either way, recall and reflection can cross-reference. **OQ 6** commits the column shape once OQ 3 lands.

---

## Security Considerations

- **No new auth surface.** Interaction id is a correlation token, not a permission boundary. Authorization remains at channel-membership granularity ([chat-as-DM §Security note](0011-amendment-chat-as-dm.md)).
- **Identifier predictability.** UUIDv7 leaks creation order. For multi-tenant futures (RFC 0009 / RFC 0013 territory), the choice between UUIDv7 and unguessable UUIDv4 matters and is captured in OQ 4.
- **Façade deprecation (if OQ 1b).** Removing `POST /api/v1/agents/{id}/chat` and `AgentService.SendChatMessage` is a public-API break. The deprecation arc must preserve a working path for external integrations through the bridging release.
- **Cross-session leakage.** Interaction ids are scoped under operator-session ([RFC 0031](0031-per-session-namespacing-channels.md)); cross-session recall paths must filter consistently. Verified by integration test, not relied on as an isolation property.

## Phased Implementation Plan

Concrete phases TBD pending Open Questions 1–6. Indicative shape (no commitments):

### Phase 1 (sketch): Wire identifier

Add `interaction_id` to `ChannelMessageEvent` and the RFC 0011 `messages` table. Channel router generates it. Per-agent RFC 0020 `InteractionTracker` consumes it as input. No chat-surface changes yet — `chat_session_id` continues to ride in metadata and is independently assigned.

### Phase 2 (sketch): Chat-surface alias

Make `ChatRequest.chat_session_id` resolve to the DM channel's `interaction_id` server-side. External JSON consumers see no break; internal storage normalizes on one id. Per-agent `interactions` table gains the wire-id reference (OQ 6).

### Phase 3 (sketch): Façade decision (OQ 1)

Either:
- **3a** — keep the chat façade indefinitely as the ergonomic synchronous-reply path, with `chat_session_id` as a documented alias for `interaction_id`; OR
- **3b** — deprecate the chat REST/gRPC surface in favor of `AwaitChannelReply` (or equivalent generic primitive); remove in the post-deprecation release.

### Phase 4 (sketch): Group-channel topic-shift

Implement RFC 0020 §G's deferred topic-shift detection by rolling `interaction_id` at the router. Per-agent trackers observe and close their local interactions in response.

Phases 1 and 2 are independently shippable. Phase 3 requires OQ 1 resolution. Phase 4 requires Phase 1 + 2 and is independent of Phase 3.

## Files Touched (Estimated)

Estimates are structural, not commitments — they shift with OQ resolutions.

| Component | Files | Change |
|-----------|-------|--------|
| Protos | `proto/task.proto` | Add `interaction_id` to `ChannelMessageEvent`; field number TBD. Possibly add `AwaitChannelReply` RPC (OQ 1b). |
| Go orchestrator | `internal/channels/router.go`, `internal/channels/store.go`, `internal/server/chat_handler.go`, `internal/server/types.go` | Router generates `interaction_id`; store persists it on the `messages` row; chat handler aliases `chat_session_id` → `interaction_id`. |
| Python agents | `agents/server_servicers.py`, `agents/persona.py`, RFC 0020 `InteractionTracker` integration | Consume wire `interaction_id` as the canonical id for the local interaction. |
| Rust CLI | `cli/src/commands/chat.rs`, `cli/src/types.rs` | Receive `interaction_id` echo on reply; render or surface per CLI UX decision. |
| Storage | RFC 0011 `messages` table; RFC 0020 per-agent `interactions` table | New column; new cross-reference column. |
| Docs | RFC 0011, RFC 0016, RFC 0020 amendments; operator guides | Amendment blocks noting the unification. |

## Test Strategy

- **Unit tests**: Router-side `interaction_id` generation and rollover; chat-handler alias resolution; per-agent tracker consumption of wire id.
- **Integration tests**: DM round-trip with explicit and implicit `interaction_id`; group-channel rollover at topic-shift (Phase 4); chat-surface alias compatibility across the deprecation arc.
- **E2E / smoke tests**: `persatrix chat` REPL continues to work end-to-end after Phase 2; `persatrix channel` group-message flow gets symmetric `interaction_id` tagging.
- **Manual tests**: New MT for cross-participant interaction-id agreement on a DM; extension of MT-MEMORY-005 to verify dementia-test continuity is unaffected.

## Open Questions

1. **Chat façade fate.** Keep `POST /api/v1/agents/{id}/chat` and `AgentService.SendChatMessage` as the ergonomic synchronous-reply path (3a), or deprecate in favor of a generic `AwaitChannelReply` primitive (3b)? Trade-off: ergonomic loss vs. one less special case. Resolution required before Phase 3 can sequence.

2. **Field name.** `interaction_id` (matches RFC 0020), `conversation_id` (more familiar to API consumers), `session_id` (collides with RFC 0031 — rejected), or `thread_id` (collides with RFC 0011's reply-pointer — rejected)? Decision affects every wire surface and structured-log key.

3. **Relationship to RFC 0020 internal `interaction_id`.** RFC 0020 §D defines a per-agent `interaction_id` on the `interactions` table. Is the wire id the same value (one id flowing wire→agent), or two distinct ids cross-referenced (wire id and agent-local id)? The former is simpler; the latter preserves per-agent independence if two agents need to close at different turns.

4. **Generator and identifier shape.** Channel router generates on first publish (canonical), or first participant to publish (operator-controllable)? UUIDv7 (creation-order sort) or UUIDv4 (unguessable)? Multi-tenant futures (RFC 0009) may force the latter.

5. **Rollover semantics.** What rolls the `interaction_id` over on group channels? Idle-gap (mirrors RFC 0020 §B), explicit operator/agent close action, topic-shift detection (deferred per RFC 0020 §G), or all three? DMs are simpler — one open interaction per pair at a time — but group channels need a policy.

6. **Cross-reference column on RFC 0020 `interactions` table.** Add `wire_interaction_id` column? Hash-join via the `messages` table? Schema commitment depends on OQ 3.

7. **Migration of historical rows.** Pre-RFC `messages` and `episodes` rows have no `interaction_id`. Synthetic `legacy` value (mirrors RFC 0031's legacy-session approach), or NULL with a one-row-per-message fallback semantic? Affects recall behaviour for cross-RFC queries.

## Decision / Next Steps

This RFC is **🔨 Draft (stub)**. Before moving to `📋 Proposed`:

1. Resolve **Open Question 1** (chat façade fate). This determines whether the RFC is additive (3a) or deprecating (3b) and re-shapes the phased plan accordingly.
2. Resolve **Open Question 2** (field name). Touches every wire surface.
3. Confirm **Open Question 3** (wire-id ↔ RFC 0020 per-agent id relationship) with the memory subsystem owner. Composes with [RFC 0029](0029-personal-society-storage-split.md) facade signatures.

Open Questions 4–7 may be resolved during phased-implementation review without blocking promotion to `📋 Proposed`. Note that OQ 4 (generator/identifier shape) and OQ 7 (historical-row migration) gate **Phase 1 implementation** even though they do not gate proposal — they must land before Phase 1 ships, not before the document promotes.

This stub does not commit to wire field numbers, schema deltas, or migration sequencing. Once OQs 1–3 resolve, this document is rewritten as a full proposal with the canonical phased plan.

## Related Documentation

- [RFC 0011 — Channels + Bridges](0011-channels-bridges.md)
- [RFC 0011 amendment — Chat as DM](0011-amendment-chat-as-dm.md)
- [RFC 0016 — Human-Participant Chat Interface](0016-human-participant-chat-interface.md)
- [RFC 0020 — Interaction Lifecycle](0020-interaction-lifecycle.md)
- [RFC 0029 — Personal/Society Storage Split](0029-personal-society-storage-split.md)
- [RFC 0031 — Per-Session Namespacing for Channels and Persona Memory](0031-per-session-namespacing-channels.md)
- [RFC 0030 — Multi-Agent Conversation Governance](0030-multi-agent-conversation-governance.md) — consumer of uniform interaction tagging
- [Channels operator guide](../guides/channels.md)
