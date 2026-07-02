package channels

// chair_escalation.go — the chair-stall-escalation amendment (RFC 0030
// minimal Layer 5 slice, docs/rfcs/0030-amendment-chair-stall-escalation.md),
// orchestrator half. A floor round that ends with zero replies across ≥1
// granted turns, on a channel whose open interaction is tracked, is the
// STALL — the state the convergence review identified as the remaining
// silent-death mode: every Tier B bid honestly passed on an unresolved
// question, so no publish can ever carry the Layer 4 vote that would close
// it. Detection is deterministic and free (CE1 — the round outcome already
// encodes it); the response is ONE directed forced turn to the channel's
// configured `escalation_chair_id` (CE2/CE3), after which the chair
// proposes and the quorum disposes (CE4 — no new close path).
//
// Everything here runs at the floor round's tail in [ChannelRouter.fanout],
// AFTER the floor is released and the round's floor-speaker set is cleared
// (§C 1): the chair's reply must re-fanout as a fresh open-floor stimulus —
// inside the round it would be recognised as a floor turn and suppressed
// from re-fanout, stranding the synthesis in history. The dispatch is a
// send, never an await (CE7): no reply waiter, no turn timeout, and every
// degraded branch nets to the status quo the stall already was.

import (
	"context"
	"maps"

	"github.com/google/uuid"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/metric"
	"go.uber.org/zap"
)

// chair_escalation{outcome} attribute values (§C 1): every DETECTED stall
// emits exactly one of these — the disposition checks happen after
// detection, not as preconditions of the metric, so the counter surfaces
// stalls an operator could configure a chair for.
const (
	chairEscalationDispatched       = "dispatched"
	chairEscalationNoChair          = "no_chair"
	chairEscalationAlreadyEscalated = "already_escalated"
	chairEscalationDispatchError    = "dispatch_error"
	// chairEscalationSelfStimulus (PR #609 deep review): the chair authored
	// the stalled stimulus itself, so the forced turn is withheld — see the
	// guard in [ChannelRouter.maybeEscalateStall] for why dispatching it is
	// guaranteed-futile and why the ration stays unspent.
	chairEscalationSelfStimulus = "self_stimulus"
	// chairEscalationResynthesized (ISSUE-0099) is the one synthesize-only
	// re-dispatch [ChannelRouter.dispatchResynthesizeMisfire] sends after the
	// chair's first forced-turn reply provably reached no floor-capable
	// member. Unlike the stall dispositions above it is NOT a stall count — it
	// fires at the chair-reply publish seam, not the round tail — so it shares
	// the instrument as a distinct lifecycle label.
	chairEscalationResynthesized = "resynthesized"
	// chairEscalationResynthesizeError (ISSUE-0099) is the resynthesize
	// re-dispatch's OWN failure — chair drift or a dispatch error at the
	// publish seam. It is deliberately NOT chairEscalationDispatchError:
	// reusing that would fold a publish-seam failure into a counter documented
	// as "one per DETECTED floor-round stall", so summing dispatch_error would
	// no longer equal the round-tail stall errors. A distinct label keeps both
	// publish-seam outcomes ({resynthesized, resynthesize_error}) off the stall
	// dispositions.
	chairEscalationResynthesizeError = "resynthesize_error"
)

// floorRoundOutcome is what [ChannelRouter.floorRound] now hands back to its
// caller: the per-turn tallies it already records as `floor_turn{outcome}`
// telemetry, aggregated so the fanout tail can run CE1's stall test. A round
// with `granted == 0` is vacuously silent, not stalled (nobody was asked
// anything); `replied > 0` is a working conversation.
type floorRoundOutcome struct {
	granted int
	replied int
}

// SetEscalationChair resolves the per-channel `escalation_chair_id` knob
// (CE2). Empty unsets it — the opt-in default, no escalation. The agent id
// is the *participant* receiving the forced turn; it closes nothing (CE4)
// and need not be declared `chair` (chair-ness does not survive persistence
// — the canonicalization amendment's encoding rule — which is why this is a
// knob and not an inference).
func (r *ChannelRouter) SetEscalationChair(channelID, agentID string) {
	r.escalationMu.Lock()
	defer r.escalationMu.Unlock()
	if agentID == "" {
		delete(r.escalationChairs, channelID)
		return
	}
	r.escalationChairs[channelID] = agentID
}

