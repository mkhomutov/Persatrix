---
id: ISSUE-0197
summary: "Four memory writes commit only when they changed a row. A write that changes nothing still opens a transaction, so its connection keeps the SQLite write lock, and the next write from another memory tier fails with `database is locked` after five seconds and is dropped with a warning."
status: open
severity: medium
area: agents/memory
created: 2026-11-10
refs:
  - agents/memory/interaction_janitor.py
  - agents/memory/episodic_retention.py
  - agents/memory/_episodic_replay_dedup.py
  - agents/memory/relationship_mutations.py
  - agents/persona_runtime/memory_assembly.py
  - agents/persona_runtime/record_close.py
---

# ISSUE-0197: A write that touches no rows keeps the memory store's write lock

## Summary

Four functions run an `UPDATE` or `DELETE` and call `commit()` only when
the statement changed at least one row. Python's SQLite driver has by then
opened a transaction, and it stays open until the next commit on that
connection. The episodic, relationship and facts tiers, the commitments
store and the timer cache each hold their own connection to the same
`memory.db`, so the open transaction blocks every other tier's writes.

## Context

The four sites:

| Function | File |
|---|---|
| `cleanup_closing_interactions` | `agents/memory/interaction_janitor.py` |
| `delete_old_episodes` | `agents/memory/episodic_retention.py` |
| `clear_failed_episode_for_interaction` | `agents/memory/_episodic_replay_dedup.py` |
| `apply_decay` | `agents/memory/relationship_mutations.py` |

Reproduced on one temporary store with an episodic and a relationship
tier open:

1. Run the janitor sweep with nothing to sweep. It reports 0 rows, and the
   episodic connection's `in_transaction` is `True`.
2. Call `RelationshipMemory.record_interaction`. It raises
   `OperationalError: database is locked` after 5.17 seconds, the driver's
   default wait.

`delete_old_episodes` with nothing to delete gives the same result.

The janitor is the last store action of a tick, and "nothing to sweep" is
its normal case. A semi-autonomous or autonomous persona whose config has
no `autonomy.timers` key ticks every `tick_interval_seconds`, 60 by
default. `clear_failed_episode_for_interaction` runs on the boot replay
path, where it also normally deletes nothing.

## Impact

The next turn's fact reinforcement or close-time `record_interaction`
waits five seconds and is then dropped. The callers catch the error and
log one WARNING (`memory_assembly.py`, `record_close.py`), so the persona
keeps answering while its relationship and fact records fall behind.

## Proposed fix / investigation path

Commit, or roll back, after every write statement whatever its row count,
and on the error path too. Today only `scheduled_wakes.py` rolls back. A
test that opens two tiers on one file and runs each zero-row write before
a write on the other tier would pin it.

## Notes

> 2026-11-10 — found by a whole-repository quality review of `main` at
> `ee9cfaac`; reproduced twice with a scratch probe against a temporary
> store.
