# RFC 0039 Amendment — Enabled-Mode Exposure: the Browser Session Surface & Login Throttling

**Type**: amendment to [RFC 0039](0039-user-accounts-authentication.md) — Non-Goal *"A web or GUI login"*, §D (session presentation), §E (the policy matrix), §K (REST surface summary), the Security Considerations bullets *"CSRF / XSS"* and *"Brute force"*, and the Phase 1/2 step lists
**Status**: ✅ Implemented — §A transports/cookie/same-origin-assertion/headers + the `{@html}` CI gate and §B throttling landed in PR 3 ([#790](https://github.com/mkhomutov/Persatrix/pull/790)); the §A4 console login form + cookie session under live enforcement landed in PR 5 ([#793](https://github.com/mkhomutov/Persatrix/pull/793)). Ratified 2026-07-29 by the maintainer call at the [0039 PR plan](0039-pr-plan.md) PR 3 gate (all three [open questions](#open-questions) resolved — see their §Resolution notes); authored at the [v0.3.12](../v0.3.12-plan.md) plan opening (2026-07-25); flipped at the PR 6 closeout (the [0050-amendment precedent](0050-amendment-interaction-budget-enforcement.md))
**Author**: Maksim Khomutov
**Date**: 2026-07-25
**Target**: v0.3.12, inside RFC 0039 Phases 1–2 — this is a correction to the shipping scope, not a new train; it lands in the [0039 PR plan](0039-pr-plan.md) PRs 3/5/6
**Authoritative model**: [RFC 0039 — User Accounts & Authentication](0039-user-accounts-authentication.md)
**Supersedes**: RFC 0039 Non-Goal *"A web or GUI login. CLI and REST only. No browser surface, so no cookies, no CSRF, no session-management UI."*; the Security Consideration *"CSRF / XSS. Authentication is bearer-token only — no cookies — so CSRF does not apply; there is no browser surface, so XSS does not apply. **Stated explicitly so a future web UI re-opens both.**"*; and the placement of all brute-force defence in Phase 3.

---

## Table of Contents

- [Context](#context)
- [The two gaps](#the-two-gaps)
- [Decision A — the browser session surface](#decision-a--the-browser-session-surface)
- [Decision B — login throttling ships with the endpoint](#decision-b--login-throttling-ships-with-the-endpoint)
- [What changes in RFC 0039](#what-changes-in-rfc-0039)
- [Where this lands](#where-this-lands)
- [Test strategy](#test-strategy)
- [Residual risk](#residual-risk)
- [Open questions](#open-questions)
- [Related documentation](#related-documentation)

---

## Context

RFC 0039 was written (2026-05-16) as a **CLI-and-REST** foundation. Its
Non-Goals say so, and two of its Security Considerations are *derived from*
that scope rather than argued independently:

> **CSRF / XSS.** Authentication is bearer-token only — no cookies — so CSRF
> does not apply; there is no browser surface, so XSS does not apply. *Stated
> explicitly so a future web UI re-opens both.*

> **Brute force.** Login attempts route through the existing
> `internal/security.RateLimiter` […] Sustained failures trip per-account
> lockout in Phase 3.

The v0.3.12 scope lock invalidates the premise of the first and leaves the
second unowned. v0.3.12 makes *"with `auth.mode: enabled`, the web console and
REST API are safe to run beyond localhost"* the **headline framing** of the
RFC 0039 workstream ([v0.3.12 plan](../v0.3.12-plan.md)), the
[0039 PR plan](0039-pr-plan.md) PR 5 ships a console login form and a
session-carrying `fetch`, and the closeout flips the
[web-console guide](../guides/web-console.md)'s *"beyond localhost requires a
reverse proxy"* limitation to *"set `auth.mode: enabled`"*.

So the browser surface RFC 0039 excluded is exactly what v0.3.12 ships, and the
RFC pre-registered that this re-opens CSRF and XSS. This amendment does the
re-opening.

## The two gaps

**Gap 1 — a browser surface with no browser-surface design.** The PR plan's one
line (*"same-origin `fetch` carries the session; a minimal login form on 401"*)
does not say **how** the session is carried. The default reading — the bearer
token from the login response body, held in JS — puts a long-lived credential
inside a page that renders **persona- and LLM-authored channel content**, i.e.
untrusted text by construction (RFC 0009 treats the same strings as untrusted on
the prompt side). Any XSS is then full session theft, and the theft outlives the
page. Nothing in the plan chooses a storage location, and `internal/server/ui.go`
serves the console today with **no security headers at all** — no CSP, no
`nosniff`, no `frame-ancestors`.

**Gap 2 — brute-force defence is promised but unassigned.** The RFC's Security
Considerations state that login routes through the `RateLimiter`, but the
Phase 1 steps (1–10) and Phase 2 steps (1–6) never list that wiring; the only
phased mention is Phase 3 item 4, *"Failed-login lockout"* — v0.4.0. The
[0039 PR plan](0039-pr-plan.md) faithfully mirrors the omission across PRs 1–6.
As written, v0.3.12 ships `auth.mode: enabled` — the entire "safe beyond
localhost" claim — with an **unthrottled** password endpoint.

That is worse than ordinary credential-guessing exposure, for a reason the RFC
does not draw out: §C's account-existence non-disclosure hashes against a fixed
dummy PHC string when the username is absent, so **every** failed login performs
a full Argon2id verification at the configured cost. Login is therefore a
**CPU-and-memory amplification vector** for an unauthenticated caller, entirely
independent of whether any password is ever guessed. Phase-3 per-account lockout
does not address it at all — the amplification needs no valid account.

---

## Decision A — the browser session surface

**One session token, two presentation channels.** The opaque token of §D is
unchanged; how a client presents it becomes explicit.

### A1. Transport is chosen by the caller, not sniffed

`POST /api/v1/auth/login` gains an optional request field:

```
session_transport: "bearer" | "cookie"     (default "bearer")
```

- **`"bearer"` (default, unchanged)** — the token is returned in the response
  body. The CLI, scripts, and every programmatic caller keep the §D contract
  byte-for-byte.
- **`"cookie"`** — the response body carries **no token**. The server sets:

  ```
  Set-Cookie: __Host-persatrix_session=<token>; HttpOnly; Secure; SameSite=Strict; Path=/
  ```

  The console sends `"cookie"`, so **the session token never enters JS** and no
  XSS can exfiltrate it.

Explicit beats sniffing `Origin`/`Sec-Fetch-*` to guess "is this a browser": the
behaviour is a request parameter, so it is testable and cannot drift with client
header changes.

`authMiddleware` resolves identity in a fixed order — `Authorization: Bearer`
first, cookie second. A request presenting both uses the bearer token and ignores
the cookie, so resolution is deterministic.

`__Host-` requires `Secure`, `Path=/`, and no `Domain`. Browsers treat
`http://localhost` as a trustworthy origin and accept `Secure` cookies there, so
the loopback dev path works; PR 3 verifies this per browser and the un-prefixed
`persatrix_session` is the documented fallback if it proves inconsistent.

### A2. CSRF — re-opened, and closed two ways

A bearer token in a header is immune to CSRF because a cross-site attacker cannot
set the `Authorization` header. **A cookie is not**, so the cookie transport
re-opens it. Two independent defences, because neither alone is a server-side
invariant:

1. **`SameSite=Strict`** — the cookie is withheld on every cross-site request,
   including top-level navigations. This defeats classic CSRF, but it is enforced
   by the *client*.
2. **A same-origin assertion in `authMiddleware`** — for any **cookie**-resolved
   identity on a method other than `GET`/`HEAD`/`OPTIONS`, require
   `Sec-Fetch-Site: same-origin`, or an `Origin` header matching the server's own
   origin. Otherwise `403`. **Bearer**-resolved requests skip the check entirely,
   so the CLI (which sends no `Origin`) is unaffected.

The check sits next to identity resolution — one place, every route, and it
fails closed on unmapped routes exactly like the §E policy map.

### A3. XSS — reduce probability, bound the damage

`HttpOnly` bounds the *damage*: XSS cannot steal the token. It does **not**
prevent an XSS from acting as the user while the page is open (session riding) —
see [Residual risk](#residual-risk). Probability is reduced by two gates:

- **No `{@html}`, enforced.** The console already avoids it deliberately — see
  the comments in `web/src/panels/ChannelMessage.svelte` and
  `web/src/lib/mentions.js`. This amendment promotes that convention to a **CI
  gate**: a check rejecting `{@html}` anywhere under `web/src`, so the discipline
  survives a contributor who has not read those comments.
- **A Content-Security-Policy on the console**, which `internal/server/ui.go`
  does not set today:

  ```
  Content-Security-Policy: default-src 'self'; script-src 'self';
      style-src 'self' 'unsafe-inline'; img-src 'self' data:;
      connect-src 'self'; object-src 'none'; base-uri 'none';
      frame-ancestors 'none'
  X-Content-Type-Options: nosniff
  Referrer-Policy: same-origin
  ```

  `frame-ancestors 'none'` also closes clickjacking, which `SameSite` does not
  touch. If the Svelte build turns out to need `'unsafe-inline'` for *scripts*,
  that is a build-configuration fix in PR 3 — **not** a CSP relaxation.

### A4. Console obligations

- Never write the token to `localStorage`, `sessionStorage`, or IndexedDB — under
  `"cookie"` transport it never receives one.
- Never place a token in a URL, query string, or fragment.
- Logout calls `POST /api/v1/auth/logout` (server-side revocation, unchanged) and
  the server clears the cookie with `Max-Age=0`. Client-side clearing alone is
  not logout.

---

## Decision B — login throttling ships with the endpoint

**Throttling moves to Phase 1** — the phase that introduces `/auth/login`.
Per-account **lockout** stays Phase 3, unchanged; these are different mechanisms
for different threats and the RFC conflated them.

### B1. Two limiters, because there are two keys

Reuse `internal/security.RateLimiter` (sliding window, bounded LRU —
[`ratelimit.go`](../../internal/security/ratelimit.go)). A login attempt must
pass **both**:

| Limiter | Key | Bounds |
|---|---|---|
| per-source | client IP | the Argon2id amplification DoS; single-source credential stuffing |
| per-username | normalized username | targeted guessing across rotating source addresses |

### B2. The LRU-eviction caveat (why not one limiter)

`RateLimiter` hard-caps tracked keys and evicts LRU. An attacker rotating
usernames can therefore **evict their own** per-username entries — so the
per-username limiter cannot be the flood defence. Two consequences, recorded
because they are easy to get wrong:

- The login limiters get their **own** `RateLimitConfig` instances with their own
  caps. They must not share the agent limiter, whose cardinality budget is sized
  for agents.
- The **per-source** limiter is the load-bearing one under key-flooding (an
  attacker cannot rotate source addresses as cheaply as usernames). The
  per-username limiter is the targeted-guessing defence, not the flood defence.

Eviction already emits a telemetry-class audit event; that becomes the operator's
cardinality-blow-up signal.

### B3. Client-IP resolution is a precondition, not a detail

Under `auth.mode: enabled` on a non-loopback bind, TLS termination at a reverse
proxy is already **mandatory** (§Security, Transport). Behind a proxy, a naive
`RemoteAddr` collapses every user to the proxy's address and the per-source
limiter silently becomes a **global** limiter — throttling legitimate operators
while doing nothing per-attacker.

So the per-source limiter requires a trusted-proxy configuration
(`auth.trusted_proxies` + `X-Forwarded-For` depth) before it is meaningful. When
`auth.mode: enabled` on a non-loopback bind **without** that configuration, the
orchestrator emits a startup `WARN` alongside the existing non-loopback warning,
and the per-username limiter still applies.

### B4. Response and audit

- `429 Too Many Requests` + `Retry-After`, added to the §E status matrix (which
  today names only `401`/`403`).
- The `429` is **identical** regardless of whether the username exists —
  throttling must not become an account-existence oracle, undoing §C.
- Audit reuses `auth.login_failed` plus the existing `rate_limit.violated`.

### B5. This does not break the Phase-1 inertness contract

Throttling is live from Phase 1 under **both** `auth.mode` values. That is a
deliberate, narrow exception to "Phase 1 is inert", and it is sound: the throttle
applies only to `/api/v1/auth/login`, a route that **does not exist** before
Phase 1. No pre-existing route changes behaviour, so the PR 4 closeout gate — the
full suite passing unchanged under `auth.mode: disabled` — is unaffected.

---

## What changes in RFC 0039

| Location | Change |
|---|---|
| Non-Goals — *"A web or GUI login"* | Superseded for the **login/session surface only**. The console gets a login form and cookie-borne sessions (§A). Session-*management* UI (listing/revoking sessions) remains a Non-Goal until Phase 3. |
| §D Sessions | Adds the `session_transport` field, the cookie form, and the bearer-first resolution order (§A1). |
| §E Middleware | Adds the same-origin assertion for cookie-resolved identities (§A2) and `429` to the status matrix (§B4). |
| §K REST surface summary | `POST /auth/login` gains `session_transport`; `429` added. |
| Security — *"CSRF / XSS"* | Replaced by §A2/§A3 and [Residual risk](#residual-risk). |
| Security — *"Brute force"* | Throttling is Phase 1 (§B); lockout stays Phase 3. Adds the Argon2id-amplification framing. |
| Phase 1 steps | Step 5 gains `session_transport` + cookie; step 6 gains the same-origin assertion; step 7 gains the login limiters, trusted-proxy config, and the CSP/security headers. |
| Phase 2 steps | Step 5 gains the console login form and the cookie flow. |

## Where this lands

No new PRs. Folded into the [0039 PR plan](0039-pr-plan.md):

- **PR 3** (`rest-middleware`) — `session_transport` + cookie issue/clear; the
  same-origin assertion; both login limiters + trusted-proxy config + schema;
  `429`; the CSP/`nosniff`/`Referrer-Policy` headers on `internal/server/ui.go`;
  the `{@html}` CI gate.
- **PR 5** (`enforcement`) — the console login form and the cookie flow on `401`.
- **PR 6** (`closeout`) — `MT-AUTH-001` grows a browser leg; the web-console
  guide and `SECURITY.md` carry the browser posture and the residuals below.

## Test strategy

- `session_transport: "bearer"` returns a body token and **no** `Set-Cookie`;
  `"cookie"` sets the cookie with all four attributes and returns **no** body
  token; default (field absent) is `"bearer"`.
- Bearer-first resolution when both are presented.
- Cookie-authenticated `POST` without `Origin`/`Sec-Fetch-Site` → `403`; with a
  foreign `Origin` → `403`; with a matching `Origin` → `200`.
- Bearer-authenticated `POST` with no `Origin` → `200` (the CLI regression).
- Logout revokes server-side **and** clears the cookie.
- Login throttling: per-source trip, per-username trip, `429` + `Retry-After`,
  identical `429` for existing and non-existent usernames; the throttle is live
  under `auth.mode: disabled`.
- A `{@html}` introduced under `web/src` fails CI.
- The console response carries the CSP and `frame-ancestors 'none'`.

## Residual risk

- **Session riding under XSS.** `HttpOnly` prevents token *theft*, not *use*: an
  XSS can issue same-origin requests as the operator while the page is open.
  Fully closing this needs per-action re-authentication, which is out of scope for
  v0.3.12. The CSP and the `{@html}` gate are probability reduction, not
  elimination.
- **No password-strength policy.** Phases 1–2 impose no complexity or length
  floor, so throttling protects a password the operator may have chosen badly.
  See [Open questions](#open-questions).
- **Distributed slow guessing.** Per-source and per-username throttling raise cost
  but do not stop a patient attacker spread across many addresses. Per-account
  lockout (Phase 3) is the answer, and it is deliberately not in v0.3.12.
- **`SameSite=Strict` UX.** A link into the console from an external page arrives
  without the cookie and looks logged-out until an in-app navigation. Acceptable
  for an operator console; documented in the guide.
- **TLS remains the deployment's.** Unchanged from §Security — the orchestrator
  cannot observe the proxy.

## Open questions

*All three resolved by the 2026-07-29 maintainer call at the
[0039 PR plan](0039-pr-plan.md) PR 3 gate.*

1. **A minimum password length at `account bootstrap`?** A length floor in PR 4
   is cheap and closes the worst case of the residual above. Recommend yes; the
   full policy stays Phase 3.
   *Resolution (2026-07-29): **yes — a 12-character floor**, enforced at
   `persatrix-server account bootstrap` in PR 4. The full strength policy stays
   Phase 3.*
2. **Cookie session TTL — same as bearer, or shorter?** A browser session is
   likelier to be left open on an unattended screen. Recommend a separate,
   shorter default.
   *Resolution (2026-07-29): **separate, shorter default** — a new
   `auth.cookie_session_ttl` defaulting to `8h` (a workday), while the bearer
   `auth.session_ttl` keeps its `24h` default. Both independently configurable;
   lands in PR 3.*
3. **Cap sizing for the two login limiters** — a concrete number is a PR 3
   decision informed by the existing agent-limiter budget.
   *Resolution (2026-07-29): **per-source 10 attempts / 60 s, per-username 5
   attempts / 60 s**, each on its own `RateLimitConfig` instance with its own
   1000-key LRU (matching the agent limiter's cardinality default, never its
   instance — §B2). Bounds Argon2id amplification to ~10 verifications/min per
   source while leaving room for an operator's fat-fingered retries; lands in
   PR 3.*

## Related documentation

- [RFC 0039 — User Accounts & Authentication](0039-user-accounts-authentication.md) — the authoritative model this amends · [0039 PR plan](0039-pr-plan.md) — where it lands
- [RFC 0048 — Operator & Tester Web Console](0048-operator-tester-web-console.md) — the browser surface in question; its *"beyond localhost requires a reverse proxy until RFC 0039"* posture is what v0.3.12 flips
- [RFC 0009 — Agent Identity, Security & Sandboxing](0009-security-sandboxing.md) — the `RateLimiter` and `AuditLogger` reused here
- [RFC 0002 — REST API Server](0002-rest-api-server.md) — places TLS at the proxy layer
- [v0.3.12 plan](../v0.3.12-plan.md) — the release whose scope re-opened these questions
