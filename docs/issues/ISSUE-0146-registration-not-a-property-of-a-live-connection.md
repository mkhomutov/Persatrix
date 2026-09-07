---
id: ISSUE-0146
summary: "Agent registration is a one-shot call, not a property of a live connection: RFC 0040 §C sketches `RegisterAgent` as a unary RPC, so Phase 2 migrates the transport and keeps the registry's liveness half unanswered — ISSUE-0125 closed on the interim shape (4), an agent-side reconnect watcher that proxies channel health for orchestrator identity, and this issue carries the destination shape (5) it deferred: a connection-scoped or streaming registration where a broken stream is both the re-register trigger and the liveness signal, which needs an RFC 0040 §C amendment before the Phase 2 proto lands"
status: open
severity: low
area: grpc
created: 2026-09-07
refs:
  - docs/rfcs/0040-agent-orchestrator-transport-unification.md
  - docs/issues/ISSUE-0125-agents-never-reregister-after-orchestrator-restart.md
  - docs/issues/ISSUE-0125-design-record.md
  - internal/registry/
  - agents/server.py
---

## Summary

A persona tells the orchestrator it exists exactly once, at startup. Since
v0.3.15 it tells it again whenever its orchestrator connection drops and
comes back ([ISSUE-0125](ISSUE-0125-agents-never-reregister-after-orchestrator-restart.md)
shape (4)), which is what keeps the fleet reachable across a restart. What
neither shape gives the orchestrator is a way to know, on its own, whether
an agent that registered is still there: registration is a fact the agent
asserts, not a connection the orchestrator holds.

## Context

The [re-registration design record](ISSUE-0125-design-record.md) weighed
five shapes. Shape (4) — re-register on the existing gRPC channel's
connectivity state — shipped in v0.3.15 PR C1 and was verified live across
three orchestrator restarts. Shape (5) — **make registration a property of a
live connection** — was named the destination and deferred to v0.4.0,
because reaching it means amending [RFC 0040 §C](../rfcs/0040-agent-orchestrator-transport-unification.md#c-proto-surface--orchestratorservice):
that section sketches `RegisterAgent` as a *unary* RPC, one call from the
same startup path over a typed transport, and its design notes lean on that
("the new unary RPCs pick these up for free"). Landing Phase 2 as written
migrates the transport and leaves the liveness gap exactly where it is.

Shape (4) approximates (5) on wiring that ships today, and the approximation
is stated in the record: channel health stands in for orchestrator
*identity*, so a reconnect to a process that never lost its registry
re-registers redundantly (harmless, since `Register` is an upsert), and a
process that lost its registry while the channel stayed up is not detected
by the watcher at all. The half (5) adds — "is it still here", not only
"who is here" — has no mechanism: `Registry.UpdateStatus` remains undriven.

ISSUE-0125 resolved on 2026-09-03 with its file promising that the v0.3.15
Phase 4 follow-up would re-file this shape and re-point RFC 0040. The
follow-up did not; this file is that re-file, opened at the v0.3.16
planning-readiness audit.

## Impact

- **RFC 0040 Phase 2 is non-additive here.** A unary `RegisterAgent` that
  ships becomes the contract; turning it into a stream afterwards is a
  proto change every agent must follow. The decision has to precede the
  proto, which is why RFC 0040 OQ 6 says "resolve before the proto lands".
- **Liveness stays asserted, never observed.** An agent that dies without
  deregistering stays in the registry until something dispatches to it and
  fails; nothing drives `UpdateStatus`. v0.4.0 organizations
  ([RFC 0012](../rfcs/0012-protocols-organizations.md)) multiply dispatch
  targets and assume a reachable fleet.
- **Two mechanisms where one would do.** Shape (4)'s watcher and a future
  liveness probe would each own half of "who is here and is it still here";
  a stream owns both.

## Proposed fix / investigation path

1. **Amend RFC 0040 §C** with a connection-scoped or streaming
   `RegisterAgent`: the agent opens the stream at startup, the orchestrator
   registers on open and deregisters (or marks unhealthy) on break, and
   shape (4)'s watcher retires with the REST registration it re-drives.
   Resolve RFC 0040 OQ 6 with it. Cost to state in the amendment: it is the
   one non-unary RPC on the service and forfeits the "unary RPCs inherit
   the middleware for free" property §C relies on.
2. **Keep the zero-registered-agents signal** ISSUE-0125 added; a stream
   changes how the registry empties, not whether that state is worth
   logging.
3. **Slot with RFC 0040 Phases 2–4 on the v0.4.0 train.** Not v0.3.x work:
   the interim is verified live and the fix is a proto-shape decision.

## Notes

> 2026-09-07 — filed at the v0.3.16 planning-readiness audit as the re-file
> ISSUE-0125 and the v0.3.15 plan both promised for Phase 4 and the
> follow-up ([#874](https://github.com/mkhomutov/Persatrix/pull/874))
> reported as done without doing. RFC 0040 §C and OQ 6 now point here
> instead of at the resolved ISSUE-0125. Slotted **v0.4.0** with RFC 0040
> Phase 2; explicitly out of v0.3.16 scope.
