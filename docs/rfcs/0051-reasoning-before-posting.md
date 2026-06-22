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
  - RFC-0050
  - RFC-0009
---

# RFC 0051 — Reasoning Before Posting

**Type**: feature  
**Status**: 📋 Proposed  
**Author**: Maksim Khomutov  
**Date**: 2026-06-22  
**Target**: v0.3.x  
**Depends on**: RFC 0030 (relevance gate / Tier B), RFC 0034 (conversational working memory), RFC 0023 (LLM call leasing), RFC 0050 (extensible channel configuration), RFC 0009 (audit / prompt-safety boundary)

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
5. **The trace is private and auditable.** The reasoning never enters the channel store, never reaches another persona's working memory, and is recorded only as an audit event (count/decision, not verbatim plan text).
6. **Operator-controlled, off by default.** A per-channel `reasoning` knob fits the [RFC 0050](0050-extensible-channel-configuration.md) config surface; the default is conservative.

## Non-Goals

- **Group deliberation toward a decision.** Reasoning *across* personas toward a justified recommendation is [RFC 0028](0028-agent-decision-policy-engine.md) (v0.4.x). This RFC is per-persona, per-turn, and private.
- **Action-class selection.** Choosing between `respond` / `ask_clarification` / `delegate` / `end_vote` is RFC 0028's pre-act checkpoint. This RFC reasons *inside* the already-selected `publish_channel` lane.
- **A draft → critique → revise (reflexion) loop.** A multi-round self-critique is explicitly deferred ([OQ 3](#open-questions)); Phase 1–3 is a single deliberation pass.
- **Replacing Tier A.** The free, deterministic addressing gate ([response_gate.py](../../agents/response_gate.py)) stays exactly as it is; deliberation runs only on turns Tier A admits.
- **A durable reasoning store.** The trace is ephemeral + audit-only; no new persisted "thoughts" tier.

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
- **Tier B** — `run_salience_gate` / [`salience_bid.py`](../../agents/salience_bid.py): a leased `fast`-model bid (≈64 tokens, temp 0.0, bias-to-silence) that runs *only* on the ambiguous open-floor admit. Drop → `DO_NOTHING`.
- **Tier C** — the `quality`-model compose call (multi-turn tool loop), parsed into actions and published.

Tier B **is the seam.** "Reasoning before posting" is Tier B promoted from a scalar verdict into a structured deliberation. No new orchestration layer is introduced — the bid module's leased call, regex-tolerant parsing, fail-closed-to-silence default, and idle-guard placement are all reused.

```
inbound → idle guard → Tier A (free) → ┌─ DELIBERATE (fast, leased) ─┐ → Tier C compose (quality) → publish
                                       │   should_post? + plan        │      (under the private plan)
                                       └─ should_post=false → DO_NOTHING
```

### C. The deliberation verdict

The deliberation pass returns a small structured object (parsed prompt-side, as the salience bid already does — there is no `response_format` in the provider protocol today):

```
{
  "should_post": true | false,
  "reason": "<one short clause — why post / why stay silent>",
  "plan": {                         // present only when should_post = true
    "intent": "<what this contribution is for>",
    "key_points": ["...", "..."],   // ≤3, the substance to land
    "addressed_to": "<participant_id | 'channel'>",
    "avoid_restating": ["..."]      // what peers already said; don't repeat
  }
}
```

`should_post=false` short-circuits to the existing `DO_NOTHING` outcome (and still ingests memory, exactly as a Tier-A/B suppression does today via [`gate_suppress.py`](../../agents/persona_runtime/gate_suppress.py) — "the gate decides whether to respond, not whether to remember"). When `should_post=true`, `plan` is serialized into a single private system-prompt section appended for the Tier-C compose only, alongside the RFC 0034 working-memory sections — never published, never written to the channel store.

### D. Mechanism — three realizations, one recommendation

There is no extended-thinking path in the codebase today; the providers in [`agents/llm_providers.py`](../../agents/llm_providers.py) accept only `model / messages / system / tools / max_tokens / temperature`. So the realization is a genuine fork:

| Option | Mechanism | Cost / turn | Assessment |
|--------|-----------|-------------|------------|
| **A — Two-pass (generalize Tier B)** | `fast`-model deliberation → plan → `quality` compose under the plan | one cheap pass + compose only when posting | **Recommended.** Reuses the leased salience seam; cheap hard-suppress; zero provider-protocol change. |
| B — Native extended thinking | add a `thinking` budget to the `quality` compose call | single call; thinking tokens billed as output | Best raw reasoning quality, but needs a provider-protocol change *and* the lease must budget thinking tokens. **Follow-on / opt-in.** |
| C — Single structured `quality` call | one call emits `{reasoning, should_post, message}`, strip reasoning before publish | full compose cost even on silence | Weakest — pays the quality price to decide *not* to post. |

**Decision: ship Option A** as the v0.3.x slice; record **Option B** as an opt-in `reasoning.depth: deep` upgrade gated on a provider-protocol change ([OQ 1](#open-questions)). Option A delivers the silence win immediately, amortizes cost (a cheap pass kills an expensive compose), and threads cleanly through the existing model-alias layer ([`model_aliases.py`](../../agents/model_aliases.py)) so the deliberation can run on `fast` while compose stays on `quality`.

### E. Privacy boundary — the trace is walled

The plan and reason are the most context-revealing artifacts a persona produces, so they are fenced:

- **Never a message.** The plan is never wrapped in an `AgentAction` and never published; it cannot reach [`action_executor.py`](../../agents/action_executor.py)'s `SEND_CHANNEL_MESSAGE` path.
- **Never in the channel store.** It is not persisted as a message, so [RFC 0034](0034-persona-conversational-working-memory.md) transcript reconstruction can never surface it into *another* persona's `messages` array.
- **Not `<external_data>`.** It is the persona's *own* reasoning, not untrusted external input, so it is a normal trusted system-prompt section — not wrapped in the [RFC 0009](0009-security-sandboxing.md) quarantine envelope (which is for tool/bridge output).
- **Audit-only egress.** Each deliberation emits one `channel.reason` audit event recording the *decision and counts, not the verbatim plan* — observable without logging the private text, in the RFC 0009 audit shape.

### F. Cost and the idle invariant

- **After the idle guard.** The deliberation is invoked strictly *after* the empty-context short-circuit in [`action_loop.py`](../../agents/persona_runtime/action_loop.py) (the four-condition TICK guard), so a bored persona never reasons. The "bored persona costs nothing" gate in [`agents/tests/test_persona_tick_shortcircuit.py`](../../agents/tests/test_persona_tick_shortcircuit.py) is extended to assert the deliberation fires zero times on an idle tick.
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

Default `mode: off` (or `bid` for `participant`/`chair` dispositions, [OQ 2](#open-questions)). The knob is a typed router field + schema entry + CLI/web surface — the same shape as the seven knobs RFC 0050 already hosts.

## Security Considerations

- **Private-reasoning leakage** is the primary new risk. Mitigated structurally ([§E](#e-privacy-boundary--the-trace-is-walled)): the plan is never an action, never persisted to the channel store, and so is unreachable by RFC 0034 cross-persona reconstruction. A regression test asserts no `messages` row and no peer-visible artifact is produced by a deliberation.
- **Prompt-injection through the plan.** The deliberation reads the same (already-sanitized, RFC 0034 `<|user_message|>`-wrapped) transcript the compose reads; its output is the persona's own trusted text, not external data, and is consumed only by the same persona's next call. No new untrusted ingress.
- **Audit completeness.** Per RFC 0009 §G, the `channel.reason` post-commit audit uses `context.WithoutCancel` semantics where applicable so a cancelled turn still records the decision; the event records counts/decision, never the verbatim plan.
- **Cost-exhaustion as a denial vector** is bounded by the shared interaction lease ([§F](#f-cost-and-the-idle-invariant)) — deliberation cannot exceed the channel's `interaction_budget_tokens`.

## Phased Implementation Plan

### Phase 1: Structured silence verdict (Tier B generalization)

Summary: extend the salience bid to emit `{ should_post, reason }` (plan omitted), short-circuiting to `DO_NOTHING` on false. Pure enrichment of the existing leased seam.

Deliverables:
1. `{ should_post, reason }` prompt + regex-tolerant parser in [`salience_bid.py`](../../agents/salience_bid.py), fail-closed to silence.
2. Wire the verdict into [`action_loop.py`](../../agents/persona_runtime/action_loop.py) at the existing Tier-B seam; reuse the `DO_NOTHING` + memory-ingest path.
3. `channel.reason` audit event (decision + counts).
4. Extend the idle-cost gate test to cover the deliberation pass.

Dependencies: none beyond shipped RFC 0030 Tier B.

### Phase 2: Plan-threaded compose

Summary: add the `plan` object to the verdict and thread it as a private system-prompt section into the Tier-C compose.

Deliverables:
1. Extend the verdict schema + parser with `plan`.
2. Private plan-rendering prompt section, appended for compose only; covered by a no-leak test.
3. Compose-prompt assembly change in `action_loop.py`.

Dependencies: Phase 1.

### Phase 3: Configuration + telemetry

Summary: the per-channel `reasoning` knob and observability.

Deliverables:
1. `reasoning.{mode,model,depth}` router field + RFC 0050 schema/validate/apply/persist + revision bump.
2. CLI (`channel config set … reasoning.mode=plan`) + web settings surface.
3. Telemetry: deliberation-rate, suppress-rate, and the cost delta vs. an unreasoned baseline.

Dependencies: Phases 1–2.

### Phase 4 (optional / follow-on): Native extended-thinking depth

Summary: `reasoning.depth: deep` (Option B) — add a `thinking` budget to the provider protocol and the compose lease. Gated on [OQ 1](#open-questions).

Dependencies: Phase 3; provider-protocol change.

## Files Touched (Estimated)

| Component | Files | Change |
|-----------|-------|--------|
| Python agents | [`agents/salience_bid.py`](../../agents/salience_bid.py) | Verdict schema + parser (`should_post`, `reason`, `plan`) |
| Python agents | [`agents/persona_runtime/action_loop.py`](../../agents/persona_runtime/action_loop.py) | Deliberation invocation at Tier-B seam; private plan section into compose |
| Python agents | [`agents/persona_runtime/gate_suppress.py`](../../agents/persona_runtime/gate_suppress.py) | Reuse `DO_NOTHING` + memory-ingest for reasoned silence |
| Python agents | [`agents/llm_providers.py`](../../agents/llm_providers.py), `agents/llm_types.py` | (Phase 4 only) `thinking` budget on the provider protocol |
| Go orchestrator | `internal/channels/...`, `internal/server/...` | `reasoning` config field + apply path + REST PATCH/GET (RFC 0050) |
| Rust CLI | `cli/src/...` | `channel config` surface for `reasoning.*` |
| Config | `config/channels.yaml`, `config/ui.yaml` | `reasoning` block + web toggle |
| Audit | audit event registration | `channel.reason` event |

## Test Strategy

- **Unit tests**: verdict parser (well-formed, malformed → fail-closed silence); plan serialization; the four-condition idle guard still suppresses with deliberation present.
- **Integration tests**: a `should_post=false` turn produces zero `SEND_CHANNEL_MESSAGE` and zero compose calls; a `should_post=true` turn composes once *under* the plan; the plan never appears in the channel store nor in a second persona's reconstructed `messages` (the no-leak gate); both calls meter against one `interaction_id` and a low `interaction_budget_tokens` starves deliberation to silence.
- **E2E / smoke**: a 3-persona group brainstorm with `reasoning.mode: plan` shows fewer pile-on turns and higher per-post substance than the `off` baseline; one `channel.reason` audit event per deliberation, count-not-content.
- **Manual tests**: `MT-REASON-001` — *think before posting* — a persona stays silent with a stated reason on a directed-elsewhere / already-answered turn, and posts a plan-shaped contribution on a turn it can add to; the private plan is absent from every other participant's view and from the channel transcript.

## Open Questions

1. **Native thinking vs. two-pass for `depth: deep`.** Option B needs a `thinking` parameter on the provider protocol and a lease that budgets thinking tokens (billed as output). Worth it only if the `fast`-model deliberation proves too shallow in practice. Defer to Phase 4 telemetry from Phase 3.
2. **Default `mode` per disposition.** Should `participant`/`chair` default to `bid` (silence verdict on) while `addressed` stays `off`? Lean yes — the pile-on problem is concentrated in open-floor `participant` turns. Decide at the Phase 3 config PR.
3. **Single pass vs. draft→critique→revise.** A reflexion loop would raise quality further but multiplies cost per post. Out of scope here; revisit only if single-pass plans prove low-quality.
4. **Does the plan belong in the persona's own episodic memory?** Storing it agent-locally (not in the channel) could improve continuity, but risks the persona "remembering" intentions it never acted on. Default: audit-only, no episodic write. Revisit with RFC 0027 (reflection-driven consolidation).

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
