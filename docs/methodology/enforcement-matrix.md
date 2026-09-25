# Enforcement Matrix

> **Last updated**: 2026-09-25
> Every rule the project states, with the document that states it, the check
> that enforces it, and how hard the enforcement is. Read the **Enforcement**
> column literally: a rule is only as strong as the weakest place it is
> checked. Branch protection was read with `gh api` on 2026-09-15.

## Enforcement levels

| Level | Meaning |
|-------|---------|
| **Required** | A CI status check branch protection requires. A red check blocks the merge. |
| **CI-advisory** | Runs in CI on every PR, but branch protection does not require it. A red check is visible and mergeable. No row uses it since 2026-09-15 (every job is required); kept for the next job added before it is made required. |
| **Pre-commit** | Runs only in the local hook (`scripts/pre_commit.py`). Skipped by any contributor without the hook installed, and by `--no-verify`. |
| **Make-only** | A `make` target exists; nothing calls it automatically. |
| **Convention** | Stated in a document; no check. |

Branch protection on `main` today: linear history required, force-push
blocked, **0 approving reviews required**, and **twelve required contexts** —
every CI job, exactly the set versioned in
[`branch-protection.json`](branch-protection.json): `Go (build + test)`,
`Proto staleness check`, `Python (lint + test)`, `Rust (build + clippy)`,
`Validate configs`, `Validate PR Title`, `File size check`, `Third-party
license check`, `Web console (build + test)`, `Dockerignore context hygiene`,
`Cost regression gate (bored persona)` and `Docs hygiene` (`make
branch-protection-show` reported no diff on 2026-09-15). No CI job is
advisory any more: where a row below says Required, it rides inside one of
the twelve, and the job is named where it is not one of the original six.

---

## Code and build

