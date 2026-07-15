# RFC 0040 — PR Implementation Plan (Agent–Orchestrator Transport Unification)

**RFC**: [0040-agent-orchestrator-transport-unification.md](0040-agent-orchestrator-transport-unification.md)
**Created**: 2026-07-15
**Branch prefix**: `feature/v03x-rfc0040-` (Phase 1, v0.3.x) · `feature/v040-rfc0040-` (Phases 2–4, v0.4.0)
**Target**: `main`
**Merge strategy**: Squash merge per [BRANCHING.md](../BRANCHING.md)
**Master plan**: TBD — Phase 1 rides an open v0.3.x patch (v0.3.12 the last open slot); Phases 2–4 belong to the not-yet-opened v0.4.0 plan

> ⚠️ **SKELETON — not yet actionable.** This plan is drafted ahead of RFC acceptance so the shape is visible. RFC 0040 is still **🔨 Draft**; per its [Decision / Next Steps](0040-agent-orchestrator-transport-unification.md#decision--next-steps) it must reach **Accepted** before PR 1 opens, and the [Phase 0 Hard Gate](#phase-0-hard-gate) below lists the scope decisions that must resolve first. Sizes are calibrated estimates; PR numbers, merge dates, and checklists are placeholders.

---

## Overview

RFC 0040 migrates the remaining agent→orchestrator **control-plane** calls — channel publish, channel-history fetch, agent registration/deregistration — off REST and onto a new gRPC `OrchestratorService`, leaving REST as the dedicated client edge (CLI + future Web UI). The agent→orchestrator path is already mixed today (gRPC for `LogService` logs and the RFC 0023 `WalletService` lease, REST for channels/registration); this RFC makes it uniform. Both transports become thin adapters over one shared publish core (`publishCommit`), so no invariant can be bypassed by picking a transport.

The RFC ships in **4 phases** ([RFC §Phased Implementation Plan](0040-agent-orchestrator-transport-unification.md#phased-implementation-plan)): Phase 1 (contract hardening, v0.3.x hygiene, no transport change) is independent and lands first; Phases 2–4 (proto + orchestrator handlers → agent migration → retire the agent-only REST surface) form a v0.4.0 PR train. This plan splits the work into **8 PRs**, mirroring the [RFC 0017](0017-pr-plan.md) / [RFC 0023](0023-pr-plan.md) PR-plan structure. Each PR leaves the repo in a passing-tests, lint-clean state and stays within the [BRANCHING.md](../BRANCHING.md) < 500-line size guidance.

> **Estimate calibration**: RFC 0017 and RFC 0023 PRs landed within a ~1.7× calibration factor of their initial estimates. This plan applies the same factor. Sizes below are calibrated estimates.

**Prerequisite**: RFC 0002 (REST API Server — Implemented), RFC 0004 (Python Agent gRPC Server — Implemented), RFC 0011 (Channels & Internal Agent Messaging — the publish/history surface being migrated) merged. RFC 0023 (LLM Call Leasing — Implemented, v0.3.2) is the live architectural precedent: `WalletService` already shares the `:9090` agent-facing listener and the agent's shared outbound gRPC channel, both of which this RFC reuses.

**Recommended merge order**: **PR 1** (any open v0.3.x patch) → *[v0.4.0 opens]* → **PR 2 → PR 3 → PR 4 → PR 5 → PR 6 → PR 7 → PR 8**. PR 1 is a standalone v0.3.x hygiene PR with no dependency on the rest. PRs 2–3 land the gRPC contract and orchestrator handlers with no agent-side change. PRs 4–6 migrate the agent and retire the old surface. The only hard cross-process ordering constraint is **orchestrator (PR 3) before agent (PR 4)** — the orchestrator must serve gRPC before agents dial it.

---

## Phase 0 Hard Gate

RFC 0040 is **Draft** with unresolved scope decisions that are **non-additive once the proto ships**. PR 2 (the proto) does **not** open until these resolve in the RFC review thread and the RFC advances to Accepted. (PR 1 carries no proto change and no scope dependency — it may proceed as soon as the RFC is Proposed.)

| # | Blocker (RFC ref) | Why it gates | Recommended resolution |
|---|-------------------|--------------|------------------------|
| G1 | **`sender_id` scope** — [RFC §D TODO](0040-agent-orchestrator-transport-unification.md#d-shared-core-invariant-enforcement) | Determines whether the proto/handlers carry any authentic-sender machinery, or only the transport. `sender_id` is client-supplied and trusted today; there is nothing to relocate. | **Out of scope** — RFC 0040 ships the typed gRPC carrier; authentic-sender enforcement is RFC 0029 Phase 2 (capability tokens as call metadata). Confirm at review. |
| G2 | **History migration** — [RFC OQ 3 / Goal 1](0040-agent-orchestrator-transport-unification.md#open-questions) | Sets whether **PR 5** exists at all. Goal 1 marks history optional pending this. | Migrate it for a uniform agent path, but treat PR 5 as the droppable scope if the train needs trimming. |
| G3 | **Service shape** — [RFC OQ 1](0040-agent-orchestrator-transport-unification.md#open-questions) | Pins the proto in PR 2 (new `OrchestratorService` vs. folding into an existing service). Non-additive once stubs generate. | New dedicated `OrchestratorService` (keeps each service single-direction, single-concern). Confirm at review. |
| G4 | **Transport selection** — [RFC OQ 2](0040-agent-orchestrator-transport-unification.md#open-questions) | Shapes the agent config surface + fallback logic in **PR 4**. | (a) explicit agent config flag (recommended — observable, no per-call probe) vs. (b) capability detection on `UNIMPLEMENTED`. Decide before PR 4. |

**Not blocking** (resolvable during implementation, recorded here for tracking): OQ 4 co-sequencing with RFC 0029 Phase 2 — decide when the v0.4.0 plan opens (may share a PR train so the proto/handlers are touched once); OQ 5 — **resolved** in the RFC (the `orchestrator_grpc` config field and shared outbound channel already exist).

---

## Progress Overview

| PR | Title | Phase | Version | Depends on | Est. size | Status |
|----|-------|-------|---------|------------|-----------|--------|
| 1 | Contract hardening (JSON Schema + contract test) | RFC Ph 1 | v0.3.x | — | ~250–400 | ⬜ Not started |
| 2 | `orchestrator.proto` + stubs + no-op servicer | RFC Ph 2 | v0.4.0 | G1–G3, PR 1 | ~200–350 | ⬜ Blocked (Phase 0) |
| 3 | Orchestrator handlers over `publishCommit`/registry + dual-transport test | RFC Ph 2 | v0.4.0 | PR 2 | ~350–500 | ⬜ Blocked |
| 4 | `GRPCChannelPublisher` + register/deregister + sticky transport selection | RFC Ph 3 | v0.4.0 | PR 3, G4 | ~350–500 | ⬜ Blocked |
| 5 | `GrpcChannelHistoryFetcher` (history migration) | RFC Ph 3 | v0.4.0 | PR 3 | ~200–350 | ⬜ Blocked (G2 — droppable) |
| 6 | Retire agent-only REST register + drop aiohttp control-plane client | RFC Ph 4 | v0.4.0 | PR 4 (+ PR 5 if taken) | ~200–350 | ⬜ Blocked |
| 7 | Review follow-ups | — | v0.4.0 | PR 6 | ~150–300 | ⬜ Not started |
| 8 | Full-RFC closeout (status → ✅ Implemented) | — | v0.4.0 | PR 7 | ~50–100 | ⬜ Not started |

---

## Dependency Graph

```
PR 1 (JSON Schema + contract test over today's REST path)              [RFC Phase 1, v0.3.x]
      — independent hygiene; no dependency on the rest —
· · · · · · · · · · · · · [Phase 0 Hard Gate: G1–G4 resolved, RFC Accepted, v0.4.0 opens] · · · · · · · · · · · · ·
  ↓
PR 2 (proto/orchestrator.proto + Go/Python stubs + no-op servicer skeleton)   [RFC Phase 2]
  ↓
PR 3 (orchestrator gRPC handlers → publishCommit / registry + dual-transport invariant test)   [RFC Phase 2]
  ↓
PR 4 (GRPCChannelPublisher + _self_register/_self_deregister + sticky transport selection)   [RFC Phase 3]
  ↓                                   ↘ (G2: optional, droppable)
  │                                     PR 5 (GrpcChannelHistoryFetcher — history over gRPC)   [RFC Phase 3]
  ↓                                   ↙
PR 6 (retire agent-only POST /agents/register; drop aiohttp control-plane client; docs)   [RFC Phase 4]
  ↓
PR 7 (review follow-ups)
  ↓
PR 8 (full-RFC closeout — status: ✅ Implemented)
```

PR 1 is fully independent. PRs 2–3 add no agent-side change — the contract and handlers are reviewable in isolation with REST unchanged. PR 4 is the agent flip (with REST fallback). PR 5 (history) hard-depends only on PR 3 and is gated by G2 — it can land in parallel with PR 4 or be dropped. PR 6 removes the old surface only after PR 4 (and PR 5, if taken) verify in a release.

---

## PR Sequence

### PR 1: `feature/v03x-rfc0040-contract-hardening` — Channel Payload Contract + Test

**Depends on**: Nothing (builds on the current v0.3.x baseline). No proto change, no RFC-blocking scope decision — may proceed once RFC 0040 is **Proposed**.
**Purpose**: Pin the agent↔orchestrator channel-publish/history payload contract over *today's* REST path, closing the drift risk ([RFC Motivation 1](0040-agent-orchestrator-transport-unification.md#motivation)) before the dual-surface window opens. Implements [RFC Phase 1](0040-agent-orchestrator-transport-unification.md#phase-1-contract-hardening-v03x).

#### Scope

| File | Change |
|------|--------|
| `schemas/` (new channel-payload schema — confirm the repo's canonical schema location during PR 1) | **New** — JSON Schema for the channel-publish body (`sender_id`, `content`, `mentions`, `metadata.cascade_depth`) and the channel-history response, matching today's REST bodies ([`internal/server/channel_types.go`](../../internal/server/channel_types.go)). |
| [`agents/channel_publisher.py`](../../agents/channel_publisher.py), [`agents/channel_history_fetcher.py`](../../agents/channel_history_fetcher.py) | Validate the assembled payload against the shared schema on the send side (behind the existing publish path — no transport change). |
| Go publish/history decode path ([`internal/server/channel_handlers.go`](../../internal/server/channel_handlers.go)) | Validate/asserts the decoded body against the same schema on the orchestrator side. |
| `tests/` — contract test | **New** — asserts the agent's request shape matches the orchestrator's handler expectation; runs in CI on changes to the publish/history path. |

#### Key implementation details

- The schema is **not throwaway**: the REST publish/history endpoints remain the client edge after Phase 2 (never removed), so the schema keeps validating the REST payload for the lifetime of that surface. Phase 2 adds a *separate* protobuf contract; it does not retire this schema.
- No behaviour change on either side — validation is additive. A schema mismatch fails the contract test, not production traffic (decide fail-open vs. fail-closed for the runtime validation during PR 1 review; default fail-open + log, matching the "no flag day" guarantee).

#### Tests

- Contract test: a representative publish payload and a history response validate against the schema; a deliberately drifted field fails.
- Round-trip: the agent-assembled body validates, then decodes on the Go side without loss.

#### PR checklist

- [ ] `make test` passes
- [ ] `make lint` clean
- [ ] `make validate` passes
- [ ] Shared schema covers publish (`sender_id`/`content`/`mentions`/`metadata.cascade_depth`) and history response
- [ ] Contract test runs in CI on the publish/history path
- [ ] No transport change; REST path behaviour identical

**Merged**: _PR #— — YYYY-MM-DD_

---

### PR 2: `feature/v040-rfc0040-proto-skeleton` — `OrchestratorService` Proto + No-Op Servicer

**Depends on**: [Phase 0 Hard Gate](#phase-0-hard-gate) (G1 sender_id scope, G3 service shape) resolved; RFC Accepted; PR 1 merged (verified contract to mirror into proto).
**Purpose**: Land the `OrchestratorService` gRPC contract and a thin/no-op servicer skeleton registered on the existing `:9090` listener. No handler logic, no agent-side change — the cross-language contract is reviewable in isolation. Implements [RFC Phase 2](0040-agent-orchestrator-transport-unification.md#phase-2-orchestratorservice-proto--orchestrator-handlers-v040) (proto half).

#### Scope

| File | Change |
|------|--------|
| `proto/orchestrator.proto` | **New** — `OrchestratorService` with `PublishChannelMessage`, `GetChannelHistory`, `RegisterAgent`, `DeregisterAgent` ([RFC §C](0040-agent-orchestrator-transport-unification.md#c-proto-surface--orchestratorservice)). **Pin the response message fields here** (per RFC §C design note): `GetChannelHistoryResponse` carries the ordered message list; `PublishChannelMessageResponse` carries `message_id` + timestamp — and resolves the chat-as-DM reply-correlation shape (unary response vs. follow-up). Request messages mirror the JSON body; `cascade_depth` becomes a typed field. |
| `internal/generated/orchestratorpb/` (new, generated) | Regenerated Go stubs (`make proto` / pinned protoc toolchain). |
| `agents/generated/` | Regenerated Python stubs. |
| [`cmd/orchestrator/grpcserver.go`](../../cmd/orchestrator/grpcserver.go) | Register a no-op `OrchestratorService` servicer on the same `srv` that already hosts `LogService` + `WalletService` (the **third** service on that listener). |

#### Key implementation details

- Servicer methods return `UNIMPLEMENTED` in this PR — PR 3 fills them. This lets the contract + stub regen land reviewably without behaviour.
- Regenerate stubs with the **CI-pinned** protoc + plugin versions (local toolchains are newer and trip the staleness gate).
- Confirm the error-status mapping table ([RFC §C](0040-agent-orchestrator-transport-unification.md#c-proto-surface--orchestratorservice)) is captured as proto comments / a handler-side plan so PR 3 implements it: `ErrNotMember` → `PERMISSION_DENIED`, channel-not-found → `NOT_FOUND`, invalid `sender_id`/type-mismatch/mentions-cap → `INVALID_ARGUMENT`, channels-disabled → `UNAVAILABLE`.

#### Tests

- Stub-generation freshness check passes (regenerated stubs match `.proto`).
- Servicer registers on `:9090` and returns `UNIMPLEMENTED` for each RPC (smoke test).

#### PR checklist

- [ ] `make test` / `make lint` / `make validate` pass
- [ ] `proto/orchestrator.proto` defines all four RPCs with **response fields pinned**
- [ ] Go + Python stubs regenerated with CI-pinned toolchain; staleness gate green
- [ ] No-op servicer registered on the existing `:9090` listener alongside LogService + WalletService
- [ ] REST endpoints unchanged

**Merged**: _PR #— — YYYY-MM-DD_

---

### PR 3: `feature/v040-rfc0040-orchestrator-handlers` — Handlers over `publishCommit` + Dual-Transport Test

**Depends on**: PR 2 merged (proto + stubs available).
**Purpose**: Implement the orchestrator-side gRPC handlers as thin adapters over the shared publish core and the registry, and prove neither transport bypasses a shared-core invariant. Implements [RFC Phase 2](0040-agent-orchestrator-transport-unification.md#phase-2-orchestratorservice-proto--orchestrator-handlers-v040) (handler half) + [RFC §D](0040-agent-orchestrator-transport-unification.md#d-shared-core-invariant-enforcement).

#### Scope

| File | Change |
|------|--------|
| New `internal/server/` gRPC handler file | `OrchestratorService` handlers: `PublishChannelMessage` routes through **`publishCommit`/`PublishAsync`** (not blocking `Publish` — see §D; blocking `Publish` reintroduces the console-latency regression), `GetChannelHistory` reads the store, `RegisterAgent`/`DeregisterAgent` call the registry. Error→status mapping per PR 2's table. |
| [`internal/channels/waiter.go`](../../internal/channels/waiter.go), `internal/channels/router.go` | Refresh the two doc-comments that hardcode "the agent's REST publish satisfies the waiter" — they go stale once a second transport publishes. |
| `tests/` — dual-transport invariant test | **New** — the *same* membership + cascade-clamp assertions exercised through **both** the REST and gRPC adapters, proving neither bypasses the shared core. (No `sender_id`-authenticity assertion — that invariant does not exist on either transport; see G1.) |
| `tests/` — handler unit tests | Each handler delegates correctly to `ChannelRouter` / registry against stubs. |

#### Key implementation details

- Handlers are **thin adapters** — no duplicated validation/fanout/persistence. Membership (`ChannelStore.PublishMessage` → `ErrNotMember`) and the cascade clamp (`clampCascadeDepth` in `publishCommit`) are already below both transports and inherited for free.
- OTel tracing, panic recovery, and the RFC 0009 per-agent rate limiter are inherited from the `:9090` listener's interceptors — no per-handler wiring.
- The `503 channels-disabled` sticky signal maps to `UNAVAILABLE` with a typed detail, preserving existing behaviour.

#### Tests

- Dual-transport: publish the same message over REST and gRPC → identical store state, identical fanout, identical membership rejection for a non-member.
- Cascade clamp holds over gRPC (out-of-range depth clamped before commit).
- `GetChannelHistory` over gRPC returns the same window as the REST `GET`.
- Chat-as-DM publish-and-await round-trip still completes when publish arrives over gRPC (waiter correlation intact).

#### PR checklist

- [ ] `make test` / `make lint` / `make validate` pass
- [ ] All four RPCs implemented as thin adapters over `publishCommit` / store / registry
- [ ] `PublishChannelMessage` routes through `publishCommit`/`PublishAsync`, **not** blocking `Publish`
- [ ] Dual-transport invariant test green (membership + cascade clamp via both surfaces)
- [ ] Stale waiter/router doc-comments refreshed
- [ ] Error→gRPC status mapping matches PR 2's table
- [ ] REST endpoints unchanged and fully functional

**Merged**: _PR #— — YYYY-MM-DD_

---

### PR 4: `feature/v040-rfc0040-agent-publish-register` — Agent Publish + Register over gRPC

**Depends on**: PR 3 merged (orchestrator must serve gRPC first — the one hard cross-process ordering constraint); G4 (transport selection) resolved.
**Purpose**: Flip the agent's publish and registration control-plane calls to `OrchestratorService`, with REST as a sticky per-process fallback. Implements [RFC Phase 3](0040-agent-orchestrator-transport-unification.md#phase-3-agent-side-grpc-migration-v040) (publish + registration).

#### Scope

| File | Change |
|------|--------|
| [`agents/channel_publisher.py`](../../agents/channel_publisher.py) | **New** `GRPCChannelPublisher` (all-caps, matching `HTTPChannelPublisher`) implementing the existing `ChannelPublisher` Protocol; reuses the shared `self._orchestrator_channel`. `ActionExecutor` call site unchanged. |
| [`agents/server.py`](../../agents/server.py) | `_self_register` / `_self_deregister` migrated to `OrchestratorService.RegisterAgent` / `DeregisterAgent` over the shared channel (single call site each). Retry-with-backoff on the register path (listener-not-up race). |
| Agent config / transport selection | Sticky per-process transport selection with REST fallback, per G4 (recommended: explicit config flag). Mirrors the existing `_disabled` sticky flag in `HTTPChannelPublisher`. |
| `tests/` | `GRPCChannelPublisher` unit tests against a stubbed channel; register/deregister-over-gRPC tests; fallback-to-REST test (gRPC unreachable → sticky REST). |

#### Key implementation details

- No new dependency class — the agent already runs outbound gRPC clients (log Shipper, WalletClient) over the shared channel; the new stubs reuse it (OQ 5 resolved).
- The action-executor's transport-agnostic `asyncio.wait_for` publish ceiling stays and now wraps `GRPCChannelPublisher`.
- REST publish/register paths remain fully functional (removed only in PR 6) — this PR is backwards-compatible against a pre-PR-3 orchestrator via the fallback.

#### Tests

- `GRPCChannelPublisher.publish()` delegates over the stub channel; `channel_id` carried as a typed field (no URL interpolation).
- Sticky fallback: simulated gRPC `UNAVAILABLE` → process sticks to REST for subsequent calls.
- Register/deregister round-trip over gRPC; register retry-with-backoff on cold start.

#### PR checklist

- [ ] `make test` / `make lint` / `make validate` pass
- [ ] `GRPCChannelPublisher` implements `ChannelPublisher`; call site unchanged
- [ ] `_self_register` / `_self_deregister` over `OrchestratorService`
- [ ] Sticky per-process transport selection + REST fallback (G4 mechanism)
- [ ] Reuses the shared `self._orchestrator_channel` (no new connection)
- [ ] Backwards-compatible: falls back to REST against a pre-PR-3 orchestrator

**Merged**: _PR #— — YYYY-MM-DD_

---

### PR 5: `feature/v040-rfc0040-agent-history` — `GrpcChannelHistoryFetcher` *(gated by G2 — droppable)*

**Depends on**: PR 3 merged. **Gated by [G2](#phase-0-hard-gate)** — exists only if history migrates; the most droppable scope if the train needs trimming ([RFC OQ 3](0040-agent-orchestrator-transport-unification.md#open-questions)). Can land in parallel with PR 4.
**Purpose**: Migrate channel-history fetch to `OrchestratorService.GetChannelHistory`. Implements [RFC Phase 3](0040-agent-orchestrator-transport-unification.md#phase-3-agent-side-grpc-migration-v040) (history).

#### Scope

| File | Change |
|------|--------|
| [`agents/channel_history_fetcher.py`](../../agents/channel_history_fetcher.py) | **New** `GrpcChannelHistoryFetcher` (mixed-case, matching `HttpChannelHistoryFetcher` — see [RFC §E casing note](0040-agent-orchestrator-transport-unification.md#e-agent-side-client-migration)) implementing the existing `ChannelHistoryFetcher` Protocol; preserves the **`None`-on-failure** contract callers already branch on. Reuses the shared channel + the PR 4 sticky transport selection. |
| `tests/` | `GrpcChannelHistoryFetcher` unit tests; `None`-on-error preserved; catch-up-on-restart replays history over gRPC. |

#### PR checklist

- [ ] `make test` / `make lint` / `make validate` pass
- [ ] `GrpcChannelHistoryFetcher` implements `ChannelHistoryFetcher`; casing matches its `Http…` sibling
- [ ] `None`-on-failure contract preserved
- [ ] Reuses shared channel + sticky transport selection from PR 4
- [ ] **If G2 resolves "keep history on REST": this PR is not opened; update Goal 1 + OQ 3 in the RFC accordingly**

**Merged**: _PR #— — YYYY-MM-DD (or: dropped per G2)_

---

### PR 6: `feature/v040-rfc0040-retire-rest-surface` — Retire the Agent-Only REST Surface

**Depends on**: PR 4 merged (+ PR 5 if taken), verified in a release.
**Purpose**: Remove what is now unused and document the two-surface model. Implements [RFC Phase 4](0040-agent-orchestrator-transport-unification.md#phase-4-retire-the-agent-only-rest-surface-v040).

#### Scope

| File | Change |
|------|--------|
| `internal/server/` REST routes | Remove the agent-only `POST /api/v1/agents/register`. **Do not remove** `DELETE /api/v1/agents/{id}` — it is the shared operator/CLI agent-delete endpoint ([`internal/server/agent_handlers.go`](../../internal/server/agent_handlers.go)); agents have moved *off* it (PR 4) onto `DeregisterAgent`, but the REST route is retained for operators. |
| [`agents/server.py`](../../agents/server.py) + aiohttp usage | Drop the aiohttp client dependency for control-plane calls once no control-plane path uses it. |
| Docs — `docs/diagrams/`, architecture/observability docs | Document the two-surface model: REST = client edge, gRPC = internal control plane. |

#### PR checklist

- [ ] `make test` / `make lint` / `make validate` pass
- [ ] `POST /api/v1/agents/register` removed; **`DELETE /api/v1/agents/{id}` retained for operators**
- [ ] aiohttp control-plane client dependency dropped from the agent
- [ ] Architecture diagrams + observability docs updated to the two-surface model
- [ ] No client-edge (CLI) regression

**Merged**: _PR #— — YYYY-MM-DD_

---

### PR 7: `feature/v040-rfc0040-followups` — Review Follow-Ups

**Depends on**: PR 6 merged (all core PRs complete).
**Purpose**: Address review findings from PRs 1–6, grouped by component. Per repo convention, each entry paraphrases the finding and does **not** reference or link any local PR review report.

#### Scope

Populated as PRs are reviewed.

- _From PR 1 review_: …
- _From PR 2 review_: …
- _From PR 3 review_: …
- _From PR 4 review_: …
- _From PR 5 review_: …
- _From PR 6 review_: …

#### PR checklist

- [ ] All deferred review findings addressed (status-by-finding table below)
- [ ] All deferred test gaps filled
- [ ] `make test` / `make lint` / `make validate` pass

**Merged**: _PR #— — YYYY-MM-DD_

---

### PR 8: `feature/v040-rfc0040-close` — RFC Close

**Depends on**: PR 7 merged (all follow-ups addressed).
**Purpose**: Status updates only.

#### Scope

| File | Change |
|------|--------|
| [`0040-agent-orchestrator-transport-unification.md`](0040-agent-orchestrator-transport-unification.md) | Status → `✅ Implemented` (YAML + header); re-run `make rfcs` to regenerate `INDEX.md`. |
| `ROADMAP.md` | RFC 0040 status → `✅ Implemented`; merged count; component status; merged-PR rows; header refreshed. |
| `docs/rfcs/0040-pr-plan.md` | All checklists complete; merged-PR numbers + dates filled in for every PR; record the G1–G4 resolutions and (if applicable) that PR 5 was dropped. |

#### PR checklist

- [ ] RFC 0040 status = `✅ Implemented` (YAML + header); `INDEX.md` regenerated
- [ ] ROADMAP.md RFC tracker + merged-PR history updated
- [ ] `make test` / `make lint` / `make validate` pass
- [ ] Phase 0 Hard Gate resolutions (G1–G4) recorded in this plan

**Merged**: _PR #— — YYYY-MM-DD_

---

## Open Items Carried From the RFC

These are tracked here so the plan and the RFC stay in sync until the gate clears:

1. **G1 — `sender_id` scope** ([RFC §D TODO](0040-agent-orchestrator-transport-unification.md#d-shared-core-invariant-enforcement)): confirm out-of-scope (defer to RFC 0029 Phase 2). Blocks PR 2.
2. **G2 — history migration** ([RFC OQ 3 / Goal 1](0040-agent-orchestrator-transport-unification.md#open-questions)): decides whether PR 5 exists. Resolve before the v0.4.0 train opens.
3. **G3 — service shape** ([RFC OQ 1](0040-agent-orchestrator-transport-unification.md#open-questions)): pins the PR 2 proto. Blocks PR 2.
4. **G4 — transport selection** ([RFC OQ 2](0040-agent-orchestrator-transport-unification.md#open-questions)): shapes PR 4 config. Blocks PR 4.
5. **OQ 4 — co-sequencing with RFC 0029 Phase 2**: decide when the v0.4.0 plan opens; the two workstreams touch the same proto/handlers and may share one PR train (touch the surface once).
