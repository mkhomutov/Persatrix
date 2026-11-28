---
id: ISSUE-0268
summary: "After a restart the log buffer reads a finished run back from disk oldest entry first and stops when the run's ring is full. A run with more entries on disk than a ring holds therefore comes back with its oldest entries and without its newest, and `persatrix logs`, and the buffered entries `--follow --since` prints first, miss that run's latest lines."
status: open
severity: low
area: internal/observability
created: 2026-11-16
refs:
  - internal/observability/logbuffer/lru.go
  - internal/observability/logbuffer/buffer.go
  - internal/observability/logbuffer/ring.go
  - docs/observability.md
  - docs/issues/ISSUE-0223-logs-follow-ignores-its-filters.md
  - docs/issues/ISSUE-0264-late-log-entry-hides-a-finished-runs-earlier-entries.md
  - https://github.com/mkhomutov/Persatrix/pull/1131
---

# ISSUE-0268: A reloaded log ring keeps a run's oldest entries

## Summary

When the orchestrator starts, `warmLoad`
(`internal/observability/logbuffer/lru.go`) rebuilds a ring in memory
for each of the newest 50 finished runs on disk. For each run it reads
every entry the run has on disk, appends them to a new ring oldest
first, and stops as soon as the ring is full:

```go
for _, e := range entries {
	ring.append(e)
	if ring.full {
		break
	}
}
```

A ring holds 1,000 entries (`PERSATRIX_LOGBUFFER_PER_EXEC`). A run with
more than that on disk comes back as its first 1,000 entries. Its
newest entries are on disk and not in the ring, and `Snapshot` answers
from the ring whenever a run has one, so neither log route returns
them.

## Context

A run has more entries on disk than a ring holds in two cases:

- **Entries that arrived after the run ended.** A run's ring is written
  to disk two seconds after the run ends. An entry that arrives later
  is kept in the ring and written to a further file. A run that filled
  its ring and then got one late entry has 1,001 entries on disk; after
  a restart the ring holds the first 1,000 and the late entry is left
  out. The agents' log shipper sends what it queued when it reconnects,
  so late entries are ordinary.
- **A smaller ring after the restart.** If
  `PERSATRIX_LOGBUFFER_PER_EXEC` is lowered between two starts, every
  run that had filled the larger ring comes back as its oldest part.

Shown with a throwaway test, not committed: a ring of four entries, a
run that logs four entries and is sealed, then one late entry, then a
restart.

| Step | `Snapshot` of the run |
|---|---|
| before the restart | `m1`, `m2`, `m3`, `late` (the ring dropped `m0` to make room) |
| on disk | `m0`, `m1`, `m2`, `m3`, `late` |
| after the restart | `m0`, `m1`, `m2`, `m3`: `late` is missing |

The code is the same on `main` at `111b041d`. The observability guide
said that across a restart a followed log with `--since` prints "the
entries of that window that the restarted orchestrator read back from
disk"; for such a run those are not its latest. The guide now says so.

## Impact

For a run in one of the two cases, `persatrix logs <id>` and the first
lines of `persatrix logs <id> --follow --since …` show the run's
beginning and not its end, until 50 newer runs push it out of memory;
from then on `Snapshot` reads the disk and returns everything. The
last lines of a run are the ones a person looks for after a failure.
Nothing is lost: every entry is still on disk.

Proposed low: it takes a restart and a run that outgrew its ring on
disk, the entries are hidden and not lost, and the tracker's precedent
for a hidden-not-lost log fault is low
([ISSUE-0264](ISSUE-0264-late-log-entry-hides-a-finished-runs-earlier-entries.md)).
The severity is proposed here, not ruled.

## Proposed fix / investigation path

Append every entry in `warmLoad` and let the ring do what it does for a
live run: drop its oldest entry to make room. The reloaded ring then
holds the run's newest entries, as it did before the restart. A test
with a ring smaller than the run's entries on disk pins it.

## Notes

> 2026-11-16 — found by the review of
> [#1131](https://github.com/mkhomutov/Persatrix/pull/1131), which
> wrote the guide's restart sentence for
> [ISSUE-0223](ISSUE-0223-logs-follow-ignores-its-filters.md). Filed
> there; the code is not changed by that pull request.

> 2026-11-28 — severity ruled `low`, as proposed: the maintainer's
> ruling at the
> [strategy review of 2026-11-28](../project-strategy-review-2026-11-28.md#3-the-maintainers-rulings),
> ratified by ruling (ab) of the
> [sequencing Amendment 2026-11-28](../v0.4.x-sequencing-2026-11-28.md#rulings).