// escalationChairFor returns the channel's configured escalation chair, ""
// when unset.
func (r *ChannelRouter) escalationChairFor(channelID string) string {
	r.escalationMu.Lock()
	defer r.escalationMu.Unlock()
	return r.escalationChairs[channelID]
}

// EscalationChairFor reports the channel's resolved escalation chair ("" when
// no chair is set) and whether an explicit entry exists. Exposed for ops
// introspection and the RFC 0050 `GET …/config` effective-value read, mirroring
// [ChannelRouter.FloorControlFor]; the hot path reads the unexported
// [ChannelRouter.escalationChairFor]. `set` is redundant with a non-empty chair
// today (the unset state is the empty map entry), but is returned for symmetry
// with the other `*For` getters so the config read can label provenance without
// special-casing this knob.
func (r *ChannelRouter) EscalationChairFor(channelID string) (chair string, set bool) {
	r.escalationMu.Lock()
	defer r.escalationMu.Unlock()
	chair, set = r.escalationChairs[channelID]
	return chair, set
}

// ResolveEscalationChairs applies the per-channel escalation chairs for
// every config-declared channel at startup, the sibling of
// [ChannelRouter.ResolveEndVotes]. Absent knob = unset = no escalation;
// store-resident channels not in config are simply never escalated, so
// there is no store enumeration. Idempotent; call once after
// ReconcileConfig. Member-existence is validated at config load
// ([Config.Validate]), so a bad id never reaches here from config.
func (r *ChannelRouter) ResolveEscalationChairs(_ context.Context, cfg *Config) error {
	if cfg == nil {
		return nil
	}
	for _, decl := range cfg.Channels {
		if decl.EscalationChairID != "" {
			r.SetEscalationChair(decl.CanonicalID(), decl.EscalationChairID)
		}
	}
	return nil
}

