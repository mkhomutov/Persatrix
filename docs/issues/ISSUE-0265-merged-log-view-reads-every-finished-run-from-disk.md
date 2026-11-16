---
id: ISSUE-0265
summary: "Each request for the merged log view (`GET /api/v1/executions/_/logs`) reads and parses the log files of every finished run on disk, up to the 512 MB disk cap, before it applies `since` and keeps at most `limit` entries: about five seconds and over a gigabyte of memory on a full store. A stream on `_` with `since` now does the same once per connection, holds its subscriber slot for that long after the client has gone, and a `persatrix logs _ --follow --since 5m` that reconnects repeats the read each time."
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
  - https://github.com/mkhomutov/Persatrix/pull/1131
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

### Measured

On a store of 510 MB (915 finished runs of 1,000 entries, about 560
bytes a line), on a development laptop, 2026-11-16:

| What | Time | Memory |
|---|---|---|
| The buffered entries of one stream on `_` with `since`, taken from the buffer (`SubscribeWithBacklog`): 915,000 entries read to send at most 1,000 | 4.8 s | heap from 49 MB to 1,065 MB when the call returns; 920 MB still held while the entries are in hand |
| The same entries gathered the snapshot route's way (`ListExecutions`, then `Snapshot` of each) | 4.4 s | not taken |
| Registering a stream with a backlog when all 50 rings are full (50,000 entries copied, nothing read from disk), 40 times | mean 12 ms, worst 33 ms | |
| The slowest `Append` beside those registrations | 33 ms | |

The rows were taken through the buffer with a throwaway test, not
committed, while another test run was going on the same machine (load
average 7 to 8). Read them as an order of magnitude.

The review of [#1131](https://github.com/mkhomutov/Persatrix/pull/1131)
measured the two routes themselves on a store of 510 MB, on another
machine under heavier load. These figures are that review's and were
not taken again:

- One connection to the stream on `_` with `since=5m`, the client gone
  after 0.2 s: the orchestrator went on reading for 4.9 s and its heap
  went from 49 MB to 1,541 MB.
- The snapshot route with the same flags: 4.8 s and 1,798 MB.
- `Append` held off while the rings are copied: 29 ms on average and
  56 ms at worst with 50 full rings.

### What the stream route adds to the old cost

The snapshot route has paid this cost since it was built. Giving the
stream route the same selection, in
[#1131](https://github.com/mkhomutov/Persatrix/pull/1131), put it on a
second route and added three things of its own:

1. **The slot stays taken after the client has left.** The handler
   registers its subscriber and then reads the disk inside one call. It
   cannot see that the client has gone until that call returns, so for
   the length of the read (4.9 s above) one of the 64 subscriber slots
   is held for nobody, and the memory with it. Sixty-four such
   connections refuse every other log stream with `429` for that long.
2. **`Append` waits while the rings are copied.** Registering with a
   backlog copies every ring under the lock that `Append` takes to
   hand an entry to the subscribers. With 50 full rings that is some
   tens of milliseconds for each new `since` stream, on `_` or on one
   run, during which no log entry is admitted. Before, registering held
   that lock for a map insert.
3. **Live entries wait in memory while the read goes on.** Up to 5,000
   matching entries for each stream that is opening (the hold that
   keeps the seam whole), on top of the entries read from disk.

## Impact

One request, or one reconnecting follower, makes the orchestrator read
its whole log store and hold it in memory: with a full store, about
five seconds of one core and between one and two gigabytes for each
request that is in flight. Several at once multiply the memory; the
REST rate limit allows 60 a minute for each caller. An orchestrator
with a memory limit near that figure can be killed by a handful of
merged-view requests, and it is the process that holds every run's
state.

Filed as low: the store is bounded, the cost is the orchestrator's own
disk and memory for the length of one request, and nothing is lost or
disclosed that the route does not already return. The reviewer of
[#1131](https://github.com/mkhomutov/Persatrix/pull/1131) thinks medium
is arguable on the measurements above: the cost is reachable by any
caller under the default `auth.mode: disabled`, it is paid in the one
process that must not die, and the stream route now holds a slot for
it. Against that: it needs a store that has grown to hundreds of
megabytes, and the routes are the operator's own tools. The severity
is as filed and not ruled; the figures above are here so that it can
be.

## Proposed fix / investigation path

Skip a run on disk when its newest file was written before `since`: an
entry is stamped before it is flushed, so such a run has nothing to
return (an entry stamped by an agent whose clock runs ahead is the
exception to settle). Without `since`, read runs newest first and stop
once `limit` entries are in hand. Either belongs in the buffer, so both
routes get it.

For the three things the stream route adds: read the disk before
registering the subscriber, or in a step the handler can abandon when
the request is cancelled, so that a client who has left frees its slot
at once; and copy the rings one at a time under each ring's own lock,
taking the subscribers' lock only for the registration, if the seam can
be kept exact that way.

## Notes

> 2026-11-15 — noticed while fixing the server half of
> [ISSUE-0223](ISSUE-0223-logs-follow-ignores-its-filters.md) in
> [#1131](https://github.com/mkhomutov/Persatrix/pull/1131), which
> gave the stream route the snapshot route's selection for `since` and
> with it this cost on `_`.

> 2026-11-16 — the review of
> [#1131](https://github.com/mkhomutov/Persatrix/pull/1131) measured
> the cost and named the two effects the stream route adds (the slot
> held after the client leaves, `Append` held off during the copy); the
> hold added in answer to that review is the third. The buffer-level
> rows of the table were taken again for this note; the route-level
> figures are the review's. The severity stays as filed until it is
> ruled.
