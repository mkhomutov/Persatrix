# ISSUE-0125 — the re-registration design record (record)

**Companion to**: [ISSUE-0125](ISSUE-0125-agents-never-reregister-after-orchestrator-restart.md)
**Covers**: the five candidate shapes, why shape (4) was taken and shape (5) deferred
**Release**: v0.3.15 *Who said what* — shipped at PR C1, verified live at the Phase 3 arc

Split out of ISSUE-0125 on 2026-09-03, when the issue stood at **2 996/3 000
words** and its closure note would not fit. Splitting rather than trimming
follows the precedent set by [ISSUE-0082 Part 2](ISSUE-0082-part2-v0314-build-log.md)
and applied the same day to [ISSUE-0124](ISSUE-0124-design-record.md): the
option analysis below is *why the fix has the shape it has*, and shape (5) is
still live work — [RFC 0040](../rfcs/0040-agent-orchestrator-transport-unification.md)
§C and OQ 6 both route their ask through it, which the v0.3.15 Phase 4
follow-up re-files as its own issue.

---

## Proposed fix / investigation path

**One constraint every shape below inherits: registration is not
idempotent.** `InMemoryRegistry.Register` returns
`ErrAgentAlreadyRegistered` when the id is already present — "re-registration
requires calling `Unregister` first" — which the REST handler surfaces as
`409` ([`internal/server/agent_handlers.go`](../../internal/server/agent_handlers.go))
and `_self_register` logs at info and shrugs off. So a re-register call
repairs an *emptied* registry but is a no-op against a populated one: a
stale address is never corrected, and any boot-time seed `409`-blocks the
agent's own registration — the one call that carries the real
`advertise_address`. Making `Register` an upsert (or pairing it with
`Unregister`) is a precondition shared by (1), (3), (4) and (5), not a
detail of any one of them.

Five shapes, roughly in increasing order of rightness:

1. **Agent-side periodic re-register.** Simplest and self-healing, but it
   reintroduces *polling*. It would not breach the v0.3.3 idle-cost
   guarantee in substance — no recall query, no `_inject_memory_context`,
   no provider call, no wallet lease — but "structurally event-driven, not
   polled" is [RFC 0024](../rfcs/0024-event-driven-scheduling.md)'s stated
   property, and a fleet-wide timer is exactly the shape it removed. If
   chosen, make the interval long and the call trivially cheap.
2. **Persist the registry.** Survives restart, but an entry is a liveness
   claim: a persisted row for a dead agent is a dispatch that fails slower.
   Needs a liveness check regardless, so it solves the symptom and inherits
   the harder half.
3. **Seed from config at boot + verify by dial.** The orchestrator knows
   the *roster* from `config/agents.yaml` and channel membership — but not
   where any of it lives. There is no address field anywhere under
   `config/`: an agent's address is a property of the agent process
   (`--advertise-address`, defaulting to its own `host:port` and rewritten
   after bind when the port is dynamic —
   [`agents/server.py`](../../agents/server.py)), so outside the Docker
   `service:50051` naming convention the orchestrator has nothing to dial,
   and "verify by dial" has no address to verify. This shape needs an
   address column in `agents.yaml` first — a config surface change, not the
   cheap interim it looks like — and even then covers only config-declared
   agents, never dynamically registered ones.
4. **Re-register on the existing channel's connectivity state.** The agent
   already holds the connection (Context 3) and gRPC exposes its state
   transitions, so a `READY → TRANSIENT_FAILURE → READY` cycle can drive
   the re-register. Event-driven, not polled — no timer, so none of (1)'s
   RFC 0024 tension — and it needs no new transport, no proto and no
   orchestrator change. It is (5)'s property approximated on wiring that
   ships today; the approximation is that channel health proxies for
   orchestrator *identity*, so reconnecting to a process that did not lose
   its registry re-registers redundantly (harmless once `Register` is an
   upsert, a `409` no-op until then).
5. **Make registration a property of a live connection.** The right shape:
   the agent holds a stream to the orchestrator, and a broken stream is
   both the trigger to re-register and the liveness signal — one mechanism
   answering both "who is here" and "is it still here", which (2) and (3)
   each solve only half of.
   [RFC 0040](../rfcs/0040-agent-orchestrator-transport-unification.md)
   moves **agent registration** (with channel publish and history) from
   REST onto a gRPC `OrchestratorService` in Phase 2 (v0.4.0), which is the
   right place for this to live — **but Phase 2 as drafted does not deliver
   it.** Its §C surface sketches `RegisterAgent` as a *unary* RPC, and the
   design notes turn on that ("the new unary RPCs pick these up for free"):
   one call, once, from the same agent startup path, over a typed transport
   instead of REST. No stream, no liveness property. Landing Phase 2
   unchanged leaves this issue exactly where it is.

Recommendation: treat (5) as the destination, but note that reaching it
means **amending RFC 0040 §C** — a connection-scoped or streaming
`RegisterAgent` — rather than waiting on Phase 2 as written. That amendment
is design work this issue is asking for. If the gap bites before it lands,
(4) is the cheapest interim: no polling, no config surface, no proto, and
the same property (5) formalises.

Whichever lands, the observability gap deserves closing on its own: an
orchestrator that has **zero** registered agents while channels have
members is a state worth logging at ERROR once, or exposing as a metric, so
the condition is visible without reading dispatch WARNs.
