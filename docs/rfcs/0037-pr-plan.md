# RFC 0037 — PR Implementation Plan (Phases 1–3 — v0.3.12 scope, + the ISSUE-0106(b) rider)

**RFC**: [0037-memory-confidentiality-channel-classification.md](0037-memory-confidentiality-channel-classification.md)
**Created**: 2026-07-25
**Branch prefix**: `feature/v0312-rfc0037-`
**Target**: `main`
**Merge strategy**: Squash merge per [BRANCHING.md](../BRANCHING.md)
**Master plan**: [v0.3.12-plan.md Phase 1](../v0.3.12-plan.md#phase-1--implement-rfc-0037-rfc-0049-p01-rfc-0039-p12)

---

## Overview

RFC 0037 is the **keystone** of the v0.3.12 cross-channel persona-experience release: an ordered confidentiality classification on every channel, a protection level on every channel-derived memory entry, and a deterministic hard gate in the memory-injection layer — so a persona can *learn* from a confidential channel without *leaking* it. Nothing in the [RFC 0049 PR plan](0049-pr-plan.md) may merge before this plan's §D gate + §F filter are on `main` (the [master-plan merge gate](../v0.3.12-plan.md#phase-1--implement-rfc-0037-rfc-0049-p01-rfc-0039-p12)).

This plan covers all three phases across **8 PRs**, mirroring the RFC's [phasing](0037-memory-confidentiality-channel-classification.md#phased-implementation-plan):

- **Phase 1 — the deterministic boundary (PRs 1–5).** Lattice helpers → channel classification (config/store/wire) → protection levels on the memory tiers → the §D hard gate + gated read surfaces + the §B single-channel-turn guard → the §F recall classification filter (+ the ISSUE-0106(b) rider on the same endpoint).
- **Phase 2 — declassification projections (PR 6, cuttable).** The close-consolidation call emits lower-level one-line projections; the §D gate serves the highest projection `≤ L` instead of a blunt withhold.
- **Phase 3 — the leak tripwire (PR 7, cuttable).** The §G verbatim-span tripwire + RFC 0009 audit event + rate instrumentation.
- **Closeout (PR 8).** Docs/diagrams + `MT-PERSONA-CONFIDENTIALITY-001` + the RFC 0044 golden recipe + RFC flip.

**Hard prerequisites (all shipped):** RFC 0011 channels (✅ v0.3.0), RFC 0036 recall endpoint + scoped query (✅ v0.3.9 — step 5 retrofits it), RFC 0020 interaction close (✅ v0.3.0 — §C stamping point), RFC 0009 audit subsystem (✅ v0.3.0 — Phase 3 consumer).

**The item-8 dark-window rule (from the RFC).** All Phase-1 steps land within one release; **no channel may be operator-classified above `internal` before the full Phase-1 set ships**; the notes-tier stamping leg lands in the same PR as — or after — the §D gate *and* the `recall_notes` gating (its soundness argument presumes both). PRs 1–3 are therefore *dark substrate*: columns exist and are stamped, but nothing reads them until PR 4.

### Scope decisions locked at plan-authoring time (2026-07-25)

- **All three phases ride v0.3.12; Phases 2–3 are cuttable** ([master plan](../v0.3.12-plan.md#scope-decisions-locked-at-plan-authoring-time-2026-07-25)). Cutting Phase 2 downgrades "informed by, doesn't disclose" to "withheld" — documented in release notes if taken.
- **ISSUE-0106 direction (b) rides PR 5.** The recall endpoint's `epoch_id` body override is dropped in the same PR that adds the required acting-channel parameter — the endpoint's request shape changes once, not twice. RFC 0036 §OQ-6 is amended in-PR; the `TestRecallEndpoint_RealPublishPath_ExplicitEpochUnreachable` tripwire retires with the axis it guarded.
- **The §B single-channel-turn guard (RFC 0038 §B carve-in, Decision #3) lands in PR 4**, with/after the §D gate whose tick exception it presumes — not as its own PR.

## Dependency Graph

```
PR 1 (lattice helpers + channel classification: config/schema + store v11 + DM stamping)   [dark]
  │
  ├── PR 2 (wire: proto classification + dispatch + history/catch-up stamping)             [dark]
  │       │
  │       └── PR 3 (memory substrate: protection_level/source_channel_id/provenance_json
  │             + memory_projections table + interaction-open capture + episodic/facts
  │             stamping)                                                                  [dark]
  │               │
  │               └── PR 4 (§D hard gate + classification_scope per turn + notes leg
  │                     [stamp + gated recall_notes + update_note re-stamp] + autonomous-
  │                     tick public floor + §B single-channel-turn guard)             [gate live]
  │                       │
  │                       ├── PR 5 (§F recall filter + acting-channel param + tool pass-
  │                       │     through + ISSUE-0106(b) epoch-override removal)  ══ MERGE GATE
  │                       │     └─→ RFC 0049 PR plan slices unblock here
  │                       ├── PR 6 (Phase 2: projections writer + §D projection branch)  [cuttable]
  │                       └── PR 7 (Phase 3: §G tripwire + audit + instrumentation)      [cuttable]
  │                               │
  └───────────────────────────────┴── PR 8 (closeout: docs/diagrams + MT + golden + RFC flip)
```

---

## PR 1 — `feature/v0312-rfc0037-lattice-config` (Phase 1 steps 1–2a: lattice + channel classification at rest)

- `internal/channels/classification.go` (new): `classification_rank` — total order over the §A lattice; `agents/persona_runtime/classification.py` (new): the Python twin. Single source per side, property-tested for agreement on the shared enum.
- **No blanket unknown-default on the rank helper.** §A splits fail-closed into three rules *because* "restrictive" flips direction across the helper's uses: (a) stamping/labeling → `internal`; (b) acting level at gate/recall time → the **`public` floor**; (c) an unknown/unparseable *entry* protection level → **withheld and logged** (treated as above-`secret`). A single `unknown → internal` default would make (c) unimplementable through the helper — a corrupted entry label would rank `internal` and inject cleanly into any `internal` turn. So `classification_rank` takes only known levels and the unknown case is explicit: an `ok`/sentinel return (Go `(rank, bool)`; Python `None`), with three named resolvers — `rank_for_stamp` (a), `acting_rank` (b), `entry_rank_or_withhold` (c) — owning one rule each. No caller applies its own default.
- `config/channels.yaml` + `schemas/channel.schema.json`: per-channel `classification` field + the `dm_default_classification` knob (default `internal`); `make validate` rejects unknown levels.
- Channel-store migration **v11** (`sqlite_schema.go` version const + history, `sqlite_migrations.go` `migrateV10ToV11`): `channels.classification` column, backfill `internal`.
- `internal/channels/sqlite_dm.go` (`GetOrCreateDM`): stamp `dm_default_classification` at DM creation; thread replies inherit the parent row's classification by construction (§B — asserted by test, no code path).
- Tests: rank totality; **the three §A fail directions asserted here, at the helper** (stamp→`internal`, acting→`public`, entry→withhold) on both sides, so the contract is pinned where it is defined — PR 4 then asserts the same three *through the gate*; migration on a populated v10 store; schema validation; DM stamping.
- **Dark**: nothing reads `channels.classification` yet.

## PR 2 — `feature/v0312-rfc0037-wire` (Phase 1 step 2b: classification on the wire)

- `proto/task.proto`: `classification` on `ChannelMessageEvent`; regen stubs with the **CI-pinned protoc/plugin versions** (the standing toolchain-pin discipline).
- Go dispatch path: lift the channel's classification onto every dispatched event; REST message/history response builder carries `classification` for catch-up replay (§B).
- Python: `agents/server_servicers.py` + `agents/channel_catchup.py` read it off both delivery paths; `agents/channel_wire_metadata.py` threads it toward the (PR 3) interaction-open capture.
- Tests: dispatch + catch-up carry the stamp; an unclassified legacy event resolves fail-closed.
- **Dark**: the value is delivered and ignored.

## PR 3 — `feature/v0312-rfc0037-memory-substrate` (Phase 1 step 3, minus the notes leg)

- `agents/memory/migrations.py`: `protection_level` / `source_channel_id` / nullable `provenance_json` (the §C multi-source shape, created now so the v0.4.0 pump needs no second migration) on the **episodic and facts** tiers (+ the notes columns created but not yet stamped — see PR 4); the `memory_projections` table (created here, used in PR 6). Backfill `internal`.
- `agents/memory/interactions.py` + `interaction_types.py`: capture the acting channel's classification at interaction-open (frozen-at-open, in-memory until close — the `session_id` precedent).
- `agents/memory/episodic.py` + `facts.py`: stamp `protection_level` at close-consolidation; `agents/tools/identity_write_through.py`: the §C ≤-`internal` write-through rule (room-scoped-note fallback above it).
- Tests: stamping from a `restricted` interaction; migration on populated pre-migration stores; backfill.
- **Dark**: stamped, never read.

## PR 4 — `feature/v0312-rfc0037-hard-gate` (Phase 1 steps 4 + 3-notes + 6: the gate goes live)

- `agents/persona_runtime/memory_context.py`: the §D hard gate — an entry whose `protection_level` outranks the acting channel's classification is **withheld** (projection branch arrives in PR 6); `MemoryInjectionResult` grows the injection manifest (§G's future input); the **autonomous-tick `public` floor**.
- `agents/persona_runtime/action_loop.py`: enter `classification_scope(L)` per turn; thread the manifest.
- The **notes leg**, now sound: `agents/memory/notes.py` stamping + gated `recall_notes` (`agents/tools/builtin.py`, `agents/memory/_notes_recall.py`) + `update_note` re-stamp (§C).
- The **§B single-channel-turn guard** (RFC 0038 §B carve-in): event-aware post-parse check in `_on_event_inner` — a non-tick `SEND_CHANNEL_MESSAGE` whose `channel_id` differs from the acting channel becomes `DO_NOTHING` + WARNING (audit wire-up is a tracked follow-up); tick turns publish anywhere (their injection is already floored `public`).
- Tests: at/below/above-rank injection matrix; tick floor; notes gating + re-stamp; the guard's positive-list `EventType` → acting-level resolution (v0.3.12 review item 5); the §A three-way fail directions (unknown acting → `public` floor; unknown entry → withheld + logged).
- **Gate live** — but no channel is classified above `internal` yet (item-8 rule), so behavior on real deployments is unchanged until operators opt in post-release.

## PR 5 — `feature/v0312-rfc0037-recall-filter` (Phase 1 step 5 + ISSUE-0106(b)) ══ the merge gate opens here

- `internal/channels/sqlite_search.go`: the §F acting-channel classification clause composed with the RFC 0036 membership join.
- `internal/server/persona_recall_handlers.go` + `channel_types.go`: **required** acting-channel parameter on the recall endpoint; `agents/tools/recall.py` passes the event classification.
- **ISSUE-0106 direction (b)**: drop the `epoch_id` body override from the endpoint and `EpochOverrideFromContext` from this path; retire `TestRecallEndpoint_RealPublishPath_ExplicitEpochUnreachable`; amend [RFC 0036 §OQ-6](0036-persona-message-recall.md#open-questions) + the stale "recall and publish agree on the epoch axis" claim in [0036-pr-plan.md](0036-pr-plan.md); close [ISSUE-0106](../issues/ISSUE-0106-recall-epoch-filter-decoupled-from-unpersisted-publish-epoch.md) (deployment model: separate runs never share a channel-store DB — locked at [plan opening](../v0.3.12-plan.md#scope-decisions-locked-at-plan-authoring-time-2026-07-25)).
- Tests: `secret`-channel message excluded acting-`public`, included acting-`secret`; composition with membership scoping; the removed override returns a clear 4xx on old callers.
- **Merging this PR opens the [RFC 0049 PR plan](0049-pr-plan.md).**

## PR 6 — `feature/v0312-rfc0037-projections` (Phase 2 — cuttable)

- `agents/persona_runtime/summarize_close.py` (+ `fact_envelope.py` / `fact_extractor.py`): the RFC 0020 close-consolidation LLM call emits one-line projections at each lower level into `memory_projections` (RFC 0027 reflection is the future second producer).
- `memory_context.py`: the §D projection-selection branch — highest projection `≤ L` replaces the blunt withhold.
- Integration test: a persona acting `public` is *informed by* a `restricted` memory via its projection, without verbatim disclosure.

## PR 7 — `feature/v0312-rfc0037-tripwire` (Phase 3 — cuttable, independent of PR 6)

- `agents/channel_publisher.py` + `agents/action_executor.py`: the §G verbatim-span tripwire over the injection manifest (metadata-only audit — never the text); `internal/security/audit_event.go`: `channel.confidentiality_tripwire`.
- Tripwire-rate instrumentation alongside the existing persona metrics.
- Tests: fires on a seeded verbatim span; silent on benign traffic; audit payload carries metadata only.

## PR 8 — `feature/v0312-rfc0037-closeout`

- Docs: `docs/guides/persona-agents.md` + `channels.md` + `sessions.md` (classification, protection levels, the two-axis model, the operator opt-in path); `docs/diagrams/memory-architecture.md`.
- `MT-PERSONA-CONFIDENTIALITY-001` (manual test: learn-restricted / act-public / withheld-or-projected / act-restricted verbatim / tripwire leg) + an RFC 0044 golden-trace recipe for the gate.
- RFC 0037 front-matter → ✅ Implemented; ROADMAP row flip; catch-up replay stamping test for `secret`-channel episodes (v0.3.12 review item 8) if not landed earlier.
- **RFC 0038 front-matter → ⚠️ Partially Implemented** (§B single-channel-turn guard shipped v0.3.12 via this plan's PR 4; §C–§E stay v0.4.0) + its ROADMAP/INDEX rows — the §B carve-in is implemented here, so 0038 must not stay 📋 Proposed through the release that ships it.

---

## Progress Overview

| PR | Step | Branch | Status | GitHub PR | Merged |
|----|------|--------|--------|-----------|--------|
| 1 | 1–2a — lattice helpers + channel classification at rest (config/schema + store v11 + DM stamping; dark) | `feature/v0312-rfc0037-lattice-config` | 🔀 PR open | _this PR_ | — |
| 2 | 2b — classification on the wire (proto + dispatch + history/catch-up; dark) | `feature/v0312-rfc0037-wire` | ⬜ | — | — |
| 3 | 3 — memory substrate (protection levels + projections table + interaction-open capture; dark) | `feature/v0312-rfc0037-memory-substrate` | ⬜ | — | — |
| 4 | 4 + 3-notes + 6 — §D hard gate + notes leg + tick floor + §B guard (gate live) | `feature/v0312-rfc0037-hard-gate` | ⬜ | — | — |
| 5 | 5 — §F recall filter + acting-channel param + ISSUE-0106(b) ══ merge gate | `feature/v0312-rfc0037-recall-filter` | ⬜ | — | — |
| 6 | Phase 2 — declassification projections (cuttable) | `feature/v0312-rfc0037-projections` | ⬜ | — | — |
| 7 | Phase 3 — §G leak tripwire (cuttable) | `feature/v0312-rfc0037-tripwire` | ⬜ | — | — |
| 8 | closeout — docs/diagrams + MT + golden recipe + RFC flips | `feature/v0312-rfc0037-closeout` | ⬜ | — | — |

**Status legend**: ⬜ Not started · 🔄 In progress · 🔀 PR open · ✅ Merged

**PR 1 note.** Group channels take the migration's `internal` DEFAULT at creation in PR 1; the *declared* `classification` is parsed + validated but threads into the store row with PR 2's `Channel` plumbing (the wire lift reads the same field). Behaviour-identical while the item-8 dark-window rule holds — and PR 1 makes that rule **enforced**, not documented: `Config.Validate` rejects any declared level above `internal` (`ErrClassificationAboveDarkWindow`) precisely because a declaration that never reaches the store row would read as a boundary to the operator while every path still sees `internal`. **PR 4 must delete that guard** (`CheckDarkWindowClassification` + its two call sites + the guard tests) when the §D gate arms; the schema enum already advertises all four levels, so nothing else changes.

**PR 2 note — existing rows.** Threading the declared `classification` through `CreateChannel` is not sufficient on its own: there is no `UPDATE channels SET classification` path today, and `ReconcileFromYAML` reconciles only `config_overrides_json` under the RFC 0050 revision gate. Any store created at PR 1 or earlier keeps `classification='internal'` on its group rows forever. PR 2 (or PR 4 at the latest, before the gate reads the column) owes an explicit adoption step for pre-existing rows, or a `restricted` declaration will silently under-classify on every upgraded deployment while reading as correct in config.

---

## Test strategy cross-reference

The RFC's [Test Strategy](0037-memory-confidentiality-channel-classification.md#test-strategy) maps: unit lattice/gate/stamping/query/tripwire → PRs 1/4/3/5/7; migration tests → PRs 1/3; integration (restricted→public withhold, projection, tripwire) → PRs 4/6/7; the v0.3.12 review items 5/6/8 → PRs 4/4/8.

## Risks

| Risk | Mitigation |
|------|------------|
| The dark window leaks (a channel classified above `internal` mid-Phase-1). | Enforced, not documented: from PR 1 `Config.Validate` rejects any declared level above `internal` (`ErrClassificationAboveDarkWindow`) on both the per-channel field and the DM knob, so the window cannot be opened by an operator reading the schema ahead of the guides. PR 4 removes the guard as it arms the §D gate. |
| The §D gate slows every turn (it sits on the injection hot path). | The gate is a rank comparison over already-loaded rows — no new queries; the recall-latency regression gate (RFC 0029 Phase 1) is watched at PR 4/5. |
| Two schema migrations (channel v11, memory-side) in one release. | Both follow the shipped in-transaction versioned discipline with `internal` backfill; migration tests run on populated stores; the memory-side columns for notes land in PR 3 but stamp only from PR 4 (soundness rule). |
| ISSUE-0106(b) breaks an unknown caller passing `epoch_id`. | Grep-verified: no non-test caller passes a non-`live` epoch (the ISSUE-0106 evidence chain); the endpoint 4xxes with a pointed message for one release. |
