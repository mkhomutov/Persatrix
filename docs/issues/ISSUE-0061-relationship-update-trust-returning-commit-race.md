---
id: ISSUE-0061
summary: RelationshipMemory.update_trust runs an INSERT … RETURNING upsert and drains it with execute() + a separate fetchone(), leaving the write VDBE active across the await; a concurrent COMMIT on RelationshipMemory's shared connection in that gap raises "cannot commit transaction - SQL statements in progress". Structural twin of the episodic-connection RETURNING race fixed under ISSUE-0055 — latent today (no concurrent production caller) but on the public RelationshipMemory surface. Found in the PR 380 review.
status: resolved
severity: medium
area: agents/memory
created: 2026-05-19
closed: 2026-05-19
closed_pr: 380
refs:
  - docs/issues/ISSUE-0055-close-path-sqlite-commit-race-catchup-storm.md
  - docs/issues/ISSUE-0060-shared-connection-commit-race-unguarded-writers.md
  - agents/memory/relationship_mutations.py
  - agents/memory/relationship.py
---

## Summary

`RelationshipMemory` shares a single `aiosqlite` connection across an
agent's async tasks (`RelationshipMemory.__init__` holds one
`aiosqlite.Connection`). `update_trust`
([`agents/memory/relationship_mutations.py`](../../agents/memory/relationship_mutations.py))
performs an `INSERT … ON CONFLICT DO UPDATE … RETURNING trust_score`
upsert and drains the `RETURNING` row with `await db.execute()` followed
by a separate `await cursor.fetchone()`.

`aiosqlite.Connection.execute()` steps a `RETURNING` statement only to
its first result row, so the *write* VDBE stays active across the
`await` gap between `execute()` and `fetchone()`. SQLite raises

```
sqlite3.OperationalError: cannot commit transaction -
SQL statements in progress
```

whenever a `COMMIT` runs while another *write* statement is still an
active VDBE (`db->nVdbeWrite > 0`). So any plain-DML writer that
`COMMIT`s on the shared relationship connection while `update_trust`
sits in that `execute()` → `fetchone()` gap raises — either the trust
update or the innocent concurrent writer fails.

This is the exact mechanism of
[ISSUE-0055](ISSUE-0055-close-path-sqlite-commit-race-catchup-storm.md),
the close-path commit race fixed in PR #380 for
`increment_interaction_count` on the *episodic* connection.
`update_trust` is its structural twin on the *relationship* connection.

## Context

Found during the [PR #380](https://github.com/mkhomutov/Persatrix/pull/380)
review (finding M1). PR #380 fixed the episodic-connection `RETURNING` race and,
via [ISSUE-0060](ISSUE-0060-shared-connection-commit-race-unguarded-writers.md),
audited the episodic connection and concluded `increment_interaction_count`
was its sole `RETURNING` writer — correct *for that connection*. The
relationship tier is a structurally identical single-shared-connection
design and has its own `RETURNING` writer that the ISSUE-0055 /
ISSUE-0060 audit did not cover.

## Impact

**Severity: medium, currently latent.** A `RETURNING` write only races a
*concurrent `COMMIT` on the same connection*. `update_trust` has no
concurrent production caller today — the persona close path bumps
relationships through `record_interaction`
([`agents/persona_runtime/record_close.py`](../../agents/persona_runtime/record_close.py)),
which is plain DML and correctly *not* a hazard, and `update_trust`'s
only other callers are the `RelationshipMemory.update_trust` facade
method and tests. So the bug does not fire in the current runtime.

But `update_trust` is part of the public `RelationshipMemory` tier
surface; a future concurrent caller — e.g. a periodic `apply_decay`
background sweep running alongside interaction-driven trust updates —
silently re-arms it. The failure mode would be a lost trust update, or
a spurious `OperationalError` surfacing on an unrelated writer that
happened to `COMMIT` in the gap.

## Resolution

`update_trust` now drains the `RETURNING` row in a single
`execute_fetchall` round-trip:

```python
rows = list(await db.execute_fetchall(""" … RETURNING trust_score """, ( … )))
await db.commit()
row = rows[0] if rows else None
```

`aiosqlite.Connection.execute_fetchall` runs `conn.execute()` and
`cursor.fetchall()` synchronously inside a single queued worker
function — there is no event-loop yield between the `RETURNING` step
and the drain, so the write VDBE is created, fully consumed, and reset
before the worker returns and is never observable by a concurrent
`COMMIT`. This is the same fix PR #380 applied to
`increment_interaction_count` under ISSUE-0055.

The `INSERT` / `ON CONFLICT` / `RETURNING` SQL and its parameter tuple
are unchanged; only the cursor-draining call changed, so the trust
arithmetic and the `row is None` handling are behaviour-preserving.

Regression test:
[`tests/unit/python/test_relationship_memory_concurrent_writes.py`](../../tests/unit/python/test_relationship_memory_concurrent_writes.py)
drives a real `RelationshipMemory` on a real `aiosqlite` connection — a
decay sweep (`apply_decay`, a plain `UPDATE` + `COMMIT` writer)
concurrent with a burst of `update_trust` upserts. Red before the fix
(the real `OperationalError`, 8/8 decay `COMMIT`s raced); green after.

## Notes

> 2026-05-19 — filed and resolved together. Raised as finding M1 of the
> PR #380 deep review. PR #380 established a clean, general mechanism
> ("an un-drained `RETURNING` cursor on a shared connection races
> `COMMIT`; plain DML does not") and closed ISSUE-0060 as "no remaining
> hazard", but that audit covered only the episodic connection.
> `RelationshipMemory.update_trust` is the same pattern on the
> relationship connection. Fixed with the identical `execute_fetchall`
> one-statement change. ISSUE-0060's summary is qualified to scope its
> "no remaining hazard" claim to the episodic connection and to
> cross-reference this issue.
