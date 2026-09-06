# Automation Catalogue

> **Last updated**: 2026-09-06
> Everything that runs without a human typing the steps: `make` targets,
> scripts, the pre-commit hook, and the GitHub workflows — grouped by
> purpose, with **when it runs**. `make help` is the live list of targets;
> this page adds the *when* and the *why*.

Conventions: every target carries a `##` help line and is printed by
`make help`. Scripts under `scripts/` are Python-stdlib-only and
cross-platform (Windows, macOS, Linux); `scripts/checks/` shares a small
framework (`walking.py` file walker, `patterns.py` scanner and reporter,
`analysis.py` allow-comment helper, UTF-8 stream helpers). `PYTHON` defaults
to `python3`; pass `PYTHON=.venv/bin/python` when the repo venv holds the
dependencies.

---

## Build and run

| Target | Does | When |
|--------|------|------|
| `make all` | `proto` + `build` | Fresh checkout; after a proto change |
| `make build` / `build-orchestrator` / `build-cli` / `build-agents` | Go binary → `bin/persatrix-server`; Rust → `cli/target/release/persatrix`; `pip install -e ".[dev]"` | Development |
| `make ui` / `build-orchestrator-ui` / `run-ui` | Vite bundle into `internal/ui/assets/` (overwrites the tracked placeholder `index.html` — restore it before committing); orchestrator with the bundle embedded; local console iteration | Console work; release asset lane |
| `make run` / `run-agent AGENT= PORT=` | Run the orchestrator / one Python agent | Manual tests |
| `make docker-build` / `docker-up` / `docker-down` / `docker-logs` / `reset` | Compose lifecycle; `reset` purges **all** named volumes (ISSUE-0051 workaround) | Live arcs start from `make reset` |
| `make demo-offline` / `demo-autonomous` / `demo-ollama` / `demo-anthropic` / `demo-openai` / `demo-gemini` / `demo-watsonx` | The demo society on one provider via a per-provider alias overlay; the first two spend $0 | `demo-autonomous` is the offline smoke in release-prep PRs 1 and 4 |
| `make clean` | Remove build artifacts | — |

## Code generation and sync gates

| Target / script | Does | Check twin | When the check runs |
|-----------------|------|------------|---------------------|
| `make proto` (`proto-go`, `proto-python`) | Regenerate Go and Python gRPC stubs, incl. `.pyi` via mypy-protobuf. Pinned toolchain: protoc 34.1, protoc-gen-go 1.36.11, protoc-gen-go-grpc 1.6.1 (local brew tools are newer and fail the gate) | `make proto-check` = `proto-python-check` + `proto-orphans-check`; Go side is `make proto-go && git diff --exit-code internal/generated/` | CI (`Proto staleness`, `Python`) |
| `make generate-sanitizer-patterns` | Regenerate `agents/security_patterns.py` + `security_enums.py` from the Go canonical sources | `generate-sanitizer-patterns-check` | CI (`Go`) |
| `make notices` (`scripts/generate_third_party_notices.py`) | Regenerate `THIRD_PARTY_NOTICES.md` from the three dependency graphs | `notices-check` | Make-only; release-prep PR 4 |
| `make rfcs` (`scripts/rfcs.py`) | Regenerate `docs/rfcs/INDEX.md` from RFC YAML front-matter | `rfcs-check` | CI (`Validate configs`) + pre-commit |
| `make issues` (`scripts/issues.py`) | Regenerate `docs/issues/INDEX.md` from issue front-matter | `issues-check` | CI (`Validate configs`) |
| `make merged-prs` (`scripts/merged_prs.py`) | Regenerate `docs/merged-prs.md` from the first-parent squash subjects on `main` (Area column derived from the title). Replaced ROADMAP's hand-kept table, which had stopped at #708 | `merged-prs-check` — passes when behind by the newest merges only | Pre-commit regenerates + stages it; CI (`Docs hygiene`) checks it |
| `scripts/generate_filemap.py` | Regenerate `FILEMAP.md` from `git ls-files` (tracked files only — `git add` new files first). **Kept on purpose** (decision 2026-09-06): it is the one-read index assistants load before touching the tree, and the writer now leaves the date alone so it no longer churns on every commit | `--check` (ignores the header date) | Pre-commit regenerates and stages it; CI (`Docs hygiene`) checks it |
| `make generate-persona-nickname COUNT= SEED=` (`scripts/persona_nickname_generator.py`) | Nickname-style persona id/name pairs | — | On demand |
| `make bump-version VERSION=X.Y.Z [DRY_RUN=--dry-run]` (`scripts/bump_version.py`) | Bump the five version strings ([guide](../guides/version-bump.md)) | checklist §2 | Release-prep PR 3 |
| `make release-doc KIND= VERSION= CODENAME=` (`scripts/release/open_doc.py`) | Open a plan / scope-locks / release-prep plan / release checklist / execution report from its template with version, codename, previous version and date filled and the guidance blockquotes removed; never overwrites | — | Phase 0, release-prep PRs 0–2 |
| `make release-sweep [RUN=1] [REPORT=path] [ONLY=…] [SKIP=…]` (`scripts/release/sweep.py`) | The checklist §1 gates as one command; dry-run prints the plan, `RUN=1` runs them and prints the execution-report results table (Docker smoke opt-in) | — | Release-prep PR 4; PR 1's structural-gates table |

