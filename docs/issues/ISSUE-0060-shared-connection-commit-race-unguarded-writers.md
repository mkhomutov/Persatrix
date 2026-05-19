---
id: ISSUE-0060
summary: Filed during the PR 380 review as "unguarded writers on the shared episodic connection". Deeper review of the SQLite failure mode showed plain INSERT/UPDATE/DELETE never leave an active write VDBE and so cannot trip the "SQL statements in progress" commit race — the sole real culprit was increment_interaction_count's unfetched RETURNING cursor, fixed under ISSUE-0055 in PR 380. No remaining hazard.
status: resolved
severity: medium
area: agents/memory
created: 2026-05-19
closed: 2026-05-19
closed_pr: 380
refs:
  - docs/issues/ISSUE-0055-close-path-sqlite-commit-race-catchup-storm.md
  - agents/memory/episodic.py
  - agents/memory/episodic_queries.py
  - agents/memory/notes.py
---

## Summary

Filed during the PR #380 review while ISSUE-0055 was still believed to
be a *write-vs-write* contention bug on the shared `aiosqlite`
connection. The hypothesis recorded here was that every writer other
than the two episode write paths — `recall()`'s access-count `UPDATE`,
`delete_episode`, the interaction-counter / agent-state helpers, and
every `NoteStore` write — was an equally unguarded hazard that could
raise the same `OperationalError`.

A deeper review of the actual SQLite failure mode showed that hypothesis
was wrong. This issue is resolved by the corrected ISSUE-0055 fix in the
same PR; see the Resolution section below.

## Resolution

`sqlite3.OperationalError: cannot commit transaction - SQL statements in
progress` is raised by SQLite only when a `COMMIT` runs while another
*write* statement is still an active VDBE (`db->nVdbeWrite > 0`).

- A plain `INSERT` / `UPDATE` / `DELETE` is stepped to completion inside
  its own `execute()` call and leaves **no** active VDBE. Through
  `aiosqlite`'s single per-connection worker thread every operation is
  serialised, so a later `COMMIT` never observes a plain write in
  flight. `recall()`'s access-count bump, `delete_episode`, the
  non-RETURNING counter / agent-state upserts, and every `NoteStore`
  write are all plain DML — **not** hazards. (`recall()`'s `SELECT`
  cursors are read-only and do not increment `nVdbeWrite` either.)
- The only statement that leaves a *write* VDBE suspended is an
  `INSERT … RETURNING`: `aiosqlite.Connection.execute()` steps it only
  to its first result row, so the write VDBE stays active across the
  `await` gap until a `fetchone()` drains it. `increment_interaction_count`
  was the sole `RETURNING` writer on the connection; `NoteStore` has
  none.

So there was exactly one culprit, not a class of unguarded writers.
ISSUE-0055 (#380) fixes it at the root: `increment_interaction_count`
now drains its `RETURNING` cursor in a single `execute_fetchall`
round-trip, so no write VDBE is ever suspended across an `await` and no
`COMMIT` on the shared connection can race. That closes the
shared-connection hazard this issue was opened to track — no separate
follow-up work remains, and `NoteStore`'s constructor needs no `db`-lock
change.

## Notes

> 2026-05-19 — initial capture during the PR #380 review, under the
> (then-current) write-vs-write framing of ISSUE-0055.

> 2026-05-19 — resolved. The continued #380 review reproduced the race
> against a real `EpisodicMemory` and established the mechanism above:
> the hazard is the unfetched `RETURNING` cursor, not unguarded plain
> writers. Fixed at the root in #380 under ISSUE-0055; this issue's
> premise no longer holds and there is nothing further to guard.
