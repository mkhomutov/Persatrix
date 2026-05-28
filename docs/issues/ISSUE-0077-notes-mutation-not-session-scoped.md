---
id: ISSUE-0077
summary: "`NoteStore.update_note` / `delete_note` / `count_notes` remain agent-scoped after RFC 0031 Phase 2 PR 2 — a non-active session that knows a note's UUID can mutate or delete a row owned by another session. The recall (read) surface is session-scoped + legacy carve-out, so UUID exposure to the LLM is constrained, but the write/admin surface bypasses §D — defence-in-depth gap, not an active exploit."
status: open
severity: low
area: agents/memory
created: 2026-05-28
refs:
  - docs/rfcs/0031-per-session-namespacing-channels.md
  - docs/rfcs/0031-phase2-pr-plan.md
  - agents/memory/notes.py
  - tests/unit/python/test_episodic_session_scope.py
---

## Summary

RFC 0031 Phase 2 PR 2 ([#449](https://github.com/mkhomutov/Persatrix/pull/449))
closed F-3 on the recall surface of the `notes` tier:
`NoteStore.recall_notes(sessions=…)` now filters by `(agent_id,
session_id)` and `_prune_notes` is scoped to `(agent_id, session_id)`.
The three remaining `NoteStore` public methods are **not** scoped:

* `update_note(note_id, content)` — `notes.py:287`
* `delete_note(note_id)` — `notes.py:306`
* `count_notes()` — `notes.py:315`

All three filter only on `agent_id`. A caller operating under
`PERSATRIX_SESSION_ID=run-b` who knows a UUID belonging to a `run-a`
note can mutate or delete it without §D guarding the write.

## Context

Found during the deep-review pass on PR #449
([review thread](https://github.com/mkhomutov/Persatrix/pull/449)),
finding 4. The PR title scopes the change to "Episodic + notes
**recall** filtering" so the mutation surface is intentionally
out-of-scope; this issue carries the gap forward.

## Impact

Defence-in-depth, **not** an active exploit at the current
LLM-prompt surface:

* The LLM only ever sees note IDs surfaced by `recall_notes`, which
  is now §D-filtered to active-session + legacy. A non-active
  session's LLM never sees another session's `run-a` UUIDs through
  normal channels.
* If a UUID leaks via another path (log line, error message,
  external storage, persisted agent state), the leak escalates to
  cross-session mutation because the write surface doesn't re-check
  the session.

Operator-visible symptom would be a `delete_note(...)` from
`run-b` that succeeds against a `run-a` row — same shape as the F-3
read-side reproduction in
`TestCrossEpisodicMemoryInstanceIsolation` (which only exercises
the read path).

`count_notes()` is the lowest-severity of the three — it returns an
agent-wide total rather than the active session's total, so a
caller reasoning about "how full am I in this session?" gets a
misleading number. Cosmetic, not corrupting.

## Proposed fix / investigation path

1. Add `session_id` to the `WHERE` clause on all three methods:

   ```python
   # update_note
   "UPDATE notes SET content = ?, updated_at = ? "
   "WHERE id = ? AND agent_id = ? AND session_id IN (?, ?)"
   # → (content, now, note_id, self._agent_id, self._active_session_id, LEGACY_SESSION_ID)

   # delete_note — same shape

   # count_notes — same shape, but returns from active session + legacy
   ```

2. Decide the policy on the **legacy carve-out** for mutations:
   * **Permissive** (matches recall): any session can update / delete
     a `session_id="legacy"` row. Most consistent with the read
     surface — the carve-out is "always visible", so "always
     mutable" is symmetric.
   * **Restrictive**: legacy rows are read-only from non-legacy
     sessions. Safer but breaks the symmetry and requires call-site
     thinking about why a `delete_note` returned `False`.

   Recommend **permissive** — keep the symmetry. If a future
   product requirement needs restrictive, gate it behind an explicit
   kwarg.

3. Mirror the tests in `TestCrossEpisodicMemoryInstanceIsolation`
   on the mutation surface: two `EpisodicMemory` instances under
   different `PERSATRIX_SESSION_ID`, then assert that
   `mem_b.update_note(run_a_id, …)` returns `False` and the
   underlying row is unchanged.

4. Consider whether `MemoryFacade` / `_EpisodicNotesAPIMixin` need a
   `sessions=` kwarg passthrough on the three methods, parallel to
   the recall-side passthrough. If yes, fold into [RFC 0031 Phase 2
   PR plan](../rfcs/0031-phase2-pr-plan.md) as a Phase 4 follow-up
   alongside the facade read-path extension.

## Notes

> 2026-05-28 — initial capture during PR #449 deep review.
> Read-path closer landed in PR #449; this is the mutation-path
> carry-forward. Currently latent (no LLM exposure of out-of-session
> UUIDs through normal channels). Track for Phase 2 PR 5
> (dementia-test bridge + review follow-ups) or a dedicated PR if
> the mutation tests reveal more surface than expected.
