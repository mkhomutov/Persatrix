# Persatrix — Project Guidelines

Polyglot AI agent orchestration framework: **Go** orchestrator, **Python** agent runtime, **Rust** CLI. Agents communicate via gRPC (protobuf) and REST/SSE. Workflows are DAG-based YAML with Jinja2-like templating.

## Architecture

```
CLI (Rust)  ←──REST──→  Orchestrator (Go)  ←──gRPC──→  Agents (Python)
  cli/                   cmd/orchestrator/               agents/
                         internal/                        memory/
                                                          sub_agents/
                                                          tools/
```

| Layer | Language | Entry point | Key dirs |
|-------|----------|-------------|----------|
| Orchestrator | Go 1.24 | `cmd/orchestrator/main.go` | `internal/` (planner, scheduler, executor, registry, security, state, cost, telemetry, mcp, resilience) |
| Agents | Python ≥3.11 | `agents/server.py` | `agents/` (base, coder, reviewer, planner, persona), `agents/memory/`, `agents/tools/`, `agents/sub_agents/` |
| CLI | Rust 2021 | `cli/src/main.rs` | `cli/` |
| Protos | Protobuf | `proto/task.proto`, `proto/agent_message.proto` | `proto/` |
| Config | YAML | `config/agents.yaml` | `config/`, `config/environments/` |

### Component boundaries

- **Go orchestrator** owns workflow execution, scheduling, agent registry, cost tracking, telemetry, security gates. It does **not** contain LLM call logic.
- **Python agents** own LLM interaction, tool execution, persona behavior, memory, and sub-agent spawning. Each agent is a gRPC service.
- **Rust CLI** is a thin client that talks to the orchestrator's REST API. All business logic lives server-side.
- **Protos** define the contract between orchestrator and agents—change these carefully.

### Phased development

| Phase | Scope |
|-------|-------|
| v0.1 | Core orchestrator, task agents, workflows, tool registry, MCP bridge |
| v0.2 | Persona agents, memory (episodic/relationship/working), channels, bridges, organizations |
| v0.3 | Mesh networking, A2A protocol, distributed multi-node deployment |

Many `internal/` packages and agent subsystems are intentional **TODO stubs** for their target phase. Do not remove TODO placeholders—implement them when the phase is active.

## Build and Test

All commands are defined in the [Makefile](../Makefile). Prefer Make targets over raw commands.

```shell
# Full build (proto + all components)
make all

# Component builds
make build-orchestrator    # → bin/persatrix-server
make build-cli             # → cli/target/release/orch
make build-agents          # pip install -e ".[dev]"

# Tests
make test                  # all suites
make test-go               # go test ./internal/... -v -race -cover
make test-python           # pytest tests/unit/python/ -v --tb=short
make test-integration      # pytest tests/integration/ -v --tb=short

# Lint
make lint                  # all linters (Go, Python ruff, Rust clippy)

# Validate config YAML against JSON schemas
make validate

# Docker
make docker-build          # docker compose build
make docker-up             # docker compose up -d

# Clean
make clean
```

CI runs on every PR: Go build+test, Python lint+test, Rust build+clippy, config validation. See [`.github/workflows/ci.yml`](workflows/ci.yml).

## Code Conventions

### Go (orchestrator)

- Structured logging with `go.uber.org/zap`; use `logger.Info/Error/Debug` with structured fields, not `fmt.Sprintf`.
- Testing with `github.com/stretchr/testify`.
- Race detector enabled in CI: `go test -race`.
- Minimal external dependencies by design.

### Python (agents)

- **Type hints required** (mypy enforced). Use Python 3.11+ syntax: `X | None` not `Optional[X]`, `dict[str, Any]` not `Dict[str, Any]`.
- **Linting:** ruff (configured in pyproject.toml).
- **Async-first:** all agent methods are `async def`. Use `asyncio.run()` at entry points.
- **Testing:** pytest with `asyncio_mode = "auto"`. Fixtures use `autouse` pattern for cleanup (see `test_tools.py`).
- **Naming:** PascalCase classes, snake_case functions, SCREAMING_SNAKE enums, `_leading_underscore` for private.
- **Error handling:** Distinguish transient vs permanent errors via `error_type` field. Raise `NotImplementedError` for unimplemented stubs, don't silently succeed.
- **Dataclasses:** Use `field(default_factory=...)` for mutable defaults.
- **Platform awareness:** Windows signal handling differs; check `sys.platform != "win32"` before `loop.add_signal_handler()`.

### Rust (CLI)

- `clap` v4 derive macros for CLI argument parsing.
- Exhaustive `match` on command enums (no catch-all `_`); adding a command must produce a compile error until handled.
- `tokio` async runtime.

