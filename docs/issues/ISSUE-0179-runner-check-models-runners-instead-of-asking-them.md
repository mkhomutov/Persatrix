---
id: ISSUE-0179
summary: "The unit test that fails when a test file sits where no CI step runs it (#1020) decides what each step runs by copying the rules of pytest, Vitest, cargo and Go into Python and reading configs as text. A copy can drift from its runner, and then an unrun test file passes the check. The #1020 review reproduced five such disagreements, each fixed in the copy. Each CI job already has its runner and could list the files it runs, for a diff against git ls-files."
status: open
severity: low
area: ci
created: 2026-09-29
refs:
  - https://github.com/mkhomutov/Persatrix/pull/1020
  - tests/unit/python/test_every_test_file_has_a_runner.py
  - tests/unit/python/test_every_test_file_has_a_runner_cases.py
  - .github/workflows/ci.yml
  - docs/issues/ISSUE-0178-runner-rule-checked-only-for-go.md
  - docs/methodology/testing-strategy.md
---

# ISSUE-0179: The runner check models each runner instead of asking it

## Summary

`tests/unit/python/test_every_test_file_has_a_runner.py`, added by
[#1020](https://github.com/mkhomutov/Persatrix/pull/1020) for
[ISSUE-0178](ISSUE-0178-runner-rule-checked-only-for-go.md), fails when a
tracked test file sits where no CI step runs it. It works out what each step
runs by reading text: pytest's default collection rules, the Vitest `include`
in `web/vite.config.js`, Cargo's target rules, and Go's rules for file names
and build lines, each copied into Python. A copy can drift from the runner it
copies. When it does, a test file that no CI step runs can pass the check,
which is the failure ISSUE-0178 exists to stop.

## Context

Found as F-11 in the review of #1020. The same review reproduced five places
where the copy and the runner disagreed:

- a Go test behind a `//go:build` line, with another platform's file suffix,
  or in a vendor directory;
- a `vitest.config.js` beside `vite.config.js` (Vitest reads it first), or a
  nested `coverage.include` read as the test include;
- an include glob read wider than Vitest reads it;
- an `addopts` in `agents/pyproject.toml`, or a conftest `collect_ignore`;
- `test = false` on the CLI's only Cargo target, which turns off all 297
  CLI tests.

#1020 fixes each one in the copy, and anything the copy does not recognise now
fails with "cannot read" rather than passing. Not calling the runners was a
choice: the check runs in the Python job, which has no Go, Rust or Node
toolchain.

Measured offline during the review:

- `go list -f '{{.Dir}} {{.TestGoFiles}} {{.XTestGoFiles}}' ./...` takes about
  0.24 s and matched `git ls-files '*_test.go'` 311 of 311. It leaves out the
  files a build line, a platform suffix or a vendor directory excludes.
- CI's three pytest commands with `--collect-only -q` take about 9.4 s
  together, and leave out the files a conftest `collect_ignore` or an ini
  `addopts` removes.
- `vitest list --filesOnly --json` takes 0.21 s on Vitest 4.1.8 and settles
  config files and nested blocks by itself.
- With `test = false` set, `cargo test -- --list` lists no tests. The test
  binary's dependency file listed exactly the 40 tracked `.rs` files, so it
  would also show a Rust file that no `mod` line reaches, which #1020 names as
  out of its sight.

## Impact

Low today. The copy now fails loudly on config it cannot read, so most drift
shows as a false failure, not a silent pass. What it cannot see is a change
inside a runner: a new default, a new config file name, or a new Cargo or Go
rule. A test file that change leaves unrun would pass. Each new case also
costs a code change in the test, where the runner would simply report it.

## Proposed fix / investigation path

In each CI job that already has the toolchain, add a step that lists the test
files its runner would run and compares them with `git ls-files`, failing on a
tracked test file the runner does not run:

- **go:** `go list` as above, with CI's build settings (linux/amd64; `-race`
  sets the `race` build tag), `testdata` exempt.
- **python:** `pytest --collect-only -q` for each root, with the arguments its
  CI step uses. `tests/integration/test_workflow.py` holds no tests and
  collects nothing, and `-c agents/pyproject.toml` makes `agents/` the root
  that test ids are relative to.
- **web-console:** `npx vitest list --filesOnly --json` after `npm ci`. Check
  first that Vitest 5, which `web/package-lock.json` pins, still has it.
- **rust:** require `cargo test -- --list` to list tests, or compare the test
  binary's dependency file with the tracked `.rs` files.

The Python-side copy could then shrink to the CI-step pins, or stay as a fast
first check that needs no other toolchain.

## Notes

> 2026-09-29 — filed from F-11 of the #1020 review; #1020's description links
> it.
