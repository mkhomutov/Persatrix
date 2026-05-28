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

---

### PR 2: `feature/v035-rfc0031p2-episodic-recall` — Episodic + Notes Recall Filtering

**Depends on**: PR 1 merged.
**Purpose**: Make episodic and notes recall session-scoped per [RFC §D](0031-per-session-namespacing-channels.md#d-recall-semantics). Establish the `sessions` parameter shape and the active-session resolution that PRs 3–4 reuse.

#### Scope

| File | Change |
|------|--------|
| [`agents/memory/episodic.py`](../../agents/memory/episodic.py#L267) | `EpisodicMemory.recall` gains `sessions: list[str] | str | None = None` (keyword-only). `__init__` resolves `_active_session_id` once via `resolve_session_id_silent()` ([`agents/session_id.py`](../../agents/session_id.py) — the leaf module the facade already uses). Add `session_id` to the `EPISODIC_RECALL_SPAN` attributes (OQ #7). |
| [`agents/memory/episodic_queries.py`](../../agents/memory/episodic_queries.py) | `recall_fts5` (L188), `recall_like` (L237), `recall_recency` (L273) accept a resolved session-filter argument and append `AND (e.session_id IN (…) OR e.session_id = 'legacy')` to the WHERE clause. The `"*"` mode drops the predicate entirely. |
| [`agents/memory/notes.py`](../../agents/memory/notes.py#L144) | `NoteStore.recall_notes` (L144) gains the same `sessions` parameter; `_recall_notes_fts5` / `_recall_notes_like` / `_recall_notes_recency` add the predicate. |
| [`agents/memory/episodic_notes_api.py`](../../agents/memory/episodic_notes_api.py#L51) | `recall_notes` delegation forwards `sessions`. |
| [`agents/memory/scope_recall.py`](../../agents/memory/scope_recall.py#L42) | `recall_with_scope_filter` gains a `sessions` passthrough to `episodic.recall`. Orthogonal to the RFC 0020 §G `scope` filter per [RFC §F](0031-per-session-namespacing-channels.md#f-interaction-with-rfc-0020-g-scope) — separate predicate, separate index; no `scope`-prefix widening. |
| `tests/unit/python/test_episodic_session_scope.py` | **New** — default recall (`sessions=None`) returns active-session + `legacy` rows; explicit `sessions=[a,b]` list; `sessions="*"` returns everything; empty list raises `ValueError` per §D. Mirror cases for `recall_notes`. |

#### Key implementation details

- **Three modes per [RFC §D](0031-per-session-namespacing-channels.md#d-recall-semantics)**: `None` → `[_active_session_id]` + `legacy` carve-out; explicit list → that list + `legacy`; `"*"` → no IN-clause. Empty list raises `ValueError("sessions must be None, '*', or a non-empty list")` — the §D guard against the silent legacy-only collapse.
- **The `legacy` carve-out** (`session_id = 'legacy'` always visible in modes 1–2) is the load-bearing detail that ships Phase 2 with **no backfill** of pre-RFC rows. In mode 3 (`"*"`) the carve-out is a no-op.
- **Active-session ownership.** §D's pseudocode reads `self._active_session_id`. The tier resolves it from `PERSATRIX_SESSION_ID` at construction — *not* only the facade — because the persona prompt-assembly path reads `EpisodicMemory.recall` **directly, bypassing the facade** (the explicit comment at [`episodic.py:335-344`](../../agents/memory/episodic.py#L335)). Resolving on the tier makes `sessions=None` correct on both the facade path and the persona-direct path. The facade passes its own `_session_id` explicitly when it calls down (PR 4) — defence in depth, not the only line.
- The empty-query recency path (`recall_recency`) must filter too — [`channel_history.py`](../../agents/persona_runtime/channel_history.py) calls recall with an empty query.

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
| [`agents/memory/store.py`](../../agents/memory/store.py) | `retrieve_relevant` (L256) gains `sessions: list[str] | str | None = None`, forwarded to `EpisodicMemory.recall` / `recall_notes` / `scope_recall`. Default `None` → the facade's own `_session_id` ([`store.py:160`](../../agents/memory/store.py#L160)). |
| [`agents/memory/facade_procedural.py`](../../agents/memory/facade_procedural.py) | `retrieve_procedures` (L186) gains `sessions`. |
| [`agents/memory/shared_pool_facade.py`](../../agents/memory/shared_pool_facade.py) | `read_from_pool` (L141) gains `sessions`. |
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
| 1 | Notes-tier session coverage | `feature/v035-rfc0031p2-notes-coverage` | ⬜ Not started | — | — |
| 2 | Episodic + notes recall filtering | `feature/v035-rfc0031p2-episodic-recall` | ⬜ Not started | — | — |
| 3 | Relationship + facts recall filtering | `feature/v035-rfc0031p2-relationship-facts-recall` | ⬜ Not started | — | — |
| 4 | Facade read-path extension + call-site threading | `feature/v035-rfc0031p2-facade-callsites` | ⬜ Not started | — | — |
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
