# Persona Agents & Memory — User Guide

A practical walkthrough of what you can do with v0.2 persona agents: declaring
one in config, the three-tier memory system, and the cost budgets that keep a
persona from running away with your API bill.

> **Spec-level detail** lives in [RFC 0005](../rfcs/0005-persona-agent-memory.md)
> (persona runtime + memory system) and
> [RFC 0006](../rfcs/0006-efficiency-execution-limits.md) (execution limits,
> budgets, and defaults). This guide is deliberately non-exhaustive and points
> into those RFCs for design rationale.

---

## 1. Declaring a persona

Personas are declared in [config/agents.yaml](../../config/agents.yaml)
alongside v0.1 task agents. The schema is
[schemas/agent.schema.json](../../schemas/agent.schema.json) — run
`make validate` after any edit to this file.

### Persona vs. task agent, in one sentence

A **task agent** (`type: task`) needs an `instructions` system prompt and runs
only when the orchestrator assigns it a workflow step. A **persona agent**
(`type: persona`) needs `persona` / `autonomy` / `memory` blocks and runs as a
long-lived gRPC service with a tick-driven event loop of its own.

The schema enforces this distinction with conditional validation: a `persona`
agent must declare the `persona` block, a `task` agent must declare
`instructions`, and an agent that declares any persona-only field without
setting `type` is rejected
([schemas/agent.schema.json:73–109](../../schemas/agent.schema.json#L73-L109)).

### Naming policy for personas

Use nickname-style persona names and IDs, not human-like names, to avoid
accidentally matching real people.

- Good: `ember-owl`, `orbit-kite`, `nova-sparrow`
- Avoid: first-name + last-name IDs

Generate candidates with:

```bash
make generate-persona-nickname COUNT=5
```

For reproducible output (useful in docs/tests), pass a seed:

```bash
make generate-persona-nickname COUNT=3 SEED=42
```

### A worked example

The repository ships with `ember-owl`, a "VP of Engineering" persona
([config/agents.yaml:133–192](../../config/agents.yaml#L133-L192)):

```yaml
- id: "ember-owl"
  type: "persona"
  name: "Ember Owl"
  role: "Engineering leadership and technical oversight"
  model: "claude-sonnet-4-20250514"
  temperature: 0.7
  capabilities: [architecture_review, sprint_planning, team_management]
  tools: [file_read, mcp:github]
  max_retries: 2
  timeout_seconds: 300

  persona:
    title: "VP of Engineering"
    background: |
      15 years in software engineering. Former tech lead at a Series B startup.
      Values pragmatism over perfection.
    behavior:
      directness: direct           # indirect | balanced | direct
      detail_focus: big-picture    # big-picture | balanced | detail-focused
      formality: professional      # casual | professional | formal
      risk_tolerance: moderate     # cautious | moderate | bold
      expressiveness: reserved     # reserved | moderate | expressive
    quirks:
      - "Starts every Monday with 'Alright, what's on fire?'"
      - "Hates meetings longer than 30 minutes"
    goals:
      primary: "Ship v2.0 on time with acceptable quality"
      secondary: ["Reduce tech debt by 20%"]
      hidden: "Prove the team can self-organize"
    knowledge:
      domains: ["system design", "team management", "Go"]
      limitations: ["frontend/CSS", "ML internals"]

  autonomy:
    level: "semi-autonomous"       # passive | reactive | semi-autonomous
    tick_interval_seconds: 60
    max_actions_per_tick: 3
    idle_after_ticks: 10

  memory:
    db_path: "data/memory.db"
    notes:
      enabled: true
      max_notes: 500
      auto_reflect_after: 5
      inject_recent_notes: 3

  relationships:
    - agent_id: "iron-fox"
      type: "reports_to_me"
      trust_level: 0.9
```

Launch it once the orchestrator is running:

```bash
make run-agent AGENT=ember-owl
```

### What each block controls

**`persona`** shapes the agent's voice and decisions. The five-dimension
`behavior` block replaces free-text personality traits — it renders into
structured prompt fragments at event time
([agents/persona_behavior.py](../../agents/persona_behavior.py)). Valid
values for each dimension are enumerated in
[schemas/agent.schema.json:142–203](../../schemas/agent.schema.json#L142-L203).

**`autonomy`** controls the tick loop. Only three of the five enum levels are
wired today — `passive`, `reactive`, and `semi-autonomous`. `autonomous` and
`supervisor` parse but are deferred to a v0.2 follow-up RFC
([config/agents.yaml:174–177](../../config/agents.yaml#L174-L177)). The
remaining fields in the block govern cadence
(`tick_interval_seconds`), parallelism (`max_actions_per_tick`), and
back-off after inactivity (`idle_after_ticks`).

**`memory`** configures per-agent memory behaviour — see §2. Omit the block
entirely to accept every default.

**`relationships`** seeds initial trust scores. Seeding uses SQL
`INSERT OR IGNORE` so re-deploying the same config will never overwrite a
trust score already learned at runtime
([agents/memory/relationship.py:77–98](../../agents/memory/relationship.py#L77-L98)).

---

## 2. The three memory tiers

Every persona has three tiers of memory, composed on every event by
[agents/persona_runtime/memory_context.py](../../agents/persona_runtime/memory_context.py):

| Tier | Module | Persistence | Rough purpose |
|------|--------|-------------|--------|
| **Episodic** | [agents/memory/episodic.py](../../agents/memory/episodic.py) | SQLite (`memory.db_path`) | "What happened, and did it work?" |
| **Relationship** | [agents/memory/relationship.py](../../agents/memory/relationship.py) | SQLite (same DB) | "Who do I work with, and do I trust them?" |
| **Working** | [agents/memory/working.py](../../agents/memory/working.py) | In-process | "What fits in this prompt?" |

On each event, `_inject_memory_context` clears stale sections, queries each
tier, truncates content to per-tier character caps, and writes the result
into working memory at a priority that controls how it's included in the
prompt
([agents/persona_runtime/memory_context.py:96–200](../../agents/persona_runtime/memory_context.py#L96-L200)).

### Episodic memory

Episodes are ranked, searchable records of past interactions. The agent calls
`store_episode(summary, context, outcome, importance=0.5, tags=None)` after
a decision or interaction, and `recall(query, limit=10, min_importance=0.0)`
to retrieve ranked matches.

- **Ranking** uses FTS5 BM25 combined with `importance × access_count × recency`
  when both a query string and FTS5 are available. If the query syntax is
  malformed (`recall_fts5` raises) the code falls back to a LIKE search
  with the same `importance × access_count × recency` ordering; if FTS5 is
  unavailable on the SQLite build, LIKE is used directly; and when no
  query string is supplied, the ordering is `importance × access_count ×
  recency` alone via `recall_recency`
  ([agents/memory/episodic_queries.py:103–191](../../agents/memory/episodic_queries.py#L103-L191)).
- **Retention**: `memory.episodic.retention_days` (default 90) applies to
  compressed episodes only. Raw (uncompressed) episodes are preserved until
  they have been summarised — the compression pass is a separate explicit
  step, not a silent drop
  ([agents/memory/episodic.py:372–400](../../agents/memory/episodic.py#L372-L400)).

### Relationship memory

Relationship memory tracks pairwise trust and the last N interactions between
two agents.

- `record_interaction(other, type, outcome, sentiment)` adds a row and bumps
  the interaction counter.
- `update_trust(other, delta, reason)` clamps the delta to ±0.2 per call to
  prevent a single event from moving trust to extremes, then clamps the
  result to `[0.0, 1.0]`
  ([agents/memory/relationship.py:129–214](../../agents/memory/relationship.py#L129-L214)).
- `apply_decay(decay_rate)` nudges every relationship toward the neutral
  midpoint (0.5) each cycle — the rate comes from
  `memory.relationship.decay_rate` (default `0.01`).
- `get_relationship_summary(other)` returns trust, interaction count, and up
  to 10 recent interactions. The guide uses this when assembling context for
  a message from that agent.

Relationship context is only injected into working memory when trust has
moved at least 0.01 away from neutral — if the trust is effectively 0.5,
the LLM wouldn't learn anything from seeing it, so the section is skipped
([agents/persona_runtime/memory_context.py:41–42](../../agents/persona_runtime/memory_context.py#L41-L42)).

### Working memory

Working memory is the in-process buffer that assembles the next LLM
prompt. It holds `ContextSection` entries with a `priority` (higher = kept
longer) and a rough `token_count`.

- `build_context()` returns sections in priority order, dropping from the
  lowest-priority end once cumulative tokens exceed `max_tokens`
  ([agents/memory/working.py:99–136](../../agents/memory/working.py#L99-L136)).
- `compress_if_needed(llm_client)` kicks in when the buffer overflows.
  It walks compressible sections in ascending-priority order and asks
  Claude Haiku to summarise each one, stopping as soon as the total fits
  within `max_tokens`. A replacement is only kept if the summary is
  strictly shorter than the original, which guards against a model
  producing longer output and prevents retry loops
  ([agents/memory/working.py:160–245](../../agents/memory/working.py#L160-L245)).

Priorities used by the runtime for the `ContextSection` entries injected
into working memory: relationship context = 8, episodic recall = 7, notes
= 6
([agents/persona_runtime/memory_context.py:185,233,274](../../agents/persona_runtime/memory_context.py#L185)).
The system prompt and persona description are assembled as a plain string
by `_build_system_prompt()` in
[action_loop.py:327–345](../../agents/persona_runtime/action_loop.py#L327-L345)
and concatenated with the retrieved memory sections before the LLM call —
they are not stored as `ContextSection` objects and therefore do not
appear in the priority ordering above. User events are passed as a
separate `messages` list and are likewise outside working memory.

### A minimal walkthrough

A single tick of `ember-owl` after receiving a pull-request event from
`iron-fox`:

1. **Tick fires** at `tick_interval_seconds: 60`.
2. **Episodic recall** — `_inject_memory_context` queries
   `episodic_memory.recall("code review iron-fox", limit=10)`.
   BM25 returns the top matches; their `access_count` is incremented so
   frequently-used memories outrank stale ones on later queries.
3. **Relationship context** — `relationship_memory.get_relationship_summary("iron-fox")`
   returns `trust_score=0.9` plus recent interactions. Because 0.9 is well
   away from neutral, the section is injected at priority 8.
4. **LLM call** — the action loop
   ([agents/persona_runtime/action_loop.py](../../agents/persona_runtime/action_loop.py))
   runs up to `max_llm_calls` turns (see §3), subject to the budget check.
5. **Record outcome** — after the decision the agent calls
   `record_interaction("iron-fox", "code_review", outcome="approved", sentiment=0.8)`
   and `update_trust("iron-fox", delta=+0.1, reason="delivered on time")`.
   Trust stays at `1.0` (clamped).
6. **Compression (if needed)** — if working memory is now over budget,
   `compress_if_needed` summarises the oldest conversation section before
   the next tick.

---

## 3. Cost budgets

Cost control in v0.2 has three parts: **execution limits** (how many LLM
calls, how many tokens per call), **USD budgets** (daily total, per-workflow,
per-agent), and the **`/api/v1/cost/summary`** endpoint for observability.

### Execution limits — three-level cascade

For every workflow step, the orchestrator resolves
`max_llm_calls`, `max_tokens`, and `timeout_seconds` from three sources, in
order of precedence
([internal/scheduler/budget.go:27–89](../../internal/scheduler/budget.go#L27-L89)):

1. **Workflow step config** (highest priority) — set per step in the
   workflow YAML.
2. **Agent config** — set per agent in `config/agents.yaml` (the
   `max_llm_calls` and `max_tokens` fields at the top of the agent entry).
3. **System defaults** — defined in
   [internal/defaults/defaults.go](../../internal/defaults/defaults.go):

   - `DefaultMaxLLMCalls = 5`
   - `DefaultMaxTokens = 8192`
   - `DefaultTimeoutSeconds = 60`

> **Upgrade note from v0.1.** The `max_llm_calls` default was lowered from
> `10` to `5` in v0.2. Agents that rely on deeper tool-use loops must set
> `max_llm_calls` explicitly on the agent entry. See
> [CHANGELOG.md](../../CHANGELOG.md) and RFC 0006 Section B.

A zero value at any level means "not configured" and the resolver falls
through to the next level. Negative values produce a warning log and are
treated the same way.

### USD budgets

Daily, per-workflow, and per-agent USD caps are declared in
[config/optimization.yaml](../../config/optimization.yaml):

```yaml
cost:
  pricing:
    models:
      claude-sonnet-4-20250514: { input_per_1m_tokens: 3.00, output_per_1m_tokens: 15.00 }
      claude-haiku-4-5:         { input_per_1m_tokens: 0.80, output_per_1m_tokens:  4.00 }
  budgets:
    global:
      max_daily_usd: 100.00
      alert_at_percent: [50, 80, 95]
      on_exceed: "pause_and_alert"   # NOTE: placeholder in v0.2 — currently logged and treated as "fail"
    per_workflow:
      default_max_usd: 10.00
    per_agent:
      default_max_usd: 5.00
```

Before every gRPC dispatch the orchestrator calls
`BudgetEnforcer.CheckBudget` with a worst-case cost estimate (pricing table
× estimated output tokens). All three scopes are read in a single atomic
snapshot to avoid torn reads; if any scope would be exceeded the dispatch is
rejected **before** the LLM is called
([internal/cost/cost.go:260–336](../../internal/cost/cost.go#L260-L336)).

A rejection today causes the workflow step to fail with
`ErrBudgetExceeded`. The failure is visible in the step status
([internal/scheduler/stage_runner.go:152–158](../../internal/scheduler/stage_runner.go#L152-L158))
and on the OTEL span (`span.RecordError` + `codes.Error`). The
`BudgetError` struct carries the offending scope plus spent / limit /
estimated-cost numbers — for example:

```text
global budget exceeded: spent=87.500000, limit=100.000000, estimated=25.000000
```

A structured HTTP 429 response to REST clients is planned but not yet
wired; the mapping is deferred to a follow-up PR noted in
[internal/scheduler/budget.go:21](../../internal/scheduler/budget.go#L21).

### Reading cost — `GET /api/v1/cost/summary`

```bash
curl http://localhost:8080/api/v1/cost/summary
```

Returns a global summary plus top agents by spend. The handler is
[internal/server/cost_handlers.go](../../internal/server/cost_handlers.go) —
note that it returns **503** if the cost reporter is not wired, i.e. cost
tracking is optional and the rest of the orchestrator degrades gracefully
when it is absent.

Each completed step also records cost metadata (`EstimatedCostUSD`,
`TokensUsed`, `LLMCallCount`, `RetryCount`, `CacheHit`, `WallTimeMs`) that
is visible through the workflow-run APIs and OTEL spans
([internal/scheduler/budget.go:226–240](../../internal/scheduler/budget.go#L226-L240)).

---

## 4. Where to go next

- **RFC 0005 — Persona Agent & Memory System**:
  [docs/rfcs/0005-persona-agent-memory.md](../rfcs/0005-persona-agent-memory.md).
  Design rationale for the tick loop, behaviour dimensions, and memory tiers.
- **RFC 0006 — Efficiency & Execution Limits**:
  [docs/rfcs/0006-efficiency-execution-limits.md](../rfcs/0006-efficiency-execution-limits.md).
  Design rationale for the three-level cascade, budget enforcement, response
  cache, and derived deadlines.
- **Manual tests** exercising the persona and memory surfaces:
  [docs/manual-tests/README.md](../manual-tests/README.md) — in particular the
  `MT-PERSONA-*`, `MT-MEMORY-*`, and `MT-COST-*` suites.
- **Architecture diagrams** for persona runtime and memory tier interaction:
  [docs/diagrams/](../diagrams/).
