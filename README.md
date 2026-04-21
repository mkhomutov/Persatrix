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

**v0.2.1** (Human Participant & Chat Interface) adds:

- A `persatrix chat <agent_id>` REPL for talking to a persona agent from a
  terminal.
- A first-class `Participant` abstraction that treats human users alongside agents.
- Persistent user identity — the agent remembers who you are across restarts and
  builds a relationship with you over repeated conversations.

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

## What's in v0.2.0

| Capability | Where it lives | Spec |
|------------|----------------|------|
| **Persona agents** — tick loop, structured behaviour, event dispatch | [agents/persona_runtime/](agents/persona_runtime/), [agents/persona.py](agents/persona.py) | [RFC 0005](docs/rfcs/0005-persona-agent-memory.md) |
| **Three-tier memory** — episodic (SQLite), relationship, working | [agents/memory/](agents/memory/) | [RFC 0005](docs/rfcs/0005-persona-agent-memory.md) |
| **Cost tracking & budgets** — token counting, per-run enforcement, `GET /api/v1/cost/summary` | [internal/cost/](internal/cost/), [internal/server/cost_handlers.go](internal/server/cost_handlers.go) | [RFC 0006](docs/rfcs/0006-efficiency-execution-limits.md) |
| **Execution limits** — `max_llm_calls`, derived per-call deadlines, shared retry budget | [internal/executor/](internal/executor/), [internal/scheduler/](internal/scheduler/) | [RFC 0006](docs/rfcs/0006-efficiency-execution-limits.md) |
| **Response cache** — in-memory cache keyed on prompt + config | [internal/cost/cache.go](internal/cost/cache.go) | [RFC 0006](docs/rfcs/0006-efficiency-execution-limits.md) |
| **OTEL tracing** — spans flowing through orchestrator → agents, visible in Jaeger | [internal/](internal/) | — |

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

# Orchestrator API:   http://localhost:8080
# Jaeger UI (traces): http://localhost:16686
```

### Run a Workflow (v0.1 surface)

```bash
# Via CLI
persatrix run workflows/feature-builder.yaml --input "Build a REST API for user management"

# Via API
curl -X POST http://localhost:8080/api/v1/workflows/run \
  -H "Content-Type: application/json" \
  -d '{"workflow": "feature-builder", "input": "Build a REST API for user management"}'
```

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

---

## License

Persatrix is distributed under the Business Source License 1.1 (`BUSL-1.1`).
Production use is not granted under the default terms in this repository.
Each version transitions to Apache License, Version 2.0 four years after its
first public release.
See [LICENSE](LICENSE) for the full terms.

Third-party dependencies and their licenses are listed in
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
