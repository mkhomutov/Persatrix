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

// fanout looks up subscribers, filters the sender, and dispatches with
// bounded concurrency (ISSUE-0014). The publish call BLOCKS on fanout
// completion — Publish only returns once every recipient has either been
// dispatched or hit `channelFanoutPerRecipientTimeout` — but in-flight
// dispatches are capped at `channelFanoutMaxConcurrency` so a single
// stalled recipient cannot starve the tail and a 1000-member channel
// does not spawn 1000 goroutines.
//
// Detaches the request context (`context.WithoutCancel`) so a client
// disconnect mid-fanout does not silently drop later subscribers — the
// HTTP response has already been written by the time we are here, so the
// caller's deadline is no longer the right shape.
//
// History: PR #245 added a per-recipient timeout to fix intra-publish
// starvation; ISSUE-0014 added the concurrency bound once the PR-4 gRPC
// dispatcher made the worst-case tail visible (a stalled recipient ties
// up the publish path for `channelFanoutPerRecipientTimeout` whether or
// not the loop is concurrent — but a sequential loop multiplies that by
// N).
func (r *ChannelRouter) fanout(ctx context.Context, msg ChannelMessage, ct ChannelType, threadParentSenderID string) {
	members, err := r.store.GetMembers(ctx, msg.ChannelID)
	if err != nil {
		r.logger.Warn("channels: fanout member lookup failed",
			zap.String("channel_id", msg.ChannelID),
			zap.Error(err))
		return
	}

	detached := context.WithoutCancel(ctx)

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
		m := m
		sem <- struct{}{}
		wg.Add(1)
		go func() {
			defer wg.Done()
			defer func() { <-sem }()

			dispatchCtx, cancel := context.WithTimeout(detached, channelFanoutPerRecipientTimeout)
			defer cancel()
			err := r.dispatcher.Dispatch(dispatchCtx, DispatchEnvelope{
				Recipient:            m,
				ThreadParentSenderID: threadParentSenderID,
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
				r.metrics.MessagesDelivered.Add(detached, 1, metric.WithAttributes(
					attribute.String("channel_type", string(ct)),
					attribute.String("status", status),
				))
			}
		}()
	}
	wg.Wait()
}
