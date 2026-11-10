---
id: ISSUE-0212
summary: "With the default `auth.mode: disabled` the REST API relies on being bound to loopback and checks neither the `Host` nor the `Origin` header. A web page the operator opens can reach it through DNS rebinding, and the POST routes that take no body accept a plain cross-site form post."
status: open
severity: medium
area: security
created: 2026-11-10
refs:
  - internal/server/auth_middleware.go
  - internal/server/channel_convene_handlers.go
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
  unquarantine, session archive, and convene where channel config editing
  is on.

Recent Chrome versions ask before a public page reaches a local address,
which reduces the first path there; other browsers do less. The security
docs do not mention rebinding.

## Impact

"Bound to loopback" is weaker than the docs suggest for anyone who
browses the web on the machine that runs the stack, which is the normal
local setup.

## Proposed fix / investigation path

In both auth modes, refuse a request whose `Host` is not an expected name
(`localhost`, `127.0.0.1`, the configured public name), and refuse writes
whose `Origin` or `Sec-Fetch-Site` shows another site. Add a line on this
to the auth guide.

## Notes

> 2026-11-10 — found by a whole-repository quality review of `main` at
> `ee9cfaac`; confirmed by reading the middleware. No rebinding was
> carried out.
