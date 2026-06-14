# RFC 0050 — PR Implementation Plan (Phase 1 — operator-editable channel config)

**RFC**: [0050-extensible-channel-configuration.md](0050-extensible-channel-configuration.md)
**Created**: 2026-06-14
**Branch prefix**: `feature/rfc0050p1-`
**Target**: `main`
**Merge strategy**: Squash merge per [BRANCHING.md](../BRANCHING.md) (< 500 lines of meaningful change per PR)
**Master plan**: none — targets the v0.3.x line, unscheduled. This plan stands alone.

---

## Overview

RFC 0050 makes per-channel governance **operator-editable at runtime** from the
CLI and web console, with the **channel store as the single source of truth**
and a **revision-gated YAML loader** so config-as-code and live edits coexist
(higher per-channel revision wins). Phase 1 delivers the store-canonical apply
path plus the CLI surface; Phase 2 (web settings panel) and Phase 3
(schema-driven generic config / profiles) are separate workstreams.

### The decisions this plan consumes (locked, do not re-litigate)

From the RFC's *Decision* section and the 2026-06-14 scoping discussion:

- **Truth model**: store-canonical; one validated apply path; three writers
  (YAML loader, CLI, web); last-writer-wins ordered by per-channel revision.
- **Boot rule**: revision-gated YAML loader; absent revision = `0` / seed-only.
- **Authorization (RFC Open Q1, resolved)**: gate the config-edit surface behind
  a **feature toggle**, mirroring the web-console mechanism — a capability knob
  in [`config/ui.yaml`](../../config/ui.yaml) reported via `GET /api/v1/ui/config`,
  exactly like `create_enabled`. Default **off**. No dedicated operator role in
  Phase 1.
- **Lineage (RFC Open Q2, resolved)**: reserve the nullable lineage column in
  the Phase 1 migration now (retrofitting it later is the expensive part); wire
  the revision counter in Phase 1, leave the lineage column **dormant**.

### The refinement this plan adds to the RFC

The RFC's Phase 1 bullet said "per-channel `revision` column." Exploration
showed that is necessary **but not sufficient**: the governance knobs
(`floor_control`, end-vote `K`/`W`, reply budget, salience cap, escalation
chair, idle timeout, interaction budget) are **not persisted in the channel
store today** — they live only in `config/channels.yaml`. Most are seeded into
the router's in-memory maps at startup by per-knob `Resolve*` methods on
`*ChannelRouter` (defined across `internal/channels/{floor_control,
router_salience,reply_budget,end_vote,interaction_resolver,chair_escalation}.go`),
invoked at boot — after `ChannelRouter.ReconcileConfig` has created the store
rows — from [`cmd/orchestrator/channels.go`](../../cmd/orchestrator/channels.go).
**Interaction budget is the exception**: it is *not* router-held — there is no
`Resolve*` boot call and no router setter/map for it; it is resolved on demand
by the value method [`ChannelConfig.ResolveInteractionBudgetTokens`](../../internal/channels/config.go)
and enforced on the wallet path (`internal/wallet/`). This asymmetry shapes the
apply path below (see PR 2). The store
(`internal/channels/sqlite_schema.go`, `PRAGMA user_version` v7) persists, on
`channels`, only `id`/`name`/`channel_type`/`description`/`created_at` plus the
isolation axes `session_id` (v3) and `epoch_id` (v6) — and `memberships`. No
governance knob lives there.

For G1 ("the change … survives restart"), **Phase 1 must persist the per-channel
overrides in the store**, not merely a revision number. Decision:

- Persist overrides as a **single sparse `config_overrides_json` TEXT column** on
  `channels`, not one column per knob. Rationale: (a) preserves tri-state
  inherit semantics (an absent key = inherit), (b) **future knobs need no new
  migration** — this is the seam the eventual schema-driven generic config (G2,
  Phase 3) grows into, (c) keeps the migration to a single additive step.
- This changes the boot flow from **YAML → router** to
  **YAML →(revision-gated)→ store → router**: the apply path writes store +
  router together, and at boot the router is seeded from the *store* overrides,
  with the YAML loader acting as one (revision-gated) writer into the store.

## Migration (PR 1)

Channel store `PRAGMA user_version` **v7 → v8**, one additive transaction
(`internal/channels/sqlite_migrations.go`, mirroring `migrateV6ToV7`):

