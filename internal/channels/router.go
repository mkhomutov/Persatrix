package channels

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"time"

	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/metric"
	"go.uber.org/zap"
)

// MessageDispatcher is the gRPC seam through which the [ChannelRouter]
// fans a published message out to every subscriber other than the sender.
//
// PR 2 of RFC 0011 ships only the dispatcher *interface* and a no-op
// implementation. The wire-side gRPC call to `ReceiveChannelMessage`
// (proto regen + servicer) lands in PR 3 + PR 4 — splitting the seam from
// its first concrete implementation keeps the PR diff under the 500-line
// soft cap and lets the router unit tests exercise the fanout topology
// without booting a fake gRPC server.
//
// Implementations MUST treat `Dispatch` as fire-and-forget: the publish
// path's HTTP response has already been written by the time fanout runs.
// Errors returned here are recorded via the
// `channel.messages.delivered{status="error"}` counter and logged at warn,
// but do not surface to the publisher.
type MessageDispatcher interface {
	// Dispatch delivers msg to participantID. The router has already
	// filtered the sender out of the recipient list and validated
	// `channel_type` against the `channel_id` prefix. Returns an error if
	// the dispatch could not be enqueued; the caller logs and counts.
	Dispatch(ctx context.Context, participantID string, msg ChannelMessage) error
}

// NoopDispatcher is the v0.3.0-PR-2 placeholder: it counts the calls and
// returns nil, so the router's fanout topology can be tested end-to-end
// without a wired gRPC client. Replaced in PR 4 by the real gRPC-backed
// dispatcher that resolves participantID → registry address and invokes
// `AgentService.ReceiveChannelMessage`.
type NoopDispatcher struct{}

// Dispatch implements [MessageDispatcher] by no-op.
func (NoopDispatcher) Dispatch(_ context.Context, _ string, _ ChannelMessage) error {
	return nil
}

// RouterMetrics is the subset of orchestrator OTEL handles the router
// needs. Defined locally (rather than imported from the metrics package)
// so the channels package does not take a dependency on the orchestrator-
// wide instrument struct — that would invert the dependency direction
// (channels is consumed *by* server, not the other way around).
//
// Nil-safe: a nil RouterMetrics value disables metric emission so unit
// tests and minimal deployments can run without OTEL wiring.
type RouterMetrics struct {
	// MessagesDelivered counts each per-subscriber dispatch attempt with
	// labels `channel_type` and `status` (`ok` | `error`). One increment
	// per recipient, not per publish. Sender filtering happens before the
	// counter fires, so the count reflects effective delivery attempts.
	MessagesDelivered metric.Int64Counter
}

// ChannelRouter is the publish-and-fanout entry point used by the REST
// `POST /api/v1/channels/{id}/messages` handler and (in PR 4) the
// `SEND_CHANNEL_MESSAGE` action executor.
//
// Responsibilities:
//
//  1. Validate that `msg.ChannelType` (when non-empty) agrees with the
//     `channel_id` prefix — RFC 0011 §C "channel_type proto-field
//     redundancy" requires the orchestrator to reject a publish when the
//     two disagree.
//  2. Persist the message via [ChannelStore.PublishMessage] (which itself
//     enforces membership and the per-channel cap).
//  3. Look up subscribers via [ChannelStore.GetMembers], filter the
//     sender out, and call `Dispatcher.Dispatch` for each remaining
//     participant.
//
// Steps 1+2 run synchronously on the publish path; step 3 fires after
// the store commit returns and is detached from the HTTP request
// lifetime (`context.WithoutCancel`) so a client disconnect during
// fanout cannot leave half the subscribers undelivered.
type ChannelRouter struct {
	store      ChannelStore
	dispatcher MessageDispatcher
	logger     *zap.Logger
	metrics    *RouterMetrics
}

// NewChannelRouter wires a router around a store, dispatcher, logger, and
// optional metrics handle. Pass [NoopDispatcher]{} until the gRPC-backed
// dispatcher lands in PR 4. Logger must be non-nil; pass `zap.NewNop()`
// in tests that do not care about log output.
func NewChannelRouter(store ChannelStore, dispatcher MessageDispatcher, logger *zap.Logger, metrics *RouterMetrics) *ChannelRouter {
	if logger == nil {
		logger = zap.NewNop()
	}
	if dispatcher == nil {
		dispatcher = NoopDispatcher{}
	}
	return &ChannelRouter{
		store:      store,
		dispatcher: dispatcher,
		logger:     logger,
		metrics:    metrics,
	}
}

