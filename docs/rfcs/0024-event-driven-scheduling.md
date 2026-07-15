---
id: RFC-0024
title: Event-Driven Agent Scheduling
summary: Shifts agent activation from poll-driven dispatch to event-driven scheduling with persistable timers — enables proactive personas and scheduled reminders.
type: architecture
status: partially_implemented
author: Maksim Khomutov
created: 2026-05-09
target: v0.3.3 (Phases 1–4) + v0.4.0 (Phase 5) + v0.5+ (Phase 6)
depends_on:
  - RFC-0005
  - RFC-0011
  - RFC-0017
---

# RFC 0024 — Event-Driven Agent Scheduling

**Type**: architecture
**Status**: ⚠️ Partially Implemented (Phases 1–4)
**Author**: Maksim Khomutov
**Date**: 2026-05-09
**Target**: v0.3.3 (Phases 1–4) + v0.4.0 (Phase 5) + v0.5+ (Phase 6) — PR plan: [`0024-pr-plan.md`](0024-pr-plan.md)
**Depends on**: RFC 0005 (Persona Agent + Memory), RFC 0011 (Channels & Bridges), RFC 0017 (Memory Injection Budget)
**Soft-depends on (Phase 2+)**: SA-1 (Personal/Society Storage Split) — tracked as a v0.4.0 RFC in [storage-architecture-roadmap.md](../storage-architecture-roadmap.md) (📋 Pending RFC at SA-1); see Open Question §1. Phase 2 ships in v0.3.x and therefore *before* SA-1; the soft-dependency is forward-only — once SA-1 lands, Phase 2's timer-persistence backend migrates as a follow-up.
**Relates to**: RFC 0023 (LLM Call Leasing), RFC 0027 (Reflection-Driven Consolidation) — see §D for salience-signal ownership.

---

## Table of Contents

