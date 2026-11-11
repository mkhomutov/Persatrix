---
id: ISSUE-0218
summary: "The weekly audit checks published security advisories for the Rust CLI only. For the Go orchestrator and the web console the only other net is GitHub's Dependabot alerts, a repository setting no file pins; nothing tracked shows any net for the Python agents. The Docker base images have no update path at all, and no security linter runs."
status: open
severity: low
area: ci
created: 2026-11-10
refs:
  - .github/workflows/scheduled-audit.yml
  - .github/dependabot.yml
  - .golangci.yml
  - ruff.toml
  - Dockerfile.agent
  - Dockerfile.orchestrator
  - docker-compose.yaml
  - deny.toml
  - docs/issues/ISSUE-0087-web-toolchain-dependabot-advisories.md
---

# ISSUE-0218: Advisory scanning covers the Rust CLI only

## Summary

`.github/workflows/scheduled-audit.yml` runs every Monday and executes
`cargo deny check`, which reads the advisory database for the CLI's Rust
dependencies. The CLI is a thin REST client. The orchestrator and the
agents, where the auth, sandbox and model-facing code live, have no
equivalent check in the repository.

## Context

A search of `.github`, the Makefile, `scripts/` and the lint configs
finds none of these:

| Ecosystem | Usual tool | Present |
|---|---|---|
| Go modules | `govulncheck` | no |
| Python packages | `pip-audit` | no |
| npm packages | `npm audit` | no |
| Go code | `gosec` | no (`.golangci.yml` enables five linters, none for security) |
| Python code | bandit rules in ruff | no |

`.github/dependabot.yml` covers `gomod`, `pip`, `cargo`,
`rust-toolchain`, `npm` and `github-actions`. It has no `docker` or
`docker-compose` entry, so base images and the compose services have no
update path:

- `Dockerfile.orchestrator` ends on `alpine:3.20`. By Alpine's two-year
  support window that release left support in spring 2026 (not re-checked
  online).
- `Dockerfile.agent` uses the moving tag `python:3.12-slim`.
- The collector, Loki and Prometheus images in `docker-compose.yaml` are
  2024 releases.

GitHub's own Dependabot alerts and security updates are on. The comment
at the top of `.github/dependabot.yml` says security updates ran before
the file existed, for Go, npm and Cargo, and
[ISSUE-0087](ISSUE-0087-web-toolchain-dependabot-advisories.md) records
three alerts raised on the web console's lock file. So an advisory
against a Go, npm or Cargo dependency raises an alert and a PR. Nothing
tracked shows the same for a Python package. This net is a repository
setting, not a file, so nothing in the repository holds it in place.

## Impact

- A published advisory in a base image or a compose image reaches
  nothing.
- One in a Go module or an npm package reaches a Dependabot alert, and
  no gate or scheduled job in the repository.
- For a Python package there is no evidence of either.
- No check reads the project's own code for security faults.

Rated low because the Dependabot net does exist for three of the four
ecosystems.

## Proposed fix / investigation path

Add `govulncheck ./...`, `pip-audit` against the constraints file and
`npm audit` to the weekly audit, opening an issue on a finding as the
cargo step does. Add the `docker` and `docker-compose` ecosystems to
Dependabot.

## Notes

> 2026-11-10 — found by a whole-repository quality review of `main` at
> `ee9cfaac`; confirmed by reading the workflow and config files. No
> dependency was checked against an advisory database.
