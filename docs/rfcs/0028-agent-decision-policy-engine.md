---
id: RFC-0028
title: Agent Decision Policy Engine
summary: Per-agent declarative policy engine governing tool selection, delegation, and channel-publish decisions — Phases 1–3 in v0.4.0, collective extension in v0.5.0+.
type: architecture
status: accepted
author: Maksim Khomutov
created: 2026-05-02
target: v0.4.0 (Phases 1–3); v0.5.0+ (Phase 4 collective extension)
depends_on:
  - RFC-0005
  - RFC-0009
---

# RFC 0028 - Agent Decision Policy Engine

**Type**: architecture
**Status**: 👍 Accepted — Phases 1, 2 and 2a for v0.4.0, Phase 3 cuttable; Phase 4 for v0.5.0+
**PR plan**: [0028-pr-plan.md](0028-pr-plan.md) — Phases 1–3, the v0.4.0 plan's rows B1–B3 and D1
**Accepted**: by the maintainer's review approval of [#1047](https://github.com/mkhomutov/Persatrix/pull/1047), row R1 of the [v0.4.0 plan](../v0.4.0-plan.md#progress)
**Author**: Maksim Khomutov
**Date**: 2026-05-02
**Target**: v0.4.0 (Phases 1–3); v0.5.0+ (Phase 4 collective extension)
**Depends on**: RFC 0005, RFC 0009
**Integrates with**: RFC 0008, RFC 0011, RFC 0012, RFC 0020, RFC 0021, RFC 0037 (recommended sequencing, not hard gates)

---

## Table of Contents

