---
id: ISSUE-0218
summary: "The weekly audit checks published security advisories for the Rust CLI only. Nothing does the same for the Go orchestrator, the Python agents, the web console or the Docker base images, no security linter runs, and Dependabot is not set up for the Docker files."
status: open
severity: medium
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
---

# ISSUE-0218: Advisory scanning covers the Rust CLI only

## Summary

`.github/workflows/scheduled-audit.yml` runs every Monday and executes
`cargo deny check`, which reads the advisory database for the CLI's Rust
dependencies. The CLI is a thin REST client. The orchestrator and the
agents, where the auth, sandbox and model-facing code live, have no
equivalent.

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

Whether GitHub's own Dependabot alerts are switched on for the repository
was not checked; that is a repository setting, not a file.

## Impact

A published advisory in a Go module, a Python package, an npm package or
a base image reaches no gate and no scheduled job. Someone has to notice
it by hand.

## Proposed fix / investigation path

Add `govulncheck ./...`, `pip-audit` against the constraints file and
`npm audit` to the weekly audit, opening an issue on a finding as the
cargo step does. Add the `docker` and `docker-compose` ecosystems to
Dependabot.

## Notes

> 2026-11-10 — found by a whole-repository quality review of `main` at
> `ee9cfaac`; confirmed by reading the workflow and config files. No
> dependency was checked against an advisory database.
