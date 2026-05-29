---
id: ISSUE-0079
summary: "`FactStore.store` invokes `_facts_supersede.apply_supersession`, which keys symmetric latest-asserted-wins on `(agent_id, subject, predicate)` with no `session_id` predicate — a fact written under one session can mark the same `(subject, predicate)` row from another session as `superseded_by`, removing it from the other session's default recall. Read-side §D recall filtering shipped in PR #450, but the write-side gap leaves F-3 only partially closed on the facts surface."
status: resolved
resolution: "Closed by RFC 0031 Phase 2 PR 5 — supersede is now keyed on `(agent_id, subject, predicate, session_id)` per the RFC 0026 §F amendment.  Each session keeps its own truth about `(subject, predicate)`; a write in `run-b` cannot retroactively contaminate `run-a`'s view.  Pinned by `tests/unit/python/test_facts_session_scope.py::TestCrossSessionSupersedeIsSessionScoped` (xfail marker removed) and by the integration-level `tests/integration/test_session_continuity.py::TestMultiSessionWriteSideIsolation::test_arc_2_fact_does_not_supersede_arc_1_fact`."
severity: medium
area: agents/memory
created: 2026-05-28
closed: 2026-05-29
refs:
  - docs/rfcs/0031-per-session-namespacing-channels.md
  - docs/rfcs/0031-phase2-pr-plan.md
  - docs/rfcs/0026-declarative-facts-tier.md
  - agents/memory/_facts_supersede.py
  - agents/memory/facts.py
  - tests/unit/python/test_facts_session_scope.py
  - tests/integration/test_session_continuity.py
---

## Summary

