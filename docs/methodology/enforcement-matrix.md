# Enforcement Matrix

> **Last updated**: 2026-11-12
> Every rule the project states, with the document that states it, the check
> that enforces it, and how hard the enforcement is. Read the **Enforcement**
> column literally: a rule is only as strong as the weakest place it is
> checked. Branch protection was read with `gh api` on 2026-09-15.
> The rules for code and builds are here; those for documents and process
> are in [enforcement-matrix-documents.md](enforcement-matrix-documents.md).

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
| Go builds; all Go tests pass with `-race` | CONTRIBUTING | `go build ./cmd/orchestrator`; `make test-go` (`go test ./...`), pinned by `test_go_test_gate_ci.py` | Required (`Go`) — `cmd/` since ISSUE-0177 |
| Every test tree has a named runner | testing-strategy | Go: `./...` reaches every package; `test_every_test_file_has_a_runner.py` names a test file where no CI step runs it | Required (`Go`, `Python`); Convention for opt-in tests and Rust files no `mod` reaches ([ISSUE-0178](../issues/ISSUE-0178-runner-rule-checked-only-for-go.md)) |
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
| Go and Python protobuf stubs match `proto/*.proto`; no orphans | Makefile; ISSUE-0017/0023; CONTRIBUTING | `make proto-go && git diff --exit-code`; `make proto-python-check proto-orphans-check`; on the protoc release `.github/actions/setup-protoc` pins with its SHA-256, which `test_protoc_pin.py` holds every workflow to | Required (`Proto staleness`, `Python`) |
| MIT-candidate primitives never import BUSL code (RFC 0045 §B) | RFC 0045; CONTRIBUTING | `make imports-check` (import-linter) | Required (`Python`) |
| No module in the agents package imports a sibling by an absolute name, `agents.…` or `persatrix_agents.…`: the package runs under both, and an absolute import loads a second copy or none | [ISSUE-0210](../issues/ISSUE-0210-agent-package-runs-under-two-import-roots.md); the test's docstring | `test_agents_package_imports_are_relative.py` reads every `import` statement and every string import with a literal name, outside the package's tests and generated stubs; the `Python` job also starts the installed package from an empty folder with `python -P`, which stops on such an import if it runs at start-up | Required (`Python`) — since 2026-11-11; ruff's banned-import rule cannot tell a relative import from an absolute one here |
| Every write statement in `agents/memory/` ends its transaction on every path: one that returns, raises or is cancelled without a commit keeps the database's write lock and shuts the persona's other memory tiers out | [ISSUE-0197](../issues/ISSUE-0197-zero-row-write-keeps-the-memory-write-lock.md); `agents/memory/_write_boundary.py` | `test_memory_write_boundary.py` reads the package and fails on an `INSERT`, `UPDATE`, `DELETE` or `REPLACE` run outside `execute_write`, `write_boundary` and `@ends_transaction`, and on a statement whose text it cannot read, each outside a reviewed list; `test_memory_write_releases_the_lock.py` runs the writes on two tiers of one database | Required (`Python`) — since 2026-11-12; migrations are exempt (the runner rolls back), and the check sees only statements run through a connection's `execute` family inside `agents/memory/` |
| The CLI compiles on the oldest Rust it claims, `rust-version` in `cli/Cargo.toml` | `cli/Cargo.toml`; README | the `Rust` job's last step installs that release and runs `cargo +<rust-version> check --locked --all-targets`, so a dependency update that needs a newer Rust fails there; clippy's `incompatible_msrv` lint, under `-D warnings`, names any newer standard-library API in CLI code; `test_toolchain_minimums.py` pins the step | Required (`Rust`, `Python`) — since 2026-09-26; CI had only ever built with the newest release |
| Rust builds; clippy clean; `cargo test` passes (incl. lockstep guards) | instructions | `cargo build`, `cargo clippy -- -D warnings`, `cargo test` | Required (`Rust`) |
| CI builds and lints the CLI on one pinned Rust release, so a new release cannot fail every open PR at once | `cli/rust-toolchain.toml`; `.github/dependabot.yml` | every job that runs cargo installs what the file names (`rustup toolchain install` in `cli/`) and runs cargo there; Dependabot proposes each release as one PR; `test_rust_toolchain_pin.py` fails a workflow that installs or names a toolchain of its own | Required (`Rust`, `Python`) — since 2026-09-26; CI used to install whatever `stable` was that day |
| YAML configs validate against `schemas/` | CLAUDE.md; instructions | `python agents/validate.py config/` | Required (`Validate configs`) |
| No agent in the shipped `config/agents.yaml` lists a shell command nobody has reviewed: a listed command is trusted with every argument it accepts and runs outside the path rules | [ISSUE-0196](../issues/ISSUE-0196-shipped-shell-allowlist-voids-the-file-and-network-rules.md); the comment in `config/agents.yaml` | `test_builtin_tools_schema_valid_config.py` fails on any listed command that is not in its reviewed set, which is empty | Required (`Python`) — since 2026-11-12; a commented example and an operator's own config are not read |
| The shipped agents' path rules keep to the comment beside them: the `code-writer` writes only under `src/` and `tests/` and to top-level files named exactly, and no shipped agent with file rules reads or writes a `.env*` file or a `.git` folder's contents | [ISSUE-0216](../issues/ISSUE-0216-path-globs-match-at-any-depth.md); the comment in `config/agents.yaml` | `test_sandbox.py` fails on a `code-writer` write rule that is neither under `/workspace/src/` or `/workspace/tests/` nor a top-level path with no wildcard (`*`, `?`, `[`), and on a shipped agent whose file rules let a sample secrets path through | Required (`Python`) — since 2026-11-12; holds where file names are case-sensitive ([ISSUE-0234](../issues/ISSUE-0234-deny-rules-miss-a-path-spelled-in-another-case.md)); skipped on Windows; an operator's own config is not read |
| RFC and issue INDEX files fresh; front-matter valid; each RFC's `**Status**` header line agrees with its front-matter | rfcs/README, issues/README | `make rfcs-check issues-check` | Required (`Validate configs`) |
| PR title is a Conventional Commit | CONTRIBUTING; BRANCHING | `commitlint.yml` | Required (`Validate PR Title`) |
| Web console unit tests pass; bundle builds; orchestrator compiles with it | web-console guide | `make ui-test`, `make ui`, `go build` | Required (`Web console (build + test)`) |
| No `{@html}` under `web/src` (session-riding XSS) | RFC 0039 amendment §A3 | `make ui-html-check` | Required (`Web console (build + test)`) |
| `.dockerignore` excludes nested `node_modules` | ISSUE-0104 | `make dockerignore-check` | Required (`Dockerignore context hygiene`) |
| Third-party licences on the allow-list (Go, Python, Rust) | Makefile; `allowed_licenses.txt`; `deny.toml` | `make check-licenses`, with go-licenses at the Makefile pin (`GO_LICENSES_VERSION`: the target refuses any other version, and CI installs it with `make go-licenses-install`) | Required (`Third-party license check`) — go-licenses pinned since 2026-09-26; was `@latest` |
| Idle persona spends nothing (RFC 0024) | RFC 0024 §Test Strategy | `test_bored_persona_cost.py`, path-filtered | Required (`Cost regression gate (bored persona)`) |
| `gofmt` / `cargo fmt` clean | instructions | `gofmt -l` over every tracked `.go` file; `cargo fmt -- --check` | Required (`Go`, `Rust`) + Pre-commit (hook covers `internal/`, `cmd/` only) |
| Python sanitizer patterns/enums match the Go canonical source | Makefile (RFC 0009 PR 3) | `make generate-sanitizer-patterns-check` | Required (`Go`) |
| `THIRD_PARTY_NOTICES.md` matches the dependency graphs | Makefile | `make notices-check`, with go-licenses and cargo-license at the Makefile pins | Make-only — deliberately: the notices file is regenerated in the tag PR, so it is legitimately stale between a dependency bump and the next release (it is stale today) |
| `agents.yaml` `instructions_file` references resolve | prompt-organization | `scripts/checks/prompt_refs.py` | Required (`Validate configs`) |
| Personal-tier recall latency within 20 % of baseline | RFC 0029 | `tests/perf/personal_tier_latency.py` | Required job, **informational** until a baseline exists |
| Weekly Rust advisory / bans / sources audit | CONTRIBUTING | `scheduled-audit.yml` (cargo-deny, Mondays; files an issue on failure) | Scheduled |
| Dependencies bumped monthly, one grouped PR per ecosystem (Go, pip, Cargo, the Rust toolchain, npm, Actions), `chore(deps)` titles | `.github/dependabot.yml` | Dependabot; for pip, the ranges in `agents/pyproject.toml` only (CI's pins move through `python-constraints-refresh.yml`); for the Rust toolchain, the release in `cli/rust-toolchain.toml` | Scheduled (security updates run on their own cadence) |
| Required status checks on `main` match the versioned set | `branch-protection.json` | `make branch-protection-show`; `test_branch_protection_config.py` pins the file to the CI job list | Owner applies; file is reviewed |

## Size, documentation and process

The rules about file size and shape, documents, and how work is
planned and merged are in
[enforcement-matrix-documents.md](enforcement-matrix-documents.md), with
the same columns and the levels above.

---

## What this table says the project should change

The matrix's own gaps, mostly closed, are listed in
[enforcement-gaps.md](enforcement-gaps.md).

## Related documentation

- [testing-strategy.md](testing-strategy.md) — the layers the checks guard
- [automation-catalogue.md](automation-catalogue.md) — where each check lives
- [enforcement-matrix-documents.md](enforcement-matrix-documents.md) — the rules for size, documents and process
- [enforcement-gaps.md](enforcement-gaps.md) — what the matrix says the project should change
- [documentation-guide.md §Size Limits](../documentation-guide.md#size-limits)
- [CONTRIBUTING.md §Quality Gates & CI](../../CONTRIBUTING.md#quality-gates--ci)
