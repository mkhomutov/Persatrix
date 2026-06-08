package channels

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"sync"
	"time"

	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/metric"
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
	// FloorTurn counts each completed RFC 0030 Layer 2.5 floor-control
	// speaker turn, labelled by `channel_type` and `outcome`
	// (`replied`|`timeout`). The `timeout` share is the stalled-floor-holder
	// rate that calibrates the per-turn timeout default (amendment D2).
	FloorTurn metric.Int64Counter
	// FloorRoundDuration records the wall-clock duration of a serialized
	// floor round in milliseconds, labelled by `channel_type` — the
	// serialization latency trade made observable (the trigger to revisit
	// the no-cap decision, amendment D4).
	FloorRoundDuration metric.Float64Histogram
	// GovernanceDrop counts each publish dropped by an RFC 0030 deterministic
	// governance layer (v0.3.8), labelled by `channel_type` and `layer`
	// (`reply_budget` in this PR; `cost`/`depth`/`end_vote` join in PR 5). The
	// per-drop attribution (channel, interaction, participant) is on the Warn line.
	GovernanceDrop metric.Int64Counter
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
	// (governance.exempt_principals → `user`). All guarded by replyBudgetMu.
	replyBudgetMu          sync.Mutex
	replyBudgets           map[string]int
	replyCounts            map[string]map[string]int
	exemptParticipantTypes map[string]struct{}

	// maxCascadeDepth — see cascade_depth.go; defaultSessionID — see router_session.go (RFC 0031 Phase 1).
	maxCascadeDepth  int
	defaultSessionID string
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

	// RFC 0030 Layer 2 (v0.3.8) per-participant reply budget: reject the
	// sender's (K+1)th publish in this interaction BEFORE the store commit, so
	// a throttled message never enters channel history (§F). A no-op when the
	// channel is uncapped, the publish is untracked (no interaction_id), or the
	// sender is an exempt human principal — so the layer is additive.
	if err := r.enforceReplyBudget(ctx, msg, derivedType); err != nil {
		return err
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
		return nil
	}

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
