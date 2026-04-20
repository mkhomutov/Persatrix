# Contributing to Persatrix

Thank you for your interest in contributing to the Persatrix project!

## Table of Contents

- [Quality Gates & CI](#quality-gates--ci)
- [Development Setup](#development-setup)
- [Making Changes](#making-changes)
- [Pull Request Process](#pull-request-process)
- [Architecture](#architecture)

---

## Quality Gates & CI

This project enforces quality gates to ensure code quality and security. **All checks must pass before a PR can be merged.**

### Local Quality Checks

Before opening a PR, run the quality gate locally:

```bash
make test    # all test suites
make lint    # all linters
make validate # config validation
```

### CI Workflows

- **[`ci.yml`](.github/workflows/ci.yml)** — Runs on every PR and push to `main`. Covers Go build+test, Python lint+test, Rust build+clippy, config validation.
- **[`commitlint.yml`](.github/workflows/commitlint.yml)** — Enforces Conventional Commit format on PR titles (for automated changelog generation).
- **[`scheduled-audit.yml`](.github/workflows/scheduled-audit.yml)** — Weekly dependency audit for Rust crates.

### Pre-commit Hook

Install the git pre-commit hook to run fast checks automatically:

```bash
python scripts/install_hooks.py            # installs .git/hooks/pre-commit
python scripts/pre_commit.py               # run manually (same checks the hook runs)
```

---

## Development Setup

### Prerequisites

- **Go 1.24+** — Orchestrator
- **Python 3.11+** — Agents
- **Rust 2021 edition** — CLI
- **protoc** — Protocol buffer compiler (for gRPC codegen)
- **Windows only:** `make` — install via [GnuWin32](https://gnuwin32.sourceforge.net/packages/make.htm) or `winget install GnuWin32.Make`, then add `C:\Program Files (x86)\GnuWin32\bin` to your PATH

### Quick Start

```bash
# Install Python agent dependencies (dev mode)
make build-agents

# Build all components
make all

# Run tests
make test
```

### Component-Specific Commands

```bash
# Go orchestrator
make build-orchestrator    # → bin/persatrix-server
make test-go               # go test ./internal/... -v -race -cover

# Python agents
make build-agents          # pip install -e ".[dev]"
make test-python           # pytest tests/unit/python/ -v --tb=short

# Rust CLI
make build-cli             # → cli/target/release/persatrix

# Config validation
make validate              # validate YAML against JSON schemas
```

---

## Making Changes

### Branching Strategy

Trunk-based development. See [BRANCHING.md](docs/BRANCHING.md) for details.

- Feature branches: `feature/v0X-component-description` (1–5 day lifetime)
- Squash merge to `main`
- PRs < 500 lines

### Significant Design Changes

For changes that affect architecture, cross-component interfaces, or introduce new subsystems, follow the [RFC process](docs/rfcs/README.md) before implementation. See the [Development Workflow](docs/development-workflow.md) for the full lifecycle from version planning through RFC closure.

### Code Conventions

#### Go (Orchestrator)

- Structured logging with `go.uber.org/zap` (not `fmt.Sprintf`)
- Testing with `github.com/stretchr/testify`
- Race detector enabled: `go test -race`

#### Python (Agents)

- **Type hints required** — `X | None` not `Optional[X]`, `dict[str, Any]` not `Dict[str, Any]`
- **Linting:** ruff (configured in pyproject.toml)
- **Async-first:** all agent methods are `async def`
- **Testing:** pytest with `asyncio_mode = "auto"`

#### Rust (CLI)

- `clap` v4 derive macros for CLI argument parsing
- Exhaustive `match` on command enums (no catch-all `_`)
- `tokio` async runtime

### Commit Messages

Use [Conventional Commits](https://www.conventionalcommits.org/):

```
feat: add agent health check endpoint
fix: resolve gRPC timeout on large payloads
docs: update workflow templating guide
ci: add scheduled dependency audit
refactor: extract agent registry interface
test: add integration tests for DAG executor
```

### Licensing

This repository is licensed under the Business Source License 1.1 (`BUSL-1.1`).
By submitting a contribution, you agree that your contribution will be distributed
under the same license terms as the rest of the repository, including the stated
change date and transition to Apache License, Version 2.0 in [LICENSE](LICENSE).

---

## Pull Request Process

1. Create a feature branch from `main`
2. Make your changes (keep PRs < 500 lines)
3. Run `make test && make lint` locally
4. Open a PR with a Conventional Commit title
5. Address review feedback
6. Squash merge once approved

### PR Checklist

- [ ] Tests pass (`make test`)
- [ ] Linters pass (`make lint`)
- [ ] Config validation passes (`make validate`)
- [ ] PR title follows Conventional Commits format
- [ ] Documentation updated if needed
- [ ] No TODO placeholders removed (they track phased development)

---

## Architecture

See [copilot-instructions.md](.github/copilot-instructions.md) for the full architecture overview.

```
CLI (Rust)  ←──REST──→  Orchestrator (Go)  ←──gRPC──→  Agents (Python)
  cli/                   cmd/orchestrator/               agents/
                         internal/                        memory/
                                                          sub_agents/
                                                          tools/
```

### Component Boundaries

- **Go orchestrator** owns workflow execution, scheduling, agent registry, cost tracking, telemetry, security gates. No LLM call logic.
- **Python agents** own LLM interaction, tool execution, persona behavior, memory, sub-agent spawning. Each agent is a gRPC service.
- **Rust CLI** is a thin client for the orchestrator's REST API. All business logic lives server-side.
- **Protos** define the gRPC contract — change carefully.
