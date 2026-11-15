---
id: ISSUE-0265
summary: "Each request for the merged log view (`GET /api/v1/executions/_/logs`) reads and parses the log files of every finished run on disk, up to the 512 MB disk cap, before it applies `since` and keeps at most `limit` entries. A stream on `_` with `since` now does the same once per connection, so a `persatrix logs _ --follow --since 5m` that reconnects repeats that read each time."
status: open
severity: low
area: internal/server
created: 2026-11-15
refs:
  - internal/server/logs_handler.go
  - internal/server/logs_stream_handler.go
  - internal/observability/logbuffer/buffer.go
  - internal/observability/logbuffer/disk.go
  - internal/observability/logbuffer/subscribe.go
  - docs/issues/ISSUE-0223-logs-follow-ignores-its-filters.md
---

# ISSUE-0265: The merged log view reads every finished run from disk

## Summary

The path id `_` asks for the entries of every run. `collectAllEntries`
(`internal/server/logs_handler.go`) lists every run the buffer knows,
in memory or on disk, and takes a `Snapshot` of each. For a run with no
ring in memory, `Snapshot` reads and parses all of that run's files.
The filters and the `limit` are applied afterwards, to the whole set.

The buffer keeps 50 runs in memory and up to 512 MB of finished runs on
disk (`PERSATRIX_LOGBUFFER_MAX_EXEC`, `PERSATRIX_LOGBUFFER_DISK_MB`). On
a deployment that has run for a while, one request for the merged view
therefore reads and parses up to 512 MB of JSON lines and holds the
result in memory until the response is written, to return at most 5,000
entries.

## Context

- `persatrix logs _ --since 5m` asks for five minutes and pays for the
  whole disk store: `since` is checked entry by entry, after the read.
- The stream route's buffered entries for `since` are the same
  selection, taken by `SubscribeWithBacklog`
  (`internal/observability/logbuffer/subscribe.go`). A stream on `_`
  with `since` does the read once per connection, outside the buffer's
  locks. The CLI reconnects with the same flags, 0.5 to 15 seconds
  apart, so a stream that keeps dropping repeats it. A stream on `_`
  without `since`, and any request for one run, do not read the store.
- Both routes are open to any caller under the default `auth.mode:
  disabled`. The REST rate limit (60 requests a minute for each caller
  by default) is the only bound on how often.

Read from the code. The time and memory of a request against a full
store were not measured.

## Impact

One request, or one reconnecting follower, can make the orchestrator
read its whole log store and hold it in memory. Proposed low: the store
is bounded, the cost is the orchestrator's own disk and memory for the
length of one request, and nothing is lost or disclosed that the route
does not already return. A measurement on a full store could argue for
medium. The severity is proposed here, not ruled.

## Proposed fix / investigation path

Skip a run on disk when its newest file was written before `since`: an
entry is stamped before it is flushed, so such a run has nothing to
return (an entry stamped by an agent whose clock runs ahead is the
exception to settle). Without `since`, read runs newest first and stop
once `limit` entries are in hand. Either belongs in the buffer, so both
routes get it.

## Notes

> 2026-11-15 — noticed while fixing the server half of
> [ISSUE-0223](ISSUE-0223-logs-follow-ignores-its-filters.md), which
> gave the stream route the snapshot route's selection for `since` and
> with it this cost on `_`.
