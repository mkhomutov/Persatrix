# Accounts & Auth — Operator Guide

A practical walkthrough of the account/auth surface shipped in v0.3.12
([RFC 0039](../rfcs/0039-user-accounts-authentication.md) Phases 1–2): how to
create the first operator, turn on `auth.mode: enabled`, log in from the CLI
and the web console, what each role may do, and — load-bearing — what enabling
auth does **not** close. With auth enabled, the web console and REST API are
safe to run beyond localhost **over HTTPS**, and the caller's `participant_id`
becomes a verified claim instead of a body field.

> **Spec-level detail** lives in [RFC 0039](../rfcs/0039-user-accounts-authentication.md)
> (§E policy matrix, §F verified claim, §G bootstrap, §H rollout) and the
> [enabled-mode exposure amendment](../rfcs/0039-amendment-enabled-mode-exposure.md)
> (browser cookie transport, CSRF/XSS posture, login throttling). This guide is
> deliberately non-exhaustive and points into both for rationale.

> **Three different "identities" — do not conflate them.** An **account**
> (this guide) is a human login: username, password, role, revocable auth
> sessions. A **participant** is a chat identity (RFC 0016) — each account
> binds to exactly one, which is what makes the claim verified. A **memory
> session** ([sessions guide](sessions.md)) is a *room* — per-`(agent,
> channel)` memory continuity, unrelated to login sessions despite the shared
> word. Logging in does not switch rooms; switching rooms does not touch auth.

