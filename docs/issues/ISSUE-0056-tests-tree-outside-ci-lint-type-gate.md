---
id: ISSUE-0056
summary: Repo-root tests/ tree is outside CI's `cd agents && ruff check . && mypy .` scope — new test code under tests/ is silently unlinted and untyped
status: open
severity: low
area: ci
created: 2026-05-18
refs:
  - docs/rfcs/0029-pr-plan.md
  - .github/workflows/ci.yml
---

## Summary

CI lints and type-checks only the `agents/` tree — `ci.yml` runs
`cd agents && ruff check .` and `cd agents && mypy .`. The repo-root
`tests/` tree is outside both, so test code added under `tests/` is
never linted or type-checked by CI.

## Context

Surfaced during the RFC 0029 Phase 1 PR 3 review. PR 3 adds two files
under the repo-root `tests/` tree —
`tests/unit/python/test_rfc0029_callsite_refactor.py` and
`tests/perf/personal_tier_latency.py`. A PR checklist line reading
"ruff / mypy clean" reads as CI-enforced, but because the CI steps
`cd agents` first, neither file is actually covered; `ruff check` was
clean only when run manually on the two files during review.

This is a pre-existing structural gap, not introduced by PR 3. Test
modules under `agents/tests/` *are* covered (they sit inside the
`agents/` tree); only the repo-root `tests/` tree is blind. A
double-blank-line import-group nit in one of the PR 3 files (fixed in
the same review) is the kind of finding CI would otherwise have caught.

## Impact

- Test code under `tests/` can accumulate lint violations and type
  errors with no CI signal.
- PR checklists and reviews that claim "ruff / mypy clean" over-state
  coverage for files in this tree.

## Proposed fix / investigation path

Extend the CI lint/type steps to also cover the repo-root `tests/`
tree — either a second `ruff check tests/` / `mypy tests/` invocation,
or a top-level ruff/mypy configuration that includes both trees.
Expect a one-time triage pass: `tests/` has never been gated, so the
first run may surface accumulated findings to clean up or baseline.

## Notes

> 2026-05-18 — initial capture during RFC 0029 Phase 1 PR 3 review.
