---
id: ISSUE-0223
summary: "`persatrix logs --follow` sends `--level`, `--workflow` and `--since` to the stream endpoint, which never reads them, so a followed log shows every level and backfills nothing. Entries logged between a dropped connection and the reconnect are also lost with no notice."
status: open
severity: medium
area: cli
created: 2026-11-10
refs:
  - cli/src/commands/logs.rs
  - internal/server/logs_stream_handler.go
  - internal/server/logs_handler.go
  - internal/observability/logbuffer/subscribe.go
  - docs/observability.md
---

# ISSUE-0223: `logs --follow` ignores `--level`, `--workflow` and `--since`

## Summary

The CLI builds one URL for both log modes (`build_logs_url` in
`cli/src/commands/logs.rs`) and puts `since`, `workflow` and `level` in
the query string. The snapshot handler parses them (`parseLogsRequest` in
`internal/server/logs_handler.go`). The stream handler,
`handleStreamLogs`, reads only the execution id from the path. The CLI
itself filters only `--trace` and `--agent` on the client side.

## Context

- The observability guide's own example,
  `persatrix logs exec-7f2a1 --follow --level INFO`, prints DEBUG lines
  too.
- `--since 5m --follow` shows nothing from the last five minutes; the
  stream starts at the moment of connection.
- `Buffer.Subscribe` (`internal/observability/logbuffer/subscribe.go`)
  has no replay. When the stream drops, the CLI reconnects and prints
  `[reconnected]`, and whatever was logged in between is missing.

## Impact

A person following a busy execution cannot narrow the stream, and a
reconnect quietly loses lines at exactly the moment something went
wrong.

## Proposed fix / investigation path

Parse the same filters in the stream handler and apply them to each
entry before it is sent. On connect, when `since` is given, send the
matching entries from the buffer first. On reconnect, have the CLI ask
for everything since the last timestamp it printed.

## Notes

> 2026-11-10 — found by a whole-repository quality review of `main` at
> `ee9cfaac`; confirmed by reading both handlers and the CLI.