> **Known limitation — logging in does not partition persona memory.** What a
> persona remembers is bounded by room membership and
> [RFC 0037](../rfcs/0037-memory-confidentiality-channel-classification.md)
> classification, **never by which account is speaking**. The per-room half of
> the isolation rail is live (the orchestrator binds a session per `(agent,
> channel)`), but it emits no per-request *principal*, so every caller —
> authenticated or not — collapses to the single `local` tenant
> ([ISSUE-0081](../issues/ISSUE-0081-session-id-process-global-not-task-local.md)
> / [ISSUE-0082](../issues/ISSUE-0082-orchestrator-per-request-session-principal-emission.md)
> Part 2, targeted **v0.3.14** per the [sequencing Amendment
> 2026-08-02](../v0.3.x-sequencing.md#amendment-2026-08-02--v0313--v0314-the-two-release-tail-to-v040)).
> So two accounts in one room share that room's memory session by design, and
> the deliberately cross-room tiers (travelling facts, person identity) carry
> across accounts too. Until then, give users who must not share a persona's
> memory separate rooms — and classify those rooms.

---

## Table of Contents

- [The switch: `auth.mode`](#the-switch-authmode)
- [Quick start](#quick-start)
- [The role gate](#the-role-gate)
- [The verified `participant_id` claim](#the-verified-participant_id-claim)
- [The browser session (console login)](#the-browser-session-console-login)
- [HTTPS is required beyond localhost](#https-is-required-beyond-localhost)
- [What stays open under `enabled` — the agent ingress](#what-stays-open-under-enabled--the-agent-ingress)
- [Login throttling](#login-throttling)
- [Operational notes](#operational-notes)
- [Troubleshooting](#troubleshooting)
- [Related documentation](#related-documentation)

---

## The switch: `auth.mode`

Auth is configured in [`config/security.yaml`](../../config/security.yaml)
(validated by `make validate`; an absent file means every default below):

- **`disabled`** (the shipped default) — behaviour is byte-for-byte
  pre-RFC-0039: every request resolves to the anonymous `local` identity, no
  route is gated, `persatrix chat` on localhost needs no login. The
  orchestrator **WARNs at startup** on a non-loopback `--http-bind` while
  disabled.
- **`enabled`** — identity resolution and the
  [§E policy matrix](../rfcs/0039-user-accounts-authentication.md#e-the-auth-middleware-and-the-role-gate)
  are live: anonymous callers get `401` on gated routes, a valid session below
  a route's requirement gets `403` (plus a security-class `authz.denied` audit
  record).

The posture is loud-fail: a **malformed** `security.yaml` stops startup rather
than soft-degrading — a typo'd `mode: enabled` must never silently boot an
unauthenticated deployment.

Accounts and auth sessions live in their own orchestrator-side store
(`data/accounts.db`, `--accounts-db` to relocate) — never in any persona's
`memory.db`. The bundled compose stack relocates it to
`/var/lib/persatrix/accounts.db` on the `orchestrator-data` volume (the
cwd-relative default is not writable in the container) — containerized
`bootstrap` runs must pass the same `--accounts-db` or they write a store
the server never reads; the compose form is under [Operational
notes](#operational-notes).

## Quick start

```bash
# 1. Create the first operator — a local subcommand, never a network call
#    (RFC 0039 §G: no unauthenticated first-account REST hole).
./bin/persatrix-server account bootstrap --username maksim

# 2. Turn auth on.
#    config/security.yaml:  auth: { mode: enabled }
#    Then restart the orchestrator.

# 3. Log in from the CLI (token stored in ~/.persatrix/credentials, mode 0600,
#    keyed by orchestrator URL; every later command attaches it automatically).
persatrix login
persatrix whoami
persatrix logout        # revokes server-side first, then clears the file
```

Bootstrap prompts for the password twice without echo (12-character minimum;
piped stdin works for provisioning: `printf '%s\n%s\n' "$PW" "$PW" | …`) and **refuses to
run once any account exists** — it can never add a second operator or take
over an existing install. Additional accounts are Phase 3 (v0.4.0) territory;
until then a deployment is single-account or operator-provisioned at bootstrap
time only.

The web console needs no separate setup: under `enabled`, the first `401`
swaps the content area for a login form — see
[the browser session](#the-browser-session-console-login).

## The role gate

Two roles, one coarse gate
([§E](../rfcs/0039-user-accounts-authentication.md#e-the-auth-middleware-and-the-role-gate)):

| Route class | Requirement | Examples |
|-------------|-------------|----------|
| `public` | none | `/healthz`, login, the console shell + boot endpoints, and the [agent ingress](#what-stays-open-under-enabled--the-agent-ingress) |
| `authenticated` | any valid session (`user` or `operator`) | reads (agents, workflows, sessions, cost, channel activity/config), chat + chat history |
| `operator` | the `operator` role | every mutation (workflow run/delete, channel create/delete/members/config, session create/archive), unquarantine, persona-memory `recall` |

Anything **not** in the table — including a wrong method on a known path —
fails closed to `operator`: a newly added handler is never accidentally
world-open, and there is no route-existence oracle.

Note the two deliberate `operator` reads: persona-memory `recall` is
read-shaped but memory-exposing, and unquarantine under `enabled` ignores the
`SECURITY_UNQUARANTINE_TOKEN` env token entirely (the token remains only the
`disabled`-mode gate) and stamps the verified participant into the breaker
audit.

## The verified `participant_id` claim

Under `enabled`, chat stops trusting the request body
([§F](../rfcs/0039-user-accounts-authentication.md#f-the-verified-participant_id-claim)):

- **Chat POST** — any body `user_id` is ignored; the turn acts as the
  session's bound participant.
- **Chat history** — the `user_id` query parameter was an unauthenticated
  lookup key; now absent means *your own history*, and naming anyone else is
  an explicit `403` — **operators included**. The coarse gate has no
  cross-user read; a finer administrative story (acting-as with audit) is
  Phase 3+.

## The browser session (console login)

The console logs in with `session_transport: "cookie"`
([amendment §A](../rfcs/0039-amendment-enabled-mode-exposure.md#decision-a--the-browser-session-surface)),
so the browser session rides a `__Host-persatrix_session` cookie that is
`HttpOnly` + `Secure` + `SameSite=Strict` — **the token never enters
JavaScript** (`document.cookie` stays empty), on a deliberately shorter TTL
(`cookie_session_ttl: 8h` vs the bearer's `24h`). Three defences ride with it:

- **CSRF** — beyond `SameSite=Strict`, every cookie-authenticated *write*
  must pass a server-side same-origin assertion (`Sec-Fetch-Site:
  same-origin`, or an `Origin` matching the `Host`), else `403`. Bearer
  callers (the CLI) skip it — a bearer never rides implicitly.
- **XSS probability** — a CSP (`script-src 'self'`, `frame-ancestors 'none'`)
  + `nosniff` + `Referrer-Policy` on every console response, and a CI gate
  rejecting `{@html}` anywhere under `web/src`.
- **Residual, recorded** (the amendment's
  [Residual risk](../rfcs/0039-amendment-enabled-mode-exposure.md#residual-risk)):
  `HttpOnly` prevents token *theft*, not *use* — an XSS while the page is
  open can still issue same-origin requests as the operator. And
  `SameSite=Strict` means a link into the console from an external page
  arrives cookie-less and *looks* logged out until an in-app navigation —
  expected, not a bug.

## HTTPS is required beyond localhost

**The failure mode is a silent login loop, not an error.** The session cookie
is `__Host-`-prefixed, which requires `Secure` — and a browser silently drops
a `Secure` cookie set over plain HTTP on a non-loopback origin. Login answers
`200`, the cookie never sticks, the next request `401`s, and the console shows
the login form again with nothing in the console or the server log to explain
it. `http://localhost` works (browsers treat loopback as trustworthy);
`http://<lan-ip>` does not.

So the exposure rule is: **`auth.mode: enabled` beyond localhost means the
console must be served over HTTPS** — terminate TLS in front of the
orchestrator (TLS remains the deployment's concern, unchanged from the RFC's
§Security posture). The CLI's bearer transport has no such cliff, but the
token deserves TLS on a routable network just as much.

If a TLS-terminating proxy fronts the orchestrator, also set
`auth.trusted_proxies` so the login throttle sees real client addresses (see
[Login throttling](#login-throttling)).

## What stays open under `enabled` — the agent ingress

Enabling auth gates the **human** surface. The **agent-attributable REST
ingress stays public by design** — persona agents hold no accounts (RFC 0039
§Non-Goals places their authorization on the
[RFC 0009](../rfcs/0009-security-sandboxing.md) agent-token track), and
gating these routes would break every deployed persona fleet:

- `POST /api/v1/agents/register` and `DELETE /api/v1/agents/{id}`
  (self-registration/deregistration),
- `GET /api/v1/channels`, `GET /api/v1/channels/{id}`,
  `GET`/`POST /api/v1/channels/{id}/messages` (the RFC 0011 channel seams the
  fleet publishes and catches up through),
- `POST /api/v1/channels/{id}/convene` (the standing-schedule callback).

Two consequences to take seriously on a non-loopback `enabled` bind (the
orchestrator WARNs about the residual at startup):

- **Anonymous channel reads remain possible.** Channel lists and message
  histories are readable without a login — including channels classified
  `restricted`/`secret` under
  [RFC 0037](../rfcs/0037-memory-confidentiality-channel-classification.md).
  Classification governs **persona memory egress** (what a persona may carry
  *out* of a room), not REST read access to the room's own stored messages;
  auth does not change that. Until the
  [ISSUE-0117](../issues/ISSUE-0117-agent-ingress-close-knob.md)
  `auth.agent_ingress` close knob lands, a deployment with confidential
  channel content should keep the orchestrator loopback-bound or
  network-restrict the `/api/v1/channels` paths (proxy rule / firewall) to
  the agent fleet.
- **Anonymous channel writes remain possible** on the same paths (they are
  the production persona transport). The defence is the RFC 0009 per-agent
  rate limiter + quarantine, unchanged.

A fleet-less deployment (no persona agents calling back over REST) has no
reason to keep this surface reachable at all — that is exactly ISSUE-0117.

## Login throttling

Live under **both** modes, from the endpoint's first day
([amendment §B](../rfcs/0039-amendment-enabled-mode-exposure.md#decision-b--login-throttling-ships-with-the-endpoint)):
per-source (10/60 s — every failed login burns a full Argon2id verification,
so this bounds a CPU-amplification flood) and per-username (5/60 s — targeted
guessing across rotating sources), each on its own tracked-key LRU, answering
an **identical** `429` + `Retry-After` whether or not the username exists.
Behind a reverse proxy, set `auth.trusted_proxies` (CIDRs) or the per-source
limiter degrades to a global one — WARN'd at startup under `enabled`.

## Operational notes

- **Audit trail**: `auth.login_succeeded` / `auth.login_failed` /
  `auth.logout` / `authz.denied` are security-class (fsync'd) audit events —
  metadata only, never the password or token. The failed-login record keeps
  the true reason (`invalid_credentials` vs `account_disabled`) operator-side
  while the wire `401` stays identical.
- **Sessions are opaque and revocable**: only a hash is stored server-side;
  `persatrix logout` (or the console's logout) revokes server-side first and
  clears local state only once the orchestrator confirms.
- **No password reset until Phase 3.** The pragmatic single-operator
  recovery: stop the orchestrator, remove the accounts store (accounts and
  auth sessions only — no persona memory, no channels), and bootstrap again.
  On host runs that is `rm data/accounts.db` followed by the [quick
  start](#quick-start)'s `account bootstrap`. Under the compose stack the
  store lives at `/var/lib/persatrix/accounts.db` **inside** the
  `orchestrator-data` volume, which also carries `channels.db`, the log
  buffer, and `audit.jsonl` — so remove the one file, never the volume
  (`docker volume rm orchestrator-data` takes your channels with it):

  ```bash
  docker compose down
  docker compose run --rm --no-deps --entrypoint sh orchestrator \
    -c 'rm -f /var/lib/persatrix/accounts.db'
  docker compose run --rm --no-deps orchestrator \
    account bootstrap --username <name> \
    --accounts-db /var/lib/persatrix/accounts.db
  docker compose up -d
  ```
- **The CLI hints on 401**: any command answered `401` prints a
  `persatrix login` hint. A `403` is a *role* problem, not a login problem —
  no hint.

## Troubleshooting

| Symptom | Cause / fix |
|---------|-------------|
| Console login form reappears after a successful login, no error | Plain HTTP on a non-loopback origin — the `Secure` cookie was dropped. Serve HTTPS or use `http://localhost`. See [HTTPS is required](#https-is-required-beyond-localhost). |
| `403` on channel edit / workflow run after logging in | The account's role is `user`; mutations need `operator`. |
| `429` on login | The per-source or per-username throttle; wait `Retry-After` seconds. Behind a proxy, unset `trusted_proxies` makes all logins share one source bucket. |
| `curl` cookie logout answers `403` | Cookie-authenticated writes require the same-origin assertion headers a browser sends; `curl` without `Origin`/`Sec-Fetch-Site` fails it by design. Use the bearer transport for scripting. |
| Console link from another page looks logged out | `SameSite=Strict` — navigate in-app once; the session is intact. |
| Bootstrap refuses: accounts already exist | By design (§G). Use the existing credential, or see the recovery note under [Operational notes](#operational-notes). |

## Related documentation

- [RFC 0039 — User Accounts & Authentication](../rfcs/0039-user-accounts-authentication.md) — the spec: §E matrix, §F claim, §G bootstrap, §H rollout, §J CLI surface.
- [Enabled-mode exposure amendment](../rfcs/0039-amendment-enabled-mode-exposure.md) — cookie transport, CSRF/XSS posture, throttling, residual risk.
- [Web console guide §Security](web-console.md#security--exposure-beyond-localhost) — the console-side view of the same posture.
- [Sessions guide](sessions.md) — *memory* sessions (rooms); not auth sessions.
- [RFC 0009 — Agent Identity, Security & Sandboxing](../rfcs/0009-security-sandboxing.md) — the agent-identity axis; owns the agent-ingress authorization story.
- [ISSUE-0117 — agent-ingress close knob](../issues/ISSUE-0117-agent-ingress-close-knob.md) — closing the carve-out for fleet-less deployments.
- [MT-AUTH-001](../manual-tests/MT-AUTH-001.md) — the live acceptance arc (bootstrap → matrix → browser leg → disabled no-delta).
