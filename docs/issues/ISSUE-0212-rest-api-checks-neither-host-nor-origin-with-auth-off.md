---
id: ISSUE-0212
summary: "With the default `auth.mode: disabled` the REST API relies on being bound to loopback and checks neither the `Host` nor the `Origin` header. A web page the operator opens can reach it through DNS rebinding, and the POST routes that take no body accept a plain cross-site form post."
status: resolved
severity: medium
area: security
created: 2026-11-10
closed: 2026-11-26
refs:
  - internal/server/request_origin.go
  - internal/server/auth_config.go
  - cmd/orchestrator/process_origin_test.go
  - config/security.yaml
  - docs/guides/request-origin-checks.md
  - docs/issues/ISSUE-0286-an-allowed-host-that-is-not-localhost-trusts-the-operators-dns.md
  - internal/server/auth_middleware.go
  - internal/server/channel_convene_handlers.go
  - internal/server/agent_handlers.go
  - config/ui.yaml
  - docker-compose.yaml
  - cmd/orchestrator/auth.go
  - docs/rfcs/0039-user-accounts-authentication.md
  - docs/issues/ISSUE-0196-shipped-shell-allowlist-voids-the-file-and-network-rules.md
---

# ISSUE-0212: With auth off, the REST API checks neither `Host` nor `Origin`

## Summary

`authMiddleware` (`internal/server/auth_middleware.go`) runs its checks
only when auth is enforced. Its same-origin test, `sameOriginAllowed`,
applies to writes that arrive with a session cookie. Under the default
`auth.mode: disabled` no request is checked for where it came from. The
`Host` header is read in one place, inside that same-origin test.

The defence in that mode is the loopback bind. A browser on the same
machine is inside that boundary.

## Context

Two ways in for a web page the operator visits while the stack runs:

- **DNS rebinding.** The page's own host name is re-pointed at
  `127.0.0.1`. The browser then treats the page as same-origin with the
  orchestrator on port 8080, and the page can read every channel and chat
  history, chat as any user id, and submit workflow runs. A submitted run
  reaches the shell tool described in
  [ISSUE-0196](ISSUE-0196-shipped-shell-allowlist-voids-the-file-and-network-rules.md).
- **A plain form post.** Routes that take no body and do not require a
  JSON content type can be called by a cross-site form with no rebinding:
  session archive; unquarantine, unless `SECURITY_UNQUARANTINE_TOKEN` is
  set; and convene on a channel that is already armed, where channel
  config editing is on, as it is in the shipped `config/ui.yaml`.

Recent Chrome versions ask before a public page reaches a local address,
which should narrow the first path in that browser; this was not tested
here. The security docs do not mention rebinding.

## Impact

"Bound to loopback" is weaker than the docs suggest for anyone who
browses the web on the machine that runs the stack, which is the normal
local setup.

## Proposed fix / investigation path

In both auth modes, refuse a request whose `Host` is not on a list of
expected names, and refuse writes whose `Origin` or `Sec-Fetch-Site`
shows another site. The list needs a new setting. The agents reach the
orchestrator as `orchestrator:8080` on the compose network, so a fixed
list of `localhost`, `127.0.0.1` and `[::1]` would cut off the whole
fleet, and no setting for a public name exists today. Add a line on this
to the auth guide.

## Notes

> 2026-11-10 — found by a whole-repository quality review of `main` at
> `ee9cfaac`; confirmed by reading the middleware. No rebinding was
> carried out.

> 2026-11-12 — the shipped `code-writer` no longer has a shell tool (ISSUE-0196's
> config half), so a submitted run reaches one only where an operator's
> own config lists commands; it still reaches `file_read` and `file_write`.

> 2026-11-26 — resolved, on the maintainer's two choices: both checks, in
> both sign-in modes, and the expected names as a list in
> `config/security.yaml`.
>
> **Reproduced first.** On `main` at `c409027b` a real orchestrator process
> on the shipped config served `GET /api/v1/agents` and the channel list
> under `Host: evil.example:8080`, which is what a rebound page sends. A
> cross-site form post archived a session (`204`, and the session then read
> `"archived":true`), and reached the unquarantine and convene routes, which
> answered as they answer the operator. The same was seen in a browser
> (Chrome 156): a page on another site archived a session with one request.
> No DNS rebinding was carried out, and Chrome's prompt for a public page
> that reaches a local address was not tested: the page used was itself on a
> loopback address.
>
> **The Host check** (`internal/server/request_origin.go`). Every route but
> `GET /healthz` refuses a request whose `Host` does not name an allowed
> host: `403`, code `FORBIDDEN`, `host name "<name>" is not expected: add it
> to auth.allowed_hosts in config/security.yaml`. The port and the letter
> case are ignored and an IPv6 address is compared in one spelling. The
> health route is left out because a liveness probe calls it by address.
> The names are `auth.allowed_hosts`, by default `localhost`, `127.0.0.1`
> and `[::1]`; the orchestrator logs them at start-up.
>
> **The cross-origin write check.** A `POST`, `PUT`, `PATCH` or `DELETE`
> is refused when it carries a `Sec-Fetch-Site` other than `same-origin`
> or `none`, or, without that header, an `Origin` that does not match the
> request's `Host`. It is Go's `http.CrossOriginProtection`. A request with
> neither header is not from a browser and passes, so the CLI, the agents
> and scripts are untouched.
>
> **The compose fleet.** The agents call the orchestrator as
> `orchestrator:8080`. The name is added for that stack only, by a new
> flag on the orchestrator's command in `docker-compose.yaml`
> (`--http-extra-allowed-hosts orchestrator`), and is not in the shipped
> `config/security.yaml`, which a run outside compose reads too. A test
> reads every compose file and checks each agent's `--orchestrator-url`
> against the real check.
>
> **What stays.** An allowed name that is not `localhost` can be rebound
> by whoever answers the operator's DNS queries, and under compose
> `orchestrator` is such a name:
> [ISSUE-0286](ISSUE-0286-an-allowed-host-that-is-not-localhost-trusts-the-operators-dns.md).
> Neither check stops a client that is not a browser; the bind address
> and sign-in do.
>
> **Not run.** The compose stack itself: Docker's engine did not answer on
> the machine this was written on, so `make smoke-offline` was not run. The
> fleet's path is held by a real orchestrator process started with the
> compose file's flag and called under `Host: orchestrator:8080`.
