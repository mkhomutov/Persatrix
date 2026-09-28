---
id: ISSUE-0177
summary: "The orchestrator's own Go tests under cmd/ run in no CI job and no make target: CI and `make test-go` test only ./internal/..., so a change that breaks the startup wiring they cover merges green. None has ever run in CI; the first landed on 2026-04-17."
status: resolved
severity: medium
area: ci
created: 2026-09-28
closed: 2026-09-28
closed_pr: 1019
refs:
  - .github/workflows/ci.yml
  - Makefile
  - cmd/orchestrator/
  - docs/methodology/testing-strategy.md
  - docs/issues/ISSUE-0076-full-integration-suite-not-run-in-ci.md
---

# ISSUE-0177: The orchestrator's tests under cmd/ run in no CI job

## Summary

`cmd/orchestrator/` holds twelve test files: 47 tests, plus 20 subtests, of
the orchestrator's startup wiring. They check that a mistyped `auth.mode`
stops the start instead of running without authentication, that the first
operator account can be created only once, that a panicking gRPC handler
does not take the server down, and how the rate-limit, epoch, session and
unquarantine-token environment variables are read. CI's `Go` job runs
`go test ./internal/... -v -race -cover` and
`go test ./tests/integration/... -v -race`, and `make test-go` runs the
first. Neither pattern reaches `cmd/`, and no other workflow runs
`go test`. These tests ran only when someone ran them by hand.

## Context

Found on 2026-09-28 while fixing ISSUE-0176
([#1017](https://github.com/mkhomutov/Persatrix/pull/1017)): its
regression tests for the orchestrator's stop, `TestServeAgentGRPC_*` in
`cmd/orchestrator/grpcserver_test.go`, would never have run in CI. The job
has tested `./internal/...` alone since the repository's first commit on
2026-04-08, and the first `cmd/` test arrived nine days later
([#90](https://github.com/mkhomutov/Persatrix/pull/90)). The same job
builds `cmd/orchestrator` and checks its formatting, and since v0.3.16 PR C1
its lint step type-checks the test files too. So a test that stops
compiling fails CI; a test that compiles and fails does not.

This is the fifth test tree the repository has found with no runner, after
`agents/tests/`, the Rust suite, the Python integration tier
([ISSUE-0076](ISSUE-0076-full-integration-suite-not-run-in-ci.md)) and the
Go integration tests
([testing strategy](../methodology/testing-strategy.md#the-one-rule-that-history-paid-for)).

## Impact

Nothing is broken today: all 47 tests pass on `main` at `b6804d13`. But a
change that breaks what they check, such as the refusal to start on a
malformed security config or the recovery from a handler panic, would merge
with every check green. It would surface only when someone next ran
`go test ./...` by hand, or in production.

## Fix

The `Go` job's test step and `make test-go` now run
`go test ./internal/... ./cmd/... -v -race -cover`. The tests needed no
changes. Before the gate was added they were run as CI will run them:

- with `-race` on macOS: all 47 pass;
- on Linux, in a `golang:1.26-alpine` container without `-race`: all pass;
- 20 times in shuffled order with `-race`: no failure;
- the same checks on the ISSUE-0176 fix's tree, which adds four
  `TestServeAgentGRPC_*` tests: all 51 pass.

The step names the two trees rather than `./...`, which would also run
`tests/integration/` a second time, ahead of its own step.
`cmd/genpatterns` has no tests yet; `./cmd/...` runs them once it has.

The [testing strategy](../methodology/testing-strategy.md),
[enforcement matrix](../methodology/enforcement-matrix.md),
[automation catalogue](../methodology/automation-catalogue.md),
[review process](../methodology/review-process.md),
[CONTRIBUTING](../../CONTRIBUTING.md) and the
[execution-report template](../templates/EXECUTION_REPORT_TEMPLATE.md) name
the new command.
