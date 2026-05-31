# RFC 0031 — PR Implementation Plan (Epoch Axis — Run/Test Isolation)

**RFC**: [0031-per-session-namespacing-channels.md](0031-per-session-namespacing-channels.md) · design home: [Memory Scope Axes §Epoch](../memory-scope-axes.md#epoch--the-testrun-isolation-axis)
**Tracks**: [ISSUE-0085](../issues/ISSUE-0085-epoch-axis-run-isolation.md)
**Status**: 🚧 Implementing — v0.3.5 ([Phase 3b](../v0.3.5-plan.md#phase-3b--rfc-0031-epoch-axis-issue-0085)); PR 1 (leaf) merged, PR 2 (migration) open ([#474](https://github.com/mkhomutov/Persatrix/pull/474)), PRs 3–6 remaining
**Created**: 2026-05-31
**Branch prefix**: `feature/v035-issue0085-` / `feature/v035-epoch-`
**Target**: `main`
**Merge strategy**: Squash merge per [BRANCHING.md](../BRANCHING.md)
**Companion to**: [0031-phase3-pr-plan.md](0031-phase3-pr-plan.md) (operator CLI — its [scope-axes amendment](0031-phase3-pr-plan.md#amendment--scope-axes-reframing) flagged the epoch-operator-surface decision this plan resolves) · [0031-phase2-pr-plan.md](0031-phase2-pr-plan.md) (recall filtering — closed the *recall* half of F-3)

---

## Overview

The [scope-axes reframing](../memory-scope-axes.md) redefined `session` as **room-continuity** (`(agent, channel)`, accumulating, with a `legacy` carve-out that exists *for* continuity — [ISSUE-0083](../issues/ISSUE-0083-session-binding-sender-axis-fragments-multiparty-rooms.md) dropped the sender axis). Once session is continuity it can no longer be the per-run isolation namespace RFC 0031 originally built it to be, so **F-3 ("a rerun must not inherit the prior run's state") needs its own axis** — `epoch`.

RFC 0031 Phase 2 closed the **recall** half of F-3 (session-scoped recall stops cross-run *reads*). This plan ships the **structural** half: an `epoch_id` dimension that resets *both* room-scoped (episodes) and person-scoped (relationship, person-facts) memory at once, which a fresh channel name cannot — relationship/person-facts are keyed on the participant and survive a room rename, so a rerun reusing `--user alice` still inherits old trust until epoch isolates it.

**The epoch axis is structurally identical to the `principal_id` tenant axis** ([ISSUE-0081](../issues/ISSUE-0081-session-id-process-global-not-task-local.md) PR 3), not the session axis:

- **Strict equality**, **no `legacy` carve-out**, **no `"*"` sentinel** — a fresh epoch must see *nothing* (contrast `agents/memory/_session_filter.py`, whose carve-out exists for continuity).
- Default `live`; production never changes it (behaviour unchanged), CI bumps it per job.
- `epoch_id` in the `relationships` **primary key** (not just a column) — same `ON CONFLICT DO UPDATE` reasoning as `principal_id` there: a column-only addition would let a write bleed trust across epochs while a residual filter masks it.
- `make reset` is **kept** as the documented nuke; it wipes the volume (all epochs), so it cannot express the isolated-but-coexisting worlds epoch gives CI.

Overloading `principal_id` for test isolation was **rejected** (re-commits the "one identifier, many meanings" mistake the reframing exists to fix; in real multi-tenant prod the principal is already meaningful).

### Resolves the Phase 3 plan's open decision

The [Phase 3 plan amendment](0031-phase3-pr-plan.md#amendment--scope-axes-reframing) left open *"does Phase 3 (or a sibling phase) ship an operator surface for the epoch axis, or does `make reset` remain the run-isolation tool?"* **Resolved (maintainer, 2026-05-31): epoch ships as its own v0.3.5 phase (Phase 3b) with an operator surface (PR 5), and the `make reset` operator-guide framing is redirected at epoch (PR 6).** This is why the [v0.3.5 plan](../v0.3.5-plan.md) acceptance now gates on the full epoch axis.

---

## The axis this sits parallel to

Three orthogonal logical-key axes after this lands; epoch is the third producer:

| Axis | Question | Recall predicate | Default | Producer |
|------|----------|------------------|---------|----------|
| `session_id` | which room / conversation? | active session **∪ `legacy` carve-out** | `legacy` | session resolution chain (Phase 3) |
| `principal_id` | which tenant owns this? | **strict equality**, no carve-out | `local` | silent until RFC 0039 |
| `epoch_id` | which test run / logical branch? | **strict equality**, no carve-out, no `"*"` | `live` | orchestrator, live from day one (PR 4) |

The epoch leaf (`agents/epoch_id.py`, PR 1) is the dependency-free sibling of [`agents/principal_id.py`](../../agents/principal_id.py); the filter helper (`agents/memory/_epoch_filter.py`, PR 3) is the sibling of [`agents/memory/_principal_filter.py`](../../agents/memory/_principal_filter.py).

---

## Dependency Graph

```
PR 1 (epoch leaf: agents/epoch_id.py — env/header/contextvar/scope) ── ✅ merged (#472)
  ↓
PR 2 (migration v12: epoch_id TEXT NOT NULL DEFAULT 'live' across the five persona-memory tiers
      + channel-store v6; epoch_id in the relationships PK; backfill 'live')
  ↓
PR 3 (agents/memory/_epoch_filter.py strict-equality predicate + per-tier recall+write wiring)
  ↓
PR 4 (gRPC rail: orchestrator resolves PERSATRIX_EPOCH at boot + emits persatrix-epoch per request;
      persona-runtime on_event lifts it into epoch_scope)
  ↓
PR 5 (operator surface: --epoch override + PERSATRIX_EPOCH documentation)
  ↓
PR 6 (closeout: F-3 structural-isolation integration test; make reset framing → epoch; RFC/ROADMAP/plan status)
```

PR 2 before PR 3 — the filter cannot wire to a column that does not exist. PR 3 before PR 4 — emitting an epoch header onto an unfiltered storage layer is a no-op. PR 4 before PR 5 — the operator override rides the rail PR 4 builds. PR 6 depends on all of them — the integration test drives a real rerun under a fresh epoch.

---

## PR Sequence

### PR 1: `feature/v035-issue0085-epoch-leaf` — Epoch Leaf Module ✅

**Status**: ✅ Merged ([#472](https://github.com/mkhomutov/Persatrix/pull/472)).
**Delivered**: `agents/epoch_id.py` — the dependency-free leaf (env var `PERSATRIX_EPOCH` default `live`, the `persatrix-epoch` gRPC header + `persatrix_epoch` event key, the task-local `ContextVar`, `epoch_scope` / `epoch_scope_from_metadata`). Test-driven by `tests/unit/python/test_epoch_id_leaf_module.py` (symbol/constant contract, resolution precedence, scope set/restore + task-local isolation, metadata lift, the no-carve-out/no-`"*"` pin, the no-logging-dependency AST pin). Silent by design; mirrors the principal leaf's PR-1 enabler split.

### PR 2: `feature/v035-epoch-migration` — Migration (epoch_id columns + relationships PK)

**Status**: 🔀 PR open ([#474](https://github.com/mkhomutov/Persatrix/pull/474)).
**Depends on**: PR 1.
**Purpose**: Add `epoch_id TEXT NOT NULL DEFAULT 'live'` to the five persona-memory tiers (`episodes`, `relationships`, `facts`, `notes`, `interactions`) as persona-memory **migration v12**, and the sibling column on the Go channel store (**schema v6**, after the ISSUE-0083 v5). Put `epoch_id` in the `relationships` **primary key**, mirroring the `principal_id` v11 migration's table-rebuild handler ([`agents/memory/_migration_principal.py`](../../agents/memory/_migration_principal.py)).

**Key details**: backfill `'live'` onto pre-existing rows (the default makes this implicit for `ALTER ... ADD COLUMN`; the relationships rebuild copies rows with `epoch_id = 'live'`). No data loss; single-world / pre-migration deployments are byte-identical (everything is the default epoch). Follows the v11 callable-handler pattern for the PK rebuild, the plain `ALTER` for the other four tiers. The PK change forces the two `relationships` upsert `ON CONFLICT` targets ([`relationship_mutations.py`](../../agents/memory/relationship_mutations.py)) to add `epoch_id` to match — the *only* per-tier write touch in this PR; resolving and *tagging* the active epoch stays in PR 3 (rows keep the `'live'` column default here). The Go-store half adds `epoch_id` to `channels` + `messages` (mirroring the v3 `session_id` placement); the v6 `migrateV5ToV6` addition tipped `internal/channels/sqlite_schema.go` over the 500-line cap, so the migration functions were extracted into `internal/channels/sqlite_migrations.go` (mechanical move, no behaviour change).

**Tests**: migration idempotency + version-record; relationships PK includes `epoch_id` (an `ON CONFLICT` upsert under two epochs creates two rows, never bleeds); backfill leaves pre-existing rows at `'live'`. Mirror `test_principal_migration.py`.

### PR 3: `feature/v035-epoch-filter` — Strict-Equality Filter + Per-Tier Wiring

**Depends on**: PR 2.
**Purpose**: Add `agents/memory/_epoch_filter.py` (`resolve_active_epoch` + `epoch_eq_clause`, the sibling of [`_principal_filter.py`](../../agents/memory/_principal_filter.py)) and wire **every recall and per-request write path** across the five tiers to filter and tag by the resolved epoch. Unconditional `AND epoch_id = ?` — **no carve-out, no `"*"` bypass**.

**Key details**: same single-seam discipline as principal — resolve once at each tier's public-API boundary, pass the resolved id to both the recall helper *and* the write so a row is always readable by the epoch that wrote it. **Maintenance-sweep caveat (inherited):** the agent-global eviction/retention/janitor sweeps already skip the `principal_id` filter; `epoch_id` inherits the same gap and the same deferral (a capacity-policy decision, not a per-request read path) — recorded, not closed, here.

**Tests**: a row written under epoch A is invisible under epoch B across all five tiers; default-epoch (`live`) behaviour unchanged; the no-`"*"`/no-carve-out predicate pinned. Mirror `test_principal_filter.py` + `test_principal_legacy_carveout.py` (inverted: epoch must have *no* carve-out).

### PR 4: `feature/v035-epoch-rail` — gRPC Rail (orchestrator emission + ingress lift)

**Depends on**: PR 3.
**Purpose**: Light up the producer. The orchestrator resolves the epoch at boot from `PERSATRIX_EPOCH` (default `live`) and emits it on the `persatrix-epoch` gRPC header per request; the persona-runtime ingress lifts the header into an `epoch_scope` for the handler's lifetime via `on_event` (mirroring the principal metadata rail wiring + `test_principal_metadata_rail.py` / `test_principal_scope.py`).

**Key details**: unlike the principal rail (silent until RFC 0039), the epoch rail has a live producer immediately — `live` in prod, a per-job id in CI. Cross-language contract: the emitted header string-matches `agents.epoch_id.EPOCH_METADATA_GRPC_KEY` (asserted as a literal, per the ISSUE-0082 PR 2 discipline). The `channel.dispatch` span carries the resolved epoch (low-cardinality-on-span, never a metric label — per OQ #7).

### PR 5: `feature/v035-epoch-operator` — Operator Surface

**Depends on**: PR 4.
**Purpose**: The operator surface resolved as in-scope by the Phase 3 plan's open decision. Minimum: a `--epoch <id>` override (parity with `--session`, precedence above the boot env) on the dispatch-bearing verbs, and `PERSATRIX_EPOCH` documented as the per-process/CI knob. Whether epoch warrants registry verbs (`epoch new/list`) like sessions, or stays a bare flag + env (epoch has no continuity-room lifecycle to manage), is settled in the PR thread — the bare flag + env is the default, registry verbs an explicit add.

### PR 6: `feature/v035-epoch-close` — Closeout

**Depends on**: PR 5.
**Purpose**: Prove the structural F-3 fix end-to-end and land the documentation/status closeout. No new production code.

**Scope**:

- `tests/integration/test_epoch_run_isolation.py` (new): the acceptance gate — establish trust + a person-fact + episodes under epoch `run-1` with `--user alice`, then **rerun under a fresh `PERSATRIX_EPOCH`** and assert recall surfaces *none* of it (relationship, facts, episodes all reset) — the symptom a fresh channel name alone cannot cure.
- `make reset` operator-guide framing reframed to point at **epoch** as the everyday logical-branch tool (*"epoch isolates a run; `make reset` wipes all epochs across all sessions"*), per [ISSUE-0085 step 5](../issues/ISSUE-0085-epoch-axis-run-isolation.md). This **supersedes** the make-reset breadcrumb that RFC 0031 Phase 4 pointed at `session new --activate` (which the reframing established is wrong — a continuity room is not a clean slate).
- RFC 0031 / [Memory Scope Axes](../memory-scope-axes.md) / [v0.3.5 plan](../v0.3.5-plan.md) status refresh; [ISSUE-0085](../issues/ISSUE-0085-epoch-axis-run-isolation.md) → `resolved`.

---

## Risk and Mitigations

| Risk | Mitigation |
|------|------------|
| The relationships `epoch_id` lands as a column, not in the PK, so an `ON CONFLICT` upsert bleeds trust across epochs while the recall filter masks it. | PR 2 puts `epoch_id` in the PK via the v11-style table-rebuild handler; PR 3's cross-epoch test writes the same `(agent, subject)` under two epochs and asserts two rows. |
| A new epoch silently sees old rows because some tier's recall path was missed in the wiring. | PR 3 routes every tier through the single `_epoch_filter` seam (no per-tier predicate drift, the `_principal_filter` discipline); PR 6's integration rerun exercises all tiers at once. |
| The strict-equality epoch filter accidentally grows a carve-out / `"*"` escape, re-opening F-3. | The leaf (PR 1) AST-pins no carve-out / no-`"*"` constant; PR 3 mirrors `test_principal_legacy_carveout.py` inverted. |
| `make reset` docs still point at `session new --activate` (pre-reframing) after epoch ships, misdirecting operators. | PR 6 reframes the breadcrumb at epoch and supersedes the Phase 4 wording. |

---

## Progress Overview

| # | Title | Branch | Status | GitHub PR | Merged |
|---|-------|--------|--------|-----------|--------|
| 1 | Epoch leaf module | `feature/v035-issue0085-epoch-leaf` | ✅ Merged | [#472](https://github.com/mkhomutov/Persatrix/pull/472) | 2026-05-31 |
| 2 | Migration (epoch_id columns + relationships PK) | `feature/v035-epoch-migration` | 🔀 PR open | [#474](https://github.com/mkhomutov/Persatrix/pull/474) | — |
| 3 | Filter helper + per-tier wiring | `feature/v035-epoch-filter` | ⬜ Not started | — | — |
| 4 | gRPC rail (emission + ingress lift) | `feature/v035-epoch-rail` | ⬜ Not started | — | — |
| 5 | Operator surface (`--epoch` + env docs) | `feature/v035-epoch-operator` | ⬜ Not started | — | — |
| 6 | Closeout (F-3 structural-isolation gate + docs) | `feature/v035-epoch-close` | ⬜ Not started | — | — |

**Status legend**: ⬜ Not started · 🔄 In progress · 🔀 PR open · ✅ Merged · ⏭ Deferred

---

## Related Documentation

- [Memory Scope Axes §Epoch](../memory-scope-axes.md#epoch--the-testrun-isolation-axis) — the design home; canonical analysis of why isolation needs its own axis.
- [ISSUE-0085](../issues/ISSUE-0085-epoch-axis-run-isolation.md) — the tracking issue; this plan executes its "Proposed fix" steps.
- [v0.3.5 master plan §Phase 3b](../v0.3.5-plan.md#phase-3b--rfc-0031-epoch-axis-issue-0085) — the umbrella; gates the release on this axis.
- [0031-phase3-pr-plan.md](0031-phase3-pr-plan.md) — the operator-CLI plan whose open epoch decision this resolves.
- [agents/principal_id.py](../../agents/principal_id.py) / [agents/memory/_principal_filter.py](../../agents/memory/_principal_filter.py) — the structural template every PR here mirrors.
