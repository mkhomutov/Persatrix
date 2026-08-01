---
id: ISSUE-0120
summary: "Personas that ran group channels before the ISSUE-0119 fix hold a human's relationship state on TWO rows — the correct user-typed row from DMs and an agent-typed row accumulated from group traffic. New writes land correctly now, but the pre-fix group-room trust, notes, and identity stay orphaned on the agent-typed row and never merge. A backfill needs both a human-id oracle the persona's memory.db does not have and a merge semantic for trust/interaction counts."
status: open
severity: low
area: agents/memory
created: 2026-08-01
refs:
  - docs/issues/ISSUE-0119-channel-publish-drops-human-participant-type.md
  - docs/issues/ISSUE-0093-person-identity-cross-room-tier.md
  - agents/memory/relationship_mutations.py
  - agents/memory/relationship_queries.py
---

## Summary

[ISSUE-0119](ISSUE-0119-channel-publish-drops-human-participant-type.md)
typed every human's group-channel traffic as `other_participant_type="agent"`,
so a persona that ran group channels before the fix holds the same human on
two relationship rows: the correct `(id, "user")` row written from DMs, and an
`(id, "agent")` row carrying whatever it learned about them in groups.

The fix stops the split from widening — every new publish resolves to `user`,
and the persona reads the DM-written row again, which is the reported symptom
gone. What remains is **cleanup of pre-fix data**: the group-room trust,
notes, and identity stranded on the agent-typed row.

## Why it did not ship with the fix

Two decisions the fix could not make on its own:

1. **No human-id oracle.** The merge has to know which `agent`-typed ids are
   actually humans, and the persona's `memory.db` has no registry access — the
   registry that answers this lives in the orchestrator, one process away. The
   plausible local heuristic is *an id holding BOTH a user-typed and an
   agent-typed row is one human* (real agents never accumulate a user-typed
   row, since only the chat path writes that type and it is the human door).
   It is a good heuristic, not a proof, and it is about to mutate person
   records irreversibly.
2. **Undecided merge semantics.** `merge_identity` already defines the
   identity-JSON union, but trust scores (average? max? keep the user row's?),
   interaction counts (sum? keep the larger?), and notes (concatenate? prefer
   the user row?) each change how the persona subsequently behaves toward that
   person. These are product calls, not mechanical ones.

Also note the merge must range over every `(principal, epoch)` pair, and the
epoch axis is a hard wall by design — rows must not be folded across it.

## Impact

Low and non-widening. The persona behaves correctly toward the human from the
fix onward; what is lost is pre-fix group-room history about them, which reads
as a one-time gap in an otherwise continuous relationship rather than an
ongoing fault. Deployments that never ran a human in a group channel before
the fix have nothing to migrate.

## Proposed fix / investigation path

Decide the two questions above, then a migration in `agents/memory/` that,
per `(principal, epoch)`, folds each `(id, "agent")` row into `(id, "user")`
where both exist — identity via `merge_identity`, the rest per the chosen
semantic — and drops the emptied agent-typed row. Worth an operator-visible
report of what merged: a silent rewrite of person records is the wrong shape
for this even when the heuristic is right.

## Notes

> 2026-08-01 — split out of ISSUE-0119 item 3 when the fix landed (PR #799).
