---
id: ISSUE-0164
summary: "A persona cannot edit or delete a note it stored in a channel. `NoteStore.update_note`, `delete_note` and `count_notes` scope their SQL to the session the note store was given at start-up (`PERSATRIX_SESSION_ID`, or `legacy` when unset), while the `store_note` tool tags a note with the channel's session and `recall_notes` reads that session when called. The orchestrator gives each persona one session per channel and every channel turn runs under it, so a note the persona has just stored and recalled answers 'Note not found' to an edit or a delete (and `count_notes`, which no persona tool calls, leaves it out under a bound session). The reverse holds when `PERSATRIX_SESSION_ID` is set: a note tagged with it is recalled in no channel but can be changed by id from every channel. The fix reads the session at call time with the recall path's own helpers."
status: resolved
severity: medium
area: memory
created: 2026-09-22
closed: 2026-09-22
closed_pr: 979
refs:
  - agents/memory/notes.py
  - agents/memory/_notes_mutations.py
  - agents/memory/note_types.py
  - agents/memory/_session_filter.py
  - agents/tools/memory_tools.py
  - agents/request_scope.py
  - agents/session_id.py
  - internal/channels/session_binding.go
  - tests/unit/python/test_notes_mutation_session_scope.py
  - tests/unit/python/test_scope_snapshot_reads.py
  - docs/issues/ISSUE-0077-notes-mutation-not-session-scoped.md
  - docs/issues/ISSUE-0081-session-id-process-global-not-task-local.md
  - docs/experiments/EXP-001-preregistration.md
---

# ISSUE-0164: A persona cannot edit or delete a note it stored in a channel

## Summary

The note tools disagree about which session a note belongs to. `store_note`
tags a new note with the channel's session, and `recall_notes` looks in that
session, but `update_note`, `delete_note` and `count_notes` look in the session
the note store was given when the persona runtime started. Every channel turn
runs under its channel's session, so a persona can store a note and read it
back, and is then told "Note not found" when it tries to change or remove it.

## Context

**Two sessions in one turn.** Since
[ISSUE-0081](ISSUE-0081-session-id-process-global-not-task-local.md) the
session is read when it is used. The orchestrator gives each persona one
session per channel, a UUIDv7 minted the first time the pair is seen
(`SessionResolver` in `internal/channels/session_binding.go`), and sends it
with every channel message. The persona runtime binds it with `session_scope`
for the whole turn (`request_scope_from_metadata` in `agents/request_scope.py`,
entered in `_LLMPersonaAgent.on_event`), and `current_session_id()` returns it.
The memory tiers are built once, at start-up, with nothing bound, and keep what
`PERSATRIX_SESSION_ID` said then (`legacy` when it is unset) as the fallback.

| Note path | Session it reads |
|---|---|
| `store_note` tool (`agents/tools/memory_tools.py`) | `current_session_id() or` its start-up snapshot: the channel's |
| `NoteStore.recall_notes` | `_resolve_session_list(None, snapshot)`: the channel's, plus `legacy` |
| `NoteStore.update_note`, `delete_note`, `count_notes` | the start-up snapshot, plus `legacy`: never the channel's |

