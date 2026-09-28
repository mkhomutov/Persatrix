---
id: ISSUE-0177
summary: "The orchestrator's own Go tests under cmd/ run in no CI job and no make target: no `go test` pattern in CI or in `make test-go` reaches cmd/, so a change that breaks the startup wiring they cover merges green. None has ever run in CI; the first landed on 2026-04-17."
status: resolved
severity: medium
area: ci
created: 2026-09-28
closed: 2026-09-29
closed_pr: 1019
refs:
  - .github/workflows/ci.yml
  - Makefile
  - cmd/orchestrator/
  - tests/unit/python/test_go_test_gate_ci.py
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
`cmd/orchestrator/grpcserver_test.go`, would never have run in CI. No
`go test` pattern CI has run since it was added
([#1](https://github.com/mkhomutov/Persatrix/pull/1), 2026-04-08) reached
`cmd/`, and the first `cmd/` test arrived nine days later
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

`make test-go` now runs `go test ./... -v -race -cover`: every package in
the module, so a new Go test tree runs with no edit. The required `Go` job
runs `make test-go` instead of its own copy of the command, and its separate
Go integration step folded into it, since `./...` includes
`tests/integration/`. `tests/unit/python/test_go_test_gate_ci.py` pins both
halves: the recipe tests `./...` with `-race`, and the Go job runs
`make test-go` and no other `go test`. The first version of the fix added
`./cmd/...` to both hand-made lists; review replaced the two lists with one
pattern in one place.

The tests needed no changes. They pass in CI on Linux with `-race`, and
locally with `-race`, once and 20 times in shuffled order. They also pass on
Linux without `-race`, in a local `golang:1.26-alpine` container. #1017's
head `df9ee1c5`, which adds nine tests (56 in all), passes with `-race` once
and 10 times shuffled. A throwaway failing test in `cmd/orchestrator`, added
through `go test -overlay` so nothing entered the tree, makes `make test-go`
fail; the old `./internal/...` command passed with it in place.
