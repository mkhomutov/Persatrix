---
id: ISSUE-0237
summary: "A memory write made of several statements is not all-or-nothing. A tier's connection is shared by every coroutine of its persona with no lock, so when such a write fails or is cancelled after its first statement, that first half is committed anyway: `record_interaction` leaves an interaction row with no count on the relationship, `insert_fact` a fact that supersedes nothing, `store_note` a pruned note with no new one, `replace_entry_projections` an entry with no projections."
status: open
severity: low
area: agents/memory
created: 2026-11-12
refs:
  - agents/memory/_write_boundary.py
  - agents/memory/relationship_mutations.py
  - agents/memory/_facts_write.py
  - agents/memory/_facts_erasure.py
  - agents/memory/notes.py
  - agents/memory/projections.py
  - docs/issues/ISSUE-0055-close-path-sqlite-commit-race-catchup-storm.md
  - docs/issues/ISSUE-0061-relationship-update-trust-returning-commit-race.md
  - docs/issues/ISSUE-0197-zero-row-write-keeps-the-memory-write-lock.md
---

# ISSUE-0237: A memory write of several statements is not all-or-nothing

## Summary

Five memory writes run more than one statement and commit once at the
end. Nothing makes the group atomic. If the write stops after its first
statement, because a later statement raises or the turn's task is
cancelled, the first statement's change is still committed.

## Context

Each memory tier holds one connection, and every coroutine of the persona
uses it with no lock around it (the commit races of
[ISSUE-0055](ISSUE-0055-close-path-sqlite-commit-race-catchup-storm.md)
and
[ISSUE-0061](ISSUE-0061-relationship-update-trust-returning-commit-race.md)
come from the same sharing). SQLite keeps one transaction per connection,
so a statement one coroutine has run is committed by whichever coroutine
commits next.

The five writes, and what the first half alone leaves:

| Function | File | First half alone |
|---|---|---|
| `record_interaction` | `agents/memory/relationship_mutations.py` | An interaction row; the relationship's count and last-seen time not updated |
| `insert_fact` | `agents/memory/_facts_write.py` | A new fact; the older facts it replaces still live |
| `delete_by_subject` | `agents/memory/_facts_erasure.py` | Facts about the subject deleted; facts sourced from its interactions kept |
| `store_note` | `agents/memory/notes.py` | The oldest note pruned; the new note missing |
| `replace_entry_projections` | `agents/memory/projections.py` | An entry's projections deleted, some or none written back |

Reproduced on `main` at `0f3eb3ac` with a relationship tier on a
temporary database: `record_interaction` cancelled after its first
statement left the connection in a transaction; a second connection saw
no rows; one unrelated `update_trust` on the first connection later, the
second connection saw the interaction row.

The fix for
[ISSUE-0197](ISSUE-0197-zero-row-write-keeps-the-memory-write-lock.md)
does not change the outcome, only its timing: a write that stops part-way
now commits at once instead of leaving its first half for the
connection's next commit. It commits and does not roll back because a
rollback on a shared connection would also discard a statement another
coroutine had run and not yet committed, and that coroutine's own commit
would then save nothing.

## Impact

Rare, and small when it happens. The second statement of each write is a
plain insert or update on the same database, so it fails only on a
cancellation at that point or on a fault such as a full disk. What is
left is a row that is true but incomplete: a count one short, a fact that
should have been replaced, one note fewer. Nothing reads it as corrupt.
The erasure case matters most, because `delete_by_subject` reports its
two counts only when both deletes ran.

Rated low on that reading; the severity is the maintainer's to set.

## Proposed fix / investigation path

Either give each tier a write lock, so that one write's statements and
commit run with no other coroutine's statement in between, after which
the error path can roll back safely; or rewrite each of the five as a
single statement where SQLite allows it. The lock is the general answer
and would let `agents/memory/_write_boundary.py` roll back instead of
commit.

## Notes

> 2026-11-12 — found while fixing
> [ISSUE-0197](ISSUE-0197-zero-row-write-keeps-the-memory-write-lock.md),
> when its error path had to choose between a commit and a rollback.