RFC 0031 Phase 2 PR 3 ([#450](https://github.com/mkhomutov/Persatrix/pull/450))
closed F-3 on the recall surface of the `facts` tier:
`FactStore.recall(sessions=…)` now filters by
`session_id IN (active, legacy)` by default. The complementary
write surface — RFC 0026 §F symmetric latest-asserted-wins —
remains **un-scoped by `session_id`**.

`agents/memory/_facts_supersede.py:87-97`:

```python
SELECT fact_id FROM facts
WHERE agent_id = ?
  AND subject = ?
  AND predicate = ?
  AND superseded_by IS NULL
  AND asserted_at <= ?
  AND fact_id != ?
```

No `session_id` predicate. A `FactStore.store(session_id="run-b")`
call with a later `asserted_at` than an existing live row tagged
`session_id="run-a"` writes `superseded_by = <run-b-row-id>` onto the
`run-a` row. Default recall in `run-a` filters
`superseded_by IS NULL AND session_id IN ('run-a', 'legacy')` — the
`run-a` row is now excluded by the supersede predicate, *and* the
superseding row is invisible to `run-a` because it lives in `run-b`.
Net result: `run-a`'s view of its own fact silently disappears.

## Context

Found during the deep-review pass on PR #450 (relationship + facts
recall filtering), finding M1. Reproduced empirically:

```
session run-a writes (bob, lives_in, "A") @ t=1000  → run-a recall: [A]
session run-b writes (bob, lives_in, "B") @ t=2000  → marks A as superseded
session run-a default recall                        → []   ← F-3 hole
```

The PR 3 test seeder
`tests/unit/python/test_facts_session_scope.py::_seed_three_session_facts`
deliberately uses **three distinct predicates** to sidestep this
interaction — the comment "the rows must use distinct predicates so
all three survive without superseding one another" documents the gap
implicitly. The PR description's claim that PR 3 "closes F-3 on the
relationship and facts surfaces" is technically true for the read
path only.

Pinned by
[`tests/unit/python/test_facts_session_scope.py::TestCrossSessionSupersedeIsDocumentedGap`](../../tests/unit/python/test_facts_session_scope.py)
as `pytest.mark.xfail(strict=True)` so the day the gap closes the
suite trips on `XPASS` and forces marker removal.

## Impact

A real F-3 carry-over symptom on the load-bearing facts surface
(MT-MEMORY-005 Legs 1 / 2 / 5):

* `run-a` writes a fact (`bob lives_in NYC`).
* The operator switches to `run-b` and the persona, under
  legitimate session-isolated operation, writes the same predicate
  with a different object (`bob lives_in LA` — different storyline
  in `run-b`).
* Switching back to `run-a` and asking "where does Bob live?" the
  persona sees **no live row** for `(bob, lives_in)` and falls back
  to the recall-empty branch — the dementia-test symptom on the
  facts tier, just inverted (instead of cross-session leak the
  persona forgets the active-session fact).

This is *worse* than the read-side leak in some respects: the leak
adds noise, but the supersede silently *destroys* the active
session's view. A run that writes a `(subject, predicate)` row in
another session retroactively contaminates the active session's
recall.

Currently undetected because the production `(session_id="run-b")`
write path is gated behind PR 4 (facade extension + persona-runtime
call-site threading). Today the production write path writes the
column-default `"legacy"` session id, so all writes share the
`legacy` carve-out and the cross-session supersede collapses to
within-session supersede. Once PR 4 ships and writes carry the
active session id, the gap goes from latent to live.

## Proposed fix / investigation path

The fix is straightforward in code but requires an RFC 0026 §F
semantics decision:

1. **§F amendment.** Decide whether symmetric latest-asserted-wins
   is:

   * **(A) per-session** — supersede keys
     `(agent_id, subject, predicate, session_id)`. Each session
     keeps its own "truth" about Bob's address. Symmetric with
     §D's read-side per-session predicate, and matches RFC 0031's
     "rerun's persona must not see prior storyline" intent. Likely
     correct, but a real semantics expansion of §F.
   * **(B) global with session join** — supersede stays global,
     but recall additionally filters out rows whose
     `superseded_by` points at a fact in a non-recall-eligible
     session. More complex, retains "one truth per agent."
   * **(C) keep current global semantics, document the gap** —
     declare that cross-session supersede is by design and the
     dementia-test surface must not write the same
     `(subject, predicate)` in multiple sessions. Unlikely.

   Recommend **(A)**. Aligns the write predicate with §D's read
   predicate; the production extractor (PR 2 path) already writes
   session-tagged rows.

2. **Implementation.** Add `session_id` to the supersede SELECT
   `WHERE` in `agents/memory/_facts_supersede.py`. The
   `apply_supersession` signature already receives the session via
   the row inserted in `FactStore.store`; thread it through.

3. **Remove the xfail marker** on
   `TestCrossSessionSupersedeIsDocumentedGap` once the §F amendment
   lands. Strict-xfail will flip the suite to `XPASS` failure as the
   forcing function.

4. **Decide the legacy carve-out's role under (A).** Mirror §D:
   `session_id="legacy"` is always eligible for supersede chain
   participation (a `run-a` write can supersede a `legacy` row
   because `legacy` is the pre-RFC carve-out and the active-session
   fact represents the persona's current knowledge). The reverse —
   a `legacy` write retroactively superseding a `run-a` row — is
   the dementia-test contamination path and should be rejected.
   Test pin should cover both directions.

5. **Update RFC 0026 §F** with the resolved semantics and a
   pointer to the test class once the marker is removed.

## Notes

> 2026-05-28 — initial capture during PR #450 deep review.
> Owned by **PR 5** (dementia-test bridge + review follow-ups) per
> [docs/rfcs/0031-phase2-pr-plan.md §PR 5](../rfcs/0031-phase2-pr-plan.md#pr-5-featurev035-rfc0031p2-dementia-bridge--dementia-test-bridge--review-follow-ups).
> If a future facade-extension PR (PR 4) starts producing
> session-tagged writes from the production path, severity escalates
> to **high** because the symptom becomes operator-visible on the
> dementia-test path.
