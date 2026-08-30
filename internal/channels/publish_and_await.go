package channels

import (
	"context"
	"errors"
	"fmt"
	"time"
)

// publish_and_await.go — the chat-as-DM façade's one blocking publish surface
// (RFC 0011 amendment), split out of router.go so that file stays under the
// 500-line review cap. A pure move: the code below is byte-identical to what
// router.go held, and the split follows the same reasoning router_publish_async.go
// was carved out on — router.go keeps the publish + fanout topology, and the
// surfaces layered on top of it live beside the machinery they use.

// ErrChatTimeout is returned by [PublishAndAwait] when no matching
// reply arrives within the caller's timeout. The inbound message is
// still persisted (the user's turn is not lost just because the agent
// failed to reply).
var ErrChatTimeout = errors.New("channels: chat reply timed out")

// PublishAndAwait powers the chat-as-DM façade (RFC 0011 amendment).
// The chat REST handler calls this with the user's inbound
// CHANNEL_MESSAGE; the call returns when the agent's reply
// (`SEND_CHANNEL_MESSAGE` published from `awaitFromAgentID` on the same
// DM channel) arrives, or when `timeout` elapses.
//
// Sequence:
//
//  1. Register a waiter for `(msg.ChannelID, awaitFromAgentID)` BEFORE
//     publishing — closes the race where the agent replies faster than
//     the handler can install the waiter.
//  2. Call [Publish] (persistence + fanout via gRPC). The agent's
//     `ReceiveChannelMessage` is invoked downstream.
//  3. Block on the waiter chan until either:
//     - the agent's REST publish satisfies the waiter (happy path), or
//     - `timeout` elapses (`ErrChatTimeout`), or
//     - the caller's context is cancelled (e.g. client disconnect).
//
// On any non-happy-path exit, the waiter is removed via the deferred
// cancel so a late-arriving reply does not leak into a future chat.
//
// Auth: this entry point assumes the caller (HTTP handler) has already
// validated the user is permitted to address the agent. The DM-creation
// boundary in [ChannelStore.GetOrCreateDM] is the canonical access
// check (see [RFC 0011 amendment §"DM gate-bypass"]); the response gate
// is implicitly `always` for DM channels and is therefore not consulted
// here.
//
// Scaling constraint: correlation is **in-process** via [replyWaiter].
// Horizontal-scale rollouts require
// a cross-process replacement before chat can survive the topology —
// see the `replyWaiter` doc-string for the full rationale.
func (r *ChannelRouter) PublishAndAwait(
	ctx context.Context,
	msg ChannelMessage,
	awaitFromAgentID string,
	timeout time.Duration,
) (ChannelMessage, error) {
	// Defense-in-depth: reject the self-reply trap before any store
	// mutation. If `msg.SenderID == awaitFromAgentID`, the inbound
	// publish would satisfy its own waiter via `Publish` → `Notify`
	// (which keys on `(channelID, senderID)`) and the call would
	// return the caller's inbound message AS the "reply".
	// `ChannelStore.GetOrCreateDM` already blocks `user == agent`
	// upstream of the chat handler today, but `PublishAndAwait` is
	// part of this package's public surface and may gain other
	// callers (workflow steps, integration tests). Reusing the
	// existing `ErrInvalidParticipantID` sentinel gives the chat
	// handler's existing `errors.Is` arm the right 400 mapping for
	// free, without inventing a new error class.
	if msg.SenderID == awaitFromAgentID {
		return ChannelMessage{}, fmt.Errorf(
			"%w: PublishAndAwait requires sender_id (%q) to differ from awaitFromAgentID",
			ErrInvalidParticipantID, msg.SenderID,
		)
	}
	replyCh, cancel, err := r.waiter.Register(msg.ChannelID, awaitFromAgentID)
	if err != nil {
		return ChannelMessage{}, fmt.Errorf("channels: PublishAndAwait register: %w", err)
	}
	defer cancel()

	if err := r.Publish(ctx, msg, ""); err != nil {
		return ChannelMessage{}, err
	}

	timer := time.NewTimer(timeout)
	defer timer.Stop()
	select {
	case reply := <-replyCh:
		return reply, nil
	case <-timer.C:
		return ChannelMessage{}, ErrChatTimeout
	case <-ctx.Done():
		return ChannelMessage{}, ctx.Err()
	}
}
