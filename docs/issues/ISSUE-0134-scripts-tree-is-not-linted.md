---
id: ISSUE-0134
summary: "CI lints `agents/` (`cd agents && ruff check .`) and `tests/` (`ruff check tests/`), but never `scripts/` — the 27-file tree that holds every pre-commit check, the file-map generator, the hook installer, and the version bumper. Nothing formats or lints the code that enforces formatting and linting. Confirmed during the #840 review, where an `I001` introduced in `scripts/install_hooks.py` was caught only by running ruff there by hand; CI was green on the same content."
status: resolved
severity: low
area: ci
created: 2026-08-22
closed: 2026-09-06
closed_pr: 858
refs:
  - .github/workflows/ci.yml
  - ruff.toml
  - agents/pyproject.toml
  - scripts/
---

## Summary

`scripts/` is outside both of CI's ruff invocations, so the tooling that
gates the repo is itself ungated.

## Context

`.github/workflows/ci.yml` runs ruff twice:

```yaml
- name: Lint with ruff
  run: cd agents && ruff check .

- name: Lint tests/ tree with ruff
  run: ruff check tests/
```

The second exists because of ISSUE-0056 — the `cd agents` in the first
leaves the repo-root `tests/` tree unlinted. The same reasoning applies
verbatim to `scripts/`, which was not included. `mypy` has the same
shape: `cd agents && mypy .` plus `mypy tests/`, and no `mypy scripts/`.

`ruff.toml` at the repo root already `extend`s `agents/pyproject.toml`,
so the rule set is single-sourced and a third invocation needs no new
configuration.

Confirmed concretely during the
[PR #840](https://github.com/mkhomutov/Persatrix/pull/840) review: an
edit to `scripts/install_hooks.py` introduced an `I001` (un-sorted import
block). `ruff check scripts/install_hooks.py` flagged it; the full CI
gate set did not, because nothing points ruff at that path.

## Impact

Low — this is hygiene, not correctness. But the tree in question is the
one holding `pre_commit.py`, `checks/`, `generate_filemap.py`,
`install_hooks.py`, `issues.py`, `rfcs.py`, and `bump_version.py`: the
machinery every other gate runs through. Drift there is drift in the
thing that detects drift.

The practical cost today is that a contributor cannot tell whether a
`scripts/` file is clean without running ruff manually, and reviewers
have no reason to expect it to be.

## Proposed fix / investigation path

Mirror the existing `tests/` steps:

```yaml
- name: Lint scripts/ tree with ruff
  run: ruff check scripts/
```

Run it locally first — the tree has never been linted, so expect a
backlog. If the backlog is large, land the step and the cleanup in one
PR rather than adding per-file ignores, since a `scripts/**` carve-out
would reproduce the current situation with extra steps.

`mypy scripts/` is the natural companion but is a bigger commitment:
the root `mypy.ini` sets a `mypy_path` for the test tree's helpers, and
`scripts/` uses a `sys.path` insertion plus `# noqa: E402` imports that
mypy may not resolve without configuration. Worth splitting into its own
change if the ruff half turns out to be noisy.

## Notes

> 2026-08-22 — captured during the #840 review. The `I001` finding above
> was in that PR's own diff and was fixed before merge; the gap that let
> it through to CI is what this issue records.

> 2026-09-06 — RESOLVED in #858. CI now runs `ruff check scripts/ evaluators/`
> and `mypy scripts/ evaluators/` in the Python job (evaluators/ was covered
> by `make lint-python` but by no CI step — same gap, found by the
> enforcement matrix). The backlog was 31 ruff findings: 13 auto-fixed, 17
> E501 wrapped by hand, no per-file ignores. The mypy half turned out cheap:
> `mypy.ini` gained `scripts` on `mypy_path` plus a crawl exclusion for
> `scripts/_doc_index.py` — the same shape the tests/ helpers already use —
> so the generators' sibling import resolves and the tree type-checks clean.