| Rule | Stated in | Check | Enforcement |
|------|-----------|-------|-------------|
| Go builds; Go unit tests pass with `-race` | CONTRIBUTING | `go build ./cmd/orchestrator`; `go test ./internal/... -race -cover` | Required (`Go`) |
| Go lint clean under one pinned golangci-lint and the committed linter set | [ISSUE-0142](../issues/ISSUE-0142-ci-never-runs-golangci-lint.md); `.golangci.yml`; Makefile `GOLANGCI_LINT_VERSION` | `make lint-go` — refuses any other version; CI installs the pin it reads from the Makefile and runs the same target | Required (`Go`) — since v0.3.16 PR C1; was Make-only and unpinned |
| Committed UI embed is the placeholder, never build output | `.gitignore` comment; `ci.yml` | `grep` assert in the `go` job | Required (`Go`) |
| Python lint (ruff) — `agents/`, `tests/` | instructions; ISSUE-0056 | `ruff check` ×2 | Required (`Python`) |
| Python types (mypy) — `agents/`, `tests/` | instructions; ISSUE-0062 | `mypy` ×2 | Required (`Python`) |
| Python lint + types — `scripts/`, `evaluators/` | Makefile `lint-python`; ISSUE-0134 | `ruff check scripts/ evaluators/`; `mypy scripts/ evaluators/` | Required (`Python`) — since the CI-promotion PR |
| Python unit + agents + integration suites pass | testing-strategy | three `pytest` steps | Required (`Python`) |
| CI installs the Python dependencies at the committed pins, not the newest release of the minute ([#1002](https://github.com/mkhomutov/Persatrix/pull/1002) failed every open PR at once) | Makefile §Python constraints; `ci.yml` | every workflow installs through `make build-agents`, which passes `.github/python-constraints.txt` with `-c`; `test_ci_python_constraints.py` fails any workflow step that installs the agents package around it | Required (`Python`) |
| The pins match `agents/pyproject.toml`: every declared dependency pinned, inside its range | Makefile §Python constraints; `agents/pyproject.toml` | `make python-constraints-check` (the pinned uv re-resolves into a copy and diffs it); `test_ci_python_constraints.py` checks the same offline | Required (`Python`) |
| The pins move to the newest releases only after the Python checks pass on them | `python-constraints-refresh.yml` | weekly, and on a push to `main` that changes `agents/pyproject.toml`: re-resolve, install, run the checks, report in one issue; the PR that moves the pins is opened by a person from the issue's link | Scheduled |
| Every `stable` golden-trace eval replays green ([RFC 0044](../rfcs/0044-eval-set-golden-traces.md) §F) | RFC 0044; [evaluators guide](../evaluators-guide.md#promoting-a-recipe-to-stable) | `make eval-replay TIER=stable` — exits 1 on a failed assertion, a cassette miss, a malformed or golden-less stable recipe, or an empty selection | Required (`Python`) — since v0.3.16 PR C2; was Make-only |
| Go and Python protobuf stubs match `proto/*.proto`; no orphans | Makefile; ISSUE-0017/0023 | `make proto-go && git diff --exit-code`; `make proto-python-check proto-orphans-check` | Required (`Proto staleness`, `Python`) |
| MIT-candidate primitives never import BUSL code (RFC 0045 §B) | RFC 0045; CONTRIBUTING | `make imports-check` (import-linter) | Required (`Python`) |
| Rust builds; clippy clean; `cargo test` passes (incl. lockstep guards) | instructions | `cargo build`, `cargo clippy -- -D warnings`, `cargo test` | Required (`Rust`) |
| YAML configs validate against `schemas/` | CLAUDE.md; instructions | `python agents/validate.py config/` | Required (`Validate configs`) |
| RFC and issue INDEX files fresh; front-matter valid; each RFC's `**Status**` header line agrees with its front-matter | rfcs/README, issues/README | `make rfcs-check issues-check` | Required (`Validate configs`) |
| PR title is a Conventional Commit | CONTRIBUTING; BRANCHING | `commitlint.yml` | Required (`Validate PR Title`) |
| Web console unit tests pass; bundle builds; orchestrator compiles with it | web-console guide | `make ui-test`, `make ui`, `go build` | Required (`Web console (build + test)`) |
| No `{@html}` under `web/src` (session-riding XSS) | RFC 0039 amendment §A3 | `make ui-html-check` | Required (`Web console (build + test)`) |
| `.dockerignore` excludes nested `node_modules` | ISSUE-0104 | `make dockerignore-check` | Required (`Dockerignore context hygiene`) |
| Third-party licences on the allow-list (Go, Python, Rust) | Makefile; `allowed_licenses.txt`; `deny.toml` | `make check-licenses` | Required (`Third-party license check`) |
| Idle persona spends nothing (RFC 0024) | RFC 0024 §Test Strategy | `test_bored_persona_cost.py`, path-filtered | Required (`Cost regression gate (bored persona)`) |
| `gofmt` / `cargo fmt` clean | instructions | `gofmt -l` over every tracked `.go` file; `cargo fmt -- --check` | Required (`Go`, `Rust`) + Pre-commit (hook covers `internal/`, `cmd/` only) |
| Go integration tests pass | testing-strategy | `go test ./tests/integration/... -race` | Required (`Go`) |
| Python sanitizer patterns/enums match the Go canonical source | Makefile (RFC 0009 PR 3) | `make generate-sanitizer-patterns-check` | Required (`Go`) |
| `THIRD_PARTY_NOTICES.md` matches the dependency graphs | Makefile | `make notices-check` | Make-only — deliberately: the notices file is regenerated in the tag PR, so it is legitimately stale between a dependency bump and the next release (it is stale today) |
| `agents.yaml` `instructions_file` references resolve | prompt-organization | `scripts/checks/prompt_refs.py` | Required (`Validate configs`) |
| Personal-tier recall latency within 20 % of baseline | RFC 0029 | `tests/perf/personal_tier_latency.py` | Required job, **informational** until a baseline exists |
| Weekly Rust advisory / bans / sources audit | CONTRIBUTING | `scheduled-audit.yml` (cargo-deny, Mondays; files an issue on failure) | Scheduled |
| Dependencies bumped monthly, one grouped PR per ecosystem (Go, pip, Cargo, npm, Actions), `chore(deps)` titles | `.github/dependabot.yml` | Dependabot; for pip, the ranges in `agents/pyproject.toml` only (CI's pins move through `python-constraints-refresh.yml`) | Scheduled (security updates run on their own cadence) |
| Required status checks on `main` match the versioned set | `branch-protection.json` | `make branch-protection-show`; `test_branch_protection_config.py` pins the file to the CI job list | Owner applies; file is reviewed |

## Size and shape

| Rule | Stated in | Check | Enforcement |
|------|-----------|-------|-------------|
| Code files ≤ 800 lines | documentation-guide §Size Limits | `file_size.py --strict` | Required (`File size check`) + Pre-commit |
| A code file over 500 lines is warned, and split at a real seam when a change edits it for another reason — never trimmed to fit, never in a sweep (sequencing Amendment 2026-09-12, ruling (e)) | documentation-guide §Size Limits; release-cycle §The debt sweep (retired) | `file_size.py` lists it under `[WARN]` on every run; the split is review's call | Advisory output + Convention |
| Docs ≤ 3 000 words; RFCs ≤ 8 000 words | documentation-guide | same | Required (`File size check`) + Pre-commit |
| Grandfathered files carry a reason and an exit condition | `file_size_allowlist.py` docstring | review; `test_allowlist_has_no_dead_entries`, `test_allowlist_entries_are_not_already_excluded` | Convention + unit tests |
| Released version-cycle docs are frozen evidence, exempt from the cap | documentation-guide §Where Documents Live | `file_size.py` excludes them once `CHANGELOG.md` has the version's dated heading (ISSUE-0139; read from the tree, not `git tag`, so a depth-1 checkout agrees with a full clone); a still-allowlisted released doc prints `[STALE-ALLOWLIST]` (advisory; the tag PR drops an open plan's last-resort entry) | Required (`Python` unit tests pin it) |
| Near-cap notice at 3 % of each limit | `file_size.py` | `--near-cap` output on every run | Advisory output |
| PRs under 500 changed lines | CONTRIBUTING; BRANCHING; copilot-instructions | — | **Guidance, stated as such** since the BRANCHING rewrite — a third of merges exceed it, almost all docs-heavy; code PRs split |
| Squash merge; linear history | BRANCHING | branch protection | Required |
| Branch naming (`feature/vNNN-…`, `docs/`, `fix/`, `ci/`) | BRANCHING; each plan's header | — | Convention |

## Documentation

| Rule | Stated in | Check | Enforcement |
|------|-----------|-------|-------------|
| No broken relative links or anchors in tracked markdown | documentation-guide; consistency checklist | `doc_links.py` | Required (`Docs hygiene`) + Pre-commit |
| Only the standard status markers | documentation-guide §Status Markers | `doc_status_markers.py` | Required (`Docs hygiene`) + Pre-commit |
| No leaked tool-call markup in docs | — | `doc_leaked_markup.py` | Required (`Docs hygiene`) + Pre-commit |
| `FILEMAP.md` matches `git ls-files` | `generate_filemap.py` | `--check` (date-insensitive; on a PR it compares against the merge tree, so a PR behind a file-adding merge fails until updated) | Required (`Docs hygiene`) + Pre-commit regenerates — closed [ISSUE-0133](../issues/ISSUE-0133-no-ci-gate-on-filemap-freshness.md) |
| Merged-PR history (`docs/merged-prs.md`) matches the squash log, allowing only the newest merges to be missing | automation-catalogue | `scripts/merged_prs.py --check` | Required (`Docs hygiene`) + Pre-commit regenerates |
| No plan row says "PR open" / "not started" for a PR that has merged | release-cycle §Phase 1 | `scripts/checks/plan_status.py` (`make plan-status-check`) | Required (`Docs hygiene`) + Pre-commit — first run found ten stale rows; judging an unlinked 🔀 row by the commit that wrote it found eight more |
| A patch release keeps one document: no `docs/vX.Y.Z-*.md` file beside its plan but a test-findings PR plan | release-cycle §Phase 0 (sequencing Amendment 2026-09-12, ruling (e)) | `scripts/checks/plan_status.py` (`make plan-status-check`) fails one for any `X.Y.Z` after 0.3.16 with `Z` above 0, dated or not; v0.3.16 and earlier keep their files, a minor release is not judged | Required (`Docs hygiene`) + Pre-commit |
| Every artifact the methodology names exists (documents, tools, make targets, Docs-hygiene steps) | [conformance.json](conformance.json) | `make conformance-check` | Required (`Docs hygiene`) + Pre-commit |
| No ROADMAP Component Status row says less than the RFC it names | ROADMAP §How to Update | `scripts/checks/roadmap_status.py` (`make roadmap-status-check`) | Required (`Validate configs`) + Pre-commit — its first run found `internal/security/` still "In progress" four months after RFC 0009 closed part-way |
| Unified doc audit (links + markers + sizes) | `doc_audit.py` | — | Local convenience wrapper; its three checks run individually in CI |
| Local-only files never referenced from committed files | CLAUDE.md; copilot-instructions; review-process | review | Convention |
| Glossary terms mandatory; new terms added in the same change | CLAUDE.md §Terminology | review | Convention |
| Plain English; lead with the point | documentation-guide §Writing Style | review | Convention |
| Status hygiene before and after every task | ROADMAP §How to Update; CLAUDE.md | review; ROADMAP's Component Status rows also by `roadmap_status.py` (above) | Convention; Required for the Component Status rows |
| Every RFC has front-matter, required sections, ToC | rfcs/README checklist | `rfcs-check` (front-matter and its `**Status**` header line only) | Required for front-matter; Convention for sections |

## Process

| Rule | Stated in | Check | Enforcement |
|------|-----------|-------|-------------|
| Every PR reviewed; findings dispositioned | review-process | — | Convention (0 GitHub approvals required) |
| Migrations land ahead of their consumer, one store per PR | release-cycle | review | Convention |
| Scope locks change only by amendment | decisions | review | Convention |
| Every sequencing amendment since 2026-09-12 records its external evidence | decisions rule 6 | `scripts/checks/amendment_evidence.py` (`make amendment-evidence-check`): the section and its five rows are present, filled and given once, and no heading from that date names an amendment in another form (a unit test pins the CI step and the make target, which the conformance manifest no longer does); what the amendment concludes from them is review's call | Required (`Docs hygiene`) + Pre-commit |
| Live arc runs once, live, before the tag | release-cycle | the plan's release checklist | Convention, evidenced in the report |
| Version strings aligned across five files | version-bump guide | `make bump-version` | Manual in the tag PR |
| TDD for new unit-level code | CLAUDE.md §TDD | review | Convention |
| Version-train gate | release-cycle | review | Convention |
| PR body follows What / Why / How / Not in this PR / Gates / Review | BRANCHING §Pull requests | `.github/PULL_REQUEST_TEMPLATE.md` pre-fills it | Template (GitHub applies it to every new PR) |

---

## What this table says the project should change

Listed here so the matrix is honest about its own gaps.

1. ~~Make the six advisory CI jobs required.~~ Done: the owner applied the
   set versioned in [`branch-protection.json`](branch-protection.json) (all
   twelve contexts; a test keeps it equal to the CI job list), and
   `make branch-protection-show` reported the live setting equal to the
   file on 2026-09-15. It stays the one step in this list that is run by
   hand, because it needs the owner's admin token.
2. ~~Move the pre-commit-only and make-only checks into CI.~~ Done in the
   CI-promotion PR of the methodology series: gofmt, cargo fmt, ruff + mypy
   on `scripts/` and `evaluators/`, the Go integration tests, the sanitizer
   sync, `prompt_refs`, and the four doc-hygiene checks. `notices-check`
   stays make-only on purpose (see its row).
3. **Arm the perf gate** by dispatching `perf-baseline-capture.yml` once —
   after [ISSUE-0058](../issues/ISSUE-0058-perf-gate-runner-variance-tolerance.md)
   settles the runner-variance tolerance; a 20 % fixed band on shared runners
   was judged too flaky to arm during a release-prep window (2026-09-06).
4. ~~Decide the PR-size rule.~~ Decided in the BRANCHING rewrite: guidance,
   with the split heuristic code PRs follow; documentation-heavy release
   evidence is the exception and is named as such.
5. ~~Take RFC 0044 Phase 2 (evals in CI) at v0.3.16, not cut.~~ Taken:
   v0.3.16 PR C2 runs `make eval-replay TIER=stable` inside the required
   `Python` job — $0, deterministic, and every release's paid live arc
   becomes a free regression gate for the next once its golden is promoted.
6. **Re-audit of Make targets with no CI step (2026-09-12, ISSUE-0142
   step 4).** Every `*-check`, `lint-*`, `test-*` and `validate` target now
   has a CI step that runs it or its tool directly, with one exception:
   `notices-check` (Make-only on purpose, see its row). `lint-go` closed
   with PR C1 and `eval-replay` with PR C2 (item 5).
   `eval-drift`, `eval-verdict`, `release-sweep`, `release-doc` and
   `branch-protection-show` are on-demand tools, not gates.

## Related documentation

- [testing-strategy.md](testing-strategy.md) — the layers the checks guard
- [automation-catalogue.md](automation-catalogue.md) — where each check lives
- [documentation-guide.md §Size Limits](../documentation-guide.md#size-limits)
- [CONTRIBUTING.md §Quality Gates & CI](../../CONTRIBUTING.md#quality-gates--ci)
