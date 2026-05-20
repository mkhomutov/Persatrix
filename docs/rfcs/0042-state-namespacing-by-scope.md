---
id: RFC-0042
title: State Namespacing by Scope Prefix
summary: Introduce a closed set of scope prefixes (app / persona / channel / session / interaction / temp) that every piece of agent-runtime state declares as part of its key, so persistence, visibility, and lifetime rules are determined by the key itself rather than by which subsystem happens to own it.
type: architecture
status: draft
author: Maksim Khomutov
created: 2026-05-20
target: v0.4.0+
depends_on:
  - RFC-0031
  - RFC-0034
---

# RFC 0042 — State Namespacing by Scope Prefix

**Type**: architecture
**Status**: 🔨 Draft
**Author**: Maksim Khomutov
**Date**: 2026-05-20
**Target**: v0.4.0+
**Depends on**: RFC 0031 (Per-Session Namespacing — defines the `Session` primitive this RFC reuses for the `session:` scope), RFC 0034 (Persona Conversational Working Memory — provides the per-interaction working-memory layer this RFC names `interaction:`)
**Relates to**: RFC 0041 (Typed Event Taxonomy — `StateDelta` events carry this RFC's scope as a typed field), RFC 0029 (Personal/Society Storage Split — the storage facade this RFC layers naming convention on top of, without changing the split), RFC 0037 (Memory Confidentiality & Channel Classification — the confidentiality layer composes with the scope layer)
**Spawned from**: [agent-runtime-vocabulary-roadmap.md §Seam 3](../agent-runtime-vocabulary-roadmap.md#seam-3--scoped-state-namespaces)

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
- [Security Considerations](#security-considerations)
- [Phased Implementation Plan](#phased-implementation-plan)
- [Files Touched (Estimated)](#files-touched-estimated)
- [Test Strategy](#test-strategy)
- [Open Questions](#open-questions)
- [Decision / Next Steps](#decision--next-steps)
- [Related Documentation](#related-documentation)

---

## Summary

Agent-runtime state in Persatrix today lives in several stores — persona memory ([RFC 0005](0005-persona-agent-memory.md)), channel store ([RFC 0011](0011-channels-bridges.md)), session namespaces ([RFC 0031](0031-per-session-namespacing-channels.md)), per-interaction working memory ([RFC 0034](0034-persona-conversational-working-memory.md)), wallet state ([RFC 0023](0023-llm-call-leasing.md)), and various in-process caches. Each store has its own lifetime, visibility, and persistence rules, and the rules live in the code that owns the store rather than in the key itself. A new piece of state has to re-decide every time: where does this belong? Who can see it? When does it expire?

This RFC introduces a **closed set of scope prefixes** — `app:`, `persona:`, `channel:`, `session:`, `interaction:`, `temp:` — that every key carries. The prefix determines persistence, visibility, and lifetime by convention; the same key in two scopes is two different facts. The convention is shallow on purpose: it does not migrate storage, change schemas, or alter access patterns. It adds a uniform vocabulary that removes a class of design decisions.

## Motivation

### M-1. Where does this state belong?

Recent RFCs have each had to invent their own answer:

- [RFC 0031](0031-per-session-namespacing-channels.md) introduced `Session` as a first-class primitive because channel and memory state were being polluted across CLI runs.
- [RFC 0034](0034-persona-conversational-working-memory.md) introduced per-interaction working memory because the persona needed to remember its own in-progress turn without persisting it as long-term memory.
- [RFC 0037](0037-memory-confidentiality-channel-classification.md) introduced confidentiality tags because some channel state needed visibility rules separate from its storage location.
- [RFC 0023](0023-llm-call-leasing.md) wallet state lives at persona/session granularity but is currently keyed by ad-hoc composite strings.

Each was the right call locally. Aggregated, they show the pattern: new state-bearing features keep re-discovering that scope is a thing, and each picks a different way to express it.

### M-2. The reviewer test

When a new RFC proposes "we'll store X here," the reviewer's first question is "for how long, and who sees it?" With no shared vocabulary, the answer is a paragraph. With prefixes, the answer is one of six words.

### M-3. State-delta events need a scope field

[RFC 0041](0041-typed-event-taxonomy-lifecycle-callbacks.md) emits `StateDelta` events. The event needs to carry "which scope was this delta in" so subscribers (channel publish, audit log, eval recorder) can filter. Without a closed vocabulary, that field is an opaque string and consumers each pick their own conventions. With this RFC, it is a typed enum.

### M-4. Confidentiality and scope are orthogonal

[RFC 0037](0037-memory-confidentiality-channel-classification.md) tags state by *who can see it*. This RFC tags state by *how long it lives and what it belongs to*. The two compose: a `channel:#planning:topic` value can be marked confidential to channel members; a `persona:ember-owl:trust:alice` value can be marked private to the persona. Confidentiality without scope is too coarse; scope without confidentiality is too loose. This RFC settles the scope axis so RFC 0037 can layer cleanly.

## Goals

1. **A closed, named set of scope prefixes.** Six scopes, no more without an RFC amendment.
2. **Persistence and lifetime determined by the prefix.** Every store implementation maps a scope to a persistence rule; the caller never has to know.
3. **Visibility determined by the prefix, with confidentiality layered on top.** The default visibility for each scope is fixed in this RFC; [RFC 0037](0037-memory-confidentiality-channel-classification.md) overlays finer-grained rules.
4. **No storage migration.** Existing tables stay where they are. The prefix is a *naming* and *routing* layer over the current stores.
5. **One `ScopedState` API.** All state access — by callbacks, by tools, by the persona runtime — goes through one typed API that takes scope + key.
6. **Backwards compatible.** Existing call sites continue to work; the prefix layer is added as a wrapper, and call sites migrate incrementally.

## Non-Goals

- **Storage layout changes.** [RFC 0029](0029-personal-society-storage-split.md) owns the storage layout; this RFC layers naming on top.
- **Cross-store joins.** A query like "all state for persona X across all scopes" is not in scope. Each scope is queried independently.
- **Schema or type registration.** This RFC does not introduce a typed schema per key; values remain opaque to the framework.
- **Distributed coordination.** No locking, no consistency guarantees beyond what the underlying store provides.
- **Replacing memory tiers.** The episodic/declarative/working tiers ([RFC 0008](0008-agent-memory-context-optimization.md), [RFC 0026](0026-declarative-facts-tier.md), [RFC 0034](0034-persona-conversational-working-memory.md)) are unchanged. This RFC's `interaction:` scope is the *runtime* working memory, not the memory tier — though they overlap and the mapping table makes that overlap explicit.

## Design / Implementation

### A. Scope vocabulary

| Scope | Owner | Lifetime | Example |
|-------|-------|----------|---------|
| `app:` | Process | Until restart (in-memory) or forever (config-backed) | `app:llm:default_alias`, `app:build:version` |
| `persona:` | One persona, across sessions | Persistent in persona's memory store | `persona:ember-owl:trust:alice`, `persona:ember-owl:speaking_style` |
| `channel:` | One channel, across sessions | Persistent in channels.db | `channel:#planning:topic`, `channel:#planning:members` |
| `session:` | One session ([RFC 0031](0031-per-session-namespacing-channels.md)) | Persistent for the life of the session | `session:abc:active_personas`, `session:abc:wallet:budget_remaining` |
| `interaction:` | One interaction ([RFC 0020](0020-interaction-lifecycle.md)) | Lives until interaction closes | `interaction:xyz:open_questions`, `interaction:xyz:working_summary` |
| `temp:` | One turn | Discarded at `Control(turn_completed)` | `temp:tool_args:read_file:path`, `temp:retry_count` |

The set is closed. Adding a scope requires an RFC amendment.

### B. Persistence and lifetime rules

| Scope | Persistence | Cleared by |
|-------|------------|------------|
| `app:` | In-memory or config file (per-key declaration) | Process restart |
| `persona:` | Persona memory store | Explicit delete only |
| `channel:` | Channel store | Channel deletion |
| `session:` | Session-scoped store ([RFC 0031](0031-per-session-namespacing-channels.md)) | Session end |
| `interaction:` | Interaction-scoped store ([RFC 0020](0020-interaction-lifecycle.md) / [RFC 0034](0034-persona-conversational-working-memory.md)) | `interaction_closed` event |
| `temp:` | In-memory only | `Control(turn_completed)` or `turn_aborted` |

`temp:` is the only scope guaranteed never to be persisted. Callbacks and tools that handle secrets should use `temp:` and never `persona:` / `channel:` / `session:`.

### C. Visibility rules

Default visibility (overridden by [RFC 0037](0037-memory-confidentiality-channel-classification.md) when applicable):

| Scope | Default visibility |
|-------|-------------------|
| `app:` | All agents in the process |
| `persona:` | Only the persona that owns it |
| `channel:` | All current channel members |
| `session:` | All participants in the session |
| `interaction:` | Only participants in that interaction |
| `temp:` | Only the current turn |

A `persona:` value is *not* visible to another persona in the same channel by default. A `channel:` value *is* visible to every persona currently in the channel.

### D. The `ScopedState` API

```python
# agents/state.py — sketch

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

Every mutation emits a `StateDelta` event ([RFC 0041](0041-typed-event-taxonomy-lifecycle-callbacks.md)) with the scope as a typed field. Reads do not emit events.

### E. Mapping to existing stores

| Scope | Backing store today | Change |
|-------|--------------------|--------|
| `app:` | `internal/config/` + process globals | Wrapper exposes config keys under `app:config:*` |
| `persona:` | Per-persona `memory.db` ([RFC 0005](0005-persona-agent-memory.md)), `MemoryStore` facade ([RFC 0029](0029-personal-society-storage-split.md)) | Wrapper translates `persona:<id>:<key>` to the existing facade |
| `channel:` | `channels.db` ([RFC 0011](0011-channels-bridges.md)) | Wrapper translates `channel:<name>:<key>` to a new key-value column on the channel record, or a sidecar table |
| `session:` | Session-scoped namespace ([RFC 0031](0031-per-session-namespacing-channels.md)) | Wrapper translates `session:<id>:<key>` to the existing namespace |
| `interaction:` | Working-memory store ([RFC 0034](0034-persona-conversational-working-memory.md)) | Wrapper translates `interaction:<id>:<key>` to the existing per-interaction store |
| `temp:` | In-process dict per agent worker, cleared per turn | New |

No data is moved. Existing call sites continue to work; the wrappers are the seam.

### F. Key composition

Keys are colon-separated:

```
<scope>:<owner_id>:<dotted.key.path>
```

- `<scope>` — one of the six.
- `<owner_id>` — persona ID, channel name, session ID, interaction ID, or omitted for `app:` / `temp:`.
- `<dotted.key.path>` — domain-specific, no fixed shape, conventional `dotted.snake.case`.

Examples:

```
persona:ember-owl:trust.scores.alice
channel:#planning:topic.current
session:S-abc123:wallet.budget_remaining_usd
interaction:I-xyz789:open_questions
temp::retry_count                              # no owner for temp
app::llm.default_alias                         # no owner for app
```

Double colon on owner-less scopes is intentional — it keeps the parser uniform.

## Security Considerations

- **Cross-scope leakage.** A bug in a wrapper that misroutes a key from `persona:` to `channel:` exposes private state. The wrapper layer has table-driven tests asserting that every (scope, owner_id, key) tuple resolves to exactly one storage location, and that resolution is deterministic.
- **Secrets in persisted scopes.** Tools that handle credentials must use `temp:`. A static check in the agent loop flags any `set(scope=persona|channel|session|interaction, ...)` call whose key matches known secret-name patterns; the flag is a hard error, not a warning, in CI.
- **Visibility leak through `StateDelta` events.** A `persona:` delta is visible only to the owning persona by default — but an event subscriber (channel publish, eval recorder) could over-read. The event-stream contract from [RFC 0041](0041-typed-event-taxonomy-lifecycle-callbacks.md) must enforce that subscribers see only deltas in scopes they have visibility for. The redactor callback (privileged, runs first per RFC 0041 §Security) is the chokepoint.
- **`app:` writes from agents.** Agents must not be able to write to `app:`. Writes are restricted to orchestrator init code; the `ScopedState` implementation in the agent worker process exposes a read-only `app:` view.

## Phased Implementation Plan

### Phase 1 — `ScopedState` API + wrappers

Ship the API, the six-scope enum, and the wrapper layer over existing stores. No call-site migration. New code uses the API; old code continues unchanged. `StateDelta` events emit with typed scope (depends on [RFC 0041](0041-typed-event-taxonomy-lifecycle-callbacks.md) Phase 1).

### Phase 2 — call-site migration

Persona runtime, channel publish, wallet client, F-3 recall filter, and working-memory layer migrate to `ScopedState`. The wrapper layer remains; the legacy direct-access call sites are removed.

### Phase 3 — confidentiality overlay integration

[RFC 0037](0037-memory-confidentiality-channel-classification.md) integration: confidentiality tags layer on top of scope visibility. Out of this RFC's scope; this phase only verifies the composition.

## Files Touched (Estimated)

| Component | Files | Change |
|-----------|-------|--------|
| Python agents | `agents/state.py` (new), `agents/persona_runtime.py`, `agents/recall.py`, `agents/working_memory.py` | New API + migration |
| Python agents | `agents/wallet_client.py` ([RFC 0023](0023-llm-call-leasing.md)) | Wallet state migrates to `session:` scope |
| Go orchestrator | `internal/server/channel*.go`, `internal/session/` ([RFC 0031](0031-per-session-namespacing-channels.md)) | Channel state wrappers; session-scope read-through |
| Storage | (no schema changes) | Wrappers route to existing stores |
| CI | `tests/unit/python/test_scoped_state.py`, `tests/integration/scope_routing_test.go` | Deterministic-routing tests, secret-name lint |

## Test Strategy

- **Unit tests**: scope-parsing round-trip; key-composition rules; per-scope persistence behavior; secret-name lint rejection.
- **Integration tests**: wrapper routing for each scope hits the correct underlying store; cross-scope keys with the same body are independent.
- **E2E**: a full chat-channel flow exercises all six scopes; visibility defaults enforced (a second persona in the same channel cannot read `persona:` state of the first).
- **Manual tests**: [MT-MEMORY-005](../manual-tests/MT-MEMORY-005-dementia-test.md) re-runs unchanged; behavior is byte-identical to pre-RFC.

## Open Questions

1. **`channel:` storage layout.** Add a key-value column to the channel record, or a sidecar `channel_state` table? Sidecar is cleaner but adds a join. Decision deferred to Phase 1.
2. **Are `interaction:` and the working-memory tier ([RFC 0034](0034-persona-conversational-working-memory.md)) the same store, or layered?** Lean: same store, this RFC names what RFC 0034 already built.
3. **Does `wallet:budget_remaining` belong in `session:` or `persona:`?** Today the wallet is per-session per-persona ([RFC 0023](0023-llm-call-leasing.md) §`WalletService`). The composite suggests *both*: `session:S-abc:wallet:ember-owl:budget` — but two-owner keys break the single-owner key composition rule. Resolve by treating wallet keys as `session:` scope with a compound owner in the key path: `session:S-abc:wallet.ember-owl.budget`.
4. **`app:` write boundary.** Where exactly is "orchestrator init code"? Probably the bootstrap path in `cmd/orchestrator` and nothing else. Document explicitly.
5. **Event subscriber visibility enforcement.** Where does the visibility filter live — in the event dispatcher (RFC 0041) or in each subscriber? Lean: dispatcher, as a privileged middleware, so subscribers cannot accidentally over-read.

## Decision / Next Steps

Draft. Phase 1 cannot begin until [RFC 0041](0041-typed-event-taxonomy-lifecycle-callbacks.md) Phase 1 lands (the `StateDelta` event shape). Open questions above must be resolved before status advances to Proposed.

## Related Documentation

- [Agent Runtime Vocabulary — Discussion Notes](../agent-runtime-vocabulary-roadmap.md)
- [RFC 0041 — Typed Event Taxonomy and Lifecycle Callbacks](0041-typed-event-taxonomy-lifecycle-callbacks.md)
- [RFC 0031 — Per-Session Namespacing for Channels and Persona Memory](0031-per-session-namespacing-channels.md)
- [RFC 0034 — Persona Conversational Working Memory](0034-persona-conversational-working-memory.md)
- [RFC 0029 — Personal/Society Storage Split](0029-personal-society-storage-split.md)
- [RFC 0037 — Memory Confidentiality & Channel Classification](0037-memory-confidentiality-channel-classification.md)
- [RFC 0020 — Interaction Lifecycle](0020-interaction-lifecycle.md)
- [RFC 0023 — LLM Call Leasing](0023-llm-call-leasing.md)