```sql
ALTER TABLE channels ADD COLUMN config_overrides_json TEXT;          -- sparse per-channel governance overrides (NULL = none)
ALTER TABLE channels ADD COLUMN config_revision INTEGER NOT NULL DEFAULT 0;  -- monotonic, store-owned
ALTER TABLE channels ADD COLUMN config_change_lineage TEXT;          -- RESERVED / dormant (RFC Open Q2) — governance interaction id of the mutation
```

All additive nullable / defaulted columns → no PK rebuild, existing rows get
`config_revision = 0` (seed-only under the revision gate — see RFC *Migration*).

## PR breakdown

Phase 1 splits into **5 PRs**, each independently shippable and < 500 lines.

### PR 1 — Persisted channel config (storage only, no behavior change)

**Summary.** Land the migration and the store read/write layer. Overrides are
written/read but not yet consulted by the router, so this PR changes no runtime
behavior.

Deliverables:
1. Migration v7→v8 (three columns above) + `channelStoreSchemaVersion` bump.
2. `ChannelConfigOverrides` Go type — the sparse, tri-state-aware governance
   subset of `ChannelConfig` (pointers / presence flags so "unset = inherit").
3. Store accessors: `GetChannelConfig(ctx, id) (overrides, revision, error)`;
   `PutChannelConfig(ctx, id, overrides, expectedRevision, lineage)` that bumps
   `config_revision` in one transaction and returns a typed conflict error when
   `expectedRevision` is stale (optimistic concurrency primitive).
4. Tests: round-trip persistence; revision monotonicity; stale-revision
   conflict; absent-overrides → empty (inherit-all).

Dependencies: none. **No REST/CLI/router wiring.**

### PR 2 — Apply path + boot repoint (wire storage to runtime)

**Summary.** Introduce the single apply path and seed the router from the store.

Deliverables:
1. `ApplyChannelConfig(ctx, id, patch, expectedRevision, lineage)` —
   validate (single-channel rules extracted from
   [`config_validate.go`](../../internal/channels/config_validate.go)) →
   `PutChannelConfig` (persist + bump) → call the existing router setters for
   the six **router-held** knobs: `SetFloorControl`,
   `SetSalienceMaxChannelMembers`, `SetReplyBudget`, `SetEndVoteParams`,
   `SetEscalationChair`, `SetInteractionIdleTimeout`.
   **Interaction budget is excluded from this PR's apply path**: it is not
   router-held (see the refinement note above), so there is no existing setter to
   call and no in-memory map to update. Its override is still *persisted* by
   `PutChannelConfig` in PR 1's uniform `config_overrides_json` (storage is knob-
   agnostic), but applying it live requires new plumbing — a router-side budget
   map + setter, or repointing the wallet enforcement path to read the stored
   override — which is deferred (see *Open items*). So PR 2 makes six of the
   seven knobs runtime-editable; interaction budget is store-persisted but its
   live application lands later.
2. Boot repoint: a `ResolveFromStore` step after `ReconcileConfig` that loads
   each channel's persisted overrides into the router (so the in-memory maps are
   seeded from the canonical store). Empty overrides → identical to today.
3. Tests: apply persists and is reflected by the router getters; invalid patch
   rejected before any write; restart simulation (reload from store) preserves a
   prior apply.

Dependencies: PR 1.

### PR 3 — Revision-gated YAML reconciliation + drift detection

**Summary.** Make the YAML loader a revision-gated writer into the store.

Deliverables:
1. Optional `revision:` field on YAML channel blocks
   ([`config.go`](../../internal/channels/config.go) +
   [`channel.schema.json`](../../schemas/channel.schema.json)).
2. Boot reconciliation: per channel, apply the YAML overrides via the PR-2 apply
   path **only if** `yaml.revision > store.revision`; absent revision = `0` =
   seed-only (apply only to channels the store has never seen).
3. Drift detection: equal revision + differing content hash → `WARN` log (and
   the signal `config diff` will surface in PR 5).
4. Tests: the decision table — YAML newer / equal+same-hash / equal+diff-hash /
   older / absent — and the migration case (existing channel, absent revision,
   left untouched).

Dependencies: PR 2.

### PR 4 — REST: PATCH/GET config + feature toggle

**Summary.** Expose the apply path over HTTP behind the feature toggle.

Deliverables:
1. `config_edit_enabled` capability knob on `channel_timeline` in
   [`config/ui.yaml`](../../config/ui.yaml) + `schemas/ui.schema.json`, surfaced
   in `GET /api/v1/ui/config`. Default **off** (ships dark, like `memory_strip`).
