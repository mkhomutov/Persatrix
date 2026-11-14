---
id: ISSUE-0218
summary: "The weekly audit checks published security advisories for the Rust CLI only. For the Go orchestrator and the web console the only other net is GitHub's Dependabot alerts, a repository setting no file pins; nothing tracked shows any net for the Python agents. The Docker base images have no update path at all, and no security linter runs."
status: resolved
severity: low
area: ci
created: 2026-11-10
closed: 2026-11-14
closed_pr: 1117
refs:
  - tests/unit/python/test_scheduled_audit.py
  - tests/unit/python/test_scheduled_audit_issue.py
  - tests/unit/python/test_dependabot_image_entries.py
  - docs/issues/ISSUE-0249-no-security-linter-reads-the-projects-own-code.md
  - docs/issues/ISSUE-0250-ci-builds-and-tests-on-a-go-release-with-known-vulnerabilities.md
  - docs/issues/ISSUE-0251-the-compose-file-runs-a-jaeger-line-that-has-ended.md
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

> 2026-11-14 — resolved. What changed:
>
> - **The weekly audit reads all four ecosystems.** Beside `cargo deny`
>   it now runs `govulncheck ./...`, `pip-audit` over every
>   `.github/python-constraints*.txt`, and `npm audit` on the web
>   console's lock file, each tool at an exact version. One audit failing
>   does not stop the others, any failure still fails the job, and the
>   failure issue names the audits that failed and the command each ran.
> - **Dependabot has `docker` and `docker-compose` entries** for the
>   repository root, the one directory that holds Dockerfiles and compose
>   files. It is kept from two tags: the agent image's Python, whose only
>   possible move is a minor one and belongs to
>   [ISSUE-0219](ISSUE-0219-agent-image-is-not-what-ci-tests.md)'s open
>   choice, and the Node that builds the console in the image, which a
>   test holds to the lowest release `web/package.json` admits.
>
> What this issue did not know, found while doing it:
>
> - **`alpine:3.20`, checked online.** Alpine's release page gives the
>   3.20 branch an end of support of 2026-04-01, so "spring 2026" was
>   right. The Dockerfiles are unchanged here; the new `docker` entry
>   proposes the move.
> - **The web console has only development dependencies.** Svelte and
>   Vite build the bundle the orchestrator embeds, so `npm audit` counts
>   them; leaving them out would audit nothing. It fails from "moderate"
>   up, the lowest level of any advisory
>   [ISSUE-0087](ISSUE-0087-web-toolchain-dependabot-advisories.md)
>   recorded. Two "low" findings with no fix available were open on this
>   date and are printed, not failed on.
> - **`govulncheck` answers for the Go release it runs on.** The audit
>   runs it on the newest patch release of the Go line the orchestrator
>   image builds with, where it reports nothing the code reaches. On the
>   release CI builds with it reports 37:
>   [ISSUE-0250](ISSUE-0250-ci-builds-and-tests-on-a-go-release-with-known-vulnerabilities.md).
> - **The first run will be red for one Python pin**:
>   `opentelemetry-api` 1.45.1 (GHSA-mf5v-w3w3-f6cg), fixed in 1.45.2.
>   The bump is its own change.
> - **One compose image has no newer tag to propose**: Jaeger's v1
>   line ended,
>   [ISSUE-0251](ISSUE-0251-the-compose-file-runs-a-jaeger-line-that-has-ended.md).
> - **The security linters** this issue's summary names are not turned
>   on here: a stricter linter ships with the fixes for what it finds.
>   That half, with counts, is
>   [ISSUE-0249](ISSUE-0249-no-security-linter-reads-the-projects-own-code.md).

> 2026-11-14 — four corrections from the review of the fix:
>
> - **One line of the Impact section is still true after the fix**: "a
>   published advisory in a base image or a compose image reaches
>   nothing". GitHub offers security updates for neither the `docker` nor
>   the `docker-compose` ecosystem, and no advisory audit reads an image.
>   What the fix gives the images is the monthly version update, and that
>   is their only update path.
> - **pip-audit says nothing about a pin whose marker is false on the
>   Python it runs on**, and drops it without a word. That covers a pin
>   for another platform and a pin for another Python version. The audit
>   runs on Python 3.11, as CI does, so a pin written for Python 3.12 and
>   up is audited by no run. The agent image runs 3.12 and does not
>   install from the pins at all. Auditing other Pythons goes with the
>   choice of which Python the image runs
>   ([ISSUE-0219](ISSUE-0219-agent-image-is-not-what-ci-tests.md)).
> - **An audit now waits for its own set-up steps.** As first written,
>   an audit ran even after the step that installs its tool had failed,
>   with whatever the runner had, and the opened issue then blamed a
>   dependency. It is skipped now, and the issue says the set-up failed.
> - **The advice for a Python finding names a route that works**: raise
>   the package's floor in `agents/pyproject.toml`, then `make
>   python-constraints`. The first text pointed to the two moves of every
>   pin at once, which are held while any check fails on the newest
>   releases.
