package channels

import (
	"context"
	"sync"
	"time"

	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/metric"
	"go.uber.org/zap"
)

// channelFanoutMaxConcurrency caps the number of in-flight per-recipient
// dispatches inside [ChannelRouter.fanout]. Without a cap, a publish to an
// N-member channel where each Dispatch costs `channelFanoutPerRecipientTimeout`
// (5s) would block the publish path for O(N × 5s) on a stalled tail; the cap
// keeps worst-case publish latency at O(ceil(N / limit) × 5s) while bounding
// goroutine pressure at the publish call site. 16 is sized for v0.3.0 group-
// channel sizes (≤ 50 members typical) — review when channels grow large
// enough to make ceil(N/16) slow paths visible.
const channelFanoutMaxConcurrency = 16

// channelFanoutPerRecipientTimeout caps how long a single per-recipient
// dispatch can block before the router moves on. With the PR-4 gRPC
// dispatcher live, this is the deadline propagated into
// `AgentService.ReceiveChannelMessage`; the receiver is contractually
// fire-and-forget so the deadline only protects against a stuck dial.
const channelFanoutPerRecipientTimeout = 5 * time.Second

// fanout looks up subscribers, filters the sender, and dispatches the
// message to the remaining recipients. It chooses between two paths:
//
//   - **Serialized floor round** (RFC 0030 Layer 2.5) when floor control is
//     enabled for the channel and there are ≥2 candidate responders: the
//     responders take the floor one at a time, each reading the prior
//     speaker's reply (see [ChannelRouter.floorRound]). Non-responders are
//     delivered fire-and-forget for memory ingestion only, off the floor.
//   - **Concurrent fanout** otherwise (flag off, DM, or a single responder):
//     the pre-amendment path — every non-sender, non-`never` member is
//     dispatched with bounded concurrency (ISSUE-0014).
//
// Either way the publish call BLOCKS on fanout completion — Publish only
// returns once fanout is done. For the concurrent path that is bounded at
// O(ceil(N / `channelFanoutMaxConcurrency`) × `channelFanoutPerRecipientTimeout`);
// for the floor path it is the round duration (responders go serial, each
// bounded by the per-turn timeout) — the documented latency trade.
//
// Detaches the request context (`context.WithoutCancel`) so a client
// disconnect mid-fanout does not silently drop later subscribers — the
// HTTP response shape is no longer the caller's deadline by the time we are
// here.
//
// History: PR #245 added a per-recipient timeout to fix intra-publish
// starvation; ISSUE-0014 added the concurrency bound once the PR-4 gRPC
// dispatcher made the worst-case tail visible.
func (r *ChannelRouter) fanout(ctx context.Context, msg ChannelMessage, ct ChannelType, threadParentSenderID string) {
	members, err := r.store.GetMembers(ctx, msg.ChannelID)
	if err != nil {
		r.logger.Warn("channels: fanout member lookup failed",
			zap.String("channel_id", msg.ChannelID),
			zap.Error(err))
		return
	}

	// RFC 0030 Layer 2.5: serialize when floor control is on for this channel
	// and the candidate responder set is large enough to overlap. A DM or a
	// single responder cannot collide, so it falls through to the concurrent
	// path with no per-turn latency.
	// RFC 0030 Tier B (v0.3.8): the channel's member count rides every dispatch
	// so the agent-side seam can apply the TB6 channel-size cap. A per-publish
	// value (identical across recipients), captured once here.
	channelSize := len(members)

	if settings, ok := r.floorSettingsFor(msg.ChannelID); ok && settings.enabled {
		responders, nonResponders := orderResponders(members, msg, threadParentSenderID)
		if len(responders) >= 2 {
			r.floorRound(ctx, msg, ct, threadParentSenderID, responders, nonResponders, settings.turnTimeout, channelSize)
			return
		}
	}

	r.dispatchConcurrent(context.WithoutCancel(ctx), msg, ct, threadParentSenderID, members, channelSize)
}

