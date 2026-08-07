---
id: ISSUE-0125
summary: "The orchestrator's agent registry is an in-memory map (`internal/registry/registry.go` `InMemoryRegistry`) and agents call `_self_register()` exactly once, in their own startup path (`agents/server.py`) — there is no heartbeat, no retry and no re-registration trigger. So an orchestrator restart empties the registry and the fleet never comes back: every dispatch is dropped with `channels: dispatch target not registered`, personas fall silent, and nothing self-heals until each agent process is restarted by hand. The failure is near-silent — /healthz is green, containers are up, publishes return 201, and the only signal is one WARN per dropped dispatch. Found 2026-08-07 when it voided a live MT arc on a paid provider."
status: open
severity: medium
area: internal/registry
created: 2026-08-07
refs:
  - internal/registry/registry.go
  - internal/channels/grpc_dispatcher.go
  - agents/server.py
  - agents/channel_catchup.py
  - docs/rfcs/0040-agent-orchestrator-transport-unification.md
  - docs/manual-tests/MT-MEMORY-MULTIUSER-001.md
---

## Summary

The orchestrator forgets every agent when it restarts, and no agent ever
tells it again.

## Context

Three facts compose into it:

1. **The registry is in-memory only.**
   [`InMemoryRegistry`](../../internal/registry/registry.go) holds
   `agents map[string]*AgentInfo`. There is no persistence and no
   rebuild-from-config at boot.
2. **Agents register exactly once.**
   [`AgentServer._self_register()`](../../agents/server.py) is called from
   the startup path, immediately before the on-startup catch-up
   (`replay_for_persona_agents`). There is no heartbeat loop, no retry, and
   nothing that re-registers on a failed call or on orchestrator reconnect.
3. **Nothing detects the gap from either side.** `Registry.UpdateStatus`
   exists on the interface but no health monitor drives it, so the
   orchestrator never notices it has lost the fleet; and agents are the
   *target* of dispatch, not the caller, so they cannot observe that
   deliveries stopped.

The recovery machinery already exists — `replay_for_persona_agents` pulls
missed channel history so a returning agent loses nothing — but it is
bound to **agent** startup, not to orchestrator reconnect. Nothing invokes
it when the orchestrator is the process that restarted.

Found 2026-08-07 while running a live multi-agent group-channel arc under
`auth.mode: enabled`: the `account bootstrap` → restart-the-orchestrator
step that
[MT-MEMORY-MULTIUSER-001](../manual-tests/MT-MEMORY-MULTIUSER-001.md)
prescribes silently voided the whole run.

## Impact

After any orchestrator restart, every
[`GRPCMessageDispatcher.Dispatch`](../../internal/channels/grpc_dispatcher.go)
call fails the `registry.ErrAgentNotFound` branch and drops the message.
Personas stop replying permanently. In a `restart: unless-stopped`
deployment an orchestrator OOM or crash-loop therefore disables the whole
society until an operator restarts every agent by hand, with no
self-healing and no alarm.

**The failure is near-silent, which is the sharp edge.** `/healthz` is
green, containers are up and healthy, publishes return `201`, the channel
transcript accepts the message — the only signal is one WARN per dropped
dispatch, logged at *warn* precisely because a single unregistered member
is normally benign:

```
WARN  channels: dispatch target not registered; dropping (read via history on reconnect)
```

That log line's own promise — "read via history on reconnect" — is what
does not hold here, because the reconnect never happens.

Worse in test contexts than in production ones: a silent persona is
indistinguishable from a persona that correctly recalled nothing, so any
absence-bar assertion **passes vacuously**. That is how it voided a live
MT run on a paid provider before anyone noticed
([MT-MEMORY-MULTIUSER-001](../manual-tests/MT-MEMORY-MULTIUSER-001.md)
carries the same restart instruction and now carries a warning).

Severity is **medium**, not high: no data is lost or leaked, catch-up
restores the missed transcript once agents return, and the workaround is a
restart. It rises as deployments get longer-lived — and v0.4.0
organizations ([RFC 0012](../rfcs/0012-protocols-organizations.md)) assume
a fleet that stays reachable.

## Proposed fix / investigation path

Four shapes, roughly in increasing order of rightness:

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
3. **Seed from config at boot + verify by dial.** The orchestrator already
   knows the roster (`config/agents.yaml`, channel membership), so it can
   rebuild the registry at startup and confirm each address. Bounded and
   self-contained, but it only covers config-declared agents — not
   dynamically registered ones.
4. **Make registration a property of a live connection.** The right shape:
   the agent holds a stream to the orchestrator, and a broken stream is
   both the trigger to re-register and the liveness signal. This is
   already on the roadmap — [RFC 0040](../rfcs/0040-agent-orchestrator-transport-unification.md)
   migrates **agent registration** (with channel publish and history) from
   REST onto a gRPC `OrchestratorService`, which is where a
   reconnect-aware registration naturally lives. Phases 2–4 are the v0.4.0
   train.

Recommendation: treat (4) as the destination and fold the fix into RFC 0040
Phase 2 rather than building (1) or (2) standalone. If the gap bites before
that lands, (3) is the cheapest interim that adds no polling.

Whichever lands, the observability gap deserves closing on its own: an
orchestrator that has **zero** registered agents while channels have
members is a state worth logging at ERROR once, or exposing as a metric, so
the condition is visible without reading dispatch WARNs.

## Notes

> 2026-08-07 — filed after the behaviour voided a live MT arc. The
> MT-side mitigation shipped separately
> ([#823](https://github.com/mkhomutov/Persatrix/pull/823)): restart the
> personas after any orchestrator restart, and confirm
> `GET /api/v1/agents` lists them `healthy` before proceeding. That is a
> procedure, not a fix — this issue is the fix.
