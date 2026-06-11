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

	// Disposition chain (§C 1) — every branch from here emits the metric.
	chairID := r.escalationChairFor(msg.ChannelID)
	if chairID == "" {
		r.recordChairEscalation(ctx, ct, chairEscalationNoChair)
		return
	}
	if escalated || !r.markChairEscalated(msg.ChannelID, interactionID) {
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
	// (the stamped interaction_id rides along for lease attribution, CE6).
	forced := msg
	forced.ID = uuid.NewString()
	dispatchCtx, cancel := context.WithTimeout(context.WithoutCancel(ctx), channelFanoutPerRecipientTimeout)
	defer cancel()
	err := r.dispatcher.Dispatch(dispatchCtx, DispatchEnvelope{
		Recipient:                 *chair,
		ChannelSize:               channelSize,
		SalienceMaxChannelMembers: r.salienceMaxFor(msg.ChannelID),
		FloorMentions:             floorMentions,
		ChairEscalation:           true,
	}, forced)
	if err != nil {
		r.logger.Warn("channels: chair escalation dispatch failed; stall stands",
			zap.String("channel_id", msg.ChannelID),
			zap.String("escalation_chair_id", chairID),
			zap.Error(err))
		r.recordChairEscalation(ctx, ct, chairEscalationDispatchError)
		return
	}
	r.logger.Info("channels: stalled round escalated to chair",
		zap.String("channel_id", msg.ChannelID),
		zap.String("interaction_id", interactionID),
		zap.String("escalation_chair_id", chairID))
	r.recordChairEscalation(ctx, ct, chairEscalationDispatched)
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
