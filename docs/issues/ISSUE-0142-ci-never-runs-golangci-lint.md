---
id: ISSUE-0142
summary: "CI runs no Go linter and the repository pins no golangci-lint version or config, so `make lint`'s Go leg is both unenforced by any merge and unreproducible between machines — the same gap shape as the cargo-test omission #813 found, surviving the methodology series that was meant to close exactly this class"
status: open
severity: medium
area: ci
created: 2026-09-06
refs:
  - .github/workflows/ci.yml
  - docs/methodology/enforcement-matrix.md
  - docs/v0.3.15-release-checklist.md
---

## Summary

`make lint` runs `golangci-lint run ./...`. No CI job does. The Go job builds,
checks `gofmt`, runs unit and integration tests and the sanitizer sync — and
never lints. There is also no `.golangci.yml`, so what the command *means*
depends on whichever version the developer happens to have installed.

## Context

Found during the v0.3.15 release-prep PR 4 gate sweep. `make lint` failed on a
clean checkout with:

```
golangci-lint run ./...
make: golangci-lint: No such file or directory
```

The tool simply was not installed — and nothing had noticed, because nothing
requires it. A search across `.github/workflows/` returns no `golangci` step;
the sole match is a comment. The other two lint legs *are* enforced: Python
lint/type-check and Rust `clippy -D warnings` both have CI jobs, and both pass.

Installing `golangci-lint@latest` (v2.13.2) and running it reports **30 issues**
— 24 `errcheck`, 5 `staticcheck`, 1 `ineffassign`. Two land in files v0.3.15
touched, and neither is a defect:

- `cmd/orchestrator/channels.go:128` — `ineffectual assignment to cleanup`. The
  variable is reassigned at line 227 and there is no read and **no early return**
  between the two, so no path can observe the first assignment. Dead code, not a
  leaked store handle.
- `internal/server/principal_producer_test.go:86` — `ST1023`, a redundant type
  declaration in a test.

## Impact

Two distinct problems, and the second is the worse one.

**Unenforced.** A Go lint regression cannot fail a PR. This is the same shape as
the finding at [#813](https://github.com/mkhomutov/Persatrix/pull/813), where CI
ran `cargo build` and `clippy` but never `cargo test`, which is how a red Rust
suite merged green. That one was caught by a release arc; this one survived
[#858](https://github.com/mkhomutov/Persatrix/pull/858) — the methodology PR
whose stated purpose was that "a local-only check is a check the merge never
sees". The enforcement matrix should have caught it and did not.

**Unreproducible.** With no `.golangci.yml`, the linter's default set varies by
version. "make lint is green" has therefore never been a portable claim for Go:
one developer's clean tree is another's 30 findings, and neither is wrong.

Nothing here blocks v0.3.15 — the 30 findings are style and unchecked-error
noise, mostly in tests, and the two in touched files are inert. The gap is the
absent gate.

## Proposed fix / investigation path

1. **Pin the tool and its config.** Add a `.golangci.yml` declaring the enabled
   linters explicitly, and pin the version the same way the proto toolchain is
   pinned — an unpinned linter is a flaky gate waiting to happen.
2. **Add the CI step**, `golangci-lint-action` with that pinned version, in the
   existing `go` job beside the `gofmt` check.
3. **Land the two together**, and expect a first run to be red: decide per
   linter whether to fix the 30 findings or to disable the rule in the config.
   `errcheck` on `defer resp.Body.Close()` in tests is the usual candidate for a
   test-scoped exclusion rather than 24 edits.
4. **Re-audit the enforcement matrix** for other `make` targets with no CI job —
   this one was missed by a sweep that was explicitly looking for it.

## Notes

Filed at v0.3.15 release-prep PR 4. Not release-blocking; recorded as a Known
Gap on the [v0.3.15 release checklist](../v0.3.15-release-checklist.md#6-known-gaps-to-document-in-release-notes)
and noted in §1 there, so the next cycle does not re-derive it from a failing
sweep.
