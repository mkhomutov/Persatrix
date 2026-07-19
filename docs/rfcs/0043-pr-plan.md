# RFC 0043 — PR Implementation Plan (Inbound Agent-Interop Endpoint)

**RFC**: [0043-inbound-agent-interop-endpoint.md](0043-inbound-agent-interop-endpoint.md)
**Branch prefix**: `feature/v04x-rfc0043-`
**Target**: `main`
**Merge strategy**: Squash merge per [BRANCHING.md](../BRANCHING.md)
**Master plan**: TBD — the v0.4.0 plan is not yet open; this plan slots in when it lands.

> ⚠️ **SKELETON — not yet actionable.** Drafted ahead of RFC acceptance so the shape is
> visible. RFC 0043 is **🔨 Draft**; the dedicated security review is the Phase-1b/1c gate and
> the leave-Draft checklist ([RFC §Decision](0043-inbound-agent-interop-endpoint.md#decision--next-steps))
> must resolve first. **Phase 1a carries no unshipped-RFC dependency** and may proceed once the RFC
> reaches Proposed. **Phase 1b hard-depends on RFC 0039 Phases 1–2** (0039 is 📋 Proposed with zero
> implementation and no PR plan; its Phase 3 — account administration — is not on this path). **Phase 1c additionally depends on OQ 1 (listener topology) and the
> RFC 0012 §G admission gate.** Sizes are calibrated estimates; PR numbers, merge dates, and checklists
> are placeholders.

---

## Overview

RFC 0043 adds a bounded HTTP/JSON inbound endpoint and a third participant-type value (`external_agent`) that let a non-Persatrix agent join a channel — post, be addressed, and observe authorized traffic — over a surface disjoint from the internal gRPC contract ([RFC 0040](0040-agent-orchestrator-transport-unification.md), untouched). An external participant is a **real channel member** (a `RespondNever` `memberships` row, reusing the chat-as-DM precedent), so it inherits membership, history-scoping, and audit machinery rather than duplicating it.

The RFC ships in **phases split by dependency readiness** (the [RFC 0042](0042-pr-plan.md) 1a/1b precedent): **Phase 1a** is additive work on shipped subsystems and is buildable today; **Phase 1b** waits on the credential substrate ([RFC 0039](0039-user-accounts-authentication.md) Phases 1–2 + the [RFC 0009](0009-security-sandboxing.md) Phase 4 token model); **Phase 1c** (outbound) additionally waits on the listener-topology decision and the [RFC 0012](0012-protocols-organizations.md) admission gate. This plan splits the work into **15 PRs**, mirroring the [RFC 0040](0040-pr-plan.md) PR-plan structure (Style A: skeleton + Phase 0 Hard Gate). Each PR leaves the repo passing-tests, lint-clean, and within the [BRANCHING.md](../BRANCHING.md) < 500-line size guidance.

> **Estimate calibration**: prior umbrella RFCs (0040/0041/0042) landed near their estimates within a ~1.7× factor. This plan applies the same factor. Sizes below are calibrated estimates.

**Prerequisites**: RFC 0002 (REST API Server — Implemented, the host), RFC 0011 (Channels — Implemented), RFC 0016 (Participant model — Implemented), RFC 0030 (Governance — Implemented), RFC 0035 (Membership Interval Ledger — Implemented). **Shipping gates** (not yet met): RFC 0039 Phases 1–2, RFC 0009 Phase 4 (credential model), RFC 0012 §G (admission gate).

**Recommended merge order**: **PR 1 → 2 → 3 → 4 → 5** (Phase 1a, unblocked) → *[credential substrate ships]* → **PR 6 → 7 → 8 → 9** (Phase 1b) → *[OQ 1 + admission gate resolve]* → **PR 10 → 11 → 12** (Phase 1c) → **PR 13 → 14 → 15** (close-out). The only hard intra-plan ordering constraints are the store (PR 2/4/5) before the handlers that use it (PR 8+), and the dispatcher multiplexer (PR 10) before long-poll drain (PR 11).

---

## Phase 0 Hard Gate

RFC 0043 is **🔨 Draft** with decisions that are **non-additive once code ships**. Phase 1b/1c PRs do **not** open until these resolve in the RFC review thread and the RFC advances. (Phase 1a PRs carry no unshipped-RFC dependency — they may proceed once the RFC is Proposed.)

| # | Blocker (RFC ref) | Why it gates | Recommended resolution |
|---|-------------------|--------------|------------------------|
| G1 | **Credential track** — [§C](0043-inbound-agent-interop-endpoint.md#c-auth-identity-and-credentials), NEW-1 | RFC 0039's Non-Goal disclaims agent-attributable REST ingress; an 0039 account does not fit a machine principal | **Resolved in RFC**: HTTP variant of the RFC 0009 Phase 4 `AgentCapabilityToken` (HMAC, short-TTL, capability-scoped) + denylist revocation. Confirm at security review. Blocks all of 1b. |
| G2 | **Membership model** — [§D](0043-inbound-agent-interop-endpoint.md#d-capability-scope-and-membership), NEW-3 | Determines the data model, the store migration, and whether inbound publish works at all (`ErrNotMember` is enforced in-transaction) | **Resolved in RFC**: real `RespondNever` `memberships` row; scope is a refinement. Blocks PR 4. |
| G3 | **ID grammar** — [§A](0043-inbound-agent-interop-endpoint.md#a-the-externalagentparticipant-participant-type) | `ext:` is rejected at four validators; `ext-` passes all unchanged | **Resolved in RFC**: `ext-<account_id>`. Blocks PR 1. |
| G4 | **Outbound seam** — [§F](0043-inbound-agent-interop-endpoint.md#f-message-inbound--outbound-flow) | No subscriber registry exists; the router holds a single `dispatcher` field; the cited RFC 0041 subscriber is Python-side and never lands on the Go path | **Resolved in RFC**: dispatcher multiplexer + `queueDispatcher` in `internal/channels`. New machinery. Blocks PR 10. |
| G5 | **Listener topology + channel-surface auth** — [OQ 1](0043-inbound-agent-interop-endpoint.md#open-questions) | Non-additive (ports, certs, ops). Same-listener is unsound while `/api/v1/channels/*` is unauthenticated *by design* today | **Open**: requires RFC 0039 Phase 2 to cover the channel surface, or a dedicated listener that does not route into the internal mux. Blocks all of 1c. |
| G6 | **External-admission owner** — [§Security](0043-inbound-agent-interop-endpoint.md#security-considerations), NEW-2 | RFC 0037's total-order lattice cannot express it and disclaims both gates | **Resolved in RFC**: `external_participants_allowed` (default false), enforced by the RFC 0012 §G membership-time gate. Sequencing cost: 0012 is v0.4.0. Blocks the security review. |
| G7 | **Storage home + RFC 0029 tier** — [§E](0043-inbound-agent-interop-endpoint.md#e-storage-and-provisioning), NEW-4 | No store exists; a `channels.db` addition becomes RFC 0029 Phase 3 `migrate` input | **Open**: `channels.db` vs. a dedicated DB; pick the migration number (next at PR time — RFC 0037's v0.3.12 migration is projected to take v11 first) and the RFC 0029 tier. Blocks PR 2/4/5. |
| G8 | **RFC 0039 Phases 1–2 shipped** *(shipping dependency, not an OQ)* | No account/session/role/enforcement substrate exists today (Phase 3 — account administration — is not on this path: the credential is an RFC 0009-track token, not an 0039 account) | Ship 0039 Phases 1–2 first. Blocks all of 1b. |

**Not blocking**: OQ 2 (→ Phase 3), OQ 3 (resolved — at-least-once folded into the cursor), OQ 4 (answerable now — `ModelOutput` only + externally-meaningful `Error`), OQ 5 (behaviour choice; extend to operator surfaces).

---

## Progress Overview

| PR | Title | Phase | Version | Depends on | Est. size | Status |
|----|-------|-------|---------|------------|-----------|--------|
| 1 | Participant-type vocabulary + `ext-` ID grammar | 1a | v0.4.x | G3 | ~150–250 | ⬜ Not started |
| 2 | `internal/extagents/` participant + `CapabilityScope` models (ID-keyed) | 1a | v0.4.x | G7 | ~250–350 | ⬜ Not started |
| 3 | Config loader + `external_agent.schema.json` + `_SCHEMA_MAP` | 1a | v0.4.x | PR 2 | ~300–400 | ⬜ Not started |
| 4 | Scope/grant store + membership reconciliation + `channels.db` migration | 1a | v0.4.x | PR 2, G2, G7 | ~350–450 | ⬜ Not started |
| 5 | Idempotency store (scope, caps, sweep, replay) | 1a | v0.4.x | PR 2 | ~300–400 | ⬜ Not started |
| 6 | HTTP bearer-validation middleware + principal resolution + denylist | 1b | v0.4.x | G1, G8, PR 4 | ~300–450 | ⬜ Blocked (Phase 0) |
| 7 | Per-participant tiered rate limiter (new subsystem) | 1b | v0.4.x | PR 6 | ~300–450 | ⬜ Blocked |
| 8 | `POST .../messages` + capability check + audit emission | 1b | v0.4.x | PR 6, PR 7, PR 5 | ~350–500 | ⬜ Blocked |
| 9 | `GET /identity` + `GET /channels` | 1b | v0.4.x | PR 6 | ~150–250 | ⬜ Blocked |
| 10 | Dispatcher multiplexer + bounded per-participant queue + `queueDispatcher` | 1c | v0.4.x | G4, G5, PR 4 | ~350–500 | ⬜ Blocked |
| 11 | Long-poll drain + `event_id` cursor contract + history-on-join | 1c | v0.4.x | PR 10 | ~350–500 | ⬜ Blocked |
| 12 | New `AuditEventType` kinds + client-IP/UA capture + trusted-proxy config + `external_participants_allowed` enforcement | 1c | v0.4.x | PR 8, PR 11, G6 | ~300–450 | ⬜ Blocked |
| 13 | Integration + E2E + **MT-EXTAGENT-001** | close | v0.4.x | PR 12 | ~300–450 | ⬜ Blocked |
| 14 | SSE stream + operator CLI + **MT-EXTAGENT-002** *(RFC Phase 2)* | 2 | v0.4.x | PR 11, PR 13 | ~350–500 | ⬜ Blocked |
| 15 | Observability counters + full-RFC status hygiene | close | v0.4.x | PR 13 | ~150–300 | ⬜ Blocked |

---

## Dependency Graph

```
Phase 1a — additive on shipped subsystems, no unshipped-RFC dependency        [v0.4.x]
PR 1 (participant vocab + ext- grammar)
  ↓
PR 2 (extagents models, ID-keyed) ──→ PR 3 (config + schema + _SCHEMA_MAP)
  ↓                               └──→ PR 5 (idempotency store)
PR 4 (scope/grant store + membership reconcile + channels.db migration)
· · · · · [Phase 0 Hard Gate: G1/G8 credential substrate ships; G5 listener + G6 admission resolve] · · · · ·
Phase 1b — credential + inbound
PR 6 (bearer middleware + denylist) → PR 7 (tiered limiter) → PR 8 (POST + capability + audit)
                                    └────────────────────────→ PR 9 (GET identity/channels)
Phase 1c — outbound
PR 10 (dispatcher multiplexer + queue) → PR 11 (long-poll drain + cursor + history-on-join)
                                        → PR 12 (audit kinds + IP capture + admission enforcement)
Close-out
PR 13 (integration + E2E + MT-EXTAGENT-001) → PR 14 (SSE + operator CLI) 
                                            → PR 15 (observability + status hygiene → advance status)
```

Phase 1a (PRs 1–5) is fully unblocked and reviewable in isolation with no auth surface. The Phase 0 Hard Gate barrier sits between 1a and 1b. PR 10 (outbound seam) is the other new-machinery focal point and gates PR 11.

---

## PR Sequence

### PR 1: `feature/v04x-rfc0043-participant-vocab` — Participant-Type Vocabulary + `ext-` ID Grammar

**Depends on**: G3 resolved (`ext-<account_id>`). No unshipped-RFC dependency — may proceed once RFC 0043 is Proposed.
**Purpose**: Add the third participant-type value across the four hand-mirrored allowlists and handle the silent clamp. Implements [RFC §A](0043-inbound-agent-interop-endpoint.md#a-the-externalagentparticipant-participant-type).

#### Scope

| File | Change |
|------|--------|
| [`internal/channels/participant_type.go`](../../internal/channels/participant_type.go) | Add `external_agent` to `validParticipantTypes` |
| [`agents/participant.py`](../../agents/participant.py) | Add `external_agent` to `VALID_PARTICIPANT_TYPES` |
| [`agents/persona_runtime/record_close.py`](../../agents/persona_runtime/record_close.py) | Handle `external_agent` in the peer-type clamp deliberately (ISSUE-0068 defect class) |
| [`agents/memory/relationship_queries.py`](../../agents/memory/relationship_queries.py) | Accept `external_agent` in `validate_participant_types` |

#### Tests

- Extend the pinned conformance tests: `internal/channels/participant_type_test.go`, `internal/server/chat_handler_participant_type_test.go`, `tests/unit/python/test_participant.py` (currently pins the 2-value frozenset — updates to 3), and the proto round-trip test.
- Assert the clamp no longer degrades `external_agent` to `agent`.

#### PR checklist

- [ ] `make test` / `make lint` / `make validate` pass
- [ ] All four allowlists updated in lockstep; no proto change
- [ ] Clamp behaviour asserted
- [ ] ROADMAP + RFC status hygiene

### PR 2: `feature/v04x-rfc0043-extagents-models` — `internal/extagents/` Models

**Depends on**: G7 (storage home decided). **Purpose**: The `ExternalAgentParticipant` + `CapabilityScope` types, keyed by **canonical channel ID**, with the `ChannelsWrite ⊆ ChannelsRead` invariant. Implements [RFC §A/§D](0043-inbound-agent-interop-endpoint.md#d-capability-scope-and-membership). ~250–350 lines.

### PR 3: `feature/v04x-rfc0043-config-schema` — Config Loader + JSON Schema

**Depends on**: PR 2. **Purpose**: `schemas/external_agent.schema.json` (`.schema.json` suffix), `agents/validate.py` `_SCHEMA_MAP` registration, `config/external_agents.yaml` reconciled-seed loader (the RFC 0050 single-source-of-truth idiom). Schema-guard test. ~300–400 lines.

### PR 4: `feature/v04x-rfc0043-store-membership` — Scope/Grant Store + Membership Reconciliation

**Depends on**: PR 2, G2, G7. **Purpose**: `channels.db` migration at the next version at PR time (or dedicated DB — G7); invitation writes a `RespondNever` `memberships` row; revocation closes the RFC 0035 interval. Tests assert a `membership_intervals` stint opens, publish succeeds, fanout does not dispatch (the chat-as-DM precedent). ~350–450 lines.

### PR 5: `feature/v04x-rfc0043-idempotency` — Idempotency Store

**Depends on**: PR 2. **Purpose**: `(account_id, key, request_hash, message_id, created_at)` with `UNIQUE(account_id, key)`; key-length cap (mirror the 256-byte `MaxAgentIDLen`); per-participant LRU cap; 24h sweep; `200`-with-original-`message_id` on replay; `409` on same-key-different-body. Best-isolated Phase 1a piece. ~300–400 lines.

### PR 6: `feature/v04x-rfc0043-bearer-middleware` — HTTP Bearer Validation + Denylist

**Depends on**: G1, G8 (RFC 0039 Phases 1–2 + credential track), PR 4. **Purpose**: HTTP variant of the RFC 0009 Phase 4 `AgentCapabilityToken` (HMAC, short-TTL, capability-scoped), the validation middleware on `/external/*` (resolves principal into request context), and the denylist revocation (RFC 0009 OQ 4). Live-stream re-check hook. ~300–450 lines.

### PR 7: `feature/v04x-rfc0043-tiered-ratelimit` — Per-Participant Tiered Rate Limiter

**Depends on**: PR 6. **Purpose**: New subsystem — tiered (per-second/minute/hour) limiter keyed **server-side from the validated token, never `X-Agent-ID`**, with its own per-participant LRU pool so an external flood cannot evict internal agents' rate state. ~300–450 lines.

### PR 8: `feature/v04x-rfc0043-post-messages` — Inbound Publish

**Depends on**: PR 6, PR 7, PR 5. **Purpose**: `POST .../messages` → capability check → rate-limit → idempotency → `publishCommit` with **server-set** `SenderID` and `Metadata["participant_type"]="external_agent"`; audit emission. ~350–500 lines.

### PR 9: `feature/v04x-rfc0043-identity-list` — Identity + Channel List

**Depends on**: PR 6. **Purpose**: `GET /identity` (own id + scope) and `GET /channels` (own invitation set — not discovery). ~150–250 lines.

### PR 10: `feature/v04x-rfc0043-dispatcher-mux` — Dispatcher Multiplexer + Queue

**Depends on**: G4, G5, PR 4. **Purpose**: The router's single `dispatcher` becomes a composite multiplexer fronting `GRPCMessageDispatcher` + a new `queueDispatcher`; bounded per-participant in-memory queue (drop-oldest + counter, never blocks fanout — mirror the `logbuffer` Config shape). New machinery. ~350–500 lines.

### PR 11: `feature/v04x-rfc0043-longpoll-cursor` — Long-Poll Drain + Cursor

**Depends on**: PR 10. **Purpose**: `GET .../messages?since=` drain; monotonic per-channel `event_id` cursor (new sequence column in the PR 4 migration); empty-timeout → `200` + unchanged cursor; over-cap → `503` with existing connections intact; history-on-join via `GetHistoryScoped` (invite-time-forward). ~350–500 lines.

### PR 12: `feature/v04x-rfc0043-audit-admission` — Audit Kinds + IP Capture + Admission

**Depends on**: PR 8, PR 11, G6. **Purpose**: New `external_agent.read/.publish/.denied` `AuditEventType` constants (three-site enum edit or the severity-classification test fails); client-IP/User-Agent capture + trusted-proxy policy; per-delivery message-id auditing; `external_participants_allowed` enforcement at the membership write path (RFC 0012 §G). ~300–450 lines.

### PR 13: `feature/v04x-rfc0043-tests` — Integration + E2E + MT

**Depends on**: PR 12. **Purpose**: invite→post→receive round-trip; revocation→401 + live-stream termination; over-scope 403; over-rate 429; over-poll-cap 503; delete→recreate→denied ([OQ 7](0043-inbound-agent-interop-endpoint.md#open-questions)); **MT-EXTAGENT-001** (modeled on MT-CHAT-001 + MT-CHANNEL-006). ~300–450 lines.

### PR 14: `feature/v04x-rfc0043-sse-cli` — SSE + Operator CLI *(RFC Phase 2)*

**Depends on**: PR 11, PR 13. **Purpose**: SSE stream sharing the Phase-1c queue; `cli/src/commands/external_agent.rs` invite/revoke/list/scope/audit; **MT-EXTAGENT-002** (modeled on MT-CHANNEL-001). *Note*: plan the CLI files (`external_agent.rs` / `external_agent_dispatch.rs` / `external_agent_tests.rs`) from the start against the 500-line cap; `cli/tests/` does not exist and `mockito` is not yet a dev-dependency. ~350–500 lines.

### PR 15: `feature/v04x-rfc0043-close` — Observability + Status Hygiene

**Depends on**: PR 13. **Purpose**: `internal/observability/metrics/` counters (requests by outcome, capability denials, rate-limit rejections, long-poll connections/cap-rejections, queue depth/drops, egress volume); advance RFC status; regenerate INDEX via `make rfcs`. ~150–300 lines.

---

## Open Items Carried From the RFC

Resolve before the corresponding phase (see [RFC §Open Questions](0043-inbound-agent-interop-endpoint.md#open-questions)):

- **OQ 1 / G5** — listener topology (same-listener needs RFC 0039 Phase 2 over `/api/v1/channels/*`, else a dedicated listener). Blocks PR 10.
- **G7 / NEW-4** — storage home + migration number + RFC 0029 tier. Blocks PR 2/4/5.
- **G6 / NEW-2** — the RFC 0012 §G admission gate must exist before any external credential is issued. Blocks the security review and PR 12.
- **NEW-5** — memory-poisoning policy (provenance-tag / exclude-from-fact-extraction / operator forget-path). Security-review decision; shapes PR 8/12.
- **NEW-6** — egress budget + per-delivery auditing. Shapes PR 11/12.
- **NEW-7** — client-IP capture + trusted-proxy policy. Shapes PR 12.

**File-size headroom**: `internal/server/*.go` and the CLI files are the ones to watch against the 500-line code cap (`scripts/checks/file_size.py --strict`); `cli/src/commands/channel_dispatch.rs` is already at 499/500, so the external-agent CLI must start split.
