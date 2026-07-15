---
id: RFC-0040
title: Agent–Orchestrator Transport Unification
summary: Migrates the agent→orchestrator control-plane calls (channel publish, channel history, agent registration) from REST to gRPC, leaving REST as the dedicated client edge — so the orchestrator's inbound surface splits into two audience-specific APIs (gRPC for agents, REST for CLI / future Web UI) sharing one business-logic core, and the agent→orchestrator path gains the typed protobuf contract the orchestrator→agent path already has.
type: protocol
status: proposed
author: Maksim Khomutov
created: 2026-05-17
target: v0.3.x (Phase 1) + v0.4.0 (Phases 2–4)
depends_on:
  - RFC-0002
  - RFC-0004
  - RFC-0011
---

# RFC 0040 — Agent–Orchestrator Transport Unification

**Type**: protocol
**Status**: 📋 Proposed
**Author**: Maksim Khomutov
**Date**: 2026-05-17
**Target**: v0.3.x (Phase 1) + v0.4.0 (Phases 2–4)
**Depends on**: RFC 0002 (REST API Server — the surface this RFC re-scopes to clients-only), RFC 0004 (Python Agent gRPC Server — the existing `AgentService` this RFC mirrors in the reverse direction), RFC 0011 (Channels & Internal Agent Messaging — the channel publish/history endpoints being migrated)
**Relates to**: RFC 0023 (LLM Call Leasing — shipped v0.3.2; already runs a gRPC `WalletService` round-trip on the agent→orchestrator path, so the wallet proto and this RFC's `OrchestratorService` share a transport story), RFC 0029 (Personal/Society Storage Split — Phase 2 capability tokens are wire-auth on the agent→orchestrator path; co-sequencing target — see §Open Questions 4), RFC 0039 (User Accounts & Authentication — the REST surface's auth story, which this RFC narrows to the client edge), RFC 0032 (Wire-Level Channel Interaction Layer — an *orthogonal* channel-wire change: it adds a conversation `interaction_id` to the message payload, this RFC changes the *transport* the payload travels over; the two compose without conflict), the v0.6.0 Distributed Mesh milestone (multi-node message routing via `internal/mesh/`, explicitly out of scope here — this is *not* RFC 0006, which is the already-shipped Efficiency & Execution Limits work)

---

## Table of Contents

