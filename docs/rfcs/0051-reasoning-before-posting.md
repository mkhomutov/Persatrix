---
# Allowed values are documented in README.md. The YAML front-matter is the
# source of truth read by `scripts/rfcs.py` to regenerate INDEX.md — keep
# it in sync with the bold-markdown header below (which is what GitHub
# renders for human readers).
id: RFC-0051
title: "Reasoning Before Posting"
summary: "A private per-turn deliberation a persona runs before publishing a channel message — generalizes the RFC 0030 Tier-B salience bid into a structured should-post + plan verdict, threaded privately into the compose call so posts are considered rather than reflexive."
type: feature
status: proposed
author: Maksim Khomutov
created: 2026-06-22
target: "v0.3.x"
depends_on:
  - RFC-0030
  - RFC-0034
  - RFC-0023
  - RFC-0024
  - RFC-0017
  - RFC-0050
  - RFC-0009
---

# RFC 0051 — Reasoning Before Posting

**Type**: feature  
**Status**: 📋 Proposed  
**Author**: Maksim Khomutov  
**Date**: 2026-06-22  
**Target**: v0.3.x  
**Depends on**: RFC 0030 (relevance gate / Tier B), RFC 0034 (conversational working memory), RFC 0023 (LLM call leasing), RFC 0024 (event-driven scheduling — idle-cost invariant), RFC 0017 (memory-injection budget — the empty-context TICK short-circuit), RFC 0050 (extensible channel configuration), RFC 0009 (audit / prompt-safety boundary)

---

## Table of Contents

