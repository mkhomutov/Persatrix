package channels

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"sync"
	"time"

	"go.uber.org/zap"

	"github.com/mkhomutov/persatrix/internal/defaults"
)

// Cascade-depth helpers (read/clamp/recordCascadeCap) live in
// [cascade_depth.go] — pulled out of this file so the router stays
// focused on publish + fanout topology.

// DispatchEnvelope — the per-recipient dispatcher contract — lives in
// dispatch_envelope.go (split out so router.go stays focused on publish +
// fanout topology and under the 500-line cap).

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

// RouterMetrics — the router's OTEL-handle struct — lives in router_metrics.go
// (split out so this file stays under the 500-line review cap).

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

	// floors is the RFC 0030 Layer 2.5 per-channel floor registry —
	// serializes concurrent speaker rounds on the same channel so at most
	// one round runs at a time. Always non-nil (init in [NewChannelRouter]).
	floors *floorRegistry

	// floorMu guards floorSettings and floorSpeakers.
	//
	// floorSettings is the resolved per-channel floor-control config
	// (enabled + per-turn timeout), keyed by channel id. Populated via
	// [ChannelRouter.SetFloorControl] at startup; a channel absent from the
	// map is floor-control-off (the PR-2 default — PR 3 flips the resolved
	// default on for group channels and wires the setter from config).
	//
	// floorSpeakers records, per channel, the set of speakers that have been
	// granted the floor during the *currently active* round — the seam the
	// deferred-fanout skip (D1) reads in [ChannelRouter.Publish] to recognise
	// a floor-turn reply and suppress its re-fanout. It is a set, not a single
	// holder, on purpose: a speaker that exhausts its turn budget (D2) and then
	// replies late — while a *later* speaker holds the floor — is still a
	// participant of this round, so its reply must be suppressed too rather
	// than spawn a competing fanout. Cleared as a whole when the round ends, so
	// a reply that genuinely arrives after the round re-fanouts normally
	// (bounded by `cascade_depth`). Empty when no round is active on a channel.
	floorMu       sync.Mutex
	floorSettings map[string]channelFloorSettings
	floorSpeakers map[string]map[string]struct{}

	// salienceMu guards salienceMaxMembers — the resolved RFC 0030 Tier B (v0.3.8)
	// per-channel salience-bid channel-size cap, keyed by channel id. Populated
	// via [ChannelRouter.SetSalienceMaxChannelMembers]; methods live in
	// router_salience.go. An absent channel resolves to [DefaultSalienceMaxChannelMembers].
	salienceMu         sync.Mutex
	salienceMaxMembers map[string]int

	// replyBudgetMu guards the RFC 0030 Layer 2 (v0.3.8) per-participant reply
	// budget state; methods + the full field contracts live in reply_budget.go.
	// replyBudgets: channel id → resolved K (0 = uncapped). replyCounts:
	// interaction id → participant id → publishes so far (discarded on close).
	// exemptParticipantTypes: participant types exempt from the budget
	// (governance.exempt_principals → `user`). defaultReplyBudget: fleet
	// `default_max_replies_per_participant`, captured for runtime inheritance.
	// All guarded by replyBudgetMu.
	replyBudgetMu          sync.Mutex
	replyBudgets           map[string]int
	replyCounts            map[string]map[string]int
	exemptParticipantTypes map[string]struct{}
	defaultReplyBudget     int

	// endVoteMu guards the RFC 0030 Layer 4 (v0.3.8) end-of-interaction vote
	// state; methods + the full field contracts live in end_vote.go.
	// endVoteThresholds/endVoteWindows: channel id → resolved K / W (absent
	// falls back to [DefaultEndVoteThreshold]/[DefaultEndVoteWindow]). endVotes:
	// interaction id → per-interaction vote accumulator (created on the first
	// vote, discarded on close). closedInteractions: interactions already closed
	// by an end-vote quorum, so a late publish stays suppressed and the close is
	// emitted once. All guarded by endVoteMu.
	endVoteMu          sync.Mutex
	endVoteThresholds  map[string]int
	endVoteWindows     map[string]int
	endVotes           map[string]*interactionEndVotes
	closedInteractions map[string]struct{}

	// maxCascadeDepth — see cascade_depth.go; defaultSessionID — see router_session.go (RFC 0031 Phase 1).
	maxCascadeDepth  int
	defaultSessionID string

	// fanoutWG tracks the detached fanout goroutines spawned by
	// [ChannelRouter.PublishAsync] so a graceful shutdown (or a test) can drain
	// them via [ChannelRouter.WaitForPendingFanout] rather than racing a
	// half-delivered round. The synchronous [ChannelRouter.Publish] does not
	// touch it (its fanout completes before the call returns). Zero value ready.
	fanoutWG sync.WaitGroup
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
		store:              store,
		dispatcher:         dispatcher,
		logger:             logger,
		metrics:            metrics,
		waiter:             newReplyWaiter(),
		floors:             newFloorRegistry(),
		floorSettings:      make(map[string]channelFloorSettings),
		floorSpeakers:      make(map[string]map[string]struct{}),
		salienceMaxMembers: make(map[string]int),
		replyBudgets:       make(map[string]int),
		replyCounts:        make(map[string]map[string]int),
		endVoteThresholds:  make(map[string]int),
		endVoteWindows:     make(map[string]int),
		endVotes:           make(map[string]*interactionEndVotes),
		closedInteractions: make(map[string]struct{}),
		maxCascadeDepth:    defaults.DefaultMaxCascadeDepth,
	}
}