- [Summary](#summary)
- [Motivation](#motivation)
- [Goals](#goals)
- [Non-Goals](#non-goals)
- [Design / Implementation](#design--implementation)
- [Security Considerations](#security-considerations)
- [Phased Implementation Plan](#phased-implementation-plan)
- [Files Touched (Estimated)](#files-touched-estimated)
- [Test Strategy](#test-strategy)
- [Open Questions](#open-questions)
- [Decision / Next Steps](#decision--next-steps)
- [Related Documentation](#related-documentation)

---

## Summary

Introduce a shared decision layer for both task agents and persona agents so action selection is explicit, auditable, and tunable. Today, decision behavior is split across prompt text, ad-hoc runtime branches, and per-feature hooks. This RFC defines a Decision Policy Engine that evaluates candidate actions at well-defined decision checkpoints and supports three operating modes: manual (rule-based), semi-automated (policy recommendation with approval gates), and automated (policy-selected actions within hard guardrails).

The goal is not to replace model reasoning. The goal is to make decision-making behavior measurable, replayable, and evolvable without breaking existing autonomy controls or deny-by-default permissions.

## Motivation

Current agent behavior has three gaps:

1. **Decision logic is implicit.** Many choices (respond, ask clarification, delegate, post to channel, wait) are hidden in prompt composition or local branches, making behavior hard to reason about.
2. **Task agent and persona agent diverge.** Similar choices are implemented differently, which blocks shared calibration and slows feature rollout.
3. **Low observability for decision quality.** We capture outputs and tool calls, but not a structured "why this action" record suitable for replay, tuning, or regression analysis.

Without a shared decision layer, increasing autonomy risks brittle behavior and longer incident triage.

## Goals

1. Define a unified decision contract for task agents and persona agents.
2. Add explicit decision checkpoints in runtime loops (pre-act, pre-tool, pre-delegate, post-outcome).
3. Support three approaches in one architecture:
   1. **Manual (deterministic):** rule tree and fixed scoring.
   2. **Semi-automated:** policy proposes ranked actions; guardrails and approval gates arbitrate.
   3. **Automated:** policy selects action directly within non-bypassable guardrails.
4. Preserve existing autonomy semantics (`manual`, `semi-autonomous`, `autonomous`, `supervisor`) by mapping them to policy mode + approval configuration.
5. Emit decision telemetry and audit records so behavior can be replayed and calibrated.
6. Define a future-compatible path for collective decisions when persona agents operate in societies.
7. Define mandatory human-in-the-loop controls for decision classes that must never auto-execute.

## Non-Goals

- Replacing deny-by-default permission checks from RFC 0009.
- Replacing LLM planning prompts or memory retrieval with a new model stack.
- Introducing cross-agent consensus or voting in v0.4.0.
- Shipping online self-modifying policy weights in production (offline tuning only in this RFC).

## Design / Implementation

### A. Decision checkpoint model

Add checkpoints where decisions are currently implicit:

1. **Pre-act:** choose next action class (`respond`, `ask_clarification`, `tool_call`, `delegate`, `publish_channel`, `defer`).
2. **Pre-tool:** runs only when pre-act selected `tool_call`; selects tool strategy (single call, sequence, abort). Always sequenced after pre-act in the same tick.
3. **Pre-delegate:** runs only when pre-act selected `delegate`; evaluates whether a sub-agent delegation is necessary and allowed.
4. **Post-outcome:** record outcome quality signal for future calibration.

Checkpoints are strictly sequential within a tick: `pre-act → (pre-tool | pre-delegate) → execute → post-outcome`. Each checkpoint produces a `DecisionRecord` with candidates, selected action, constraints applied, and rationale summary.

### B. Policy modes (manual, semi-automated, automated)

| Mode | How selection works | Human/approval path | Primary use |
|------|----------------------|---------------------|-------------|
| Manual | Rules + deterministic ranking only | Required for high-impact classes | Safety-first or early rollout |
| Semi-automated | Policy ranks actions; guardrails filter; approval may be required | Required per configured action class | Default for persona and task agents in v0.4.0 |
| Automated | Policy selects highest valid action directly | Optional, only for whitelisted low-risk classes | Mature, well-calibrated paths |

All modes share the same guardrail pipeline. Automation changes who selects among valid candidates, not what is permitted.

> 2026-10-20 — The [PR plan](0028-pr-plan.md#phase-0--open-questions-and-design-points) settles these points differently from this RFC's text, which stands corrected by it:
> - The default mode is manual, and semi-automated is opt-in per agent (E-1; this table's last column).
> - The autonomy level does not set the mode, and `autonomy.requires_approval_for` is the approval list (E-3; Goal 4).
> - In manual mode the existing permission, channel-access and wallet gates enforce after selection. From semi-automated mode on, the constraint stage calls the permission and channel-access gates before selection; the wallet, which has no read-only check, keeps refusing at the lease (E-2; §C step 2 and Security Considerations).
> - For a persona, pre-tool runs inside the model loop, before pre-act sees the final action list — the reverse of §A's order (E-6).
> - The hold replaces RFC 0009 §F's agent-side pause: a task agent waits inside its step, a persona is released into a later turn (E-9).
> - The confidentiality-egress gate that §E's RFC 0012 row wires as a mandatory-HITL class is unslotted with RFC 0012 §H, so in v0.4.0 §H's external-communications class covers no shipped action (OQ 7).

### C. Guardrail and selection pipeline

At each checkpoint:

1. Build candidate set from context, memory, and current task state.
2. Apply hard constraints (permissions, autonomy, channel ACL, budget, deadlines).
3. Score remaining candidates (rules, heuristic model, or both).
4. Route through approval gate if action class requires it.
5. Execute selected action and persist `DecisionRecord`.

If no candidate survives constraints, fallback is `defer` plus structured reason.

### H. Mandatory human-in-the-loop decision classes

Some decisions are non-delegable and require explicit human approval even in automated mode. This is a fail-closed control.

Default mandatory human-in-the-loop classes:

1. External communications crossing trust boundaries (public channels, external bridges).
2. Security-sensitive policy changes (permission elevation, sandbox policy change).
3. Irreversible or destructive operations (bulk delete, irreversible data mutation).
4. High-impact organizational actions in society mode (role reassignment, escalation, sanctions).

Execution rules:

1. Agent or collective policy may propose an action, but cannot execute it without an approval token.
2. Approval must be bound to action hash, actor, scope, and expiration time.
3. Missing, invalid, or expired approval token forces `defer` with `reason=hitl_required`.
4. Emergency override path is explicit, auditable, and restricted to authorized human operators.

This section is tightly coupled with RFC 0009: deny-by-default permissions, audit integrity, and policy tamper resistance remain authoritative.

### D. Shared policy contract

Add a Python-side interface for policy evaluation:

- `DecisionContext`: normalized checkpoint input.
- `DecisionCandidate`: action + structured metadata.
- `DecisionOutcome`: selected action, confidence, rejected reasons.
- `DecisionPolicy`: strategy interface with `select(context, candidates)`.

Task agent and persona agent runtimes call the same interface. Persona-specific state (mood, relationship memory, autonomy posture) is carried via typed optional fields in `DecisionContext`.

**Persistence and replay backend.** `DecisionRecord` instances are persisted via two coordinated sinks:

1. OpenTelemetry spans (one per checkpoint) under the existing RFC 0019 tracer, carrying selected action, candidate count, and confidence as span attributes for live observability.
2. A dedicated audit-log stream (`agents/observability/decision_log.py`) appending the full record (candidate set, scores, rejected reasons, rationale) for forensic replay. This is the source of truth for the Phase 2 replay harness; OTEL is sampled and not authoritative.

No new protobuf message is introduced in v0.4.0 (resolves OQ #4); a protobuf schema may be promoted later if cross-process replay or collective scope (Phase 4) requires it.

### E. Integration into existing plans and RFCs

| Existing RFC | Integration point | Effect |
|--------------|-------------------|--------|
| RFC 0005 (Persona + memory) | Persona tick loop and memory-backed context shaping | Persona decisions become explicit and replayable |
| RFC 0008 (Memory context optimization) | Candidate features use budgeted memory signals | Better decision quality under strict context budgets |
| RFC 0009 (Security) | Guardrail stage enforces permission and audit invariants | Automation cannot bypass deny-by-default |
| RFC 0011 (Channels + bridges) | `publish_channel` candidate class and ACL filtering | Safer channel action selection |
| RFC 0020 (Interaction lifecycle) | Checkpoints align with interaction open/close boundaries | Per-interaction decision traces |
| RFC 0021 (Temporal awareness) | Recency and timing features feed candidate scoring | Time-aware decisions become configurable |
| RFC 0012 (Protocols & Organizations) | A cross-context directive enters the pre-act checkpoint as a hard-constraint input (RFC 0012 §F); the accept/adapt/refuse outcome — including conflict resolution and tie escalation — is a `DecisionRecord`; the confidentiality-egress gate (RFC 0012 §H) is wired as a mandatory-HITL class (§H below) | Cross-context influence is decided, ranked, and audited through this engine rather than by an ad-hoc mechanism |
| RFC 0037 (Memory confidentiality) | The acting channel's confidentiality classification scopes which memory candidates are eligible when a checkpoint builds its candidate set | Decisions respect confidentiality without a second enforcement path |

This RFC becomes the umbrella decision architecture for those RFC surfaces; it does not supersede them. RFC 0012 and RFC 0037 are *consumers* of this engine — they supply constraint inputs and a HITL class; they do not change its checkpoint model.

### F. Calibration and rollout

Calibration is offline-first in v0.4.0:

1. Collect `DecisionRecord` telemetry in manual mode.
2. Replay records against candidate outcomes to tune heuristics.
3. Promote selected action classes from manual -> semi-automated -> automated based on thresholds.

No production online learning loop is introduced in this RFC.

### G. Collective decisions for agent societies (future extension)

When societies are introduced (team, lab, or organization-level operation), some actions should support group-level decision policies instead of single-agent choice.

The decision engine should support a second decision scope:

1. **Individual scope** (v0.4.0 baseline): one agent chooses one action.
2. **Collective scope** (v0.5.0+ extension): multiple eligible agents submit ranked candidates, then a collective policy selects the group action.

Planned collective approaches:

1. **Semi-automated collective mode**:
   1. Eligible agents produce proposals and confidence.
   2. Aggregator computes quorum outcome.
   3. High-impact outcomes require supervisor approval.
2. **Automated collective mode**:
   1. Aggregator resolves by configured strategy (majority, weighted majority, role-weighted).
   2. Guardrails still apply before execution.
   3. Automatic execution only for whitelisted action classes.

Each collective decision should emit a `CollectiveDecisionRecord` containing participating agent IDs, proposal set hash, winning option, quorum metadata, and dissent summary for replay and audit.

Integration seams for this extension:

- Organizations and role hierarchies ([RFC 0012](0012-protocols-organizations.md)) define eligibility and vote weight; the same org graph ranks authority for the cross-context directive decisions in Section E.
- Channels and bridges (RFC 0011) define where collective outcomes are published.
- Security and audit (RFC 0009) enforce non-bypassable permissions and forensic traceability.
- Interaction lifecycle (RFC 0020) defines collective decision boundaries and timeout behavior.

## Security Considerations

- **Non-bypassable constraints:** permission, budget, and autonomy checks run before policy selection in every mode.
- **Prompt-injection resilience:** untrusted text may influence candidate scoring but cannot inject disallowed action classes.
- **Approval gate integrity:** approvals are evaluated by runtime configuration, never by model self-assertion.
- **Mandatory HITL controls:** non-delegable classes are fail-closed; no automated bypass is allowed.
- **Approval token security:** approval artifacts are scoped, signed, time-bounded, and replay-protected.
- **Auditability:** each checkpoint emits a decision event for forensic reconstruction and policy regression debugging.
- **Blast-radius control:** rollout is per action class and per agent, with instant fallback to manual mode.

## Phased Implementation Plan

### Phase 1: Decision contract and manual mode baseline

1. Add decision types and policy interface in Python runtime.
2. Instrument task agent and persona agent with pre-act checkpoints.
3. Implement deterministic baseline policy + decision event logging.
4. Add config schema for policy mode and per-action-class approval settings.

### Phase 2: Semi-automated mode + approval routing

1. Add candidate scoring policy with confidence output.
2. Implement approval routing for high-impact classes (`delegate`, `publish_channel`, external bridge actions).
3. Add replay harness for decision traces and regression diffs.
4. Extend telemetry dashboards with acceptance, rejection, and fallback rates.

### Phase 2a: Human-in-the-loop enforcement (security-coupled)

1. Add required-HITL action class registry in configuration and schema.
2. Implement signed, scoped, expiring approval tokens and replay checks. **Issuer:** the orchestrator (Go-side, the security boundary of RFC 0009 §H) is the sole token authority; agents only consume tokens, and the orchestrator verifies each one when it is spent. Tokens are signed with a dedicated approval-signing key that the orchestrator generates once and keeps in the society tables in `data/channels.db`, beside the pending approvals ([RFC 0029 amendment](0029-personal-society-storage-split.md#amendment-2026-10-20--v040s-society-backend)), so a pending approval and its token survive a restart *(Open Question 9)*. They are scoped to `(action_hash, actor_id, scope, expires_at, nonce)` and replay-protected by the pending approval itself: a spend finds the approval by the token's nonce, requires it approved, unexpired and for the calling agent, and flips it to spent in the same transaction. A token expires no later than the pending approval it answers.
3. Enforce fail-closed runtime behavior when approval is absent or invalid.
4. Add RFC 0009-aligned audit events: `decision.hitl_requested`, `decision.hitl_approved`, `decision.hitl_denied`, `decision.hitl_expired`.

### Phase 3: Controlled automation and calibration gates

1. Enable automated mode for whitelisted low-risk classes.
2. Define promotion criteria from semi-automated to automated per class.
3. Add failure policy: confidence floor, forced fallback, and rollout kill-switch.
4. Publish operator guidance for mode selection by environment.

### Phase 4: Collective decision extension for societies (v0.5.0+)

1. Add collective scope decision types and `CollectiveDecisionRecord` schema.
2. Implement aggregation strategies (majority, weighted majority, role-weighted) behind configuration.
3. Add quorum/timeout handling and supervisor override path.
4. Extend replay harness and dashboards with dissent, quorum, and override metrics.

## Files Touched (Estimated)

| Component | Files | Change |
|-----------|-------|--------|
| Python agents | `agents/decision/` (new package) | Decision types, policy interfaces, baseline policies |
| Python agents | `agents/task_agent.py` | Inject decision checkpoints in task loop |
| Python agents | `agents/persona_runtime/` | Inject decision checkpoints in persona lifecycle |
| Python agents | `agents/persona.py`, `agents/base.py` | Shared runtime wiring for decision policy selection |
| Config / schema | `config/agents.yaml`, `schemas/agent.schema.json` | Policy mode + approval routing config |
| Observability | `agents/observability/`, `docs/observability.md` | Decision metrics, dashboards, decision audit log |
| Tests | `tests/unit/python/`, `tests/integration/` | Policy, guardrail, and replay tests |
| Glossary | `docs/ai-glossary.md` | Add canonical entries (see Decision / Next Steps) |
| Docs | `docs/v0.4.0-plan.md` (new), `docs/rfcs/0028-pr-plan.md` (new) | PR slicing and delivery plan |

## Test Strategy

- **Unit tests**: decision candidate generation, constraint filtering, policy selection, and fallback behavior.
- **Integration tests**: task agent and persona agent produce valid `DecisionRecord` events across checkpoints.
- **Security tests**: disallowed actions are rejected in all policy modes.
- **Replay tests**: historical trace replay remains stable across policy changes.
- **Manual tests**: operator-run scenarios comparing manual vs semi-automated outcomes for the same prompts.

## Open Questions

> 2026-10-20 — The [PR plan](0028-pr-plan.md#phase-0--open-questions-and-design-points)
> settles questions 1–3, 7 and 9 for v0.4.0 and names the PR that confirms
> each default; 5 and 6 stay with Phase 4.

1. *(Phase 3)* Which action classes are eligible for automated mode in v0.4.0 versus v0.5.0?
2. *(Phase 2)* Should approval routing be centralized in orchestrator policy services or remain Python-runtime local first?
3. *(Phase 3)* What confidence metric threshold is robust enough for promotion decisions across different persona types?
4. ~~*(Phase 1)* Do we need a protobuf-level decision event schema in v0.4.0, or can we keep it runtime-local until v0.5.0?~~ **Resolved:** runtime-local in v0.4.0 — see Section D (Persistence and replay backend). Protobuf is deferred to v0.5.0 if Phase 4 collective scope requires cross-process replay.
5. *(Phase 4 / v0.5.0+)* Which collective strategy should be the default for societies: simple majority, weighted majority, or role-weighted?
6. *(Phase 4 / v0.5.0+)* How should dissent be persisted and surfaced so collective decisions remain explainable to operators?
7. *(Phase 2a)* Which action classes are globally non-delegable and require mandatory HITL in every environment?
8. *(Phase 2a)* ~~Should HITL approval issuance live in orchestrator security services first, with agents only consuming signed tokens?~~ **Resolved:** yes — see Phase 2a step 2.
9. *(Phase 2a)* ~~Which key signs approval tokens?~~ **Resolved** on acceptance (2026-10-20): the plan-opening default below, confirmed by the society-backend scope lock of the [v0.4.0 plan](../v0.4.0-plan.md#scope-locks); Phase 2a step 2 now says so. *As recorded 2026-10-18:* Phase 2a step 2 said "the same audit-key material used for RFC 0009 audit-log integrity". But RFC 0009's audit chain is an unkeyed SHA-256 checksum, and the RFC 0009 §H it names as issuer defines no token authority. RFC 0009's capability-token key is generated per process and changes on every restart, while a pending approval lives 24 h. **Plan-opening default** (2026-10-18, readiness audit for [v0.4.0's shape](../v0.4.x-sequencing.md#amendment-2026-10-18--v040s-shape-its-prerequisites-its-cuts-and-the-end-of-the-v03x-line)): a dedicated approval-signing key held by the orchestrator, generated once and persisted beside the society backend, so a pending approval and its token survive a restart. That holds because the pending approvals are persisted there too ([RFC 0029 Open Question 2](0029-personal-society-storage-split.md#open-questions), [RFC 0009 open question 10](0009-security-sandboxing.md#genuinely-open-deferred-to-phase-4--v040)). Step 2's nonce store is persisted in the same place. Held in memory, it would forget every used nonce on restart, and a used approval token would verify again until it expired. A token expires no later than the pending approval it answers (RFC 0009's 24 h default), and its used nonce is kept until then. Step 2's wording is corrected when this RFC is accepted.

## Decision / Next Steps

1. Review and accept this RFC as the canonical architecture for agent decision-making.
2. Create `docs/rfcs/0028-pr-plan.md` with PR slices targeting v0.4.0 — written as [0028-pr-plan.md](0028-pr-plan.md), proposed with the acceptance. PR 1 (Phase 1 contract + types) must include canonical glossary entries in `docs/ai-glossary.md` for: *Decision Policy Engine*, *decision checkpoint*, *DecisionRecord*, *human-in-the-loop / HITL*, *approval token*, *agent society*, *collective scope*, *individual scope*.
3. Start Phase 1 in manual mode only; collect baseline telemetry before enabling semi-automated mode.
4. Add roadmap dependency notes so RFC 0028 implementation is sequenced after active v0.3.0 critical-path RFC work. Recommended (not hard-gated) sequencing: land RFC 0008/0011/0020/0021 surfaces before promoting checkpoints from manual to semi-automated.
5. Track collective decision scope as a follow-on for society support (v0.5.0+), aligned with organizations planning.
6. Split out a security-coupled implementation checklist aligned with RFC 0009 for mandatory HITL classes.

## Related Documentation

- [RFC 0005 - Persona Agent & Memory System](0005-persona-agent-memory.md)
- [RFC 0008 - Agent Memory & Context Optimization](0008-agent-memory-context-optimization.md)
- [RFC 0009 - Agent Identity, Security & Sandboxing](0009-security-sandboxing.md)
- [RFC 0011 - Channels + Bridges](0011-channels-bridges.md)
- [RFC 0012 - Protocols & Organizations](0012-protocols-organizations.md)
- [RFC 0020 - Interaction Lifecycle](0020-interaction-lifecycle.md)
- [RFC 0021 - Persona Temporal Awareness](0021-persona-temporal-awareness.md)
- [RFC 0037 - Memory Confidentiality & Channel Classification](0037-memory-confidentiality-channel-classification.md)
- [Development Workflow](../development-workflow.md)
