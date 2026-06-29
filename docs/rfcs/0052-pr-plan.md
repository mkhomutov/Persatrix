# RFC 0052 — PR Implementation Plan (Phases 1–4 — v0.3.11 scope)

**RFC**: [0052-autonomous-agent-channels.md](0052-autonomous-agent-channels.md)
**Created**: 2026-06-28
**Branch prefix**: `feature/v0311-rfc0052-`
**Target**: `main`
**Merge strategy**: Squash merge per [BRANCHING.md](../BRANCHING.md)
**Master plan**: [v0.3.11-plan.md Phase 1 — Implement RFC 0052 + RFC 0053](../v0.3.11-plan.md#phase-1--implement-rfc-0052--rfc-0053)

---

## Overview

RFC 0052 makes a channel **run a productive discussion with no human in the loop**: it convenes a roster of personas on a topic, and the discussion converges, terminates, and yields a readable synthesis — exactly the v0.3.8 brainstorm, with the human removed from every step. It is **assembly of shipped seams** (RFC 0011 channels, RFC 0030 governance/chair/end-of-interaction, RFC 0024 `InboundEventWake` + timers, RFC 0050 config, RFC 0051 reasoning, RFC 0020 interaction summary, RFC 0023 leasing) **plus four new pieces** ([RFC §B–§E](0052-autonomous-agent-channels.md#design--implementation)): self-convening (assembled from the publish path) and three mechanisms that each *extend a shipped governance invariant* — anti-collapse cadence, a mandatory cost cap with a reserved synthesis allowance + deterministic bounded close, and a standing-schedule aggregate bound + timer-wiring seam.

This plan covers **all four phases** ([master-plan scope lock](../v0.3.11-plan.md#scope-decisions-locked-at-plan-authoring-time-2026-06-28)) across **9 PRs**:

- **Phase 1 — convene + bounded one-shot brainstorm (PR 1–5).** A channel that opens itself on a topic, runs to a bound, and synthesizes — operator-creatable from CLI **and** web, with the mandatory cap enforced from the first PR. PR 1 lands the `autonomous` config **backend** (Go validate/apply/persist + the RFC 0050 REST PATCH/GET layer; dark); **PR 2 the CLI + web config surfaces** so an operator can author/edit an autonomous channel without hand-editing YAML; PR 3 the self-convening opening turn + the **convene action across all three surfaces** (CLI verb, REST endpoint, web button); PR 4 the deterministic bounded close + the **roster-scaled (`1 + N`)** wallet synthesis reserve + the OQ #6 summary-metering edit + the interaction-closed wallet eviction + mandatory synthesis-on-close; PR 5 the Phase-1 integration suite + `MT-AUTONOMOUS-001`.
- **Phase 2 — anti-collapse cadence (PR 6).** The convener per-agenda-item escalation ration (generalizing the shipped CE5 one-shot ration) with the per-item loop guard preserved, scoped to `autonomous.enabled`; `MT-AUTONOMOUS-002` + the human-channel regression.
- **Phase 3 — standing / scheduled convening (PR 7).** `autonomous.schedule` over RFC 0024 timers via the config-round-trip seam + the mandatory standing **aggregate** bound + `validate` gate + the web convening-count/aggregate-bound readout; `MT-AUTONOMOUS-003`.
- **Phase 4 — flagship demo (PR 8–9).** PR 8 the offline `make demo-autonomous` (curated roster, mock provider, zero keys); PR 9 the four-vendor cross-vendor headline blueprint + `MT-AUTONOMOUS-MULTIPROVIDER-001` (live, depends on RFC 0053) + RFC/ROADMAP/CHANGELOG closeout.

> **The CLI + web surfaces are first-class, not an afterthought.** RFC 0052 is an *operator-editable* feature built on the RFC 0050 surface, exactly as RFC 0051's `reasoning` knob was — so the `autonomous` block must be creatable/editable from the `persatrix channel config` CLI **and** the web Channel-settings panel, and the **convene** action must be reachable from CLI, REST, and the web (RFC 0052 [§B](0052-autonomous-agent-channels.md#b-self-convening--starting-without-a-human-turn) names all three convene triggers). The RFC's [Files-Touched table](0052-autonomous-agent-channels.md#files-touched-estimated) under-counted this — it omitted a `web/` row and listed only the `convene` verb for CLI — so this plan dedicates **PR 2 (config surfaces)** and the surface legs of **PR 3 (convene action)** to it, following the RFC 0051 PR-4/PR-5 backend-then-surfaces split. Survey: ~785 net new surface lines (CLI ~290, web ~40 + a child component, server ~455).

**Hard prerequisites (all shipped):** RFC 0011 channels (v0.3.0 ✅), RFC 0030 chair / relevance / end-of-interaction / chair-stall-escalation (v0.3.6–0.3.8 ✅), RFC 0024 `InboundEventWake` + timers (v0.3.3 ✅), RFC 0050 config surface + REST PATCH/GET + CLI `channel config` + web Channel-settings panel + interaction-budget amendment (v0.3.8 ✅), RFC 0051 reasoning + its nested-knob CLI/web precedent (v0.3.10 ✅), RFC 0020 interaction summary (v0.3.0 ✅), RFC 0023 leasing (v0.3.2 ✅), RFC 0033 alias layer (v0.3.4 ✅), RFC 0048 web console (v0.3.6 ✅).

### Open-question resolutions locked at plan-authoring time

Locked with the maintainer at plan opening (see [master plan §Scope decisions](../v0.3.11-plan.md#scope-decisions-locked-at-plan-authoring-time-2026-06-28)). Two **reverse** the RFC's own lean and are recorded in the [RFC Open Questions](0052-autonomous-agent-channels.md#open-questions) §Status as well:

- **[OQ #1](0052-autonomous-agent-channels.md#open-questions) — distinct `convener` role** *(reverses the RFC's "lean chair = convener")*. The **convener** (a new `autonomous.convener` agent id) owns the agenda lifecycle — authors the opening turn and **advances the agenda** on a stall; the **chair** (the RFC 0050 `escalation_chair_id`) keeps its shipped v0.3.8 role (stall-escalation → propose synthesis → end-vote). **Consequence for PR 6:** the anti-collapse per-agenda-item ration lives on the **convener** path, *not* the chair path — the chair's CE5 one-shot ration is untouched.
- **[OQ #2](0052-autonomous-agent-channels.md#open-questions) — anti-collapse scoped to `autonomous.enabled`** (not a general `liveness` knob). The convener ration is gated by `autonomous.enabled`; human channels keep the shipped bias-to-silence + CE5 guard. PR 6 carries the human-channel regression that proves it.
- **[OQ #6](0052-autonomous-agent-channels.md#open-questions) — meter the closing summary; reserve scales with the roster** *(takes the heaviest option)*. Verified that the RFC 0020 close summary is **currently unmetered** — it passes no `cause`/`interaction_id` so it bypasses the wallet lease ([`agents/llm_client.py:212`](../../agents/llm_client.py), [`agents/persona_runtime/summarize_close.py:172`](../../agents/persona_runtime/summarize_close.py)). PR 4 brings the **autonomous-close** summary under a lease (stamped with the interaction's `interaction_id`) so it counts toward the cap. **The close summary is authored per-agent** ([`close_path.py`](../../agents/persona_runtime/close_path.py) spawns one `finalize_closed_interaction` per `agent_id`), so an N-persona roster issues **N** metered summary calls on the *shared* per-interaction budget — the reserve is therefore sized for **`1 + N`** (chair synthesis turn + one summary per persona), **not** the fixed "two" an earlier framing assumed.
- **[OQ #3](0052-autonomous-agent-channels.md#open-questions) / [#4](0052-autonomous-agent-channels.md#open-questions) / [#5](0052-autonomous-agent-channels.md#open-questions)** — free-text `goal` for v0.3.x; Phase 3 ships fixed/rotating topic + the **config-round-trip** timer seam (not the deferred runtime `RegisterTimer` API; topic *queue* is a follow-up); conservative defaults + an OQ #5 calibration tracked-issue.

### Sequencing

**Recommended merge order**: **PR 1 → PR 2 → PR 3 → PR 4 → PR 5 → PR 6 → PR 7 → PR 8 → PR 9.**

Phase 1 (PR 1–5) is a near-strict chain: the config backend (PR 1) precedes the operator surfaces (PR 2) and self-convening (PR 3); the bounded close + reserve (PR 4) is exercised by the integration suite (PR 5). **PR 2 (CLI + web config surfaces) may merge any time after PR 1** — it depends only on the REST layer, not on convening, so it can land in parallel with PR 3/PR 4 if reviewer bandwidth allows. Phase 2 (PR 6, anti-collapse) depends on the convener path (PR 3) + the bounded close (PR 4). Phase 3 (PR 7, standing) depends on the cap + convene path + the web config surface (it adds an aggregate-bound readout). Phase 4 PR 8 (offline demo) depends on Phase 1–2; **PR 9 (four-vendor headline) additionally depends on RFC 0053 PR 1–2** and is **cuttable** — if RFC 0053 slips, PR 9's four-vendor leg tracks into v0.3.12 and PR 8's offline demo carries the headline.

### File-size constraints (verified at plan authoring, cap = 500 per [`file_size.py --strict`](../../scripts/checks/file_size.py))

| File | Lines | Headroom | Routing |
|------|-------|----------|---------|
| [`internal/wallet/wallet.go`](../../internal/wallet/wallet.go) | **499** | **1** | **At the cap.** The PR 4 roster-scaled (`1 + N`) **synthesis-reserve accounting** + the interaction-closed eviction land in [`internal/wallet/interaction_budget.go`](../../internal/wallet/interaction_budget.go) (155, ample) or a new `synthesis_reserve.go`, **not** `wallet.go`. |
| [`internal/channels/channels.go`](../../internal/channels/channels.go) | **499** | **1** | **At the cap.** The PR 3 convene publish logic routes through [`router.go`](../../internal/channels/router.go) / a new `convene.go`; do not grow `channels.go`. |
| [`internal/channels/config.go`](../../internal/channels/config.go) | **500** (post PR 1) | **0** | **At the cap after PR 1** (the `Autonomous` struct field + LoadConfig normalize line). Any later `ChannelConfig` field/loader logic routes to a sibling (e.g. `config_autonomous.go`); do not grow `config.go`. |
| [`cli/src/commands/channel_config.rs`](../../cli/src/commands/channel_config.rs) | **500** | **0** | **At the cap — must split.** PR 2's `autonomous.*` nested knobs go in a **new `channel_config_autonomous.rs`**, mirroring the [`channel_config_reasoning.rs`](../../cli/src/commands/channel_config_reasoning.rs) (165) split RFC 0051 used for its first nested knob. |
| [`web/src/panels/ChannelSettings.svelte`](../../web/src/panels/ChannelSettings.svelte) | **483** | **17** | **Tight.** PR 2's ~6 autonomous fields + control blocks + the PR 3 Convene button would bust the cap — extract the autonomous section into a **new child component** (`web/src/panels/AutonomousSettings.svelte`) consumed by `ChannelSettings.svelte`. |
| [`cli/src/commands/channel_dispatch.rs`](../../cli/src/commands/channel_dispatch.rs) | 484 | 16 | PR 3 adds one `Convene` action variant + dispatch arm — a few lines; the handler lives in a new `channel_convene.rs`. |
| [`internal/server/channel_config_handlers.go`](../../internal/server/channel_config_handlers.go) | 468 | 32 | PR 1 adds one `case "autonomous"` dispatch arm; the merge/response logic lives in a new `channel_config_autonomous.go` (mirrors [`channel_config_reasoning.go`](../../internal/server/channel_config_reasoning.go) 161). |
| [`web/src/lib/api.js`](../../web/src/lib/api.js) | 470 | 30 | PR 3 adds a `conveneChannel()` wrapper; the autonomous config fields ride the existing `getChannelConfig`/`patchChannelConfig`. |
| [`internal/server/channel_types.go`](../../internal/server/channel_types.go) | 315 | ample | PR 1 adds an `autonomousConfigResponse` struct. |
| [`internal/channels/config_validate.go`](../../internal/channels/config_validate.go) / [`chair_escalation.go`](../../internal/channels/chair_escalation.go) (411) | — | — | PR 1 cap-required + PR 7 aggregate-bound `validate` gates; PR 6 convener ration in a **new `convener_cadence.go`** (keeps the shipped CE5 path readable). |
| New: `channel_config_autonomous.{rs,go}`, `AutonomousSettings.svelte`, `channel_convene.rs`, `channel_convene_handlers.go`, `convene.go`, `convener.py`, `convener_cadence.go`, `synthesis_reserve.go` | — | — | Own modules so the at-cap files stay under cap. |

---

## Dependency Graph

```
RFC 0011/0030/0024/0050/0051/0020/0023/0033/0048 (all shipped)        ← HARD PREREQUISITES
   │
   ├── PR 1 (Phase 1a: `autonomous` config BACKEND — block on the RFC 0050 surface + schema;
   │     │   validate REJECTS autonomous.enabled without interaction_budget_tokens;
   │     │   REST PATCH/GET layer [channel_types.go + channel_config_handlers.go +
   │     │   new channel_config_autonomous.go])   [dark — no convene, no surfaces]
   │     ├───────────────────────────────────────────────────────────┐
   │     ↓                                                             ↓
   ├── PR 2 (Phase 1b: CLI + WEB config SURFACES — new channel_config_autonomous.rs
   │     │   [mirrors reasoning.rs split] + new AutonomousSettings.svelte child component;
   │     │   operator creates/edits an autonomous channel from CLI + web)  ← any time after PR 1
   │     ↓
   ├── PR 3 (Phase 1c: self-convening — convener opening turn under fresh interaction_id;
   │     │   operator topic/agenda/goal wrapped in RFC 0009 <external_data>;
   │     │   CONVENE ACTION across 3 surfaces: `persatrix channel convene` verb +
   │     │   POST /api/v1/channels/{id}/convene + web "Convene" button [conveneChannel()])
   │     ↓
   ├── PR 4 (Phase 1d: deterministic bounded close [max_rounds / soft-budget / agenda-exhausted];
   │     │   roster-scaled (1+N) wallet synthesis reserve [new accounting] + interaction-closed eviction;
   │     │   OQ #6 meter the autonomous-close
   │     │   summary; mandatory synthesis-on-close — chair synthesis turn + interaction summary)
   │     ↓
   ├── PR 5 (Phase 1e: integration suite + MT-AUTONOMOUS-001 — convene→converge→terminate→
   │     │   both artifacts; no-runaway leg; close-by-budget leg honours the 1+N roster-scaled reserve)
   │     ↓
   ├── PR 6 (Phase 2: convener per-agenda-item escalation ration [generalizes CE5, per-item loop
   │     │   guard preserved, autonomous-scoped] + best-effort liveness target; MT-AUTONOMOUS-002;
   │     │   HUMAN-CHANNEL REGRESSION proving CE5 one-shot ration unchanged off the autonomous path)
   │     ↓
   ├── PR 7 (Phase 3: autonomous.schedule over RFC 0024 timers via config-round-trip seam;
   │     │   mandatory standing aggregate bound [max_convenings / standing cost budget] + validate gate
   │     │   + web convening-count/aggregate-bound readout; MT-AUTONOMOUS-003 — stops at the bound)
   │     ↓
   ├── PR 8 (Phase 4a: `make demo-autonomous` + curated roster blueprint; OFFLINE face = mock
   │     │   provider, zero keys; offline smoke produces a non-empty synthesis)
   │     ↓
   └── PR 9 (Phase 4b: four-vendor headline blueprint [Anthropic + OpenAI + Gemini + watsonx];
             MT-AUTONOMOUS-MULTIPROVIDER-001 [live, all four keyed]; RFC + ROADMAP + CHANGELOG closeout)
                ▲
                └── depends on RFC 0053 PR 1–2 (Gemini + watsonx) — CUTTABLE: if 0053 slips,
                    PR 9's four-vendor leg tracks to v0.3.12; PR 8 carries the offline headline
```

---

## Deep-review follow-ups (carried from PR 1)

Items surfaced by the PR 1 deep review (commit `700fa72`) that are **deliberately deferred** to the PR where they become load-bearing, rather than enforced early in the dark backend. Each names its owning PR so it is not lost.

> **Second-pass deep review (PR 1, in-PR fixes).** A follow-up review hardened two more gaps *inside* PR 1: (a) **group-only arming** — the apply path now rejects an `autonomous.enabled` DM/thread (`ErrAutonomousNotGroup`); autonomy is an open-floor group concept (`ResolveAutonomous` already seeds groups only), so an armed non-group channel was un-convenable-by-construction yet validation accepted it; and (b) the **first-edit baseline drop** now keys on "armed AND un-convenable", so an armed rung with *no* convener (not only a *drifted* one) is dropped rather than freezing a block that would 400 an unrelated edit. The residual item below is the one not fixed in-PR.

- **[PR 3 / future RFC 0050 hardening] Store-canonical convener (and chair) drift locks unrelated config edits.** On a channel at revision > 0, the merge base is the stored blob, which keeps naming the convener even after that member leaves the roster (`RemoveMember` does not touch the config blob), so `validateAutonomousConvener` rejects **every** subsequent edit until the operator disarms / re-points / clears the convener. PR 1 **pins and documents** this as deliberate and **escalation-chair-symmetric** (the chair has the identical lockout — it is *not* softened convener-only, which would create a worse asymmetry) and **always recoverable** (`TestChannelConfig_AutonomousConvenerDriftLocksThenRecovers`). The real fix is symmetric across both roles — either guard `RemoveMember` against orphaning a convener/chair, or make the apply path tolerate a drifted role it is not changing — and belongs with a chair-aware change, not this config-backend PR.
- **[PR 3] Confirm the convene path's receiver-gate semantics, then finalize the convener-disposition rule.** PR 1 hardened the convener gate to reject a non-member, an **observer (`respond: never`)**, and a chair-collision — mirroring the escalation chair, on the assumption that a convene wake passes the same receiver gate that suppresses an observer *before any LLM*. PR 3 implements convening, so it must **confirm that assumption**: if the convene wake is gated, the observer rejection is load-bearing (an observer convener would be a silent dispatch failure on an unattended channel — the runaway-class failure the safety contract exists to catch); if convening bypasses the gate (a system-initiated seed publish), keep the rejection as defense-in-depth and record the rationale. Also decide whether a `when_mentioned`/`addressed` convener is acceptable (the chair allows it — the forced-turn marker lifts the gate) or whether the convener must be open-floor (`participant`/`chair`).
- **[PR 3] Defensive max-length bound on operator free-text at the injection point.** PR 1 persists `topic` / `goal` / `agenda[]` with **no length bound** (only the 64-item agenda cap and a non-blank, whitespace-trimmed item check). PR 3 wraps these in the RFC 0009 `<external_data>` envelope before injecting them into the convener prompt — the natural seam to add a max-length bound, since that is the one genuinely new injection surface. There is **no codebase precedent for capping prose fields** (the only schema `maxLength`s are on opaque tokens like `interaction_id`), so the bound and its rationale are a PR 3 decision, not a PR 1 magic number.
- **[PR 4] Require a chair (or a designated synthesizer) on an armed channel.** PR 1 validates `convener != escalation_chair_id` but does **not** require a chair to exist — an armed channel with no `escalation_chair_id` currently validates, which makes the distinctness rule vacuous in that case. RFC [§D](0052-autonomous-agent-channels.md#d-termination-and-synthesis--always-produce-an-artifact) "always produce an artifact" is the **chair's** synthesis-on-close job, which lands in PR 4. When PR 4 ships the bounded close + synthesis reserve, add the `validate` gate that an `autonomous.enabled` channel declares the role that authors the mandatory synthesis turn — at which point convener/chair distinctness stops being vacuous.
- **[PR 3 / PR 7] The mandatory cap is per-interaction, not aggregate** *(already in scope — noted for traceability)*. PR 1's "mandatory cap" bounds a single interaction; the opening turn is structurally uncapped ([PR 3](#key-implementation-details-2)) and a standing schedule needs an **aggregate** bound (PR 7). PR 1 also rejects *clearing*/uncapping an armed channel's budget over REST (the apply path cannot see the fleet default, so it fails safe) — a deliberate, tested asymmetry with the YAML load path, not a bug.

---

## PR Sequence

### PR 1: `feature/v0311-rfc0052-config-backend` — Phase 1a: `autonomous` config backend + mandatory-cap gate (dark)

**Depends on**: RFC 0050 config surface + REST layer (shipped).
**Purpose**: Land the `autonomous` block on the channel config surface, make an uncapped autonomous channel **un-creatable** (`validate` rejects `autonomous.enabled` without `interaction_budget_tokens`), and expose it over the RFC 0050 REST PATCH/GET layer so the CLI + web surfaces in PR 2 have something to read/write. Ships dark: no convene path yet, so the block is inert until PR 3.

#### Scope

| File | Change |
|------|--------|
| [`internal/channels/config_validate.go`](../../internal/channels/config_validate.go) | `validate` rejects `autonomous.enabled: true` without a positive `interaction_budget_tokens`; validates `convener` is a roster member id (distinct from the chair `escalation_chair_id`, [OQ #1](0052-autonomous-agent-channels.md#open-questions)). Standing aggregate-bound gate deferred to PR 7. |
| [`internal/channels/config_apply.go`](../../internal/channels/config_apply.go) + config schema | The sparse `autonomous` block (`enabled`, `topic`, `agenda[]`, `convener`, `goal`, `max_rounds`, `interaction_budget_tokens`) — apply/persist following the RFC 0050 `config_reasoning.go` precedent. Conservative defaults ([OQ #5](0052-autonomous-agent-channels.md#open-questions)). |
| New [`internal/server/channel_config_autonomous.go`](../../internal/server/) | `mergeAutonomousPatch()` + `autonomousResponse()` — the nested-block merge/response, mirroring [`channel_config_reasoning.go`](../../internal/server/channel_config_reasoning.go). |
| [`internal/server/channel_config_handlers.go`](../../internal/server/channel_config_handlers.go) (468) + [`channel_types.go`](../../internal/server/channel_types.go) | One `case "autonomous"` dispatch arm in `mergeConfigPatch`; an `autonomousConfigResponse` struct (value/source cells, If-Match-guarded). |
| `config/` channel schema + example | JSON-schema entry + an example autonomous channel. |
| Go tests | `validate` rejects uncapped autonomy; accepts a capped one; the REST PATCH/GET round-trips the sparse block with revision guard. |

#### Key implementation details

- **Cap-required is a safety invariant** ([RFC §Security](0052-autonomous-agent-channels.md#security-considerations)) — the rejection is unconditional, asserted by a dedicated test, the first line of the no-runaway defense.
- **Distinct convener + chair fields** ([OQ #1](0052-autonomous-agent-channels.md#open-questions)) — `autonomous.convener` is new; the chair reuses `escalation_chair_id`.
- **Dark** — without the PR 3 convene path the block changes no runtime behaviour; the REST layer just persists/returns it.

#### PR checklist

- [ ] `go test ./internal/channels/... ./internal/server/... -race`; `go vet` clean.
- [ ] `validate` rejects `autonomous.enabled` without a cap (dedicated test).
- [ ] REST PATCH/GET round-trips the autonomous block (If-Match revision guard); example channel parses; `make validate` green.
- [ ] RFC 0052 Master-Index note `📋 Proposed → 🚧 Implementing` already applied by the planning PR; [v0.3.11-plan row 1](../v0.3.11-plan.md#master-progress-overview) → 🔄 In progress.

---

### PR 2: `feature/v0311-rfc0052-config-surfaces` — Phase 1b: CLI + web config surfaces

**Depends on**: PR 1 merged (the REST layer). May land in parallel with PR 3/PR 4.
**Purpose**: Let an operator **create and edit** an autonomous channel from the `persatrix channel config` CLI **and** the web Channel-settings panel — not only by hand-editing config-as-code YAML. This is the RFC 0050 operator-editable contract applied to the new block, exactly as RFC 0051 PR 5 surfaced its `reasoning` knob.

#### Scope

| File | Change |
|------|--------|
| New [`cli/src/commands/channel_config_autonomous.rs`](../../cli/src/commands/) | The `autonomous.*` nested knobs (dotted-key get/set: `autonomous.enabled`, `.topic`, `.agenda`, `.convener`, `.goal`, `.max_rounds`, `.interaction_budget_tokens`), an `AutonomousConfigView`, `nest_dotted`/enum coercers — mirroring [`channel_config_reasoning.rs`](../../cli/src/commands/channel_config_reasoning.rs). **Extracted to its own file because [`channel_config.rs`](../../cli/src/commands/channel_config.rs) is at the 500 cap.** |
| [`cli/src/commands/channel_config.rs`](../../cli/src/commands/channel_config.rs) | Register the `autonomous` nested knob (one entry), delegating to the new file (no net growth at the cap). |
| New [`web/src/panels/AutonomousSettings.svelte`](../../web/src/panels/) | The autonomous config section — a **child component** of `ChannelSettings.svelte` (extracted because that panel is at 483/500): the ~6 fields with bool/int/text/enum controls, reusing the existing `fieldFor`/`setBody` dotted-key handling + the PATCH/If-Match save flow. |
| [`web/src/panels/ChannelSettings.svelte`](../../web/src/panels/ChannelSettings.svelte) | Mount `AutonomousSettings` (a few lines); gated by a `config/ui.yaml` toggle if the panel is feature-sliced. |
| Tests | CLI `cargo test` round-trips `autonomous.*` set/get/unset; Vitest covers the autonomous section render + save. |

#### Key implementation details

- **Follow the nested-knob precedent exactly** — RFC 0051's `reasoning.*` is the template on both surfaces (CLI split file; web dotted-key controls). No new pattern is invented.
- **The cap forces extraction on both surfaces** — `channel_config.rs` (CLI) and `ChannelSettings.svelte` (web) are both at/near the cap, so the autonomous surface is a new file/component, not an inline add.

#### PR checklist

- [ ] `cargo test` (CLI) + `npm run test` (Vitest, web) green; `clippy` clean.
- [ ] An operator can set every `autonomous.*` field from CLI **and** web; the edit round-trips through the REST PATCH.
- [ ] `channel_config.rs` (500) + `ChannelSettings.svelte` (483) gain no net lines past the cap (extraction verified).

---

### PR 3: `feature/v0311-rfc0052-convene` — Phase 1c: Self-convening + convene action (CLI + REST + web)

**Depends on**: PR 1 merged (PR 2 not required — convene works on a YAML-or-API-configured channel).
**Purpose**: The convener authors the **opening turn** under a fresh `interaction_id`, with no human message — from which the existing `InboundEventWake` chain carries the discussion. Expose the convene action on **all three** RFC 0052 [§B](0052-autonomous-agent-channels.md#b-self-convening--starting-without-a-human-turn) triggers (CLI / REST / web; the timer trigger is PR 7).

#### Scope

| File | Change |
|------|--------|
| New `agents/persona_runtime/convener.py` | Convener opening-turn authoring: compose the seed turn (topic + first agenda item) as a normal channel publish stamped with a fresh `interaction_id` (RFC 0030 producer). **Operator-supplied `topic`/`agenda`/`goal` wrapped in the RFC 0009 `<external_data>` envelope** before injection ([RFC §Security](0052-autonomous-agent-channels.md#security-considerations)) — the one genuinely new injection surface. |
| [`agents/persona_runtime/action_loop.py`](../../agents/persona_runtime/action_loop.py) (495) | A **one-line call** into `convener.py` on a convene wake; no inline authoring. |
| New `internal/channels/convene.go` + [`router.go`](../../internal/channels/router.go) | The convene publish logic — a thin wrapper over the existing publish path (no new transport, no new wake type, no new store table). |
| New `internal/server/channel_convene_handlers.go` | `POST /api/v1/channels/{id}/convene` REST endpoint (the surface the CLI + web call). |
| New [`cli/src/commands/channel_convene.rs`](../../cli/src/commands/) + [`channel_dispatch.rs`](../../cli/src/commands/channel_dispatch.rs) (484) | `persatrix channel convene <id>` verb → POST `/convene`; one dispatch variant. |
| [`web/src/panels/ChannelSettings.svelte`](../../web/src/panels/ChannelSettings.svelte) (or `AutonomousSettings.svelte`) + [`web/src/lib/api.js`](../../web/src/lib/api.js) (470) | A **"Convene" action button** (the first per-channel action button in the panel) + a `conveneChannel()` API wrapper. A minimal "convening…" indicator may reuse the existing RFC 0048 activity poll. |
| Tests | Convener authors **exactly one** opening turn under a fresh `interaction_id`; the `<external_data>` wrap is present; convene reachable from CLI, REST, and web; no human turn required. |

#### Key implementation details

- **Convening = "author the seed turn under a fresh interaction id"** ([RFC §B](0052-autonomous-agent-channels.md#b-self-convening--starting-without-a-human-turn)) — reuses the publish path end-to-end; the three surfaces are thin wrappers over the one endpoint.
- **First action button in the web panel** — `ChannelSettings.svelte` has only a "Save settings" submit today; "Convene" is the first per-channel *action*. Keep it a small affordance (button → `conveneChannel()` → toast), not a new sub-panel.
- **The `<external_data>` wrap is mandatory** — operator config is a distinct trust class from persona-authored content. This is also the seam to add the deferred **max-length bound** on `topic`/`goal`/`agenda[]` (see [Deep-review follow-ups](#deep-review-follow-ups-carried-from-pr-1)).
- **Confirm the convener gate, then finalize the disposition rule** — PR 1 rejects an observer (`respond: never`) convener on the assumption the convene wake is receiver-gated; PR 3 must verify that and decide whether a `when_mentioned` convener is acceptable ([Deep-review follow-ups](#deep-review-follow-ups-carried-from-pr-1)).
- **The opening turn resolves *uncapped*** ([RFC §B](0052-autonomous-agent-channels.md#b-self-convening--starting-without-a-human-turn)) — the wallet snapshots the per-interaction cap at first commit, so the lease that *produces* the opening message predates the snapshot and is uncapped ([`interaction_budget.go`](../../internal/wallet/interaction_budget.go) `resolveInteractionBudget`). For a standing channel that is one uncapped opening turn *per convening*. PR 3 either documents this (the Layer-0 depth cap is the always-on net + the §E aggregate bound limits the count) or has the convener's opening lease carry the channel's resolved cap explicitly.

#### PR checklist

- [ ] `pytest agents/tests/test_convener.py -q`; `cargo test` (CLI); `npm run test` (web); `go test ./internal/channels/... ./internal/server/... -race`.
- [ ] Convener authors exactly one opening turn; fresh `interaction_id`; `<external_data>` wrap asserted.
- [ ] The opening-turn cap behaviour is settled (documented-uncapped or explicitly-capped), not left implicit.
- [ ] `persatrix channel convene`, `POST /convene`, and the web button all trigger a convene against the local orchestrator.
- [ ] No new wake type / transport / store table (assembly only).

---

### PR 4: `feature/v0311-rfc0052-bounded-close` — Phase 1d: Deterministic bounded close + roster-scaled synthesis reserve

**Depends on**: PR 3 merged.
**Purpose**: Guarantee an autonomous interaction **terminates** and **always leaves both artifacts**, even on a budget-exhausted close — the part of [RFC §D](0052-autonomous-agent-channels.md#d-termination-and-synthesis--always-produce-an-artifact) that is not pure reuse.

#### Scope

| File | Change |
|------|--------|
| New `internal/channels/convene.go` (or a sibling) | The **deterministic bounded-close trigger** — fires on agenda-exhausted *or* `max_rounds` *or* a **soft** budget threshold; dispatches the chair synthesis turn and *then* closes the interaction (respecting RFC 0030 CE4 — the chair still cannot close itself). This is what finally enforces `max_rounds`. **It also emits the wallet's interaction-closed eviction** (next row). |
| New `internal/wallet/synthesis_reserve.go` (sibling of [`interaction_budget.go`](../../internal/wallet/interaction_budget.go)) | The **roster-scaled synthesis reserve** — new wallet accounting (no shipped analog): split the cap so the discussion is bounded by `interaction_budget_tokens` and a reserve is held back for the close path. The **soft** threshold trips synthesize-and-close *before* the hard cap denies leases. Sized for **`1 + N` close-path calls** — the chair turn **plus one RFC 0020 summary per participating persona** (the close summary is authored per-agent — see the `summarize_close.py` row), **not** a fixed two. **Plus an interaction-closed eviction** of the wallet's `interactionTokens` residue (the shipped wallet never prunes a capped interaction that settled non-zero spend — [`interaction_budget.go`](../../internal/wallet/interaction_budget.go) "nothing currently evicts it" — which would leak one map entry per standing convening; PR 7). **`wallet.go` (499) gains no net lines.** |
| [`agents/persona_runtime/summarize_close.py`](../../agents/persona_runtime/summarize_close.py) (480) | **OQ #6 metering edit** — on the **autonomous** close path, thread `cause` + the interaction's `interaction_id` into the existing `create_message` call ([line 172](../../agents/persona_runtime/summarize_close.py)) so the summary is leased and counts toward the cap. **This file is the per-agent close path ([`close_path.py`](../../agents/persona_runtime/close_path.py) spawns one `finalize_closed_interaction` per `agent_id`), so the edit meters every participating persona's summary** — hence the reserve is sized for `1 + N`. Minimal edit; human-channel close unchanged. |
| `agents/` chair synthesis turn | Mandatory goal-directed synthesis turn against `autonomous.goal`, drawn from the reserve. |
| [`internal/channels/config_validate.go`](../../internal/channels/config_validate.go) | **`validate` gate: an `autonomous.enabled` channel must declare a chair** (the role that authors the mandatory synthesis turn). Deferred from PR 1 (which validates only `convener != escalation_chair_id`); enforced here, where the chair becomes load-bearing — see [Deep-review follow-ups](#deep-review-follow-ups-carried-from-pr-1). |
| Tests | Bounded close fires on each trigger; reserve covers `1 + N` calls on a **≥2-persona** roster; close-by-budget unit test proves the chair turn + every persona's summary lease are honoured; the closed-interaction eviction drops the wallet residue; **`validate` rejects an armed channel with no chair**; human close byte-for-byte unchanged. |

#### PR checklist

- [ ] `go test ./internal/wallet/... ./internal/channels/... -race -cover`; `pytest agents/tests/ -q`.
- [ ] `wallet.go` net line delta = 0 (reserve in a sibling file).
- [ ] Reserve sized for `1 + N` (chair + one summary per persona); close-by-budget test on a ≥2-persona roster proves every lease honoured.
- [ ] The bounded close evicts the interaction's wallet `interactionTokens` entry (no residue leak).
- [ ] Human-channel summarization path byte-for-byte unchanged (metering is autonomous-only).

---

### PR 5: `feature/v0311-rfc0052-phase1-mt` — Phase 1e: Acceptance suite + `MT-AUTONOMOUS-001`

**Depends on**: PR 4 merged.
**Purpose**: Prove the Phase-1 contract end-to-end on the mock provider, then live.

#### Scope

| File | Change |
|------|--------|
| `internal/channels/` + `agents/tests/` integration | Full convene→converge→terminate→synthesis cycle on the mock provider, **zero human turns**, spend ≤ cap; the **no-runaway leg** (turns + tokens bounded under an adversarial roster); the **close-by-budget leg** (on a **≥2-persona** roster) asserting **both** artifacts are still produced (chair synthesis turn + a real RFC 0020 summary **for each persona**, not the `[interaction summary unavailable]` placeholder — exercising the `1 + N` reserve). |
| `docs/manual-tests/MT-AUTONOMOUS-001.md` | One-shot brainstorm, live provider — converges + synthesizes, no human; convened from the CLI and (smoke) the web button. |

#### PR checklist

- [ ] Integration suite green on mock; no-runaway + close-by-budget legs assert their invariants.
- [ ] `MT-AUTONOMOUS-001` documented + dry-run on mock (live run is master-plan Phase 3).
- [ ] CHANGELOG `[0.3.11]` entry extended with the Phase-1 acceptance (seeded at PR 1 merge per [ROADMAP Hygiene](#roadmap-hygiene); this PR adds the convene→synthesize close).

---

### PR 6: `feature/v0311-rfc0052-anti-collapse` — Phase 2: Anti-collapse cadence (convener, autonomous-scoped)

**Depends on**: PR 5 merged.
**Purpose**: Supply the counter-pressure that keeps a human-free discussion alive without un-doing the realism arc's bias-to-silence — the design heart of the RFC ([§C](0052-autonomous-agent-channels.md#c-the-central-tension--anti-collapse-cadence)).

#### Scope

| File | Change |
|------|--------|
| New [`internal/channels/`](../../internal/channels/) `convener_cadence.go` | The **convener per-agenda-item escalation ration** — generalizing the shipped CE5 *one-escalation-per-interaction* ration to **one per agenda item**, on the **convener** path ([OQ #1](0052-autonomous-agent-channels.md#open-questions)). On a stall with the agenda not exhausted, the convener advances to the next item (poses the next sub-topic or a pointed NL-addressed question). **The per-item loop guard is preserved**: a stall on item *N* escalates **once**; if that also draws silence, advance to *N+1* (or, agenda exhausted, propose synthesis-and-close) — never twice into silence on the same item. Bounds total convener turns at one per agenda item. |
| same file | A best-effort `min_substantive_turns_per_agenda_item` **liveness target** (not an enforceable floor — RFC 0051 semantic silence cannot be compelled). |
| Scope gate | Gated by `autonomous.enabled` ([OQ #2](0052-autonomous-agent-channels.md#open-questions)); silence stays semantic (the threshold is **not** lowered globally). |
| Tests | `MT-AUTONOMOUS-002` + a **human-channel regression** proving the shipped CE5 one-shot ration and bias-to-silence are byte-for-byte unchanged off the autonomous path. |

#### PR checklist

- [ ] `go test ./internal/channels/... -race`; chair-loop test asserts ≤ one convener escalation per agenda item.
- [ ] Human-channel regression green (CE5 one-shot ration unchanged).
- [ ] `MT-AUTONOMOUS-002` documented — a collapse-prone roster works the agenda under autonomous pressure.

---

### PR 7: `feature/v0311-rfc0052-standing` — Phase 3: Standing / scheduled convening + aggregate bound

**Depends on**: PR 6 merged.
**Purpose**: Let the convener be woken on an RFC 0024 timer to **open** or **advance** a discussion — with a mandatory aggregate bound, since the per-interaction cap does not bound a recurring schedule ([§E](0052-autonomous-agent-channels.md#e-standing-and-scheduled-discussions)).

#### Scope

| File | Change |
|------|--------|
| `internal/channels/` + `agents/` timer wiring | `autonomous.schedule` (an RFC 0024 timer spec + a topic source) reaches the convener via the **config-round-trip** seam — write into the convener's `agents.yaml` timer set ([OQ #4](0052-autonomous-agent-channels.md#open-questions); the runtime `RegisterTimer` API stays deferred). Phase 3 ships **fixed/rotating** topic; an operator-supplied queue is a follow-up. |
| [`internal/channels/config_validate.go`](../../internal/channels/config_validate.go) | The **standing aggregate-bound gate** — `validate` rejects a standing (`autonomous.schedule`-bearing) channel without `max_convenings` and/or a standing-window cost budget. |
| CLI + web config surfaces | `autonomous.schedule` + `max_convenings` join the `channel_config_autonomous.rs` / `AutonomousSettings.svelte` surfaces (PR 2); the web panel shows a **convening-count / aggregate-bound readout** so an operator sees how close a standing channel is to its bound. |
| Tests | `MT-AUTONOMOUS-003` — a standing channel convenes a fresh interaction on schedule across a window, unattended, **and stops at the aggregate bound**; **and the wallet footprint stays bounded** — each convening's `interactionTokens` residue is evicted on close (PR 4), so the map does not grow one entry per convening across the window. |

#### PR checklist

- [ ] `go test ./internal/channels/... -race`; `validate` rejects an unbounded standing channel.
- [ ] Standing leg asserts the aggregate bound stops re-convening; the web readout reflects the count.
- [ ] Standing leg asserts the wallet `interactionTokens` map does not accumulate one residual entry per convening (PR 4 eviction holds across the window).
- [ ] `MT-AUTONOMOUS-003` documented; timer wiring is config-round-trip (no new runtime API).

---

### PR 8: `feature/v0311-rfc0052-demo-offline` — Phase 4a: `make demo-autonomous` (offline face)

**Depends on**: PR 7 merged.
**Purpose**: The one-command adoption demo — boots a curated roster on a topic and shows the whole arc (convene → discuss → converge → synthesize) with zero human input and **zero keys**.

#### Scope

| File | Change |
|------|--------|
| `Makefile` + `blueprints/` | `make demo-autonomous` + a curated roster blueprint; the **offline face maps every seat to the `mock` provider** so the demo and the no-runaway smoke run with zero keys and zero spend. |
| `docs/` | Demo doc (incl. convening from the web "Convene" button); the offline E2E smoke produces a non-empty synthesis. |

#### PR checklist

- [ ] `make demo-autonomous` runs offline (mock) and produces a non-empty synthesis.
- [ ] No keys required; spend = 0 on the offline face.

---

### PR 9: `feature/v0311-rfc0052-demo-multivendor` — Phase 4b: Four-vendor headline + closeout (cuttable)

**Depends on**: PR 8 merged **+ RFC 0053 PR 1–2 (Gemini + watsonx)**.
**Purpose**: The flagship cross-vendor demo + the RFC closeout. **Cuttable**: if RFC 0053 slipped, the four-vendor leg + `MT-AUTONOMOUS-MULTIPROVIDER-001` track into the v0.3.12 point release; PR 8's offline demo carries the headline and PR 9 lands the closeout only.

#### Scope

| File | Change |
|------|--------|
| `blueprints/` | The **four-vendor blueprint** — four personas, each pinned by RFC 0033 alias to a *different cloud vendor* (Anthropic + OpenAI + Gemini + watsonx.ai), brainstorming one topic in one channel with no human. Pure alias config (RFC 0053 Phase 3 handoff). |
| `docs/manual-tests/MT-AUTONOMOUS-MULTIPROVIDER-001.md` | The headline cross-vendor MT — live, all four vendors keyed; converge + synthesize, no human; total interaction spend ≤ the mandatory cap (a single shared per-interaction ceiling, **not** a per-seat cap). |
| RFC + ROADMAP + CHANGELOG | RFC 0052 front-matter → `✅ Implemented`; Master-Index row; CHANGELOG `[0.3.11]` finalized; the OQ #5 calibration tracked-issue filed. |

#### PR checklist

- [ ] Four-vendor blueprint validates; `MT-AUTONOMOUS-MULTIPROVIDER-001` documented (live run is master-plan Phase 3).
- [ ] **If RFC 0053 slipped**: four-vendor leg deferred to v0.3.12 with a slip note; closeout still lands.
- [ ] RFC 0052 → ✅ Implemented; ROADMAP + CHANGELOG updated; `make rfcs` regenerates [INDEX.md](INDEX.md).

---

## Documentation & diagrams

A new channel *mode* + CLI/web surfaces needs real doc + diagram **edits**, not the "verify" pass the release-prep template inherits. Authored **in the feature PRs** that ship the behaviour (the RFC 0051 precedent — guide/diagram changes ride the PR that makes them true), then *verified* at release-prep:

| Artifact | Change | Owning PR |
|----------|--------|-----------|
| [`docs/guides/channels.md`](../guides/channels.md) | New **§Autonomous channels** — `autonomous.enabled`, the mandatory cost cap, convener vs. chair ([OQ #1](0052-autonomous-agent-channels.md#open-questions)), standing schedule, the "no human in the loop" contract; CLI + web how-to. | PR 3 (once convene is demoable) |
| [`docs/guides/persona-agents.md`](../guides/persona-agents.md) | New **§Autonomous channels** (persona side) — anti-collapse scoping, semantic-silence-still-applies, the convener's agenda role. | PR 6 (anti-collapse) |
| [`docs/guides/web-console.md`](../guides/web-console.md) | Note the Channel-settings panel now renders the `autonomous` block + a Convene action. | PR 2 (web surfaces) |
| [`docs/diagrams/workflow-execution.md`](../diagrams/workflow-execution.md) | A **third sequence** — autonomous brainstorm: convene (or timer-fire) → discussion loop with anti-collapse → metered bounded close → synthesis. | PR 5 (full flow exists) |
| `docs/manual-tests/MT-AUTONOMOUS-00{1,2,3}.md` + `docs/manual-tests/README.md` index | The three acceptance MTs + the index rows. | PR 5 / PR 6 / PR 7 |

> `docs/diagrams/system-overview.md` (provider list) is owned by the [RFC 0053 PR plan](0053-pr-plan.md#documentation--diagrams) since RFC 0053 adds the vendors. README + ROADMAP version rows + the release checklist are release-prep (master plan Phase 2).

## Risk and Mitigations

| Risk | Mitigation |
|------|------------|
| An at-cap file busts the 500 cap (`wallet.go` 499, `channels.go` 499, **`channel_config.rs` 500**, `ChannelSettings.svelte` 483, `action_loop.py` 495). | New modules/components per the [file-size table](#file-size-constraints-verified-at-plan-authoring-cap--500-per-file_sizepy---strict): `synthesis_reserve.go`, `convene.go`, `channel_config_autonomous.{rs,go}`, **`AutonomousSettings.svelte`**, `channel_convene.rs`, `convener.py`, `convener_cadence.go`. The CLI + web both follow the RFC 0051 nested-knob extraction precedent. |
| CLI/web surface work is under-scoped (the RFC's Files-Touched omitted `web/`). | Corrected here: **PR 2** dedicates a config-surfaces PR and **PR 3** carries the convene action on all three surfaces; the RFC [Files-Touched table](0052-autonomous-agent-channels.md#files-touched-estimated) is annotated to add the `web/` row. |
| Metering the summary (OQ #6) regresses the human-channel close. | The metering edit is **autonomous-path-only**; PR 4 carries a regression proving the human close is byte-for-byte unchanged. |
| A fixed "two-call" reserve under-sizes the close path: the RFC 0020 summary is authored **per persona** ([`close_path.py`](../../agents/persona_runtime/close_path.py)), so a roster issues `1 + N` leased close calls on the shared interaction budget — a 2-call reserve denies all but one persona's summary → placeholder. | PR 4 sizes the reserve **roster-scaled (`1 + N`)**; the close-by-budget leg runs on a **≥2-persona** roster and asserts every persona's summary survives. Fallback if `1 + N` eats too much cap: scope metering + reserve to one designated summarizer, documented. |
| A standing channel leaks one wallet `interactionTokens` entry per convening — the shipped wallet never prunes a settled-nonzero capped interaction ([`interaction_budget.go`](../../internal/wallet/interaction_budget.go)). | PR 4's bounded close emits the interaction-closed **eviction**; PR 7's standing leg asserts the wallet footprint stays bounded across the convening window. |
| The convener's **opening turn is uncapped** — the wallet snapshots the cap at first commit, so the lease producing the opening message predates it ([`interaction_budget.go`](../../internal/wallet/interaction_budget.go)); one uncapped turn per convening on a standing channel. | PR 3 settles this explicitly (documented-uncapped, with the Layer-0 depth cap + the §E aggregate bound as the nets, **or** the opening lease carries the resolved cap). |
| Anti-collapse re-introduces pile-on on human channels. | Scoped to `autonomous.enabled` (OQ #2); PR 6 human-channel regression. |
| The four-vendor demo gates the release on two new SDKs. | PR 9 is cuttable; the offline demo (PR 8) carries the headline if RFC 0053 slips. |
| `max_rounds` / cap / reserve defaults are uncalibrated. | Conservative defaults + an OQ #5 calibration tracked-issue (tune after a soak). |

---

## ROADMAP Hygiene

- **This planning PR** (the v0.3.11 plan) → RFC 0052 Master-Index row `📋 Proposed → 🚧 Implementing`, target `v0.3.x → v0.3.11`.
- **PR 1 merges** → CHANGELOG `[0.3.11]` seeded (opt-in autonomous channels + mandatory cap).
- **PR 9 (closeout) merges** → RFC 0052 → ✅ Implemented; `Last updated` + Current-phase refresh (RFC flips at its closeout PR, not the tag).

---

## Progress Overview

| PR | Phase | Branch | Status |
|----|-------|--------|--------|
| 1 | 1a — config backend + cap gate + REST (dark) | `feature/v0311-rfc0052-config-backend` | 🔀 PR open |
| 2 | 1b — CLI + web config surfaces | `feature/v0311-rfc0052-config-surfaces` | ⬜ |
| 3 | 1c — self-convening + convene action (CLI/REST/web) | `feature/v0311-rfc0052-convene` | ⬜ |
| 4 | 1d — bounded close + roster-scaled (1+N) reserve + eviction + OQ #6 metering | `feature/v0311-rfc0052-bounded-close` | ⬜ |
| 5 | 1e — acceptance suite + MT-AUTONOMOUS-001 | `feature/v0311-rfc0052-phase1-mt` | ⬜ |
| 6 | 2 — anti-collapse cadence (convener, scoped) | `feature/v0311-rfc0052-anti-collapse` | ⬜ |
| 7 | 3 — standing/scheduled + aggregate bound + web readout | `feature/v0311-rfc0052-standing` | ⬜ |
| 8 | 4a — `make demo-autonomous` (offline) | `feature/v0311-rfc0052-demo-offline` | ⬜ |
| 9 | 4b — four-vendor headline + closeout (cuttable) | `feature/v0311-rfc0052-demo-multivendor` | ⬜ |

**Status legend**: ⬜ Not started · 🔄 In progress · 🔀 PR open · ✅ Merged

---

## Related Documentation

- [RFC 0052 — Autonomous Agent-Only Channels](0052-autonomous-agent-channels.md) — the spec; [§B self-convening](0052-autonomous-agent-channels.md#b-self-convening--starting-without-a-human-turn) (the three convene surfaces), [§C anti-collapse](0052-autonomous-agent-channels.md#c-the-central-tension--anti-collapse-cadence), [§D termination/synthesis](0052-autonomous-agent-channels.md#d-termination-and-synthesis--always-produce-an-artifact), [§E standing](0052-autonomous-agent-channels.md#e-standing-and-scheduled-discussions).
- [v0.3.11-plan.md](../v0.3.11-plan.md) — the master version plan + the locked scope/OQ decisions.
- [RFC 0053 PR plan](0053-pr-plan.md) — the bundled providers PR 9 depends on for the four-vendor leg.
- [RFC 0050](0050-extensible-channel-configuration.md) + [interaction-budget amendment](0050-amendment-interaction-budget-enforcement.md) — the operator-editable config surface (CLI `channel config` + web Channel-settings panel) PR 2 extends; [RFC 0051 PR plan](0051-pr-plan.md) PR 4/5 — the backend-then-surfaces split this mirrors.
- [RFC 0030](0030-multi-agent-conversation-governance.md) (chair / CE4 / CE5 / end-of-interaction) · [RFC 0024](0024-event-driven-scheduling.md) (timers) · [RFC 0020](0020-interaction-lifecycle.md) (summary) · [RFC 0023](0023-llm-call-leasing.md) (lease) · [RFC 0009](0009-security-sandboxing.md) (`<external_data>`) · [RFC 0048](0048-operator-tester-web-console.md) (web console).