// maybeEscalateStall is the floor round's tail hook (CE1–CE7): detect the
// stall, run the disposition chain, and — on the dispatched branch — mark
// the interaction's ration (CE5) and send the forced turn. `members` is the
// channel membership the fanout already loaded (the chair's envelope needs
// its row); `floorMentions` rides through so the forced turn carries the
// same Tier A basis as the stimulus dispatch did.
func (r *ChannelRouter) maybeEscalateStall(
	ctx context.Context,
	msg ChannelMessage,
	ct ChannelType,
	threadParentSenderID string,
	outcome floorRoundOutcome,
	members []Member,
	channelSize int,
	floorMentions []string,
) {
	// CE1 — detection. Zero granted turns is not a stall; any reply is a
	// working conversation; an untracked channel (resolver bypass) has no
	// interaction to escalate.
	if outcome.granted == 0 || outcome.replied > 0 {
		return
	}
	interactionID, escalated, tracked := r.openInteractionEscalationState(msg.ChannelID)
	if !tracked {
		return
	}
	// Detection, continued (PR #609 deep review): the stall must belong to
	// the interaction that is still open. publishCommit stamped the resolved
	// id onto the stimulus metadata, and a serialized round runs up to
	// N×turnTimeout while rotation/close happen lazily on CONCURRENT
	// publishes — so the open id read above can be a different generation
	// than the one this round stalled on. Escalating then would spend the
	// successor's ration on the predecessor's silence and dispatch a forced
	// turn whose stamped interaction_id disagrees with the ration's — and
	// the divergence itself proves new traffic arrived mid-round, so the
	// channel is not stalled. Fails CE1 ("open tracked interaction") like
	// the untracked branch: no metric (a non-stall on the counter would
	// pollute it) — but it IS the chain's one silently-dropped branch, so
	// the divergence goes on debug record for whoever chases a "stall that
	// didn't escalate". An absent stamp falls through on readInteractionID's
	// tolerant-reader posture (the router stamps every routed publish; only
	// a commit-bypassing direct call lacks it).
	if stamped := readInteractionID(msg.Metadata); stamped != "" && stamped != interactionID {
		r.logger.Debug("channels: stalled round outlived its interaction; not escalating",
			zap.String("channel_id", msg.ChannelID),
			zap.String("stamped_interaction_id", stamped),
			zap.String("open_interaction_id", interactionID))
		return
	}

	// Disposition chain (§C 1) — every branch from here emits the metric.
	chairID := r.escalationChairFor(msg.ChannelID)
	if chairID == "" {
		r.recordChairEscalation(ctx, ct, chairEscalationNoChair)
		return
	}
	if escalated {
		r.recordChairEscalation(ctx, ct, chairEscalationAlreadyEscalated)
		return
	}
	if chairID == msg.SenderID {
		// The chair authored the stalled stimulus (PR #609 deep review).
		// Every dispatch path filters the sender ([orderResponders]'
		// never-reply-to-self, [dispatchConcurrent]'s sender skip), and the
		// receiver gate's self_sender defence (agents/response_gate.py)
		// suppresses a self-delivery before any LLM regardless — a forced
		// turn here is guaranteed-suppressed, and dispatching it would burn
		// the interaction's one ration while recording `dispatched`.
		// Withhold WITHOUT spending the ration: unlike the membership-drift
		// branch below this is per-stimulus, not persistent, so a later
		// stall on another member's stimulus still deserves the escalation.
		r.recordChairEscalation(ctx, ct, chairEscalationSelfStimulus)
		return
	}
	if !r.markChairEscalated(msg.ChannelID, interactionID) {
		// The CAS re-check covers a concurrent stalled round racing this one
		// to the same ration; both observing `escalated == false` is fine —
		// exactly one mark wins.
		r.recordChairEscalation(ctx, ct, chairEscalationAlreadyEscalated)
		return
	}

	var chair *Member
	for i := range members {
		if members[i].ParticipantID == chairID {
			chair = &members[i]
			break
		}
	}
	if chair == nil {
		// Config load validates membership, so this is runtime drift (the
		// chair left the channel after startup). The ration is spent — CE5
		// deliberately does not refund a failed escalation (one attempt per
		// interaction keeps the loop guard simple); idle rotation nets it.
		r.logger.Warn("channels: escalation chair is not a member; stall stands",
			zap.String("channel_id", msg.ChannelID),
			zap.String("escalation_chair_id", chairID))
		r.recordChairEscalation(ctx, ct, chairEscalationDispatchError)
		return
	}

	// CE3 — the forced turn: the stalled stimulus under a FRESH event id
	// (the agent-side conversation window dedups by message id, so the
	// original id would be silently dropped), same content and metadata
	// (the stamped interaction_id rides along for lease attribution, CE6;
	// the bag is CLONED — the forced turn outlives the stimulus's round and
	// crosses the dispatcher seam, so aliasing the map would let a future
	// metadata write on either side silently corrupt the other).
	//
	// Delivered via [ChannelRouter.dispatchTo] (PR #609 deep review), the
	// single deadline + `channel.messages.delivered` contract point every
	// other recipient dispatch uses — a bypass made forced turns invisible
	// to delivered/error dashboards. The chair is re-stamped as thinking
	// first: it is an expected-reply dispatch and its round-start mark has
	// typically aged out across the silent round (the same TTL decay
	// [runFloorTurn]'s re-mark exists for; marking after the send instead
	// would let a fast reply's publishCommit clear-then-lose to the mark).
	// Pre-lift agents never reply (the §D skew posture); the TTL prune
	// clears the mark, the same degraded path as any gate-suppressed
	// candidate. A FAILED send is different: no reply can ever clear it, so
	// the error branch unmarks rather than stranding a "thinking" line on
	// the console until the TTL prune.
	forced := msg
	forced.ID = uuid.NewString()
	forced.Metadata = maps.Clone(msg.Metadata)
	r.markActivity(msg.ChannelID, []string{chairID})
	if err := r.dispatchTo(ctx, forced, ct, threadParentSenderID, *chair, channelSize, floorMentions, markerChairEscalation); err != nil {
		r.clearActivity(msg.ChannelID, chairID)
		r.logger.Warn("channels: chair escalation dispatch failed; stall stands",
			zap.String("channel_id", msg.ChannelID),
			zap.String("escalation_chair_id", chairID),
			zap.Error(err))
		r.recordChairEscalation(ctx, ct, chairEscalationDispatchError)
		return
	}
	// ISSUE-0099: stash the stalled stimulus (and the thread-parent this forced
	// turn carried) now that it is on the wire, so a provable misfire of the
	// chair's reply can re-force one synthesize-only turn from the ORIGINAL
	// (non-chair) sender in the same thread context. Stored on the success path
	// only — a withheld or failed escalation has no reply to misfire. CAS-scoped
	// to the escalated id inside the helper.
	r.storeEscalatedStimulus(msg.ChannelID, interactionID, threadParentSenderID, msg)
	r.logger.Info("channels: stalled round escalated to chair",
		zap.String("channel_id", msg.ChannelID),
		zap.String("interaction_id", interactionID),
		zap.String("escalation_chair_id", chairID))
	r.recordChairEscalation(ctx, ct, chairEscalationDispatched)
}

