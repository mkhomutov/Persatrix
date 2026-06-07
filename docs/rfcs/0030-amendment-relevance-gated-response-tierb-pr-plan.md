# RFC 0030 Relevance-Gated-Response Amendment — Tier B PR Implementation Plan (v0.3.8 scope: salience bid + `chair` + NL-addressing)

**Amendment**: [0030-amendment-relevance-gated-response.md](0030-amendment-relevance-gated-response.md)
**RFC**: [0030-multi-agent-conversation-governance.md](0030-multi-agent-conversation-governance.md) (Layer 3, the response gate this evolves; Layer 3b is the new salience tier)
**Created**: 2026-06-07
**Branch prefix**: `feature/v038-rfc0030-tierb-`
**Target**: `main`
**Merge strategy**: Squash merge per [BRANCHING.md](../BRANCHING.md)
**Master plan**: [v0.3.8-plan.md](../v0.3.8-plan.md) (Workstream 1a — the cheap salience bid; no-pile-on)
**Predecessor**: [Tier A PR plan](0030-amendment-relevance-gated-response-pr-plan.md) (v0.3.7 — shipped the disposition vocabulary + the free directed-elsewhere filter this plan builds on)

---

## Overview

v0.3.7 shipped **Tier A** — the free, deterministic, addressing-aware eligibility filter — and the `participant`/`addressed`/`observer` disposition vocabulary, with a per-disposition `threshold` field **reserved/no-op** in the schema so this release is additive ([amendment §Scope](0030-amendment-relevance-gated-response.md#scope--v037--v038--v040)). This plan ships the **v0.3.8 slice** — the three deferred pieces that all hang off the cheap salience bid:

1. **Tier B — the cheap salience bid (no pile-on).** On the *ambiguous open-floor remainder* Tier A leaves (a `participant` admitted with `reason="policy_always"` — not a directed `@`-mention, not a DM, not a thread-reply-to-self), run a cheap, **leased** `fast`-model bid that asks "do I have something worth adding that hasn't been said?" Only `participant`s whose bid clears their `threshold` reach the expensive quality turn. **Bias-to-silence**: an unset/zero threshold biases to *no reply*. The bid reads the v0.3.7 in-round transcript ([RFC 0034 Phase 2](0034-persona-conversational-working-memory.md), shipped v0.3.7) so "someone already made my point" → silence. This is the layer that turns Tier A's "reply when admitted" into a real dynamic "reply when you have something to add."
2. **The `chair` disposition — a low-threshold facilitator (Layer-5-inert).** A `participant` with a low (configurable) default `threshold`: it clears Tier B easily and keeps the discussion moving. Its "Layer 5 hooks" (moderator transcript-level continue/wrap-up/terminate) are **wired but inert** — a v0.3.8 `chair` **cannot close an interaction**; convergence is owned by Layers 1/2/4 ([governance-layers PR plan](0030-governance-layers-pr-plan.md)). Full moderator behaviour stays **v0.4.0** ([RFC 0030 §I](0030-multi-agent-conversation-governance.md#i-layer-5--moderator-role)).
3. **Natural-language addressing as a salience signal.** A free-text "let's hear from Iron Fox on this" raises Iron Fox's bid and lowers others'. It is **not** a hard filter — structured `@`-mentions remain the only deterministic directed-elsewhere drop ([amendment OQ #2](0030-amendment-relevance-gated-response.md#open-questions)).

**Explicitly deferred** (not in this plan, per the [master plan §Out of scope](../v0.3.8-plan.md)): RFC 0030 Layer 5 moderator / bid-and-select (the `chair`'s active half) → v0.4.0; salience-threshold calibration (ship conservative, calibrate post-soak — [amendment OQ #3](0030-amendment-relevance-gated-response.md#open-questions)); Layer 6 declarative conversation types. The governance Layers 1/2/4 and the interaction-summary surface are sibling Phase-1 workstreams with their own PR plans — Tier B composes with them on the publish path (see [§Composition](#composition-with-the-governance-layers)) but does not depend on them.

**Prerequisite (satisfied)**: the v0.3.7 in-round transcript ([RFC 0034 Phase 2](0034-phase2-pr-plan.md), shipped) — Tier B's salience quality hard-depends on the persona seeing what has already been said this round ("judging relevance in a vacuum is hopeless" — [amendment](0030-amendment-relevance-gated-response.md#the-graduated-response-gate-layer-3-evolved)). Also reuses: the RFC 0024 SalienceWake threshold/rate-limit machinery, the [RFC 0033](0033-model-alias-layer.md) `fast` model alias (the bid model), and [RFC 0023](0023-llm-call-leasing.md) leasing (bounds + attributes the bid cost).

### The architectural seam: Tier A is pure; Tier B is not

Tier A lives in [`agents/response_gate.py`](../../agents/response_gate.py)'s `evaluate_response_gate` — a **pure** function (no LLM, no I/O, no lease). Tier B issues a leased `fast`-model call, so it **cannot** live inside that pure function. The seam is therefore:

- `evaluate_response_gate` stays pure and unchanged in shape: it returns `GateDecision(respond=True, policy=POLICY_ALWAYS, reason="policy_always")` for an open-floor `participant` admit — exactly the branch Tier B refines ([response_gate.py:259](../../agents/response_gate.py#L259)).
- Tier B is a **new async, leased stage** in the caller (`_on_event_inner` / the persona runtime), invoked **only** when the pure gate returns the open-floor admit (`policy=POLICY_ALWAYS, reason="policy_always"`). Directed admits (`reason="mentioned"`, `"dm"`, `"thread_reply_to_self"`) and structural admits skip the bid — they are already the persona's lane.

Keeping Tier A pure and Tier B downstream means the v0.3.7 gate suite stays green unchanged, and the bid's cost is leased + attributable at the exact point it materialises (mirrors [RFC 0030 §E](0030-multi-agent-conversation-governance.md#e-layer-1--per-conversation-cost-ceiling)'s "why on the lease, not on the publish").

### Decisions locked at plan-authoring time

Resolved in the [master plan §Open-question status](../v0.3.8-plan.md#open-question-status) and the [amendment OQs](0030-amendment-relevance-gated-response.md#open-questions); mirrored here as load-bearing constraints:

- **TB1 — Tier B fires only on the open-floor admit.** The bid runs iff the pure gate admitted with `policy=POLICY_ALWAYS, reason="policy_always"`. `addressed`/`when_mentioned` admits, DM admits, thread-reply-to-self, `observer`, and self-sender never reach the bid. **PR 2.**
- **TB2 — bias-to-silence.** An unset/zero `threshold` biases the bid to *no reply*. Conservative by construction; calibration is post-soak ([amendment OQ #3](0030-amendment-relevance-gated-response.md#open-questions)). **PR 1 (default) + PR 2 (consumption).**
- **TB3 — every bid is leased + attributable.** The bid acquires an RFC 0023 lease (`cause=CAUSE_CHANNEL_MESSAGE`, `fast` model) before the call; a lease denial fails closed → no bid → silence (RFC 0023 §F). The cost-regression gate asserts an idle/`observer` persona costs **zero** and every bid is leased. **PR 2.**
- **TB4 — structured `@`-mentions stay the only hard directedness filter.** NL addressing biases the bid; it never becomes a deterministic directed-elsewhere drop (amendment OQ #2). **Constrains PR 3.**
- **TB5 — `chair` cannot close an interaction in v0.3.8.** `chair` = `participant` + low default threshold; Layer 5 hooks present but inert. Convergence is owned by Layers 1/2/4. Asserted in test. **PR 4.**
- **TB6 — channel-size cap.** Even a cheap bid × N members is non-trivial at 50+ members. Above a configurable `tier_b_max_channel_members` cap, fall back to `addressed`-only (skip bidding) so Tier B fan-out stays small ([amendment OQ #4](0030-amendment-relevance-gated-response.md#open-questions)). **PR 2.**

---

## Sequencing

**Merge order: PR 1 → PR 2 → PR 3 → PR 4.**

- **PR 1** activates the per-disposition `threshold` field (schema + Go loader) and adds the `chair` disposition value, **inert by construction** — nothing reads `threshold` yet, and `chair` normalizes to `participant` with a low default threshold. (Mirrors the Tier A plan's inert PR 1.)
- **PR 2** is the behaviour-defining PR: the leased `fast`-model salience bid stage downstream of the pure gate's open-floor admit, reading the in-round transcript, consuming `threshold`, with the channel-size cap and the cost-regression extension. The Trigger-style integration repro (open-floor → small relevant set, redundant follow-up → silence) lives here.
- **PR 3** adds NL-addressing as a bid signal (a separable input to PR 2's bid prompt; no new hard filter).
- **PR 4** lights up the `chair` low-threshold behaviour + the inert Layer-5 hooks, authors the manual test, sweeps docs, flips status, and seeds the CHANGELOG Upgrade Note.

Every PR is **TDD-first**: author the failing test (red) — schema-accept-`threshold`/`chair` for PR 1, the redundant-follow-up-→-silence bid test for PR 2 — then implement to green.

---

## Dependency Graph

```
PR 1 (threshold field activation + chair disposition value; schema + Go loader; INERT — nothing reads threshold)
  ↓
PR 2 (Tier B leased fast-model salience bid downstream of the pure open-floor admit; reads in-round transcript;
      consumes threshold; bias-to-silence; channel-size cap; cost-regression extension)
  ↓                              ↓
PR 3 (NL-addressing → bid signal)   PR 4 (chair low-threshold behaviour + inert Layer-5 hooks; MT; docs; status; CHANGELOG)
```

PR 3 and PR 4 both build on PR 2 and are independent of each other; sequence PR 3 before PR 4 so the closeout PR documents the full Tier B surface (bid + NL signal + chair).

---

## Composition with the governance layers

Tier B is Layer 3b in the evaluation order ([amendment §Composition](0030-amendment-relevance-gated-response.md#composition-with-the-existing-layers)). The sibling [governance-layers PR plan](0030-governance-layers-pr-plan.md) owns Layers 1/2/4. The shared contract: **a publish proceeds only if every active layer admits it; a lower-layer drop short-circuits the higher layers** ([RFC 0030 §B](0030-multi-agent-conversation-governance.md#b-layered-architecture)). Concretely:

```
Layer 0:   depth >= cap?                          ──yes──► drop
Layer 1:   lease available? (cost ceiling)         ──no──► drop          ← governance-layers plan
Layer 2:   participant under reply budget?          ──no──► drop          ← governance-layers plan
Layer 3a:  eligible? (Tier A, free, shipped v0.3.7) ──no──► drop
Layer 3b:  salient? (Tier B bid — THIS PLAN)        ──no──► stay silent
Layer 2.5: floor control orders the passers (shipped)
Layer 3c:  the quality turn  ──► reply
Layer 4:   end-of-interaction votes                                      ← governance-layers plan
```

The **bid itself is an LLM call**, so the Layer 1 cost ceiling governs it: a bid lease denied by `INTERACTION_BUDGET_EXHAUSTED` fails closed → no bid → silence, exactly like any other lease denial. This is the join point both plans must keep green; the combined manual test (master-plan Phase 3) exercises it.

---

## PR Sequence

### PR 1: `feature/v038-rfc0030-tierb-threshold` — Activate `threshold` + `chair` disposition (inert)

**Depends on**: the v0.3.7 Tier A slice (disposition vocabulary + reserved `threshold`).
**Purpose**: Land the per-disposition `threshold` activation and the `chair` disposition value with a low-threshold mapping, **inert** — nothing reads `threshold` yet and `chair` normalizes to a `participant`-equivalent so the gate is unchanged. Reviewable and bisectable before the bid lands.

| File | Change |
|------|--------|
| `schemas/channel.schema.json` | Extend the member `respond` enum to add `"chair"` (alongside the v0.3.7 `participant`/`addressed`/`observer` + legacy values). Keep the optional per-disposition `threshold` field (number, `0`–`1`) but **remove the "reserved/no-op" caveat** from its description — it is now read by Tier B (PR 2). `additionalProperties: false` still rejects unknown keys (PR 1's red test). |
| [`internal/channels/channels.go`](../../internal/channels/channels.go) | Extend `RespondPolicy.Valid()` / the disposition mapping to accept `chair`; `chair` normalizes to the legacy `always` wire value **plus** a low default `threshold` (so the wire/gate keep reading the canonical legacy value and the threshold rides alongside). Carry the per-member `threshold` through the config struct to the wire payload (a new optional scalar; absent → unset → bias-to-silence). |
| [`internal/channels/config.go`](../../internal/channels/config.go) | Parse + validate the per-member `threshold` (range `0`–`1`; absent allowed); apply the `chair`→`always`+low-threshold default at the load boundary; an unknown disposition still errors with `ErrInvalidRespondPolicy`. |
| `config/channels.yaml` (template) | Add a commented example of a `chair` member and a per-member `threshold`; note bias-to-silence on unset. |
| [`agents/response_gate.py`](../../agents/response_gate.py) | Add a `POLICY_CHAIR` / `chair` alias to `_DISPOSITION_ALIASES` (→ `always`) for defence-in-depth if a `chair` value ever reaches the gate un-normalized. **No branch-behaviour change** — the pure gate keeps reading the normalized legacy values; `threshold` is carried on the payload but not yet read here. |
| `internal/channels/config_test.go` | **(TDD — write first.)** A `chair` member loads and normalizes to `always` with the low default threshold; a per-member `threshold` in `[0,1]` loads; out-of-range `threshold` errors; an unknown disposition errors. |
| schema-validation + `tests/unit/python/test_response_gate*.py` | **(TDD — write first.)** `make validate` accepts `chair` and a `threshold`; rejects an out-of-range `threshold` and an unknown `respond`; existing v0.3.7 configs (no `threshold`) still validate (back-compat). The pure gate suite passes unchanged. |

**Acceptance**: `go test ./internal/channels/...` + the Python schema-validation lane green; `make validate` accepts `chair`/`threshold` and rejects unknown/out-of-range; **no gate behaviour change** — the existing response-gate suite passes unchanged; configs without `threshold` load identically to v0.3.7.

---

### PR 2: `feature/v038-rfc0030-tierb-bid` — The leased salience bid (Tier B core)

**Depends on**: PR 1.
**Purpose**: The behaviour-defining PR — a `participant` admitted on open-floor traffic no longer always reaches the quality turn; it first runs a cheap, leased `fast`-model bid and stays silent unless the bid clears its `threshold`. This is the no-pile-on win.

| File | Change |
|------|--------|
| `agents/tier_b_salience.py` (new) | The bid stage. `async def evaluate_salience(event, *, agent_id, threshold, transcript, channel_size) -> SalienceDecision`. Builds a compact prompt over the inbound message + the v0.3.7 in-round transcript (RFC 0034 P2 group working memory); calls the **`fast`** alias under an **RFC 0023 lease** (`cause=CAUSE_CHANNEL_MESSAGE`); parses a constrained `speak: yes/no` + salience score in `[0,1]`. **Bias-to-silence**: parse failure, lease denial, or `score < threshold` → `speak=False`. An unset/zero `threshold` → silence unless the score is decisively high (the conservative default, TB2). |
| [`agents/response_gate.py`](../../agents/response_gate.py) | Tier B is **downstream of** the pure gate, not inside it. Expose a small helper (e.g. `is_open_floor_admit(decision)` → `decision.respond and decision.policy == POLICY_ALWAYS and decision.reason == "policy_always"`) so the caller can cheaply decide whether to invoke the bid. The pure function's branches are unchanged. |
| `agents/event_loop.py` (or the persona-runtime event handler) | After the pure gate admits, gate the expensive turn behind the bid **only** for the open-floor admit (TB1): resolve the member `threshold` + channel size from the payload; if `channel_size > tier_b_max_channel_members` (TB6), **skip bidding and fall back to `addressed`-only** (i.e. an un-addressed `participant` stays silent on oversized channels) and emit a `tier_b_skipped{reason="channel_too_large"}` log; else run `evaluate_salience`; on `speak=False`, suppress the turn with a `channel.messages.gated{reason="low_salience"}` metric; on `speak=True`, proceed to recall + the quality turn unchanged. Reuse the RFC 0024 SalienceWake threshold/rate-limit machinery. |
| `agents/model_aliases.py` | Consumer only — the bid uses the `fast` alias; no change (asserted by test). |
| schema/config | Add `tier_b_max_channel_members` (channel-level, default e.g. `20`) to `schemas/channel.schema.json` + `config/channels.yaml` + the Go loader; absent → default. |
| `tests/unit/python/test_tier_b_salience.py` (new) | **(TDD — write first.)** The bid returns `no` when the in-round transcript already contains the persona's point (redundant follow-up); `yes` when the message is in the persona's domain and unaddressed; an unset/zero `threshold` biases to `no`; a lease denial → `no` (fail-closed, TB3); a parse failure → `no`. The bid uses the `fast` alias. |
| `tests/integration/...` (Tier B no-pile-on) | **(TDD — write first.)** A 4-`participant` channel given an open question produces a **small, relevant** reply set, not 4; a follow-up one persona already covered draws **no** duplicate; an oversized channel (`> tier_b_max_channel_members`) falls back to `addressed`-only (no bids fired). |
| cost-regression gate (extend) | **(TDD — write first.)** Tier B **never bills idle/`observer`/self-sender personas** (they never reach the bid); every bid is **leased + attributable** (`cause=CAUSE_CHANNEL_MESSAGE`, `fast` model); an N-member all-`participant` channel spends N cheap bids + k full turns (k ≪ N), not N full turns — assert the Tier-C population shrank vs. the v0.3.7 all-admit baseline and total wallet spend did not rise on busy channels. |

**Acceptance**: the redundant-follow-up bid test returns silence; the 4-persona open-question integration draws a small relevant set (not pile-on); the cost-regression gate confirms idle/`observer` cost zero, every bid is leased, and Tier-C population shrank; with `threshold` unset everywhere the channel is conservative (bias-to-silence) but a directed `@`-mention still draws exactly one reply (Tier A path, no bid).

---

### PR 3: `feature/v038-rfc0030-tierb-nl-addressing` — Natural-language addressing as a salience signal

**Depends on**: PR 2.
**Purpose**: Free-text "let's hear from Iron Fox" biases the bid toward Iron Fox and away from others — **without** re-introducing a deterministic NL directed-elsewhere drop (TB4 / amendment OQ #2).

| File | Change |
|------|--------|
| `agents/tier_b_salience.py` | Add a light recipient-extraction signal to the bid prompt/scoring: when the inbound message names a participant in free text (e.g. "to Iron Fox", "let's hear from …"), raise that persona's salience and lower others'. The signal is an **input to the bid**, never a hard pre-filter — structured `@`-mentions remain the only deterministic Tier-A drop. Keep extraction conservative (high precision; an ambiguous name does not suppress anyone). |
| `tests/unit/python/test_tier_b_salience.py` | **(TDD — write first.)** NL addressing shifts bid outcomes: the named persona's `speak` flips toward `yes`, a non-named persona toward `no` — **without** a deterministic drop (a non-named `participant` with a genuinely novel, in-domain contribution can still clear the bid). Assert no new hard NL filter exists (a non-named persona is never suppressed *before* the bid). |
| `tests/integration/...` | **(TDD — write first.)** "let's hear from Iron Fox on this" on a multi-`participant` channel draws primarily Iron Fox; the others mostly defer but are not hard-dropped. |

**Acceptance**: NL addressing biases the bid (named persona up, others down) without a deterministic directed-elsewhere drop; structured `@`-mention behaviour from Tier A is unchanged; the unit suite proves the no-hard-filter invariant.

---

### PR 4: `feature/v038-rfc0030-tierb-chair-closeout` — `chair` facilitator (Layer-5-inert) + MT + docs + status

**Depends on**: PR 3.
**Purpose**: Light up the `chair` low-threshold behaviour, wire the inert Layer-5 hooks, and land the operator-facing surface + the acceptance record.

| File | Change |
|------|--------|
| `agents/tier_b_salience.py` / config | `chair` resolves to a `participant` with the low default `threshold` from PR 1, so a `chair` clears the bid readily and keeps the discussion moving. Wire the **inert** Layer-5 hooks — a documented seam where the v0.4.0 moderator's transcript-level continue/wrap-up/terminate will attach — present but **not invoked** (the same reserved-field pattern v0.3.7 used for `threshold`). |
| test (chair) | **(TDD — write first.)** A `chair` member clears the bid at a low threshold where a default `participant` would stay silent; a `chair` **cannot close an interaction** (TB5) — assert no close path is reachable from the `chair` disposition in v0.3.8 (the Layer-5 hook is inert). |
| `docs/manual-tests/MT-CHANNEL-RELEVANCE-002.md` (new) | **New.** Multi-`participant` channel with one `chair`: an open-floor question draws a small relevant set (no pile-on), a redundant follow-up draws silence, the `chair` participates readily, NL addressing ("let's hear from X") biases toward X. Records explicitly that the `chair` does **not** terminate the conversation (that is Layers 1/2/4 / v0.4.0). |
| [`docs/guides/channels.md`](../../docs/guides/channels.md) + [`persona-agents.md`](../../docs/guides/persona-agents.md) | Document the salience bid, the per-disposition `threshold` (bias-to-silence default), the `chair` disposition (low-threshold facilitator, **cannot close interactions** in v0.3.8), `tier_b_max_channel_members`, and NL-addressing-as-a-signal. Clear any residual "`threshold` reserved/no-op" prose. |
| [`0030-amendment-relevance-gated-response.md`](0030-amendment-relevance-gated-response.md) + [`0030-multi-agent-conversation-governance.md`](0030-multi-agent-conversation-governance.md) | Status hygiene: Tier B + `chair` + NL-addressing marked implemented in v0.3.8; Layer 5 moderator / bid-and-select remains v0.4.0. RFC 0030 Master Index note reflects the Tier-B layer landing; `make rfcs` regenerates INDEX. |
| `docs/ai-glossary.md` | Add/confirm *salience bid*, *disposition threshold*, *`chair`* entries. |
| CHANGELOG | `[0.3.8]` Upgrade Note: the salience bid (no pile-on, opt-in via `threshold`), the `chair` disposition, NL-addressing-as-a-signal, `tier_b_max_channel_members` — all additive; defaults bias to silence on open-floor traffic, directed `@`-mentions unchanged. |

**Acceptance**: a fresh `--enable-ui` run on a multi-persona channel shows an open question drawing a small relevant set (not pile-on) and a redundant follow-up drawing silence; the `chair` participates readily but does not close the conversation; `MT-CHANNEL-RELEVANCE-002` recorded; RFC 0030 status flipped; CHANGELOG Upgrade Note present.

---

## Test Strategy (summary)

- **Unit (PR 1)**: Go loader accepts `chair` (→ `always` + low threshold) and a per-member `threshold` in `[0,1]`, rejects out-of-range/unknown; schema accepts both, rejects unknown; existing configs without `threshold` still validate; pure gate suite unchanged.
- **Unit (PR 2)**: bid returns `no` on a redundant in-round point; `yes` on in-domain unaddressed; unset threshold → silence; lease denial / parse failure → silence (fail-closed); bid uses `fast`.
- **Integration (PR 2)**: 4-`participant` open question → small relevant set (not 4); redundant follow-up → no duplicate; oversized channel → `addressed`-only fallback.
- **Cost regression (PR 2)**: idle/`observer`/self cost zero; every bid leased + attributable; Tier-C population shrank vs. the v0.3.7 baseline; busy-channel spend does not rise.
- **Unit + integration (PR 3)**: NL addressing biases the bid without a hard filter; named persona up, others defer-not-dropped.
- **Chair (PR 4)**: `chair` clears at low threshold; `chair` cannot close an interaction (Layer-5 hook inert).
- **Manual (PR 4)**: `MT-CHANNEL-RELEVANCE-002` — no pile-on, silence-when-redundant, `chair` facilitates but does not terminate.
- **Regression**: every PR keeps the v0.3.7 response-gate + Tier A suites green; PR 1 is behaviourally inert.

---

## Status & ROADMAP hygiene

Per [master-plan §ROADMAP hygiene](../v0.3.8-plan.md#roadmap-hygiene):

- **PR 1 open** → no RFC status change (vocabulary/field activation only; companion PR plans excluded from `INDEX.md`). Clear the "`threshold` reserved/no-op" caveat in the schema description as `threshold` becomes live.
- **PR 2 merges (Tier B lands)** → RFC 0030 Master Index note reflects the Tier-B salience layer moving from planned to implementing; `make rfcs` regenerates INDEX; `Last updated` refresh.
- **PR 4 merges** → CHANGELOG `[0.3.8]` Upgrade Note seeded; amendment status records Tier B + `chair` + NL-addressing landed; Layer 5 remains v0.4.0.
- **v0.3.8 tag** → `MT-CHANNEL-RELEVANCE-002` + the combined convergence MT (`MT-CONVERSATION-CONVERGENCE-001`) re-run live on HEAD as a release gate (master-plan Phase 3); the cost-regression gate confirms the idle-cost invariant survives Tier B on the tag tip.

---

## Related documentation

- [RFC 0030 Relevance-Gated-Response Amendment](0030-amendment-relevance-gated-response.md) — the design; Tier B + `chair` + NL-addressing is this plan's slice; bid-and-select is the v0.4.0 Layer 5 target.
- [Tier A PR plan](0030-amendment-relevance-gated-response-pr-plan.md) — the v0.3.7 predecessor; this plan reuses its disposition vocabulary and builds Tier B on its open-floor admit branch.
- [RFC 0030 — Multi-Agent Conversation Governance](0030-multi-agent-conversation-governance.md) — §G Layer 3 (the gate), §I Layer 5 (the moderator Tier B evolves toward), §B (the layered admission contract).
- [Governance-layers PR plan](0030-governance-layers-pr-plan.md) — Layers 1/2/4, the sibling Phase-1 workstream Tier B composes with on the publish path.
- [RFC 0034 Phase 2 PR plan](0034-phase2-pr-plan.md) — supplies the in-round transcript Tier B's bid reads (shipped v0.3.7).
- [RFC 0023 — LLM Call Leasing](0023-llm-call-leasing.md) — bounds + attributes the bid cost; the fail-closed guarantee Tier B inherits.
- [RFC 0033 — Model Alias Layer](0033-model-alias-layer.md) — the `fast` alias the bid runs on.
- [v0.3.8 plan](../v0.3.8-plan.md) — the release this lands in; Workstream 1a.
- [`agents/response_gate.py`](../../agents/response_gate.py), [`agents/event_loop.py`](../../agents/event_loop.py), [`internal/channels/channels.go`](../../internal/channels/channels.go), [`config.go`](../../internal/channels/config.go) — the code this plan touches.
