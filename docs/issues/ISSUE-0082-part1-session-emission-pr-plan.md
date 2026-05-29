# ISSUE-0082 — PR Implementation Plan (Part 1 — Orchestrator Per-Request Session Emission)

**Issue**: [ISSUE-0082](ISSUE-0082-orchestrator-per-request-session-principal-emission.md)
**Status**: 📋 Ready
**Created**: 2026-05-29
**Branch prefix**: `feature/v035-issue0082-`
**Target**: `main`
**Merge strategy**: Squash merge per [BRANCHING.md](../BRANCHING.md)
**Spawned from**: [ISSUE-0081](ISSUE-0081-session-id-process-global-not-task-local.md) closeout (PR 4, [#456](https://github.com/mkhomutov/Persatrix/pull/456))
**RFC**: [RFC 0031 §B/§E amendments](../rfcs/0031-per-session-namespacing-channels.md#b-session-lifecycle) (the session-unit decision this plan executes)

---

## Overview

ISSUE-0081's Python vertical (PRs [#453](https://github.com/mkhomutov/Persatrix/pull/453)–[#456](https://github.com/mkhomutov/Persatrix/pull/456)) moved the persona-side session id from process-global-cached to **task-local, resolved at call time**, and laid the full gRPC + event-envelope rail that binds a per-request session inside `on_event`. That rail is **armed but never fed**: the Go orchestrator still resolves **one** session id per process at boot ([`cmd/orchestrator/startup.go:39`](../../cmd/orchestrator/startup.go) `resolveSessionID`) and emits **no** per-request `persatrix-session` gRPC header, so [`agents.session_metadata._session_from_context`](../../agents/session_metadata.py) always returns `None` and every handler falls back to its construction-time snapshot — exactly the pre-ISSUE-0081 behaviour.

ISSUE-0082 has two independently shippable parts (see the issue's §Proposed fix):

1. **Session emission** — a self-contained Go follow-up. **This plan.**
2. **Principal emission** — gated on [RFC 0039](../rfcs/0039-user-accounts-authentication.md) (authenticated accounts). Out of scope here; tracked in §Future Work.

**This plan ships Part 1: the orchestrator becomes the authoritative, persisted source of a per-request session id and emits it as the `persatrix-session` header on every outbound dispatch.** It activates the cross-conversation isolation ISSUE-0081 built; it does **not** add any new storage, transport, or binding mechanism on the persona side — those are already merged.

### The session unit is already decided

[RFC 0031 §B amendment (PR 2)](../rfcs/0031-per-session-namespacing-channels.md#b-session-lifecycle) records the canonical decision this plan implements, so no design call is re-litigated here:

* **Session unit = `(agent, channel, user)`** — the finest grain. Two peers in one channel, or two DM threads with one agent, are distinct sessions even within one process.
* **Orchestrator-authoritative + persisted.** The Go orchestrator owns and *persists* the id; it is **not** derived process-side. Authoritative + persisted is what lets a multi-day dementia-test arc survive a persona-process restart — a derived-per-process id would not.
* **`PERSATRIX_SESSION_ID` stays the construction-time seed / single-session fallback**, so CLI / boot / test paths are unchanged when no per-request id is emitted.

### Where the three axes of the unit are available

All three components of `(agent, channel, user)` are in hand at the single live dispatch chokepoint — [`GRPCMessageDispatcher.Dispatch`](../../internal/channels/grpc_dispatcher.go) — with no plumbing:

| Unit axis | Source at dispatch |
|-----------|--------------------|
| agent | `env.Recipient.ParticipantID` (the recipient persona) |
| channel | `msg.ChannelID` |
| user | `msg.SenderID` (the message author) |

`Dispatch` is per-recipient, so resolving the session there yields one id per `(recipient-agent, channel, sender)` triple — exactly the §B grain. The synchronous `SendChatMessage` gRPC path is **dead-but-wired** ([ISSUE-0035](ISSUE-0035-chat-executor-dead-but-wired-cleanup.md)); the production REST chat path routes through `ChannelRouter.Publish` → fanout → `Dispatch` → `ReceiveChannelMessage`, so the dispatcher is the only emission site that needs wiring.

### What is already in place (do not rebuild)

* **Persona-side rail** — `persatrix-session` header lift in the servicer ([`agents/session_metadata.py`](../../agents/session_metadata.py)), `EVENT_SESSION_METADATA_KEY` stamp on `AgentEvent.metadata`, and the `session_scope` re-establishment in `_LLMPersonaAgent.on_event` via [`request_scope_from_metadata`](../../agents/request_scope.py). All merged in ISSUE-0081 PR 2.
* **Orchestrator-side `sessions` table** — `channels.db` already carries a `sessions` table and `session_id` columns on `channels` / `messages` (RFC 0031 Phase 1, migration v3, [`internal/channels/sqlite_schema.go`](../../internal/channels/sqlite_schema.go)). This plan adds the per-request **binding** table, not the session registry.
* **Outbound-metadata pattern** — [`internal/observability/grpcmeta`](../../internal/observability/grpcmeta/grpcmeta.go) already injects four kebab-case `persatrix-*` correlation IDs on the executor path via `metadata.AppendToOutgoingContext`. PR 2 extends that package with the session key rather than inventing a parallel mechanism.

---

## Dependency Graph

```
PR 1 (per-request session binding store: (agent, channel, user) → session_id, persisted in channels.db)
  ↓
PR 2 (grpcmeta persatrix-session key + emission wired into GRPCMessageDispatcher.Dispatch)
  ↓
PR 3 (end-to-end concurrent-isolation test through the live gRPC path + RFC/issue closeout)
```

PR 1 must precede PR 2 — the dispatcher cannot emit an id the resolver does not yet mint. PR 2 is the behaviour change (the activation). PR 3 proves the end-to-end property and lands the doc closeout.

---

## PR Sequence

### PR 1: `feature/v035-issue0082-session-source` — Per-Request Session Binding Store

**Depends on**: Nothing (RFC 0031 Phase 1 `sessions` table merged in v0.3.1).
**Purpose**: Make the orchestrator the **authoritative, persisted** source of a per-request session id keyed on `(agent, channel, user)`. Pure addition — the resolver is built and tested but **not yet wired into dispatch**, so there is no behaviour change. Mirrors how ISSUE-0081 PR 1 shipped the decision-free contextvars enabler ahead of the propagation PR.

#### Scope

| File | Change |
|------|--------|
| [`internal/channels/sqlite_schema.go`](../../internal/channels/sqlite_schema.go) | New migration `v3 → v4` under the existing `channelStoreSchemaVersion` runner (`user_version` stamped in-transaction, idempotent on reopen — the established discipline): `CREATE TABLE session_bindings (agent_id TEXT NOT NULL, channel_id TEXT NOT NULL, user_id TEXT NOT NULL, session_id TEXT NOT NULL, created_at REAL NOT NULL, PRIMARY KEY (agent_id, channel_id, user_id))`. Bump `channelStoreSchemaVersion` 3 → 4. |
| `internal/channels/session_binding.go` (new) | `SessionResolver.Resolve(ctx, agentID, channelID, userID) (string, error)`: look up the binding row; on miss, **mint** a UUIDv7-derived id, register it in the `sessions` table (so `persatrix session list` surfaces it — Phase 3 CLI), insert the binding, and return it. First-sight mint + insert run in **one transaction**; a concurrent first-sight of the same triple resolves to one id (UPSERT / `INSERT … ON CONFLICT DO NOTHING` then re-read — SQLite is single-writer). |
| `internal/channels/session_binding_test.go` (new) | Stable id for the same triple across a store reopen (the persistence/restart property); distinct triples → distinct ids; concurrent first-sight of one triple never double-mints; the minted session is registered in `sessions`. |
| `internal/channels/sqlite_session_migration_test.go` | Extend: v4 migration on a fresh DB (table + PK present); v4 on a v3 fixture DB (existing rows untouched, no backfill); idempotent replay. |

#### Key implementation details

* **Why a binding table, not deterministic derivation.** A pure hash of `(agent, channel, user)` would survive a restart without persistence — but [RFC 0031 §B](../rfcs/0031-per-session-namespacing-channels.md#b-session-lifecycle) explicitly chose *authoritative + persisted* over *derived*: the orchestrator owns the id so a future operator surface (`persatrix session …`) can label, list, and archive it, and so the session is a first-class registered row rather than an opaque hash. The binding table is the `(agent, channel, user) → session_id` map; the `sessions` table stays the session registry (id, label, created_at, …).
* **UUIDv7, not v4** — matches the §B `persatrix session new` mint, so emitted ids sort lexicographically by creation time (the default ordering `persatrix session list` will rely on in Phase 3).
* **Society state, per [RFC 0031 §G.1](../rfcs/0031-per-session-namespacing-channels.md#g-interaction-with-rfc-0029-storage-split).** The binding lives in `channels.db` alongside `channels` / `sessions`, not in any per-agent `memory.db`. When RFC 0029 Phase 3 moves the society store to Postgres, the binding table moves with `channels` — no redesign.
* **No emission, no wiring in PR 1.** The dispatcher still injects nothing; behaviour is byte-identical to today. PR 1 is reviewable as a self-contained store.

#### Tests

* Resolve `(a, c, u)` twice → identical id; reopen the store → still identical (persistence pin — the dementia-arc-survives-restart property).
* `(a, c, u1)` vs `(a, c, u2)` vs `(a2, c, u)` → three distinct ids.
* Concurrent first-sight of one triple from N goroutines → exactly one id, one `sessions` row.
* Migration v4: fresh + v3-upgrade + idempotent replay.

#### PR checklist

* [ ] `make test` passes; `make lint` clean.
* [ ] Migration tested against a v3 fixture DB; `user_version` stamped in-transaction.
* [ ] No call site wired yet — `grep` confirms `SessionResolver` has no production caller (enabler only).
* [ ] [ISSUE-0082 row in ROADMAP](../../ROADMAP.md) / issues index reflects work-in-progress per [Status Hygiene](../development-workflow.md#status-hygiene).

---

### PR 2: `feature/v035-issue0082-emit` — `persatrix-session` Emission on the Dispatch Path

**Depends on**: PR 1 merged.
**Purpose**: Emit the resolved per-request session id as the `persatrix-session` gRPC header on every outbound dispatch — the activation that feeds the ISSUE-0081 rail. This is the behaviour change.

#### Scope

| File | Change |
|------|--------|
| [`internal/observability/grpcmeta/grpcmeta.go`](../../internal/observability/grpcmeta/grpcmeta.go) | Add `MDSession = "persatrix-session"` (the cross-language wire key — must match [`agents.session_id.SESSION_METADATA_GRPC_KEY`](../../agents/session_id.py)) and a small `InjectSession(ctx, sessionID) context.Context` helper that appends it via `metadata.AppendToOutgoingContext` (empty id → no-op, matching the existing partial-set semantics). Keep the four-correlation-`IDs` struct untouched — session is a distinct concern with a distinct persona-side consumer (the servicer + `on_event`, **not** the RFC 0018 logging interceptor). |
| [`internal/channels/grpc_dispatcher.go`](../../internal/channels/grpc_dispatcher.go) | `GRPCMessageDispatcher` gains a `SessionResolver` dependency (constructor/option). In `Dispatch`, before `client.ReceiveChannelMessage(ctx, event)`, resolve `sid := resolver.Resolve(ctx, env.Recipient.ParticipantID, msg.ChannelID, msg.SenderID)` and `ctx = grpcmeta.InjectSession(ctx, sid)`. Pin the resolved id on the `channel.dispatch` span for trace correlation. |
| [`cmd/orchestrator/channels.go`](../../cmd/orchestrator/channels.go) | Construct the `SessionResolver` over the channels store and pass it into the dispatcher wiring. |
| `internal/channels/grpc_dispatcher_test.go` | A fake `AgentService` server asserts the incoming gRPC metadata carries `persatrix-session` == the resolver's id for the dispatched `(agent, channel, sender)`; a second concurrent dispatch for a different sender carries a different id. |
| `internal/observability/grpcmeta/grpcmeta_test.go` | `InjectSession` round-trip (inject → `metadata.FromOutgoingContext`); empty id is a no-op; coexists with `InjectIDs` on the same ctx. |

#### Key implementation details

* **Single emission site.** Only `Dispatch` needs the header — the dead `SendChatMessage` path ([ISSUE-0035](ISSUE-0035-chat-executor-dead-but-wired-cleanup.md)) is intentionally left alone. Wiring it would feed a path no production caller reaches and couple this change to dead-code cleanup.
* **Behaviour change is the point.** After PR 2 a single-conversation deployment that previously fell through to the `legacy` construction snapshot now recalls under a real `(agent, channel, user)` session. Pre-RFC and pre-activation rows stay visible via the §D `legacy` carve-out, so no row is stranded; concurrent conversations for one agent now recall in isolation. This is the ISSUE-0081 fix going live on the session axis.
* **Header hygiene** — lowercase kebab-case `persatrix-session` (HTTP/2 convention, lifted case-insensitively persona-side); the value is the resolver's id, never empty on the live path (the resolver always returns a concrete id).
* **No principal header.** `persatrix-principal` stays unemitted (resolves to `'local'` persona-side) until [RFC 0039](../rfcs/0039-user-accounts-authentication.md) — see §Future Work.

#### Tests

* Dispatcher emits `persatrix-session` == resolved id; distinct senders in one channel → distinct emitted ids.
* `InjectSession` unit round-trip + empty-id no-op + coexistence with `InjectIDs`.
* Existing dispatcher tests (unknown participant drop, unhealthy agent, RPC error) stay green — emission is additive on the happy path only.

#### PR checklist

* [ ] `make test` passes; `make lint` clean.
* [ ] Emitted wire key string-matches `agents.session_id.SESSION_METADATA_GRPC_KEY` (cross-language contract — assert the literal in a test, not just a Go-side constant).
* [ ] No emission on the dead `SendChatMessage` path.
* [ ] `channel.dispatch` span carries the resolved `session_id` attribute (low-cardinality-on-span, never a metric label — same posture as RFC 0031 OQ #7).

---

### PR 3: `feature/v035-issue0082-e2e-close` — End-to-End Isolation Test + Closeout

**Depends on**: PR 2 merged.
**Purpose**: Prove the property end-to-end through the **live gRPC path** (the persona-side units already cover their half) and land the documentation closeout.

#### Scope

| File | Change |
|------|--------|
| `tests/integration/test_session_emission_isolation.py` (new) | The ISSUE-0082 acceptance gate: two concurrent conversations for **one** agent — two senders in one channel (and/or two DM threads) — driven through the real orchestrator → gRPC → persona path, asserting each conversation recalls only its own writes (no cross-conversation bleed), while a pre-activation `legacy` row stays visible to both. The Go→Python wire proof that complements `tests/unit/python/test_principal_scope.py` + the PR 2 binding tests. |
| [`docs/rfcs/0031-per-session-namespacing-channels.md`](../rfcs/0031-per-session-namespacing-channels.md) | §B / §E amendment update: record that the orchestrator now **emits** `persatrix-session` per request from the persisted `(agent, channel, user)` binding — the "armed but not fed" gap is closed on the **session** axis. The principal axis stays armed-not-fed pending RFC 0039. |
| [`docs/issues/ISSUE-0082-…`](ISSUE-0082-orchestrator-per-request-session-principal-emission.md) | Notes entry: Part 1 (session emission) landed; Part 2 (principal) remains, gated on RFC 0039. Issue stays `open` until Part 2. |
| [`docs/issues/ISSUE-0081-…`](ISSUE-0081-session-id-process-global-not-task-local.md) | Notes entry: the session half of the activation is now fed; ISSUE-0081 stays `open` until ISSUE-0082 Part 2 (principal emission) lands, per its closeout contract. |
| [`ROADMAP.md`](../../ROADMAP.md) | Status hygiene refresh for the ISSUE-0082 session-emission work. |

No production code in PR 3 — test + docs only.

#### Key implementation details

* **The integration test is the gate a future change cannot silently regress.** It pins both halves of the §B intent: per-request isolation (conversation A ∦ conversation B for one agent) **and** the `legacy` carve-out's continued visibility (no pre-activation row stranded).
* **Scope discipline on the RFC amendment.** PR 3 updates the *status* of the existing §B/§E amendments (emission now landed); it does not re-open the session-unit decision or touch the principal amendments.

#### PR checklist

* [ ] `make test` passes; the new integration test runs in the full `tests/integration/` suite (gated in CI per [ISSUE-0076](ISSUE-0076-full-integration-suite-not-run-in-ci.md)).
* [ ] RFC 0031 §B/§E reflect that session emission landed; principal axis still flagged deferred.
* [ ] ISSUE-0082 Part-1 note added; issue stays `open` for Part 2.
* [ ] ISSUE-0081 note added; stays `open` until ISSUE-0082 Part 2.

---

## Risk and Mitigations

| Risk | Mitigation |
|------|------------|
| Activation changes default recall for **existing** single-conversation deployments (was: `legacy` snapshot; now: a real `(agent, channel, user)` session), which could read as "the persona forgot everything." | The §D `legacy` carve-out keeps every pre-activation row visible from every session; PR 3's integration test asserts the carve-out rows survive activation. No backfill, no stranded rows. |
| The emitted wire key drifts from the persona-side constant, silently disabling the rail (header present but never matched). | PR 2 asserts the literal `persatrix-session` against `agents.session_id.SESSION_METADATA_GRPC_KEY` in a test, not just a Go-side const; the binding is exercised end-to-end in PR 3. |
| Concurrent first-sight of one `(agent, channel, user)` triple double-mints two session ids (two rows, split recall). | PR 1 mints + inserts in one transaction with `ON CONFLICT DO NOTHING` + re-read; a concurrency test pins single-id resolution under N goroutines. SQLite single-writer makes the window narrow but not zero without the guard. |
| Wiring emission onto the dead `SendChatMessage` path couples this work to ISSUE-0035 cleanup. | PR 2 emits only on the live `Dispatch` chokepoint; the dead path is explicitly out of scope. |
| Session unit re-litigated mid-implementation. | The unit (`(agent, channel, user)`, authoritative + persisted) is already locked in [RFC 0031 §B amendment](../rfcs/0031-per-session-namespacing-channels.md#b-session-lifecycle); this plan executes it, it does not decide it. |

---

## Progress Overview

| # | Title | Branch | Status | GitHub PR | Merged |
|---|-------|--------|--------|-----------|--------|
| 1 | Per-request session binding store | `feature/v035-issue0082-session-source` | ✅ Merged | [#458](https://github.com/mkhomutov/Persatrix/pull/458) | ✅ |
| 2 | `persatrix-session` emission on the dispatch path | `feature/v035-issue0082-emit` | ✅ Merged | [#459](https://github.com/mkhomutov/Persatrix/pull/459) | ✅ |
| 3 | End-to-end isolation test + closeout | `feature/v035-issue0082-e2e-close` | 🔀 PR open | — | — |

**Status legend**: ⬜ Not started · 🔄 In progress · 🔀 PR open · ✅ Merged · ⏭ Deferred

---

## Future Work — Part 2: Principal Emission (gated on RFC 0039)

Out of scope for this plan; tracked here and in [ISSUE-0082](ISSUE-0082-orchestrator-per-request-session-principal-emission.md).

Once [RFC 0039](../rfcs/0039-user-accounts-authentication.md) lands authenticated accounts, the orchestrator emits the **verified principal** as the `persatrix-principal` header (matching [`agents.principal_id.PRINCIPAL_METADATA_GRPC_KEY`](../../agents/principal_id.py)) on the same dispatch chokepoint. The RFC 0039 §F verified-`participant_id` claim is the source; the persona-side principal rail (strict-equality tenant filter, `principal_scope` binding) is already merged in ISSUE-0081 PR 3 and resolves to `'local'` until that source exists. Until then the storage layer correctly collapses to the single-tenant `'local'` principal — no cross-tenant surface ships before the verified source does.

---

## Related Documentation

* [ISSUE-0082 — Orchestrator per-request session/principal emission](ISSUE-0082-orchestrator-per-request-session-principal-emission.md) — the issue this plan's Part 1 closes (session axis).
* [ISSUE-0081 — Session id process-global, not task-local](ISSUE-0081-session-id-process-global-not-task-local.md) — the Python vertical this activates; stays open until Part 2.
* [RFC 0031 §B/§E amendments](../rfcs/0031-per-session-namespacing-channels.md#b-session-lifecycle) — the session-unit decision (`(agent, channel, user)`, authoritative + persisted) this plan executes.
* [RFC 0039 — User Accounts & Authentication](../rfcs/0039-user-accounts-authentication.md) — gates Part 2 (principal emission).
* [ISSUE-0035 — dead-but-wired chat executor](ISSUE-0035-chat-executor-dead-but-wired-cleanup.md) — why emission targets only the live `Dispatch` path.
* [`internal/observability/grpcmeta`](../../internal/observability/grpcmeta/grpcmeta.go) — the outbound `persatrix-*` metadata pattern PR 2 extends.
* [BRANCHING.md](../BRANCHING.md) — branching / squash-merge convention.
