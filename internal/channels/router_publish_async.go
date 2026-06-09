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
func (r *ChannelRouter) DrainPendingFanout(ctx context.Context) bool {
	done := make(chan struct{})
	go func() {
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
// [ChannelRouter.PublishAsync] has completed. Intended for graceful shutdown
// (drain in-flight deliveries before exit) and for tests that need a
// deterministic point to assert on dispatched recipients. A no-op when no
// async fanout is in flight.
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

	// RFC 0030 Layer 2 (v0.3.8) per-participant reply budget + store commit:
	// reserve the sender's slot, persist, and release the reservation if the
	// persist fails — so a throttled (K+1)th publish never enters channel
	// history (§F) and a store-rejected publish never erodes the allowance.
	// Additive: a no-op when uncapped, untracked, or an exempt human.
	if err := r.publishWithReplyBudget(ctx, msg, derivedType); err != nil {
		return nil, err
	}

	if r.metrics != nil && r.metrics.MessagesPublished != nil {
		r.metrics.MessagesPublished.Add(ctx, 1, metric.WithAttributes(attribute.String("channel_type", string(derivedType))))
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
