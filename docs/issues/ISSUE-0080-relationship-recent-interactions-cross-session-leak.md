---
id: ISSUE-0080
summary: "`RelationshipMemory.get_relationship_summary` filters the `relationships` row by `session_id` (RFC 0031 Phase 2 PR 3) but the secondary fetch into the `interactions` table is un-scoped — and the `interactions` table has no `session_id` column at all. When the relationship row IS visible to the active session, `recent_interactions` returns *every* cross-session interaction for that peer, and `interaction_count` is the global running total (`record_interaction`'s ON-CONFLICT increments the original first-seen row). F-3 read-side leak on the load-bearing prompt-injection surface; needs a v10 migration adding `session_id` to `interactions`."
status: resolved
resolution: "Closed by RFC 0031 Phase 2 PR 5.  Migration v10 adds `session_id TEXT NOT NULL DEFAULT 'legacy'` + `idx_interactions_session` to the `interactions` table; `record_interaction` threads the active session id onto every INSERT; both `interactions` SELECTs in `get_relationship_summary` (recent-history page + the `MIN(created_at)`/`MAX(created_at)` span) carry the §D predicate; `interaction_count` and `last_interaction_at` are derived per-session from the filtered subquery (Policy C — columns survive unchanged for the unfiltered admin / debug path); `get_all_relationships` derives both the count and the last-interaction timestamp via a LEFT JOIN with the same predicate so cadence aggregations no longer inherit the cross-session-inflated count or the cross-session `last_interaction_at` bump.  Deep-review follow-up: `last_interaction_at` was the `MAX(created_at)` twin of the enumerated `first_interaction_at` leak — `record_interaction`'s ON-CONFLICT refreshes the `relationships` column with no session predicate, so reading it surfaced another session's 'Last seen' (RFC 0021 cadence upper bound); now derived from the filtered subquery alongside `MIN`.  Pinned by `tests/unit/python/test_relationship_session_scope.py::TestRecentInteractionsAreSessionScoped` (xfail markers removed), `tests/unit/python/test_relationship_last_interaction_session_scope.py::TestLastInteractionAtIsSessionScoped` (the timestamp-twin pins, in their own module to keep the parent file under the 500-line cap), `tests/unit/python/test_session_id_interactions_migration.py`, and the integration-level `tests/integration/test_session_continuity.py::TestMultiSessionWriteSideIsolation::test_summary_count_does_not_inflate_across_sessions`."
severity: medium
area: agents/memory
created: 2026-05-28
closed: 2026-05-29
refs:
  - docs/rfcs/0031-per-session-namespacing-channels.md
  - docs/rfcs/0031-phase2-pr-plan.md
  - agents/memory/relationship_queries.py
  - agents/memory/relationship_mutations.py
  - agents/memory/migrations.py
  - agents/memory/_migration_interactions_session.py
  - tests/unit/python/test_relationship_session_scope.py
  - tests/unit/python/test_session_id_interactions_migration.py
  - tests/integration/test_session_continuity.py
---

## Summary

