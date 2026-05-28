# RFC 0031 — PR Implementation Plan (Phase 2 — Recall Filtering + Dementia-Test Bridge)

**RFC**: [0031-per-session-namespacing-channels.md](0031-per-session-namespacing-channels.md)
**Status**: 📋 Ready — assigned to v0.3.5 ([v0.3.5-plan.md](../v0.3.5-plan.md) is the umbrella; Phase 1 of that plan executes this workstream)
**Created**: 2026-05-19
**Branch prefix**: `feature/v035-rfc0031p2-` *(assigned — v0.3.5, per [v0.3.5-plan.md §Phase 0](../v0.3.5-plan.md#phase-0--this-pr))*
**Target**: `main`
**Merge strategy**: Squash merge per [BRANCHING.md](../BRANCHING.md)
**Companion to**: [0031-pr-plan.md](0031-pr-plan.md) (Phase 1 — v0.3.1, shipped)

---

## Overview

RFC 0031 Phase 1 ([0031-pr-plan.md](0031-pr-plan.md)) shipped in v0.3.1: the `sessions` table, `session_id` columns on `channels` / `messages` / `episodes` / `relationships`, and `PERSATRIX_SESSION_ID` env-var threading on every **write** path. Phase 1 deliberately shipped **no recall filtering** — every row is tagged, but every read still surfaces every session's rows. **F-3 cross-run state bleed is therefore still open after Phase 1** ([RFC §Phased Implementation Plan Phase 1](0031-per-session-namespacing-channels.md#phase-1-sessions-table-column-additions-default-session-plumbing): *"F-3 closes when Phase 2's recall filtering lands"*).

**Phase 2 closes F-3.** Default recall becomes session-scoped per [RFC §D](0031-per-session-namespacing-channels.md#d-recall-semantics); cross-session recall lands as an explicit `sessions` parameter; the dementia test ([MT-MEMORY-005](../manual-tests/MT-MEMORY-005-dementia-test.md)) is updated to exercise multi-session continuity so the fix is provably compatible with the long-arc memory contract.

Phase 2 splits into **6 PRs**: one storage gap-closer, three recall-filtering PRs (episodic+notes, relationship+facts, facade+call-sites), one dementia-test-bridge + review-follow-ups PR, and a Phase 2 closeout.

### Scope correction surfaced during planning research

The [RFC §C storage-model table](0031-per-session-namespacing-channels.md#c-storage-model) — authored 2026-05-12 — lists `channels`, `messages`, `episodes`, `relationships`. Two persona-memory recall tiers feed the persona prompt and are **not** in that table:

- **`facts`** — the RFC 0026 declarative-facts tier (migration v8) **already carries `session_id`**: [`_migration_facts.py:75`](../../agents/memory/_migration_facts.py#L75) added the column following the RFC 0031 v7 convention, and [`facts.py`](../../agents/memory/facts.py) stamps it on every write. Facts needs **recall-side filtering only** — no migration, no write work.
- **`notes`** — the RFC 0008 notes tier ([`migrations.py:120`](../../agents/memory/migrations.py#L120), migration v2) **has no `session_id` column**. Phase 1's migration v7 ([`_migration_handlers.py:378`](../../agents/memory/_migration_handlers.py#L378)) scoped to `episodes` + `relationships` only. Notes are recalled into the persona prompt at [`memory_context.py`](../../agents/persona_runtime/memory_context.py), so a notes tier with no session dimension re-introduces F-3 on the notes surface even after episodes/relationships are filtered.

**PR 1 of this plan closes the `notes` gap** (column + index + write threading). The [RFC §C table is amended](#pr-6-featurev035-rfc0031p2-close--phase-2-closeout) in PR 6 to list all four persona-memory tiers (`episodes`, `relationships`, `facts`, `notes`) so the spec matches the tier set.

### Open-question status (carried from Phase 1)

[RFC §Decision/Next Steps](0031-per-session-namespacing-channels.md#decision--next-steps) names two open questions as gates before resuming Phases 2–4. Both are resolved by events; this plan records the resolutions.

- **[OQ #1](0031-per-session-namespacing-channels.md#open-questions) — default-recall semantics: resolution 1a** (single-session default). Already locked at Phase 1 plan-authoring time ([0031-pr-plan.md §Open-question resolutions](0031-pr-plan.md#open-question-resolutions-locked-at-plan-authoring-time)). It is **load-bearing in this phase** — §D's `sessions = [active_session_id]` default is the 1a shape. PR 5's dementia-test bridge is the acceptance proof that 1a does not break long-arc continuity.
- **[OQ #4](0031-per-session-namespacing-channels.md#open-questions) — sequencing against RFC 0029 Phase 1 facade.** RFC 0029 Phase 1 merged in v0.3.2 ([#370](https://github.com/mkhomutov/Persatrix/pull/370)–[#376](https://github.com/mkhomutov/Persatrix/pull/376)). The frozen `MemoryStore` facade carries `session_id` on **write** methods (`store_observation`, `store_procedure`, `publish_to_pool`) but **not on read** methods — RFC 0031 Phase 1 only added `session_id` to write APIs, so there was nothing for RFC 0029 to carry on the read side. OQ #4's cheap path ("facade has `session_id` on reads from day one") is therefore off the table; this plan takes OQ #4's explicitly-anticipated **back-compat-extension path** — PR 4 adds an optional `sessions` keyword to the frozen facade read signatures. The change is additive (a defaulted keyword-only parameter), but it amends a signature RFC 0029 declared frozen for v0.4.0. OQ #4 is resolved (facade-owner-confirmed at v0.3.5 planning); PR 4 records the amendment in [RFC 0029 §C](0029-personal-society-storage-split.md#c-memorystore-facade).
- **[OQ #7](0031-per-session-namespacing-channels.md#open-questions) — `session_id` as an OTEL trace attribute.** The `sessions.writes` counter shipped both sides in Phase 1 ([`agents/observability/metrics.py`](../../agents/observability/metrics.py), [`channel_instruments.go`](../../internal/observability/metrics/channel_instruments.go)) but `session_id` is on **no** span attribute today. PR 2 folds `session_id` into the existing `EPISODIC_RECALL_SPAN` attributes ([`episodic.py:304`](../../agents/memory/episodic.py#L304)) as a span attribute — `session_id` cardinality grows unbounded over a deployment's lifetime, which is acceptable on a trace span but never a metric label (OQ #7 resolved — observability-reviewer-confirmed at v0.3.5 planning).

---

## Prerequisites

Phase 2 implementation was gated on a maintainer sequencing call. That call is now made: **RFC 0031 Phases 2–4 are committed to v0.3.5** ([v0.3.x-sequencing.md §Amendment 2026-05-23](../v0.3.x-sequencing.md#amendment-2026-05-23--v034-carries-rfc-0033-ahead-of-rfc-0031-phases-24) — v0.3.5 = Session Isolation), with [v0.3.5-plan.md](../v0.3.5-plan.md) as the umbrella. The per-version one-story contract holds — v0.3.5's story is *"every run is auto-isolated; persona memory no longer bleeds across runs."*

The three prerequisites below are satisfied as of the v0.3.5 Phase 0 PR:

1. Done — [v0.3.x-sequencing.md](../v0.3.x-sequencing.md) amended: the `RFC 0031 Phases 2–4` row is committed to v0.3.5 (Amendment 2026-05-23).
2. Done — patch master plan opened ([`docs/v0.3.5-plan.md`](../v0.3.5-plan.md)); this Phase 2 PR plan is its Phase 1 implementation workstream.
3. Done — branch prefix swept to `feature/v035-rfc0031p2-` across all six PR headings, the [Progress Overview](#progress-overview-phase-2) table, and the PR 6 anchor link.

This plan stands as the implementation detail regardless of which patch absorbed it; the version label and branch prefix resolved to v0.3.5.

---

## Dependency Graph

```
PR 1 (notes-tier session coverage: migration v9 column+index + store_note write threading)
  ↓
PR 2 (episodic + notes recall filtering; active-session resolution on the tiers; OTEL span attr)
  ↓
PR 3 (relationship + facts recall filtering)
  ↓
PR 4 (MemoryStore facade read-path sessions= extension + persona_runtime call-site threading)
  ↓
PR 5 (dementia-test bridge: MT-MEMORY-005 multi-session + cross-process recall integration test; review follow-ups)
  ↓
PR 6 (Phase 2 closeout — RFC §C table amendment, status, ROADMAP)
```

PR 1 must precede PR 2 — recall filtering on a `notes.session_id` column that does not exist fails. PR 2 → PR 3 is recommended review order (the recall-filter predicate shape is identical across tiers; PR 2 establishes it, PR 3 reuses it). PR 4 depends on PRs 2 + 3 — the facade delegates to the tier reads those PRs change. PR 5 depends on PR 4 — the dementia-test bridge exercises the full persona recall path through the facade.

---

## PR Sequence

### PR 1: `feature/v035-rfc0031p2-notes-coverage` — Notes-Tier Session Coverage

**Depends on**: Nothing (v0.3.3 baseline; RFC 0031 Phase 1 + RFC 0026 + RFC 0029 Phase 1 all merged).
**Purpose**: Bring the `notes` tier to `session_id` parity with `episodes` / `relationships` / `facts` so Phase 2 recall filtering has a non-degenerate column to filter on. Pure write-path + migration — mirrors Phase 1 PR 3.

#### Scope

| File | Change |
|------|--------|
| [`agents/memory/_migration_handlers.py`](../../agents/memory/_migration_handlers.py) (or a split `_migration_notes_session.py` if the parent trips the 500-line cap) | New handler `_apply_migration_9`: `ALTER TABLE notes ADD COLUMN session_id TEXT NOT NULL DEFAULT 'legacy'`; `CREATE INDEX IF NOT EXISTS idx_notes_session ON notes(session_id)`. `sqlite_master` guard + idempotent replay path, mirroring the v7/v8 handler pattern. |
| [`agents/memory/migrations.py`](../../agents/memory/migrations.py) | Register migration v9 in the umbrella runner; bump latest schema version 8 → 9. |
| [`agents/memory/notes.py`](../../agents/memory/notes.py) | `NoteStore.store_note` (write path) accepts `session_id: str = "legacy"`, persists it on INSERT. |
| [`agents/memory/episodic_notes_api.py`](../../agents/memory/episodic_notes_api.py) | `_EpisodicNotesAPIMixin.store_note` delegation accepts + forwards `session_id`. |
| Notes write call sites (`agents/persona_runtime/`, `agents/memory/store.py` if `store_note` is facade-exposed) | Thread the resolved per-process `session_id` through, matching the Phase 1 episode/relationship plumbing. |
| `tests/unit/python/test_session_id_notes_migration.py` | **New** — fresh + upgrade migration paths; idempotence; no-op early-return on missing `notes` table. |
| `tests/unit/python/test_session_id_writes.py` | Extend Phase 1's write-path round-trip suite with `notes` cases. |

#### Key implementation details

- The migration handler version is assigned at PR-author time; the umbrella `schema_version` table linearises against any parallel RFC's handler (collision surfaces as a CI failure on `make test`) — same discipline as Phase 1 PR 3.
- `notes` already carries an FTS5 mirror + sync triggers (migration v2). The `ADD COLUMN` is a plain table-column add; it does not touch the FTS virtual table — `idx_notes_session` is a B-tree index on the base table only. Confirm the v2 sync triggers do not need a `session_id` projection (notes FTS indexes `topic` / `content`, not `session_id` — no trigger change expected).
- No recall changes in PR 1 — `recall_notes` still returns every session's rows. Phase 2's filter lands in PR 2.

#### Tests

- Migration v9 on a fresh DB: column + `idx_notes_session` present.
- Migration v9 on a v8 fixture DB (`notes` rows present): existing rows default to `'legacy'`, no backfill UPDATE.
- Idempotence: running the migration twice produces no diff.
- `store_note(session_id="run-a")` round-trips; default `"legacy"` on omission.

#### PR checklist

- [ ] `make test` passes; `make lint` clean.
- [ ] Migration tested against a legacy DB fixture frozen at schema v8.
- [ ] All four persona-memory tiers (`episodes`, `relationships`, `facts`, `notes`) now carry `session_id` on every write path.
- [ ] [RFC 0031 row in ROADMAP](../../ROADMAP.md#rfc-master-index) → `🚧 Implementing` on this PR opening.
- [ ] [RFC 0031 file](0031-per-session-namespacing-channels.md) `status:` frontmatter (`partially_implemented` → `implementing`) **and** the `**Status**:` heading line → `🚧 Implementing` on this PR opening. Per [Status Hygiene Rule 1](../development-workflow.md#status-hygiene), the flip must happen **in both the RFC file and ROADMAP** — regenerate [INDEX.md](INDEX.md) via `make rfcs` to pick up the frontmatter change. (PR 1 review F15 — the original single-line checklist omitted the RFC file and the flip was missed at PR-opening; this paired item closes the systemic gap, not just the one-off.)

#### PR 1 review follow-ups carried forward

Findings deferred from the PR 1 deep review because they exceed PR 1's "pure write-path + migration — no recall-side filtering" scope. Each is recorded inline in the affected downstream PR so a future contributor cannot miss it:

- **F1** — `NoteStore._prune_notes` is session-blind (deletion bleed across sessions). Carried into [PR 2 scope row for `agents/memory/notes.py`](#pr-2-featurev035-rfc0031p2-episodic-recall--episodic--notes-recall-filtering) — decision: scope prune to `(agent_id, session_id)` for parity with the recall filter.
- **F6** — `Note` dataclass + `_NOTE_COLS` projection omit `session_id` while the table now carries it (PR 1's intentional INSERT/SELECT asymmetry). Carried into the same PR 2 row — extend both in lockstep with the recall predicate; the PR 1 contract pin at `tests/unit/python/test_session_id_notes_migration.py::TestNotesProjectionContract` will fail until the projection tuple is updated alongside the dataclass field count.
- **F8** — `_EpisodicNotesAPIMixin.store_note` defaults `session_id` to the literal `LEGACY_SESSION_ID` rather than falling back to a facade-resolved value, asymmetric with `MemoryStore.store_observation` at [`store.py:397`](../../agents/memory/store.py#L397). Carried into [PR 4 scope row for `agents/memory/store.py`](#pr-4-featurev035-rfc0031p2-facade-callsites--facade-read-path-extension--call-site-threading) — apply the same `_session_id` fallback discipline if `MemoryStore.store_note` is promoted to a facade method.
- **F12** — `idx_notes_session` (and the v7/v8 siblings) is single-column on `session_id` only; under the dominant `'legacy'` carve-out the planner will reject it for `WHERE agent_id=? AND session_id=?`. Carried into [PR 2 Key implementation details — index shape re-evaluation](#key-implementation-details-1) — default to a v10 migration creating composite `idx_{tier}_agent_session(agent_id, session_id)` indexes unless measurement shows the single-column shape suffices. **PR 449 chose path (b) (measurement-mode); a follow-on measurement pass should also cover total-notes-per-agent growth across sessions** — `NoteStore._prune_notes` is now per-session (PR 1 F1 fix landed in PR 449), so a long-lived agent operating across N sessions accumulates up to N × `max_notes` rows in aggregate with no metric or log surfacing the growth. Fold a total-rows-per-agent gauge / log line into the same observability sweep that re-evaluates the index shape.
- **F16** — `NoteStore.store_note` normalises empty / whitespace-only `session_id` → `LEGACY_SESSION_ID` at the storage boundary (PR 1 F4), but the sibling primitives — `EpisodicMemory.store_episode` ([`episodic.py:195`](../../agents/memory/episodic.py#L195)), `RelationshipMemory.record_interaction` (via [`relationship_mutations.py`](../../agents/memory/relationship_mutations.py)), `FactStore.store` — do not. The orphan-row exposure on those tiers is lower because every production caller reaches them through [`MemoryStore`](../../agents/memory/store.py#L397) (which forwards `session_id if session_id is not None else self._session_id`, and `self._session_id` came from `resolve_session_id_silent()` so is already non-empty), but a direct programmatic caller or test fixture passing `session_id=""` would still persist a row that escapes both real-session and legacy-carve-out filters once recall lands in PR 2 / PR 3. Carried into [PR 4 scope row for `agents/memory/store.py`](#pr-4-featurev035-rfc0031p2-facade-callsites--facade-read-path-extension--call-site-threading) — when PR 4 sweeps the facade call sites, also extract the normalisation into a small `agents.session_id.normalize_session_id()` helper and apply it at all four storage-primitive boundaries so the contract is uniform; add round-trip pins for empty / whitespace input on each tier mirroring `TestStoreNoteSessionIDNormalization`. (PR 1 second deep-review finding #1 — the F4 fix was correct on notes but introduced an asymmetric invariant: notes is stricter than its siblings without a documented reason beyond "F4 fired on notes." Symmetry beats per-tier discretion.)
- **F17** — `relationship_mutations.record_interaction` at [`relationship_mutations.py:277-286`](../../agents/memory/relationship_mutations.py#L277) calls `inst.sessions_writes.add(...)` **without** wrapping it in `contextlib.suppress(Exception)`, while `EpisodicMemory.store_episode` ([`episodic.py:251`](../../agents/memory/episodic.py#L251)) and `NoteStore.store_note` ([`notes.py:176`](../../agents/memory/notes.py#L176)) do. An OTEL backend exception there propagates to the caller **after** `db.commit()` has already persisted the row, so the caller sees a write failure on a write that actually succeeded — exactly the failure-isolation regression PR #337 M1 closed for `store_episode`. Pre-existing; not introduced by RFC 0031 PR 1 but newly surfaced because PR 1's notes-tier emit explicitly cites the "matching the failure-isolation contract" goal. Carried into [PR 3 scope row for `agents/memory/relationship_mutations.py`](#pr-3-featurev035-rfc0031p2-relationship-facts-recall--relationship--facts-recall-filtering) since PR 3 touches that file; add a regression test mirroring `tests/unit/python/test_session_id_metric_failure_isolation.py` (the episode-tier pin) for `record_interaction`. (PR 1 second deep-review finding #2.)

---

### PR 2: `feature/v035-rfc0031p2-episodic-recall` — Episodic + Notes Recall Filtering

**Depends on**: PR 1 merged.
**Purpose**: Make episodic and notes recall session-scoped per [RFC §D](0031-per-session-namespacing-channels.md#d-recall-semantics). Establish the `sessions` parameter shape and the active-session resolution that PRs 3–4 reuse.

#### Scope

| File | Change |
|------|--------|
| [`agents/memory/episodic.py`](../../agents/memory/episodic.py#L267) | `EpisodicMemory.recall` gains `sessions: list[str] | str | None = None` (keyword-only). `__init__` resolves `_active_session_id` once via `resolve_session_id_silent()` ([`agents/session_id.py`](../../agents/session_id.py) — the leaf module the facade already uses). Add `session_id` to the `EPISODIC_RECALL_SPAN` attributes (OQ #7). |
| [`agents/memory/episodic_queries.py`](../../agents/memory/episodic_queries.py) | `recall_fts5` (L188), `recall_like` (L237), `recall_recency` (L273) accept a resolved session-filter argument and append `AND (e.session_id IN (…) OR e.session_id = 'legacy')` to the WHERE clause. The `"*"` mode drops the predicate entirely. |
| [`agents/memory/notes.py`](../../agents/memory/notes.py#L144) | `NoteStore.recall_notes` (L144) gains the same `sessions` parameter; `_recall_notes_fts5` / `_recall_notes_like` / `_recall_notes_recency` add the predicate. **PR 1 review F6 carry-forward**: extend the `Note` dataclass + `_NOTE_COLS` projection tuple with `session_id` in the same change — the PR 1 contract pin at `tests/unit/python/test_session_id_notes_migration.py::TestNotesProjectionContract` will fail until both sides move together; update its expected `_NOTE_COLS` tuple and the dataclass field count in lockstep. **PR 1 review F1 carry-forward**: decide whether `NoteStore._prune_notes` (L251–L268) becomes session-scoped — today it filters by `agent_id` only, so a write tagged `session_id="run-b"` that trips `max_notes` can delete the oldest `session_id="run-a"` row on the same agent (write-side isolation is one-way; lifecycle path still bleeds). Recommendation: scope prune to `(agent_id, session_id)` for parity with the recall filter, accepting that session B can no longer evict session A's stale notes — record the decision in PR 2's `key implementation details`. |
| [`agents/memory/episodic_notes_api.py`](../../agents/memory/episodic_notes_api.py#L51) | `recall_notes` delegation forwards `sessions`. |
| [`agents/memory/scope_recall.py`](../../agents/memory/scope_recall.py#L42) | `recall_with_scope_filter` gains a `sessions` passthrough to `episodic.recall`. Orthogonal to the RFC 0020 §G `scope` filter per [RFC §F](0031-per-session-namespacing-channels.md#f-interaction-with-rfc-0020-g-scope) — separate predicate, separate index; no `scope`-prefix widening. |
| `tests/unit/python/test_episodic_session_scope.py` | **New** — default recall (`sessions=None`) returns active-session + `legacy` rows; explicit `sessions=[a,b]` list; `sessions="*"` returns everything; empty list raises `ValueError` per §D. Mirror cases for `recall_notes`. |

#### Key implementation details

- **Three modes per [RFC §D](0031-per-session-namespacing-channels.md#d-recall-semantics)**: `None` → `[_active_session_id]` + `legacy` carve-out; explicit list → that list + `legacy`; `"*"` → no IN-clause. Empty list raises `ValueError("sessions must be None, '*', or a non-empty list")` — the §D guard against the silent legacy-only collapse.
- **The `legacy` carve-out** (`session_id = 'legacy'` always visible in modes 1–2) is the load-bearing detail that ships Phase 2 with **no backfill** of pre-RFC rows. In mode 3 (`"*"`) the carve-out is a no-op.
- **Active-session ownership.** §D's pseudocode reads `self._active_session_id`. The tier resolves it from `PERSATRIX_SESSION_ID` at construction — *not* only the facade — because the persona prompt-assembly path reads `EpisodicMemory.recall` **directly, bypassing the facade** (the explicit comment at [`episodic.py:335-344`](../../agents/memory/episodic.py#L335)). Resolving on the tier makes `sessions=None` correct on both the facade path and the persona-direct path. The facade passes its own `_session_id` explicitly when it calls down (PR 4) — defence in depth, not the only line.
- The empty-query recency path (`recall_recency`) must filter too — [`channel_history.py`](../../agents/persona_runtime/channel_history.py) calls recall with an empty query.
- **PR 1 review F12 carry-forward — index shape re-evaluation.** Migrations v7 / v8 / v9 each shipped a *single-column* `idx_{tier}_session(session_id)` index. Under the dominant `session_id='legacy'` carve-out (pre-RFC rows + operator-default writes), that column's selectivity is near-zero, so SQLite's planner will pick the existing `agent_id` index and post-filter `session_id` in memory — making the dedicated index nearly useless on `WHERE agent_id=? AND session_id=?`. v3's `idx_interactions_lookup(agent_id, other_agent_id, created_at DESC)` is the project's own composite-covering-index precedent. PR 2 must either (a) add a v10 migration creating composite `idx_{tier}_agent_session(agent_id, session_id)` indexes on the four tiers and dropping the single-column variants, or (b) measure the planner's choice on a representative DB and record that the single-column shape is acceptable in the PR description. Default to (a) unless the measurement shows the planner already does the composite scan via index intersection.

#### Tests

- `sessions=None` against a DB with `run-a` + `run-b` + `legacy` rows under `_active_session_id="run-a"` → returns `run-a` ∪ `legacy`, never `run-b`.
- `sessions=["run-a","run-b"]` → returns both, plus `legacy`.
- `sessions="*"` → returns all three.
- `sessions=[]` → `ValueError`.
- FTS5, LIKE, and recency paths each exercised under all three modes.
- `EPISODIC_RECALL_SPAN` carries a `session_id` attribute.

#### PR checklist

- [ ] `make test` passes; `make lint` clean.
- [ ] All three recall paths (FTS5 / LIKE / recency) filter by session.
- [ ] `legacy` rows visible from every session in modes 1–2.
- [ ] Observability reviewer signed off on the `session_id` span attribute (OQ #7) in the PR thread.

---

### PR 3: `feature/v035-rfc0031p2-relationship-facts-recall` — Relationship + Facts Recall Filtering

**Depends on**: PR 2 merged (recommended — reuses the §D predicate shape).
**Purpose**: Apply the same session-scoped recall to the relationship and facts tiers.

#### Scope

| File | Change |
|------|--------|
| [`agents/memory/relationship.py`](../../agents/memory/relationship.py) | `get_trust` (L133), `get_relationship_summary` (L231) gain `sessions`; `_active_session_id` resolved in `__init__` as in PR 2. |
| [`agents/memory/relationship_queries.py`](../../agents/memory/relationship_queries.py) | `get_all_relationships` (L181) and the per-method WHERE clauses gain the `session_id IN (…) OR session_id = 'legacy'` predicate. |
| [`agents/memory/facts.py`](../../agents/memory/facts.py) | The `FactStore` recall path (the RFC 0026 §D `WHERE agent_id=? AND subject=?` query) gains `sessions` and the predicate. `facts.session_id` already exists ([migration v8](../../agents/memory/_migration_facts.py#L75)) — recall-only change. |
| `tests/unit/python/test_relationship_session_scope.py`, `tests/unit/python/test_facts_session_scope.py` | **New** — default / explicit-list / `"*"` modes; `legacy` carve-out; empty-list `ValueError`. |

#### Key implementation details

- The relationship first-seen contract (Phase 1: stamp on INSERT, preserve on UPDATE — [MT-SESSION-001 Step 7](../manual-tests/MT-SESSION-001.md)) is unchanged; PR 3 is recall-side only.
- Facts recall feeds the **primary dementia-test surface** ([MT-MEMORY-005](../manual-tests/MT-MEMORY-005-dementia-test.md) Legs 1/2/5). The `legacy` carve-out means pre-RFC fact rows stay visible — a persona upgraded into v0.3.5 does not "forget" facts asserted before sessions existed.
- Reuse the exact predicate helper from PR 2 (extract it to a shared `agents/memory/_session_filter.py` if PR 2 did not already) so the four tiers cannot drift.

#### Tests

- Relationship + facts recall exercised under all three §D modes.
- `legacy` fact / relationship rows visible from every session.
- Predicate-helper unit test pins the SQL fragment shape once.

#### PR checklist

- [ ] `make test` passes; `make lint` clean.
- [ ] All four persona-memory tiers filter recall by session.
- [ ] Shared `_session_filter` helper — one predicate definition, four call sites.

---

### PR 4: `feature/v035-rfc0031p2-facade-callsites` — Facade Read-Path Extension + Call-Site Threading

**Depends on**: PRs 2 + 3 merged.
**Purpose**: Extend the frozen RFC 0029 `MemoryStore` facade read methods with the `sessions` parameter (OQ #4 back-compat extension) and route the persona-runtime recall call sites through it.

#### Scope

| File | Change |
|------|--------|
| [`agents/memory/store.py`](../../agents/memory/store.py) | `retrieve_relevant` (L256) gains `sessions: list[str] | str | None = None`, forwarded to `EpisodicMemory.recall` / `recall_notes` / `scope_recall`. Default `None` → the facade's own `_session_id` ([`store.py:160`](../../agents/memory/store.py#L160)). **PR 1 review F8 carry-forward**: if PR 4 promotes `store_note` to a facade method (mirroring `store_observation` at [`store.py:340`](../../agents/memory/store.py#L340)), use the same `session_id=session_id if session_id is not None else self._session_id` fallback discipline — the current mixin default at `agents/memory/episodic_notes_api.py:43` is the literal `LEGACY_SESSION_ID` constant, with no facade-aware fallback, so a naive `MemoryStore.store_note` wrapper that omits the fallback would silently write `'legacy'` even when `_session_id` resolved to `'run-a'`. Pin the contract in `tests/unit/python/test_session_id_writes.py`. |
| [`agents/memory/facade_procedural.py`](../../agents/memory/facade_procedural.py) | `retrieve_procedures` (L186) gains `sessions`. |
| [`agents/memory/shared_pool_facade.py`](../../agents/memory/shared_pool_facade.py) | `read_from_pool` (L141) gains `sessions` — **PR 449 deep-review M1 carry-forward ([`ISSUE-0078`](../issues/ISSUE-0078-shared-pool-read-session-filter-policy.md))**: the threading is three layers deep (`read_from_pool` → `read_via_facade` → `pool.read` → `pool._episodic.recall`), not just the outermost facade method. [`SharedMemoryPool.read`](../../agents/memory/shared_pool.py#L221) currently calls `self._episodic.recall` with no `sessions=` kwarg, so the pool's read view silently collapses to the pool's *init-time* `_active_session_id` + `legacy`; a row written under a different session (e.g., pool constructed in `legacy`, then a facade-routed write tagged `session_id='run-a'`) is invisible to every reader. Decide the default-mode policy for shared pools when `sessions=None`: **A** — default to `"*"` on the underlying recall (cross-session by RFC 0008 §H design), or **B** — default to the caller's facade `_session_id`. Recommend A; whichever PR 4 picks, document in the PR description and pin with the read-side mirror tests sketched in ISSUE-0078 (parallel to `tests/unit/python/test_session_id_facade_surfaces.py::TestPublishViaFacadeSessionID`). |
| [`agents/memory/facade.py`](../../agents/memory/facade.py) | The `MemoryFacade` legacy `retrieve_relevant` shim forwards `sessions` (defaulted) so a missed external caller keeps working. |
| [`agents/persona_runtime/memory_context.py`](../../agents/persona_runtime/memory_context.py) | The episodic recall call (L307) and notes recall call (L331) pass `sessions=None` explicitly (documents intent; default already correct). |
| [`agents/persona_runtime/channel_history.py`](../../agents/persona_runtime/channel_history.py) | The `recall_with_scope_filter` call (L131) passes `sessions=None`. |
| [`docs/rfcs/0029-personal-society-storage-split.md`](0029-personal-society-storage-split.md) | **Amendment block**: "RFC 0031 Phase 2 adds an optional `sessions` keyword to the frozen `MemoryStore` read signatures (`retrieve_relevant`, `retrieve_procedures`, `read_from_pool`) — additive, OQ #4 back-compat path." Coordinate with the RFC 0029 author before merge. |
| `tests/integration/test_session_recall_isolation.py` | **New** — facade-level: a persona constructed under `PERSATRIX_SESSION_ID=run-b` does not recall `run-a` episodes / notes / relationships / facts; `legacy` rows visible; `sessions="*"` (debug path) surfaces all. |
| `tests/unit/python/test_session_recall_default_path.py` | **New** — per [RFC §Security Considerations](0031-per-session-namespacing-channels.md#security-considerations): assert `_active_session_id` / `_session_id` is consulted at every recall site in `agents/persona_runtime/` and the `"*"` sentinel is **not** reachable from the default persona-runtime context path. |

#### Key implementation details

- **OQ #4 back-compat extension.** Adding a defaulted keyword-only parameter is additive — no existing caller breaks — but it amends a signature RFC 0029 declared frozen for the v0.4.0 Postgres split. The amendment block + RFC 0029-author coordination is the contract that this is a *known, recorded* extension, not signature drift.
- **`"*"` is a footgun** ([RFC §Security Considerations](0031-per-session-namespacing-channels.md#security-considerations)): a debug path that defaults to all-sessions and is wired into a prompt context re-introduces F-3. PR 4's `test_session_recall_default_path.py` pins that the persona-runtime context path never reaches `"*"`. The `"*"` sentinel is CLI/debug-only (Phase 3 surfaces it via `persatrix memory recall --all-sessions`).
- After PR 4 the **full persona recall path is session-scoped end to end** — F-3 is closed. PR 5 proves it.

#### Tests

- Cross-session isolation at the facade layer for all four tiers.
- `legacy` visibility preserved through the facade.
- The persona-runtime default context path never passes `sessions="*"`.

#### PR checklist

- [ ] `make test` passes; `make lint` clean; `mypy agents/` clean (frozen-signature change is type-checked).
- [ ] RFC 0029 author signed off on the facade amendment in the PR thread.
- [ ] F-3 reproduction (rerun with same channel name + `--user` under a new `PERSATRIX_SESSION_ID`) shows no carryover — manual check noted in the PR description.

---

### PR 5: `feature/v035-rfc0031p2-dementia-bridge` — Dementia-Test Bridge + Review Follow-Ups

**Depends on**: PR 4 merged.
**Purpose**: Update [MT-MEMORY-005](../manual-tests/MT-MEMORY-005-dementia-test.md) to exercise multi-session continuity explicitly — the [RFC §Phase 2 deliverable #4](0031-per-session-namespacing-channels.md#phase-2-recall-filtering-and-the-dementia-test-bridge) — and the [RFC §Test Strategy E2E pin](0031-per-session-namespacing-channels.md#test-strategy). Absorb PR 1–4 review findings.

#### Scope

| File | Change |
|------|--------|
| [`docs/manual-tests/MT-MEMORY-005-dementia-test.md`](../manual-tests/MT-MEMORY-005-dementia-test.md) | New Setup step: export `PERSATRIX_SESSION_ID=<arc-id>` once and reuse it across all five interactions — per [OQ #1 resolution 1a](0031-per-session-namespacing-channels.md#open-questions), default single-session recall **is** the dementia-test recall path because the arc shares one session id. New leg / variant note: a second arc under a different `PERSATRIX_SESSION_ID` must **not** surface the first arc's facts unless `sessions="*"` is passed. New Test-Results row for the v0.3.5 run. |
| `tests/integration/test_session_continuity.py` | **New** — the [RFC §Test Strategy E2E](0031-per-session-namespacing-channels.md#test-strategy) pin: one session spanning a simulated multi-day arc → recall stays inside the arc; a second session → no bleed; explicit `sessions=[arc1, arc2]` → continuity across both. This is the canonical "fixed F-3 without breaking the dementia test" regression test. |
| [`docs/manual-tests/MT-SESSION-001.md`](../manual-tests/MT-SESSION-001.md) | Edge Case 2 ("Phase 2 recall semantics") updated from "will still surface the prior session's rows" to the shipped Phase 2 behaviour. |
| Review-follow-up subsections | "From PR N review" entries per the [RFC 0017 PR plan §PR 6 precedent](0017-pr-plan.md) — each finding paraphrased inline, no link to local review reports per [.github/copilot-instructions.md](../../.github/copilot-instructions.md). |

#### Key implementation details

- The dementia-test bridge is where [OQ #1 resolution 1a](0031-per-session-namespacing-channels.md#open-questions) is *proven*, not just asserted: single-session default recall must reproduce the long-arc continuity the dementia test demands. If the arc shares one session id, it does. The MT update makes that operator contract explicit.
- `test_session_continuity.py` is the gate that a future plan author cannot regress — it pins both halves of the RFC's goal: isolation by default **and** continuity within a session.
- **PR 449 deep-review carry-forwards** (Episodic + Notes recall filtering):
  - [`ISSUE-0077`](../issues/ISSUE-0077-notes-mutation-not-session-scoped.md) — `NoteStore.update_note` / `delete_note` / `count_notes` remain agent-scoped (the recall surface is §D-filtered but the mutation surface is not). Defence-in-depth gap, not an active exploit at the current LLM-prompt surface. PR 5 should either close it (preferred — symmetric with PR 4's facade read-path extension) or downgrade it to a tracked issue with rationale per the PR checklist.
  - [`ISSUE-0078`](../issues/ISSUE-0078-shared-pool-read-session-filter-policy.md) — `SharedMemoryPool.read` silently inherits the new tier-default session filter via the pool's own `_episodic.recall` call, breaking RFC 0008 §H cross-agent / cross-session sharing for any write whose `session_id` differs from the pool's init-time `_active_session_id`. **Owned by PR 4** (the read-chain threading scope row above); listed here so PR 5's review-follow-ups sweep cross-references it. If PR 4 ships before PR 5 lands, mark this carry-forward resolved in PR 5's PR description.
  - **Notes recall observability symmetry** — `EpisodicMemory.recall` sets `session_id` as an OTEL attribute on `EPISODIC_RECALL_SPAN` ([`episodic.py:329`](../../agents/memory/episodic.py#L329); RFC §OQ #7), but `NoteStore.recall_notes` emits no recall span at all and therefore no equivalent attribute. Tolerable today (notes recall is not on the latency-dashboard critical path), but once notes recall earns a span the `session_id` attribute should land alongside it for parity with the episodic tier. Fold into PR 5's review-follow-up sweep — no separate issue file, captured here.
  - **Per-session capacity observability** — see F12 carry-forward extension above. `_prune_notes(agent_id, session_id)` is now load-bearing for write-side isolation but introduces unbounded total-notes growth across sessions. Fold the gauge into the same measurement-mode pass that revisits the index shape.
- **PR 450 deep-review carry-forwards** (Relationship + Facts recall filtering):
  - [`ISSUE-0079`](../issues/ISSUE-0079-cross-session-supersede-not-scoped.md) — `_facts_supersede.apply_supersession` keys symmetric latest-asserted-wins on `(agent_id, subject, predicate)` with no `session_id` predicate; a `FactStore.store(session_id="run-b")` with a later `asserted_at` than an existing `run-a` row writes `superseded_by` onto the `run-a` row, removing it from `run-a`'s default recall (which filters both `superseded_by IS NULL` *and* `session_id IN ("run-a", "legacy")`). Write-side F-3 hole on the facts surface. Pinned by [`tests/unit/python/test_facts_session_scope.py::TestCrossSessionSupersedeIsDocumentedGap`](../../tests/unit/python/test_facts_session_scope.py) as strict-`xfail`. Latent today (production writes still column-default `"legacy"`) but goes live the moment PR 4 wires the active session id into the production write path. The fix is an **RFC 0026 §F amendment** — supersede semantics must align with §D's per-session predicate. PR 5 should land the §F amendment + the supersede-side filter, and remove the xfail marker.
  - [`ISSUE-0080`](../issues/ISSUE-0080-relationship-recent-interactions-cross-session-leak.md) — `RelationshipMemory.get_relationship_summary` filters the `relationships` row by `session_id` (PR 3) but the secondary fetch into the `interactions` table is un-scoped, and the `interactions` table has **no `session_id` column at all** (migration v7 added the column to `episodes` and `relationships` only). When the relationship row IS visible to the active session, `recent_interactions` returns every cross-session interaction for that peer, `interaction_count` is the global running total (via `record_interaction`'s ON-CONFLICT increment on the original first-seen row), and `first_interaction_at` (via `MIN(created_at)`) reflects the very first interaction in any session. F-3 read-side leak on the load-bearing prompt-injection surface — `get_relationship_summary` feeds the persona's LLM context directly. Pinned by [`tests/unit/python/test_relationship_session_scope.py::TestRecentInteractionsCrossSessionLeakIsDocumentedGap`](../../tests/unit/python/test_relationship_session_scope.py) as strict-`xfail`. Fix needs **migration v10** adding `session_id` to `interactions` + a §D filter on both the recent-interactions SELECT and the `MIN(created_at)` SELECT, plus a policy decision on `interaction_count` (per-session derived count vs. global column kept). PR 5 should land the migration + recall-side filter, and remove the xfail marker.
  - **`update_trust` write path tagged `legacy` by column default** — `agents/memory/relationship_mutations.py:89-113` does `INSERT INTO relationships (...)` with no `session_id` column in the column list, so the migration v7 column default `'legacy'` kicks in for first-seen pairs. Asymmetric with `record_interaction` and `seed_trust`, which both thread `session_id`. **Not** filed as an issue — `update_trust` has zero production callers today (only test code calls it), and the column-default `'legacy'` is semantically defensible for "trust bump with no associated interaction in any session" (the carve-out makes it visible to every session, which is appropriate when no session context attached). If a future PR wires a production `update_trust` caller, add a `session_id: str = "legacy"` kwarg there and thread it into the INSERT branch — at that point the gap becomes worth filing.
- **PR 451 deep-review carry-forwards** (Facade read-path extension + call-site threading):
  - **M1 — Default-session-resolution lives in two places.** `MemoryStore.retrieve_relevant` resolves `sessions=None` to `[self._session_id]` (facade snapshot) while `channel_history.recall_channel_episodes` passes `sessions=None` straight to the tier, which resolves against `self._active_session_id` (tier snapshot). The two snapshots are env-resolved at near-identical times in production and never diverge, but the duplicated logic is a smell: a future refactor that changes the resolution rule in one place can silently miss the other. **Not** filed as an issue — no observable bug. Fold into PR 5's review-follow-up sweep: either thread `self._session_id` through the tier-direct paths so the facade is the single source of truth, or drop the facade-side resolution and rely on the tier — pick one and document it.
  - **L5 — Runtime "never sees star" pin is synthetic.** `test_session_recall_default_path.py::TestPersonaRuntimeCallSitesDoNotPassAllSentinel::test_episodic_recall_default_path_never_sees_star` calls `EpisodicMemory.recall` directly with kwargs that *resemble* what `_inject_memory_context` passes, rather than invoking `_inject_memory_context` itself. A future edit to the prompt-assembly pipeline that wires `sessions="*"` into the mixin won't break this test if it bypasses the spied recall. Fold into PR 5: with the dementia-test bridge already wiring the full persona pipeline, add a spy on `EpisodicMemory.recall` and drive a real `_inject_memory_context` call so the assertion is against the actual call site, not a stand-in. The source-level scan above remains as the cheap defence.
  - **ISSUE-0078 status update** — closed by PR 4's M2 fix. `SharedMemoryPool.read` now defaults `sessions="*"` at the data layer (was: silent narrowing to the pool's `_active_session_id`); `MemoryStore.read_from_pool` and `read_via_facade` are pure pass-throughs. Mark ISSUE-0078 resolved in PR 5's PR description.

#### PR checklist

- [ ] `make test` passes; `make lint` clean.
- [ ] MT-MEMORY-005 carries a multi-session continuity step and a v0.3.5 Test-Results row.
- [ ] MT-SESSION-001 Edge Case 2 reflects shipped Phase 2 behaviour.
- [ ] All PR 1–4 review findings addressed or downgraded to tracked issues with rationale.

---

### PR 6: `feature/v035-rfc0031p2-close` — Phase 2 Closeout

**Depends on**: PR 5 merged.
**Purpose**: Mark Phase 2 implemented; amend the RFC §C storage table; hand off to Phase 3 (operator CLI).

#### Scope

| File | Change |
|------|--------|
| [`docs/rfcs/0031-per-session-namespacing-channels.md`](0031-per-session-namespacing-channels.md) | [§C storage-model table](0031-per-session-namespacing-channels.md#c-storage-model) amended to list all four persona-memory tiers (`episodes`, `relationships`, `facts`, `notes`) with their migration versions (v7 / v7 / v8 / v9). Status note: "Phase 2 implemented in v0.3.5." [§Decision/Next Steps](0031-per-session-namespacing-channels.md#decision--next-steps) updated — OQ #4 / OQ #7 resolutions recorded; remaining work is Phases 3–4. |
| [`ROADMAP.md`](../../ROADMAP.md) | RFC 0031 row stays `⚠️ Partially Implemented`; target line updated to `v0.3.1 (P1) + v0.3.5 (P2) + v0.3.x (P3–4)`. `Last updated` refresh. |
| [`docs/rfcs/0031-phase2-pr-plan.md`](0031-phase2-pr-plan.md) | [Progress Overview](#progress-overview-phase-2) rows filled with merged-PR numbers and dates. |
| [`docs/issues/ISSUE-0051-…`](../issues/ISSUE-0051-per-session-memory-namespacing-channels.md) | Note appended: "F-3 root cause closed by RFC 0031 Phase 2 (v0.3.5); issue stays `open` until Phase 4 operator-docs closeout." |

No code changes; doc-only.

#### PR checklist

- [ ] RFC 0031 §C table lists all four tiers.
- [ ] ROADMAP target line updated; `Last updated` refreshed.
- [ ] [Progress Overview](#progress-overview-phase-2) complete.

---

## Risk and Mitigations

| Risk | Mitigation |
|------|------------|
| The persona prompt-assembly path reads `EpisodicMemory.recall` directly, bypassing the `MemoryStore` facade. Filtering only at the facade would leave F-3 open on the persona-direct path. | PR 2 resolves `_active_session_id` **on the tier**, not only the facade — `sessions=None` is correct on both paths. PR 4's `test_session_recall_default_path.py` pins every persona-runtime recall site. |
| Migration v9 version collides with a parallel RFC's migration handler. | Version assigned at PR-author time; the umbrella `schema_version` table linearises and surfaces a collision as a CI failure on `make test` — same discipline as Phase 1 PR 3. |
| OQ #4 amends a `MemoryStore` signature RFC 0029 declared frozen for v0.4.0. | The change is additive (defaulted keyword-only param). PR 4 records it as an amendment block in RFC 0029 and gets the RFC 0029 author's sign-off in the PR thread before merge. |
| The `legacy` carve-out makes every recall see `legacy` rows by default — it can hide a recall bug where the active-session filter silently never engages. | `test_episodic_session_scope.py` / `test_relationship_session_scope.py` / `test_facts_session_scope.py` assert a `run-b` row is **absent** under `run-a`, not merely that `run-a` ∪ `legacy` is present — a no-op filter fails those tests. |
| `sessions="*"` wired into a prompt context re-introduces F-3 against the very fix this phase ships. | The `"*"` sentinel is CLI/debug-only; PR 4 pins `test_session_recall_default_path.py` that the persona-runtime context path never reaches it ([RFC §Security Considerations](0031-per-session-namespacing-channels.md#security-considerations)). |
| Session filtering and RFC 0020 §G `scope` filtering get conflated into one predicate. | [RFC §F](0031-per-session-namespacing-channels.md#f-interaction-with-rfc-0020-g-scope): separate column, separate index, separate WHERE clause. PR 2's `scope_recall` change keeps the two predicates independent; the shared `_session_filter` helper touches only `session_id`. |
| OQ #1's 1a resolution is load-bearing in this phase but a future plan author might re-litigate "why is default recall single-session?". | PR 5's `test_session_continuity.py` is the executable record; the [§Open-question status](#open-question-status-carried-from-phase-1) section above cites the lock. |

---

## ROADMAP Hygiene

Per [.github/copilot-instructions.md §Status Hygiene](../../.github/copilot-instructions.md):

- **PR 1 opens** → RFC 0031 row → `🚧 Implementing` (resuming active implementation from the `⚠️ Partially Implemented` Phase 1 pause, per Status Hygiene rule 1 — not a status regression); the v0.3.5 master-plan progress row → 🔄 In progress.
- **Each PR merges** → fill the [Progress Overview](#progress-overview-phase-2) row.
- **PR 6 merges** → RFC 0031 stays `⚠️ Partially Implemented` (Phases 3–4 remain); target line updated; `Last updated` refresh.

---

## Progress Overview (Phase 2)

| # | Title | Branch | Status | GitHub PR | Merged |
|---|-------|--------|--------|-----------|--------|
| 1 | Notes-tier session coverage | `feature/v035-rfc0031p2-notes-coverage` | ✅ Merged | [#448](https://github.com/mkhomutov/Persatrix/pull/448) | 2026-05-28 |
| 2 | Episodic + notes recall filtering | `feature/v035-rfc0031p2-episodic-recall` | ✅ Merged | [#449](https://github.com/mkhomutov/Persatrix/pull/449) | 2026-05-28 |
| 3 | Relationship + facts recall filtering | `feature/v035-rfc0031p2-relationship-facts-recall` | 🔀 PR open | [#450](https://github.com/mkhomutov/Persatrix/pull/450) | — |
| 4 | Facade read-path extension + call-site threading | `feature/v035-rfc0031p2-facade-callsites` | 🔀 PR open | — | — |
| 5 | Dementia-test bridge + review follow-ups | `feature/v035-rfc0031p2-dementia-bridge` | ⬜ Not started | — | — |
| 6 | Phase 2 closeout | `feature/v035-rfc0031p2-close` | ⬜ Not started | — | — |

**Status legend**: ⬜ Not started · 🔄 In progress · 🔀 PR open · ✅ Merged · ⏭ Deferred

---

## Future Phases

Out of scope for this plan; tracking notes only.

- **Phase 3 — Operator CLI.** `persatrix session new / use / list / archive / current`; active-session pointer at `~/.persatrix/active-session` + `PERSATRIX_ACTIVE_SESSION_FILE`; `--session` flag on `persatrix chat` / `persatrix channel …`; `persatrix memory recall --all-sessions` surfaces the `"*"` mode. Per [RFC §E](0031-per-session-namespacing-channels.md#e-operator-surface).
- **Phase 4 — Operator documentation pass.** New `docs/guides/sessions.md`; `make reset` deprecation breadcrumb; **closes [ISSUE-0051](../issues/ISSUE-0051-per-session-memory-namespacing-channels.md)**; scopes (does not build) `persatrix memory legacy-prune`.

---

## Related Documentation

- [RFC 0031 — Per-Session Namespacing for Channels and Persona Memory](0031-per-session-namespacing-channels.md) — canonical spec; §D recall semantics is this phase's contract.
- [RFC 0031 PR plan (Phase 1)](0031-pr-plan.md) — the shipped storage/write-path phase.
- [ISSUE-0051](../issues/ISSUE-0051-per-session-memory-namespacing-channels.md) — root issue; closes at Phase 4.
- [RFC 0029 — Personal/Society Storage Split](0029-personal-society-storage-split.md) — the `MemoryStore` facade PR 4 extends (OQ #4).
- [RFC 0020 §G — Per-Channel Scoping](0020-interaction-lifecycle.md#g-per-channel-scoping) — the orthogonal `scope` dimension (RFC §F).
- [RFC 0026 — Declarative Facts Tier](0026-declarative-facts-tier.md) — the `facts` tier filtered in PR 3.
- [MT-MEMORY-005 — Dementia Test](../manual-tests/MT-MEMORY-005-dementia-test.md) — Phase 2 acceptance gate; updated by PR 5.
- [MT-SESSION-001](../manual-tests/MT-SESSION-001.md) — Phase 1 write-contract MT; Edge Case 2 updated by PR 5.
- [v0.3.x-sequencing.md](../v0.3.x-sequencing.md) — the `RFC 0031 Phases 2–4` row this plan asks to commit.
