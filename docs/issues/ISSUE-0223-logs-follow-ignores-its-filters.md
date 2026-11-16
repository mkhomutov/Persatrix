---
id: ISSUE-0223
summary: "Server half fixed: the log stream endpoint used to ignore the `--level`, `--workflow` and `--since` that `persatrix logs --follow` sends, so a followed log showed every level and backfilled nothing; it now applies them by the snapshot endpoint's rules and, with `since`, sends the buffered entries first. Still open, under the CLI freeze: on a reconnect the CLI sends the same flags again, so without `--since` the entries logged in between are lost with no notice, and with `--since` the whole window is printed a second time."
status: in_progress
severity: medium
area: cli
created: 2026-11-10
refs:
  - cli/src/commands/logs.rs
  - internal/server/logs_stream_handler.go
  - internal/server/logs_handler.go
  - internal/observability/logbuffer/subscribe.go
  - docs/observability.md
  - internal/server/logs_stream_filters_test.go
  - internal/server/logs_stream_replay_test.go
  - internal/server/logs_stream_seam_test.go
  - internal/server/logs_stream_selection_test.go
  - internal/observability/logbuffer/backlog_test.go
  - docs/rfcs/0018-structured-logging-framework.md
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
  `[reconnected]`, and whatever was logged in between is missing. The
  guide says that after an orchestrator restart `--follow` "resumes from
  the warm-loaded ring"; it resumes from the moment it reconnects.

## Impact

A person following a busy execution cannot narrow the stream, and a
reconnect quietly loses lines at exactly the moment something went
wrong.

## Proposed fix / investigation path

Parse the same filters in the stream handler and apply them to each
entry before it is sent. On connect, when `since` is given, send the
matching entries from the buffer first. On reconnect, have the CLI ask
for everything since the last timestamp it printed. Correct the guide's
restart sentence if the replay is not built.

What remains, as of 2026-11-15 (the first two sentences above are done):

1. The CLI's reconnect. `follow_logs` in `cli/src/commands/logs.rs`
   builds its URL once and uses it for every connection, so a reconnect
   repeats the flags as typed. Without `--since` the entries logged
   while it was disconnected are still lost with no notice. With
   `--since` they now arrive, and so does the rest of the window a
   second time. The CLI should remember the timestamp of the last entry
   it printed and reconnect with that as `since`, dropping the entries
   at that timestamp it has already printed (`since` keeps an entry
   stamped exactly at it). Two things on the server side to settle with
   it: the stream refuses a `since` in the future, which an entry
   stamped by an agent whose clock runs ahead would be; and a cursor the
   server hands out (an SSE `id:` per entry, sent back as
   `Last-Event-ID`) would need no comparison of timestamps at all.

2. Telling the reader. A stream whose reader is too slow still loses
   entries: past the 256 a live stream queues, and past the 5,000 that
   wait while the buffered entries of a `since` stream are sent. The
   orchestrator now logs each loss at `WARN`, with the stream and the
   count. The reader gets nothing: no frame says that lines are
   missing, and the CLI would not print one. A frame the CLI shows
   belongs with the reconnect work above.
3. `limit` on the stream route. RFC 0018 gives the stream route "the
   same query params" as the snapshot route, which include `limit`
   (`docs/rfcs/0018-structured-logging-framework.md`, the route table).
   The stream does not read it, on `main` and now, and a test pins
   that; the buffered entries are always the newest 1,000. Either the
   route reads `limit` for its buffered entries, which would refuse a
   `limit=abc` that opens a stream today, or the RFC's row is
   corrected.

This issue is not resolved until the first is done or ruled out. The
Rust CLI is frozen, so it waits for that freeze to lift.

## Notes

> 2026-11-10 — found by a whole-repository quality review of `main` at
> `ee9cfaac`; confirmed by reading both handlers and the CLI.

> 2026-11-15 — the server half is done in
> [#1131](https://github.com/mkhomutov/Persatrix/pull/1131); the CLI's
> reconnect remains, so this stays `in_progress`. Reproduced first on `main` at `111b041d`
> with a test per filter on the stream route: `level=INFO` sent the
> DEBUG, WARN and ERROR entries as well, `workflow=code-review` sent
> every workflow, `since=5m` sent nothing from the buffer, and
> `level=TRACE` or `since=-5m` opened a stream where the snapshot route
> answers `400`.
>
> What changed. Both routes read the three filters through one function
> (`parseLogsFilter` in `internal/server/logs_handler.go`), and the
> stream applies them to every entry it sends. With `since` it first
> sends the entries the snapshot route returns for the same filters and
> no `limit` (the newest 1,000), then the live ones. The buffered
> entries are taken at the instant the subscription is registered
> (`SubscribeWithBacklog`): `Append` now writes an entry to its ring and
> hands it to the subscribers under one read lock, and that instant
> takes the write lock, so the buffer puts each entry in the buffered
> half or the live half and never in both or neither. They are a copy,
> written to the client with no buffer lock held and under the same
> write deadline as a live entry. A stream with no filter is sent what
> it was sent before.
>
> Three things the text above does not say. `level` is an exact match
> on both routes, so `--follow --level INFO` shows no WARN or ERROR
> line. The stream route still does not read `limit`; the CLI sends
> none to either route. And `since` holds for live entries too: an
> entry stamped before it that reaches the orchestrator late is not
> sent, as the snapshot route would not return it.

> 2026-11-16 — the review of
> [#1131](https://github.com/mkhomutov/Persatrix/pull/1131) found that
> the route lost entries where the buffer did not. The subscriber was
> registered, then the disk was read and up to 1,000 buffered entries
> were written, and only then was the subscriber's channel read. The
> channel takes 256 entries; the 257th live entry that arrived before
> the buffered ones had been sent was dropped, counted in a counter
> nothing reads, with no line in any log. The filters ran after the
> channel, so `--level ERROR` did not keep an ERROR line from being
> pushed out by 256 others. Shown with a client that does not read
> while 400 live entries arrive: it got the first 256.
>
> Fixed in the same pull request. A subscriber now carries the
> stream's filters, and the buffer asks them before an entry takes a
> place in its queue, with `since` and without it. A subscriber that
> asked for buffered entries starts with a hold: live entries wait in
> memory, up to 5,000 (`sseHeldEntries` in
> `internal/server/logs_stream_handler.go`), from the instant of
> registration until the handler has sent the buffered entries and
> found the hold empty; from then on they go to the channel as before.
> The claim "never neither" therefore holds inside that bound: an
> entry is lost when more than 5,000 matching entries arrive before the
> client has taken the buffered ones, or when a live stream's reader
> falls more than 256 entries behind. Either loss is now logged by the
> orchestrator at `WARN` with the stream and the count, when the hold
> has been sent, at each heartbeat and when the stream ends. Five
> thousand is one run at the buffer's rate limit of 1,000 entries a
> second for five seconds, the time a full disk store took to read
> when measured (see
> [ISSUE-0265](ISSUE-0265-merged-log-view-reads-every-finished-run-from-disk.md)).
>
> The same review asked for the order of entries that share a
> timestamp on the merged view: the stream's buffered entries came in
> a different order on each connection. Both routes now order such
> entries by run id, which is the order the snapshot route already
> gave. Two items joined "What remains" above: the reader is still
> told nothing about a loss, and RFC 0018's row for the stream route
> names `limit`.
