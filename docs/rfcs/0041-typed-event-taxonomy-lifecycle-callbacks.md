---
id: RFC-0041
title: Typed Event Taxonomy and Lifecycle Callbacks
summary: Introduce a single ordered stream of typed events per agent turn (ModelOutput / ToolCallEvent / ToolResultEvent / StateDelta / Error / Control) and four named lifecycle callbacks (before_model / after_model / before_tool / after_tool), giving every consumer (channel publish, tracer, structured logger, eval harness, dead-letter) one auditable handle on what happened and giving cross-cutting concerns (recall filtering, wallet leases, prompt redaction, persona quality bar) one place to plug in.
type: architecture
status: proposed
author: Maksim Khomutov
created: 2026-05-20
target: v0.4.0+
depends_on:
  - RFC-0004
  - RFC-0044
relates_to:
  - RFC-0023
  - RFC-0031
  - RFC-0009
  - RFC-0042
  - RFC-0040
---

# RFC 0041 — Typed Event Taxonomy and Lifecycle Callbacks

**Type**: architecture
**Status**: 📋 Proposed
**Author**: Maksim Khomutov
**Date**: 2026-05-20
**Target**: v0.4.0+
**Depends on**: RFC 0004 (Python Agent gRPC Server — the agent loop this RFC threads typed events through), RFC 0044 (Eval-Set Shape with Golden Traces — its golden-trace format is the Phase-1 test surface this RFC records against; the format shipped in v0.3.11, so this precondition is already met)
**Relates to**: RFC 0023 (LLM Call Leasing — the wallet pre-charge / refund work becomes a `before_model` / `after_model` callback), RFC 0031 (Per-Session Namespacing — the F-3 recall filter becomes a `before_model` callback), RFC 0009 (Security & Sandboxing — the input sanitizer / output redactor become `before_model` / `after_model` callbacks), RFC 0042 (State Namespacing by Scope — finalizes `StateDelta.scope` and the `ScopedState` type, both shipped here as forward-compatible transitional forms), RFC 0040 (Agent–Orchestrator Transport Unification — **Phase-2-only, soft**: the Go-orchestrator consumer migration rides RFC 0040's `OrchestratorService`. Phase 1 is entirely in-process Python with no transport surface, so it does **not** depend on RFC 0040; the `AgentService` reply path these events can also ride is already gRPC via RFC 0004 and RFC 0040 leaves it unchanged)
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
| [RFC 0023](0023-llm-call-leasing.md) wallet lease pre-charge / refund | Already centralized: the `async with self._wallet.lease(...)` chokepoint lives inside [`LLMClient.create_message`](../../agents/llm_client.py) (one site), discriminated by a `walletpb.Cause` enum whose five values (workflow-task, chat, autonomous TICK, sub-agent, channel-message) are set by the caller. Each of the two agent loops (`base.py`, `persona_runtime/action_loop.py`) derives and threads the `Cause` itself | One `before_model` hook (firing once **per model call**) that owns cause-derivation uniformly across both loops and makes the pre-charge / refund observable as typed `Error` / `StateDelta` events instead of an internal `async with` |
| [RFC 0031](0031-per-session-namespacing-channels.md) Phase 2 F-3 recall filter | Threaded into the recall path inside the persona prompt builder | One `before_model` hook that inspects retrieved memories and mutates context |
| [Persona quality bar](../memory-quality-roadmap.md#quality-bar--the-dementia-test) gating | Discussed but unimplemented; currently lives in scenario tests only | An `after_model` hook that can flag a turn as quality-bar-failing without aborting it |
| [RFC 0009](0009-security-sandboxing.md) `InputSanitizer` / output redactor | Called from the agent loop at two specific points | `before_model` / `after_model` hooks |

Each thread today threads its own hook in its own place. The aggregate is harder to read, harder to test in isolation, and harder to reorder when policy changes (e.g., does redaction happen before or after the wallet pre-charge?). A shared seam makes that ordering an explicit, testable decision — [§Security](#security-considerations) pins it: sanitize/redact before the wallet pre-charge.

### M-3. Consumers read different fields

Today's consumers reach into the agent loop at different points:

- The channel publish path reads the final assistant message string.
- The structured logger ([RFC 0018](0018-structured-logging-framework.md)) reads tool-call structs and message contents.
- The OTEL tracer ([RFC 0019](0019-opentelemetry-completion.md)) reads timing and token counts from the LLM client and tool registry.
- The dead-letter queue reads exceptions.
- The eval scenario runner reads the final transcript and tool-call records.

Each consumer has its own coupling. A typed event stream is a single contract every consumer subscribes to, with one stable serialization for telemetry and replay.

### M-4. Eval is gated on a stable surface

[RFC 0044](0044-eval-set-golden-traces.md) (sequenced first per the [vocabulary roadmap](../agent-runtime-vocabulary-roadmap.md#recommended-sequencing)) wants to assert "this sequence of typed events in this order." The relationship with RFC 0044 is a **staged handoff, not a cycle** — the two halves land in order:

1. **RFC 0044's format spec** (the eval-set file shape, assertion vocabulary, and record/replay runner — `schemas/eval_set.schema.json`, `evaluators/assertions.py`, `evaluators/runner.py`) blocks *this* RFC's Phase-1 tests. It **already shipped in v0.3.11** (RFC 0044 PRs 1–4a), so this precondition is met.
2. **This RFC's typed events** (Phase 1) block RFC 0044's *typed-event* goldens — the `event_sequence` / `event_count` assertions for `EVAL-ERROR-001` / `002` (RFC 0044 PR 4b, explicitly gated on this RFC). Until the events are typed and ordered, those goldens cannot be recorded.

This RFC **owns the event type-name vocabulary** (`ModelOutput`, `ToolCallEvent`, …); RFC 0044 *consumes* those names in its assertion YAML. The two are co-designed but sequenced; neither blocks the other's already-shipped surface.

## Goals

1. **One ordered, typed event stream per turn.** Every side effect an agent turn produces is observable as an event of a known type, in a known order, on a single channel. The taxonomy has **six primitive event types** (`ModelOutput`, `ToolCallEvent`, `ToolResultEvent`, `StateDelta`, `Error`, `Control`), plus `CallbackModelOutput` for in-callback model usage. Log lines and telemetry spans are **not** primitive events — they are *derived outputs* produced by the structured-logger and OTEL-tracer subscribers from the primitive stream (see [§E](#e-consumer-migration)).
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
    event_id: str           # ULID, unique per event within the worker process
    turn_id: str            # ULID, monotonic within an interaction
    seq: int                # 0-indexed position within the turn
    occurred_at: datetime   # UTC

@dataclass(frozen=True)
class ModelOutput(TurnEvent):
    role: Literal["assistant"]
    content: str
    stop_reason: StopReason  # the existing agents/llm_types.StopReason enum
                             # (END_TURN | TOOL_USE | MAX_TOKENS); error
                             # conditions are a separate Error event, not a
                             # stop_reason value
    token_usage: TokenUsage

@dataclass(frozen=True)
class ToolCallEvent(TurnEvent):   # renamed from ToolCall to avoid colliding
    tool_name: str                # with agents/llm_types.ToolCall
    args: dict
    tool_call_id: str

@dataclass(frozen=True)
class ToolResultEvent(TurnEvent):  # renamed from ToolResult to avoid colliding
    tool_call_id: str              # with agents/tools/registry.ToolResult
    ok: bool
    content: str | dict
    error_kind: ToolErrorKind | None = None

@dataclass(frozen=True)
class StateDelta(TurnEvent):
    scope: str              # opaque str in Phase 1. RFC 0042 §A defines the
                            # closed set: app | persona | channel | session |
                            # interaction | temp (re-typed to Scope in Phase 2)
    key: str
    op: Literal["set", "delete", "increment"]
    value: Any | None

@dataclass(frozen=True)
class Error(TurnEvent):
    kind: ErrorKind         # wallet_denied | lease_cap | rate_limit |
                            # resource_exhausted | tool_denied | internal
    message: str
    retryable: bool
    cause_event_id: str | None = None   # references another event's event_id

@dataclass(frozen=True)
class Control(TurnEvent):
    kind: Literal["turn_started", "turn_completed", "turn_aborted"]
    reason: str | None = None

@dataclass(frozen=True)
class CallbackModelOutput(TurnEvent):   # a callback's own model call (e.g. a
    callback_name: str                  # moderation LLM). Distinct from
    content: str                        # ModelOutput so channel-publish never
    token_usage: TokenUsage             # mistakes it for the assistant's turn
                                        # output. (Resolves Open Q #3.)


class ErrorKind(str, Enum):
    WALLET_DENIED = "wallet_denied"
    LEASE_CAP = "lease_cap"
    RATE_LIMIT = "rate_limit"
    RESOURCE_EXHAUSTED = "resource_exhausted"
    TOOL_DENIED = "tool_denied"
    INTERNAL = "internal"


class ToolErrorKind(str, Enum):
    DENIED = "denied"          # a before_tool veto (maps to Error.kind=tool_denied)
    TIMEOUT = "timeout"
    NOT_FOUND = "not_found"    # unknown tool name
    INVALID_ARGS = "invalid_args"
    INTERNAL = "internal"
```

**Event identity.** Every event carries an `event_id` (ULID, unique within the worker process). `(turn_id, seq)` is the ordering key within a turn; `event_id` is the *reference* key — `Error.cause_event_id` and the redaction transform ([§B](#b-event-stream-contract)) address events by `event_id`, not by position.

**Name collisions (implementers must observe).** The event dataclasses `ToolCallEvent` / `ToolResultEvent` are deliberately *not* named `ToolCall` / `ToolResult`: the agent runtime already defines [`agents/llm_types.ToolCall`](../../agents/llm_types.py) (imported into `agents/base.py`) and [`agents/tools/registry.ToolResult`](../../agents/tools/registry.py) — the very module that emits the `ToolResultEvent`. Do **not** `from .events import ToolCall`.

**Closed enums.** `ErrorKind` and `ToolErrorKind` are closed (Goal 5). The tool-denial path spells the same denial at two layers: the tool-result layer emits `ToolResultEvent(error_kind=ToolErrorKind.DENIED)` and the turn layer emits `Error(kind=ErrorKind.TOOL_DENIED)` (see [§D](#d-veto-semantics)). These enums are **Python-only in Phase 1** — no Go consumer routes on them today (the typed chat-error for [ISSUE-0065](../issues/ISSUE-0065-chat-rest-budget-denied-no-channel-reply.md)/[0066](../issues/ISSUE-0066-chat-rest-resource-exhausted-no-channel-reply.md) is published Python-side by [`agents/channel_publisher.py`](../../agents/channel_publisher.py)). If a Go subscriber later routes on `kind` (Phase 2), the enum must cross the wire with the same generated-parity discipline the repo already uses for cross-language closed sets ([`agents/security_enums.py`](../../agents/security_enums.py), byte-parity gate `tests/unit/python/test_pattern_parity.py`), or become a proto enum on `proto/task.proto` (cf. `proto/wallet.proto` `enum LeaseDeniedReason`). See [§Security](#security-considerations) and [Open Q #4](#open-questions).

`StateDelta.scope` ships as an opaque `str` in Phase 1 so this RFC and RFC 0042 land independently; RFC 0042 §A narrows it to the closed `Scope` set (`app | persona | channel | session | interaction | temp`) in the Phase-2 sweep. Note `StateDelta` records state *writes*, not recall *reads*: the F-3 recall filter mutates the prompt via a `before_model` callback and is asserted through `final_transcript`, not through a dedicated recall event (a `ContextInjection` event is deferred — see [Open Q #7](#open-questions)).

### B. Event stream contract

- Every turn opens with `Control(kind="turn_started")` and closes with `Control(kind="turn_completed" | "turn_aborted")`.
- Events within a turn are totally ordered by `seq`, monotonically increasing. `(turn_id, seq)` is the ordering key; `event_id` is the identity/reference key ([§A](#a-event-taxonomy)).
- A turn that aborts emits exactly one terminal `Error` event before the `turn_aborted` control event.
- **Redaction runs before fan-out.** A single privileged stream-level redaction transform rewrites event content (tool args, model output, `StateDelta` values) *before* any subscriber's queue sees the event. It is a property of the stream, not a callback or a subscriber — so it covers `ToolCallEvent` / `ToolResultEvent` / `StateDelta` content that callbacks cannot mutate, and no subscriber ever observes un-redacted content. (This is distinct from the RFC 0009 input-sanitizer / output-redactor *callbacks* in [§C](#c-lifecycle-callbacks), which mutate the model-facing message path.)
- The stream is consumed lazily by subscribers; one slow subscriber must not block the agent loop. Implementation uses a bounded in-process queue per subscriber with a drop-oldest policy on overflow and a `dropped_events` counter exposed to telemetry.
- **Terminal events are undroppable.** Drop-oldest applies **only to non-terminal, non-`Control` events**. Every `Control` event and any terminal `Error` (`retryable=False`) is delivered losslessly — a full queue blocks or spills to a lossless slot rather than dropping them. The dead-letter subscriber and the eval-runner subscriber are on the lossless path. Rationale: drop-oldest discards the *leading* events of a sequence, which would (a) silently lose the terminal `Error` the dead-letter queue exists to capture and (b) make RFC 0044 `event_sequence` goldens non-deterministic — contradicting Goal 6 ("no silent behavior change"). Silent drops of non-terminal events remain surfaced via the `dropped_events` metric.

### C. Lifecycle callbacks

```python
# agents/callbacks.py — sketch

class CallbackContext(Protocol):
    interaction_id: str
    turn_id: str
    persona: PersonaState              # agents/persona_types.PersonaState
    channel_id: str | None             # the wire channel id, or None off-channel
    state: LegacyState                 # transitional; re-typed to ScopedState
                                       # (RFC 0042) in the Phase-2 sweep (Open Q #6)
    emit: Callable[[TurnEvent], None]

class Callback(Protocol):
    name: str
    priority: int          # lower runs first; ties broken by registration order.
                           # priority < 0 is the RESERVED privileged band that
                           # user/domain callbacks cannot occupy (see §Security).

    # before_model / after_model fire once PER MODEL CALL (per create_message),
    # not once per turn — a turn issues up to max_llm_calls model calls.
    def before_model(self, ctx: CallbackContext, messages: list[dict[str, Any]]) -> CallbackResult: ...
    def after_model(self, ctx: CallbackContext, output: ModelOutput) -> CallbackResult: ...
    def before_tool(self, ctx: CallbackContext, call: ToolCallEvent) -> CallbackResult: ...
    def after_tool(self, ctx: CallbackContext, result: ToolResultEvent) -> CallbackResult: ...

@dataclass
class CallbackResult:
    veto: bool = False
    veto_reason: str | None = None
    mutate_messages: list[dict[str, Any]] | None = None  # before_model only
    mutate_output: ModelOutput | None = None             # after_model only
    mutate_call: ToolCallEvent | None = None             # before_tool only
    mutate_result: ToolResultEvent | None = None         # after_tool only
    extra_events: list[TurnEvent] = field(default_factory=list)
```

Each method has a default no-op implementation. A callback only overrides the hooks it cares about.

`CallbackResult` mixes per-hook mutation fields — each is meaningful from exactly one hook: `mutate_messages` (`before_model`), `mutate_output` (`after_model`), `mutate_call` (`before_tool`), `mutate_result` (`after_tool`). The `mutate_call` / `mutate_result` fields let a callback sanitize tool arguments before execution and redact a tool result before it re-enters the model context — the tool-boundary redaction the model-path redactor callback cannot reach. The dispatcher rejects off-hook fields with an `Error(kind="internal")` rather than silently ignoring them — i.e., a `before_tool` returning a non-`None` `mutate_messages` is a programming error, not a no-op. A future revision may split `CallbackResult` into per-hook subtypes; the current shared shape is chosen so the four hook methods have one return type, but the rejection rule preserves the per-hook contract.

### D. Veto semantics

- A `before_model` veto emits an `Error` event with `kind` chosen by the callback (e.g., `ErrorKind.WALLET_DENIED`) and a `Control(kind="turn_aborted")`. The model is not called.
- A `before_tool` veto emits an `Error(kind=ErrorKind.TOOL_DENIED)` and the tool is not executed; the agent loop receives a synthetic `ToolResultEvent(ok=False, error_kind=ToolErrorKind.DENIED)` so the model's next round sees the refusal. The two spellings are the same denial at two layers — the tool-result layer (`ToolErrorKind.DENIED`) and the turn layer (`ErrorKind.TOOL_DENIED`); this mapping is intentional, not a duplication.
- `after_model` and `after_tool` **cannot veto** — the action already happened. They *may still mutate* the just-produced artifact (`after_model` → `mutate_output`, e.g. output redaction; `after_tool` → `mutate_result`, e.g. result redaction) and emit extra events (e.g., a quality-bar flag), but they cannot rewind the action or abort the turn. Mutation edits what downstream sees; veto prevents the action — only the `before_*` hooks can do the latter.
- A callback raising an exception is treated as `Error(kind=ErrorKind.INTERNAL, retryable=False)` and aborts the turn. Callback failures do not silently swallow.

### E. Consumer migration

| Consumer | Today | After |
|---------|-------|-------|
| Channel publish | Publishes the typed chat-error / assistant reply **Python-side** ([`agents/channel_publisher.py`](../../agents/channel_publisher.py), [`agents/chat_reply.py`](../../agents/chat_reply.py)) | Subscribes to `ModelOutput` (publish content) and `Error` (publish typed chat-error). Ignores `CallbackModelOutput`. **Process note**: in Phase 1/2 the subscriber is the Python channel-publisher; a *Go-orchestrator* subscriber over a proactive agent→orchestrator stream is gated on RFC 0040's `OrchestratorService` (Phase 2) — see [§Transport](#transport-encoding-phase-2) |
| Structured logger ([RFC 0018](0018-structured-logging-framework.md)) | Reads tool-call structs and message contents at log-call sites | Subscribes to all events; each event has a canonical log encoder |
| OTEL tracer ([RFC 0019](0019-opentelemetry-completion.md)) | Wraps LLM client and tool registry with span context managers | Spans open/close on `Control` and `ToolCallEvent`/`ToolResultEvent` pairs. (The model-call span needs a start boundary `ModelOutput` alone does not provide — the span model is [Open Q #5](#open-questions), Phase 2) |
| Dead-letter queue | Catches exceptions in agent loop | Subscribes to `Error(retryable=False)` on the lossless path ([§B](#b-event-stream-contract)) |
| Eval scenario runner | Reads transcripts and tool-call records | Subscribes to the full stream on the lossless path; goldens are sequences of events ([RFC 0044](0044-eval-set-golden-traces.md)) |

#### Transport encoding (Phase 2)

Events live in the agent worker process and are serialized only at transport boundaries ([Non-Goals](#non-goals) permits this). The wire encoding is a **Phase-2** decision with two distinct shapes for two distinct consumers, not a single field:

- **Live / back-pressure consumers** (a Go-orchestrator subscriber): a genuine **server-streaming RPC** that yields `TurnEvent`s as they occur. A single repeated field on a unary reply cannot satisfy §B's lazy/back-pressure contract — it forces whole-turn buffering — so live consumers must not ride a unary field.
- **Eval / replay** (record-once, read-after): a **repeated `TurnEvent` field on the unary reply** (`TaskResponse` / `ChatResponse` under `service AgentService`, [`proto/task.proto`](../../proto/task.proto)) is acceptable, since the whole turn is already complete when recorded.

The proactive Go-orchestrator stream rides RFC 0040's `OrchestratorService` (agent→orchestrator direction); `AgentService` (`proto/task.proto`) is already gRPC via RFC 0004 and RFC 0040 leaves it unchanged.

### F. Backwards compatibility

- The agent loop's public Python entrypoint ([`BaseAgent.handle`](../../agents/base.py)) is unchanged. Events are an *additional* output channel, not a replacement. (The internal loops — `BaseAgent._run_llm_loop` for the workflow/TaskAgent path and `persona_runtime/action_loop.py` for the persona path — gain event emission but keep their public shape.)
- Existing logging keys and OTEL span names are preserved by the adapter layer for the migration window. Removal of the legacy log-call sites is a follow-up after consumers are migrated.
- Existing wallet-lease, recall-filter, and sanitizer code paths are wrapped as callbacks in Phase 1 with no semantic change; their inline call sites are removed in Phase 2.
- `CallbackContext.state` ships as the transitional `LegacyState` Protocol in Phase 1 (bundling the existing per-store accessors), re-typed to `ScopedState` in the Phase-2 sweep. Subscribers and callbacks must not depend on the `ScopedState` shape until then ([Open Q #6](#open-questions)).
- `StateDelta.scope` ships as `str` in Phase 1 (see [§A](#a-event-taxonomy)). Re-typing it to the closed `Scope` enum from [RFC 0042](0042-state-namespacing-by-scope.md) (vocabulary in §A, `Scope` StrEnum in §D) is an explicit follow-up that lands once RFC 0042 Phase 1 ships; until then subscribers must accept any string value. This is tracked in [Open Q #2](#open-questions) and is a Phase-1 → Phase-2 deliverable, not a "maybe."

## Security Considerations

- **Event content may carry sensitive data.** Tool arguments, model outputs, and state deltas can contain user PII or credentials. Event content is redacted by the **stream-level redaction transform** ([§B](#b-event-stream-contract)) *before* any subscriber's queue sees the event — it is a property of the stream (covering `ToolCallEvent` / `ToolResultEvent` / `StateDelta` content), **not** a callback and **not** an ordinary subscriber. This is distinct from RFC 0009's input-sanitizer / output-redactor *callbacks* (below), which act on the model-facing message path. Both must run before their respective downstream sees content; neither can be reordered by domain callbacks.
- **Callback ordering is security-relevant.** The `priority` field is data, not config. Priorities `< 0` are a **reserved privileged band** that user/domain callbacks (which use `priority >= 0`) cannot occupy. The privileged model-path callback set is fixed in code as **`{input-sanitizer, wallet-lease}`** with a pinned intra-band order: **input-sanitizer → wallet-lease** (the wallet pre-charge estimates tokens from the sanitized message set, so it must run after sanitization — this answers the "redaction/sanitize before or after wallet pre-charge?" question: before). The event-stream redactor is the §B transform, ordered ahead of all callbacks by construction. The F-3 recall filter and the persona quality-bar gate are **domain logic, not privileged** (`priority >= 0`).
- **Closed-enum parity across the wire.** `ErrorKind` / `ToolErrorKind` are Python-only in Phase 1 (no Go consumer routes on them — [§A](#a-event-taxonomy)). Any Phase-2 Go consumer that routes on `kind` must consume the enum through the repo's generated cross-language parity discipline (`cmd/genpatterns` → `agents/security_enums.py`, gated by `tests/unit/python/test_pattern_parity.py`) or a `proto/task.proto` enum — never a hand-copied string set.
- **Veto cannot be silenced.** A vetoed turn always emits an `Error` event. There is no callback API to mark a veto as "soft" or to suppress the resulting event — that closes a class of "looks like it ran but didn't" bugs.
- **Event-stream backpressure.** A subscriber that blocks must not block the agent loop. Drop-oldest with a counter is documented behavior for non-terminal, non-`Control` events; `Control` and terminal `Error` events are undroppable ([§B](#b-event-stream-contract)); silent drops are surfaced as a metric.

## Phased Implementation Plan

### Phase 1 — vocabulary + adapter

Ship the event types, the callback Protocol, the in-process stream, and an adapter layer that translates legacy log/trace/publish calls into events behind the scenes. No semantic change visible to users. Existing wallet-lease, sanitizer, and recall-filter code paths are wrapped as `Callback` instances and registered with fixed priorities (privileged band). Phase 1 is **entirely in-process Python — no transport or Go surface** (that is Phase 2).

Phase 1's eval deliverable is scoped precisely: emit the typed `Error` events that let RFC 0044 record the **`EVAL-ERROR-001` / `EVAL-ERROR-002`** goldens (the ISSUE-0065/0066 paths — RFC 0044 PR 4b, explicitly gated on this RFC). It consumes RFC 0044's already-landed format + runner (`schemas/eval_set.schema.json`, `evaluators/assertions.py`, `evaluators/runner.py`, `evaluators/persona_driver.py`, `evaluators/replay_llm_client.py`). The dementia golden (`EVAL-MEMORY-001`) already landed pre-0041 (RFC 0044 PR 4a, transcript-only) and is RFC-0044-owned; the F-3 recall golden (`EVAL-RECALL-001`) is a separate RFC 0044 deliverable and is **not** recorded here.

### Phase 2 — consumer migration

Channel publish, structured logger, OTEL tracer, dead-letter queue, and eval runner are rewritten as event subscribers. Adapter layer removed. Legacy log-call sites deleted.

### Phase 3 — open the seam

Document the callback API for in-repo extension (e.g., a future moderator callback for [RFC 0030](0030-multi-agent-conversation-governance.md) Phase 2). Out-of-tree callbacks remain explicitly unsupported.

## Files Touched (Estimated)

| Component | Files | Change |
|-----------|-------|--------|
| Python agents (new) | `agents/events.py`, `agents/callbacks.py` | Event taxonomy (§A), closed enums, callback Protocol + dispatcher, bounded in-process stream |
| Python agents (emission sites) | `agents/persona_runtime/action_loop.py` (**primary** — the persona loop RFC 0044 goldens drive; `create_message` at ~L386), `agents/base.py` (`_run_llm_loop`, the workflow/TaskAgent path), `agents/persona_runtime/prompt_assembly.py`, `agents/llm_client.py` (`create_message` — the wallet-lease chokepoint) | Emit `Control` / `ModelOutput` / `Error` / `StateDelta` events; dispatch `before_model` / `after_model` |
| Python agents | `agents/tools/registry.py`, `agents/tools/builtin.py` | Tool execution emits `ToolCallEvent` / `ToolResultEvent`; dispatch `before_tool` / `after_tool` (note: `registry.ToolResult` already exists — the event is `ToolResultEvent`) |
| Python agents (→ callbacks) | `agents/llm_client.py` ([RFC 0023](0023-llm-call-leasing.md) wallet lease), `agents/persona_runtime/memory_context.py` + `agents/memory/scope_recall.py` ([RFC 0031](0031-per-session-namespacing-channels.md) F-3 recall filter), RFC 0009 sanitizer/redactor | Migrate to privileged `Callback` implementations (**not** `agents/tools/recall.py`, which is the RFC 0036 `recall_channel_messages` tool) |
| Go orchestrator (**Phase 2**, gated on RFC 0040) | `internal/observability/`, `internal/server/channel*.go` | A Go-orchestrator subscriber over the RFC 0040 `OrchestratorService` stream. Phase 1/2 channel publish stays Python-side (`agents/channel_publisher.py`) |
| Protos (**Phase 2**) | `proto/task.proto` (`service AgentService`; replies `TaskResponse` / `ChatResponse`) | Server-streaming `TurnEvent` RPC for live consumers; repeated `TurnEvent` field on the unary reply for eval/replay only. **There is no `proto/agent.proto`.** |
| Config | (none) | Callbacks register in code, not config |

## Test Strategy

- **Unit tests**: event ordering invariants (`(turn_id, seq)` monotonic; terminal `Error` before `turn_aborted`); event identity; callback veto semantics; priority resolution and reserved-band enforcement; off-hook `CallbackResult` field rejection; drop-oldest for non-terminal events **and** lossless delivery of `Control` / terminal `Error`; stream-level redaction-before-fan-out invariant.
- **Integration tests**: the existing wallet-lease, sanitizer, and recall-filter integration tests pass unchanged after migration to callbacks (this is the "no silent behavior change" check).
- **E2E / smoke tests**: full chat-channel turn with all current cross-cutting concerns active; verify the channel-published transcript and the OTEL trace are byte-identical (modulo timestamps) to pre-RFC.
- **Manual tests**: re-run [MT-MEMORY-005](../manual-tests/MT-MEMORY-005-dementia-test.md) and verify the persona quality-bar gate fires as an `after_model` callback emitting a flagging event.
- **Goldens**: emit the typed `Error` events that let [RFC 0044](0044-eval-set-golden-traces.md) record the `EVAL-ERROR-001` / `EVAL-ERROR-002` event-stream goldens (ISSUE-0065/0066 paths, RFC 0044 PR 4b), consuming the already-landed RFC 0044 format + runner (`schemas/eval_set.schema.json`, `evaluators/assertions.py` `event_count` / `event_sequence`, `evaluators/runner.py`, `evaluators/persona_driver.py`). `EVAL-MEMORY-001` (dementia, already landed) and `EVAL-RECALL-001` (F-3) are RFC-0044-owned and not recorded here.

## Open Questions

Each question is marked **DECIDED** (resolution folded into the design above; a Phase-1-surface decision) or **DEFERRED** (Phase-2, with a placeholder resolution that keeps Phase 1 forward-compatible).

1. **Event identity across retries. — DECIDED.** A retried model call is a *distinct* `before_model` / `after_model` firing (hooks fire once per `create_message`, not per turn) sharing the turn's `turn_id` with monotonically increasing `seq`, so retries are visible in the stream. Per-event `event_id` (§A) disambiguates individual events. ([§A](#a-event-taxonomy), [§C](#c-lifecycle-callbacks).)
2. **State-delta scope wording. — DEFERRED (Phase 2).** `StateDelta.scope` ships as `str` in Phase 1; re-typed to RFC 0042's `Scope` StrEnum in the same Phase-2 sweep as OQ #6. No Phase-1 blocker.
3. **In-callback model calls (was "token-usage attribution under veto"). — DECIDED.** A callback's own model call (e.g. a moderation LLM) emits a dedicated `CallbackModelOutput` event, **not** `ModelOutput(role="callback")` — channel-publish subscribes to `ModelOutput` only and would otherwise mis-publish a moderation call as the assistant's turn output. Under a `before_model` veto the model is not called, so there is no `ModelOutput`/token usage. ([§A](#a-event-taxonomy).)
4. **Privileged callback set. — DECIDED.** Model-path privileged callbacks are `{input-sanitizer, wallet-lease}`, fixed order input-sanitizer → wallet-lease, in the reserved `priority < 0` band. The event-stream redactor is a separate §B stream transform (ordered ahead of all callbacks). The persona quality-bar gate and the F-3 recall filter are non-privileged domain logic (`priority >= 0`). ([§Security](#security-considerations).)
5. **OTEL span model. — DEFERRED (Phase 2).** After migration, spans wrap event-pair intervals (`ToolCallEvent` → `ToolResultEvent`) nested under a top-level turn span. The model-call span needs a *start* boundary that completion-only `ModelOutput` does not provide — Phase 2 adds either a `Control` model-call marker or a `ModelCallStarted` event. Resolved with the consumer-migration work; no Phase-1 blocker.
6. **`CallbackContext.state` typing during the 0041 → 0042 gap. — DECIDED.** Phase 1 ships `CallbackContext.state` as a transitional `LegacyState` Protocol bundling the existing per-store accessors (`MemoryStore`, channel store, session namespace, working memory); the rename to `ScopedState` is part of the same Phase-2 sweep that re-types `StateDelta.scope` (OQ #2). Subscribers/callbacks must not depend on the `ScopedState` shape until then. ([§C](#c-lifecycle-callbacks), [§F](#f-backwards-compatibility).)
7. **Recall / context-injection event. — DEFERRED (Phase 2).** `StateDelta` records writes, not recall reads, so the F-3 recall filter is asserted via `final_transcript`, not the event stream. A dedicated `ContextInjection` event (which would let RFC 0044 assert recall on the stream) is deferred; it fits the "emitted side-effects, not prompt-assembly inputs" design and is not needed for the Phase-1 `EVAL-ERROR` goldens.

## Decision / Next Steps

**Proposed.** All seven open questions carry a DECIDED/DEFERRED disposition (four decided into the Phase-1 surface, three deferred to Phase 2 with forward-compatible placeholders), and the taxonomy / consistency holes flagged in review are closed inline — the preconditions the RFC set for leaving Draft are met. Awaiting maintainer review to advance Proposed → Accepted.

The one external precondition — RFC 0044's golden-trace *format* that this RFC's Phase-1 tests record against — **is already satisfied** (shipped v0.3.11: `schemas/eval_set.schema.json`, `evaluators/assertions.py`, `evaluators/runner.py`, `evaluators/persona_driver.py`). This RFC's typed events in turn unblock RFC 0044's `EVAL-ERROR-001` / `002` event goldens (RFC 0044 PR 4b) — a staged handoff, not a cycle ([M-4](#m-4-eval-is-gated-on-a-stable-surface)).

**Next steps to implementation:**
1. Author/maintainer review; on sign-off, flip status Draft → 📋 Proposed, then → Accepted per the repo's RFC review gate.
2. The Phase-1 PR plan is drafted at [`0041-pr-plan.md`](0041-pr-plan.md) (7 test-first slices; the slice summary below mirrors it). At Accepted, schedule it into the v0.4.0 plan.
3. Phase 1 is in-process Python only (no transport/Go surface); Phase 2 (consumer migration + wire encoding) is gated on RFC 0040.

**Phase-1 PR slices (draft, for the pr-plan):**
- **PR 1** — closed `ErrorKind` / `ToolErrorKind` enums (fully enumerated; Python-only per §A) + membership tests.
- **PR 2** — `agents/events.py`: the `TurnEvent` taxonomy (with `event_id` identity, `ToolCallEvent` / `ToolResultEvent`, `CallbackModelOutput`); no collision with `llm_types.ToolCall` / `registry.ToolResult`. Tests: ordering, identity, terminal-`Error`-before-`turn_aborted`.
- **PR 3** — `agents/callbacks.py`: Callback Protocol + dispatcher, `CallbackContext` (`PersonaState` / `channel_id` / `LegacyState`), `CallbackResult` (`mutate_*` fields), reserved privileged band + fixed order, off-hook rejection. Pure unit tests.
- **PR 4** — bounded in-process stream: per-subscriber queue, drop-oldest for non-terminal only, lossless `Control` / terminal `Error`, `dropped_events` counter, stream-level redaction transform. Tests: backpressure, lossless-terminal, sequence determinism.
- **PR 5** — emit events at the real sites (`action_loop.py` primary, `base.py`, `tools/registry.py`) behind the adapter; existing integration tests pass unchanged.
- **PR 6** — wrap RFC 0023 wallet-lease, RFC 0009 sanitizer/redactor, RFC 0031 F-3 recall filter as fixed-priority callbacks.
- **PR 7** — emit the `Error` events for `EVAL-ERROR-001` / `002`; hand back to RFC 0044 PR 4b.

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
