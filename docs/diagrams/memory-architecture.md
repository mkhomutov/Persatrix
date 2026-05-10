# Memory Architecture

Persona agents have four complementary memory stores with different lifetimes,
purposes, and persistence characteristics. Working memory lives in-process;
the other three are backed by a shared SQLite database.

In v0.3.0 the **`MemoryFacade`** ([agents/memory/facade.py](../../agents/memory/facade.py),
[RFC 0008 §B](../rfcs/0008-agent-memory-context-optimization.md#b-memory-facade-shape))
is the read/write contract for **task agents** —
`store_observation` / `retrieve_relevant` / `compress`. Task-agent callers
must not depend on the tier-specific schemas; the facade is the boundary.

The **persona runtime is asymmetric in v0.3.0**: its read path
([memory_context.py](../../agents/persona_runtime/memory_context.py),
[channel_history.py](../../agents/persona_runtime/channel_history.py))
calls the tier modules directly — `episodic.recall`, `recall_notes`,
`recall_relationship_summary`, `recall_with_scope_filter`. The only facade
contact on the persona side is the pure-function `MemoryFacade.compress`
invoked by [summarize_close.py](../../agents/persona_runtime/summarize_close.py)
on interaction close (no facade instance / DB connection required).
Closing the persona-runtime read path through the facade is a deferred
follow-up.

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

    subgraph Facade["MemoryFacade — agents/memory/facade.py (RFC 0008 §B)<br/>task-agent contract; persona runtime uses compress() only in v0.3.0"]
        F["store_observation /<br/>retrieve_relevant /<br/>compress<br/>+ scope-filtered recall<br/>(scope_recall.py)"]
    end

    subgraph Stores["Tier modules — agents/memory/"]
        W["Working memory<br/>working.py<br/>context-window retention<br/>+ auto-summarization"]
        E["Episodic memory<br/>episodic.py + episodic_queries.py<br/>one entry per interaction (RFC 0020)<br/>scope tag = channel/DM id"]
        R["Relationship memory<br/>relationship.py<br/>per-pair trust + history"]
        N["Agent notes<br/>notes.py<br/>agent-curated knowledge"]
    end

    subgraph Persistence["Persistence"]
        DB[(memory.db<br/>SQLite + FTS5)]
        MIG["migrations.py<br/>schema versions"]
    end

    PR --> CTX
    PR --> ITX
    ITX -->|on close →<br/>summarize_close.py<br/>calls MemoryFacade.compress<br/>(pure function on turn list)| F
    CTX -.->|persona-runtime read path<br/>bypasses facade in v0.3.0:<br/>recall / recall_notes /<br/>recall_with_scope_filter| E
    CTX -.->|relationship summary<br/>(direct)| R
    F --> W
    F --> E
    F --> R
    F --> N

    PR -.->|append turn| W

    E --> DB
    R --> DB
    N --> DB
    MIG -.migrates.-> DB

    classDef volatile fill:#fff8e1,stroke:#d39e00
    classDef facade fill:#e3f2fd,stroke:#1976d2,stroke-width:2px
    class W volatile
    class F facade
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
- **Switch tier implementations without rewriting callers** — the facade
  is what lets RFC 0026 (declarative facts tier, v0.3.1) ship as an
  additive tier without churning every task-agent call site.
- **Apply per-channel / per-DM scoping uniformly** — `recall_with_scope_filter`
  is the single read-side dual to the `interaction_id` + scope tag the
  episodic store writes under (RFC 0020). Without the facade, every
  caller would have to remember to pass the right scope.

### v0.3.0 reality check — persona runtime is asymmetric

The persona runtime predates the facade and was not migrated as part of
v0.3.0. Its read path
([`memory_context.py`](../../agents/persona_runtime/memory_context.py) +
[`channel_history.py`](../../agents/persona_runtime/channel_history.py))
calls tier modules directly — `episodic.recall`, `recall_notes`,
`recall_relationship_summary`, `recall_with_scope_filter`. The only facade
contact on the persona side is the **pure-function** `MemoryFacade.compress`
invoked by [`summarize_close.py`](../../agents/persona_runtime/summarize_close.py)
on interaction close — no facade instance or DB connection is required.

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

## Context assembly order

On every LLM call, `memory_context.py` composes the system prompt by layering
(in order):

1. Persona identity + behaviour (static from `config/agents.yaml`).
2. Relationship snapshot for participants in the current interaction.
3. Episodic recall — top-K semantically relevant past episodes.
4. Selected notes (if referenced by the event/goal).
5. Working memory (most recent turns, priority-weighted).

The list above is the **textual layering** (the order sections appear in the
assembled prompt), not the eviction ranking. Each section is tagged with a
numeric `priority` (see [persona_runtime/memory_context.py](../../agents/persona_runtime/memory_context.py):
relationship=8, episodic=7, notes=6; persona identity and working memory sit
higher still). When the assembled context exceeds the model's window,
`working.py` summarizes sections in **ascending priority order** (lowest first),
so the list position above does not predict what gets dropped — the numeric
priority does. See the [Working memory section of the persona guide](../guides/persona-agents.md#working-memory)
for the walk-through.

## Schema & migrations

`agents/memory/migrations.py` owns the schema for `memory.db`. All three
persistent stores share the same database and migration runner — a single
schema version covers episodic, relationship, and notes tables.

`NoteStore` explicitly piggybacks on the connection managed by
`EpisodicMemory` and does not run its own migrations.

## Token counting

`working.py::estimate_tokens` defaults to `chars/4` (≈85% accurate for English
prose). Callers that need higher fidelity pass `accurate=True` to use
`tiktoken cl100k_base` if available, falling back to `chars/4` if not
installed.

See [persona-runtime.md](persona-runtime.md) for how the context assembler
is invoked on each tick / event.
