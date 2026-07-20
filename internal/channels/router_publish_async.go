package channels

import (
	"context"
	"fmt"
	"runtime/debug"

	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/metric"
	"go.uber.org/zap"
)

// Non-blocking publish surface (RFC 0048 console publish-latency fix). Split
// from router.go so that file stays under the 500-line review cap; the
// synchronous [ChannelRouter.Publish] stays there as the topology entry point.

// defaultMaxInFlightFanout caps the number of detached fanout goroutines
// [ChannelRouter.PublishAsync] keeps running at once. The async seam removed the
// backpressure the blocking POST used to apply (a client could not enqueue a new
// round until the prior one finished), so without a ceiling a looping caller or
// a runaway agent could spawn fanout goroutines — each potentially parked on a
// per-channel floor lock for a full round — without bound. At the ceiling
// PublishAsync runs fanout INLINE: the message still commits, but the over-cap
// caller pays the round latency (the pre-async behaviour), shedding load rather
// than leaking goroutines or dropping messages. Sized well above any realistic
// concurrent-channel count so only pathological traffic reaches it; override per
// deployment with [ChannelRouter.SetMaxInFlightFanout].
const defaultMaxInFlightFanout = 512

// PublishAsync commits the message synchronously (validate + clamp + persist +
// reply-budget + end-vote + notify) and then runs the fanout on a DETACHED,
// tracked goroutine, returning as soon as the message is durably persisted.
//
// This is the entry point for the REST `POST /api/v1/channels/{id}/messages`
// handler: a human keystroke must not block on the agent round it triggers.
// With floor control on, fanout serializes per-speaker turns each waiting up
// to [DefaultFloorTurnTimeoutSeconds] for a reply, so the synchronous
// [ChannelRouter.Publish] coupled the HTTP response to a multi-turn LLM round
// (observed 90-135s POST latencies, RFC 0048 console). The fanout still runs —
// the caller just no longer waits for it; agent replies surface through the
// console's existing history poll.
//
// The error contract matches [ChannelRouter.Publish]: a synchronous error
// means the publish was REJECTED (nothing persisted, or the reply-budget
// reservation released). A nil error means the message is committed; fanout
// delivery failures are recorded via the `channel.messages.delivered` counter,
// never surfaced here (the [MessageDispatcher] contract is fire-and-forget).
//
// The fanout context is detached from the request via [context.WithoutCancel]
// so a client disconnect after the 201 cannot abort an in-flight round (trace
// values survive; only cancellation is dropped). Spawned goroutines are
// tracked by [ChannelRouter.fanoutWG]; drain them with
// [ChannelRouter.WaitForPendingFanout] on shutdown or in tests.
//
// One exception: at the in-flight ceiling (see [defaultMaxInFlightFanout]) the
// fanout runs INLINE on the caller's goroutine and is therefore NOT tracked by
// [ChannelRouter.fanoutWG] — the drain cannot wait for it. That is correct (an
// inline round completes before this call returns), but it means a graceful
// shutdown relies on the HTTP server's own in-flight-request drain, not the
// fanout drain, to bound that round. Only reachable under pathological traffic.
func (r *ChannelRouter) PublishAsync(ctx context.Context, msg ChannelMessage, declaredType string) error {
	plan, err := r.publishCommit(ctx, msg, declaredType)
	if err != nil || plan == nil {
		return err
	}
	detached := context.WithoutCancel(ctx)

	// Backpressure valve: at the in-flight ceiling, run fanout inline rather
	// than spawning an unbounded goroutine (see [defaultMaxInFlightFanout]). The
	// publish already committed; only this caller pays the round latency. Still
	// recovered — the inline fanout's per-recipient dispatch workers run on
	// their own goroutines, outside any HTTP-handler recover umbrella.
	if r.maxInFlightFanout > 0 && r.fanoutInFlight.Load() >= int64(r.maxInFlightFanout) {
		defer r.recoverFanout("publish_async_inline", plan.msg.ChannelID, plan.msg.ID)
		r.fanout(detached, plan.msg, plan.derivedType, plan.threadParentSenderID)
		return nil
	}

	r.fanoutInFlight.Add(1)
	r.fanoutWG.Add(1)
	go func() {
		// LIFO defers: recover first (so a panic never escapes this goroutine
		// and crashes the process — the synchronous Publish ran under the
		// server's recoveryMiddleware, the detached goroutine has no such
		// umbrella), then release the in-flight slot, then the drain WaitGroup.
		defer r.fanoutWG.Done()
		defer r.fanoutInFlight.Add(-1)
		defer r.recoverFanout("publish_async", plan.msg.ChannelID, plan.msg.ID)
		r.fanout(detached, plan.msg, plan.derivedType, plan.threadParentSenderID)
	}()
	return nil
}

