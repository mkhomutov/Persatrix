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

	"github.com/mkhomutov/persatrix/internal/defaults"
)

// Cascade-depth helpers (read/clamp/recordCascadeCap) live in
// [cascade_depth.go] — pulled out of this file so the router stays
// focused on publish + fanout topology.

// DispatchEnvelope bundles the per-recipient inputs the dispatcher needs
// to render a [taskpb.ChannelMessageEvent] without the router exposing the
// raw proto type to the channels package boundary. The envelope is built
// once per-recipient inside [ChannelRouter.fanout]:
//
//   - `Recipient` carries the per-recipient `RespondPolicy` so the
//     receiver-side response gate can decide pre-LLM (RFC 0011 PR 4b).
//   - `ThreadParentSenderID` is pre-resolved once per publish in
//     [ChannelRouter.Publish] so a thread-heavy channel pays one
//     `GetMessage` lookup per publish, not one per recipient (RFC 0011
//     PR plan §PR 4 — "amortizes the lookup across fanout").
//
// Adding fields here is an additive change to the dispatcher contract
// and does not require touching every test seam — both fields default
// to their zero values when unset.
type DispatchEnvelope struct {
	// Recipient is the membership row of the agent receiving this
	// dispatch. The router has already filtered the sender and any
	// `RespondNever` entries upstream of [MessageDispatcher.Dispatch],
	// so `Recipient.RespondPolicy` is always one of [RespondAlways]
	// or [RespondWhenMentioned].
	Recipient Member

	// ThreadParentSenderID is the sender id of the message addressed
	// by [ChannelMessage.ThreadID], pre-resolved by the router. Empty
	// for non-thread events. The empty-string default for proto3
	// strings is preserved on the wire so receivers can branch on
	// `thread_id != "" && thread_parent_sender_id != ""` without a
	// secondary lookup.
	ThreadParentSenderID string
}

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
	// Dispatch delivers msg to env.Recipient. The router has already
	// filtered the sender out of the recipient list, dropped any
	// `RespondNever` members, and validated `channel_type` against the
	// `channel_id` prefix. Returns an error if the dispatch could not
	// be enqueued; the caller logs and counts.
	Dispatch(ctx context.Context, env DispatchEnvelope, msg ChannelMessage) error
}

// NoopDispatcher is the v0.3.0-PR-2 placeholder: it counts the calls and
// returns nil, so the router's fanout topology can be tested end-to-end
// without a wired gRPC client. Replaced in PR 4 by the real gRPC-backed
// dispatcher that resolves participantID → registry address and invokes
// `AgentService.ReceiveChannelMessage`.
type NoopDispatcher struct{}

// Dispatch implements [MessageDispatcher] by no-op.
func (NoopDispatcher) Dispatch(_ context.Context, _ DispatchEnvelope, _ ChannelMessage) error {
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
	MessagesPublished metric.Int64Counter // pairs with MessagesDelivered (ISSUE-0013)
	// MessagesCascadeCapped counts per-recipient fanout dispatches
	// suppressed by the cascade-depth cap (RFC 0011 amendment
	// 'Cascade-depth wire propagation'). One increment per suppressed
	// recipient — directly comparable to MessagesDelivered. Labelled by
	// `channel_type`; `channel_id` lives on the structured log line.
	MessagesCascadeCapped metric.Int64Counter
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

	// waiter is the chat-as-DM publish-and-await correlation table
	// (RFC 0011 PR 4a-ii-β-2). Always non-nil — initialised in
	// [NewChannelRouter] — so the publish hot path can call
	// `Notify` unconditionally without a nil check.
	waiter *replyWaiter

	// maxCascadeDepth is the primary-enforcement cap on the cooperative-
	// path cascade backstop (RFC 0011 amendment 'Cascade-depth wire
	// propagation'). Defaults to [defaults.DefaultMaxCascadeDepth];
	// operators override via [ChannelRouter.SetMaxCascadeDepth]. MUST
	// stay aligned with the Python dispatcher's `max_cascade_depth`
	// (agents/dispatch.py:43) — the two are one conceptual cap with
	// two enforcement points (primary + defense-in-depth).
	maxCascadeDepth int
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
		store:           store,
		dispatcher:      dispatcher,
		logger:          logger,
		metrics:         metrics,
		waiter:          newReplyWaiter(),
		maxCascadeDepth: defaults.DefaultMaxCascadeDepth,
	}
}

