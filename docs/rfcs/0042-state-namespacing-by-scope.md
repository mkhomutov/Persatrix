---
id: RFC-0042
title: State Namespacing by Scope Prefix
summary: Introduce a closed set of scope prefixes (app / persona / channel / session / interaction / temp) that every piece of agent-runtime state declares as part of its key, so persistence, visibility, and lifetime rules are determined by the key itself rather than by which subsystem happens to own it. The six scopes sit inside the already-shipped (principal, epoch) envelope from RFC 0031 / the memory-scope-axes model.
type: architecture
status: draft
author: Maksim Khomutov
created: 2026-05-20
target: v0.4.0+
depends_on:
  - RFC-0031
  - RFC-0034
  - RFC-0041
---

# RFC 0042 — State Namespacing by Scope Prefix

**Type**: architecture
**Status**: 🔨 Draft
**Author**: Maksim Khomutov
**Date**: 2026-05-20
**Target**: v0.4.0+
**Depends on**: RFC 0031 (Per-Session Namespacing — provides the `session_id` room-continuity axis this RFC's `session:` scope aligns with, plus the `epoch` run-isolation axis and `principal` tenant axis this RFC treats as an envelope), RFC 0034 (Persona Conversational Working Memory — the per-*channel* conversation window; referenced for how live-turn transcript state is reconstructed, **not** as the `interaction:` backing store), RFC 0041 (Typed Event Taxonomy — **Phase-1 build gate**: `ScopedState` mutations emit `StateDelta` events, so this RFC's Phase 1b cannot land until RFC 0041 Phase 1 ships the event shape)
**Relates to**: RFC 0020 (Interaction Lifecycle — the in-memory `InteractionTracker` turn buffer and the interaction-close boundary that back the `interaction:` scope), RFC 0029 (Personal/Society Storage Split — the `MemoryStore` facade this RFC layers a naming convention on top of, without changing the split; owns the Scratchpad working-memory tier), RFC 0037 (Memory Confidentiality & Channel Classification — the channel-classification layer composes with the scope layer), RFC 0039 (User Accounts & Authentication — supplies the verified `principal`/tenant envelope axis), RFC 0023 (LLM Call Leasing — the wallet, whose server-side budget granularities are discussed in §F)
**Spawned from**: [agent-runtime-vocabulary-roadmap.md §Seam 3](../agent-runtime-vocabulary-roadmap.md#seam-3--scoped-state-namespaces)
**Reconciles with**: [memory-scope-axes.md](../memory-scope-axes.md) — the four-axis (session / relationship / epoch / principal) discussion model that "touches RFC 0042 vocabulary"; see [§G](#g-reconciliation-with-the-shipped-scope-model).

---

## Table of Contents

- [Summary](#summary)
- [Motivation](#motivation)
- [Goals](#goals)
- [Non-Goals](#non-goals)
- [Design / Implementation](#design--implementation)
  - [A. Scope vocabulary](#a-scope-vocabulary)
  - [B. Persistence and lifetime rules](#b-persistence-and-lifetime-rules)
  - [C. Visibility rules](#c-visibility-rules)
  - [D. The `ScopedState` API](#d-the-scopedstate-api)
  - [E. Mapping to existing stores](#e-mapping-to-existing-stores)
  - [F. Key composition](#f-key-composition)
  - [G. Reconciliation with the shipped scope model](#g-reconciliation-with-the-shipped-scope-model)
- [Security Considerations](#security-considerations)
- [Phased Implementation Plan](#phased-implementation-plan)
- [Files Touched (Estimated)](#files-touched-estimated)
- [Test Strategy](#test-strategy)
- [Open Questions](#open-questions)
- [Decision / Next Steps](#decision--next-steps)
- [Related Documentation](#related-documentation)

---

## Summary

Agent-runtime state in Persatrix today lives in several stores — persona memory ([RFC 0005](0005-persona-agent-memory.md)), channel store ([RFC 0011](0011-channels-bridges.md)), session namespaces ([RFC 0031](0031-per-session-namespacing-channels.md)), per-interaction working memory ([RFC 0020](0020-interaction-lifecycle.md) / [RFC 0029](0029-personal-society-storage-split.md) Scratchpad tier), wallet state ([RFC 0023](0023-llm-call-leasing.md)), and various in-process caches. Each store has its own lifetime, visibility, and persistence rules, and the rules live in the code that owns the store rather than in the key itself.

This RFC introduces a **closed set of scope prefixes** — `app:`, `persona:`, `channel:`, `session:`, `interaction:`, `temp:` — that every key carries. The prefix determines persistence, visibility, and lifetime by convention; the same key in two scopes is two different facts.

The convention is deliberately shallow: for five of the six scopes it does not migrate storage, change schemas, or alter access patterns — it adds a uniform vocabulary that removes a class of design decisions. **One exception**: the `channel:` scope needs a small additive `channel_state` table (see [§E](#e-mapping-to-existing-stores) and [OQ #1](#open-questions)), so the "no schema changes" promise is qualified — it holds for `persona:` / `session:` / `interaction:` / `temp:` / `app:`, and `channel:` requires exactly one pure-additive migration with no data movement.

Critically, this RFC does **not** invent scope from scratch. The runtime already ships a multi-axis scope model — `session` (room continuity), `epoch` (run/test isolation), `principal` (tenant), `sender_type` — bound per request through `agents/request_scope.py` and resolved at call time by the `MemoryStore` facade ([memory-scope-axes.md](../memory-scope-axes.md)). This RFC's six prefixes name the *per-key state-write surface* that rides that existing axis vocabulary; [§G](#g-reconciliation-with-the-shipped-scope-model) reconciles the two so we do not create a second, divergent identity vocabulary.

## Motivation

### M-1. Where does this state belong?

Recent RFCs have each had to invent their own answer:

- [RFC 0031](0031-per-session-namespacing-channels.md) introduced session namespacing (later reframed into the four-axis model of [memory-scope-axes.md](../memory-scope-axes.md)) because channel and memory state were being polluted across CLI runs.
- [RFC 0034](0034-persona-conversational-working-memory.md) introduced the per-channel conversation window because the persona needed to see its own in-progress turns reconstructed as a transcript.
- [RFC 0020](0020-interaction-lifecycle.md) introduced the in-memory `InteractionTracker` (the live turn buffer for an open interaction) so memory writes get a stable episode scope.
- [RFC 0037](0037-memory-confidentiality-channel-classification.md) introduced channel classification because some channel-derived memory needed visibility rules separate from its storage location.
- [RFC 0023](0023-llm-call-leasing.md) wallet spend is keyed by fixed `global` / `per_workflow` / `per_agent` budget scopes (the `per_agent` scope is persona-granular), plus an RFC 0030 per-interaction budget ceiling — server-side in `internal/wallet/`, not by an ad-hoc per-session composite.

Each was the right call locally. Aggregated, they show the pattern: state-bearing features keep re-expressing *which axis* a fact belongs to, one wrapper at a time. A shared, closed vocabulary — riding the axes that already exist — removes the re-decision.

### M-2. The reviewer test

When a new RFC proposes "we'll store X here," the reviewer's first question is "for how long, and who sees it?" With no shared vocabulary, the answer is a paragraph. With prefixes, the answer is one of six words.

### M-3. State-delta events need a scope field

[RFC 0041](0041-typed-event-taxonomy-lifecycle-callbacks.md) emits `StateDelta` events. The event needs to carry "which scope was this delta in" so subscribers (channel publish, audit log, eval recorder) can filter. Without a closed vocabulary, that field is an opaque string and consumers each pick their own conventions. With this RFC, it becomes a typed enum. (Sequencing note: RFC 0041 Phase 1 deliberately ships `StateDelta.scope` as an opaque `str` so the two RFCs land independently; this RFC's Phase 1 ships the `Scope` enum, and RFC 0041's Phase-2 sweep re-types the field to it — see [§D](#d-the-scopedstate-api) and [Phase 1](#phased-implementation-plan).)

### M-4. Confidentiality and scope are orthogonal

[RFC 0037](0037-memory-confidentiality-channel-classification.md) classifies *channel-derived* memory by a channel-classification lattice plus a per-entry protection level, gated by the *acting channel's* classification. This RFC tags state by *how long it lives and what it belongs to*. The two compose: a value derived from a `restricted`-classified `channel:#leadership` is withheld from prompts assembled for a lower-classified acting channel, while its scope key still determines its lifetime and owner. Note the composition surface is *partial* — RFC 0037 classifies channel-derived memory tiers, which this RFC lists as a Non-Goal — and the simple "private to the persona" case is already covered by the `persona:` scope's default visibility ([§C](#c-visibility-rules)) without RFC 0037. This RFC settles the scope axis so RFC 0037 can layer on the channel-classification surface cleanly.

## Goals

1. **A closed, named set of scope prefixes.** Six scopes, no more without an RFC amendment — sitting inside the existing `(principal, epoch)` envelope ([§G](#g-reconciliation-with-the-shipped-scope-model)).
2. **Persistence and lifetime determined by the prefix.** Every store implementation maps a scope to a persistence rule; the caller never has to know.
3. **Visibility determined by the prefix, with confidentiality layered on top.** The default visibility for each scope is fixed in this RFC and *enforced by resolving owner identity from the ambient request scope*, not by trusting a caller-supplied key segment ([§C](#c-visibility-rules), [Security](#security-considerations)); [RFC 0037](0037-memory-confidentiality-channel-classification.md) overlays finer-grained channel-classification rules.
4. **Minimal storage change.** Existing tables stay where they are for `persona:` / `session:` / `interaction:` / `temp:` / `app:`; the prefix is a *naming* and *routing* layer over the current stores. The single exception is the `channel:` scope, which requires one additive `channel_state` table (no data movement).
5. **One `ScopedState` API.** All state access — by callbacks, by tools, by the persona runtime — goes through one typed API (`agents/scoped_state.py`) that takes scope + key.
6. **Backwards compatible.** Existing call sites continue to work; the prefix layer is added as a wrapper, and call sites migrate incrementally.

## Non-Goals

- **Storage-layout redesign.** [RFC 0029](0029-personal-society-storage-split.md) owns the storage layout; this RFC layers naming on top (with the one additive `channel_state` table noted in Goal 4).
- **Cross-store joins.** A query like "all state for persona X across all scopes" is not in scope. Each scope is queried independently.
- **Schema or type registration.** This RFC does not introduce a typed schema per key; values are opaque to the framework, serialized as JSON (see [§D](#d-the-scopedstate-api)).
- **Distributed coordination.** No locking, no consistency guarantees beyond what the underlying store provides — except that `increment` is atomic within a single backend (see [§D](#d-the-scopedstate-api) / [Test Strategy](#test-strategy)).
- **Replacing memory tiers.** The episodic/declarative/working tiers ([RFC 0008](0008-agent-memory-context-optimization.md), [RFC 0026](0026-declarative-facts-tier.md), [RFC 0029](0029-personal-society-storage-split.md)) are unchanged. This RFC's `interaction:` scope is the *runtime* interaction-scoped state (the `InteractionTracker` turn buffer / Scratchpad snapshot), not a memory tier.
- **A new tenant or run-isolation axis.** `principal` (tenant) and `epoch` (run/test isolation) already exist as orthogonal axes; this RFC does not add them to the closed six-scope set — it inherits them as an envelope ([§G](#g-reconciliation-with-the-shipped-scope-model)).

## Design / Implementation

### A. Scope vocabulary

| Scope | Owner | Lifetime | Example |
|-------|-------|----------|---------|
| `app:` | Process | Until restart (in-memory) or forever (config-backed) | `app::llm.default_alias` *(illustrative — see [§E](#e-mapping-to-existing-stores))* |
| `persona:` | One persona, across sessions | Persistent in persona's memory store | `persona:ember-owl:trust.scores.alice`, `persona:ember-owl:speaking_style` |
| `channel:` | One channel, across sessions | Persistent in the `channel_state` table | `channel:#planning:topic.current`, `channel:#planning:members` |
| `session:` | One session = room continuity `(agent, channel)` ([RFC 0031](0031-per-session-namespacing-channels.md) / [memory-scope-axes.md](../memory-scope-axes.md)) | Persistent; **accumulates** across the session, archived not cleared | `session:S-abc:active_personas` |
| `interaction:` | One interaction ([RFC 0020](0020-interaction-lifecycle.md)) | Lives while the interaction is open; frozen at close | `interaction:xyz:open_questions`, `interaction:xyz:working_summary` |
| `temp:` | One turn | Discarded at `Control(turn_completed)` or `turn_aborted` | `temp::tool_args.read_file.path`, `temp::retry_count` |

The set is closed. Adding a scope requires an RFC amendment. The six scopes are keyed *within* the ambient `(principal, epoch)` envelope — a `persona:` fact is implicitly scoped to the current tenant and run-isolation epoch, inherited from the request scope, not restated in the key ([§G](#g-reconciliation-with-the-shipped-scope-model)).

> **Two things the word "scope" already means in this codebase.** (1) RFC 0023 / `proto/wallet.proto` use `scope` for *budget granularity* (`global` / `per_workflow` / `per_agent`). (2) RFC 0020 §G uses `scope` for the recall-partition key (`dm:` / `group:` / `thread:`, surfaced in `agents/memory/scope_recall.py`). This RFC's `scope` is the *state-namespace prefix*. [§F](#f-key-composition) and [§G](#g-reconciliation-with-the-shipped-scope-model) disambiguate the three.

### B. Persistence and lifetime rules

| Scope | Persistence | Cleared by |
|-------|------------|------------|
| `app:` | In-memory or config file (per-key declaration) | Process restart |
| `persona:` | Persona memory store | Explicit delete only |
| `channel:` | `channel_state` table | Channel deletion |
| `session:` | Session-scoped rows ([RFC 0031](0031-per-session-namespacing-channels.md)) | Explicit delete only — session state accumulates and is archived, not cleared at "session end" ([memory-scope-axes.md](../memory-scope-axes.md): a room-continuity session resets only when a new channel is used). Run/test isolation is the separate `epoch` axis. |
| `interaction:` | In-memory `InteractionTracker` ([RFC 0020](0020-interaction-lifecycle.md) §C) + Scratchpad snapshot ([RFC 0029](0029-personal-society-storage-split.md) §B) | Interaction transitions to `closed` / `summarized` ([RFC 0020](0020-interaction-lifecycle.md) §C) |
| `temp:` | In-memory only | `Control(turn_completed)` or `turn_aborted` ([RFC 0041](0041-typed-event-taxonomy-lifecycle-callbacks.md)) |

`temp:` is the only scope guaranteed never to be persisted. Callbacks and tools that handle secrets should use `temp:` and never `persona:` / `channel:` / `session:` / `interaction:`.

### C. Visibility rules

Default visibility (overridden by [RFC 0037](0037-memory-confidentiality-channel-classification.md) channel classification when applicable):

| Scope | Default visibility |
|-------|-------------------|
| `app:` | All agents in the process (read-only from agent workers; see [Security](#security-considerations)) |
| `persona:` | Only the persona that owns it |
| `channel:` | All current channel members |
| `session:` | All participants in the session |
| `interaction:` | Only participants in that interaction |
| `temp:` | Only the current turn |

A `persona:` value is *not* visible to another persona by default. A `channel:` value *is* visible to every persona currently in the channel.

**How this is enforced.** The `ScopedState` API deliberately does **not** take a caller/principal argument (that would fight the repo's ambient-identity convention, and `principal` ≠ `persona`). Instead, for owner-scoped reads (`persona:` / `session:` / `interaction:`) the wrapper **resolves or validates** the owner segment against ambient identity — the running persona's own id for `persona:`, the bound `session` / `interaction` scope for those — rather than trusting a caller-supplied key segment. This is descriptive-default *and* enforced: an agent running as persona A resolves its own owner id and can never address `persona:B:*`. See the cross-owner-read bullet in [Security](#security-considerations).

### D. The `ScopedState` API

```python
# agents/scoped_state.py — sketch
#
# Distinct from (a) the Go internal/state package (orchestrator workflow-run
# execution state — WorkflowRun/RunStatus/StepState), and (b) RFC 0041
# StateDelta *events*. This is the agent-runtime scoped key-value surface.

class ScopedState(Protocol):
    def get(self, scope: Scope, key: str) -> Any | None: ...
    def set(self, scope: Scope, key: str, value: Any) -> None: ...
    def delete(self, scope: Scope, key: str) -> None: ...
    def increment(self, scope: Scope, key: str, by: int = 1) -> int: ...

    # Bulk read within a single scope; never crosses scopes.
    def list_keys(self, scope: Scope, prefix: str = "") -> list[str]: ...

class Scope(StrEnum):
    APP = "app"
    PERSONA = "persona"
    CHANNEL = "channel"
    SESSION = "session"
    INTERACTION = "interaction"
    TEMP = "temp"
```

- **Owner resolution.** For owner-scoped scopes the caller passes only the `dotted.key.path`; the wrapper injects/validates the owner segment from ambient identity ([§C](#c-visibility-rules), [§F](#f-key-composition)).
- **Value serialization.** Values are opaque to the framework but must be JSON-serializable (`str` / `int` / `float` / `bool` / `None` / `list` / `dict`); the persistent backends store the JSON encoding. Non-serializable values raise at `set` time. (Pinning the on-disk encoding here closes a design hole — the backends are SQLite, so "opaque `Any`" alone is not implementable.)
- **`increment` atomicity.** `increment` is atomic *within a single backend* (INSERT … ON CONFLICT / `RETURNING`), so two turns incrementing the same counter converge. No cross-backend/distributed guarantee is offered (Non-Goals).
- **Events.** Every mutation emits a `StateDelta` event ([RFC 0041](0041-typed-event-taxonomy-lifecycle-callbacks.md)). In RFC 0041 Phase 1 the event's `scope` field is an opaque `str` carrying this enum's value; RFC 0041's Phase-2 sweep re-types it to `Scope`. Reads do not emit events.

**Cross-language note.** `Scope` is a Python `StrEnum`. In Phase 1 all scope parsing/routing stays Python-side: the wrappers translate keys before anything crosses the wire, and the Go orchestrator stores channel/session state as opaque key-value blobs without parsing the prefix. If a Go component ever needs to recognize scope prefixes, `Scope` must first be promoted to a generated cross-language closed set (a `proto/` enum, or the `cmd/genpatterns` → `agents/security_enums.py` parity gate), never a hand-copied string literal — the same discipline RFC 0041 §A/§Security applies to `StateDelta.scope`.

### E. Mapping to existing stores

| Scope | Backing store today | Change |
|-------|--------------------|--------|
| `app:` | `internal/defaults/defaults.go` (system-default constants) + `config/*.yaml` loaded at `cmd/orchestrator` boot; Python side `agents/optimization.py` + `agents/model_aliases.py` (LLM alias/provider config) + in-process globals | Wrapper exposes these read-through under `app::config.*` / `app::llm.*`. **No single `internal/config/` package exists** — `app:` reads span the sources above. |
| `persona:` | Per-persona `memory.db` ([RFC 0005](0005-persona-agent-memory.md)), typed `MemoryStore` facade ([RFC 0029](0029-personal-society-storage-split.md)) | The facade exposes only *typed tier methods* (`store_episode`/`recall_episodes`/trust/bond methods), **not** a `get(key)`/`set(key,value)` surface. A generic key-value adapter over the facade is **new work** — trust-style keys map onto the typed bond methods; free-form persona config keys need the adapter. |
| `channel:` | `channels.db` ([RFC 0011](0011-channels-bridges.md)), owned by `internal/channels/` | New additive `channel_state(channel_id, key, value)` sidecar table — the next `channelStoreSchemaVersion` after v10, following the pure-additive migration precedent (v6 `epoch_id`, v8 operator config, v9 membership intervals). No data moved. |
| `session:` | RFC 0031 provides a `session_id` *tagging/filter* dimension plus a `sessions` registry table (`internal/channels/session_registry.go`, `session_binding.go`), **not** a general key-value store | A `session:<id>:<key>` KV surface for arbitrary keys is **new work** (mirror the `channel:` sidecar pattern, e.g. a `session_state` table), not "the existing namespace." |
| `interaction:` | In-memory `InteractionTracker` turn buffer ([RFC 0020](0020-interaction-lifecycle.md) §C; `agents/persona_runtime/conversation_window.py` reconstructs the live transcript) + Scratchpad tier ([RFC 0029](0029-personal-society-storage-split.md) §B) | Neither is a general KV store. A `interaction:<id>:<key>` KV surface (e.g. `open_questions`, `working_summary`) is **new work** bounded by the Scratchpad's interaction-close lifetime. |
| `temp:` | In-process dict per agent worker, cleared per turn | New |

No **persona/session/interaction/app** data is moved. The `channel:` scope is the one exception: it adds the additive `channel_state` table above. Where "existing store" is a typed facade (`persona:`) or a tagging dimension (`session:`) rather than a KV surface, the generic key-value adapter is new code — the wrapper is still the seam, but §E is honest that three of the six scopes need a thin new KV surface rather than a pure rename.

### F. Key composition

Keys are colon-separated:

```
<scope>:<owner_id>:<dotted.key.path>
```

- `<scope>` — one of the six.
- `<owner_id>` — persona ID, channel name, session ID, interaction ID, or omitted for `app:` / `temp:`. For owner-scoped scopes it is **resolved/validated from ambient identity** ([§C](#c-visibility-rules)), not blindly trusted from the caller.
- `<dotted.key.path>` — domain-specific, no fixed shape, conventional `dotted.snake.case`.

Examples:

```
persona:ember-owl:trust.scores.alice
channel:#planning:topic.current
session:S-abc123:active_personas
interaction:I-xyz789:open_questions
temp::retry_count                              # no owner for temp
app::llm.default_alias                         # no owner for app
```

Double colon on owner-less scopes is intentional — it keeps the parser uniform.

**Multi-owner facts.** Some state belongs to a *pair* of owners. The rule is: pick the *outer* owner (whose lifecycle the state lives and dies with) as the scope `owner_id`, and encode the *inner* owner inside the dotted key path — e.g. a per-session, per-persona counter:

```
session:S-abc123:speaker.ember-owl.turns_taken     # illustrative
```

The scope (`session:`) determines lifetime and visibility; the inner owner (`ember-owl`) is part of the key path because it does not change the lifetime answer. Multi-owner keys are a documented pattern, not an exception to §F.

> **The wallet is *not* the multi-owner example.** RFC 0023's wallet lives **server-side in `internal/wallet/`**; `agents/wallet_client.py` is a stateless gRPC client with no local state to "migrate." Wallet budgets are keyed by fixed `global` / `per_workflow` / `per_agent` scopes (the wallet's own `scope` vocabulary, `proto/wallet.proto`) plus an RFC 0030 per-interaction ceiling — there is **no session dimension**, and budgets reset on a time/config schedule (`internal/cost/cost.go`), so wallet state does **not** "expire with the session." If a `session:`-scoped read-through *view* of wallet spend is later wanted, that is a client-side convenience over server-side truth, not a state migration, and it would introduce a new session dimension RFC 0023 does not have today — call it out explicitly rather than attributing it to RFC 0023.

### G. Reconciliation with the shipped scope model

The premise of this RFC is *not* that scope has no vocabulary — the runtime already ships a unified, dependency-injected, multi-axis model that this RFC must ride, not replace:

- `agents/request_scope.py` binds four orthogonal axes per request via one context manager, composed from leaf modules `agents/session_id.py`, `agents/principal_id.py`, `agents/epoch_id.py`, `agents/sender_type.py`.
- Recall/write predicates resolve those axes at call time (`agents/memory/_session_filter.py`, `_principal_filter.py`, `_epoch_filter.py`; the `MemoryStore` facade partitions rows by `(agent, principal, session, epoch)`).
- The design record is [memory-scope-axes.md](../memory-scope-axes.md), which explicitly "touches RFC 0042 vocabulary" and defines the four axes: **session = room** `(agent, channel)`, **relationship** `(agent, participant)`, **epoch** (run/test isolation, default `live`), **principal** (tenant, RFC 0039).

Two reconciliations follow:

1. **`session:` is room continuity, not an ephemeral run.** This RFC adopts the shipped semantics: `session:` accumulates across runs/restarts and resets only when a new channel is used; it is *not* cleared at a "session end." The disposable run/test namespace is the separate **`epoch`** axis — deliberately **not** a member of the closed six-scope set.

2. **`principal` (tenant) and `epoch` (run isolation) are an envelope, not scopes.** Every `persona:` / `channel:` / `session:` / `interaction:` fact is implicitly scoped to the current `(principal, epoch)` inherited from the request scope. A naming-only wrapper over the `MemoryStore` facade **must preserve** the facade's existing `principal`/`epoch` partitioning; it does not restate them in the six-scope key. `persona:` aligns with the model's persona substrate; it is **not** the `principal`/tenant axis (persona ≠ tenant). Relationship state (`persona:<id>:trust.*`) maps onto the typed bond methods, not a free KV path.

`ScopedState` therefore **consumes** the resolved `session` / `interaction` ids from the existing ContextVars (`current_session_id()` etc.) rather than re-parsing them out of composed key strings, so we do not grow a second identity vocabulary — the exact failure mode Motivation M-1 exists to prevent.

## Security Considerations

- **Cross-scope leakage.** A bug in a wrapper that misroutes a key from `persona:` to `channel:` exposes private state. The wrapper layer has table-driven tests asserting that every (scope, owner_id, key) tuple resolves to exactly one storage location, and that resolution is deterministic.
- **Cross-owner reads within an owner-scoped namespace.** Because owner-scoped reads resolve/validate `owner_id` from ambient identity ([§C](#c-visibility-rules)), a `persona:B:*` request issued while running as persona A is denied at the wrapper — persona A can only ever resolve its own owner id. Tests assert that a caller cannot read another owner's rows by hand-constructing the key segment.
- **Secrets in persisted scopes.** Tools that handle credentials must use `temp:`. A static check in the agent loop flags any `set(scope=persona|channel|session|interaction, ...)` call whose key matches known secret-name patterns; the flag is a hard error, not a warning, in CI. The pattern source is the RFC 0009 secret patterns in `internal/security/redactor.go` (`SecretRedactor` default patterns), surfaced to Python via the same generation/parity mechanism used elsewhere — **not** `agents/security_patterns.py`, which holds prompt-injection patterns, not secret-name patterns.
- **Visibility leak through `StateDelta` events.** A `persona:` delta is visible only to the owning persona by default, but an event subscriber (channel publish, eval recorder) could over-read. **RFC 0041 provides no per-scope subscriber filter today**: its [§B](0041-typed-event-taxonomy-lifecycle-callbacks.md) stream-level *redaction transform* rewrites event **content** (scrubbing PII/credentials in tool args, model output, and `StateDelta` values) *before* fan-out — it is a property of the stream, "not a callback," and it redacts content uniformly rather than filtering by scope. Per-subscriber, scope-based visibility filtering is therefore a **new mechanism this RFC must specify** (dispatcher-level privileged middleware vs. per-subscriber), tracked in [OQ #5](#open-questions) — not something borrowed from RFC 0041.
- **`app:` writes from agents.** Agents must not write to `app:`. Writes are restricted to orchestrator-init code (`cmd/orchestrator/main.go` boot path; the Python-side `agents/optimization.py` / `agents/model_aliases.py` load). The `ScopedState` implementation in the agent worker process exposes a read-only `app:` view whose `set(scope=app, ...)` raises. (Operator config switches happen via config edit + restart, or, for channel-scoped runtime edits, via `channel:` per RFC 0050 — not an agent `app:` write.)

## Phased Implementation Plan

### Phase 1a — `ScopedState` API + wrappers (no RFC 0041 dependency)

Ship `agents/scoped_state.py`: the six-scope `Scope` enum, key parser/composition ([§F](#f-key-composition)), per-scope wrapper routing ([§E](#e-mapping-to-existing-stores)) including the additive `channel_state` table and the new `session:` / `interaction:` / `persona:` KV adapters, the secret-name lint ([Security](#security-considerations)), the ambient owner-resolution, and the deterministic-routing / cross-owner / serialization / independence tests. **This phase depends on nothing from RFC 0041** and is the critical path.

### Phase 1b — `StateDelta` emission (gated on RFC 0041 Phase 1)

Emit a `StateDelta` on every mutation ([§D](#d-the-scopedstate-api)). This is the **only** deliverable gated on RFC 0041 Phase 1 landing (the `StateDelta` event *shape*). It needs only the event type's existence — `Scope` is a `StrEnum` whose values feed RFC 0041 Phase 1's opaque `str` field directly; the Phase-2 re-typing is RFC 0041's follow-up.

### Phase 2 — call-site migration

Persona runtime, channel publish, wallet-client read-through view, F-3 recall filter, and the interaction-scoped runtime layer migrate to `ScopedState`. The wrapper layer remains; the legacy direct-access call sites are removed. (This is a multi-module sweep, not a single file — see [Files Touched](#files-touched-estimated).)

### Phase 3 — confidentiality overlay integration

[RFC 0037](0037-memory-confidentiality-channel-classification.md) integration: channel-classification tags layer on top of scope visibility. This phase only verifies the composition.

## Files Touched (Estimated)

| Component | Files | Change |
|-----------|-------|--------|
| Python agents (new) | `agents/scoped_state.py` (new), `tests/unit/python/test_scoped_state.py` (new) | New `ScopedState` API + `Scope` enum + tests |
| Python agents (call sites, Phase 2) | `agents/persona_runtime/` package — `memory_context.py` (F-3 recall callback), `conversation_window.py`, `_conversation_window_cache.py`, `action_loop.py`; `agents/memory/` — `_notes_recall.py`, `scope_recall.py`, `_session_filter.py`, `_principal_filter.py`, `_epoch_filter.py`, `facade.py`; `agents/wallet_client.py` (client-side read-through view only — no local wallet state) | Migrate state access to `ScopedState`. Note: `persona_runtime` is a **package** (~40 modules); the sweep touches many files. |
| Python identity axes (reconcile, not rewrite) | `agents/request_scope.py`, `agents/session_id.py`, `agents/principal_id.py`, `agents/epoch_id.py`, `agents/session_metadata.py` | `ScopedState` consumes the resolved ids from these ContextVars ([§G](#g-reconciliation-with-the-shipped-scope-model)); no change to the axis binders themselves |
| Go orchestrator | `internal/channels/` (channel-state store — new `channel_state` table in `sqlite_migrations.go` + a store accessor; **not** `internal/server/channel*.go`, which is the REST handler surface / Phase-2 publish call site); RFC 0031 session namespacing lives in `internal/channels/session_registry.go` + `session_binding.go` and `internal/server/session_handlers.go` | Channel-state sidecar table; session-scope read-through. In Phase 1 the Go side stores opaque blobs (no scope-prefix parsing). |
| Config / app: sources | `internal/defaults/defaults.go`, `config/*.yaml`, `cmd/orchestrator/main.go`, `agents/optimization.py`, `agents/model_aliases.py` | Read-through only. **No `internal/config/` package exists.** |
| Storage | `internal/channels/sqlite_migrations.go` (+`sqlite_schema.go`) | **One** additive migration: `channel_state` table. All other scopes: no schema change. |
| CI | `tests/unit/python/test_scoped_state.py`, `tests/integration/scope_routing_test.go` | Deterministic-routing tests, cross-owner-read tests, serialization round-trip, secret-name lint |

> **Naming collision.** The proposed `agents/scoped_state.py` / "state namespacing" is distinct from the pre-existing, unrelated Go `internal/state/` package (orchestrator workflow-run execution state — `WorkflowRun` / `RunStatus` / `StepState`) and from RFC 0041 `StateDelta` *events*. The module is named `scoped_state.py` (matching `test_scoped_state.py` and the `ScopedState` class) precisely to avoid overloading "state" a third time.

## Test Strategy

- **Unit tests**: scope-parsing round-trip; key-composition rules; per-scope persistence behavior; ambient owner-resolution; secret-name lint rejection; **value serialization round-trip** (`str`/`int`/`float`/`bool`/`None`/`list`/`dict`) through each persistent backend, asserting the pinned JSON encoding; `set(scope=app, ...)` from the agent-worker view **raises** (read-only enforcement).
- **`increment` concurrency**: N concurrent tasks incrementing the same key converge to N, per backend (in-process `temp:` dict and each SQLite-backed scope). Precedent: the interaction-count `INSERT … RETURNING` race in `tests/.../test_episodic_memory_concurrent_writes.py` (ISSUE-0055), not the unrelated `internal/state` workflow-run store.
- **`StateDelta` emission** (Phase 1b): each mutating op emits exactly one `StateDelta` with the correct scope value.
- **Integration tests**: wrapper routing for each scope hits the correct underlying store; cross-scope keys with the same body are independent; a `persona:B:*` read while running as persona A is denied.
- **E2E**: a full chat-channel flow exercises all six scopes; visibility defaults hold — a second persona in the same channel, resolving its own owner id, can never address the first persona's `persona:` rows.
- **Manual tests**: [MT-MEMORY-005](../manual-tests/MT-MEMORY-005-dementia-test.md) re-runs unchanged (byte-identical no-regression guard — it validates recall quality, which this RFC does not change). Optionally add an `MT-STATE-00x` acceptance overlay for the security-sensitive path (two-persona `persona:` isolation; `temp:` vanishing at `Control(turn_completed)`; `session:` accumulation) once the [PR plan](#decision--next-steps) exists.

## Open Questions

1. ~~`channel:` storage layout — KV column or sidecar table?~~
   **Resolved**: sidecar `channel_state(channel_id, key, value)` table — the next `channelStoreSchemaVersion` after v10, a pure-additive migration with no data movement (precedent: v6/v8/v9). This is the single carve-out from the "no schema changes" guarantee (Summary / Goal 4 / Non-Goals / Files Touched all reflect it).
2. ~~Are `interaction:` and the RFC 0034 conversation window the same store?~~
   **Resolved (re-attributed)**: RFC 0034 is the per-*channel* conversation window (a live transcript reconstruction, no interaction-keyed KV store). The `interaction:` scope is backed by RFC 0020's in-memory `InteractionTracker` plus the RFC 0029 Scratchpad tier; a general `interaction:<id>:<key>` KV surface is **new work** bounded by the Scratchpad's interaction-close lifetime (see [§E](#e-mapping-to-existing-stores)).
3. ~~Wallet scope assignment.~~
   **Resolved (re-framed)**: the wallet is server-side (`internal/wallet/`) with `global`/`per_workflow`/`per_agent` budget scopes and no session dimension; it does not "expire with the session." It is therefore **not** migrated to `session:` and is not the multi-owner example (see [§F](#f-key-composition)). A `session:`-scoped read-through *view* remains a possible future convenience over server-side truth, to be proposed separately if wanted.
4. ~~`app:` write boundary — where is "orchestrator init code"?~~
   **Resolved**: the Go init writer is the `cmd/orchestrator/main.go` boot path; the Python-side seed is the `agents/optimization.py` / `agents/model_aliases.py` config load. Agent workers get a read-only `app:` view (see [Security](#security-considerations)). No `internal/config/` package exists; `app:` reads span `internal/defaults/`, `config/*.yaml`, and the Python config loaders.
5. **Event-subscriber visibility enforcement — still open.** RFC 0041's stream-level redactor scrubs *content* uniformly and is explicitly "not a callback"; it is **not** a per-subscriber scope filter. Where does scope-based subscriber gating live — a privileged dispatcher middleware, or per-subscriber? This is a **new mechanism** (possibly an RFC 0041 amendment), not something borrowable from RFC 0041, and must be settled before Phase 3.
6. **`(principal, epoch)` envelope wiring — new.** Does `ScopedState` inherit `principal`/`epoch` purely from the ambient ContextVars for every scope (preferred), or does any scope need them in the key? Lean: envelope-only, matching the `MemoryStore` facade's existing partitioning ([§G](#g-reconciliation-with-the-shipped-scope-model)). Confirm no scope needs epoch/principal in the composed key before Phase 1a freezes the parser.

## Decision / Next Steps

Draft. This revision corrects the cross-reference and code-path errors surfaced in review and reconciles the six-scope vocabulary with the shipped scope-axis model. Dependency and sequencing state:

- **True dependency distance.** Only **Phase 1b** (`StateDelta` emission, [§D](#d-the-scopedstate-api)) is gated on RFC 0041 — and RFC 0041 is currently only **Proposed**, so "lands" is two status transitions away (Accepted → a landed Phase 1). **Phase 1a** (the enum, API, wrappers, `channel_state` table, secret-lint, and tests) carries **no** RFC 0041 dependency and can be scheduled independently.
- **Open-question dispositions.** OQ #1–#4 are resolved above; OQ #5 (subscriber visibility) and OQ #6 (envelope wiring) remain open and must close before, respectively, Phase 3 and Phase 1a freezes.

**Leave-Draft → Proposed checklist:**

1. Author ratifies the design decisions applied in this revision (sidecar `channel_state`; `session:` = room-continuity; `epoch`/`principal` as envelope, not scopes; wallet not `session:`-scoped; `agents/scoped_state.py` naming).
2. Resolve OQ #6 (envelope wiring) and settle the OQ #5 mechanism (dispatcher middleware vs. per-subscriber).
3. Author `docs/rfcs/0042-pr-plan.md` mirroring [`0041-pr-plan.md`](0041-pr-plan.md) — test-first slices: `Scope` enum + parser → per-scope wrappers + routing tests → secret-lint → `temp:` turn-clear → (Phase 1b) `StateDelta` emission → per-module call-site migration → RFC 0037 overlay verification, each with a deliverable, dependency, and green-gate.
4. `make rfcs` re-run if any INDEX-surfaced field changes (none changed in this revision).

**Accepted → Implementing** additionally requires RFC 0041 Phase 1 to land for Phase 1b.

## Related Documentation

- [Agent Runtime Vocabulary — Discussion Notes](../agent-runtime-vocabulary-roadmap.md)
- [Memory Scope Axes — Discussion Notes](../memory-scope-axes.md) — the four-axis (session/relationship/epoch/principal) model this RFC reconciles with
- [AI Glossary](../ai-glossary.md) — Conversation Window vs. Scratchpad vs. working-memory tier
- [RFC 0041 — Typed Event Taxonomy and Lifecycle Callbacks](0041-typed-event-taxonomy-lifecycle-callbacks.md)
- [RFC 0031 — Per-Session Namespacing for Channels and Persona Memory](0031-per-session-namespacing-channels.md)
- [RFC 0034 — Persona Conversational Working Memory](0034-persona-conversational-working-memory.md)
- [RFC 0029 — Personal/Society Storage Split](0029-personal-society-storage-split.md)
- [RFC 0037 — Memory Confidentiality & Channel Classification](0037-memory-confidentiality-channel-classification.md)
- [RFC 0020 — Interaction Lifecycle](0020-interaction-lifecycle.md)
- [RFC 0023 — LLM Call Leasing](0023-llm-call-leasing.md)
