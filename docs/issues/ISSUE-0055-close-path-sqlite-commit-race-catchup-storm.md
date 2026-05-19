---
id: ISSUE-0055
summary: Concurrent interaction idle-closes during a startup channel catch-up storm raise sqlite3.OperationalError "cannot commit transaction - SQL statements in progress" in _persist_closed_interaction; the affected episode fails to persist and the janitor backfills it to a summary sentinel. Surfaced repeatedly during the ISSUE-0054 live re-runs.
status: resolved
severity: medium
area: agents/persona_runtime
created: 2026-05-17
closed: 2026-05-18
closed_pr: 380
refs:
  - docs/issues/ISSUE-0054-rfc0026-facts-tier-extracts-no-facts.md
  - docs/rfcs/0020-interaction-lifecycle.md
  - agents/persona_runtime/episode_routing.py
---

## Summary

When a persona agent starts up and the channel catch-up replays a large
backlog of stale events, the resulting burst of concurrent RFC 0020
idle-gap interaction closes raises
`sqlite3.OperationalError: cannot commit transaction - SQL statements in
progress` inside `_persist_closed_interaction`. The affected close fails
to persist its episode summary; the janitor later backfills the row to a
sentinel (`[interaction summary unavailable]` / `[summary pending]`).

## Context

Observed twice while live-verifying [ISSUE-0054](ISSUE-0054-rfc0026-facts-tier-extracts-no-facts.md)
on the Docker Compose stack — not while reproducing ISSUE-0054 itself,
but as an adjacent failure during the same runs:

- **Re-run with `issue54-bob`** — startup channel catch-up replayed ~68
  stale events; the concurrent idle-closes raised the
  `OperationalError` in `_persist_closed_interaction`. Two old scopes
  failed to close and were janitor-backfilled to
  `[interaction summary unavailable]`.
- **Re-run with `issue54-carol`** — the catch-up storm again raised the
  same error for one replayed scope (`dm:ember-owl:my-custom-user`),
  backfilled to `[summary pending]`.

In both runs a single *uncontended* close (the one interaction actually
under test) succeeded cleanly, and the final ISSUE-0054 verification run
used a minimal stack with no catch-up storm and did not surface the
error at all. So the trigger is specifically **many concurrent closes
contending the same SQLite connection**, not the close path in
isolation.

The error text — `cannot commit transaction - SQL statements in
progress` — is the signature of a `COMMIT` issued on a connection that
still has an open cursor/statement from a concurrently-running
operation. The persona memory DB connection is shared across the
agent's async tasks; a catch-up storm fans out enough simultaneous
idle-closes that one close's `COMMIT` races another's in-flight
statement on that shared connection.

Investigation should start at
[`agents/persona_runtime/episode_routing.py`](../../agents/persona_runtime/episode_routing.py)
`_persist_closed_interaction` and the episodic-store write path it calls
(`store_episode`), looking at how the SQLite connection is shared and
whether close-path writes are serialised.

## Impact

**Severity: medium.** The failure is masked — the janitor owns the
orphaned row and backfills a summary sentinel, so there is no crash and
no lost episode row. But the affected interaction loses its real
episode summary (and, post-ISSUE-0054, its extracted facts), degrading
episodic recall for any interaction unlucky enough to close during a
catch-up storm. It is load-dependent (only large replay backlogs
trigger it) and so will surface intermittently on agent restarts in a
busy deployment rather than in steady state.

## Proposed fix / investigation path

1. Confirm the shared-connection hypothesis: reproduce by replaying a
   large stale-event backlog into a persona agent at startup and
   watching for the `OperationalError` in `_persist_closed_interaction`.
2. Serialise close-path DB writes — e.g. an `asyncio.Lock` around the
   persist/commit critical section, or a per-connection write queue —
   so concurrent idle-closes cannot interleave a `COMMIT` with another
   close's in-flight statement.
3. Alternatively, give the close path its own connection rather than
   sharing the agent-wide one, if the episodic store's connection model
   allows it.
4. Add a regression test that drives several concurrent interaction
   closes against one episodic store and asserts every episode persists
   a real summary (no sentinel backfill).

## Notes

> 2026-05-17 — initial capture. Split out of
> [ISSUE-0054](ISSUE-0054-rfc0026-facts-tier-extracts-no-facts.md),
> whose live re-run notes flagged this adjacent close-path concurrency
> bug twice as "worth its own ticket". Untouched by the ISSUE-0054 fix
> chain.

> 2026-05-18 — resolved in #380. Confirmed the shared-connection
> hypothesis (proposed-fix step 1) and took step 2: `EpisodicMemory`
> now carries a `_write_lock` (`asyncio.Lock`) held across the
> `INSERT`/`UPDATE` + `COMMIT` critical section of both episode write
> paths — `store_episode` (close-path Phase 1) and
> `update_episode_summary` (Phase 2). Concurrent close-path writes can
> no longer interleave a `COMMIT` with another write's in-flight
> statement. The `store_episode` `INSERT` was extracted to
> `agents/memory/episodic_queries.py` (`insert_episode`) so `episodic.py`
> stayed under the 500-line file-size cap. A regression test
> (`tests/unit/python/test_episodic_memory_concurrent_writes.py`, step 4)
> drives a concurrent close storm against one episodic store and asserts
> every write commits. Scope is close-vs-close, matching the diagnosis:
> `recall`'s access-count bump and the notes/counter write paths were
> not implicated in the storm and are left untouched.

> 2026-05-19 — follow-up filed as
> [ISSUE-0060](ISSUE-0060-shared-connection-commit-race-unguarded-writers.md):
> the `_write_lock` serialises only the two episode write paths, so
> `recall`'s access-count `UPDATE`, `delete_episode`, the counter /
> agent-state helpers, and `NoteStore` writes can still race a
> close-path write on the shared connection. The close-vs-close fix
> here stands; ISSUE-0060 tracks the remaining unguarded writers.
