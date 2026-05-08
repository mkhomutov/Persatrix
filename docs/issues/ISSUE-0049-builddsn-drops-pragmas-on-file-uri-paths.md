---
id: ISSUE-0049
summary: "buildDSN concatenates path + '?' + params and silently drops every PRAGMA when path already contains a query string (e.g. file::memory:?cache=shared)"
status: resolved
severity: medium
area: internal/channels
created: 2026-05-08
closed: 2026-05-08
refs:
  - internal/channels/sqlite.go
  - docs/rfcs/0011-pr-plan.md
---

## Summary

[`buildDSN`](../../internal/channels/sqlite.go#L105-L111) attaches the
three required PRAGMAs (`foreign_keys(1)`, `journal_mode(WAL)`,
`busy_timeout(5000)`) by concatenating `path + "?" + q.Encode()`.
The function's caller-facing doc-comment on
[`NewSQLiteStore`](../../internal/channels/sqlite.go#L42-L49) explicitly
advertises `file::memory:?cache=shared` as a supported path form, but
when such a path is passed the resulting DSN contains two `?`
separators — the second one is treated as a literal character inside
the first query string and every PRAGMA is silently dropped by the
SQLite driver.

## Context

Captured during PR #231 deep review (Should-Fix #1) and pinned to
RFC 0011 PR 8 close-out. No production caller passes a `file:` URI
today (production uses an absolute filesystem path, tests use the
bare `:memory:` form), so the bug is invisible at runtime — but the
doc-comment promises a path form that does not work.

The concrete failure mode for `path = "file::memory:?cache=shared"`:

| Step | Value |
|------|-------|
| `q.Encode()` | `_pragma=foreign_keys%281%29&_pragma=journal_mode%28WAL%29&_pragma=busy_timeout%285000%29` |
| `path + "?" + q.Encode()` | `file::memory:?cache=shared?_pragma=foreign_keys%281%29&...` |
| Driver-parsed query | `cache=shared?_pragma=foreign_keys(1)&_pragma=journal_mode(WAL)&_pragma=busy_timeout(5000)` (PRAGMAs become a single malformed value of `cache`) |
| Effective PRAGMAs | none — `foreign_keys` defaults to OFF, `journal_mode` defaults to `delete`, `busy_timeout` defaults to 0 |

Without `foreign_keys=ON`, the `messages.thread_id` self-cascade and
the `memberships`/`messages` → `channels(id)` cascades documented in
[`NewSQLiteStore`](../../internal/channels/sqlite.go#L42-L49) silently
do not fire — a regression that would only surface as orphaned rows
under a future test or contributor that adopts the documented path form.

## Impact

- Doc/code mismatch: the doc-comment's promise breaks the moment any
  contributor uses the documented form for a shared in-memory test
  fixture (a common pattern when multiple `*sql.DB` instances need
  to read the same in-memory database).
- Foreign-key cascades silently disabled for any caller using a `file:`
  URI — orphaned `memberships`/`messages` rows that would normally be
  cascade-deleted persist, and `messages.thread_id` self-cascade does
  not fire on parent-message delete.
- WAL mode + busy_timeout silently disabled — every concurrent write
  becomes a hard `SQLITE_BUSY` instead of the 5s wait the rest of the
  store assumes.
- Defense-in-depth: a future caller (e.g. a test that wants
  `cache=shared`) will hit the failure as a hard-to-diagnose schema
  cascade bug rather than an obvious URL parse error.

## Proposed fix / investigation path

Two options were captured in the PR plan:

1. **Reject paths containing `?` with a typed error** — strictest, but
   breaks the doc-comment's `file::memory:?cache=shared` promise.
2. **Merge caller-supplied query params into the PRAGMA `url.Values`** —
   preserves the documented form. Parse `path` to split off any
   pre-existing query string, merge into `q`, re-encode.

Option 2 keeps the doc-comment honest. Implementation sketch:

```go
func buildDSN(path string) string {
    base, existing := path, ""
    if i := strings.Index(path, "?"); i >= 0 {
        base, existing = path[:i], path[i+1:]
    }
    q, _ := url.ParseQuery(existing) // empty string → empty Values; never errors
    q.Add("_pragma", "foreign_keys(1)")
    q.Add("_pragma", "journal_mode(WAL)")
    q.Add("_pragma", "busy_timeout(5000)")
    return base + "?" + q.Encode()
}
```

`url.ParseQuery("")` returns an empty `url.Values` and a nil error, so
the bare-path case (`":memory:"`, `/tmp/foo.db`) keeps the existing
behavior. `q.Add` (vs `q.Set`) preserves repeated keys — the three
PRAGMAs are already added with `Add` to keep all three values present.

## Notes

> 2026-05-08 — initial capture from PR #231 review SF-1, pinned to
> RFC 0011 PR 8 close-out.
> 2026-05-08 — fix landed: `buildDSN` now splits on `?` and merges
> caller-supplied query params into the PRAGMA `url.Values` before
> re-encoding. Regression test
> [`TestBuildDSN_FileURIPreservesPRAGMAs`](../../internal/channels/sqlite_dsn_test.go)
> demonstrates the original failure (the open call was rejected with
> "no such cache mode: shared?_pragma=…", not silent drop) and pins
> the fix; companion tests cover the bare-path regression guard and
> caller-query-param preservation.