- [Summary](#summary)
- [Motivation](#motivation)
- [Goals](#goals)
- [Non-Goals](#non-goals)
- [Design / Implementation](#design--implementation)
  - [A. Current State and Gaps](#a-current-state-and-gaps)
  - [B. Event Loop Inversion](#b-event-loop-inversion)
    - [B.1. Synchronous-dispatch callers under the queue model](#b1-synchronous-dispatch-callers-under-the-queue-model)
  - [C. Scheduled Timer Registry](#c-scheduled-timer-registry)
  - [D. Salience-Triggered Wakes](#d-salience-triggered-wakes)
  - [E. Channel Message Integration](#e-channel-message-integration)
  - [F. Failure Modes](#f-failure-modes)
  - [G. Migration of Existing TICK Logic](#g-migration-of-existing-tick-logic)
- [Security Considerations](#security-considerations)
- [Phased Implementation Plan](#phased-implementation-plan)
- [Files Touched (Estimated)](#files-touched-estimated)
- [Test Strategy](#test-strategy)
- [Open Questions](#open-questions)
- [Decided](#decided)
- [Decision / Next Steps](#decision--next-steps)
- [Related Documentation](#related-documentation)

---

## Summary

Today the persona autonomy loop is a fixed-interval polling timer ([`agents/tick.py`](../../agents/tick.py), default 60s, 1.0s floor). It fires `on_tick()` at every interval whether or not anything happened. Idle suppression and the empty-context TICK short-circuit ([RFC 0017 §F](0017-persona-memory-injection-budget.md#f-empty-context-tick-short-circuit)) are downstream filters: the tick still wakes, runs `_inject_memory_context`, and only *then* decides not to invoke the LLM. This RFC inverts the model — the loop wakes on (a) inbound RPC event, (b) memory write above a salience threshold, or (c) explicit scheduled timer. "Bored persona" stops meaning "spinning every N seconds and short-circuiting" and starts meaning "no scheduled work — the asyncio task is parked on `queue.get()`." This collapses the empty-context cost-leak class structurally instead of patching it per release, and gives v0.3 channels a clean integration shape: a channel message becomes a wake event, not another consumer of the polling loop.

## Motivation

Polling created the leak. The README's [Cost Warning](../../README.md#-cost-warning) documents a $35 v0.2.1 incident where a fixed-interval tick spun on empty context overnight; v0.2.2 shipped the empty-context TICK short-circuit ([RFC 0017 §F](0017-persona-memory-injection-budget.md#f-empty-context-tick-short-circuit)) to close *that* specific leak. Fixed-interval polling is the substrate that makes "agent left running over a weekend" expensive — the structural fix is to stop polling.

Concretely, four problems compound:

1. **The current short-circuit is a downstream filter, not a structural fix.** [`TickScheduler._run`](../../agents/tick.py) wakes every `interval` seconds, calls `agent.on_tick()`, which calls `_inject_memory_context` (paying the SQLite query cost on *every* tick), and only then does `_ActionLoopMixin._on_event_inner` ([`agents/persona_runtime/action_loop.py`](../../agents/persona_runtime/action_loop.py)) decide whether to suppress the LLM call. The cost is bounded but non-zero on every tick of every idle agent.
2. **Two scheduling models already coexist.** `TickScheduler.wake()` exists (called by [`EventDispatcher.dispatch()`](../../agents/dispatch.py) at line 140 on inbound events) but it only resets the idle counter and short-circuits the *next* interval — events still race the polling timer, and a tick can fire 100 ms before an event arrives, doubling work.
3. **The §F guard is policy embedded in code path.** Each new event type (channel message, sub-agent reply, scheduled reminder) has to remember to flip the right state for `_has_active_goal_payload` / `_has_pending_turn` so the guard either fires or doesn't. The guard is correct today; it will rot as event types proliferate.
4. **v0.3 channels add a new event source.** [RFC 0011](0011-channels-bridges.md) introduces inter-agent channel messages dispatched through `AgentService.ReceiveChannelMessage` ([`proto/task.proto`](../../proto/task.proto) line 23). Wired naïvely, every agent in a channel pays polling cost when no one is talking, and the §F guard grows another condition.

What happens if we do nothing: v0.3 channels ship on top of the tick loop. Inter-agent traffic compounds the polling cost. The empty-context guard grows new conditions for every new event type. The cost-leak class survives the v0.3 release and becomes harder to extract once channels have written assumptions into the scheduler.

## Goals

1. The persona loop is event-driven: wake → process → park. No fixed-interval timer driving LLM calls in the steady state.
2. Wake sources are explicit and enumerable: inbound RPC event, salience-triggered memory write, scheduled timer (cron-like).
3. The empty-context TICK short-circuit ([RFC 0017 §F](0017-persona-memory-injection-budget.md#f-empty-context-tick-short-circuit)) becomes vestigial: in the steady state, no tick fires for a bored agent.
4. Channel messages ([RFC 0011](0011-channels-bridges.md)) integrate as a wake source, not as another consumer of a polling loop.
5. Backwards-compatible degradation: agents that explicitly opt into a periodic heartbeat (e.g. for memory consolidation cadence) can register a scheduled wake, but it is policy not default.
6. Migration path is incremental: existing TICK-driven behaviour stays runnable until each consumer is migrated.

## Non-Goals

- Replacing the asyncio-per-agent concurrency model. Agents remain in-process tasks; this RFC changes *when* they wake, not *how* they execute.
- Changing the gRPC surface of `AgentService`. Those RPCs already are events (`SendChatMessage`, `ReceiveChannelMessage`).
- Reworking memory I/O. `_inject_memory_context` keeps its current shape; it just runs less often.
- Cross-agent orchestration of wakes (that's RFC 0007 territory).
- Distributed scheduler (Temporal, Celery, etc.). Per-process asyncio remains the substrate.
- Making the §F guard go away in v0.3. It stays in place through Phase 5 and is removed in Phase 6 as a follow-up release.

## Design / Implementation

### A. Current State and Gaps

```mermaid
flowchart TD
    Timer["asyncio timer<br/>(every interval s)"] -->|elapsed| Run["TickScheduler._run"]
    Inbound["EventDispatcher.dispatch()<br/>(chat / channel / task)"] -->|wake_event.set()| Run
    Run -->|is_idle?| IdleBranch["recover_idle_energy()<br/>continue"]
    Run -->|not idle| OnTick["agent.on_tick()"]
    OnTick --> InjectMem["_inject_memory_context<br/>(SQLite recall)"]
    InjectMem --> Guard["§F guard<br/>(empty + no goal + no turn)?"]
    Guard -->|yes| DoNothing["return [DO_NOTHING]"]
    Guard -->|no| LLM["LLM call"]
    LLM --> Actions["execute actions"]
```

Three scheduling concerns mixed in `TickScheduler`:

1. **Polling timer**: `await asyncio.wait_for(..., timeout=self._interval)` drives the steady state.
2. **Wake handling**: `self._wake_event.set()` on inbound events short-circuits the next interval, but the loop is structured around polling.
3. **Idle filtering**: `is_idle` skips LLM calls but still runs energy recovery.

Layered on top is the §F guard inside `_on_event_inner` that suppresses LLM calls on TICK events with empty memory + no active goal + no pending turn. The result is a four-stage decision (poll → memory load → guard check → maybe-LLM) where three stages exist to undo the work the first stage created.

### B. Event Loop Inversion

Replace `TickScheduler._run`'s polling loop with an `EventLoop` that:

1. Maintains a single `asyncio.Queue[WakeEvent]` per agent.
2. Awaits `queue.get()` indefinitely (no timeout).
3. Processes the wake event according to its type.
4. Returns to await — no synthetic tick generation.

Wake event taxonomy:

| Variant | Producer | Carries |
|---------|----------|---------|
| `InboundEventWake` | `EventDispatcher.dispatch()` from RPC handlers | Original `AgentEvent` (chat, channel, task) |
| `ScheduledWake` | Timer registry (§C) | `timer_id`, `callback_kind` |
| `SalienceWake` | Memory write path (§D) | `MemoryWriteEvent` with computed salience |

The `wake()` method on the current `TickScheduler` remains a public entry point but becomes a thin adapter: `queue.put_nowait(InboundEventWake(...))`. Existing callers of `scheduler.wake()` keep working through Phase 4.

#### B.1. Synchronous-dispatch callers under the queue model

[`EventDispatcher.dispatch()`](../../agents/dispatch.py) is currently *synchronous-return*: it `await`s `agent.on_event(event)` and returns the resulting `list[AgentAction]`. Two distinct contracts ride on that shape, and they are not the same:

- **Return-value contract** — `SendChatMessage` is the sole consumer. **[`SendChatMessage`](../../agents/server_servicers.py)** uses `dispatch(..., execute_actions=False)` so it can extract the chat reply from the returned actions before side-effects fire (the OQ 5/7 resolution captured at [`agents/dispatch.py`](../../agents/dispatch.py) lines 92–97). The reply is the gRPC response payload — the handler cannot return until the agent has produced it. A bare enqueue-and-return would surface as `DEADLINE_EXCEEDED` for every chat call.
- **Await/serialisation contract** — every caller depends on it; the return value is incidental to most. **[`ActionExecutor.dispatch`](../../agents/action_executor.py#L328)** wraps the inner dispatch in `asyncio.wait_for(..., timeout=_DEFAULT_DISPATCH_TIMEOUT)` and discards the return value (no assignment, no unpack). What it actually needs is that the inner dispatch *completes* — for timeout accounting and for serialisation against concurrent inbound events on the target agent — not the `list[AgentAction]`. A `Future` that resolves on `on_event` completion satisfies this even if its value is `None`.

A naïve "enqueue and park" therefore silently breaks the chat path (no return value to read) *and* the cascade path (timeout accounting can no longer bound the wait). The RFC commits to **option (a): keep `dispatch()` synchronous-return through Phase 5**:

```text
dispatcher.dispatch(target, event):
    handle = SyncDispatchHandle()                # asyncio.Future-shaped
    queue.put_nowait(InboundEventWake(event, handle=handle))
    return await handle                           # resolved by the loop after on_event()
```

The `EventLoop` resolves `handle` with the `list[AgentAction]` produced by `on_event()` *before* moving to the next wake, preserving (i) the return-value contract that `SendChatMessage` reads, (ii) the await/serialisation contract that `ActionExecutor.dispatch`'s `wait_for` needs for timeout bounding, and (iii) the queue-mediated ordering that justifies the inversion in the first place. Fire-and-forget callers (`ReceiveChannelMessage`, salience writes, scheduled timers) construct their wakes without a handle and the loop simply does not resolve one.

Why option (a), not (b) carve-out or (c) bypass-the-queue:

- **(b)** "carve `SendChatMessage` out of the queue entirely" loses the queue-mediated serialisation guarantee with concurrent inbound events — a chat message racing a channel message could interleave inside the agent's lock window. This is the load-bearing argument; the return-value preservation is secondary because `SendChatMessage` is its only consumer.
- **(c)** "leave `dispatch()` synchronous and have *it* call `on_event` directly, skipping the queue" defeats the entire RFC: the queue is what makes wake sources enumerable and what unparks the agent.
- **(a)** preserves all three contracts at the cost of one `asyncio.Future` per synchronous wake. The `Future` is short-lived (resolved within one `on_event` call) and adds no steady-state cost for idle agents.

Phase 6 may revisit this once `SendChatMessage` is itself rethought (streaming reply, async chat protocol, etc.) — but that is out of scope here. (PR #308 deep review C2 + S1 — S1 corrected the prior framing that conflated the return-value contract with the await/serialisation contract; only chat reads the return value, every caller depends on the await.)

```mermaid
sequenceDiagram
    participant RPC as gRPC Handler
    participant Disp as EventDispatcher
    participant Queue as asyncio.Queue
    participant Loop as EventLoop._run
    participant Agent as _LLMPersonaAgent

    RPC->>Disp: dispatch(event)
    Disp->>Queue: put_nowait(InboundEventWake(event))
    Note over Loop: parked on queue.get()
    Queue-->>Loop: InboundEventWake
    Loop->>Agent: on_event(event)
    Agent->>Agent: _inject_memory_context (event-keyed)
    Agent->>Agent: LLM call
    Agent-->>Loop: actions
    Loop->>Loop: execute actions
    Note over Loop: return to queue.get()
```

### C. Scheduled Timer Registry

A per-agent registry of `ScheduledTimer` entries replaces the global `tick_interval_seconds`:

```python
@dataclass
class ScheduledTimer:
    timer_id: str
    interval: timedelta | None       # periodic when set
    next_fire_at: datetime | None    # one-shot when set
    callback_kind: str               # what handler runs on fire
    jitter_max: timedelta | None     # ±jitter to avoid thundering-herd
```

Config schema gains an `autonomy.timers` list:

```yaml
autonomy:
  level: "semi-autonomous"
  timers:
    - id: "memory_consolidation"
      interval_seconds: 300
      kind: "memory_consolidation"
      jitter_max_seconds: 30
```

Default config has zero timers. An agent with no timers and no inbound events sleeps forever (the asyncio task is parked on `queue.get()`). Liveness/health is independent — it lives in `AgentService.HealthCheck`, not in the autonomy loop.

Timers fire on `asyncio.loop.call_later` (monotonic), not wall-clock comparisons, to avoid drift over long uptime. Each fired timer enqueues a `ScheduledWake(timer_id, callback_kind)` and re-arms itself (for periodic timers).

### D. Salience-Triggered Wakes

When a memory write occurs (episodic, notes, reflection), the write path emits a `MemoryWriteEvent` with a `salience: float` attribute. If `salience > threshold` (configured per agent), a `SalienceWake` is enqueued.

**Status of the salience signal.** No write-side `salience` field exists in the codebase today: `grep -ri salience agents/memory/` returns zero hits, and the only relevance signal in [`agents/memory/episodic.py`](../../agents/memory/episodic.py) is the recall-side FTS5 BM25 score (normalised against `min_score` per [RFC 0017 §B/C/E](0017-persona-memory-injection-budget.md)). Recall-side BM25 is *not* the right primitive to repurpose: it scores a candidate against a query, while the wake trigger needs a query-free importance score for an inbound write. (PR #308 deep review C1 — initial draft incorrectly framed this as an existing signal; corrected here.)

This RFC therefore introduces the write-side salience computation as part of Phase 3, with the following ownership boundaries:

- **RFC 0024 owns**: the wake-trigger plumbing — `MemoryWriteEvent`, the `salience: float` field on writes, the threshold config, the `SalienceWake` enqueue path, and the loop-back guard in §F.
- **RFC 0027 (Reflection-Driven Consolidation), if accepted, owns**: the formal definition of how `salience` is computed for consolidations and contradictions. Until 0027 lands, Phase 3 ships a deliberately conservative computation (constant 0.0 for episodic appends, a fixed positive value for reflection contradictions) so the wake plumbing can be exercised end-to-end without committing to a scoring model. The threshold default stays above the conservative scores — salience-driven wakes stay disabled-by-default until a calibrated scoring model lands. See [Open Question §2](#open-questions).

Use cases (post-Phase 3, gated by the threshold default and by RFC 0027's scoring model):

- A reflection consolidation produced a contradiction → wake the agent to reconcile.
- A counterparty wrote a relationship update marking a long-running goal as resolved → wake to acknowledge.
- An inbound channel message processed by another agent changed shared context the current agent cares about.

This is the first time the memory layer drives the scheduler; the dependency direction (memory writes flow *out* to scheduler) is new, and §F's failure-mode analysis covers the loop-back risk.

### E. Channel Message Integration

In v0.3, channel messages arrive via `AgentService.ReceiveChannelMessage` ([`proto/task.proto`](../../proto/task.proto) line 23). The handler today builds an `AgentEvent` and calls `EventDispatcher.dispatch()`, which in turn calls `scheduler.wake()`. Under the new model:

1. `ReceiveChannelMessage` handler validates the event (existing `agents/channel_validation.py` logic, RFC 0011 PR 4a-i).
2. Builds an `AgentEvent(event_type=CHANNEL_MESSAGE, ...)`.
3. Calls `event_loop.enqueue(InboundEventWake(event))` directly.
4. Returns `TaskAck(success=True)` immediately — the agent will process when the loop drains its queue.

No tick fires for channel quiet periods. No agent that joined a channel pays polling cost when no one is talking. The change in Phase 4 is mechanical: `scheduler.wake()` becomes `event_loop.enqueue(InboundEventWake(event))` and the call now carries the event payload directly instead of relying on the agent to look it up after waking.

### F. Failure Modes

| Failure | Behaviour | Mitigation |
|---------|-----------|------------|
| `EventLoop` task crashes mid-event | Agent stops responding to all wake sources | Supervisor restart with exponential backoff; structured log emitted with `agent_id` and last-handled wake type ([RFC 0018](0018-structured-logging-framework.md) schema) |
| `SalienceWake` enqueued during shutdown | Wake is dropped | Acceptable — durable writes already landed; next agent restart reprocesses if needed |
| Memory write triggers wake → wake triggers memory write → loop | Infinite wake cycle, runaway cost (the v0.2.1 leak in a new costume) | `SalienceWake` is *not* enqueued for writes that originated inside the agent's own LLM response — track `source_span_id` on the write and suppress if it matches an active LLM span. **Phase 3 prerequisite**: today `agents/memory/` writes do not carry `source_span_id` (verified by `grep`). The Phase 3 PR must add the attribute to the write path before `SalienceWake` ships, or fall back to a coarser guard (e.g. "no `SalienceWake` while the agent's `on_event` lock is held"). RFC 0019 covers OTEL completion broadly; the memory-write span attribute is a follow-up PR rather than something to assume in place. (PR #308 deep review S3.) |
| Scheduled timer drift over long uptime | Timers fire on monotonic clock with `jitter_max` cap | `asyncio.loop.call_later` instead of wall-clock comparisons |
| Backpressure: queue grows faster than agent can drain | Memory growth, eventual OOM | `asyncio.Queue(maxsize=1024)` with `put_nowait` discard policy + counter metric `agent.wake.dropped`. Trade-off: under discard, a slow agent becomes invisible to its peers — the orchestrator's [`ChannelRouter.fanout`](../../internal/channels/fanout.go#L49) will not notice the drop, and the peer's `respond_policy` cannot react. Block-the-producer would push the slow-agent cost back into the channel router and degrade cross-agent traffic; discard is chosen so a single slow agent cannot stall the orchestrator. The drop is observable via `agent.wake.dropped`, not load-bearing. (Decided — see [Decided §1: backpressure](#decided-backpressure)) |
| Misconfigured timer `interval_seconds: 0.001` | Busy loop | Validate at config load with a `_MIN_INTERVAL` floor matching today's `TickScheduler._MIN_INTERVAL = 1.0` |

### G. Migration of Existing TICK Logic

The current `EventType.TICK` event handler does three things:

1. Calls `_inject_memory_context` to load relevant memory.
2. Runs the §F guard to decide whether to invoke the LLM.
3. If invoked, the LLM may produce actions (`DO_NOTHING`, `COMPLETE_TASK`, etc.).

Under the new model these collapse into:

1. **Inbound wake**: memory loads with the inbound event as the recall query (already happens on chat/channel events today).
2. **Salience wake**: memory loads with the *triggering write* as the recall query — the agent reflects on what just changed.
3. **Scheduled wake**: opt-in only; the timer's `callback_kind` determines what to load.

The §F guard becomes vestigial because the "empty context" condition can no longer arise — every wake has a triggering payload (an event, a write, or an explicit timer with a documented purpose). The guard stays in place through Phase 5 as a defence-in-depth measure during the migration; Phase 6 removes it.

## Security Considerations

- **Salience DoS.** `SalienceWake` is enqueued from inside the memory write path. A malicious or buggy memory write that sets `salience: 1.0` on a high-frequency loop could DoS the agent. Mitigation: clip salience to `[0.0, 1.0]` at the write site, and rate-limit `SalienceWake` to N/sec per agent (default 10/sec, configurable).
- **Config-driven busy loop.** Scheduled timer config is loaded from `agents.yaml`. A misconfigured `interval_seconds: 0.001` would busy-loop. Validate at config load with a `_MIN_INTERVAL` floor matching today's `TickScheduler._MIN_INTERVAL = 1.0`. Reject schemas at load time; do not silently clamp.
- **Inbound wake authentication.** Inbound wakes from `ReceiveChannelMessage` are already authenticated (RFC 0011 §C — `sender_id` is orchestrator-authoritative on publish; receivers MUST drop on mismatch per [`proto/task.proto`](../../proto/task.proto) lines 130-135). No new trust boundary is introduced for channel-driven wakes.
- **Wake source attribution under RFC 0023 leasing.** Once `WalletService` leasing lands ([RFC 0023](0023-llm-call-leasing.md)), every wake-driven LLM call carries a `wake.kind` attribute on the lease request so cost dashboards can attribute spend by wake source — and so a runaway-salience agent is visible at the wallet boundary, not only at the LLM-call metric.

## Phased Implementation Plan

| Phase | Scope | Backwards-compat |
|-------|-------|-------------------|
| 1 | Introduce `EventLoop` + `WakeEvent` types in a new `agents/event_loop.py`. `TickScheduler` becomes a deprecated alias that wraps `EventLoop` and synthesises a `ScheduledWake(timer_id="legacy_tick")` at the configured interval. **`EventDispatcher.dispatch()` keeps its synchronous-return contract via the `SyncDispatchHandle` mechanism in §B.1** — chat and cascading-dispatch callers continue to receive `list[AgentAction]`; only the *delivery* of the wake moves through the queue. Behaviour unchanged. | All existing config still works; `tick_interval_seconds` maps to one legacy timer. `SendChatMessage` and `ActionExecutor.dispatch` keep their current return shape. |
| 2 | Add `autonomy.timers` config block. Document the migration path. Both `tick_interval_seconds` and `timers` accepted (latter wins if both present). | Yes — old config still runs; warning logged when both are set. |
| 3 | Introduce the write-side `salience: float` field on `MemoryWriteEvent` (does not exist today — see §D), implement `SalienceWake` from the memory write path, add metric `agent.wake.salience_total`. Phase-3 prerequisite: memory writes must carry `source_span_id` for the loop-back guard in §F (verified absent today; ship the attribute or the coarser fallback in this phase). Default `salience_threshold` set above the observed maximum (effectively disabled) until calibrated. | Yes — opt-in by lowering `autonomy.salience_threshold`. Threshold default keeps salience wakes off, so the new field is observable but not behaviour-changing. |
| 4 | Migrate channel-message dispatch ([RFC 0011](0011-channels-bridges.md)) to call `event_loop.enqueue(InboundEventWake(event))` directly. `scheduler.wake()` keeps working for non-channel callers. | Yes — `wake()` adapter remains. |
| 5 | `tick_interval_seconds` emits a deprecation warning at config load. Document the path to `timers: []` (the no-timers case). Update RFC 0017 §F with a note that the guard is now vestigial. | Yes — warning only, behaviour unchanged. |
| 6 | Remove `tick_interval_seconds`. Delete §F guard code. Delete the `TickScheduler` legacy adapter. Remove `EventType.TICK` from the event taxonomy. | Breaking — minor version bump (pre-1.0). |

Phases 1–4 ship inside the v0.3.x window. Phase 5 lands in v0.4.0. Phase 6 deferred to v0.5+ once no consumer uses the legacy config.

**Phase 6 entrance criteria.** "No consumer uses the legacy config" is operationalised as all of:

1. Every persona shipped in [`config/agents.yaml`](../../config/agents.yaml) has migrated to the `autonomy.timers` shape (zero in-tree references to `tick_interval_seconds`).
2. The Phase 5 deprecation-warning counter (emitted by `agents/server_persona.py` at config load when `tick_interval_seconds` is set) reports zero hits across one full minor-version cycle in any monitored deployment Persatrix is aware of.
3. The RFC 0017 §F regression tests in [`agents/tests/test_persona_tick_shortcircuit.py`](../../agents/tests/test_persona_tick_shortcircuit.py) have been retitled to assert vestigial behaviour (`test_*_no_longer_reachable`) — i.e. the guard is provably unreachable in the event-driven model, not merely "still passes."

If any criterion is unmet at v0.5 cut, Phase 6 slips one minor version. This is explicit because RFC 0017 §F's guard removal is the irreversible step — once deleted, an empty-context TICK regression cannot be caught at the guard layer, only at the cost-regression CI gate (Test Strategy below). (PR #308 deep review S2.)

## Files Touched (Estimated)

| File | Phase | Change |
|------|-------|--------|
| `agents/event_loop.py` (new) | 1 | New module: `EventLoop`, `WakeEvent` taxonomy |
| [`agents/tick.py`](../../agents/tick.py) | 1 | `TickScheduler` becomes thin adapter over `EventLoop`; legacy timer synthesised |
| [`agents/persona.py`](../../agents/persona.py) | 1 | Wire `EventLoop` into agent lifecycle (start/stop) |
| [`agents/dispatch.py`](../../agents/dispatch.py) | 1 | `EventDispatcher.dispatch()` enqueues an `InboundEventWake` carrying a `SyncDispatchHandle` and `await`s the handle (§B.1). Replaces the current `scheduler.wake()` + `await agent.on_event(event)` shape but preserves the `list[AgentAction]` return contract for `SendChatMessage` and `ActionExecutor.dispatch`. |
| [`agents/server_persona.py`](../../agents/server_persona.py) | 2 | Read `autonomy.timers` from config; pass to `EventLoop` |
| [`schemas/agent.schema.json`](../../schemas/agent.schema.json) | 2 | Add `autonomy.timers` schema; mark `tick_interval_seconds` deprecated |
| [`config/agents.yaml`](../../config/agents.yaml) | 2 | Migrate stock personas to `timers: []` (or explicit consolidation timer) |
| [`agents/memory/episodic.py`](../../agents/memory/episodic.py) | 3 | Introduce write-side `salience: float` (no such field today, verified by `grep`) and `source_span_id`; emit `MemoryWriteEvent` on write. Conservative scoring per §D until RFC 0027's scoring model lands. |
| `agents/event_loop.py` | 3 | Subscribe to memory write events; enqueue `SalienceWake` above threshold; rate-limit |
| [`agents/observability/metrics.py`](../../agents/observability/metrics.py) | 3 | Add `agent.wake.{inbound,scheduled,salience,dropped}` counters with `wake.kind` attribute |
| [`agents/server_servicers.py`](../../agents/server_servicers.py) | 4 | `ReceiveChannelMessage` enqueues `InboundEventWake` directly |
| [`agents/persona_runtime/action_loop.py`](../../agents/persona_runtime/action_loop.py) | 5–6 | RFC 0017 §F guard marked vestigial in Phase 5; removed in Phase 6 |
| [`docs/rfcs/0017-persona-memory-injection-budget.md`](0017-persona-memory-injection-budget.md) | 5 | Cross-link: §F is superseded by RFC 0024 |
| [`agents/tests/test_persona_tick_shortcircuit.py`](../../agents/tests/test_persona_tick_shortcircuit.py) | 5–6 | Test names updated; tests deleted in Phase 6 with the guard |
| `agents/tests/test_event_loop.py` (new) | 1 | Coverage: enqueue/drain, queue-full discard, supervisor restart, legacy adapter, `SyncDispatchHandle` resolves with the agent's actions for chat-style callers and stays unresolved for fire-and-forget callers (§B.1) |
| `agents/tests/test_event_loop_salience.py` (new) | 3 | Salience threshold, write-loop guard, rate limit |
| `agents/tests/test_event_loop_timers.py` (new) | 2 | Periodic firing, jitter bounds, monotonic-clock drift |

## Test Strategy

- **Unit**: `EventLoop` enqueue/drain semantics; queue-full discard behaviour and metric increment; salience threshold enforcement; write-loop guard (a wake-triggered write must not enqueue a follow-up wake); timer monotonic firing; jitter within configured bounds.
- **Integration**: chat → wake → response; channel message → wake → response; scheduled timer firing under monotonic clock; agent with zero timers and zero events stays asleep across a 60s observation window (zero LLM calls, zero `_inject_memory_context` invocations, zero billable provider activity).
- **Regression**: existing RFC 0017 §F tests pass through Phases 1–5 because the guard remains in place. In Phase 6 they are deleted alongside the guard.
- **Cost regression** (new gate on this RFC): a "bored persona" benchmark — start an agent with `timers: []` and no events, observe for 60 s, assert zero LLM calls, zero `_inject_memory_context` invocations, zero token spend at the [RFC 0023](0023-llm-call-leasing.md) `WalletService` boundary. Wire this into CI as a soak test that runs on PRs touching any of the wake-path files: `agents/event_loop.py`, `agents/tick.py`, `agents/dispatch.py`, `agents/persona.py`, `agents/persona_runtime/**` (for the §F guard removal in Phase 5/6), `agents/memory/**` (for the salience plumbing in Phase 3 — a misconfigured threshold could enqueue `SalienceWake` for every write), `agents/server_persona.py` (config wiring), and `agents/observability/metrics.py` (a metric-emit change that drags a synchronous query into the wake path). The 60-second benchmark is cheap enough that running it on the broader trigger set is the right default — it is structurally trying to defend against the v0.2.1 leak class, and that class re-enters via any of these files. (PR #308 deep review S3.)
- **Migration**: a config-compat test asserts that an unmodified pre-Phase-2 `agents.yaml` (with `tick_interval_seconds: 60`) produces the same observable wake cadence as a Phase-2 config with `timers: [{id: "legacy_tick", interval_seconds: 60}]`.

## Open Questions

1. **Timer registry persistence.** Should scheduled timers survive an agent restart by default? If yes, where do they persist — the per-agent SQLite, or a new orchestrator-side table? Channels (RFC 0011 §A) chose per-agent SQLite with the `messages` table; reusing that table feels wrong (timers aren't messages). A new `scheduled_wakes` table per agent SQLite is the cheapest option but couples the scheduler to the storage backend that SA-1 (Personal/Society Storage Split, [storage-architecture-roadmap.md](../storage-architecture-roadmap.md) v0.4.0 RFC) may re-shape — hence the soft-dependency in the frontmatter. **Resolved before Phase 2** — SA-1 ships in v0.4.0, *after* Phase 2's v0.3.x window, so SA-1's outcome is not available at Phase 2 start. Phase 2 therefore ships a per-agent SQLite `scheduled_wakes` table now and migrates if/when SA-1 lands a society-store partition for timer state; the one-time `CREATE TABLE` (and a follow-up move) is cheap because there is no production timer data at v0.3.x scale. **Source of truth**: `agents.yaml` is canonical for the timer set in v0.3.x; the SQLite table is a derived cache rebuilt on startup from config. Runtime timer mutation (a `RegisterTimer()` API, etc.) would invert that contract — defer that decision until a use case appears. (PR #308 deep review C1 + M1 — RFC 0025 forward-reference replaced with SA-1; source-of-truth resolution added.)

2. **Salience threshold default.** Phase 3 ships with a high default that effectively disables salience wakes. What value enables it usefully without firing on every reflection? Needs a sample of production reflection scores from a deployed agent to calibrate. **Resolved before Phase 3 ships** — collect a week of salience-distribution data from a long-running persona before flipping the default.

3. **Interaction with RFC 0023 leasing.** Do `SalienceWake`-triggered LLM calls share a wallet bucket with inbound-event LLM calls, or do they have a separate budget bucket so a runaway-salience agent cannot starve user-facing traffic? Recommend: single bucket but emit `wake.kind` as a metric attribute and as a lease-request attribute so dashboards can attribute spend by wake source. Separate buckets would create a partition problem (when does the user-facing budget refill from the salience budget?). **Resolved before Phase 3.**

4. **Observability across the new wake sources.** Today `agent.tick.duration` is a useful single histogram. Under three wake sources, dashboards either (a) split into three histograms or (b) keep one with a `wake.kind` attribute. Recommend (b) for cardinality budget. Cross-reference [RFC 0019 §F](0019-opentelemetry-completion.md#f-metrics).

5. *(Promoted to [Decided §2: sub-agent non-inheritance](#decided-subagent-non-inheritance) — the recommendation was load-bearing on the Phase 1 PR shape and the question-then-self-answer construction was the same anti-pattern that S2 promoted out of OQ §6. PR #308 deep review M3.)*

## Decided

These were prior open questions whose resolution is now load-bearing on the design (referenced from §F failure-modes and §B.1). Documented here so future readers do not reopen them without re-reading the rationale.

1. <a id="decided-backpressure"></a>**Backpressure: discard, not block.** When the wake queue is full, `put_nowait` discards and increments `agent.wake.dropped`. Block-the-producer was rejected because the producer is the orchestrator's [`ChannelRouter.fanout`](../../internal/channels/fanout.go#L49) (or the memory write path), and a slow agent must not stall cross-agent traffic. The trade-off — a slow agent becomes invisible to its peers — is intentional and recovered via the `agent.wake.dropped` metric and by the per-recipient `respond_policy` already carried on `ChannelMessageEvent` (see [`proto/task.proto`](../../proto/task.proto) lines 161–167). Was Open Question §6 in the draft. (PR #308 deep review S2 — promoted from OQ to Decided to remove the assertion-as-question framing.)

2. <a id="decided-subagent-non-inheritance"></a>**Sub-agents do not inherit an `EventLoop`.** Sub-agents ([`agents/sub_agents/`](../../agents/sub_agents/)) do **not** have a `TickScheduler` today — `SubAgentSpawner.dispatch` (see [`agents/sub_agents/spawner.py:167`](../../agents/sub_agents/spawner.py#L167)) calls `BaseAgent.handle()` synchronously and returns the `TaskOutput`; there is no autonomy loop on the sub-agent side. Under this RFC the sub-agent dispatch path *remains* a direct in-process call — it does not enqueue an `InboundEventWake` and the parent agent's `EventLoop` is not shared with the child. Rationale: sub-agents are reactive by definition (RFC 0008 PR 3 explicitly framed them as request/response under the `DelegationRequest`/`DelegationResult` contract). A long-running sub-agent (RFC 0009 process-isolation work, if it lands) would get its *own* `EventLoop` instance at that point, not an inherited one — sharing the parent's queue would couple lifecycles in a way the request/response contract deliberately avoids. The Phase 1 PR documents this non-inheritance explicitly so a future reader doesn't infer "every agent has an `EventLoop`" from §B's prose. Was Open Question §5 in the draft. (PR #308 deep review S4 + M3 — S4 corrected the original inheritance error in the OQ; M3 promoted to Decided to mirror §1's structure, since this is now load-bearing on `EventLoop`'s shape, not an open question.)

## Decision / Next Steps

If accepted:

1. File `docs/rfcs/0024-pr-plan.md` with Phases 1–4 broken into PRs.
2. Resolve Open Question §1 (timer persistence) before Phase 1 lands. (Backpressure was OQ §6 in the draft and is now in [Decided §1](#decided-backpressure).)
3. Cross-link from RFC 0017 §F (guard becomes vestigial in Phase 5) and RFC 0011 (channel dispatch becomes an event source, not a tick consumer).
4. Sequence after [RFC 0023](0023-llm-call-leasing.md) lands at least Phase 1 — the leasing protocol gives the new wake sources structured cost attribution from day one, and `wake.kind` as a lease attribute is the cheapest moment to add.

### Implemented in v0.3.3 (Phases 1–4)

Phases 1–4 shipped under the v0.3.3 umbrella per [`0024-pr-plan.md`](0024-pr-plan.md) — PRs 1 ([#406](https://github.com/mkhomutov/Persatrix/pull/406)), 2 ([#407](https://github.com/mkhomutov/Persatrix/pull/407)), 2.1 ([#408](https://github.com/mkhomutov/Persatrix/pull/408)), 3a ([#409](https://github.com/mkhomutov/Persatrix/pull/409)), 3b ([#410](https://github.com/mkhomutov/Persatrix/pull/410)), 4 ([#411](https://github.com/mkhomutov/Persatrix/pull/411)), 5 ([#412](https://github.com/mkhomutov/Persatrix/pull/412)), 5.1 ([#413](https://github.com/mkhomutov/Persatrix/pull/413)). The agent autonomy loop is now structurally event-driven: `agents/event_loop.py` owns a per-agent `asyncio.Queue[WakeEvent]`; `TickScheduler` is a thin adapter that synthesises `ScheduledWake(timer_id="legacy_tick")` from the legacy `tick_interval_seconds`; `EventDispatcher.dispatch()` preserves its synchronous-return contract via `SyncDispatchHandle`. `autonomy.timers` config + a per-agent SQLite `scheduled_wakes` cache (wired into `initialize_persona_agents`) back scheduled wakes; write-side `salience` + `source_span_id` back the `SalienceWake` path (default-off at threshold `0.95`, above the conservative `0.6` scoring max, with a loop-back guard and per-agent rate-limit); the channel-message dispatch path enqueues `InboundEventWake` fire-and-forget; and the "bored persona costs nothing" cost-regression CI gate is wired as a release-blocker on the wake-path file set. [RFC 0017 §F](0017-persona-memory-injection-budget.md#f-empty-context-tick-short-circuit) is now structurally unreachable but stays in place — an inline cross-link in `agents/persona_runtime/action_loop.py` names this RFC's Phase 5/6 as its deletion path.

**Still scheduled**: Phase 5 (`tick_interval_seconds` deprecation warning at config load; the RFC 0017 §F file amendment) ships in v0.4.0; Phase 6 (`tick_interval_seconds` removal, §F guard deletion, `TickScheduler` adapter removal, `EventType.TICK` removal — breaking, minor bump pre-1.0) ships in v0.5+, gated on the [Phase 6 entrance criteria](#phased-implementation-plan). This is a partial-RFC closeout; the full-RFC closeout waits for Phase 6.

## Related Documentation

- [RFC 0017 — Persona Memory Injection Budget](0017-persona-memory-injection-budget.md) — §F empty-context TICK short-circuit, the symptom this RFC subsumes
- [RFC 0023 — LLM Call Leasing](0023-llm-call-leasing.md) — the cost-attribution layer that consumes `wake.kind` as a metric and lease attribute
- [RFC 0011 — Channels & Bridges](0011-channels-bridges.md) — channel message dispatch becomes a wake source (Phase 4)
- [RFC 0005 — Persona Agent + Memory](0005-persona-agent-memory.md) — current `_LLMPersonaAgent` and `TickScheduler` design
- [RFC 0019 — OpenTelemetry Completion](0019-opentelemetry-completion.md) — `wake.kind` attribute conventions
- [`agents/tick.py`](../../agents/tick.py) — current polling implementation
- [`agents/dispatch.py`](../../agents/dispatch.py) — current `EventDispatcher.dispatch()` and `scheduler.wake()` callsite
- [README — Cost Warning](../../README.md#-cost-warning) — the $35 incident motivating this work
