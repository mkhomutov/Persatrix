---
id: ISSUE-0290
summary: "Behind a reverse proxy that browsers reach over plain HTTP and that sends its upstream's name as `Host`, the web console loads and every write it makes is refused with `403`. A browser sends no `Sec-Fetch-Site` to a plain-HTTP address that is not `localhost`, so the cross-origin check compares the write's `Origin` with the rewritten `Host`, and the two differ. The operator has no setting that names a trusted origin."
status: open
severity: low
area: server
created: 2026-11-26
refs:
  - internal/server/request_origin.go
  - internal/server/auth_config.go
  - config/security.yaml
  - docs/guides/request-origin-checks.md
  - docs/guides/auth.md
  - docs/issues/ISSUE-0212-rest-api-checks-neither-host-nor-origin-with-auth-off.md
  - docs/issues/ISSUE-0286-an-allowed-host-that-is-not-localhost-trusts-the-operators-dns.md
  - docs/issues/ISSUE-0117-agent-ingress-close-knob.md
  - https://github.com/mkhomutov/Persatrix/pull/1147
---

# ISSUE-0290: Behind a plain-HTTP proxy that rewrites `Host`, the console's writes are refused

## Summary

Since [ISSUE-0212](ISSUE-0212-rest-api-checks-neither-host-nor-origin-with-auth-off.md)
the orchestrator refuses a write that a browser marks as coming from another
origin (`internal/server/request_origin.go`). One honest set-up looks the
same to that check as a write from another site: a reverse proxy that
browsers reach over plain HTTP and that sends its upstream's name as `Host`.
The console loads, and every write it makes answers `403`. The operator can
change the proxy, and has no setting on the orchestrator's side.

## Context

- A write passes when the browser marks it `Sec-Fetch-Site: same-origin` or
  `none`. Without that header, its `Origin` must name the same host and port
  as the request's `Host`.
- A browser sends `Sec-Fetch-Site` only to an address it counts as secure: an
  HTTPS address, `localhost` or a loopback address. To any other plain-HTTP
  address it sends none. That is the rule of the Fetch Metadata
  specification, and it was seen in Chromium 156 during the review of
  [#1147](https://github.com/mkhomutov/Persatrix/pull/1147); no other browser
  was run.
- The set-up: browsers open `http://console.lan`. The proxy forwards to
  `127.0.0.1:8080` and sends that as `Host`, which is what nginx does unless
  told otherwise (from its documentation; no proxy was run). `127.0.0.1` is
  an allowed host, so the page loads. A write then carries
  `Origin: http://console.lan`, no `Sec-Fetch-Site`, and
  `Host: 127.0.0.1:8080`. The two names differ, and the answer is `403`,
  `cross-origin write refused: the browser reports that this request comes
  from another origin`. The orchestrator logs `request refused: a write that
  the browser marks as coming from another origin` at `WARN`, with the
  `origin` it saw.
- The check trusts no other origin: `originMiddleware` uses Go's
  `http.CrossOriginProtection` as it comes, and `config/security.yaml` has no
  key for one.
- With sign-in off this set-up worked before ISSUE-0212's fix, when nothing
  was checked. With `auth.mode: enabled` it never worked: the session cookie
  needs HTTPS beyond `localhost`
  ([auth guide](../guides/auth.md#https-is-required-beyond-localhost)). So
  what is affected is a console with sign-in off, reached through a
  plain-HTTP proxy.

## What an operator can do today

Both were checked on 2026-11-26 against a running orchestrator built from
that pull request, with requests shaped as a browser sends them. No proxy and
no browser were part of that run.

- **Have the proxy pass the browser's own `Host` through, port included, and
  list that name in `auth.allowed_hosts`.** A session was created (`201`)
  under `Host: console.lan` with `Origin: http://console.lan`, and under
  `Host: console.lan:8081` with `Origin: http://console.lan:8081`. With the
  name passed and the port dropped, `Host: console.lan` against
  `Origin: http://console.lan:8081`, the write was refused. In nginx the
  line is `proxy_set_header Host $http_host;` (from its documentation).
- **Serve the proxy over HTTPS.** The browser then marks the console's
  writes `same-origin`, and they pass whichever `Host` the proxy sends, as
  long as that name is allowed: `201` with the upstream's name as `Host` and
  `201` with the public name.

The [guide](../guides/request-origin-checks.md#behind-a-reverse-proxy) has
the three cases as a table, and a row in its troubleshooting table.

## Impact

Proposed low, not ruled, on the precedent of
[ISSUE-0117](ISSUE-0117-agent-ingress-close-knob.md) (low), where an
operator also lacks a setting and the guide says what to do instead. The
check fails closed: writes are refused and nothing is exposed. What an
operator meets is a console that loads and cannot write after an upgrade,
with a refusal that does not name the proxy as the cause; the Upgrade Notes
and the guide do.

## Proposed fix / investigation path

Options, not a decision:

1. **A list of trusted origins**, for example `auth.trusted_origins`, handed
   to `http.CrossOriginProtection.AddTrustedOrigin`. An entry is a scheme, a
   host and an optional port. It is the smallest change. Its cost: an
   operator who lists a plain-HTTP origin trusts whoever can answer for that
   name on the network, the weakness
   [ISSUE-0286](ISSUE-0286-an-allowed-host-that-is-not-localhost-trusts-the-operators-dns.md)
   describes for an allowed host.
2. **Believe a forwarded host from a listed proxy only.** When the
   connection comes from an address in `auth.trusted_proxies`, compare the
   `Origin` with the host the proxy says the browser asked for. No new
   setting; a second mechanism beside the list, and the proxy has to send
   the header.
3. **Leave the check and say more when it refuses.** A write refused with an
   `Origin` and no `Sec-Fetch-Site` could be answered, and logged, with both
   names and a pointer to the guide. It fixes nothing and shortens the
   search.

Whichever is taken, the guide's proxy table and the Upgrade Notes row change
with it.

## Notes

> 2026-11-26 — filed from the review of
> [#1147](https://github.com/mkhomutov/Persatrix/pull/1147), whose texts had
> said that only an old browser leaves `Sec-Fetch-Site` out. That pull request
> corrected the texts and changes no behaviour for this case.
