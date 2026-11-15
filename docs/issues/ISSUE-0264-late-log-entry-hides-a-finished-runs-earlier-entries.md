---
id: ISSUE-0264
summary: "When a finished run's log ring has left memory and one more entry arrives for that run, the buffer starts a new ring that holds only the late entry. The snapshot route, and the buffered entries a stream sends for `since`, then show that one entry and not the run's earlier ones, which are still on disk; the late entry is never written to disk and is gone after a restart."
status: open
severity: low
area: internal/observability
created: 2026-11-15
refs:
  - internal/observability/logbuffer/buffer.go
  - internal/observability/logbuffer/lru.go
  - internal/observability/logbuffer/seal.go
  - internal/observability/logbuffer/subscribe.go
  - docs/issues/ISSUE-0223-logs-follow-ignores-its-filters.md
---

# ISSUE-0264: A late log entry hides a finished run's earlier entries

## Summary

The log buffer keeps one ring per run in memory, 50 at most
(`PERSATRIX_LOGBUFFER_MAX_EXEC`). A finished run's ring is written to
disk and can then be dropped from memory to make room; after a restart
only the newest 50 runs are loaded back. `Snapshot`
(`internal/observability/logbuffer/buffer.go`) answers from the ring
when the run has one and from disk only when it has none.

An entry that arrives for a finished run with no ring in memory goes
through `getOrCreateRing`, which makes a new, empty, unsealed ring:

- `Snapshot` now answers from that ring, so the run shows the late entry
  alone. Its earlier entries are on disk and no longer reachable through
  the snapshot route, nor through the buffered entries a stream sends
  for `since`, which are the same selection.
- Nothing seals the new ring. `Append` schedules a seal only for a ring
  already marked sealed, and the scheduler's end-of-run call has already
  happened. `Close` does not flush unsealed rings. So the late entry
  never reaches disk.

## Context

Shown with a throwaway test on `main` at `111b041d`, with room for one
run in memory:

| Step | `Snapshot` of the finished run |
|---|---|
| two entries, sealed, then another run starts and evicts it | `first`, `second` (from disk) |
| one late entry arrives | `late` only; the ring is unsealed and no seal is scheduled |
| restart | `first`, `second`; `late` is gone |

A late entry is not unusual: the agents' log shipper backs off up to 30
seconds and sends what it queued when it reconnects, which is also what
happens after an orchestrator restart. For the ring to be gone by then,
50 newer runs must have started, or the orchestrator must have
restarted with more than 50 runs on disk.

## Impact

For that run, `persatrix logs <id>` shows one line where there were
many until the next restart, and the late line itself is lost. Proposed
low: it needs a late entry for a run that has already left memory, and
the run's earlier entries are not lost, only hidden. The severity is
proposed here, not ruled.

## Proposed fix / investigation path

When `getOrCreateRing` makes a ring for a run that has files on disk,
load those entries into it and mark it sealed with nothing waiting, as
the warm load does for a run it reads back. A late entry then follows
the path it already has for a sealed ring in memory: it is appended and
a seal is scheduled, which writes it to a further file.

## Notes

> 2026-11-15 — noticed while fixing the server half of
> [ISSUE-0223](ISSUE-0223-logs-follow-ignores-its-filters.md) in
> [#1131](https://github.com/mkhomutov/Persatrix/pull/1131): the
> buffered entries a stream sends for `since` are what `Snapshot`
> returns, so they inherit this.