// SetFloorControl resolves RFC 0030 Layer 2.5 floor control for `channelID`:
// when `enabled`, a publish to this channel with ≥2 candidate responders runs
// the serialized speaker round instead of the concurrent fanout. A
// non-positive `turnTimeout` normalizes to [DefaultFloorTurnTimeoutSeconds].
//
// PR 3 wires it two ways: at startup via [ChannelRouter.ResolveFloorControl]
// (config-declared + store-resident group channels, default on for groups),
// and at runtime via [Server.handleCreateChannel] when a group channel is
// created through `POST /api/v1/channels` (the RFC 0048 console "New channel"
// path). The runtime call lands post-startup on the live router concurrently
// with traffic — the floorMu guard below makes that safe, so "set before
// traffic" is the contract for the *config* defaults, not a hard precondition.
func (r *ChannelRouter) SetFloorControl(channelID string, enabled bool, turnTimeout time.Duration) {
	if turnTimeout <= 0 {
		turnTimeout = time.Duration(DefaultFloorTurnTimeoutSeconds) * time.Second
	}
	r.floorMu.Lock()
	defer r.floorMu.Unlock()
	r.floorSettings[channelID] = channelFloorSettings{enabled: enabled, turnTimeout: turnTimeout}
}

// floorSettingsFor returns the resolved floor config for `channelID` and
// whether any was set. A missing entry means floor control is off.
func (r *ChannelRouter) floorSettingsFor(channelID string) (channelFloorSettings, bool) {
	r.floorMu.Lock()
	defer r.floorMu.Unlock()
	s, ok := r.floorSettings[channelID]
	return s, ok
}

// FloorControlFor reports the resolved RFC 0030 Layer 2.5 floor-control
// settings for `channelID`: whether floor control is enabled, the per-turn
// timeout, and whether any settings were resolved at all (`set` false means no
// entry — floor control is off). Exposed for tests and ops introspection,
// mirroring [ChannelRouter.MaxCascadeDepth]; the runtime hot path reads the
// unexported [ChannelRouter.floorSettingsFor].
func (r *ChannelRouter) FloorControlFor(channelID string) (enabled bool, turnTimeout time.Duration, set bool) {
	s, ok := r.floorSettingsFor(channelID)
	return s.enabled, s.turnTimeout, ok
}

// SetMaxCascadeDepth overrides the default cap. Non-positive values
// are ignored so a zero/negative config row cannot silently disable
// the backstop. MUST run at startup before any [ChannelRouter.Publish]
// call — `maxCascadeDepth` is unsynchronised, so a runtime-reload path
// needs an [sync/atomic.Int64] promotion first (PR #319 review 5.1).
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
	plan, err := r.publishCommit(ctx, msg, declaredType)
	if err != nil || plan == nil {
		return err
	}
	r.fanout(ctx, plan.msg, plan.derivedType, plan.threadParentSenderID)
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
