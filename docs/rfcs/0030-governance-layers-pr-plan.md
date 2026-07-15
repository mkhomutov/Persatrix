# RFC 0030 Deterministic Governance Layers — PR Implementation Plan (v0.3.8 scope: Layers 1/2/4 — converge + bounded cost)

**RFC**: [0030-multi-agent-conversation-governance.md](0030-multi-agent-conversation-governance.md) (§E Layer 1, §F Layer 2, §H Layer 4; §L telemetry; §M wire/config; §N failure modes)
**Created**: 2026-06-07
**Branch prefix**: `feature/v038-rfc0030-layers-`
**Target**: `main`
**Merge strategy**: Squash merge per [BRANCHING.md](../BRANCHING.md)
**Master plan**: [v0.3.8-plan.md](../v0.3.8-plan.md) (Workstream 1b — the deterministic convergence + bounded-cost layers)

---

## Overview

This plan ships RFC 0030 **Phase 1 — the deterministic layers** ([RFC 0030 §Phased Implementation Plan](0030-multi-agent-conversation-governance.md#phase-1--deterministic-layers-v03x---landed-v038)): the three cheap, fail-safe-by-construction layers that make a conversation *end* with bounded cost — no LLM judgement, **opt-in via config**, additive by default:

1. **Layer 1 — per-interaction cost ceiling** ([§E](0030-multi-agent-conversation-governance.md#e-layer-1--per-conversation-cost-ceiling)). Extend [RFC 0023 leasing](0023-llm-call-leasing.md) `AcquireLease` with an optional `interaction_id` attribution field and an `interaction_budget_tokens` ceiling. The wallet tracks a per-`interaction_id` running total; once it crosses the ceiling, leases are denied with `INTERACTION_BUDGET_EXHAUSTED`, fail-closed (no LLM call happens). Default `0` (uncapped).
2. **Layer 2 — per-participant reply budget** ([§F](0030-multi-agent-conversation-governance.md#f-layer-2--per-participant-reply-budget)). A new in-memory `interactionReplyBudget` tracker on the orchestrator keyed by the RFC 0020 `interaction_id`. A participant's `(K+1)`th publish in one interaction is rejected **pre-persistence** (HTTP 429 + `ErrParticipantBudgetExhausted`). Default `0` (uncapped); human principals are exempt.
3. **Layer 4 — end-of-interaction signal** ([§H](0030-multi-agent-conversation-governance.md#h-layer-4--end-of-interaction-signal)). An explicit `END_INTERACTION_VOTE` agent action ([§OQ-4](0030-multi-agent-conversation-governance.md#open-questions) resolved in favour of the explicit action for auditability). The orchestrator accumulates votes per interaction; K distinct participants voting within W consecutive turns triggers RFC 0020's structural close. Defaults K=2, W=3.

Plus the cross-cutting plumbing all three need: **`interaction_id` propagation on the wire** (REST metadata bag + proto field), the **composition + failure-down rules** ([§B](0030-multi-agent-conversation-governance.md#b-layered-architecture)), and the **telemetry** ([§L](0030-multi-agent-conversation-governance.md#l-telemetry-and-observability)).

> **Producer follow-on (2026-06-10) — ✅ discharged.** This plan shipped the layers *ahead of the `interaction_id` producer* — every "inert until the producer" note below described that interim, which the [interaction-id producer PR plan](0030-interaction-id-producer-pr-plan.md) has since ended (v0.3.8, PRs [#604](https://github.com/mkhomutov/Persatrix/pull/604)–[#606](https://github.com/mkhomutov/Persatrix/pull/606)): the orchestrator-side resolver stamps every publish (overriding inbound claims, which also retires the spoofable-token hazard the PR 3/4 hardening notes flagged), the agent-side `END_INTERACTION_VOTE` + lease threading landed, and both close-path discard preconditions are wired (vote-close inline; idle rotation one-generation-deferred). The layers are load-bearing on real traffic; enforcement knobs stay opt-in/uncapped.

**Explicitly deferred** (not in this plan, per the [master plan §Out of scope](../v0.3.8-plan.md)): Layer 5 moderator ([§I](0030-multi-agent-conversation-governance.md#i-layer-5--moderator-role)) → v0.4.0; Layer 6 declarative conversation types ([§J](0030-multi-agent-conversation-governance.md#j-layer-6--declarative-conversation-types)) → v0.5.0+ (this plan exposes the **raw per-channel knobs**, not the named-type presets that bundle them); Layer 1/2 **default-value calibration** ([§OQ-5](0030-multi-agent-conversation-governance.md#open-questions)) → post-soak (ship uncapped/opt-in, no normative non-zero numbers). The Tier B salience bid ([Tier B PR plan](0030-amendment-relevance-gated-response-tierb-pr-plan.md)) and the interaction-summary surface ([summary-surface PR plan](0020-interaction-summary-surface-pr-plan.md)) are sibling Phase-1 workstreams; this plan composes with them on the publish path but does not depend on them.

**Prerequisites (satisfied)**: RFC 0011 channels + `mentions` payload (shipped v0.3.0); [RFC 0020](0020-interaction-lifecycle.md) Interaction lifecycle + `InteractionTracker` (shipped — Layers 2/4 key on its `interaction_id`); [RFC 0023](0023-llm-call-leasing.md) leasing with the `CAUSE_CHANNEL_MESSAGE = 5` cause reserved (shipped v0.3.2 — Layer 1 extends `AcquireLease`).

### Decisions locked at plan-authoring time

Resolved in the [master plan §Open-question status](../v0.3.8-plan.md#open-question-status); mirrored here as load-bearing constraints:

- **GL1 — all three layers ship opt-in / uncapped by default.** `interaction_budget_tokens=0`, `max_replies_per_participant_per_interaction=0`, end-votes only act when an agent emits the action. With every knob at its default, end-to-end behaviour is **identical to v0.3.7** (the back-compat proof). **All PRs.**
- **GL2 — end-of-interaction signal is an explicit action** (`END_INTERACTION_VOTE`), not a metadata bag (resolves [§OQ-4](0030-multi-agent-conversation-governance.md#open-questions) for auditability — the RFC's own lean). **PR 4.**
- **GL3 — Layer 2 enforces pre-persistence.** An over-budget publish must not appear in channel history (it would pollute future memory recall). The store boundary is the enforcement point ([§F](0030-multi-agent-conversation-governance.md#f-layer-2--per-participant-reply-budget)). **PR 3.**
- **GL4 — human principals are exempt from Layer 2** via `governance.exempt_principals: [human]` ([§OQ-7](0030-multi-agent-conversation-governance.md#open-questions)). DMs are governed, but the reply budget is enforced only against non-human principals. **PR 3.**
- **GL5 — Layer 1 fail-closed.** A wallet/lease failure (or budget exhaustion) means the LLM call does not happen and fanout terminates — the depth cap (Layer 0) remains the always-on net ([§N](0030-multi-agent-conversation-governance.md#n-failure-modes)). **PR 2.**
- **GL6 — composition + failure-down.** A publish proceeds only if every active layer admits it; a lower-layer drop short-circuits the higher layers and increments `governance_drop{layer}`; higher layers fail safely down to lower ones ([§B](0030-multi-agent-conversation-governance.md#b-layered-architecture)). **PR 5.**
- **GL7 — RFC 0028 forward-compat.** Layer 4's end-vote is a plain action and Layer 1/2 drops hand-write their records; proto fields are reserved where the RFC 0028 `DecisionRecord` will later consolidate them, with **no behaviour change** when it lands ([§K](0030-multi-agent-conversation-governance.md#k-integration-with-rfc-0028-decision-engine)). This plan is **not** gated on RFC 0028. **All PRs.**

---

## Sequencing

**Merge order: PR 1 → PR 2 → PR 3 → PR 4 → PR 5.**

- **PR 1** lands `interaction_id` propagation on the wire (REST metadata bag + proto field) — the shared substrate all three layers need. No layer behaviour yet; inert.
- **PR 2** lands Layer 1 (the lease-budget extension on `AcquireLease`).
- **PR 3** lands Layer 2 (the per-participant reply-budget tracker).
- **PR 4** lands Layer 4 (the `END_INTERACTION_VOTE` action + accumulator).
- **PR 5** wires the composition + failure-down rules, the full telemetry surface, the back-compat proof, docs, status, and CHANGELOG.

Layers 1/2/4 are largely independent given the PR 1 substrate; the linear order keeps each PR small and bisectable, and lets the composition PR (5) assume all three are present.

Every PR is **TDD-first**: author the failing test (red) — the small-budget lease-denial for PR 2, the `(K+1)`th-publish 429 for PR 3, the K-distinct-votes close for PR 4 — then implement to green.

---

## Dependency Graph

```
PR 1 (interaction_id on the wire: REST metadata bag + proto field; orchestrator threads it into the lease + tracker; INERT)
  ↓
PR 2 (Layer 1: AcquireLease interaction_budget_tokens + INTERACTION_BUDGET_EXHAUSTED; fail-closed; default 0/uncapped)
  ↓
PR 3 (Layer 2: interactionReplyBudget tracker; pre-persistence 429 + ErrParticipantBudgetExhausted; human-exempt; default 0)
  ↓
PR 4 (Layer 4: END_INTERACTION_VOTE action + per-interaction vote accumulator; K/W close; per-(participant,interaction) dedupe)
  ↓
PR 5 (composition + failure-down rules; telemetry; back-compat proof; docs; status; CHANGELOG)
```

---

## Composition with Tier B and the summary surface

The three layers slot into the single publish-admission order alongside the [Tier B salience bid](0030-amendment-relevance-gated-response-tierb-pr-plan.md) ([§B](0030-multi-agent-conversation-governance.md#b-layered-architecture); [amendment §Composition](0030-amendment-relevance-gated-response.md#composition-with-the-existing-layers)):

```
Layer 0:   depth >= cap?                          ──yes──► drop; END
Layer 1:   lease available? (cost ceiling)         ──no──► drop; END         ← THIS PLAN
Layer 2:   participant under reply budget?          ──no──► drop; END         ← THIS PLAN
Layer 3a:  eligible? (Tier A, shipped v0.3.7)       ──no──► drop; END
Layer 3b:  salient? (Tier B bid)                    ──no──► stay silent; END   ← Tier B plan
Layer 2.5: floor control orders the passers (shipped)
Layer 3c:  the quality turn  ──► reply
Layer 4:   end-of-interaction votes  ──► close      ──────────────────────────► THIS PLAN
```

The join points this plan must keep green: **(a)** the Tier B bid is itself an LLM call, so Layer 1's cost ceiling governs it — a bid lease denied by `INTERACTION_BUDGET_EXHAUSTED` fails closed → no bid → silence; **(b)** when Layer 4 closes the interaction, the [summary surface](0020-interaction-summary-surface-pr-plan.md) must hand back a readable result (every close `trigger` — votes/cost/idle — carries a summary). PR 5's composition tests and the combined convergence MT (master-plan Phase 3) exercise both.

---

## PR Sequence

### PR 1: `feature/v038-rfc0030-layers-interaction-id-wire` — `interaction_id` propagation (substrate, inert)

**Depends on**: RFC 0020 Interaction (shipped); RFC 0011 channels (shipped).
**Status**: ✅ Merged ([#576](https://github.com/mkhomutov/Persatrix/pull/576)).
**Purpose**: Pin the RFC 0020 `interaction_id` on the wire so Layers 1/2/4 can attribute spend, count replies, and accumulate votes per interaction. Mirror the `cascade_depth` / `sender_participant_type` amendment pattern (a publish-metadata bag value lifted onto a typed proto field, then onto the agent's event metadata). No layer behaviour yet.

> **Field-number correction (2026-06-08).** This plan named `interaction_id = 13`, but the Tier B salience PRs ([#572](https://github.com/mkhomutov/Persatrix/pull/572)–[#575](https://github.com/mkhomutov/Persatrix/pull/575)) landed fields **13–16** (`salience_gated`/`threshold`/`channel_size`/`salience_max_channel_members`) on `ChannelMessageEvent` after this doc was authored. **The shipped field is `string interaction_id = 17`** — the next free tag. The string-field-pin tests (Go + Python) needed a multi-byte-varint tag helper since field 17's tag (`(17<<3)|2 = 138`) exceeds one byte.
>
> **Scope note.** "Resolve the open Interaction at publish" is implemented as the cascade_depth-style metadata pass-through: a publisher supplies `interaction_id` in the metadata bag and the orchestrator carries it to the proto field. There is no orchestrator-side Interaction tracker today (RFC 0020 tracking is agent-side), so building one is out of scope for this inert substrate PR — the layer PRs that consume the id own any richer resolution.

| File | Change |
|------|--------|
| `schemas/channel.schema.json` | Extend `messageMetadata` (already carries `cascade_depth`) with an optional `interaction_id` (opaque string; RFC 0020 §D calls it a ULID but the agent mints a uuid4, so don't assume ULID sortability). Back-compat: absent is allowed. |
| [`proto/task.proto`](../../proto/task.proto) (channel event) | Add `string interaction_id = 17` to `ChannelMessageEvent`, typed scalar — same rationale as cascade_depth ([§M](0030-multi-agent-conversation-governance.md#m-wire-and-config-surfaces)). Regenerate Go + Python stubs (`_pb2.py`/`_pb2_grpc.py` with the Makefile relative-import rewrite, plus the `_pb2.pyi` mypy stub). |
| [`internal/channels/interaction_id.go`](../../internal/channels/interaction_id.go) (new) + `grpc_dispatcher.go` | `interactionIDMetadataKey` const + `readInteractionID(metadata)` helper (mirrors `participant_type.go`); wired into `channelMessageToProto` so the publish-metadata value lands on the typed field. Tolerant: absent/non-string → empty (untracked). |
| [`agents/channel_wire_metadata.py`](../../agents/channel_wire_metadata.py) (new) + `server_servicers.py` | Lift `request.interaction_id` off the typed proto field onto `event.metadata["interaction_id"]` (only when non-empty), alongside the existing `sender_participant_type` lift. Carved into a sibling helper so `server_servicers.py` stays under the 500-line cap. |
| tests | **(TDD — write first.)** A dispatch stamps the metadata `interaction_id` on the proto field (`interaction_id_test.go`); absent/non-string → empty; the servicer lifts a non-empty wire field onto event metadata and leaves an empty one absent; the proto round-trips field 17 (Go + Python pin tests). |

**Acceptance**: `make proto` regenerates cleanly; `interaction_id` rides the event + metadata; existing publishes without it are unaffected; **no layer behaviour change**; strict file-size + proto-freshness/pyi-parity gates green.

---

### PR 2: `feature/v038-rfc0030-layers-cost-ceiling` — Layer 1 per-interaction cost ceiling

**Depends on**: PR 1; RFC 0023 leasing (shipped v0.3.2).
**Status**: ✅ Merged ([#577](https://github.com/mkhomutov/Persatrix/pull/577)).
**Purpose**: A channel with `interaction_budget_tokens=N` denies further leases in the same interaction once the running total crosses N — bounding cost, fail-closed.

> **Field-number note (2026-06-08).** `LeaseRequest` had `trace_id = 7` as its max, so the new fields landed as **`string interaction_id = 8`** and **`int64 interaction_budget_tokens = 9`**. `LeaseDenied` had no denial-reason enum, so a typed **`LeaseDeniedReason reason = 6`** was added (`UNSPECIFIED = 0` for back-compat, `BUDGET = 1` for the existing RFC 0023 per-scope denial, `INTERACTION_BUDGET_EXHAUSTED = 2` for this layer). The wallet's per-interaction running total is a self-contained `map[interaction_id]int64` on `WalletService` (guarded by the existing `mu`), **not** a fourth scope threaded into the shared `cost.TokenCounter` — it accumulates the granted estimate at acquire and reconciles to actuals on settle/release (a released bid frees its hold), keeping the cost primitives unchanged on the trusted scheduler path. Layer 1 logic is carved into `internal/wallet/interaction_budget.go` to keep `wallet.go` under the file-size cap.
>
> **Scope note — what PR 2 lands vs. defers.** PR 2 is the **enforcement substrate + opt-in surface**, mirroring PR 1's inert-substrate pattern: the wallet enforces the ceiling (fully unit-tested), the proto carries the fields + typed reason, the channel schema/config + Go loader expose `interaction_budget_tokens` (channel-level) and `default_interaction_budget_tokens` (fleet) with channel-over-fleet precedence (`ChannelConfig.ResolveInteractionBudgetTokens`), and the Python `WalletClient.lease()` forwards both fields and maps the typed denial like a workflow-budget denial (fail-closed). Two pieces are **deferred to PR 5 (composition)**, where they belong: **(a)** delivering the resolved channel budget down to the agent's `LeaseRequest` at fanout (the orchestrator→agent stamping — PR 5 wires the publish path); and **(b)** the `governance_drop{layer=cost}` **counter** — the wallet holds no metrics handle and the counter's natural emission point is the channel publish path PR 5 instruments. PR 2 makes the denial fully observable via the typed wire `reason` + a dedicated `layer=cost` structured wallet log; PR 5 adds the counter.

| File | Change |
|------|--------|
| [`proto/wallet.proto`](../../proto/wallet.proto) | Extend `LeaseRequest` (the `AcquireLease` request message — `trace_id = 7` is the current max, so 8/9 are free) with `string interaction_id = 8` and `int64 interaction_budget_tokens = 9` ([§E](0030-multi-agent-conversation-governance.md#e-layer-1--per-conversation-cost-ceiling)). For the denial: `LeaseDenied` today carries only a free-text `string message` and a `string scope` — there is **no** denial-reason enum — so introduce a typed `LeaseDeniedReason` enum (or a typed `reason` field) and surface `INTERACTION_BUDGET_EXHAUSTED` through it, so the consumer can machine-distinguish a budget denial from a wallet-limit denial. Regenerate stubs. |
| `internal/wallet/…` | `WalletService.AcquireLease` tracks a per-`interaction_id` running token total alongside the existing per-workflow / per-agent totals; when the total would cross `interaction_budget_tokens` (and it is non-zero), return `LeaseDenied{reason=INTERACTION_BUDGET_EXHAUSTED}` — **fail-closed** (the call does not happen; GL5). Default `0` (uncapped) → never denies. |
| `agents/…` (lease consumer) | Treat `INTERACTION_BUDGET_EXHAUSTED` exactly like a workflow-budget denial (RFC 0023 §F): no LLM call, surface a `governance.cost_ceiling` event, terminate the channel publish chain for that interaction. |
| schema/config | Add `interaction_budget_tokens` (channel-level + `default_interaction_budget_tokens`, default `0`) to `schemas/channel.schema.json` + `config/channels.yaml` + the Go loader ([§M](0030-multi-agent-conversation-governance.md#m-wire-and-config-surfaces)). |
| tests | **(TDD — write first.)** Declare a small `interaction_budget_tokens`; assert a later lease in the same interaction is denied with `INTERACTION_BUDGET_EXHAUSTED` and the LLM call does not fire; `governance_drop{layer=cost}` increments; a wallet RPC timeout fails closed (GL5); default `0` never denies. |

**Acceptance**: a small budget denies later leases in the interaction (fail-closed, no LLM call) and increments `governance_drop{layer=cost}`; default `0` keeps existing channels unchanged; the depth cap (Layer 0) remains the net if the wallet is unreachable.

---

### PR 3: `feature/v038-rfc0030-layers-reply-budget` — Layer 2 per-participant reply budget

**Depends on**: PR 1.
**Status**: ✅ Merged ([#579](https://github.com/mkhomutov/Persatrix/pull/579)).
**Purpose**: With `max_replies_per_participant_per_interaction=K`, a participant's `(K+1)`th publish in one interaction is rejected pre-persistence — fair, finite turn-taking with no LLM judge.

> **Implementation note (2026-06-08).** Landed in the orchestrator router (the publish-admission path), not a free-standing `interactionReplyBudget` struct: the per-channel cap (`replyBudgets`) and the per-interaction counters (`replyCounts map[interaction_id]map[participant_id]int`) live on `ChannelRouter` under `replyBudgetMu`, with the methods carved into the new [`internal/channels/reply_budget.go`](../../internal/channels/reply_budget.go) to keep `router.go` under the file-size cap (mirroring `router_salience.go`). `enforceReplyBudget` runs in `ChannelRouter.Publish` immediately before `store.PublishMessage`, so a denied (K+1)th publish never persists. The gate **reserves** a reply slot atomically under the mutex and the publish path **releases** it if `store.PublishMessage` then fails — so a store-rejected publish (oversized content, non-member, …) does not consume the sender's allowance; the §F counter tracks messages that entered history, not attempts. A publish with **no `interaction_id`** is never gated (nothing to scope the counter to — the untracked/pre-v0.3.8 case stays uncapped), matching PR 2's empty-interaction posture. **Producer caveat:** no orchestrator- or agent-side producer writes `interaction_id` onto publish metadata yet (`readInteractionID` returns `""` on real traffic — see `interaction_id.go`), so like PR 1 the gate is *inert in production* and only fires for callers that supply the id explicitly; the layer is wired and tested ahead of the producer, not yet load-bearing. Runtime-created channels inherit the fleet default via `ChannelRouter.ApplyDefaultReplyBudget` in the create handler (a distinct method because reply-budget zero is uncapped-as-a-value, so the salience-style `Set(_, 0)` sentinel cannot carry the default). `governance.exempt_principals: [human]` resolves to the `user` participant type (`exemptPrincipalParticipantType`); the resolved set is fleet-wide. The `governance_drop{layer=reply_budget}` counter ships **in this PR** (the router holds the metrics handle, unlike PR 2's wallet) as the new `channel.conversation.governance_drop{channel_type, layer}` instrument — the shared instrument PR 5 reuses for `cost`/`depth`/`end_vote`. The §F close/reset seam is `ChannelRouter.DiscardInteractionReplyBudget(interaction_id)`, wired by the Layer 4 / RFC 0020 close path in PR 4.

> **Hardening constraints before this layer is load-bearing (2026-06-08, from the PR 3 deep review).** Two assumptions hold only because the gate is inert today and MUST be discharged with (or before) the `interaction_id` producer:
>
> 1. **`replyCounts` is bounded only by the close seam.** A live interaction's counter map is pruned only by `DiscardInteractionReplyBudget` (a failed-persist release prunes just its own reservation). So `replyCounts` grows one map per distinct `interaction_id` until PR 4 wires that discard into the close path. **The producer must not be enabled before PR 4**, or each distinct id (a 128-byte, attacker-influenceable token) leaks a counter map for the orchestrator's lifetime.
> 2. **The human exemption trusts a caller-asserted field.** `enforceReplyBudget` reads `participant_type` from the publish metadata bag; the raw publish path (`POST /channels/{id}/messages`) forwards it verbatim rather than re-deriving it from an authenticated identity (only the chat handler stamps/validates it). A caller that self-asserts `participant_type: "user"` would be treated as exempt. Before the gate fires on real traffic the exemption must derive the principal from a trusted source — otherwise an agent can opt out of the cap meant to keep it from dominating. See the `SECURITY` note on `exemptPrincipalParticipantType` in `reply_budget.go`.

| File | Change |
|------|--------|
| `internal/channels/…` (orchestrator) | New in-memory `interactionReplyBudget{interactionID, maxPerParticipant, counts map[string]int}` ([§F](0030-multi-agent-conversation-governance.md#f-layer-2--per-participant-reply-budget)). On every publish: resolve the Interaction (PR 1's `interaction_id`), increment `counts[SenderID]`; if it exceeds `maxPerParticipant` (and it is non-zero), drop **before persistence** with `ErrParticipantBudgetExhausted` → REST **429** + log + counter (GL3). Counters live on the Interaction and are discarded on close (reset semantics, §F). |
| config | Add `max_replies_per_participant_per_interaction` (channel-level + `default_max_replies_per_participant`, default `0`/uncapped) and `governance.exempt_principals: [human]` to `schemas/channel.schema.json` + `config/channels.yaml` + the Go loader. Human principals are exempt (GL4). |
| orchestrator startup | Emit a startup **Warn** when a channel has all-`participant` (all-`always`) membership and an uncapped reply budget — same shape as the existing unauthenticated-REST warning in [cmd/orchestrator/channels.go](../../cmd/orchestrator/channels.go). Advisory only (not a behaviour change). |
| tests | **(TDD — write first.)** With `K=2`, a participant's 3rd publish in one interaction returns 429 + `ErrParticipantBudgetExhausted` and is **not persisted** (assert channel history excludes it); a human principal is exempt; default `0` preserves v0.3.0 behaviour; counters reset on interaction close; the all-`participant`+uncapped startup Warn fires. |

**Acceptance**: the `(K+1)`th publish is rejected pre-persistence with 429 (never enters channel history); human principals exempt; default `0` preserves v0.3.0 behaviour; `governance_drop{layer=reply_budget}` increments; the advisory startup Warn fires on all-`participant`+uncapped.

---

### PR 4: `feature/v038-rfc0030-layers-end-vote` — Layer 4 end-of-interaction signal

**Depends on**: PR 1.
**Status**: ✅ Merged ([#580](https://github.com/mkhomutov/Persatrix/pull/580)).
**Purpose**: When K distinct participants vote within W consecutive turns, the interaction closes on its own — an explicit, auditable action (GL2).

> **Implementation note (2026-06-08).** Landed on the orchestrator router (the publish-admission path), mirroring the Layer 2 reply budget. The accumulator + methods are carved into the new [`internal/channels/end_vote.go`](../../internal/channels/end_vote.go) (per-channel K/W in `endVoteThresholds`/`endVoteWindows`, per-interaction state in `endVotes`, a `closedInteractions` set), all guarded by `endVoteMu` on `ChannelRouter`. `processEndVote` runs in `ChannelRouter.Publish` **post-persistence** (a vote is a real message) and returns a "suppress fanout" bool — the close stops the conversation drawing new replies rather than dropping the vote from history. **Wire shape:** rather than a proto/action wire change, a vote rides as a `end_interaction_vote: true` flag on the publish metadata bag (`readEndInteractionVote`, sibling of `readInteractionID`), scoped by `interaction_id`; this keeps the substrate identical to PR 1's metadata pattern. The Python `END_INTERACTION_VOTE` action ([`agents/persona_types.py`](../../agents/persona_types.py) + `action_executor.py`) is the recognised vocabulary entry (GL2 — distinct from RFC 0020's structural `END_INTERACTION`). **Turn/window model:** the per-interaction `turn` counter is created lazily on the *first* vote and incremented on every subsequent tracked publish; a vote counts toward the quorum only while `currentTurn - voteTurn < W` (recency), and re-voting overwrites the participant's turn stamp so it dedupes to one vote. Counting from the first vote (not the interaction's true start) is exact because only turn *differences* matter, and it bounds state growth to interactions that actually vote. **Telemetry:** the close emits the new `channel.conversation.interaction_closed{channel_type, trigger=end_votes}` instrument (the shared instrument PR 5 reuses for `idle`/`structural`/`cost`); vote-spam (a re-vote) logs at Warn (`layer=end_vote`) so an adversarial pattern is visible in audit. **Close seam:** a vote-triggered close calls `DiscardInteractionReplyBudget` (wiring the §F reset seam reply_budget.go reserved for Layer 4), and `DiscardInteractionEndVotes` is the sibling RFC 0020 close/reset seam. **Producer caveat (inert):** no producer writes `interaction_id` or the vote flag on real traffic yet, and the Python action returns `not_implemented`, so the layer is wired + tested ahead of the producer, not yet load-bearing — same posture as PR 1/2/3. The `closedInteractions` marker (like `replyCounts`) is pruned only by `DiscardInteractionEndVotes`, so the producer MUST wire that into the close path before it is enabled (ORDERING note in end_vote.go). **File-size hygiene:** to stay under the 500-line review cap, `RouterMetrics` moved to [`router_metrics.go`](../../internal/channels/router_metrics.go) and `Config.Validate` to [`config_validate.go`](../../internal/channels/config_validate.go) (pure moves, no behaviour change). **Deferred to PR 5 (composition), per scope:** the RFC 0028 `DecisionRecord` proto-field reservation — `END_INTERACTION_VOTE` carries no proto wire today (it is a publish-metadata flag), so there is no natural field to reserve in this PR; it lands with the composition/telemetry surface where the other governance audit records consolidate (GL7, no behaviour change either way).

| File | Change |
|------|--------|
| agent action vocabulary | Add `END_INTERACTION_VOTE` to the agent action set ([§H](0030-multi-agent-conversation-governance.md#h-layer-4--end-of-interaction-signal) Option A), distinct from RFC 0020's structural `END_INTERACTION`. An agent emits it when it judges its contribution complete. |
| `internal/channels/…` (orchestrator) | Per-interaction `end_vote_set` accumulator keyed by `interaction_id`. On each vote, dedupe per-`(participant_id, interaction_id)` (double-voting counts once); when K **distinct** participants have voted within W **consecutive** turns (defaults K=2, W=3), trigger RFC 0020's structural close and stop fanning out new replies. Vote-spam is logged (a participant voting every turn collapses to one vote; the rate is observable). |
| config | Add per-channel `end_vote_threshold` (K) and `end_vote_window` (W) to `schemas/channel.schema.json` + `config/channels.yaml` + the Go loader (defaults K=2, W=3). |
| RFC 0028 forward-compat | Reserve the proto fields where the RFC 0028 `DecisionRecord` will later consolidate the vote/decision audit — no behaviour change when it lands (GL7). |
| tests | **(TDD — write first.)** K distinct votes within W turns closes the interaction (`interaction_closed{trigger=end_votes}`); a single participant voting twice counts once (no premature close); votes outside the W window do not accumulate; vote-spam is logged; end-votes do **not** reset cascade_depth (orthogonal, §H). |

**Acceptance**: K-distinct-votes-within-W closes the interaction with `interaction_closed{trigger=end_votes}`; double-voting dedupes; the depth cap (Layer 0) and cost ceiling (Layer 1) remain the safety nets if no votes arrive; vote-spam is logged and observable.

---

### PR 5: `feature/v038-rfc0030-layers-compose-closeout` — Composition + telemetry + docs + status

**Depends on**: PR 2, PR 3, PR 4.
**Status**: 🔀 PR open.
**Purpose**: Wire the composition + failure-down rules across all layers, complete the telemetry surface, prove opt-in back-compat, and land the operator-facing surface + status.

> **Implementation note (2026-06-08).** The composition rule is documented as the single source of truth in the new [`internal/channels/governance.go`](../../internal/channels/governance.go) (the §B order + the two-phase reality of the channel publish path: Layer 2 rejects **pre-persistence**, Layers 4/0/2.5 suppress fanout **post-persistence**, Layer 1 fails closed **upstream** in the wallet) rather than by reordering the already-correct publish path — the short-circuit/failure-down behaviour is pinned by [`governance_composition_test.go`](../../internal/channels/governance_composition_test.go) instead of refactored into a single gate (lower risk, same contract). **Telemetry completed (channel-owned surface):** the cascade cap (Layer 0) and the duplicate-vote suppression (Layer 4) now attribute themselves on the shared `governance_drop{layer=depth|end_vote}` counter alongside the existing `reply_budget`; a new `end_vote_emitted{channel_type}` counter measures vote volume; a new `reply_budget_remaining{channel_type}` histogram records each tracked participant's leftover allowance **at interaction close** (a Layer 4 → Layer 2 composition seam — recorded just before `DiscardInteractionReplyBudget` prunes the counters); and every drop stamps the `conversation.governance.layer` attribute on the inbound publish span (`annotateGovernanceDropSpan`, a no-op on a span-less/unsampled publish). **Back-compat (GL1)** is proven by `TestGovernance_BackCompat_DefaultsAreInert`: with every layer at its default, a multi-persona publish fans out + persists identically and no governance telemetry fires. **Deferred to a focused follow-up (scope-confirmed):** the `governance_drop{layer=cost}` counter + the orchestrator→agent budget-stamping at fanout (`cost_tokens_per_interaction` histogram). Both need a proto/Python wire change + a wallet metrics handle, and Layer 1 is inert until the `interaction_id` producer lands — so they ride with that producer rather than this composition closeout; the `cost` label string is reserved in `governance.go` so the future wiring uses the same vocabulary. The RFC 0028 `DecisionRecord` proto reservation (GL7) likewise waits for a wire field to reserve (`END_INTERACTION_VOTE` is a metadata flag today).

| File | Change |
|------|--------|
| `internal/channels/…` (publish path) | Enforce the evaluation order (GL6): a publish proceeds only if every **active** layer admits it; a lower-layer drop short-circuits the higher layers; higher layers fail safely down to lower ones. The order is the single source of truth ([§B](0030-multi-agent-conversation-governance.md#b-layered-architecture)). |
| telemetry ([§L](0030-multi-agent-conversation-governance.md#l-telemetry-and-observability)) | Emit `channel.conversation.governance_drop{layer ∈ depth,cost,reply_budget,end_vote}`, `interaction_closed{trigger ∈ idle,structural,end_votes,cost}`, `end_vote_emitted`, and the `reply_budget_remaining` / `cost_tokens_per_interaction` histograms. Structured logs carry `channel_id`, `interaction_id`, `participant_id`, and the layer reason; the publish span gets the `conversation.governance.layer` attribute for trace correlation. |
| back-compat proof | **(TDD — write first.)** With every layer at its default (`0`/uncapped/no votes emitted), end-to-end behaviour is **identical to v0.3.7** — a golden multi-persona channel run produces the same fanout/persistence as before the layers landed (GL1). |
| composition tests | **(TDD — write first.)** A lower-layer drop short-circuits higher layers (e.g. cost-exhausted → Tier B bid never fires); the `governance_drop{layer}` counter attributes the right layer; a Layer 1 wallet failure fails down to the depth cap. |
| [`docs/guides/channels.md`](../../docs/guides/channels.md) | New "Conversation governance" subsection: `interaction_budget_tokens`, `max_replies_per_participant_per_interaction`, `END_INTERACTION_VOTE` + `end_vote_threshold`/`end_vote_window`, `governance.exempt_principals`, and the composition/failure-down contract. All opt-in; defaults preserve v0.3.7 behaviour. |
| [`0030-multi-agent-conversation-governance.md`](0030-multi-agent-conversation-governance.md) + ROADMAP | Status hygiene: Layers 1/2/4 marked implemented in v0.3.8; Layer 5 (moderator) remains v0.4.0, Layer 6 (types) v0.5.0+. RFC 0030 Master Index note reflects the Phase-1 deterministic layers landing; `make rfcs` regenerates INDEX. |
| CHANGELOG | `[0.3.8]` Upgrade Note: the new opt-in channel knobs + the `END_INTERACTION_VOTE` action — all additive; defaults (`0`/uncapped) preserve v0.3.7 behaviour; the all-`participant`+uncapped startup Warn is advisory. |

**Acceptance**: the composition/failure-down rules hold (a lower-layer drop short-circuits higher layers, attributed by `governance_drop{layer}`); the back-compat proof shows defaults are behaviourally identical to v0.3.7; the telemetry surface is complete; channel guide documents the knobs; RFC 0030 status flipped; CHANGELOG Upgrade Note present.

---

## Test Strategy (summary)

- **Unit (PR 1)**: `interaction_id` stamped on event + metadata; proto field 17 round-trips; back-compat for publishes without it.
- **Unit (PR 2)**: small budget denies later leases (`INTERACTION_BUDGET_EXHAUSTED`, fail-closed, no LLM call); wallet timeout fails closed; default `0` never denies; `governance_drop{layer=cost}`.
- **Unit (PR 3)**: `(K+1)`th publish → 429 + `ErrParticipantBudgetExhausted`, not persisted; human exempt; default `0` unchanged; counters reset on close; startup Warn.
- **Unit (PR 4)**: K distinct votes within W → close (`trigger=end_votes`); double-vote dedupes; out-of-window votes don't accumulate; vote-spam logged; cascade_depth untouched.
- **Composition + back-compat (PR 5)**: lower-layer drop short-circuits higher layers, attributed; defaults identical to v0.3.7 (golden run); Layer 1 failure falls down to the depth cap.
- **Manual (master-plan Phase 3)**: `MT-CONVERSATION-CONVERGENCE-001` — the combined story: no pile-on (Tier B) → bounded cost (Layer 1) → ends on votes (Layer 4) → readable summary (summary surface).
- **Regression**: every PR keeps the existing channels + wallet + RFC 0020 suites green; PR 1 is behaviourally inert.

---

## Status & ROADMAP hygiene

Per [master-plan §ROADMAP hygiene](../v0.3.8-plan.md#roadmap-hygiene):

- **PR 1 open** → no RFC status change (wire substrate only; companion PR plans excluded from `INDEX.md`).
- **PR 2/3/4 merge (each layer lands)** → RFC 0030 Master Index note reflects the corresponding Phase-1 layer moving from planned to implementing; `make rfcs` regenerates INDEX; `Last updated` refresh on each flip.
- **PR 5 merges** → CHANGELOG `[0.3.8]` Upgrade Note seeded; RFC 0030 §Phased Implementation Plan records Phase 1 (Layers 1/2/4) landed; Layer 5 remains v0.4.0.
- **v0.3.8 tag** → `MT-CONVERSATION-CONVERGENCE-001` re-run live on HEAD as a release gate (master-plan Phase 3): cost ceiling denies once crossed; the interaction closes on K votes; the back-compat proof holds on the tag tip.

---

## Related documentation

- [RFC 0030 — Multi-Agent Conversation Governance](0030-multi-agent-conversation-governance.md) — §E/§F/§H are the Layer 1/2/4 contracts; §B the composition rule; §L telemetry; §M wire/config; §N failure modes; §Phase 1 the deliverable list this plan executes.
- [Tier B PR plan](0030-amendment-relevance-gated-response-tierb-pr-plan.md) — Layer 3b salience bid, the sibling Phase-1 workstream these layers compose with (Layer 1 governs the bid's lease).
- [Interaction-summary surface PR plan](0020-interaction-summary-surface-pr-plan.md) — turns Layer 4's "closed" into a readable result; every close `trigger` carries a summary.
- [RFC 0023 — LLM Call Leasing](0023-llm-call-leasing.md) — the cost-ceiling primitive Layer 1 extends (`AcquireLease`, `CAUSE_CHANNEL_MESSAGE`, fail-closed §F); shipped v0.3.2.
- [RFC 0020 — Interaction Lifecycle](0020-interaction-lifecycle.md) — the conversation scope Layers 2/4 key on; the structural close Layer 4 triggers.
- [RFC 0028 — Agent Decision Policy Engine](0028-agent-decision-policy-engine.md) — the future `DecisionRecord` home; this plan reserves fields but is not gated on it (§K).
- [v0.3.8 plan](../v0.3.8-plan.md) — the release this lands in; Workstream 1b.