// Publish runs steps 1+2 synchronously; on success, fanout (step 3) runs
// inline with a detached context so the publish handler can return as
// soon as the store commit is durable.
//
// `declaredType` carries the optional `channel_type` field from the wire
// (REST body or proto). Pass an empty string to skip the cross-check;
// the canonical type is always derived from the `channel_id` prefix.
//
// Returns:
//
//   - [ErrInvalidChannelType] if `declaredType` disagrees with the
//     `channel_id` prefix or the prefix is unknown.
//   - [ErrChannelNotFound] if the target channel does not exist.
//   - [ErrNotMember] if the sender is not a member.
//   - any other error surfaced by the store.
//
// Caller MUST set `msg.ID` (UUID); `msg.Timestamp` is derived by the
// store when zero.
func (r *ChannelRouter) Publish(ctx context.Context, msg ChannelMessage, declaredType string) error {
	derivedType, err := channelTypeFromID(msg.ChannelID)
	if err != nil {
		return err
	}
	if declaredType != "" && ChannelType(declaredType) != derivedType {
		return fmt.Errorf("%w: channel_type=%q disagrees with channel_id prefix (%s)",
			ErrInvalidChannelType, declaredType, derivedType)
	}

	if err := r.store.PublishMessage(ctx, msg); err != nil {
		return err
	}

	r.fanout(ctx, msg, derivedType)
	return nil
}

// fanout looks up subscribers, filters the sender, and dispatches. Runs
// inline (not as a goroutine) for v0.3.0 simplicity: the dispatcher in
// PR 2 is a no-op, and even the PR 4 gRPC-backed dispatcher will fire
// fire-and-forget RPCs that return on enqueue, not on agent ack. If a
// future PR adds streaming or per-subscriber retry, lift this onto a
// worker pool.
//
// Detaches the request context (`context.WithoutCancel`) so a client
// disconnect mid-fanout does not silently drop later subscribers. Adds
// a soft 5s deadline so a wedged dispatcher cannot block the publish
// handler indefinitely.
func (r *ChannelRouter) fanout(ctx context.Context, msg ChannelMessage, ct ChannelType) {
	members, err := r.store.GetMembers(ctx, msg.ChannelID)
	if err != nil {
		r.logger.Warn("channels: fanout member lookup failed",
			zap.String("channel_id", msg.ChannelID),
			zap.Error(err))
		return
	}

	dispatchCtx, cancel := context.WithTimeout(context.WithoutCancel(ctx), 5*time.Second)
	defer cancel()

	for _, m := range members {
		if m.ParticipantID == msg.SenderID {
			continue
		}
		if m.RespondPolicy == RespondNever {
			// `respond: never` participants do not receive dispatches in
			// the v0.3.0 contract — they read history on demand. The
			// response gate (PR 4) is the canonical enforcement point;
			// short-circuiting here keeps the dispatcher free of policy
			// knowledge and saves a wasted gRPC call.
			continue
		}
		err := r.dispatcher.Dispatch(dispatchCtx, m.ParticipantID, msg)
		status := "ok"
		if err != nil {
			status = "error"
			r.logger.Warn("channels: dispatch failed",
				zap.String("channel_id", msg.ChannelID),
				zap.String("recipient", m.ParticipantID),
				zap.Error(err))
		}
		if r.metrics != nil && r.metrics.MessagesDelivered != nil {
			r.metrics.MessagesDelivered.Add(dispatchCtx, 1, metric.WithAttributes(
				attribute.String("channel_type", string(ct)),
				attribute.String("status", status),
			))
		}
	}
}

