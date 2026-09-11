---
id: ISSUE-0152
summary: "Agent IDs are self-reported and unchecked, and while ANY agent ID is quarantined the orchestrator refuses every call that sends none — today every REST call from the Python agents and every CLI command — so about 65 quick requests under an invented ID, with no account in either auth mode, stop all persona channel replies and the CLI until an operator releases the ID, through an unquarantine endpoint that refuses anonymous callers too"
status: open
severity: high
area: security
created: 2026-09-11
refs:
  - docs/rfcs/0009-security-sandboxing.md
  - docs/rfcs/0039-user-accounts-authentication.md
  - docs/rfcs/0040-agent-orchestrator-transport-unification.md
  - docs/rfcs/0048-operator-tester-web-console.md
  - docs/manual-tests/v0.3.0-execution-report.md
  - docs/issues/ISSUE-0111-anonymous-wallet-rpcs-share-rate-limit-bucket.md
  - internal/security/middleware.go
  - internal/security/circuitbreaker.go
  - internal/security/security.go
  - cmd/orchestrator/ratelimit.go
  - cmd/orchestrator/main.go
  - internal/server/server.go
  - internal/server/auth_policy.go
  - internal/server/agent_handlers.go
  - internal/server/server_unquarantine_test.go
  - agents/server.py
  - agents/channel_publisher.py
  - cli/src/main.rs
---

## Summary

Anyone who can reach the orchestrator's REST port can stop every persona's
channel replies and almost every CLI command, with about 65 quick requests and
no account. Two rules combine to allow it:

- An agent ID that keeps going over its rate limit is quarantined: the
  orchestrator refuses its calls until an operator releases it. Agent IDs are
  self-reported and nothing checks them, so an outsider can invent one and
  trip it.
- While any ID is quarantined, the orchestrator also refuses every call that
  sends no agent ID. The Python agents' REST calls and the Rust CLI send none.

Nothing recovers on its own, and the release endpoint refuses callers without
an agent ID too.

## Context

