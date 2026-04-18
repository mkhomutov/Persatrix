# Persatrix

[![CI](https://github.com/mkhomutov/Persatrix/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/mkhomutov/Persatrix/actions/workflows/ci.yml)
[![License: BUSL-1.1](https://img.shields.io/badge/license-BUSL--1.1-blue.svg)](LICENSE)
[![Go 1.24+](https://img.shields.io/badge/Go-1.24%2B-00ADD8?logo=go&logoColor=white)](https://go.dev/)
[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Rust 1.80+](https://img.shields.io/badge/Rust-1.80%2B-DEA584?logo=rust&logoColor=white)](https://www.rust-lang.org/)

A general-purpose **agent society engine** — a runtime for creating, connecting, and
observing groups of AI agents that behave as individuals within organizational or
social structures.

With **v0.2.0** (Persona Core — Persatrix's first public release) you can:

- Define a persona agent with a personality, a role, and explicit behaviour dimensions.
- Launch it as a long-running gRPC service with a tick-driven autonomy loop.
- Give it three tiers of memory — episodic, relationship, and working — that persist
  across restarts.
- Bound its cost and execution with per-run token budgets, LLM-call limits, and a
  shared response cache.
- Submit classical task workflows from v0.1 alongside personas — the orchestrator
  handles both through the same gRPC surface.

---

## What's in v0.2

| Capability | Where it lives | Spec |
|------------|----------------|------|
| **Persona agents** — tick loop, structured behaviour, event dispatch | [agents/persona_runtime/](agents/persona_runtime/), [agents/persona.py](agents/persona.py) | [RFC 0005](docs/rfcs/0005-persona-agent-memory.md) |
| **Three-tier memory** — episodic (SQLite), relationship, working | [agents/memory/](agents/memory/) | [RFC 0005](docs/rfcs/0005-persona-agent-memory.md) |
| **Cost tracking & budgets** — token counting, per-run enforcement, `GET /api/v1/cost/summary` | [internal/cost/](internal/cost/), [internal/server/cost_handlers.go](internal/server/cost_handlers.go) | [RFC 0006](docs/rfcs/0006-efficiency-execution-limits.md) |
| **Execution limits** — `max_llm_calls`, derived per-call deadlines, shared retry budget | [internal/executor/](internal/executor/), [internal/scheduler/](internal/scheduler/) | [RFC 0006](docs/rfcs/0006-efficiency-execution-limits.md) |
| **Response cache** — in-memory cache keyed on prompt + config | [internal/cost/cache.go](internal/cost/cache.go) | [RFC 0006](docs/rfcs/0006-efficiency-execution-limits.md) |
| **OTEL tracing** — spans flowing through orchestrator → agents, visible in Jaeger | [internal/](internal/) | — |

> The MCP bridge ([agents/tools/mcp_bridge.py](agents/tools/mcp_bridge.py)) is
> scaffolded but not yet functional — tracked as a follow-up to v0.2. Agents that
> reference MCP tools emit a startup warning and run without them.

> **Upgrade note from v0.1 baseline:** `max_llm_calls` default changed from `10` to
> `5`. See [CHANGELOG.md](CHANGELOG.md).

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

# Orchestrator API:   http://localhost:8080
# Jaeger UI (traces): http://localhost:16686
```

### Run a Workflow (v0.1 surface)

```bash
# Via CLI
orch run workflows/feature-builder.yaml --input "Build a REST API for user management"

# Via API
curl -X POST http://localhost:8080/api/v1/workflows/run \
  -H "Content-Type: application/json" \
  -d '{"workflow": "feature-builder", "input": "Build a REST API for user management"}'
```

### Run a Persona Agent (v0.2)

Personas are declared in [config/agents.yaml](config/agents.yaml). A worked example
is shipped with the repo — `sarah-chen`, a semi-autonomous "VP of Engineering"
persona with structured behaviour, quirks, and memory configuration.

```bash
# 1. Start the orchestrator (if not already running via compose)
make run

# 2. Launch the persona as a gRPC service (separate terminal)
make run-agent AGENT=sarah-chen

# 3. Ping it through the orchestrator
orch agent test --persona sarah-chen

# 4. Inspect its registration and status
orch agent info sarah-chen
```

Live tick-loop output — decisions, memory writes, tool calls — streams in the
terminal where `make run-agent` (or the `agent-*` compose service) is running.
The persona's tick loop fires every `autonomy.tick_interval_seconds`, reads
episodic and relationship memory, decides on up to `autonomy.max_actions_per_tick`
actions, and writes results back. State survives process restarts.

### Inspect Cost & Budget (v0.2)

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

For the full set of architecture diagrams — system overview, persona runtime
loop, memory tiers, workflow execution — see [docs/diagrams/](docs/diagrams/)
(Mermaid source embedded in `.md` files; render in-place in any Mermaid-aware
viewer such as GitHub or VS Code).

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
│   └── cost/               Token counting, budgets, response cache (v0.2)
├── proto/                  Protobuf definitions
├── agents/                 Python agent runtime (persatrix_agents package)
│   ├── base.py             BaseAgent ABC
│   ├── persona.py          Persona agent entrypoint
│   ├── persona_runtime/    Memory context, action loop, state persistence
│   ├── memory/             Episodic, relationship, working memory (v0.2)
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
| **v0.2** | Run persistent AI agents with personas, memory, and cost-bounded execution from a terminal | 🚧 In Progress — first public release |
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
- [Architecture diagrams](docs/diagrams/)
- [Manual test suite](docs/manual-tests/README.md)
- [RFCs](docs/rfcs/README.md) — engineering design docs
- [Development workflow](docs/development-workflow.md)
- [Branching strategy](docs/BRANCHING.md)
- [MVP specification](docs/ai-agents-orchestration-spec.md)
- [Extension specification](docs/persatrix-extension-spec.md)
- [Spec audit](docs/persatrix-spec-audit.md)

---

## License

Persatrix is distributed under the Business Source License 1.1 (`BUSL-1.1`).
Production use is not granted under the default terms in this repository.
Each version transitions to Apache License, Version 2.0 four years after its
first public release.
See [LICENSE](LICENSE) for the full terms.
