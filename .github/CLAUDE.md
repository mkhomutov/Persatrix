# CLAUDE.md

Guidance for Claude Code (claude.ai/code) working in this repository.

## Response Style

Be brief by default. Expand only when asked.

- **Answer first**, background second.
- Routine answers: ≤8 lines. Code explanations: ≤3 bullets + 1 example.
- Reviews: list findings; skip long recap.
- Do not restate the prompt, repeat unchanged context, or add preambles.
- Ask at most one clarifying question; prefer acting on the most reasonable interpretation.
- Status updates: 1–2 sentences.
- Never add `Co-Authored-By: Claude` trailers or Claude attribution in PRs/commits.

## What is Persatrix

Polyglot AI agent orchestration: **Go** orchestrator, **Python** agent runtime, **Rust** CLI, connected by protobuf/gRPC. DAG-based YAML workflows with Jinja2-like templating.

## Build & Test

All commands are in the Makefile. Prefer Make targets.

```bash
make all            # proto + full build
make build-agents   # pip install -e ".[dev]"
make test           # all suites (Go + Python + integration)
make lint           # golangci-lint + ruff + mypy + clippy
make validate       # YAML configs against JSON schemas
make proto          # regenerate gRPC stubs after proto changes
```

Detailed test invocations: `go test ./internal/planner -v -run TestName`, `python3 -m pytest tests/unit/python/test_agents.py::test_name -v`.

## Architecture

```
CLI (Rust)  <--REST-->  Orchestrator (Go)  <--gRPC-->  Agents (Python)
  cli/                   cmd/orchestrator/               agents/
                         internal/                        memory/
                                                          sub_agents/
                                                          tools/
```

Component boundaries:
- **Go orchestrator** (`internal/`): scheduling, registry, security, cost, telemetry. No LLM logic.
- **Python agents** (`agents/`): LLM calls, tools, persona behavior, memory, sub-agent spawning.
- **Rust CLI** (`cli/`): thin REST client; all business logic is server-side.
- **Protos** (`proto/`): cross-language gRPC contract — changes require RFC review.

Phased stubs: many `internal/` packages are intentional TODO stubs. Do not remove them.

Key Go packages: `internal/planner/`, `internal/scheduler/`, `internal/executor/`, `internal/registry/`, `internal/state/`, `internal/server/`.  
Key Python modules: `agents/base.py`, `agents/persona.py`, `agents/llm_client.py`, `agents/server.py`, `agents/memory/`, `agents/tools/`.  
Python import path: `persatrix_agents` (via `agents/pyproject.toml`). Run with `python -m persatrix_agents.server`.

## Code Conventions

Language-specific rules are in `.github/instructions/`. Cross-cutting:

- **Agent IDs:** `^[a-z0-9][a-z0-9-]*[a-z0-9]$`
- **Persona names:** nickname-style, not human-like (e.g. `ember-owl`). Use `make generate-persona-nickname COUNT=5`.
- **Permissions:** deny-by-default. Whitelist in `config/agents.yaml`.
- **Config YAML:** always run `make validate` after editing. Schemas in `schemas/`.
- **Commits:** Conventional Commits (`feat:`, `fix:`, `refactor:`, …). PRs < 500 lines, squash-merge to `main`.

Go: `go.uber.org/zap` structured logging; `testify` with `-race`; `fmt.Errorf("ctx: %w", err)`.  
Python: `X | None` types; `async def`; ruff line-length 100; `asyncio_mode = "auto"`; guard `loop.add_signal_handler()` on `sys.platform != "win32"`.  
Rust: `clap` v4 derive; exhaustive `match` (no `_`); `tokio`.

## Status Hygiene

Before and after every task, verify consistency across RFC files, PR plans, and ROADMAP.md. Follow [Status Hygiene rules](../docs/development-workflow.md#status-hygiene).

PR review reports (`docs/pr-reviews/`) are local-only — never reference them in committed documents.

## Documentation

- Architecture: `.github/copilot-instructions.md`
- Development lifecycle: `docs/development-workflow.md`
- Branching: `docs/BRANCHING.md`
- RFC process: `docs/rfcs/README.md`
- Specs: `docs/ai-agents-orchestration-spec.md`, `docs/persatrix-extension-spec.md`
