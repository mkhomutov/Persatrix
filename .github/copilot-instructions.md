# Persatrix — Project Guidelines

Polyglot AI agent orchestration framework: **Go** orchestrator, **Python** agent runtime, **Rust** CLI. Agents communicate via gRPC (protobuf) and REST/SSE. Workflows are DAG-based YAML with Jinja2-like templating.

## Response Style

Be brief by default. Expand only when asked.

- **Answer first**, background second.
- Routine answers stop once the question is answered. Code explanations: a few points and one example.
- Reviews: list findings; skip long recap.
- Do not restate the prompt, repeat unchanged context, or add preambles.
- Ask at most one clarifying question; prefer acting on the most reasonable interpretation.
- Status updates: what changed and what comes next, briefly.
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
- **Python agents** (`agents/`): LLM calls, tools, persona behavior, memory, and the sub-agent code (`agents/sub_agents/`), which only tests call so far.
- **Rust CLI** (`cli/`): thin REST client; all business logic is server-side.
- **Protos** (`proto/`): cross-language gRPC contract — change carefully.

Phased stubs: several `internal/` packages are intentional TODO stubs; ROADMAP.md shows when each is planned. Do not remove them — implement one only when ROADMAP.md schedules it for the release in progress.

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
- **Workflow templating:** `{{ variable }}` syntax (Jinja2-like) in a step's `input`.
- **Workflow steps always run.** A step's `condition`, `approval_required` and `approval_timeout` pass `make validate` and are then ignored: the step runs, without a pause ([ISSUE-0202](../docs/issues/ISSUE-0202-workflow-condition-and-approval-validate-then-do-nothing.md)). Leave them out of a new workflow.

Key patterns: `@tool(name=...)` auto-generates a tool's parameter schema; three-tier memory (Episodic/Relationship/Working); optimization profiles in `config/optimization.yaml`.

What is checked before a tool runs, and what is not:

- The `permissions=[...]` a tool declares with `@tool` are checked. A model's call is refused with `Permission denied: <name>` for an agent whose config does not grant every one (`admit_call` in `agents/tools/tool_list.py`). A tool can declare only what an agent's config can grant: `filesystem:read`, `filesystem:write`, `network:http`, `shell:exec`, `memory:read`, `memory:write` and `channels:recall`. Any other name is an error when the tool is defined: the agent schema has no place to grant it, so a tool cannot have a permission of its own yet.
- A tool must still ask, in its own body, for anything finer than the name: which path, which command, which domain. The built-in file, shell and HTTP tools do (`PathValidator`, `is_command_allowed` and `is_domain_allowed`, called in `agents/tools/builtin.py`). A tool that declares no permission is held only by the agent's `tools` list.
- A task's `allowed_tools` list is enforced when it names a tool: a tool it does not name is not offered and does not run. An empty list restricts nothing, and the orchestrator never sets the field.
- Sub-agents are not in use. A spawner that runs one inside the agent's own process exists (`agents/sub_agents/spawner.py`) and tests run it, but nothing a persona or the orchestrator does reaches it. No permission is inherited or narrowed: the rule that a sub-agent gets no more than its parent is not built ([ISSUE-0285](../docs/issues/ISSUE-0285-sub-agent-permission-rule-and-delegation-timeout-are-not-built.md)).

## TDD

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
| `templates/personas.yaml` | Persona archetypes that the blueprints name with `extends:`; nothing applies them yet ([ISSUE-0267](../docs/issues/ISSUE-0267-extends-template-references-are-documented-and-not-built.md)) |
| `schemas/` | JSON Schema for validation |
| `docs/` | Specs, RFCs, guides |
| `docs/issues/` | Deferred/cross-cutting findings (`make issues` to list) |
| `workflows/` | Workflow DAG definitions |

How things work today: the guides in `docs/guides/`, [observability.md](../docs/observability.md) and [ROADMAP.md](../ROADMAP.md). The two specs, [ai-agents-orchestration-spec.md](../docs/ai-agents-orchestration-spec.md) and [persatrix-extension-spec.md](../docs/persatrix-extension-spec.md), are the April 2026 design, written before most of the code. Read them for intent, never as a description of what is built, and never to judge what protects a deployment.

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

How releases are planned, reviewed, and shipped: [docs/methodology/README.md](../docs/methodology/README.md) — the release cycle (Phases 0–2: one version plan, implementation PRs, the tag PR), scope locks / cuttable items / amendments, the review process, and the process glossary. The RFC-level sub-cycle stays in [docs/development-workflow.md](../docs/development-workflow.md).

**v0.4.0 is paused** since 2026-11-07. The [strategy review of 2026-11-28](../docs/project-strategy-review-2026-11-28.md#3-the-maintainers-rulings) was held, and ruled that the pause ends only when the sequencing amendment that ratifies it merges. Until then: start no PR from its plan's remaining rows, nor a quality-review fix that a paused row carries (ISSUE-0196's environment half, ISSUE-0213). The licence PR, the strategy and README rewrites and EXP-002's pre-registration wait for that amendment too. Other quality-review fixes (ISSUE-0196 onward), EXP-001's own work, and dependency and CI upkeep still land ([v0.4.0 plan](../docs/v0.4.0-plan.md#paused-until-the-strategy-review)).

## Status Hygiene

Follow [Status Hygiene rules](../docs/development-workflow.md#status-hygiene). In brief:
- Verify consistency across RFC files, PR plans, and [ROADMAP.md](../ROADMAP.md) before and after every task.
- PR merged → update PR plan checklist + RFC count immediately; the next commit made through the pre-commit hook regenerates `docs/merged-prs.md` and flips the PR's plan row (with none coming, flip it by hand), and `make plan-status-check` flags a row still saying "PR open" after that.
- All PRs for an RFC merged → RFC and ROADMAP status → `✅ Implemented`.
- New RFC → add to ROADMAP RFC Tracker.
- **Local-only files MUST NEVER be referenced** in any committed file (docs, code, comments, tests, commit messages, PR descriptions, or issue refs). "Local-only" means any path ignored by `.gitignore` — notably `docs/pr-reviews/` (PR review reports) and any other gitignored artifact. If a finding from a local review needs to be recorded, paraphrase the finding inline; do not link the source file by path or filename.

## Branching

Trunk-based. Branches: `feature/vNNN-component-description` (also `docs/`, `fix/`, `ci/`), hours to days, rebase then squash-merge to `main`. PRs target < 500 changed lines (guidance, not enforced). Release tags are cut from `main`; there are no release branches. See [BRANCHING.md](../docs/BRANCHING.md).

## Assistant-specific files

This file is the shared source for every assistant. Claude Code reads the repo-root [CLAUDE.md](../CLAUDE.md), which imports this file and adds only Claude-specific guidance; language rules are in `instructions/`.