// recoverFanout turns a panic on a detached fanout / dispatch goroutine into a
// logged error instead of a process-fatal crash. The synchronous publish path
// runs under the server's recoveryMiddleware, but [ChannelRouter.PublishAsync]'s
// detached goroutine — and the per-recipient dispatch workers, which run off the
// request goroutine on BOTH publish paths — have no such umbrella, and an
// unrecovered panic in any goroutine terminates the whole orchestrator. Deferred
// at each such goroutine's top frame; `where`/`channelID`/`messageID` localise
// the failure. A no-op when there is no panic.
func (r *ChannelRouter) recoverFanout(where, channelID, messageID string) {
	if rec := recover(); rec != nil {
		r.logger.Error("channels: recovered panic on fanout goroutine (process preserved)",
			zap.String("where", where),
			zap.String("channel_id", channelID),
			zap.String("message_id", messageID),
			zap.Any("panic", rec),
			zap.String("stack", string(debug.Stack())),
		)
	}
}

// SetMaxInFlightFanout overrides the detached-fanout ceiling (see
// [defaultMaxInFlightFanout]); a non-positive value disables the cap (unbounded
// — the pre-cap behaviour). Intended for deployment tuning and tests.
func (r *ChannelRouter) SetMaxInFlightFanout(n int) {
	if n < 0 {
		n = 0
	}
	r.maxInFlightFanout = n
}

// inFlightFanout reports the number of detached fanout goroutines currently
// running. Unexported — for in-package tests asserting the cap holds.
func (r *ChannelRouter) inFlightFanout() int64 {
	return r.fanoutInFlight.Load()
}

// DrainPendingFanout blocks until every detached fanout goroutine has completed
// OR `ctx` is done, returning true if it fully drained and false if the context
// expired first. This is the BOUNDED drain a graceful shutdown uses: a fanout
// wedged on a silent agent (under floor control, up to M×turnTimeout) must not
// hang process exit past the shutdown budget. The unbounded
// [ChannelRouter.WaitForPendingFanout] remains for tests that need a hard
// barrier. When `ctx` expires, the internal waiter goroutine outlives this call
// until the fanout eventually finishes — benign at shutdown, where the process
// exits immediately after.
//
// PR #718 review finding 1, ordering settled by the second follow-up review's
// DRAINING GATE. Waiting fanoutWG before the sweep (the first revision) closed
// the sweep-outrun race but opened the symmetric one: a synthesis timer firing
// during that first Wait ran fanoutWG.Add(1) (onSynthesisTimeout → boundedClose
// → notifyInteractionClose) from a goroutine holding NO fanoutWG count — an
// Add-from-zero concurrent with an in-progress Wait, the documented
// sync.WaitGroup misuse. The gate closes BOTH races without a fanoutWG wait
// before the sweep:
//
//  1. `draining` is set under interactionMu — the lock the arm CAS runs under
//     — so [ChannelRouter.maybeArmSynthesisClose] refuses every arm serialized
//     after it and degrades to the immediate close (deterministic termination
//     preserved; the arming fanout goroutine holds its own fanoutWG count, so
//     its close-notification Adds are legal).
//  2. The sweep is therefore FINAL: every synthesisWG.Add is under the same
//     lock and either preceded the sweep's critical section (its count is
//     registered before the Wait below starts) or finds its arm already swept
//     and never Adds. No Add-from-zero can race synthesisWG.Wait.
//  3. synthesisWG.Wait: swept timers released their counts at the sweep;
//     a timer caught mid-fire (Stop()==false) self-releases via its deferred
//     Done — deferred FIRST in onSynthesisTimeout, so its close work's
//     fanoutWG.Add(1)s happen-before that Done, which happens-before this
//     Wait returns.
//  4. One fanoutWG.Wait: by (3) every timer-originated Add is already held
//     when it starts, and every other Add came from a goroutine registered at
//     spawn (PublishAsync), holding a count (the notification fan), or holding
//     the arm's TRANSFERRED synthesisWG count (the commit-path reply claim —
//     [ChannelRouter.closeOnSynthesisReply] releases it only after its close's
//     Adds, so (3)'s happens-before covers that path too) — no Add-from-zero
//     can race it either.
//
// The flag clears on return (deferred), so a router reused after a bounded
// (ctx-expired) drain resumes arming — at real shutdown the process exits
// right after, and a straggler arm past an ABANDONED drain is the same
// accepted exposure as the outlived waiter goroutine above.
func (r *ChannelRouter) DrainPendingFanout(ctx context.Context) bool {
	r.interactionMu.Lock()
	r.draining = true
	r.interactionMu.Unlock()
	defer func() {
		r.interactionMu.Lock()
		r.draining = false
		r.interactionMu.Unlock()
	}()
	done := make(chan struct{})
	go func() {
		r.disarmAllPendingSyntheses()
		r.synthesisWG.Wait()
		r.fanoutWG.Wait()
		close(done)
	}()
	select {
	case <-done:
		return true
	case <-ctx.Done():
		return false
	}
}

