---
id: ISSUE-0060
summary: ISSUE-0055's _write_lock serialises only the two episode write paths; recall()'s access-count UPDATE, delete_episode, the interaction-counter / agent-state helpers, and every NoteStore write still run execute+commit unguarded on the same shared aiosqlite connection, so a recall (or note write) concurrent with a close-path write during a catch-up storm can raise the same "cannot commit transaction - SQL statements in progress" OperationalError.
status: open
severity: medium
area: agents/memory
created: 2026-05-19
refs:
  - docs/issues/ISSUE-0055-close-path-sqlite-commit-race-catchup-storm.md
  - agents/memory/episodic.py
  - agents/memory/episodic_queries.py
  - agents/memory/notes.py
---

## Summary

ISSUE-0055 (#380) added `EpisodicMemory._write_lock` and holds it across
the `INSERT`/`UPDATE` + `COMMIT` critical section of the two episode
write paths — `store_episode` and `update_episode_summary`. It does not
make the shared `aiosqlite` connection safe in general: every *other*
writer on that same connection still runs its `execute` + `commit` as
two unserialised `await`s. A `recall()` (or note write) that runs
concurrently with a close-path write — which is expected during the very
catch-up storm ISSUE-0055 describes — can still interleave one writer's
`COMMIT` with another's in-flight statement and raise the identical
`sqlite3.OperationalError: cannot commit transaction - SQL statements in
progress`.

## Context

Captured during the review of PR #380 (the ISSUE-0055 fix). The
`_write_lock` is acquired only by `store_episode`
([`agents/memory/episodic.py`](../../agents/memory/episodic.py) — the
`async with self._write_lock` around `insert_episode`) and
`update_episode_summary`. The following writers share the same
connection (`EpisodicMemory._db`, also handed to `NoteStore` as `db=`)
and take **no** lock around their `execute` + `commit`:

- `recall()` — the `access_count` bump: `UPDATE episodes SET
  access_count = access_count + 1 ...` followed by `await db.commit()`.
- `delete_episode()`.
- `increment_interaction_count` / `reset_interaction_count` /
  `persist_agent_state` (the helpers in `episodic_queries.py`).
- Every `NoteStore` write (`store_note` / `update_note` / `delete_note`)
  — `NoteStore` is constructed with `db=self._db`, i.e. the same
  connection object.

ISSUE-0055 scoped its fix to "close-vs-close" because the captured storm
was a burst of idle-gap closes. But a catch-up storm replays the whole
agent's event backlog: while some interactions idle-close (close-path
writes), others are active and the agent issues `recall()` to build
their prompt context. `recall`-vs-close on one shared connection is
therefore plausible in the same storm, and `_write_lock` does not cover
it because `recall` never acquires the lock.

## Impact

A `recall()` access-count `COMMIT` (or a `NoteStore` write) that
interleaves with a close-path `INSERT` during a catch-up storm raises
the same `OperationalError`. For `recall` the access-count bump is
best-effort bookkeeping, but the exception propagates out of `recall`
(its `try/except` only marks the OTEL span ERROR and re-raises), so the
recall itself fails and the caller loses the retrieved episodes for that
turn. A failed `NoteStore` write loses the note. The blast radius is
wider than the episode-write race ISSUE-0055 closed: not observed in the
ISSUE-0055 captures, hence `medium`, but the hazard is real and latent.

## Proposed fix / investigation path

The root cause is one connection shared across the agent's async tasks
with multiple unserialised `execute`+`commit` pairs. Make *every* writer
on that connection go through a single lock:

- Wrap `recall()`'s access-count `UPDATE` + `commit` and
  `delete_episode`'s `DELETE` + `commit` in `async with self._write_lock`
  (the read/SELECT portion of `recall` stays outside the lock so reads
  are not serialised).
- Route `increment_interaction_count` / `reset_interaction_count` /
  `persist_agent_state` through the lock at their `EpisodicMemory` call
  sites.
- Share the lock with `NoteStore`: pass `_write_lock` into its
  constructor and hold it across each note write's `execute` + `commit`.

A single per-connection write lock is the smallest correct change.
NoteStore's constructor signature change is why this is a follow-up
rather than part of #380 — it is a wider edit than the close-path fix
warranted on its own.

## Notes

> 2026-05-19 — initial capture during the PR #380 (ISSUE-0055) review.
> #380 correctly closes the close-vs-close race it was scoped to; this
> issue tracks the remaining unguarded writers on the same shared
> connection so the shared-connection hazard is recorded rather than
> lost once ISSUE-0055 is marked resolved.