RFC 0031 Phase 2 PR 3 ([#450](https://github.com/mkhomutov/Persatrix/pull/450))
closed F-3 on the `relationships`-row read path:
`RelationshipMemory.get_trust` / `get_relationship_summary` /
`get_all_relationships` now filter by
`session_id IN (active, legacy)` by default. The
`get_relationship_summary` surface has **two** SQL fetches; only the
first one (the `relationships` row) is scoped:

`agents/memory/relationship_queries.py:121-132` — first fetch, §D
filtered (PR 3):

```sql
SELECT trust_score, interaction_count, last_interaction_at, notes
FROM relationships
WHERE participant_id = ? AND participant_type = ?
  AND other_participant_id = ? AND other_participant_type = ?
  AND session_id IN (...)   -- ← PR 3 added
```

`agents/memory/relationship_queries.py:147-158` — second fetch, **not**
scoped:

```sql
SELECT id, participant_id, participant_type,
       other_participant_id, other_participant_type,
       interaction_type, outcome, sentiment, created_at
FROM interactions
WHERE participant_id = ? AND participant_type = ?
  AND other_participant_id = ? AND other_participant_type = ?
ORDER BY created_at DESC LIMIT ?
-- ← no session predicate, and no session_id column to filter on
```

The `interactions` table has **no `session_id` column** —
`agents/memory/migrations.py:156-164` shows the v3 DDL; migration v7
added `session_id` to `episodes` and `relationships` but
deliberately skipped `interactions` (Phase 1 scoped the recall
predicate to the parent tables only).

Combined with `record_interaction`'s ON-CONFLICT path
(`agents/memory/relationship_mutations.py:248-271` — the conflict
branch updates `interaction_count` + `last_interaction_at` on the
**original first-seen row** regardless of the current session), the
net leak shape is:

* `interaction_count` on the visible row is the **global** count
  (sum across all sessions that interacted with the peer).
* `recent_interactions` returns every interaction for the
  participant pair, regardless of which session it occurred in.

## Context

Found during the deep-review pass on PR #450, finding M2. Reproduced
empirically:

```python
# Both interactions under the same RelationshipMemory(active=run-a)
await mem.record_interaction("peer-a", "task_delegation",
                             outcome="ok-A", session_id="run-a")  # row tagged run-a
await mem.record_interaction("peer-a", "task_delegation",
                             outcome="ok-B", session_id="run-b")  # ON CONFLICT bumps run-a row

summary = await mem.get_relationship_summary("peer-a")  # default = run-a + legacy
# summary.interaction_count == 2                  ← run-b's ON-CONFLICT bump leaked
# {i.outcome for i in summary.recent_interactions} == {"ok-A", "ok-B"}
#                                                  ← ok-B leaked into run-a's prompt
```

The PR 3 description hand-waves at this: "the §D filter applies to
the `relationships` row only; the `interactions` history we load
below is keyed off the same row's `other_participant_id`, so once
the row is filtered out the summary collapses to the 'no
relationship' branch and no interactions are fetched." This is
correct for the **row-filtered-out** path, but silent on the
**row-visible** path where the leak happens.

Pinned by
[`tests/unit/python/test_relationship_session_scope.py::TestRecentInteractionsCrossSessionLeakIsDocumentedGap`](../../tests/unit/python/test_relationship_session_scope.py)
as `pytest.mark.xfail(strict=True)`.

## Impact

`get_relationship_summary` is the **primary prompt-injection
surface** for the relationship tier — the structured render goes
directly into the persona's LLM context (see
`agents/persona_runtime` consumers). The leak is:

* `recent_interactions[*].outcome` and `.sentiment` exposed to the
  LLM despite the cross-session boundary — this is the canonical
  RFC 0031 §Motivation symptom ("a rerun's persona surfaces old
  participants and topics from a prior session"), just on the
  interaction-history channel rather than the episodes channel.
* `interaction_count` is misleading for cadence-rendering (RFC
  0021): "you and Alice have talked 47 times" when only 3 of those
  were in the current arc. The cadence prompt is built off this
  count.
* `first_interaction_at` (also fetched at
  `agents/memory/relationship_queries.py:180-187` via
  `MIN(created_at) FROM interactions`) inherits the same leak —
  the timestamp reflects the very first interaction in any
  session, not the active one.
* `get_all_relationships` reads the same `interaction_count`
  column directly on every visible row (without populating
  `recent_interactions`).  The cross-session-inflated count
  surfaces in list-mode reads too, so any cadence / intimacy
  computation that aggregates over all visible relationships
  inherits the noise.  Asymmetric: only the first-seen session's
  tier sees the inflated count (the row stays tagged with that
  session_id, so cross-session readers don't see the row at all).
  Pinned by `TestRecentInteractionsCrossSessionLeakIsDocumentedGap::test_get_all_relationships_count_excludes_foreign_session`
  as a sibling strict-`xfail` to the summary-surface pin.

Currently mitigated only by the fact that PR 3 ships before PR 4
(facade extension), so the production write path still tags rows
with the column-default `"legacy"` — all interactions share the
carve-out and the leak is masked. Once PR 4 lands and writes carry
the active session id, the leak goes from latent to live on the
dementia-test recall surface.

## Proposed fix / investigation path

1. **Migration v10**: add
   `session_id TEXT NOT NULL DEFAULT 'legacy'` to the `interactions`
   table + an `idx_interactions_session` (or the composite-covering
   variant from PR plan ISSUE-0078 — `(agent_id,
   other_participant_id, session_id, created_at DESC)`). Follow the
   precedent in `_apply_migration_7` for `relationships` /
   `episodes` and `_apply_migration_9` for `notes`.

2. **Write path**: thread `session_id` into
   `record_interaction`'s `INSERT INTO interactions (...)` —
   `agents/memory/relationship_mutations.py:221-240`. The function
   already accepts `session_id: str = "legacy"`; the value is
   currently used only for the `relationships` upsert and the
   metric attribute.

3. **Read path**: add the shared `_session_filter.session_in_clause`
   predicate to both `interactions` SELECTs in
   `get_relationship_summary`:

   ```python
   sess_clause, sess_params = session_in_clause(sessions, column="session_id")
   "SELECT ... FROM interactions "
   "WHERE participant_id = ? AND participant_type = ? "
   "AND other_participant_id = ? AND other_participant_type = ?"
   f"{sess_clause} "
   "ORDER BY created_at DESC LIMIT ?",
   (..., *sess_params, _MAX_RECENT_INTERACTIONS)
   ```

   Same shape for the `MIN(created_at)` `first_interaction_at`
   query.

4. **`interaction_count` policy**: decide whether the column is

   * **(A)** kept as the global cumulative count (matches the
     current column semantics — interaction *count*, not
     interaction count *in this session*), OR
   * **(B)** replaced by `SELECT COUNT(*) FROM interactions WHERE
     … AND session_id IN (…)` derived at read time, OR
   * **(C)** kept as the column but the read path overrides with
     a per-session count derived from the filtered interactions
     subquery.

   Recommend **(C)** — preserves the v3 column for the unfiltered
   admin / debug path, surfaces a per-session count to the prompt
   without touching the write path. Decision belongs in the same
   PR that adds the migration.  Apply the chosen policy
   uniformly across **both** `get_relationship_summary` (the
   row + secondary interactions read) and `get_all_relationships`
   (the row-only list-mode read) — they are pinned by sibling
   xfails and PR 5 should close both in the same patch.

5. **Update tests**:
   * Remove the `xfail` markers on **both** tests in
     `TestRecentInteractionsCrossSessionLeakIsDocumentedGap`
     (`test_recent_interactions_excludes_foreign_session_history`
     and `test_get_all_relationships_count_excludes_foreign_session`)
     once fixed. Strict-xfail will fail the suite as the forcing
     function.
   * Extend `test_relationship_memory_interactions.py` with
     migration-v10 round-trip coverage.
   * Add a `MIN(created_at)` first-interaction-at session-filter
     test, parallel to the cadence-rendering coverage RFC 0021
     pinned in PR 2 of this RFC.

## Notes

> 2026-05-28 — initial capture during PR #450 deep review.
> Owned by **PR 5** (dementia-test bridge + review follow-ups) per
> [docs/rfcs/0031-phase2-pr-plan.md §PR 5](../rfcs/0031-phase2-pr-plan.md#pr-5-featurev035-rfc0031p2-dementia-bridge--dementia-test-bridge--review-follow-ups).
> Severity is medium-not-low because `get_relationship_summary` is
> the primary prompt-injection surface for the tier and the leak
> directly contaminates the LLM prompt with cross-session content
> once PR 4's facade extension wires the active session id into
> production writes — that's a load-bearing dementia-test failure
> mode.

> 2026-05-29 — PR 5 deep-review follow-up. The §3 fix and the §Impact
> enumeration covered `recent_interactions` / `interaction_count` /
> `first_interaction_at` but missed `last_interaction_at`, the
> `MAX(created_at)` twin of the first-interaction-at leak. It is read
> straight from the `relationships.last_interaction_at` column, which
> `record_interaction`'s ON-CONFLICT refreshes keyed on the participant
> 4-tuple with no session predicate — so a cross-session write bumps
> the first-seen (or `legacy`) row's "Last seen" and the RFC 0021
> cadence upper bound. Closed in the same tier: both `get_relationship_summary`
> and `get_all_relationships` now derive `last_interaction_at` from the
> session-filtered `interactions` subquery (`MAX(created_at)`), symmetric
> with the `MIN` already in place.
