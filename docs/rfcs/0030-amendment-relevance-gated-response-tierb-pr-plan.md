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

1. **Tier B — the cheap salience bid (no pile-on).** On the *ambiguous open-floor remainder* Tier A leaves (a `participant` admitted with `reason="policy_always"` — not a directed `@`-mention, not a DM, not a thread-reply-to-self), run a cheap, **leased** `fast`-model bid that asks "do I have something worth adding that hasn't been said?" Only `participant`s whose bid clears their `threshold` reach the expensive quality turn. **Bias-to-silence**: an unset/zero threshold biases to *no reply*. The bid reads the v0.3.7 in-round transcript (RFC 0034 [persona conversational working memory](0034-persona-conversational-working-memory.md), Phase 2 — shipped v0.3.7) so "someone already made my point" → silence. This is the layer that turns Tier A's "reply when admitted" into a real dynamic "reply when you have something to add."
2. **The `chair` disposition — a low-threshold facilitator (Layer-5-inert).** A `participant` with a low (configurable) default `threshold`: it clears Tier B easily and keeps the discussion moving. Its "Layer 5 hooks" (moderator transcript-level continue/wrap-up/terminate) are **wired but inert** — a v0.3.8 `chair` **cannot close an interaction**; convergence is owned by Layers 1/2/4 ([governance-layers PR plan](0030-governance-layers-pr-plan.md)). Full moderator behaviour stays **v0.4.0** ([RFC 0030 §I](0030-multi-agent-conversation-governance.md#i-layer-5--moderator-role)).
3. **Natural-language addressing as a salience signal.** A free-text "let's hear from Iron Fox on this" raises Iron Fox's bid and lowers others'. It is **not** a hard filter — structured `@`-mentions remain the only deterministic directed-elsewhere drop ([amendment OQ #2](0030-amendment-relevance-gated-response.md#open-questions)).

**Explicitly deferred** (not in this plan, per the [master plan §Out of scope](../v0.3.8-plan.md)): RFC 0030 Layer 5 moderator / bid-and-select (the `chair`'s active half) → v0.4.0; salience-threshold calibration (ship conservative, calibrate post-soak — [amendment OQ #3](0030-amendment-relevance-gated-response.md#open-questions)); Layer 6 declarative conversation types. The governance Layers 1/2/4 and the interaction-summary surface are sibling Phase-1 workstreams with their own PR plans — Tier B composes with them on the publish path (see [§Composition](#composition-with-the-governance-layers)) but does not depend on them.

**Prerequisite (satisfied)**: the v0.3.7 in-round transcript ([RFC 0034 Phase 2](0034-phase2-pr-plan.md), shipped) — Tier B's salience quality hard-depends on the persona seeing what has already been said this round ("judging relevance in a vacuum is hopeless" — [amendment](0030-amendment-relevance-gated-response.md#the-graduated-response-gate-layer-3-evolved)). Also reuses: the RFC 0024 SalienceWake threshold/rate-limit machinery, the [RFC 0033](0033-model-alias-layer.md) `fast` model alias (the bid model), and [RFC 0023](0023-llm-call-leasing.md) leasing (bounds + attributes the bid cost).

### The architectural seam: Tier A is pure; Tier B is not

Tier A lives in [`agents/response_gate.py`](../../agents/response_gate.py)'s `evaluate_response_gate` — a **pure** function (no LLM, no I/O, no lease). Tier B issues a leased `fast`-model call, so it **cannot** live inside that pure function. The seam is therefore:

- `evaluate_response_gate` stays pure and unchanged in shape: inside its `if policy == POLICY_ALWAYS:` branch ([response_gate.py:259](../../agents/response_gate.py#L259)) it returns `GateDecision(respond=True, policy=POLICY_ALWAYS, reason="policy_always")` for an open-floor `participant` admit ([the return at response_gate.py:287](../../agents/response_gate.py#L287)) — exactly the branch Tier B refines.
- Tier B is a **new async, leased stage** in the caller (`_ActionLoopMixin._on_event_inner`, [agents/persona_runtime/action_loop.py](../../agents/persona_runtime/action_loop.py), where `evaluate_response_gate` is invoked), invoked **only** when the pure gate returns the open-floor admit (`policy=POLICY_ALWAYS, reason="policy_always"`). Directed admits (`reason="mentioned"`, `"dm"`, `"thread_reply_to_self"`) and structural admits skip the bid — they are already the persona's lane.

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

**Merge order: PR 1 → PR 2a → PR 2b → PR 3 → PR 4.**

> **PR 2 split (maintainer decision, 2026-06-07).** PR 2 is delivered as two
> bisectable PRs so the genuinely novel bid logic lands and is fully tested
> *before* the cross-language store/wire plumbing:
> - **PR 2a — the bid core (Python only).** The leased `fast`-model bid
>   module (`agents/tier_b_salience.py`), the `is_open_floor_admit` gate
>   helper, the action-loop seam (`agents/persona_runtime/tier_b_gate.py`),
>   the suppression metrics, and the unit + wiring + cost-regression tests.
>   The seam is **dormant by construction**: it fires only when the inbound
>   event is flagged `tier_b_active`, a flag nothing sets until PR 2b — so
>   PR 2a is additive and the v0.3.7 response behaviour is unchanged (the
>   same inertness discipline PR 1 used).
> - **PR 2b — the store/wire boundary (Go + proto).** A nullable
>   `threshold` column on the `memberships` table (SQLite migration), carried
>   through `Member`/`AddMember`/`GetMembers`/reconcile → `DispatchEnvelope`
>   → new `ChannelMessageEvent` proto fields (`tier_b_active`, `threshold`,
>   `channel_size`, `tier_b_max_channel_members`) → the Python payload,
>   plus the `tier_b_max_channel_members` channel knob in the schema/config.
>   This **flips the bid live** and lands the no-pile-on integration tests
>   (4-persona open question → small relevant set; redundant follow-up →
>   silence; oversized channel → `addressed`-only). The durable-column
>   approach (vs. an in-memory `Config` resolve at fanout) was chosen to
>   mirror exactly how `respond_policy` already round-trips.


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
PR 2a (Python bid core: leased fast-model salience bid downstream of the pure open-floor admit; reads in-round
       transcript; consumes threshold; bias-to-silence; channel-size cap; action-loop seam; DORMANT until 2b)
  ↓
PR 2b (Go + proto: carry threshold + channel size across the store/wire boundary; flips the bid live;
       no-pile-on integration tests)
  ↓                              ↓
PR 3 (NL-addressing → bid signal)   PR 4 (chair low-threshold behaviour + inert Layer-5 hooks; MT; docs; status; CHANGELOG)
```

PR 3 and PR 4 both build on PR 2b and are independent of each other; sequence PR 3 before PR 4 so the closeout PR documents the full Tier B surface (bid + NL signal + chair).

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
| [`internal/channels/channels.go`](../../internal/channels/channels.go) | Extend `RespondPolicy.Valid()` / the disposition mapping to accept `chair`; `chair` normalizes to the legacy `always` wire value **plus** a low default `threshold` applied at the load boundary (so the wire/gate keep reading the canonical legacy value and the threshold rides alongside *on the in-memory `Config` only*). Hold the per-member `threshold` on the config struct (a new optional `*float64`; nil → unset → bias-to-silence). **Scope note (PR 1):** the threshold does **not** cross the store/wire boundary — neither the `memberships` row nor the `ChannelMessageEvent` wire carry it yet; carrying it to where the bid runs is **PR 2's** job (see the `PERSISTENCE/WIRE GAP` note on `MemberConfig.Threshold`). |
| [`internal/channels/config.go`](../../internal/channels/config.go) | Parse + validate the per-member `threshold` (finite, range `0`–`1` — an explicit `IsNaN` guard closes the gap the bare range comparison misses; absent allowed); reject a `threshold` set on a non-open-floor disposition with `ErrThresholdNotApplicable` (the salience bid only runs on `participant`/`chair`/legacy `always`, so a bar on `addressed`/`observer`/`when_mentioned` is a silent no-op — a cross-field invariant the JSON schema does not express). **Note:** this rejection is a deliberate tightening vs. the v0.3.7 reserved/no-op field — a config that set `threshold` on a non-open-floor member loaded as a no-op before and now fails loudly at load — so capture it in PR 4's CHANGELOG Upgrade Note. Apply the `chair`→`always`+low-threshold default at the load boundary; an unknown disposition still errors with `ErrInvalidRespondPolicy`. |
| `config/channels.yaml` (template) | Add a commented example of a `chair` member and a per-member `threshold`; note bias-to-silence on unset. |
| [`agents/response_gate.py`](../../agents/response_gate.py) | Add a `POLICY_CHAIR` / `chair` alias to `_DISPOSITION_ALIASES` (→ `always`) for defence-in-depth if a `chair` value ever reaches the gate un-normalized. **No branch-behaviour change** — the pure gate keeps reading the normalized legacy values; `threshold` is **not** on the wire in PR 1 (see the channels.go scope note above), so the gate neither sees nor reads it. |
| `internal/channels/config_test.go` | **(TDD — write first.)** A `chair` member loads and normalizes to `always` with the low default threshold; an explicit `chair` threshold overrides the default; a per-member `threshold` in `[0,1]` (incl. boundaries) loads; out-of-range and NaN `threshold` error (`ErrInvalidThreshold`); a `threshold` on a non-open-floor disposition errors (`ErrThresholdNotApplicable`); an unknown disposition errors. |
| schema-validation + `tests/unit/python/test_response_gate*.py` | **(TDD — write first.)** `make validate` accepts `chair` and a `threshold`; rejects an out-of-range `threshold` and an unknown `respond`; existing v0.3.7 configs (no `threshold`) still validate (back-compat). The pure gate suite passes unchanged. |

**Acceptance**: `go test ./internal/channels/...` + the Python schema-validation lane green; `make validate` accepts `chair`/`threshold` and rejects unknown/out-of-range; **no gate behaviour change** — the existing response-gate suite passes unchanged; configs without `threshold` load identically to v0.3.7.

---

### PR 2a: `feature/v038-rfc0030-tierb-bid` — The leased salience bid core (Python; dormant)

**Depends on**: PR 1.
**Status**: 🔀 PR open.
**Purpose**: Land the bid logic and its action-loop seam, fully TDD'd, **dormant** until PR 2b carries its inputs across the store/wire boundary. A `participant` admitted on open-floor traffic of a Tier-B-governed channel runs a cheap, leased `fast`-model bid and stays silent unless it clears its `threshold` (the no-pile-on win); the seam is gated on a `tier_b_active` flag that nothing sets until PR 2b, so PR 2a is additive (v0.3.7 behaviour unchanged).

| File | Change |
|------|--------|
| `agents/tier_b_salience.py` (new) | The pure bid: `evaluate_salience(...) -> SalienceDecision`. Builds a compact prompt over the inbound message + the v0.3.7 in-round transcript; calls the **`fast`** alias under an **RFC 0023 lease** (`cause=CAUSE_CHANNEL_MESSAGE`); parses `speak`/`score`. **Bias-to-silence**: parse failure, lease denial, unresolvable alias, or `score < threshold` → silence; an unset (`None`) threshold demands a *decisive* score (TB2). Also the pure `skip_bid_for_channel_size(...)` predicate (TB6). |
| `agents/response_gate.py` | `is_open_floor_admit(decision)` helper — the seam the caller uses to decide whether to layer the bid on a pure Tier-A admit (TB1); plus a `POLICY_LOW_SALIENCE` synthetic metric label. The pure gate's branches are unchanged. |
| `agents/persona_runtime/tier_b_gate.py` (new) | The action-loop seam (`run_tier_b_gate`): reads the bid inputs off the inbound payload (`tier_b_active`/`threshold`/`channel_size`/`tier_b_max_channel_members`), enforces the TB6 cap, runs the bid, emits the suppression metrics, ingests a suppressed message, and hands the reusable formatted-message + conversation-window seed back on the speak path. Carved out of `action_loop.py` to respect the 500-line cap. |
| `agents/persona_runtime/action_loop.py` | Invoke `run_tier_b_gate` right after the gate admits; on a silent verdict return `DO_NOTHING` *before* memory recall / the quality turn; reuse the seam's seed/message on the speak path. |
| `agents/observability/metrics.py` | A `channel.messages.tier_b_skipped{reason}` counter (bid skipped, not run — TB6) + `tier_b_skip_attrs`; the low-salience suppression rides the existing `channel.messages.gated` counter with `policy=low_salience`. |
| `tests/unit/python/test_tier_b_salience.py` (new) | **(TDD.)** redundant follow-up → silence; in-domain unaddressed → speak; unset threshold → decisive-only; lease denial / parse failure / unresolvable alias → silence; the bid is leased (`cause=CAUSE_CHANNEL_MESSAGE`) on the `fast` alias; the channel-size cap. |
| `tests/integration/test_tier_b_action_loop.py` (new) | **(TDD — wiring + cost-regression.)** the bid runs only on the open-floor admit of a governed channel; an `observer`/self-sender/directed-mention/explicit `@everyone` broadcast/un-governed channel **never reaches the bid** (no *quality-turn* cost — the bid path itself still pays one cheap bid + a window fetch); a silent verdict suppresses *before* the quality turn; a speak verdict proceeds; the TB6 oversized-channel skip stays silent. |
| `tests/unit/python/test_response_gate_relevance.py` | **(TDD.)** `is_open_floor_admit` is true only for the genuinely un-addressed open-floor admit; false for directed/`@everyone` broadcast/DM/suppressed/observer. |
| `agents/tests/test_observability_metrics.py` | Extend the existing instrument-inventory + unit-parity guard so the new `channel.messages.tier_b_skipped` counter is touched, listed in the expected-instrument set, and pinned to its `{message}` unit — the same drift guard the other split-registered counters already carry. |

**Acceptance**: the new unit + wiring suites pass; `make lint-python` (ruff + mypy) green. The Tier A **response behaviour** (whether each member speaks, i.e. `GateDecision.respond`) is unchanged in every case — additive proof, since the salience seam is dormant without `tier_b_active`. The pure gate's `reason` label *is* refined where it must be to keep explicit-address traffic out of Tier B (an individually-mentioned `always` member → `mentioned`; an explicit `@everyone` broadcast → `broadcast`; both formerly collapsed into the open-floor `policy_always`), so the two response-gate-relevance assertions that pinned those `reason` values moved with the gate. `reason` has no runtime consumer besides `is_open_floor_admit`, so this is inert for v0.3.7 behaviour.

> **File-size-cap heads-up (carry into PR 2b/3/4).** This PR leaves both `agents/persona_runtime/action_loop.py` and `agents/observability/metrics.py` sitting at **exactly 500 lines — the `scripts/checks/file_size.py` ceiling, with zero headroom**. The next edit that *adds* a line to either (e.g. PR 2b's `server_servicers.py` unpack is separate, but any further action-loop seam wiring, or a new instrument in `metrics.py`) will trip the cap. Plan the split *before* writing the change rather than discovering it at lint time — the established move is to carve into a sibling helper (`tier_b_gate.py` / `_metrics_tier_b.py` are the precedents from this PR).

---

### PR 2b: `feature/v038-rfc0030-tierb-wire` — Carry `threshold` + channel size across the store/wire boundary (Go + proto)

**Depends on**: PR 2a.
**Status**: 🔀 PR open.
**Purpose**: Flip the bid live. Carry the per-member `threshold` and the channel-size/cap inputs from the in-memory `Config` (PR 1) all the way to the Python bid, and land the no-pile-on integration story.

> **Design decision (PR 2b, 2026-06-07).** "Tier-B-governed" is resolved **per-member**, not per-channel: a new nullable `memberships.tier_b_active` column (alongside `threshold`) records whether a member was declared with the open-floor participant vocabulary (`participant`/`chair`), and that boolean rides the `ChannelMessageEvent.tier_b_active` wire field. Only `participant`/`chair` members (or a legacy `always` carrying an explicit `threshold`, read as a deliberate opt-in) bid; a bare legacy `always` keeps replying unconditionally, so v0.3.7 channels are byte-identical. This is what survives the `participant`→`always` normalization that would otherwise erase the distinction. The only new *channel-level* knob is `tier_b_max_channel_members` (the TB6 cap).

| File | Change |
|------|--------|
| [`internal/channels/sqlite_schema.go`](../../internal/channels/sqlite_schema.go) + `sqlite_migrations.go` | Add a **nullable `threshold REAL`** column to the `memberships` table via a versioned migration (back-compat: existing rows → `NULL` → unset → bias-to-silence). |
| [`internal/channels/channels.go`](../../internal/channels/channels.go) (`Member`) + `sqlite.go`/`sqlite_query.go` | Add `Threshold *float64` to the `Member` struct; persist + read it in `AddMember` / `CreateChannelWithMembers` / `GetMembers`; carry it from `MemberConfig.Threshold` (PR 1) through `ReconcileConfig`. (Durable column over an in-memory `Config` resolve — mirrors `respond_policy`.) |
| `proto/task.proto` (`ChannelMessageEvent`) + `make proto` | Add `bool tier_b_active`, `optional double threshold`, `int32 channel_size`, `int32 tier_b_max_channel_members`. Regenerate Go + Python stubs. |
| [`internal/channels/grpc_dispatcher.go`](../../internal/channels/grpc_dispatcher.go) (`channelMessageToProto`) + `fanout.go` | Populate the new proto fields: `Threshold` from `env.Recipient.Threshold`, `ChannelSize` from the fanout member count, `TierBActive`/cap from the channel config. `tier_b_active` true iff the channel is Tier-B-governed. |
| `agents/server_servicers.py` | Unpack the new fields into `event.payload` (`tier_b_active`/`threshold`/`channel_size`/`tier_b_max_channel_members`) so `run_tier_b_gate` (PR 2a) reads them. This is the line that **flips the dormant seam live**. |
| `schemas/channel.schema.json` + `config/channels.yaml` + Go loader | Add `tier_b_max_channel_members` (channel-level, default `20`); absent → default. |
| `internal/channels/*_test.go` | **(TDD.)** the migration preserves rows + adds the nullable column; `AddMember`/`GetMembers` round-trip a `*float64` threshold (incl. `NULL`); `ReconcileConfig` carries the PR-1 `MemberConfig.Threshold` to the store. |
| `tests/integration/...` (Tier B no-pile-on) | **(TDD.)** A 4-`participant` governed channel given an open question produces a **small, relevant** reply set, not 4; a follow-up one persona already covered draws **no** duplicate; an oversized channel (`> tier_b_max_channel_members`) falls back to `addressed`-only (no bids fired). The bid-core cost invariants already hold from PR 2a. |

**Acceptance**: the migration + store round-trip tests pass; the 4-persona open-question integration draws a small relevant set (not pile-on) on a governed channel; with `threshold` unset everywhere the channel is conservative (bias-to-silence) but a directed `@`-mention still draws exactly one reply (Tier A path, no bid); existing channels (no `tier_b_active`) behave exactly as v0.3.7 (back-compat).

> **Carry-over from PR 2a review — finalize the suppression-metric taxonomy here.** PR 2a ships the *correct, shippable* version: every bid-silence verdict rides `channel.messages.gated` with `policy=low_salience` and a `reason` attribute (`below_threshold` / `declined` / `lease_denied` / `llm_error` / `model_unresolvable` / `parse_failure`), so a fail-closed branch is at least distinguishable on a dashboard. PR 2b is the natural place to refine the *taxonomy* now that the bid fires live: a fail-closed branch (a wallet/provider outage) is arguably **not** a no-pile-on suppression and should not inflate the `low_salience` bucket. The cleaner shape routes genuine declines (`below_threshold` / `declined`) to `gated{low_salience}` and the fail-closed branches to `channel.messages.tier_b_skipped` (broadening that counter's meaning from "bid skipped (not run)" to "bid produced no genuine verdict"). **Decide deliberately — the bucket is not cleanly binary:** `lease_denied` / `model_unresolvable` never reach the LLM call (a true skip), but `parse_failure` *ran and was billed* with unusable output, and `llm_error` was attempted — so "produced a verdict?" and "cost money?" cross-cut, and a three-way split (genuine-decline / ran-but-unusable / never-ran) may fit better than two. Trade-off: a per-cause split means the "stayed-silent" total is `gated{low_salience}` + `tier_b_skipped` rather than a single counter. Update the `_metrics_tier_b` + `SalienceDecision.reason` docstrings and split the test assertions with whatever shape is chosen.

---

### PR 3: `feature/v038-rfc0030-tierb-nl-addressing` — Natural-language addressing as a salience signal

**Depends on**: PR 2b.
**Status**: 🔀 PR open.
**Purpose**: Free-text "let's hear from Iron Fox" biases the bid toward Iron Fox and away from others — **without** re-introducing a deterministic NL directed-elsewhere drop (TB4 / amendment OQ #2).

| File | Change |
|------|--------|
| `agents/salience_addressing.py` (new) | The pure recipient-extraction signal — `detect_nl_addressing(content, persona_name) -> NLAddressing`. A curated, high-precision set of free-text invitation cues ("let's hear from …", "over to …", "what does … think") captures the named recipient and classifies it as *this* persona (`self_named`) or *another* (`other_named`); a pronoun / ambiguous capture / no cue → neither, so no one is suppressed. Carved into its own module so `salience_bid.py` stays under the 500-line cap. |
| `agents/salience_bid.py` | Consume the signal in `evaluate_salience`: invited-by-name lowers the effective score bar (`_ADDRESSED_SELF_BONUS`), someone-else-invited raises it (`_ADDRESSED_OTHER_PENALTY`); `self` wins when both fire; the bar is clamped to `[0,1]`. A short advisory note is added to the bid prompt. The signal is an **input to the bid**, never a hard pre-filter — structured `@`-mentions remain the only deterministic Tier-A drop, and a decisive score still clears even when someone else was invited. |
| `tests/unit/python/test_salience_addressing.py` (new) | **(TDD — write first.)** The pure extractor: self/other classification, first-name subset match, pronoun → neither, no-cue → neither, empty inputs, self-precedence-when-both-named. |
| `tests/unit/python/test_salience_bid.py` | **(TDD — write first.)** NL addressing shifts bid outcomes: a middling score that clears the bar stays silent when someone else is invited, and a sub-bar score speaks when the persona is invited — **without** a deterministic drop (a decisive score still clears; the bid still runs, no pre-filter short-circuit). |
| `tests/integration/test_salience_nl_addressing.py` (new) | **(TDD — write first.)** "let's hear from Iron Fox on this" on a governed channel draws Iron Fox to the quality turn while an un-named persona defers on the same bid score; an un-named persona with a decisive score still speaks (no hard filter). Drives the *real* bid through the action-loop seam. |

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
| CHANGELOG | `[0.3.8]` Upgrade Note: the salience bid (no pile-on, opt-in via `threshold`), the `chair` disposition, NL-addressing-as-a-signal, `tier_b_max_channel_members` — additive **except one validation tightening**: a `threshold` set on a non-open-floor disposition (`addressed`/`observer`/`when_mentioned`), a silent no-op under v0.3.7's reserved field, now fails config load with `ErrThresholdNotApplicable` (landed in PR 1). Defaults bias to silence on open-floor traffic; directed `@`-mentions unchanged. |

**Acceptance**: a fresh `--enable-ui` run on a multi-persona channel shows an open question drawing a small relevant set (not pile-on) and a redundant follow-up drawing silence; the `chair` participates readily but does not close the conversation; `MT-CHANNEL-RELEVANCE-002` recorded; RFC 0030 status flipped; CHANGELOG Upgrade Note present.

---

## Test Strategy (summary)

- **Unit (PR 1)**: Go loader accepts `chair` (→ `always` + low threshold) and a per-member `threshold` in `[0,1]`, rejects out-of-range/NaN (`ErrInvalidThreshold`), a threshold on a non-open-floor disposition (`ErrThresholdNotApplicable`), and unknown dispositions; schema accepts `chair`/`threshold`, rejects unknown; existing configs without `threshold` still validate; pure gate suite unchanged.
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
- [`agents/response_gate.py`](../../agents/response_gate.py), [`agents/persona_runtime/action_loop.py`](../../agents/persona_runtime/action_loop.py) (the gate caller / Tier B stage), `agents/tier_b_salience.py` (new), [`internal/channels/channels.go`](../../internal/channels/channels.go), [`config.go`](../../internal/channels/config.go) — the code this plan touches.
