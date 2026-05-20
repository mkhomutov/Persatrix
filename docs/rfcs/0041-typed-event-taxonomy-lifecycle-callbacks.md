---
id: RFC-0041
title: Typed Event Taxonomy and Lifecycle Callbacks
summary: Introduce a single ordered stream of typed events per agent turn (ModelOutput / ToolCall / ToolResult / StateDelta / Error / Control) and four named lifecycle callbacks (before_model / after_model / before_tool / after_tool), giving every consumer (channel publish, tracer, structured logger, eval harness, dead-letter) one auditable handle on what happened and giving cross-cutting concerns (recall filtering, wallet leases, prompt redaction, persona quality bar) one place to plug in.
type: architecture
status: draft
author: Maksim Khomutov
created: 2026-05-20
target: v0.4.0+
depends_on:
  - RFC-0004
  - RFC-0040
---

# RFC 0041 — Typed Event Taxonomy and Lifecycle Callbacks

**Type**: architecture
**Status**: 🔨 Draft
**Author**: Maksim Khomutov
**Date**: 2026-05-20
**Target**: v0.4.0+
**Depends on**: RFC 0004 (Python Agent gRPC Server — the agent loop this RFC threads typed events through), RFC 0040 (Agent–Orchestrator Transport Unification — the transport these events ride on once the agent→orchestrator path is gRPC)
**Relates to**: RFC 0023 (LLM Call Leasing — the wallet pre-charge / refund work becomes a `before_model` / `after_model` callback), RFC 0031 (Per-Session Namespacing — the F-3 recall filter becomes a `before_model` callback), RFC 0009 (Security & Sandboxing — the input sanitizer / output redactor become `before_model` / `after_model` callbacks)
**Spawned from**: [agent-runtime-vocabulary-roadmap.md §Seam 1 + §Seam 2](../agent-runtime-vocabulary-roadmap.md#seam-1--typed-events-as-the-agent-turn-primitive)

---

## Table of Contents

- [Summary](#summary)
- [Motivation](#motivation)
- [Goals](#goals)
- [Non-Goals](#non-goals)
- [Design / Implementation](#design--implementation)
  - [A. Event taxonomy](#a-event-taxonomy)
  - [B. Event stream contract](#b-event-stream-contract)
  - [C. Lifecycle callbacks](#c-lifecycle-callbacks)
  - [D. Veto semantics](#d-veto-semantics)
  - [E. Consumer migration](#e-consumer-migration)
  - [F. Backwards compatibility](#f-backwards-compatibility)
- [Security Considerations](#security-considerations)
- [Phased Implementation Plan](#phased-implementation-plan)
- [Files Touched (Estimated)](#files-touched-estimated)
- [Test Strategy](#test-strategy)
- [Open Questions](#open-questions)
- [Decision / Next Steps](#decision--next-steps)
- [Related Documentation](#related-documentation)

---

## Summary

An agent turn today emits a heterogeneous mix of side effects — model output, tool calls, tool results, state mutations, log lines, error replies, telemetry spans — and each consumer (channel publish path, structured logger, OTEL tracer, dead-letter queue, eval scenario runner) reaches into a different field of the agent loop to pick up what it needs. This RFC proposes one **ordered, typed stream of events per turn** as the single surface every consumer reads from, and four **named lifecycle callbacks** — `before_model`, `after_model`, `before_tool`, `after_tool` — as the single seam where cross-cutting concerns (wallet leases, recall filtering, redaction, persona quality-bar gating) plug in.

The two halves are co-designed because callbacks consume and emit events. Splitting them across two RFCs would force one to encode the other's data shape before it exists.

## Motivation

### M-1. The error-reply incidents

[ISSUE-0065](../issues/ISSUE-0065-chat-rest-budget-denied-no-channel-reply.md) (wallet denial), [ISSUE-0066](../issues/ISSUE-0066-chat-rest-resource-exhausted-no-channel-reply.md) (lease-cap + rate-limit + `AioRpcError(RESOURCE_EXHAUSTED)`) all solved the same shape of problem: a turn fails partway through and the system has nowhere to put the failure event except as a synthesized chat reply on the channel. The fixes ([#395](https://github.com/mkhomutov/Persatrix/pull/395), [#396](https://github.com/mkhomutov/Persatrix/pull/396), [#398](https://github.com/mkhomutov/Persatrix/pull/398)) worked, but each manually decided "what kind of error this is" at the publish site. A typed `Error` event with a closed `kind` enum (`wallet_denied`, `lease_cap`, `rate_limit`, `resource_exhausted`, `tool_denied`, `internal`) means the publish site routes on a tag instead of pattern-matching on free text, and every other consumer (trace, eval, dead-letter) sees the same tag without re-deriving it.

### M-2. Cross-cutting work has no shared seam

Four ongoing or recent threads of work all want the same hook shape:

| Work | Where it plugs in today | What it wants |
|------|------------------------|---------------|
| [RFC 0023](0023-llm-call-leasing.md) wallet lease pre-charge / refund | Wrapped around `LLMClient.create_message` at five separate origin sites (workflow, chat, autonomous TICK, sub-agent, channel-message) | One `before_model` hook that fires once per turn regardless of origin |
| [RFC 0031](0031-per-session-namespacing-channels.md) Phase 2 F-3 recall filter | Threaded into the recall path inside the persona prompt builder | One `before_model` hook that inspects retrieved memories and mutates context |
| [Persona quality bar](../memory-quality-roadmap.md#quality-bar--the-dementia-test) gating | Discussed but unimplemented; currently lives in scenario tests only | An `after_model` hook that can flag a turn as quality-bar-failing without aborting it |
| [RFC 0009](0009-security-sandboxing.md) `InputSanitizer` / output redactor | Called from the agent loop at two specific points | `before_model` / `after_model` hooks |

Each thread today threads its own hook in its own place. The aggregate is harder to read, harder to test in isolation, and harder to reorder when policy changes (e.g., does redaction happen before or after the wallet pre-charge?).

### M-3. Consumers read different fields

Today's consumers reach into the agent loop at different points:

- The channel publish path reads the final assistant message string.
- The structured logger ([RFC 0018](0018-structured-logging-framework.md)) reads tool-call structs and message contents.
- The OTEL tracer ([RFC 0019](0019-opentelemetry-completion.md)) reads timing and token counts from the LLM client and tool registry.
- The dead-letter queue reads exceptions.
- The eval scenario runner reads the final transcript and tool-call records.

Each consumer has its own coupling. A typed event stream is a single contract every consumer subscribes to, with one stable serialization for telemetry and replay.

### M-4. Eval is gated on a stable surface

[RFC 0044](0044-eval-set-golden-traces.md) (sequenced first per the [vocabulary roadmap](../agent-runtime-vocabulary-roadmap.md#recommended-sequencing)) wants to assert "this sequence of typed events in this order." It cannot do that until the events are typed and ordered. This RFC is the prerequisite that makes RFC 0044 expressible.

## Goals

1. **One ordered, typed event stream per turn.** Every side effect an agent turn produces is observable as an event of a known type, in a known order, on a single channel.
2. **One set of named lifecycle hooks.** `before_model`, `after_model`, `before_tool`, `after_tool` — uniform signature, uniform veto semantics, uniform composition order.
3. **Existing consumers migrate to event subscribers.** Channel publish, structured logger, OTEL tracer, dead-letter queue, and eval runner all read the same stream.
4. **Existing cross-cutting work migrates to callbacks.** RFC 0023 leases, RFC 0031 recall filter, RFC 0009 sanitizer/redactor all become callback implementations.
5. **The taxonomy is closed.** Adding a new event kind or callback hook is an RFC-level change, not a casual addition. This is the point of having a vocabulary.
6. **No silent behavior change.** Migration is mechanical; the visible outputs (channel messages, logs, traces, eval transcripts) are identical before and after.

## Non-Goals

- **Replacing the agent loop's overall control flow.** The model-call / tool-call / message-append rhythm in [`agents/base.py`](../../agents/base.py) is unchanged. This RFC adds a *seam* and a *vocabulary*; it does not restructure the loop.
- **Replacing OpenTelemetry.** OTEL spans continue as the telemetry export; events feed spans via an adapter, they do not replace them.
- **Cross-process event federation.** Events live in the agent worker process and are serialized only at transport boundaries (gRPC reply, log buffer, eval recorder). No event bus, no pub/sub, no cross-agent broadcasting.
- **Async callbacks across the network.** Callbacks run in-process in the agent worker. A future RFC may introduce a remote-callback shape; this RFC does not.
- **User-pluggable callbacks via config.** Callbacks register in code, not in YAML. Config-driven callback ordering invites the same opaqueness this RFC is trying to fix.

## Design / Implementation

### A. Event taxonomy

```python
# agents/events.py — sketch

@dataclass(frozen=True)
class TurnEvent:
    turn_id: str            # ULID, monotonic within an interaction
    seq: int                # 0-indexed position within the turn
    occurred_at: datetime   # UTC

@dataclass(frozen=True)
class ModelOutput(TurnEvent):
    role: Literal["assistant"]
    content: str
    stop_reason: Literal["end_turn", "tool_use", "max_tokens", "error"]
    token_usage: TokenUsage

@dataclass(frozen=True)
class ToolCall(TurnEvent):
    tool_name: str
    args: dict
    tool_call_id: str

@dataclass(frozen=True)
class ToolResult(TurnEvent):
    tool_call_id: str
    ok: bool
    content: str | dict
    error_kind: ToolErrorKind | None = None

@dataclass(frozen=True)
class StateDelta(TurnEvent):
    scope: str              # "persona" | "channel" | "session" | "interaction" | "temp"
                            # finalized in RFC 0042
    key: str
    op: Literal["set", "delete", "increment"]
    value: Any | None

@dataclass(frozen=True)
class Error(TurnEvent):
    kind: ErrorKind         # wallet_denied | lease_cap | rate_limit |
                            # resource_exhausted | tool_denied | internal
    message: str
    retryable: bool
    cause_event_id: str | None = None

@dataclass(frozen=True)
class Control(TurnEvent):
    kind: Literal["turn_started", "turn_completed", "turn_aborted"]
    reason: str | None = None
```

`StateDelta.scope` uses the prefix vocabulary RFC 0042 finalizes. This RFC ships with `scope` as an opaque string so the two can land independently; RFC 0042 narrows it to a closed set.

### B. Event stream contract

- Every turn opens with `Control(kind="turn_started")` and closes with `Control(kind="turn_completed" | "turn_aborted")`.
- Events within a turn are totally ordered by `seq`, monotonically increasing.
- A turn that aborts emits exactly one terminal `Error` event before the `turn_aborted` control event.
- The stream is consumed lazily by subscribers; one slow subscriber must not block the agent loop. Implementation uses a bounded in-process queue per subscriber with a drop-oldest policy on overflow and a `dropped_events` counter exposed to telemetry.

### C. Lifecycle callbacks

```python
# agents/callbacks.py — sketch

class CallbackContext(Protocol):
    interaction_id: str
    turn_id: str
    persona: PersonaConfig
    channel: ChannelRef | None
    state: ScopedState     # RFC 0042
    emit: Callable[[TurnEvent], None]

class Callback(Protocol):
    name: str
    priority: int          # lower runs first; ties broken by registration order

    def before_model(self, ctx: CallbackContext, messages: list[Message]) -> CallbackResult: ...
    def after_model(self, ctx: CallbackContext, output: ModelOutput) -> CallbackResult: ...
    def before_tool(self, ctx: CallbackContext, call: ToolCall) -> CallbackResult: ...
    def after_tool(self, ctx: CallbackContext, result: ToolResult) -> CallbackResult: ...

@dataclass
class CallbackResult:
    veto: bool = False
    veto_reason: str | None = None
    mutate_messages: list[Message] | None = None   # before_model only
    mutate_output: ModelOutput | None = None       # after_model only
    extra_events: list[TurnEvent] = field(default_factory=list)
```

Each method has a default no-op implementation. A callback only overrides the hooks it cares about.

`CallbackResult` mixes per-hook mutation fields (`mutate_messages` is meaningful only from `before_model`; `mutate_output` only from `after_model`). The dispatcher rejects off-hook fields with an `Error(kind="internal")` rather than silently ignoring them — i.e., a `before_tool` returning a non-`None` `mutate_messages` is a programming error, not a no-op. A future revision may split `CallbackResult` into per-hook subtypes; the current shared shape is chosen so the four hook methods have one return type, but the rejection rule preserves the per-hook contract.

### D. Veto semantics

- A `before_model` veto emits an `Error` event with `kind` chosen by the callback (e.g., `wallet_denied`) and a `Control(kind="turn_aborted")`. The model is not called.
- A `before_tool` veto emits an `Error(kind="tool_denied")` and the tool is not executed; the agent loop receives a synthetic `ToolResult(ok=False, error_kind="denied")` so the model's next round sees the refusal.
- `after_model` and `after_tool` cannot veto — the action already happened. They can emit extra events (e.g., a quality-bar flag) but cannot rewind.
- A callback raising an exception is treated as `Error(kind="internal", retryable=False)` and aborts the turn. Callback failures do not silently swallow.

### E. Consumer migration

| Consumer | Today | After |
|---------|-------|-------|
| Channel publish | Reads final assistant string from agent loop | Subscribes to `ModelOutput` (publish content) and `Error` (publish typed chat-error) |
| Structured logger ([RFC 0018](0018-structured-logging-framework.md)) | Reads tool-call structs and message contents at log-call sites | Subscribes to all events; each event has a canonical log encoder |
| OTEL tracer ([RFC 0019](0019-opentelemetry-completion.md)) | Wraps LLM client and tool registry with span context managers | Spans open/close on `Control` and `ToolCall`/`ToolResult` event pairs |
| Dead-letter queue | Catches exceptions in agent loop | Subscribes to `Error(retryable=False)` |
| Eval scenario runner | Reads transcripts and tool-call records | Subscribes to the full stream; goldens are sequences of events ([RFC 0044](0044-eval-set-golden-traces.md)) |

### F. Backwards compatibility

- The agent loop's public Python API (`BaseAgent.run`, `BaseAgent.execute_task`) is unchanged. Events are an *additional* output channel, not a replacement.
- Existing logging keys and OTEL span names are preserved by the adapter layer for the migration window. Removal of the legacy log-call sites is a follow-up after consumers are migrated.
- Existing wallet-lease, recall-filter, and sanitizer code paths are wrapped as callbacks in Phase 1 with no semantic change; their inline call sites are removed in Phase 2.
- `StateDelta.scope` ships as `str` in Phase 1 (see [§A](#a-event-taxonomy)). Re-typing it to the closed `Scope` enum from [RFC 0042](0042-state-namespacing-by-scope.md) §D is an explicit follow-up that lands once RFC 0042 Phase 1 ships; until then subscribers must accept any string value. This is tracked in [Open Q #2](#open-questions) and is a Phase-1 → Phase-2 deliverable, not a "maybe."

## Security Considerations

- **Event content may carry sensitive data.** Tool arguments, model outputs, and state deltas can contain user PII or credentials. Events flow through the existing redactor ([RFC 0009](0009-security-sandboxing.md)) before reaching any persistence or transport boundary. The redactor itself is a callback in the new scheme, so this RFC must enforce that the redactor runs *before* any subscriber sees the event — i.e., the redactor is a privileged subscriber that runs first and its output is what other subscribers see.
- **Callback ordering is security-relevant.** The wallet lease check, the input sanitizer, and the F-3 recall filter must run in a deterministic order with no override. The `priority` field is data, not config; the privileged set of callbacks (auth/wallet/redactor) is fixed in code with priority ranges that user callbacks cannot occupy.
- **Veto cannot be silenced.** A vetoed turn always emits an `Error` event. There is no callback API to mark a veto as "soft" or to suppress the resulting event — that closes a class of "looks like it ran but didn't" bugs.
- **Event-stream backpressure.** A subscriber that blocks must not block the agent loop. Drop-oldest with a counter is documented behavior; silent drops are surfaced as a metric.

## Phased Implementation Plan

### Phase 1 — vocabulary + adapter

Ship the event types, the callback Protocol, the in-process stream, and an adapter layer that translates legacy log/trace/publish calls into events behind the scenes. No semantic change visible to users. Existing wallet-lease, sanitizer, and recall-filter code paths are wrapped as `Callback` instances and registered with fixed priorities. Goldens recorded for the dementia test, the F-3 recall scenario, and the ISSUE-0065/0066 error paths.

### Phase 2 — consumer migration

Channel publish, structured logger, OTEL tracer, dead-letter queue, and eval runner are rewritten as event subscribers. Adapter layer removed. Legacy log-call sites deleted.

### Phase 3 — open the seam

Document the callback API for in-repo extension (e.g., a future moderator callback for [RFC 0030](0030-multi-agent-conversation-governance.md) Phase 2). Out-of-tree callbacks remain explicitly unsupported.

## Files Touched (Estimated)

| Component | Files | Change |
|-----------|-------|--------|
| Python agents | `agents/events.py` (new), `agents/callbacks.py` (new), `agents/base.py`, `agents/persona_runtime.py`, `agents/llm_client.py` | Event emission, callback dispatch, agent-loop integration |
| Python agents | `agents/tools/registry.py`, `agents/tools/builtin.py` | Tool execution emits `ToolCall` / `ToolResult` |
| Python agents | `agents/wallet_client.py` ([RFC 0023](0023-llm-call-leasing.md)), `agents/recall.py` ([RFC 0031](0031-per-session-namespacing-channels.md)) | Migrate to `Callback` implementations |
| Go orchestrator | `internal/observability/`, `internal/server/channel*.go` | Channel publish subscribes to event stream over the existing gRPC reply path |
| Protos | `proto/agent.proto` | Optional event stream field on the task-reply message; back-compat preserved |
| Config | (none) | Callbacks register in code, not config |

## Test Strategy

- **Unit tests**: event ordering invariants; callback veto semantics; priority resolution; drop-oldest behavior under backpressure; redactor-runs-first invariant.
- **Integration tests**: the existing wallet-lease, sanitizer, and recall-filter integration tests pass unchanged after migration to callbacks (this is the "no silent behavior change" check).
- **E2E / smoke tests**: full chat-channel turn with all current cross-cutting concerns active; verify the channel-published transcript and the OTEL trace are byte-identical (modulo timestamps) to pre-RFC.
- **Manual tests**: re-run [MT-MEMORY-005](../manual-tests/MT-MEMORY-005-dementia-test.md) and verify the persona quality-bar gate fires as an `after_model` callback emitting a flagging event.
- **Goldens**: record event-stream goldens for ISSUE-0065/0066 paths; [RFC 0044](0044-eval-set-golden-traces.md) consumes these.

## Open Questions

1. **Event identity across retries.** When the agent loop retries an LLM call after a transient failure, do the retried `before_model` calls share a `turn_id` or get distinct ones? Leaning toward shared `turn_id` with monotonic `seq` so retries are visible in the stream.
2. **State-delta scope wording.** This RFC uses `scope: str` so RFC 0042 can finalize the closed set. If RFC 0042 lands first, this becomes a typed enum here.
3. **Token-usage attribution under veto.** If `before_model` vetoes, the model was not called and there is no token usage — but if a callback itself called the model (e.g., a moderation LLM), where is that usage recorded? Lean: callbacks that call models emit their own `ModelOutput` events with `role="assistant"` is wrong; we need a separate `role="callback"` shape or a dedicated `CallbackModelOutput` event. Resolve before Phase 1.
4. **Privileged callback set.** Which callbacks are "privileged" (fixed priority, cannot be reordered)? Initial set: redactor, wallet-lease, input-sanitizer. Persona quality-bar gate and F-3 recall filter are *not* privileged — they are domain logic.
5. **OTEL span model.** Spans today wrap function calls. After migration, do spans wrap *event-pair intervals* (e.g., `ToolCall` → `ToolResult`) or wrap callback executions? Probably both, with the agent loop emitting top-level "turn" spans and event-pair intervals nested under them.

## Decision / Next Steps

Draft. Open questions above must be resolved before status advances to Proposed. Phase 1 cannot begin until [RFC 0044](0044-eval-set-golden-traces.md) defines the golden-trace format this RFC's tests depend on.

## Related Documentation

- [Agent Runtime Vocabulary — Discussion Notes](../agent-runtime-vocabulary-roadmap.md) — the umbrella memo this RFC implements
- [RFC 0042 — State Namespacing by Scope Prefix](0042-state-namespacing-by-scope.md) — finalizes `StateDelta.scope`
- [RFC 0043 — Inbound Agent-Interop Endpoint](0043-inbound-agent-interop-endpoint.md) — independent, no shared surface
- [RFC 0044 — Eval-Set Shape with Golden Traces](0044-eval-set-golden-traces.md) — consumes the event stream as golden assertions
- [RFC 0023 — LLM Call Leasing](0023-llm-call-leasing.md)
- [RFC 0031 — Per-Session Namespacing for Channels and Persona Memory](0031-per-session-namespacing-channels.md)
- [RFC 0009 — Agent Identity, Security & Sandboxing](0009-security-sandboxing.md)
- [RFC 0018 — Structured Logging Framework](0018-structured-logging-framework.md)
- [RFC 0019 — OpenTelemetry Completion](0019-opentelemetry-completion.md)
- [ISSUE-0065](../issues/ISSUE-0065-chat-rest-budget-denied-no-channel-reply.md) / [ISSUE-0066](../issues/ISSUE-0066-chat-rest-resource-exhausted-no-channel-reply.md)
