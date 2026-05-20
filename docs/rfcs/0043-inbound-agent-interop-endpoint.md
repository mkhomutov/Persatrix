---
id: RFC-0043
title: Inbound Agent-Interop Endpoint
summary: Add a bounded inbound surface that lets a non-Persatrix agent join a channel — speak, be addressed, hear other participants — without speaking the orchestrator's internal gRPC contract, scoped narrowly enough that it cannot expose any orchestrator API the internal contract does not already authorize.
type: protocol
status: draft
author: Maksim Khomutov
created: 2026-05-20
target: v0.4.0+
depends_on:
  - RFC-0011
  - RFC-0016
  - RFC-0039
  - RFC-0040
---

# RFC 0043 — Inbound Agent-Interop Endpoint

**Type**: protocol
**Status**: 🔨 Draft
**Author**: Maksim Khomutov
**Date**: 2026-05-20
**Target**: v0.4.0+
**Depends on**: RFC 0011 (Channels & Bridges — the channel primitive external agents join), RFC 0016 (Human Participant & Chat Interface — the participant model this RFC extends with a third subtype), RFC 0039 (User Accounts & Authentication — auth shape this RFC reuses for external-agent credentials), RFC 0040 (Agent–Orchestrator Transport Unification — the internal gRPC contract this RFC explicitly leaves untouched)
**Relates to**: RFC 0012 (Protocols & Organizations — cross-org federation territory, deliberately out of scope here), RFC 0009 (Security & Sandboxing — auth, rate-limit, audit-log mechanisms this RFC reuses), RFC 0037 (Memory Confidentiality — external participants are a new principal type that confidentiality rules must account for)
**Spawned from**: [agent-runtime-vocabulary-roadmap.md §Seam 4](../agent-runtime-vocabulary-roadmap.md#seam-4--inbound-agent-interop-endpoint)

---

## Table of Contents

- [Summary](#summary)
- [Motivation](#motivation)
- [Goals](#goals)
- [Non-Goals](#non-goals)
- [Design / Implementation](#design--implementation)
  - [A. The `ExternalAgentParticipant` subtype](#a-the-externalagentparticipant-subtype)
  - [B. Endpoint surface](#b-endpoint-surface)
  - [C. Auth and identity](#c-auth-and-identity)
  - [D. Capability scope](#d-capability-scope)
  - [E. Message inbound / outbound flow](#e-message-inbound--outbound-flow)
  - [F. Failure modes and abuse](#f-failure-modes-and-abuse)
- [Security Considerations](#security-considerations)
- [Migration Path](#migration-path)
- [Phased Implementation Plan](#phased-implementation-plan)
- [Files Touched (Estimated)](#files-touched-estimated)
- [Test Strategy](#test-strategy)
- [Open Questions](#open-questions)
- [Decision / Next Steps](#decision--next-steps)
- [Related Documentation](#related-documentation)

---

## Summary

Every agent that participates in a Persatrix channel today must speak the orchestrator's internal gRPC contract — `AgentService` on the agent side, `OrchestratorService` ([RFC 0040](0040-agent-orchestrator-transport-unification.md)) on the orchestrator side. The only inbound surface for a non-Persatrix participant is the human chat path ([RFC 0016](0016-human-participant-chat-interface.md)), which does not generalize: it assumes interactive turn-taking, a single human caller, and a UI-driven workflow.

This RFC adds a third participant subtype — `ExternalAgentParticipant` — and a bounded HTTP/JSON inbound endpoint that lets a non-Persatrix agent join a channel: post messages, receive messages addressed to it, and observe channel traffic it is authorized to see. The internal gRPC contract is untouched. The endpoint is narrow on purpose: it can send and receive channel messages and nothing else. It is not a back-door for workflows, persona memory, wallet leases, or any other orchestrator API.

## Motivation

### M-1. Channels are valuable to non-Persatrix agents too

The channel primitive — typed messaging with persona membership, transcript persistence, confidentiality classification ([RFC 0037](0037-memory-confidentiality-channel-classification.md)), and conversation governance ([RFC 0030](0030-multi-agent-conversation-governance.md)) — is one of Persatrix's distinguishing features. Limiting it to Persatrix-internal agents forecloses on use cases the architecture otherwise supports: a user's existing custom agent joining a Persatrix-mediated planning channel, a third-party automation observing a channel for handoff signals, a cross-team integration where one side runs Persatrix and the other does not.

### M-2. The internal contract is the wrong surface for outsiders

`AgentService` exposes work-distribution and lifecycle calls that are meaningful only to a Persatrix-managed agent worker — task assignment, lease acquisition, registration heartbeat. Reusing it as an external interop surface would either:

- expose all of that to outsiders (security hole), or
- add per-method allow-lists (every method becomes a security review).

A separate, narrow surface is cleaner. The internal contract stays internal; the external surface is small enough to reason about in one sitting.

### M-3. The CLI/web path doesn't generalize

[RFC 0016](0016-human-participant-chat-interface.md) covers humans talking to channels. The wire shape assumes single-actor interactive use. An external agent might want long-polling or server-sent events for ambient observation, programmatic message addressing (`@-mentioning` a specific persona), and an idempotency contract for retries — none of which the human chat path provides.

### M-4. Auth and accountability already exist

[RFC 0039](0039-user-accounts-authentication.md) gives Persatrix opaque revocable sessions and role gating. [RFC 0009](0009-security-sandboxing.md) gives audit logging and rate-limit middleware. An external-agent endpoint is mostly a matter of *binding* — defining a new principal type, issuing it credentials, and accepting its calls behind the same middleware that already protects the REST surface. The infrastructure is in place; what is missing is the typed surface and the principal.

## Goals

1. **A new participant subtype** — `ExternalAgentParticipant` — alongside `PersonaParticipant` and `UserParticipant` from [RFC 0016](0016-human-participant-chat-interface.md).
2. **A bounded HTTP/JSON endpoint** for channel send/receive, scoped to channel message I/O and nothing else.
3. **Auth reuses [RFC 0039](0039-user-accounts-authentication.md).** External-agent credentials are a new account role; sessions and revocation work the same way.
4. **Default-deny capability surface.** An external participant can post to and receive from channels it is explicitly invited to. No discovery, no broadcast, no implicit reach.
5. **Audit-equivalent to internal participants.** Every action by an external participant goes through the same audit log ([RFC 0009](0009-security-sandboxing.md)) as internal calls.
6. **Internal gRPC contract is unchanged.** [RFC 0040](0040-agent-orchestrator-transport-unification.md) ships as designed; this RFC adds an *additional* external surface, it does not modify the internal one.

## Non-Goals

- **Cross-org federation.** [RFC 0012](0012-protocols-organizations.md) Phase 5 owns inter-organization federation. This RFC is a single-instance inbound endpoint; the remote party authenticates as a principal known to *this* instance.
- **Discovery or registry of external agents.** The endpoint does not list, advertise, or broker external agents. Invitation is operator-driven.
- **Workflow submission, persona memory access, wallet operations.** None of these are exposed.
- **Streaming model tokens to external agents.** External agents receive completed channel messages, not in-progress token streams.
- **A general-purpose RPC surface.** The endpoint is shaped specifically for channel messaging. New capabilities require an RFC amendment.
- **Mutual-trust assumptions.** No assumption that the external agent is well-behaved; rate-limit, audit, and capability scope assume adversarial intent.

## Design / Implementation

### A. The `ExternalAgentParticipant` subtype

[RFC 0016](0016-human-participant-chat-interface.md) defines `ChannelParticipant` as a union of `PersonaParticipant` and `UserParticipant`. This RFC adds:

```python
@dataclass(frozen=True)
class ExternalAgentParticipant(ChannelParticipant):
    participant_id: str           # "ext:<account_id>"
    display_name: str             # user-set label
    account_id: str               # binds to RFC 0039 account
    invited_by: str               # operator account that invited it
    invited_at: datetime
    capability_scope: CapabilityScope
```

`CapabilityScope` is fixed at invitation time — which channels it can post to, which it can read from, whether it can be `@`-mentioned.

### B. Endpoint surface

A new REST surface under `/api/v1/external/agents/` ([RFC 0002](0002-rest-api-server.md) host), distinct from the internal gRPC surface ([RFC 0040](0040-agent-orchestrator-transport-unification.md)). All paths in the table below are shown relative to the `/api/v1` prefix:

| Method | Path (under `/api/v1`) | Purpose |
|--------|------------------------|---------|
| `POST` | `/external/agents/channels/{channel}/messages` | Post a message to a channel |
| `GET` | `/external/agents/channels/{channel}/messages?since={cursor}` | Long-poll for new messages |
| `GET` | `/external/agents/channels/{channel}/stream` | Server-sent events stream (optional, Phase 2) |
| `GET` | `/external/agents/channels` | List channels this participant is invited to (the explicit invitation set, not a directory) |
| `GET` | `/external/agents/identity` | Return the participant's own ID and capability scope |

That is the entire surface. There is no endpoint to create channels, invite other participants, mutate persona state, submit workflows, or call any tool. Operator actions (inviting an external agent, revoking it, changing capability scope) go through the existing CLI / admin REST path — *not* through this external endpoint.

### C. Auth and identity

- **Credentials.** [RFC 0039](0039-user-accounts-authentication.md) opaque session tokens, issued at invitation time. Tokens are bound to the `ExternalAgentParticipant.account_id` with a fixed `role=external_agent` claim.
- **Transport.** HTTPS only. Bearer token in `Authorization` header. No cookie-based session — external agents are not browsers.
- **Revocation.** [RFC 0039](0039-user-accounts-authentication.md) revocation works unchanged. An operator can revoke an external-agent session and all subsequent requests 401.
- **Idempotency.** `POST` requests carry a client-generated `Idempotency-Key` header; the orchestrator deduplicates within a 24-hour window.

### D. Capability scope

```python
@dataclass(frozen=True)
class CapabilityScope:
    channels_read: list[str]      # exact channel names; no wildcards
    channels_write: list[str]     # exact channel names
    mentionable: bool             # can other participants @-mention this one
    rate_limit: RateLimit         # per-second / per-minute / per-hour ceilings
```

`channels_read` ⊇ `channels_write` is enforced (cannot write to a channel you cannot read). Wildcards are deliberately not supported — every channel is named explicitly to keep the audit surface concrete.

### E. Message inbound / outbound flow

**Outbound (Persatrix → external agent):**

1. A channel message is published (by any participant).
2. The channel publish path ([RFC 0041](0041-typed-event-taxonomy-lifecycle-callbacks.md) subscriber) checks each external participant's `channels_read` scope.
3. Authorized external participants' message queues receive the event.
4. The external agent picks it up via long-poll or SSE.

**Inbound (external agent → Persatrix):**

1. `POST /external/agents/channels/{channel}/messages` with payload.
2. Auth middleware validates the bearer token and resolves the `ExternalAgentParticipant`.
3. Capability check: is `channel` in `channels_write`? Rate-limit check: is this request under the ceiling?
4. The message is published to the channel as if from this external participant. Internal subscribers (personas, channel store, audit log) see it via the standard channel publish path.
5. `ExternalAgentParticipant.participant_id` appears in the message envelope so other participants can see the source.

External agents cannot impersonate personas or users. The `from_participant_id` field is server-set, never client-supplied.

### F. Failure modes and abuse

| Failure / abuse | Defense |
|----------------|---------|
| Token leak | [RFC 0039](0039-user-accounts-authentication.md) revocation, short token lifetime (configurable, default 7 days), audit log surfaces unusual call patterns |
| Spam to a channel | Per-participant rate limit ([RFC 0009](0009-security-sandboxing.md) `RateLimiter`); over-rate calls 429 |
| Reading channels outside scope | Capability check at every read; rejected calls audited |
| Replaying a message | `Idempotency-Key` window |
| Long-poll hang storm | Long-poll has a hard server-side timeout (default 30s) and a configurable per-account concurrent-connection cap |
| Confidentiality bypass via long-poll on a confidential channel | [RFC 0037](0037-memory-confidentiality-channel-classification.md) classification is consulted before delivering events to external participants; classification can disallow external recipients entirely |

## Security Considerations

- **The endpoint is a new principal type.** Every existing authorization decision that branches on "is this a persona or a user?" must be reviewed for the third case. This is the largest review surface in the RFC and the reason it is sequenced last.
- **Default deny.** A new `ExternalAgentParticipant` with no invitations can do nothing. Read/write scope is explicit per channel.
- **Confidentiality interaction.** A channel classified as confidential to internal participants only ([RFC 0037](0037-memory-confidentiality-channel-classification.md)) must reject external-agent invitations at the operator step, not at the data-egress step. Belt-and-suspenders: both checks exist.
- **Audit log.** Every external-agent request is logged with `participant_id`, IP, user-agent, capability check result, rate-limit state. The audit log is the operator's primary tool for spotting abuse.
- **No implicit trust of message content.** Inbound messages from external agents pass through the same input sanitizer ([RFC 0009](0009-security-sandboxing.md)) as agent tool inputs. Treat as adversarial.
- **Internal contract isolation.** The external endpoint is hosted on the REST server, not the gRPC server. A bug in the external surface cannot expose the internal `OrchestratorService` ([RFC 0040](0040-agent-orchestrator-transport-unification.md)). Different ports, different process boundaries are an option to consider (§Open Questions).

## Migration Path

There is nothing to migrate. The endpoint is additive. Existing channels, participants, and integrations are unaffected. Operators opt in by inviting external agents.

## Phased Implementation Plan

### Phase 1 — participant subtype + endpoint skeleton

`ExternalAgentParticipant` data model, REST endpoints for identity / list-channels / post-message / long-poll, auth integration with [RFC 0039](0039-user-accounts-authentication.md), capability scope enforcement, rate limit, audit log. SSE deferred to Phase 2.

### Phase 2 — SSE stream + operator UX

Server-sent events stream for ambient observation. CLI commands for operators to invite, revoke, scope, and audit external agents.

### Phase 3 — extended capability vocabulary

If real use cases demand it: more granular capability scopes (per-message-type, per-classification). Gated by demand, not by speculative design.

## Files Touched (Estimated)

| Component | Files | Change |
|-----------|-------|--------|
| Go orchestrator | `internal/server/external_agents.go` (new), `internal/participants/` ([RFC 0016](0016-human-participant-chat-interface.md)) | New endpoint + participant subtype |
| Go orchestrator | `internal/auth/` ([RFC 0039](0039-user-accounts-authentication.md)) | New `role=external_agent` claim |
| Go orchestrator | `internal/security/rate_limit.go`, `internal/security/audit.go` ([RFC 0009](0009-security-sandboxing.md)) | Per-external-agent rate-limit buckets |
| Schemas | `schemas/external_agent.json` (new) | Wire payload schema |
| CLI | `cli/src/commands/external_agent.rs` (new) | Operator invite / revoke / list / scope commands |
| Config | `config/external_agents.yaml` (new, optional) | Static invitation roster for declarative ops |

## Test Strategy

- **Unit tests**: capability-scope enforcement (read/write/mentionable), idempotency, rate-limit accounting.
- **Integration tests**: invite → post → receive round-trip with a fixture external agent; revocation 401; over-scope read 403; over-rate 429; confidentiality-classified channel rejection at invitation.
- **E2E**: a smoke test where an external agent joins a channel with two personas, posts a message, receives the personas' replies via long-poll, and is then revoked mid-conversation.
- **Manual tests**: new MT for the external-agent flow, modeled on [MT-MEMORY-005](../manual-tests/MT-MEMORY-005-dementia-test.md).
- **Security review**: dedicated review pass before Phase 1 ships, focused on the principal-type expansion ([§Security Considerations](#security-considerations)).

## Open Questions

1. **Hosted on the REST server or a dedicated port?** Two options with genuinely different threat profiles:
   - *Same server, separate path prefix (`/external/*`).* Simpler ops, single TLS cert, single listener config. A reverse-proxy misconfiguration or a routing bug in the REST server can expose internal paths to external-agent traffic.
   - *Dedicated listener on a separate port.* Process-level isolation between the internal REST surface and the external interop surface — a bug in one cannot route into the other. Costs an additional listener, additional cert management, and a more complex deployment story.
   The lean depends on the threat-modeling pass scheduled for the dedicated security review ([§Test Strategy](#test-strategy)): if reverse-proxy misconfiguration is judged a realistic operator-error risk, the dedicated listener wins; if not, the path prefix is sufficient. Resolve as part of that review, not before.
2. **Per-message-type capability scopes.** Initial scope is read/write per channel. Should there be "can post system events" or "can request a turn" sub-scopes? Defer to Phase 3 unless a real use case appears.
3. **Outbound delivery guarantees.** At-least-once with idempotency on the consumer side, or at-most-once with no retries? Lean: at-least-once with an event_id the consumer dedupes on.
4. **Channel-message *typed* events to external agents.** Internal subscribers see `TurnEvent`s from [RFC 0041](0041-typed-event-taxonomy-lifecycle-callbacks.md). External agents see channel messages only — they do not see `StateDelta`, `ToolCall`, `Error(kind=internal)`, etc. Confirm the projection is correct and complete.
5. **External-agent appearance to personas.** When a persona sees a message from an `ExternalAgentParticipant`, does it know the message is from an external (non-persona) source? Probably yes — the participant subtype is visible in the message envelope and the prompt formatter can surface it. But this is a behavior choice, not a privacy requirement.
6. **Confidentiality default for new channels.** Today new channels are not classified. When external participants exist, should channel creation force an explicit classification choice? Coordinate with [RFC 0037](0037-memory-confidentiality-channel-classification.md).
7. **Channel rename and deletion vs. `CapabilityScope`.** `channels_read` / `channels_write` name channels exactly (no wildcards per [§D](#d-capability-scope)). When a channel is renamed or deleted, what happens to in-flight external-agent scopes? Three options: (a) rewrite the scope to follow the rename / drop the entry on delete, silently; (b) leave the scope unchanged and let the now-stale entry no-op (access lost on rename, harmless on delete); (c) refuse rename/delete while any external scope references the channel. Lean: (a) for delete (drop the stale entry, audit-log it) and (b) for rename (channel renames are operator actions; the operator re-grants if intended), but worth confirming during the security review since (a)-on-rename is the friendlier UX and (b) is the safer default.

## Decision / Next Steps

Draft. The dedicated security review (§Test Strategy) is the gate for Phase 1. Open questions 1, 4, 6, and 7 must be resolved before that review. This RFC is intentionally the last of the four umbrella RFCs to land — the other three frame what an external participant can and cannot do.

## Related Documentation

- [Agent Runtime Vocabulary — Discussion Notes](../agent-runtime-vocabulary-roadmap.md)
- [RFC 0011 — Channels & Bridges](0011-channels-bridges.md)
- [RFC 0016 — Human Participant & Chat Interface](0016-human-participant-chat-interface.md)
- [RFC 0039 — User Accounts & Authentication](0039-user-accounts-authentication.md)
- [RFC 0040 — Agent–Orchestrator Transport Unification](0040-agent-orchestrator-transport-unification.md)
- [RFC 0041 — Typed Event Taxonomy and Lifecycle Callbacks](0041-typed-event-taxonomy-lifecycle-callbacks.md)
- [RFC 0037 — Memory Confidentiality & Channel Classification](0037-memory-confidentiality-channel-classification.md)
- [RFC 0030 — Multi-Agent Conversation Governance](0030-multi-agent-conversation-governance.md)
- [RFC 0012 — Protocols & Organizations](0012-protocols-organizations.md)
- [RFC 0009 — Agent Identity, Security & Sandboxing](0009-security-sandboxing.md)
