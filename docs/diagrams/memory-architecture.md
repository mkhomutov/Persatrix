# Memory Architecture

Persona agents have five complementary memory stores with different lifetimes,
purposes, and persistence characteristics. Working memory lives in-process;
the other four are backed by a shared SQLite database.

> **v0.3.1 adds the declarative-facts tier.** RFC 0026 introduces a fifth
> tier — `FactStore` ([agents/memory/facts.py](../../agents/memory/facts.py)) —
> that persists stated `(subject, predicate, object)` facts about users and
> named entities, extracted by the close-path summariser at interaction
> close. Like the rest of the persona read path it is read **directly**, not
> through `MemoryFacade` (see [§v0.3.1 reality check](#v031-reality-check--persona-runtime-is-asymmetric)).
> RFC 0031 Phase 1 also lands a `session_id` column on the `episodes`,
> `relationships`, and `facts` rows (default `legacy`) — see
> [§Schema & migrations](#schema--migrations).

In v0.3.0 the **`MemoryFacade`** ([agents/memory/facade.py](../../agents/memory/facade.py),
[RFC 0008 §B](../rfcs/0008-agent-memory-context-optimization.md#b-memory-facade-shape))
is the read/write contract for **task agents** —
`store_observation` / `retrieve_relevant` / `compress`. Task-agent callers
must not depend on the tier-specific schemas; the facade is the boundary.

The **persona runtime is asymmetric**: its read path
([memory_context.py](../../agents/persona_runtime/memory_context.py),
[channel_history.py](../../agents/persona_runtime/channel_history.py),
[facts_section.py](../../agents/persona_runtime/facts_section.py))
calls the tier modules directly — `episodic.recall`, `recall_notes`,
`recall_relationship_summary`, `recall_with_scope_filter`, and (v0.3.1)
`recall_facts_for_event`. The only facade
contact on the persona side is the pure-function `MemoryFacade.compress`
invoked by [summarize_close.py](../../agents/persona_runtime/summarize_close.py)
on interaction close (no facade instance / DB connection required) — the
same close path also runs the RFC 0026 fact extractor and writes the
extracted facts straight to `FactStore`. Closing the persona-runtime read
path through the facade is a deferred follow-up.

Per-channel and per-DM **scoping** is layered on top through
`recall_with_scope_filter` ([agents/memory/scope_recall.py](../../agents/memory/scope_recall.py)) —
RFC 0020 P3 + RFC 0011 P3 joint delivery — so an agent in many channels does
not pull unrelated history into its prompt for a single-channel turn. Task
agents reach it through the facade; the persona runtime calls it directly
from `channel_history.py`.

```mermaid
graph TB
    subgraph Runtime["Persona runtime (per-agent process)"]
        PR["persona_runtime/<br/>action_loop"]
        CTX["memory_context<br/>assembler"]
        ITX["InteractionTracker<br/>(RFC 0020)"]
    end

    subgraph Facade["MemoryFacade — agents/memory/facade.py (RFC 0008 §B)<br/>task-agent contract; persona runtime uses compress() only"]
        F["store_observation /<br/>retrieve_relevant /<br/>compress<br/>+ scope-filtered recall<br/>(scope_recall.py)"]
    end

    subgraph Stores["Tier modules — agents/memory/"]
        W["Working memory<br/>working.py<br/>context-window retention<br/>+ auto-summarization"]
        E["Episodic memory<br/>episodic.py + episodic_queries.py<br/>one entry per interaction (RFC 0020)<br/>scope tag = channel/DM id"]
        R["Relationship memory<br/>relationship.py<br/>per-pair trust + history"]
        N["Agent notes<br/>notes.py<br/>agent-curated knowledge"]
        FCT["Declarative facts<br/>facts.py — FactStore (RFC 0026)<br/>(subject, predicate, object) tuples<br/>extracted at interaction close"]
    end

    subgraph Persistence["Persistence"]
        DB[(memory.db<br/>SQLite + FTS5<br/>episodes / relationships / facts<br/>carry session_id — RFC 0031 P1)]
        MIG["migrations.py<br/>schema versions"]
    end

    PR --> CTX
    PR --> ITX
    ITX -->|on close →<br/>summarize_close.py<br/>calls MemoryFacade.compress<br/>— pure function on turn list| F
    ITX -.->|on close →<br/>fact extractor writes<br/>(subject, predicate, object)| FCT
    CTX -.->|persona-runtime read path<br/>bypasses facade<br/>recall / recall_notes /<br/>recall_with_scope_filter| E
    CTX -.->|relationship summary<br/>— direct call| R
    CTX -.->|facts_section.py<br/>recall_facts_for_event<br/>— direct call| FCT
    F --> W
    F --> E
    F --> R
    F --> N

    PR -.->|append turn| W

    E --> DB
    R --> DB
    N --> DB
    FCT --> DB
    MIG -.migrates.-> DB

    classDef volatile fill:#fff8e1,stroke:#d39e00
    classDef facade fill:#e3f2fd,stroke:#1976d2,stroke-width:2px
    classDef facts fill:#e8f5e9,stroke:#2e7d32,stroke-width:2px
    class W volatile
    class F facade
    class FCT facts
```

## Why a facade (RFC 0008)

Pre-v0.3.0 task agents called tier modules directly — `episodic_memory.recall(...)`,
`relationship_memory.get_relationship_summary(...)`, `note_store.recall_notes(...)`.
That coupling made it impossible to:

- **Allocate a token budget across tiers** — every caller had to know the
  cap of every tier, and there was no central place to weigh competing
  recalls against a shared budget. Per-step packaging
  ([internal/executor/packaging/](../../internal/executor/packaging/))
  now allocates a single context budget that the facade fans out across
  episodic / relationship / notes.
- **Switch tier implementations without rewriting callers** — task-agent
  callers depend only on `store_observation` / `retrieve_relevant` /
  `compress`, so a tier's storage backend or ranking can change without
  touching every call site.
- **Apply per-channel / per-DM scoping uniformly** — `recall_with_scope_filter`
  is the single read-side dual to the `interaction_id` + scope tag the
  episodic store writes under (RFC 0020). Without the facade, every
  caller would have to remember to pass the right scope.

### v0.3.1 reality check — persona runtime is asymmetric

The persona runtime predates the facade and has not been migrated to it.
Its read path
([`memory_context.py`](../../agents/persona_runtime/memory_context.py),
[`channel_history.py`](../../agents/persona_runtime/channel_history.py),
[`facts_section.py`](../../agents/persona_runtime/facts_section.py))
calls tier modules directly — `episodic.recall`, `recall_notes`,
`recall_relationship_summary`, `recall_with_scope_filter`, and the v0.3.1
`recall_facts_for_event`. The only facade
contact on the persona side is the **pure-function** `MemoryFacade.compress`
invoked by [`summarize_close.py`](../../agents/persona_runtime/summarize_close.py)
on interaction close — no facade instance or DB connection is required. That
same close path runs the RFC 0026 fact extractor and writes the extracted
`(subject, predicate, object)` tuples directly to `FactStore`.

So the budget-allocation and uniform-scoping properties above describe the
**task-agent** contract; the persona runtime gets per-channel scoping today
by calling `recall_with_scope_filter` directly. Closing the persona-runtime
read path through the facade is a deferred follow-up.

## Per-channel scoping (RFC 0020 P3 + RFC 0011 P3)

`InteractionTracker.add_turn` accepts a `scope` (e.g. `dm:alice:ember-owl`,
`group:planning`, or a per-workflow id). On interaction close,
`summarize_close.py` writes the resulting episodic entry tagged with that
scope. On the read side, `recall_with_scope_filter` applies the scope as
an AND filter on top of the BM25 / LIKE / recency ranking, so a recall
from inside a `group:planning` turn does not surface `dm:alice:ember-owl`
history. The cross-RFC priority order between scoped channel-history
recall and the relationship/episodic tiers is pinned by
[`tests/unit/python/test_memory_context_priority_order.py`](../../tests/unit/python/test_memory_context_priority_order.py).

## Tier characteristics

| Tier | Lifetime | Backing store | Primary use |
|------|----------|--------------|-------------|
| **Working** | Volatile — cleared on agent shutdown | In-process (Python) | Assemble the current prompt; priority-weighted retention; summarizes low-priority sections when the token budget is exceeded |
| **Episodic** | Persistent | SQLite + FTS5 | Recall past interactions by relevance; outcome + decision log |
| **Relationship** | Persistent | SQLite | Per-agent-pair trust score, bidirectionally decayed toward neutral (0.5) so grudges don't calcify |
| **Notes** | Persistent | SQLite | Agent-authored structured knowledge, reached via tool calls (`store_note`, `recall_notes`) |
| **Facts** | Persistent | SQLite | Stated `(subject, predicate, object)` facts about users and named entities; extracted at interaction close, recalled by subject, reinforced on restatement, retracted on contradiction (RFC 0026) |

## Context assembly order

On every LLM call, `memory_context.py` composes the system prompt by layering
(in order):

1. Persona identity + behaviour (static from `config/agents.yaml`).
2. Relationship snapshot for participants in the current interaction.
3. Declarative facts — stated `(subject, predicate, object)` facts about
   the current sender, recalled by subject so a follow-up that does not
   repeat the subject string still surfaces them (RFC 0026).
4. Episodic recall — top-K semantically relevant past episodes.
5. Selected notes (if referenced by the event/goal).
6. Working memory (most recent turns, priority-weighted).

The list above is the **textual layering** (the order sections appear in the
assembled prompt), not the eviction ranking. Each section is tagged with a
numeric `priority` (see [persona_runtime/memory_context.py](../../agents/persona_runtime/memory_context.py):
relationship=8, episodic=7, facts=7, notes=6; persona identity and working
memory sit higher still). When the assembled context exceeds the model's window,
`working.py` summarizes sections in **ascending priority order** (lowest first),
so the list position above does not predict what gets dropped — the numeric
priority does. See the [Working memory section of the persona guide](../guides/persona-agents.md#working-memory)
for the walk-through.

## Schema & migrations

`agents/memory/migrations.py` owns the schema for `memory.db`. All four
persistent stores share the same database and migration runner — a single
schema version covers the episodic, relationship, notes, and facts tables.

`NoteStore` explicitly piggybacks on the connection managed by
`EpisodicMemory` and does not run its own migrations.

The v0.3.1 schema (`memory.db` v7) adds a `session_id TEXT NOT NULL DEFAULT
'legacy'` column to the `episodes` and `relationships` tables, and the RFC
0026 `facts` table carries `session_id` from creation — RFC 0031 Phase 1's
forward-only column plumbing. Every persona-memory write stamps the active
`PERSATRIX_SESSION_ID`; an unset env var falls back to the `legacy`
carve-out. Phase 1 is write-path only — recall does not yet filter by
session, so pre-existing rows stay visible. See
[RFC 0031](../rfcs/0031-per-session-namespacing-channels.md).

## Token counting

`working.py::estimate_tokens` defaults to `chars/4` (≈85% accurate for English
prose). Callers that need higher fidelity pass `accurate=True` to use
`tiktoken cl100k_base` if available, falling back to `chars/4` if not
installed.

See [persona-runtime.md](persona-runtime.md) for how the context assembler
is invoked on each tick / event.