### Cross-cutting

- **Agent IDs:** lowercase alphanumeric + hyphens, pattern `^[a-z0-9][a-z0-9-]*[a-z0-9]$`.
- **Persona naming policy:** avoid human-like names in persona IDs/display names. Use nickname-style names (for example `ember-owl`).
- **Nickname generator:** use `make generate-persona-nickname COUNT=5` (or `python scripts/persona_nickname_generator.py`) when creating persona examples or test fixtures.
- **Config files** validate against JSON schemas in `schemas/`. Always run `make validate` after editing YAML configs.
- **Permissions are deny-by-default.** Agent permissions in `config/agents.yaml` explicitly whitelist filesystem paths, network domains, and shell commands.
- **Workflow templating** uses `{{ variable }}` syntax (Jinja2-like) for step inputs and conditions.
- **Template references** use `extends: "templates/personas.yaml#anchor"` to inherit persona/sub-agent definitions.

## Key Patterns

- **BaseAgent → PersonaAgent hierarchy:** `BaseAgent` (ABC) defines `handle()` for task execution. `PersonaAgent` extends it with event-driven `on_event()` and autonomous `on_tick()`.
- **Tool decorator pattern:** `@tool(name=..., permissions=[...])` auto-generates parameter schemas from type hints.
- **Sub-agent spawning:** Parent agents spawn ephemeral sub-agents with inherited (but restricted) permissions and resource budgets.
- **Three-tier memory:** Episodic (long-term, SQLite), Relationship (trust/interaction), Working (context window management).
- **Optimization profiles:** `cost_optimized`, `speed_optimized`, `quality_optimized`, `simulation_optimized` in `config/optimization.yaml`.

## Project Structure Quick Reference

| Path | Purpose |
|------|---------|
| `config/agents.yaml` | Agent definitions (model, permissions, capabilities) |
| `config/mcp-servers.yaml` | External MCP server connections (GitHub, filesystem) |
| `config/optimization.yaml` | Model routing, caching, budgets, pricing |
| `config/environments/` | Per-environment overrides (dev, staging, prod) |
| `templates/personas.yaml` | Reusable persona archetypes |
| `templates/sub_agents.yaml` | Sub-agent role templates |
| `blueprints/` | Project templates (social-experiment, software-team) |
| `workflows/` | Workflow DAG definitions |
| `schemas/` | JSON Schema for agent, channel, workflow validation |
| `evaluators/` | Conversation scoring and evaluation |

## Documentation

Detailed specs and design decisions live in `docs/`. Refer to these rather than duplicating content:

- [ROADMAP.md](../ROADMAP.md) — Development progress, RFC status, component completion, merged PR history
- [development-workflow.md](../docs/development-workflow.md) — End-to-end development lifecycle (version planning → RFC → PR plan → implementation → follow-ups → refactoring → close)
- [ai-agents-orchestration-spec.md](../docs/ai-agents-orchestration-spec.md) — Core MVP specification (agents, orchestrator, tasks, workflows, REST API)
- [persatrix-extension-spec.md](../docs/persatrix-extension-spec.md) — Extension spec (personas, channels, bridges, memory, autonomy, blueprints)
- [persatrix-spec-audit.md](../docs/persatrix-spec-audit.md) — Audit of 45 resolved spec gaps
- [BRANCHING.md](../docs/BRANCHING.md) — Trunk-based branching strategy, naming conventions, PR size limits (<500 lines)

**When completing work**: Update [ROADMAP.md](../ROADMAP.md) with the merged PR, component status changes, and RFC status transitions. Follow the instructions at the bottom of ROADMAP.md.

**PR review reports are local-only artifacts.** Never reference, link to, or mention review report files (e.g. `docs/pr-reviews/*.md`) in committed documents such as PR plans, RFCs, or ROADMAP.md. Review reports are generated for local consumption and are not committed to the repository.

## Status Hygiene

Follow the [Status Hygiene rules](../docs/development-workflow.md#status-hygiene) in the development workflow guide. Key points:

- **Before and after every task**, verify consistency across RFC files, PR plans, and [ROADMAP.md](../ROADMAP.md).
- When a PR is merged → update PR plan checklist, ROADMAP merged-PR table and RFC merged count immediately.
- When all PRs for an RFC are merged → status to `✅ Implemented` in RFC file and ROADMAP.
- When creating a new RFC → add it to the ROADMAP RFC Tracker table.
- Never leave a stale status.

## Branching

Trunk-based development. Feature branches: `feature/v0X-component-description` (1–5 day lifetime). Squash merge to `main`. PRs < 500 lines. See [BRANCHING.md](../docs/BRANCHING.md).
