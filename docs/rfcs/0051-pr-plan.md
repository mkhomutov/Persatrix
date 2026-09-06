# RFC 0051 — PR Implementation Plan (Phases 1–3 + 5 — v0.3.10 scope)

**RFC**: [0051-reasoning-before-posting.md](0051-reasoning-before-posting.md)
**Created**: 2026-06-23
**Branch prefix**: `feature/v0310-rfc0051-`
**Target**: `main`
**Merge strategy**: Squash merge per [BRANCHING.md](../BRANCHING.md)
**Master plan**: [v0.3.10-plan.md Phase 1 — Reasoning Before Posting](../v0.3.10-plan.md#phase-1--implement-reasoning-before-posting)

---

## Overview

RFC 0051 makes a persona **think before it speaks**: before publishing into a channel it privately decides *whether* the turn is worth a post and *what* the post should accomplish, then composes under that plan. It generalizes the shipped [RFC 0030](0030-multi-agent-conversation-governance.md) Tier-B salience bid from a scalar `speak/score` verdict into a structured `{ should_post, plan }` — `should_post=false` ends the turn in `DO_NOTHING` *before* the expensive compose (semantic silence, a net saving on pile-on), and `should_post=true` threads a private `CompositionPlan` (intent / key points / addressed-to / avoid-restating) into the existing Tier-C compose. It reuses the leased `fast`-model seam already in production (metered against the same `interaction_id`; idle stays free), and the trace is **walled** — never a message, never the channel store, never a peer's RFC 0034 working memory ([RFC §E](0051-reasoning-before-posting.md#e-privacy-boundary--the-trace-is-walled)).

This plan covers **Phases 1–3 + Phase 5** — the full live feature plus the committed reflexion follow-on. **[Phase 4](0051-reasoning-before-posting.md#phase-4-optional--follow-on-native-extended-thinking-depth) (native extended-thinking `depth: deep`) is explicitly excluded from v0.3.10**: it needs a provider-protocol change *and* is telemetry-gated on [OQ 1](0051-reasoning-before-posting.md#open-questions) — only unblocked *if* Phase 3 telemetry shows the `fast`-model deliberation is too shallow, which cannot be known until Phase 3 ships and runs. `validate` keeps rejecting `depth: deep` as an unbacked enum value until then.

The work splits into **9 PRs** across the four in-scope phases:

- **Phase 1 — structured silence verdict (PR 1–2), ships dark.** PR 1 restructures the bid grammar in [`salience_bid.py`](../../agents/salience_bid.py) behind an internal `mode` parameter that defaults to today's score gate (byte-for-byte); PR 2 threads the verdict through the [`salience_gate.py`](../../agents/persona_runtime/salience_gate.py) seam and adds the `agent.deliberated` audit + the idle-cost gate test. **Behaviourally inert in production** — the structured path is reachable only via tests / config-as-code until [PR 4](#pr-4-featurev0310-rfc0051-config-backend--phase-3a-reasoning-config-backend) lands the `reasoning.mode` knob and [PR 6](#pr-6-featurev0310-rfc0051-telemetry-golive--phase-3c-telemetry--default-flip-go-live) flips the default.
- **Phase 2 — plan-threaded compose (PR 3), ships dark.** A new `deliberation_plan.py` owns the `CompositionPlan` value type + parser + renderer; `action_loop.py` appends the rendered private plan to the Tier-C compose. The no-leak test pins the privacy wall.
- **Phase 3 — config + telemetry (PR 4–6), go-live.** PR 4 lands the full `reasoning.{mode,model,depth,revise}` schema + RFC 0050 validate/apply/persist (capability-gated, `revise` validate-only until PR 8, default still `off`); PR 5 the CLI + web surfaces (first enum-valued + first nested/dotted knob); **PR 6 lands the telemetry suite and flips the governed-channel default `off → bid` in lockstep with the kill switch** — the moment the feature becomes live.
- **OQ 6(a) operator reveal (PR 7), separate + cuttable.** The debug-toggled web-console reasoning-reveal affordance + its backing verbatim-`reason_note` egress path. Net-new UI + backend, **not** a `ChannelSettings` row; explicitly its own PR ([OQ 6](0051-reasoning-before-posting.md#open-questions)) and droppable without affecting the headline.
- **Phase 5 — reflexion loop (PR 8–9), committed, default off.** A new `reflexion.py` critic→revise loop around compose, governed by `reasoning.revise: 0|1|2` (default `0` = single pass); PR 9 extends the no-leak test to discarded drafts and closes the release out.

**Hard prerequisites (all shipped):** RFC 0030 Tier B (the leased salience seam, v0.3.8 ✅), RFC 0050 (the channel-config surface the `reasoning` knob rides, v0.3.8 ✅), RFC 0034 Phase 2 (the transcript the trace must stay out of, v0.3.7 ✅), RFC 0023 leasing (v0.3.2 ✅), RFC 0024 idle-cost invariant (v0.3.3 ✅). No new substrate is required — this is a generalization of an existing seam.

### Open-question resolutions locked at plan-authoring time

All seven RFC open questions are **resolved in the RFC** (see its [Open Questions](0051-reasoning-before-posting.md#open-questions) §Status 2026-06-23). The plan inherits them; the load-bearing ones for sequencing:

- **[OQ 7](0051-reasoning-before-posting.md#open-questions) — supersede the score gate (sets the PR 1 parser contract).** Under `mode: bid`/`plan` the bid emits **no `score`**; `should_post` *is* the silence decision and the per-member `threshold` ([RFC 0050](0050-extensible-channel-configuration.md)) is **inert under reasoning** (governs `mode: off` only). PR 1 implements this as a mechanism change, not a pure add.
- **[OQ 2](0051-reasoning-before-posting.md#open-questions) — default `mode: bid` on a governed channel, reached in Phase 3.** Phases 1–2 ship dark (default `off`); **PR 6 flips the governed-channel default to the silence-only `bid` rung** in lockstep with the kill switch and telemetry. `off` stays a one-flip kill switch; promotion to `plan` is an explicit operator step.
- **[OQ 1](0051-reasoning-before-posting.md#open-questions) — `depth: deep` deferred to Phase 4, out of v0.3.10.** Excluded here; `validate` rejects it as unbacked.
- **[OQ 3](0051-reasoning-before-posting.md#open-questions) — reflexion is in scope as committed Phase 5**, feature-toggled off (`revise: 0`) and capability-gated.
- **[OQ 6(a)](0051-reasoning-before-posting.md#open-questions) — operator reveal is its own PR** (PR 7) alongside Phase 3, not bundled with the config knob.

### Sequencing

**Recommended merge order**: **PR 1 → PR 2 → PR 3 → PR 4 → PR 5 → PR 6 → PR 8 → PR 9**, with **PR 7 (operator reveal) sequenced flexibly** any time after PR 4 (it needs the `agent.deliberated`/`reason_note` shape, not the default flip).

Phases 1–2 (PR 1–3) are a strict chain — the seam carries the verdict before the plan rides it. Phase 3's config backend (PR 4) gates the surfaces (PR 5) and the go-live flip (PR 6). Phase 5 (PR 8) depends on Phases 2–3 (the plan to critique against + the config surface to toggle on). PR 9 closes out.

### File-size constraints (verified at plan authoring, cap = 500 per [`file_size.py --strict`](../../scripts/checks/file_size.py))

| File | Lines | Headroom | Routing |
|------|-------|----------|---------|
| [`agents/persona_runtime/action_loop.py`](../../agents/persona_runtime/action_loop.py) | **493** | **7** | The Phase 2 plan-append must be a **one-line `render_plan_section` call**; if it busts the cap, the compose-prompt assembly is the next extraction candidate ([RFC Phase 2](0051-reasoning-before-posting.md#phase-2-plan-threaded-compose)). |
| [`agents/salience_bid.py`](../../agents/salience_bid.py) | 468 | 32 | Room for the verdict restructure; watch it — the structured grammar adds parser branches. |
| [`agents/persona_runtime/salience_gate.py`](../../agents/persona_runtime/salience_gate.py) | 235 | ample | Seam threading + audit emit fit. |
| [`agents/observability/_metrics_salience.py`](../../agents/observability/_metrics_salience.py) | 76 | ample | **All new deliberation/reasoning instruments land here** (PR 1 parse-failure counter + the PR 6 deliberation-rate / suppress-by-`reason_code` / latency-histogram / starvation / divergence counters). |
| [`agents/observability/metrics.py`](../../agents/observability/metrics.py) | **500** | **0** | **At the hard cap — must not gain net lines.** PR 6 *reuses* the [`agent.llm.duration`](../../agents/observability/metrics.py) instrument **shape** from `_metrics_salience.py`; it does **not** add the new instrument definitions to `metrics.py`. If any edit to `metrics.py` proves unavoidable, extract first (it is not grandfathered — [`file_size.py`](../../scripts/checks/file_size.py) fails at `> 500`). |
| New: `deliberation_plan.py`, `reflexion.py` | — | — | Own modules (RFC Phase 2 / 5) precisely so `action_loop.py` stays under cap. |

---

## Dependency Graph

```
RFC 0030 Tier B + RFC 0050 + RFC 0034 P2 (all shipped)        ← HARD PREREQUISITES
   │
   ├── PR 1 (Phase 1a: salience_bid.py structured verdict behind internal `mode`,
   │     │   score-gate superseded, _BID_MAX_OUTPUT_TOKENS scaled; parse-failure counter)   [dark]
   │     ↓
   ├── PR 2 (Phase 1b: thread verdict through salience_gate seam; agent.deliberated audit;
   │     │   idle-cost gate test)   [dark]
   │     ↓
   ├── PR 3 (Phase 2: deliberation_plan.py CompositionPlan + parser + renderer;
   │     │   plan on SalienceOutcome; action_loop append; no-leak test)   [dark]
   │     ↓
   ├── PR 4 (Phase 3a: full reasoning.{mode,model,depth,revise} schema + RFC 0050 validate/
   │     │   apply/persist; deep + revise≥1 capability-rejected (revise validate-only here);
   │     │   default still off)
   │     ├──────────────────────────────────────────────┐
   │     ↓                                               ↓
   ├── PR 5 (Phase 3b: CLI enum/dotted-key path + web   PR 7 (OQ 6a: operator reveal —
   │     │   enum/select generalization; ui.yaml)            verbatim reason_note debug-egress
   │     ↓                                                    + timeline reveal)  ← separate/cuttable
   ├── PR 6 (Phase 3c: telemetry suite + FLIP default
   │     │   off → bid — GO-LIVE)
   │     ↓
   ├── PR 8 (Phase 5a: reflexion.py critic→revise loop; LIFT reasoning.revise gate +
   │     │   wire apply/persist (field defined in PR 4); CLI + web)   [default revise: 0]
   │     ↓
   └── PR 9 (Phase 5b + closeout: no-leak test → discarded draft; revise telemetry;
             review follow-ups; RFC + ROADMAP + CHANGELOG + MT-REASON-001)

   EXCLUDED from v0.3.10: Phase 4 (depth: deep) — needs provider-protocol change + OQ-1 telemetry trigger
```

---

## PR Sequence

### PR 1: `feature/v0310-rfc0051-silence-verdict` — Phase 1a: Structured Silence Verdict (dark)

**Depends on**: RFC 0030 Tier B (shipped).
**Purpose**: Replace the scalar bid grammar with `{ should_post, reason_code, reason_note }` and supersede the numeric score gate — gated behind an internal `mode` parameter defaulting to today's score gate, so the change is **behaviourally inert** until Phase 3.

#### Scope

| File | Change |
|------|--------|
| [`agents/salience_bid.py`](../../agents/salience_bid.py) | Restructure [`SalienceDecision`](../../agents/salience_bid.py): `should_post` **reuses** the existing `speak` boolean; `reason_code` **is** the existing `reason` low-cardinality label, value-set extended with the semantic cases (`only_agreeing` / `already_answered` / `nothing_to_add` / `adds_substance` / …) and *pruned* of the score-only `below_threshold`; `reason_note` is the one genuinely new field (optional, one short clause, **debug-only**). The bid emits the new `should_post:`/`reason_code:` wire grammar **only under `mode: bid|plan`**; under `mode: off` it emits the byte-for-byte `speak:`/`score:` scalar bid (the per-member `threshold` gate). **No `score` under reasoning** ([OQ 7](0051-reasoning-before-posting.md#open-questions)). Raise the output-token budget from the scalar 64, **scaled by `mode`** (a modest bump for `bid`) — as built, the single `_BID_MAX_OUTPUT_TOKENS` constant split into `_SCALAR_MAX_OUTPUT_TOKENS` (64) → `_BID_MAX_OUTPUT_TOKENS` (128) → `_PLAN_MAX_OUTPUT_TOKENS` (320) in [`salience_deliberation.py`](../../agents/salience_deliberation.py). Regex-tolerant parser, **fail-closed to silence**. |
| [`agents/observability/_metrics_salience.py`](../../agents/observability/_metrics_salience.py) | A **deliberation parse-failure counter**, kept **distinct** from the existing Tier-B `channel.messages.gated{policy=low_salience}` suppression rows — a fail-closed parse error is otherwise buried in the suppression totals and reads as intended dampening ([RFC Phase 1 §5](0051-reasoning-before-posting.md#phase-1-structured-silence-verdict-tier-b-generalization)). **This counter is the mandatory, never-gated safety net.** |
| `agents/tests/test_salience_bid.py` | Unit tests (below). |

#### Key implementation details

- **`mode` is an internal parameter here, not yet config-wired** — it threads from a hardcoded `off` default until [PR 4](#pr-4-featurev0310-rfc0051-config-backend--phase-3a-reasoning-config-backend) supplies it from the channel router. This is what lets Phase 1 ship dark: the score path is the default, the structured path is reachable only when a caller passes `bid`/`plan`.
- **`reason_code` is a single label, not a third field** — it *is* `SalienceDecision.reason`, the label the metric and the audit both read; folding the LLM's free-text justification onto it would blow up metric cardinality, so the prose lives in `reason_note` (debug egress only).
- **Mechanism change, not a pure add** — under reasoning the silence decision keys off `should_post` alone; the score/threshold machinery is bypassed. The `mode: off` regression test pins that the scalar path is unchanged.

#### Tests

- Well-formed structured response → correct `should_post` + `reason_code`.
- Malformed response → **fail-closed silence** *and* the parse-failure counter increments (asserted, not incidental).
- `mode: off` path is byte-for-byte the existing scalar bid (no `should_post`, score gate intact) — the dark-ship regression.
- The output-token budget scales with `mode` (as built: `_SCALAR_MAX_OUTPUT_TOKENS` 64 → `_BID_MAX_OUTPUT_TOKENS` 128 → `_PLAN_MAX_OUTPUT_TOKENS` 320).

#### PR checklist

- [ ] `pytest agents/tests/test_salience_bid.py -q` passes; `ruff` + `mypy` clean.
- [ ] `mode: off` regression proves byte-for-byte unchanged scalar behaviour (dark-ship gate).
- [ ] Parse-failure counter is distinct from `channel.messages.gated` and increments on fail-closed.
- [ ] No seam/config wiring yet (PR 2 / PR 4); structured path unreachable in prod.
- [ ] RFC 0051 Master-Index note `📋 Proposed → 🚧 Implementing` already applied by this planning PR; [v0.3.10-plan row 1](../v0.3.10-plan.md#master-progress-overview) → 🔄 In progress.

---

### PR 2: `feature/v0310-rfc0051-deliberate-seam` — Phase 1b: Seam Threading + Audit (dark)

**Depends on**: PR 1 merged.
**Purpose**: Thread the structured verdict through the Tier-B seam, emit the `agent.deliberated` audit event, and prove the idle path stays free.

#### Scope

| File | Change |
|------|--------|
| [`agents/persona_runtime/salience_gate.py`](../../agents/persona_runtime/salience_gate.py) | Thread the verdict through `run_salience_gate` → [`SalienceOutcome`](../../agents/persona_runtime/salience_gate.py) (the path [`action_loop.py`](../../agents/persona_runtime/action_loop.py) consumes); **reuse** the seam's existing `DO_NOTHING` + `_store_event_episode` suppressed-memory-ingest path (decide whether to respond, not whether to remember). Emit the `agent.deliberated` audit event: **decision + `reason_code` + counts, never the verbatim `reason_note` or plan** ([RFC §Security](0051-reasoning-before-posting.md#security-considerations)). |
| audit event registration | New `agent.deliberated` event in the RFC 0009 audit shape; add to the closed-set classifier (the severity-classification test fails otherwise). Forward-compatible precursor to RFC 0028's `DecisionRecord`. |
| [`agents/tests/test_persona_tick_shortcircuit.py`](../../agents/tests/test_persona_tick_shortcircuit.py) | Extend the idle-cost gate to assert **no deliberation lease is acquired on a `TICK`** — the seam *is* reached on a tick and no-ops there (`run_salience_gate` early-returns `None` because a `TICK` is not an open-floor admit — its guard is `if not (is_open_floor_admit(decision) and _governed(event))` at [`salience_gate.py`](../../agents/persona_runtime/salience_gate.py); `policy_always` is the unrelated Tier-A always-respond label in `salience_bid.py`), so the test asserts the no-op rather than assuming the path is unreachable ([RFC §F](0051-reasoning-before-posting.md#f-cost-and-the-idle-invariant)). |

#### Key implementation details

- **Post-commit audit** — `agent.deliberated` must survive a cancelled turn (the RFC 0009 §G "the decision already happened — don't drop the record" rule, applied on the Python emit path).
- **No new orchestration** — `action_loop.py` only *calls* `run_salience_gate` and consumes its outcome; the open-floor gating, channel-size cap, and suppression metrics are reused unchanged.

#### Tests

- A `should_post=false` turn (mode forced on in-test) → zero `SEND_CHANNEL_MESSAGE`, the existing `DO_NOTHING` outcome, memory still ingested, **one** `agent.deliberated` event carrying the `reason_code` (assertable, unlike free text).
- A `TICK` acquires **no** deliberation lease (idle-cost regression).
- The audit event never carries `reason_note` or plan content.

#### PR checklist

- [ ] `pytest agents/tests/test_persona_tick_shortcircuit.py -q` + the seam tests pass.
- [ ] `agent.deliberated` registered + classified in the closed-set severity test.
- [ ] Idle-tick no-deliberation-lease asserted (not assumed).
- [ ] Still dark — no production enable path (no config knob until PR 4).

---

### PR 3: `feature/v0310-rfc0051-plan-compose` — Phase 2: Plan-Threaded Compose (dark)

**Depends on**: PR 2 merged.
**Purpose**: Add the `CompositionPlan` artifact and thread it as a private system-prompt section into the Tier-C compose — proving the privacy wall.

#### Scope

| File | Change |
|------|--------|
| `agents/persona_runtime/deliberation_plan.py` (new) | The `CompositionPlan` value type (`intent` / `key_points ≤3` / `addressed_to` / `avoid_restating`), its regex-tolerant parser (**fail-closed to "no plan"** — an unparseable plan composes as today rather than blocking the post; the bias is opposite the gate's bias-to-*silence*, because the gate has already decided the persona *should* post), and a pure `render_plan_section(plan) -> str` renderer. **No `action_loop`/agent coupling** — unit-testable in isolation; the no-leak test points straight at it. |
| [`agents/persona_runtime/salience_gate.py`](../../agents/persona_runtime/salience_gate.py) | Carry `plan: CompositionPlan \| None` back on `SalienceOutcome`; the parser populates it alongside the gate verdict on the `should_post=true` path. The pure bid's `SalienceDecision` is **not** widened ([RFC §C "why two types"](0051-reasoning-before-posting.md#c-the-deliberation-verdict)). |
| [`agents/persona_runtime/action_loop.py`](../../agents/persona_runtime/action_loop.py) | Read `plan` off the seam outcome and append `render_plan_section(plan)` to the Tier-C compose system prompt (alongside the RFC 0034 working-memory sections). **A one-line call** — the assembly stays in `deliberation_plan.py`. File is at **493/500**; if the delta busts the cap, extract the compose-prompt assembly. |
| `tests/integration/persona/test_deliberation_no_leak.py` (new), `agents/tests/test_deliberation_plan.py` (new) | Plan parser + no-leak tests (below). |

#### Key implementation details

- **Never a message** — the plan is a distinct value type, never wrapped in an `AgentAction`, with no path to [`action_executor.py`](../../agents/action_executor.py)'s `SEND_CHANNEL_MESSAGE` handler; not persisted, so RFC 0034 reconstruction can never surface it into a peer's `messages` array.
- **Not `<external_data>`** — it is the persona's own trusted reasoning, a normal system-prompt section, not the RFC 0009 quarantine envelope (which is for tool/bridge output).

#### Tests

- Plan parser: well-formed → populated `CompositionPlan`; malformed → fail-closed "no plan" (composes unplanned, **does not block the post**).
- `render_plan_section` renders a stable private section.
- **No-leak (load-bearing)**: a `should_post=true` turn composes once *under* the plan; the plan appears in **zero** `messages` rows and in **zero** of a second persona's reconstructed `messages`.
- Both the deliberation and compose calls meter against **one** `interaction_id`; a low `interaction_budget_tokens` starves deliberation to silence.

#### PR checklist

- [x] `pytest tests/unit/python/test_deliberation_plan.py tests/unit/python/test_salience_gate_plan.py tests/integration/test_deliberation_no_leak.py -q` passes (the seam-threading leg landed as its own `test_salience_gate_plan.py`; the no-leak test lives under `tests/integration/`, matching the repo's actual test layout — there is no `tests/integration/persona/`).
- [x] `action_loop.py` ≤ 500 lines (one-line `render_plan_section` append; the section assembly lives in `deliberation_plan.py`). `salience_bid.py` held at 500 net-zero (the bid surfaces its raw text via an optional `deliberation_out`, the plan is parsed seam-side).
- [x] No-leak test green; `SalienceDecision` **not** widened (plan rides `SalienceOutcome` only; the bid hands back raw text, the seam parses the plan).
- [x] Still dark — the plan path is reachable only under `mode: plan`, not yet config-exposed (the `action_loop` seam call passes no `mode`, so prod stays `off`).

---

### PR 4: `feature/v0310-rfc0051-config-backend` — Phase 3a: `reasoning` Config Backend

**Depends on**: PR 3 merged.
**Purpose**: The full `reasoning.{mode,model,depth,revise}` schema on the RFC 0050 surface, capability-gated — **default still `off`** (the flip is PR 6). `mode`/`model` get the full validate→apply→persist path; `revise` is **defined here but validate-only** (every value `≥ 1` is capability-rejected — its apply/execution wiring lands with the loop in [PR 8](#pr-8-featurev0310-rfc0051-reflexion--phase-5a-reflexion-loop-default-off)). Defining the whole schema in one place keeps the capability-rejection uniform and avoids a window where `reasoning.revise` is an unknown-key error rather than a clean "Phase 5 not yet deployed" rejection.

#### Scope

| File | Change |
|------|--------|
| `internal/channels/…`, `internal/server/…` | `reasoning` config block (`mode`/`model`/`depth`/`revise`, full schema defined here) + apply path (router setters for `mode`/`model`) + REST PATCH/GET, through the existing RFC 0050 `validate → apply → persist → bump revision` path (runtime-editable, no restart). **`validate` gates the accepted enum set on *deployed capability*** ([RFC §G](0051-reasoning-before-posting.md#g-configuration--an-rfc-0050-knob)): reject `depth: deep` (Phase 4 unbuilt) and `revise ≥ 1` (Phase 5 not yet deployed) rather than silently degrading; reject `mode != off` on a channel without `salience_gated` (the knob does not by itself arm the gate); reject `depth: deep` unless `mode: plan`; **warn** on `model: quality` (defeats the cheap-pass economics). |
| [`config/channels.yaml`](../../config/channels.yaml) | `reasoning` block (default `mode: off`, `model: fast`, `depth: shallow`). |
| Go validate/apply tests | Capability-gated rejection + interaction-with-`threshold` tests (below). |

#### Key implementation details

- **The knob rides the existing Tier-B governance** — deliberation runs only on a `salience_gated` channel at an open-floor admit; `reasoning.mode` is inert on an ungoverned channel, so `validate` rejects `mode != off` there rather than letting it silently no-op.
- **`threshold` ↔ `mode` interaction is surfaced** — the per-member `threshold` is read only under `mode: off`; the config docs say so and `validate` notes it.
- **Reject-at-validate, not silent downgrade** — operator sets `deep`, gets an error, not `bid`. The classic feature-flag footgun, closed near-free on the existing path.

#### Tests

- `validate` rejects `depth: deep` and `revise ≥ 1` as unbacked (capability gate).
- `validate` rejects `mode != off` on an ungoverned channel; rejects `depth: deep` unless `mode: plan`.
- Apply/persist/revision-bump round-trips a `mode: bid` and `mode: plan` override (default unchanged at `off`).

#### PR checklist

- [ ] `go test ./internal/channels/ ./internal/server/ -run 'Reasoning|Config|Validate' -count=1` passes (`-race` clean).
- [ ] Capability-gated rejection tested for **every** unbacked value (`deep`, `revise≥1`).
- [ ] Governed-channel default still `off` (flip deferred to PR 6).
- [ ] No CLI/web surface yet (PR 5).

#### Review hardening (in-PR, post-implementation)

A critical read of [#695](https://github.com/mkhomutov/Persatrix/pull/695) surfaced three defects in how the new (router-held) `reasoning` knob threads through the **existing** RFC 0050 freeze/reconcile machinery — the first nested knob exposed seams the flat knobs never did. All three are fixed inside PR 4 (TDD; tests named below), so the knob is a *complete* member of the router-held set rather than wired into only some of its seams:

- **Snapshot omission → boot-clobber + blind drift (the load-bearing one).** `ChannelConfig.toConfigOverrides` — the "COMPLETE resolved governance set" the YAML reconcile persists and hashes — did **not** capture `reasoning`. Consequence for any channel with `revision: > 0` declaring a non-`off` rung: the boot round-trip `ResolveReasoning → ReconcileFromYAML → ResolveFromStore` re-stamped the store snapshot (which lacked reasoning) and **silently reset the rung to `off`**; and `channelConfigContentHash` was **blind** to a reasoning-only YAML edit (`off` and `bid` hashed identically), exactly the divergence drift-detection exists to catch. Fix: a single source of truth `ReasoningConfig.FreezeOverrides()` (per-sub-knob, conditional like the escalation chair), wired into both `toConfigOverrides` and the REST first-edit baseline. Pinned by `TestToConfigOverrides_CapturesNonDefaultReasoning`, `TestReconcileRoundTrip_ReasoningSurvivesBoot`, `TestChannelConfigContentHash_DistinguishesReasoning`. *(Latent on the shipped config — `planning` carries no `revision:` and ships `mode: off` — but a correctness trap for committed `bid` rungs and for PR 6's go-live.)*
- **Governance-drift blocked unrelated edits.** `validateReasoningGoverned` runs on every apply against the merged patch, so once `mode: bid` was persisted, the departure of the last `salience_gated` member made **every** later config PATCH (even an unrelated knob) 400 on a rung the operator never touched. Fix mirrors the escalation chair's drifted-member handling (`chairIsEnforceableMember`): the **first-edit baseline drops the inert non-off `mode`** when governance has drifted (`channelHasSalienceGatedMember`), matching what dispatch already does. Pinned by `TestChannelConfig_ReasoningFirstEditWithDriftedGovernanceDoesNotBlock`. *(The subsequent-edit case on an already-store-canonical channel remains the same accepted limitation the chair carries.)*
- **Whole-rung freeze could opt a channel out of the PR 6 flip.** The first-edit freeze keyed on whole-rung equality, so a channel non-default **only** because of `model: quality` (with `mode: off`) was frozen to an *explicit* `mode: off` — silently declining the future `off → bid` default flip an operator who only touched `model` never meant to decline. Fix: `FreezeOverrides` is **per-sub-knob** — `mode: off` stays inherit regardless of siblings (and the non-default `model` still freezes). Pinned by `TestChannelConfig_ReasoningOffModeStaysResponsiveDespiteNonDefaultModel`; the default-rung-stays-inherit branch is now also pinned by `TestChannelConfig_ReasoningFirstEditOnDefaultStaysInherit`.

Plus two non-behavioural cleanups: the dead `warning` return on `ReasoningConfig.validate` was removed (the `model: quality` warning is surfaced by `ResolveReasoning`/`ApplyChannelConfig`, which have a logger), and the stale "six/seven router-held knobs" doc counts were corrected to **eight** across `config_apply.go` and `buildChannelConfigResponse`.

---

### PR 5: `feature/v0310-rfc0051-config-surfaces` — Phase 3b: CLI + Web Config Surfaces

**Depends on**: PR 4 merged.
**Purpose**: Operator surfaces for `reasoning.*` — the **first enum-valued and first nested/dotted** RFC 0050 knob.

#### Scope

| File | Change |
|------|--------|
| [`cli/src/commands/channel_config.rs`](../../cli/src/commands/channel_config.rs) | Extend the hand-coded `CONFIG_KNOBS` registry + render map (pinned to the Go merge switch by test). `KnobType` is `Bool\|Int\|Str` today with **no enum**, and the `key=value` parser assumes **flat** keys — so add an **enum `KnobType`** for `mode`/`depth` and a **dotted-key (`reasoning.mode`) parse path**. Net-new on the CLI, not just a new row. |
| [`web/src/panels/ChannelSettings.svelte`](../../web/src/panels/ChannelSettings.svelte) | Add `reasoning.*` to the hardcoded `KNOBS` array + a **generic enum/select control branch**. The `<select>` primitive exists only as the `chair` branch (special-cased to chair candidates), so **generalize** it rather than introduce it. |
| [`config/ui.yaml`](../../config/ui.yaml) | `reasoning` web toggle. |

#### Key implementation details

- **This is the reasoning *config* knob only** — the [OQ 6(a)](0051-reasoning-before-posting.md#open-questions) reasoning-*reveal* panel (PR 7) is a separate UI surface, **not** bundled here.
- **Monotonic ladder** — `off → bid → plan` is a strict superset chain, so a channel is promoted/demoted one rung at a time with no re-plumbing.
- **The `depth` select is forward-compat scaffolding** — in v0.3.10 its only *accepted* value is `shallow` (`deep` is validate-rejected until Phase 4 ships). The enum control is built generically here anyway so adding `deep` later is a value-set change, not a new control; the panel should make `shallow` the only enabled option rather than render a lone dead `deep` entry.

#### Implementation notes (as built)

- **`config/ui.yaml` was a no-op.** `ui.yaml` is a panel-level feature-toggle surface (`enabled`/`create_enabled`/`config_edit_enabled`) with `additionalProperties: false`; reasoning editing rides the **existing** `channel_timeline.config_edit_enabled` toggle (already on) like every other governance knob, so there is no per-knob `reasoning` toggle to author — adding one would be a schema-validation error.
- **File-size cap forced a module split (mirrors the server).** The CLI surface pushed `channel_config.rs` over the 500-line cap, so the reasoning surface (value-sets, the dotted `KNOBS` slice, `ReasoningConfigView` + sub-knob accessor, the `nest_dotted` lifter, the enum value-set check) lives in a new [`channel_config_reasoning.rs`](../../cli/src/commands/channel_config_reasoning.rs) — exactly as the server split `channel_config_reasoning.go` out of `channel_config_handlers.go`. The flat `CONFIG_KNOBS` ∪ `reasoning::KNOBS` union is exposed via `editable_knobs()`.
- **The lockstep guards now span both Go switches.** `cli_knob_set_matches_server_merge_switch` / `cli_knob_types_match_server_decode_types` treat `reasoning` as a *namespace* (its `mergeConfigPatch` arm delegates, no `decodeKnob`) and parse the nested `reasoning.<sub>` leaf knobs from `channel_config_reasoning.go`, so the union stays pinned to the server. The name/type guards do **not** cover the enum *value-sets*, so a third guard — `cli_enum_value_sets_match_server_accepted_values` — parses the **accepted** values from the server's `ReasoningOverrides.validate()` switches and pins `MODES`/`MODELS`/`DEPTHS` to them (a Phase-4 `deep` promotion or a renamed value now fails here instead of round-tripping a 400; `deep` is correctly absent because it is a *rejected* case, the same gate `DEPTHS` encodes by omission).
- **YAML verbs (`export`/`import`/`diff`) defer the nested block — but loudly.** `export`/`diff` filter `config_rows` to the flat knobs. On the parse side `parse_channel_block` routes a reasoning key by shape: a flat dotted `reasoning.mode:` key is **rejected client-side** (it can never round-trip — the server's switch has only the `reasoning` namespace, no `reasoning.<sub>` leaf), and a nested `reasoning:` block (the form `config/channels.yaml` uses, applied by the boot loader) is dropped from the patch but sets `ParsedChannel.deferred_reasoning`, so `import`/`diff` emit a note (stderr) rather than silently swallowing it. Runtime `set`/`unset`/`get` and the web panel cover reasoning fully today; nested-block YAML round-tripping stays a follow-up.

#### Tests

- CLI `channel config set … reasoning.mode=plan` round-trips through PATCH/GET; the dotted-key parser nests `{reasoning: {mode}}`, enum validation rejects a bad value client-side, the registry stays pinned to the Go switches, and the value-set guard pins the enum vocab to the server's accepted set.
- CLI YAML: a flat dotted `reasoning.mode:` key is rejected client-side and a nested `reasoning:` block is flagged-and-skipped (`parse_channel_block_*` tests), so neither vanishes silently.
- Web panel renders the `mode`/`model`/`depth` selects, persists a **nested** sparse edit via `If-Match`, reverts a sub-knob with a nested explicit `null`, and nests `revise` as a JSON number.

#### PR checklist

- [ ] `cargo test` (CLI config) + `vitest` (web panel) pass; CLI↔Go knob-set pinning test green.
- [ ] Enum control + dotted-key path are generic (not `reasoning`-special-cased) so future enum/nested knobs reuse them.
- [ ] `ruff`/`clippy`/`go vet` clean; `make validate` green.

---

### PR 6: `feature/v0310-rfc0051-telemetry-golive` — Phase 3c: Telemetry + Default Flip (GO-LIVE)

**Depends on**: PR 5 merged.
**Purpose**: Land the observability that makes an active default safe, then **flip the governed-channel default `off → bid`** in lockstep — the moment the feature goes live.

#### Scope

| File | Change |
|------|--------|
| [`agents/observability/_metrics_salience.py`](../../agents/observability/_metrics_salience.py) (**new instruments land here** — `metrics.py` is at the 500-line cap, see [File-size constraints](#file-size-constraints-verified-at-plan-authoring-cap--500-per-file_sizepy---strict)) | Telemetry beyond the PR 1 parse-failure counter: **deliberation-rate**; **suppress-rate by `reason_code`** (silence charted by cause, not just totalled); a **deliberation-latency histogram** (the pass is a serial `fast` call *before* compose — reuses the [`agent.llm.duration`](../../agents/observability/metrics.py) instrument *shape*, defined here in `_metrics_salience.py`, **without editing `metrics.py`**); a **budget-starvation counter** (deliberation starved by a low `interaction_budget_tokens` — distinct from "nothing to add"); a **`should_post=true`-but-empty-compose divergence counter**. Kept distinct from the Tier-B `channel.messages.gated` rows. |
| `internal/channels/…` (router default) | **Flip the governed-channel default `off → bid`** ([OQ 2](0051-reasoning-before-posting.md#open-questions)) — the silence-only rung, never `plan`. `off` remains the one-flip kill switch. |
| cost-delta counterfactual | The two arms cannot run on one turn — measure cost-delta vs. baseline as a **`mode: bid` shadow arm** (the cheaper counterfactual, reuses the ladder) and record which. This deliverable **proves** the [RFC §F](0051-reasoning-before-posting.md#f-cost-and-the-idle-invariant) net-saving claim; it does not gate the ship and may stage in incrementally. |

#### Key implementation details

- **Added latency, not added spend, is the operational risk** — the deliberation is a serial `fast` call before compose; today's scalar bid is *already* that serial call, so the *added* latency is the larger structured generation. The latency histogram is the signal; flipping a channel to `off` is the immediate escape hatch.
- **The flip is coupled to the kill switch and telemetry on purpose** — Phases 1–2 shipped dark precisely so the score-gate supersession never goes live before its kill switch and watch-dogs exist.

#### Tests

- Each instrument emits on the right path (suppress-by-`reason_code`, latency on an admitted turn, starvation on a low budget, divergence on empty compose).
- A newly `salience_gated` channel now defaults to `mode: bid` (the flip); an existing `off` override is preserved.
- E2E smoke: a 3-persona `reasoning.mode: plan` brainstorm shows **fewer pile-on turns** than the `off` baseline (countable via suppress-rate).

#### Implementation notes (as built)

- **The go-live needed the dispatch wire, not just a router-default change.** Phases 1–2 left the rung dark because the agent-side seam never received a `mode` (the `action_loop` call passed nothing). PR 6 adds a `reasoning_mode` field to `ChannelMessageEvent` (proto field 25, stubs regenerated with the CI-pinned protoc 34.1), stamped at fanout from the (flip-aware) [`ChannelRouter.ReasoningFor`](../../internal/channels/router_reasoning.go) onto the [`DispatchEnvelope`](../../internal/channels/dispatch_envelope.go), lifted onto the event payload in [`channel_wire_metadata.py`](../../agents/channel_wire_metadata.py). **`action_loop.py` is untouched** (it is at the 500-line cap): the seam reads the rung off the payload itself (`_reasoning_mode`) when the caller pins no `mode`, mirroring how it already reads `salience_gated`/`threshold`. An empty/unknown rung fails safe to `off` (the scalar gate).
- **The flip is governance-aware, not a constant change.** `DefaultReasoningMode` stays `off` (the ungoverned/inherit baseline); a `governedDefaultMode(governed)` helper returns `bid` for a governed channel. It is threaded through every resolution + freeze seam — load-normalize ([`config.go`](../../internal/channels/config.go)), `validate`, `ResolveReasoning`, `applyOverridesToRouter` (now takes a `governed` arg, computed via a new `channelGoverned`), `FreezeOverrides` (now `governed`-aware so an explicit `off` is captured as the kill switch and survives boot replay / the YAML reconcile), the REST first-edit baseline, and the runtime-create path ([`applyRuntimeGroupGovernance`](../../internal/server/channel_governance.go)). So a channel resolves to `bid` the moment it is governed — at YAML load, boot, runtime-create, and runtime-edit — and an explicit `mode: off` is preserved across all of them. Pinned by the kill-switch-survives-boot tests in `config_reasoning_test.go` / `config_reconcile_test.go`.
- **Telemetry is module-owned in `_metrics_salience.py`.** `deliberation.{total,suppressed,duration,budget_starved}` live in module state with a `record_deliberation()` helper rather than on `_Instruments`, because `metrics.py` is at the 500-line cap and could not gain the class annotations the `inst.X` pattern needs (the File-size constraint above). `register()` recreates them on every `init_metrics`. The parse-failure counter (PR 1) stays on `inst` as the never-gated safety net.
- **The `should_post=true`-but-empty-compose divergence counter is deferred.** Its only emit site is post-compose in `action_loop.py`, which is at the 500-line cap; adding it needs the compose-prompt extraction the PR plan already names as the next candidate. Deferred to PR 9 closeout (instruments "may stage incrementally"), so PR 6 ships the four seam-side instruments (rate, suppress-by-`reason_code`, latency, starvation).
- **The cost-delta counterfactual is deferred to the live release-prep run** (Phase 3) — it proves the §F claim, it does not gate the ship.

#### PR checklist

- [x] `pytest` (metrics) + `go test` (default flip) pass; suppress-rate / latency / starvation instruments asserted on the right paths. (Live E2E suppress-rate delta is the Phase 3 release-prep deliverable.)
- [x] Parse-failure counter (PR 1) remains the never-gated safety net; new instruments may stage incrementally (divergence deferred — see notes).
- [x] Governed default is now `bid`; `off` kill switch verified one-flip and survives boot replay. (Cost-delta counterfactual arm deferred to the live run.)
- [x] **Feature is live.** CHANGELOG `[0.3.10]` seeded (the user-facing line lands with this PR).

---

### PR 7: `feature/v0310-rfc0051-operator-reveal` — OQ 6(a): Operator Reasoning Reveal (separate / cuttable)

**Depends on**: PR 4 merged (needs the `reason_note` shape). **Independent of the PR 5/6 chain** — may land any time alongside Phase 3, and is **droppable** without affecting the headline.

#### Scope

| File | Change |
|------|--------|
| backend debug-egress | A verbatim-`reason_note` (and, under a debug flag, the rendered plan) egress path to the agent debug log + a small web-reachable debug endpoint. The verbatim `reason_note` **does not reach the web today** — only the count-only `agent.deliberated` audit and the agent log do ([RFC §E](0051-reasoning-before-posting.md#e-privacy-boundary--the-trace-is-walled)). |
| web console (timeline-side) | A debug-toggled reasoning affordance in the channel-timeline panel (the surface [MT-CHANNEL-RELEVANCE-001](../manual-tests/MT-CHANNEL-RELEVANCE-001.md) already uses). **Net-new UI + backend — not** a `ChannelSettings.svelte` row. |

#### Key implementation details

- **You cannot tune the plan-generation prompt without seeing plans** — this is the operator tuning surface. The §E wall for **peer personas + the channel store stays absolute**; this is the opt-in operator-debug audience only.
- **End-user reveal stays deferred** — any future end-user "watch them think" surface is a *separate explicit egress*, never a relaxation of the §E wall ([OQ 6(b)](0051-reasoning-before-posting.md#open-questions)).

#### PR checklist

- [ ] The verbatim egress reaches **only** the operator-debug path; the `agent.deliberated` audit stays count-only.
- [ ] `MT-REASON-001`'s "stayed silent *with a stated reason*" leg is **already** observable via the operator-debug **agent log** ([RFC §E](0051-reasoning-before-posting.md#e-privacy-boundary--the-trace-is-walled) — the log is what the MT reads); this reveal only *additionally* surfaces it in the web console, so cutting this PR does **not** break the `MT-REASON-001` acceptance gate.
- [ ] Behind a debug toggle, off in prod by default; cuttable from the release without touching the headline.

---

### PR 8: `feature/v0310-rfc0051-reflexion` — Phase 5a: Reflexion Loop (default off)

**Depends on**: PR 6 merged (the plan to critique against + the live config surface).
**Purpose**: The committed critic→revise loop, feature-toggled off (`revise: 0`) and capability-gated.

#### Scope

| File | Change |
|------|--------|
| `agents/persona_runtime/reflexion.py` (new) | The critic + revise loop around the compose call — after Tier-C compose, a **critic** re-reads the draft against the `CompositionPlan` and, if it flags weakness, a **revise** pass rewrites it, bounded to `revise` rounds (`≤ 2`). **Fail-soft**: a parse/critic failure or exhausted lease degrades to the last good draft rather than blocking the post. Own module to keep `action_loop.py` under the 500-line cap (same reason as `deliberation_plan.py`). Discarded drafts + critic notes are walled exactly like the plan ([RFC §E](0051-reasoning-before-posting.md#e-privacy-boundary--the-trace-is-walled)). |
| `internal/channels/…`, `internal/server/…` | The `reasoning.revise` field + its `validate` were **defined in [PR 4](#pr-4-featurev0310-rfc0051-config-backend--phase-3a-reasoning-config-backend)** (validate-only, capability-rejected). This PR **lifts the capability gate** now that Phase 5 is deployed and **wires the apply/persist/revision-bump path** so a `revise ≥ 1` override takes effect. `validate` still rejects `revise ≥ 1` unless `mode: plan` (the critic checks the draft *against the plan*). |
| [`cli/src/commands/channel_config.rs`](../../cli/src/commands/channel_config.rs), [`web/src/panels/ChannelSettings.svelte`](../../web/src/panels/ChannelSettings.svelte) | `reasoning.revise` surface — a **plain `Int` knob** reusing the existing `Int` `KnobType` / `int` render branch; only needs the nested-key path the `reasoning.*` block already established in PR 5 (no new enum control). |

#### Key implementation details

- **Cost is bounded** — each revise round is another `quality`-model call, so an N-round post costs up to N+1 composes; hard-capped by the round limit (`≤ 2`) and the shared interaction lease (a low budget starves later rounds first, degrading to the already-composed draft).
- **Default `revise: 0`** keeps single-pass the norm; operators opt a channel up. Meaningful only under `mode: plan`.

#### Tests

- A weak draft triggers one revise round and improves; a strong draft is a no-op (the critic passes it).
- Round limit `≤ 2` enforced; an exhausted lease degrades to the last good draft (fail-soft, post not blocked).
- `validate` rejects `revise ≥ 1` unless `mode: plan`.

#### PR checklist

- [x] `pytest` (reflexion) + `go test` (validate) pass; fail-soft degradation proven.
- [x] `reasoning.revise` reuses the `Int` knob path (no new enum control).
- [x] Default `revise: 0`; capability gate lifted only now that Phase 5 is deployed.

#### Implementation notes (as built)

- **The CLI + web `reasoning.revise` surface was already built in [PR 5](#pr-5-featurev0310-rfc0051-config-surfaces--phase-3b-cli--web-config-surfaces).** PR 5 added the whole `reasoning.*` block — `("reasoning.revise", KnobType::Int)` in the CLI `reasoning::KNOBS` and a nested `int` knob in the web `KNOBS` array — so PR 8's surface work is *only* correcting the now-stale "capability-gated / Phase 5 not deployed" comments to the new "`revise ≥ 1` requires `mode: plan`" rule. No new control; the `Int` path the plan named was already in place.
- **`validate` lifts the blanket Phase-5 gate and replaces it with a cross-field rule + a cap.** `revise < 0` or `> MaxReasoningRevise (2)` is rejected per-field (both the load-time `ReasoningConfig.validate` and the REST `ReasoningOverrides.validate`); `revise ≥ 1` additionally requires the **effective** `mode: plan`. The full-struct path checks `rc.Mode` directly; the override path's cross-field check lives in `validateReasoningGoverned` (alongside the mode↔governance rule) because it needs the *merged* mode — a `revise` PATCH may not touch `mode`. Since `plan` is never a default, an effective `plan` can only come from an explicit override, so this is a clean reject of "set revise without promoting the rung."
- **The revise count crosses the wire like the mode (PR 6).** A new proto field `ChannelMessageEvent.reasoning_revise = 26` (`int32`, stubs regenerated with the CI-pinned protoc 34.1), stamped at fanout from `ChannelRouter.ReasoningFor(channelID).Revise` onto the `DispatchEnvelope`, lifted onto the event payload in `channel_wire_metadata.py`. The seam carries it onto `SalienceOutcome.revise` **only on the `plan` rung** (and pins it to 0 when the plan failed to parse — fail-open), clamped to `MAX_REVISE_ROUNDS` as defense-in-depth.
- **`action_loop.py` stayed under the 500-line cap via the named extraction.** The compose-prompt assembly (persona base + working memory + plan section) moved to a new `compose_prompt.py` — the next extraction candidate the plan named — freeing room for a one-line `maybe_revise_channel_message` call after `synthesize_channel_reply`. `run_reflexion` itself imports no agent/action type (unit-testable in isolation); the thin `maybe_revise_channel_message` glue in `reflexion.py` is the only `AgentAction`-aware seam, replacing only the one `SEND_CHANNEL_MESSAGE` action's content on a successful rewrite.
- **Critic on `fast`, revise on the compose model.** The critic is the cheap leased `fast` judgement (not one of the §F composes); only a `weak: yes` verdict pays the `quality` rewrite, so the cost is the RFC's `N+1` composes. Both passes bill against the same interaction lease as the compose, so a low budget starves the later rounds first. The no-leak extension to a discarded draft + critic note is [PR 9](#pr-9-featurev0310-rfc0051-close--phase-5b--closeout)'s leg (the existing plan-leak test stays green here).

---

### PR 9: `feature/v0310-rfc0051-close` — Phase 5b + Closeout

**Depends on**: PR 8 (and PR 7 if not cut) merged.
**Purpose**: Extend the privacy wall to reflexion intermediates, add revise telemetry, fold review follow-ups, and mark RFC 0051 implemented.

#### Scope

| File | Change |
|------|--------|
| `tests/integration/test_deliberation_no_leak.py` | Extend the no-leak test to cover a **discarded draft** and a critic note — never an `AgentAction`, never persisted; only the final revised message is published. |
| observability | A **revise-round counter** + a **draft-changed / no-op-revise / degraded** signal, reusing the Phase 3 instrument shapes (the `degraded` outcome keeps a fail-soft hiccup separable from a strong-draft no-op). |
| (various) | `From PR N review` follow-ups, paraphrased inline (never linking a local review report per [.github/copilot-instructions.md](../../.github/copilot-instructions.md)). |
| [`docs/rfcs/0051-reasoning-before-posting.md`](0051-reasoning-before-posting.md) | Status → `✅ Implemented`; "Implemented in v0.3.10" note in Decision/Next Steps; `make rfcs` to regenerate [INDEX.md](INDEX.md). |
| [`ROADMAP.md`](../../ROADMAP.md) | RFC 0051 Master-Index row → `✅ Implemented`; `Last updated` + Current-phase refresh. The v0.3.10 Version-Map row → ✅ Released is **deferred to the Phase 4 tag** (RFCs flip to Implemented in their closeout PR; the version releases at the tag — the v0.3.9 precedent + master plan §ROADMAP-hygiene). |
| [`CHANGELOG.md`](../../CHANGELOG.md) | `[0.3.10]` finalized. |
| [`docs/guides/persona-agents.md`](../guides/persona-agents.md), [`docs/manual-tests/MT-REASON-001.md`](../manual-tests/) (new) | Document the `reasoning` knob + reasoning-before-posting behaviour; author `MT-REASON-001` (live execution is a release-prep deliverable). |

#### PR checklist

- [x] No-leak test covers a discarded draft + critic note (privacy wall complete) — `tests/integration/test_deliberation_no_leak.py::TestReflexionDraftAndCritiqueNeverLeak`.
- [x] `make validate` + lint clean across Python (ruff/mypy); `pytest` green for the reflexion/metrics/no-leak suites. (Full `make test` Go/Rust/web + live Docker smoke is the Phase 3 release-prep deliverable.)
- [x] RFC 0051 status flipped; `make rfcs` regenerated `INDEX.md`.
- [x] `CHANGELOG.md` `[0.3.10]` finalized; ROADMAP + [v0.3.10-plan](../v0.3.10-plan.md#master-progress-overview) reflect final state.
- [x] `MT-REASON-001` authored; `docs/guides/persona-agents.md` documents the knob.

---

## Risk and Mitigations

| Risk | Mitigation |
|------|------------|
| The score-gate supersession changes silence semantics and **goes live before its kill switch exists**. | Phases 1–2 ship **dark** (gated behind `mode`, default `off` = byte-for-byte today's score gate); the default flips to `bid` only in **PR 6**, in lockstep with the `off` kill switch and the telemetry that watches it. |
| **Private-reasoning leakage** — the plan/reason is the most context-revealing artifact a persona produces. | Structural wall ([RFC §E](0051-reasoning-before-posting.md#e-privacy-boundary--the-trace-is-walled)): the plan is a distinct value type, never an `AgentAction`, never persisted, unreachable by RFC 0034 reconstruction. The no-leak test (PR 3, extended PR 9) pins it for the plan *and* a discarded reflexion draft. |
| **Added latency** on every admitted turn (a serial `fast` call before compose). | The scalar bid is *already* that serial call; the added latency is the larger structured generation. The PR 6 latency histogram is the signal; `mode: off` is the one-flip escape hatch. |
| **Net-negative-cost is an empirical claim**, not a given — deliberation *adds* output on every admitted turn and only *saves* on incremental semantic suppressions. | PR 6's cost-delta counterfactual (a `mode: bid` shadow arm) proves or refutes it; it does not gate the ship. |
| **File-size cap** breach in `action_loop.py` (493/500). | The plan-append is a one-line `render_plan_section` call; the assembly lives in `deliberation_plan.py`. PR 3 checklist re-verifies the cap; the compose-prompt assembly is the named next extraction. |
| **Reflexion multiplies compose cost** (up to N+1 composes). | Hard-capped by the round limit (`≤ 2`) + the shared interaction lease (starves later rounds first); default `revise: 0` keeps single-pass the norm. |
| Operator sets an **unbacked enum value** (`deep`, or `revise≥1` pre-Phase-5) and silently gets a lesser rung. | `validate` is **capability-gated** — it rejects unbacked values with an error rather than degrading (PR 4 / PR 8). |
| Phase 4 (`depth: deep`) **pulled into v0.3.10** prematurely. | Excluded by decision — it needs a provider-protocol change *and* the OQ-1 telemetry trigger that only Phase 3 data can satisfy. `validate` rejects it until built. |

---

## ROADMAP Hygiene

Per [.github/copilot-instructions.md §Status Hygiene](../../.github/copilot-instructions.md) and [v0.3.10-plan §ROADMAP hygiene](../v0.3.10-plan.md#roadmap-hygiene):

- **This planning PR** → RFC 0051 Master-Index note `📋 Proposed → 🚧 Implementing` (front-matter `status:` + `make rfcs`); v0.3.10 Version-Map row added (📋 Planned); [v0.3.x-sequencing.md](../v0.3.x-sequencing.md) amendment recorded.
- **PR 1 opens** → [v0.3.10-plan row 1](../v0.3.10-plan.md#master-progress-overview) → 🔄 In progress.
- **PR 6 merges (go-live)** → seed CHANGELOG `[0.3.10]`; `Last updated` refresh.
- **PR 9 merges** → RFC 0051 → `✅ Implemented`; `Last updated` + Current-phase refresh; docs updated. The v0.3.10 Version-Map row → ✅ Released is **deferred to the Phase 4 tag** (the version releases at the tag, not at the RFC closeout — see the master plan §ROADMAP-hygiene + the v0.3.9 precedent).

---

## Progress Overview

| # | Phase | Title | Branch | Status | GitHub PR | Merged |
|---|-------|-------|--------|--------|-----------|--------|
| 1 | 1a | Structured silence verdict (dark) | `feature/v0310-rfc0051-silence-verdict` | ✅ Merged | [#692](https://github.com/mkhomutov/Persatrix/pull/692) | ✅ |
| 2 | 1b | Seam threading + `agent.deliberated` audit (dark) | `feature/v0310-rfc0051-deliberate-seam` | ✅ Merged | [#693](https://github.com/mkhomutov/Persatrix/pull/693) | ✅ |
| 3 | 2 | Plan-threaded compose + no-leak test (dark) | `feature/v0310-rfc0051-plan-compose` | ✅ Merged | [#694](https://github.com/mkhomutov/Persatrix/pull/694) | ✅ |
| 4 | 3a | `reasoning` config backend (capability-gated) | `feature/v0310-rfc0051-config-backend` | ✅ Merged | [#695](https://github.com/mkhomutov/Persatrix/pull/695) | ✅ |
| 5 | 3b | CLI + web config surfaces (enum + dotted-key) | `feature/v0310-rfc0051-config-surfaces` | ✅ Merged | [#696](https://github.com/mkhomutov/Persatrix/pull/696) | ✅ |
| 6 | 3c | Telemetry + default flip `off → bid` (GO-LIVE) | `feature/v0310-rfc0051-telemetry-golive` | ✅ Merged | [#697](https://github.com/mkhomutov/Persatrix/pull/697) | ✅ |
| 7 | OQ 6a | Operator reasoning reveal (separate / cuttable) | `feature/v0310-rfc0051-operator-reveal` | ⏭ Cut from v0.3.10 | — | — |
| 8 | 5a | Reflexion loop (default `revise: 0`) | `feature/v0310-rfc0051-reflexion` | ✅ Merged | [#698](https://github.com/mkhomutov/Persatrix/pull/698) | ✅ |
| 9 | 5b | No-leak extension + closeout | `feature/v0310-rfc0051-close` | ✅ Merged | [#699](https://github.com/mkhomutov/Persatrix/pull/699) | ✅ |

**Status legend**: ⬜ Not started · 🔄 In progress · 🔀 PR open · ✅ Merged · ⏭ Deferred
**Excluded from v0.3.10**: Phase 4 (`depth: deep` native extended thinking) — deferred behind the OQ-1 telemetry trigger + a provider-protocol change.

---

## Related Documentation

- [RFC 0051 — Reasoning Before Posting](0051-reasoning-before-posting.md) — canonical spec; §C verdict/plan types, §D mechanism, §E privacy wall, §F idle/cost, §G config knob, the seven resolved OQs.
- [v0.3.10-plan.md](../v0.3.10-plan.md) — master version plan (this is its Phase 1 workstream); the release contract + release-prep + tag phases.
- [RFC 0030 — Multi-Agent Conversation Governance](0030-multi-agent-conversation-governance.md) + [relevance-gated-response amendment](0030-amendment-relevance-gated-response.md) — the Tier A/B/C gate this generalizes (Tier B is the seam).
- [RFC 0050 — Extensible Channel Configuration](0050-extensible-channel-configuration.md) — the `validate → apply → persist` config surface the `reasoning` knob rides.
- [RFC 0034 — Persona Conversational Working Memory](0034-persona-conversational-working-memory.md) — the transcript reconstruction the private trace must stay out of.
- [RFC 0023 — LLM Call Leasing](0023-llm-call-leasing.md) / [RFC 0024 — Event-Driven Scheduling](0024-event-driven-scheduling.md) — the metering + idle-cost invariants.
- [RFC 0028 — Agent Decision Policy Engine](0028-agent-decision-policy-engine.md) — the v0.4.x decision engine this is distinct from and forward-compatible with (`agent.deliberated` → `DecisionRecord`).
- [RFC 0009 — Agent Identity, Security & Sandboxing](0009-security-sandboxing.md) — the audit shape + post-commit-emit rule.
- [BRANCHING.md](../BRANCHING.md) — squash-merge + file-size-cap conventions.
