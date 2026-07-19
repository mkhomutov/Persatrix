---
id: RFC-0043
title: Inbound Agent-Interop Endpoint
summary: Add a bounded inbound surface that lets a non-Persatrix agent join a channel — speak, be addressed, hear other participants — without speaking the orchestrator's internal gRPC contract, scoped narrowly enough that it cannot expose any orchestrator API the internal contract does not already authorize.
type: protocol
status: draft
author: Maksim Khomutov
created: 2026-05-20
target: v0.4.x
depends_on:
  - RFC-0009
  - RFC-0011
  - RFC-0012
  - RFC-0016
  - RFC-0030
  - RFC-0035
  - RFC-0039
---

# RFC 0043 — Inbound Agent-Interop Endpoint

**Type**: protocol
**Status**: 🔨 Draft
**Author**: Maksim Khomutov
**Date**: 2026-05-20
**Target**: v0.4.x (Phase 1a is unblocked today; Phase 1b/1c are gated — see [Decision / Next Steps](#decision--next-steps))
**Depends on**: RFC 0009 (Agent Identity, Security & Sandboxing — the capability-token credential model this RFC's external credential is an HTTP variant of, plus the `RateLimiter` and `AuditLogger` it reuses), RFC 0011 (Channels & Internal Agent Messaging — the channel primitive external agents join), RFC 0012 (Protocols & Organizations — its §G membership-time clearance check is the owner of the external-admission gate this RFC needs), RFC 0016 (Human Participant & Chat Interface — the `Participant` protocol this RFC extends with a third type value), RFC 0030 (Multi-Agent Conversation Governance — the floor-control / reply-budget machinery an external post interacts with), RFC 0035 (Channel Membership Interval Ledger — an external participant is a real membership row that opens a ledger stint), RFC 0039 (User Accounts & Authentication — the adjacent REST surface an external listener shares must be authenticated before same-listener hosting is sound)
**Relates to**: RFC 0002 (REST API Server — the host this endpoint mounts on), RFC 0029 (Personal/Society Storage Split — every store this RFC adds is society-scoped and must pick a tier), RFC 0037 (Memory Confidentiality & Channel Classification — an orthogonal confidentiality axis, *not* the owner of external admission), RFC 0040 (Agent–Orchestrator Transport Unification — the internal gRPC contract this RFC explicitly leaves untouched; a non-interaction, not a build dependency), RFC 0041 (Typed Event Taxonomy and Lifecycle Callbacks — the projected event subset an external agent sees), RFC 0052 (Autonomous Agent-Only Channels — an external post advances the idle clock this RFC must reconcile with)
**Spawned from**: [agent-runtime-vocabulary-roadmap.md §Seam 4](../agent-runtime-vocabulary-roadmap.md#seam-4--inbound-agent-interop-endpoint)

---

## Table of Contents

- [Summary](#summary)
- [Motivation](#motivation)
- [Goals](#goals)
- [Non-Goals](#non-goals)
- [Design / Implementation](#design--implementation)
  - [A. The `ExternalAgentParticipant` participant type](#a-the-externalagentparticipant-participant-type)
  - [B. Endpoint surface and wire contract](#b-endpoint-surface-and-wire-contract)
  - [C. Auth, identity, and credentials](#c-auth-identity-and-credentials)
  - [D. Capability scope and membership](#d-capability-scope-and-membership)
  - [E. Storage and provisioning](#e-storage-and-provisioning)
  - [F. Message inbound / outbound flow](#f-message-inbound--outbound-flow)
  - [G. Governance and delivery mechanics](#g-governance-and-delivery-mechanics)
  - [H. Failure modes and abuse](#h-failure-modes-and-abuse)
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

This RFC adds a third participant type — `ExternalAgentParticipant` — and a bounded HTTP/JSON inbound endpoint that lets a non-Persatrix agent join a channel: post messages, receive messages addressed to it, and observe channel traffic it is authorized to see. The internal gRPC contract is untouched. The endpoint is narrow on purpose: it can send and receive channel messages and nothing else. It is not a back-door for workflows, persona memory, wallet leases, or any other orchestrator API.

An external participant is a **real channel member** (a `memberships` row demoted to `RespondNever`, reusing the chat-as-DM façade precedent), not a parallel roster — so it inherits the membership, history-scoping, and audit machinery every other participant already has, and cannot bypass the invariants the channel store enforces.

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

### M-4. The reusable half of the substrate exists; the auth half is new work

Two claims that this RFC's original draft conflated. To keep them straight:

- **Reusable today.** [RFC 0009](0009-security-sandboxing.md) ships a wired `security.RateLimiter` ([`internal/security/ratelimit.go`](../../internal/security/ratelimit.go)) and `security.AuditLogger` ([`internal/security/audit.go`](../../internal/security/audit.go)) on the request path, and defines the `AgentCapabilityToken` credential *model* (HMAC-signed, short-TTL, capability-scoped bearer — [RFC 0009 §Phase 4](0009-security-sandboxing.md#phase-4-agent-identity-tokens--hitl-gates)). Those are genuine reuse.
- **Not in place.** There is **no authentication on the REST surface today** — [`internal/server/server.go`](../../internal/server/server.go) `registerRoutes` still reads `// TODO(security): no auth in v0.1`, the middleware chain (recovery → requestID → logging → rate-limit) contains no auth stage, and the orchestrator-side capability gate is a stub (`internal/security/security.go` — `// TODO: Implement PermissionGate`). [RFC 0039](0039-user-accounts-authentication.md) is 📋 Proposed with zero implementation and no PR plan; [RFC 0009 Phase 4](0009-security-sandboxing.md#phase-4-agent-identity-tokens--hitl-gates) is v0.4.0 and unimplemented.

So the honest framing is: rate-limiting and audit are reusable; the credential, its HTTP-side validation middleware, the principal type, and the capability gate are all **new work**, sequenced behind the credential-track decision in [§C](#c-auth-identity-and-credentials). This is why Phase 1 is split (see [Phased Implementation Plan](#phased-implementation-plan)).

## Goals

1. **A new participant type** — `ExternalAgentParticipant` — alongside the existing `agent` and `user` values of the [RFC 0016](0016-human-participant-chat-interface.md) `Participant` protocol.
2. **A bounded HTTP/JSON endpoint** for channel send/receive, scoped to channel message I/O and nothing else, with a fully specified wire contract ([§B](#b-endpoint-surface-and-wire-contract)).
3. **A capability-scoped bearer credential** modeled on [RFC 0009](0009-security-sandboxing.md)'s `AgentCapabilityToken`, delivered over HTTP, with operator revocation ([§C](#c-auth-identity-and-credentials)).
4. **Default-deny capability surface.** An external participant can post to and receive from channels it is explicitly invited to. No discovery, no broadcast, no implicit reach.
5. **Audit-equivalent to internal participants.** Every action by an external participant — including every outbound message delivered to it — goes through the audit log ([RFC 0009](0009-security-sandboxing.md)). This requires new audit event kinds and a read-path audit site that do not exist today ([§Security](#security-considerations)).
6. **Internal gRPC contract is unchanged.** [RFC 0040](0040-agent-orchestrator-transport-unification.md) ships as designed; this RFC adds an *additional* external surface, it does not modify the internal one.

## Non-Goals

- **Cross-org federation.** [RFC 0012](0012-protocols-organizations.md) Phase 5 owns inter-organization federation. This RFC is a single-instance inbound endpoint; the remote party authenticates as a principal known to *this* instance.
- **Discovery or registry of external agents.** The endpoint does not list, advertise, or broker external agents. Invitation is operator-driven. (The `GET /external/agents/channels` route returns only the *caller's own* explicit invitation set — its own membership — which is not discovery of anyone or anything else.)
- **Workflow submission, persona memory access, wallet operations.** None of these are exposed as API surface. Note that channel *content* an external agent posts still transitively reaches persona memory via the normal interaction→consolidation path; that transitive write is a threat the Security section addresses, not an exposed API.
- **Streaming model tokens to external agents.** External agents receive completed channel messages, not in-progress token streams.
- **A general-purpose RPC surface.** The endpoint is shaped specifically for channel messaging. New capabilities require an RFC amendment.
- **Mutual-trust assumptions.** No assumption that the external agent is well-behaved; rate-limit, audit, and capability scope assume adversarial intent.

## Design / Implementation

### A. The `ExternalAgentParticipant` participant type

**Ground truth first.** [RFC 0016](0016-human-participant-chat-interface.md) does *not* define a `ChannelParticipant` union of `PersonaParticipant` and `UserParticipant` — those identifiers exist nowhere in the tree. What it built is a **PEP-544 structural `Protocol`**, `Participant` ([`agents/participant.py:26`](../../agents/participant.py)), with three read-only properties (`participant_id`, `participant_type`, `display_name`). `UserParticipant` ([`agents/participant.py:58`](../../agents/participant.py)) is the *only* concrete dataclass; personas satisfy the protocol structurally via properties on `BaseAgent`, without inheritance ([RFC 0016 §"Using `Protocol` … means existing agents do not need to inherit from a new base class"](0016-human-participant-chat-interface.md)). So there is **no base class to subclass and no union to extend**. An external agent satisfies `Participant` structurally, exactly as a persona does.

The substantive change is therefore **not a new subclass** but a **third value in a closed participant-type vocabulary that is hand-mirrored across four sites**. Adding `external_agent` means editing all four, in lockstep, or the value is silently corrupted:

1. [`internal/channels/participant_type.go`](../../internal/channels/participant_type.go) — `validParticipantTypes = map[string]struct{}{"agent": {}, "user": {}}` (the Go anchor; REST/gRPC boundaries validate against it).
2. [`agents/participant.py`](../../agents/participant.py) — `VALID_PARTICIPANT_TYPES = frozenset({"agent", "user"})` (the Python mirror; pinned by `tests/unit/python/test_participant.py`, which will fail loudly until updated — a feature, not a bug).
3. [`agents/persona_runtime/record_close.py`](../../agents/persona_runtime/record_close.py) — the **silent clamp**: `extract_peer_from_interaction` maps any peer type not in `{"agent", "user"}` to `"agent"` with no error. Left unedited, every external agent's relationship row is recorded as a plain agent. **This is ISSUE-0068's exact defect class**, which already cost an [RFC 0011 amendment](0011-amendment-participant-type-wire-propagation.md) to fix once; it must be handled deliberately here (either add `external_agent` to the clamp's accepted set, or decide external agents do not form relationship rows and document why).
4. [`agents/memory/relationship_queries.py`](../../agents/memory/relationship_queries.py) `validate_participant_types`, called from [`relationship_mutations.py`](../../agents/memory/relationship_mutations.py).

**The wire cost is smaller than it looks.** `sender_participant_type` is a bare proto3 `string` field ([`proto/task.proto:224`](../../proto/task.proto)), *not* an enum — so **no proto change, no stub regen, no field-number negotiation**. The type rides in `ChannelMessage.Metadata["participant_type"]` ([`participant_type.go`](../../internal/channels/participant_type.go)) and is lifted to proto field 12 only at the gRPC dispatch boundary ([`grpc_dispatcher_proto.go`](../../internal/channels/grpc_dispatcher_proto.go)).

**The record.** The external-agent participant record and its capability grant are Go types (the enforcement all lives on the Go REST server; the Python side needs only the vocabulary value from site 2 above):

```go
// internal/extagents/participant.go (new)
type ExternalAgentParticipant struct {
    ParticipantID string    // "ext-<account_id>" — see the ID-grammar note below
    DisplayName   string    // operator-set label
    CredentialRef string    // opaque handle to the issued capability token (never the token itself)
    InvitedBy     string    // operator principal that invited it
    InvitedAt     time.Time
    // CapabilityScope is stored per-channel-grant (see §D/§E), not inline,
    // so it is mutable without rewriting the participant record.
}
```

**ID grammar (was a blocker).** The original `ext:<account_id>` form is **rejected at four independent validators** because `:` is reserved by the canonical-address grammar (`group:<name>`, `dm:<a>:<b>`, `thread:<msg-id>`): the channel-member ID pattern ([`channels.go` `ValidateParticipantID`](../../internal/channels/channels.go), `^[A-Za-z0-9][A-Za-z0-9_-]*$`), the request-middleware ID pattern ([`middleware.go`](../../internal/security/middleware.go), `^[a-z0-9][a-z0-9-]*[a-z0-9]$` — which 400s *before* the limiter is consulted), the Python participant-ID pattern ([`participant.py`](../../agents/participant.py), same shape, also rejecting uppercase), and the web mention regex ([`web/src/lib/mentions.js`](../../web/src/lib/mentions.js)). It is also illegal under [RFC 0039](0039-user-accounts-authentication.md)'s own `participant_id` constraint, and would import the RFC 0011 v0.5.0 escape-grammar question ([RFC 0011](0011-channels-bridges.md)).

The fix is to use **`ext-<account_id>`** (lowercase, hyphen): it passes all four patterns unchanged, needs zero validator edits, zero schema edits, and no grammar amendment. Wildcards and colons stay out of the identity by construction.

### B. Endpoint surface and wire contract

A new REST surface under `/api/v1/external/agents/` ([RFC 0002](0002-rest-api-server.md) host), distinct from the internal gRPC surface ([RFC 0040](0040-agent-orchestrator-transport-unification.md)). All paths in the table below are shown relative to the `/api/v1` prefix:

| Method | Path (under `/api/v1`) | Purpose |
|--------|------------------------|---------|
| `POST` | `/external/agents/channels/{channel}/messages` | Post a message to a channel |
| `GET` | `/external/agents/channels/{channel}/messages?since={cursor}&limit={n}` | Long-poll for new messages |
| `GET` | `/external/agents/channels/{channel}/history?before={cursor}&limit={n}` | Backfill within the caller's membership stint (see [§G](#g-governance-and-delivery-mechanics)) |
| `GET` | `/external/agents/channels/{channel}/stream` | Server-sent events stream (optional, Phase 2) |
| `GET` | `/external/agents/channels` | List channels this participant is invited to (the explicit invitation set, not a directory) |
| `GET` | `/external/agents/identity` | Return the participant's own ID and capability scope |

That is the entire surface. There is no endpoint to create channels, invite other participants, mutate persona state, submit workflows, or call any tool. Operator actions (inviting an external agent, revoking it, changing capability scope) go through a **new operator-authenticated REST route group + CLI** ([§E](#e-storage-and-provisioning)) — *not* through this external endpoint, and not through any pre-existing path (there is none today).

**Wire contract.** The endpoint reuses the server's established JSON envelope: the error shape is `errorResponse{ "error": string, "code": string }` via [`writeError`](../../internal/server/helpers.go), and message DTOs mirror [`channelMessageResponse`](../../internal/server/channel_types.go). All bodies are `application/json`.

**`POST .../messages`** — request:

```json
{ "content": "string (required, ≤ the channel content cap)",
  "mentions": ["persona-id", "..."]          // optional; resolved per §G
}
```

The sender is **never** client-supplied — see [§F](#f-message-inbound--outbound-flow). Response `201`:

```json
{ "message_id": "uuid", "channel": "group:planning", "event_id": 4213, "timestamp": "RFC3339" }
```

`event_id` is the monotonic per-channel sequence the consumer dedupes and cursors on (see [§G](#g-governance-and-delivery-mechanics)).

**`GET .../messages?since={cursor}`** — long-poll. Response `200`:

```json
{ "messages": [ { "message_id": "...", "event_id": 4213, "sender_participant_id": "...",
                  "sender_participant_type": "agent|user|external_agent",
                  "content": "...", "timestamp": "RFC3339", "mentions": ["..."] } ],
  "next_cursor": "opaque-string" }
```

An empty poll that reaches the server timeout returns `200` with `"messages": []` and the **unchanged** `next_cursor`. The cursor is opaque to the client and encodes the last-delivered `event_id`.

**Error taxonomy** (reusing the [`channel_errors.go`](../../internal/server/channel_errors.go) sentinel→code→status mapping where a sentinel already exists):

| Status | `code` | When |
|--------|--------|------|
| `400` | `invalid_request` | malformed body, bad cursor, missing content |
| `401` | `unauthenticated` | missing / invalid / expired / revoked credential |
| `403` | `capability_denied` | channel not in the caller's read (for GET) or write (for POST) scope |
| `404` | `channel_not_found` | channel does not exist |
| `409` | `idempotency_conflict` | same `Idempotency-Key`, different body ([§C](#c-auth-identity-and-credentials)) |
| `413` | `content_too_large` | body exceeds the channel content cap |
| `429` | `rate_limited` | over the per-participant ceiling ([§D](#d-capability-scope-and-membership)) |
| `503` | `poll_capacity` | over the per-account concurrent long-poll cap ([§H](#h-failure-modes-and-abuse)) |

The `schemas/external_agent.schema.json` file (note the `.schema.json` suffix, matching all six existing schemas) codifies these bodies and is registered in [`agents/validate.py`](../../agents/validate.py)'s `_SCHEMA_MAP`.

### C. Auth, identity, and credentials

**Credential track (was a blocker).** External-agent credentials are **not** an [RFC 0039](0039-user-accounts-authentication.md) account. RFC 0039 forecloses exactly this — its Non-Goals state that "agent self-registration … and peer endpoints follow the **RFC 0009 track, not this one**" ([RFC 0039 §Non-Goals](0039-user-accounts-authentication.md#non-goals)) — and its account model is human-shaped (a 1:1 binding to a `UserParticipant`, plus a `username` + Argon2id password an unattended machine principal does not have).

The credential is instead an **HTTP variant of [RFC 0009](0009-security-sandboxing.md)'s `AgentCapabilityToken`** ([RFC 0009 §Phase 4](0009-security-sandboxing.md#phase-4-agent-identity-tokens--hitl-gates)): HMAC-signed, short-TTL, carrying capability claims that must be a subset of the invitation grant. What differs from the RFC 0009 shape, and is new work:

- **Transport.** RFC 0009 tokens ride gRPC metadata at spawn time; this credential is delivered to the operator at invitation time and presented by the external agent as an HTTP `Authorization: Bearer <token>` header. **HTTPS only.** No cookie session — external agents are not browsers. (There is no TLS in the Go server today — [`server.go`](../../internal/server/server.go) calls `ListenAndServe()` with no `crypto/tls` — so "HTTPS only" presumes a terminating reverse proxy; this must be stated in the deployment guide and interacts with [OQ 1](#open-questions).)
- **Validation middleware.** A new HTTP bearer-validation middleware on the `/external/*` routes, modeled on RFC 0009's HMAC validation but on the REST side. It resolves the token to an `ExternalAgentParticipant` and puts the principal in the request context. This does not exist and is Phase 1b work.
- **Revocation.** RFC 0009's `AgentCapabilityToken` is deliberately **not revocable** — "valid until expiry," mitigated only by short TTL ([RFC 0009 §Security Considerations](0009-security-sandboxing.md#security-considerations), and its OQ 4 sketches a lightweight revocation list). Operator revocation is a hard requirement here (Goal 3), so this RFC adopts RFC 0009 OQ 4's denylist: revocation writes the credential's `jti` to a short-lived denylist checked by the validation middleware; the denylist entry's TTL equals the token TTL, after which the token has expired anyway. This bounds the denylist and makes "operator revokes → subsequent requests 401" real.
- **Token lifetime.** Short by default. The RFC 0039 session TTL is 24h; a machine credential with no interactive rotation should be *shorter*, not longer — the original draft's "default 7 days" is the wrong direction. Default **1h**, operator-configurable, with an operator re-issue action. In-flight long-poll / SSE connections are already-authorized streams, not "subsequent requests": revocation or expiry **MUST** terminate a live stream, so the delivery loop re-checks credential validity on each wakeup and at a bounded cadence (≤15s, matching the SSE heartbeat precedent in [`logs_stream_handler.go`](../../internal/server/logs_stream_handler.go)).

**Idempotency.** `POST` requests carry a client-generated `Idempotency-Key` header; the orchestrator deduplicates within a 24-hour window. There is no prior art ([RFC 0002](0002-rest-api-server.md) records its deliberate absence and anticipated the name `X-Idempotency-Key`; this RFC standardizes on `Idempotency-Key` and RFC 0002 should be reconciled to match). The ledger is a first-class store, not a hand-wave — see [§E](#e-storage-and-provisioning) for its schema, per-account key scoping (so one agent cannot pre-claim another's keys), key-length cap (mirroring the 256-byte `MaxAgentIDLen` cap that exists precisely to stop header-map poisoning), replay semantics (`200` replaying the original `message_id`), and same-key-different-body handling (`409 idempotency_conflict`).

**Adjacent-surface dependency.** The credential authenticates the `/external/*` routes. It does **not** protect the pre-existing `/api/v1/channels/*` routes, which are unauthenticated today and let a caller post as any `sender_id` — see [§Security](#security-considerations) and [OQ 1](#open-questions). This is why [RFC 0039](0039-user-accounts-authentication.md) Phase 2 (REST enforcement covering the channel surface) is a dependency: without it, the scoping this endpoint enforces is bypassable one route over.

### D. Capability scope and membership

**Membership (was a blocker).** An invited external participant **is a real channel member** — a `memberships` row — not a parallel roster. This is forced by ground truth: `PublishMessage` runs a membership probe *inside the write transaction* and returns `ErrNotMember` on a miss ([`sqlite_messages.go`](../../internal/channels/sqlite_messages.go)), so a scope list alone cannot publish; and a participant with no membership row opens no [RFC 0035](0035-channel-membership-interval-ledger.md) `membership_intervals` stint, so `GetHistoryScoped`, `GetMembershipIntervals`, and every recall/scoped-history query silently exclude it — which would make the audit story hollow.

The precedent is exact: the **chat-as-DM façade** already adds a non-registry human as a real member and demotes it to `RespondNever` so fanout skips dispatch ([`store.go`](../../internal/channels/store.go) `AddMember` / `SetMemberPolicy`, ISSUE-0034), and the dispatcher already tolerates a member with no registry entry. So:

- **Invitation** writes a `memberships` row with policy `RespondNever` (which auto-opens an RFC 0035 interval stint via the `AddMember` hook). The external participant can be addressed and can read, but never enters the floor responder set ([§G](#g-governance-and-delivery-mechanics)).
- **Revocation** closes the interval stint (the RFC 0035 `RemoveMember` path), so history-scoping naturally excludes post-revocation traffic.
- **`CapabilityScope` is a refinement on top of membership**, not a second source of truth for "who is on this channel." It carries read/write mode and the rate ceiling:

```go
// internal/extagents/scope.go (new)
type CapabilityScope struct {
    ChannelsRead  []string   // canonical channel IDs (not names — see below); no wildcards
    ChannelsWrite []string   // canonical channel IDs; must be ⊆ ChannelsRead
    Mentionable   bool       // may other participants @-mention this one — see §G
    RateLimit     RateLimit  // per-participant ceiling; see the rate-limit note below
}
```

`ChannelsWrite ⊆ ChannelsRead` is enforced (cannot write to a channel you cannot read).

**Keyed by canonical channel ID, not name (was a minor blocker).** Every store and enforcement path keys on the canonical channel ID; DM and thread channels have NULL/empty names ([`channels.go`](../../internal/channels/channels.go)), so a name-keyed scope structurally cannot reference them. Scope entries therefore name canonical IDs (`group:planning`, `dm:a:b`), not bare names. (See [OQ 7](#open-questions) for the delete-then-recreate hazard this leaves, which ID-keying does *not* fully close because `group:<name>` IDs regenerate byte-identically.)

**Mutable, not frozen (was an internal contradiction).** The original draft said `CapabilityScope` is "fixed at invitation time" *and* that operators can change scope. It is **mutable**: §B and Phase 2 both promise a scope-change operator action, and revoke-and-reinvite would needlessly churn credentials. Scope changes are audited, carry a revision, and narrowing while a long-poll is in flight takes effect on the next delivery wakeup.

**Rate limit is new work, not reuse.** The shipped `security.RateLimiter` ([`ratelimit.go`](../../internal/security/ratelimit.go)) has a *single* window (`CallsPerWindow` over `WindowSeconds`), one process-global instance built from env vars, and is keyed by a **client-supplied, spoofable** `X-Agent-ID` header (the repo says so: "self-reported and trivially spoofable"). The per-participant per-second/minute/hour ceilings this RFC's `RateLimit` implies are a **new subsystem** (a tiered limiter, or a multi-window ring), keyed **server-side from the validated token — never from `X-Agent-ID`** — with its own per-participant LRU pool so an external flood cannot evict internal agents' rate state. This is called out in [Files Touched](#files-touched-estimated) and gets its own PR.

### E. Storage and provisioning

The original draft named a dataclass and a scope struct but never said where either is persisted. Three new **society-scoped** durable stores are introduced (society-scoped by [RFC 0029](0029-personal-society-storage-split.md)'s own test — they are channel/instance state, not per-persona state — so each must pick a tier per RFC 0029 Goal 8):

| Store | Contents | Recommended home |
|-------|----------|------------------|
| External-agent registry | `ExternalAgentParticipant` records + credential refs (never the token) | `channels.db` at the **next schema version at PR time** (one additive migration; [`sqlite_schema.go`](../../internal/channels/sqlite_schema.go) `channelStoreSchemaVersion` is 10 today, and [RFC 0037](0037-memory-confidentiality-channel-classification.md)'s v0.3.12 classification migration is projected to take v11 first — this RFC lands v0.4.x, so re-verify the number when the PR opens) **or** a dedicated `external_agents.db` |
| Capability grants | one row per `(participant_id, channel_id, mode)` | same DB as the registry |
| Idempotency ledger | `(account_id, key, request_hash, message_id, created_at)`, `UNIQUE(account_id, key)` | same DB; 24h sweep |

The per-participant **delivery queue** ([§G](#g-governance-and-delivery-mechanics)) is a separate, bounded, in-memory structure with the cursor as the authoritative source of truth — it is a wakeup signal, not a store — so it does not appear in this table.

**RFC 0029 interaction.** Anything landed in `channels.db` becomes declared input to RFC 0029 Phase 3's `persatrix memory migrate` (which reads `channels.db`); anything landed elsewhere is a fourth storage idiom RFC 0029 exists to prevent. The tier decision (which DB, which migration number) is [OQ 4-new](#open-questions) and gates the store PRs.

**Provisioning.** Operators invite / revoke / re-scope via a new operator-authenticated route group (`POST/DELETE /api/v1/admin/external-agents…`) and the CLI (`cli/src/commands/external_agent.rs`, new). A `config/external_agents.yaml` roster is **optional and reconciled at boot** against the mutable store (the [`config/channels.yaml`](../../config/channels.yaml) + reconcile pattern, which [RFC 0050](0050-extensible-channel-configuration.md) established as the single-source-of-truth idiom) — the store is authoritative; the YAML is a seed, not a competing grant surface.

### F. Message inbound / outbound flow

**Inbound (external agent → Persatrix):**

1. `POST /external/agents/channels/{channel}/messages` with payload.
2. The bearer-validation middleware ([§C](#c-auth-identity-and-credentials)) validates the token and resolves the `ExternalAgentParticipant`; the principal lands in the request context.
3. Capability check: is `{channel}` (resolved to its canonical ID) in `ChannelsWrite`? Rate-limit check ([§D](#d-capability-scope-and-membership)). Idempotency check ([§C](#c-auth-identity-and-credentials)).
4. The message is published through the **same shared publish core** every participant uses — `publishCommit` → `fanout` ([`router_publish_async.go`](../../internal/channels/router_publish_async.go), [`fanout.go`](../../internal/channels/fanout.go)). Because the participant is a real member ([§D](#d-capability-scope-and-membership)), the in-transaction membership probe passes; interaction stamping, reply-budget reservation, and end-vote accounting all run exactly as for an internal post ([§G](#g-governance-and-delivery-mechanics)).
5. The sender identity is **server-set from the resolved principal, never client-supplied**: the real field is `ChannelMessage.SenderID` ([`channels.go`](../../internal/channels/channels.go)), with the type carried in `Metadata["participant_type"] = "external_agent"`. (Note: the existing publish path only checks that `sender_id` is non-empty and stamps it verbatim — server-set sender identity is genuinely *new* on this path, not inherited.)

External agents cannot impersonate personas or users **on this endpoint**. (They can on the *adjacent* unauthenticated `/api/v1/channels/*` surface until [RFC 0039](0039-user-accounts-authentication.md) Phase 2 closes it — see [§Security](#security-considerations).)

**Outbound (Persatrix → external agent):**

1. A channel message is committed (by any participant) via `publishCommit`.
2. `fanout` resolves recipients from channel **membership** ([`fanout.go`](../../internal/channels/fanout.go) `store.GetMembers`) and dispatches each through a `MessageDispatcher`. Today the router holds a **single** `dispatcher` field ([`router.go`](../../internal/channels/router.go)); there is no subscriber registry and no event bus. So the outbound path is **new machinery**, not an existing seam:
   - The router's single `dispatcher` becomes a **multiplexer** (a composite `MessageDispatcher`) fronting the existing `GRPCMessageDispatcher` plus a new `queueDispatcher`.
   - `queueDispatcher` appends the projected message to the bounded per-participant delivery queue for any external member whose `ChannelsRead` covers the channel.
3. The external agent drains its queue via long-poll (`GET .../messages?since=`) or SSE (Phase 2), reading through the authoritative cursor.

The original draft attributed this outbound hook to an "[RFC 0041](0041-typed-event-taxonomy-lifecycle-callbacks.md) subscriber." That is the wrong dependency: RFC 0041's channel-publish subscriber is **Python-side** and its Go-orchestrator subscriber is a later, RFC-0040-gated phase — neither lands on the `internal/channels` fanout path this RFC extends. RFC 0041 is therefore *not* a build dependency of §F; it is relevant only to the projected event subset ([OQ 4](#open-questions)).

### G. Governance and delivery mechanics

**Governance (was an unaddressed gap).** The channel subsystem shipped [RFC 0030](0030-multi-agent-conversation-governance.md) governance, [RFC 0035](0035-channel-membership-interval-ledger.md), and [RFC 0052](0052-autonomous-agent-channels.md) autonomous channels since this RFC's first draft. An external post is not inert with respect to them:

- **Floor control.** With floor control on and ≥2 responders, an external post triggers a serialized persona floor round ([`fanout.go`](../../internal/channels/fanout.go), [`floor_control.go`](../../internal/channels/floor_control.go)) — i.e. one external message can drive multi-persona LLM spend, which a message-*count* rate limit does not bound (see the cost-abuse note in [§Security](#security-considerations)). Critically, because the external participant is `RespondNever` ([§D](#d-capability-scope-and-membership)) it **never enters the responder set**, so it never burns a floor turn on an undeliverable dispatch (the failure mode that a `RespondAlways` external member would hit: `runFloorTurn` records the dispatch failure but does not short-circuit, blocking on the full `DefaultFloorTurnTimeoutSeconds = 45` timer). The sender is also structurally excluded from its own round ("never reply to self"), so publishing needs no floor grant.
- **Reply budget / end-vote (RFC 0030 Layers 2 & 4).** `publishCommit` reserves per-participant reply budget and processes end-vote accounting. An external participant is not in the governance-exemption vocabulary (`config/channels.yaml` `exempt_principals: [human]`), so its budget/exemption identity must be decided — [OQ 5-new](#open-questions). Recommended: treat external participants as governance-*subject* for budget accounting but, being `RespondNever`, they consume no *reply* budget.
- **Autonomous channels (RFC 0052).** An external post advances the idle clock (it settles an interaction), so steady external traffic suppresses idle rotation and can starve a convener (`ConveneChannel` refuses with `ErrChannelAlreadyConvening` while an interaction is open). This is a [§H](#h-failure-modes-and-abuse) row, not a blocker, but must be acknowledged.

**Delivery queue and cursor.** The delivery mechanics the original draft named in one sentence ("message queues receive the event") are specified here, mirroring the shipped [`logbuffer`](../../internal/observability/logbuffer/buffer.go) Config precedent (which pins exactly these knobs):

- **Queue.** Bounded per-participant, in-memory, **drop-oldest with a counter — never block**. This is load-bearing: `fanout` runs on a detached goroutine off `PublishAsync`, so a blocking write to a slow external consumer would stall fanout for *every other member of that channel*. Capacity, drop counter, and restart behaviour follow the logbuffer defaults shape (`PerExecution`/`MaxSubscribers`/drop-counter analogues).
- **Cursor.** Opaque to the client; encodes a **monotonic per-channel `event_id`**. The substrate is a monotonic per-channel sequence column (added with the [§E](#e-storage-and-provisioning) migration) — *not* a timestamp (the store orders `ORDER BY timestamp DESC` with no tiebreaker and ms resolution can repeat, which is why the CLI watch loop dedupes by id) and *not* the current UUIDv4 `message_id` (random, unsortable). The cursor is authoritative; the queue is only a wakeup. This makes at-least-once delivery fall out for free and folds [OQ 3](#open-questions) in: retention stops mattering because a reconnecting agent reads forward from its cursor through the scoped history path.
- **History on join.** An external agent sees history **from its invitation forward, by default** (confidentiality-defensible: pre-invitation traffic was written when no external principal was in the room). This is exactly what [RFC 0035](0035-channel-membership-interval-ledger.md)'s `GetHistoryScoped` already implements — it trims results to the participant's `membership_intervals` stints — so it is reused, not built. An operator-set backfill window at invitation is a possible refinement, deferred.
- **Mentionability.** `Mentionable` today has no mechanism: mention-lifting resolves a token only against channel members and pulls display names from the **agent registry** ([`mention_lift.go`](../../internal/channels/mention_lift.go)), which an external agent has no row in. For Phase 1, **addressability follows from membership** (the external participant is a member, so it can be named), and `Mentionable: false` is honored by a filter at the lift site. The display-name source for an external agent is its `ExternalAgentParticipant.DisplayName`, surfaced via a small lift-path adapter (not the registry). If this proves fiddly, dropping `Mentionable` from Phase 1 and relying on plain membership addressing is the fallback.

### H. Failure modes and abuse

| Failure / abuse | Defense |
|----------------|---------|
| Token leak | [RFC 0009](0009-security-sandboxing.md)-style short TTL (default 1h) + the operator denylist ([§C](#c-auth-identity-and-credentials)); audit log surfaces unusual call patterns |
| Spam to a channel | Per-participant rate limit ([§D](#d-capability-scope-and-membership), new tiered limiter keyed server-side); over-rate calls `429` |
| Reading channels outside scope | Capability check at every read against canonical channel IDs; rejected calls audited |
| Replaying a message | `Idempotency-Key` window ([§C](#c-auth-identity-and-credentials)); duplicate → `200` with original `message_id`, same-key-different-body → `409` |
| Long-poll hang storm | Hard server-side timeout (default 30s) + per-account concurrent-connection cap; over-cap → `503 poll_capacity` on the *new* connection, existing connections intact (the [`logbuffer` `ErrSubscriberCapExceeded`](../../internal/observability/logbuffer/subscribe.go) precedent) |
| Slow-consumer backpressure | Per-participant queue is drop-oldest + counter, never blocks fanout ([§G](#g-governance-and-delivery-mechanics)) |
| External post drives LLM spend via floor rounds | See the cost-abuse note in [§Security](#security-considerations); message-count rate limit is *not* a spend limit |
| External post suppresses autonomous idle rotation / starves convener (RFC 0052) | Acknowledged; idle-clock interaction documented; mitigation deferred (external traffic is operator-invited, so this is a trust-boundary the operator controls) |
| Confidentiality of the channel vs. external recipient | Owned by the RFC 0012 external-admission gate ([§Security](#security-considerations)) — admission is decided at the operator/membership step, fail-closed |

## Security Considerations

- **The endpoint is a new principal type.** Every existing authorization decision that branches on "is this a persona or a user?" must be reviewed for the third case. This is the largest review surface in the RFC and the reason a dedicated security review gates Phase 1b/1c.
- **Default deny.** A new `ExternalAgentParticipant` with no invitations can do nothing. Read/write scope is explicit per channel, keyed by canonical ID.
- **The adjacent unauthenticated surface defeats scoping until RFC 0039 Phase 2.** `POST /api/v1/channels/{id}/messages` takes `sender_id` from the request body with only a non-empty check and **no auth on the route** ([`server.go`](../../internal/server/server.go) `// TODO(security): no auth`), and `GET /api/v1/channels` / `GET /api/v1/channels/{id}/messages` are likewise open (the scoped-history handler's own comment says the read "is not an access boundary"). So `ChannelsRead`/`ChannelsWrite` constrain nothing an attacker must respect unless the channel surface is authenticated first. This is not contingent on a reverse-proxy misconfiguration — it is unauthenticated *by design* today. Same-listener hosting is unsound until [RFC 0039](0039-user-accounts-authentication.md) Phase 2 covers `/api/v1/channels/*`, **or** the external endpoint is a dedicated listener that does not route into the internal mux ([OQ 1](#open-questions)).
- **External-admission is owned by RFC 0012, not RFC 0037.** [RFC 0037](0037-memory-confidentiality-channel-classification.md)'s classification is a *total order* over sensitivity (`public < internal < restricted < secret`); "external participants allowed / not allowed" is an **orthogonal categorical** decision a total order cannot express, and RFC 0037 explicitly disclaims both the enforced egress gate (→ RFC 0012) and membership-time admission ("it does not change who is configured into a channel"). Admission is therefore a **flows-in** decision, which [RFC 0012 §G](0012-protocols-organizations.md#g-persona-clearance-and-authority-from-role) already owns as its membership-time clearance check. This RFC defines the *property* — an `external_participants_allowed` gate on the channel, default **false** (fail-closed), enforced at the membership write path — and assigns its enforcement to the RFC 0012 §G gate. The sequencing cost is real: RFC 0012 Phases 1–3 target v0.4.0, so the enforced admission gate gates the security review ([OQ 6](#open-questions), [OQ 2-new](#open-questions)). Until then there is **no external-admission control**, which is why Phase 1a's provisioning is operator-only and no external credential is issued before the gate exists.
- **Memory poisoning — a durable write primitive.** A channel message becomes an interaction turn and, on close, `summarize_closed_interaction` feeds turn payloads through the summariser; the single-turn shortcut is explicitly bypassed for message-bearing turns so "an RFC 0026 fact stated in a one-turn interaction still reaches the facts tier." So **one external message can write a declarative fact** into persona long-term memory, and **revoking the token unwrites none of it** — revocation is not remediation. Policy (choose in the security review): provenance-tag external-derived memory and exclude it from fact extraction in Phase 1, plus an operator "forget everything from this external agent" path bound to revocation. This corrects the Non-Goal's "persona memory … not exposed," which is true of the API but false transitively.
- **Data egress.** An authorized external agent can drain full scoped history for every channel in `ChannelsRead`; channel content is not self-contained (persona replies are assembled from cross-scope recall). Required: a per-participant egress budget *distinct from request rate*, egress-volume metrics, and — load-bearing for "the audit log is the operator's primary abuse tool" — **auditing of the message ids delivered** on the outbound path. Default: external participants read only from their `membership_intervals` stint (already implemented by `GetHistoryScoped`).
- **Cost abuse.** An external post can trigger persona LLM calls (floor rounds, [§G](#g-governance-and-delivery-mechanics)). That is a money-spending primitive handed to an external party; a message-count rate limit does not bound spend. The security review must decide whether external-triggered turns draw on a bounded budget (coordinate with [RFC 0023](0023-llm-call-leasing.md) leasing / [RFC 0050](0050-extensible-channel-configuration.md) interaction budget).
- **Audit log requires new event kinds and fields.** `AuditEvent` is a fixed 9-field struct ([`audit_event.go`](../../internal/security/audit_event.go)) with a closed, CI-pinned `AuditEventType` enum; none of `IP` / `User-Agent` / capability-check-result / rate-limit-state exist, and no client IP is captured anywhere in the orchestrator today. Adding typed fields changes the chain-hash canonical form; the alternative (stuffing into `Detail`) is untyped and unqueryable. New `external_agent.read` / `.publish` / `.denied` event kinds need the three-site enum edit or the severity-classification test fails. Client-IP capture and a trusted-proxy policy must be designed. All of this is **new work**, not inherited — see [Goal 5](#goals).
- **No implicit trust of message content.** The Go `InputSanitizer` exists ([`internal/security/sanitize.go`](../../internal/security/sanitize.go)) but has zero production call sites; the sanitizer that processes tool inputs is Python-side, inside the agent process, which an inbound REST endpoint never reaches. Reusing the Go type is a **new call site**, and adding a sixth `ContextSource` provenance value is a cross-language codegen change. And a sanitizer does **not** stop prompt injection: an external agent posting adversarial text that a persona reads and acts on is the single largest threat this endpoint introduces, and is bounded by the admission gate (who is let in), the audit trail (what they said), and the memory-poisoning policy (what persists), not by sanitization.
- **Internal contract isolation.** The external endpoint is hosted on the REST server, not the gRPC server. A bug in the external surface cannot expose the internal `OrchestratorService` ([RFC 0040](0040-agent-orchestrator-transport-unification.md)). Whether the external surface shares the REST listener or gets its own is [OQ 1](#open-questions), and interacts with RFC 0040's framing of REST as a "dedicated client edge."

## Migration Path

The endpoint is additive to *existing* channels, participants, and integrations — none are disrupted. But "nothing to migrate" was wrong: there are three real migrations plus one deliberately-free part.

- **Participant-type vocabulary change** across the four sites in [§A](#a-the-externalagentparticipant-participant-type), with the cross-language conformance tests (`internal/channels/participant_type_test.go`, `internal/server/chat_handler_participant_type_test.go`, `tests/unit/python/test_participant.py` — which pins the 2-value frozenset and *will fail until updated*) and the `record_close.py` clamp handled deliberately.
- **New stores** for the external-agent registry + capability grants ([§E](#e-storage-and-provisioning)), one additive `channels.db` migration at the next version at PR time (or a dedicated DB).
- **New store** for the 24h idempotency ledger.
- **Free:** no proto change — `sender_participant_type` is a bare string, not an enum ([§A](#a-the-externalagentparticipant-participant-type)).

Operators opt in by inviting external agents; no existing deployment changes behaviour until they do.

## Phased Implementation Plan

Split to separate what is buildable now from what waits on unshipped dependencies (the [RFC 0042](0042-state-namespacing-by-scope.md) 1a/1b precedent). The companion [PR plan](0043-pr-plan.md) enumerates the PRs.

### Phase 1a — unblocked today (no unshipped-RFC dependency)

Participant-type vocabulary + `ext-` ID grammar; the `ExternalAgentParticipant` / `CapabilityScope` model keyed by canonical channel ID; the external-agent registry + capability-grant store + one `channels.db` migration; membership reconciliation (invitation writes a `RespondNever` `memberships` row, opening an RFC 0035 stint); the idempotency store; the config loader + `schemas/external_agent.schema.json` + `_SCHEMA_MAP` registration. All additive, all testable in isolation.

### Phase 1b — gated on the credential track + RFC 0039 Phases 1–2

Principal resolution and the HTTP bearer-validation middleware (the [RFC 0009](0009-security-sandboxing.md) Phase 4 token model, HTTP variant, + denylist revocation); the per-participant tiered rate limiter (new subsystem); `POST .../messages` + capability check + audit emission; `GET /identity` + `GET /channels`. Gated because the credential and its validation are new, and the adjacent channel surface must be authenticated first ([§Security](#security-considerations)).

### Phase 1c — outbound; additionally gated on OQ 1 + the RFC 0012 admission gate

Dispatcher multiplexer + bounded per-participant queue + `queueDispatcher`; long-poll drain + the `event_id` cursor contract; the new `AuditEventType` kinds + client-IP capture + trusted-proxy config; the `external_participants_allowed` admission enforcement (RFC 0012 §G).

### Phase 2 — SSE stream + operator UX

Server-sent events stream (shares the Phase-1c queue). CLI commands for operators to invite, revoke, scope, and audit external agents.

### Phase 3 — extended capability vocabulary

If real use cases demand it: more granular capability scopes (per-message-type, per-classification). Gated by demand, not by speculative design.

## Files Touched (Estimated)

| Component | Files | Change |
|-----------|-------|--------|
| Go — participant vocab | [`internal/channels/participant_type.go`](../../internal/channels/participant_type.go), [`agents/participant.py`](../../agents/participant.py), [`agents/persona_runtime/record_close.py`](../../agents/persona_runtime/record_close.py), [`agents/memory/relationship_queries.py`](../../agents/memory/relationship_queries.py) | Add `external_agent` to the four allowlists; handle the clamp. **No proto change.** |
| Go — new package | `internal/extagents/` (new) | `ExternalAgentParticipant`, `CapabilityScope`, registry/grant/idempotency stores |
| Go — REST | `internal/server/external_agent_routes.go` (new), [`internal/server/server.go`](../../internal/server/server.go) (mount `/external/*` + operator admin routes), bearer-validation middleware | Endpoint + auth middleware |
| Go — channels | [`internal/channels/router.go`](../../internal/channels/router.go) (dispatcher multiplexer), [`internal/channels/fanout.go`](../../internal/channels/fanout.go), `internal/channels/queue_dispatcher.go` (new), [`internal/channels/sqlite_schema.go`](../../internal/channels/sqlite_schema.go) (next version — [§E](#e-storage-and-provisioning)) | Outbound queue + membership + migration |
| Go — auth/rate/audit | credential (modeled on `internal/security/token.go` from [RFC 0009](0009-security-sandboxing.md) Phase 4), [`internal/security/ratelimit.go`](../../internal/security/ratelimit.go) (tiered per-participant), [`internal/security/audit_event.go`](../../internal/security/audit_event.go) (new event kinds + IP/UA) | New credential + tiered limiter + audit kinds |
| Observability | [`internal/observability/metrics/`](../../internal/observability/metrics/) | Counters: requests by outcome, capability denials, rate-limit rejections, long-poll connections/cap-rejections, queue depth/drops, egress volume |
| Schemas | `schemas/external_agent.schema.json` (new), [`agents/validate.py`](../../agents/validate.py) `_SCHEMA_MAP` | Wire payload schema + registration |
| CLI | `cli/src/commands/external_agent.rs` (new), [`cli/src/commands/mod.rs`](../../cli/src/commands/mod.rs) | Operator invite / revoke / list / scope commands |
| Web | [`web/src/lib/mentions.js`](../../web/src/lib/mentions.js), `web/src/panels/ChannelMembers.svelte` | Recognize `external_agent` in mention/roster surfaces |
| Config | `config/external_agents.yaml` (new, optional) | Reconciled seed roster (not authoritative) |
| Tests | the four conformance tests above, `internal/extagents/*_test.go`, integration + E2E, `docs/manual-tests/MT-EXTAGENT-001.md` | See Test Strategy |

## Test Strategy

- **Unit tests**: participant-vocabulary conformance across Go/Python (extend the existing pinned tests); capability-scope enforcement (read/write/mentionable) keyed by canonical ID; idempotency (replay → original id, same-key-different-body → 409, per-account scoping, key-length cap); tiered rate-limit accounting keyed server-side; cursor monotonicity and drop-oldest queue behaviour.
- **Integration tests**: invite → membership row + RFC 0035 stint opens → post → receive round-trip with a fixture external agent; revocation → 401 + denylist + live-stream termination; over-scope read → 403; over-rate → 429; over-poll-cap → 503 with existing connections intact. (The confidentiality-classified-channel-rejection test requires the RFC 0012 admission gate and is Phase 1c, not Phase 1a.)
- **E2E**: an external agent joins a channel with two personas, posts a message, receives the personas' replies via long-poll, and is revoked mid-conversation (verifying the live-stream termination).
- **Manual tests**: **MT-EXTAGENT-001** (protocol surface — invite → post → long-poll receive → revoke → 401, modeled on [`MT-CHAT-001`](../manual-tests/MT-CHAT-001.md) for REST framing and [`MT-CHANNEL-006`](../manual-tests/MT-CHANNEL-006.md) for lifecycle/status-code assertions) in Phase 1; **MT-EXTAGENT-002** (operator CLI, modeled on [`MT-CHANNEL-001`](../manual-tests/MT-CHANNEL-001.md)) in Phase 2. *Not* modeled on MT-MEMORY-005 — that is the qualitative "dementia test," an LLM-judgement memory-quality gate, wrong for deterministic protocol assertions.
- **Security review**: dedicated review pass before Phase 1b/1c ships, focused on the principal-type expansion, the adjacent-surface interaction, memory poisoning, egress, and cost abuse ([§Security](#security-considerations)).

## Open Questions

1. **Hosted on the REST server or a dedicated port?** *(Still open; the original framing was wrong.)* No reverse-proxy misconfiguration is required for the risk — `/api/v1/channels/*` is unauthenticated *by design* today ([§Security](#security-considerations)), so same-listener hosting hands an external principal the full channel surface. Restated precondition: **same-listener is sound only if [RFC 0039](0039-user-accounts-authentication.md) Phase 2 enforcement covers `/api/v1/channels/*` first**; otherwise use a dedicated listener that does not route into the internal mux. Also note there is no TLS in the Go tree ([`server.go`](../../internal/server/server.go) `ListenAndServe()`), so "HTTPS only" assumes a terminating proxy that must be stated. **Owner**: this RFC + the security review. **Gates**: the server bootstrap shape → blocks the first outbound handler PR.
2. **Per-message-type capability scopes.** Defer to Phase 3 unless a real use case appears. *(Correctly deferred, non-blocking.)*
3. **Outbound delivery guarantees.** **Resolved**: at-least-once, folded into the cursor design ([§G](#g-governance-and-delivery-mechanics)) — the queue is a wakeup and the monotonic `event_id` cursor is authoritative, so the consumer dedupes on `event_id` and retention stops mattering.
4. **Channel-message *typed* events to external agents.** **Answerable from ground truth**: [RFC 0041](0041-typed-event-taxonomy-lifecycle-callbacks.md) defines 7 event subtypes; only assistant channel-content (`ModelOutput`) is publishable to a channel, and RFC 0041 already specifies channel-publish subscribes to `ModelOutput` + `Error` and ignores `CallbackModelOutput`. The external projection is a strict subset of that: **`ModelOutput` only, plus externally-meaningful `Error` kinds** (excluding `Error(kind=internal)`, `StateDelta`, `ToolCall`). No new design work; downgraded from a gate.
5. **External-agent appearance to personas.** **Answer: yes** — once `external_agent` is a real participant-type value ([§A](#a-the-externalagentparticipant-participant-type)) it rides `Metadata["participant_type"]` → proto field 12 → the prompt formatter, exactly as `user` does. Extend the same visibility to **operator surfaces**: an external agent must not render as a bare id indistinguishable from an internal agent in the web console roster and CLI.
6. **Confidentiality / external-admission default for new channels.** There is no classification concept at all today (no column, config key, or wire field). The external-admission property `external_participants_allowed` defaults **false** and is enforced by the [RFC 0012 §G](0012-protocols-organizations.md#g-persona-clearance-and-authority-from-role) membership-time gate ([§Security](#security-considerations)). Open sub-question: when RFC 0037 ships its `internal` default, confirm the two axes stay independent (a channel can be `internal`-classified yet still forbid externals, and vice versa).

### New open questions surfaced by the readiness review

- **NEW-1 — Credential shape confirmation.** The credential track is decided (RFC 0009 Phase 4 model, HTTP variant, denylist revocation — [§C](#c-auth-identity-and-credentials)). Confirm at the security review that the HTTP HMAC variant + denylist is preferred over waiting for a full RFC 0039 machine-principal amendment.
- **NEW-2 — Admission gate sequencing.** `external_participants_allowed` is owned by RFC 0012 §G (v0.4.0). Confirm no external credential is issued before that gate exists, and that Phase 1a provisioning stays operator-only until then. **Blocks the security review.**
- **NEW-3 — Governance disposition.** External participants are `RespondNever` members ([§D](#d-capability-scope-and-membership)); confirm they are governance-*subject* for budget accounting but consume no reply budget, and add `external_agent` (or a generic `external`) to the `exempt_principals` reasoning if needed ([§G](#g-governance-and-delivery-mechanics)).
- **NEW-4 — Storage home + RFC 0029 tier.** `channels.db` vs. a dedicated DB, and the migration number (RFC 0037's v0.3.12 migration is projected to take v11 first); every new store is society-scoped ([§E](#e-storage-and-provisioning)). **Blocks the store PRs.**
- **NEW-5 — Memory-poisoning policy.** Provenance-tag external-derived memory / exclude from fact extraction in Phase 1 / operator forget-path ([§Security](#security-considerations)).
- **NEW-6 — Egress budget + delivery auditing.** A per-participant egress volume ceiling distinct from request rate, and mandatory auditing of delivered message ids ([§Security](#security-considerations)).
- **NEW-7 — Client-IP capture + trusted-proxy policy.** Required by the audit story; exists nowhere today ([§Security](#security-considerations)).

7. **Channel rename and deletion vs. `CapabilityScope`.** *(Half moot, half hardened.)* **Rename: struck** — `ChannelStore` has no rename method or route, and group IDs embed the name (`ch.ID != "group:"+ch.Name` is a hard error), so a rename would change a primary key that memberships/messages reference by `ON DELETE CASCADE` — a far larger question than scope staleness, out of scope here. **Delete: real, and the "harmless" lean was wrong** — it is a silent privilege re-grant. `DeleteChannel` is a bare `DELETE FROM channels WHERE id = ?` with no tombstone and no name reservation, and `group:<name>` IDs regenerate byte-identically, so *delete `group:planning` → recreate an unrelated `group:planning`* re-grants a stale scope entry to a conversation the operator never authorized. **Re-keying scopes by canonical ID does NOT close this.** Requirement: dropping the scope entry (and closing the membership stint) on delete is normative, with an audit event; bind the grant to the channel's creation identity (`created_at` / a channel UUID) and treat a mismatch as scope-expired; add a delete→recreate→denied integration test.

## Decision / Next Steps

Draft. This RFC stays 🔨 **Draft** until the leave-Draft checklist below is dispositioned, because the readiness review surfaced blockers whose resolution changes the design, not just the prose.

**Leave-Draft checklist** (all must be dispositioned to advance to Proposed):

1. **Credential track** — resolved in this revision: RFC 0009 Phase 4 capability-token model, HTTP variant, denylist revocation ([§C](#c-auth-identity-and-credentials)). Confirm at security review (NEW-1).
2. **External-admission owner** — resolved: RFC 0012 §G membership-time gate owns `external_participants_allowed` ([§Security](#security-considerations)). Sequencing cost recorded (NEW-2).
3. **Membership model** — resolved: real `RespondNever` `memberships` row; `CapabilityScope` is a refinement ([§D](#d-capability-scope-and-membership)).
4. **ID grammar** — resolved: `ext-<account_id>` ([§A](#a-the-externalagentparticipant-participant-type)).
5. **Storage home + tier** — open (NEW-4); pick the DB + migration number.
6. **OQ 1 listener topology** — open; blocks the outbound handler PR.

**Hard shipping gates** (dependencies that must land before the corresponding phase):

- **Phase 1a**: none — buildable today.
- **Phase 1b**: RFC 0039 Phases 1–2 (the account/session substrate + the Phase 2 REST enforcement that authenticates the adjacent channel surface) and the credential-track confirmation. (0039 Phase 3 — account administration & hardening — is *not* on this path: the credential is an RFC 0009-track token, not an 0039 account.)
- **Phase 1c**: OQ 1 resolved + `/api/v1/channels/*` authenticated (RFC 0039 Phase 2) + the RFC 0012 §G admission gate.

This RFC remains sequenced after the other three umbrella RFCs, but is otherwise **independent of RFC 0040 and RFC 0041** (neither is a build dependency — see [Related Documentation](#related-documentation)). Realistic landing zone: **v0.4.x** — its critical path runs through RFC 0039 Phase 2 (targeted v0.3.x but 📋 Proposed with zero implementation today) and the RFC 0012 §G admission gate (v0.4.0).

## Related Documentation

- [Agent Runtime Vocabulary — Discussion Notes](../agent-runtime-vocabulary-roadmap.md)
- [RFC 0043 — PR Implementation Plan](0043-pr-plan.md)
- [RFC 0002 — REST API Server](0002-rest-api-server.md)
- [RFC 0009 — Agent Identity, Security & Sandboxing](0009-security-sandboxing.md)
- [RFC 0011 — Channels & Internal Agent Messaging](0011-channels-bridges.md)
- [RFC 0012 — Protocols & Organizations](0012-protocols-organizations.md)
- [RFC 0016 — Human Participant & Chat Interface](0016-human-participant-chat-interface.md)
- [RFC 0029 — Personal/Society Storage Split](0029-personal-society-storage-split.md)
- [RFC 0030 — Multi-Agent Conversation Governance](0030-multi-agent-conversation-governance.md)
- [RFC 0035 — Channel Membership Interval Ledger](0035-channel-membership-interval-ledger.md)
- [RFC 0037 — Memory Confidentiality & Channel Classification](0037-memory-confidentiality-channel-classification.md)
- [RFC 0039 — User Accounts & Authentication](0039-user-accounts-authentication.md)
- [RFC 0040 — Agent–Orchestrator Transport Unification](0040-agent-orchestrator-transport-unification.md) — the internal gRPC contract this RFC leaves untouched (a non-interaction, not a build dependency)
- [RFC 0041 — Typed Event Taxonomy and Lifecycle Callbacks](0041-typed-event-taxonomy-lifecycle-callbacks.md) — the projected event subset ([OQ 4](#open-questions)), not a build dependency
- [RFC 0052 — Autonomous Agent-Only Channels](0052-autonomous-agent-channels.md)