// WaitForPendingFanout blocks until every fanout goroutine spawned by
// [ChannelRouter.PublishAsync] has completed. Intended for tests that need a
// deterministic point to assert on dispatched recipients — several call it
// MID-ARM and expect the pending synthesis close to survive
// (synthesis_close.go), so it deliberately takes no draining gate and runs no
// disarm sweep. A no-op when no async fanout is in flight.
//
// KNOWN EXPOSURE (accepted, test-facing): a synthesis timer that fires
// CONCURRENTLY with this wait runs its close-notification fanoutWG.Add(1)s on
// a goroutine holding no fanout count — the Add-from-zero WaitGroup misuse
// the drain's gate exists to close. Graceful shutdown must use
// [ChannelRouter.DrainPendingFanout]; a test that deliberately lets the
// timeout net fire polls the dispatcher instead of calling this
// (TestSynthesisClose_ReplyTimeoutFallsBackToImmediateClose).
func (r *ChannelRouter) WaitForPendingFanout() {
	r.fanoutWG.Wait()
}

// fanoutPlan is the resolved outcome of [ChannelRouter.publishCommit]: the
// (possibly cascade-clamped) message plus the channel type and pre-resolved
// thread-parent sender that fanout needs. A nil plan from publishCommit means
// the publish committed but fanout is intentionally suppressed (end-vote
// close, cascade cap, or a floor-turn reply).
type fanoutPlan struct {
	msg                  ChannelMessage
	derivedType          ChannelType
	threadParentSenderID string
}

