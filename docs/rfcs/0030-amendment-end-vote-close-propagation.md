# RFC 0030 Amendment — End-Vote Close Propagation (the Close Must Reach the Room)

**Type**: amendment to [RFC 0030 §H](0030-multi-agent-conversation-governance.md#h-layer-4--end-of-interaction-signal) (Layer 4 — end-of-interaction signal), closing the gap between the orchestrator's close and the agents' knowledge of it
**Status**: 📋 Proposed (this doc is PR 1 of 3; acceptance tests land here skip-guarded — orchestrator dispatch is PR 2, agent-side consumption is PR 3)
**Author**: Maksim Khomutov
**Date**: 2026-06-12
**Target**: v0.3.8
**Trigger**: [MT-CHANNEL-GOV-004](../manual-tests/MT-CHANNEL-GOV-004.md)'s 2026-06-12 execution. The chair-stall-escalation arc passed live end-to-end on the orchestrator side (stall → forced turn → synthesis-in-vote → concurrence → `trigger=end_votes` close, nine seconds after escalation) — and then **every member's interaction summary rendered the discussion "went idle"**, the exact label the whole workstream exists to retire. The cause is structural, not behavioural: the close-quorum vote's fanout is suppressed ([`router_publish_async.go`](../../internal/channels/router_publish_async.go), the `processEndVote` early return), so **no member's agent-local tracker ever hears about an `end_votes` close** — including the chair whose synthesis caused it. The only propagation path is the *successor* publish's OQ 5 close-cause stamp ([#607](https://github.com/mkhomutov/Persatrix/pull/607)), which exists solely on channels that get fresh traffic within each agent's local idle window (600 s). A room that converges and then goes quiet — the *success case* — buries its own decision.
**Supersedes**: nothing — strictly additive. The OQ 5 successor-publish stamp stays as the propagation path for lazy idle rotations (CP4).

---

## Context — what the gap looks like

1. K distinct members vote within W turns; the orchestrator closes the interaction (`interaction_closed{trigger=end_votes}`), persists the closing vote, and **suppresses its fanout** — correctly, so the conversation stops drawing replies (§H).
2. Suppression starves every member of the closing event: the voter's concurrence never reaches the chair or the room. Each agent's local tracker still holds the interaction **open**.
3. With no follow-up traffic, each tracker closes it by its own 600 s idle timer — structural label "went idle", summary delayed by up to the window, close cause lost. The chair never learns its synthesis closed the discussion; the recorded outcome reads as a trail-off.

MT-CHANNEL-GOV-004 step 3 ("the close trigger renders as **ended**") is therefore unsatisfiable today on the arc it tests: the expectation is right, the wire contract under it is missing. The deterministic layers cannot observe the problem — it lives entirely in the orchestrator→agent seam after the close decision is already made.

## A. Decisions

- **CP1 — an `end_votes` close is propagated to the room, not inferred by it.** The contract: every non-sender member of the channel receives notice of the close — carrying the closing message (the synthesis/concurrence is real history) and the truthful trigger — promptly at close time, not at the mercy of future traffic. The closing *sender* needs no notice: its own vote action already closes its local tracker ([`test_end_interaction_vote_action.py`](../../tests/unit/python/test_end_interaction_vote_action.py)).
- **CP2 — the mechanism is a marked ingestion-grade dispatch, NOT un-suppressed fanout.** Fanout suppression exists to stop post-close turns, bids, and LLM spend; that posture stands. Instead, the close site re-dispatches the closing message to every non-sender member with a first-class `interaction_close_notification` boolean on `ChannelMessageEvent` (the `cascade_depth` / `chair_escalation` precedent: a typed field, no metadata map). Orchestrator-authored, same trust class as `floor_mentions_resolved`. Fresh event ids per recipient (the conversation-window dedup rule, CE3's lesson).
- **CP3 — receiver-side, the marked event is control, never stimulus.** It appends the closing message to the conversation window (history), closes the agent-local tracker with trigger `end_votes` (immediate close, truthful "ended" render, summary generated now instead of an idle window later), and is **hard-suppressed from every response path** — no gate evaluation as a turn, no Tier B bid, no LLM call. Honoured only on the typed field (defence-in-depth: a metadata-borne impostor key is ignored — the `floor_mentions_resolved` posture).
- **CP4 — scope is the `end_votes` close alone.** Lazy idle rotation has no event to send at close time (the close *is* the successor publish) and already propagates truthfully via the OQ 5 stamp — unchanged. The Layer 1 cost-ceiling close, when its close path lands, should reuse this seam rather than grow a second one. Thread channels ride the same seam (a thread is its interaction).
- **CP5 — fail-open, fire-and-forget, off the publish path.** Notification dispatch runs after the close decision and is never awaited (the CE7 posture); a failed or dropped notification degrades to exactly today's behaviour — the member's tracker idles out with the legacy label. Every dispatch outcome is observable: `channel.conversation.close_notification{outcome=dispatched|dispatch_error}` per recipient. No retry machinery in this slice — the degraded state is the status quo, not corruption.

## B. What does NOT change

- **The close decision.** Layer 4's quorum, the W-window mechanics, the vote vocabulary: untouched. This amendment begins where the close has already happened.
- **Fanout suppression.** The closing message still draws no turns and no bids from current agents; the room still stops. CP2's dispatch is delivery of a fact, not an invitation to speak.
- **The OQ 5 successor stamp.** Still the propagation path for idle rotations and the safety net under CP5's degraded branch.
- **No DB change, no new principal, no new capability.**

## C. Mechanism (implementation sketch — PRs 2 and 3)

1. **Go (PR 2).** At the `processEndVote` close site (before the suppression return), fan the closing message to every non-sender member through the standard per-recipient dispatch seam (`dispatchTo`: the 5 s deadline, the delivered counter) with `interaction_close_notification: true` and a fresh event id per recipient; never awaited (CP5). Proto: one additive boolean field on `ChannelMessageEvent` (regen with the CI-pinned toolchain). Metric as CP5. Unskips [`TestEndVoteClose_NotifiesEveryMemberOfTheClose`](../../internal/channels/interaction_close_notification_test.go).
2. **Python (PR 3).** Wire lift of the marker (`channel_wire_metadata`), the ingestion-only routing (window append + tracker close with `end_votes` + response-path hard-suppression), the "ended" summary render, drift pins for the marker literal. Unskips [`test_interaction_close_notification.py`](../../tests/unit/python/test_interaction_close_notification.py).
3. **Acceptance.** Re-run MT-CHANNEL-GOV-004 step 3 after PR 3: the escalated interaction's summary must render "ended" with the synthesis, with **no** follow-up traffic required.

## D. Mixed-version analysis

- **New orchestrator + old agent**: the marker field is unknown, so the old agent sees an ordinary single-recipient delivery of the closing vote — it runs one Tier B bid (almost certainly passing: the message is a sign-off) and, if it does reply, the reply lands on a closed interaction and hits the existing post-close drop on the publish path. Cost: at most one bid per member per close. Degraded but bounded and safe; no breakage. The wire field is additive (proto3 default `false` is byte-identical for old producers).
- **Old orchestrator + new agent**: the marker never arrives; the tracker idles out with the legacy label — exactly v0.3.8 behaviour. No breakage.

## E. Acceptance tests land in PR 1, red and skip-guarded (TDD)

Both tests below are written against the *contract* in this document and committed with this PR, `skip`-guarded so `main` stays green. The implementation PRs do not write their own acceptance — they **unskip**:

- [`internal/channels/interaction_close_notification_test.go`](../../internal/channels/interaction_close_notification_test.go) — after a quorum close, every non-sender member received exactly one dispatch carrying the closing message; the suppression of *ordinary* fanout is asserted unchanged. PR 2 removes the skip (and extends the assertion to the typed marker once the proto field exists).
- [`tests/unit/python/test_interaction_close_notification.py`](../../tests/unit/python/test_interaction_close_notification.py) — a marked event closes the local tracker with trigger `end_votes`, renders "ended", and produces no turn, no bid, no LLM call. PR 3 removes the skip.

## Open questions

1. **Should the notification also carry the closing quorum's vote count?** Deferred — the tracker needs only the trigger; richer close records belong to RFC 0028's `DecisionRecord` consolidation (v0.4.0).
2. **Observer members.** They receive ordinary fanout for ingestion today, so they receive the notification too (CP1 says "every non-sender member"); their gates already suppress all response paths. No special casing.

## Related documentation

- [Chair-stall-escalation amendment](0030-amendment-chair-stall-escalation.md) — the arc whose recorded outcome this amendment rescues; CE3/CE7 are the dispatch and fail-open precedents
- [Interaction-id producer plan](0030-interaction-id-producer-pr-plan.md) — OQ 5, the successor-stamp path that stays for idle rotations
- [MT-CHANNEL-GOV-004](../manual-tests/MT-CHANNEL-GOV-004.md) — the live execution that exposed the gap (2026-06-12 Test Results row)
- [ISSUE-0095](../issues/ISSUE-0095-idle-rotation-no-fire-observability.md) / [ISSUE-0096](../issues/ISSUE-0096-display-name-mentions-resolve-to-nobody.md) / [ISSUE-0097](../issues/ISSUE-0097-persona-vote-and-bid-calibration.md) — the same execution's sibling findings, fixed in their own PRs
