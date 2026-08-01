---
id: ISSUE-0120
summary: "Personas that ran group channels before the ISSUE-0119 fix hold a human's relationship state on TWO rows — the correct user-typed row from DMs and an agent-typed row accumulated from group traffic. New writes land correctly now, but the pre-fix group-room trust, notes, and identity stay orphaned on the agent-typed row and never merge. A backfill needs both a human-id oracle the persona's memory.db does not have and a merge semantic for trust/interaction counts."
status: resolved
severity: low
area: agents/memory
created: 2026-08-01
closed: 2026-08-01
refs:
  - docs/issues/ISSUE-0119-channel-publish-drops-human-participant-type.md
  - docs/issues/ISSUE-0093-person-identity-cross-room-tier.md
  - agents/memory/relationship_mutations.py
  - agents/memory/relationship_queries.py
  - agents/memory/_migration_split_participant.py
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

## Resolution — memory migration v17

Both questions were decided by the maintainer and are encoded in
[`_migration_split_participant.py`](../../agents/memory/_migration_split_participant.py):

- **Oracle**: the heuristic above, narrowed to its safest form — the fold
  selects only ids holding **both** a user-typed and an agent-typed row for
  the same `(agent, principal, epoch)`, expressed as a self-join, so a row
  with no twin is never returned and a genuine agent peer is never rewritten.
  The blast radius is exactly the bug's own footprint.
- **Merge semantics**: `interaction_count` **sums**; `last_interaction_at`
  takes the **max**; `trust_score` is the **interaction-weighted average**,
  because trust *is* an aggregate over interactions — a 2-interaction group
  row cannot outvote a 40-interaction DM row, and with no interactions on
  either side the user row's value stands; `identity` merges
  **older-into-newer** by `last_interaction_at` so the live write-through's
  last-writer-wins scalar rule survives; `notes` takes the **newer** row's
  value rather than a concatenation, because the column holds the latest
  *trust-change reason* and joining two would manufacture one that never
  existed. `principal_id` / `epoch_id` are match axes, never merged across.
- **Delivery**: an automatic migration (the v14 backfill precedent) rather
  than an operator command, so no deployment stays split through not knowing
  it should run something — with one INFO line per fold plus a total, since a
  rewrite of person records nobody can see afterwards is the wrong shape even
  when the heuristic is right.
- **Idempotency** comes from deleting the agent-typed row as part of each
  fold: a re-run finds no pair, so a crash-replay between the handler and the
  `schema_version` record cannot double the summed count.

Verified end-to-end against the real post-chain schema (the live write paths
produce the split, v17 folds it, the identity/count/trust land merged and the
agent-typed row is gone) on top of the unit suite in
[`test_split_participant_migration.py`](../../tests/unit/python/test_split_participant_migration.py).

Registry note: adding v17 pushed `agents/memory/migrations.py` past the
500-line code cap, so the `MIGRATIONS` list moved to
`agents/memory/_migration_registry.py` and is re-exported — reference data
whose length scales with migration history, split from logic for the same
reason `scripts/checks/file_size_allowlist.py` and `_migration_handlers.py`
already were. Every `from .migrations import MIGRATIONS` call site is
unchanged.

## Notes

> 2026-08-01 — split out of ISSUE-0119 item 3 when the fix landed (PR #799).