**How it came apart.**
[ISSUE-0077](ISSUE-0077-notes-mutation-not-session-scoped.md) scoped the three
mutation methods to `session_id IN (active, legacy)` in
[#452](https://github.com/mkhomutov/Persatrix/pull/452), where "active" was the
start-up snapshot, then the only session there was. The next PR,
[#453](https://github.com/mkhomutov/Persatrix/pull/453) (ISSUE-0081 PR 1), made
the active session task-local and moved every recall path to call time through
`_resolve_session_list`; the issue's plan for it named the "recall *and*
mutation paths". It did not touch `notes.py`, so the three methods kept binding
`self._active_session_id`. Nothing bound a session yet, so nothing broke until
[#459](https://github.com/mkhomutov/Persatrix/pull/459) (ISSUE-0082 PR 2) began
sending one with each channel message the same day.

**Why the tests missed it.** `test_notes_mutation_session_scope.py` changed
only `PERSATRIX_SESSION_ID` between two stores. None of its tests bound a
`session_scope`, so the snapshot and the active session were always the same.

## Impact

- **A persona's own notes are read-only in every channel.** `update_note` and
  `delete_note` answer "Note not found: <id>" for any note stored during a
  channel turn, including one the persona recalled a moment earlier. It cannot
  correct a note, or remove one a person asked it to forget. The 500-note cap
  does not clear it either: the cap prunes the least-recalled notes first, and
  every recall counts, including the one that puts a note into the prompt, so
  a note that keeps being recalled stays for good.
- **`count_notes` leaves them out, but only under a bound session.** No
  persona tool, runtime path or operator surface calls it. A caller with no
  session bound gets the same count before and after the fix; only one inside
  a `session_scope` (today, a test) missed the channel's notes.
- **The reverse, when `PERSATRIX_SESSION_ID` is set.** A note stored with no
  session bound (a tick turn, say) is tagged with the start-up session. No
  channel recalls it, yet `update_note` and `delete_note` reach it by id from
  every channel. The id has to reach the channel some other way first, since
  recall will not show it, so this is ISSUE-0077's defence-in-depth gap again,
  not a leak. With the variable unset, such a note is tagged `legacy`: every
  channel may change it by design, and every channel reads it unless the
  note's protection level is above the channel's.
- **Medium, not high.** No note reaches another channel's prompt, or one its
  protection level bars. A shipped pair of persona tools fails on the path
  every channel turn takes, and a note a person asked the persona to forget
  stays in that channel's memory.

## Fix

The three methods build their scope with the helpers `recall_notes` uses,
through one private function, `_mutation_scope_clause`:
`_resolve_session_list(None, snapshot)` and `session_in_clause` for the
session, then `principal_eq_clause` and `epoch_eq_clause`. A bound session
wins; the start-up snapshot applies only when none is bound; the `legacy`
carve-out and the strict principal and epoch equality are unchanged. A turn
now changes and counts the notes its recall reaches on the session, tenant
and epoch axes.

The methods still ignore the RFC 0037 protection level, as they always have.
An edit re-stamps a note upward (§C). A delete, which the RFC does not cover,
removes a note whatever its level. The fix extends that reach to the
channel's own notes: if a channel is classified down, a turn acting below a
note's level can delete it by id, though its recall withholds the note.
Whether deletes should respect the level is for the maintainer to rule on,
since ruling (b) keeps the §D gate as built.

The fix took `notes.py` past 500 lines, so the same PR moves the three methods
and their scope into a mixin in `agents/memory/_notes_mutations.py`, beside the
read queries in `_notes_recall.py`. They are one concern: a turn changes only
notes in the scope it recalls from. `NoteStore` keeps them as its own methods,
so no caller changes. The content check and its 10 KB limit move to
`note_types.py`, because `store_note` and `update_note` both call it.

Tests first, in `tests/unit/python/test_notes_mutation_session_scope.py`. With
`PERSATRIX_SESSION_ID=run-boot`, a note stored under `session_scope("sess-abc")`
can be updated, deleted and counted there, and `sess-xyz` can do none of it; a
`run-boot` note cannot be changed under `sess-abc` but still can with nothing
bound; a `legacy` note can still be changed under a bound session. Two more
call the `store_note`, `update_note` and `delete_note` tools directly inside
`session_scope`. One drives two real channel turns through `on_event`: the
first stores, recalls, edits and deletes its notes, and the second channel
gets "Note not found". `tests/unit/python/test_scope_snapshot_reads.py` scans
`agents/` so that no other surface puts a start-up snapshot straight into a
query.

## Slot: merges before EXP-001, by the maintainer's call of 2026-09-22

- **No plan is needed.** Ruling (a) of the
  [sequencing Amendment 2026-09-12](../v0.3.x-sequencing.md#amendment-2026-09-12--close-v0316-small-then-measure-before-any-train-opens)
  opens no plan before EXP-001 reports and the first strategy review logs its
  result. Like [#913](https://github.com/mkhomutov/Persatrix/pull/913) and
  [#963](https://github.com/mkhomutov/Persatrix/pull/963), which merged after
  the v0.3.16 tag, this is a standalone fix: no store migration, no RFC, no new
  setting.
- **Ruling (b)** keeps further memory isolation work off every release after
  v0.3.16 unless an outside ask is recorded. This fix does narrow one live
  path: with `PERSATRIX_SESSION_ID` set, a channel turn could change a
  start-up-session note by id, and now cannot. It narrows it to the boundary
  ISSUE-0077 promised and the recall path already keeps, and adds none of its
  own. It also widens the delete reach noted under Fix, and leaves the
  RFC 0037 §D gate and the audience check alone. That is still memory
  isolation work in ruling (b)'s terms, so it merges as an exception, on the
  maintainer's call below.
- **EXP-001.** The advisers keep the built-in note tools
  ([pre-registration §2](../experiments/EXP-001-preregistration.md#2-the-arms)),
  and every meeting is a new channel, so a new session. With the fix, an
  adviser that edits or deletes a note it stored earlier in the same meeting
  succeeds instead of reading "Note not found". That changes arm D, and arms
  B, C and D′ too unless the harness switches their memory writing off. It
  does not change what arm D carries from one meeting to the next: a note
  stays in its channel's session, so none crosses a meeting, with or without
  the fix. That is the difference from the fix for
  [ISSUE-0163](ISSUE-0163-withheld-episodes-reinforced-before-the-gate.md),
  [#972](https://github.com/mkhomutov/Persatrix/pull/972), which changes the
  ranking of what does carry and stays held.
- **Decision, 2026-09-22.** The maintainer chose to merge this before the
  EXP-001 run rather than hold it with #972, so every arm runs note tools
  that work in their own channel.

## Notes

> 2026-09-22 — found in the review of
> [#977](https://github.com/mkhomutov/Persatrix/pull/977), outside that PR's
> docstring-only scope, and filed with the fix drafted test-first. Against
> `4e84f5ab`, 5 of the 8 new tests failed (update, delete, count, the reverse
> case and the tool path); with the fix, none. The other 3 guard the scope in
> both directions: the two cross-session tests fail when the session clause
> is removed, and the carve-out test fails when `legacy` is dropped from it.
>
> 2026-09-22 — **resolved by [#979](https://github.com/mkhomutov/Persatrix/pull/979)**,
> which carries the fix, the `notes.py` split and this file. It merges before
> the EXP-001 run, by the maintainer's call recorded under Slot.
