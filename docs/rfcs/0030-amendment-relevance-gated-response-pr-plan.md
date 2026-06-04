# RFC 0030 Relevance-Gated-Response Amendment — PR Implementation Plan (v0.3.7 scope: Tier A + disposition reframe)

**Amendment**: [0030-amendment-relevance-gated-response.md](0030-amendment-relevance-gated-response.md)
**RFC**: [0030-multi-agent-conversation-governance.md](0030-multi-agent-conversation-governance.md) (Layer 3, the response gate this evolves)
**Created**: 2026-06-04
**Branch prefix**: `feature/v037-rfc0030-relevance-`
**Target**: `main`
**Merge strategy**: Squash merge per [BRANCHING.md](../BRANCHING.md)
**Master plan**: [v0.3.7-plan.md](../v0.3.7-plan.md) (Workstream 1b — addressing-awareness; free, no LLM)

---

## Overview

The v0.3.6 manual test surfaced two response-gate defects ([amendment §Trigger](0030-amendment-relevance-gated-response.md#context)). This plan ships the **v0.3.7 slice only** — the two coupled pieces that are free, deterministic, and need no in-round transcript:

1. **Tier A eligibility — addressing-aware.** A message `@`-mentioning agent *X* must **not** draw a reply from a `participant` agent *Y* who is not also addressed. The gate today has no notion of *directedness*: `always` literally means every message. Tier A adds the **directed-elsewhere** filter (free, no LLM) on top of the existing self-sender / `never` filters.
2. **The disposition reframe.** `respond_policy` (`always`/`when_mentioned`/`never`) is reframed as a **disposition** vocabulary (`participant`/`addressed`/`observer`) with a back-compat mapping, because Tier A's eligibility rule reads in disposition terms (`observer` → filtered; `addressed` → mention-gated; `participant` → open-floor, minus directed-elsewhere). They are coupled, so they ship together ([master-plan §Open-question status](../v0.3.7-plan.md#open-question-status)).

**Explicitly deferred to v0.3.8** (Tier B convergence patch), not in this plan: the cheap salience bid (`fast`-model, leased), the `chair` disposition, natural-language recipient parsing ("only to Iron Fox"), per-disposition salience-threshold calibration. The per-disposition **threshold** field is *reserved/no-op* in v0.3.7 — it exists in the schema so v0.3.8 is additive, but nothing reads it yet ([amendment §Scope](0030-amendment-relevance-gated-response.md#scope--v037--v038--v040)). Tier B hard-depends on the in-round transcript from [RFC 0034 Phase 2](0034-phase2-pr-plan.md), which lands this same release but is consumed only in v0.3.8.

**Prerequisite**: the v0.3.0 channels stack + the RFC 0011 `mentions` payload field (✅ shipped — Tier A reuses it, introducing **no** new LLM-call origin). The response gate [`agents/response_gate.py`](../../agents/response_gate.py) is the single enforcement point; the Go side ([`internal/channels/channels.go`](../../internal/channels/channels.go), [`config.go`](../../internal/channels/config.go)) owns config parsing + the wire `respond_policy` value.

### Decisions locked at plan-authoring time

Resolved in the [master plan](../v0.3.7-plan.md#open-question-status) and the [amendment OQs](0030-amendment-relevance-gated-response.md#open-questions); mirrored here as load-bearing constraints:

- **D1 — disposition lands with Tier A (v0.3.7), not Tier B.** Tier A's eligibility rule is expressed in disposition terms, so the vocabulary must ship with it. **PR 1.**
- **D2 — structured `@`-mentions only.** v0.3.7 keys directedness on the `mentions` list. Natural-language addressing is a v0.3.8 Tier-B salience signal, not a v0.3.7 hard filter (amendment OQ #2). **Constrains PR 2.**
- **D3 — "everyone"/`@here` disables the directed-elsewhere filter** (amendment OQ #5, adopted default): an explicit broadcast admits all `participant`s; no special-case beyond that. **PR 2.**
- **D4 — back-compat is non-negotiable.** `always→participant`, `when_mentioned→addressed`, `never→observer`; existing `config/channels.yaml` files keep loading. Normalization happens at the **Go config-load boundary** so the wire `respond_policy` and every downstream reader (fanout candidate set, floor control, the Python gate) keep seeing the legacy three values — making PR 1 behaviourally inert. **PR 1.**
- **D5 — the idle-cost invariant survives.** Tier A is free (no LLM, no recall) and runs before any provider call; the RFC 0023/0024 "uninvolved persona costs zero" guarantee is re-asserted by the existing cost-regression gate. No new LLM-call origin is introduced. **PR 2 + the cost gate.**

### Where the "everyone" signal comes from (PR 2 decision to lock)

D3 needs a representation of an explicit broadcast. The directed-elsewhere filter must distinguish "addressed to specific others" from "addressed to the room." Lock one of these in PR 2 (the gate is the consumer either way):

- **(a)** a reserved sentinel in the `mentions` list (e.g. `@here`/`everyone` resolves to a well-known token the gate treats as "broadcast → do not suppress"); or
- **(b)** a boolean `mention_everyone` on the payload set by the orchestrator when it expands `@here`.

Default proposal: **(a)** — it reuses the existing `mentions` plumbing end-to-end and needs no new wire field. The gate's rule becomes: *suppress iff `mentions` is non-empty AND `agent_id ∉ mentions` AND the broadcast sentinel ∉ `mentions`.*

---

## Sequencing

**Merge order: PR 1 → PR 2 → PR 3.**

- **PR 1** adds the disposition vocabulary (schema + Go loader back-compat normalization) with **no gate-behaviour change** — disposition values normalize to the legacy `respond_policy` the whole stack already reads. Dark by construction. (Mirrors the floor-control plan's inert PR 1.)
- **PR 2** is the behaviour-defining PR: the Tier A directed-elsewhere filter in `agents/response_gate.py`, plus replicating the cheap filter in the Go candidate-responder set so floor control does not waste a turn on a member the gate will suppress. The integration tests (the Trigger repro) live here.
- **PR 3** is the operator-facing surface + status: `MT-CHANNEL-RELEVANCE-001`, the channel/persona guide naming, RFC 0030 `📋 → 🚧 Implementing`, CHANGELOG Upgrade Note.

Every PR is **TDD-first**: author the failing test (red) — schema-reject for PR 1, the `@ember-owl` directedness repro for PR 2 — then implement to green.

---

## Dependency Graph

```
PR 1 (disposition vocab: schema + config + Go-loader back-compat normalization; threshold reserved/no-op; INERT)
  ↓
PR 2 (Tier A directed-elsewhere filter in response_gate.py; Go candidate-set parity; "everyone" disables filter;
      unit + integration tests; cost-regression gate)
  ↓
PR 3 (MT-CHANNEL-RELEVANCE-001; docs/guides disposition naming; RFC 0030 status; CHANGELOG Upgrade Note)
```

PR 1 carries no behaviour change (back-compat normalization only); PR 2 is the directedness fix; PR 3 is docs + status.

---

## PR Sequence

### PR 1: `feature/v037-rfc0030-relevance-disposition` — Disposition vocabulary + back-compat (inert)

**Depends on**: v0.3.0 channels baseline.
**Purpose**: Land the `participant`/`addressed`/`observer` vocabulary with a back-compat mapping, normalized at the config-load boundary so nothing downstream changes behaviour yet. Reviewable and bisectable before the gate rewire.

| File | Change |
|------|--------|
| `schemas/channel.schema.json` | Extend the member `respond` enum to accept the disposition vocabulary **and** the legacy values: `["participant", "addressed", "observer", "always", "when_mentioned", "never"]`. Add a reserved, optional per-disposition `threshold` field (number, documented **reserved/no-op until v0.3.8 Tier B**). `additionalProperties: false` still rejects anything else (so `make validate` fails an unknown value — PR 1's red test). |
| [`internal/channels/channels.go`](../../internal/channels/channels.go) | Extend `RespondPolicy.Valid()` to accept the disposition values; add a `Normalize()` (or load-time mapping) collapsing `participant→always`, `addressed→when_mentioned`, `observer→never`. The canonical internal/wire representation stays the legacy three values so `fanout`, `floor_control`, and the Python gate are untouched. |
| [`internal/channels/config.go`](../../internal/channels/config.go) | Apply the normalization where `RespondPolicy(raw.Respond)` is parsed, before `Valid()`/validation — a disposition value loads and maps; a legacy value passes through unchanged; an unknown value still errors with `ErrInvalidRespondPolicy`. |
| `config/channels.yaml` (template) | Migrate the demo personas to `participant` (the new recommended surface); leave a commented note that legacy values still load. |
| `agents/response_gate.py` | Add disposition string constants (`POLICY_PARTICIPANT`/`POLICY_ADDRESSED`/`POLICY_OBSERVER`) as recognized aliases of the legacy constants, for defence in depth if a disposition value ever reaches the wire un-normalized. No branch-behaviour change in PR 1 (the gate keeps reading the normalized legacy values). |
| `internal/channels/config_test.go` | **(TDD — write first.)** A `participant`/`addressed`/`observer` member loads and normalizes to `always`/`when_mentioned`/`never`; a legacy-value member loads unchanged; an unknown value errors. |
| `tests/unit/python/test_response_gate*.py` + schema validation test | **(TDD — write first.)** `make validate` accepts the disposition vocabulary and rejects an unknown `respond` value; the existing `always`/`when_mentioned`/`never` configs still validate (back-compat). |

**Acceptance**: `go test ./internal/channels/...` + the Python schema-validation lane green; `make validate` rejects an unknown value and accepts both vocabularies; **no gate behaviour change** — the existing response-gate suite passes unchanged.

---

### PR 2: `feature/v037-rfc0030-relevance-tier-a` — Tier A directed-elsewhere eligibility

**Depends on**: PR 1.
**Purpose**: The behaviour-defining PR — a message `@`-mentioning someone else no longer draws a reply from other `participant`s. Fixes the Trigger directedness defect.

| File | Change |
|------|--------|
| [`agents/response_gate.py`](../../agents/response_gate.py) | Add the **directed-elsewhere** filter ahead of the `always`/`participant` admit branch: when `mentions` is non-empty, `agent_id ∉ mentions`, and the broadcast sentinel (§"everyone" decision) ∉ `mentions`, return `respond=False` with `reason="directed_elsewhere"` — even for a `participant`/`always` member. An **addressed** (`when_mentioned`) member is unchanged (already mention-gated). `observer`/`never` and self-sender stay filtered as today. An open-floor message (empty `mentions`, or a broadcast) admits all `participant`s — forwarded straight to the turn, since Tier B does not exist yet. Add a `channel.messages.gated{reason="directed_elsewhere"}` metric label. |
| [`internal/channels/fanout.go`](../../internal/channels/fanout.go) / [`floor_control.go`](../../internal/channels/floor_control.go) | Replicate the cheap directed-elsewhere filter in the **candidate-responder** computation (it already reads `msg.Mentions` and `RespondPolicy`), so a directed-elsewhere `always`/`participant` member is not queued into the serialized floor round only to be suppressed by the gate and burn the per-turn timeout. Mirrors the floor-control plan's existing best-effort candidate replication; correctness still rests on the receiver gate (PR 2 above). |
| `tests/unit/python/test_response_gate_relevance.py` (new) | **(TDD — write first.)** Unit: a message mentioning `X` does **not** admit `participant` `Y`; admits `X`; an open-floor (no `mentions`) message admits all `participant`s; a broadcast/`@here` mention admits all `participant`s (D3); self-sender and `observer` always filtered; an `addressed` member still requires a direct mention. |
| `tests/integration/...` (channel relevance) | **(TDD — write first.)** Reproduce the Trigger: `"how about you @ember-owl?"` on a multi-`participant` channel draws **exactly one** reply (Ember Owl). An open-floor prompt admits all `participant`s (Tier B silence is v0.3.8, so all reach the turn here). |
| cost-regression gate | Re-assert the idle-cost invariant (D5): Tier A introduces no new LLM-call origin; an idle / directed-elsewhere persona costs **zero** tokens and zero recall. |

**Acceptance**: the `@ember-owl` integration repro draws exactly one reply; unit legs hold; the cost-regression gate confirms no new LLM-call origin and idle-cost-zero preserved; with no mentions present, behaviour is identical to today (open-floor admit-all) — no regression for un-addressed channels.

---

### PR 3: `feature/v037-rfc0030-relevance-closeout` — Manual test + docs + status

**Depends on**: PR 2.
**Purpose**: Operator-facing surface and the acceptance record.

| File | Change |
|------|--------|
| `docs/manual-tests/MT-CHANNEL-RELEVANCE-001.md` | **New.** Multi-`participant` channel; a directed question (`@`-mention of one persona), an open-floor question, and a redundant follow-up. Expected (v0.3.7 scope): directedness suppression — the directed question draws exactly one reply; the open-floor question admits all `participant`s (no Tier B yet). The "no pile-on / silence-when-nothing-to-add" leg is recorded as a **v0.3.8 Tier B** expectation (documented, not asserted here). |
| [`docs/guides/channels.md`](../../docs/guides/channels.md) + [`persona-agents.md`](../../docs/guides/persona-agents.md) | Name the disposition vocabulary (`participant`/`addressed`/`observer`) as the recommended surface; document the back-compat mapping and that the per-disposition `threshold` is reserved for v0.3.8. |
| [`0030-amendment-relevance-gated-response.md`](0030-amendment-relevance-gated-response.md) | Status hygiene: Tier A + disposition marked implemented in v0.3.7; Tier B / `chair` / NL-addressing remain v0.3.8. |
| [`0030-multi-agent-conversation-governance.md`](0030-multi-agent-conversation-governance.md) + ROADMAP | RFC 0030 `📋 Proposed (Draft) → 🚧 Implementing` for the Phase-1 relevance layer; reflect the Tier-A/disposition slice in the RFC Master Index note; `make rfcs` regenerates INDEX. CHANGELOG `[0.3.7]` Upgrade Note: `respond_policy → disposition` (additive, back-compat — existing values keep working). |

**Acceptance**: a fresh `--enable-ui` run on a multi-persona group channel shows a directed `@`-mention drawing exactly one reply; `MT-CHANNEL-RELEVANCE-001` recorded; RFC 0030 status flipped; CHANGELOG Upgrade Note present.

---

## Test Strategy (summary)

- **Unit (PR 1)**: Go loader normalizes disposition→legacy and rejects unknown; schema accepts both vocabularies, rejects unknown; existing configs still validate.
- **Unit (PR 2)**: directed-elsewhere suppresses other `participant`s; mentioned member admitted; open-floor admits all `participant`s; broadcast/`@here` disables the filter; self / `observer` filtered; `addressed` still mention-gated.
- **Integration (PR 2)**: the Trigger repro — `@ember-owl` → exactly one reply; open-floor → all `participant`s reach the turn.
- **Cost regression (PR 2)**: no new LLM-call origin; idle / directed-elsewhere persona costs zero (D5).
- **Manual (PR 3)**: `MT-CHANNEL-RELEVANCE-001` — directedness + the v0.3.8-deferred no-pile-on note.
- **Regression**: every PR keeps the existing response-gate suite (`test_response_gate*.py`) and channel tests green; PR 1 is behaviourally inert.

---

## Status & ROADMAP hygiene

Per [master-plan §ROADMAP hygiene](../v0.3.7-plan.md#roadmap-hygiene):

- **PR 1 open** → no RFC status change yet (vocabulary only; companion PR plans excluded from `INDEX.md`).
- **PR 2 merges (the relevance layer lands)** → RFC 0030 `📋 Proposed (Draft) → 🚧 Implementing`; Master Index note reflects the Tier-A/disposition slice; `make rfcs` regenerates INDEX.
- **PR 3 merges** → CHANGELOG `[0.3.7]` Upgrade Note seeded; amendment status records Tier A + disposition landed, Tier B/Layers remain v0.3.8; `Last updated` refresh.
- **v0.3.7 tag** → `MT-CHANNEL-RELEVANCE-001` + the `@ember-owl` repro re-run live on HEAD as a release gate (master-plan Phase 3); the cost-regression gate confirms the idle-cost invariant on the tag tip.

---

## Related documentation

- [RFC 0030 Relevance-Gated-Response Amendment](0030-amendment-relevance-gated-response.md) — the design; Tier A + disposition is this plan's slice, Tier B/bid-and-select are deferred.
- [RFC 0030 — Multi-Agent Conversation Governance](0030-multi-agent-conversation-governance.md) — §G Layer 3, the gate Tier A evolves.
- [RFC 0034 Phase 2 PR plan](0034-phase2-pr-plan.md) — supplies the in-round transcript Tier B (v0.3.8) will consume; ships the same release.
- [RFC 0030 floor-control amendment + PR plan](0030-amendment-floor-control-pr-plan.md) — Layer 2.5, which orders the speakers Tier A admits; the candidate-responder replication PR 2 extends.
- [RFC 0011 — Channels & Internal Agent Messaging](0011-channels-bridges.md) §D — the `respond_policy` enum + `mentions` field this plan reframes and reuses.
- [v0.3.7 plan](../v0.3.7-plan.md) — the release this lands in; Workstream 1b.
- [`agents/response_gate.py`](../../agents/response_gate.py), [`internal/channels/channels.go`](../../internal/channels/channels.go), [`config.go`](../../internal/channels/config.go) — the code this plan touches.
