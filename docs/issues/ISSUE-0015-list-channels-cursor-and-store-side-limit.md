---
id: ISSUE-0015
summary: handleListChannels loads all rows then truncates client-side; no next_cursor in response
status: resolved
severity: low
area: internal/server
created: 2026-05-04
closed: 2026-05-08
refs:
  - docs/rfcs/0011-channels.md
---

## Summary

`handleListChannels` in `internal/server/channel_handlers.go` calls
`store.ListChannels()` which returns *all* rows, then truncates to
`limit` in handler code. The `listChannelsResponse` does not include a
`next_cursor` (or `total_count`) field, so clients cannot know whether
more pages exist.

Fine at the current 50-channel default cap, but the handler will
misreport "no more pages" the moment a future PR lifts the cap or a
deployment legitimately exceeds it.

## Context

Captured during PR #245 deep review (Nice-to-have #4). Must be addressed
before any deployment exceeds the 50-channel cap or before the cap is
lifted — whichever comes first.

## Impact

- v0.3.0: cosmetic; deployments are well below the cap.
- Future: silent data truncation from clients' perspective once the cap
  is lifted.

## Proposed fix / investigation path

Two complementary changes:

1. Push `LIMIT ?` (and an `offset` or `WHERE id > ?` cursor) into the
   `ListChannels` SQL in `internal/channels/sqlite.go` — avoids loading
   the full table for a paginated read.
2. Add `next_cursor string` (omitempty) to `listChannelsResponse`. Cursor
   format: opaque, currently the last-returned channel id.

Add a regression test that creates `limit + 1` channels and asserts the
response surfaces a non-empty `next_cursor`.

## Notes

> 2026-05-04 — initial capture during PR #245 review (Nice-to-have #4).
>
> 2026-05-08 — resolved. `ChannelStore.ListChannels` now takes
> `(ctx, limit, afterID)` and pushes `WHERE id > ? ORDER BY id ASC LIMIT ?`
> into the SQL. The handler fetches `limit + 1` rows so the presence of
> the extra row signals "more pages exist" without a separate `COUNT(*)`,
> trims to `limit`, and surfaces the last kept id as
> `listChannelsResponse.next_cursor` (omitempty when the page returns
> the trailing rows). Test coverage in
> `internal/channels/sqlite_pagination_test.go` (LIMIT pushdown, zero-
> limit back-compat, keyset paging walk, after-id strict-inequality)
> and `internal/server/channel_handlers_pagination_test.go`
> (next_cursor present when more rows exist, absent on the last page,
> round-trip cursor walk with no duplicates or skips).
