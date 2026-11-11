---
id: ISSUE-0233
summary: "`test_personal_tier_latency_harness_runs` passes only when another test file has already loaded the perf harness. Run alone, its file fails with an `AttributeError` inside `dataclasses`, because its loader does not register the module in `sys.modules` before running it. The suite is green only because of the order its files run in."
status: resolved
severity: low
area: tests
created: 2026-11-11
closed: 2026-11-12
closed_pr: 1099
refs:
  - tests/unit/python/test_rfc0029_callsite_refactor.py
  - tests/unit/python/test_personal_tier_latency_harness.py
  - tests/perf/personal_tier_latency.py
  - docs/issues/ISSUE-0219-agent-image-is-not-what-ci-tests.md
---

# ISSUE-0233: The latency-harness test fails when its file runs alone

## Summary

`_load_perf_harness` in
`tests/unit/python/test_rfc0029_callsite_refactor.py` loads
`tests/perf/personal_tier_latency.py` by path and runs it without first
putting the new module into `sys.modules`. The harness defines
dataclasses, and `dataclasses` looks a class's module up in `sys.modules`
while the class is being built. With no entry there, it fails.

The sibling loader in
`tests/unit/python/test_personal_tier_latency_harness.py` registers the
module first, and a comment there explains why. In a full run that file
is collected earlier and leaves its entry behind, so this one passes.

## Context

On Python 3.12.13:

| Run | Result |
|---|---|
| `pytest tests/unit/python/test_rfc0029_callsite_refactor.py` | 1 failed, 9 passed |
| The sibling file, then this one | 14 passed |

A second probe during review loaded a small dataclass module by path the
same way and saw the same failure on Python 3.11 and 3.13, so the
interpreter version is not the cause.

## Impact

Running this one file, or this one test, fails for a reason that has
nothing to do with the change being tested. Anything that changes the
order the files run in, such as a renamed file or a run split across
workers, would turn the suite red the same way.

## Proposed fix / investigation path

Register the module before running it, as the sibling loader does, or
have both files share one loader.

## Notes

> 2026-11-11 — found in the review of the pull request that filed
> [ISSUE-0219](ISSUE-0219-agent-image-is-not-what-ci-tests.md), whose
> first draft read this failure as a Python 3.12 fault. Both runs in the
> table were reproduced.

> 2026-11-11 — fixed. The three test files that load the harness (there
> were three copies of the loader, not two:
> `test_personal_tier_latency_gate.py` had its own) now share
> `tests/unit/python/_perf_harness_loader.py`, which registers the module
> before running it. A new test removes any entry an earlier file left,
> then loads the harness, so an unregistered load fails in any file
> order. Reproduced first: alone 1 failed, 9 passed; after the sibling
> 14 passed; before the sibling 1 failed, 13 passed; the same error on
> Python 3.11.
