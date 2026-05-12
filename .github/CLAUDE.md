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
make proto-check    # CI gate: Python stubs in sync + no orphans (ISSUE-0023)
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

## TDD (from v0.3.0 onward)

All new unit-level code follows Red-Green-Refactor:

1. Write a failing test first. Confirm it fails before writing the implementation.
2. Write the minimum implementation to make it pass. Then refactor.
3. **Do not** write production code without a corresponding failing test (unit layer only).

Per-language details are in `.github/instructions/`. Key rules:
- **Go:** failing `_test.go` before `*.go`; table-driven tests; interface mocks in `internal/testutil/`.
- **Python:** failing pytest in `tests/unit/python/` first; mock `LLMClient` at the boundary; no real network calls.
- **Rust:** `#[cfg(test)]` unit tests inline; `cli/tests/` for CLI integration tests; mock HTTP with `mockito`.
- **Integration tests** (`tests/integration/`) are exempt — write them after the unit layer validates the pieces.

## Terminology

Use the canonical terms in [`docs/ai-glossary.md`](../docs/ai-glossary.md) by
default in outputs, edits, plans, and reviews.

- Glossary terms are mandatory; prefer canonical wording over ad-hoc phrasing.
- Avoid synonyms unless clarity requires one — then map back to the canonical
  term once and continue with the canonical term.
- If a user message uses a non-canonical synonym, acknowledge it once and switch
  to the canonical term in the response.
- Do not introduce new project terms without updating `docs/ai-glossary.md` in
  the same change.

## Status Hygiene

Before and after every task, verify consistency across RFC files, PR plans, and ROADMAP.md. Follow [Status Hygiene rules](../docs/development-workflow.md#status-hygiene).

## Local-only files

**Local-only files MUST NEVER be referenced** in any committed file (docs, code, comments, tests, commit messages, PR descriptions, or issue refs). "Local-only" means any path ignored by `.gitignore` — notably `docs/pr-reviews/` (PR review reports) and any other gitignored artifact. If a finding from a local review needs to be recorded, paraphrase the finding inline; do not link the source file by path or filename.

## Documentation

- Architecture: `.github/copilot-instructions.md`
- Glossary: `docs/ai-glossary.md`
- Development lifecycle: `docs/development-workflow.md`
- Branching: `docs/BRANCHING.md`
- RFC process: `docs/rfcs/README.md`; live RFC list: [`docs/rfcs/INDEX.md`](../docs/rfcs/INDEX.md) (`make rfcs`)
- Issue tracker: `docs/issues/README.md`; live issue list: [`docs/issues/INDEX.md`](../docs/issues/INDEX.md) (`make issues`)
- Specs: `docs/ai-agents-orchestration-spec.md`, `docs/persatrix-extension-spec.md`
