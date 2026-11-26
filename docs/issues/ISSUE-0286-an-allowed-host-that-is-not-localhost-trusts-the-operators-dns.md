---
id: ISSUE-0286
summary: "The Host check accepts every name on `auth.allowed_hosts`. A browser resolves `localhost` itself and an IP address needs no lookup, but any other name is looked up, so whoever answers the operator's DNS queries can point it at their own server first and at `127.0.0.1` afterwards. The compose stack has to allow `orchestrator` for its agents, so a browser on a machine that runs the stack is open to this on a hostile network."
status: open
severity: low
area: security
created: 2026-11-26
refs:
  - internal/server/request_origin.go
  - cmd/orchestrator/auth.go
  - docker-compose.yaml
  - config/security.yaml
  - docs/guides/request-origin-checks.md
  - docs/issues/ISSUE-0212-rest-api-checks-neither-host-nor-origin-with-auth-off.md
  - docs/issues/ISSUE-0117-agent-ingress-close-knob.md
  - https://github.com/mkhomutov/Persatrix/pull/1147
---

# ISSUE-0286: An allowed host that is not `localhost` trusts the operator's DNS

## Summary

Since [ISSUE-0212](ISSUE-0212-rest-api-checks-neither-host-nor-origin-with-auth-off.md)
the orchestrator refuses a request whose `Host` is not an allowed host
(`internal/server/request_origin.go`). That stops a page on an ordinary web
site: its owner controls the DNS of their own name only, and that name is not
on the list.

It does not stop someone who answers the operator's own DNS queries. Such a
person can make an allowed name mean their server, serve a page from it, and
then make the same name mean `127.0.0.1`. The browser sends the allowed name
as `Host`, and the page reads and writes the API.

## Context

- `localhost` is safe: browsers resolve it to the loopback address
  themselves and ask no server. `127.0.0.1` and `[::1]` are addresses, so
  nothing is looked up. The shipped `config/security.yaml` lists only these
  three.
- The compose stack's agents call the orchestrator as
  `http://orchestrator:8080`, so `docker-compose.yaml` adds `orchestrator`
  with `--http-extra-allowed-hosts`. The orchestrator's port is published on
  the host's `127.0.0.1:8080`, where the operator's browser reaches it. A
  request from the browser and a request from an agent arrive on the same
  port, and the check cannot tell them apart.
- `orchestrator` is a single-label name. On a network whose DNS, or whose
  local name service, is in someone else's hands (a public Wi-Fi, a
  compromised router), that person can answer for it.
- An operator who adds a name of their own, such as `persatrix.example`, is
  in the same position for that name.
- The second check does not help here: after the name is rebound the
  browser treats the page as the orchestrator's own origin and marks its
  writes `same-origin`.
- With `auth.mode: enabled` the rebound page has no session: the operator's
  session cookie belongs to the name they signed in under, which is not the
  rebound one unless they opened the console by it, and a browser sends no
  bearer token on its own. The routes that stay open without a session are
  the agent ingress
  ([ISSUE-0117](ISSUE-0117-agent-ingress-close-knob.md)).

## Impact

Proposed low, not ruled: the attacker must already control name lookups on
the operator's network, the operator must browse while the compose stack
runs with sign-in off, and the attacker must know or guess the port. What
they gain is what ISSUE-0212 described: the channels and chats, a chat as
any user, a workflow run. Before ISSUE-0212's fix any web page could do this;
now it takes a position on the network.

## Proposed fix / investigation path

Give the fleet its own way in, so that the port a browser can reach never
has to answer to a looked-up name:

- serve the agents' REST calls on a second port that compose does not
  publish on the host, and let the published port answer to `localhost` and
  the two addresses only; or
- accept a fleet name only together with an agent token (RFC 0009 Phase 4),
  which a browser page does not hold.

Until then the guide says it: do not browse on a network you do not trust
while the stack runs with sign-in off, or turn sign-in on.

## Notes

> 2026-11-26 — found while fixing ISSUE-0212, by reasoning about which
> names a browser resolves by itself; no such attack was carried out. Filed
> with that fix, [#1147](https://github.com/mkhomutov/Persatrix/pull/1147).
> The severity is proposed, not ruled.
