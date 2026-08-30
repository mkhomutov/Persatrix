# Manual Test MT-AUTH-001: Accounts & auth — bootstrap, the role gate, the browser session, and the disabled-mode no-delta

**Test ID**: `MT-AUTH-001`
**Feature Area**: Security (human accounts & authentication — RFC 0039 Phases 1–2 + the enabled-mode exposure amendment)
**Version**: 1.0
**Created**: 2026-07-30
**Last Updated**: 2026-07-30
**Status**: Active — authored at RFC 0039 PR 6; **live execution is a v0.3.12 release-prep deliverable** (run against real binaries per [v0.3.12-plan §Acceptance](../v0.3.12-plan.md#acceptance-for-v0312)).

---

## Overview

**Purpose**: Verify the v0.3.12 accounts/auth promise live — **with `auth.mode: enabled`, the web console and REST API are safe to run beyond localhost (over HTTPS), and the caller's `participant_id` is a verified claim**. The arc: first-operator bootstrap on an empty `accounts.db` → the §E 401/403/200 matrix under `enabled` (including what deliberately stays open) → the verified §F claim → the browser cookie session → logout → the `disabled`-mode no-delta.

**Scope**: [RFC 0039](../rfcs/0039-user-accounts-authentication.md) Phases 1–2 as shipped (PRs [#779](https://github.com/mkhomutov/Persatrix/pull/779), [#780](https://github.com/mkhomutov/Persatrix/pull/780), [#790](https://github.com/mkhomutov/Persatrix/pull/790), [#791](https://github.com/mkhomutov/Persatrix/pull/791), [#793](https://github.com/mkhomutov/Persatrix/pull/793)) plus the [enabled-mode exposure amendment](../rfcs/0039-amendment-enabled-mode-exposure.md) (cookie transport, same-origin assertion, throttling).

**Out of Scope** — explicitly deferred, **not asserted** here:

- **Account administration, password change, lockout** — RFC 0039 Phase 3 (v0.4.0). Only the bootstrap operator account exists in this MT.
- **Agent-ingress authorization** — the RFC 0009 agent-token track; this MT *confirms* the carve-out is open (Leg 2), it does not exercise closing it ([ISSUE-0117](../issues/ISSUE-0117-agent-ingress-close-knob.md)).
- **Load/exhaustion behaviour of the login throttle** — the limiter caps are pinned in `internal/server/auth_handlers_test.go`; Leg 2 trips it once, qualitatively.

---

## Related Documentation

- [Accounts & Auth — Operator Guide](../guides/auth.md) — the operator surface this MT walks.
- [RFC 0039](../rfcs/0039-user-accounts-authentication.md) — §E policy matrix, §F verified claim, §G bootstrap, §H rollout.
- [Enabled-mode exposure amendment](../rfcs/0039-amendment-enabled-mode-exposure.md) — the browser posture Leg 4 verifies.
- [Web console guide §Security](../guides/web-console.md#security--exposure-beyond-localhost) — the console-side exposure rule.

**Related Automated Tests** — the deterministic CI backbone of this MT:

- `internal/server/auth_enforcement_test.go` — the §E 401/403 matrix per route class, fail-closed default, method mismatch.
- `internal/server/auth_claim_test.go` — the §F claim in chat POST + history (mismatch → 403, operators included).
- `internal/server/auth_middleware_test.go` / `auth_handlers_test.go` — transports, CSRF matrix, throttles, disabled-mode no-op.
- `internal/accounts/bootstrap_test.go` — §G zero-accounts transaction, 12-char floor.
- `cli/src/commands/auth_tests.rs` + `cli/src/credentials.rs` tests — login/logout/whoami, 0600 credential file.

This live MT confirms the *operator-observable* behaviour on real binaries and a real browser; the invariants themselves are pinned in CI.

---

## Preconditions

1. Fresh build: `make build-orchestrator build-cli` (and `make ui` so the console serves the real bundle).
2. **No `data/accounts.db`** (fresh checkout or delete it) — Leg 1 needs the zero-accounts state.
3. `config/security.yaml` present with the shipped defaults (`auth.mode: disabled` to start; the MT flips it).
4. A browser for Leg 4, on `http://localhost:8080` (loopback — see Edge Case 1 for why plain-HTTP non-loopback is *expected* to fail).
5. No persona fleet needed; a running orchestrator with `--enable-ui` suffices. `PERSATRIX_CREDENTIALS_FILE` unset (use the real `~/.persatrix/credentials`, or point it at a scratch file).

---

## Test Procedure

### Leg 1 — Bootstrap the first operator (§G)

```bash
./bin/persatrix-server account bootstrap --username maksim
```

1. A **short password** (< 12 chars) is refused before the confirm prompt.
2. A 12+-char password with a mismatched confirm is refused.
3. A valid run prints the created account (role `operator`, participant defaulting to the folded username) and exits 0.
4. **Run it again** → refused with "accounts already exist", exit 1 — durable across restart.

### Leg 2 — The §E matrix under `enabled`

Set `auth.mode: enabled` in `config/security.yaml`, start the orchestrator (`--enable-ui`), and probe anonymously:

```bash
curl -s -o /dev/null -w '%{http_code}\n' localhost:8080/api/v1/agents          # 401 (authenticated read)
curl -s -o /dev/null -w '%{http_code}\n' localhost:8080/api/v1/auth/whoami     # 401 (no honest-anonymous under enabled)
curl -s -o /dev/null -w '%{http_code}\n' -X POST localhost:8080/api/v1/sessions # 401 (operator mutation)
curl -s -o /dev/null -w '%{http_code}\n' localhost:8080/healthz                # 200 (public)
curl -s -o /dev/null -w '%{http_code}\n' localhost:8080/api/v1/channels        # 200 (agent ingress — OPEN BY DESIGN)
```

**Pass criterion**: exactly the codes above. The last probe is the deliberate carve-out — anonymous channel reads stay open (RFC 0039 §Non-Goals; [auth guide](../guides/auth.md#what-stays-open-under-enabled--the-agent-ingress)). Also trip the login throttle once: 11 rapid failed logins from one source → the 11th answers `429` + `Retry-After`.

**Optional — the residual WARNs.** They never fire on the loopback bind the rest of this MT uses, so to observe them: restart once with `--http-bind 0.0.0.0` and confirm the startup log carries the agent-ingress WARN (and, with `auth.trusted_proxies` unset, the per-source-limiter degradation WARN), then return to the loopback bind before Leg 3.

### Leg 3 — CLI login, the role gate, and the §F claim

```bash
persatrix login          # bootstrap credentials; token → ~/.persatrix/credentials (verify mode 0600)
persatrix whoami         # the account, role operator, bound participant
TOKEN=$(jq -r '."http://localhost:8080".token' ~/.persatrix/credentials)   # JSON, keyed by orchestrator URL
curl -s -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer $TOKEN" localhost:8080/api/v1/agents  # 200
```

With the bearer, exercise the §F claim on chat history:

```bash
curl -s -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer $TOKEN" \
  'localhost:8080/api/v1/agents/any-agent/chat/history?user_id=somebody-else'   # 403 — operators included
```

**Pass criterion**: the cross-user history read is an explicit `403` (no cross-user read exists at the coarse gate), and `audit.jsonl` now carries `auth.login_succeeded` plus an `authz.denied` record for the 403, checksum-chained.

### Leg 4 — The browser leg (amendment §A)

Open `http://localhost:8080/ui`:

1. The console shows the **login form** (first 401 swaps the content region). Log in with the bootstrap credentials.
2. The console renders normally; `/api/v1/ui/context` (network tab) reports the verified principal with `"authenticated": true`.
3. **The token never enters JS**: `document.cookie` in the devtools console is **empty** (the `__Host-persatrix_session` cookie is `HttpOnly`).
4. **A cross-site write is rejected**: replay a console write (e.g. the chat POST) from `curl` with the cookie but a foreign `Origin`:

   ```bash
   curl -s -o /dev/null -w '%{http_code}\n' -X POST localhost:8080/api/v1/agents/any-agent/chat \
     -H 'Cookie: __Host-persatrix_session=<value from devtools>' \
     -H 'Origin: https://evil.example' -H 'Content-Type: application/json' -d '{"message":"hi"}'   # 403
   ```

5. **Logout clears the cookie**: log out in the console → the login form returns; the logout response carries an expiring `Set-Cookie`; a reload does not resurrect the session.

### Leg 5 — CLI logout

```bash
persatrix logout   # revokes server-side first
persatrix whoami   # 401 → prints the login hint
```

### Leg 6 — The disabled-mode no-delta (§H)

Set `auth.mode: disabled` back, restart:

```bash
curl -s localhost:8080/api/v1/auth/whoami   # 200 — the anonymous `local` identity
persatrix chat <agent>                       # works with no login, as pre-v0.3.12
```

**Pass criterion**: behaviour is indistinguishable from pre-RFC-0039 (the byte-for-byte inertness contract; the full-suite half of this claim is CI's).

---

## Expected Results Summary

| Leg | Surface | Pass criterion | Pass/Fail |
|-----|---------|----------------|-----------|
| 1 — Bootstrap | subcommand | floor + confirm enforced; second bootstrap refused durably | ☐ |
| 2 — §E matrix | anonymous REST | 401/401/401/200/200 exactly; one 429 (optional: the non-loopback WARNs) | ☐ |
| 3 — Role gate + §F | CLI + bearer | 0600 credential file; reads 200; cross-user history 403 + `authz.denied` | ☐ |
| 4 — Browser | console | cookie login; `document.cookie` empty; foreign-`Origin` write 403; logout clears | ☐ |
| 5 — CLI logout | CLI | server-side revocation; whoami 401 + hint | ☐ |
| 6 — Disabled | REST + CLI | anonymous `local`, chat works, no login anywhere | ☐ |

**Overall pass**: all six legs. A Leg 2 carve-out probe answering 401 means the persona fleet is broken — file immediately. A Leg 4.3 non-empty `document.cookie` or a Leg 4.4 `200` is a security regression — release-blocking.

---

## Edge Cases & Error Scenarios

### Edge Case 1: plain HTTP on a non-loopback origin

**Scenario**: Leg 4 is attempted against `http://<lan-ip>:8080/ui`.

**Expected Behavior**: login answers 200 but the browser drops the `Secure` `__Host-` cookie — the login form **reappears with no error**. This is the documented HTTPS requirement ([auth guide](../guides/auth.md#https-is-required-beyond-localhost)), not a bug; run the leg on localhost or behind TLS.

### Edge Case 2: `curl` logout on the cookie transport

**Scenario**: replaying the console's logout from `curl` with only the cookie header.

**Expected Behavior**: `403` — a cookie-authenticated write without `Sec-Fetch-Site`/`Origin` fails the same-origin assertion by design. Use the bearer transport for scripting.

### Edge Case 3: wrong password vs. disabled account

**Scenario**: a failed login for an existing vs. non-existing username.

**Expected Behavior**: identical `401` on the wire (no account-existence oracle); the true reason lives only in the `auth.login_failed` audit record.

---

## Test Results

| Date | Tester | OS | Result | Notes |
|------|--------|----|--------|-------|
| — | — | — | — | Live execution scheduled for v0.3.12 release-prep ([v0.3.12-plan §Acceptance](../v0.3.12-plan.md#acceptance-for-v0312)). The full arc was smoke-verified informally at PR [#791](https://github.com/mkhomutov/Persatrix/pull/791)/[#793](https://github.com/mkhomutov/Persatrix/pull/793) development (scratch enabled-mode boot; CLI + browser cookie login; audit chain verified). |
