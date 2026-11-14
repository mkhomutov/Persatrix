---
id: ISSUE-0249
summary: "No linter reads the project's own code for security faults. `.golangci.yml` enables five linters and `gosec` is not one; ruff runs without its bandit rules. Run once on `main`, `gosec` reports 103 findings and the bandit rules 84 outside the test trees; none has been read to say whether it is a fault."
status: open
severity: low
area: ci
created: 2026-11-14
refs:
  - .golangci.yml
  - agents/pyproject.toml
  - ruff.toml
  - Makefile
  - docs/issues/ISSUE-0218-advisory-scanning-covers-rust-only.md
---

# ISSUE-0249: No security linter reads the project's own code

## Summary

The weekly audit and Dependabot watch the project's dependencies. Nothing
watches its own code for the common security mistakes a linter can see: a
file created readable by everyone, a path or an SQL string built from
input, a weak random source, a YAML load that can build objects.

[ISSUE-0218](ISSUE-0218-advisory-scanning-covers-rust-only.md) named this
in its summary and left it out of its fix. This issue takes that half over.

## Context

Both linters were run once on `main` at `fb0e69b8`, with the versions CI
pins and their default rules. Nothing was changed and no finding was read
closely, so these are counts of reports, not of faults.

**Go: `gosec`, through golangci-lint 2.13.2** (`golangci-lint run
--enable-only gosec --max-issues-per-linter 0 --max-same-issues 0 ./...`):
103 findings, 39 in product code and 64 in tests. Without the two
`--max-…` flags the tool stops at its default caps and prints 43.

| Rule | Count | What it reports |
|---|---|---|
| G306 | 30 | a file written with permissions wider than 0600 |
| G304 | 21 | a file opened from a path held in a variable |
| G115 | 17 | an integer conversion that can overflow |
| G301 | 8 | a directory created with permissions wider than 0750 |
| G404 | 6 | `math/rand` used where the result may matter |
| G302 | 4 | a file opened or changed with wide permissions |
| G202 | 4 | an SQL string built by concatenation |
| G118 | 4 | a goroutine that drops the request's context |
| G101 | 4 | a string that looks like a hard-coded credential |
| G703 | 2 | a path that taint analysis traces to input |
| G104 | 2 | an error not checked |
| G204 | 1 | a subprocess started with a variable |

**Python: ruff 0.17.2 with the bandit rules** (`ruff check --select S`):

| Tree | Count | Largest groups |
|---|---|---|
| `agents/`, its tests left out | 50 | S608 SQL built from strings (35), S105 and S106 possible hard-coded passwords (10), S311 non-cryptographic random (2), S506 a YAML load with a custom loader (1) |
| `scripts/` and `evaluators/` | 34 | S607 and S603 subprocess calls (24), S101 `assert` (5), S602 a subprocess with `shell=True` (1), S506 (1) |
| `tests/`, `assert` left out | 118 | S603 and S607 subprocess calls (67), S105 to S107 password-like strings (26) |

With its tests included, `agents/` reports 871, of which 818 are S101,
the `assert` every test uses.

## Impact

A new fault of one of these kinds is not caught by any gate. Proposed
low: the counts say how much reading the first pass needs, not how many
faults there are, and the code that takes untrusted input (the tools, the
REST handlers) has tests of its own for paths and permissions. The
severity is proposed here, not ruled.

## Proposed fix / investigation path

A stricter linter ships with the fixes for what it finds, so each
language is one change of its own:

1. Read the findings rule by rule. Fix the real ones. For a rule that is
   noise in this code base (S101 in tests, G304 where the path is the
   operator's own config), turn the rule off for that tree with its
   reason, the way `.golangci.yml` and `ruff.toml` already record every
   exclusion.
2. Then enable `gosec` in `.golangci.yml` and add `S` to ruff's `select`
   in `agents/pyproject.toml`, in the same change that leaves the tree
   clean.

## Notes

> 2026-11-14 — counted while fixing
> [ISSUE-0218](ISSUE-0218-advisory-scanning-covers-rust-only.md).
