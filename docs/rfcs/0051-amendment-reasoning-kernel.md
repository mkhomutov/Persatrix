# RFC 0051 Amendment — The Reasoning Kernel (Extension & Extraction Contract)

**Type**: amendment to [RFC 0051](0051-reasoning-before-posting.md) §C / §D / [OQ 5](0051-reasoning-before-posting.md#open-questions) — a design contract; moves no code
**Status**: 📋 **Proposed**
**Author**: Maksim Khomutov
**Date**: 2026-06-24
**Target**: v0.3.10 (contract recorded alongside PR 3); the extraction it governs is *conditional*, not scheduled
**Trigger**: Per-turn channel deliberation (RFC 0051 Phases 1–2) is the **first** reasoning instance, not the last. Three more are foreseen: (1) **DM / 1:1 reasoning** with different goals and questions than `should_post` — a DM must reply, so the *silence* half is inapplicable while the *planning* half is not; (2) **task delegation** and (3) **decision-making / action-class selection** — both [RFC 0028](0028-agent-decision-policy-engine.md)'s lane. This amendment records the contract that keeps the deliberation **extensible, adjustable, and cheaply extractable** as those arrive — without extracting anything now.
**Supersedes**: nothing. **Extends** RFC 0051 [OQ 5](0051-reasoning-before-posting.md#open-questions) (the "keep it in-tree with library-quality seams" resolution) by writing down *what those seams are*.

---

## Why now

RFC 0051 OQ 5 already ruled: **do not extract a standalone library at N=1.** One in-repo consumer, soaked in Persatrix substrate (the RFC 0023 wallet lease, `model_aliases`, `prompt_loader`, the RFC 0034 transcript shape, the OTEL instruments), is speculative generality. The resolution was to keep the module/type boundaries *library-quality* so extraction is cheap **if** [RFC 0045](0045-open-core-extraction-policy.md) ever calls for it.

PR 3 (Phase 2) is the moment the deliberation grows a second artifact (the `CompositionPlan`) and a third touch-point (`action_loop`). Before PRs 4–9 and the consumers above add coupling, this amendment fixes the seams in writing so they survive contact.

It is a **contract, not a refactor**: no code moves, no generic engine is built. It defines the boundary future work holds to.

## The layering today

The implementation already splits into a reusable **kernel** and a bespoke **salience wrapper**, and the kernel is genuinely decoupled (imports verified):

| Layer | Where | Coupling | Reuse status |
|-------|-------|----------|--------------|
| Plan value / parse / render | [`deliberation_plan.py`](../../agents/persona_runtime/deliberation_plan.py) | stdlib only (`re`, `dataclasses`, `typing`) | extraction-ready |
| Structured verdict parse | [`salience_deliberation.parse_verdict`](../../agents/salience_deliberation.py) | stdlib + OTEL only — no channel/gate/agent types | pattern-ready |
| Leased cheap-model call + fail-closed parse | [`salience_bid.evaluate_salience`](../../agents/salience_bid.py) | wallet lease, `model_aliases`, salience prompt, NL-addressing, `SalienceDecision` | **the extraction seam** — bespoke at N=1 |
| Gate / orchestration | [`salience_gate.run_salience_gate`](../../agents/persona_runtime/salience_gate.py) | open-floor policy, channel-size cap, suppression metrics, audit, plan-threading | bespoke (correctly so) |

The top two layers are use-case-agnostic; the bottom two are salience-specific. That split is **correct at N=1** — the bespoke layers are exactly where a second consumer would otherwise copy-paste.

## The extension contract — one kernel, four injection points

A deliberation instance is the kernel parameterized by exactly four use-case bindings:

1. **Prompt** — the question being deliberated (today: the `salience-bid-*` snippets).
2. **Verdict schema** — `{ act: bool, reason_code: <closed enum>, note?: str }` with a use-case reason vocabulary (today: `should_post` + the pile-on codes).
3. **Plan schema** — a structured value type + regex-tolerant fail-closed parser + pure renderer (today: `CompositionPlan`). A **pattern replicated per use case, not a shared base class**.
4. **Gate policy** — when the deliberation fires and what proceed/suppress means (today: open-floor admit on a governed channel).

The kernel proper — *lease a cheap call, parse fail-closed to a safe default, meter against the interaction budget, emit a `DecisionRecord`-shaped audit* — is use-case-agnostic and is the part worth extracting when N≥2.

### How the foreseen consumers map

- **DM / 1:1 reasoning.** A different prompt + verdict (not `should_post`/pile-on, but e.g. "which open threads does this turn close; what does the user actually need"), and **no silence gate** — a DM must reply or the chat-as-DM round-trip 504s ([RFC 0011 §D](0011-channels-bridges.md)). The *planning* half is wanted; the *silence* half is not. A DM reasoner reuses injection points 1 and 3 with a degenerate always-proceed policy (4), and forces the kernel to offer **a plan pass decoupled from the silence gate** — which today is fused into the `should_post=true` path. **This is the first thing that breaks if the kernel is not extracted at N=2**, and the concrete answer to "are DM replies less considered?": they will be, until the planning pass is reusable on its own.
- **Task delegation** and **decision-making / action-class selection.** Both are [RFC 0028](0028-agent-decision-policy-engine.md)'s lane (its summary names "tool selection, delegation, and channel-publish decisions"). RFC 0051 is already shaped as 0028's precursor (§A — the two *stack*; the `agent.deliberated` audit is a forward-compatible `DecisionRecord` precursor). These reuse all four injection points with their own vocabularies and a `DecisionRecord` egress.

## The extraction rule: at N=2, not before

`evaluate_salience` is **the extraction seam.** When the second consumer lands — DM reasoning, most likely first — lift its reusable core (the lease-cheap-call → fail-closed-parse → meter → audit engine) into a generic `deliberate(prompt, verdict_schema, plan_schema, *, gate_policy)` rather than copy-pasting it. **Not before:** extracting at N=1 is the speculative generality OQ 5 ruled against. The signal to extract is the *second caller*, never a hypothetical one. Whether the result is ever published as a library is then an [RFC 0045](0045-open-core-extraction-policy.md) boundary call, separate again.

## Invariants to hold through PRs 4–9

These are cheap to keep and expensive to recover; review should enforce them:

1. **The pure layer stays pure.** `deliberation_plan` and `parse_verdict` never import a channel/gate/agent type. (True today.)
2. **Use-case semantics live in their own modules.** Reason-code vocabularies and plan schemas never fold into the lease / wallet / metric layer — that is what lets a new use case add a vocabulary without touching infra.
3. **The plan is a pattern, not a base class.** Each use case gets its own plan value type (a delegation brief ≠ a `CompositionPlan`); resist a premature `BasePlan` until ≥2 plan types share genuine structure.
4. **Hold the `agent.deliberated` → `DecisionRecord` precursor shape** so RFC 0028 subsumes it rather than colliding.
5. **Do not widen `SalienceDecision`.** The verdict type stays the gate's business; per-use-case payload (the plan) rides the orchestration outcome (`SalienceOutcome`), never the verdict. PR 3 honors this; future instances do the same with their own outcome type.
6. **Mind the inbound trust boundary on the plan.** ✅ **Resolved (closeout) — structural breakout closed at the renderer; bounded-free-text elevation affirmed as accepted risk.** §E walls the plan from leaking *out* (never an `AgentAction`, never persisted). The *opposite* direction is unguarded by that wall: the plan is *shaped by* the untrusted transcript the bid reads, yet [`render_plan_section`](../../agents/persona_runtime/deliberation_plan.py) renders it as a **trusted** system-prompt section (deliberately *not* the RFC 0009 `<external_data>` envelope — the plan is meant to *steer* compose, which the "DO NOT TREAT AS INSTRUCTIONS" envelope exists to forbid), so a peer message can attempt to steer a field and reach that trusted prompt. The obligation was to resolve this deliberately before the `plan` rung can go live — *either* quarantine the transcript-derived text *or* affirm the bounded-free-text elevation as accepted risk.

   **Resolution — both halves, split by what each actually guards:**
   - **Structural half — fixed in code.** Full `<external_data>` quarantine is the wrong tool (it would tell the LLM *not* to follow the plan). Instead the renderer now neutralizes the plan's *own* envelope tags: [`render_plan_section`](../../agents/persona_runtime/deliberation_plan.py) runs every field through `_neutralize_envelope_tags` before splicing, mirroring [`agents/security.py`](../../agents/security.py)'s `<external_data>` body escaping (PR #253 deep-review F1/M1: break out via a forged close tag, mint a fake nested envelope via a forged open tag). Kept stdlib-local so the module stays extraction-ready (invariant #1). With the close tag un-forgeable, *all* field text is trapped inside the one private-plan envelope — it can neither break out into the trusted prompt nor masquerade as a sibling top-level frame — so escaping the plan's own envelope is **both necessary and sufficient**. Pinned by `tests/unit/python/test_deliberation_plan.py::TestRenderPlanSectionNeutralizesEnvelopeTags`.
   - **Free-text half — affirmed accepted.** The residual is bounded free text (parser-capped: echoed placeholders discarded, list fields capped, every field length-bounded) that *stays inside* the persona's own, clearly-labelled-private envelope and steers its own post. With the structural breakout closed, this elevation is the **accepted risk**, sound precisely because content can no longer escape the frame it is declared within.

   Each future deliberation with its own plan schema inherits this boundary and the same obligation; a renderer that wraps its plan in any envelope inherits the same neutralize-its-own-tags duty.

## Non-goals

- Building a generic `deliberate()` engine now (N=1; OQ 5).
- Moving any code or standing up an extracted package — [RFC 0045](0045-open-core-extraction-policy.md) governs that; this amendment moves nothing.
- Generalizing the gate. The salience gate's open-floor / size-cap / metric policy is salience-specific and stays so; a second consumer brings its own gate policy, not a shared one.

## Related documentation

- [RFC 0051 — Reasoning Before Posting](0051-reasoning-before-posting.md) — the deliberation this generalizes; §C value types, §D mechanism, OQ 5 extraction resolution.
- [RFC 0028 — Agent Decision Policy Engine](0028-agent-decision-policy-engine.md) — the decision engine (delegation / decision-making / action-class); RFC 0051's forward consumer.
- [RFC 0045 — Open-Core Library Extraction Policy](0045-open-core-extraction-policy.md) — the governance that decides *if* the kernel is ever extracted.
- [RFC 0023 — LLM Call Leasing](0023-llm-call-leasing.md) / [RFC 0034 — Persona Conversational Working Memory](0034-persona-conversational-working-memory.md) — the lease + transcript substrate the kernel depends on.