// channelTypeFromID derives the canonical channel type from a channel id's
// prefix. Returns [ErrInvalidChannelType] if the prefix is unknown.
func channelTypeFromID(id string) (ChannelType, error) {
	switch {
	case strings.HasPrefix(id, "group:"):
		return ChannelTypeGroup, nil
	case strings.HasPrefix(id, "dm:"):
		return ChannelTypeDM, nil
	case strings.HasPrefix(id, "thread:"):
		return ChannelTypeThread, nil
	default:
		return "", fmt.Errorf("%w: unknown channel_id prefix in %q", ErrInvalidChannelType, id)
	}
}

// ReconcileConfig applies a loaded [Config] against the store at startup.
//
// v0.3.0 §B coexistence rules:
//
//   - Channels declared in config but absent from the store are created.
//   - Channels in the store but not in config are preserved untouched
//     (REST is allowed to create channels at runtime).
//   - Memberships declared in config are inserted (idempotent re-add for
//     existing rows).
//   - When a config-declared channel exists in the store with a
//     **different** member set than the config declares, that is a loud
//     failure: `ErrConfigStoreMembershipDivergence` is returned listing
//     the divergent participant ids. Operators must reconcile by editing
//     `config/channels.yaml` or running `DELETE /api/v1/channels/{id}`.
//
// Returns nil on a clean reconcile; the only non-nil error path is the
// divergence case above (and unrecoverable store errors).
func (r *ChannelRouter) ReconcileConfig(ctx context.Context, cfg *Config) error {
	if cfg == nil {
		return nil
	}
	for _, decl := range cfg.Channels {
		canonicalID := decl.CanonicalID()
		existing, err := r.store.GetChannel(ctx, canonicalID)
		switch {
		case err == nil:
			// Channel already in store — verify membership parity.
			storeMembers, mErr := r.store.GetMembers(ctx, canonicalID)
			if mErr != nil {
				return fmt.Errorf("channels: reconcile %s: %w", canonicalID, mErr)
			}
			if div := membershipDivergence(decl, storeMembers); len(div) > 0 {
				return fmt.Errorf("%w: channel=%s divergent_participants=%v",
					ErrConfigStoreMembershipDivergence, canonicalID, div)
			}
			r.logger.Debug("channels: config channel present in store",
				zap.String("channel_id", canonicalID))
			_ = existing
		case errors.Is(err, ErrChannelNotFound):
			if err := r.store.CreateChannel(ctx, Channel{
				ID:          canonicalID,
				Name:        decl.Name,
				Type:        ChannelTypeGroup,
				Description: decl.Description,
			}); err != nil {
				return fmt.Errorf("channels: reconcile create %s: %w", canonicalID, err)
			}
			for _, m := range decl.Members {
				if err := r.store.AddMember(ctx, canonicalID, m.ID, m.RespondPolicy); err != nil {
					return fmt.Errorf("channels: reconcile add member %s/%s: %w",
						canonicalID, m.ID, err)
				}
			}
		default:
			return fmt.Errorf("channels: reconcile lookup %s: %w", canonicalID, err)
		}
	}
	return nil
}

// membershipDivergence returns the symmetric-difference participant ids
// between the declared config and the live store. Empty result means the
// two agree (modulo policy — divergent policy is logged but not
// considered hard divergence in v0.3.0; OQ-deferred to PR 7).
func membershipDivergence(decl ChannelConfig, store []Member) []string {
	declSet := make(map[string]struct{}, len(decl.Members))
	for _, m := range decl.Members {
		declSet[m.ID] = struct{}{}
	}
	storeSet := make(map[string]struct{}, len(store))
	for _, m := range store {
		storeSet[m.ParticipantID] = struct{}{}
	}
	var diff []string
	for id := range declSet {
		if _, ok := storeSet[id]; !ok {
			diff = append(diff, "-"+id) // declared but missing in store
		}
	}
	for id := range storeSet {
		if _, ok := declSet[id]; !ok {
			diff = append(diff, "+"+id) // present in store but undeclared
		}
	}
	return diff
}

// ErrConfigStoreMembershipDivergence is returned by [ChannelRouter.ReconcileConfig]
// when a config-declared channel has a member set in the store that
// disagrees with the declaration. RFC 0011 §B coexistence rules treat
// this as a loud-failure to surface ad-hoc REST additions that were not
// rolled into config.
var ErrConfigStoreMembershipDivergence = errors.New("channels: config-vs-store membership divergence")
