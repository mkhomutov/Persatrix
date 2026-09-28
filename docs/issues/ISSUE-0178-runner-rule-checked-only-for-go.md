---
id: ISSUE-0178
summary: "13 tests marked requires_orchestrator, three of them $0 EXP-001 real-process tests, are skipped by every CI job, make target and release-sweep gate. The issue's other half, that nothing checked a new Python, Rust or web test tree got a runner, is now checked by a unit test."
status: open
severity: low
area: ci
created: 2026-09-28
refs:
  - docs/methodology/testing-strategy.md
  - docs/methodology/enforcement-matrix.md
  - tests/conftest.py
  - agents/pyproject.toml
  - tests/integration/test_exp001_arm_d_meetings.py
  - tests/integration/test_exp001_channel_meeting.py
  - tests/integration/test_logs_e2e.py
  - tests/integration/test_session_operator_surface.py
  - docs/issues/ISSUE-0177-cmd-go-tests-run-in-no-ci-job.md
  - tests/unit/python/test_every_test_file_has_a_runner.py
---

# ISSUE-0178: The rule that every test tree has a runner is checked only for Go

## Summary

Since [ISSUE-0177](ISSUE-0177-cmd-go-tests-run-in-no-ci-job.md),
`make test-go` tests `./...`, so every Go test runs in CI by construction, and
a unit test holds that in place. Nothing does the same for the rest of the
repository:

- **Opt-in tests that no gate runs.** 13 tests carry the
  `requires_orchestrator` marker: 2 in `test_exp001_arm_d_meetings.py`, 1 in
  `test_exp001_channel_meeting.py`, 5 in `test_logs_e2e.py` and 5 in
  `test_session_operator_surface.py`. `tests/conftest.py` skips them unless
  pytest runs with `-m requires_orchestrator`. No workflow, make target or
  release-sweep gate does that, and no CI job builds the orchestrator binary
  they start. The manual command is written down, in the marker's entry in
  `agents/pyproject.toml` and in each file's docstring, but nothing runs it.
- **New test trees that nobody checks.** A new Python, Rust or web test tree
  gets a runner only if its PR adds one. Review is the only check, and
  `agents/tests/` ran in no job for 180 days that way.

## Context

Found in the review of the ISSUE-0177 fix
([#1019](https://github.com/mkhomutov/Persatrix/pull/1019)), which made the
Go half structural. Under CI's own integration command the four files report
`13 skipped`. Three of the tests are EXP-001 harness tests on real processes,
with every model alias on the offline mock, so they cost nothing. One of them,
`test_a_briefing_that_ends_by_the_idle_window_keeps_its_memory`, covers the
path ISSUE-0172 fixed. The
[v0.3.5 execution report](../manual-tests/v0.3.5-execution-report.md) records
`test_session_operator_surface.py` as carried by CI, but CI skips it.

## Impact

A change that breaks what these tests check merges with every check green:
arm D's memory across restarts, the idle-window briefing, and the logs and
session CLI surfaces. The tests report "skipped", not "failed". A new Python
test directory outside the three pytest roots would be linted and
type-checked, but never run.

## Proposed fix / investigation path

- **Opt-in tests:** a CI step that builds the orchestrator (and, for the logs
  and session tests, the CLI) and runs `pytest -m requires_orchestrator`, or
  a release-sweep gate if that costs too much CI time. Measure the runtime
  first.
- **New trees:** a unit test that lists every tracked test file and fails when
  one falls outside every root a CI step runs: the three pytest roots for
  Python, the Vitest include for the web console, the crate for Rust. Go needs
  none.

## Progress

**New trees: done.** `tests/unit/python/test_every_test_file_has_a_runner.py`
lists every tracked test file and fails, naming the file, when no CI step runs
it: a Python test file outside the three pytest roots, a JavaScript test file
outside the web console's Vitest `include`, a Rust test file outside the CLI
crate, or a Go test file where `./...` cannot reach (inside a nested module,
or under a name starting with `.` or `_`). Beside each language, a pin fails
when the CI step stops running that place, or runs only part of it. A new test
tree is added to the test's list of roots in the same PR as its CI step.

**Opt-in tests: open.** The 13 `requires_orchestrator` tests still run in no
gate.
