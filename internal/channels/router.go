package channels

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"go.uber.org/zap"

	"github.com/mkhomutov/persatrix/internal/defaults"
)

// Cascade-depth helpers (read/clamp/recordCascadeCap) live in
// [cascade_depth.go] — pulled out of this file so the router stays
// focused on publish + fanout topology.

// DispatchEnvelope, [MessageDispatcher], and [NoopDispatcher] — the
// per-recipient dispatcher contract — live in dispatch_envelope.go (split out
// so router.go stays focused on publish + fanout topology and under the
// 500-line cap; the interface + no-op joined the envelope there when the
// PR #718 follow-up review's delivery-miss contract expansion pushed this
// file past it).

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

	// budgetMu guards the RFC 0030 Layer 1 (v0.3.8) per-channel interaction cost
	// ceiling; methods + the full field contracts live in interaction_budget.go.
	// channelBudgets: channel id → resolved interaction_budget_tokens (0 =
	// uncapped). defaultInteractionBudget: fleet default_interaction_budget_tokens,
	// captured for runtime inheritance (zero is a meaningful "uncapped" value, like
	// the reply budget, so it cannot ride a Set(_, 0) sentinel).
	// interactionBudgetSnapshots: interaction id → the channel budget snapshotted
	// when that interaction first committed (RFC 0050 amendment — the snapshot the
	// wallet's server-side resolver reads; stable for the interaction's life,
	// evicted on close alongside the end-vote tombstone). Only capped (>0)
	// interactions get an entry, so an uncapped fleet leaves it empty. All guarded
	// by budgetMu.
	budgetMu                   sync.Mutex
	channelBudgets             map[string]int64
	defaultInteractionBudget   int64
	interactionBudgetSnapshots map[string]int64

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

	// spend is the RFC 0052 (v0.3.11) bounded-close soft-budget read — the wallet's
	// per-interaction running total ([interactionSpender]; wired via
	// [ChannelRouter.SetInteractionSpender], contract + methods in bounded_close.go).
	// Unsynchronised like maxCascadeDepth — set at startup before traffic.
	spend interactionSpender

	// synthesisTimeout bounds the RFC 0052 §D close-on-reply's wait for the
	// chair's synthesis reply before the timeout net closes without it
	// ([defaultSynthesisReplyTimeout]; story in synthesis_close.go).
	// Unsynchronised like maxCascadeDepth — set at startup (or by a test)
	// before traffic.
	synthesisTimeout time.Duration

	// fanoutWG tracks the detached fanout goroutines spawned by
	// [ChannelRouter.PublishAsync] so a graceful shutdown (or a test) can drain
	// them via [ChannelRouter.WaitForPendingFanout] rather than racing a
	// half-delivered round. The synchronous [ChannelRouter.Publish] does not
	// touch it (its fanout completes before the call returns). Zero value ready.
	fanoutWG sync.WaitGroup

	// synthesisWG tracks the RFC 0052 PR 4b-ii armed synthesis timeout nets
	// (synthesis_close.go) — detached [time.Timer] goroutines whose close work
	// Add(1)s to fanoutWG. SEPARATE from fanoutWG on purpose (PR #718 finding 1):
	// the arm is a long-lived wait on the chair's reply, so WaitForPendingFanout
	// must not block on it; only the shutdown drain DrainPendingFanout waits it,
	// after disarming, so no timer races the fanoutWG.Wait. Zero value ready.
	synthesisWG sync.WaitGroup

	// fanoutInFlight counts the detached fanout goroutines currently running,
	// and maxInFlightFanout caps that count (0 = unbounded). The async seam
	// removed the backpressure the blocking POST used to apply, so without a
	// ceiling a looping caller could spawn goroutines without bound. At the cap
	// [ChannelRouter.PublishAsync] runs fanout inline instead — see
	// [defaultMaxInFlightFanout] and [ChannelRouter.SetMaxInFlightFanout].
	fanoutInFlight    atomic.Int64
	maxInFlightFanout int

	// activityMu guards channelActivity: channel_id → (participant_id → marked-at).
	// The router's in-flight "thinking" set powers the RFC 0048 console presence
	// Tier 1 read (`GET /channels/{id}/activity`). activityNow is the clock used
	// for the TTL prune, overridable in tests. See activity.go for the lifecycle.
	activityMu      sync.Mutex
	channelActivity map[string]map[string]time.Time
	activityNow     func() time.Time

	// interactionMu guards the RFC 0030 interaction-id producer state; methods +
	// the full field contracts live in interaction_resolver.go.
	// openInteractions: channel id → the open interaction + its pending retiree.
	// interactionIdleTimeouts: channel id → resolved idle window (absent falls
	// back to defaultInteractionIdleTimeout). interactionNow: the resolver
	// clock, overridable in tests (the activityNow pattern).
	interactionMu                 sync.Mutex
	openInteractions              map[string]*openInteraction
	interactionIdleTimeouts       map[string]time.Duration
	defaultInteractionIdleTimeout time.Duration
	interactionNow                func() time.Time
	// draining flags an in-progress [ChannelRouter.DrainPendingFanout]. Guarded
	// by interactionMu — the SAME lock the synthesis arm CAS runs under
	// ([ChannelRouter.maybeArmSynthesisClose]), which is what makes the drain's
	// disarm sweep final: an arm serialized after the flag is set refuses and
	// degrades to the immediate close, so no timer (and no synthesisWG.Add) can
	// appear behind the sweep (PR #718 follow-up review; ordering story in
	// router_publish_async.go).
	draining bool

	// escalationMu guards escalationChairs — the per-channel
	// `escalation_chair_id` knob (the chair-stall-escalation amendment, CE2);
	// methods + the full contract live in chair_escalation.go.
	escalationMu     sync.Mutex
	escalationChairs map[string]string

	// reasoningMu guards reasoning — the resolved RFC 0051 (v0.3.10) per-channel
	// reasoning-before-posting block, keyed by channel id. Populated via
	// [ChannelRouter.SetReasoning]; methods + the resolver live in
	// router_reasoning.go. An absent channel resolves to [DefaultReasoningConfig].
	reasoningMu sync.Mutex
	reasoning   map[string]ReasoningConfig

	// autonomousMu guards autonomous — the resolved RFC 0052 (v0.3.11) per-channel
	// autonomous-discussion block, keyed by channel id. Populated via
	// [ChannelRouter.SetAutonomous]; methods + the resolver live in
	// router_autonomous.go. An absent channel resolves to [DefaultAutonomousConfig].
	// An RWMutex, unlike the sibling knob mutexes: see [ChannelRouter.AutonomousFor].
	autonomousMu sync.RWMutex
	autonomous   map[string]AutonomousConfig

	// conveningMu guards convenings — the RFC 0052 §E (v0.3.11 PR 7b) per-channel
	// aggregate convening count, keyed by channel id. Incremented on each
	// SUCCESSFUL [ChannelRouter.ConveneChannel] and consulted against the resolved
	// `autonomous.max_convenings`; the count bound the config gate (PR 7a) only
	// required be DECLARED becomes a live ceiling here. Process-lifetime state (a
	// restart resets it, like the wallet's per-interaction accounting); cleared on
	// channel delete ([ChannelRouter.PurgeChannelInteraction]). Its own mutex —
	// never held across the dispatch RPC — so a convening reservation on one
	// channel never blocks traffic on another. Methods live in
	// convening_counter.go.
	conveningMu sync.Mutex
	convenings  map[string]int

	// applyMu serializes the RFC 0050 Phase 1 PR 2 store-config apply path
	// ([ChannelRouter.ApplyChannelConfig]). It is NOT a per-knob lock — each knob
	// already has its own setter mutex above. It exists to make the persist →
	// re-read → stamp sequence atomic as a whole: PR 1's optimistic-concurrency
	// CAS serializes the STORE write, but without this lock two concurrent applies
	// could each re-read and stamp in an order that leaves the live router on a
	// SUPERSEDED override (a slow apply stamping its now-stale snapshot after a
	// newer one committed), so the router would silently diverge from the
	// canonical store until the next restart. Taken only on the (infrequent) apply
	// path, never on the publish hot path. The boot repoint
	// ([ChannelRouter.ResolveFromStore]) runs single-threaded before traffic and
	// does not take it.
	applyMu sync.Mutex
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
		store:                         store,
		dispatcher:                    dispatcher,
		logger:                        logger,
		metrics:                       metrics,
		waiter:                        newReplyWaiter(),
		floors:                        newFloorRegistry(),
		floorSettings:                 make(map[string]channelFloorSettings),
		floorSpeakers:                 make(map[string]map[string]struct{}),
		salienceMaxMembers:            make(map[string]int),
		replyBudgets:                  make(map[string]int),
		replyCounts:                   make(map[string]map[string]int),
		channelBudgets:                make(map[string]int64),
		interactionBudgetSnapshots:    make(map[string]int64),
		endVoteThresholds:             make(map[string]int),
		endVoteWindows:                make(map[string]int),
		endVotes:                      make(map[string]*interactionEndVotes),
		closedInteractions:            make(map[string]struct{}),
		maxCascadeDepth:               defaults.DefaultMaxCascadeDepth,
		maxInFlightFanout:             defaultMaxInFlightFanout,
		channelActivity:               make(map[string]map[string]time.Time),
		activityNow:                   time.Now,
		openInteractions:              make(map[string]*openInteraction),
		interactionIdleTimeouts:       make(map[string]time.Duration),
		defaultInteractionIdleTimeout: time.Duration(DefaultInteractionIdleTimeoutSeconds) * time.Second,
		interactionNow:                time.Now,
		escalationChairs:              make(map[string]string),
		reasoning:                     make(map[string]ReasoningConfig),
		autonomous:                    make(map[string]AutonomousConfig),
		convenings:                    make(map[string]int),
		synthesisTimeout:              defaultSynthesisReplyTimeout,
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
