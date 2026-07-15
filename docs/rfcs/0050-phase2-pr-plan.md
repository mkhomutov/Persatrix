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

The whole server side already exists and is correct (Phase 1 PR #643). Like
RFC-0048 Phase 1, this is "mostly a render-over-existing-API problem." **Phase 2
ships zero Go changes** — every PR is in `web/` plus docs. This keeps
each PR reviewable without a Go reviewer and lets the panel land additively
behind the already-default-**off** `config_edit_enabled` toggle — the same
`{enabled, available}` capability contract `create` already ships behind. (The
panel ships *dark* via the **toggle** being off, distinct from `memory_strip`,
which ships dark because it is structurally **unavailable** — `panelAvailable`
returns `false` for it regardless of any toggle.) Two
RFC Phase-2 bullets that *do* need backend work — editable member threshold and
the effective-policy preview — are explicitly **deferred** (see
[Deferred](#deferred-requires-backend-work--out-of-phase-2-scope)), keeping this workstream pure-web.

### Prerequisite + cross-phase dependencies (reconciling with the RFC)

"Web-only" describes the *rendering* work, **not** the full set of conditions for
RFC 0050 to close. Two items in the RFC sit outside these three web PRs and must
be tracked so the plan does not read as "Phase 2 ships → RFC done":

1. **Blocking prerequisite —
   [ISSUE-0103](../issues/ISSUE-0103-first-config-edit-detaches-yaml-seeded-knobs.md)
   (Go, `internal/channels`).** The store-canonical apply path resets every
   *other* non-default knob to fleet default on the **first** edit of a
   YAML-seeded channel (most visibly detaching the escalation chair), with no
   validation warning. It is bounded today — the surface ships dark and only
   `planning` carries a non-default chair — but the RFC
   ([Progress](0050-extensible-channel-configuration.md#progress)) flags it as a
   "routine silent-data-loss footgun once a UI invites operators to edit
   YAML-configured channels," i.e. exactly what PR 2 does. **This must be fixed
   before the panel ships non-dark** (toggle flipped on in any real deployment),
   even though it is not itself web work. It is the one Go change Phase 2 cannot
   pretend away.
2. **Scope reconciliation — editable member threshold.** The RFC's Phase 2
   summary lists editable member thresholds *inside* Phase 2; this plan defers
   them ([Deferred](#deferred-requires-backend-work--out-of-phase-2-scope) item 1) because no
   member-config mutation endpoint exists. That is a deliberate **narrowing** of
   the RFC's Phase 2, not a silent drop — closing RFC 0050 still requires either
   building that backend+web slice or amending the RFC to move member-threshold
   editing out of Phase 2.

Also note Open item 4 from Phase 1 (`interaction_budget_tokens` was store-persisted
but not router-wired, so its inherited value read back `null` — see the
[surface notes](#the-surface-phase-2-renders-over-landed-in-phase-1)) — **resolved
2026-06-15** by the [interaction-budget amendment](0050-amendment-interaction-budget-enforcement.md)
(#657/#658): the budget is now router-held, the server resolves the effective
value (no longer `null`), and the wallet enforces it. The null-handling the panel
shipped (below) is now a harmless no-op for a live server; updating the mocked
panel test that still feeds `null` is a minor follow-up. None of this changes that
the three PRs below were web-only.

## The surface Phase 2 renders over (landed in Phase 1)

Verified against `internal/server/channel_config_handlers.go`,
`channel_types.go`, `ui_config.go`, `ui_handlers.go`.

**`GET /api/v1/channels/{id}/config`** → `channelConfigResponse`: a `revision`
(`int64`) plus eight knobs, each a `{ "value": <bool|int|string|null>, "source":
"channel" | "default" }` pair (`configFieldResponse`). `source` *is* the
provenance signal the panel renders (overridden-on-this-channel vs inherited
fleet default). **`value` is `null` for an *inherited* `interaction_budget_tokens`** —
that knob is not router-held, so the server cannot resolve its inherited fleet
default and returns `value: null, source: "default"` (it echoes the stored value
only when overridden; see `buildChannelConfigResponse`,
`channel_config_handlers.go`). The panel must treat that one `null` as "inherited,
unset," **not** coerce it to `0`. The eight JSON keys:

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
`428` (If-Match missing), `400` (unknown knob / wrong type / unparseable
If-Match), `404` (no channel), and `503` (channel store/router not wired — the
same availability gate `config_edit.available` reflects, so a client that trusts
`available` should not normally hit it, but the client must still degrade rather
than crash).

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
| `web/src/lib/bootstrap.js` `selectPanels()` | spreads `panels[].create` onto the descriptor (no `create` field on the `KNOWN_PANELS` entry — it is purely server-driven) | **missing** — needs the same `panels[].config_edit` spread, and likewise **no** `KNOWN_PANELS` change |
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
1. `bootstrap.js`: in `selectPanels()`, spread the server-reported
   `panels[name]?.config_edit` onto the descriptor — exactly as `create` is
   threaded (`const configEdit = panels[panel.name]?.config_edit; return
   configEdit ? { ...panel, config_edit: configEdit } : panel`). Keep the key
   `config_edit` (snake) so it matches the server JSON and `App.svelte`'s
   `activePanel?.config_edit` access below. Do **not** add a field to the
   `KNOWN_PANELS` entry — `create` adds none either (the descriptor never
   fabricates a capability the server didn't report).
2. `App.svelte`: derive `canConfigEdit = Boolean(activePanel?.config_edit?.enabled
   && activePanel?.config_edit?.available)` and pass it to `ChannelTimeline`.
3. `api.js`: `getChannelConfig(channelID)` → the config response; `patchChannelConfig(channelID, patch, ifMatch)`
   that sends the sparse body + `If-Match` header and maps the full status set
   the surface declares — `403`/`409`/`428`/`400`/`404`/`503` — onto `ApiError`
   with usable messages (the panel branches on `409` for reload-not-overwrite, so
   the status must survive onto `ApiError`, not collapse to a generic message).
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
   (revert sends `null`). **Edge case:** `interaction_budget_tokens` arrives as
   `value: null` when inherited (see surface notes) — render its number input as
   empty/"inherited," not `0`, and only emit it in the patch when the operator
   actually overrides it.
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
   Mark Phase **2**, not the RFC as a whole — ISSUE-0103, the member-threshold
   slice, and Open item 4 (see [Prerequisite + cross-phase
   dependencies](#prerequisite--cross-phase-dependencies-reconciling-with-the-rfc))
   keep RFC 0050 open past this workstream.

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
   member-constrained persona picker. The Phase 1 server rule
   (`validateEscalationChair`, `config_apply.go`) is **three** conjuncts, not one:
   the chair must (a) be a declared member, (b) **not** be an observer
   (`respond: never`), and (c) resolve under `floor_control: ON` (an explicit
   `floor_control: false` in the *same* patch makes the chair inert and is
   rejected). A member-constrained picker satisfies (a)+(b) but **cannot** prevent
   the (c) cross-field failure, so the panel must still surface the resulting
   `400` rather than assume the picker guarantees a valid chair. Picker is
   friendlier but needs the member list in the panel; finalize in PR 2.
2. **Provenance vocabulary.** Server `source` is `"channel"` | `"default"`. Pick
   the user-facing labels ("Overridden on this channel" / "Inherited default")
   in PR 2; no server change.
3. **Number-knob bounds.** Mirror the Phase 1 server validation ranges
   (non-negative ints, etc.) as client-side input constraints for fast feedback;
   the server stays the authority (a bad value still round-trips to a `400`).