**Callers name themselves.** A REST caller puts its
[agent ID](../ai-glossary.md#agent-id) in the `X-Agent-ID` header, a gRPC
caller in `x-agent-id` metadata. The rate-limit hooks in
`internal/security/middleware.go` read both and check only the shape (1–256
characters of the agent ID pattern). The file's own comment says "Self-reported
until token validation lands in Phase 4": the orchestrator-issued agent tokens
of [RFC 0009 Phase 4](../rfcs/0009-security-sandboxing.md#phase-4-agent-identity-tokens--hitl-gates),
planned for v0.4.0. A caller that sends no ID is anonymous, and all anonymous
callers share one rate-limit budget.

**How an ID is quarantined.** Each ID may make 60 calls a minute by default.
Every call refused for going over counts as a violation against that ID (calls
with no ID never count, so it takes an invented ID), and five violations in ten
minutes quarantine it: the circuit breaker's `ViolationRateLimit` rule in
`cmd/orchestrator/ratelimit.go`. That is the only way in today, because the
rate-limit refusal is the only code that records a violation. A quarantine
never ends by itself ("no automatic recovery", `internal/security/circuitbreaker.go`).
Two things end it: an operator's `POST /api/v1/agents/{id}/unquarantine`, or an
orchestrator restart, because the quarantine list lives only in memory. No
endpoint lists quarantined IDs; the operator finds the ID in the audit log's
`agent.quarantined` event.

**The refusal.** [#244](https://github.com/mkhomutov/Persatrix/pull/244), which
built the rate limiter, added a rule from its review (finding H-01): while any
ID is quarantined, a call that sends no ID is refused, with
`403 QUARANTINE_ACTIVE` on REST and `PermissionDenied` on gRPC. The aim was to
stop a quarantined agent from dropping its header and carrying on as anonymous.
But the rule asks "is anyone quarantined?", not "is this caller the quarantined
one?", so it refuses every anonymous caller.

**Who sends no agent ID today.**

| Caller | Sends an ID? | Refused while any ID is quarantined |
|---|---|---|
| Python agents, REST calls | No: `agents/server.py` builds one shared HTTP session with no headers, and `agents/channel_publisher.py` posts through it | Channel publishes; history, roster and start-up catch-up reads; convene callbacks; verbatim recall; registration and deregistration |
| Python agents, wallet calls (gRPC) | Yes, since [ISSUE-0111](ISSUE-0111-anonymous-wallet-rpcs-share-rate-limit-bucket.md) | Nothing |
| Python agents, log stream (gRPC) | No, but the rate-limit hook covers only one-request, one-reply calls, and the log stream is a stream | Nothing |
| Rust CLI | No: `cli/src/main.rs` attaches only `Authorization` | Every command except `login`, `logout` and `whoami`, whose routes sit outside the rate limiter |
| Web console | Yes: a fixed ID, `web-console`, that is never quarantined; its page and boot routes sit outside the rate limiter | Nothing |

So the gRPC half of the rule catches no agent traffic today, and the REST half
catches all of it. The `internal/security` package comment, rewritten by
[#905](https://github.com/mkhomutov/Persatrix/pull/905), states both halves;
no issue tracks what they add up to.

**It has happened.** During the v0.3.0 release checks, a `curl` loop sent 70
requests as `probe-1778578135`, and the audit log recorded that ID's
quarantine (the rate-limit row of the
[v0.3.0 execution report](../manual-tests/v0.3.0-execution-report.md#docker-smoke-test)).
The operator note under that row reads the lockout as the anonymous caller
itself being quarantined, and records the way out: send the release call with
an invented `X-Agent-ID: operator`. It was marked not a release blocker and
left for a runbook; no guide explains it. The unquarantine tests do the same:
the ones that release a real quarantine send `X-Agent-ID: operator`, because
the rule refuses anonymous calls (`internal/server/server_unquarantine_test.go`).

**The console was moved out of its way.** The embedded web console
([RFC 0048](../rfcs/0048-operator-tester-web-console.md)) registers its routes
beside `/healthz`, outside the rate limiter, because "leaving it under the H-01
deny would 403 the console whenever an agent is quarantined"
(`internal/server/server.go`, `Handler`).
[#592](https://github.com/mkhomutov/Persatrix/pull/592) gave the console's API
calls a fixed ID that is never quarantined. The agents and the CLI got neither
fix.

**Signing in does not close it.** Under `auth.mode: enabled`
([RFC 0039](../rfcs/0039-user-accounts-authentication.md)) the routes the
agents use stay open to callers with no account: registration, channel list,
history and publish, and convene (`internal/server/auth_policy.go`), because
agents hold no accounts. The comment there names "the RFC 0009 per-agent
limiter + quarantine" as their defence. An outsider can turn that defence into
the attack: invent an ID on one of those open routes and trip it. A signed-in
operator is refused too, because the rule reads only `X-Agent-ID`, not the
session.

## Impact

- **One outsider, about a minute, the whole fleet.** Roughly 65 requests under
  an invented ID stop every persona's channel replies and the CLI, in either
  auth mode. The default mode is `disabled`.
- **Spending goes on.** A quarantine does not stop the orchestrator handing
  personas their messages, for example ones sent from the web console. The
  persona still calls its model, because wallet calls carry its ID; its reply
  is then refused at publish, with a warning in the agent's log. Its history
  read is refused as well, so it answers without the recent messages it would
  normally read first.
- **Any quarantine does it, not only an attack.** A real agent that trips its
  own limit on wallet calls takes every other agent's REST calls down with it.
  An agent that starts during a quarantine is refused registration, so the
  orchestrator cannot send it work while the quarantine lasts.
- **A named persona can be singled out.** The REST and gRPC hooks share one
  circuit breaker, and wallet calls carry the agent's ID. Flooding the REST
  port as `ember-owl` quarantines `ember-owl`: its wallet calls are refused, so
  it can no longer call its model, and the anonymous refusal then stops the
  rest of the fleet as well.
- **Recovery is hard to find.** Nothing expires. The CLI has no unquarantine
  command and cannot send the header, and the console has no unquarantine
  control. The release endpoint is behind the same refusal, so the operator
  must know to invent an `X-Agent-ID`, on top of the operator session under
  `enabled` or `SECURITY_UNQUARANTINE_TOKEN` when that is set, and must dig the
  ID out of the audit log. The handler's fallback, which names the actor
  "operator" when the header is missing, can only run when there is nothing to
  release. A restart works too, but it also empties the in-memory agent
  registry until each agent registers again.
- **The rule buys little.** It stops only a quarantined caller that drops its
  header. The same caller can send a fresh invented ID and get a full budget
  back; `middleware.go` already says so about the console's ID ("quarantine
  never gated a determined caller (they rotate ids)").
- **High severity.** It is cheap, needs no account in either auth mode, stops
  the whole fleet, and lasts until a person steps in. Nothing is granted that
  should be refused: what is lost is service.

## Proposed fix / investigation path

The steps below can land separately; the owner picks which, and when, at
slotting.

1. **Let the release call through.** Exempt
   `POST /api/v1/agents/{id}/unquarantine` from the anonymous refusal. Under
   `enabled` it is operator-only already, and under `disabled` anyone can
   already reach the handler by inventing a header, so the exemption opens
   nothing; the token checks stay. Test first: an anonymous release during a
   quarantine succeeds.
2. **Stop refusing everyone for one ID.** While a quarantine is open, leave
   anonymous callers in their shared, rate-limited budget rather than refusing
   them. That budget already caps what a header-dropping agent can do, and a
   fresh invented ID gets around the rule anyway. A narrower rule needs an
   identity the caller cannot invent, which is what RFC 0009 Phase 4 brings.
   `TestRESTMiddleware_QuarantineActiveBlocksAnonymous`,
   `TestGRPCInterceptor_QuarantineActiveBlocksAnonymous` and
   `internal/server/ui_quarantine_test.go` pin today's refusal and change with
   it.
3. **Give the project's own callers an ID,** as #592 did for the console and
   ISSUE-0111 did for wallet calls. The agents' REST session sends the hosted
   agent's ID; the CLI sends a fixed ID that is never quarantined. This alone
   does not fix the issue: `curl`, scripts and other clients stay exposed, and
   a persona's own ID can still be flooded (Impact). But it gives each agent
   its own REST budget, which closes the REST half of ISSUE-0111's open
   residuals.
4. **Give a quarantine an end.** Release it automatically after a set time,
   with an audit event, so no lockout outlasts it. That changes
   [RFC 0009 §H](../rfcs/0009-security-sandboxing.md#h-orchestrator-as-security-boundary)
   ("until an operator intervenes"), so it needs a dated RFC note.
5. **The root fix is verified identity: RFC 0009 Phase 4.** Once agent IDs
   come from orchestrator-issued tokens, an invented ID cannot open the circuit
   breaker. Phase 4 lists token checks for gRPC calls only. The REST calls the
   agents make today need them too, or must first move to gRPC as
   [RFC 0040](../rfcs/0040-agent-orchestrator-transport-unification.md) plans.
   Moving them without an ID would change nothing, because the gRPC half of
   the rule refuses anonymous calls in the same way.

**Recommendation:** steps 1 and 2 together, test first, in one small Go PR.
They remove the whole-fleet switch without waiting for Phase 4 and change no
wire format. Step 3 is worth doing for ISSUE-0111 either way; step 4 is the
owner's design call; step 5 stays the real fix.

**Slot: the owner's call.** v0.3.16's scope was ratified by the
[sequencing amendment of 2026-08-19](../v0.3.x-sequencing.md#amendment-2026-08-19--v0315--v0316-attribution-and-audience-before-the-v040-train)
and does not include this, so taking steps 1 and 2 into v0.3.16 needs a new
dated amendment. Otherwise they are an early v0.4.0 PR, next to RFC 0009
Phase 4. Under the
[version-train gate](../methodology/process-glossary.md#version-train-gate), a
v0.4.0 PR can be written and reviewed now but does not merge until v0.3.16 is
tagged.

## Notes

> 2026-09-11 — filed from a code reading at `0f12f464`; every file, route and
> test named above was checked there. Docs only: no code changes.
> [ISSUE-0111](ISSUE-0111-anonymous-wallet-rpcs-share-rate-limit-bucket.md)
> covers the shared anonymous budget, not this refusal; its notes now link
> here.
