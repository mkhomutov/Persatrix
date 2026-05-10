# Persona Agents & Memory — User Guide

A practical walkthrough of what you can do with v0.2 persona agents: declaring
one in config, the three-tier memory system, and the cost budgets that keep a
persona from running away with your API bill.

> **Spec-level detail** lives in [RFC 0005](../rfcs/0005-persona-agent-memory.md)
> (persona runtime + memory system),
> [RFC 0006](../rfcs/0006-efficiency-execution-limits.md) (execution limits,
> budgets, and defaults), and
> [RFC 0016](../rfcs/0016-human-participant-chat-interface.md)
> (the human chat surface introduced in v0.2.1). v0.3.0 layers three new
> shapes onto the persona runtime — see the callouts in §2 (interactions
> are not messages — [RFC 0020](../rfcs/0020-interaction-lifecycle.md)),
> §2 (now-anchor + relative-time — [RFC 0021](../rfcs/0021-persona-temporal-awareness.md))
> and the new §6 (externally-inspectable prompt sections — [RFC 0022](../rfcs/0022-persona-prompt-section-templating.md)).
> This guide is deliberately non-exhaustive and points into those RFCs for
> design rationale.

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
[schemas/agent.schema.json:142–203](../../schemas/agent.schema.json#L142-L203);
the natural-language descriptions rendered into the prompt live in
[prompts/runtime/persona/sections/behavior-dimensions.yaml](../../prompts/runtime/persona/sections/behavior-dimensions.yaml).

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

> **v0.2.2 — bounded memory injection.** A per-event `MemoryBudget` allocator
> (default 1500 tokens) caps the combined episodic + relationship + notes
> context admitted into a single event, and `recall` / `recall_notes` accept
> a `min_score` relevance threshold that drops weak matches before truncation.
> When an autonomous TICK fires with zero admitted memory, no active goal,
> and no pending conversation turn, the LLM call is skipped entirely and
> `idle_count` is incremented — see
> [RFC 0017 §B](../rfcs/0017-persona-memory-injection-budget.md#b-memory-budget-allocator)
> and [§F](../rfcs/0017-persona-memory-injection-budget.md#f-empty-context-tick-short-circuit)
> for the spec.

> **v0.3.0 — conversations are interactions, not messages.** Multi-turn
> dialogues no longer write one episodic entry per inbound event. Instead,
> [`InteractionTracker`](../../agents/memory/interactions.py) opens a scope
> on the first turn, accumulates turns via `add_turn`, and on close — either
> a quiescence timeout or an explicit close signal — collapses the whole
> exchange into **one** episodic record summarised by
> [`agents/persona_runtime/summarize_close.py`](../../agents/persona_runtime/summarize_close.py).
> A janitor on the tick cadence drives stale interactions to close. The
> `interaction_id` is the lookup key for per-channel and per-DM scoping;
> `recall_with_scope_filter` ([agents/memory/scope_recall.py](../../agents/memory/scope_recall.py))
> is the read-side dual. See [RFC 0020](../rfcs/0020-interaction-lifecycle.md)
> for the full lifecycle (`open → multi-turn → close → summarize`) and
> [`docs/diagrams/persona-runtime.md`](../diagrams/persona-runtime.md) for
> where the stages sit in the runtime.

> **v0.3.0 — what time is it?** A persona now anchors every prompt with a
> `now-anchor` line ([prompts/runtime/persona/sections/now-anchor.md](../../prompts/runtime/persona/sections/now-anchor.md))
> and renders episode and relationship timestamps as relative time
> ("yesterday", "3 days ago", "just now") instead of raw epoch seconds.
> Wall-clock reads route through the [`Clock`](../../agents/clock.py)
> Protocol — `WallClock` in production, `FrozenClock` in tests — and the
> rendering helpers ([agents/temporal/rendering.py](../../agents/temporal/rendering.py))
> are pure functions. This is Phase 1 only; structured commitment tracking,
> scheduled callbacks, and conversation-thread temporal grounding land in
> Phases 2–4 ([RFC 0021](../rfcs/0021-persona-temporal-awareness.md);
> v0.4.0).

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

> **Chat is currently outside cost tracking.** The chat dispatch path
> ([internal/server/chat_handler.go:30–155](../../internal/server/chat_handler.go#L30-L155) →
> [internal/executor/chat.go:75–145](../../internal/executor/chat.go#L75-L145)) does not yet
> call `BudgetEnforcer.CheckBudget` and chat replies are not recorded by the
> cost reporter. Per-agent `max_llm_calls` and `max_tokens` from
> `config/agents.yaml` still bound a single chat turn (the limits are
> enforced inside the action loop), but daily / per-workflow USD caps do
> **not** apply to `persatrix chat` traffic. Wiring chat into
> `BudgetEnforcer` is a planned follow-up.

---

## 4. Chatting with a persona agent

v0.2.1 adds a synchronous human-to-agent chat surface so you can talk to a
persona agent from a terminal instead of authoring a workflow. The CLI
command is `persatrix chat <agent_id>`; under the hood it calls the new
REST endpoint `POST /api/v1/agents/{id}/chat` on the orchestrator, which
dispatches a `SendChatMessage` gRPC call to the agent.

> **Spec-level detail** for the chat surface lives in
> [RFC 0016](../rfcs/0016-human-participant-chat-interface.md). The
> manual tests exercising it are
> [MT-CHAT-001](../manual-tests/MT-CHAT-001.md) through
> [MT-CHAT-004](../manual-tests/MT-CHAT-004.md).

### Starting the stack

Chat needs the orchestrator and the target persona agent both running.

> **Prerequisite — `ANTHROPIC_API_KEY`.** Chat is a live LLM round-trip, not
> a stub, so the agent process must see `ANTHROPIC_API_KEY` in its
> environment. Export it in the shell that launches the agent (host path)
> or pass it through to the agent container (Docker path) before running
> the commands below — otherwise the agent will start but every chat turn
> will fail.

The simplest local stack is Docker:

```bash
make docker-build
make docker-up   # orchestrator + all agents declared in config/agents.yaml
make build-cli   # → cli/target/release/persatrix
```

If you would rather run the orchestrator and an agent on the host:

```bash
make run                                 # orchestrator on :8080
make run-agent AGENT=ember-owl PORT=50051  # in a second terminal
```

The orchestrator binds to `:8080` by default; override with `--server` on
the CLI if you have it elsewhere.

### Opening a chat

```bash
persatrix chat ember-owl
```

The REPL connects to `http://localhost:8080/api/v1/agents/ember-owl/chat`
and prints a banner, then prompts for input:

```text
Connected to ember-owl. Type exit or Ctrl-C to quit.
You: How would you triage a flaky integration test?
ember-owl: ...
```

Flags:

- `--user <id>` — the participant identity to attribute messages to.
  Defaults to your OS username, normalized to the resource-ID format
  (lowercase alphanumeric + hyphens). The resolver reads `USERNAME`
  (Windows) then `USER` (POSIX) and falls back to `local` if neither is
  set, so minimal containers without those env vars get a usable default
  rather than an error
  ([cli/src/main.rs:240–250](../../cli/src/main.rs#L240-L250)). The same
  resource-ID rules are enforced server-side and on the CLI before any
  request is sent
  ([cli/src/commands/chat.rs:22](../../cli/src/commands/chat.rs#L22)).
- `--server <url>` — orchestrator base URL. Defaults to
  `http://localhost:8080`.

Type `exit`, send EOF (Ctrl-D on Unix, Ctrl-Z + Enter on Windows), or hit
Ctrl-C to quit. The REPL handles Ctrl-C gracefully via a shared flag rather
than crashing.

### Session persistence

The REST contract carries a `session_id` field on every request and reply.
The first request from the REPL sends an empty `session_id`; the server
allocates a new one and returns it on the response. Subsequent requests
reuse that ID for the lifetime of the REPL process, so the agent sees one
continuous conversation per `persatrix chat` invocation.

`session_id` is currently used as a client-side conversation token only —
it is recorded into `event.metadata["session_id"]` and flows into episodic
memory records, but no agent-side logic branches on it. Cross-session
threading and session-scoped memory queries are deferred to v0.3.0
(see [RFC 0016 §Non-goals](../rfcs/0016-human-participant-chat-interface.md)).

### Relationship memory evolution

User messages are processed by the persona runtime as `CHANNEL_MESSAGE`
events with `participant_type: "user"` in the event metadata (the event
name was `MESSAGE_RECEIVED` prior to RFC 0011 PR 4a-ii-α; the
`participant_type` metadata path is unchanged under either name). The
relationship memory tier (§2) treats the user as a first-class participant
thanks to the v0.2.1 generalization — `RelationshipMemory` now keys on
`(participant_id, participant_type)` pairs rather than agent-only entity
IDs.

After a few exchanges with `--user alice` you can inspect the trust score
and interaction count the same way you would for an agent–agent
relationship (see [MT-CHAT-004](../manual-tests/MT-CHAT-004.md) for the
recorded procedure). Trust starts at the seed value declared in
`agents.yaml` (or `0.5` neutral if unseeded) and updates via
`record_interaction` / `update_trust` calls in the action loop, exactly the
same mechanism that drives agent–agent trust.

User identity itself is persisted in a new `users` table in the agent
SQLite database via `UserStore`
([agents/participant.py](../../agents/participant.py)), so the same
`--user` ID is recognised across agent restarts.

### Known limitations

The v0.2.1 chat surface is intentionally minimal. The following are
deferred (matched against
[RFC 0016 §Non-goals](../rfcs/0016-human-participant-chat-interface.md)):

| Area | v0.2.1 behaviour | Deferred to |
|------|------------------|-------------|
| Concurrency | Single `UserParticipant` per session | v0.3.0 (RFC 0011) |
| Authentication | Sessions are local; `--user` is caller-supplied | v0.3.0 (RFC 0009) |
| Streaming | Synchronous request-response, no SSE | future RFC |
| Agent-initiated messages | No notification path; agents can only reply within an active session | future RFC |
| Channel routing | Point-to-point user ↔ agent only | v0.3.0 (RFC 0011) |
| Chat history API | No `GET /chat/history` endpoint; inspect via memory tools | v0.2.2 candidate |
| Cost gating | Chat dispatch bypasses `BudgetEnforcer` (see callout in §3) | follow-up |
| Rate limiting | No per-user rate limit on the chat endpoint | v0.3.0 (RFC 0009) |
| Web / GUI | CLI only | future RFC |

> **Operational warning — no authentication.** Because `--user` is
> caller-supplied and the chat endpoint performs no authentication in
> v0.2.1, do not expose the orchestrator chat endpoint on a network shared
> with untrusted callers. Treat `persatrix chat` as a local-developer
> surface until RFC 0009 lands.

The chat endpoint enforces a 4000-character message ceiling (counted in
runes, not bytes, so emoji and CJK text are measured consistently) and
rejects unknown agent IDs with `404`. Both behaviours are exercised by
[MT-CHAT-001](../manual-tests/MT-CHAT-001.md).

---

## 5. Observability (v0.2.3)

v0.2.3 ships [RFC 0018](../rfcs/0018-structured-logging-framework.md)
(structured JSON logging) and
[RFC 0019](../rfcs/0019-opentelemetry-completion.md) (OpenTelemetry
completion) together. Persona agents emit the resulting signals without
any per-agent configuration — the `observability.tracing` and
`observability.logging` modules initialise on agent startup.

- **Structured JSON logs** on a versioned schema (`schema_version: "1"`).
  Every log line from the agent tick loop, event handler, and memory
  store carries the reserved correlation IDs (`execution_id`, `agent_id`,
  `workflow_id`, `step_id`) populated from gRPC metadata, plus
  `trace_id` / `span_id` when a span is active. Set
  `PERSATRIX_LOG_FORMAT=pretty` for a human-readable console renderer
  while developing. Full schema: [docs/observability.md](../observability.md).
- **Distributed traces** across the Go orchestrator and the Python
  agent. Each tick fires an `agent.persona.tick` span; each inbound
  event fires an `agent.persona.event` span; memory operations produce
  `agent.memory.episodic.{recall,remember}` and
  `agent.memory.relationship.{lookup,update}`; LLM calls emit
  `agent.llm.call` with OTEL
  [Gen-AI semantic conventions](../observability.md#10-span-conventions-rfc-0019-pr-2)
  (`gen_ai.system`, `gen_ai.request.model`, `gen_ai.usage.*`). Open
  <http://localhost:16686> and search by tag
  `persatrix.workflow_id=<your-workflow>` to see the full trace tree.
- **`persatrix logs <execution_id>`** snapshots or tails a specific run
  — `--follow` for live SSE streaming, `--trace <trace_id>` to pull
  every record emitted under a specific trace ID across processes.
  Details in
  [docs/observability.md § 12](../observability.md#12-operations-persatrix-logs-rfc-0018-pr-6).

The signal-flow overview — log shipper, OTLP pipeline, and baggage
propagation across the gRPC boundary — is drawn in
[docs/diagrams/observability-stack.md](../diagrams/observability-stack.md).

---

## 6. Inspecting and customising the persona prompt (v0.3.0)

Every fragment that gets concatenated into a persona's system prompt lives
in a discrete file under
[`prompts/runtime/persona/sections/`](../../prompts/runtime/persona/sections/).
You can read them, diff them, swap them, or template them without touching
Python ([RFC 0022](../rfcs/0022-persona-prompt-section-templating.md)).

| Section | File | What it controls |
|---------|------|------------------|
| Identity | [`identity.md`](../../prompts/runtime/persona/sections/identity.md) | Who the persona is — name, role, title |
| Background | [`background.md`](../../prompts/runtime/persona/sections/background.md) | Persona's narrative history |
| Behavior | [`behavior.md`](../../prompts/runtime/persona/sections/behavior.md) + [`behavior-dimensions.yaml`](../../prompts/runtime/persona/sections/behavior-dimensions.yaml) | Rendering of the five-dimension `behavior` block |
| Goals | [`goals.md`](../../prompts/runtime/persona/sections/goals.md) | How `goals.{primary,secondary,hidden}` are surfaced |
| Quirks | [`quirks.md`](../../prompts/runtime/persona/sections/quirks.md) | How quirks are introduced into the prompt |
| Now-anchor | [`now-anchor.md`](../../prompts/runtime/persona/sections/now-anchor.md) | RFC 0021 Phase 1 temporal anchor — current time + last interaction |
| Current state | [`current-state.md`](../../prompts/runtime/persona/sections/current-state.md) | Stamina / energy / idle state surfaced at prompt time |

Sections render in a fixed order (set by the runtime, not the section
files). The wins:

- **Auditable** — `git log -- prompts/runtime/persona/sections/` is the
  history of every persona-prompt change since v0.3.0.
- **Diffable across versions** — promotes prompt edits out of code review
  bikeshed and into a reviewable surface.
- **Hot-swappable per persona** — alternate sections can be referenced
  per-persona in `config/agents.yaml` without re-rendering or re-deploying.

Prompt sections **are part of the public surface** — third-party tools that
inspect or override prompt assembly should pin against this directory
shape.

---

## 7. Where to go next

- **RFC 0005 — Persona Agent & Memory System**:
  [docs/rfcs/0005-persona-agent-memory.md](../rfcs/0005-persona-agent-memory.md).
  Design rationale for the tick loop, behaviour dimensions, and memory tiers.
- **RFC 0006 — Efficiency & Execution Limits**:
  [docs/rfcs/0006-efficiency-execution-limits.md](../rfcs/0006-efficiency-execution-limits.md).
  Design rationale for the three-level cascade, budget enforcement, response
  cache, and derived deadlines.
- **RFC 0016 — Human Participant & Chat Interface**:
  [docs/rfcs/0016-human-participant-chat-interface.md](../rfcs/0016-human-participant-chat-interface.md).
  Design rationale for the `Participant` Protocol, the chat REST/gRPC surface,
  and the v0.2.1 non-goals.
- **Manual tests** exercising the persona and memory surfaces:
  [docs/manual-tests/README.md](../manual-tests/README.md) — in particular the
  `MT-PERSONA-*`, `MT-MEMORY-*`, and `MT-COST-*` suites.
- **Architecture diagrams** for persona runtime and memory tier interaction:
  [docs/diagrams/persona-runtime.md](../diagrams/persona-runtime.md) and
  [docs/diagrams/memory-architecture.md](../diagrams/memory-architecture.md)
  (see [docs/diagrams/README.md](../diagrams/README.md) for the full index).
