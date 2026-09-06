# Enforcement Matrix

> **Last updated**: 2026-09-06
> Every rule the project states, with the document that states it, the check
> that enforces it, and how hard the enforcement is. Read the **Enforcement**
> column literally: a rule is only as strong as the weakest place it is
> checked. Branch protection was read with `gh api` on 2026-09-06.

## Enforcement levels

| Level | Meaning |
|-------|---------|
| **Required** | A CI status check branch protection requires. A red check blocks the merge. |
| **CI-advisory** | Runs in CI on every PR, but branch protection does not require it. A red check is visible and mergeable. |
| **Pre-commit** | Runs only in the local hook (`scripts/pre_commit.py`). Skipped by any contributor without the hook installed, and by `--no-verify`. |
| **Make-only** | A `make` target exists; nothing calls it automatically. |
| **Convention** | Stated in a document; no check. |

Branch protection on `main` today: linear history required, force-push
blocked, **0 approving reviews required**, and six required contexts —
`Go (build + test)`, `Proto staleness check`, `Python (lint + test)`,
`Rust (build + clippy)`, `Validate configs`, `Validate PR Title`. The other
CI jobs (`File size check`, `Third-party license check`, `Web console (build
+ test)`, `Dockerignore context hygiene`, `Cost regression gate`) run but are
not required.

---

## Code and build

| Rule | Stated in | Check | Enforcement |
|------|-----------|-------|-------------|
| Go builds; Go unit tests pass with `-race` | CONTRIBUTING | `go build ./cmd/orchestrator`; `go test ./internal/... -race -cover` | Required (`Go`) |
| Committed UI embed is the placeholder, never build output | `.gitignore` comment; `ci.yml` | `grep` assert in the `go` job | Required (`Go`) |
| Python lint (ruff) — `agents/`, `tests/` | instructions; ISSUE-0056 | `ruff check` ×2 | Required (`Python`) |
| Python types (mypy) — `agents/`, `tests/` | instructions; ISSUE-0062 | `mypy` ×2 | Required (`Python`) |
| Python lint + types — `evaluators/` (the eval harness) | Makefile `lint-python` | `ruff check evaluators/`; `mypy evaluators/` | Make-only — CI lints `agents/` and `tests/` but never `evaluators/` |
| Python unit + agents + integration suites pass | testing-strategy | three `pytest` steps | Required (`Python`) |
| Go and Python protobuf stubs match `proto/*.proto`; no orphans | Makefile; ISSUE-0017/0023 | `make proto-go && git diff --exit-code`; `make proto-python-check proto-orphans-check` | Required (`Proto staleness`, `Python`) |
| MIT-candidate primitives never import BUSL code (RFC 0045 §B) | RFC 0045; CONTRIBUTING | `make imports-check` (import-linter) | Required (`Python`) |
| Rust builds; clippy clean; `cargo test` passes (incl. lockstep guards) | instructions | `cargo build`, `cargo clippy -- -D warnings`, `cargo test` | Required (`Rust`) |
| YAML configs validate against `schemas/` | CLAUDE.md; instructions | `python agents/validate.py config/` | Required (`Validate configs`) |
| RFC and issue INDEX files fresh; front-matter valid | rfcs/README, issues/README | `make rfcs-check issues-check` | Required (`Validate configs`) |
| PR title is a Conventional Commit | CONTRIBUTING; BRANCHING | `commitlint.yml` | Required (`Validate PR Title`) |
| Web console unit tests pass; bundle builds; orchestrator compiles with it | web-console guide | `make ui-test`, `make ui`, `go build` | CI-advisory |
| No `{@html}` under `web/src` (session-riding XSS) | RFC 0039 amendment §A3 | `make ui-html-check` | CI-advisory |
| `.dockerignore` excludes nested `node_modules` | ISSUE-0104 | `make dockerignore-check` | CI-advisory |
| Third-party licences on the allow-list (Go, Python, Rust) | Makefile; `allowed_licenses.txt`; `deny.toml` | `make check-licenses` | CI-advisory |
| Idle persona spends nothing (RFC 0024) | RFC 0024 §Test Strategy | `test_bored_persona_cost.py`, path-filtered | CI-advisory |
| `gofmt` / `cargo fmt` clean | instructions | `gofmt -l`; `cargo fmt --check` | Pre-commit |
| `scripts/` lint-clean | [ISSUE-0134](../issues/ISSUE-0134-scripts-tree-is-not-linted.md) | — | **Nowhere** |
| Go integration tests pass | testing-strategy | `go test ./tests/integration/...` | **Nowhere** |
| Python sanitizer patterns/enums match the Go canonical source | Makefile (RFC 0009 PR 3) | `make generate-sanitizer-patterns-check` | Make-only |
| `THIRD_PARTY_NOTICES.md` matches the dependency graphs | Makefile | `make notices-check` | Make-only (release-prep PR 4 runs it by hand) |
| `agents.yaml` `instructions_file` references resolve | prompt-organization | `scripts/checks/prompt_refs.py` via `make validate` | Make-only — CI's validate job calls `agents/validate.py` directly and skips this |
| Personal-tier recall latency within 20 % of baseline | RFC 0029 | `tests/perf/personal_tier_latency.py` | CI-advisory, **informational** until a baseline exists |
| Weekly Rust advisory / bans / sources audit | CONTRIBUTING | `scheduled-audit.yml` (cargo-deny, Mondays; files an issue on failure) | Scheduled |

