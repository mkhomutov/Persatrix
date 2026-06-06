# RFC 0030 Amendment — Relevance-Gated Response (Graduated Response Gate)

**Type**: amendment to [RFC 0030](0030-multi-agent-conversation-governance.md) §A / §G (Layer 3) / §I (Layer 5)
**Status**: 🚧 Implementing — Tier A (addressing-aware eligibility) + the `respond_policy → disposition` reframe **shipped in v0.3.7** (the directed-elsewhere filter in [`agents/response_gate.py`](../../agents/response_gate.py) + Go candidate-set parity + the `participant`/`addressed`/`observer` vocabulary with back-compat; acceptance [MT-CHANNEL-RELEVANCE-001](../manual-tests/MT-CHANNEL-RELEVANCE-001.md)). Tier B (cheap salience bid), the `chair` disposition, and natural-language addressing land in **v0.3.8** with the convergence layers; the per-disposition `threshold` field is reserved/no-op until then. The two version-split scoping questions are resolved in the [v0.3.7 plan §Open-question status](../v0.3.7-plan.md#open-question-status); the remaining open questions below are version-routed (Tier B / v0.4.0), not blocking.
**Author**: Maksim Khomutov
**Date**: 2026-06-04
**Target**: v0.3.7 (Tier A eligibility + `respond_policy → disposition` reframe — pairs with [RFC 0034](0034-persona-conversational-working-memory.md) Phase 2 group working memory, also v0.3.7) + v0.3.8 (Tier B cheap salience bid + `chair` disposition + natural-language addressing) + v0.4.0 (bid-and-select moderator — RFC 0030 Layer 5; principled home [RFC 0028](0028-agent-decision-policy-engine.md))
**Trigger**: Manual end-to-end testing of multi-persona group channels. Two distinct failures observed in the same session:
1. A message addressed to one persona (`"how about you @ember-owl?"`, and an explicit `"question only to Iron Fox"`) drew replies from **every** `respond: always` member — including one that said *"I'm Nova Sparrow, not Ember Owl, but…"* and answered anyway. The gate has no notion of *directedness*.
2. On open-floor prompts, every `always` member replies regardless of whether it has anything to add — bland convergence and pile-on, with no member ever choosing to stay out because *"someone already said that"* or *"that's not my lane."*
**Supersedes**: nothing. **Extends** RFC 0030 §G (Layer 3) — the response gate graduates from a static admission switch into a three-tier relevance decision — and reframes RFC 0011 §D's `respond_policy` enum as a *disposition / threshold* rather than a mechanical trigger.

---

## Table of Contents

- [Context](#context)
- [The gap Layer 3 leaves](#the-gap-layer-3-leaves)
- [The reframe — "should I speak?" is a graded judgment, not a switch](#the-reframe--should-i-speak-is-a-graded-judgment-not-a-switch)
- [The graduated response gate (Layer 3, evolved)](#the-graduated-response-gate-layer-3-evolved)
- [Membership becomes a disposition, not a trigger](#membership-becomes-a-disposition-not-a-trigger)
- [Preserving the idle-cost invariant](#preserving-the-idle-cost-invariant)
- [The bid-and-select direction (Layer 5)](#the-bid-and-select-direction-layer-5)
- [Composition with the existing layers](#composition-with-the-existing-layers)
- [What is prompt, what is architecture](#what-is-prompt-what-is-architecture)
- [Dependencies and sequencing](#dependencies-and-sequencing)
- [Scope — v0.3.x vs v0.4.0](#scope--v03x-vs-v040)
- [Open questions](#open-questions)
- [Files touched (estimated)](#files-touched-estimated)
- [Test strategy](#test-strategy)
- [Related documentation](#related-documentation)

---

## Context

RFC 0030 Layer 3 is the **response gate** ([`agents/response_gate.py`](../../agents/response_gate.py)): the receiver-side, pre-LLM, pre-memory-recall check that decides whether a persona answers an inbound `CHANNEL_MESSAGE`. It enforces the per-membership `respond_policy` from [RFC 0011 §D](0011-channels-bridges.md):

- `always` — fire the LLM unconditionally (except when the agent is the sender);
- `when_mentioned` — fire iff the agent id is in `event.payload["mentions"]`, or the message is a thread reply to one the agent authored;
- `never` — always suppress.

The gate runs **before** any LLM call or memory-recall round-trip, deliberately, so an uninvolved persona costs zero tokens and zero retrieval (the RFC 0023 leasing / RFC 0024 event-driven-idle cost guarantee). That property is load-bearing and this amendment must preserve it.

The defect surfaced in testing is that `always` literally means *every message*, and `when_mentioned` keys only on the structured `mentions` field. There is no representation of:

- **Directedness** — a message clearly aimed at *someone else* (an `@`-mention of a different agent, or natural-language *"only to Iron Fox"*) does not suppress the other `always` members.
- **Relevance** — whether this persona actually has something worth adding (in its lane, not already said, a stance worth voicing) is never evaluated, because that judgment is dynamic and the gate is a static enum read.

## The gap Layer 3 leaves

RFC 0030 §G already states the limitation explicitly: *"The gate is necessary but not sufficient — it admits every event for `always` members… The higher layers exist precisely because Layer 3 cannot distinguish 'good cascade' from 'loop.'"* The higher layers (cost ceiling, reply budget, end-votes, moderator) bound **volume and termination**. The floor-control amendment (Layer 2.5) added **ordering**. None of them answer the per-message question a human answers constantly:

> *Should **I**, specifically, speak to **this** message, right now?*

That is an **admission** question — Layer 3's job — but the current Layer 3 can only answer it from a static switch set once at channel-join time. The result is the two failure modes in the Trigger: everyone answers everything, and nobody respects who a message was for.

## The reframe — "should I speak?" is a graded judgment, not a switch

In a real room, a participant speaks when some combination of signals crosses a threshold:

| Signal | Speak when… | Stay out when… |
|--------|-------------|----------------|
| **Directedness** | addressed to me (by name / `@`) | clearly aimed at someone else |
| **Relevance** | my expertise / role is implicated | not my lane, others better placed |
| **Novelty** | I'd add something not already said | someone already made my point |
| **Stance** | I disagree or can build, and I care | I'd only be agreeing / "great point!" |
| **Floor / social** | it's quiet and someone should | I've been dominating this round |

This is a **dynamic, graded relevance score**, not a boolean. No static enum value (`when_relevant`, `engaged`, …) can express it, because relevance changes message to message. So a richer membership *type* alone cannot fix it — and pure prompt behavior cannot fix it either, because evaluating relevance in-prompt requires first waking the full quality-model turn for every member on every message, which reintroduces exactly the cost class RFC 0024 eliminated, and the model's assistant bias means an awake persona usually speaks anyway.

The answer is therefore **both, plus a modest re-architecture of the gate**: keep the cheap pre-LLM admission stage, but make the *cost of deciding scale with the ambiguity of the situation.*

## The graduated response gate (Layer 3, evolved)

Layer 3 becomes a three-tier decision instead of a single switch:

| Tier | Decides | Cost | Mechanism |
|------|---------|------|-----------|
| **A — eligibility** (deterministic, addressing-aware) | Can I even consider replying? | **free** (no LLM) | Filters: agent is sender; disposition is `observer`; message is **directed at someone else** (an `@`-mention of a *different* agent, or a parsed "to X" recipient) and I am not also addressed. Subsumes today's `always` / `when_mentioned` / `never`. |
| **B — salience** (cheap LLM, only for the ambiguous middle) | Do I have something worth adding that hasn't been said? | **cheap** (`fast` alias, leased, ~50–100 tok) | Runs only for open-floor messages not clearly aimed at anyone, for `participant`-disposition members. Returns `speak: yes/no` + a salience score, with a **skeptical default (bias to silence)**. Reuses the RFC 0024 SalienceWake threshold/rate-limit machinery and the RFC 0033 `fast` model alias; the call is gated by an RFC 0023 lease like any other. |
| **C — the turn** (expensive, quality model) | The actual reply | full | Runs only for members that pass B; then **Layer 2.5 floor control** orders the concurrent passers into a coherent, mutually-visible round. |

Tier A alone fixes the directedness defect from the Trigger and costs nothing. Tier B makes "reply when you need to" a real dynamic decision while only spending tokens on genuinely ambiguous, open-floor traffic — and even then on the cheap model, under a lease. Tier C is unchanged from today except that fewer members reach it.

Tier B's quality depends on the persona seeing **what has already been said this round** (to judge novelty / "did someone already cover this") — i.e. on [RFC 0034](0034-persona-conversational-working-memory.md) Phase 2 group working memory. Judging relevance in a vacuum is hopeless, which is why **Tier B is sequenced to v0.3.8**, after RFC 0034 P2 lands in v0.3.7. Tier A (this amendment's free, addressing-only stage) ships first in **v0.3.7** alongside RFC 0034 P2 — it needs no in-round transcript.

## Membership becomes a disposition, not a trigger

The `respond_policy` enum (`schemas/channel.schema.json`) is reframed from a mechanical trigger into a declaration of **role-in-the-conversation**, which sets the **salience threshold** rather than a hard on/off:

| Disposition | Meaning | Tier behaviour | Back-compat |
|-------------|---------|----------------|-------------|
| `observer` | lurking; never speaks | filtered at Tier A | = today's `never` |
| `addressed` | speaks only when directly addressed | Tier A pass on mention/recipient | = today's `when_mentioned` |
| `participant` | actively in it; runs the salience gate, speaks when it clears the threshold | Tier A pass → Tier B judges | **new — the "reply when you need to" default** |
| `chair` | low threshold / floor manager | Tier B with low threshold; Layer 5 hooks | maps to moderator |

The type sets *how eager* a member is; the gate makes the *per-message call*. The demo personas should be `participant`. Back-compat: `always` maps to `participant` with a permissive threshold, `when_mentioned` → `addressed`, `never` → `observer`, so existing channel configs keep working while the new vocabulary becomes the recommended surface. The orchestrator's existing "all-`always` + uncapped reply budget" startup Warn (RFC 0030 §F) extends to flag all-`participant`-low-threshold channels.

## Preserving the idle-cost invariant

This is the constraint that shapes the whole design. The RFC 0023/0024 guarantee — *an uninvolved persona costs nothing* — must survive:

- **Tier A is free** and removes the bulk of traffic (sender-self, directed-elsewhere, observers).
- **Tier B fires only on the ambiguous remainder**, only for `participant` members, on the **`fast`** model, under a **lease** — so its worst case is bounded and attributable. It is **default-off-conservative**: an unset threshold biases to silence, never to speech.
- **Tier C population shrinks**, so the expensive quality turns drop relative to today's all-`always` behaviour — this amendment is expected to *reduce* aggregate cost on busy channels, not raise it, by replacing N full turns with N cheap bids + k full turns (k ≪ N).

The bias-to-silence default is a deliberate product stance: over-talking is the current failure, and a room where members speak only when they have something reads far more human than one where everyone answers everything. Better to occasionally miss a contribution than to pile on.

## The bid-and-select direction (Layer 5)

The graduated gate above is per-agent and independent. The more human-like target — and the natural evolution of RFC 0030's **Layer 5 moderator** — is **bid-and-select**, which unifies relevance-gating, anti-pile-on, and floor control into one mechanism:

1. Each Tier-A-eligible `participant` emits a cheap **bid** (`fast` model, leased): `want_to_speak` + salience score + a one-line intent.
2. A **selector** — deterministic for v0.3.x, a `chair` persona for v0.4.0 (Layer 5) — grants the floor to the **top 1–2 bidders**, subject to the Layer 2 reply budget.
3. Only the granted member(s) run the expensive Tier-C turn; the rest let it go.

This is how a real meeting works: several people may *want* to speak, but turn-taking grants the floor to the most relevant and the others defer. It degrades gracefully (one bid → that member speaks; zero bids → silence) and it composes with Layer 2.5 (floor control orders the granted speakers). The per-agent graduated gate (Tiers A–C) is the v0.3.x step; bid-and-select is the v0.4.0 moderator step that subsumes it. The principled long-term home for "should I speak, and what" is the [RFC 0028](0028-agent-decision-policy-engine.md) decision engine's pre-act checkpoint — Tier B and the bid are decision-policy inputs, not a parallel decision system.

## Composition with the existing layers

The gate change is confined to Layer 3; the stack is otherwise unchanged. Evaluation order (extending RFC 0030 §B):

```
Layer 0:   depth >= cap?                         ──yes──► drop; END
Layer 1:   lease available?                       ──no──► drop; END
Layer 2:   participant under reply budget?         ──no──► drop; END
Layer 3a:  eligible? (addressing-aware, free)      ──no──► drop; END   ← NEW (fixes directedness)
Layer 3b:  salient? (cheap fast-model bid, leased) ──no──► stay silent; END   ← NEW (relevance)
Layer 2.5: floor control orders the passers (shipped)
Layer 3c:  the quality turn  ──► reply
Layer 4:   end-of-interaction votes (unchanged)
Layer 5:   moderator / bid-select (v0.4.0) — replaces the deterministic selector
```

Layer 3a/3b sit exactly where today's binary Layer 3 sits — receiver-side, before the expensive turn — so the cost ordering and the floor-control hand-off are preserved.

## What is prompt, what is architecture

The gate handles the *cheap binary*; the peer-conversation **prompt** still carries the residual social judgment the gate cannot encode: don't pile on if someone already made your point, defer when another is better placed, and silence is a valid outcome. The two are complementary — the prompt shapes the *quality and content* of the speak/stay decision; the gate makes the binary *affordable and dynamic*. (A prior experiment confirmed that a peer-framing prompt section alone does not fix the directedness failure — that failure is structural, in the gate.)

## Dependencies and sequencing

- **Hard dependency for Tier B quality**: [RFC 0034](0034-persona-conversational-working-memory.md) Phase 2 (group working memory) — the salience judge needs the in-round transcript. RFC 0034 P2 lands in **v0.3.7**; Tier B is therefore sequenced to **v0.3.8** (the convergence patch), one release behind. Tier A (addressing-only, no transcript) ships in v0.3.7 with RFC 0034 P2.
- **Reuses**: RFC 0024 SalienceWake (threshold, rate-limit, default-off); RFC 0033 `fast` alias (the cheap bid model); RFC 0023 leasing (bounds and attributes the bid cost).
- **Evolves into**: RFC 0030 Layer 5 moderator (bid-and-select); RFC 0028 decision engine (the principled checkpoint home, v0.4.0).

## Scope — v0.3.7 / v0.3.8 / v0.4.0

**v0.3.7 (pairs with RFC 0034 P2 — realism):**
- Tier A eligibility with addressing-awareness, **structured `@`-mentions only** (fixes the directedness defect — the smallest standalone fix, shippable ahead of Tier B; free, no LLM).
- `respond_policy` reframed as disposition (`observer` / `addressed` / `participant`) with back-compat mapping. The per-disposition threshold field is reserved/no-op until Tier B.

**v0.3.8 (convergence — pairs with the cost/reply-budget/end-of-interaction layers):**
- Tier B cheap salience bid on the `fast` model, leased, bias-to-silence.
- The `chair` disposition (low threshold / floor-manager).
- Natural-language recipient parsing ("only to Iron Fox") as a Tier-B salience signal (OQ #2).

**v0.4.0 (RFC 0030 Layer 5 / RFC 0028):**
- Bid-and-select with a `chair` selector / moderator.
- Consolidation of Tier B + bid into RFC 0028 `DecisionRecord` checkpoints.

## Open questions

1. **Tier B without RFC 0034 P2.** Can a useful first cut of the salience judge run on the single inbound message alone (no in-round transcript), shipping Tier A + a degraded Tier B before group working memory lands? Proposed: ship **Tier A alone** first (it is free and fixes the worst defect), gate Tier B on RFC 0034 P2.
2. **Natural-language recipient parsing.** Tier A keys cleanly on structured `@`-mentions; "only to Iron Fox" needs light recipient extraction. Proposed: structured mentions in v0.3.x; treat free-text addressing as a Tier-B salience signal, not a hard Tier-A filter, until parsing is reliable.
3. **Threshold calibration.** The salience threshold per disposition needs production data (cf. the RFC 0024 salience-wake calibration deferral). Proposed: ship conservative (high threshold / strong silence bias) and calibrate after a soak.
4. **Bid cost on large channels.** Even a cheap bid × N members is non-trivial at 50+ members. Proposed: Tier A must shed enough that Tier B fan-out stays small; if not, gate bidding behind a channel-size cap and fall back to `addressed`-only above it.
5. **Self-mention / "everyone" addressing.** How do `@here` / "everyone" interact with Tier A? Proposed: "everyone" disables the directed-elsewhere filter (all `participant`s reach Tier B); no special-case beyond that.

## Files touched (estimated)

| Component | Files | Change |
|-----------|-------|--------|
| Python agents | [`agents/response_gate.py`](../../agents/response_gate.py) | Tier A eligibility (addressing-aware); Tier B salience bid; disposition mapping |
| Python agents | [`agents/event_loop.py`](../../agents/event_loop.py) | Reuse SalienceWake threshold/rate-limit for the bid |
| Python agents | `agents/model_aliases.py` | `fast` alias is the bid model (no change; consumer) |
| Config / schema | `schemas/channel.schema.json`, `config/channels.yaml` | `respond_policy` → disposition vocabulary + back-compat mapping; per-disposition threshold |
| Go orchestrator | [`internal/channels/fanout.go`](../../internal/channels/fanout.go) | Bid-and-select fan-out + selector (v0.4.0 Layer 5) |
| Docs | [`0030-multi-agent-conversation-governance.md`](0030-multi-agent-conversation-governance.md), `docs/ai-glossary.md` | Layer 3 graduated-gate note; *disposition*, *salience bid*, *bid-and-select* glossary entries |
| Tests | `tests/unit/python/test_response_gate*.py`, `tests/integration/` | Per Test strategy |

## Test strategy

- **Unit (Tier A)**: a message `@`-mentioning agent X does **not** admit `participant` agent Y; a message addressed to the channel (no recipient) admits all `participant`s to Tier B; sender-self and `observer` always filtered.
- **Unit (Tier B)**: the salience judge returns `no` when the in-round transcript already contains the persona's point; `yes` when the message is in the persona's domain and unaddressed; unset threshold biases to `no` (silence).
- **Cost regression**: an N-member all-`participant` channel spends N cheap bids + k full turns (k ≪ N), not N full turns — assert the Tier-C population shrank and the wallet spend dropped vs. the all-`always` baseline. Idle persona still costs zero (Tier A free).
- **Integration**: reproduce the Trigger — `"how about you @ember-owl?"` draws exactly one reply (Ember Owl); `"only to Iron Fox"` draws exactly one once recipient parsing lands (OQ #2); an open-floor brainstorm draws replies only from members with a non-redundant contribution.
- **Manual**: `MT-CHANNEL-RELEVANCE-001` — multi-persona channel; directed question, open-floor question, and a redundant follow-up; assert directedness suppression, no pile-on, and silence-when-nothing-to-add.

## Related documentation

- [RFC 0030 — Multi-Agent Conversation Governance](0030-multi-agent-conversation-governance.md) — §G (Layer 3, the gate this evolves), §I (Layer 5, the moderator this targets), §A (problem decomposition).
- [RFC 0030 floor-control amendment](0030-amendment-floor-control-speaker-serialization.md) — Layer 2.5, which orders the speakers this gate admits.
- [RFC 0011 — Channels & Internal Agent Messaging](0011-channels-bridges.md) §D — the `respond_policy` enum this reframes.
- [RFC 0034 — Persona Conversational Working Memory](0034-persona-conversational-working-memory.md) — Phase 2 group working memory, the in-round transcript Tier B needs.
- [RFC 0024 — Event-Driven Agent Scheduling](0024-event-driven-scheduling.md) — SalienceWake threshold/rate-limit machinery the bid reuses; the idle-cost invariant this preserves.
- [RFC 0033 — Provider-Agnostic Model Alias Layer](0033-model-alias-layer.md) — the `fast` alias the cheap bid runs on.
- [RFC 0023 — LLM Call Leasing](0023-llm-call-leasing.md) — the lease that bounds and attributes the bid's cost.
- [RFC 0028 — Agent Decision Policy Engine](0028-agent-decision-policy-engine.md) — the pre-act checkpoint that is the principled long-term home for "should I speak, and what."
