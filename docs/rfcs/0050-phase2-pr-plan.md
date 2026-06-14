# RFC 0050 — PR Implementation Plan (Phase 2 — web-console channel settings panel)

**RFC**: [0050-extensible-channel-configuration.md](0050-extensible-channel-configuration.md)
**Phase 1 plan**: [0050-phase1-pr-plan.md](0050-phase1-pr-plan.md) (landed — PRs #640–#646)
**Created**: 2026-06-14
**Branch prefix**: `feature/rfc0050p2-`
**Target**: `main`
**Merge strategy**: Squash merge per [BRANCHING.md](../BRANCHING.md) (< 500 lines of meaningful change per PR)
**Master plan**: none — targets the v0.3.x line, unscheduled. This plan stands alone.

---

## Overview

RFC 0050 Phase 1 made per-channel governance **operator-editable** end to end —
persisted store-canonical config, a revision-gated apply path, the
`config_edit_enabled` feature toggle, REST `GET`/`PATCH
/api/v1/channels/{id}/config`, and the CLI `channel config` verb. **Phase 2
brings that surface to the web console**: a *channel settings* panel that reads
the effective config and lets an operator override/inherit each governance knob
from the browser, with the same optimistic-concurrency and toggle semantics the
CLI already uses.

### Scoping principle: Phase 2 is web-only (no backend changes)

The whole server side already exists and is correct (Phase 1 PRs #643). Like
RFC-0048 Phase 1, this is "mostly a render-over-existing-API problem." **Phase 2
ships zero Go changes** beyond none — every PR is in `web/` plus docs. This keeps
each PR reviewable without a Go reviewer and lets the panel land additively
behind the already-default-**off** toggle (exactly as `memory_strip` did). Two
RFC Phase-2 bullets that *do* need backend work — editable member threshold and
the effective-policy preview — are explicitly **deferred** (see
[Deferred](#deferred-requires-backend-work)), keeping this workstream pure-web.

## The surface Phase 2 renders over (landed in Phase 1)

Verified against `internal/server/channel_config_handlers.go`,
`channel_types.go`, `ui_config.go`, `ui_handlers.go`.

**`GET /api/v1/channels/{id}/config`** → `channelConfigResponse`: a `revision`
(`int64`) plus eight knobs, each a `{ "value": <bool|int|string>, "source":
"channel" | "default" }` pair (`configFieldResponse`). `source` *is* the
provenance signal the panel renders (overridden-on-this-channel vs inherited
fleet default). The eight JSON keys:

```
floor_control                              salience_max_channel_members
interaction_budget_tokens                  max_replies_per_participant_per_interaction
end_vote_threshold                         end_vote_window
escalation_chair_id                        interaction_idle_timeout_seconds
```

**`PATCH /api/v1/channels/{id}/config`** — sparse `{key: value}` body where an
explicit `null` = unset→inherit and an absent key = leave unchanged. The current
`revision` **must** be sent in the `If-Match` header (bare integer). Status
codes the panel must handle: `403` (toggle off), `409` (revision conflict),
`428` (If-Match missing), `400` (unknown knob / wrong type), `404` (no channel).

**Toggle** — `config_edit_enabled` on the `channel_timeline` panel in
`config/ui.yaml` (default **false**), surfaced in `GET /api/v1/ui/config` as
`panels.channel_timeline.config_edit = { enabled, available }`
(`panelConfigEdit()` in `ui_handlers.go` — `available` iff store **and** router
are wired, mirroring the handler's 503). This is the exact shape of the existing
`create` capability.

## The gap (all in `web/`)

The capability is **not yet threaded into the SPA** — a clean, mechanical gap:

| Layer | `create` (exists) | `config_edit` (Phase 2 adds) |
|-------|-------------------|------------------------------|
| `web/src/lib/bootstrap.js` `selectPanels()` | spreads `panels[].create` onto the descriptor | **missing** — needs the same spread + a `configEdit` field on the `channel_timeline` `KNOWN_PANELS` entry |
| `web/src/App.svelte` | derives `canCreate` from `activePanel.create.{enabled,available}` | **missing** — derive `canConfigEdit`, pass to `ChannelTimeline` |
| `web/src/lib/api.js` | `createChannel()` etc. | **missing** — `getChannelConfig()`, `patchChannelConfig()` (with `If-Match`) |
| `web/src/panels/` | `ChannelMembers.svelte` (nested in `ChannelTimeline`) | **missing** — new `ChannelSettings.svelte`, same nesting |

### Architecture decision: nested panel, mirroring `ChannelMembers`

`ChannelSettings.svelte` is **nested inside `ChannelTimeline.svelte`**, not a new
top-level tab — mirroring `ChannelMembers` (rendered `{#if canCreate &&
selectedChannel && !isDM}`). Governance applies to group channels, so the
settings affordance renders `{#if canConfigEdit && selectedChannel && !isDM}`.
This reuses the existing channel-selection state and refresh pattern
(`onChanged`) and adds no route/tab. Default-off toggle → it ships dark.

## PR breakdown

Phase 2 splits into **3 PRs**, each < 500 lines, web-only.

### PR 1 — Capability threading + API client (plumbing, no visible UI)

**Summary.** Thread `config_edit` from `ui/config` to a `canConfigEdit` prop and
add the two API client functions. No new panel yet, so the console looks
identical (the prop is unused until PR 2).

Deliverables:
1. `bootstrap.js`: add `configEdit` to the `channel_timeline` `KNOWN_PANELS`
   descriptor and spread `panels[name]?.config_edit` in `selectPanels()`, exactly
   as `create` is threaded.
2. `App.svelte`: derive `canConfigEdit = Boolean(activePanel?.config_edit?.enabled
   && activePanel?.config_edit?.available)` and pass it to `ChannelTimeline`.
3. `api.js`: `getChannelConfig(channelID)` → the config response; `patchChannelConfig(channelID, patch, ifMatch)`
   that sends the sparse body + `If-Match` header and maps `409`/`428`/`403` onto
   `ApiError` with usable messages.
4. Tests (`bootstrap.test.js`, `api` tests): `config_edit` threading parity with
   `create`; `patchChannelConfig` sends `If-Match`; `409`/`428` surface as
   `ApiError`.

Dependencies: none (Phase 1 server surface is live).

### PR 2 — Channel settings panel (the hero)

**Summary.** `ChannelSettings.svelte`, nested in `ChannelTimeline`, reads the
effective config and edits each knob with an inherit/override control.

Deliverables:
1. `ChannelSettings.svelte`: on channel select, `getChannelConfig` → render the
   eight knobs. Each row shows the effective `value` and a provenance badge from
   `source` (**overridden** vs **inherited default**). Per-knob control typed by
   knob: checkbox (`floor_control`), number (the int knobs), text/persona-picker
   (`escalation_chair_id`), each with an **override / revert-to-inherit** toggle
   (revert sends `null`).
2. Save: collect only changed keys into a sparse patch, `patchChannelConfig` with
   the loaded `revision` as `If-Match`. On `409`, reload config and surface
   "changed elsewhere — reloaded"; on success, refresh via the existing
   `onChanged` path so the new `revision` is picked up.
3. Mount in `ChannelTimeline` under `{#if canConfigEdit && selectedChannel &&
   !isDM}`, beside `ChannelMembers`.
4. Tests: renders knobs + provenance from a mocked response; editing one knob
   sends the correct single-key sparse PATCH with `If-Match`; revert sends
   `null`; `409` triggers a reload, not a silent overwrite; `403`/disabled path.

Dependencies: PR 1.

### PR 3 — Docs, manual test, closeout

**Summary.** Operator-facing docs + a live web manual test + RFC status bump.

Deliverables:
1. `docs/guides/` (web-console + channels guides): document the settings panel,
   the `config_edit_enabled` toggle, and that it ships off by default.
2. Manual test `docs/manual-tests/MT-CHANNEL-CONFIG-002` (web sibling of the
   Phase 1 CLI arc MT-CHANNEL-CONFIG-001): toggle on, edit a knob in the browser,
   confirm the running channel honors it and that the CLI `channel config get`
   shows the same value (cross-surface single-source-of-truth check, G4).
3. RFC-0050 *Phased Implementation Plan*: mark Phase 2 delivered; link this plan.

Dependencies: PR 2.

## Files touched (by PR)

| PR | Area | Files |
|----|------|-------|
| 1 | web plumbing | `web/src/lib/bootstrap.js`, `web/src/App.svelte`, `web/src/lib/api.js` (+ `bootstrap.test.js`, api tests) |
| 2 | web panel | `web/src/panels/ChannelSettings.svelte` (new), `web/src/panels/ChannelTimeline.svelte` (+ component test) |
| 3 | docs | `docs/guides/web-console*.md`, `docs/guides/channels.md`, `docs/manual-tests/MT-CHANNEL-CONFIG-002.md`, `docs/rfcs/0050-extensible-channel-configuration.md` |

## Test strategy

- **Unit (vitest)**: `selectPanels()` `config_edit` threading; `api.js`
  `If-Match`/`409`/`428` mapping; `ChannelSettings` render-from-response,
  sparse-patch construction (changed-keys-only), revert→`null`, `409`→reload.
- **Build/CI**: `make ui-test` + `make ui` (the `web-console` CI lane, Node 22 /
  x86_64 — unaffected by the arm64 Docker caveat, ISSUE-0104). The Go-only lane
  is untouched since Phase 2 adds no Go.
- **Manual**: `MT-CHANNEL-CONFIG-002` — browser edit honored live + cross-checked
  against CLI `channel config get` (G4 single-source-of-truth).

## Deferred (requires backend work — out of Phase 2 scope)

Two RFC Phase-2 bullets need server changes, so they are **not** in this
web-only workstream; recommend a separate small slice if wanted:

1. **Editable member threshold / disposition.** Member fields
   (`respond`, `threshold`, `salience_gated`) are **read-only** in
   `ChannelMembers.svelte` today, and there is **no** member-config mutation
   endpoint (only add/remove exist; no `PATCH
   /api/v1/channels/{id}/members/{participant_id}`). Making threshold editable is
   a backend+web slice (new endpoint + store write + UI), not a render-over-
   landed-endpoints change. *Recommendation: a focused "Phase 2.5" slice.*
2. **Effective-policy preview** ("what will each member actually do, given
   salience gating + floor control"). Needs runtime salience/floor state not
   currently exposed by any endpoint. *Recommendation: defer until/if an
   operator asks; it overlaps observability (RFC-0030), not config editing.*

## Open items carried into implementation

1. **`escalation_chair_id` control.** Render as a free-text field or a
   member-constrained persona picker (the chair must be a non-observer member —
   a Phase 1 server validation rule). Picker is friendlier but needs the member
   list in the panel; finalize in PR 2.
2. **Provenance vocabulary.** Server `source` is `"channel"` | `"default"`. Pick
   the user-facing labels ("Overridden on this channel" / "Inherited default")
   in PR 2; no server change.
3. **Number-knob bounds.** Mirror the Phase 1 server validation ranges
   (non-negative ints, etc.) as client-side input constraints for fast feedback;
   the server stays the authority (a bad value still round-trips to a `400`).
