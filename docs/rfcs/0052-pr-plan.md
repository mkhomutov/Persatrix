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

This plan covers **all four phases** ([master-plan scope lock](../v0.3.11-plan.md#scope-decisions-locked-at-plan-authoring-time-2026-06-28)) across **8 PRs**:

- **Phase 1 — convene + bounded one-shot brainstorm (PR 1–4).** The smallest demoable unit: a channel that opens itself on a topic, runs to a bound, and synthesizes — with the mandatory cap enforced from the first PR. PR 1 lands the `autonomous` config block + the `validate` cap-required gate (dark — no convene path yet); PR 2 the self-convening opening turn + the `persatrix channel convene` verb; PR 3 the deterministic bounded close + the **two-call** wallet synthesis reserve + the OQ #6 summary-metering edit + mandatory synthesis-on-close; PR 4 the Phase-1 integration suite + `MT-AUTONOMOUS-001`.
- **Phase 2 — anti-collapse cadence (PR 5).** The convener per-agenda-item escalation ration (generalizing the shipped CE5 one-shot ration) with the per-item loop guard preserved, scoped to `autonomous.enabled`; `MT-AUTONOMOUS-002` + the human-channel regression.
- **Phase 3 — standing / scheduled convening (PR 6).** `autonomous.schedule` over RFC 0024 timers via the config-round-trip seam + the mandatory standing **aggregate** bound + `validate` gate; `MT-AUTONOMOUS-003`.
- **Phase 4 — flagship demo (PR 7–8).** PR 7 the offline `make demo-autonomous` (curated roster, mock provider, zero keys); PR 8 the four-vendor cross-vendor headline blueprint + `MT-AUTONOMOUS-MULTIPROVIDER-001` (live, depends on RFC 0053) + RFC/ROADMAP/CHANGELOG closeout.

**Hard prerequisites (all shipped):** RFC 0011 channels (v0.3.0 ✅), RFC 0030 chair / relevance / end-of-interaction / chair-stall-escalation (v0.3.6–0.3.8 ✅), RFC 0024 `InboundEventWake` + timers (v0.3.3 ✅), RFC 0050 config surface + interaction-budget amendment (v0.3.8 ✅), RFC 0051 reasoning (v0.3.10 ✅), RFC 0020 interaction summary (v0.3.0 ✅), RFC 0023 leasing (v0.3.2 ✅), RFC 0033 alias layer (v0.3.4 ✅). No new substrate — this is a generalization + assembly plus the three autonomous-scoped mechanisms.

### Open-question resolutions locked at plan-authoring time

Locked with the maintainer at plan opening (see [master plan §Scope decisions](../v0.3.11-plan.md#scope-decisions-locked-at-plan-authoring-time-2026-06-28)). Two **reverse** the RFC's own lean and are recorded in the [RFC Open Questions](0052-autonomous-agent-channels.md#open-questions) §Status as well:

- **[OQ #1](0052-autonomous-agent-channels.md#open-questions) — distinct `convener` role** *(reverses the RFC's "lean chair = convener")*. The **convener** (a new `autonomous.convener` agent id) owns the agenda lifecycle — authors the opening turn and **advances the agenda** on a stall; the **chair** (the RFC 0050 `escalation_chair_id`) keeps its shipped v0.3.8 role (stall-escalation → propose synthesis → end-vote). **Consequence for PR 5:** the anti-collapse per-agenda-item ration lives on the **convener** path (PR 2/PR 5), *not* the chair path — the chair's CE5 one-shot ration is untouched.
- **[OQ #2](0052-autonomous-agent-channels.md#open-questions) — anti-collapse scoped to `autonomous.enabled`** (not a general `liveness` knob). The convener ration is gated by `autonomous.enabled`; human channels keep the shipped bias-to-silence + CE5 guard. PR 5 carries the human-channel regression that proves it.
- **[OQ #6](0052-autonomous-agent-channels.md#open-questions) — meter the closing summary; reserve covers two calls** *(takes the heaviest option)*. Verified that the RFC 0020 close summary is **currently unmetered** — it passes no `cause`/`interaction_id` so it bypasses the wallet lease ([`agents/llm_client.py:212`](../../agents/llm_client.py), [`agents/persona_runtime/summarize_close.py:172`](../../agents/persona_runtime/summarize_close.py)). PR 3 brings the **autonomous-close** summary under a lease (stamped with the interaction's `interaction_id`) so it counts toward the cap, and sizes the synthesis reserve for **both** close-path calls (chair synthesis turn + summary).
- **[OQ #3](0052-autonomous-agent-channels.md#open-questions) / [#4](0052-autonomous-agent-channels.md#open-questions) / [#5](0052-autonomous-agent-channels.md#open-questions)** — free-text `goal` for v0.3.x; Phase 3 ships fixed/rotating topic + the **config-round-trip** timer seam (not the deferred runtime `RegisterTimer` API; topic *queue* is a follow-up); conservative defaults + an OQ #5 calibration tracked-issue.

### Sequencing

**Recommended merge order**: **PR 1 → PR 2 → PR 3 → PR 4 → PR 5 → PR 6 → PR 7 → PR 8.**

Phase 1 (PR 1–4) is a strict chain: the config + cap gate (PR 1) precedes self-convening (PR 2), which precedes the bounded close + reserve (PR 3), which the integration suite (PR 4) exercises. Phase 2 (PR 5, anti-collapse) depends on the convener path (PR 2) + the bounded close (PR 3 — the convener advances *or* the close fires). Phase 3 (PR 6, standing) depends on the cap + convene path. Phase 4 PR 7 (offline demo) depends on Phase 1–2; **PR 8 (four-vendor headline) additionally depends on RFC 0053 PR 1–2** and is **cuttable** — if RFC 0053 slips, PR 8's four-vendor leg tracks into v0.3.12 and PR 7's offline demo carries the headline.

### File-size constraints (verified at plan authoring, cap = 500 per [`file_size.py --strict`](../../scripts/checks/file_size.py))

| File | Lines | Headroom | Routing |
|------|-------|----------|---------|
| [`internal/wallet/wallet.go`](../../internal/wallet/wallet.go) | **499** | **1** | **At the cap — must not gain net lines.** The PR 3 two-call **synthesis-reserve accounting** lands in [`internal/wallet/interaction_budget.go`](../../internal/wallet/interaction_budget.go) (155, ample) or a new `synthesis_reserve.go`, **not** `wallet.go`. |
| [`internal/channels/channels.go`](../../internal/channels/channels.go) | **499** | **1** | **At the cap.** The PR 2 convene endpoint routes through [`router.go`](../../internal/channels/router.go) / a new `convene.go`; do not grow `channels.go`. |
| [`agents/persona_runtime/summarize_close.py`](../../agents/persona_runtime/summarize_close.py) | 480 | 20 | The PR 3 OQ #6 metering edit must be a **minimal `cause=`/`interaction_id=` thread** on the existing `create_message` call ([line 172](../../agents/persona_runtime/summarize_close.py)); if it busts the cap, the prompt assembly is the extraction candidate. |
| [`agents/persona_runtime/action_loop.py`](../../agents/persona_runtime/action_loop.py) | 495 | 5 | Convener opening-turn + agenda-advance authoring is a **call into a new `convener.py`**, not inline. |
| [`internal/channels/chair_escalation.go`](../../internal/channels/chair_escalation.go) | 411 | 89 | The CE5 mechanism the convener ration generalizes; the autonomous per-agenda-item ration lands in a **new `convener_cadence.go`** to keep the shipped CE5 path readable + the autonomous scope visibly separate. |
| [`internal/channels/config_validate.go`](../../internal/channels/config_validate.go) | — | — | Owns the PR 1 cap-required + PR 6 aggregate-bound `validate` gates (extends the RFC 0050 validate pattern). |
| New: `convener.py`, `convener_cadence.go`, `convene.go`, `synthesis_reserve.go` | — | — | Own modules so the at-cap files (`wallet.go`, `channels.go`, `action_loop.py`) stay under cap. |

---

## Dependency Graph

```
RFC 0011/0030/0024/0050/0051/0020/0023/0033 (all shipped)        ← HARD PREREQUISITES
   │
   ├── PR 1 (Phase 1a: `autonomous` config block on the RFC 0050 surface + schema;
   │     │   validate REJECTS autonomous.enabled without interaction_budget_tokens)   [dark — no convene]
   │     ↓
   ├── PR 2 (Phase 1b: self-convening — convener opening turn under fresh interaction_id;
   │     │   operator topic/agenda/goal wrapped in RFC 0009 <external_data>;
   │     │   `persatrix channel convene` CLI verb + convene endpoint over the publish path)
   │     ↓
   ├── PR 3 (Phase 1c: deterministic bounded close [max_rounds / soft-budget / agenda-exhausted];
   │     │   two-call wallet synthesis reserve [new accounting]; OQ #6 meter the autonomous-close
   │     │   summary; mandatory synthesis-on-close — chair synthesis turn + interaction summary)
   │     ↓
   ├── PR 4 (Phase 1d: integration suite + MT-AUTONOMOUS-001 — convene→converge→terminate→
   │     │   both artifacts; no-runaway leg; close-by-budget leg honours the two-call reserve)
   │     ↓
   ├── PR 5 (Phase 2: convener per-agenda-item escalation ration [generalizes CE5, per-item loop
   │     │   guard preserved, autonomous-scoped] + best-effort liveness target; MT-AUTONOMOUS-002;
   │     │   HUMAN-CHANNEL REGRESSION proving CE5 one-shot ration unchanged off the autonomous path)
   │     ↓
   ├── PR 6 (Phase 3: autonomous.schedule over RFC 0024 timers via config-round-trip seam;
   │     │   mandatory standing aggregate bound [max_convenings / standing cost budget] + validate gate;
   │     │   MT-AUTONOMOUS-003 — standing convenes across a window, stops at the aggregate bound)
   │     ↓
   ├── PR 7 (Phase 4a: `make demo-autonomous` + curated roster blueprint; OFFLINE face = mock
   │     │   provider, zero keys; offline smoke produces a non-empty synthesis)
   │     ↓
   └── PR 8 (Phase 4b: four-vendor headline blueprint [Anthropic + OpenAI + Gemini + watsonx];
             MT-AUTONOMOUS-MULTIPROVIDER-001 [live, all four keyed]; RFC + ROADMAP + CHANGELOG closeout)
                ▲
                └── depends on RFC 0053 PR 1–2 (Gemini + watsonx) — CUTTABLE: if 0053 slips,
                    PR 8's four-vendor leg tracks to v0.3.12; PR 7 carries the offline headline
```

---

## PR Sequence

### PR 1: `feature/v0311-rfc0052-config-cap` — Phase 1a: `autonomous` config block + mandatory-cap gate (dark)

**Depends on**: RFC 0050 config surface (shipped).
**Purpose**: Land the `autonomous` block on the channel config surface and make an uncapped autonomous channel **un-creatable** — `validate` rejects `autonomous.enabled` without `interaction_budget_tokens`. Ships dark: no convene path yet, so the block is inert until PR 2.

#### Scope

| File | Change |
|------|--------|
| [`internal/channels/config_validate.go`](../../internal/channels/config_validate.go) | `validate` rejects `autonomous.enabled: true` without a positive `interaction_budget_tokens`; validates `convener` is a roster member id and (when present) `escalation_chair_id` ≠ `convener` is allowed but not required (distinct roles, [OQ #1](0052-autonomous-agent-channels.md#open-questions)). The standing aggregate-bound gate is **deferred to PR 6** (no `schedule` field yet). |
| [`internal/channels/config_apply.go`](../../internal/channels/config_apply.go) + config schema | The sparse `autonomous` block (`enabled`, `topic`, `agenda[]`, `convener`, `goal`, `max_rounds`, `interaction_budget_tokens`) — apply/persist following the RFC 0050 `config_reasoning.go` precedent. Conservative defaults ([OQ #5](0052-autonomous-agent-channels.md#open-questions)). |
| `config/` channel schema + example | JSON-schema entry + an example autonomous channel (documented, not wired into a society yet). |
| Go tests | `validate` rejects uncapped autonomy; accepts a capped one; round-trips the sparse block. |

#### Key implementation details

- **Cap-required is a safety invariant, not a tuning knob** ([RFC §Security](0052-autonomous-agent-channels.md#security-considerations)) — the rejection is unconditional, asserted by a dedicated test that is the first line of the no-runaway defense.
- **Distinct convener + chair fields** ([OQ #1](0052-autonomous-agent-channels.md#open-questions)) — `autonomous.convener` is the new field; the chair reuses the RFC 0050 `escalation_chair_id`. Both are plain agent ids.
- **Dark** — without the PR 2 convene path the block changes no runtime behaviour.

#### PR checklist

- [ ] `go test ./internal/channels/... -race` passes; `go vet` clean.
- [ ] `validate` rejects `autonomous.enabled` without a cap (dedicated test).
- [ ] Example autonomous channel parses; `make validate` green.
- [ ] RFC 0052 Master-Index note `📋 Proposed → 🚧 Implementing` already applied by the planning PR; [v0.3.11-plan row 1](../v0.3.11-plan.md#master-progress-overview) → 🔄 In progress.

---

### PR 2: `feature/v0311-rfc0052-convene` — Phase 1b: Self-convening + `channel convene` verb

**Depends on**: PR 1 merged.
**Purpose**: The convener authors the **opening turn** under a fresh `interaction_id`, with no human message — from which the existing `InboundEventWake` chain carries the discussion. Add the three convene triggers' first two surfaces (CLI + endpoint; the timer surface is PR 6).

#### Scope

| File | Change |
|------|--------|
| New `agents/persona_runtime/convener.py` | Convener opening-turn authoring: compose the seed turn (topic + first agenda item) as a normal channel publish stamped with a fresh `interaction_id` (RFC 0030 producer). **Operator-supplied `topic`/`agenda`/`goal` wrapped in the RFC 0009 `<external_data>` envelope** before injection into the convener prompt ([RFC §Security](0052-autonomous-agent-channels.md#security-considerations)) — the one genuinely new injection surface. |
| [`agents/persona_runtime/action_loop.py`](../../agents/persona_runtime/action_loop.py) | A **one-line call** into `convener.py` on a convene wake; no inline authoring (495/500). |
| New `internal/channels/convene.go` + [`router.go`](../../internal/channels/router.go) | The convene endpoint — a thin wrapper over the existing publish path (no new transport, no new wake type, no new store table). |
| [`cli/src/commands/channel.rs`](../../cli/src/commands/channel.rs) | `persatrix channel convene <id>` verb (mirrors the `channel_config_reasoning.rs` sub-verb pattern). |
| Tests | Convener authors **exactly one** opening turn under a fresh `interaction_id`; the `<external_data>` wrap is present; no human turn required. |

#### Key implementation details

- **Convening = "author the seed turn under a fresh interaction id"** ([RFC §B](0052-autonomous-agent-channels.md#b-self-convening--starting-without-a-human-turn)) — reuses the publish path end-to-end.
- **The `<external_data>` wrap is mandatory** — operator config is a distinct trust class from persona-authored content; the RFC 0036 §F per-row escaping is *not* the right control (it escapes recall rows, not config).

#### PR checklist

- [ ] `pytest agents/tests/test_convener.py -q`; `cargo test` (CLI); `go test ./internal/channels/... -race`.
- [ ] Convener authors exactly one opening turn; fresh `interaction_id`; `<external_data>` wrap asserted.
- [ ] `persatrix channel convene` round-trips against the local orchestrator.
- [ ] No new wake type / transport / store table (assembly only).

---

### PR 3: `feature/v0311-rfc0052-bounded-close` — Phase 1c: Deterministic bounded close + two-call synthesis reserve

**Depends on**: PR 2 merged.
**Purpose**: Guarantee an autonomous interaction **terminates** and **always leaves both artifacts**, even on a budget-exhausted close — the part of [RFC §D](0052-autonomous-agent-channels.md#d-termination-and-synthesis--always-produce-an-artifact) that is not pure reuse.

#### Scope

| File | Change |
|------|--------|
| New `internal/channels/convene.go` (or a sibling) | The **deterministic bounded-close trigger** — fires on agenda-exhausted *or* `max_rounds` *or* a **soft** budget threshold; dispatches the chair synthesis turn and *then* closes the interaction (respecting RFC 0030 CE4 — the chair still cannot close itself). This is what finally enforces `max_rounds` (no shipped enforcement today). |
| New [`internal/wallet/interaction_budget.go`](../../internal/wallet/interaction_budget.go) sibling (`synthesis_reserve.go`) | The **two-call synthesis reserve** — new wallet accounting (no shipped analog): split the cap so the discussion is bounded by `interaction_budget_tokens` and a reserve is held back for the close path. The **soft** threshold trips synthesize-and-close *before* the hard cap denies leases. Sized for **both** close-path calls (chair synthesis turn + summary). **`wallet.go` (499) gains no net lines.** |
| [`agents/persona_runtime/summarize_close.py`](../../agents/persona_runtime/summarize_close.py) | **OQ #6 metering edit** — on the **autonomous** close path, thread `cause` + the interaction's `interaction_id` into the existing `create_message` call ([line 172](../../agents/persona_runtime/summarize_close.py)) so the summary is leased and counts toward the cap (today it bypasses the lease). Minimal edit (480/500). Human-channel close is unchanged. |
| `agents/` chair synthesis turn | Mandatory goal-directed synthesis turn against `autonomous.goal`, drawn from the reserve. |
| Tests | Bounded close fires on each trigger; reserve covers two calls; the metered summary is denied **only** when the reserve is exhausted (it isn't, by construction). |

#### Key implementation details

- **The reserve has no shipped analog** ([RFC §Files Touched](0052-autonomous-agent-channels.md#files-touched-estimated)) — the shipped wallet is a single hard integer cap with no reserved-fraction or soft-threshold concept. Size this as new accounting, not a config knob.
- **Why two calls** ([OQ #6](0052-autonomous-agent-channels.md#open-questions)) — metering the summary closes its out-of-cap gap on an unattended channel, but means a budget-exhausted close would deny it without the reserve. The reserve covers chair turn + summary.
- **CE4 stays intact** — the close trigger is orchestrator-side and distinct from the chair, which still cannot close an interaction.

#### PR checklist

- [ ] `go test ./internal/wallet/... ./internal/channels/... -race -cover`; `pytest agents/tests/ -q`.
- [ ] `wallet.go` net line delta = 0 (reserve in a sibling file).
- [ ] Reserve sized for two calls; close-by-budget unit test proves both leases are honoured.
- [ ] Human-channel summarization path byte-for-byte unchanged (the metering is autonomous-only).

---

### PR 4: `feature/v0311-rfc0052-phase1-mt` — Phase 1d: Acceptance suite + `MT-AUTONOMOUS-001`

**Depends on**: PR 3 merged.
**Purpose**: Prove the Phase-1 contract end-to-end on the mock provider, then live.

#### Scope

| File | Change |
|------|--------|
| `internal/channels/` + `agents/tests/` integration | Full convene→converge→terminate→synthesis cycle on the mock provider, **zero human turns**, spend ≤ cap; the **no-runaway leg** (turns + tokens bounded under an adversarial "everyone wants to talk" roster); the **close-by-budget leg** asserting **both** artifacts are still produced (chair synthesis turn + a real RFC 0020 summary, not the `[interaction summary unavailable]` placeholder). |
| `docs/manual-tests/MT-AUTONOMOUS-001.md` | One-shot brainstorm, live provider — converges + synthesizes, no human. |

#### PR checklist

- [ ] Integration suite green on mock; no-runaway + close-by-budget legs assert their invariants.
- [ ] `MT-AUTONOMOUS-001` documented + dry-run on mock (live run is Phase 3 of the master plan).
- [ ] CHANGELOG `[0.3.11]` seeded (autonomous channels — opt-in, mandatory cap).

---

### PR 5: `feature/v0311-rfc0052-anti-collapse` — Phase 2: Anti-collapse cadence (convener, autonomous-scoped)

**Depends on**: PR 4 merged.
**Purpose**: Supply the counter-pressure that keeps a human-free discussion alive without un-doing the realism arc's bias-to-silence — the design heart of the RFC ([§C](0052-autonomous-agent-channels.md#c-the-central-tension--anti-collapse-cadence)).

#### Scope

| File | Change |
|------|--------|
| New [`internal/channels/`](../../internal/channels/) `convener_cadence.go` | The **convener per-agenda-item escalation ration** — generalizing the shipped CE5 *one-escalation-per-interaction* ration to **one per agenda item**, on the **convener** path ([OQ #1](0052-autonomous-agent-channels.md#open-questions)). On a stall with the agenda not exhausted, the convener advances to the next item (poses the next sub-topic or a pointed NL-addressed question). **The per-item loop guard is preserved**: a stall on item *N* escalates **once**; if that also draws silence, advance to *N+1* (or, agenda exhausted, propose synthesis-and-close) — never twice into silence on the same item. Bounds total convener turns at one per agenda item. |
| same file | A best-effort `min_substantive_turns_per_agenda_item` **liveness target** (not an enforceable floor — RFC 0051 semantic silence cannot be compelled). |
| Scope gate | The entire mechanism is gated by `autonomous.enabled` ([OQ #2](0052-autonomous-agent-channels.md#open-questions)); silence stays semantic (the threshold is **not** lowered globally). |
| Tests | `MT-AUTONOMOUS-002` + a **human-channel regression** proving the shipped CE5 one-shot ration and bias-to-silence are byte-for-byte unchanged off the autonomous path. |

#### Key implementation details

- **New mechanism, not a behavior toggle** — making a multi-item agenda workable requires lifting CE5's per-interaction ration to per-item; the loop guard is non-negotiable.
- **Raises salience honestly** — anti-collapse gives the convener something concrete to ask; it does not force low-value "I agree" turns.

#### PR checklist

- [ ] `go test ./internal/channels/... -race`; chair-loop test asserts ≤ one convener escalation per agenda item.
- [ ] Human-channel regression green (CE5 one-shot ration unchanged).
- [ ] `MT-AUTONOMOUS-002` documented — a collapse-prone roster works the agenda under autonomous pressure.

---

### PR 6: `feature/v0311-rfc0052-standing` — Phase 3: Standing / scheduled convening + aggregate bound

**Depends on**: PR 5 merged.
**Purpose**: Let the convener be woken on an RFC 0024 timer to **open** or **advance** a discussion — with a mandatory aggregate bound, since the per-interaction cap does not bound a recurring schedule ([§E](0052-autonomous-agent-channels.md#e-standing-and-scheduled-discussions)).

#### Scope

| File | Change |
|------|--------|
| `internal/channels/` + `agents/` timer wiring | `autonomous.schedule` (an RFC 0024 timer spec + a topic source) reaches the convener via the **config-round-trip** seam — write into the convener's `agents.yaml` timer set ([OQ #4](0052-autonomous-agent-channels.md#open-questions): the cheaper of the two; the runtime `RegisterTimer` API stays deferred). Phase 3 ships **fixed/rotating** topic; an operator-supplied queue is a follow-up. |
| [`internal/channels/config_validate.go`](../../internal/channels/config_validate.go) | The **standing aggregate-bound gate** — `validate` rejects a standing (`autonomous.schedule`-bearing) channel without `max_convenings` and/or a standing-window cost budget. |
| Tests | `MT-AUTONOMOUS-003` — a standing channel convenes a fresh interaction on schedule across a window, unattended, **and stops at the aggregate bound**. |

#### PR checklist

- [ ] `go test ./internal/channels/... -race`; `validate` rejects an unbounded standing channel.
- [ ] Standing leg asserts the aggregate bound stops re-convening.
- [ ] `MT-AUTONOMOUS-003` documented; the timer wiring is config-round-trip (no new runtime API).

---

### PR 7: `feature/v0311-rfc0052-demo-offline` — Phase 4a: `make demo-autonomous` (offline face)

**Depends on**: PR 6 merged.
**Purpose**: The one-command adoption demo — boots a curated roster on a topic and shows the whole arc (convene → discuss → converge → synthesize) with zero human input and **zero keys**.

#### Scope

| File | Change |
|------|--------|
| `Makefile` + `blueprints/` | `make demo-autonomous` + a curated roster blueprint; the **offline face maps every seat to the `mock` provider** so the demo and the no-runaway smoke run with zero keys and zero spend. |
| `docs/` | Demo doc; the offline E2E smoke produces a non-empty synthesis. |

#### PR checklist

- [ ] `make demo-autonomous` runs offline (mock) and produces a non-empty synthesis.
- [ ] No keys required; spend = 0 on the offline face.

---

### PR 8: `feature/v0311-rfc0052-demo-multivendor` — Phase 4b: Four-vendor headline + closeout (cuttable)

**Depends on**: PR 7 merged **+ RFC 0053 PR 1–2 (Gemini + watsonx)**.
**Purpose**: The flagship cross-vendor demo + the RFC closeout. **Cuttable**: if RFC 0053 slipped, the four-vendor leg + `MT-AUTONOMOUS-MULTIPROVIDER-001` track into the v0.3.12 point release; PR 7's offline demo carries the headline and PR 8 lands the closeout only.

#### Scope

| File | Change |
|------|--------|
| `blueprints/` | The **four-vendor blueprint** — four personas, each pinned by RFC 0033 alias to a *different cloud vendor* (Anthropic + OpenAI + Gemini + watsonx.ai), brainstorming one topic in one channel with no human. Pure alias config (RFC 0053 Phase 3 handoff). |
| `docs/manual-tests/MT-AUTONOMOUS-MULTIPROVIDER-001.md` | The headline cross-vendor MT — live, all four vendors keyed; converge + synthesize, no human; spend ≤ cap per seat. |
| RFC + ROADMAP + CHANGELOG | RFC 0052 front-matter → `✅ Implemented`; Master-Index row; CHANGELOG `[0.3.11]` finalized; the OQ #5 calibration tracked-issue filed. |

#### PR checklist

- [ ] Four-vendor blueprint validates; `MT-AUTONOMOUS-MULTIPROVIDER-001` documented (live run is master-plan Phase 3).
- [ ] **If RFC 0053 slipped**: four-vendor leg deferred to v0.3.12 with a slip note; closeout still lands.
- [ ] RFC 0052 → ✅ Implemented; ROADMAP + CHANGELOG updated; `make rfcs` regenerates [INDEX.md](INDEX.md).

---

## Risk and Mitigations

| Risk | Mitigation |
|------|------------|
| An at-cap file (`wallet.go` 499, `channels.go` 499, `action_loop.py` 495) busts the 500 line cap. | New modules per the [file-size table](#file-size-constraints-verified-at-plan-authoring-cap--500-per-file_sizepy---strict): `synthesis_reserve.go`, `convene.go`, `convener.py`, `convener_cadence.go`. |
| Metering the summary (OQ #6) regresses the human-channel close. | The metering edit is **autonomous-path-only**; PR 3 carries a regression proving the human close is byte-for-byte unchanged. |
| Anti-collapse re-introduces pile-on on human channels. | Scoped to `autonomous.enabled` (OQ #2); PR 5 human-channel regression. |
| The four-vendor demo gates the release on two new SDKs. | PR 8 is cuttable; the offline demo (PR 7) carries the headline if RFC 0053 slips. |
| `max_rounds` / cap / reserve defaults are uncalibrated. | Conservative defaults + an OQ #5 calibration tracked-issue (tune after a soak). |

---

## ROADMAP Hygiene

- **This planning PR** (the v0.3.11 plan) → RFC 0052 Master-Index row `📋 Proposed → 🚧 Implementing`, target `v0.3.x → v0.3.11`.
- **PR 1 merges** → CHANGELOG `[0.3.11]` seeded (opt-in autonomous channels + mandatory cap).
- **PR 8 (closeout) merges** → RFC 0052 → ✅ Implemented; `Last updated` + Current-phase refresh (RFC flips at its closeout PR, not the tag).

---

## Progress Overview

| PR | Phase | Branch | Status |
|----|-------|--------|--------|
| 1 | 1a — config + cap gate (dark) | `feature/v0311-rfc0052-config-cap` | ⬜ |
| 2 | 1b — self-convening + `convene` verb | `feature/v0311-rfc0052-convene` | ⬜ |
| 3 | 1c — bounded close + two-call reserve + OQ #6 metering | `feature/v0311-rfc0052-bounded-close` | ⬜ |
| 4 | 1d — acceptance suite + MT-AUTONOMOUS-001 | `feature/v0311-rfc0052-phase1-mt` | ⬜ |
| 5 | 2 — anti-collapse cadence (convener, scoped) | `feature/v0311-rfc0052-anti-collapse` | ⬜ |
| 6 | 3 — standing/scheduled + aggregate bound | `feature/v0311-rfc0052-standing` | ⬜ |
| 7 | 4a — `make demo-autonomous` (offline) | `feature/v0311-rfc0052-demo-offline` | ⬜ |
| 8 | 4b — four-vendor headline + closeout (cuttable) | `feature/v0311-rfc0052-demo-multivendor` | ⬜ |

**Status legend**: ⬜ Not started · 🔄 In progress · 🔀 PR open · ✅ Merged

---

## Related Documentation

- [RFC 0052 — Autonomous Agent-Only Channels](0052-autonomous-agent-channels.md) — the spec; [§C anti-collapse](0052-autonomous-agent-channels.md#c-the-central-tension--anti-collapse-cadence), [§D termination/synthesis](0052-autonomous-agent-channels.md#d-termination-and-synthesis--always-produce-an-artifact), [§E standing](0052-autonomous-agent-channels.md#e-standing-and-scheduled-discussions).
- [v0.3.11-plan.md](../v0.3.11-plan.md) — the master version plan + the locked scope/OQ decisions.
- [RFC 0053 PR plan](0053-pr-plan.md) — the bundled providers PR 8 depends on for the four-vendor leg.
- [RFC 0030](0030-multi-agent-conversation-governance.md) (chair / CE4 / CE5 / end-of-interaction) · [RFC 0050](0050-extensible-channel-configuration.md) + [interaction-budget amendment](0050-amendment-interaction-budget-enforcement.md) · [RFC 0024](0024-event-driven-scheduling.md) (timers) · [RFC 0020](0020-interaction-lifecycle.md) (summary) · [RFC 0023](0023-llm-call-leasing.md) (lease) · [RFC 0009](0009-security-sandboxing.md) (`<external_data>`).
