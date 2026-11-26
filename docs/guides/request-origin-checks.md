# Where a Request May Come From — Operator Guide

The orchestrator's REST API and web console check two things about every
request before anything else looks at it: that it is addressed to a name the
operator expects, and that it is not a write sent by a web page on another
origin. Both checks run whether sign-in (`auth.mode`) is on or off. This guide
says why they exist, what each refuses, how to add a name, and what they do
not protect against.

> The checks answer [ISSUE-0212](../issues/ISSUE-0212-rest-api-checks-neither-host-nor-origin-with-auth-off.md).
> The code is `internal/server/request_origin.go`. Sign-in itself is the
> [accounts & auth guide](auth.md).

## Table of Contents

- [Why a loopback bind is not enough](#why-a-loopback-bind-is-not-enough)
- [The two checks](#the-two-checks)
- [The setting: `auth.allowed_hosts`](#the-setting-authallowed_hosts)
- [How a `Host` is compared](#how-a-host-is-compared)
- [What is never refused](#what-is-never-refused)
- [The web console](#the-web-console)
- [Behind a reverse proxy](#behind-a-reverse-proxy)
- [What the checks do not do](#what-the-checks-do-not-do)
- [Troubleshooting](#troubleshooting)

---

## Why a loopback bind is not enough

With sign-in off, which is the shipped default, the orchestrator relies on
being bound to `127.0.0.1`: nothing outside the machine can connect. A web
browser on the same machine is inside that boundary, and any page it has open
can try to use it. Two ways were open before these checks.

**DNS rebinding.** You open a page on `evil.example`. Its owner then points
the name `evil.example` at `127.0.0.1`. The browser still thinks it is talking
to the site it loaded, so it lets the page's script call
`http://evil.example:8080/api/v1/...`, which now reaches your orchestrator,
and lets the script read the answers. Such a page could read every channel
and chat, chat as any user and submit workflow runs.

**A cross-site form.** A page on any site can submit a plain HTML form to
`http://localhost:8080` without reading the answer. Routes that take no body
never asked for a JSON content type, so a form could archive a session,
release a quarantined agent, or convene an armed channel.

## The two checks

| Check | Applies to | Refused when | Answer |
|-------|-----------|--------------|--------|
| **Host** | every request, on every route but `/healthz` | the `Host` header does not name an [allowed host](../ai-glossary.md#allowed-host) | `403`, code `FORBIDDEN`, `host name "<name>" is not expected: add it to auth.allowed_hosts in config/security.yaml` |
| **Cross-origin write** | every `POST`, `PUT`, `PATCH` and `DELETE` | the browser marks the request as coming from another origin | `403`, code `FORBIDDEN`, `cross-origin write refused: the browser reports that this request comes from another origin` |

The Host check stops rebinding: the browser sends the page's own name as
`Host`, and a page cannot change that header. The cross-origin check stops
the form: browsers mark where a request came from, in `Sec-Fetch-Site` and,
before that header existed, in `Origin`.

A write is judged this way:

- `Sec-Fetch-Site: same-origin` or `none` passes. `none` is what a browser
  sends for something the user did directly, such as typing an address.
- Any other `Sec-Fetch-Site` value is refused: `cross-site`, and also
  `same-site`, which is what a browser sends from another port of the same
  name or from a sibling name of the same site.
- With no `Sec-Fetch-Site`, an `Origin` must carry the same host and port as
  the request's own `Host`. `Origin: null` is refused.
- A request with neither header is not from a browser, and passes.

Each refusal is also logged by the orchestrator at `WARN`, with the name or
the headers it saw, cut to 64 bytes.

## The setting: `auth.allowed_hosts`

The names live in [`config/security.yaml`](../../config/security.yaml):

```yaml
auth:
  allowed_hosts:
    - localhost
    - 127.0.0.1
    - "[::1]"
```

Those three are also the default when the key or the file is absent. To
reach the orchestrator under another name, add it:

```yaml
auth:
  allowed_hosts:
    - localhost
    - 127.0.0.1
    - "[::1]"
    - persatrix.example
    - 192.168.1.20
```

- Write a host name or an IP address on its own: no `http://`, no port, no
  path, no `*`. An entry that is none of these stops the orchestrator at
  start-up with the entry in the error, and `make validate` refuses it.
- Quote an IPv6 address. Unquoted, YAML does not read `[::1]` as text.
- A list replaces the default, so keep the names you still use.
- The list cannot be empty, and there is no way to switch the check off.

At start-up the orchestrator logs the names it answers to, in the
`allowed_hosts` field of the line `auth: the REST API and the web console
answer to these host names only, and a request under any other is refused`.

**The compose stack.** Its agents call the orchestrator as
`http://orchestrator:8080`, a name that exists only on the compose network.
`docker-compose.yaml` adds it for that stack with the orchestrator's
`--http-extra-allowed-hosts orchestrator` flag, which takes a comma-separated
list and adds to the configured names for one process. A run outside compose
reads the same `config/security.yaml` and does not answer to `orchestrator`.
If you write your own compose file, or rename the service, pass the flag or
list the name.

## How a `Host` is compared

- **The port is ignored.** `localhost:8080`, `localhost:5173` and `localhost`
  are one name. A rebound page keeps its own name on any port, so the port
  says nothing about who is asking, and you may publish the orchestrator on
  another port without touching the list.
- **Letter case is ignored.**
- **An address is compared as an address.** `[::1]` and
  `[0:0:0:0:0:0:0:1]` are the same; `127.0.0.1` and `127.0.0.2` are not, and
  no address passes because it is a loopback or a private one.
- **Nothing else is folded.** `localhost.` with a trailing dot, or a
  subdomain of an allowed name, is another name.
- **A request with no `Host`** (possible in HTTP/1.0) is refused.

## What is never refused

- **`GET /healthz`.** A liveness probe calls it with an address as `Host`
  (a Kubernetes probe sends the pod's own address), and a refused probe
  would restart the orchestrator for ever. The route says only that an
  orchestrator is running and which parts it runs without.
- **A client that is not a browser, under an allowed name.** The CLI, the
  agents, `curl` and scripts send no `Sec-Fetch-Site` and no `Origin`, so
  the cross-origin check passes them.
- **Reads from another site.** A `GET` is not refused by the cross-origin
  check: the browser does not let a page on another site read the answer,
  and a rebound page fails the Host check. The event streams (`.../stream`)
  are reads.

## The web console

The console served by the orchestrator at `/ui` calls the API on its own
origin, so its writes carry `Sec-Fetch-Site: same-origin` and pass. Open it
under an allowed name: `http://localhost:8080/ui`, not a LAN address you have
not listed.

In development (`npm run dev` in `web/`), the browser talks to the Vite
server and Vite passes `/api` on to the orchestrator. Vite sends the name of
its target as `Host` (`localhost:8080` by default), which is allowed, and
passes on the browser's `Sec-Fetch-Site: same-origin`, so writes pass. If
you point `VITE_API_TARGET` at another name, list that name. A browser too
old to send `Sec-Fetch-Site` is refused through Vite, because its `Origin`
names the Vite port and the `Host` names the orchestrator's.

## Behind a reverse proxy

The orchestrator compares the `Host` it receives. It does not read
`X-Forwarded-Host`, which any client can set. So list the name the proxy
sends: the public name if the proxy passes `Host` through, the upstream name
if it rewrites it.

Pass the public `Host` through where you can. A browser too old to send
`Sec-Fetch-Site` is judged by `Origin`, which carries the public name, and
that only matches a `Host` the proxy did not rewrite.

## What the checks do not do

- **They do not stop a client that is not a browser.** Such a client writes
  any `Host` it likes. What keeps it out is the bind address, the network,
  and sign-in ([`auth.mode: enabled`](auth.md)).
- **They are not sign-in.** A page on an allowed name, such as the console
  itself, is let through and then meets whatever sign-in is on.
- **A name that is not `localhost` trusts your DNS.** Browsers resolve
  `localhost` themselves, and an address needs no lookup. Any other allowed
  name can be rebound by whoever answers your DNS queries, for example on a
  hostile network. Under compose that includes `orchestrator`
  ([ISSUE-0286](../issues/ISSUE-0286-an-allowed-host-that-is-not-localhost-trusts-the-operators-dns.md)).
- **They do not send CORS headers.** A page on another origin still cannot
  read the API, which is the intent.

## Troubleshooting

| Symptom | Cause / fix |
|---------|-------------|
| `403` with `host name "..." is not expected` | You reached the orchestrator under a name that is not listed. Add the name in the message to `auth.allowed_hosts`, or use `localhost`. |
| Agents log `Failed to register agent ...: HTTP 403` with that message | The agents call the orchestrator by a name it does not expect. Under compose, keep `--http-extra-allowed-hosts orchestrator` on the orchestrator's command; elsewhere, list the name. |
| The orchestrator exits at start-up with `auth.allowed_hosts: "..." is not a host name or an IP address on its own` | An entry has a port, a scheme, a path or a wildcard. Write the bare name. The same message with `--http-extra-allowed-hosts:` in front names the flag. |
| The orchestrator exits at start-up with a YAML parse error after you added an IPv6 address | Quote it: `"[::1]"`. |
| `403` with `cross-origin write refused` from a page you wrote | The page is on another origin, which includes another port. Serve it from the orchestrator's origin, or call the API from a server, not from the browser. |
| The health check passes and everything else is `403` | The probe is exempt; the name you use is not listed. |
