---
id: ISSUE-0250
summary: "CI builds and tests the orchestrator with exactly the Go release `go.mod` names, 1.26.0, while the image builds with the newest patch release of that line. On Go 1.26.0 `govulncheck` reports 37 standard-library vulnerabilities the orchestrator's code reaches; on Go 1.26.11 it reports none. Anyone who builds from source with the oldest Go the project accepts gets the first binary."
status: open
severity: low
area: ci
created: 2026-11-14
refs:
  - go.mod
  - Dockerfile.orchestrator
  - .github/workflows/ci.yml
  - .github/workflows/scheduled-audit.yml
  - tests/unit/python/test_toolchain_minimums.py
  - tests/unit/python/test_dockerfile_go_toolchain.py
  - docs/issues/ISSUE-0218-advisory-scanning-covers-rust-only.md
  - docs/issues/ISSUE-0219-agent-image-is-not-what-ci-tests.md
---

# ISSUE-0250: CI builds and tests on a Go release with known vulnerabilities

## Summary

`go.mod` says `go 1.26.0`. That line is the oldest Go the project
accepts, and it is also the exact release every CI job installs
(`go-version-file: go.mod`; a CI log of 2026-11-14 shows `go version
go1.26.0`). `Dockerfile.orchestrator` builds on `golang:1.26-alpine`, a
tag that follows the newest 1.26 patch release, 1.26.11 on that date.

So the orchestrator that CI tests and the one the image ships are built
by Go releases eleven patch releases apart, and most of those patch
releases were security fixes.

## Context

`govulncheck ./...` (v1.9.0) on `main` at `fb0e69b8`, same code, three Go
releases:

| Go release | Vulnerabilities the code reaches | Where |
|---|---|---|
| 1.26.0 (CI, and the oldest accepted) | 37 | all in the standard library: `net/http`, `crypto/tls`, `crypto/x509`, `html/template`, `net/url` and others |
| 1.26.3 | 23 | the same packages |
| 1.26.11 (the image, on a fresh pull) | 0 | |

The weekly audit added for
[ISSUE-0218](ISSUE-0218-advisory-scanning-covers-rust-only.md) runs on
the newest patch release of the image's Go line, so it does not report
these. That is deliberate: on Go 1.26.0 it would be red every Monday for
findings that no dependency bump can clear.

Nothing built by CI is shipped, so this is not an exposed fault today.
What is exposed:

- A build from source (`make build`, the path the README offers beside
  Docker) uses whatever Go the builder has, down to 1.26.0, and nothing
  says that release is unpatched.
- A fault that only a newer patch release shows is not seen by CI. This
  is the Go side of
  [ISSUE-0219](ISSUE-0219-agent-image-is-not-what-ci-tests.md), which
  records the same gap for the agent image's Python.
- An image built from a cached `golang:1.26-alpine` layer has whatever
  patch release was current when the layer was pulled.

## Impact

Proposed low: the shipped image, freshly built, has none of the 37. The
severity is proposed here, not ruled.

## Proposed fix / investigation path

A choice for the maintainer, between two that both work:

1. **Raise the `go` line to the newest patch release** when the audit or
   a Go security release says so (`go 1.26.11`). Every build then uses a
   patched toolchain or a newer one, since the `go` command fetches the
   release `go.mod` asks for. The docs' "Go 1.26+" stays true:
   `test_toolchain_minimums.py` compares major and minor only.
2. **Keep `go 1.26.0` as the oldest accepted release and have CI use the
   newest patch release** of the line, with one step that still compiles
   on exactly 1.26.0, the way the Rust job checks the CLI's oldest Rust.

Either way, the weekly audit's Go step can then read the same release CI
uses.

## Notes

> 2026-11-14 — measured while fixing
> [ISSUE-0218](ISSUE-0218-advisory-scanning-covers-rust-only.md). The
> three runs differ only in `GOTOOLCHAIN`.