// pendingResynthesize is a claimed-but-not-yet-dispatched ISSUE-0099 re-force:
// the stashed original stimulus, its thread context, and the (chair,
// interaction) generation the claim ran against. Produced by
// [ChannelRouter.claimResynthesizeMisfire] at the fanout HEAD and consumed by
// [ChannelRouter.dispatchResynthesizeMisfire] at the tail — split so the
// once-bound claim keeps the "chair's FIRST publish" ordering while the
// re-force dispatch waits for the RFC 0052 bounded-close outcome (fanout.go).
type pendingResynthesize struct {
	stimulus             ChannelMessage
	threadParentSenderID string
	interactionID        string
	chairID              string
}

// claimResynthesizeMisfire is the ISSUE-0099 trigger's CLAIM half, run at the
// fanout HEAD for every publish (PR 4b-i review round 5 moved it back there).
// It identifies the one escalation failure the orchestrator can PROVE at
// publish time: the escalation chair's own forced-turn reply handed the floor
// to a target that lifts to a real id but cannot take it (a `respond: never`
// observer, the operator, or itself), so the hand-off reached nobody. The fix
// is option 2 (re-dispatch one synthesize-only forced turn) rather than option
// 1 (refund the ration): a refund is inert here because the misfired reply is
// itself chair-authored, so the refunded re-escalation would trip
// [ChannelRouter.maybeEscalateStall]'s self_stimulus guard and never fire.
//
// The trigger is the chair's FIRST publish after the forced turn — its
// forced-turn reply — identified by consuming the stash that
// [ChannelRouter.maybeEscalateStall] armed ([ChannelRouter.claimChairReply]).
// That consumption happens whether or not the reply misfired: a clean hand-off
// disarms the trigger so a LATER innocuous chair message naming no floor-capable
// member (an "@alex, thanks") cannot be mistaken for the reply's misfire and
// re-inject a now-stale stimulus. The head position is what keeps "FIRST
// publish" honest: claiming at the fanout TAIL instead (the round-4 shape) let
// two chair publishes on different path shapes invert by a whole multi-turn
// floor round — the real forced-turn reply parked in its round while a later
// innocuous chair message raced down the fast concurrent path, reached its
// tail first, and claimed the arm with ITS misfire flag — exactly the false
// positive the claim-on-first-reply contract exists to prevent. The head claim
// shrinks that window back to detached-goroutine spawn jitter (strict commit
// order is not literally guaranteed under PublishAsync), the pre-4b-i posture.
// Only the re-force DISPATCH waits for the tail, gated on the bounded-close
// outcome.
//
// Returns nil unless the claim succeeded AND the reply misfired — i.e. a
// re-force is actually owed. A no-op (nil) for every non-chair publish.
func (r *ChannelRouter) claimResynthesizeMisfire(msg ChannelMessage, misfired bool) *pendingResynthesize {
	// Only the escalation chair's OWN reply can misfire a hand-off. Every other
	// publish (a non-chair member, or the chair outside an escalated
	// interaction) is not an escalation failure and must not re-force.
	chairID := r.escalationChairFor(msg.ChannelID)
	if chairID == "" || msg.SenderID != chairID {
		return nil
	}
	interactionID, escalated, tracked := r.openInteractionEscalationState(msg.ChannelID)
	if !tracked || !escalated {
		return nil
	}
	// Same divergence guard as the stall tail: a reply that outlived its
	// interaction (a concurrent rotation/close moved the open id on) must not
	// consume or re-force the successor's ration. Absent stamp falls through on
	// the tolerant-reader posture.
	if stamped := readInteractionID(msg.Metadata); stamped != "" && stamped != interactionID {
		r.logger.Debug("channels: chair reply outlived its interaction; not resynthesizing",
			zap.String("channel_id", msg.ChannelID),
			zap.String("stamped_interaction_id", stamped),
			zap.String("open_interaction_id", interactionID))
		return nil
	}
	stimulus, threadParentSenderID, ok := r.claimChairReply(msg.ChannelID, interactionID)
	if !ok {
		// Not the armed reply: no stash, a rotation between read and claim, or
		// the forced-turn reply was already consumed (a clean hand-off, or a
		// prior misfire's re-dispatch — the once-bound). A later chair publish
		// is an ordinary message, not the reply's misfire. No metric.
		return nil
	}
	if !misfired {
		// The forced-turn reply took or handed the floor cleanly (it named a
		// floor-capable member, or opened the floor with no mention). Consuming
		// the stash above is the whole job — it disarms the trigger so no later
		// chair message is mistaken for a misfire. Nothing to recover, no metric.
		return nil
	}
	return &pendingResynthesize{
		stimulus:             stimulus,
		threadParentSenderID: threadParentSenderID,
		interactionID:        interactionID,
		chairID:              chairID,
	}
}

