# Persatrix — Project Guidelines

Polyglot AI agent orchestration framework: **Go** orchestrator, **Python** agent runtime, **Rust** CLI. Agents communicate via gRPC (protobuf) and REST/SSE. Workflows are DAG-based YAML with Jinja2-like templating.

## Response Style

Be brief by default. Expand only when asked.

- **Answer first**, background second.
- Routine answers: ≤8 lines. Code explanations: ≤3 bullets + 1 example.
- Reviews: list findings; skip long recap.
- Do not restate the prompt, repeat unchanged context, or add preambles.
- Ask at most one clarifying question; prefer acting on the most reasonable interpretation.
- Status updates: 1–2 sentences.
- Prefer links to existing docs over embedding large guidance text.

## Writing docs & comments

Plain English a non-programmer can follow — short, real-world purpose first.
Full rules: [Documentation Guide § Writing Style](../docs/documentation-guide.md#writing-style).

## Architecture

```
CLI (Rust)  ←──REST──→  Orchestrator (Go)  ←──gRPC──→  Agents (Python)
  cli/                   cmd/orchestrator/               agents/
                         internal/                        memory/
                                                          sub_agents/
                                                          tools/
```

Component boundaries:
- **Go orchestrator** (`internal/`): scheduling, registry, security, cost, telemetry. No LLM logic.
- **Python agents** (`agents/`): LLM calls, tools, persona behavior, memory, sub-agent spawning.
- **Rust CLI** (`cli/`): thin REST client; all business logic is server-side.
- **Protos** (`proto/`): cross-language gRPC contract — change carefully.

Phased stubs: many `internal/` packages are intentional TODO stubs for v0.2/v0.3. Do not remove them — implement when the phase is active.

## Build and Test

All commands are in the [Makefile](../Makefile). Prefer Make targets.

```shell
make all            # proto + full build
make test           # all suites (Go + Python + integration)
make lint           # golangci-lint + ruff + mypy + clippy
make validate       # YAML configs against JSON schemas
make docker-up      # docker compose up -d
```

CI runs on every PR. See [`.github/workflows/ci.yml`](workflows/ci.yml).

## Code Conventions

Language-specific rules live in the instruction files under `.github/instructions/`. Key cross-cutting rules:

- **Agent IDs:** `^[a-z0-9][a-z0-9-]*[a-z0-9]$`
- **Persona names:** nickname-style, not human-like (e.g. `ember-owl`). Use `make generate-persona-nickname COUNT=5`.
- **Permissions:** deny-by-default. Whitelist explicitly in `config/agents.yaml`.
- **Config YAML:** always run `make validate` after editing. Schemas in `schemas/`.
- **Workflow templating:** `{{ variable }}` syntax (Jinja2-like) in step `input` and `condition` fields.

Key patterns: `@tool(name=..., permissions=[...])` auto-generates schemas; sub-agents inherit restricted permissions; three-tier memory (Episodic/Relationship/Working); optimization profiles in `config/optimization.yaml`.

## TDD (from v0.3.0 onward)

All new unit-level code follows Red-Green-Refactor:

1. Write a failing test first. Confirm it fails before writing the implementation.
2. Write the minimum implementation to make it pass. Then refactor.
3. **Do not** write production code without a corresponding failing test (unit layer only).

Per-language details are in `.github/instructions/`. Key rules:
- **Go:** failing `_test.go` before `*.go`; table-driven tests; interface mocks in `internal/testutil/` (created on first use — it does not exist yet).
- **Python:** failing pytest in `tests/unit/python/` first; mock `LLMClient` at the boundary; no real network calls.
- **Rust:** `#[cfg(test)]` unit tests inline (the whole suite today); `cli/tests/` for CLI integration tests and `mockito` for HTTP mocks, both added on first use — neither exists yet.
- **Integration tests** (`tests/integration/`) are exempt — write them after the unit layer validates the pieces.

## Project Layout (key paths)

| Path | Purpose |
|------|---------|
| `config/agents.yaml` | Agent definitions |
| `config/optimization.yaml` | Model routing, caching, budgets |
| `templates/personas.yaml` | Reusable persona archetypes |
| `schemas/` | JSON Schema for validation |
| `docs/` | Specs, RFCs, guides |
| `docs/issues/` | Deferred/cross-cutting findings (`make issues` to list) |
| `workflows/` | Workflow DAG definitions |

Full details: [ai-agents-orchestration-spec.md](../docs/ai-agents-orchestration-spec.md), [persatrix-extension-spec.md](../docs/persatrix-extension-spec.md).

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

## Methodology

How releases are planned, reviewed, and shipped: [docs/methodology/README.md](../docs/methodology/README.md) — the release cycle (Phases 0–4, release-prep PRs 0–4), scope locks / cuttable items / amendments, the review process, and the process glossary. The RFC-level sub-cycle stays in [docs/development-workflow.md](../docs/development-workflow.md).

## Status Hygiene

Follow [Status Hygiene rules](../docs/development-workflow.md#status-hygiene). In brief:
- Verify consistency across RFC files, PR plans, and [ROADMAP.md](../ROADMAP.md) before and after every task.
- PR merged → update PR plan checklist + ROADMAP table + RFC count immediately.
- All PRs for an RFC merged → RFC and ROADMAP status → `✅ Implemented`.
- New RFC → add to ROADMAP RFC Tracker.
- **Local-only files MUST NEVER be referenced** in any committed file (docs, code, comments, tests, commit messages, PR descriptions, or issue refs). "Local-only" means any path ignored by `.gitignore` — notably `docs/pr-reviews/` (PR review reports) and any other gitignored artifact. If a finding from a local review needs to be recorded, paraphrase the finding inline; do not link the source file by path or filename.

## Branching

Trunk-based. Branches: `feature/vNNN-component-description` (also `docs/`, `fix/`, `ci/`), hours to days, rebase then squash-merge to `main`. PRs target < 500 changed lines (guidance, not enforced). Release tags are cut from `main`; there are no release branches. See [BRANCHING.md](../docs/BRANCHING.md).

## Assistant-specific files

This file is the shared source for every assistant. Claude Code reads the repo-root [CLAUDE.md](../CLAUDE.md), which imports this file and adds only Claude-specific guidance; language rules are in `instructions/`.
