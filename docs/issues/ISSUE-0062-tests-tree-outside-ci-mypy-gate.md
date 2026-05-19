---
id: ISSUE-0062
summary: Repo-root tests/ tree is outside CI's `cd agents && mypy .` scope — test code under tests/ is type-checked by nothing; closing it needs mypy path config plus a ~106-error triage pass
status: open
severity: low
area: ci
created: 2026-05-19
refs:
  - docs/issues/ISSUE-0056-tests-tree-outside-ci-lint-type-gate.md
  - .github/workflows/ci.yml
  - agents/pyproject.toml
---

## Summary

CI type-checks only the `agents/` tree — `ci.yml` runs `cd agents && mypy .`.
The repo-root `tests/` tree is outside that scope, so test code under
`tests/` is never type-checked. This is the type-checking half of
ISSUE-0056; the lint half (`ruff check tests/`) shipped separately.

## Context

ISSUE-0056 observed that the repo-root `tests/` tree sits outside both
CI lint steps (`cd agents && ruff check .`) and the type-check step
(`cd agents && mypy .`). Its fix added a `ruff check tests/` gate and
triaged the accumulated lint backlog. The mypy half was split off here
because it is a materially larger and structurally different change:

- **Path configuration.** `mypy tests/` cannot resolve sibling test-helper
  modules (e.g. `_otel_test_helpers`, `_temporal_test_helpers`) — a first
  pass surfaced 13 `import-not-found` errors. The Makefile's
  `test-integration` target sets `PYTHONPATH="agents/generated"` and runs
  pytest with `-c agents/pyproject.toml`; mypy needs an equivalent
  `mypy_path` / namespace-package configuration before its output is
  trustworthy.
- **Accumulated backlog.** Beyond the import errors, a first pass surfaced
  ~106 type errors across ~44 files (predominantly `index`, `union-attr`,
  `arg-type`, `no-any-return`) — accumulated because `tests/` has never
  been type-checked. Test code is mock-heavy, so a share of these are
  genuine annotation gaps and a share are noise that wants a scoped
  config (e.g. relaxed settings for test modules).

## Impact

- Type errors in test code under `tests/` accrue with no CI signal.
- A PR checklist line reading "mypy clean" over-states coverage for files
  in this tree — the same over-statement ISSUE-0056 called out for ruff,
  still true for mypy.

## Proposed fix / investigation path

1. Add a `mypy_path` (or namespace-package) configuration so `mypy tests/`
   resolves test-helper imports the way pytest already does.
2. Decide the type-strictness contract for test code — likely a
   per-module or per-directory relaxation rather than the `agents/`
   settings verbatim — and triage the resulting backlog: fix genuine
   annotation gaps, scope-ignore the rest with a rationale.
3. Add a `mypy tests/` step to the `python` job in `ci.yml`, mirroring the
   `ruff check tests/` step ISSUE-0056 added, and the matching line in the
   Makefile `lint-python` target.

## Notes

> 2026-05-19 — split off from the ISSUE-0056 fix. The lint gate
> (`ruff check tests/`) and its one-time ruff triage shipped in that PR;
> this issue tracks the type-checking gate that the same PR deliberately
> deferred.
