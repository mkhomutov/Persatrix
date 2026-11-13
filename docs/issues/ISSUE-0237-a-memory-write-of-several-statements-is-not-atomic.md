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
  - agents/memory/_facts_reinforce.py
  - https://github.com/mkhomutov/Persatrix/pull/1104
---

# ISSUE-0237: A memory write of several statements is not all-or-nothing

## Summary

Seven memory writes can run more than one statement and commit once at
the end. Nothing makes the group atomic. If the write stops after its first
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

The seven writes, and what the first part alone leaves:

| Function | File | First part alone |
|---|---|---|
| `record_interaction` | `agents/memory/relationship_mutations.py` | An interaction row; the relationship's count and last-seen time not updated |
| `insert_fact` | `agents/memory/_facts_write.py` | A new fact; the older facts it replaces still live |
| `delete_by_subject` | `agents/memory/_facts_erasure.py` | Facts about the subject deleted; facts sourced from its interactions kept |
| `store_note` | `agents/memory/notes.py` | The oldest note pruned; the new note missing |
| `replace_entry_projections` | `agents/memory/projections.py` | An entry's projections deleted, some or none written back |
| `seed_trust` | `agents/memory/relationship_mutations.py` | Some of the configured relationships seeded (one insert each) |
| `mark_recalled_for_agent` | `agents/memory/_facts_reinforce.py` | Some facts marked as recalled, when the list is longer than 900 and is written in chunks |

Reproduced on `main` at `0f3eb3ac` with a relationship tier on a
temporary database: `record_interaction` cancelled after its first
statement left the connection in a transaction; a second connection saw
no rows; one unrelated `update_trust` on the first connection later, the
second connection saw the interaction row.

The fix for
[ISSUE-0197](ISSUE-0197-zero-row-write-keeps-the-memory-write-lock.md)
commits a write that stops part-way at once, where its first half used
to wait for the connection's next commit. On a running persona that
changes the timing only. On a persona's stop path it changes the
outcome: stopping cancels unfinished work, waits for it and closes the
tiers with no further commit, so the half-write used to be discarded at
the close and is now saved for good. Reproduced on both trees with
`record_interaction` cancelled after its first statement and the tier
then closed: one interaction row and no relationship row with the fix,
neither row without it.

The fix commits and does not roll back because a rollback on a shared
connection would also discard a statement another coroutine had run and
not yet committed, and that coroutine's own commit would then save
nothing.

## Impact

Rare, and small when it happens. The second statement of each write is a
plain insert or update on the same database, so it fails only on a
cancellation at that point or on a fault such as a full disk. A
cancellation there is what a stopping persona does to a turn still in
flight, and since the fix for ISSUE-0197 the half-write that leaves is
permanent. What is
left is a row that is true but incomplete: a count one short, a fact that
should have been replaced, one note fewer. Nothing reads it as corrupt.
The erasure case matters most, because `delete_by_subject` reports its
two counts only when both deletes ran.

Rated low on that reading; the severity is the maintainer's to set.

## Proposed fix / investigation path

Either give each tier a write lock, so that one write's statements and
commit run with no other coroutine's statement in between, after which
the error path can roll back safely; or rewrite each of the seven as a
single statement where SQLite allows it (the two looping writes cannot
be). The lock is the general answer and would let
`agents/memory/_write_boundary.py` roll back instead of commit.

## Notes

> 2026-11-12 — found while fixing
> [ISSUE-0197](ISSUE-0197-zero-row-write-keeps-the-memory-write-lock.md),
> when its error path had to choose between a commit and a rollback.

> 2026-11-13 — corrected after review of the pull request that filed
> this. The text said the ISSUE-0197 fix changed only when a half-write
> is committed. On a persona's stop path it changes whether: see
> Context. Two writes were missing from the list, `seed_trust` and a
> chunked `mark_recalled_for_agent`.
