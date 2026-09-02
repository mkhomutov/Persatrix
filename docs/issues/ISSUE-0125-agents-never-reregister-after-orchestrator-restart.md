---
id: ISSUE-0125
summary: "The orchestrator's agent registry is an in-memory map (`internal/registry/registry.go` `InMemoryRegistry`) and agents call `_self_register()` exactly once, in their own startup path (`agents/server.py`) — there is no heartbeat, no retry and no re-registration trigger. So an orchestrator restart empties the registry and the fleet never comes back: every dispatch is dropped with `channels: dispatch target not registered`, personas fall silent, and nothing self-heals until each agent process is restarted by hand. The failure is near-silent — /healthz is green, containers are up, publishes return 201, and the only signal is one WARN per dropped dispatch. Recorded as an operational quirk since the v0.3.0 execution report and as F-6 (severity low) in v0.3.2; filed 2026-08-07 when the group-channel path, where the same condition is silent rather than loud, voided a live MT arc on a paid provider. **Fixed in v0.3.15 PR C1** by shape (4): each agent watches its own orchestrator gRPC channel and re-registers on any return to `READY`, `Register` becomes an upsert, and zero registered agents is raised at ERROR. Open until the release's live gate exercises a real restart."
status: resolved
severity: medium
area: internal/registry
created: 2026-08-07
closed: 2026-09-03
refs:
  - internal/registry/registry.go
  - internal/channels/grpc_dispatcher.go
  - internal/server/agent_handlers.go
  - agents/server.py
  - agents/server_reregister.py
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
   exists on the interface but no health monitor drives it — the registry
   file still ends on a bare `// TODO: Implement health check loop` — so
   the orchestrator never notices it has lost the fleet; and agents are the
   *target* of dispatch, not the caller, so they cannot observe that
   deliveries stopped.

Agents cannot see the dropped deliveries, but they *can* see the restart
itself, and nothing looks. Every agent already holds a long-lived
`grpc.aio.insecure_channel` to the orchestrator
([`agents/server.py`](../../agents/server.py)), opened at startup and shared
by the RFC 0018 log shipper and the RFC 0023 wallet client. An orchestrator
restart breaks that channel and gRPC reconnects it — the connectivity-state
transition is an unused signal sitting on a connection that already exists.
See shape (4) below.

The recovery machinery already exists — `replay_for_persona_agents` pulls
missed channel history so a returning agent loses nothing — but it is
bound to **agent** startup, not to orchestrator reconnect. Nothing invokes
it when the orchestrator is the process that restarted.

Found 2026-08-07 while running a live multi-agent group-channel arc under
`auth.mode: enabled`: the `account bootstrap` → restart-the-orchestrator
step that
[MT-MEMORY-MULTIUSER-001](../manual-tests/MT-MEMORY-MULTIUSER-001.md)
prescribes silently voided the whole run.

**Not a new behaviour — this is its fourth recorded sighting, and the
first as an issue.** The 2026-08-07 arc is what forced the filing, not
what discovered the defect:

| Where | What it said |
|-------|--------------|
| [v0.3.0 execution report](../manual-tests/v0.3.0-execution-report.md) | "agents register at startup only"; after the MT-LOGS-001 restart, `docker compose restart agent-…` was needed to repopulate `/api/v1/agents`. Filed as an **operational pattern**, described there as carried over from v0.2.x, "no regression". |
| [v0.3.1 execution report](../manual-tests/v0.3.1-execution-report.md) | An environment note: the in-memory registry "is wiped on an orchestrator restart — agents must be restarted to re-register." |
| [v0.3.2 execution report](../manual-tests/v0.3.2-execution-report.md) | **F-6**, severity **low**: "mildly painful for repeatable manual-test runs; not a v0.3.2 regression but worth a note in the operator playbook." |