- [Summary](#summary)
- [Motivation](#motivation)
- [Goals](#goals)
- [Non-Goals](#non-goals)
- [Design / Implementation](#design--implementation)
  - [A. Positioning — distinct from RFC 0028](#a-positioning--distinct-from-rfc-0028)
  - [B. Where it sits — generalize Tier B](#b-where-it-sits--generalize-tier-b)
  - [C. The deliberation verdict](#c-the-deliberation-verdict)
  - [D. Mechanism — three realizations, one recommendation](#d-mechanism--three-realizations-one-recommendation)
  - [E. Privacy boundary — the trace is walled](#e-privacy-boundary--the-trace-is-walled)
  - [F. Cost and the idle invariant](#f-cost-and-the-idle-invariant)
  - [G. Configuration — an RFC 0050 knob](#g-configuration--an-rfc-0050-knob)
- [Security Considerations](#security-considerations)
- [Phased Implementation Plan](#phased-implementation-plan)
- [Files Touched (Estimated)](#files-touched-estimated)
- [Test Strategy](#test-strategy)
- [Open Questions](#open-questions)
- [Decision / Next Steps](#decision--next-steps)
- [Related Documentation](#related-documentation)

---

## Summary

A persona should **think before it speaks**: before it publishes a message into a channel, it privately works out *whether* this turn is worth a post and *what* the post should actually accomplish, and only then composes. This RFC adds a private, per-turn **deliberation** stage to the persona runtime that generalizes the existing [RFC 0030](0030-multi-agent-conversation-governance.md) Tier-B salience bid from a bare `speak/score` verdict into a structured `{ should_post, plan }` object. When `should_post` is false the turn ends in `DO_NOTHING` *before* paying for an expensive compose; when it is true, the `plan` is threaded into the existing compose call as a **private prompt section** that never reaches the channel. The deliberation reuses the leased `fast`-model seam already in production, so it adds a bounded, metered cost on a real turn and is a **net saving** on the pile-on turns it suppresses.

## Motivation

Manual testing of the v0.3.6+ group channels surfaced the failure the relevance gate only partly closed: personas still **post reflexively**. They answer questions aimed at someone else, restate what a peer just said, pile on with "I agree" turns, and compose the first thing the model produces rather than the most useful thing. The [RFC 0030 relevance-gated-response amendment](0030-amendment-relevance-gated-response.md) attacked the *eligibility* half of this (Tier A addressing, Tier B salience-score), but two gaps remain:

1. **The silence decision is heuristic, not semantic.** Tier B returns a numeric salience score with a bias-to-silence threshold. It cannot articulate *why* a post would or would not add value, so it both over-admits ("score cleared threshold, but I'd only be agreeing") and under-admits.
2. **The post itself is uncon­sidered.** Once a persona is admitted to speak (Tier C), it composes in a single pass with no private step where it decides what this specific contribution is *for* — what to add, whom to address, what not to restate.

Doing nothing leaves the brainstorm-usefulness story (the v0.3.x thesis) capped: conversations *converge* (v0.3.8) but individual contributions still read as eager rather than deliberate. "Reasoning before posting" raises **post quality** and **silence quality** at the same seam, and is the natural next rung after "conversations that converge" — *conversations worth posting into*.

This is a v0.3.x-sized realism lever with no v0.4.0 dependency. It is explicitly **not** the [RFC 0028](0028-agent-decision-policy-engine.md) decision engine (see [§A](#a-positioning--distinct-from-rfc-0028)).

## Goals

1. **Semantic silence.** A persona can privately decide "I have nothing to add here" with a reason, suppressing the post *before* the expensive compose call — turning pile-on suppression into a cost saving.
2. **Considered posts.** When a persona does post, the message is composed *under a private plan* (intent, key points, who it addresses, what not to restate) rather than reflexively.
3. **Bounded, metered cost.** The deliberation acquires a wallet lease ([RFC 0023](0023-llm-call-leasing.md)) against the same `interaction_id`, so it is metered against the per-interaction budget and can never blow it.
4. **Idle stays free.** The deliberation fires only on a real turn; a bored persona ([RFC 0024](0024-event-driven-scheduling.md)) still costs nothing.
5. **The trace is private and auditable.** The reasoning never enters the channel store and never reaches another persona's working memory; its only durable egress is a count/decision audit event (never verbatim text), with a separate opt-in operator-debug path for tuning ([§E](#e-privacy-boundary--the-trace-is-walled)).
6. **Operator-controlled, off by default.** A per-channel `reasoning` knob fits the [RFC 0050](0050-extensible-channel-configuration.md) config surface; the default is conservative.

## Non-Goals

- **Group deliberation toward a decision.** Reasoning *across* personas toward a justified recommendation is [RFC 0028](0028-agent-decision-policy-engine.md) (v0.4.x). This RFC is per-persona, per-turn, and private.
- **Action-class selection.** Choosing between `respond` / `ask_clarification` / `delegate` / `end_vote` is RFC 0028's pre-act checkpoint. This RFC reasons *inside* the already-selected `publish_channel` lane.
- **A draft → critique → revise (reflexion) loop.** A multi-round self-critique is explicitly deferred ([OQ 3](#open-questions)); Phase 1–3 is a single deliberation pass.
- **Replacing Tier A.** The free, deterministic addressing gate ([response_gate.py](../../agents/response_gate.py)) stays exactly as it is; deliberation runs only on turns Tier A admits.
- **A durable reasoning store.** The trace is ephemeral + audit-only; no new persisted "thoughts" tier.
- **An end-user "watch them think" surface.** Revealing the private reasoning *to people observing the channel* is a separate, explicit egress mode that would change the [§E](#e-privacy-boundary--the-trace-is-walled) privacy contract — not a relaxation of the wall, and not in scope here. Named as a deliberate future, not foreclosed ([OQ 6](#open-questions)).
- **A persistent reasoning trace for offline eval.** Operator-debug egress ([§E](#e-privacy-boundary--the-trace-is-walled)) is ephemeral log output, not a durable, queryable trace store. A retention-bounded eval sink for prompt tuning is a possible follow-on, not Phase 1–3.

## Design / Implementation

### A. Positioning — distinct from RFC 0028

| Axis | RFC 0051 (this) | RFC 0028 (v0.4.x) |
|------|-----------------|-------------------|
| Scope | one persona, one turn | the conversation / decision |
| Decides | *what to privately think before composing a post* | *which action class to attempt*, auditably |
| Visibility | private to the persona | a `DecisionRecord` surface |
| Target | v0.3.x | v0.4.0+ |

The two **stack**: RFC 0028 decides the persona is in the `publish_channel` lane; RFC 0051 is the private deliberation *inside* that lane. RFC 0051's audit event is deliberately shaped as a forward-compatible precursor to RFC 0028's `DecisionRecord`, so the later engine subsumes it rather than colliding with it.

### B. Where it sits — generalize Tier B

The persona turn already runs a three-tier gate in [`agents/persona_runtime/action_loop.py`](../../agents/persona_runtime/action_loop.py):

- **Tier A** — `evaluate_response_gate` ([`response_gate.py`](../../agents/response_gate.py)): free, deterministic addressing/eligibility. Drop → `DO_NOTHING`.
- **Tier B** — `run_salience_gate` ([`salience_gate.py`](../../agents/persona_runtime/salience_gate.py)), the action-loop seam that runs the *pure* bid `evaluate_salience` ([`salience_bid.py`](../../agents/salience_bid.py)): a leased `fast`-model bid (≈64 tokens, temp 0.0, bias-to-silence) that runs *only* on the ambiguous open-floor admit. Drop → `DO_NOTHING`.
- **Tier C** — the `quality`-model compose call (multi-turn tool loop), parsed into actions and published.

Tier B **is the seam.** "Reasoning before posting" is Tier B promoted from a scalar verdict into a structured deliberation. No new orchestration layer is introduced: the pure bid in [`salience_bid.py`](../../agents/salience_bid.py) (the leased call, regex-tolerant parsing, and fail-closed-to-silence default) and its action-loop seam in [`salience_gate.py`](../../agents/persona_runtime/salience_gate.py) (open-floor gating, channel-size cap, suppression metrics, and the suppressed-message memory ingest) are both reused — `action_loop.py` only *calls* `run_salience_gate` and consumes its outcome.

```
inbound → idle guard → Tier A (free) → ┌─ DELIBERATE (fast, leased) ─┐ → Tier C compose (quality) → publish
                                       │   should_post? + plan        │      (under the private plan)
                                       └─ should_post=false → DO_NOTHING
```

### C. The deliberation verdict

The deliberation pass emits **two structurally distinct artifacts**, both parsed prompt-side from one `fast`-model response (there is no `response_format` in the provider protocol today). They are kept as **separate value types** because they have separate consumers: the *gate verdict* is the silence gate's business; the *plan* is compose-stage payload the gate never reads.

**1. The gate verdict** — an enrichment of the existing scalar [`SalienceDecision`](../../agents/salience_bid.py) with the semantic-silence fields. This is all the Tier-B gate consumes to choose `DO_NOTHING` vs. proceed:

```
{ "should_post": true | false,
  "reason_code": "only_agreeing" | "already_answered" | "nothing_to_add"
               | "adds_substance" | ...,   // closed enum — see below
  "reason_note": "<optional one short clause — debug only, never a metric/audit attribute>" }
```

**Two reason fields, deliberately.** The existing [`SalienceDecision.reason`](../../agents/salience_bid.py) is already a *low-cardinality branch label* (`parse_failure` / `lease_denied` / `llm_error` / …) that the Tier-B seam emits as the `reason` attribute on the `channel.messages.gated{policy=low_salience}` counter ([`salience_gate.py`](../../agents/persona_runtime/salience_gate.py)). Folding the LLM's free-text justification onto that field would blow up metric cardinality (one time series per unique sentence) and make the silence decision un-assertable in a test. So the semantic verdict splits in two: a **closed `reason_code` enum** (the metric- and audit-safe label — the existing fail-closed branch set extended with the *semantic*-silence cases) and an **optional `reason_note`** free clause that egresses to the debug log only ([§E](#e-privacy-boundary--the-trace-is-walled)). The `reason_code` is also the forward-compatible precursor to RFC 0028's `DecisionRecord` reason field — a structured code, not prose.

**2. The composition plan** — a *separate* `CompositionPlan` value object, present only when `should_post = true`, owned by the new `deliberation_plan.py` module ([§Phase 2](#phase-2-plan-threaded-compose)) and **never carried on the pure bid's verdict**:

```
{ "intent": "<what this contribution is for>",
  "key_points": ["...", "..."],      // ≤3, the substance to land
  "addressed_to": "<participant_id | 'channel'>",
  "avoid_restating": ["..."] }       // what peers already said; don't repeat
```

**Why two types, not one.** The plan is irrelevant to the silence decision — it is *transported through* the gate to the compose stage, not used by it. Folding it onto `SalienceDecision` would couple the gate's return type to compose-stage data and turn the pile-on gate into a carrier for a field it ignores (a leaky value type). Instead the gate verdict stays on `SalienceDecision`; the `CompositionPlan` rides back on the seam's [`SalienceOutcome`](../../agents/persona_runtime/salience_gate.py) as `plan: CompositionPlan | None` — the type that *already* hands the reusable `user_message` + `seed` back to the action loop. One LLM call still yields both fields; "parsed from one response" does not require "one type." The split keeps the pure bid focused, lets the plan be unit-tested in isolation, and makes the plan the artifact Phase 4's `depth: deep` is free to evolve without touching the gate.

`should_post=false` short-circuits to the existing `DO_NOTHING` outcome (and still ingests memory: the Tier-B seam already calls `_store_event_episode` on a suppressed turn in [`salience_gate.py`](../../agents/persona_runtime/salience_gate.py) — "decide whether to respond, not whether to remember" — the same rule the Tier-A path enforces in [`gate_suppress.py`](../../agents/persona_runtime/gate_suppress.py)). When `should_post=true`, the `CompositionPlan` is rendered into a single **private system-prompt section** appended for the Tier-C compose only, alongside the RFC 0034 working-memory sections — never published, never written to the channel store ([§E](#e-privacy-boundary--the-trace-is-walled)).

### D. Mechanism — three realizations, one recommendation

There is no extended-thinking path in the codebase today; the providers in [`agents/llm_providers.py`](../../agents/llm_providers.py) accept only `model / messages / system / tools / max_tokens / temperature`. So the realization is a genuine fork:

| Option | Mechanism | Cost / turn | Assessment |
|--------|-----------|-------------|------------|
| **A — Two-pass (generalize Tier B)** | `fast`-model deliberation → plan → `quality` compose under the plan | one cheap pass + compose only when posting | **Recommended.** Reuses the leased salience seam; cheap hard-suppress; zero provider-protocol change. |
| B — Native extended thinking | add a `thinking` budget to the `quality` compose call | single call; thinking tokens billed as output | Best raw reasoning quality, but needs a provider-protocol change *and* the lease must budget thinking tokens. **Follow-on / opt-in.** |
| C — Single structured `quality` call | one call emits `{reasoning, should_post, message}`, strip reasoning before publish | full compose cost even on silence | Weakest — pays the quality price to decide *not* to post. |

**Decision: ship Option A** as the v0.3.x slice; record **Option B** as an opt-in `reasoning.depth: deep` upgrade gated on a provider-protocol change ([OQ 1](#open-questions)). Option A delivers the silence win immediately, amortizes cost (a cheap pass kills an expensive compose), and threads cleanly through the existing model-alias layer ([`model_aliases.py`](../../agents/model_aliases.py)) so the deliberation can run on `fast` while compose stays on `quality`.

### E. Privacy boundary — the trace is walled

The plan and reason are the most context-revealing artifacts a persona produces, so they are fenced. "Private" here is **not binary** — it resolves against three distinct audiences, and the wall is absolute only for the first two:

| Audience | Sees the plan / reason? | Mechanism |
|----------|-------------------------|-----------|
| **Peer personas + channel store** | **Never** | Structural — the plan is not an `AgentAction`, is never persisted, and is unreachable by RFC 0034 reconstruction (bullets below). Load-bearing for the no-pile-on premise. |
| **Human operator (debug)** | **Opt-in** | The verbatim `reason_note` (and, under a debug flag, the rendered plan) egress to the agent **debug log** — the same channel [MT-CHANNEL-RELEVANCE-001](../manual-tests/MT-CHANNEL-RELEVANCE-001.md) already uses to observe Tier-B silence — plus a proposed web-console reveal ([OQ 6](#open-questions)). *Not* the audit event. |
| **End-users watching the channel** | **No (deferred)** | A "show the reasoning" product surface is a separate, explicit egress mode, not a relaxation of this wall — out of scope here ([Non-Goals](#non-goals), [OQ 6](#open-questions)). |

The structural wall for the first two audiences:

- **Never a message.** The plan is a distinct `CompositionPlan` value type ([§C](#c-the-deliberation-verdict)), never wrapped in an `AgentAction` and never published; it has no path to [`action_executor.py`](../../agents/action_executor.py)'s `SEND_CHANNEL_MESSAGE` handler. Keeping it a *separate type* from the published-message artifacts makes "this is private" a structural property the no-leak test pins — not a convention that the next edit could quietly violate.
- **Never in the channel store.** It is not persisted as a message, so [RFC 0034](0034-persona-conversational-working-memory.md) transcript reconstruction can never surface it into *another* persona's `messages` array.
- **Not `<external_data>`.** It is the persona's *own* reasoning, not untrusted external input, so it is a normal trusted system-prompt section — not wrapped in the [RFC 0009](0009-security-sandboxing.md) quarantine envelope (which is for tool/bridge output).
- **Two egress paths, not one.** The **audit** event (`channel.reason`) records *decision + `reason_code` + counts, never the verbatim `reason_note` or plan* — low-cardinality, durable trend data in the RFC 0009 audit shape. The **operator-debug** path (agent log, off in prod by default) carries the verbatim `reason_note` for tuning and is what `MT-REASON-001` reads to confirm "stayed silent *with a stated reason*." The two are deliberately separate contracts: audit = low-cardinality + durable; debug = verbatim + ephemeral.

### F. Cost and the idle invariant

- **Never on an idle tick.** Because the deliberation generalizes Tier B, it runs *only* on a `CHANNEL_MESSAGE` open-floor admit — it is not on the autonomous-`TICK` path at all, so a bored persona never reaches it by construction. This is reinforced upstream by the RFC 0017 §F empty-context short-circuit in [`action_loop.py`](../../agents/persona_runtime/action_loop.py) (the four-condition TICK guard), which returns `DO_NOTHING` before any gate runs. The idle-cost gate in [`agents/tests/test_persona_tick_shortcircuit.py`](../../agents/tests/test_persona_tick_shortcircuit.py) is extended to assert no deliberation call is added to the idle-tick path.
- **Metered, same interaction.** The deliberation call goes through the existing wallet lease in [`agents/llm_client.py`](../../agents/llm_client.py) with the *same* `interaction_id`, so it draws from the same RFC 0050 `interaction_budget_tokens` ceiling. A low budget correctly starves deliberation (fail-closed to silence) rather than overspending.
- **Net-negative on pile-on.** Because a `should_post=false` verdict suppresses the `quality` compose, deliberation is expected to *reduce* aggregate spend on the exact noisy turns it targets.

### G. Configuration — an RFC 0050 knob

A new per-channel governance field, applied through the existing [RFC 0050](0050-extensible-channel-configuration.md) `validate → apply (router setters) → persist → bump revision` path (runtime-editable from CLI + web, no restart):

```yaml
reasoning:
  mode: off | bid | plan      # off = today's behaviour; bid = structured silence verdict only;
                              # plan = silence verdict + plan-threaded compose (the full feature)
  model: fast                 # alias for the deliberation pass
  depth: shallow | deep       # deep = Option B native thinking (gated on OQ 1); default shallow
```

Default `mode: off` (or `bid` for `participant`/`chair` dispositions, [OQ 2](#open-questions)). The knob follows the same *pattern* as the existing 0050 knobs — typed router field + schema entry + CLI/web surface — but it is **not a free ride on it, and not literally the same shape**. Each 0050 knob is hand-coded across ~6 sites (the eight-entry CLI [`CONFIG_KNOBS`](../../cli/src/commands/channel_config.rs) registry + its render map, the Go [`mergeConfigPatch`](../../internal/server/channel_config_handlers.go) one-case-per-knob switch + a router setter, and the hardcoded [`KNOBS`](../../web/src/panels/ChannelSettings.svelte) array in the web settings panel); there is no schema-driven generic surface yet, so a new knob is per-knob plumbing, not a declaration. `reasoning` further breaks the existing shape on two axes: it is the **first enum-valued knob** (the CLI `KnobType` is `Bool|Int|Str`, and the web render has only `bool`/`int`/`chair` branches — `mode`/`depth` need a new select control on both surfaces) and the **first nested/dotted knob** (`reasoning.mode`, vs. the flat scalar keys the `key=value` parser, the Go switch, and the flat web list all assume). The capability-gated `validate` step (next paragraph) is likewise net-new. None of this is hard, but it is real Phase-3 work the "same shape" framing should not bury. The ladder is **monotonic** — each value is a strict superset of the one below — so a channel can be promoted `off → bid → plan` (and demoted) one rung at a time with no re-plumbing.

**`mode: off` is a true kill switch, not a degraded mode.** It is byte-for-byte today's scalar Tier-B bid, reachable per-channel at runtime through the same 0050 `validate → apply` path with no restart. Because the deliberation is a *serial* `fast` call *before* compose on an interactive turn ([§F](#f-cost-and-the-idle-invariant)), added latency — not added spend — is the real operational risk; the Phase 3 deliberation-latency histogram is the signal, and flipping a channel to `off` is the immediate escape hatch.

**Validation rejects enum values whose backing phase is not deployed** rather than silently degrading to the nearest implemented rung. During the phased rollout `mode: plan` (Phase 2) and `depth: deep` (Phase 4) name behaviour a Phase-1 build cannot honour; the 0050 `validate` step gates the accepted set on *deployed capability*, not the full eventual enum, and returns an error for an unbacked value. Silent degradation (operator sets `plan`, gets `bid`) is the classic feature-flag footgun — reject-at-validate is near-free on the existing path and keeps the knob from lying about which rung is live.

## Security Considerations

- **Private-reasoning leakage** is the primary new risk. Mitigated structurally ([§E](#e-privacy-boundary--the-trace-is-walled)): the plan is never an action, never persisted to the channel store, and so is unreachable by RFC 0034 cross-persona reconstruction. A regression test asserts no `messages` row and no peer-visible artifact is produced by a deliberation.
- **Prompt-injection through the plan.** The deliberation reads the same (already-sanitized, RFC 0034 `<|user_message|>`-wrapped) transcript the compose reads; its output is the persona's own trusted text, not external data, and is consumed only by the same persona's next call. No new untrusted ingress.
- **Audit completeness.** The `channel.reason` event is a post-commit audit and must survive a cancelled turn (the RFC 0009 §G post-commit-emit principle). That section's `context.WithoutCancel` is a Go-orchestrator mechanism; the deliberation runs in the Python runtime, so it applies the same "the decision already happened — don't drop the record" rule on its own emit path rather than the literal Go API. The event records decision + `reason_code` + counts, never the verbatim `reason_note` or plan.
- **Cost-exhaustion as a denial vector** is bounded by the shared interaction lease ([§F](#f-cost-and-the-idle-invariant)) — deliberation cannot exceed the channel's `interaction_budget_tokens`.

## Phased Implementation Plan

### Phase 1: Structured silence verdict (Tier B generalization)

Summary: extend the salience bid to emit `{ should_post, reason_code, reason_note }` (plan omitted), short-circuiting to `DO_NOTHING` on false. Pure enrichment of the existing leased seam.

Deliverables:
1. `{ should_post, reason_code, reason_note }` prompt + regex-tolerant parser in [`salience_bid.py`](../../agents/salience_bid.py), fail-closed to silence. `reason_code` is the closed enum ([§C](#c-the-deliberation-verdict)); `reason_note` is optional and debug-only.
2. Thread the verdict through the existing Tier-B seam in [`salience_gate.py`](../../agents/persona_runtime/salience_gate.py) (the `run_salience_gate` → `SalienceOutcome` path that [`action_loop.py`](../../agents/persona_runtime/action_loop.py) consumes); reuse the seam's `DO_NOTHING` + memory-ingest path.
3. `channel.reason` audit event (decision + `reason_code` + counts; never `reason_note`).
4. Extend the idle-cost gate test to cover the deliberation pass.
5. **Minimal observability lands with the feature, not in Phase 3.** A deliberation **parse-failure counter**, kept distinct from the existing Tier-B salience suppression on `channel.messages.gated` so a fail-closed regression is not masked. Without it Phase 1's silence decision ships unobservable: a silent parser break surfaces only as all-silence (gate fail-closed) and is indistinguishable from "working as intended."

Dependencies: none beyond shipped RFC 0030 Tier B.

### Phase 2: Plan-threaded compose

Summary: add the `CompositionPlan` artifact ([§C](#c-the-deliberation-verdict)) and thread it as a private system-prompt section into the Tier-C compose. The plan is a *separate type* from the gate verdict and gets a *separate module* — for testability, and because [`action_loop.py`](../../agents/persona_runtime/action_loop.py) is at the 493/500-line review cap and inline plan assembly would bust it (the same cap that already carved out [`channel_ingest.py`](../../agents/persona_runtime/channel_ingest.py), [`llm_call_errors.py`](../../agents/persona_runtime/llm_call_errors.py), [`gate_suppress.py`](../../agents/persona_runtime/gate_suppress.py), and [`salience_gate.py`](../../agents/persona_runtime/salience_gate.py) itself).

Deliverables:
1. New `agents/persona_runtime/deliberation_plan.py` owning the `CompositionPlan` value type, its regex-tolerant parser (**fail-closed to "no plan"**: an unparseable plan composes as today rather than blocking the post — the bias is opposite the gate's bias-to-*silence*, because by this point the gate has already decided the persona *should* post), and a pure `render_plan_section(plan) -> str` renderer. No `action_loop` / agent coupling — unit-testable in isolation, and the no-leak test points straight at it.
2. Carry `plan: CompositionPlan | None` back on `SalienceOutcome`; the parser populates it alongside the gate verdict on the speak path. The pure bid's `SalienceDecision` is **not** widened.
3. `action_loop.py` appends the rendered private section to the Tier-C compose system prompt (alongside the RFC 0034 working-memory sections) via a one-line `render_plan_section` call — not inline assembly. Covered by the no-leak test ([§E](#e-privacy-boundary--the-trace-is-walled)).

Dependencies: Phase 1.

### Phase 3: Configuration + telemetry

Summary: the per-channel `reasoning` knob and observability.

Deliverables:
1. `reasoning.{mode,model,depth}` router field + RFC 0050 schema/validate/apply/persist + revision bump. **`validate` gates the accepted enum set on deployed capability** ([§G](#g-configuration--an-rfc-0050-knob)): an unbacked value (`plan` before Phase 2, `deep` before Phase 4) is rejected, never silently downgraded.
2. CLI (`channel config set … reasoning.mode=plan`) + web settings surface. Both are per-knob hand-plumbing (no schema-driven generic surface exists yet), and `reasoning` is the first *enum*-valued + first *nested/dotted* knob — the CLI `KnobType` and the web `KNOBS` render both need a new enum/select path, not just a new entry ([§G](#g-configuration--an-rfc-0050-knob)). This is the **reasoning *config* knob only**; the OQ-6 reasoning-*reveal* panel ([OQ 6](#open-questions)) is a separate UI surface, explicitly not bundled here.
3. Telemetry beyond the Phase 1 parse-failure counter: deliberation-rate; **suppress-rate by `reason_code`** (silence charted *by cause*, not just totalled); a **deliberation-latency histogram** (the pass is a serial `fast` call *before* compose on an interactive turn — reuse the [`agent.llm.duration`](../../agents/observability/metrics.py) instrument shape); a **budget-starvation counter** (deliberation starved to silence by a low `interaction_budget_tokens` — operationally distinct from "nothing to add", per [§F](#f-cost-and-the-idle-invariant)); and a `should_post=true`-but-empty-compose divergence counter. The Phase 1 parse-failure counter is the **mandatory, never-gated safety net** (a silent parser break must not hide behind a disabled metric); the instruments in *this* phase can stage in incrementally *within* Phase 3 rather than all landing at once. **Cost delta vs. baseline needs a stated counterfactual** — the two arms cannot run on one turn, so measure it as either an A/B-by-channel split or a `mode: bid` shadow arm; record which. Of the Phase 3 deliverables this one is the **most deferrable**: it *proves* the [§F](#f-cost-and-the-idle-invariant) net-saving claim rather than gating the ship, and the `mode: bid` shadow arm is the cheaper counterfactual because it reuses the ladder instead of partitioning channels.

Dependencies: Phases 1–2.

### Phase 4 (optional / follow-on): Native extended-thinking depth

Summary: `reasoning.depth: deep` (Option B) — add a `thinking` budget to the provider protocol and the compose lease. Gated on [OQ 1](#open-questions).

Dependencies: Phase 3; provider-protocol change.

## Files Touched (Estimated)

| Component | Files | Change |
|-----------|-------|--------|
| Python agents | [`agents/salience_bid.py`](../../agents/salience_bid.py) | Enrich the scalar `SalienceDecision` gate verdict with `should_post` + the closed-enum `reason_code` (extends the existing low-cardinality branch label) + the optional debug-only `reason_note`, fail-closed to silence (Phase 1). The `plan` is **not** added here — it is a separate type (next row) |
| Python agents | `agents/persona_runtime/deliberation_plan.py` *(new)* | The `CompositionPlan` value type + regex-tolerant parser (fail-closed to "no plan") + pure `render_plan_section(plan) -> str` renderer. No agent / `action_loop` coupling; the no-leak test targets it directly (Phase 2) |
| Python agents | [`agents/persona_runtime/salience_gate.py`](../../agents/persona_runtime/salience_gate.py) | The Tier-B seam: thread the gate verdict through `run_salience_gate`; carry `plan: CompositionPlan \| None` back on `SalienceOutcome` (**not** on the pure bid); reuse its `DO_NOTHING` + `_store_event_episode` suppressed-ingest path; emit the `channel.reason` audit event |
| Python agents | [`agents/persona_runtime/action_loop.py`](../../agents/persona_runtime/action_loop.py) | Consume the richer seam outcome; append the rendered private plan section to the Tier-C compose system prompt via a one-line `render_plan_section` call (keeps the file under the 500-line cap) |
| Python agents | [`agents/llm_providers.py`](../../agents/llm_providers.py), `agents/llm_types.py` | (Phase 4 only) `thinking` budget on the provider protocol |
| Go orchestrator | `internal/channels/...`, `internal/server/...` | `reasoning` config field + apply path + REST PATCH/GET (RFC 0050); `validate` rejects enum values whose backing phase is not deployed (capability-gated, [§G](#g-configuration--an-rfc-0050-knob)) |
| Rust CLI | [`cli/src/commands/channel_config.rs`](../../cli/src/commands/channel_config.rs) | `channel config` surface for `reasoning.*`: extend the hand-coded `CONFIG_KNOBS` registry + render map (the set is pinned to the Go merge switch by test). `reasoning` is the first *enum*-valued and first *nested/dotted* knob, so `KnobType`/parse needs an enum + dotted-key path, not just a new row ([§G](#g-configuration--an-rfc-0050-knob)) |
| Web console | [`web/src/panels/ChannelSettings.svelte`](../../web/src/panels/ChannelSettings.svelte) | Add `reasoning.*` to the hardcoded `KNOBS` array + a new enum/select control branch (today only `bool`/`int`/`chair` render). The OQ-6 reasoning-*reveal* panel is **separate, net-new** UI (+ a backend debug-egress endpoint), not part of this row ([OQ 6](#open-questions)) |
| Config | `config/channels.yaml`, `config/ui.yaml` | `reasoning` block + web toggle |
| Observability | [`agents/observability/_metrics_salience.py`](../../agents/observability/_metrics_salience.py), [`agents/observability/metrics.py`](../../agents/observability/metrics.py) | Deliberation parse-failure counter (Phase 1); suppress-rate-by-`reason_code`, latency histogram, budget-starvation + empty-compose counters (Phase 3) — kept distinct from the Tier-B `channel.messages.gated{policy=low_salience}` rows |
| Audit | audit event registration | `channel.reason` event (decision + `reason_code` + counts; never `reason_note`) |

## Test Strategy

- **Unit tests**: gate-verdict parser (well-formed → correct `reason_code`; malformed → fail-closed silence *and* the parse-failure counter increments); `deliberation_plan.py` parse + `render_plan_section` (well-formed, malformed → fail-closed "no plan", composes unplanned rather than blocking the post); the four-condition idle guard still suppresses with deliberation present; the RFC 0050 `validate` step rejects an unbacked `mode`/`depth` value (capability-gated enum, [§G](#g-configuration--an-rfc-0050-knob)) rather than downgrading it.
- **Integration tests**: a `should_post=false` turn produces zero `SEND_CHANNEL_MESSAGE` and zero compose calls, and emits one `channel.reason` audit event carrying the `reason_code` (assertable, unlike free text); a `should_post=true` turn composes once *under* the plan; the plan never appears in the channel store nor in a second persona's reconstructed `messages` (the no-leak gate); both calls meter against one `interaction_id` and a low `interaction_budget_tokens` starves deliberation to silence.
- **E2E / smoke**: a 3-persona group brainstorm with `reasoning.mode: plan` shows fewer pile-on turns than the `off` baseline (a *countable* assertion via suppress-rate) and — as a **qualitative** check, not a regression gate — higher per-post substance. "Substance" has no committed metric yet; if it must become a gate, define it first (e.g. a judged rubric or a restatement-overlap proxy against peer messages) rather than asserting a vibe. One `channel.reason` audit event per deliberation, count-not-content.
- **Manual tests**: `MT-REASON-001` — *think before posting* — a persona stays silent with a stated reason on a directed-elsewhere / already-answered turn (the reason observed via the operator-debug `reason_note` egress, [§E](#e-privacy-boundary--the-trace-is-walled), not the count-only audit event), and posts a plan-shaped contribution on a turn it can add to; the private plan is absent from every other participant's view and from the channel transcript.

## Open Questions

1. **Native thinking vs. two-pass for `depth: deep`.** Option B needs a `thinking` parameter on the provider protocol and a lease that budgets thinking tokens (billed as output). Worth it only if the `fast`-model deliberation proves too shallow in practice. Defer to Phase 4 telemetry from Phase 3.
2. **Default `mode` per disposition.** Should `participant`/`chair` default to `bid` (silence verdict on) while `addressed` stays `off`? Lean yes — the pile-on problem is concentrated in open-floor `participant` turns. Decide at the Phase 3 config PR.
3. **Single pass vs. draft→critique→revise.** A reflexion loop would raise quality further but multiplies cost per post. Out of scope here; revisit only if single-pass plans prove low-quality.
4. **Does the plan belong in the persona's own episodic memory?** Storing it agent-locally (not in the channel) could improve continuity, but risks the persona "remembering" intentions it never acted on. Default: audit-only, no episodic write. Revisit with RFC 0027 (reflection-driven consolidation).
5. **Should the deliberation core become an extractable library?** The pure verdict + plan core (a cheap leased pre-flight LLM call → a structured, fail-closed should-act decision) is a general shape, but it has exactly **one** in-repo consumer and is soaked in Persatrix substrate — the RFC 0023 wallet lease, the `model_aliases` resolver, the `prompt_loader` snippets, the RFC 0034 transcript shape, the gRPC error taxonomy, the OTEL instruments. Extracting a standalone library *now* would be speculative generality (N=1 with a hypothetical second caller). The reuse that matters is internal and is already served by data-contract forward-compat (the `channel.reason` → RFC 0028 `DecisionRecord` precursor, [§A](#a-positioning--distinct-from-rfc-0028)). The one real forcing function is [RFC 0045](0045-open-core-extraction-policy.md) (open-core extraction): *if* that boundary ever places the persona runtime / deliberation core on the extracted side, the library question becomes real — but it is an RFC 0045 call, not this RFC's. Default: keep it in-tree and get the module/type boundaries ([§C](#c-the-deliberation-verdict), [Phase 2](#phase-2-plan-threaded-compose)) library-quality without paying the packaging tax.

6. **The three-audience visibility model — who sees the reasoning?** [§E](#e-privacy-boundary--the-trace-is-walled) splits visibility three ways: peer personas / channel store (never), human operator (opt-in debug), end-users watching the channel (deferred). Two sub-questions are live for ratification: **(a)** should the operator-debug path get a *first-class web-console reveal* in the channel-timeline panel (the surface [MT-CHANNEL-RELEVANCE-001](../manual-tests/MT-CHANNEL-RELEVANCE-001.md) already uses), behind a debug toggle, rather than only raw agent logs? Lean **yes** — you cannot tune the plan-generation prompt without seeing plans, and the precedent is established. **Scope it honestly, though:** unlike the config knob this is *not* a row in [`ChannelSettings.svelte`](../../web/src/panels/ChannelSettings.svelte) — it is net-new UI (a timeline-side reasoning affordance) **plus a backend debug-egress path** (the verbatim `reason_note` does not reach the web today; only the count-only `channel.reason` audit and the agent log do, [§E](#e-privacy-boundary--the-trace-is-walled)). It is its own small surface, not a checkbox, and should be sized as a separate PR from the Phase-3 config knob even if both land in the same release. **(b)** Is an end-user "show the reasoning" mode ever desirable as a demo / interpretability feature, and if so does it stay a *separate explicit egress* so [§E](#e-privacy-boundary--the-trace-is-walled)'s structural wall is never the thing relaxed? Decide **(a)** at the Phase 3 config/telemetry PR; **(b)** is a product call deferred past v0.3.x.

## Decision / Next Steps

**Status**: 📋 Proposed (this PR). On ratification:

1. Open `docs/rfcs/0051-pr-plan.md` modeled on the recent per-RFC PR plans.
2. Sequence into the v0.3.x tail — a candidate v0.3.10 headline alongside the still-open [RFC 0039](0039-user-accounts-authentication.md) (accounts/auth) and [RFC 0045](0045-open-core-extraction-policy.md) (open-core gate); record the call in [`docs/v0.3.x-sequencing.md`](../v0.3.x-sequencing.md).
3. Flip the [ROADMAP RFC Master Index](../../ROADMAP.md#rfc-master-index) row to `🚧 Implementing` when the v0.3.10 plan opens.

If a reviewer judges per-turn reasoning to belong inside RFC 0028, the fallback is to fold this in as an RFC 0028 *pre-compose sub-checkpoint* — but that bundles a small v0.3.x realism win into a large v0.4.0 surface and slips it past adoption; the default keeps it a standalone, shippable patch.

## Related Documentation

- [RFC 0030 — Multi-Agent Conversation Governance](0030-multi-agent-conversation-governance.md) and the [relevance-gated-response amendment](0030-amendment-relevance-gated-response.md) — the Tier A/B/C gate this generalizes.
- [RFC 0028 — Agent Decision Policy Engine](0028-agent-decision-policy-engine.md) — the v0.4.x decision engine this is distinct from and forward-compatible with.
- [RFC 0034 — Persona Conversational Working Memory](0034-persona-conversational-working-memory.md) — the transcript reconstruction the private trace must stay out of.
- [RFC 0023 — LLM Call Leasing](0023-llm-call-leasing.md) and [RFC 0024 — Event-Driven Agent Scheduling](0024-event-driven-scheduling.md) — the metering and idle-cost invariants.
- [RFC 0050 — Extensible Channel Configuration](0050-extensible-channel-configuration.md) — the config surface the `reasoning` knob rides.
- [RFC 0009 — Agent Identity, Security & Sandboxing](0009-security-sandboxing.md) — the audit and prompt-safety boundary.
- [v0.3.x sequencing](../v0.3.x-sequencing.md) — where this slots into the release tail.