// dispatchResynthesizeMisfire is the ISSUE-0099 trigger's DISPATCH half, run at
// the fanout tail with the [pendingResynthesize] the head claimed — after the
// RFC 0052 bounded close has decided the round's fate (fanout.go gates this
// call on "not closed"; a bounding round drops the pending re-force silently,
// since the arm died with the retired interaction and a re-forced reply would
// mint a fresh one and reopen the just-closed discussion).
//
// The re-dispatch reuses the STASHED original stimulus and its thread parent,
// not the chair's reply — the stash carries a non-chair sender, so the gate
// admits it (re-sending the chair's own reply to the chair self-suppresses).
// Once per interaction, since the claim consumed the stash; a send, never an
// await, like the stall tail.
func (r *ChannelRouter) dispatchResynthesizeMisfire(
	ctx context.Context,
	msg ChannelMessage,
	ct ChannelType,
	members []Member,
	channelSize int,
	pending *pendingResynthesize,
) {
	chairID := pending.chairID
	var chair *Member
	for i := range members {
		if members[i].ParticipantID == chairID {
			chair = &members[i]
			break
		}
	}
	if chair == nil {
		// Runtime drift (the chair left after the first escalation). The
		// re-dispatch is spent — like the stall tail's chair-gone branch, the
		// consumed stash does not refund — and the stall stands. Labelled
		// resynthesize_error, a publish-seam failure distinct from the round-tail
		// dispatch_error.
		r.logger.Warn("channels: resynthesize chair is not a member; misfire stands",
			zap.String("channel_id", msg.ChannelID),
			zap.String("escalation_chair_id", chairID))
		r.recordChairEscalation(ctx, ct, chairEscalationResynthesizeError)
		return
	}

	// The second forced turn: the STASHED original stimulus under a fresh
	// event id and cloned metadata (the stamped interaction_id rides along for
	// lease attribution), in the stimulus's own thread context, stamped
	// resynthesize so the chair renders the synthesize-only framing. No floor
	// mentions — the chair is admitted by the escalation lift, not by
	// directedness. Marked thinking before the send; a failed send unmarks (no
	// reply will ever clear it), mirroring the stall tail.
	forced := pending.stimulus
	forced.ID = uuid.NewString()
	r.markActivity(msg.ChannelID, []string{chairID})
	if err := r.dispatchTo(ctx, forced, ct, pending.threadParentSenderID, *chair, channelSize, nil, markerChairEscalationResynthesize); err != nil {
		r.clearActivity(msg.ChannelID, chairID)
		r.logger.Warn("channels: chair resynthesize dispatch failed; misfire stands",
			zap.String("channel_id", msg.ChannelID),
			zap.String("escalation_chair_id", chairID),
			zap.Error(err))
		r.recordChairEscalation(ctx, ct, chairEscalationResynthesizeError)
		return
	}
	r.logger.Info("channels: chair hand-off misfired; re-forced a synthesize-only turn",
		zap.String("channel_id", msg.ChannelID),
		zap.String("interaction_id", pending.interactionID),
		zap.String("escalation_chair_id", chairID))
	r.recordChairEscalation(ctx, ct, chairEscalationResynthesized)
}

// recordChairEscalation emits `channel.conversation.chair_escalation
// {channel_type, outcome}` — one increment per DETECTED stall, labelled with
// its disposition. Nil-safe like every other channel instrument.
func (r *ChannelRouter) recordChairEscalation(ctx context.Context, ct ChannelType, outcome string) {
	if r.metrics == nil || r.metrics.ChairEscalation == nil {
		return
	}
	r.metrics.ChairEscalation.Add(ctx, 1, metric.WithAttributes(
		attribute.String("channel_type", string(ct)),
		attribute.String("outcome", outcome),
	))
}
