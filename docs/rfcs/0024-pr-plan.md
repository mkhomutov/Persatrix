# RFC 0024 — PR Implementation Plan (Event-Driven Agent Scheduling — Phases 1–4)

**RFC**: [0024-event-driven-scheduling.md](0024-event-driven-scheduling.md)
**Created**: 2026-05-21
**Branch prefix**: `feature/v033-rfc0024-`
**Target**: `main`
**Merge strategy**: Squash merge per [BRANCHING.md](../BRANCHING.md)
**Master plan**: [v0.3.3-plan.md Phase 2 (RFC 0024 implementation)](../v0.3.3-plan.md#phase-2--implement-rfc-0024-phases-14)

---

## Overview

RFC 0024 inverts the persona autonomy loop from fixed-interval polling to event-driven scheduling: agents wake on (a) inbound RPC event, (b) salience-triggered memory write, or (c) explicit scheduled timer, then park on `queue.get()` between wakes. The structural change collapses the v0.2.1 polling-cost class — a persona with `timers: []` and no inbound traffic pays no SQLite recall, no `_inject_memory_context`, no provider activity, and no wallet lease ([RFC §Summary](0024-event-driven-scheduling.md#summary)). The [RFC 0017 §F](0017-persona-memory-injection-budget.md#f-empty-context-tick-short-circuit) empty-context TICK guard stays in place but becomes structurally unreachable and is documented as vestigial; its deletion is a follow-up release.

This plan covers **Phases 1–4** of the [RFC §Phased Implementation Plan](0024-event-driven-scheduling.md#phased-implementation-plan), which is the v0.3.3 contract. Phase 5 (`tick_interval_seconds` deprecation warning) is v0.4.0; Phase 6 (`tick_interval_seconds` removal, §F guard deletion, `EventType.TICK` removal) is v0.5+, gated on the [Phase 6 entrance criteria](0024-event-driven-scheduling.md#phased-implementation-plan). Both are scoped under [§Future Phases](#future-phases) here without PR rows.

The work splits into **7 PRs**: five implementation PRs (Phase 3 split into 3a/3b so the write-side salience plumbing and the wake-enqueue path are reviewable in isolation), one review-follow-ups PR, and one Phases-1–4 closeout PR. The closeout shape mirrors the [RFC 0017 PR plan](0017-pr-plan.md) precedent — partial-RFC closeout because Phases 5–6 ship in later versions, not full-RFC closeout. Each PR leaves the repo in a passing-tests, lint-clean state and stays within the [BRANCHING.md](../BRANCHING.md) review surface.

**Prerequisites**:
- [RFC 0023](0023-llm-call-leasing.md) (LLM Call Leasing) — shipped in v0.3.2; PR 1 emits `wake.kind` as an OTEL span attribute on the LLM-call span alongside the existing `LeaseRequest.cause` ([proto/wallet.proto](../../proto/wallet.proto)) origin attribution. The `Cause` enum is unchanged; `wake.kind` is a new observability dimension, not a proto-surface change.
- [RFC 0011](0011-channels-bridges.md) (Channels & Bridges) — shipped in v0.3.0; PR 4 reshapes the channel-message dispatch path it introduced.
- [RFC 0017](0017-persona-memory-injection-budget.md) (Memory Injection Budget) — shipped in v0.2.2; the §F guard stays in place through this plan and is the regression-target the cost CI gate defends.
- [RFC 0005](0005-persona-agent-memory.md) (Persona Agent + Memory) — shipped in v0.2.0; PR 1 wires `EventLoop` into the agent lifecycle the persona builder owns.

**Hard gates**:
- **[RFC 0024 OQ §1](0024-event-driven-scheduling.md#open-questions) (timer persistence)** is **already resolved** in the RFC body: timer storage is per-agent SQLite (`scheduled_wakes` table), source of truth is `agents.yaml`, the table is a derived cache rebuilt on startup. Recorded here as a non-blocking gate so PR 2 has a single named resolution to link in its scope. SA-1 (Personal/Society Storage Split) re-shape risk is acknowledged and accepted in the RFC; the table is a one-time migration if SA-1 lands a society-store partition for timers in v0.4.0+. See [PR 2: Hard-gate confirmation](#pr-2-featurev033-rfc0024-timer-registry--autonomytimers-config--scheduled_wakes-table).
- **[Phase 3 prerequisite — `source_span_id`](0024-event-driven-scheduling.md#f-failure-modes)**: today `agents/memory/` writes do not carry `source_span_id` (verified by `grep` at RFC authoring time). PR 3a lands the attribute on the write path *before* PR 3b enqueues `SalienceWake`. The coarser fallback ("no `SalienceWake` while the agent's `on_event` lock is held") is named in the RFC as the alternative; PR 3a names which path Phase 3 takes — see [PR 3a Key implementation details](#pr-3a-featurev033-rfc0024-salience-prereqs--write-side-salience--source_span_id).

**Recommended merge order**: **PR 1 → PR 2 → PR 3a → PR 3b → PR 4 → PR 5 → PR 6**. The order tracks the RFC's phase boundaries: PR 1 (Phase 1) is the structural foundation — every later PR builds on `EventLoop` and `SyncDispatchHandle`. PRs 3a/3b (Phase 3) are reviewable in isolation only after PR 1's wake taxonomy exists. PR 4 (Phase 4) requires PR 1's `event_loop.enqueue` API. PR 4 (Phase 4 channel dispatch) is the closing implementation PR and carries the cost-regression CI gate as its acceptance, not a follow-up.

---

## Dependency Graph

```
[Hard gates]
  OQ §1 (timer persistence)  — resolved in RFC body (per-agent SQLite, agents.yaml = source of truth)
  Phase 3 prerequisite        — source_span_id on memory writes (PR 3a) before SalienceWake (PR 3b)
  ↓
PR 1 (agents/event_loop.py + WakeEvent + SyncDispatchHandle; TickScheduler thin adapter)   [RFC Phase 1]
  ↓
PR 2 (autonomy.timers config + per-agent SQLite scheduled_wakes; cache-from-agents.yaml)    [RFC Phase 2]
  ↓
PR 3a (MemoryWriteEvent + write-side salience: float + source_span_id; no SalienceWake yet) [RFC Phase 3 prereq]
  ↓
PR 3b (SalienceWake enqueue + threshold + rate-limit + loop-back guard + metrics)           [RFC Phase 3]
  ↓
PR 4 (channel-message dispatch routed through event_loop.enqueue; cost-regression CI gate)  [RFC Phase 4]
  ↓
PR 5 (review follow-ups)
  ↓
PR 6 (Phases-1–4 closeout — status: ⚠️ Partially Implemented (Phases 1–4))
```

PR 1 lands the structural foundation with no behaviour change — `TickScheduler` survives as a thin adapter that synthesises `ScheduledWake(timer_id="legacy_tick")` from the existing `tick_interval_seconds`. PRs 2 and 3a/3b are additive: new config block, new write-side attribute, new wake variant — each opt-in by default (PR 2 defaults to no timers; PR 3b defaults the threshold above the conservative salience scoring so the wake stays off until calibrated). PR 4 is the only behaviour-changing PR for an already-shipped surface (RFC 0011 channels), and the cost-regression CI gate gates its merge.

---

## PR Sequence

### PR 1: `feature/v033-rfc0024-event-loop` — EventLoop + WakeEvent + SyncDispatchHandle

**Depends on**: nothing (builds on the v0.3.2 baseline; RFC 0023's `LeaseRequest.wake_kind` field is already present from v0.3.2 Phase 5).
**Purpose**: Introduce the event-loop substrate in a new `agents/event_loop.py`. `TickScheduler` becomes a thin adapter that synthesises `ScheduledWake(timer_id="legacy_tick")` at the configured `tick_interval_seconds`. `EventDispatcher.dispatch()` keeps its synchronous-return contract via the `SyncDispatchHandle` mechanism in [RFC §B.1](0024-event-driven-scheduling.md#b1-synchronous-dispatch-callers-under-the-queue-model). No behaviour change at the agent surface — every existing test passes unchanged. Implements [RFC §Phased Implementation Plan Phase 1](0024-event-driven-scheduling.md#phased-implementation-plan).

#### Scope

| File | Change |
|------|--------|
| `agents/event_loop.py` | **New** — `EventLoop` class owning a single `asyncio.Queue[WakeEvent]` per agent, `_run()` coroutine that awaits `queue.get()` indefinitely and dispatches by wake variant. `WakeEvent` taxonomy: `InboundEventWake` (carries `AgentEvent` + optional `SyncDispatchHandle`), `ScheduledWake` (carries `timer_id`, `callback_kind`), `SalienceWake` (declared, not yet enqueued — produced by PR 3b). `SyncDispatchHandle` is an `asyncio.Future`-shaped helper the loop resolves with the agent's `list[AgentAction]` after `on_event()` completes. Queue is `maxsize=1024` with `put_nowait` discard policy per [RFC §F backpressure](0024-event-driven-scheduling.md#f-failure-modes) (Decided §1). |
| [`agents/tick.py`](../../agents/tick.py) | `TickScheduler` becomes a thin adapter over `EventLoop`. `start()` constructs the `EventLoop` if not already wired, registers a single legacy timer (`ScheduledWake(timer_id="legacy_tick", callback_kind="tick")`) at `self._interval` (preserving the `_MIN_INTERVAL = 1.0` floor), and forwards `wake()` to `event_loop.enqueue(InboundEventWake(...))`. The existing `is_idle` / `idle_count` semantics survive on the adapter side for v0.3.3; the §F-guard regression tests at `agents/tests/test_persona_tick_shortcircuit.py` keep passing because the legacy timer reproduces the v0.3.2 tick cadence. |
| [`agents/dispatch.py`](../../agents/dispatch.py) | `EventDispatcher.dispatch()` constructs a `SyncDispatchHandle`, enqueues `InboundEventWake(event, handle=handle)` via `event_loop.enqueue`, then `await`s the handle and returns its value (a `list[AgentAction]`). The current direct `await agent.on_event(event)` path is removed. The deep-copy semantics for `event.payload` and `event.metadata` ([RFC 0011 dispatch contract](0011-channels-bridges.md)) are preserved by copying *before* enqueue. `scheduler.wake()` calls remain as fire-and-forget wakes for non-`dispatch()` callers and are forwarded by the adapter; they construct an `InboundEventWake` *without* a handle, and the loop simply does not resolve one. |
| [`agents/persona.py`](../../agents/persona.py) | Wire `EventLoop` into the agent lifecycle (start/stop). The existing `TickScheduler` start/stop hooks now own the `EventLoop` lifetime via the adapter — the agent builder does not gain a new top-level dependency. |
| [`agents/persona_runtime/action_loop.py`](../../agents/persona_runtime/action_loop.py) | No behaviour change. Inline comment cross-link from the §F guard to RFC 0024 — guard stays load-bearing through Phase 5; the cross-link names Phase 5/6 as the deletion path. ([v0.3.3-plan Acceptance row](../v0.3.3-plan.md#acceptance-for-v033) — "RFC 0017 §F guard ... documented as vestigial; cross-link naming RFC 0024 Phase 5/6 as the deletion path added to the RFC 0017 file"; the RFC 0017 file itself is amended in Phase 5 of v0.4.0 per [RFC 0024 §Files Touched](0024-event-driven-scheduling.md#files-touched-estimated). PR 1's inline cross-link in `action_loop.py` is the lightweight precursor.) |
| `agents/tests/test_event_loop.py` | **New** — `EventLoop` enqueue/drain semantics; queue-full discard with `agent.wake.dropped` increment; `SyncDispatchHandle` resolves with the agent's `list[AgentAction]` for chat-style callers and stays unresolved for fire-and-forget callers ([RFC §B.1](0024-event-driven-scheduling.md#b1-synchronous-dispatch-callers-under-the-queue-model)); legacy-adapter cadence (`TickScheduler` with `interval=60.0` synthesises a `ScheduledWake` per minute); supervisor restart on `_run()` exception with structured-log fields (`agent_id`, last-handled wake variant). |
| `agents/tests/test_event_loop_compat.py` | **New** — config-compat: an unmodified `agents.yaml` with `tick_interval_seconds: 60` produces the same observable wake cadence as a future Phase-2 config with `timers: [{id: "legacy_tick", interval_seconds: 60}]`. Asserts identical `on_tick` invocation count over a fake-clock 5-minute window and identical idle-count progression. (Implements [RFC §Test Strategy — Migration](0024-event-driven-scheduling.md#test-strategy).) |

#### Key implementation details

- **`SyncDispatchHandle` is the load-bearing piece.** [RFC §B.1](0024-event-driven-scheduling.md#b1-synchronous-dispatch-callers-under-the-queue-model) commits to option (a): keep `EventDispatcher.dispatch()` synchronous-return through Phase 5. The three contracts that ride on the current shape — return-value (`SendChatMessage` extracts the chat reply from the returned actions), await/serialisation ([`ActionExecutor.dispatch`](../../agents/action_executor.py) wraps the inner dispatch in `asyncio.wait_for` for timeout bounding and queue-mediated serialisation against concurrent inbound events), and queue-ordering — are preserved by resolving the handle inside the loop *before* moving to the next wake. The `Future` is short-lived (resolved within one `on_event` call) and adds no steady-state cost for idle agents. PR 1's `test_event_loop.py` pins all three contracts. Naïve "enqueue and park" would silently break the chat path (`DEADLINE_EXCEEDED` for every chat call) and the cascade path (timeout accounting can no longer bound the wait).
- **Sub-agents do not inherit an `EventLoop`** per [RFC §Decided §2](0024-event-driven-scheduling.md#decided-subagent-non-inheritance). [`SubAgentSpawner.dispatch`](../../agents/sub_agents/spawner.py) keeps calling `BaseAgent.handle()` synchronously; the sub-agent dispatch path *remains* a direct in-process call and does not enqueue an `InboundEventWake`. PR 1's `event_loop.py` module docstring documents the non-inheritance explicitly so a future reader does not infer "every agent has an `EventLoop`" from [RFC §B](0024-event-driven-scheduling.md#b-event-loop-inversion)'s prose.
- **`wake.kind` lights up as an OTEL span attribute, not a proto-surface change.** [RFC 0023](0023-llm-call-leasing.md)'s `LeaseRequest` ([proto/wallet.proto](../../proto/wallet.proto)) carries only `Cause cause` (`CAUSE_WORKFLOW_TASK` / `CAUSE_CHAT` / `CAUSE_AUTONOMOUS_TICK` / `CAUSE_SUB_AGENT` / `CAUSE_CHANNEL_MESSAGE`) for origin attribution — wake variants are orthogonal to that taxonomy and there is no `wake_kind` field today. PR 1 emits `wake.kind` (`inbound` / `scheduled` / `salience` / `dropped`) as an OTEL attribute on the LLM-call span and as a dimension on the new `agent.wake.*` counters; the `Cause` attribution path is unchanged, so v0.3.2 cost-attribution dashboards keep working. The legacy timer emits `wake.kind = "scheduled"` with `timer_id = "legacy_tick"`; new wake variants light up as their phases land. Adding a proto field is deferred until a dashboard genuinely needs both dimensions at the wallet boundary — `trace_id` already links the lease to the wake span via OTEL.
- **Backpressure is discard, not block** per [RFC §Decided §1](0024-event-driven-scheduling.md#decided-backpressure). PR 1's tests pin the `agent.wake.dropped` increment shape; an integration test follows in PR 4 once the channel-message origin is the dominant producer.
- **Queue ordering is not load-bearing for inbound vs scheduled wakes.** [RFC §B](0024-event-driven-scheduling.md#b-event-loop-inversion) does not promise any ordering between an inbound RPC event and a scheduled-timer firing — both enqueue into the same FIFO. PR 1's tests assert FIFO drain order but do not promise priority across wake variants.
- **No salience plumbing here.** `SalienceWake` is *declared* on the taxonomy in PR 1 so the loop's `match`-style dispatch is exhaustive from day one, but no producer enqueues it. PR 3b wires the producer; PR 1's tests do not exercise it.

#### Tests

- `EventLoop`: enqueue/drain FIFO; queue-full discard increments `agent.wake.dropped`; loop survives an `on_event` exception (supervisor restart with backoff); a fire-and-forget `wake()` does not block the producer.
- `SyncDispatchHandle`: resolves with the agent's `list[AgentAction]` for a chat-style caller; stays unresolved when the wake carries no handle (fire-and-forget); `ActionExecutor.dispatch`'s `wait_for` timeout still bounds the wait correctly.
- Legacy adapter: `TickScheduler(interval=60)` synthesises one `ScheduledWake` per minute; idle-count progression matches v0.3.2 over a fake-clock 10-tick run.
- Config-compat (separate test file): unmodified `agents.yaml` with `tick_interval_seconds: 60` produces the same observable cadence as the (future) Phase-2 `timers` equivalent.

#### PR checklist

- [ ] `pytest agents/tests/test_event_loop.py agents/tests/test_event_loop_compat.py agents/tests/test_persona_tick_shortcircuit.py -q` passes.
- [ ] `ruff check agents/` clean; `mypy agents/` clean.
- [ ] `make test` clean (full Python + Go suite — `tests/integration/test_persona_e2e_grpc_events.py` and `tests/integration/test_persona_e2e_scheduling_memory.py` exercise the dispatch + tick paths; both must pass unchanged).
- [ ] `SyncDispatchHandle` resolves with the agent's actions for chat-style callers; stays unresolved for fire-and-forget callers (pinned by `test_event_loop.py`).
- [ ] `TickScheduler` is a thin adapter; legacy `tick_interval_seconds` synthesises a `ScheduledWake(timer_id="legacy_tick")` with identical observable cadence to v0.3.2.
- [ ] [RFC 0017 §F](0017-persona-memory-injection-budget.md#f-empty-context-tick-short-circuit) regression tests at [`agents/tests/test_persona_tick_shortcircuit.py`](../../agents/tests/test_persona_tick_shortcircuit.py) pass unchanged.
- [ ] `wake.kind` OTEL attribute lights up on the LLM-call span and as a dimension on the new `agent.wake.*` counters (not on `LeaseRequest`, which keeps `Cause cause` unchanged): legacy adapter emits `wake.kind = "scheduled"` with `timer_id = "legacy_tick"`. Assertion lives in `test_event_loop.py`; [`test_action_loop_tick_lease.py`](../../agents/tests/test_action_loop_tick_lease.py) is **not** amended — `Cause` attribution is preserved verbatim.
- [ ] [RFC 0024 row in ROADMAP](../../ROADMAP.md#rfc-master-index) → `🚧 Implementing` on this PR opening (first implementation PR); [v0.3.3-plan Master Progress Overview](../v0.3.3-plan.md#master-progress-overview) row 2 → 🔄 In progress; [Progress Overview](#progress-overview) row 1 filled.
- [ ] [§Version Map](../../ROADMAP.md#version-map) v0.3.3 row stays `🚧 Planning` (no version-map flip mid-implementation; that happens at release-prep PR 4 close).

---

### PR 2: `feature/v033-rfc0024-timer-registry` — `autonomy.timers` Config + `scheduled_wakes` Table

**Depends on**: PR 1 merged (`EventLoop` + `ScheduledWake` available).
**Purpose**: Replace the global `tick_interval_seconds` with a per-agent `autonomy.timers` list. Add a per-agent SQLite `scheduled_wakes` table as a derived cache so registered timers survive an agent restart. `tick_interval_seconds` keeps working through Phase 5 — both `autonomy.timers` and `tick_interval_seconds` are accepted, latter loses if both present, deprecation warning is Phase 5 / v0.4.0. Implements [RFC §Phased Implementation Plan Phase 2](0024-event-driven-scheduling.md#phased-implementation-plan).

#### Hard-gate confirmation

[RFC §Open Questions §1](0024-event-driven-scheduling.md#open-questions) (timer persistence) is **resolved before this PR opens** in the RFC body: per-agent SQLite `scheduled_wakes` table, source of truth is `agents.yaml`, the table is a derived cache rebuilt on startup from config. Runtime timer mutation (a `RegisterTimer()` API, etc.) is deferred until a use case appears. SA-1 (Personal/Society Storage Split, RFC 0029) is the soft-dependency the RFC frontmatter records — SA-1 ships in v0.4.0+, so Phase 2 ships the table now and migrates at a `CREATE TABLE` boundary if SA-1 lands a society-store partition for timer state. No production timer data exists at v0.3.x scale, so the migration is cheap. The PR 2 scope row below links this resolution.

#### Scope

| File | Change |
|------|--------|
| [`schemas/agent.schema.json`](../../schemas/agent.schema.json) | Add `autonomy.timers` array schema — each entry has `id: string` (required, unique within the persona), `interval_seconds: number` (≥ `_MIN_INTERVAL = 1.0`), `kind: string` (callback identifier — e.g. `"memory_consolidation"`), optional `jitter_max_seconds: number`. Mark `tick_interval_seconds` deprecated (keep `minimum: 1.0`; deprecation warning is Phase 5, not this PR). `additionalProperties: false` on `autonomy`. |
| [`config/agents.yaml`](../../config/agents.yaml) | Migrate stock personas to `timers: []` (the no-timers case — the v0.3.3 default). Do not register a `memory_consolidation` timer here; that is a follow-up once RFC 0027 lands. `tick_interval_seconds` left in place where present for the duration of Phase 2; the loader prefers `timers` if both are set and logs an INFO line naming which wins. |
| [`agents/server_persona.py`](../../agents/server_persona.py) | Read `autonomy.timers` from agent config; register each entry with the agent's `EventLoop` via a new `EventLoop.register_timer(ScheduledTimer)` method. If `timers` is unset and `tick_interval_seconds` is present, synthesise a single legacy timer (same shape as PR 1's adapter) — backwards-compat path. If both are set, `timers` wins; log INFO `"autonomy.timers takes precedence over tick_interval_seconds"`. |
| `agents/event_loop.py` | Add `register_timer(timer: ScheduledTimer)`, `unregister_timer(timer_id)`, and the periodic-firing implementation. Timers fire via `asyncio.loop.call_later` (monotonic, no wall-clock drift per [RFC §C](0024-event-driven-scheduling.md#c-scheduled-timer-registry)). Each fired timer enqueues `ScheduledWake(timer_id, callback_kind)` and re-arms with jitter. `ScheduledTimer` dataclass matches the RFC §C definition (`timer_id`, `interval`, `next_fire_at` for one-shot, `callback_kind`, `jitter_max`). Reject `interval_seconds < _MIN_INTERVAL` at registration with `ValueError`; do not silently clamp. |
| `agents/memory/scheduled_wakes.py` | **New** — per-agent SQLite cache for `scheduled_wakes`. Schema: `(timer_id TEXT PRIMARY KEY, kind TEXT, interval_ms INTEGER, jitter_ms INTEGER, next_fire_at_ms INTEGER, source TEXT DEFAULT 'config')`. Rebuilt on startup from `agents.yaml` — the table is a derived cache, not a source of truth. The `source` column reserves space for a future runtime-registered-timer use case without committing to it now (per [RFC §OQ §1](0024-event-driven-scheduling.md#open-questions) — "Runtime timer mutation ... defer that decision until a use case appears"). |
| `agents/tests/test_event_loop_timers.py` | **New** — `EventLoop.register_timer` smoke; periodic firing under monotonic clock (fake-clock 5-minute window with `interval_seconds=60` fires exactly 5 times); jitter stays within `±jitter_max_seconds`; one-shot timer (`next_fire_at` set, `interval` None) fires exactly once; `interval_seconds: 0.001` raises `ValueError` at registration (busy-loop guard per [RFC §Security Considerations](0024-event-driven-scheduling.md#security-considerations)); `scheduled_wakes` table is reconstructed from `agents.yaml` on a simulated restart and contains exactly the configured entries. |
| `agents/tests/test_server_persona_wiring.py` | Extend — both-set precedence (`timers` wins, INFO log emitted); `timers` only; `tick_interval_seconds` only (back-compat synthesised legacy timer). |
| `docs/observability.md` | Document the `agent.wake.scheduled` counter's new `timer_id` attribute (alongside the existing `wake.kind` attribute from PR 1's RFC 0023 bridge). |

#### Key implementation details

- **`agents.yaml` is canonical, the table is a cache.** Per [RFC §OQ §1](0024-event-driven-scheduling.md#open-questions), the SQLite `scheduled_wakes` table is rebuilt on agent startup from the YAML config. If a timer is removed from `agents.yaml`, the stale row is deleted on the next startup. The table's only purpose is to let an agent that restarted mid-jitter-window resume its next-fire schedule without firing immediately — an in-memory-only registry would lose that. Runtime timer mutation is *not* a Phase 2 surface; deferring it keeps the source-of-truth contract clean.
- **Monotonic clock, not wall-clock.** `asyncio.loop.call_later` is used per [RFC §C](0024-event-driven-scheduling.md#c-scheduled-timer-registry) so timer drift over long uptime is bounded. The `next_fire_at_ms` column stores monotonic-clock ms relative to a per-process epoch recorded at startup; a restart resets the epoch and the table is rebuilt — this is correct because the YAML config is canonical, and an `interval`-based timer does not need a stable wall-clock anchor.
- **One-shot timers are scoped in.** `ScheduledTimer.next_fire_at` (one-shot) is supported by the dataclass but not exposed via `agents.yaml` in Phase 2 — schema only carries `interval_seconds`. The internal API surface keeps room for one-shots so RFC 0024 Phase 3+ work (e.g. a salience-wake reminder) can use them without a Phase 2 schema follow-up.
- **No `memory_consolidation` timer wired here.** RFC 0027 (Reflection-Driven Consolidation) owns the consolidation cadence ([RFC §D](0024-event-driven-scheduling.md#d-salience-triggered-wakes) ownership boundary). Phase 2 ships the *mechanism*; the *policy* lands when RFC 0027 does.
- **Busy-loop guard at config load, not at fire time.** [RFC §Security Considerations](0024-event-driven-scheduling.md#security-considerations) requires rejecting `interval_seconds < _MIN_INTERVAL` at schema validation — silent clamping would mask a misconfigured persona. The schema enforces `minimum: 1.0`; `register_timer` re-checks at the API boundary so a programmatic caller can't bypass.
- **Test gate to add: `tests/integration/test_persona_e2e_scheduling_memory.py` keeps passing.** That suite exercises today's `tick_interval_seconds` path end-to-end via the gRPC surface; PR 2 must not regress it, since the back-compat synthesised legacy timer is the only thing keeping it green until v0.4.0 Phase 5 emits the deprecation warning.

#### Tests

- `register_timer`: periodic firing under monotonic fake-clock; jitter within bounds; one-shot fires once; `< _MIN_INTERVAL` raises.
- `scheduled_wakes` table: rebuilt from YAML on simulated restart; orphan rows deleted; `source='config'` for every YAML entry.
- Schema: `make validate` rejects `interval_seconds: 0.5` and `interval_seconds: 0`; accepts `interval_seconds: 1.0` (`_MIN_INTERVAL` floor).
- `server_persona` wiring: `timers` wins when both are set (INFO log captured); `tick_interval_seconds` synthesises the legacy timer when alone.
- Integration: `tests/integration/test_persona_e2e_scheduling_memory.py` passes unchanged (back-compat gate).

#### PR checklist

- [ ] `pytest agents/tests/test_event_loop_timers.py agents/tests/test_server_persona_wiring.py agents/tests/test_validate_persona.py agents/tests/test_validate_agent_schema.py -q` passes.
- [ ] `ruff check agents/` clean; `mypy agents/` clean.
- [ ] `make validate` passes against the new `autonomy.timers` schema.
- [ ] `make test` clean (full Python + Go suite, including the v0.3.2 e2e suites).
- [ ] [RFC 0024 OQ §1](0024-event-driven-scheduling.md#open-questions) resolution (per-agent SQLite, `agents.yaml` canonical) linked from the PR description.
- [ ] Both `autonomy.timers` and `tick_interval_seconds` accepted; latter still works; INFO log emitted when both are set.
- [ ] Stock personas in [`config/agents.yaml`](../../config/agents.yaml) migrated to `timers: []` (no-timers default; back-compat synthesised legacy timer continues to handle any remaining `tick_interval_seconds` entry).
- [ ] [Progress Overview](#progress-overview) row 2 filled.

---

### PR 3a: `feature/v033-rfc0024-salience-prereqs` — Write-Side Salience + `source_span_id`

**Depends on**: PR 2 merged.
**Purpose**: Land the Phase 3 prerequisites on the memory-write path — the `salience: float` field on `MemoryWriteEvent` and the `source_span_id` attribute the loop-back guard requires. **No `SalienceWake` enqueue yet** — this PR ships the data, PR 3b ships the wake. Splitting the phase keeps each diff reviewable: PR 3a touches `agents/memory/` only; PR 3b touches `agents/event_loop.py` and metrics only.

#### Scope

| File | Change |
|------|--------|
| [`agents/memory/episodic.py`](../../agents/memory/episodic.py) | Introduce write-side `salience: float` on memory writes (episodic, notes, reflection). No `salience` *field* exists on the write path today — `grep -ri salience agents/memory/` at PR-plan-authoring time returns only a docstring mention in [`agents/memory/facts.py`](../../agents/memory/facts.py) describing the RFC 0026 facts-tier *use-based decay* concept, which is unrelated to the wake-triggering write-side salience this RFC introduces. Conservative scoring per [RFC §D](0024-event-driven-scheduling.md#d-salience-triggered-wakes): episodic appends score `0.0`; reflection contradictions score a fixed positive value (`0.6`, named in this PR's tests as a constant so the calibration follow-up has a single place to flip). Clip to `[0.0, 1.0]` at the write site per [RFC §Security Considerations](0024-event-driven-scheduling.md#security-considerations). |
| `agents/memory/_events.py` | **New** (or extension of an existing event module) — `MemoryWriteEvent` dataclass: `agent_id`, `tier` (`"episodic"` / `"notes"` / `"reflection"` / `"relationship"` / `"facts"`), `salience: float`, `source_span_id: str | None`, `written_at`. Emitted on every memory write via a new `MemoryWriteBus` (in-process pub/sub, no asyncio fan-out yet — PR 3b adds the subscriber). |
| Memory write call sites (`agents/memory/`: `episodic.py`, `notes.py`, `relationship.py`, `facts.py`, `interactions.py`) | Each write call site emits a `MemoryWriteEvent` after a successful write, carrying the current `OTEL` span id as `source_span_id` (or `None` if no active span — captured via [`agents/observability/spans.py`](../../agents/observability/spans.py)). The current OTEL span is the LLM-response span for writes that originate inside the agent's own LLM action loop — the load-bearing input to PR 3b's loop-back guard. **Fallback recorded**: if span propagation through any write path turns out to be brittle in code review, PR 3a falls back to the coarser guard named in [RFC §F failure-mode row 3](0024-event-driven-scheduling.md#f-failure-modes) ("no `SalienceWake` while the agent's `on_event` lock is held") instead of `source_span_id`. The PR description must name which path was taken. |
| [`agents/observability/spans.py`](../../agents/observability/spans.py) | Add a `current_llm_span_id() -> str | None` helper used by the memory write path. Pure read of the current span context; no behaviour change to existing spans. |
| `agents/tests/test_memory_write_event.py` | **New** — every memory tier emits a `MemoryWriteEvent` exactly once per successful write; salience is clipped to `[0.0, 1.0]`; episodic-append salience is `0.0`; reflection-contradiction salience is the named constant; `source_span_id` is populated when an LLM span is active and `None` when no span is active. |
| `agents/tests/test_memory_write_event_no_subscriber.py` | **New** — `MemoryWriteEvent` emission is a no-op when no subscriber is registered (PR 3a ships zero subscribers; PR 3b is the first). Asserts the bus does not retain events when the queue is unread. |

#### Key implementation details

- **`MemoryWriteEvent` is the new pub/sub surface; `MemoryWriteBus` is in-process and synchronous.** PR 3a wires every write site to *emit* an event. There is no subscriber yet — calls are a no-op fan-out — so PR 3a cannot regress steady-state memory-write performance. PR 3b adds the `EventLoop` subscriber and the rate-limit; PR 3a's tests pin the empty-subscriber case so a misconfigured PR 3b cannot starve the writes.
- **Conservative scoring is deliberately uncalibrated.** Per [RFC §D](0024-event-driven-scheduling.md#d-salience-triggered-wakes), the formal scoring model is RFC 0027's territory. PR 3a's `0.6` for reflection contradictions is a named constant in `agents/memory/episodic.py` — not because the value is correct, but because it is *above* the threshold default PR 3b ships, so the wake plumbing can be exercised end-to-end without committing to a model. A v0.3.3 follow-up issue tracks the calibration step; per [v0.3.x sequencing §Open questions §3](../v0.3.x-sequencing.md#open-questions), the calibration ships with the threshold default disabled until a salience-distribution data sample exists for a long-running persona.
- **`source_span_id` is the load-bearing input to PR 3b's loop-back guard.** [RFC §F row 3](0024-event-driven-scheduling.md#f-failure-modes) commits to either the `source_span_id` path or the coarser `on_event`-lock path. PR 3a authors the `source_span_id` path first; if the span propagation through any of the five memory tier write paths turns out to be brittle (e.g. a tier that writes from a background task without an active LLM span), PR 3a falls back to the lock-held guard and PR 3b consumes that signal instead. Either way, PR 3b does not ship without the loop-back guard.
- **No behaviour change for the recall path.** PR 3a touches the *write* path. The [RFC 0017 §B/C/E](0017-persona-memory-injection-budget.md) recall-side BM25 score is unaffected; the new `salience` field is write-side and is *not* repurposed from BM25 per [RFC §D](0024-event-driven-scheduling.md#d-salience-triggered-wakes).
- **No new metrics here.** The wake counters (`agent.wake.salience`, `agent.wake.dropped`) land in PR 3b alongside the producer. Adding them in 3a would pin a counter on writes that never produce wakes, which the cost-regression CI gate (PR 4) would then have to special-case.

#### Tests

- Every memory tier emits a `MemoryWriteEvent` on successful write; no event emitted on a failed write.
- Salience clipped to `[0.0, 1.0]`; episodic-append = `0.0`; reflection-contradiction = the named constant (above PR 3b's threshold default by construction).
- `source_span_id` populated when an LLM span is active; `None` when no span is active.
- No subscriber → no event retention; the bus is fan-out-only.

#### PR checklist

- [ ] `pytest agents/tests/test_memory_write_event.py agents/tests/test_memory_write_event_no_subscriber.py agents/tests/test_inject_memory_context.py -q` passes.
- [ ] `ruff check agents/` clean; `mypy agents/` clean.
- [ ] `make test` clean (memory-write changes verified against the existing memory-write suites).
- [ ] PR description names whether the `source_span_id` path or the coarser `on_event`-lock fallback ([RFC §F row 3](0024-event-driven-scheduling.md#f-failure-modes)) was taken. The chosen path is the load-bearing input to PR 3b.
- [ ] No `SalienceWake` enqueued anywhere in this PR (PR 3b's territory).
- [ ] No new metric counters (PR 3b's territory).
- [ ] [Progress Overview](#progress-overview) row 3a filled.

---

### PR 3b: `feature/v033-rfc0024-salience-wake` — SalienceWake Enqueue + Threshold + Loop-Back Guard

**Depends on**: PR 3a merged (`MemoryWriteEvent` + `source_span_id` available).
**Purpose**: Wire the salience-triggered wake. Subscribe `EventLoop` to `MemoryWriteBus`; enqueue `SalienceWake` when `salience > threshold`; apply the loop-back guard from [RFC §F row 3](0024-event-driven-scheduling.md#f-failure-modes); rate-limit per [RFC §Security Considerations](0024-event-driven-scheduling.md#security-considerations). Threshold default keeps salience wakes off until calibrated. Implements [RFC §Phased Implementation Plan Phase 3](0024-event-driven-scheduling.md#phased-implementation-plan).

#### Scope

| File | Change |
|------|--------|
| `agents/event_loop.py` | Subscribe to `MemoryWriteBus` on `EventLoop` start. For each `MemoryWriteEvent` with `salience > threshold`, enqueue `SalienceWake(write_event)`. Apply the loop-back guard: suppress the enqueue if `source_span_id` matches an active LLM span on the same agent (verified via [`agents/observability/spans.py`](../../agents/observability/spans.py) — the same surface PR 3a's `current_llm_span_id()` helper exposes). If PR 3a took the coarser-fallback path, the guard is "do not enqueue while the agent's `on_event` lock is held" instead — the implementation branches once on the path PR 3a chose. Rate-limit at N `SalienceWake` per second per agent (default 10, configurable as `autonomy.salience_rate_max_per_sec`). |
| [`schemas/agent.schema.json`](../../schemas/agent.schema.json) | Add `autonomy.salience_threshold` (default `0.95` — above PR 3a's conservative reflection-contradiction `0.6`, so salience wakes stay off by default until calibrated) and `autonomy.salience_rate_max_per_sec` (default `10`). |
| [`agents/server_persona.py`](../../agents/server_persona.py) | Read both new keys; pass to `EventLoop` constructor. |
| [`agents/observability/metrics.py`](../../agents/observability/metrics.py) | Add `agent.wake.salience` counter (carries `wake.kind`, `tier`, and `suppressed_reason` attributes — `loopback`, `rate_limit`, `below_threshold` for the suppressed-enqueue branches). Add `agent.wake.dropped` counter for queue-full discard (already referenced by PR 1's discard policy; this PR is the formal home). The `agent.wake.{inbound,scheduled}` counters PR 1 added gain the `wake.kind` attribute for consistency. |
| `agents/tests/test_event_loop_salience.py` | **New** — threshold enforcement (write at `salience > threshold` enqueues; at `=` does not); loop-back guard suppresses a wake whose `source_span_id` matches the active LLM span; rate-limit caps at the configured per-second value and increments `agent.wake.salience{suppressed_reason="rate_limit"}`; below-threshold writes increment `agent.wake.salience{suppressed_reason="below_threshold"}` so dashboards can see "how close are we to crossing"; loop-back guard works for both the `source_span_id` path and the coarser `on_event`-lock path (branched test). |
| `agents/tests/test_event_loop_salience_default_off.py` | **New** — with default config (no `autonomy.salience_threshold` override), no `MemoryWriteEvent` from PR 3a's stock scoring (max `0.6`) ever produces a `SalienceWake`. Defends against a refactor that lowers the default. |
| `docs/observability.md` | Document the `agent.wake.salience` counter and its suppression attributes; document the threshold and rate-limit config keys. |

#### Key implementation details

- **The threshold default keeps salience wakes off by construction.** PR 3a ships conservative scoring with a max of `0.6` (reflection contradictions); PR 3b ships a threshold default of `0.95`. The two land in different PRs but the constants must agree by inequality. PR 3b's `test_event_loop_salience_default_off.py` is a regression backstop against either being changed in isolation.
- **`suppressed_reason` is the dashboard discriminator.** Without it, "no salience wakes" is indistinguishable from "salience wakes are working and the agent is quiet." The four reasons (`below_threshold`, `loopback`, `rate_limit`, plus the not-suppressed case) cover every branch in the enqueue decision tree; a dashboard can attribute every `MemoryWriteEvent` to exactly one outcome.
- **Loop-back guard is the v0.2.1 leak in a new costume.** [RFC §F row 3](0024-event-driven-scheduling.md#f-failure-modes) frames it explicitly: a memory write inside an LLM response that triggers a wake that triggers another LLM response that triggers another memory write is the same unbounded cost path the polling loop opened. PR 3b's tests must exercise this directly — a fake write with `source_span_id` matching an active LLM span MUST NOT enqueue a wake.
- **Rate-limit is a DoS guard, not a calibration knob.** [RFC §Security Considerations](0024-event-driven-scheduling.md#security-considerations) — a malicious or buggy memory write with `salience: 1.0` on a high-frequency loop must not DoS the agent. Default `10/sec` is the [RFC §Security Considerations](0024-event-driven-scheduling.md#security-considerations) value; configurable so a deployed persona can tighten it.
- **No `_inject_memory_context` invocation here.** A `SalienceWake` triggers the agent's `on_event` for the triggering write — which loads memory via the existing recall path with the *write* as the query (per [RFC §G](0024-event-driven-scheduling.md#g-migration-of-existing-tick-logic)). PR 3b does not edit `_inject_memory_context`; that surface is untouched through the entire v0.3.3 work.
- **The salience-default `0.95` is reviewed at calibration time, not changed in this PR.** Per [v0.3.x sequencing §Open questions §3](../v0.3.x-sequencing.md#open-questions), v0.3.3 ships the default disabled and a tracked-issue follow-up flips it after observed salience-distribution data exists. PR 3b's PR description records the tracked issue.

#### Tests

- Threshold enforcement (strict `>`); loop-back guard (both paths); rate-limit cap + counter increment.
- Default-off invariant: stock config + stock PR 3a scoring produces zero `SalienceWake` over a 1-minute fake-clock window with a stream of memory writes.
- Counter attributes: every suppressed enqueue increments `agent.wake.salience` with the right `suppressed_reason`; the not-suppressed case increments with `suppressed_reason="none"`.

#### PR checklist

- [ ] `pytest agents/tests/test_event_loop_salience.py agents/tests/test_event_loop_salience_default_off.py agents/tests/test_observability_metrics.py -q` passes.
- [ ] `ruff check agents/` clean; `mypy agents/` clean.
- [ ] `make test` clean.
- [ ] `make validate` passes with the new `autonomy.salience_threshold` and `autonomy.salience_rate_max_per_sec` keys.
- [ ] Threshold default is strictly above PR 3a's conservative-scoring maximum — the default-off invariant test is the regression backstop.
- [ ] Loop-back guard works for whichever path PR 3a took (`source_span_id` or the coarser `on_event` lock).
- [ ] Rate-limit caps `SalienceWake` enqueues per agent per second.
- [ ] Calibration follow-up issue filed and linked in the PR description (the threshold-flip-after-soak step per [v0.3.x sequencing OQ §3](../v0.3.x-sequencing.md#open-questions)).
- [ ] [Progress Overview](#progress-overview) row 3b filled.

---

### PR 4: `feature/v033-rfc0024-channel-dispatch` — Channel-Message Dispatch + Cost-Regression CI Gate

**Depends on**: PR 3b merged (Phases 1–3 complete; the `EventLoop` API surface is final).
**Purpose**: Reshape the [RFC 0011](0011-channels-bridges.md) channel-message dispatch path: `ReceiveChannelMessage` calls `event_loop.enqueue(InboundEventWake(event))` directly instead of `scheduler.wake()`. Land the **"bored persona" cost-regression CI gate** ([RFC §Test Strategy](0024-event-driven-scheduling.md#test-strategy)) — *the* v0.3.3 acceptance ([v0.3.3-plan acceptance row 1](../v0.3.3-plan.md#acceptance-for-v033)) — as a release-blocker on the wake-path file set. Implements [RFC §Phased Implementation Plan Phase 4](0024-event-driven-scheduling.md#phased-implementation-plan).

#### Scope

| File | Change |
|------|--------|
| [`agents/server_servicers.py`](../../agents/server_servicers.py) | `ReceiveChannelMessage` handler builds the `AgentEvent` and calls `event_loop.enqueue(InboundEventWake(event))` directly. Returns `TaskAck(success=True)` immediately — the agent processes when the loop drains. No `scheduler.wake()` call on the channel path. The handler still runs `agents/channel_validation.py` first ([RFC 0011 PR 4a-i](0011-pr-plan.md)). |
| `agents/dispatch.py` | `EventDispatcher.dispatch()` already enqueues an `InboundEventWake` after PR 1. Non-channel callers (chat, sub-agent, workflow-task) are unchanged here — they keep going through `dispatch()` so the `SyncDispatchHandle` resolves their return value. The Phase 4 change is to the *channel* surface, where the gRPC handler's only consumer of the return value (the `TaskAck`) doesn't need the agent's actions. The legacy `scheduler.wake()` adapter stays in place for any non-channel caller that still invokes it. |
| `tests/integration/test_channel_memory_integration.py` and the rest of `tests/integration/test_channel_*` | Verify that the channel path still functions end-to-end under the new enqueue shape — same agents, same channel, same observed message delivery; the change is mechanical and the existing assertions should pass unchanged. If any assertion was relying on the synchronous-return shape (`ChannelRouter.fanout` waiting on the agent's actions), surface that here — but RFC 0011's `respond_policy` already runs async on the orchestrator side, so the wait was not load-bearing. |
| `tests/integration/test_bored_persona_cost.py` | **New** — the cost-regression CI gate from [RFC §Test Strategy](0024-event-driven-scheduling.md#test-strategy). Start a persona with `timers: []` and no events; observe for 60 seconds (fake-clock-driven for unit speed, with a 5-second wall-clock smoke pass alongside); assert zero LLM calls, zero `_inject_memory_context` invocations, zero `MemoryWriteEvent` emissions on the wake path that would feed back into `SalienceWake`, zero `LeaseRequest` calls at the [RFC 0023](0023-llm-call-leasing.md) `WalletService` boundary. The metric assertion uses `agent.wake.{inbound,scheduled,salience,dropped}` counters — every counter must read zero. |
| `tests/integration/test_channel_fanout_backpressure.py` | **New** — channel-message origin under load is the first place where a slow agent can plausibly hit the `EventLoop` queue cap; asserts that queue-full discards a wake and increments `agent.wake.dropped` rather than blocking the producer. Covers the branch the bored-persona test deliberately does not exercise (zero-wake vs capped-wake). |
| `.github/workflows/` (CI workflow file) | Add a `cost-regression-gate` job that triggers on PRs touching any of the wake-path file set named in [RFC §Test Strategy](0024-event-driven-scheduling.md#test-strategy): `agents/event_loop.py`, `agents/tick.py`, `agents/dispatch.py`, `agents/persona.py`, `agents/persona_runtime/**`, `agents/memory/**`, `agents/server_persona.py`, `agents/observability/metrics.py`. The job runs `pytest tests/integration/test_bored_persona_cost.py -q` and fails the build if any of the four zero-counter assertions fire. This is a release-blocker, not a nightly. |
| [`README.md`](../../README.md) | The Cost Warning section gains a forward-pointer noting v0.3.3 closes the polling-loop class structurally (per [v0.3.3-plan Phase 4 PR 2 acceptance](../v0.3.3-plan.md#phase-4--v033-release-prep-execution)). The line is authored here so the implementation PR and the README claim land together; the v0.3.3 release-prep step PR ratifies the wording. |

#### Key implementation details

- **The channel path is the only behaviour change in this PR.** PRs 1–3b shipped opt-in or back-compat surfaces. PR 4 is the only place where an already-shipped RFC 0011 user-visible path changes shape. The mechanical change ("`scheduler.wake()` → `event_loop.enqueue(InboundEventWake(event))`") is narrow; the breadth of the test surface (every `tests/integration/test_channel_*` test) is the safety net.
- **The cost-regression CI gate is the v0.3.3 acceptance.** Per [v0.3.3-plan Acceptance row 1](../v0.3.3-plan.md#acceptance-for-v033), the bored-persona gate green-and-wired is what makes v0.3.3 ship. PR 4 lands it as a release-blocker on the wake-path file set and does not move it to nightly — moving to nightly defeats the gate per [RFC §Test Strategy](0024-event-driven-scheduling.md#test-strategy) and per [v0.3.3-plan Risk row 4](../v0.3.3-plan.md#risk-and-mitigations).
- **The trigger file set covers every re-entry path.** `agents/observability/metrics.py` is in the set because a metric-emit change could drag a synchronous query into the wake path (the v0.2.1 leak class). `agents/memory/**` is in the set because a misconfigured salience threshold could enqueue a `SalienceWake` for every write. The breadth is deliberate per [RFC §Test Strategy](0024-event-driven-scheduling.md#test-strategy) ("structurally trying to defend against the v0.2.1 leak class, and that class re-enters via any of these files"). The file set is a maintenance surface, not a one-time list — a future PR that introduces a new wake-path module (e.g. a new `agents/observability/*.py` module emitting a counter the loop consumes) must extend the trigger set in the same PR; the [v0.3.3-plan Risk row 4](../v0.3.3-plan.md#risk-and-mitigations) coupling is the master-plan-level reminder.
- **`agent.wake.dropped` is exercised here.** PR 1 declared the counter; PR 4's channel-message integration test is the first place where a slow agent under channel fan-out can plausibly hit the queue cap. The bored-persona test does not cover this branch — it verifies zero wakes, not capped wakes. A separate `tests/integration/test_channel_fanout_backpressure.py` asserts the discard policy under load.
- **The `MT-IDLE-001` manual test is authored here, executed in release-prep.** `docs/manual-tests/MT-IDLE-001.md` documents the human-driven version of the bored-persona observation (start a persona, observe for 60 s, confirm zero provider activity in `persatrix logs` and zero wallet activity in `/cost`). Execution is deferred to [v0.3.3-plan Phase 4 PR 1](../v0.3.3-plan.md#phase-4--v033-release-prep-execution).
- **`MT-COST-004` (RFC 0023 PR 5) must still pass under the event-driven model.** The TICK exhaustion path now uses `ScheduledWake(timer_id="legacy_tick")` via the adapter, but the `idle_reason=budget_denied` attribute the v0.3.2 dashboards consume must still be emitted. PR 4's PR checklist confirms `tests/integration/` coverage of the TICK budget-exhaustion path passes unchanged.

#### Tests

- Channel-message integration: every `tests/integration/test_channel_*` test passes unchanged.
- Bored-persona regression: zero LLM calls, zero `_inject_memory_context`, zero `LeaseRequest`, zero `agent.wake.*` counters over a 60-second fake-clock window.
- Channel-fanout backpressure: queue-full on the receiver enqueues `agent.wake.dropped` rather than blocking the producer.
- TICK budget-exhaustion: `tests/integration/test_action_loop_resource_exhausted.py` (and the RFC 0023 MT-COST-004 surface) passes unchanged; `idle_reason=budget_denied` attribute is still emitted by the legacy-adapter path.

#### PR checklist

- [ ] `pytest tests/integration/test_channel_*.py tests/integration/test_bored_persona_cost.py tests/integration/test_action_loop_resource_exhausted.py -q` passes.
- [ ] `ruff check agents/` clean; `mypy agents/` clean.
- [ ] `make test` clean.
- [ ] `cost-regression-gate` CI job wired to the wake-path file set; triggers on PR; fails the build on a non-zero counter.
- [ ] `docs/manual-tests/MT-IDLE-001.md` authored; execution deferred to [v0.3.3-plan Phase 4 PR 1](../v0.3.3-plan.md#phase-4--v033-release-prep-execution).
- [ ] `MT-COST-004` (RFC 0023) passes unchanged under the event-driven model — `idle_reason=budget_denied` still emitted.
- [ ] Channel-message dispatch goes through `event_loop.enqueue(InboundEventWake(event))` directly, not through `scheduler.wake()` ([v0.3.3-plan Acceptance row 2](../v0.3.3-plan.md#acceptance-for-v033)).
- [ ] [README Cost Warning](../../README.md#%EF%B8%8F-cost-warning--read-before-running) forward-pointer added.
- [ ] [Progress Overview](#progress-overview) row 4 filled.

---

### PR 5: `feature/v033-rfc0024-followups` — Review Follow-Ups

**Depends on**: PR 4 merged (all five implementation PRs complete).
**Purpose**: Address review findings surfaced during PRs 1–4. Follows the [RFC 0017 PR 6](0017-pr-plan.md) and [RFC 0023 PR 7](0023-pr-plan.md) precedent — "From PR N review" subsections, each finding paraphrased inline.

#### Scope

Per [.github/copilot-instructions.md](../../.github/copilot-instructions.md) ("Local-only files MUST NEVER be referenced in any committed file"), each entry paraphrases the finding inline and does **not** reference or link any local PR review report.

##### From PR 1 review

_PR 1 review applied four findings inline (`get_event_loop()` →
`get_running_loop()`; public `EventLoop.has_timer` / `EventLoop.task`;
test monkey-patch save/restore; `SyncDispatchHandle.__await__` typing
to close a `dispatch.py` `Any`-return leak). Three deferred:_

_(1) **Reentrant-dispatch deadlock.** `on_event(A)` re-dispatching to A
enqueues on A's own queue and awaits a handle the blocked supervisor
cannot resolve. Pre-existing via non-reentrant `agent._lock`; queue
reshapes but does not create. Add a regression test pinning the
contract or a producer-side "enqueue-from-own-supervisor" guard.
Tracked here (not PR 2) — `autonomy.timers` does not change dispatch
shape. (2) **Queue-full `WARNING` spam.** Per-drop log in
`EventLoop.enqueue`. Fine in Phase 1; PR 4's channel-dispatch makes
drops a foreseeable steady state. Rate-limit or downgrade to `DEBUG`
once `agent.wake.dropped` is the observability surface — pin in PR 4.
(3) **`_wake_kind` vs. `dropped` label.** Helper returns
`inbound / scheduled / salience / unknown`; `dropped` never reaches it.
Before PR 4 wires `agent.wake.dropped`, decide whether the counter
shares the `wake.kind` attribute (add a `dropped` label the helper
never emits) or is a separate counter — pin in PR 4's RFC 0019
convention section._

##### From PR 2 review

_None recorded at plan-authoring time._

##### From PR 3a review

_None recorded at plan-authoring time._

##### From PR 3b review

_None recorded at plan-authoring time._

##### From PR 4 review

_None recorded at plan-authoring time._

#### PR checklist

- [ ] All deferred review findings addressed or downgraded to tracked issues with rationale.
- [ ] `make test` + `make lint` clean.
- [ ] Deferred test gaps from PRs 1–4 reviews filled.
- [ ] [Progress Overview](#progress-overview) row 5 filled.

---

### PR 6: `feature/v033-rfc0024-close` — Phases 1–4 Closeout

**Depends on**: PR 5 merged.
**Purpose**: Mark RFC 0024 as partially implemented through Phase 4. Phases 5–6 stay open with explicit version targets per the [RFC 0017 PR 7](0017-pr-plan.md) and [RFC 0021 closeout shape](0021-persona-temporal-awareness.md) precedent — a partial-RFC closeout, since the full-RFC closeout waits for Phase 6 in v0.5+.

#### Scope

| File | Change |
|------|--------|
| [`docs/rfcs/0024-event-driven-scheduling.md`](0024-event-driven-scheduling.md) | Status → `⚠️ Partially Implemented (Phases 1–4)`. Append an "Implemented in v0.3.3" note to Decision/Next Steps. Phases 5–6 stay scheduled per the RFC's [§Phased Implementation Plan](0024-event-driven-scheduling.md#phased-implementation-plan). |
| [`ROADMAP.md`](../../ROADMAP.md) | RFC 0024 row → `⚠️ Partially Implemented (Phases 1–4)`; target column → `v0.3.3 (Phases 1–4) + v0.4.0 (Phase 5) + v0.5+ (Phase 6)`; merged-PR rows for PRs 1–6; `Last updated` refresh. |
| [`docs/rfcs/0024-pr-plan.md`](0024-pr-plan.md) | [Progress Overview](#progress-overview) rows filled with merged-PR numbers and dates; all checklists complete. |
| [`docs/v0.3.3-plan.md`](../v0.3.3-plan.md) | [Master Progress Overview](../v0.3.3-plan.md#master-progress-overview) row 2 → ✅ Merged with the workstream's first and final merge dates. |

No code changes; doc-only. `CHANGELOG.md` is **deferred to the v0.3.3 release process** ([v0.3.3-plan Phase 3 / 4](../v0.3.3-plan.md#phase-3--v033-release-prep-plan)), mirroring the [RFC 0017 PR 7 precedent](0017-pr-plan.md) and [RFC 0023 PR 8 precedent](0023-pr-plan.md).

#### PR checklist

- [ ] RFC 0024 status = `⚠️ Partially Implemented (Phases 1–4)`.
- [ ] [ROADMAP RFC Master Index](../../ROADMAP.md#rfc-master-index) updated; merged-PR history includes PRs 1–6 (of this plan).
- [ ] [v0.3.3-plan Master Progress Overview](../v0.3.3-plan.md#master-progress-overview) row 2 → ✅ Merged.
- [ ] `make test`, `make lint`, `make validate` pass (doc-only change confirms no regression).
- [ ] [Progress Overview](#progress-overview) row 6 filled.

---

## Future Phases

Out of scope for v0.3.3; recorded here so future readers can see the full RFC arc without re-reading the RFC body.

- **Phase 5 — v0.4.0**: `tick_interval_seconds` emits a deprecation warning at config load; [RFC 0017 §F](0017-persona-memory-injection-budget.md#f-empty-context-tick-short-circuit) gains an explicit "vestigial — superseded by RFC 0024" note (the v0.3.3 work added the inline cross-link in `action_loop.py`; the RFC 0017 file amendment is Phase 5's deliverable). Behaviour unchanged.
- **Phase 6 — v0.5+**: remove `tick_interval_seconds`; delete the §F guard code; delete the `TickScheduler` legacy adapter; remove `EventType.TICK` from the event taxonomy. Breaking — minor version bump (pre-1.0). Gated on the [Phase 6 entrance criteria](0024-event-driven-scheduling.md#phased-implementation-plan): (1) every persona in `config/agents.yaml` migrated to `autonomy.timers`; (2) Phase 5 deprecation-warning counter reports zero hits across one full minor-version cycle in any monitored deployment Persatrix is aware of; (3) [RFC 0017 §F regression tests](../../agents/tests/test_persona_tick_shortcircuit.py) retitled to assert vestigial behaviour (`test_*_no_longer_reachable`). If any criterion is unmet at v0.5 cut, Phase 6 slips one minor version.

No PR rows for these phases in this plan — Phase 5 lands in a v0.4.0 PR plan (separate file); Phase 6 lands in v0.5+. Cross-link from each future plan to this one when it opens.

---

## Risk and Mitigations

| Risk | Mitigation |
|------|------------|
| `SyncDispatchHandle` is subtle — three contracts ride on `EventDispatcher.dispatch()` (return-value, await-serialisation, queue-mediated ordering). A naïve "enqueue and park" silently breaks chat and timeout accounting. | [RFC §B.1](0024-event-driven-scheduling.md#b1-synchronous-dispatch-callers-under-the-queue-model) commits to option (a) (synchronous-return preserved via `Future`-shaped handle); PR 1's `test_event_loop.py` pins all three contracts. The handle is short-lived; no steady-state cost for idle agents. |
| Phase 3 introduces two memory-write attributes (`salience`, `source_span_id`) that don't exist today. Coupling them in one PR raises partial-landing risk. | The phase is split into PR 3a (write-side attributes only, no wake) and PR 3b (wake only, consuming PR 3a's signals). PR 3a's empty-subscriber test pins that emission is a no-op until PR 3b subscribes; PR 3b's default-off invariant test pins that the threshold default is strictly above PR 3a's max scoring. |
| `source_span_id` plumbing through every memory-tier write path turns out brittle (a tier that writes from a background task without an active LLM span). | [RFC §F row 3](0024-event-driven-scheduling.md#f-failure-modes) names the fallback explicitly: "no `SalienceWake` while the agent's `on_event` lock is held." PR 3a's PR description records which path was taken; PR 3b consumes whichever signal PR 3a chose. Either path satisfies the loop-back guard. |
| Phase 4's channel-dispatch rewrite touches the v0.3.0 RFC 0011 surface (stable since 2026-05-12). Regression surfaces as message loss or latency spike. | The change is mechanical at the call site (`scheduler.wake()` → `event_loop.enqueue`). The full `tests/integration/test_channel_*` suite is the safety net; the cost-regression CI gate (PR 4) and the backpressure integration test (PR 4) cover the new shapes. RFC 0011's `respond_policy` already runs async on the orchestrator side, so no caller was depending on synchronous-return from the channel-message path. |
| The 60-second bored-persona CI gate is non-trivial CI time per PR. Risk of being skipped or moved to nightly. | The gate's trigger set ([RFC §Test Strategy](0024-event-driven-scheduling.md#test-strategy)) covers every file that could re-introduce the polling-cost class. Moving to nightly defeats the gate. This plan documents the PR-trigger shape as a release-blocker invariant; the fake-clock-driven variant (per PR 4) keeps the wall-clock overhead bounded for the PR-trigger run. |
| Salience-threshold default ships disabled; a future PR could lower it without realising the conservative-scoring max is `0.6`. | PR 3b's `test_event_loop_salience_default_off.py` is a regression backstop: stock config + stock PR 3a scoring produces zero `SalienceWake`. Editing either constant in isolation breaks the test. |
| Per-agent SQLite `scheduled_wakes` table couples to a backend SA-1 (RFC 0029) may re-shape in v0.4.0+. | Per [RFC §OQ §1](0024-event-driven-scheduling.md#open-questions): table is a derived cache, source of truth is `agents.yaml`. If RFC 0029 lands a society-store partition in v0.4.0+, the migration is a one-time `CREATE TABLE` at the new location — no production timer data at v0.3.x scale. |
| `tick_interval_seconds` legacy back-compat path silently changes wake cadence if the synthesised `ScheduledWake(timer_id="legacy_tick")` does not reproduce the previous cadence exactly. | PR 1 ships a config-compat test (`test_event_loop_compat.py`) asserting identical `on_tick` invocation count and idle-count progression over a fake-clock 5-minute window. The test runs on every CI build through Phase 5. |
| This plan rots as PRs 1–6 land. | Each PR's checklist updates the [Progress Overview](#progress-overview) and the [v0.3.3-plan Master Progress Overview](../v0.3.3-plan.md#master-progress-overview); the [ROADMAP Hygiene](#roadmap-hygiene) rules below are part of every PR. |

---

## ROADMAP Hygiene

Per [.github/copilot-instructions.md §Status Hygiene](../../.github/copilot-instructions.md) and [v0.3.3-plan §ROADMAP hygiene](../v0.3.3-plan.md#roadmap-hygiene):

- **This PR-plan PR opens / merges** → no RFC 0024 status change — authoring a PR plan does not start implementation; RFC 0024 stays `📋 Proposed`. The [RFC Master Index](../../ROADMAP.md#rfc-master-index) *target* flips from `v0.3.3` (set by the v0.3.3-plan Phase 0 PR) to `v0.3.3 (Phases 1–4) + v0.4.0 (Phase 5) + v0.5+ (Phase 6)` in this PR (the explicit version-arc shape). [§Version Map](../../ROADMAP.md#version-map) gains a v0.3.3 row at `🚧 Planning`.
- **PR 1 opens** → RFC 0024 row → `🚧 Implementing` (first implementation PR); [v0.3.3-plan Master Progress Overview](../v0.3.3-plan.md#master-progress-overview) row 2 → 🔄 In progress.
- **Each PR merges** → fill the [Progress Overview](#progress-overview) row with the PR number and date.
- **PR 6 merges** → RFC 0024 row → `⚠️ Partially Implemented (Phases 1–4)`; [v0.3.3-plan Master Progress Overview](../v0.3.3-plan.md#master-progress-overview) row 2 → ✅ Merged; `Last updated` refresh.

---

## Progress Overview

| # | RFC Phase | Title | Branch | Status | GitHub PR | Merged |
|---|-----------|-------|--------|--------|-----------|--------|
| 1 | 1 | EventLoop + WakeEvent + `SyncDispatchHandle` (TickScheduler thin adapter) | `feature/v033-rfc0024-event-loop` | 🔀 PR open | this PR | — |
| 2 | 2 | `autonomy.timers` config + per-agent SQLite `scheduled_wakes` | `feature/v033-rfc0024-timer-registry` | ⬜ Not started | — | — |
| 3a | 3 (prereq) | Write-side `salience` + `source_span_id` (no wake yet) | `feature/v033-rfc0024-salience-prereqs` | ⬜ Not started | — | — |
| 3b | 3 | `SalienceWake` + threshold + loop-back guard + rate-limit | `feature/v033-rfc0024-salience-wake` | ⬜ Not started | — | — |
| 4 | 4 | Channel-message dispatch + cost-regression CI gate | `feature/v033-rfc0024-channel-dispatch` | ⬜ Not started | — | — |
| 5 | — | Review follow-ups | `feature/v033-rfc0024-followups` | ⬜ Not started | — | — |
| 6 | — | Phases-1–4 closeout | `feature/v033-rfc0024-close` | ⬜ Not started | — | — |

**Status legend**: ⬜ Not started · 🔄 In progress · 🔀 PR open · ✅ Merged · ⏭ Deferred

---

## Related Documentation

- [RFC 0024 — Event-Driven Agent Scheduling](0024-event-driven-scheduling.md) — canonical spec.
- [v0.3.3-plan.md](../v0.3.3-plan.md) — master plan; row 2 of the Master Progress Overview is this workstream.
- [RFC 0017 PR plan](0017-pr-plan.md) — structural template (partial-RFC closeout + review-follow-ups shape).
- [RFC 0023 PR plan](0023-pr-plan.md) — most recent multi-phase example; PR 1's `wake.kind` plumbing consumes the lease attribute v0.3.2 PR 5 reserved.
- [RFC 0017 — Persona Memory Injection Budget](0017-persona-memory-injection-budget.md) — §F guard becomes structurally unreachable in v0.3.3 but stays in place through Phase 5.
- [RFC 0011 — Channels & Bridges](0011-channels-bridges.md) / [RFC 0011 PR plan](0011-pr-plan.md) — the channel-message origin PR 4 reshapes.
- [RFC 0005 — Persona Agent + Memory](0005-persona-agent-memory.md) — current `_LLMPersonaAgent` lifecycle PR 1 wires `EventLoop` into.
- [RFC 0019 — OpenTelemetry Completion](0019-opentelemetry-completion.md) — `wake.kind` attribute conventions; `source_span_id` propagation through PR 3a's write path.
- [`agents/tick.py`](../../agents/tick.py) — current polling implementation, becomes a thin adapter in PR 1.
- [`agents/dispatch.py`](../../agents/dispatch.py) — current `EventDispatcher.dispatch()` callsite, rewritten around `SyncDispatchHandle` in PR 1.
- [README — Cost Warning](../../README.md#%EF%B8%8F-cost-warning--read-before-running) — the $35 v0.2.1 incident motivating this RFC; v0.3.3 closes the polling-loop class structurally.
- `docs/manual-tests/MT-IDLE-001.md` — authored in PR 4, executed in [v0.3.3-plan Phase 4 PR 1](../v0.3.3-plan.md#phase-4--v033-release-prep-execution).
