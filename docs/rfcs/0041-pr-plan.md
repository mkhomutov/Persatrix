# RFC 0041 — PR Implementation Plan (Phase 1 — vocabulary + adapter)

> Owning RFC: [0041-typed-event-taxonomy-lifecycle-callbacks.md](0041-typed-event-taxonomy-lifecycle-callbacks.md) · Target: **v0.4.0** (no version-plan doc exists yet — this plan is authored ahead of the v0.4.0 plan and slots into it when that lands). Reciprocal gate: [RFC 0044 PR 4b](0044-pr-plan.md) consumes the typed events PR 7 emits.

## Overview

RFC 0041 Phase 1 is **vocabulary + adapter**: ship the event taxonomy, the callback Protocol + dispatcher, and the bounded in-process stream, then wrap today's cross-cutting concerns (wallet lease, sanitizer/redactor, F-3 recall filter) as callbacks — **with no user-visible behaviour change**. Phase 1 is **entirely in-process Python**: no transport surface, no proto change, no Go-orchestrator work (that is Phase 2, gated on [RFC 0040](0040-agent-orchestrator-transport-unification.md)).

The one external precondition — RFC 0044's golden-trace *format* this plan's tests record against — **already shipped** in v0.3.11 (`schemas/eval_set.schema.json`, `evaluators/assertions.py` with `event_count`/`event_sequence`, `evaluators/runner.py`, `evaluators/persona_driver.py`). This plan's terminal deliverable (PR 7) emits the typed `Error` events that unblock RFC 0044's `EVAL-ERROR-001`/`002` event goldens — a staged handoff, not a cycle ([RFC 0041 §M-4](0041-typed-event-taxonomy-lifecycle-callbacks.md#m-4-eval-is-gated-on-a-stable-surface)).

This plan slices Phase 1 into small, independently-reviewable, test-first PRs. Each PR is green on `make test-python`, `make lint-python` (root ruff + mypy), and `python scripts/checks/file_size.py --strict` before merge, and keeps ROADMAP/RFC status hygiene.

### Open-question resolutions locked at plan-authoring time

Per [RFC 0041 §Open Questions](0041-typed-event-taxonomy-lifecycle-callbacks.md#open-questions), all seven carry a DECIDED/DEFERRED disposition. The four **DECIDED** land in Phase 1 as noted; the three **DEFERRED** are Phase-2 and out of this plan:

- **OQ #1 (event identity across retries) → PR 2.** A retried model call is a distinct `before_model`/`after_model` firing (hooks fire once **per `create_message`**, not per turn) sharing the turn's `turn_id` with monotonic `seq`; per-event `event_id` (ULID) is the reference key `Error.cause_event_id` uses.
- **OQ #3 (in-callback model calls) → PR 2.** A callback's own model call emits a dedicated `CallbackModelOutput` event, **not** `ModelOutput(role="callback")` — channel-publish subscribes to `ModelOutput` only and would otherwise mis-publish a moderation call as the assistant turn.
- **OQ #4 (privileged callback set) → PR 3.** Model-path privileged callbacks are `{input-sanitizer, wallet-lease}` in the reserved `priority < 0` band, fixed order sanitizer → wallet-lease; the event-stream redactor is the §B stream transform (PR 4), ordered ahead of all callbacks; F-3 recall / quality-bar are non-privileged (`priority >= 0`).
- **OQ #6 (`CallbackContext.state` typing) → PR 3.** Ships as a transitional `LegacyState` Protocol bundling the existing per-store accessors; the `ScopedState` rename rides the Phase-2 sweep that also re-types `StateDelta.scope` (OQ #2).
- **DEFERRED — Phase 2:** OQ #2 (`StateDelta.scope` → RFC 0042 `Scope` enum), OQ #5 (OTEL span model + model-call-start boundary), OQ #7 (`ContextInjection` recall event).

### File-size constraints (cap = 500 per [`scripts/checks/file_size.py --strict`](../../scripts/checks/file_size.py))

The emission sites (PR 5/6) are **at or against the cap** — this is the sharpest constraint in the plan and shapes the emission design:

| File | Lines now | Headroom | Routing |
|------|-----------|----------|---------|
| [`agents/base.py`](../../agents/base.py) | **500** | **0** | `_run_llm_loop` / `_execute_tools`. Needs an extraction (below) *before* any emit line lands. |
| [`agents/persona_runtime/action_loop.py`](../../agents/persona_runtime/action_loop.py) | **495** | 5 | The **primary** persona emission site; the repo already extracts from it (`action_validation.py`). Needs a small extraction. |
| [`agents/tools/builtin.py`](../../agents/tools/builtin.py) | **500** | **0** | Tool *definitions* — **not** an emission site (see design note); untouched by this plan. |
| [`agents/persona_runtime/prompt_assembly.py`](../../agents/persona_runtime/prompt_assembly.py) | 489 | 11 | `before_model` context assembly; minimal touch. |
| [`agents/persona_runtime/memory_context.py`](../../agents/persona_runtime/memory_context.py) | 483 | 17 | F-3 recall callback (PR 6); minimal touch. |
| [`agents/tools/registry.py`](../../agents/tools/registry.py) | 299 | ample | Tool-event emission centralizes **here**, keeping the near-cap loop files near-neutral. |
| [`agents/llm_client.py`](../../agents/llm_client.py) | 398 | ample | The `create_message` wallet-lease chokepoint → `before_model`/`after_model` dispatch. |
| New `agents/events.py`, `agents/callbacks.py` | — | — | The taxonomy, dispatcher, and stream live here — the emission sites gain a *call*, not logic. |

**Emission design (keeps near-cap files near-neutral).** Event emission is centralized in the dispatcher (`agents/callbacks.py`): the loop holds a dispatcher and swaps its inline tool loop for `await self._dispatch.run_tool(call)` (emits `ToolCallEvent`, runs `before_tool`, executes via `registry`, emits `ToolResultEvent`, runs `after_tool`) and its model call for `await self._dispatch.run_model(messages)`. Net diff at each call site is ~neutral (one call replaces the inline block). Where a file is *exactly* at cap (`base.py`, `action_loop.py`), PR 5 first lands a mechanical extraction (matching the established `action_validation.py` split) to create headroom, in a separate commit reviewed as a pure move.

## Dependency Graph

```
RFC 0044 format + runner  (v0.3.11, LANDED)          RFC 0004 AgentService (implemented)
        │                                                     │
        └───────────────────────────┬─────────────────────────┘
                                     │
   PR 1: closed ErrorKind / ToolErrorKind enums (Python-only)   ← no external deps
        │
   PR 2: agents/events.py — TurnEvent taxonomy (event_id, ToolCallEvent,
        │       ToolResultEvent, CallbackModelOutput)   [OQ #1, #3]
        │
   PR 3: agents/callbacks.py — Callback Protocol + dispatcher + CallbackResult
        │       (reserved priority band, off-hook rejection)   [OQ #4, #6]
        │
   PR 4: bounded in-process stream (drop-oldest non-terminal; lossless
        │       Control/terminal-Error; stream-level redaction transform)
        │
   PR 5: emit at real sites behind the adapter (action_loop.py primary,
        │       base.py, tools/registry.py) — no consumer migration, no behaviour change
        │
   PR 6: wrap cross-cutting concerns as fixed-priority callbacks
        │       (RFC 0023 lease, RFC 0009 sanitizer/redactor, RFC 0031 F-3 recall)
        │
   PR 7: emit the Error events for EVAL-ERROR-001/002  ──▶ hands to RFC 0044 PR 4b

   Phase 2 (separate, gated on RFC 0040): consumer migration (channel-publish /
   logger / tracer / dead-letter / eval runner as subscribers), wire encoding on
   proto/task.proto, StateDelta.scope → RFC 0042 Scope, OTEL span model. NOT in this plan.
```

## PR Sequence

### PR 1: `feature/v040-rfc0041-enums` — closed error enums

The dependency-free leaf: the two closed enums, built test-first, Python-only (no Go consumer routes on them today — [RFC 0041 §A](0041-typed-event-taxonomy-lifecycle-callbacks.md#a-event-taxonomy)).

#### Scope

| File | Change |
|------|--------|
| New `agents/events.py` (enums only) | `ErrorKind` (`wallet_denied` / `lease_cap` / `rate_limit` / `resource_exhausted` / `tool_denied` / `internal`) and `ToolErrorKind` (`denied` / `timeout` / `not_found` / `invalid_args` / `internal`) as `str, Enum`. The `denied` ↔ `tool_denied` two-layer mapping documented in the module docstring ([§D](0041-typed-event-taxonomy-lifecycle-callbacks.md#d-veto-semantics)). |
| [`ROADMAP.md`](../../ROADMAP.md) | Add the RFC 0041 Master-Index row → 🚧 Implementing (target v0.4.0); `Last updated` refresh (concise). |

#### Tests

- New `tests/unit/python/test_events_enums.py` — enum membership is exactly the closed set (guards against silent additions per Goal 5); value strings are stable (they appear in `Error.kind`); `ToolErrorKind.DENIED` value maps to `ErrorKind.TOOL_DENIED` per the documented rule.

#### PR checklist

- [ ] Test-first (red → green); `make test-python` green for the new suite.
- [ ] `make lint-python` + `file_size.py --strict` clean.
- [ ] No new runtime dependency.
- [ ] ROADMAP + RFC status hygiene.

### PR 2: `feature/v040-rfc0041-events` — the `TurnEvent` taxonomy

`agents/events.py` grows the frozen-dataclass taxonomy. Resolves OQ #1 (identity) and OQ #3 (in-callback model calls).

#### Scope

| File | Change |
|------|--------|
| `agents/events.py` (new) | `TurnEvent` base (`event_id`, `turn_id`, `seq`, `occurred_at`); `ModelOutput` (`stop_reason: StopReason` reusing [`agents/llm_types.StopReason`](../../agents/llm_types.py), **no** `"error"` literal — errors are `Error` events); `ToolCallEvent` / `ToolResultEvent` (**renamed** to avoid colliding with `llm_types.ToolCall` and `tools/registry.ToolResult`); `StateDelta` (`scope: str`, opaque in Phase 1); `Error` (`cause_event_id` references an `event_id`); `Control`; `CallbackModelOutput` (OQ #3). **Note:** the RFC sketches `event_id`/`turn_id` as ULIDs for sortability (the ordering key), but the repo currently mints ids via `uuid` and has no ULID dep — PR 2 adds a small monotonic-id helper (or documents the `uuid` choice) rather than assuming one exists. |
| [`agents/__init__.py`](../../agents/__init__.py) | Re-export the public event types. |

#### Tests

- New `tests/unit/python/test_events_taxonomy.py` — every event is frozen/hashable; `event_id` unique per construction; `(turn_id, seq)` orders within a turn; `Error.cause_event_id` accepts an `event_id`; `ToolCallEvent`/`ToolResultEvent` are importable and distinct from `llm_types.ToolCall` / `registry.ToolResult` (the collision the rename exists to prevent); `ModelOutput.stop_reason` accepts every `StopReason` member and no `"error"`.

#### PR checklist

- [ ] Test-first (red → green); new suite green.
- [ ] `make lint-python` + `file_size.py --strict` clean.
- [ ] No import cycle (`agents.events` must not import the loop).
- [ ] ROADMAP + RFC status hygiene.

### PR 3: `feature/v040-rfc0041-callbacks` — Callback Protocol + dispatcher

`agents/callbacks.py`: the callback contract, context, result type, and the ordering dispatcher. Pure — no loop wiring yet. Resolves OQ #4 (privileged band) and OQ #6 (`LegacyState`).

#### Scope

| File | Change |
|------|--------|
| `agents/callbacks.py` (new) | `CallbackContext` Protocol (`persona: PersonaState`, `channel_id: str \| None`, `state: LegacyState`, `emit`); `Callback` Protocol (four no-op-default hooks; hooks fire **once per model call**); `CallbackResult` (`veto`/`veto_reason`, `mutate_messages`/`mutate_output`/`mutate_call`/`mutate_result`, `extra_events`); `LegacyState` transitional Protocol bundling the existing per-store accessors; the dispatcher — priority ordering (`priority < 0` reserved privileged band; ties by registration order), off-hook-field rejection (`before_tool` returning `mutate_messages` → `Error(kind=internal)`), and the fixed intra-privileged order (sanitizer → wallet-lease). |
| [`agents/persona_types.py`](../../agents/persona_types.py) | (import only) — reference `PersonaState`; no change. |

#### Tests

- New `tests/unit/python/test_callbacks_dispatch.py` — priority order (lower first, registration-order tie-break); reserved-band enforcement (a `priority < 0` user callback is rejected); off-hook `CallbackResult` field → `Error(kind=internal)`, not silent no-op; `before_model` veto stops the chain; `after_*` cannot veto (a veto from `after_model` is rejected); privileged set fixed order sanitizer → wallet-lease; each hook's default no-op.

#### PR checklist

- [ ] Test-first (red → green); new suite green.
- [ ] `make lint-python` + `file_size.py --strict` clean.
- [ ] Dispatcher is loop-agnostic (unit-testable with fake callbacks, no `agents` runtime).
- [ ] ROADMAP + RFC status hygiene.

### PR 4: `feature/v040-rfc0041-stream` — bounded in-process stream

The subscriber fan-out with the backpressure carve-out and the stream-level redaction transform ([§B](0041-typed-event-taxonomy-lifecycle-callbacks.md#b-event-stream-contract)).

#### Scope

| File | Change |
|------|--------|
| `agents/events.py` or new `agents/event_stream.py` | Per-subscriber bounded queue; **drop-oldest only for non-terminal, non-`Control` events**; `Control` + terminal `Error(retryable=False)` on a lossless path (block/spill, never drop); `dropped_events` counter exposed to telemetry; the privileged stream-level redaction transform that rewrites event content **before** any subscriber sees it (covers `ToolCallEvent`/`ToolResultEvent`/`StateDelta` content the callbacks cannot mutate). Split into `event_stream.py` if `events.py` nears the cap. |

#### Tests

- New `tests/unit/python/test_event_stream.py` — a slow subscriber does not block the producer; under overflow, non-terminal events drop-oldest and bump `dropped_events`; **`Control` and terminal `Error` are never dropped** even under sustained overflow (the dead-letter/eval guarantee); redaction runs before fan-out (no subscriber observes un-redacted content); event-sequence determinism under load (leading events of a recorded sequence survive — the RFC 0044 `event_sequence` guarantee).

#### PR checklist

- [ ] Test-first (red → green); new suite green.
- [ ] `make lint-python` + `file_size.py --strict` clean.
- [ ] Backpressure test is deterministic (no wall-clock sleeps; drive via a bounded queue seam).
- [ ] ROADMAP + RFC status hygiene.

### PR 5: `feature/v040-rfc0041-emit` — emit at the real sites (behind the adapter)

Wire the loops to the dispatcher/stream so events flow, with **no consumer migration and no behaviour change**. This is the "no silent behaviour change" checkpoint.

#### Scope

| File | Change |
|------|--------|
| [`agents/persona_runtime/action_loop.py`](../../agents/persona_runtime/action_loop.py) (**primary**) | Emit `Control(turn_started/…)`, `ModelOutput`, `Error`, `StateDelta`; route the model call and tool loop through the dispatcher. **Preceded by a pure-move extraction commit** to create headroom (file is at 495/500). |
| [`agents/base.py`](../../agents/base.py) | Same for the workflow/TaskAgent `_run_llm_loop` / `_execute_tools` path. **Preceded by a pure-move extraction commit** (file is at 500/500). |
| [`agents/tools/registry.py`](../../agents/tools/registry.py) | Tool execution emits `ToolCallEvent` / `ToolResultEvent` and runs `before_tool` / `after_tool` — the centralization that keeps the near-cap loop files near-neutral. |
| [`agents/llm_client.py`](../../agents/llm_client.py) | `create_message` becomes the `before_model` / `after_model` dispatch point (it already owns the wallet-lease chokepoint), emitting `ModelOutput` / `CallbackModelOutput`. |

#### Tests

- New `tests/unit/python/test_agent_event_emission.py` — a scripted persona turn emits `turn_started` → `ModelOutput` (→ `ToolCallEvent` → `ToolResultEvent`)\* → `turn_completed` in order; an aborted turn emits exactly one terminal `Error` then `turn_aborted`; retries share `turn_id` with monotonic `seq` (OQ #1); the same for the `base.py` loop.
- **Regression (the "no silent change" bar):** the full existing `agents` unit + integration suites pass unchanged; the RFC 0044 seeds (`EVAL-MEMORY-001`, `EVAL-WORKING-001`) replay **byte-identical** (emission is additive — no prompt/text path moves).

#### PR checklist

- [ ] Extraction commits are pure moves (diff is relocation only; reviewed separately).
- [ ] Test-first (red → green); new suite green; full `make test-python` + `make test-integration` green.
- [ ] `make lint-python` + `file_size.py --strict` clean (every touched file back under 500).
- [ ] `EVAL-MEMORY-001` / `EVAL-WORKING-001` goldens verified byte-identical.
- [ ] ROADMAP + RFC status hygiene.

### PR 6: `feature/v040-rfc0041-migrate-callbacks` — wrap cross-cutting concerns as callbacks

Move the three existing inline cross-cutting paths onto the callback seam as fixed-priority (privileged/non-privileged) callbacks, still behind the adapter — no visible change.

#### Scope

| File | Change |
|------|--------|
| [`agents/llm_client.py`](../../agents/llm_client.py) ([RFC 0023](0023-llm-call-leasing.md)) | The `async with self._wallet.lease(...)` chokepoint becomes a privileged `before_model`/`after_model` callback (pre-charge / refund), keyed by the existing `walletpb.Cause`, emitting typed `Error` on denial. |
| [`agents/security.py`](../../agents/security.py) (`sanitize`) + [`agents/observability/redact.py`](../../agents/observability/redact.py) (RFC 0009) | Input sanitizer (`security.sanitize`) → privileged `before_model` callback; output redactor → `after_model` `mutate_output`. (The event-stream redaction transform of PR 4 is separate — it covers tool/state event content, not the model-facing message path.) |
| [`agents/persona_runtime/memory_context.py`](../../agents/persona_runtime/memory_context.py) + [`agents/memory/scope_recall.py`](../../agents/memory/scope_recall.py) ([RFC 0031](0031-per-session-namespacing-channels.md) F-3) | The recall filter becomes a **non-privileged** `before_model` callback that inspects retrieved memories and mutates context. (**Not** `agents/tools/recall.py` — that is the RFC 0036 `recall_channel_messages` tool.) |
| Callback registration wiring | Register the privileged set in the reserved band with the fixed order (sanitizer → wallet-lease); recall/quality-bar non-privileged. |

#### Tests

- The **existing** wallet-lease, sanitizer, and F-3 recall integration tests pass **unchanged** after migration (the semantic-equivalence bar).
- New `tests/unit/python/test_callback_migration.py` — the wallet callback fires once per model call and denies with `Error(kind=wallet_denied)`; sanitizer runs before wallet pre-charge (privileged order); the F-3 recall callback mutates context and sits at `priority >= 0`.

#### PR checklist

- [ ] Test-first (red → green); existing cross-cutting integration suites green unchanged.
- [ ] `make lint-python` + `file_size.py --strict` clean.
- [ ] No change to wallet-lease accounting, sanitizer verdicts, or recall results (diff the pre/post behaviour).
- [ ] ROADMAP + RFC status hygiene.

### PR 7: `feature/v040-rfc0041-error-goldens` — emit the `EVAL-ERROR` events; hand to RFC 0044

The terminal Phase-1 deliverable: with typed `Error` events flowing, the ISSUE-0065/0066 paths become recordable golden traces. This PR emits/verifies the events; RFC 0044 PR 4b records the `.golden.yaml` sidecars.

#### Scope

| File | Change |
|------|--------|
| Emission sites (PR 5) | Confirm the wallet-denial and `RESOURCE_EXHAUSTED` paths emit `Error(kind=wallet_denied \| lease_cap \| rate_limit \| resource_exhausted)` with the terminal `Control(turn_aborted)` in order. |
| [RFC 0044 pr-plan](0044-pr-plan.md) cross-ref | Note PR 4b is now unblocked. |
| RFC 0041 front-matter + this plan | On Phase-1 completion, `status: implementing → implemented`; check off the Phase-1 slices. |

#### Tests

- New `tests/integration/test_eval_error_events.py` — the ISSUE-0065 (wallet-denied) and ISSUE-0066 (lease-cap / rate-limit / `RESOURCE_EXHAUSTED`) turns each emit the expected `Error` event sequence, asserted with the landed RFC 0044 `event_count` / `event_sequence` matchers — the exact shape `EVAL-ERROR-001`/`002` will assert on.

#### PR checklist

- [ ] Test-first (red → green); new integration suite green.
- [ ] `make lint-python` + `file_size.py --strict` clean.
- [ ] Hand-off to RFC 0044 PR 4b confirmed (the event shape matches its `§E` seed expectations).
- [ ] ROADMAP + RFC status hygiene (RFC 0041 Phase 1 → Implemented).

## Notes

- **Phase 2 is out of this plan.** Consumer migration (channel-publish / logger / tracer / dead-letter / eval-runner as subscribers), the wire encoding on `proto/task.proto` (server-streaming RPC for live consumers vs. a repeated field for eval-replay), `StateDelta.scope` → RFC 0042 `Scope`, and the OTEL span model (OQ #5) all land in Phase 2, gated on [RFC 0040](0040-agent-orchestrator-transport-unification.md). No proto or Go change occurs in Phase 1.
- **Enum parity is a Phase-2 concern only.** `ErrorKind`/`ToolErrorKind` stay Python-only in Phase 1 (no Go consumer routes on `kind`; the typed chat-error is published Python-side by `agents/channel_publisher.py`). If a Phase-2 Go subscriber routes on `kind`, it consumes the enum through the repo's generated cross-language parity discipline (`cmd/genpatterns` → `agents/security_enums.py`, gated by `tests/unit/python/test_pattern_parity.py`) or a `proto/task.proto` enum — never a hand-copied set.
- **The cap is the schedule risk.** `base.py` (500) and `action_loop.py` (495) are the two files PR 5 must touch and both are at/against the cap. The pure-move extraction commits (matching the established `action_validation.py` split) are mandatory pre-steps, not optional cleanup; if an extraction proves awkward, prefer a new sibling module over inflating the loop file.
- **`EVAL-RECALL-001` is not in this plan.** The F-3 recall golden is an RFC 0044 deliverable blocked on a per-interaction-session recipe extension, not on RFC 0041 events (RFC 0041 asserts recall via `final_transcript`, not a dedicated event — OQ #7). `EVAL-MEMORY-001` (dementia) already landed pre-0041.