// SetMaxCascadeDepth overrides the default cap. Non-positive values
// are ignored so a zero/negative config row cannot silently disable
// the backstop.
func (r *ChannelRouter) SetMaxCascadeDepth(d int) {
	if d > 0 {
		r.maxCascadeDepth = d
	}
}

// MaxCascadeDepth returns the active cap (exposed for tests + ops logs).
func (r *ChannelRouter) MaxCascadeDepth() int {
	return r.maxCascadeDepth
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

	if err := r.store.PublishMessage(ctx, msg); err != nil {
		return err
	}

	if r.metrics != nil && r.metrics.MessagesPublished != nil {
		r.metrics.MessagesPublished.Add(ctx, 1, metric.WithAttributes(attribute.String("channel_type", string(derivedType))))
	}

	// Primary cascade-depth enforcement: drop fanout when at/over cap.
	// The publish itself succeeded (2xx) — only the cascade is
	// terminated. Python `EventDispatcher.max_cascade_depth=5`
	// (agents/dispatch.py:108-114) remains as defense-in-depth.
	if clampedDepth >= r.maxCascadeDepth {
		r.recordCascadeCap(ctx, msg, derivedType, clampedDepth)
		return nil
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

	r.fanout(ctx, msg, derivedType, threadParentSenderID)
	return nil
}

// resolveThreadParentSenderID looks up the `sender_id` of the message
// addressed by `msg.ThreadID`. Returns "" for non-thread events or when
// the parent has been pruned. Empty is benign for the receiver gate.
func (r *ChannelRouter) resolveThreadParentSenderID(ctx context.Context, msg ChannelMessage) string {
	if msg.ThreadID == "" {
		return ""
	}
	parent, err := r.store.GetMessage(ctx, msg.ThreadID)
	if err != nil {
		r.logger.Debug("channels: thread parent lookup failed; gate will not fire thread-reply-to-self",
			zap.String("channel_id", msg.ChannelID),
			zap.String("thread_id", msg.ThreadID),
			zap.Error(err),
		)
		return ""
	}
	return parent.SenderID
}

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
		_, err := r.store.GetChannel(ctx, canonicalID)
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
		case errors.Is(err, ErrChannelNotFound):
			// PR #245 re-review (Med): the previous implementation called
			// CreateChannel followed by an N-call AddMember loop. A failure
			// mid-loop (transient store error or an invalid declared
			// member that bypassed Config.Validate) left the channel row
			// committed with only a prefix of the declared membership;
			// the next startup then tripped
			// ErrConfigStoreMembershipDivergence and required manual
			// operator cleanup. The handler-side fix already adopted
			// CreateChannelWithMembers for atomicity (PR #245 review High);
			// reconcile is now consistent with that contract.
			members := make([]Member, 0, len(decl.Members))
			for _, m := range decl.Members {
				members = append(members, Member{
					ParticipantID: m.ID,
					RespondPolicy: m.RespondPolicy,
				})
			}
			if err := r.store.CreateChannelWithMembers(ctx, Channel{
				ID:          canonicalID,
				Name:        decl.Name,
				Type:        ChannelTypeGroup,
				Description: decl.Description,
			}, members); err != nil {
				return fmt.Errorf("channels: reconcile create %s: %w", canonicalID, err)
			}
		default:
			return fmt.Errorf("channels: reconcile lookup %s: %w", canonicalID, err)
		}
	}
	return nil
}

// membershipDivergence returns the symmetric-difference participant ids
// between the declared config and the live store. Id-set divergence
// only; policy drift OQ-deferred to PR 7 (ISSUE-0010).
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
