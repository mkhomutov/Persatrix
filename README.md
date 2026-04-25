# Persatrix

[![CI](https://github.com/mkhomutov/Persatrix/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/mkhomutov/Persatrix/actions/workflows/ci.yml)
[![License: BUSL-1.1](https://img.shields.io/badge/license-BUSL--1.1-blue.svg)](LICENSE)
[![Go 1.24+](https://img.shields.io/badge/Go-1.24%2B-00ADD8?logo=go&logoColor=white)](https://go.dev/)
[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Rust 1.80+](https://img.shields.io/badge/Rust-1.80%2B-DEA584?logo=rust&logoColor=white)](https://www.rust-lang.org/)

A general-purpose **agent society engine** — a runtime for creating, connecting, and
observing groups of AI agents that behave as individuals within organizational or
social structures.

---

## Why Persatrix

Existing agent frameworks treat agents as task executors in a workflow. This works
well for structured problems — research a question, write a report, review code. It
does not capture how real collaboration works.

Real collaboration is not an assembly line. It is a society of individuals who know
each other, trust each other differently, and act on their own initiative. A research
team that works together for a year has shared history. A company has organizational
structure. A classroom has social dynamics.

Persatrix is the runtime for building software systems that work like this. Agents as
persistent individuals with memory, personality, and evolving relationships — not
tasks in a workflow.

---

With **v0.2.0** (Persona Core — Persatrix's first public release) you can:

- Define a persona agent with a personality, a role, and explicit behaviour dimensions.
- Launch it as a long-running gRPC service with a tick-driven autonomy loop.
- Give it three tiers of memory — episodic, relationship, and working — that persist
  across restarts.
- Bound its cost and execution with per-run token budgets, LLM-call limits, and a
  shared response cache.
- Submit classical task workflows from v0.1 alongside personas — the orchestrator
  handles both through the same gRPC surface.

**v0.2.1** (Human Participant & Chat Interface) adds:

- A `persatrix chat <agent_id>` REPL for talking to a persona agent from a
  terminal.
- A first-class `Participant` abstraction that treats human users alongside agents.
- Persistent user identity — the agent remembers who you are across restarts and
  builds a relationship with you over repeated conversations.

---

## What's in v0.2.0

| Capability | Where it lives | Spec |
|------------|----------------|------|
| **Persona agents** — tick loop, structured behaviour, event dispatch | [agents/persona_runtime/](agents/persona_runtime/), [agents/persona.py](agents/persona.py) | [RFC 0005](docs/rfcs/0005-persona-agent-memory.md) |
| **Three-tier memory** — episodic (SQLite), relationship, working | [agents/memory/](agents/memory/) | [RFC 0005](docs/rfcs/0005-persona-agent-memory.md) |
| **Cost tracking & budgets** — token counting, per-run enforcement, `GET /api/v1/cost/summary` | [internal/cost/](internal/cost/), [internal/server/cost_handlers.go](internal/server/cost_handlers.go) | [RFC 0006](docs/rfcs/0006-efficiency-execution-limits.md) |
| **Execution limits** — `max_llm_calls`, derived per-call deadlines, shared retry budget | [internal/executor/](internal/executor/), [internal/scheduler/](internal/scheduler/) | [RFC 0006](docs/rfcs/0006-efficiency-execution-limits.md) |
| **Response cache** — in-memory cache keyed on prompt + config | [internal/cost/cache.go](internal/cost/cache.go) | [RFC 0006](docs/rfcs/0006-efficiency-execution-limits.md) |
| **OTEL tracing** — spans flowing through orchestrator → agents, visible in Jaeger | [internal/observability/](internal/observability/) | — |

> **Upgrade note from v0.1 baseline:** `max_llm_calls` default changed from `10` to
> `5`. See [CHANGELOG.md](CHANGELOG.md).

---

## What's added in v0.2.1

| Capability | Where it lives | Spec |
|------------|----------------|------|
| **`Participant` abstraction + `UserParticipant`** — persistent human identity stored in the agent SQLite database | [agents/participant.py](agents/participant.py) | [RFC 0016](docs/rfcs/0016-human-participant-chat-interface.md) |
| **Memory generalization** — `RelationshipMemory` and `EpisodicMemory` now track user-agent exchanges | [agents/memory/](agents/memory/) | [RFC 0016](docs/rfcs/0016-human-participant-chat-interface.md) |
| **Chat REST endpoint** — `POST /api/v1/agents/{id}/chat` for synchronous user→agent messages | [internal/server/](internal/server/) | [RFC 0016](docs/rfcs/0016-human-participant-chat-interface.md) |
| **`SendChatMessage` gRPC RPC** — orchestrator→agent chat dispatch | [agents/server_servicers.py](agents/server_servicers.py), [internal/executor/](internal/executor/) | [RFC 0016](docs/rfcs/0016-human-participant-chat-interface.md) |
| **`persatrix chat` CLI** — interactive REPL for chatting with a persona agent | [cli/src/commands/chat.rs](cli/src/commands/chat.rs) | [RFC 0016](docs/rfcs/0016-human-participant-chat-interface.md) |

See the [chat walkthrough in the persona-agents guide](docs/guides/persona-agents.md#4-chatting-with-a-persona-agent)
for an end-to-end run.

---

## What's added in v0.2.2

v0.2.2 (Bounded Persona Memory Injection) is an internal hardening release —
no new REST endpoints, gRPC RPCs, or CLI commands. It ships
[RFC 0017](docs/rfcs/0017-persona-memory-injection-budget.md) in three layers:

| Capability | Where it lives | Spec |
|------------|----------------|------|
| **Per-event memory token budget** — `MemoryBudget` allocator caps how much episodic + relationship + notes context a persona injects into a single event (default 1500 tokens) | [agents/persona_runtime/memory_context.py](agents/persona_runtime/memory_context.py) | [RFC 0017 §B](docs/rfcs/0017-persona-memory-injection-budget.md#b-memory-budget-allocator) |
| **Relevance threshold on recall** — `EpisodicMemory.recall` and `recall_notes` accept a `min_score` parameter; below-threshold matches are dropped before truncation | [agents/memory/episodic.py](agents/memory/episodic.py), [agents/memory/episodic_queries.py](agents/memory/episodic_queries.py) | [RFC 0017 §C](docs/rfcs/0017-persona-memory-injection-budget.md#c-relevance-threshold) |
| **Empty-context TICK short-circuit** — autonomous TICK events with zero admitted memory, no active goal, and no pending conversation turn skip the LLM call entirely and increment `idle_count` | [agents/persona_runtime/action_loop.py](agents/persona_runtime/action_loop.py) | [RFC 0017 §F](docs/rfcs/0017-persona-memory-injection-budget.md#f-empty-context-tick-short-circuit) |

The empty-context TICK short-circuit closes the cost-leak class described in
the [Cost Warning](#%EF%B8%8F-cost-warning--read-before-running) below — bored
autonomous personas no longer burn tokens on context-free LLM calls.

Operator impact: defaults are conservative; no config change is required. See
the [v0.2.2 release checklist §3.1](docs/v0.2.2-release-checklist.md#31-required-upgrade-notes-in-changelog)
for the full upgrade notes.

---

## What's added in v0.2.3

v0.2.3 (Observability Foundation) ships
[RFC 0018](docs/rfcs/0018-structured-logging-framework.md) (Structured Logging
Framework) and [RFC 0019](docs/rfcs/0019-opentelemetry-completion.md)
(OpenTelemetry Completion) together. Everything below is visible end-to-end
from a single `docker compose up` against the reference stack.

| Capability | Where it lives | Spec |
|------------|----------------|------|
| **Structured JSON logs** — versioned schema (`schema_version: "1"`) across Go (zap), Python (structlog), and CLI; cross-process correlation IDs; redactor hook; `PERSATRIX_LOG_FORMAT=pretty` for local debugging | [internal/observability/zapenc/](internal/observability/zapenc/), [agents/observability/logging.py](agents/observability/logging.py), [docs/observability.md](docs/observability.md) | [RFC 0018](docs/rfcs/0018-structured-logging-framework.md) |
| **Distributed OTEL traces** — spans flow from REST handler through the scheduler and executor into the agent's LLM and tool calls; OTEL Gen-AI semantic conventions on `agent.llm.call`; Span Links for event→tick and sub-agent causality | [internal/observability/](internal/observability/), [agents/observability/tracing.py](agents/observability/tracing.py) | [RFC 0019 § C–E](docs/rfcs/0019-opentelemetry-completion.md) |
| **OTLP metrics with exemplars** — Go + Python counters, histograms, and gauges; histogram exemplars carry the active span's `trace_id` so dashboards click through into the originating trace | [internal/observability/metrics/](internal/observability/metrics/), [agents/observability/metrics.py](agents/observability/metrics.py) | [RFC 0019 § F](docs/rfcs/0019-opentelemetry-completion.md#f-metrics) |
| **W3C Baggage propagation** — `persatrix.workflow_id` plus reserved correlation IDs cross the gRPC boundary via `CompositePropagator(TraceContext + Baggage)` and are readable inside agent handlers | [internal/observability/grpcmeta/](internal/observability/grpcmeta/), [agents/observability/](agents/observability/) | [RFC 0018 § D](docs/rfcs/0018-structured-logging-framework.md#d-cross-process-correlation), [RFC 0019 § E](docs/rfcs/0019-opentelemetry-completion.md) |
| **Tail-sampling Collector pipeline** — reference `otel-collector.yaml` keeps all ERROR traces, traces ≥ 5 s, and every trace tagged `persatrix.workflow_id`; samples 1 % of the remaining (autonomous-tick) traffic; fans out to Jaeger / Prometheus / Loki | [config/observability/otel-collector.yaml](config/observability/otel-collector.yaml) | [RFC 0019 § H](docs/rfcs/0019-opentelemetry-completion.md#h-sampling-back-pressure-and-the-collector-pipeline) |
| **`persatrix logs` CLI** — snapshot + SSE `--follow`, server-side `--level` / `--since` / `--workflow` filters, `--trace <id>` cross-execution correlation, automatic SSE reconnect with backoff | [cli/src/commands/logs.rs](cli/src/commands/logs.rs), [internal/observability/logbuffer/](internal/observability/logbuffer/) | [RFC 0018 PR 6](docs/observability.md#12-operations-persatrix-logs-rfc-0018-pr-6) |

For the operational reference — log schema, span inventory, Collector
pipeline, `persatrix logs` usage — see
[docs/observability.md](docs/observability.md). For a signal-flow
overview (log shipper + OTLP pipeline + baggage propagation across the
gRPC boundary) see
[docs/diagrams/observability-stack.md](docs/diagrams/observability-stack.md).

> **Upgrade notes (summary).** v0.2.3 unpublishes Jaeger's host-facing
> OTLP ports (the Collector now owns `:4317`/`:4318` on the host),
> switches the Python OTLP exporter transport from gRPC to HTTP, renames
> the Go `internal/telemetry` package to `internal/observability`, and
> renames reserved zap correlation-ID keys (`runID`/`executionID` →
> `execution_id`, `agentID` → `agent_id`, etc.) to the RFC 0018 schema.
> The [CHANGELOG.md](CHANGELOG.md) `[0.2.3]` Upgrade Notes subsection is
> the canonical source; downstream log shippers, `jq` queries, and
> dashboards filtering on the old keys must be updated.

---

## ⚠️ Cost Warning — Read Before Running

Persatrix uses commercial LLM APIs (Anthropic by default; the model is selected
per agent in [config/agents.yaml](config/agents.yaml)) and runs persona agents
with autonomous tick loops that consume API tokens continuously while the
agent process is alive.

**This is experimental software (pre-1.0, BUSL-1.1). It has bugs. Some bugs
may cost you money.**

During testing of v0.2.1 the author accidentally left a persona agent running
with a faulty empty-context idle check and lost roughly $35 USD in API costs
before noticing. v0.2.2 ships the empty-context tick short-circuit
([RFC 0017](docs/rfcs/0017-persona-memory-injection-budget.md)) that
fixes that specific bug. Other bugs with similar cost implications almost
certainly exist.

**Before running Persatrix, every user must:**

1. **Set hard spending limits at your LLM provider's billing page.** This is
   the authoritative safeguard against runaway costs. Persatrix's own budget
   controls (`max_llm_calls` per agent, `max_daily_usd` in
   [config/optimization.yaml](config/optimization.yaml), per-workflow token
   budgets) are best-effort and should never be your only protection.

2. **Configure billing alerts at your LLM provider** so you are notified
   immediately if spending exceeds expected thresholds.

3. **Start with short test sessions.** Stop persona agents explicitly when
   you are done — kill the `make run-agent` process or the `agent-*` Docker
   Compose service. Do not rely on `idle_after_ticks` or the empty-context
   short-circuit alone.

4. **Review [config/agents.yaml](config/agents.yaml) and
   [config/optimization.yaml](config/optimization.yaml) and set conservative
   limits** appropriate to your budget — `max_llm_calls` per agent,
   `tick_interval_seconds` (longer = cheaper), `max_actions_per_tick`,
   `idle_after_ticks`, and the global `max_daily_usd` budget.

5. **Monitor your provider's usage dashboard during initial runs.** Do not
   assume budgets are being enforced correctly until you have verified
   against your provider's authoritative numbers. Persatrix exposes
   `GET /api/v1/cost/summary` for in-process accounting, but that is
   independent from your provider's billing.

Persatrix is distributed under [BUSL 1.1](LICENSE). No warranty is provided.
The authors are not liable for API costs, lost data, or any other damages
arising from use. Use at your own risk. See
[SECURITY.md § Responsible Use](SECURITY.md#responsible-use) for the broader
framing.

---

## Quick Start

### Prerequisites

- Go 1.24+
- Python 3.11+
- Rust 1.80+ (for CLI)
- Protobuf compiler (`protoc`)
- Docker & Docker Compose (optional, for local stack)
- **Windows only:** `make` — install via [GnuWin32](https://gnuwin32.sourceforge.net/packages/make.htm) or `winget install GnuWin32.Make`, then add `C:\Program Files (x86)\GnuWin32\bin` to your PATH

### Setup

```bash
# Clone
git clone https://github.com/mkhomutov/Persatrix.git
cd Persatrix

# Configure
cp .env.example .env
# Edit .env with your ANTHROPIC_API_KEY

# Build everything (proto + Go + Rust)
make all

# Install Python agent dependencies
make build-agents

# Validate config
make validate
```

### Run with Docker Compose

```bash
docker compose up -d

# Orchestrator API:        http://localhost:8080
# Jaeger UI (traces):      http://localhost:16686
# Prometheus UI (metrics): http://localhost:9091
# Loki HTTP API (logs):    http://localhost:3100
# OTEL Collector (OTLP):   localhost:4317 (gRPC) / 4318 (HTTP)
```

All three observability backends are dev-only; the OpenTelemetry Collector
in front (configuration in
[`config/observability/otel-collector.yaml`](config/observability/otel-collector.yaml))
applies the tail-sampling pipeline from
[RFC 0019 § H](docs/rfcs/0019-opentelemetry-completion.md#h-sampling-back-pressure-and-the-collector-pipeline).
Production operators run their own Collector and point it at their own
backends — fork the reference config rather than relying on the dev images.

### Run a Workflow (v0.1 surface)

```bash
# Via CLI
persatrix run workflows/feature-builder.yaml --input "Build a REST API for user management"

# Via API
curl -X POST http://localhost:8080/api/v1/workflows/run \
  -H "Content-Type: application/json" \
  -d '{"workflow": "feature-builder", "input": "Build a REST API for user management"}'
```

### Local Logging — `PERSATRIX_LOG_FORMAT=pretty`

Persatrix emits structured JSON logs by default
(see [docs/observability.md](docs/observability.md), `schema_version: "1"`).
For local debugging, swap to a colourised human-readable renderer with one env var:

```bash
# Pretty console output (Go orchestrator + Python agents)
PERSATRIX_LOG_FORMAT=pretty make run
PERSATRIX_LOG_FORMAT=pretty make run-agent AGENT=ember-owl

# Default (JSON) — leave the env var unset.  This is also what CI, Docker
# Compose, and the future `persatrix logs` endpoint consume.
make run
```

Pretty mode is a developer affordance and is **not** a stable wire format.
Production deployments must leave `PERSATRIX_LOG_FORMAT` unset.

### Stream Logs — `persatrix logs` (v0.2.3)

With the orchestrator running, the CLI can query its in-memory log buffer or
tail it over SSE. The first positional argument is an execution ID; `_` is the
wildcard for the chronological cross-execution merged view. Server-side filters
keep the terminal quiet; `--trace` correlates across orchestrator and agent.

```bash
# Snapshot — cross-execution merge, most recent lines
persatrix logs _

# Follow — live SSE stream, auto-reconnects with backoff
persatrix logs _ --follow

# Filter server-side: level, time window, workflow
persatrix logs _ --follow --level WARN --since 5m
persatrix logs _ --workflow 01HXY...

# Correlate across orchestrator and agent by trace id (client-side filter)
persatrix logs _ --trace 4bf92f3577b34da6a3ce929d0e0e4736

# Scope to a single execution
persatrix logs 01HXY... --follow
```

See [docs/observability.md § 12](docs/observability.md#12-operations-persatrix-logs-rfc-0018-pr-6)
for the full flag reference and buffer sizing knobs.

### Run a Persona Agent (v0.2.0)

Personas are declared in [config/agents.yaml](config/agents.yaml). A worked example
is shipped with the repo — `ember-owl`, a semi-autonomous "VP of Engineering"
persona with structured behaviour, quirks, and memory configuration.

```bash
# 1. Start the orchestrator (if not already running via compose)
make run

# 2. Launch the persona as a gRPC service (separate terminal)
make run-agent AGENT=ember-owl

# 3. Ping it through the orchestrator
persatrix agent test --persona ember-owl

# 4. Inspect its registration and status
persatrix agent info ember-owl
```

Live tick-loop output — decisions, memory writes, tool calls — streams in the
terminal where `make run-agent` (or the `agent-*` compose service) is running.
The persona's tick loop fires every `autonomy.tick_interval_seconds`, reads
episodic and relationship memory, decides on up to `autonomy.max_actions_per_tick`
actions, and writes results back. State survives process restarts.

### Chat with a Persona Agent (v0.2.1)

With the orchestrator and the agent running (steps 1–2 above, or via Docker
Compose), open a chat session from another terminal:

```bash
# Interactive REPL — type messages, receive replies, `exit` to quit
persatrix chat ember-owl

# Identify yourself with a stable user id so the agent recognises you next time
persatrix chat ember-owl --user alice
```

Under the hood the CLI POSTs each message to
`POST /api/v1/agents/{id}/chat`, the orchestrator dispatches a `SendChatMessage`
gRPC call to the agent, and the agent's reply is rendered in the terminal.
User identity, conversation episodes, and the trust score on the user-agent pair
all persist across agent restarts.

For a full walkthrough — including session continuity and relationship
evolution — see
[the chat section of the persona-agents guide](docs/guides/persona-agents.md#4-chatting-with-a-persona-agent).

### Inspect Cost & Budget (v0.2.0)

```bash
# Token usage and cost for the most recent runs
curl http://localhost:8080/api/v1/cost/summary
```

Budgets are enforced per workflow run: exceeding `max_tokens` or `max_llm_calls`
aborts the run with a structured error before further spend accumulates.

### Run Tests

```bash
make test               # all tests
make test-go            # Go unit tests
make test-python        # Python agent tests
make test-integration   # end-to-end tests
```

---

## Architecture

```
CLI (Rust) ── REST ──► Orchestrator (Go) ── gRPC ──► Agents (Python)
                            │                            │
                            │                      LLM APIs, tools,
                      workflow planning,           three-tier memory
                      scheduling, state,
                      cost, OTEL spans
```

**Go orchestrator** (`internal/`) owns workflow planning, stage scheduling, agent
registry, state, cost, telemetry, and security. It never calls an LLM directly.

**Python agents** (`agents/`) own LLM interaction, tool execution, persona
behaviour, memory, and sub-agent spawning. Each agent is a gRPC service.

**Rust CLI** (`cli/`) is a thin REST client to the orchestrator. All business
logic is server-side.

**gRPC/protobuf** (`proto/`) is the cross-language contract. Changes go through
an RFC.

For architecture diagrams — see [docs/diagrams/](docs/diagrams/) for the
full index:
[system overview](docs/diagrams/system-overview.md),
[component architecture](docs/diagrams/component-architecture.md),
[workflow execution](docs/diagrams/workflow-execution.md),
[persona runtime](docs/diagrams/persona-runtime.md), and
[memory architecture](docs/diagrams/memory-architecture.md)
(Mermaid source embedded in `.md` files; renders in any Mermaid-aware viewer
such as GitHub or VS Code).

## Project Structure

```
Persatrix/
├── cmd/orchestrator/       Go server entry point
├── internal/               Go packages
│   ├── planner/            YAML → DAG, cycle detection
│   ├── scheduler/          Stage-level run driver
│   ├── executor/           gRPC dispatch + retry
│   ├── registry/           Agent lookup
│   ├── state/              Workflow run tracking
│   ├── server/             REST API + SSE
│   └── cost/               Token counting, budgets, response cache (v0.2.0)
├── proto/                  Protobuf definitions
├── agents/                 Python agent runtime (persatrix_agents package)
│   ├── base.py             BaseAgent ABC
│   ├── persona.py          Persona agent entrypoint
│   ├── persona_runtime/    Memory context, action loop, state persistence
│   ├── memory/             Episodic, relationship, working memory (v0.2.0)
│   ├── tools/              @tool registry, built-ins, sandbox
│   └── server.py           gRPC servicer
├── cli/                    Rust CLI
├── config/                 YAML configuration (schema in schemas/)
├── workflows/              Workflow definitions
├── docs/                   Specs, RFCs, diagrams, guides, manual tests
└── docker-compose.yaml     Local development stack
```

---

## Roadmap

| Version | What a user can do | Status |
|---------|-------------------|--------|
| **v0.1** | Submit YAML workflows, orchestrate task agents via gRPC, poll status via REST | ✅ Complete — internal baseline |
| **v0.2.0** | Run persistent AI agents with personas, memory, and cost-bounded execution from a terminal | ✅ First public release |
| **v0.2.1** | Talk to a persona agent from your terminal — the agent remembers you and responds in character | ✅ Released |
| **v0.2.2** | Bounded, predictable per-event memory injection for persona agents — structural cost-leak fix unblocking RFC 0008 | ✅ Released |
| **v0.2.3** | Observe your agent society end-to-end — structured JSON logs on a versioned schema, distributed OTEL traces with Gen-AI conventions, OTLP metrics with exemplars, `persatrix logs` CLI, and a tail-sampling Collector pipeline | ✅ Released |
| **v0.3** | Give agents a shared channel and watch them talk, negotiate, and form opinions over time | 📋 Planned |
| **v0.4** | Define a team, lab, or company with roles and hierarchy — and let it run | 📋 Planned |
| **v0.5** | Bridge your agent society into Slack, Discord, or email | 📋 Planned |
| **v0.6** | Run agent societies across multiple nodes and networks | 📋 Planned |

See [ROADMAP.md](ROADMAP.md) for PR-level progress, RFC status, and per-component
completion.

---

## Documentation

- [Roadmap & Progress](ROADMAP.md)
- [Changelog](CHANGELOG.md)
- [Persona agents & memory guide](docs/guides/persona-agents.md) — declaring personas, memory tiers, cost budgets
- [Architecture diagrams](docs/diagrams/README.md) — system, components, workflow, persona runtime, memory tiers
- [Manual test suite](docs/manual-tests/README.md)
- [RFCs](docs/rfcs/README.md) — engineering design docs
- [Development workflow](docs/development-workflow.md)
- [Branching strategy](docs/BRANCHING.md)
- [MVP specification](docs/ai-agents-orchestration-spec.md)
- [Extension specification](docs/persatrix-extension-spec.md)
- [Spec audit](docs/persatrix-spec-audit.md)

---

## Known Limitations in v0.2.0

- The MCP bridge ([agents/tools/mcp_bridge.py](agents/tools/mcp_bridge.py)) is
  scaffolded but not yet functional — tracked as a follow-up to v0.2. Agents that
  reference MCP tools emit a startup warning and run without them.

## Known Limitations in v0.2.1

- **Single user per session** — `persatrix chat` assumes one `UserParticipant` at
  a time; multi-user routing is part of RFC 0011 (v0.3.0).
- **No authentication** — chat sessions are local and the user id is
  caller-supplied; auth is RFC 0009 (v0.3.0).
- **Synchronous request-response only** — chat replies are not streamed in v0.2.1.
- **No agent-initiated messages** — agents reply to user messages but cannot
  spontaneously notify users; notification infrastructure is deferred.
- **No channel routing** — chat goes directly to a single agent; channels are
  RFC 0011 (v0.3.0).

## Known Limitations in v0.2.2

- **Per-event memory budget is a module constant** — `_MEMORY_BUDGET_TOKENS`
  (default 1500) is not yet exposed as a per-agent or per-event config field.
  Operators who need a different bound can patch the constant; a config-level
  knob will be considered in a future RFC if there is demand.
- **No new operator surface** — RFC 0017 is internal-only; there is no new
  REST endpoint, gRPC RPC, or CLI command. Existing v0.2.1 limitations
  (multi-user, auth, streaming, channel routing) all carry forward unchanged.

## Known Limitations in v0.2.3

- **`persatrix logs` restart durability is gated on sealing** — the
  disk-store warm-load code path is covered by unit tests
  (`TestWarmLoad_ResumesFromDisk` in
  [internal/observability/logbuffer/buffer_test.go](internal/observability/logbuffer/buffer_test.go))
  but production never calls `Buffer.Seal`, so a restart mid-run leaves
  no queryable history for runs that were in flight at the moment of
  restart. Tracked as an RFC 0018 PR 7 follow-up; see
  [MT-LOGS-001 Step 4](docs/manual-tests/MT-LOGS-001.md) and the
  [v0.2.3 execution report](docs/manual-tests/v0.2.3-execution-report.md).
- **Cross-process trace stitching is universal for `ExecuteTask`, not
  for every gRPC path** — the executor's `otelgrpc` client handler
  stitches orchestrator and agent spans into a single trace on the
  workflow dispatch path. Other gRPC paths whose caller is not routed
  through `otelgrpc.NewClientHandler()` may still land in a separate
  root trace. Exemplar-driven and `persatrix.workflow_id`-tagged lookups
  remain correct in either topology.
- **No per-agent operator dashboard or alerting rules ship with
  v0.2.3** — the reference compose stack is for local debugging.
  Production operators fork
  [config/observability/otel-collector.yaml](config/observability/otel-collector.yaml)
  and build dashboards against their own Prometheus + Jaeger + Loki
  install.
- **`MT-COST-002` (budget-exceed workflow abort) remains
  accepted-with-known-gap** — carried forward from v0.2 / v0.2.1 /
  v0.2.2; no budget-enforcement changes in v0.2.3. See the
  [v0.2.3 execution report](docs/manual-tests/v0.2.3-execution-report.md).
- **All v0.2.0 / v0.2.1 / v0.2.2 carry-forward limitations still
  apply** — MCP bridge scaffolded only, chat still single-user and
  synchronous with no auth, chat traffic bypasses `BudgetEnforcer`,
  per-event memory budget is a module constant. See the version-specific
  sections above.

---

## License

Persatrix is distributed under the Business Source License 1.1 (`BUSL-1.1`).
Production use is not granted under the default terms in this repository.
Each version transitions to Apache License, Version 2.0 four years after its
first public release.
See [LICENSE](LICENSE) for the full terms.

Third-party dependencies and their licenses are listed in
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