2. `PATCH /api/v1/channels/{id}/config` (sparse `{key: value}`, `null` =
   unset→inherit), gated server-side on the toggle so **CLI and web are gated
   uniformly**; optimistic concurrency via an `If-Match`/revision header →
   `409 Conflict` on stale revision.
3. `GET /api/v1/channels/{id}/config` — effective values + provenance
   (fleet default → channel → member) + current revision.
4. DTOs in [`channel_types.go`](../../internal/server/channel_types.go); handlers
   mirror `handleAddChannelMember`.
5. Tests: happy-path apply; stale-revision → 409; toggle-off → 403.

Dependencies: PR 2 (PR 3 not required, but lands before for coherent reconcile).

### PR 5 — CLI `channel config` verb

**Summary.** The operator surface.

Deliverables:
1. New `cli/src/commands/channel_config.rs` + `ConfigAction` subcommand enum
   (`get`, `set`, `unset`, `export`, `import`, `diff`) wired into
   `ChannelCommands` / `dispatch`
   ([`channel_dispatch.rs`](../../cli/src/commands/channel_dispatch.rs)).
2. `get` renders effective values + provenance + revision; `set`/`unset` issue
   the PATCH with the current revision as `If-Match`; `export` writes YAML
   stamped `revision: store + 1` (the export-first foot-gun mitigation);
   `import` applies an edited file; `diff` compares declared vs effective and
   flags drift.
3. Tests: CLI integration against a toggle-on test server (set→get round-trip;
   export→edit→import; diff shows an override; conflict surfaces cleanly).

Dependencies: PR 4.

## Files touched (by PR)

| PR | Component | Files |
|----|-----------|-------|
| 1 | Storage | `internal/channels/sqlite_migrations.go`, `sqlite_schema.go`, new `channel_config_store.go` (+ test) |
| 2 | Orchestrator | new `internal/channels/config_apply.go`, `cmd/orchestrator/channels.go`, `config_validate.go` (extract single-channel rules) |
| 3 | Config | `internal/channels/config.go`, `schemas/channel.schema.json`, reconcile in `cmd/orchestrator/channels.go` |
| 4 | REST + toggle | `internal/server/server.go`, `channel_handlers.go`, `channel_types.go`, `config/ui.yaml`, `schemas/ui.schema.json` |
| 5 | CLI | `cli/src/commands/channel_config.rs` (new), `channel_dispatch.rs` |

## Test strategy (cross-PR)

- **Unit**: overrides serialization round-trip; revision monotonicity &
  conflict; single-channel validation; revision-gating decision table;
  rollback-as-new-higher-revision.
- **Integration**: `PATCH …/config` persists across a simulated restart (G1);
  optimistic-concurrency conflict; `export → edit → import`; drift warning on
  equal-revision content mismatch; toggle-off rejection.
- **E2E / smoke**: CLI `config set` flips a live knob (e.g. `floor_control`) and
  the running channel honors it without restart.
- **Manual**: an `MT-CHANNEL-CONFIG-*` arc — live-edit a governance knob
  mid-interaction, confirm the running channel honors it and that it survives a
  restart.

## Open items carried into implementation

1. **Toggle semantics.** `config_edit_enabled` lives in the *web-console* toggle
   file (`config/ui.yaml`) but gates a CLI-facing endpoint too. Accepted for
   Phase 1 (one uniform gate, matches the "similar mechanics to web-console"
   decision); revisit if a non-UI config toggle home is wanted later.
2. **Provenance rendering.** Exact `config get` provenance format (per-knob
   source labels) finalized in PR 4/5; the data (fleet default vs stored
   override vs member) is available from the resolvers.
3. **Lineage activation.** The `config_change_lineage` column ships dormant;
   populating it (RFC Open Q2) is a later, additive change with no migration.
4. **Interaction-budget live application.** Unlike the other six knobs,
   interaction budget is not router-held (no `Resolve*` boot call, no setter — it
   is read on demand by `ChannelConfig.ResolveInteractionBudgetTokens` on the
   wallet path). PR 1 persists its override uniformly, but PR 2's apply path does
   not wire it live. Closing this needs an explicit follow-up: either add a
   router-side per-channel budget map + `SetInteractionBudgetTokens` (mirroring
   the other setters) and seed it at boot, or repoint the wallet enforcement read
   at the stored override. Until then, an interaction-budget edit persists and
   takes effect on the next restart but not mid-run — so the `MT-CHANNEL-CONFIG-*`
   live-edit arc should exercise a router-held knob (e.g. `floor_control`), not
   interaction budget.