// publishCommit runs the synchronous portion of a publish — everything up to
// (but not including) fanout — shared by [ChannelRouter.Publish] and
// [ChannelRouter.PublishAsync]. It validates the channel type, clamps cascade
// depth, persists under the reply budget, accounts the end-vote, notifies
// in-process waiters, and decides whether fanout should run.
//
// Returns (plan, nil) when fanout should run with the returned params;
// (nil, nil) when the publish committed but fanout is suppressed; (nil, err)
// when the publish was rejected (nothing persisted / budget released).
func (r *ChannelRouter) publishCommit(ctx context.Context, msg ChannelMessage, declaredType string) (*fanoutPlan, error) {
	derivedType, err := channelTypeFromID(msg.ChannelID)
	if err != nil {
		return nil, err
	}
	if declaredType != "" && ChannelType(declaredType) != derivedType {
		return nil, fmt.Errorf("%w: channel_type=%q disagrees with channel_id prefix (%s)",
			ErrInvalidChannelType, declaredType, derivedType)
	}

	// RFC 0011 amendment 'Cascade-depth wire propagation': clamp inbound
	// `cascade_depth` to [0, maxCascadeDepth] BEFORE the store commit
	// so `GET /messages` returns what was enforced, not the publisher's
	// claim. Defends against over-cap poisoning (NOT reset-to-0, which
	// needs parent-message lookup — see the amendment's Future work).
	inboundDepth := readCascadeDepth(msg.Metadata)
	clampedDepth := clampCascadeDepth(inboundDepth, r.maxCascadeDepth)
	if clampedDepth != inboundDepth || (msg.Metadata != nil && msg.Metadata[cascadeDepthMetadataKey] != nil) {
		// Canonicalise the persisted shape to int (REST decode yields
		// float64 for every numeric).
		if msg.Metadata == nil {
			msg.Metadata = map[string]any{}
		}
		msg.Metadata[cascadeDepthMetadataKey] = clampedDepth
	}

	// RFC 0030 interaction-id producer (IP1/IP2): resolve the channel's open
	// interaction and stamp it — replacing any inbound claim — BEFORE the
	// reply-budget reservation and the end-vote hook below, both of which key
	// on the stamped value. From here on, every tracked publish belongs to a
	// router-minted interaction; the stamped id persists with the message and
	// rides the existing fanout lift to `ChannelMessageEvent.interaction_id`.
	// The settle hook reconciles the resolver to the persist outcome below —
	// the resolver's half of the reply-reservation pattern, so a rejected
	// publish neither retains a resolver entry nor advances the idle clock
	// (see [ChannelRouter.settleInteraction]).
	//
	// RFC 0052 no-reopen latch (PR 4b-i review rounds 5–6): on an AUTONOMOUS
	// channel a claim naming a deliberately CLOSED interaction is kept, not
	// overridden — such a publish is post-close traffic of a discussion a
	// bounded close (or end-vote) just terminated, and minting fresh would
	// re-fan it and REOPEN the unattended discussion (the §D runaway). The
	// latch DECISION lives inside resolveInteractionID's critical section:
	// deciding it here and resolving there raced a concurrent bounded close
	// into minting fresh for the very straggler the latch suppresses (review
	// #716, the TOCTOU fix). The SCOPE gate (OQ #2 — autonomous channels only,
	// stamped claims only) is the resolver's own, beside the ledger read it
	// gates. Ledger scope and lifetime — channel-scoped, deliberate closes
	// only, spanning generations — live in interaction_close_latch.go.
	inboundClaim := readInteractionID(msg.Metadata)
	resolvedInteractionID, prevClose, settleInteraction, latched := r.resolveInteractionID(ctx, msg.ChannelID, derivedType, inboundClaim)
	if latched {
		r.logger.Debug("channels: post-close claim latched on autonomous channel; publish rides the closed interaction",
			zap.String("channel_id", msg.ChannelID),
			zap.String("claimed", inboundClaim))
	}
	if msg.Metadata == nil {
		msg.Metadata = map[string]any{}
	}
	msg.Metadata[interactionIDMetadataKey] = resolvedInteractionID
	// OQ 5 close-cause attribution: stamp the retired predecessor's id +
	// trigger ("idle"/"end_votes") so the agent-side rotation close can pick
	// the truthful close reason. Inbound claims are deleted first — like the
	// interaction id itself, the cause is resolver-authoritative and a
	// publisher-supplied value must never drive receiver close labels — and
	// the delete runs unconditionally (review #716 hoisted it out of the
	// latch's branch arms, where the pair had been duplicated verbatim). No
	// retiree (fresh channel, post-restart re-mint, or a latched publish —
	// the closed record's tail, whose prevClose is zero) stamps nothing:
	// absent is the documented "unknown" the receiver keeps its legacy label
	// for.
	delete(msg.Metadata, previousInteractionIDMetadataKey)
	delete(msg.Metadata, previousInteractionTriggerMetadataKey)
	if prevClose.id != "" {
		msg.Metadata[previousInteractionIDMetadataKey] = prevClose.id
		msg.Metadata[previousInteractionTriggerMetadataKey] = prevClose.trigger
	}

	// RFC 0030 Layer 2 (v0.3.8) per-participant reply budget + store commit:
	// reserve the sender's slot, persist, and release the reservation if the
	// persist fails — so a throttled (K+1)th publish never enters channel
	// history (§F) and a store-rejected publish never erodes the allowance.
	// Additive: a no-op when uncapped, untracked, or an exempt human.
	if err := r.publishWithReplyBudget(ctx, msg, derivedType); err != nil {
		settleInteraction(false)
		return nil, err
	}
	settleInteraction(true)

	if r.metrics != nil && r.metrics.MessagesPublished != nil {
		r.metrics.MessagesPublished.Add(ctx, 1, metric.WithAttributes(attribute.String("channel_type", string(derivedType))))
	}

	// Presence Tier 1 (RFC 0048): this publish IS the sender's reply, so clear it
	// from the channel's in-flight "thinking" set. Keyed on sender id and run on
	// every committed publish, this covers the chat, floor, and fire-and-forget
	// paths uniformly — an inbound user publish clears the (unmarked) user as a
	// no-op. It MUST run here, right after the store commit and BEFORE the
	// fanout-suppression early returns below (end-vote close, post-close drop,
	// cascade cap): the sender's reply has landed regardless of whether it draws
	// further fanout, so deferring the clear past those returns would strand the
	// agent that just ended the conversation in the indicator until the TTL. See
	// activity.go.
	r.clearActivity(msg.ChannelID, msg.SenderID)

	// RFC 0052 no-reopen latch, suppression half: the latched publish is the
	// closed record's late final word — persisted above, barred from fanout
	// like any post-close traffic. Deliberately NOT delegated to
	// processEndVote's tombstone branch below: the latch ledger spans
	// generations while the tombstone lives one, so a straggler landing after
	// the NEXT close discharged its tombstone would sail through processEndVote
	// unsuppressed and fan out stamped with a dead id — every reply re-latching
	// and re-fanning, an unbounded post-close ping-pong. The drop accounting is
	// the tombstone branch's own ([ChannelRouter.dropPostCloseTraffic]), so the
	// two suppression sites meter identically.
	//
	// Notify-then-suppress (PR #716 review): the latched reply IS persisted —
	// it is the awaited speaker's real final word — so the (channel, sender)-
	// keyed reply waiter below must still fire before the drop. A deliberate
	// close retiring the id mid-floor-round otherwise starves runFloorTurn
	// (floor_control.go selects only on the waiter and its timer; no close
	// path touches the waiter table), burning the full turn timeout for every
	// remaining speaker of an already-terminated round and mislabeling each as
	// floor_turn{timeout}. Pre-latch a post-close straggler minted fresh and
	// always reached Notify, so this preserves the waiter's pre-PR contract;
	// FANOUT suppression, not Notify starvation, is what prevents the reopen.
	if latched {
		r.waiter.Notify(msg)
		r.dropPostCloseTraffic(ctx, derivedType, resolvedInteractionID)
		return nil, nil
	}

	// RFC 0052 §D close-on-reply, the COMMIT-path claim (PR #718 review —
	// moved here from the fanout head): when this publish is the chair's
	// synthesis reply claiming the armed interaction, it IS the closing
	// artifact — run the bounded teardown with it as the closing message
	// (sole delivery: redelivery=false) and suppress fanout entirely (a fanned
	// synthesis would draw replies into the closed discussion — the reopen §D
	// forbids). It MUST run before the end-vote hook below: the directive
	// explicitly invites the chair to answer with an END_INTERACTION_VOTE
	// whose content is the synthesis (agents/end_vote_action.py stamps the
	// reply echo on votes for exactly that shape), and processEndVote consumed
	// such a reply first — an in-window duplicate was suppressed as spam (the
	// claim never ran, the arm burned the full 120s timeout, and the close
	// carried the bounding stimulus instead of the artifact), while a
	// quorum-completing vote closed as `end_votes` (the unmetered wire shape:
	// every RFC 0020 summary of a bound-crossed arc silently skipped its
	// OQ #6 lease). A racing quorum completed by ANOTHER member's vote keeps
	// its CE4 supremacy unchanged — it lands in processEndVote on its own
	// publish and markInteractionClosed disarms the arm. The claim is
	// side-effect-free for every non-matching publish beyond one short-held
	// lock; the reply waiter is notified like the latch branch above, so a
	// closing reply never starves a parked floor turn. A lost tombstone CAS
	// means a racing closer beat this reply — the synthesis stays committed
	// history, the 4b-i degraded artifact shape. The claim reads the
	// PRE-RESOLVE `inboundClaim` captured above, never the bag — the stamp at
	// the top of this function overwrote the bag with the resolver's verdict,
	// which while armed always equals the armed id, so a bag re-read cannot
	// reject a stale echo (PR #718 follow-up review; see claimSynthesisReply).
	if pendingSynth := r.claimSynthesisReply(msg, inboundClaim); pendingSynth != nil {
		// The claim transferred the arm's synthesisWG count to this branch;
		// closeOnSynthesisReply notifies the reply waiter, runs the teardown,
		// and releases the count only after the close's fanoutWG.Adds — the
		// drain-ordering contract (see both functions' docs).
		r.closeOnSynthesisReply(context.WithoutCancel(ctx), msg, derivedType, pendingSynth)
		return nil, nil
	}

	// RFC 0030 Layer 4 (v0.3.8) end-of-interaction signal: accumulate this
	// publish's end-vote (if any) into the interaction's quorum and, when K
	// distinct participants have voted within W consecutive turns, close the
	// interaction — suppressing fanout so the conversation stops drawing new
	// replies (§H). Runs post-persistence (the vote is a real message) and is
	// orthogonal to cascade_depth. Inert when untracked (no interaction_id) or
	// when no producer emits the vote, so it is additive over v0.3.7.
	if r.processEndVote(ctx, msg, derivedType) {
		return nil, nil
	}

	// Primary cascade-depth enforcement: drop fanout when at/over cap.
	// The publish itself succeeded (2xx) — only the cascade is
	// terminated. Python `EventDispatcher.max_cascade_depth=5`
	// (agents/dispatch.py:108-114) remains as defense-in-depth.
	if clampedDepth >= r.maxCascadeDepth {
		r.recordCascadeCap(ctx, msg, derivedType, clampedDepth)
		// Notify-then-suppress — the latch branch's posture, same starvation
		// (ISSUE-0110): an at-cap reply from the current floor speaker IS the
		// persisted reply the round's waiter is parked on; skipping Notify
		// here burned the full turn timeout for that speaker and every
		// remaining one, each mislabeled floor_turn{timeout}.
		r.waiter.Notify(msg)
		// RFC 0052: on an armed channel a capped chain is a TERMINAL bound —
		// no human can continue past it, and the suppressed fanout means no
		// further stimulus ever arrives, so an open interaction would wedge
		// immortal-but-inert. A floor-speaker reply defers the verdict to its
		// round's tail ([ChannelRouter.maybeContinueDiscussion] reads the same
		// depth — one owner per path); any other capped publish closes here.
		if !r.isFloorSpeakerReply(msg.ChannelID, msg.SenderID) {
			r.closeOnCascadeBound(ctx, msg, derivedType)
		}
		return nil, nil
	}

	// RFC 0011 PR 4b: pre-resolve `thread_parent_sender_id` once per
	// publish so a thread-heavy channel pays one [ChannelStore.GetMessage]
	// lookup per publish, not one per recipient. Empty for non-thread
	// events. A lookup miss (parent pruned by the per-channel cap before
	// the reply lands) is logged at debug and surfaces as an empty
	// string on the wire — receivers branch on
	// `thread_id != "" && thread_parent_sender_id != ""` so the empty
	// string is a benign signal rather than an error.
	threadParentSenderID := r.resolveThreadParentSenderID(ctx, msg)

	// Resolve any chat-as-DM waiter parked for this (channel, sender)
	// pair before fanout (RFC 0011 PR 4a-ii-β-2). Notify is a non-
	// blocking buffered send and a no-op when no waiter is registered,
	// so the hot path stays cheap when no chat is in flight.
	//
	// Notify runs on EVERY publish — keyed by `(channelID, senderID)`.
	// The chat handler registers waiters keyed by
	// `(dm.ID, awaitFromAgentID)`, so an inbound user→agent publish
	// (sender = user) cannot satisfy the waiter parked for the agent's
	// reply (sender = agent). Future callers that install a waiter
	// keyed by the user's id (e.g. echo-back semantics) MUST account
	// for the fact that the inbound publish itself fires Notify before
	// any subscriber receives — install the waiter on the OTHER
	// participant's id, never on the publisher's.
	r.waiter.Notify(msg)

	// RFC 0030 Layer 2.5 deferred fanout (amendment D1): when a serialized
	// floor round is active on this channel and this inbound message is a
	// reply from a speaker that round granted the floor, the round loop is the
	// sole dispatcher. The reply has been persisted (above) and — when the
	// speaker is still its current turn-holder — has just satisfied the loop's
	// waiter via Notify, so the loop advances with the reply now in history.
	// Running fanout here would re-introduce the N-way amplification floor
	// control exists to prevent. The set membership (not just the current
	// turn-holder) also covers a speaker that exhausted its turn budget (D2)
	// and replies late while a later speaker holds the floor: still a
	// participant of this round, so suppressed rather than spawning a competing
	// round. Cross-*round* cascade stays bounded by `cascade_depth` (Layer 0,
	// enforced above).
	if r.isFloorSpeakerReply(msg.ChannelID, msg.SenderID) {
		return nil, nil
	}

	return &fanoutPlan{
		msg:                  msg,
		derivedType:          derivedType,
		threadParentSenderID: threadParentSenderID,
	}, nil
}