Two things follow. First, the finding aged from *operator inconvenience*
into *voids a paid live run* without the behaviour changing at all —
what changed is that the fleet grew a group-channel path where the same
condition is silent rather than loud. That progression is the severity
argument, not a footnote to it. Second, F-6's own ask — a note in the
operator playbook — went unwritten for three releases until
[#823](https://github.com/mkhomutov/Persatrix/pull/823); that gap is
tracked separately as ISSUE-0126.

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
carries the same restart instruction; it carried a precondition warning from
[#823](https://github.com/mkhomutov/Persatrix/pull/823) until the fix landed —
see the 2026-08-23 note).

One compounding detail from the v0.3.2 record, since it shapes the
remedy: the RFC 0009 rate-limiter bucket is **not** flushed on an
orchestrator restart, so a turn issued right after the operator has
restarted everything can still draw `429` for up to ~60 s. An operator
who reads that second symptom as "the restart did not take" will restart
again and lose another turn.

Severity is **medium**, not high: no data is lost or leaked, catch-up
restores the missed transcript once agents return, and the workaround is a
restart. It is deliberately a step above the **low** the v0.3.2 report
assigned it — the behaviour has not changed, but its blast radius has:
`low` was correct when the only known symptom was a loud `404` on a
manual-test rerun, and the group-channel path found in 2026-08-07 makes
the same condition silent, which is what converts a burnt turn into a
vacuously-passing arc. It rises further as deployments get longer-lived —
and v0.4.0 organizations
([RFC 0012](../rfcs/0012-protocols-organizations.md)) assume a fleet that
stays reachable.

## Proposed fix / investigation path

**Split out to [the re-registration design record](ISSUE-0125-design-record.md)**
— the five candidate shapes, why **(4)** (an agent-side watcher on the
orchestrator connection, re-registering on any departure from `READY` and
return) was taken at PR C1, and why **(5)** (registry persistence / registration
as a property of a live stream) was deferred to v0.4.0 as an
[RFC 0040](../rfcs/0040-agent-orchestrator-transport-unification.md) §C
amendment. Closing this issue orphans RFC 0040's §C and OQ 6 pointers; the
v0.3.15 Phase 4 follow-up re-files shape (5) as its own issue and re-points
both.

## Notes

> 2026-08-07 — filed after the behaviour voided a live MT arc. The
> MT-side mitigation shipped separately
> ([#823](https://github.com/mkhomutov/Persatrix/pull/823)): restart the
> personas after any orchestrator restart, and confirm
> `GET /api/v1/agents` lists them `healthy` before proceeding. That is a
> procedure, not a fix — this issue is the fix.

> 2026-08-08 — review fold-in (PR #824), all four to the fix section.
> (a) The RFC 0040 claim was too generous: Phase 2's `RegisterAgent` is
> *unary* as drafted, so it is a transport swap that leaves this open —
> closing it there needs a §C amendment, now stated as the ask rather than
> a phase to wait on. (b) Shape (3)'s premise was wrong: `config/` carries
> the roster but no addresses at all, so "verify by dial" had nothing to
> dial — it is a config surface change, not the cheap interim. (c) Added
> shape (4), re-register on the connectivity state of the orchestrator
> channel the agent already holds, which is the cheap interim (3) was
> claimed to be and is event-driven; Context 3 now names that unused
> signal. (d) Hoisted the constraint all shapes inherit: `Register` is not
> an upsert, it `409`s, so a re-register is a no-op against a populated
> registry and a boot seed blocks the real registration.

> 2026-08-10 — review fold-in (PR #824): backlinks, so the ask is not
> invisible from the documents it is aimed at. This issue had no inbound
> reference outside `INDEX.md`, which meant an implementer building
> [RFC 0040](../rfcs/0040-agent-orchestrator-transport-unification.md)
> Phase 2 from the RFC would have landed the unary `RegisterAgent` and
> closed the phase without ever seeing it — the exact outcome the fix
> section argues against. RFC 0040 now carries the amendment ask in two
> places: a §C design note on the unary sketch, and Open Question 6
> (connection-scoped/streaming `RegisterAgent`), to be resolved before the
> proto lands since the shape is non-additive once it ships.
> [MT-MEMORY-MULTIUSER-001](../manual-tests/MT-MEMORY-MULTIUSER-001.md)
> now links back here from its restart warning, closing the other half of
> the reference [#823](https://github.com/mkhomutov/Persatrix/pull/823)
> opened.

> 2026-08-10 — review fold-in (PR #824): prior sightings. The file read as
> a fresh 2026-08-07 discovery; it is the fourth recorded sighting (v0.3.0
> execution report, v0.3.1 environment note, v0.3.2 F-6 at severity low),
> now tabled in Context. Three consequences were folded into Impact: the
> `medium` rating is an explicit re-assessment of v0.3.2's `low` and says
> what changed (blast radius, not behaviour); the un-flushed RFC 0009
> rate-limiter bucket compounds the remedy with a `429` an operator can
> misread as a failed restart; and the front-matter summary no longer
> claims 2026-08-07 as the discovery date.

> 2026-08-19 — **slotted v0.3.15** (cuttable) by the [sequencing Amendment 2026-08-19](../v0.3.x-sequencing.md#amendment-2026-08-19--v0315--v0316-attribution-and-audience-before-the-v040-train): every release
> in that doc is gated on a live MT and this failure voids one, so it is
> fixed before the v0.4.0 train multiplies dispatch targets. Cuttable if the
> bounded "re-register on dispatch failure + heartbeat" shape grows into a
> registry-persistence redesign. Landing it takes
> [ISSUE-0126](ISSUE-0126-mt-orchestrator-restart-registry-note-missing.md)
> option 1 — delete the one warning PR #823 wrote into
> [MT-MEMORY-MULTIUSER-001](../manual-tests/MT-MEMORY-MULTIUSER-001.md) and
> confirm the eight unguarded restart steps are safe as written.
>
> 2026-08-23 — **Scoped into v0.3.15 and shaped**, by the [v0.3.15 plan](../v0.3.15-plan.md)
> (Phase 0). The [sequencing Amendment 2026-08-19](../v0.3.x-sequencing.md#amendment-2026-08-19--v0315--v0316-attribution-and-audience-before-the-v040-train)
> boarded this issue as *cuttable*; the plan takes **shape (4)** from
> §Proposed fix above — agent-side re-registration driven by the existing
> orchestrator channel's gRPC connectivity state — together with the
> `InMemoryRegistry.Register` **upsert** precondition and the
> zero-registered-agents signal this issue asks for on its own merit. Not the
> amendment's "heartbeat" wording: a fleet-wide periodic re-register is the
> polling shape RFC 0024 removed, and re-registration *on dispatch failure* has
> no orchestrator-side implementation — that side holds no address to dial.
> Shape (5) and its RFC 0040 §C amendment stay the destination and stay
> v0.4.0. Recorded as a deliberate deviation from the amendment's wording,
> taken on this issue's own more specific analysis; reversible at review.
>
> **The trigger is any `READY` departure and return — not the literal
> `READY → TRANSIENT_FAILURE → READY` cycle** that §Proposed fix (4) and the
> plan's first draft both named. `TRANSIENT_FAILURE` is entered only when a
> connection *attempt* fails. `_orchestrator_channel` is a `grpc.aio.Channel`
> shared by the log shipper and `WalletClient`; on a clean orchestrator restart
> the shipper's stream EOFs without an exception and then backs off
> (1.0–30.0 s) with no RPC pending, and `WalletClient` issues unary calls only
> during LLM turns — so the channel is usually idle when the transport drops,
> and an idle channel goes to `IDLE`, not `TRANSIENT_FAILURE`.
> `wait_for_state_change` also coalesces, so even a real failure hop can be
> missed. A watcher written to the literal cycle passes a unit test that
> injects `TRANSIENT_FAILURE` and never fires in production — the precise
> failure this issue exists to remove. Pin the test on an `IDLE` cycle.
> Two further notes for the implementer: the watched channel is
> `orchestrator_grpc` while `_self_register()` posts over aiohttp to
> `orchestrator_url`, two different addresses; and the **upsert does not fix
> the restart case** — `InMemoryRegistry` has no load path, so after a restart
> the map is empty and a re-register already succeeds without a `409`. The
> upsert is for the blip and stale-address cases. Pin them separately, or a
> green upsert test will stand in as evidence for a watcher that never runs.
>
> **Two orderings ride with it.** First, it lands **before** the workstreams it
> serves rather than beside them: every restart-bearing leg of the v0.3.15 live
> gate — the [ISSUE-0130](ISSUE-0130-catchup-replay-rederives-memory-under-default-principal.md)
> shape (b) replay verification most of all — is today a leg that leaves the
> fleet permanently mute. Second, a constraint the plan-opening audit forced:
> **the reconnect path must re-register only, never re-run catch-up.**
> `_self_register()` and `replay_for_persona_agents()` sit adjacent in
> `AgentServer.start()` ([`agents/server.py`](../../agents/server.py)), so
> re-running the startup tail on reconnect would re-ingest the catch-up window
> on every orchestrator blip — and catch-up has no watermark (RFC 0011 OQ #8),
> which makes that unbounded re-derivation: the path ISSUE-0130 shape (a) just
> bounded, re-opened through a different door. Pinned by a test. Note this
> corollary guards a door *this* issue creates; the plan's lock 4 covers the
> restart door, which is open today and is B2's to close.
>
> [ISSUE-0126](ISSUE-0126-mt-orchestrator-restart-registry-note-missing.md)
> retires in the same PR by its own option 1 **as corrected 2026-08-23** —
> **delete both** warnings, #823's on MT-MEMORY-MULTIUSER-001 *and* the one
> [MT-MEMORY-GROUP-TENANT-001](../manual-tests/MT-MEMORY-GROUP-TENANT-001.md)
> carries, with the eight unguarded restart steps confirmed safe as written.
> The earlier "one warning" wording here predates that correction and is
> superseded; the group MT is this release's own live gate, so leaving its
> warning standing would retire ISSUE-0126 on a half-executed option 1. If
> this issue cuts, neither deletion lands.

> 2026-08-23 — **shape (4) landed** (v0.3.15 PR C1). Three pieces, each pinned by
> its own test:
>
> - **The trigger.** [`agents/server_reregister.py`](../../agents/server_reregister.py)
>   watches `AgentServer._orchestrator_channel` and re-runs `_self_register()` on
>   any departure from `READY` and return. The unit bar is an
>   **`IDLE`** cycle; `TRANSIENT_FAILURE` is one departure state among several,
>   not *the* trigger. A first `READY` is boot, and does not fire. A reconnect
>   **retries**, and the loop is **supervised**: neither a lost POST nor a
>   stray channel error may leave a mute agent.
> - **The precondition.** `Register` is an upsert; `ErrAgentAlreadyRegistered`
>   and the REST `409` are gone (`201` now means "registration accepted" —
>   RFC 0001 and RFC 0002 amended). This covers the blip and the stale address,
>   not the restart.
> - **The observability this issue asks for separately.** Zero registered
>   agents while a channel still has members is now an **ERROR** from the
>   dispatch-miss path — once per outage, re-armed by any dispatch that
>   **resolves** (a clean recovery leaves no miss). The per-recipient WARN
>   stays at warn; it could not tell the benign case from this one.
>
> **The no-replay corollary holds**, pinned by a test.
>
> **Not closed yet.** The live proof is
> [MT-MEMORY-GROUP-TENANT-001](../manual-tests/MT-MEMORY-GROUP-TENANT-001.md)'s
> restart legs, which run once, at Phase 3. This issue and
> [ISSUE-0126](ISSUE-0126-mt-orchestrator-restart-registry-note-missing.md) close
> together at Phase 4, which also re-files shape (5) so closing this does not
> orphan RFC 0040 §C and OQ 6.

> **Resolved 2026-09-03 — verified live, more strongly than the gate asked**
> ([v0.3.15 execution report](../manual-tests/v0.3.15-execution-report.md), Leg 8). The arc restarted the orchestrator **three** times
> (Legs 0, 7 and 8). After each, `GET /api/v1/agents` returned all six agents
> and every persona logged `Orchestrator channel reconnected` →
> `Registered agent …`. **No agent process was touched**: every agent
> container reported `Up 39 minutes` at the end of the arc.
>
> The restarts were ordinary `docker compose restart orchestrator` — an
> `IDLE`/`CONNECTING` cycle, not `TRANSIENT_FAILURE` — which is the transition
> the connectivity watcher had to cover and the one a narrower fix would have
> missed.
