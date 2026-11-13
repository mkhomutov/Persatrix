---
id: ISSUE-0197
summary: "Five memory writes commit only when they changed a row. A write that changes nothing still opens a transaction, so its connection keeps the SQLite write lock until its next commit, and a write from another memory tier in that gap fails with `database is locked` after five seconds and is dropped with a warning. On shipped paths only the interaction janitor of a persona that ticks leaves the lock held."
status: resolved
severity: low
area: agents/memory
created: 2026-11-10
closed: 2026-11-12
closed_pr: 1104
refs:
  - agents/memory/_write_boundary.py
  - agents/memory/relationship_decay.py
  - agents/participant.py
  - tests/unit/python/test_memory_write_releases_the_lock.py
  - tests/unit/python/test_memory_write_boundary.py
  - docs/issues/ISSUE-0237-a-memory-write-of-several-statements-is-not-atomic.md
  - agents/memory/interaction_janitor.py
  - agents/memory/episodic_retention.py
  - agents/memory/_episodic_replay_dedup.py
  - agents/memory/relationship_mutations.py
  - agents/persona_runtime/finalize_close.py
  - agents/persona_runtime/memory_assembly.py
  - docs/issues/ISSUE-0186-persona-memory-has-no-capacity-bound.md
  - docs/issues/ISSUE-0198-pending-summary-janitor-never-runs-without-a-tick.md
---

# ISSUE-0197: A write that touches no rows keeps the memory database's write lock

## Summary

Five functions run an `UPDATE` or `DELETE` and call `commit()` only when
the statement changed at least one row. Python's SQLite driver has by then
opened a transaction, and it stays open until the next commit on that
connection. The episodic, relationship and facts tiers and the timer cache
each hold their own connection to the same `memory.db`, so the open
transaction blocks every other tier's writes until then.

## Context

The five sites:

| Function | File | On a shipped path today |
|---|---|---|
| `cleanup_closing_interactions` | `agents/memory/interaction_janitor.py` | Yes, for a persona that ticks |
| `clear_failed_episode_for_interaction` | `agents/memory/_episodic_replay_dedup.py` | Yes, but the close path's next write commits on the same connection at once, so the lock does not stay held |
| `delete_old_episodes` | `agents/memory/episodic_retention.py` | No caller outside tests |
| `summarize_old_episodes` | `agents/memory/episodic_retention.py` | No caller outside tests |
| `apply_decay` | `agents/memory/relationship_mutations.py` | No caller outside tests |

[ISSUE-0186](ISSUE-0186-persona-memory-has-no-capacity-bound.md) records
that no memory clean-up runs for a persona, which is why three of the
five have no caller.

Reproduced on one temporary database with an episodic and a relationship
tier open:

1. Run the janitor sweep with nothing to sweep. It reports 0 rows, and the
   episodic connection's `in_transaction` is `True`.
2. Call `RelationshipMemory.record_interaction`. It raises
   `OperationalError: database is locked` after about five seconds, the
   driver's default wait.

`delete_old_episodes` with nothing to delete gives the same result.

Who meets it today: the interaction janitor runs at the end of a tick, at
most once every 300 seconds, and "nothing to sweep" is its normal case. A
semi-autonomous or autonomous persona whose config has no
`autonomy.timers` key ticks every `tick_interval_seconds`, 60 by default.
The three stock personas ship `timers: []` and never tick.

## Impact

For a persona that ticks, the first turn after a zero-row sweep waits five
seconds on its fact reinforcement, which is then dropped with one WARNING
(`memory_assembly.py`). The persona keeps answering while its fact records
fall behind. Stock personas are not affected, and the other four sites
leave no lock held on any path that runs today.

That is why this is rated low. It becomes every persona's path if the
sweep moves to boot or to inbound events, as
[ISSUE-0198](ISSUE-0198-pending-summary-janitor-never-runs-without-a-tick.md)
proposes, so this should be fixed first.

## Proposed fix / investigation path

Commit, or roll back, after every write statement whatever its row count,
and on the error path too. Today only `scheduled_wakes.py` rolls back. A
test that opens two tiers on one file and runs each zero-row write before
a write on the other tier would pin it.

## Notes

> 2026-11-10 — found by a whole-repository quality review of `main` at
> `ee9cfaac`; reproduced with a scratch probe against a temporary
> database. A second probe during review of this issue narrowed the live
> path to the janitor.

> 2026-11-12 — fixed. Reproduced first on `main` at `0f3eb3ac`: each of
> the five writes returned 0 rows with its connection still in a
> transaction, and `RelationshipMemory.record_interaction` on the other
> tier then failed with `database is locked` after 5.2 seconds. Two
> things the text above does not say: a write that raises, or whose task
> is cancelled between its statement and its commit, keeps the lock the
> same way, and that is every write in the package, not five; and
> `summarize_old_episodes` reaches its zero-row update only when the
> episode goes away while the model is writing the summary.
>
> Every write in `agents/memory/` now goes through
> `agents/memory/_write_boundary.py`, which ends the transaction
> whether the write changed a row, raised, or was cancelled. It ends it
> with a commit on the error path too, not a rollback: a tier's
> connection is shared by all of its persona's coroutines, and a
> rollback would discard a statement another coroutine had run and not
> yet committed. The migration runner and the timer cache roll back
> instead, each being the only user of its connection while it runs;
> the timer cache used to roll back on an error but not on a
> cancellation. The user store (`agents/participant.py`), a sixth
> connection to the same file with no caller in the agent runtime, has
> its two writes on the helper too.
> `tests/unit/python/test_memory_write_boundary.py` reads the package
> and that file, and fails on a write statement outside the helpers.
> `apply_decay` moved to `agents/memory/relationship_decay.py`, because
> `relationship_mutations.py` was at the 500-line warning.
>
> Not fixed here: a write of several statements that stops part-way
> keeps its first half. Before, the half was kept when the connection
> committed again and discarded when it closed first; now it is kept
> at once, on both paths. So on a persona's stop path, which cancels
> unfinished work and then closes the tiers, a half-write that used to
> be discarded is now saved. That is
> [ISSUE-0237](ISSUE-0237-a-memory-write-of-several-statements-is-not-atomic.md).