## Tests

| Target | Runs | CI job |
|--------|------|--------|
| `make test` | `test-go test-python test-agents test-integration` — four legs, **not** Rust, web, evals, or Go integration | — |
| `make test-go` | `go test ./internal/... -v -race -cover` | `Go` |
| `make test-python` | `pytest tests/unit/python/` (~5.5 min) | `Python` |
| `make test-agents` | `pytest agents/tests/ -c agents/pyproject.toml` from the repo root | `Python` |
| `make test-integration` | `pytest tests/integration/` with `PYTHONPATH=agents/generated` | `Python` |
| `cd cli && cargo test` | Rust suite incl. lockstep guards | `Rust` |
| `make ui-test` | `npm ci && npm test` (Vitest) | `Web console` |
| `go test ./tests/integration/... -race` | Go integration (bufconn scheduler→executor, rate limiter, audit log) | `Go` |
| `make eval-replay [TARGET= REPORT=]` | Replay goldens deterministically under the offline overlay | none |
| `make eval-record` / `eval-record-offline TARGET=` / `eval-drift` | Record a golden live / against the mock; report live drift (never gates) | on demand |
| `python tests/perf/personal_tier_latency.py [--capture-baseline PATH]` | Recall latency vs baseline | `Python` (informational) |
| `scripts/perf/wallet_p99.py` | Wallet acquire+settle p99 harness (RFC 0023) | on demand |

## Lint and static checks

| Target / script | Does | Where it runs |
|-----------------|------|---------------|
| `make lint` | `lint-go` (golangci-lint) + `lint-python` (ruff + mypy on `agents/`, `tests/`, `evaluators/`; `imports-check`) + `lint-rust` (clippy `-D warnings`) | Locally; CI runs the same tools directly |
| `make imports-check` | import-linter forbidden contract, MIT↛BUSL (RFC 0045 §B) | CI (`Python`) |
| `make validate` | `agents/validate.py config/` + `scripts/checks/prompt_refs.py` | CI (`Validate configs`) runs both |
| `make ui-html-check` (`scripts/checks/ui_html_directive.py`) | Reject `{@html}` under `web/src` | CI (`Web console`) |
| `make dockerignore-check` (`scripts/checks/dockerignore_context.py`) | Seed a sentinel, run a real `docker build`, prove the context excludes nested `node_modules` | CI (`Dockerignore`) |
| `make check-licenses` (`-go`/`-python`/`-rust`) | go-licenses, `scripts/checks/python_licenses.py`, cargo-deny against `scripts/checks/allowed_licenses.txt` / `deny.toml` | CI (`Third-party license check`) |
| `scripts/checks/file_size.py [--strict] [--near-cap]` | Code ≤ 500 lines, docs ≤ 3 000 words, RFCs ≤ 8 000; allowlist in `file_size_allowlist.py`; near-cap band 3 %; version-cycle docs of released versions (dated CHANGELOG heading) excluded (ISSUE-0139) | CI (`File size check`) + pre-commit |
| `scripts/checks/doc_links.py` | Relative links and `#anchors` in every tracked `.md` | CI (`Docs hygiene`) + pre-commit |
| `scripts/checks/doc_status_markers.py` | Only the standard status markers | CI (`Docs hygiene`) + pre-commit |
| `scripts/checks/doc_leaked_markup.py` | No tool-call markup fragments in docs | CI (`Docs hygiene`) + pre-commit |
| `scripts/checks/plan_status.py` (`make plan-status-check`) | A 🔀 / ⬜ progress row whose linked PRs have all merged is stale; released versions' plans are skipped, 🔄 rows are not judged | CI (`Docs hygiene`) + pre-commit |
| `scripts/checks/released.py` | Shared: which versions shipped (dated CHANGELOG headings) and which version-cycle docs are therefore frozen — used by the size checker and the plan-status checker | library |
| `scripts/_git.py` | The one read-only git call (ISSUE-0135); new call sites use it | library |
| `scripts/checks/doc_audit.py [--format text\|json\|markdown]` | Runs links + markers + size warnings in one report | Local convenience; used by hand in PR bodies |
| `scripts/checks/proto_drift.py` | Orphan generated protobuf artifacts (backs `proto-orphans-check`) | CI |

