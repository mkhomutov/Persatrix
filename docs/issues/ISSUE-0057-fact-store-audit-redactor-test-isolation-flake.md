---
id: ISSUE-0057
summary: test_fact_store_audit.py redactor-failure test passes in isolation and per-file but fails under the full suite — a process-global redactor / contextvar state-isolation flake
status: resolved
severity: low
area: tests
created: 2026-05-18
closed: 2026-05-18
closed_pr: 374
refs:
  - docs/rfcs/0029-pr-plan.md
  - tests/unit/python/test_fact_store_audit.py
---

## Summary

`tests/unit/python/test_fact_store_audit.py::TestRedactorFailureWarning::test_redactor_raises_then_warning_logged`
fails when run as part of the full repo-root `tests/` suite, but passes
when run in isolation and when the whole `test_fact_store_audit.py`
file is run alone — a test-ordering flake rooted in process-global
redactor state.

## Context

Surfaced during the RFC 0029 Phase 1 PR 3 review. PR 3 (the call-site
refactor) touches neither `agents/observability/logging.py` nor the
fact-store audit path, so the failure is unrelated to the RFC 0029
facade rename.

Confirmed during review:

- `pytest …::TestRedactorFailureWarning::test_redactor_raises_then_warning_logged`
  in isolation → 1 passed.
- `pytest tests/unit/python/test_fact_store_audit.py` (whole file) → 7 passed.
- The full ~2500-test suite run reported the test failing.

The redactor is process-global module state in
`agents/observability/logging.py` (`set_redactor`). `test_fact_store_audit.py`
has an autouse `_reset_redactor` fixture restoring `NoopRedactor` after
each test *in that file* — but several other observability modules and
test files also call `set_redactor`, and the failing test asserts
"exactly one WARNING". That assertion is sensitive both to a redactor
leaked from a sibling module and to the re-entrancy contextvar guard
documented in the test's own docstring not being reset across modules.

## Impact

- Latent CI cost: a full-suite run can fail intermittently on a test
  whose subject code is correct, eroding trust in the gate.
- The failure depends on test ordering, so it can surface or vanish as
  unrelated test files are added or reordered.

## Proposed fix / investigation path

Identify which sibling module leaves global redactor (or contextvar)
state dirty, then make the reset symmetric — e.g. promote the autouse
`_reset_redactor` fixture to a shared conftest fixture so every module
that calls `set_redactor` restores `NoopRedactor` (and clears the
re-entrancy contextvar) on teardown. Related: ISSUE-0024
(`tests/unit/python/` full-suite isolation).

## Notes

> 2026-05-18 — initial capture during RFC 0029 Phase 1 PR 3 review;
> isolated and per-file runs confirmed passing, full-suite run reported failing.

> 2026-05-18 — resolved by #374. The fix scopes `emit_audit`'s "exactly
> one WARNING" assertion to `emit_audit`'s own logger
> (`agents.memory.facts`). Once any earlier test calls `configure_logging`,
> the structlog chain is built process-globally and its `_apply_redactor`
> step logs its own "redactor raised" WARNING under
> `agents.observability.logging` — a *separate layer's* signal, not a
> duplicate. Counting only `emit_audit`'s logger pins the intended
> contract independent of test ordering. This is a narrower fix than the
> "Proposed fix" above (a symmetric shared-conftest redactor reset) but
> resolves the flake at its assertion; full
> `pytest agents/tests/ tests/unit/python/` now passes with zero failures.
