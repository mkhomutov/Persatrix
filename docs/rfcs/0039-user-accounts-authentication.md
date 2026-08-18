---
id: RFC-0039
title: User Accounts & Authentication
summary: Human user accounts with password login, opaque revocable sessions, and a coarse operator/user role gate on the REST surface — so the caller's participant_id becomes a verified claim instead of an unverified request parameter, and the unauthenticated REST API (RFC 0002) gains a foundation that IdP federation, MFA, and RFC 0012 organizational clearance extend cleanly.
type: architecture
status: partially_implemented
author: Maksim Khomutov
created: 2026-05-16
target: v0.3.12 (Phases 1–2) + v0.4.0 (Phase 3)
depends_on:
  - RFC-0002
  - RFC-0016
---

# RFC 0039 — User Accounts & Authentication

**Type**: architecture
**Status**: ⚠️ Partially Implemented — **Phases 1–2 shipped in v0.3.12** (PRs [#779](https://github.com/mkhomutov/Persatrix/pull/779) accounts store, [#780](https://github.com/mkhomutov/Persatrix/pull/780) sessions, [#790](https://github.com/mkhomutov/Persatrix/pull/790) REST + middleware + [amendment](0039-amendment-enabled-mode-exposure.md) browser posture, [#791](https://github.com/mkhomutov/Persatrix/pull/791) bootstrap + CLI — Phase 1 complete, inert, [#793](https://github.com/mkhomutov/Persatrix/pull/793) Phase 2 enforcement + verified claim + console login; per the [PR plan](0039-pr-plan.md)): enforcement is opt-in behind `auth.mode: enabled`, the shipped default stays `disabled`. Phase 3 (account administration REST API, self-service password change, failed-login lockout) targets v0.4.0. Operator surface: [auth guide](../guides/auth.md)
**Author**: Maksim Khomutov
**Date**: 2026-05-16
**Target**: v0.3.12 (Phases 1–2) + v0.4.0 (Phase 3)
**Depends on**: RFC 0002 (REST API Server — the surface this RFC authenticates), RFC 0016 (Human Participant & Chat Interface — the `UserParticipant` an account binds to)
**Relates to**: RFC 0009 (Agent Identity, Security & Sandboxing — the *agent* identity axis this RFC is the human counterpart of; the `AuditLogger` and `RateLimiter` it reuses), RFC 0012 (Protocols & Organizations — organizational clearance attaches to an account; the §I extension seam), RFC 0037 (Memory Confidentiality & Channel Classification — its confidentiality model presupposes a verified human identity), RFC 0001 (Core Orchestration Pipeline — the orchestrator `Store` pattern)

---

## Table of Contents

- [Summary](#summary)
- [Motivation](#motivation)
- [Goals](#goals)
- [Non-Goals](#non-goals)
- [Design / Implementation](#design--implementation)
  - [A. Account vs. participant: two identities, one binding](#a-account-vs-participant-two-identities-one-binding)
  - [B. The account & session model](#b-the-account--session-model)
  - [C. Password authentication](#c-password-authentication)
  - [D. Sessions and the bearer-token gate](#d-sessions-and-the-bearer-token-gate)
  - [E. The auth middleware and the role gate](#e-the-auth-middleware-and-the-role-gate)
  - [F. The verified `participant_id` claim](#f-the-verified-participant_id-claim)
  - [G. Bootstrapping the first operator](#g-bootstrapping-the-first-operator)
  - [H. Rollout: `auth.mode` and non-breaking enforcement](#h-rollout-authmode-and-non-breaking-enforcement)
  - [I. Extension points](#i-extension-points)
  - [J. CLI surface](#j-cli-surface)
  - [K. REST surface summary](#k-rest-surface-summary)
- [Security Considerations](#security-considerations)
- [Phased Implementation Plan](#phased-implementation-plan)
- [Files Touched (Estimated)](#files-touched-estimated)
- [Test Strategy](#test-strategy)
- [Open Questions](#open-questions)
- [Decision / Next Steps](#decision--next-steps)
- [Related Documentation](#related-documentation)

---

## Summary

Persatrix has no concept of a human **account**. The REST API surface
([RFC 0002](0002-rest-api-server.md)) is entirely unauthenticated, and
the `user_id` a caller hands to the chat endpoint
([RFC 0016](0016-human-participant-chat-interface.md)) is taken on trust
— any caller can claim to be any user.

This RFC introduces the **foundation**:

1. A human **account** — username, a password credential, a role, and a
   bound `participant_id` — persisted in a durable, orchestrator-side
   store (`accounts.db`), independent of any persona's `memory.db`.
2. **Password login** that verifies the credential against a memory-hard
   KDF hash and issues an **opaque session token**.
3. **Sessions** that are server-side, expiring, and **revocable**; only a
   hash of the token is stored, never the token itself.
4. An **auth middleware** on the REST server that resolves
   `Authorization: Bearer <token>` to a verified identity and enforces a
   per-route requirement — `public`, `authenticated`, or `operator`.

Because an account binds to exactly one RFC 0016 `UserParticipant`, once
a request is authenticated the caller's **`participant_id` is a verified
claim**, not an unverified body parameter — which closes
[RFC 0016 Security Consideration #2](0016-human-participant-chat-interface.md#security-considerations)
and gives every later RFC a trustworthy human identity to build on.

The design is deliberately a foundation. Password is **one pluggable
authentication method** behind an `Authenticator` interface; the role is
a **coarse two-level gate**, not a policy engine; and the account ↔
participant binding is the **seam** where [RFC 0012](0012-protocols-organizations.md)
organizational clearance later attaches (§I). A non-breaking
`auth.mode` switch ships the mechanism inert before any deployment opts
into enforcement (§H).

## Motivation

### The dangling pointer in the RFC graph

Authentication has been promised twice and delivered by neither RFC:

- **RFC 0002 §Non-Goals** deferred it explicitly: *"No auth in v0.1 …
  Auth is deferred to a security RFC,"* and *"A dedicated security RFC
  will add Bearer token authentication, per-agent API keys, or mTLS
  before any production deployment."* This is that RFC.
- **RFC 0016 §Non-Goals** deferred user identity to *"RFC 0009
  (v0.3.0)"*, and RFC 0016 §Security #2 repeats it: *"When
  network-accessible multi-user deployments arrive, RFC 0009 identity
  tokens will gate this endpoint."* But [RFC 0009](0009-security-sandboxing.md)
  shipped **agent** identity — HMAC capability tokens for the
  agent↔orchestrator **gRPC** path — not human-user authentication.
  RFC 0009's own §Non-Goals scope it to agents and call multi-tenant
  isolation out entirely. The pointer RFC 0016 planted was never
  resolved by RFC 0009, because RFC 0009 was never about humans.

The result is a real hole. The one operator-only REST endpoint that
exists today — `POST /api/v1/agents/{id}/unquarantine` — needed a
hand-rolled single shared secret (`SECURITY_UNQUARANTINE_TOKEN`,
[`agent_handlers.go`](../../internal/server/agent_handlers.go)) as a
*"defense-in-depth stop-gap"* precisely because there is no account
system. Its own code comment says it stands *"until token validation
lands in RFC 0009 Phase 4."* Every future operator-only endpoint would
otherwise grow the same one-off secret.

### What breaks if we do nothing

The project is actively building a confidentiality and authority model
on top of an unverified human identity:

- [RFC 0037](0037-memory-confidentiality-channel-classification.md)'s
  classification gate withholds `secret`-channel memory from a `public`
  channel's prompt. That boundary is sound for *what a persona may say*
  — but the human on the other side of it is whoever the request body
  claimed to be.
- [RFC 0012](0012-protocols-organizations.md)'s authority axis ranks a
  *directive* by the issuer's organizational role. A role is attached to
  a **principal**. There is no principal to attach it to until accounts
  exist.

Confidentiality and authority are being designed against a foundation
that simply assumes the human is who they say they are. That assumption
needs a mechanism under it.

### Account is not participant

RFC 0016 already models the human **inside the society**: a
`UserParticipant` ([`agents/participant.py`](../../agents/participant.py))
with a display name, relationships, and episodic history — *who this
person is, socially, to the agents.* What is missing is the human **as
an authenticated principal**: the credential that proves a connecting
client is entitled to act as that participant. An account is not a
participant; an account is the **authority to act as one**. This RFC
adds the second concept and binds it to the first (§A).

### Why a foundation, not the whole thing

Password login, opaque sessions, and a two-role gate are *enough* to
make the REST surface safe to expose and to hand every later RFC a
verified identity. Full role-based authorization, external identity
providers, MFA, and multi-tenancy are deliberately out of scope — each
extends this foundation cleanly rather than requiring it to be rebuilt
(§I). Shipping the foundation first, inert behind `auth.mode` (§H),
means the mechanism is reviewable and testable before any deployment
depends on it.

### Why Phases 1–2 target v0.3.x, and Phase 3 v0.4.0

This RFC has **no v0.4.0 dependency**. Its hard dependencies — RFC 0002
(the REST server) and RFC 0016 (`UserParticipant`) — shipped in v0.1 and
v0.2.1; the RFC 0009 `AuditLogger`, `SecretRedactor`, and `RateLimiter`
it reuses, and the `internal/channels` SQLite-migration discipline it
follows, all shipped in v0.3.0. Nothing it needs is in flight.

The decisive reason to land the **foundation in v0.3.x** is
[RFC 0037](0037-memory-confidentiality-channel-classification.md), which
is itself v0.3.x. RFC 0037 builds a confidentiality model that governs
what crosses a channel-classification boundary — but the *human* on one
side of that boundary stays spoofable until accounts exist. The verified
`participant_id` claim (§F) is the substrate RFC 0037 implicitly
assumes; it should not lag the confidentiality RFC that leans on it.
Phase 1 is inert by construction (`auth.mode` defaults to `disabled`,
§H), so slotting it into the active v0.3.x line carries no behavioural
risk, and Phase 2 — enforcement plus the verified claim — completes the
boundary RFC 0037 needs.

**Phase 3 (account administration & hardening) targets v0.4.0.** It is
the part that can lag without weakening any v0.3.x security control: a
remote account-management REST API and failed-login lockout are
operability, not the boundary. It also pairs naturally with
[RFC 0012](0012-protocols-organizations.md) — organizational clearance
attaches to a *human principal*, i.e. an account, and joins to memory
and channels through that account's `participant_id`. RFC 0012 only
needs accounts to **exist**, which they do from v0.3.x; landing Phase 3
in v0.4.0 keeps the administration surface next to the organizational
model that consumes it.

The identity model still lands coherently: agent identity
([RFC 0009](0009-security-sandboxing.md) Phases 3–4, v0.4.0), human
identity (this RFC — foundation in v0.3.x, administration in v0.4.0),
and organizational authority (RFC 0012, v0.4.0) compose in dependency
order; they need not all ship in one shared version.

## Goals

1. A human **account** — username, password credential, role, bound
   `participant_id`, and status — persisted in a durable orchestrator-side
   store, independent of any persona's `memory.db`.
2. **Password login** that verifies a credential against a memory-hard
   KDF hash and issues an **opaque session token**.
3. **Sessions** that are server-side, expiring, and **revocable**
   (logout, operator revoke); only a hash of the token is persisted.
4. An **auth middleware** on the REST server that resolves
   `Authorization: Bearer <token>` to a verified identity and enforces a
   per-route requirement of `public`, `authenticated`, or `operator`.
5. The chat endpoint's caller identity is the **verified
   `participant_id`** carried by the session, not the request body —
   closing RFC 0016 Security Consideration #2.
6. A **bootstrap path** for the first operator account that does not
   itself require authentication and cannot be turned into a takeover.
7. A **non-breaking rollout**: an `auth.mode` switch so the mechanism
   ships inert, then enforces, with no flag-day break of the existing
   local single-user workflow.
8. **CLI support** — `persatrix login` / `logout` / `whoami`, account
   management, and token-bearing requests from every existing command.
9. Clean **extension seams** for IdP federation, MFA, finer-grained
   authorization, and RFC 0012 organizational clearance — designed in,
   not built.
10. Login, logout, account mutation, and role-gate denials emit
    [RFC 0009](0009-security-sandboxing.md) `AuditLogger` events.

## Non-Goals

- **Agent identity / agent-transport authentication.** Authenticating
  the agent↔orchestrator gRPC channel is [RFC 0009](0009-security-sandboxing.md)
  (capability tokens, Phase 4). This RFC authenticates **humans** on the
  **REST** surface. The two are split on the same line RFC 0037 / RFC 0012
  split confidentiality from authority — each axis is enforceable on its
  own.
- **Fine-grained or resource-level authorization.** The role is a coarse
  two-level gate (`operator` / `user`). Per-action policy is
  [RFC 0028](0028-agent-decision-policy-engine.md); organizational
  authority and clearance are [RFC 0012](0012-protocols-organizations.md).
  This RFC is *authentication plus a coarse gate*, not an authorization
  engine.
- **Multi-tenancy / cross-tenant isolation.** Persatrix stays
  single-tenant ([RFC 0009 §Non-Goals](0009-security-sandboxing.md#non-goals)).
  Accounts are a *prerequisite* for tenancy but this RFC introduces no
  tenant boundary.
- **External identity providers** (OAuth, OIDC, SAML, SSO). Password is
  the only authentication method in the foundation; §I shows the seam an
  IdP plugs into.
- **MFA / TOTP / WebAuthn.** A hook point is noted (§I); no second
  factor is built.
- **A web or GUI login.** CLI and REST only. No browser surface, so no
  cookies, no CSRF, no session-management UI. *(**Superseded for the
  login/session surface** by the [enabled-mode exposure amendment](0039-amendment-enabled-mode-exposure.md),
  2026-07-25 — v0.3.12 ships the web-console login this excluded, so the
  amendment carries the cookie transport, the CSRF assertion, and the XSS
  posture. Session-**management** UI remains a Non-Goal until Phase 3.)*
- **Encrypting `accounts.db` at rest.** Credentials are *hashed*
  (passwords) and *hash-only* (session tokens); the database file itself
  is unencrypted, the same posture as `channels.db` and `memory.db`
  ([RFC 0037 §Non-Goals](0037-memory-confidentiality-channel-classification.md#non-goals)).
- **Authorizing agent-attributable REST ingress.** Agent
  self-registration (`POST /api/v1/agents/register`) and peer endpoints
  follow the RFC 0009 track, not this one.
- **Per-user isolation of persona memory.** Memory remains physically
  co-located across users ([RFC 0016 §Security #3](0016-human-participant-chat-interface.md#security-considerations));
  this RFC verifies *who is calling*, it does not partition *what they
  can see in memory*.

## Design / Implementation

### A. Account vs. participant: two identities, one binding

RFC 0016 already gives a human a place *inside* the agent society. This
RFC adds the human *at the door*. They are different objects with
different lifetimes, owners, and trust domains:

| | `UserParticipant` (RFC 0016) | `Account` (this RFC) |
|---|---|---|
| Answers | "who is this, socially, to the agents" | "is this connecting client entitled to act as that someone" |
| Lives in | a persona's `memory.db` (`users` table) | the orchestrator's `accounts.db` |
| Holds | display name, relationships, episodes | username, password hash, role, status, session references |
| Created by | first chat interaction | an operator (or the bootstrap path, §G) |
| Lifetime | as long as a persona remembers the person | as long as the credential is valid |

**The binding.** An account carries a `participant_id` — a documentary,
cross-store reference to the RFC 0016 participant the account is
*authorized to act as*. Authentication proves the **account**; the
system then acts as the bound **participant**. In the foundation the
mapping is **1:1** (one account, one participant) — and the 1:1 invariant
is **enforced in the schema** by a `UNIQUE` constraint on
`participant_id` (§B), not left to application code; relaxing it to the
1:many of Open Question #5 is a deliberate later migration that drops the
constraint. The `participant_id` is constrained to the existing
participant-ID regex (`^[a-z0-9][a-z0-9-]*[a-z0-9]$`) so it is a valid
RFC 0016 identity by construction.

Account creation validates `participant_id` against that regex but does
**not** require the participant to already exist — an RFC 0016
`UserParticipant` is created lazily on first chat interaction (table
above), so an operator routinely provisions the account before the
participant's first turn. Binding the id to the *intended* human is the
operator's responsibility at creation time; §F then trusts the bound id
as the verified claim.

**Why separate stores.** A password hash is not persona memory. It must
not live in `memory.db`: that file is per-persona, is being split along
a personal/society boundary by [RFC 0029](0029-personal-society-storage-split.md),
and is the wrong trust domain for a credential. Account state is
orchestrator-owned, single, and durable — `accounts.db` is the second
orchestrator-owned SQLite database after `channels.db`.

### B. The account & session model

A new `internal/accounts/` Go package owns a new SQLite database,
`accounts.db`, built with the same versioned-migration discipline as
[`internal/channels/sqlite_schema.go`](../../internal/channels/sqlite_schema.go):
a numbered `case` arm per migration, `user_version` stamped inside the
migration transaction, idempotent on reopen.

```sql
CREATE TABLE accounts (
    id             TEXT PRIMARY KEY,           -- account UUID
    username       TEXT NOT NULL UNIQUE,       -- login name; case-folded on write
    auth_method    TEXT NOT NULL DEFAULT 'password',  -- §I extension seam
    password_hash  TEXT,                       -- argon2id PHC string; NULL for non-password methods
    role           TEXT NOT NULL DEFAULT 'user',      -- 'operator' | 'user'
    participant_id TEXT NOT NULL UNIQUE,       -- 1:1 binding to the RFC 0016 UserParticipant (§A)
    status         TEXT NOT NULL DEFAULT 'active',    -- 'active' | 'disabled'
    created_at     INTEGER NOT NULL,
    updated_at     INTEGER NOT NULL
);

CREATE TABLE sessions (
    token_hash   TEXT PRIMARY KEY,             -- sha256(opaque token); the raw token is never stored
    account_id   TEXT NOT NULL REFERENCES accounts(id),
    issued_at    INTEGER NOT NULL,
    expires_at   INTEGER NOT NULL,
    last_used_at INTEGER NOT NULL,
    revoked_at   INTEGER                       -- NULL while live
);

CREATE INDEX idx_sessions_account ON sessions(account_id);
```

`role` and `auth_method` are **TEXT columns, not enums** — extensible by
the same allowlist-validated-string pattern RFC 0016 chose for
`participant_type` (a module-level `frozenset` / Go `map` validated at
the write boundary). A new role or a new authentication method is a
one-line allowlist change, no migration.

**Foreign-key enforcement.** SQLite ignores a `REFERENCES` clause unless
`PRAGMA foreign_keys = ON` is set on the connection. The
`internal/accounts/` store sets the pragma on every connection it opens,
so `sessions.account_id → accounts.id` is enforced rather than
decorative — a session row cannot be orphaned from its account. Accounts
are never *deleted* (only `disabled`, §K), so the reference needs no
`ON DELETE` cascade.

**Why SQLite, not the RFC 0001 `Store`.** The orchestrator state store
is explicitly **in-memory** ([RFC 0001](0001-core-orchestration-pipeline.md)).
Account credentials and live sessions must survive a restart, so they
need durable storage. `accounts.db` follows the `internal/channels/`
precedent rather than waiting on a persistent rewrite of the state
store.

### C. Password authentication

Password hashing uses **Argon2id** (`golang.org/x/crypto/argon2`, a new
direct dependency — Open Question #3):

- A per-account **16-byte random salt** from `crypto/rand`.
- Cost parameters (memory, iterations, parallelism) read from config
  (§H) and **encoded into the stored PHC hash string**, so a later
  parameter change does not invalidate existing hashes — a login that
  succeeds against an out-of-date parameter set triggers a transparent
  **verify-then-rehash** that re-stores the credential at current cost.
- Verification is **constant-time** (the KDF comparison is over
  fixed-size derived keys).

**Account-existence non-disclosure.** When the supplied username matches
no account, login still computes a hash against a fixed dummy PHC string
before returning, so a missing account and a wrong password are
indistinguishable by response timing — and both return the identical
`401`.

Passwords are **never logged**. The login request DTO routes through the
existing `SecretRedactor` ([RFC 0009 §I](0009-security-sandboxing.md#i-secret-redaction)),
whose `generic-secret` pattern already covers `password`-keyed fields;
this RFC verifies that coverage rather than adding a sink.

### D. Sessions and the bearer-token gate

On a successful login the server mints a **256-bit opaque token** from
`crypto/rand`, base64url-encoded. The raw token is returned to the
client **once, in the login response body**; the server persists only
`sha256(token)` as the `sessions` primary key. *(**Amended 2026-07-25**
— the [enabled-mode exposure amendment](0039-amendment-enabled-mode-exposure.md)
§A1 adds a `session_transport` field to login: `"bearer"` (the default)
keeps this body-token contract byte-for-byte; `"cookie"` returns **no
body token** and sets an `__Host-` `HttpOnly`/`Secure`/`SameSite=Strict`
cookie instead, so the console's session never enters JS.)*

A subsequent request presents `Authorization: Bearer <token>`. The
middleware hashes the supplied token, looks up the session row by
`token_hash`, and rejects unless the session is unexpired
(`expires_at`), unrevoked (`revoked_at IS NULL`), and the bound account
is `active`.

This generalizes the discipline already proven in
[`validBearerToken`](../../internal/server/agent_handlers.go) under
ISSUE-0004: both comparison inputs are reduced to fixed-size SHA-256
digests before `crypto/subtle.ConstantTimeCompare`, so neither the
content nor the length of a stored token can leak through response
timing. Storing only the hash means a read of `accounts.db` yields **no
usable live session** — the same reason ISSUE-0004 hashes the
unquarantine secret.

Sessions have a configurable TTL (§H); `logout` sets `revoked_at`; an
operator can revoke (Phase 3). Because sessions are **server-side**, no
separate revocation list is needed — the existence check is the lookup
that already happens on every request.

`last_used_at` is refreshed **lazily**: the middleware writes it only
once it has gone more than a coarse threshold stale (a few minutes), not
on every request. SQLite is single-writer, so refreshing it per request
would serialize an `UPDATE` transaction onto the hot authentication path
for no functional gain — `last_used_at` feeds session-list display and
idle reporting, neither of which needs per-request precision.

Expired and revoked rows accumulate. The session store **prunes** them —
a sweep on store open plus a periodic `DELETE FROM sessions WHERE
expires_at < :now OR revoked_at IS NOT NULL` — so the table tracks live
sessions rather than growing without bound. Pruning is a maintenance
`DELETE` off the request path; the per-request lookup stays a single
primary-key hit on `token_hash` regardless of how many sessions have
ever been issued.

```mermaid
sequenceDiagram
    participant CLI as Rust CLI
    participant MW as authMiddleware
    participant H as REST handler
    participant DB as accounts.db

    CLI->>MW: POST /api/v1/auth/login {username, password}
    MW->>H: (public route — no token required)
    H->>DB: load account, verify argon2id hash
    H->>DB: INSERT session (token_hash = sha256(token))
    H-->>CLI: 200 {token, expires_at, participant_id, role}
    Note over CLI: token written to ~/.persatrix/credentials (0600)

    CLI->>MW: GET /api/v1/auth/whoami  Authorization: Bearer <token>
    MW->>DB: lookup session by sha256(token); check expiry/revocation/account status
    alt valid session
        MW->>H: request + verified identity in context
        H-->>CLI: 200 {participant_id, role, ...}
    else missing / invalid / expired / revoked
        MW-->>CLI: 401 UNAUTHORIZED
    end
```

### E. The auth middleware and the role gate

A new `authMiddleware` is added to `internal/server/`. It is composed
**after** the existing `recovery` → `logging` → `requestID` middleware
([RFC 0002 §Access Logging Middleware](0002-rest-api-server.md#access-logging-middleware))
and **before** the mux, so every route is resolved through it. It
attaches the resolved identity to the request `context` under an
unexported `contextKey` — the same key-typing pattern RFC 0002
established for the request ID. *(**Amended 2026-07-25** — per the
[enabled-mode exposure amendment](0039-amendment-enabled-mode-exposure.md):
identity resolves bearer-first, cookie-second (§A1); a
**cookie**-resolved identity on a non-`GET`/`HEAD`/`OPTIONS` request
must pass a same-origin assertion or is `403`-rejected, while
bearer-resolved requests skip the check (§A2); and the login limiters
add `429` to the status matrix (§B4).)*

Every route declares one **policy**:

| Policy | Meaning |
|--------|---------|
| `public` | No identity required (`/healthz`, `POST /api/v1/auth/login`). |
| `authenticated` | Any valid session (`whoami`, `logout`, chat, change-own-password). |
| `operator` | A valid session whose account role is `operator` (account administration, `unquarantine`). |

The policies form a total order — `operator` ⊃ `authenticated` ⊃
`public`. The middleware **fails closed**: a route absent from the
policy map is treated as `operator`, the most restrictive level, so a
newly added handler is never accidentally world-open. On an
auth-required route, a missing / malformed / expired / revoked token, or
a token for a `disabled` account, yields `401`; a valid identity whose
role is below the route's requirement yields `403`.

This middleware **supersedes** the one-off `validBearerToken` /
`unquarantineToken` check (§H covers the migration) — the operator role
gate replaces the bespoke shared secret.

### F. The verified `participant_id` claim

`POST /api/v1/agents/{id}/chat` today reads `user_id` from the request
body ([RFC 0016 §F](0016-human-participant-chat-interface.md#f-rest-api-for-chat)).
RFC 0016 §Security #2 names the consequence plainly: *"`user_id` is
caller-supplied with no verification. Any caller can impersonate any
`user_id`."*

With `auth.mode: enabled`, the chat handler **ignores any body
`user_id`** and uses `identity.participant_id` from the resolved
session. The orchestrator sets the gRPC `ChatRequest.user_id` field from
that verified claim before the gRPC hop — so **no proto change** and no
agent-side change is needed; the value the agent receives is simply now
trustworthy. With `auth.mode: disabled`, the handler falls back to the
body `user_id` or `"local"`, preserving RFC 0016 behavior exactly.

This is the concrete closure of RFC 0016 Security Consideration #2 and
the substrate RFC 0037's confidentiality model implicitly assumes: the
*human* side of a classification boundary becomes verified rather than
asserted. The same claim is the identity any future channel-publish or
recall endpoint authenticates against.

> **Downstream consumer — [ISSUE-0082](../issues/ISSUE-0082-orchestrator-per-request-session-principal-emission.md) (principal emission).**
> [ISSUE-0081](../issues/ISSUE-0081-session-id-process-global-not-task-local.md)
> already shipped the persona-side **principal/tenant rail** — a
> `principal_id` storage dimension with a strict-equality recall filter and
> a `persatrix-principal` gRPC header bound task-locally per request
> ([RFC 0031 §C amendment](0031-per-session-namespacing-channels.md#c-storage-model);
> [`agents/principal_id.py`](../../agents/principal_id.py)) — but it was
> **armed and unfed**: nothing emitted a principal, so every request resolved
> to the single-tenant `'local'` default. The verified `participant_id`
> claim defined here is that missing **source**, and it was supplied in
> **v0.3.14**: under `auth.mode: enabled` the orchestrator emits the
> caller's verified principal on the `persatrix-principal` header, stamped
> once in `authMiddleware` (a missed dispatch origin would fail *open* into
> the shared `'local'` tenant, so the threading is structural rather than
> per-handler). Proven live at that release's
> [MT-MEMORY-MULTIUSER-001 run](../manual-tests/v0.3.14-execution-report.md),
> with two distinct principals read off storage. This does **not** change
> this RFC's multi-tenancy / per-user-memory-isolation Non-Goals — the
> boundary is **per-turn**, and two residuals of the same shape are stated
> rather than closed: a group room's close-derived aggregate and a persona's
> relayed cascade turn do not inherit the principal (ISSUE-0082 R-1 / R-2,
> v0.3.15). Under `disabled`, and for every unauthenticated caller, the
> storage layer still correctly collapses to `'local'`.

### G. Bootstrapping the first operator

Account creation is `operator`-gated (§E) — but a fresh install has no
operator. The chicken-and-egg is resolved by a **local bootstrap
subcommand of the orchestrator binary**:

```
persatrix-server account bootstrap --username <name>
```

Bootstrap is a subcommand of the Go orchestrator (`persatrix-server`,
`cmd/orchestrator`) — deliberately **not** a Rust CLI command. It reuses
`internal/accounts/` directly: the same schema, the same versioned
migration, and the same Argon2id wrapper the running server uses. The
account-credential write path is therefore **single-sourced in Go**; the
Rust CLI never opens `accounts.db` and never hashes a password, so there
is no second copy of the schema or the KDF to keep byte-compatible
across two languages.

`account bootstrap` opens `accounts.db` **directly on the local
filesystem**, prompts for the initial password (read without echo, never
in `argv` — the §J discipline), and creates the first account with role
`operator`. It runs the zero-accounts precondition and the insert **in
one transaction** and **refuses if any account already exists** — so it
can never be used to add a second operator or to take over an existing
install.

This grants no capability an attacker did not already have: filesystem
access to `accounts.db` already means they can read every password hash
and forge the database wholesale. The bootstrap merely avoids the
genuinely dangerous alternative — a network-reachable, unauthenticated
account-creation endpoint. An **environment-seeded** initial password is
also rejected: it would sit in process environment and compose files in
plaintext.

### H. Rollout: `auth.mode` and non-breaking enforcement

A new `auth:` configuration block (a new `config/security.yaml`, with
`schemas/security.schema.json`; overridable per
`config/environments/*.yaml` — Open Question #8) carries:

```yaml
auth:
  mode: disabled            # disabled | enabled
  session_ttl: 24h
  password:
    argon2_memory_kib: 65536
    argon2_iterations: 3
    argon2_parallelism: 4
```

`auth.mode` is the rollout switch:

- **`disabled` (default).** The middleware resolves every request to the
  anonymous `local` identity and **does not evaluate** the per-route
  policy. The chat endpoint uses the body `user_id` / `"local"`.
  Behavior is **byte-for-byte identical to today** — `persatrix chat` on
  localhost needs no login.
- **`enabled`.** The middleware enforces the §E policy matrix and the
  chat endpoint uses the verified claim (§F).

Defaulting to `disabled` preserves the RFC 0016 single-user local
experience and is what makes **Phase 1 a pure, non-breaking addition**
(§Phased Plan). Any networked deployment **must** set `enabled`. The
orchestrator emits a startup `WARN` when `--http-bind` resolves to a
non-loopback address while `auth.mode: disabled` — the same
trust-boundary startup-`WARN` pattern RFC 0009 / RFC 0011 already use
for the unauthenticated channels surface.

**The `SECURITY_UNQUARANTINE_TOKEN` stop-gap.** Phase 2 moves
`POST /api/v1/agents/{id}/unquarantine` to the `operator` role. When
`auth.mode: enabled`, the role gate is the control and the env var is
ignored. When `auth.mode: disabled`, the existing env-var bearer check
([`agent_handlers.go`](../../internal/server/agent_handlers.go)) still
applies, so **no deployment loses protection in either mode**. The env
var is documented as superseded; its removal is a follow-up (Open
Question #7).

### I. Extension points

The foundation is shaped so the obvious next steps are additive.

- **Authentication methods.** Password verification sits behind an
  `Authenticator` interface — roughly `Authenticate(ctx, credentials)
  (accountID string, err error)`. `passwordAuthenticator` is the first
  implementation. An OIDC / SAML implementation adds a method without
  touching the session layer: every login path, whatever the method,
  ends in "issue a session" (§D). The `accounts.auth_method` column
  exists from the first migration so a federated account is
  representable the day an IdP authenticator lands.
- **Authorization granularity.** `role` is an extensible string column
  (§B). The middleware is the **coarse** gate; [RFC 0028](0028-agent-decision-policy-engine.md)'s
  decision-policy engine and [RFC 0012](0012-protocols-organizations.md)'s
  organizational authority layer *above* it, consuming `role` (and the
  account) as inputs rather than replacing the gate.
- **Organizational clearance (RFC 0012).** RFC 0012 attaches a clearance
  to a human principal. That principal **is an account**, and its
  `participant_id` is the join key into memory and channels. RFC 0037's
  confidentiality lattice and RFC 0012's clearance both become
  *enforceable for humans* only once accounts exist — this RFC is the
  missing substrate. `config/organizations.yaml` already exists; an
  account ↔ org-membership table is the natural RFC 0012 addition, not
  this RFC's.
- **MFA, password reset, lockout.** Failed-login lockout is Phase 3. MFA
  is a second factor checked between password verification and session
  issuance — a clean hook point in the login handler, not built here.
  Self-service password *reset* needs an out-of-band channel (email),
  which the project does not have until RFC 0011 external bridges
  (v0.5.0); **operator-driven** reset is Phase 3.
- **Session token format.** Opaque server-side sessions are the
  foundation: revocable, no key management. If a stateless token is ever
  required (multi-node mesh, v0.6.0), the `Authenticator` / session-store
  boundary contains the change — handlers depend on the *resolved
  identity*, never the token shape.
- **Multi-tenancy.** An account is the unit a future tenant boundary
  would scope. No `tenant_id` is introduced, but nothing in the schema
  precludes adding one.

### J. CLI surface

New and modified Rust CLI commands:

- `persatrix login [--username <name>]` — prompts for the password
  (read without echo; never passed in `argv`), `POST`s
  `/api/v1/auth/login`, and writes the returned token to a credential
  file `~/.persatrix/credentials` (mode `0600`), keyed by the
  orchestrator URL so multiple orchestrators do not collide.
- `persatrix logout` — `POST`s `/api/v1/auth/logout` (revoking the
  session server-side) and clears the local token.
- `persatrix whoami` — `GET`s `/api/v1/auth/whoami`.
- `persatrix account create | list | disable` — operator-gated REST
  account administration (Phase 3).

First-operator bootstrap is **not** a Rust CLI command — it is a
subcommand of the Go orchestrator binary (`persatrix-server account
bootstrap`, §G), so the account schema and the Argon2id KDF stay
single-sourced in `internal/accounts/` and are never reimplemented in
Rust.

Every existing command (`workflow`, `agent`, `channel`, `chat`, `logs`,
…) reads the credential file and attaches `Authorization: Bearer` when a
token is present. On a `401` the CLI prints a `run 'persatrix login'`
hint. When `auth.mode: disabled`, the absence of a token is unremarkable
— commands work exactly as today. Storing the token in a `0600` file is
the foundation; an OS keyring is noted as a later hardening (Open
Question #2 of the security review, tracked in §Open Questions).

### K. REST surface summary

*(**Amended 2026-07-25** — per the
[enabled-mode exposure amendment](0039-amendment-enabled-mode-exposure.md):
`POST /api/v1/auth/login` gains the `session_transport` field (§A1), and
a throttled login answers `429` with `Retry-After` (§B4).)*

| Method & path | Policy | Phase | Notes |
|---|---|---|---|
| `POST /api/v1/auth/login` | `public` | 1 | Verify credential, issue session. |
| `POST /api/v1/auth/logout` | `authenticated` | 1 | Revoke the current session. |
| `GET /api/v1/auth/whoami` | `authenticated` | 1 | Return the resolved identity. |
| `POST /api/v1/auth/password` | `authenticated` | 3 | Change own password (verify-then-rehash); revokes the account's other sessions. |
| `POST /api/v1/accounts` | `operator` | 3 | Create an account. |
| `GET /api/v1/accounts` | `operator` | 3 | List accounts. |
| `GET /api/v1/accounts/{id}` | `operator` | 3 | Fetch one account. |
| `POST /api/v1/accounts/{id}/disable` | `operator` | 3 | Disable an account; revokes its sessions. |
| `POST /api/v1/accounts/{id}/password` | `operator` | 3 | Operator-driven password reset; revokes all the target account's sessions. |
| `POST /api/v1/agents/{id}/unquarantine` | `operator` | 2 | Moved from the `SECURITY_UNQUARANTINE_TOKEN` stop-gap (§H). |
| `POST /api/v1/agents/{id}/chat` | `authenticated` | 2 | Caller is the verified `participant_id` (§F). |
| Existing workflow / agent / channel / logs routes | `authenticated` | 2 | `operator` where the route mutates shared state — assigned per route in Phase 2. |
| `GET /healthz` | `public` | 1 | Unchanged; satisfies the docker-compose healthcheck. |

The exact `authenticated`-vs-`operator` split for the pre-existing
workflow / agent / channel routes is assigned route-by-route in Phase 2
(Open Question #6 of the design review, folded into the Phase 2 scope).

## Security Considerations

- **Password storage.** Argon2id with a per-account `crypto/rand` salt;
  cost parameters encoded into the stored PHC string with transparent
  verify-then-rehash on a parameter change (§C). Plaintext passwords are
  never written to any sink — the login DTO routes through the RFC 0009
  `SecretRedactor`.
- **Session token storage.** A 256-bit `crypto/rand` token; only
  `sha256(token)` is persisted; comparison is constant-time over
  fixed-size digests (the ISSUE-0004 discipline, §D). A read of
  `accounts.db` yields no usable live session.
- **Transport.** A bearer token over plaintext HTTP is interceptable.
  When `auth.mode: enabled` on a non-loopback bind, TLS termination at
  the reverse proxy is **mandatory** ([RFC 0002 §Non-Goals](0002-rest-api-server.md#non-goals)
  places TLS at the proxy layer). The orchestrator startup-`WARN`s on a
  non-loopback bind while `auth.mode: disabled` (§H); it cannot observe
  the proxy, so the deployment owns the TLS guarantee.
- **Fail closed.** A route absent from the policy map is treated as
  `operator` (§E). An unknown `role` resolves to no privilege. A
  missing / malformed / expired / revoked token, or a token for a
  `disabled` account, is `401`; an insufficient role is `403`.
- **Account-existence non-disclosure.** Login hashes against a fixed
  dummy PHC string when the username is absent and returns the identical
  `401` for "no such user" and "wrong password" (§C).
- **The verified claim, and its residual.** §F closes RFC 0016 Security
  Consideration #2 under `auth.mode: enabled`. The residual is explicit:
  with `auth.mode: disabled` impersonation is still possible — that is
  the documented dev-only posture, surfaced by the non-loopback startup
  `WARN`.
- **Bootstrap.** `account bootstrap` runs the zero-accounts check and
  the insert in one transaction (§G); it cannot add a second operator or
  take over an existing install. It exposes nothing a holder of
  filesystem access to `accounts.db` did not already have.
- **Brute force.** Login attempts route through the existing
  `internal/security.RateLimiter` ([RFC 0009 §E](0009-security-sandboxing.md#e-tool-access-control--output-validation)),
  keyed by username + client IP. Sustained failures trip per-account
  lockout in Phase 3. ***Corrected 2026-07-25*** — no phase step owned
  this wiring (Phases 1–2 never listed it; only Phase 3's *lockout* did),
  so the endpoint would have shipped unthrottled. The
  [enabled-mode exposure amendment](0039-amendment-enabled-mode-exposure.md)
  §B moves **throttling to Phase 1**, with the endpoint, and splits it
  into per-source and per-username limiters — because §C's fixed-dummy-hash
  non-disclosure makes every failed login a full Argon2id verification,
  i.e. an unauthenticated CPU/memory amplification vector that per-account
  lockout does not address. Lockout stays Phase 3.
- **Session fixation / replay.** A fresh token is minted per login;
  logout, operator-disable, and a password change or operator-driven
  reset all revoke server-side; the configurable TTL bounds the value of
  a stolen token. No revocation list is required — the per-request
  session lookup *is* the check.
- **Audit.** New `AuditEventType`s — `auth.login_succeeded`,
  `auth.login_failed`, `auth.logout`, `account.created`,
  `account.disabled`, `account.password_changed`, `authz.denied` — are
  emitted via the RFC 0009 `AuditLogger`. They record **metadata only**:
  username, source, role, route — never the password and never the raw
  token. `auth.login_failed` records the attempted username, not the
  attempted password.
- **CSRF / XSS.** Authentication is bearer-token only — no cookies —
  so CSRF does not apply; there is no browser surface, so XSS does not
  apply. Stated explicitly so a future web UI re-opens both.
  ***Re-opened 2026-07-25*** — v0.3.12 ships that web UI, so this bullet no
  longer holds on its own premise. The
  [enabled-mode exposure amendment](0039-amendment-enabled-mode-exposure.md)
  §A supersedes it: an `HttpOnly`/`SameSite=Strict` cookie transport
  (the token never enters JS), a same-origin assertion on
  cookie-authenticated writes, a console CSP, and a `{@html}` CI gate —
  with session-riding-under-XSS recorded as an accepted residual.
- **No new prompt-injection surface.** Credentials never enter an LLM
  context. The verified `participant_id` (§F) *strengthens* the RFC 0034
  / RFC 0037 prompt-assembly story rather than adding to it.
- **`accounts.db` at rest.** Unencrypted (a Non-Goal), but the hashing
  discipline means a file read yields no plaintext credential and no
  live session. Filesystem access to the file is nonetheless full
  compromise of the auth subsystem — the same accepted posture as
  `channels.db`.

## Phased Implementation Plan

### Phase 1: The account & session foundation (v0.3.x)

The complete mechanism, shipped **inert**. `auth.mode` defaults to
`disabled`, no route is enforced, and existing behavior is unchanged —
Phase 1 is a pure addition that can land and be reviewed before any
deployment depends on it.

1. **`internal/accounts/`** — the account and session models, the
   `accounts.db` SQLite schema with a versioned migration (`user_version`
   stamped in-transaction, the `internal/channels` discipline), and store
   CRUD.
2. **Password hashing** — an Argon2id wrapper over
   `golang.org/x/crypto/argon2` (new direct dependency), with parameters
   from config and verify-then-rehash.
3. **Session store** — issue / resolve / expire / revoke / prune;
   token-hash-only persistence; constant-time resolution; lazy
   `last_used_at` refresh.
4. **`Authenticator` interface + `passwordAuthenticator`** (§I seam).
5. **Auth endpoints** — `POST /api/v1/auth/login`,
   `POST /api/v1/auth/logout`, `GET /api/v1/auth/whoami`.
6. **`authMiddleware`** — identity resolution and the per-route policy
   map, present but, under the default `auth.mode: disabled`, resolving
   every request to the anonymous `local` identity with the policy check
   skipped (no enforcement).
7. **Config** — `config/security.yaml` + `schemas/security.schema.json`
   `auth:` block; `cmd/orchestrator/main.go` wiring; the non-loopback +
   `disabled` startup `WARN`.
8. **`persatrix-server account bootstrap`** — first-operator creation as
   an orchestrator-binary subcommand reusing `internal/accounts/` (§G).
9. **CLI** — `persatrix login` / `logout` / `whoami`; credential-file
   handling.
10. **Audit** — new event types; emission for login and logout.

*(**Amended 2026-07-25** — per the
[enabled-mode exposure amendment](0039-amendment-enabled-mode-exposure.md):
step 5 gains `session_transport` + the cookie form (§A1); step 6 gains
the same-origin assertion on cookie-authenticated writes (§A2); step 7
gains the per-source + per-username login limiters,
`auth.trusted_proxies`, and the console CSP/security headers (§A3, §B)
— throttling ships with the endpoint, not in Phase 3.)*

Dependencies: the merged RFC 0002 REST server only.

### Phase 2: Enforcement and the verified-identity claim (v0.3.x)

Turns the mechanism on. A deployment that sets `auth.mode: enabled` now
has a gated REST surface.

1. **`authMiddleware` enforcement** under `auth.mode: enabled` — the §E
   `401` / `403` matrix.
2. **Per-route policy** assigned to every existing human-facing route;
   deny-by-default (`operator`) for any unmapped route.
3. **Chat handler** — the verified `participant_id` claim replaces the
   body `user_id` under `enabled` (§F).
4. **Unquarantine endpoint** moved to the `operator` role;
   `SECURITY_UNQUARANTINE_TOKEN` retained as the `disabled`-mode gate and
   documented as superseded (§H).
5. **CLI** — every command attaches the stored bearer token; a `401`
   prints the `persatrix login` hint.
6. **Audit** — `authz.denied` emission.

*(**Amended 2026-07-25** — per the
[enabled-mode exposure amendment](0039-amendment-enabled-mode-exposure.md)
§A4: step 5 gains the console login form and the cookie flow —
`session_transport: "cookie"`, so the browser session rides the
`HttpOnly` cookie and the token never enters JS.)*

Dependencies: Phase 1.

### Phase 3: Account administration & hardening (v0.4.0)

Remote account management and the abuse-resistance layer.

1. **Operator-gated account REST API** — `POST /api/v1/accounts`,
   `GET /api/v1/accounts`, `GET /api/v1/accounts/{id}`,
   `POST /api/v1/accounts/{id}/disable`,
   `POST /api/v1/accounts/{id}/password` (the reset revokes every session
   the target account holds).
2. **Self-service** `POST /api/v1/auth/password` — change own password,
   verify-then-rehash; revokes the account's other sessions, keeping the
   caller's current one.
3. **Session administration** — list and operator-revoke sessions;
   `persatrix account create | list | disable` CLI.
4. **Failed-login lockout** via `internal/security.RateLimiter`.
5. **Audit** — `account.*` emission.

Dependencies: Phase 2. Independently reviewable; nothing downstream
blocks on it.

## Files Touched (Estimated)

| Component | Files | Change |
|-----------|-------|--------|
| Go orchestrator | `internal/accounts/` (new package) | Account & session models, `accounts.db` SQLite schema + migration, store CRUD, Argon2id hashing, session issue/resolve/revoke, `Authenticator` + `passwordAuthenticator` |
| Go orchestrator | `internal/server/auth_middleware.go` (new) | Identity resolution; per-route policy map; `401`/`403` enforcement |
| Go orchestrator | `internal/server/auth_handlers.go` (new) | `login`, `logout`, `whoami`; Phase 3 self-service password change |
| Go orchestrator | `internal/server/account_handlers.go` (new) | Phase 3 operator-gated account CRUD + session administration |
| Go orchestrator | `internal/server/server.go` | Wire `authMiddleware` into `Handler()`; accounts store + `ServerOption`; route registration |
| Go orchestrator | `internal/server/types.go` | Auth/account request & response DTOs |
| Go orchestrator | `internal/server/chat_handler.go` | Verified `participant_id` claim replaces body `user_id` under `auth.mode: enabled` (§F) |
| Go orchestrator | `internal/server/agent_handlers.go` | `unquarantine` endpoint moved to the `operator` role; `validBearerToken` stop-gap retained for `disabled` mode |
| Go orchestrator | `internal/security/audit_event.go` | New `auth.*` / `account.*` / `authz.denied` audit event types |
| Go orchestrator | `cmd/orchestrator/main.go` | Load `config/security.yaml`; open `accounts.db`; non-loopback + `disabled` startup `WARN` |
| Go orchestrator | `cmd/orchestrator/` (`account bootstrap` subcommand) | First-operator bootstrap — opens `accounts.db`, zero-accounts precondition + insert in one transaction; reuses `internal/accounts/` (schema, migration, Argon2id) so the credential write path is not duplicated in Rust |
| Rust CLI | `cli/src/commands/auth.rs` (new) | `login`, `logout`, `whoami`; credential-file read/write (`0600`) |
| Rust CLI | `cli/src/commands/account.rs` (new) | `create` / `list` / `disable` (REST) |
| Rust CLI | `cli/src/main.rs`, `cli/src/types.rs` | Subcommand wiring; auth DTOs; bearer-token attachment for all commands; `401` hint |
| Config / schema | `config/security.yaml`, `schemas/security.schema.json` (new) | `auth:` block — `mode`, `session_ttl`, Argon2id parameters |
| Go dependency | `go.mod`, `go.sum` | `golang.org/x/crypto/argon2` |
| Docs | `SECURITY.md`, `docs/ai-agents-orchestration-spec.md` §8.3, a new `docs/guides/` auth guide | Document accounts, the auth posture, and the `auth.mode` rollout |
| Tests | `internal/accounts/*_test.go`, `internal/server/*_test.go`, `tests/integration/`, `docs/manual-tests/` | Per Test Strategy |

## Test Strategy

- **Unit tests (Go)**:
  - Argon2id hash + verify round-trip; verify-then-rehash on a changed
    parameter set; constant-time verification; dummy-hash path for an
    absent username.
  - Session issue / resolve / expiry / revocation / pruning; only the
    token hash is persisted; resolution of an expired, revoked, and
    `disabled`-account session all fail; the prune sweep deletes dead
    rows and keeps live ones; `last_used_at` refreshes only once past the
    staleness threshold.
  - `authMiddleware` policy matrix: `public` / `authenticated` /
    `operator` against anonymous, `user`, and `operator` identities —
    every cell; an unmapped route resolves to `operator` (fail-closed);
    `auth.mode: disabled` skips the policy check entirely.
  - The `accounts.db` migration stamps `user_version` in-transaction and
    is idempotent on reopen.
- **Unit tests (Rust)**: credential-file round-trip with `0600` mode;
  bearer-token attachment; `401` produces the login hint.
- **Integration tests**:
  - `login` → token → authenticated call → `logout` → the same token is
    now `401`.
  - `operator`-only route returns `403` for a `user`-role session and
    `200` for an `operator` session.
  - Chat under `auth.mode: enabled` uses the verified `participant_id`
    and ignores a conflicting body `user_id`; under `disabled` it uses
    the body value — RFC 0016 behavior preserved.
  - `account bootstrap` succeeds on an empty `accounts.db` and is
    rejected once any account exists.
  - A self-service password change and an operator-driven reset each
    revoke the account's other live sessions (Phase 3).
- **Adversarial tests**: wrong-password vs unknown-username timing
  indistinguishability; expired, revoked, and forged tokens; a token for
  a disabled account; SQL-injection attempts in `username`; oversized
  login bodies (the RFC 0002 `MaxBytesReader` cap applies).
- **Manual tests**: a new `MT-AUTH-001` — fresh install →
  `persatrix-server account bootstrap` → `persatrix login` → an
  authenticated `persatrix chat` session → `logout` → a subsequent call
  is refused; and the `auth.mode: disabled` path confirming the unchanged
  no-login localhost experience.

## Open Questions

1. **Bootstrap mechanism.** A local orchestrator-binary subcommand (§G)
   vs an environment-seeded initial password vs first-request-wins; and,
   if local, hosting it in the Go orchestrator vs the Rust CLI.
   *Proposed resolution*: a local `persatrix-server account bootstrap`
   subcommand with a zero-accounts precondition — hosting it in the Go
   orchestrator reuses `internal/accounts/`, so the account schema and
   the Argon2id KDF are single-sourced rather than reimplemented in the
   Rust CLI. An env-seeded password leaks into process environment and
   compose files; first-request-wins is a network takeover primitive.
2. **Session token shape.** Opaque server-side sessions vs a stateless
   JWT. *Proposed resolution*: opaque — revocable, no key management,
   and the per-request lookup is already needed for the account-status
   check. Revisit JWT only if the v0.6.0 multi-node mesh needs stateless
   validation; the `Authenticator` / session-store boundary contains
   that change.
3. **Password KDF.** Argon2id vs bcrypt vs scrypt. *Proposed
   resolution*: Argon2id via `golang.org/x/crypto/argon2` — a new direct
   dependency, accepted as the modern memory-hard default.
4. **`accounts.db` placement.** A separate database file vs a table set
   inside `channels.db` vs a unified orchestrator database. *Proposed
   resolution*: a separate `internal/accounts/` package and database now
   — a clean trust boundary, mirroring `internal/channels/`. Revisit a
   consolidated orchestrator database only if the
   [storage-architecture-roadmap](../storage-architecture-roadmap.md)
   work consolidates orchestrator-side stores.
5. **Account ↔ participant cardinality.** 1:1 vs 1:many (one human
   presenting several persona-facing identities). *Proposed resolution*:
   1:1 in the foundation; the `participant_id` column does not preclude
   a later join table for 1:many.
6. **`auth.mode` default.** *Proposed resolution*: `disabled`, to
   preserve RFC 0016's no-login localhost experience and to guarantee
   Phase 1 is non-breaking; every networked deployment must set
   `enabled`. Revisit making `enabled` the default when the project
   exits the experimental phase that [SECURITY.md](../../SECURITY.md)
   documents.
7. **Fate of `SECURITY_UNQUARANTINE_TOKEN`.** *Proposed resolution*:
   retained as the `disabled`-mode gate for the unquarantine endpoint
   through this RFC; removal is a follow-up once `auth.mode: enabled` is
   the norm — sequenced behind observed adoption, not a fixed date.
8. **Where the `auth:` config lives.** A new `config/security.yaml` vs
   an existing orchestrator config surface. *Proposed resolution*: a new
   `config/security.yaml` with a JSON schema, overridable per
   `config/environments/*.yaml`; fold it into a broader orchestrator
   config only if one emerges.

## Decision / Next Steps

1. Review this RFC alongside [RFC 0037](0037-memory-confidentiality-channel-classification.md)
   — whose v0.3.x confidentiality model presupposes the verified human
   identity Phases 1–2 establish — and [RFC 0009](0009-security-sandboxing.md)
   (the agent-identity axis) and [RFC 0012](0012-protocols-organizations.md)
   (organizational authority and clearance). The three are the agent,
   human, and organizational facets of one identity model; they compose
   in dependency order rather than needing a single shared version.
2. Confirm the split `v0.3.x (Phases 1–2) + v0.4.0 (Phase 3)` target and
   the Open Question resolutions — especially #2 (opaque sessions),
   #3 (Argon2id), and #6 (`auth.mode` default `disabled`).
3. Implement Phase 1 (the inert foundation), then Phase 2 (enforcement +
   the verified claim), then Phase 3 (administration + hardening). Phase 1
   is shippable and reviewable without changing any existing behavior.
4. Create `docs/rfcs/0039-pr-plan.md` with PR slices once this RFC is
   accepted.
5. Regenerate [INDEX.md](INDEX.md) via `make rfcs` and update the RFC
   Master Index and v0.4.0 RFC Scope rows in [ROADMAP.md](../../ROADMAP.md).

## Related Documentation

- [Amendment — Enabled-Mode Exposure: the Browser Session Surface & Login
  Throttling](0039-amendment-enabled-mode-exposure.md) (2026-07-25) — supersedes
  the *"A web or GUI login"* Non-Goal and the *CSRF / XSS* and *Brute force*
  Security Considerations for the v0.3.12 shipping scope.
- [RFC 0002 — REST API Server](0002-rest-api-server.md) — the
  unauthenticated surface this RFC secures; the JSON error envelope,
  middleware composition, and `contextKey` pattern reused here.
- [RFC 0016 — Human Participant & Chat Interface](0016-human-participant-chat-interface.md)
  — the `UserParticipant` an account binds to; §F closes its Security
  Consideration #2.
- [RFC 0009 — Agent Identity, Security & Sandboxing](0009-security-sandboxing.md)
  — the agent-identity axis; the `AuditLogger`, `SecretRedactor`, and
  `RateLimiter` this RFC reuses.
- [RFC 0012 — Protocols & Organizations](0012-protocols-organizations.md)
  — organizational authority and clearance attach to an account (§I).
- [RFC 0037 — Memory Confidentiality & Channel Classification](0037-memory-confidentiality-channel-classification.md)
  — its confidentiality model presupposes the verified human identity
  this RFC provides.
- [RFC 0001 — Core Orchestration Pipeline](0001-core-orchestration-pipeline.md)
  — the in-memory orchestrator `Store` that `accounts.db` deliberately
  does not use.
- [ISSUE-0082 — Orchestrator per-request session/principal emission](../issues/ISSUE-0082-orchestrator-per-request-session-principal-emission.md)
  — its principal half (Part 2) is gated on this RFC's verified
  `participant_id` claim (§F); the orchestrator emits `persatrix-principal`
  once `auth.mode: enabled`.
- [ISSUE-0081 — Session id process-global, not task-local](../issues/ISSUE-0081-session-id-process-global-not-task-local.md)
  — shipped the persona-side principal/tenant rail this RFC's verified
  claim feeds.
- [SECURITY.md](../../SECURITY.md) — the project security posture and
  experimental-software disclaimer.
- [ROADMAP.md](../../ROADMAP.md) — version planning; the v0.4.0 identity
  bundle.
- [Architecture spec](../ai-agents-orchestration-spec.md) §8.3 — the
  orchestrator API surface.
