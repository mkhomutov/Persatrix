---
id: ISSUE-0056
summary: Repo-root tests/ tree is outside CI's `cd agents && ruff check . && mypy .` scope — new test code under tests/ is silently unlinted and untyped
status: resolved
severity: low
area: ci
created: 2026-05-18
closed: 2026-05-19
closed_pr: 381
refs:
  - docs/rfcs/0029-pr-plan.md
  - docs/issues/ISSUE-0062-tests-tree-outside-ci-mypy-gate.md
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

> 2026-05-19 — resolved for the **lint** half. CI now runs
> `ruff check tests/` (a new step in the `python` job) and the Makefile
> `lint-python` target gained the matching invocation. A repo-root
> `ruff.toml` `extend`s `agents/pyproject.toml` so the rule set stays
> single-sourced across both trees; its sole local addition is a
> `tests/** = ["TID251"]` per-file-ignore (schema/migration tests open
> SQLite directly on purpose — same carve-out the agents config already
> grants `memory/**`). The one-time triage cleared the accumulated ruff
> backlog: 131 auto-fixes plus 87 manual fixes (82 line-too-long, 2
> banned-import-alias casing, 1 acronym alias, 2 printf-format). The
> **type-check** half is deliberately split off to
> [ISSUE-0062](ISSUE-0062-tests-tree-outside-ci-mypy-gate.md): a
> `mypy tests/` gate needs path configuration plus a ~106-error triage —
> materially larger and structurally separate from the lint gate.

> 2026-05-19 — review follow-up: a `/review` pass found two of the
> `line-too-long` triage fixes had wrapped *corrupt* source rather than
> repairing it. `test_interaction_multi_turn.py` carried two section-divider
> comments as literal `\u2500` escape text (not real `─`); these are
> restored to single-line `─` dividers. The same file held eight more
> comment lines with literal `\u2014`/`\u00a7` text — each under the
> 100-col cap, so never lint-flagged — plus one docstring `\n`; all
> corrected to real glyphs. `test_observability_schema_parity.py` had a
> Markdown link split mid-URL by a `line-too-long` fix, now a reference-style
> link. Comments and docstrings only — no test logic changed.

> 2026-05-19 — second review pass: the follow-up above scanned only `#`
> comment lines, so it missed literal `\u….` escape text inside
> *docstrings* and was wrong to call the corruption "localised to one
> file". `test_interaction_multi_turn.py` still carried 10 escapes in its
> module, class, and function docstrings (`—`, `§`, `→`);
> `test_delegation_end_to_end.py` carried 3 more in a docstring. All 13
> are now converted to real glyphs. Ruff cannot flag these (they sit
> under the line cap, inside string literals), so the gate gives no
> signal here — a `\uXXXX` scan of the whole `tests/` tree was used to
> confirm the only remaining match is the legitimate `"␤"` string
> literal in `test_delegation_rollback_edges.py`. Docstrings only — no
> test logic changed.
