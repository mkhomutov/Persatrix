# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What is Persatrix

An agent society engine — a runtime for creating, connecting, and observing groups of AI agents via organizational structures. Polyglot: **Go** orchestrator, **Python** agent runtime, **Rust** CLI, connected by **protobuf/gRPC**.

## Build & Test Commands

All commands are in the Makefile. Prefer Make targets over raw commands.

```bash
make all                    # proto + build (orchestrator + CLI)
make build-agents           # pip install -e ".[dev]" in agents/
make test                   # all suites (Go + Python + integration)
make lint                   # all linters (golangci-lint, ruff + mypy, clippy)
make validate               # YAML configs against JSON schemas
make run                    # build + run orchestrator
make run-agent AGENT=coder  # run a specific Python agent
```

### Running individual tests

```bash
# Single Go test
go test ./internal/planner -v -run TestPlannerParseWorkflow

# Single Python test
python3 -m pytest tests/unit/python/test_agents.py::test_agent_init -v

# Integration tests
python3 -m pytest tests/integration/ -v --tb=short -c agents/pyproject.toml
```

### Protobuf regeneration

After modifying `proto/*.proto`, run `make proto` to regenerate stubs into `internal/generated/` (Go) and `agents/generated/` (Python). Never edit generated files directly.

## Architecture

```
CLI (Rust)  <--REST-->  Orchestrator (Go)  <--gRPC-->  Agents (Python)
  cli/                   cmd/orchestrator/               agents/
                         internal/                        memory/
                                                          sub_agents/
                                                          tools/
```

**Component boundaries are strict:**
- **Go orchestrator** (`internal/`): workflow planning, scheduling, agent registry, state, cost, telemetry, security. No LLM logic.
- **Python agents** (`agents/`): LLM interaction, tool execution, persona behavior, memory, sub-agent spawning. Each agent is a gRPC service.
- **Rust CLI** (`cli/`): thin REST client to orchestrator. All business logic is server-side.
- **Protobuf** (`proto/`): cross-language gRPC contract. Changes require RFC review.

### Workflow execution flow

YAML workflow -> `YAMLPlanner` parses DAG + validates (cycle detection) -> topological sort into stages -> `Scheduler` drives parallel stage execution -> `Executor` dispatches to agents via gRPC -> agents call LLMs/tools -> results flow back. Step outputs use `{{ steps.step_id.output }}` Jinja2-like templating.

### Key Go packages

`internal/planner/` (YAML parsing, DAG validation), `internal/scheduler/` (run driver), `internal/executor/` (gRPC dispatch + retry), `internal/registry/` (agent lookup), `internal/state/` (workflow run tracking), `internal/server/` (REST API + SSE).

### Key Python modules

`agents/base.py` (BaseAgent ABC), `agents/persona.py` + `agents/persona_runtime.py` (persona agents with event-driven LLM execution), `agents/llm_client.py` (Anthropic + OpenAI), `agents/server.py` (gRPC servicer), `agents/memory/` (episodic/relationship/working), `agents/tools/` (registry, builtin, permissions, sandbox).

### Python package mapping

The `agents/` directory maps to `persatrix_agents` import path (configured in `agents/pyproject.toml` via `tool.setuptools.package-dir`). Run Python agents with `python -m persatrix_agents.server`.

## Phased Development

| Phase | Scope | Status |
|-------|-------|--------|
| v0.1 | Core engine: workflows, task agents, tools, MCP | Complete |
| v0.2 | Agent societies: personas, memory, channels, bridges | In progress |
| v0.3 | Distributed mesh: multi-node, A2A protocol | Planned |

Many `internal/` packages are **intentional TODO stubs** for their target phase. Do not remove TODO placeholders — implement them when the phase is active.

## Code Conventions

### Go
- Structured logging: `go.uber.org/zap` with structured fields, never `fmt.Sprintf` for logs
- Testing: `github.com/stretchr/testify`, always run with `-race`
- Error wrapping: `fmt.Errorf("context: %w", err)`

### Python
- Type hints required (mypy enforced). Use `X | None` not `Optional[X]`, `dict[str, Any]` not `Dict[str, Any]`
- Async-first: all agent methods are `async def`
- Ruff linter: line-length 100, excludes `generated/`. Server methods use PascalCase per proto contract (N802 suppressed)
- pytest with `asyncio_mode = "auto"` (configured in `agents/pyproject.toml`)
- Platform: check `sys.platform != "win32"` before `loop.add_signal_handler()`

### Rust
- `clap` v4 derive macros. Exhaustive `match` on command enums — no catch-all `_`
- `tokio` async runtime

### Cross-cutting
- Agent IDs: `^[a-z0-9][a-z0-9-]*[a-z0-9]$`
- Permissions are deny-by-default. Whitelist in `config/agents.yaml`
- Always run `make validate` after editing YAML configs
- Commit messages: Conventional Commits (`feat:`, `fix:`, `refactor:`, etc.)
- PRs < 500 lines, squash merge to `main`, trunk-based branching

## Status Hygiene

When completing work, update ROADMAP.md (merged PR table, component status, RFC status). Follow [Status Hygiene rules](docs/development-workflow.md#status-hygiene) — verify consistency across RFC files, PR plans, and ROADMAP before and after every task.

## Documentation Pointers

- Architecture details: `.github/copilot-instructions.md`
- Development lifecycle: `docs/development-workflow.md`
- Branching: `docs/BRANCHING.md`
- RFC process: `docs/rfcs/README.md`
- Specs: `docs/ai-agents-orchestration-spec.md`, `docs/persatrix-extension-spec.md`

PR review reports (`docs/pr-reviews/`) are local-only artifacts — never reference them in committed documents.
