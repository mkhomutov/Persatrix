# RFC 0039 — PR Implementation Plan (Phases 1–2 — v0.3.12 scope, bundled second workstream)

**RFC**: [0039-user-accounts-authentication.md](0039-user-accounts-authentication.md)
**Created**: 2026-07-25
**Branch prefix**: `feature/v0312-rfc0039-`
**Target**: `main`
**Merge strategy**: Squash merge per [BRANCHING.md](../BRANCHING.md)
**Master plan**: [v0.3.12-plan.md Phase 1](../v0.3.12-plan.md#phase-1--implement-rfc-0037-rfc-0049-p01-rfc-0039-p12)

---

## Overview

RFC 0039 Phases 1–2 give Persatrix **human accounts with password login, opaque revocable sessions, and a coarse operator/user role gate** on the REST surface — the *safe remote console* story: with `auth.mode: enabled`, the web console and REST API are safe to run beyond localhost, and the caller's `participant_id` becomes a **verified claim** instead of a body field. Phase 3 (account-administration REST API, self-service password change, failed-login lockout) stays v0.4.0.

This is the **bundled second workstream** of v0.3.12 ([scope lock 2026-07-25](../v0.3.12-plan.md#scope-decisions-locked-at-plan-authoring-time-2026-07-25)): fully parallel to the memory cluster (zero shared files), **independently shippable and cuttable whole** — if it slips, it becomes the v0.3.13 headline and v0.3.12 reverts to a one-story release.

**The browser surface is in scope, and it is amended in.** RFC 0039 as written excluded a web login and derived two Security Considerations (*CSRF / XSS*, *Brute force*) from that exclusion. v0.3.12 ships the console login anyway, so the [enabled-mode exposure amendment](0039-amendment-enabled-mode-exposure.md) re-opens both: §A defines the cookie transport / CSRF assertion / XSS posture, §B moves login throttling into Phase 1 with the endpoint. Its deliverables are folded into PRs 3, 5, and 6 below — no new PRs.

**The Phase-1 inertness contract.** Phase 1 ships the complete mechanism **inert**: `auth.mode` defaults to `disabled`, every request resolves to the anonymous `local` identity, no route is enforced. The Phase-1 closeout gate is the *unchanged* full test suite under the default — the same "byte-for-byte untouched" posture v0.3.11 applied to human channels. Enforcement arrives only in Phase 2 and only for deployments that opt in.

This plan covers Phases 1–2 across **6 PRs**, mirroring the RFC's [phasing](0039-user-accounts-authentication.md#phased-implementation-plan):

## Dependency Graph

```
RFC 0002 REST server (shipped) — the only prerequisite
   │
   ├── PR 1 (accounts store: internal/accounts/ models + accounts.db versioned migration
   │     + CRUD + Argon2id wrapper [x/crypto/argon2, params from config])          [inert]
   │       │
   │       └── PR 2 (session store: issue/resolve/expire/revoke/prune, token-hash-only,
   │             constant-time resolution + Authenticator seam + passwordAuthenticator) [inert]
   │               │
   │               └── PR 3 (REST: /auth/login|logout|whoami + authMiddleware [present,
   │                     non-enforcing] + config/security.yaml auth: block + schema
   │                     + non-loopback WARN)                                       [inert]
   │                       │
   │                       └── PR 4 (operator surface: account bootstrap subcommand +
   │                             persatrix login/logout/whoami CLI + credential file
   │                             + login/logout audit events)     [Phase 1 complete, inert]
   │                               │
   │                               └── PR 5 (Phase 2: enforcement — the §E 401/403 matrix
   │                                     + per-route policy [deny-by-default operator]
   │                                     + verified participant_id claim in chat
   │                                     + unquarantine → operator role + CLI bearer
   │                                     + authz.denied audit)
   │                                       │
   │                                       └── PR 6 (closeout: docs + MT-AUTH-001 + RFC flip)
```

## PR 1 — `feature/v0312-rfc0039-accounts-store` (Phase 1 steps 1–2)

- `internal/accounts/` (new): account + session models, the `accounts.db` SQLite schema with a versioned migration (`user_version` stamped in-transaction — the `internal/channels` discipline), store CRUD.
- Argon2id wrapper over `golang.org/x/crypto/argon2` (**new direct dependency** — `go.mod` + `deny.toml`/license sweep), parameters from config, verify-then-rehash.
- Tests: migration discipline; hash/verify/rehash; CRUD.

## PR 2 — `feature/v0312-rfc0039-sessions` (Phase 1 steps 3–4)

- Session store: issue / resolve / expire / revoke / prune; **token-hash-only** persistence; constant-time resolution; lazy `last_used_at` refresh.
- The `Authenticator` interface + `passwordAuthenticator` (§I seam — MFA/SSO/API-key principals plug here later).
- Tests: token lifecycle, constant-time property, prune.

## PR 3 — `feature/v0312-rfc0039-rest-middleware` (Phase 1 steps 5–7)

**Gate RESOLVED (2026-07-29 maintainer call)** — the amendment's three [open questions](0039-amendment-enabled-mode-exposure.md#open-questions) are decided (limiters: per-source 10/60 s + per-username 5/60 s, own 1000-key LRUs; cookie TTL: separate `auth.cookie_session_ttl` default `8h`, bearer stays `24h`; bootstrap floor: 12 characters, lands in PR 4) and the amendment is ratified out of 📋 Proposed (recorded in its front-matter; the ✅ flip itself is PR 6's).

- `internal/server/auth_handlers.go` (new): `POST /api/v1/auth/login`, `POST /api/v1/auth/logout`, `GET /api/v1/auth/whoami`.
- `authMiddleware`: identity resolution + the per-route policy map, present but non-enforcing under the default `auth.mode: disabled` (every request → anonymous `local`, policy check skipped).
- `config/security.yaml` + `schemas/security.schema.json` `auth:` block; `cmd/orchestrator/main.go` wiring; the non-loopback-bind + `disabled` startup `WARN`.
- **Per the [enabled-mode exposure amendment](0039-amendment-enabled-mode-exposure.md)** — the `session_transport: "bearer" | "cookie"` login field + the `__Host-` `HttpOnly`/`Secure`/`SameSite=Strict` cookie and its clearing on logout (§A1); the same-origin assertion on cookie-authenticated writes (§A2); the CSP + `nosniff` + `Referrer-Policy` headers on `internal/server/ui.go`, which sets none today, and the `{@html}` CI gate (§A3); the **per-source and per-username login limiters** + `auth.trusted_proxies` + `429`/`Retry-After` (§B — throttling ships with the endpoint, in this phase, not Phase 3).
- Tests: login/logout/whoami round-trip; the disabled-mode no-op; schema validation; the amendment's [test strategy](0039-amendment-enabled-mode-exposure.md#test-strategy) (transport matrix, CSRF matrix, the bearer/CLI no-`Origin` regression, throttle trips + the identical `429`).

**PR = #790.** As-implemented notes: the auth routes mount on the **limiter-bypass root mux** beside the RFC 0048 console (an active agent quarantine's anonymous-deny must never lock operators out of login; the §B limiters are the surface's own defence); `whoami` reports the anonymous `local` identity pre-enforcement rather than 401 (honest under both modes; the §E matrix 401s it in PR 5); login collapses `ErrAccountDisabled` into the identical invalid-credentials `401` (a disabled-account distinction would confirm existence *and* password to whoever holds it); logout revokes the **presented** token directly, mode-independent, and is idempotent on an already-revoked session; `LoadSecurityConfig` is loud-fail (absent → defaults, malformed → startup Fatal — a typo'd `mode: enabled` must not silently boot unauthenticated); the store opens (and `data/accounts.db` is created) under both modes since login functions inert. Review follow-ups (same PR): the loader mirrors the schema's 8 MiB Argon2id memory floor (the loader is the semantic authority — bypassing `make validate` must hit the same floor); empty-username login attempts throttle under a sentinel key rather than the limiter's shared "anonymous" bucket (which emits its security-class audit event unthrottled per call); the `{@html}` gate scans full file text so a directive split across lines still trips.

## PR 4 — `feature/v0312-rfc0039-cli-bootstrap` (Phase 1 steps 8–10 — Phase 1 completes)

- `persatrix-server account bootstrap` — first-operator creation as an orchestrator-binary subcommand reusing `internal/accounts/` (§G — no unauthenticated first-account REST hole), enforcing the [amendment OQ 1](0039-amendment-enabled-mode-exposure.md#open-questions) **12-character password floor**.
- CLI: `persatrix login` / `logout` / `whoami`; credential-file handling.
- Audit: new event types; emission for login and logout.
- **Phase-1 closeout gate**: the full existing suite passes unchanged under `auth.mode: disabled`.

**PR = #791.** As-implemented notes: the subcommand dispatches **before `flag.Parse()`** (`runSubcommand` in `cmd/orchestrator/bootstrap.go`), so server argv never mixes with subcommand argv; the §G zero-accounts check + insert ride one transaction in a new `Store.BootstrapFirstAccount` (validation shared with `CreateAccount` via `buildAccount`, so the two write paths cannot drift), refusing with `ErrAccountsExist` durably across reopen. The password prompt is no-echo via `golang.org/x/term` (new direct dependency) with a **confirm prompt** — a typo'd bootstrap password would otherwise be unrecoverable until Phase 3's reset — and a piped-stdin fallback for provisioning (`printf 'pw\npw\n' | …`); the 12-char floor counts runes, not bytes, and is checked before the confirm round-trip. `--participant` defaults to the folded username; the account-DB parent directory is created like `initAuth` does. The Rust CLI reads the password the same way (`rpassword` under a TTY, a stdin line under pipes — `rpassword` alone cannot read pipes) and stores the bearer token in `~/.persatrix/credentials` (mode `0600`, JSON keyed by orchestrator URL, `PERSATRIX_CREDENTIALS_FILE` override mirroring the active-session pointer seam); a malformed credential file reads as "not logged in" but **refuses to be clobbered by the next write**. `logout` revokes server-side FIRST and clears the local token only on `204`/`401` — a token the server still honours is never forgotten locally. Audit: `auth.login_succeeded` / `auth.login_failed` / `auth.logout`, all **security-class** (the `agent.token_issued`/`token_invalid` precedent; rate bounded by the §B limiters), metadata only — the failed event keeps the true reason (`invalid_credentials` vs `account_disabled`) operator-side while the wire 401 stays identical, and logout resolves the session **before** revoking purely to name the account in the record. Live-verified end-to-end on an `enabled`-mode boot: bootstrap → CLI login (0600 file written) → whoami → wrong-password 401 → logout → anonymous whoami, with all three audit events checksum-chained in `audit.jsonl`. Phase-1 closeout: full Go suite + 291 CLI tests green, zero deltas under the shipped `disabled` default — **Phase 1 complete, inert**.

## PR 5 — `feature/v0312-rfc0039-enforcement` (Phase 2)

- `authMiddleware` enforcement under `auth.mode: enabled` — the §E `401`/`403` matrix; per-route policy on every existing human-facing route, **deny-by-default (`operator`) for any unmapped route** (the route-by-route assignment folds the design review's OQ #6 into this PR).
- Chat handler: the verified `participant_id` claim replaces the body `user_id` under `enabled` (§F).
- Unquarantine endpoint → `operator` role; `SECURITY_UNQUARANTINE_TOKEN` retained as the `disabled`-mode gate, documented as superseded (§H).
- CLI: every command attaches the stored bearer token; `401` prints the `persatrix login` hint. Web console: the login surface is the console's existing panel chrome (no new SPA slice — a minimal login form on 401), logging in with `session_transport: "cookie"` so the session rides the `HttpOnly` cookie from PR 3 and **the token never enters JS** ([amendment §A](0039-amendment-enabled-mode-exposure.md#decision-a--the-browser-session-surface)).
- Audit: `authz.denied`.
- Tests: the enforcement matrix per route class; claim substitution; disabled-mode regression re-run.

**PR = #793** (originally opened as #792 stacked on PR 4 = #791; GitHub auto-closed #792 when #791's squash-merge deleted its base branch — a closed PR whose base branch is gone cannot be reopened — so it was reopened as #793 against `main`, identical content rebased onto the squash). As-implemented notes: **(1) the OQ #6 route assignment carves out the agent-attributable REST ingress as `public`** — agent self-registration *and self-deregistration* (`agents/server.py`), the RFC 0011 channel HTTP seams the persona fleet drives in production (`channel_publisher` / `channel_history_fetcher` / `channel_catchup`: channels list/get, messages GET/POST), and the convene timer callback — per the RFC's §Non-Goals ("agent-attributable REST ingress follows the RFC 0009 track"): agents hold no accounts, so gating these would break every deployed persona under `enabled`. The residual is loud: a third startup `WARN` under `enabled` + non-loopback names the ungated surface; its defence stays the RFC 0009 per-agent limiter/quarantine, its authorization story arrives with agent tokens. Everything else: reads `authenticated`, mutations `operator`, persona-memory `recall` deliberately `operator` (read-shaped but a memory-exposure surface), unmapped/method-mismatch → `operator` fail-closed. **(2)** The policy table is a sentinel-handler `ServeMux` (`auth_policy.go`) so policy matching uses the serving mux's own pattern semantics and cannot drift. **(3)** Anonymous `whoami` under `enabled` now 401s (the PR 3 honest-anonymous report was Phase-1-only, as noted there); four PR 3 middleware pins were reworked to assert the same no-fall-through invariants via 401. **(4)** §F applies to the chat *history* read too — its `user_id` was an unauthenticated lookup key (the handler's own v0.2 TODO): absent → the claim; naming someone else → explicit 403 (no cross-user read at the coarse gate, operators included — Phase 3+ owns a finer story). The chat POST ignores the body field silently, §F-literal. **(5)** `authz.denied` (security-class) emits on 403 only — an anonymous 401 is unbounded noise and would be a per-request fsync amplifier. **(6)** Unquarantine under `enabled` ignores the env token *and* stamps the verified participant as the breaker-audit actor. **(7)** `/ui/context` reports the middleware identity, so the console's `authenticated` gate (RFC 0048 amendment §E) hides the acting-as override on a real principal with no client change; the login form is `LoginPanel.svelte` rendered by the shell on the first 401 (an `onUnauthorized` seam at the single api.js error chokepoint), cookie transport, reload-on-success. **(8)** The CLI attaches the stored bearer as a reqwest *default header* (marked sensitive) — one seam, every command incl. SSE follow; the 401 login hint lives in the shared `api_error_message`. Live-verified end-to-end: bootstrap → enabled boot → anonymous 401s / agent ingress open → CLI-shaped bearer flows → browser cookie login → verified principal in the console, `document.cookie` empty.

## PR 6 — `feature/v0312-rfc0039-closeout`

- Docs: `docs/guides/web-console.md` (the "beyond localhost requires a reverse proxy" limitation flips to "set `auth.mode: enabled`" — carrying the amendment's browser posture and its [residual risk](0039-amendment-enabled-mode-exposure.md#residual-risk), notably session-riding-under-XSS and the `SameSite=Strict` UX note), README security-posture line, `docs/guides/sessions.md` cross-link (account ≠ session ≠ participant), SECURITY.md note.
- `MT-AUTH-001` (bootstrap on empty `accounts.db` → login → gated route 403/200 matrix → logout → disabled-mode no-delta) run live, **plus a browser leg**: cookie login, the token unreadable from JS, a cross-site write rejected, logout clearing the cookie.
- RFC 0039 front-matter → ⚠️ Partially Implemented (P1–2 v0.3.12 ✅; P3 v0.4.0); the [enabled-mode exposure amendment](0039-amendment-enabled-mode-exposure.md) → ✅ Implemented (the [0050-amendment precedent](0050-amendment-interaction-budget-enforcement.md)), naming the PRs that landed it; ROADMAP row flip.

---

## Risks

| Risk | Mitigation |
|------|------------|
| Enforcement breaks existing deployments. | `disabled` is the shipped default; Phase 1 is provably inert (the PR 4 no-delta gate); enforcement is opt-in and lands after the CLI/web bearer plumbing in the same PR. |
| A new crypto dependency (x/crypto/argon2). | Stdlib-adjacent, vendored via `go.mod` like existing x/ deps; license sweep in PR 1. |
| The per-route policy map misses a route (open hole under `enabled`). | Deny-by-default `operator` for unmapped routes — a missed route fails closed, not open. |
| The console holds a session token in JS, so any XSS is session theft. | `session_transport: "cookie"` — `HttpOnly`, so the token never enters JS ([amendment §A1](0039-amendment-enabled-mode-exposure.md#a1-transport-is-chosen-by-the-caller-not-sniffed)); CSP + the `{@html}` CI gate reduce XSS probability. Residual (session riding while the page is open) is recorded, not hand-waved. |
| The cookie transport re-opens CSRF. | `SameSite=Strict` **plus** a server-side same-origin assertion on cookie-authenticated writes — the second does not depend on the client honouring the first; bearer callers (the CLI) skip it. |
| The login endpoint ships unthrottled under `enabled`. | Per-source **and** per-username limiters land in PR 3, with the endpoint. Note the amplification framing: every failed login is a full Argon2id verification, so this is a DoS vector, not only a guessing one. |
| Behind a reverse proxy the per-source limiter degrades to a global one. | `auth.trusted_proxies` + `X-Forwarded-For` depth is a precondition; unconfigured on a non-loopback `enabled` bind → startup `WARN`, and the per-username limiter still applies. |
| Workstream drags the release. | Cuttable whole at any PR boundary (each is inert or opt-in); slips to v0.3.13 as its own headline. |