- [Summary](#summary)
- [Motivation](#motivation)
- [Goals](#goals)
- [Non-Goals](#non-goals)
- [Design / Implementation](#design--implementation)
  - [A. Current State — three transports, tangled by build order](#a-current-state--three-transports-tangled-by-build-order)
  - [B. Target Shape — two audience-specific surfaces over one core](#b-target-shape--two-audience-specific-surfaces-over-one-core)
  - [C. Proto Surface — `OrchestratorService`](#c-proto-surface--orchestratorservice)
  - [D. Shared-Core Invariant Enforcement](#d-shared-core-invariant-enforcement)
  - [E. Agent-Side Client Migration](#e-agent-side-client-migration)
  - [F. Failure Modes](#f-failure-modes)
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

Persatrix's two long-lived processes — the Go orchestrator and the Python agents — talk over three transports today, and the split is **directional**: orchestrator→agent is gRPC (`AgentService`), agent→orchestrator log shipping is gRPC (`LogService`), but agent→orchestrator *control-plane* calls — channel publish, channel-history fetch, agent registration — go over **REST**. That last path is not a designed choice; it is an artifact of build order (RFC 0002's REST API existed first for the CLI, RFC 0011's channel endpoints were built REST for the CLI/chat surface, and agents reused them). This RFC migrates the agent→orchestrator control-plane calls to gRPC and re-scopes REST to its real audience — the CLI and a future Web UI. The orchestrator's inbound surface ends up cleanly split: a gRPC API whose only callers are agents, and a REST API whose only callers are clients, both thin adapters over one shared business-logic core.

## Motivation

The agent→orchestrator REST path works, and at v0.3.x traffic volumes it is not a performance problem. The cost is **structural**, and it compounds as v0.4.0 work lands on top of it.

Four concrete problems:

1. **No shared typed contract on the agent→orchestrator path.** The orchestrator→agent direction has [`proto/task.proto`](../../proto/task.proto) — both sides generate types from one schema and a mismatch is a build error. The agent→orchestrator channel-publish payload is a hand-built JSON dict assembled in [`agents/channel_publisher.py`](../../agents/channel_publisher.py) (`{"sender_id": ..., "content": ..., "metadata": {...}}`) and decoded loosely on the Go side. Drift between what the agent sends and what the orchestrator's handler expects is caught only at runtime, in whatever environment first exercises the path.

2. **One REST surface serves two audiences.** The orchestrator's channel endpoints (`POST /api/v1/channels/{id}/messages`, `GET /api/v1/channels/{id}/messages`) are consumed by *both* the CLI/chat surface *and* agents. Neither audience-specific policy (auth posture, rate limiting, payload shape) nor independent evolution of the two is possible while they share one surface.

3. **Two transport stacks inside the agent process.** Each agent already runs gRPC *clients* to the orchestrator (the `LogService` shipper and the RFC 0023 `WalletService`) plus a gRPC *server* (`AgentService`), and *also* a separate aiohttp *client* for channel publish/history and registration — a second stack with its own timeout knobs. The register/deregister paths still carry hand-set, uncoordinated timeouts (`aiohttp.ClientTimeout(total=10)` and `total=5` in [`agents/server.py`](../../agents/server.py)); folding the aiohttp client onto the gRPC channel the agent already opens removes the second stack. (The action-executor's transport-agnostic `asyncio.wait_for` publish ceiling stays after migration and will also wrap `GRPCChannelPublisher`.)

4. **A non-obvious directional protocol rule.** "gRPC downstream, REST upstream, between the same two processes" is a rule every new contributor has to learn, and it has no principled basis — only a historical one.

**What happens if we do nothing.** v0.4.0 reopens exactly this surface: [RFC 0012](0012-protocols-organizations.md) (Protocols & Organizations) adds org/hierarchy traffic on the agent→orchestrator path, and [RFC 0029](0029-personal-society-storage-split.md) Phase 2 introduces capability tokens — wire-level auth — on that same path. Both would be built on the untyped REST surface and then migrated. The migration cost is lowest now and rises with every consumer added to the REST agent path.

Note that [RFC 0023](0023-llm-call-leasing.md) (shipped v0.3.2) already committed the agent→orchestrator direction to gRPC for one new call class — the `WalletService` lease round-trip. The agent→orchestrator path is therefore *already* mixed today (gRPC for logs and leasing, REST for channels/registration). This RFC removes the remaining REST control-plane calls so the path is uniform.

## Goals

1. All agent→orchestrator **control-plane** calls — channel publish and agent registration/deregistration — use gRPC against a protobuf-defined contract. Channel-history fetch is a *recommended-but-optional* inclusion, pending [Open Question 3](#open-questions).
2. REST remains the **dedicated client edge** (CLI and a future Web UI) and becomes the *only* audience the REST surface is designed for.
3. The channel publish/history endpoints **remain available over REST for clients** — they become *dual-surface* (REST for clients + gRPC for agents), not REST-removed.
4. For any dual-surface operation, both transports are **thin adapters over one shared business-logic core** — no duplicated validation, fan-out, or persistence logic.
5. Security-critical invariants that exist today — channel membership and the cascade-depth clamp — are enforced **in the shared core, below both transports**, so neither transport can bypass them. (Authoritative `sender_id` is *not* enforced today on either transport; it is new work owned by RFC 0029 Phase 2 — see §D.)
6. The migration is **incremental and backwards-compatible** — no flag day; the REST agent path keeps working until each call is migrated and verified.

## Non-Goals

- **Migrating the CLI off REST.** Browsers cannot speak native gRPC (it requires HTTP/2 trailer access the browser `fetch` API does not expose); a future Web UI therefore mandates that REST stays. With REST permanent for the browser, moving the Rust CLI to gRPC buys nothing and is explicitly excluded.
- **A gRPC-Web gateway / Envoy translation proxy.** Out of scope precisely because REST stays as the client edge — there is nothing to translate.
- **Changing the orchestrator→agent gRPC contract.** `AgentService` ([`proto/task.proto`](../../proto/task.proto)) is unchanged.
- **Changing log shipping.** `LogService` ([`proto/log_service.proto`](../../proto/log_service.proto)) is already gRPC and is untouched.
- **Introducing a message broker or any multi-node message routing.** The horizontal-scale rework of the in-process reply-correlation table ([`internal/channels/waiter.go`](../../internal/channels/waiter.go)) belongs to the v0.6.0 Distributed Mesh milestone (`internal/mesh/`) and is tracked separately.
- **Capability-token authentication itself.** That is [RFC 0029](0029-personal-society-storage-split.md) Phase 2. This RFC provides the typed gRPC transport that capability tokens will ride on (as call metadata); it does not define the tokens.
- **Authenticating the REST client edge.** That is [RFC 0039](0039-user-accounts-authentication.md). This RFC only narrows *who* the REST surface serves.

## Design / Implementation

### A. Current State — three transports, tangled by build order

```mermaid
flowchart LR
    CLI["Rust CLI"] -->|REST| OrchHTTP["Orchestrator<br/>HTTP :8080"]
    Agent["Python Agent"] -->|"REST: publish / history / register"| OrchHTTP
    Agent -->|"gRPC: LogService.StreamLogs"| OrchGRPC["Orchestrator<br/>gRPC :9090<br/>(LogService + WalletService)"]
    OrchGRPC -->|"gRPC: AgentService.*"| Agent
    OrchHTTP -.->|"shared core"| Router["ChannelRouter / registry"]
    ActionExec["SEND_CHANNEL_MESSAGE<br/>action executor"] -.->|"shared core"| Router
```

Three observations:

1. The orchestrator **already runs both** an HTTP listener (`:8080`) and a gRPC listener (`:9090`, hosting `LogService` and — when cost config is loaded — the RFC 0023 `WalletService`). Adding agent-facing RPCs is *new RPCs on an existing listener*, not a new server; `OrchestratorService` becomes the **third** service on that listener, not the second.
2. The agent→orchestrator direction is **already mixed today** — gRPC for logs and for the [RFC 0023](0023-llm-call-leasing.md) `WalletService` lease, REST for channels/registration. This RFC makes it uniform rather than introducing gRPC where there was none.
3. `ChannelRouter` ([`internal/channels/router.go`](../../internal/channels/router.go)) is **already the shared core** — its doc-comment names it "the publish-and-fanout entry point used by the REST handler *and* the `SEND_CHANNEL_MESSAGE` action executor." The business logic is already factored; what is missing is a second thin adapter.

The agent-side REST callers being migrated:

| Call | Today | Source |
|------|-------|--------|
| Publish a channel message | `POST /api/v1/channels/{id}/messages` | [`agents/channel_publisher.py`](../../agents/channel_publisher.py) — `HTTPChannelPublisher` |
| Fetch channel history | `GET /api/v1/channels/{id}/messages?limit=N` | [`agents/channel_history_fetcher.py`](../../agents/channel_history_fetcher.py) — `HttpChannelHistoryFetcher` |
| Register | `POST /api/v1/agents/register` | [`agents/server.py`](../../agents/server.py) — `_self_register` |
| Deregister | `DELETE /api/v1/agents/{id}` | [`agents/server.py`](../../agents/server.py) — `_self_deregister` |

> **Note on deregistration.** `DELETE /api/v1/agents/{id}` is **not** an agent-only route — it is the shared operator/CLI agent-delete endpoint ([`internal/server/agent_handlers.go`](../../internal/server/agent_handlers.go), `handleDeleteAgent` → `registry.Unregister`). Only `POST /api/v1/agents/register` is agent-only. This constrains Phase 4: agents move *off* `DELETE /agents/{id}`, but the REST route is retained for operators.

### B. Target Shape — two audience-specific surfaces over one core

```mermaid
flowchart LR
    CLI["Rust CLI"] -->|REST| OrchHTTP["Orchestrator HTTP :8080<br/>(client edge)"]
    WebUI["Web UI (future)"] -->|REST| OrchHTTP
    Agent["Python Agent"] -->|"gRPC: OrchestratorService.*"| OrchGRPC["Orchestrator gRPC :9090<br/>(internal control plane)"]
    Agent -->|"gRPC: LogService.StreamLogs"| OrchGRPC
    OrchGRPC -->|"gRPC: AgentService.*"| Agent
    OrchHTTP -.->|thin adapter| Core["ChannelRouter / registry<br/>(shared core — owns all invariants)"]
    OrchGRPC -.->|thin adapter| Core
```

The end state is two surfaces with disjoint audiences:

- **REST/HTTP `:8080`** — the **client edge**. CLI, future Web UI, scripts, humans. Channel publish/history endpoints stay here for clients.
- **gRPC `:9090`** — the **internal control plane**. Agents only. `AgentService` (orchestrator→agent), `LogService` (logs), the RFC 0023 `WalletService` (leasing), and the new `OrchestratorService` (this RFC).

Channel publish and history are **dual-surface**: reachable over REST (clients) *and* gRPC (agents). They are not removed from REST. Registration is **agent-only** and its REST endpoint can be retired (Phase 4).

### C. Proto Surface — `OrchestratorService`

A new proto file `proto/orchestrator.proto` defines a single service hosted by the orchestrator:

```proto
service OrchestratorService {
  // Publish a channel message. NOTE: sender_id is client-supplied and
  // trusted today on both transports; authoritative sender stamping is
  // deferred to RFC 0029 Phase 2 capability tokens (carried as call
  // metadata) — see RFC 0040 §D scope decision. Not enforced here yet.
  rpc PublishChannelMessage(PublishChannelMessageRequest) returns (PublishChannelMessageResponse);

  // Fetch the last N messages of a channel (catch-up / conversation window).
  rpc GetChannelHistory(GetChannelHistoryRequest) returns (GetChannelHistoryResponse);

  // Agent self-registration and deregistration.
  rpc RegisterAgent(RegisterAgentRequest) returns (RegisterAgentResponse);
  rpc DeregisterAgent(DeregisterAgentRequest) returns (DeregisterAgentResponse);
}
```

Design notes:

- **New service, not an extension of `LogService` or `AgentService`.** `LogService` is logs-only; `AgentService` is the *opposite* direction (orchestrator→agent). A dedicated `OrchestratorService` keeps each service's direction and concern singular. (See §Open Questions 1.)
- **Field parity with the REST body.** The request messages mirror the existing JSON bodies (`sender_id`, `content`, `mentions`, `metadata.cascade_depth`) so the migration is a transport swap, not a semantics change. The `cascade_depth` carried in `metadata` today becomes a typed field.
- **Response message shapes are not yet pinned.** The sketch names the response messages but leaves their fields to Phase 2. Before the proto lands, pin at minimum: `GetChannelHistoryResponse` carries the ordered message list (mirroring the REST `GET` body), and `PublishChannelMessageResponse` carries the assigned `message_id` + timestamp. This response is also where the **chat-as-DM publish-and-await reply-correlation** surfaces (see §D and [`internal/channels/waiter.go`](../../internal/channels/waiter.go)): decide whether the correlated reply rides the unary `PublishChannelMessageResponse` or a follow-up call, since the REST path satisfies the waiter synchronously today.
- **Error → gRPC status mapping (to be completed in Phase 2).** Only one mapping is fixed so far: the `503 channels-disabled` signal that [`agents/channel_publisher.py`](../../agents/channel_publisher.py) handles today → gRPC `UNAVAILABLE` with a typed detail, preserving the existing sticky-disable behaviour. Phase 2 must also map the remaining publish-handler outcomes: membership-denied (`ErrNotMember`) → `PERMISSION_DENIED`; channel-not-found → `NOT_FOUND`; empty/invalid `sender_id`, a `channel_id`/`channel_type` mismatch, and the mentions-count cap ([`internal/server/channel_handlers.go`](../../internal/server/channel_handlers.go)) → `INVALID_ARGUMENT`.
- **Cross-cutting middleware is inherited, not rebuilt.** The `:9090` listener already wraps every RPC with OTel tracing, panic recovery, and the RFC 0009 per-agent rate limiter ([`cmd/orchestrator/grpcserver.go`](../../cmd/orchestrator/grpcserver.go)); the new unary RPCs pick these up for free — which also delivers the per-audience rate limiting that Motivation 2 wants but the shared REST surface cannot give the agent path today.

### D. Shared-Core Invariant Enforcement

The load-bearing rule: once channel publish is reachable over *two* transports, every security-critical invariant must be enforced **below both adapters** — in the shared publish core, not in either transport handler.

**What the shared core actually is.** The real shared entry point is `publishCommit` ([`internal/channels/router_publish_async.go`](../../internal/channels/router_publish_async.go)), reached by *both* the synchronous `ChannelRouter.Publish` (used only by the chat-as-DM `PublishAndAwait` path) and `ChannelRouter.PublishAsync` (used by the production REST handler, which returns at the persistence boundary and detaches fanout to avoid a multi-second console stall — [`internal/server/channel_handlers.go`](../../internal/server/channel_handlers.go)). The gRPC `PublishChannelMessage` adapter **must route through `publishCommit`/`PublishAsync`**, not the blocking `Publish`: calling `Publish` would reintroduce the console-latency regression, and reimplementing publish would bypass the invariants below. (Two doc-comments hardcode "the agent's REST publish satisfies the waiter" — [`internal/channels/waiter.go`](../../internal/channels/waiter.go) and `router.go`; they go stale when the agent moves to gRPC and should be refreshed in Phase 3.)

The invariants, and where they live today:

- **Cascade-depth clamp — already below both transports.** `publishCommit` clamps inbound `cascade_depth` to `[0, maxCascadeDepth]` before the store commit (`clampCascadeDepth` in [`internal/channels/cascade_depth.go`](../../internal/channels/cascade_depth.go), applied on the shared publish path). The gRPC adapter inherits it for free.
- **Channel membership — already below both transports.** Membership is enforced in the store, one layer *below* `ChannelRouter`: `ChannelStore.PublishMessage` rejects a non-member with `ErrNotMember` ([`internal/channels/sqlite_messages.go`](../../internal/channels/sqlite_messages.go); documented in [`internal/channels/store.go`](../../internal/channels/store.go)). There is **nothing REST-handler-resident to relocate** for membership — the gRPC adapter inherits it too.
- **`sender_id` trust — ⚠️ does not exist yet; SCOPE DECISION REQUIRED.** The intended guarantee is that the orchestrator, not the agent's LLM, is authoritative for `sender_id`. **That guarantee is not implemented today:** the publish handler accepts the client-supplied `sender_id` verbatim after only an empty-check ([`internal/server/channel_handlers.go`](../../internal/server/channel_handlers.go); [`internal/server/channel_types.go`](../../internal/server/channel_types.go) states the orchestrator "does not infer sender identity in v0.3.0"). There is no authenticated identity to stamp from and no existing REST-resident check to relocate. Authentic-sender enforcement is therefore **new capability work that hard-depends on [RFC 0029](0029-personal-society-storage-split.md) Phase 2 capability tokens** — not a mechanical relocation this RFC can perform on its own.

> **⚠️ TODO — blocks Proposed → Accepted.** Decide whether authentic-sender enforcement is in scope for RFC 0040 at all. **Recommended: out of scope.** RFC 0040 delivers the typed gRPC transport a capability token rides on (as call metadata); [RFC 0029](0029-personal-society-storage-split.md) Phase 2 introduces the token and the enforcement. Until this decision lands, §D's only transport-parity requirement is confirming the gRPC adapter routes through `publishCommit` (above) so it inherits the cascade clamp and store-level membership check.

### E. Agent-Side Client Migration

The agent side already has the right seams — the migration swaps *implementations*, not call sites:

- [`agents/channel_publisher.py`](../../agents/channel_publisher.py) defines `ChannelPublisher` as a `typing.Protocol`. A new `GRPCChannelPublisher` is simply another implementation of that Protocol; the `ActionExecutor` call site is unchanged.
- [`agents/channel_history_fetcher.py`](../../agents/channel_history_fetcher.py) defines `ChannelHistoryFetcher` as a `Protocol` for the same reason; a `GrpcChannelHistoryFetcher` slots in identically. (Casing: match each file's existing sibling — the fetcher is `HttpChannelHistoryFetcher` (mixed-case), the publisher is `HTTPChannelPublisher` (all-caps) — so the new classes are `GrpcChannelHistoryFetcher` and `GRPCChannelPublisher` respectively, avoiding a lint churn.)
- [`agents/server.py`](../../agents/server.py) `_self_register` / `_self_deregister` are private methods with a single call site each — a localized change.

The agent already runs *outbound* gRPC clients to the orchestrator — the `LogService` log shipper and the RFC 0023 `WalletService` client — over a single shared channel it opens at startup (`self._orchestrator_channel`, [`agents/server.py`](../../agents/server.py)), so the gRPC control-plane *client* introduces no new dependency class. The orchestrator gRPC address is also already configured (`orchestrator_grpc` param / `--orchestrator-grpc` flag, defaulting to the orchestrator host on `:9090`), which **resolves Open Question 5**: the new `OrchestratorService` stubs should reuse the existing shared channel rather than dial a new connection. Once migration completes, the aiohttp client dependency can be dropped from the agent process (Phase 4).

### F. Failure Modes

| Failure | Behaviour | Mitigation |
|---------|-----------|------------|
| Orchestrator gRPC endpoint unreachable mid-rollout | Agent control-plane calls fail | Phase 3 keeps the REST path as a configured fallback until Phase 4; transport selection is sticky per process (mirrors the existing `_disabled` sticky flag in `HTTPChannelPublisher`) |
| gRPC adapter bypasses a shared-core invariant (cascade clamp / membership) by not routing through `publishCommit` | Security or fanout regression | §D — the gRPC adapter must call `publishCommit`/`PublishAsync`; gated by the dual-transport test that drives both surfaces through the same invariant assertions. (`sender_id` authenticity is *not* enforced on either transport today — see the §D scope TODO) |
| REST and gRPC payload schemas drift during the dual-surface window | Agent and orchestrator disagree on a field | Phase 1 ships a shared schema + contract test *before* any transport change, so the dual-surface window starts from a pinned contract |
| Channel-history fetch fails over gRPC | Catch-up returns no history | Same best-effort contract as today — `HttpChannelHistoryFetcher` already returns `None` on error and callers branch on it; the gRPC fetcher preserves the `None`-on-failure contract |
| Registration race — agent dials orchestrator gRPC before the listener is up | Registration fails at boot | Retry-with-backoff at the agent's self-register call site; the orchestrator gRPC listener already starts before agents are expected to connect |

## Security Considerations

- **No weakening of the `sender_id` trust boundary — because there is none to weaken yet.** `sender_id` is client-supplied and trusted on both transports today (§D); this RFC neither adds nor removes that. Authentic-sender enforcement is new work owned by [RFC 0029](0029-personal-society-storage-split.md) Phase 2, for which this RFC provides the typed gRPC carrier (call metadata). See the §D scope TODO.
- **New inbound gRPC surface, same trust zone.** `OrchestratorService` is internal-only — its only intended callers are agents, on the same gRPC listener that already hosts `LogService` *and* the RFC 0023 `WalletService` (itself a unary agent→orchestrator control-plane service). It introduces no client-facing surface and no new network trust boundary beyond what those two services already established.
- **Auth posture is inherited, not invented.** The agent→orchestrator path is unauthenticated today (both REST and gRPC). This RFC does not change that; it makes the path *uniform* so that [RFC 0029](0029-personal-society-storage-split.md) Phase 2 (capability tokens) and [RFC 0039](0039-user-accounts-authentication.md) (REST client auth) each have a single, typed surface to attach to. gRPC call metadata is the natural carrier for a capability token — a cleaner attachment point than a REST header.
- **Reduced attack surface at the client edge.** Narrowing the REST surface to clients-only means agent-specific endpoints (registration) leave the public HTTP API entirely (Phase 4), shrinking what an unauthenticated REST caller can reach.
- **Input encoding.** The REST path today must URL-encode an LLM-supplied `channel_id` to prevent path injection ([`agents/channel_publisher.py`](../../agents/channel_publisher.py) Must-Fix #1). gRPC carries `channel_id` as a typed field with no URL-path interpolation, removing that injection class for the agent path.

## Migration Path

The migration is structured so the codebase is shippable after every phase and no phase requires a flag day. The four phases and their deliverables are detailed under [Phased Implementation Plan](#phased-implementation-plan); this section covers only the compatibility and ordering guarantees that hold *between* them.

**The old surface is removed only after the new one is proven.** The REST agent endpoints stay fully functional from Phase 1 through Phase 3 — `OrchestratorService` is added *alongside* them (Phase 2), agents flip to it with REST as a configured fallback (Phase 3), and only then are the agent-only REST endpoints retired (Phase 4). The channel publish/history REST endpoints are never removed; they are the client edge.

**Backwards compatibility holds across the rollout.** An agent build from before Phase 3 talks REST to a post-Phase-2 orchestrator unchanged (REST endpoints unremoved until Phase 4). A post-Phase-3 agent against a pre-Phase-2 orchestrator falls back to REST. The only hard ordering constraint is **orchestrator-Phase-2 before agent-Phase-3** — the orchestrator must serve gRPC before agents dial it.

**Contract precedes transport.** Phase 1 pins the agent↔orchestrator channel-publish/history contract over *today's* REST path, closing the drift risk (Motivation 1) immediately, so the dual-surface window (Phases 2–3) begins from a verified contract rather than two payload schemas drifting in parallel. Phase 1 carries no proto change and no RFC-blocking dependency, so it can land on any open v0.3.x patch as hygiene. (As of 2026-07-15 it has not yet been scheduled onto any v0.3.x patch — the v0.3.x train is at its RFC 0052 capstone with v0.3.12 the last open slot — so a reader should not assume Phase 1 has shipped; the scheduling integer, and whether it slips to v0.4.0 with the rest, is the maintainer's call.)

## Phased Implementation Plan

### Phase 1: Contract Hardening (v0.3.x)

**Summary.** Pin the agent↔orchestrator channel-publish/history contract over the existing REST path — no transport change.

**Deliverables.**
1. A shared schema for the channel-publish and channel-history payloads (JSON Schema, validated on both the agent send side and the orchestrator decode side).
2. A contract test asserting the agent's request shape against the orchestrator's REST handler expectation.
3. No proto change, no RFC-blocking dependency — Phase 1 can land on whichever v0.3.x patch is open, in the manner of the [v0.3.x-sequencing.md](../v0.3.x-sequencing.md) MQ follow-ups.

**Dependencies.** None.

**Note.** The JSON Schema is not throwaway. The REST channel publish/history endpoints remain the client edge after Phase 2 (they are never removed — see [Migration Path](#migration-path)), so the schema keeps validating the REST payload for the lifetime of that surface. Phase 2 adds a *separate* protobuf contract for the agent gRPC path; it does not retire the JSON Schema.

### Phase 2: `OrchestratorService` Proto + Orchestrator Handlers (v0.4.0)

**Summary.** Introduce the gRPC surface; the orchestrator serves both transports.

**Deliverables.**
1. `proto/orchestrator.proto` (new) with `OrchestratorService`; regenerate Go + Python stubs.
2. Orchestrator-side gRPC handlers — thin adapters over `ChannelRouter` and the registry — registered on the existing `:9090` listener.
3. Confirm the gRPC adapter routes through the shared `publishCommit` core so it inherits the cascade clamp and store-level membership enforcement — both already below both transports (§D). No `sender_id`/membership relocation is required; `sender_id` authenticity is deferred to RFC 0029 Phase 2 pending the §D scope decision.
4. REST endpoints unchanged and fully functional.

**Dependencies.** Phase 1 (verified contract to mirror into proto).

### Phase 3: Agent-Side gRPC Migration (v0.4.0)

**Summary.** Agents use gRPC for control-plane calls; REST remains a fallback.

**Deliverables.**
1. `GRPCChannelPublisher` and `GrpcChannelHistoryFetcher` (casing matches each existing sibling — see §E) — new implementations of the existing `ChannelPublisher` / `ChannelHistoryFetcher` Protocols.
2. `_self_register` / `_self_deregister` migrated to `OrchestratorService` RPCs.
3. Sticky per-process transport selection with REST fallback.

**Dependencies.** Phase 2 (orchestrator must serve gRPC first).

### Phase 4: Retire the Agent-Only REST Surface (v0.4.0)

**Summary.** Remove what is now unused; document the final shape.

**Deliverables.**
1. Remove the agent-only `POST /api/v1/agents/register` endpoint. For deregistration, move agents *off* `DELETE /api/v1/agents/{id}` (onto `OrchestratorService.DeregisterAgent`) but **retain** the REST route — it is the shared operator/CLI agent-delete endpoint ([`internal/server/agent_handlers.go`](../../internal/server/agent_handlers.go)), not agent-only.
2. Remove the agent's aiohttp client dependency for control-plane calls.
3. Documentation: the orchestrator's two-surface model (REST = client edge, gRPC = internal control plane).

**Dependencies.** Phase 3 verified in a release.

## Files Touched (Estimated)

| Component | Files | Change |
|-----------|-------|--------|
| Protos | `proto/orchestrator.proto` (new) | New `OrchestratorService` definition (Phase 2) |
| Go orchestrator | `internal/generated/orchestratorpb/` (new, generated) | Regenerated stubs (Phase 2) |
| Go orchestrator | `cmd/orchestrator/` startup, new `internal/server/` gRPC handler file | Register `OrchestratorService` on the `:9090` listener; thin handlers (Phase 2) |
| Go orchestrator | [`internal/channels/router_publish_async.go`](../../internal/channels/router_publish_async.go) (`publishCommit`), [`internal/channels/waiter.go`](../../internal/channels/waiter.go) | Ensure the gRPC adapter routes through the shared publish core; refresh the "REST publish satisfies the waiter" doc-comments (Phase 2–3). No `sender_id`/membership relocation — both already sit below both transports (§D) |
| Python agents | `agents/generated/` | Regenerated stubs (Phase 2) |
| Python agents | [`agents/channel_publisher.py`](../../agents/channel_publisher.py), [`agents/channel_history_fetcher.py`](../../agents/channel_history_fetcher.py) | New gRPC Protocol implementations — `GRPCChannelPublisher` / `GrpcChannelHistoryFetcher` (Phase 3) |
| Python agents | [`agents/server.py`](../../agents/server.py) | `_self_register` / `_self_deregister` over gRPC (Phase 3) |
| Go orchestrator | `internal/server/` REST routes | Remove agent-only `register` endpoints (Phase 4) |
| Config / schemas | shared channel-payload schema (Phase 1); agent config for transport selection (Phase 3) | Phase 1 + Phase 3 |
| Tests | `tests/` — contract test (Phase 1), gRPC integration tests (Phase 2–3) | All phases |
| Docs | architecture diagrams, `docs/diagrams/`, observability docs | Phase 4 |

## Test Strategy

- **Unit tests.** `OrchestratorService` handler adapters in isolation (each delegates correctly to `ChannelRouter` / registry); the `GRPCChannelPublisher` / `GrpcChannelHistoryFetcher` Protocol implementations against a stubbed channel.
- **Integration tests.** Agent → gRPC → orchestrator → `ChannelRouter` → fanout, end to end; the dual-transport invariant test (§D / Failure Modes) — the *same* membership + cascade-clamp assertions exercised through both the REST and gRPC adapters, proving neither bypasses the shared core. (No `sender_id`-authenticity assertion — that invariant does not exist on either transport today; see §D.)
- **Contract test.** Phase 1 deliverable — the shared schema pins the payload shape; runs in CI on changes to the publish/history path.
- **E2E / smoke tests.** The chat-as-DM round-trip (publish-and-await) still completes after agents move to gRPC; channel catch-up on agent restart still replays history.
- **Manual tests.** A new `MT-*` entry for the two-surface model — verify the CLI (REST) and an agent (gRPC) can both publish to the same channel and the orchestrator treats them identically.

## Open Questions

1. **New `OrchestratorService` vs. extending an existing service.** This RFC proposes a new service. The alternative — folding the RPCs into a broadened service — was rejected to keep each service single-direction and single-concern. Confirm at review.
2. **Transport selection mechanism during the Phase 3 rollout.** Options: (a) explicit agent config flag; (b) capability detection (try gRPC, fall back to REST on `UNIMPLEMENTED`). Recommend (a) — explicit, observable, no per-call probe cost. Decide at `0040-pr-plan.md` authoring time.
3. **Does `GetChannelHistory` need to move at all?** History fetch is read-only and lower-stakes than publish. One option is to migrate publish + registration to gRPC and leave history on REST. Recommend migrating it too, for a uniform agent path — but flag it as the most droppable scope if Phase 3 needs trimming. **Ties directly to Goal 1** (now written as marking history optional pending this decision) — resolve both together before the pr-plan is written.
4. **Co-sequencing with [RFC 0029](0029-personal-society-storage-split.md) Phase 2.** RFC 0029 Phase 2 adds capability tokens — wire auth on this exact path. Phases 2–3 here and RFC 0029 Phase 2 touch the same proto/handlers. Should they share one v0.4.0 PR train so the surface is touched once? Resolve when the v0.4.0 plan opens.
5. ~~**Registration over gRPC — bootstrap ordering.**~~ **Resolved (2026-07-15):** the gRPC address config field already exists (`orchestrator_grpc` param / `--orchestrator-grpc` flag, default host `:9090`), and the agent already opens a shared outbound gRPC channel the new client stubs can reuse ([`agents/server.py`](../../agents/server.py)). No new config surface is needed; the registration RPCs reuse the existing channel. The remaining bootstrap concern (agent dials before the listener is up) is covered by the retry-with-backoff in [Failure Modes](#f-failure-modes).

## Decision / Next Steps

**Status: 📋 Proposed.** This RFC is open for review. Before it can advance to Accepted:

1. Resolve Open Questions 1 and 2 (service shape, transport selection) in the review thread — both are non-additive once the proto ships.
2. Decide the §D `sender_id` scope question — authentic-sender enforcement in scope, or hard-deferred to RFC 0029 Phase 2 (recommended). Membership and the cascade clamp already sit below both transports (§D), so **no relocation audit is required**.
3. Resolve Open Question 3 / Goal 1 — whether `GetChannelHistory` migrates — which fixes Phase 3 scope.
4. Decide the co-sequencing with RFC 0029 Phase 2 (Open Question 4) when the v0.4.0 plan opens.

If accepted: file `docs/rfcs/0040-pr-plan.md` with Phase 1 as a standalone v0.3.x hygiene PR and Phases 2–4 as a v0.4.0 PR train, modeled on the [RFC 0017 PR plan](0017-pr-plan.md) structure.

## Related Documentation

- [RFC 0002 — REST API Server](0002-rest-api-server.md) — the REST surface this RFC re-scopes to the client edge.
- [RFC 0004 — Python Agent gRPC Server](0004-python-agent-grpc-server.md) — `AgentService`, the orchestrator→agent direction this RFC mirrors.
- [RFC 0011 — Channels & Internal Agent Messaging](0011-channels-bridges.md) — the channel publish/history surface being migrated.
- [RFC 0023 — LLM Call Leasing](0023-llm-call-leasing.md) — shipped v0.3.2; already committed the agent→orchestrator path to gRPC for the `WalletService` lease.
- [RFC 0029 — Personal/Society Storage Split](0029-personal-society-storage-split.md) — Phase 2 capability tokens ride this RFC's gRPC transport; co-sequencing target.
- [RFC 0039 — User Accounts & Authentication](0039-user-accounts-authentication.md) — authenticates the REST client edge.
- [RFC 0032 — Wire-Level Channel Interaction Layer](0032-channel-interaction-layer.md) — orthogonal channel-wire change (conversation `interaction_id`); composes with this transport change without conflict.
- [RFC 0012 — Protocols & Organizations](0012-protocols-organizations.md) — v0.4.0 org/hierarchy traffic that would otherwise land on the untyped REST agent path.
- [ROADMAP.md](../../ROADMAP.md) — the v0.6.0 Distributed Mesh milestone (`internal/mesh/`) owns multi-node message routing, explicitly out of scope here.
- [v0.3.x-sequencing.md](../v0.3.x-sequencing.md) — the one-story-per-version discipline that places Phase 1 in v0.3.x and Phases 2–4 in v0.4.0.
- [`internal/channels/router.go`](../../internal/channels/router.go) — `ChannelRouter`, the shared business-logic core.
- [`agents/channel_publisher.py`](../../agents/channel_publisher.py) · [`agents/channel_history_fetcher.py`](../../agents/channel_history_fetcher.py) — the agent-side REST callers being migrated.