## Size and shape

| Rule | Stated in | Check | Enforcement |
|------|-----------|-------|-------------|
| Code files ≤ 500 lines | documentation-guide §Size Limits | `file_size.py --strict` | CI-advisory (`File size check`) + Pre-commit |
| Docs ≤ 3 000 words; RFCs ≤ 8 000 words | documentation-guide | same | CI-advisory + Pre-commit |
| Grandfathered files carry a reason and an exit condition | `file_size_allowlist.py` docstring | review | Convention |
| Near-cap warning at 3 % | `file_size.py` | `--near-cap` output on every run | Advisory output |
| PRs under 500 changed lines | CONTRIBUTING; BRANCHING; CLAUDE.md | — | **Convention only** — a third of recent merges exceed it |
| Squash merge; linear history | BRANCHING | branch protection | Required |
| Feature branch naming `feature/vNNN-…` | BRANCHING; each plan's header | — | Convention |

## Documentation

| Rule | Stated in | Check | Enforcement |
|------|-----------|-------|-------------|
| No broken relative links or anchors in tracked markdown | documentation-guide; consistency checklist | `doc_links.py` | Pre-commit |
| Only the standard status markers | documentation-guide §Status Markers | `doc_status_markers.py` | Pre-commit |
| No leaked tool-call markup in docs | — | `doc_leaked_markup.py` | Pre-commit |
| `FILEMAP.md` matches `git ls-files` | `generate_filemap.py` | `--check` exists; hook regenerates | Pre-commit (regenerate); **no CI check** — [ISSUE-0133](../issues/ISSUE-0133-no-ci-gate-on-filemap-freshness.md) |
| Unified doc audit (links + markers + sizes) | `doc_audit.py` | — | Make-only in spirit — **nothing calls it** |
| Local-only files never referenced from committed files | CLAUDE.md; copilot-instructions; review-process | review | Convention |
| Glossary terms mandatory; new terms added in the same change | CLAUDE.md §Terminology | review | Convention |
| Plain English; lead with the point | documentation-guide §Writing Style | review | Convention |
| Status hygiene before and after every task | ROADMAP §How to Update; CLAUDE.md | review | Convention |
| Every RFC has front-matter, required sections, ToC | rfcs/README checklist | `rfcs-check` (front-matter only) | Required for front-matter; Convention for sections |

## Process

| Rule | Stated in | Check | Enforcement |
|------|-----------|-------|-------------|
| Every PR reviewed; findings dispositioned | review-process | — | Convention (0 GitHub approvals required) |
| Migrations land ahead of their consumer, one store per PR | release-cycle | review | Convention |
| Scope locks change only by amendment | decisions | review | Convention |
| Live arc runs once, live, before the tag | release-cycle | release checklist §4 | Convention, evidenced in the report |
| Version strings aligned across five files | version-bump guide | `make bump-version` + checklist §2 | Manual at release-prep PR 3 |
| TDD for new unit-level code | CLAUDE.md §TDD | review | Convention |
| Version-train gate | release-cycle | review | Convention |

---

## What this table says the project should change

Listed here so the matrix is honest about its own gaps; the CI-promotion PR
in the methodology series addresses the first two groups.

1. **Make the five advisory CI jobs required.** File size, licences, web
   console, dockerignore hygiene, cost gate — all green on `main` today; a
   required-check rule costs nothing and closes a merge path that currently
   accepts red. This is a repository setting, not a code change.
2. **Move the pre-commit-only and make-only checks into CI**: doc links,
   status markers, leaked markup, FILEMAP `--check`, `prompt_refs`, sanitizer
   sync, `gofmt`/`cargo fmt`, `ruff check scripts/`, ruff + mypy over
   `evaluators/`, Go integration tests. All but `ruff check scripts/` pass on
   `main` today ([ISSUE-0134](../issues/ISSUE-0134-scripts-tree-is-not-linted.md)
   has 31 findings, 13 auto-fixable).
3. **Arm the perf gate** by dispatching `perf-baseline-capture.yml` once.
4. **Decide the PR-size rule**: enforce it (a size label or a check) or
   restate it as guidance with the split heuristic it actually follows.

## Related documentation

- [testing-strategy.md](testing-strategy.md) — the layers the checks guard
- [automation-catalogue.md](automation-catalogue.md) — where each check lives
- [documentation-guide.md §Size Limits](../documentation-guide.md#size-limits)
- [CONTRIBUTING.md §Quality Gates & CI](../../CONTRIBUTING.md#quality-gates--ci)