## The pre-commit hook

`python scripts/install_hooks.py [--force]` writes a hook into whatever
directory `git rev-parse --git-path hooks` names (so linked worktrees and
`core.hooksPath` both work). The hook runs `scripts/pre_commit.py`, which is
**version-controlled** and warns when the installed hook has drifted from the
installer. Eleven steps, target under 10 s:

0. regenerate `FILEMAP.md` and `docs/merged-prs.md` and `git add` them ·
1. `gofmt -l` on staged Go blobs (CRLF-safe) · 2. `ruff check agents/` ·
3. `cargo fmt --check` · 4. doc links · 5. leaked markup · 6. status markers ·
7. RFC index freshness · 8. file sizes (`--strict`) · 9. plan status (no
"PR open" row for a merged PR).

Because the hook is outside version control it is absent for anyone who did
not run the installer. Since the CI-promotion PR every step has a CI
counterpart: gofmt and cargo fmt in the `Go` / `Rust` jobs, ruff in `Python`,
the doc checks and FILEMAP freshness in `Docs hygiene`, the RFC index and file
sizes in `Validate configs` / `File size check`. The hook is the fast local
copy, not the only copy.

## GitHub workflows

| Workflow | Trigger | Does |
|----------|---------|------|
| `ci.yml` | push to `main`, every PR | Eleven jobs: `Go (build + test)` (incl. gofmt, Go integration tests, sanitizer sync), `Web console (build + test)`, `Dockerignore context hygiene`, `Proto staleness check`, `Python (lint + test)` (incl. ruff/mypy on `scripts/` + `evaluators/`), `Cost regression gate (bored persona)` (path-filtered), `Rust (build + clippy)` (incl. rustfmt, `cargo test`), `Validate configs` (incl. `prompt_refs`), `Docs hygiene` (links, markup, markers, FILEMAP, merged-PR history, plan status), `File size check`, `Third-party license check`. Every job carries a comment naming the incident it guards. |
| `commitlint.yml` | PR opened/edited/synchronised | Conventional Commit PR title (`Validate PR Title`) |
| `scheduled-audit.yml` | Mondays 06:00 UTC; manual | `cargo deny check advisories bans sources licenses`; opens or comments on a `Scheduled Dependency Audit Failure` issue |
| `perf-baseline-capture.yml` | manual (`workflow_dispatch`) | Captures the recall-latency baseline on a runner and opens a PR with it; merging arms the perf gate. Never run yet |

## Manual-test automation

`scripts/manual_tests/` (2026-09): a machine-paced **driver** for the release
arc (`mt_group_tenant_001.py`, dry-run by default, `--execute` to spend), a
**preflight** of three-state vacuity gates (`mt_group_tenant_preflight.py`,
`mt_gate.py`), **evidence collectors** that print the report's tables
verbatim (`mt_group_tenant_evidence.py`), and the operations vocabulary the
driver is written in (`mt_group_tenant_ops.py`). The pattern — preflight,
pace, collect, leave the verdict to the operator — is the template for the
next release's driver.

## Changelog

`cliff.toml` configures git-cliff: Conventional Commit groups
(features, fixes, security, performance, refactoring, docs, …), `chore(release)`
skipped, tags `v[0-9].*`. `git-cliff --tag vX.Y.Z --unreleased --prepend
CHANGELOG.md` at release-prep PR 3, then hand curation into one bullet per
story.

## Related documentation

- [enforcement-matrix.md](enforcement-matrix.md) — required vs advisory vs local-only
- [testing-strategy.md](testing-strategy.md) — the layers these targets run
- [CONTRIBUTING.md §Quality Gates & CI](../../CONTRIBUTING.md#quality-gates--ci)
- [Makefile](../../Makefile) — `make help`