// dispatchConcurrent fans `msg` out to every member of `members` other than
// the sender and `never` participants, with peak in-flight dispatches capped
// at `channelFanoutMaxConcurrency` (ISSUE-0014). Blocks until every selected
// recipient has been dispatched or hit `channelFanoutPerRecipientTimeout`.
//
// `ctx` is expected to already be detached from the request lifetime by the
// caller (so the floor path can reuse it for off-floor non-responder
// delivery without re-detaching).
func (r *ChannelRouter) dispatchConcurrent(ctx context.Context, msg ChannelMessage, ct ChannelType, threadParentSenderID string, members []Member, channelSize int) {
	// Buffered channel as a semaphore: each goroutine acquires a slot
	// before starting and releases it on exit, so peak in-flight
	// dispatches never exceed `channelFanoutMaxConcurrency`. The
	// acquire is on the publishing goroutine (not inside the worker)
	// so we apply backpressure on the loop itself rather than letting
	// goroutine creation outpace dispatch completion.
	sem := make(chan struct{}, channelFanoutMaxConcurrency)
	var wg sync.WaitGroup

	for _, m := range members {
		if m.ParticipantID == msg.SenderID {
			continue
		}
		if m.RespondPolicy == RespondNever {
			// `respond: never` participants do not receive dispatches in
			// the v0.3.0 contract — they read history on demand. The
			// response gate (PR 4b) is the canonical enforcement point;
			// short-circuiting here keeps the dispatcher free of policy
			// knowledge and saves a wasted gRPC call.
			continue
		}
		// RFC 0030 Tier A note: a directed-elsewhere `always` member (one
		// the floor path's [orderResponders] drops to non-responder, see
		// floor_control.go) is intentionally NOT short-circuited here. The
		// receiver gate suppresses its *reply* (directed_elsewhere) but the
		// dispatch still lands so the member *ingests* the message into
		// memory — the gate decides whether to respond, not whether to
		// remember (agents/persona_runtime/action_loop.py's ingest-on-
		// suppress). Filtering it out to mirror the floor path would make
		// un-addressed participants amnesiac. The floor path can drop it
		// because it re-delivers non-responders fire-and-forget for exactly
		// this ingestion; the concurrent path's single dispatch is that
		// delivery.
		m := m
		sem <- struct{}{}
		wg.Add(1)
		go func() {
			defer wg.Done()
			defer func() { <-sem }()
			r.dispatchTo(ctx, msg, ct, threadParentSenderID, m, channelSize)
		}()
	}
	wg.Wait()
}

// dispatchTo delivers `msg` to a single recipient with the per-recipient
// timeout and emits the `channel.messages.delivered` counter. Shared by the
// concurrent path ([dispatchConcurrent]) and the serialized floor turn
// ([runFloorTurn]) so both honour the same deadline + metric contract.
func (r *ChannelRouter) dispatchTo(ctx context.Context, msg ChannelMessage, ct ChannelType, threadParentSenderID string, m Member, channelSize int) {
	dispatchCtx, cancel := context.WithTimeout(ctx, channelFanoutPerRecipientTimeout)
	defer cancel()
	err := r.dispatcher.Dispatch(dispatchCtx, DispatchEnvelope{
		Recipient:            m,
		ThreadParentSenderID: threadParentSenderID,
		// RFC 0030 Tier B (v0.3.8): the per-publish channel-size + resolved cap
		// the agent-side seam reads for the TB6 channel-size gate. The
		// per-recipient bid signals (salience_gated/threshold) ride on
		// `m`/`Recipient`; these two are channel-wide.
		ChannelSize:               channelSize,
		SalienceMaxChannelMembers: r.salienceMaxFor(msg.ChannelID),
	}, msg)
	status := "ok"
	if err != nil {
		status = "error"
		r.logger.Warn("channels: dispatch failed",
			zap.String("channel_id", msg.ChannelID),
			zap.String("recipient", m.ParticipantID),
			zap.Error(err))
	}
	if r.metrics != nil && r.metrics.MessagesDelivered != nil {
		r.metrics.MessagesDelivered.Add(ctx, 1, metric.WithAttributes(
			attribute.String("channel_type", string(ct)),
			attribute.String("status", status),
		))
	}
}
