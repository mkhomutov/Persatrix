---
id: ISSUE-0188
summary: "The channels store has had no index on messages by channel in time order since channels-store v3 (RFC 0031 PR 2, #335) replaced `idx_messages_channel_ts` with `(channel_id, session_id, timestamp DESC)`. Every newest-first channel read and the cap's oldest-first prune now sort the whole channel, and the scoped window read (`GetHistoryScoped`) scans the whole table through the epoch index. On a channel at its 10 000-message cap a window read takes ~47 ms and a publish ~12 ms, against 0.2 ms and 1.2 ms with the index, so ten agents posting a message a second each saturate the store's one connection. Found by the v0.4.0 storage benchmark; the fix, one index in its own migration, is the first PR of RFC 0029's Phase 3 plan."
status: open
severity: medium
area: channels
created: 2026-10-21
refs:
  - internal/channels/sqlite_migrations.go
  - internal/channels/sqlite_messages.go
  - internal/channels/sqlite_messages_read.go
  - docs/storage-architecture-benchmark.md
  - docs/rfcs/0029-phase3-pr-plan.md
---

# ISSUE-0188: The channels store lost its channel-time index

## Summary

Reading a channel's latest messages, and pruning its oldest, should touch a
handful of rows. Since channels-store v3 the store has had no index that
serves either in time order, so both sort the whole channel, and the scoped
window read every persona makes on each delivery scans the whole table. A
busy channel at its cap then makes each read tens of milliseconds, and the
store's single connection saturates at the load the v0.4.0 society backend
must carry.

## Context

`migrateV2ToV3` (RFC 0031 PR 2, [#335](https://github.com/mkhomutov/Persatrix/pull/335))
dropped `idx_messages_channel_ts` and created
`idx_messages_channel_session(channel_id, session_id, timestamp DESC)`. Its
comment says the leading `channel_id` "keeps today's `WHERE channel_id = ?
ORDER BY timestamp DESC` scan cheap". It does not: with `session_id` between
them, the index orders a channel's rows by session first, so SQLite reads
every row of the channel and sorts them (`USE TEMP B-TREE FOR ORDER BY` in
`EXPLAIN QUERY PLAN`). Channels-store v6 then added
`idx_messages_epoch(epoch_id)`. With no statistics (`ANALYZE` never runs),
the planner takes it for the scoped window read, whose `WHERE` has
`m.epoch_id = ?`, and walks every message in the store, since every row has
the epoch `live`.

Three hot reads are affected:

| Read | Where | What it does today |
|------|-------|--------------------|
| History, newest first | `GetHistory` (`sqlite_messages_read.go`) | sorts the whole channel |
| The cap's prune | `pruneExcess` (`sqlite_messages.go`), on every publish once a channel is at its cap | sorts the whole channel to find the oldest rows |
| The scoped window | `GetHistoryScoped`, behind `GET /api/v1/channels/{id}/messages?as_participant=`, which each persona's conversation window fetches when a message arrives | scans every message in the store through the epoch index, then sorts |

## Impact

Measured by the [storage benchmark](../storage-architecture-benchmark.md) on
a store whose channels are at their 10 000-message cap, each call alone:

| Call | One channel (10 000 rows) | Ten channels (100 000 rows) | With the index |
|------|---------------------------|-----------------------------|----------------|
| `GetHistoryScoped(50)` | ~47 ms | ~96 ms | ~0.2 ms |
| `PublishMessage` at the cap | ~12 ms | ~22 ms | ~1.2 ms |

Every call on the store shares one connection, so a delivery's window reads
queue behind each other. At ten agents posting a message a second each into
one channel, the store fell minutes behind and published about a third of
what was offered. Small deployments do not see it; a long-lived, busy one
does, and v0.4.0's society tables would share that connection.

## Proposed fix / investigation path

Restore the index, `ON messages(channel_id, timestamp DESC)`, in a migration
of its own (v13 → v14), under a new name (`idx_messages_channel_time`) so the
session migration's tests that pin the old name's removal keep their
meaning. With it, the planner uses the index for
all three reads (checked with `EXPLAIN QUERY PLAN`) and the benchmark's gate
passes. Add a query-plan test per read, so a later migration cannot drop the
index again unnoticed.

The index alone fixes the window read only because the planner, with no
statistics, happens to prefer it to `idx_messages_epoch`, whose one value
matches every row. Run `PRAGMA optimize` when the store opens, as SQLite
advises for long-lived connections, so the planner has statistics and no
epoch-filtered read falls back to walking the whole store.

**Slot:** v0.4.0, as the first PR of
[RFC 0029's Phase 3 plan](../rfcs/0029-phase3-pr-plan.md). The society-backend
lock stands only while SQLite holds, and the benchmark shows it holds only
with this index, so the phase that boards states it as its dependency
(ruling q).

## Notes

> 2026-10-21 — filed from the v0.4.0 row R2 storage benchmark, run on
> `b1a7932f`. Docs only: no code changes.
