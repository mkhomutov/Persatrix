package channels

// synthesis_close.go — RFC 0052 §D goal-directed chair synthesis turn
// (v0.3.11 PR 4b-ii), orchestrator half: the CLOSE-ON-REPLY ordering around the
// PR 4b-i deterministic bounded close.
//
// 4b-i closed a bound-crossing autonomous interaction immediately, deferring
// §D artifact #1 — the chair's synthesis turn against `autonomous.goal` —
// because dispatching a re-fanning turn around the close would let the chair's
// reply mint a FRESH interaction and REOPEN the discussion (the runaway the
// no-reopen latch exists to stop). The safe ordering needs the reply
// recognised as the closing artifact, and 4b-i's review round 8 laid the rail:
// every agent reply now echoes its dispatched-under interaction id as the wire
// claim. So the flow here is:
//
//  1. A bound-crossing round on a CHAIRED channel does not close yet — it ARMS
//     a [pendingSynthesisClose] on the resolver entry and dispatches one
//     directed synthesis forced turn to the escalation chair (the convene
//     dispatch shape: synthetic sender, marked lane, operator goal in the
//     content, the closing interaction id stamped as metadata so the reply
//     claims it).
//  2. While armed, ALL discussion traffic is withheld (the fanout's stale
//     posture — committed history, no dispatch, no revival tails): the
//     discussion has terminated; only the closing artifact is outstanding.
//  3. The chair's claimed reply is intercepted at the fanout HEAD
//     ([ChannelRouter.claimSynthesisReply]) and handed to the 4b-i teardown as
//     the CLOSING MESSAGE: the close-notification fan delivers the synthesis
//     to every member (sole delivery — redelivery=false) with the truthful
//     structural/cost trigger, so it lands as each record's final turn and the
//     metered RFC 0020 summaries follow. The chair proposes, the ORCHESTRATOR
//     disposes — CE4 intact.
//  4. The TIMEOUT NET ([ChannelRouter.onSynthesisTimeout]): if the reply never
//     arrives (gate-suppressed after runtime drift, a CE6 lease denial at the
//     hard cap, a provider error, a Layer 2 reply-budget drop of the chair's
//     publish), the timer runs the 4b-i immediate teardown with the ORIGINAL
//     bounding stimulus — deterministic termination never waits on an LLM
//     reply. On the floor path that stimulus was already delivered live, so
//     the fallback notification is stamped redelivery=true and receivers skip
//     the duplicate ingest (close_notification.py).
//
// A racing END-VOTE quorum keeps its supremacy: it closes through the same
// single-shot tombstone CAS, [ChannelRouter.markInteractionClosed] disarms the
// pending synthesis, and the late synthesis reply lands in the no-reopen latch
// as the closed record's tail — degraded to the 4b-i artifact shape, never a
// reopen. A missing/drifted chair or a failed dispatch degrades the same way,
// immediately.
//
// KNOWN LIMITS (accepted, OQ #5 calibration territory): the chair's reply is
// an ordinary leased publish, so the Layer 2 reply budget and the hard cap can
// still deny it (the reserve's KNOWN GAP #2) — the timeout net bounds both,
// trading the artifact for termination. On the concurrent path the WITHHELD
// bounding stimulus is committed history that reaches no member's record when
// the synthesis becomes the closing message (the 4b-i stale-sibling posture;
// the synthesis supersedes it as the record's final turn).

import (
	"context"
	"fmt"
	"strings"
	"time"

	"github.com/google/uuid"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/metric"
	"go.uber.org/zap"
)

// SynthesisDispatchSenderID is the synthetic sender stamped on the synthesis
// directive — the [ConveneDispatchSenderID] rule: the `:` is forbidden in
// participant ids ([ValidateParticipantID]), so the sentinel can never equal a
// real agent id and the receiver gate's self-sender defence can never suppress
// the directive (were the sender the chair's own id, the one mandatory §D turn
// would silently self-suppress — the invisible 202-then-nothing failure the
// convene sentinel exists to prevent). Transient control, never persisted.
const SynthesisDispatchSenderID = "orchestrator:synthesis"

// defaultSynthesisReplyTimeout bounds how long an armed close waits for the
// chair's synthesis reply before the timeout net closes without it. Sized over
// the chair's worst realistic turn — the 30s persona event timeout, up to two
// RFC 0051 reflexion rounds on a `plan` channel, and dispatch/queue jitter —
// so a healthy chair virtually never loses its artifact to the net, while an
// unattended channel is never left open more than ~2 minutes past its bound.
// Calibration belongs to the OQ #5 tracked issue alongside the reserve sizing.
const defaultSynthesisReplyTimeout = 120 * time.Second

// Synthesis-turn lifecycle outcomes on `channel.conversation.synthesis_turn
// {channel_type, outcome}`. `dispatched` fires once per armed close;
// `chair_missing`/`dispatch_error` label the degraded-to-immediate-close
// branches; exactly one of `closed_on_reply`/`closed_on_timeout` follows a
// `dispatched` (a racing end-vote close can orphan the arm, in which case
// neither fires — the close is counted on interaction_closed{end_votes}).
const (
	synthesisTurnDispatched      = "dispatched"
	synthesisTurnChairMissing    = "chair_missing"
	synthesisTurnDispatchError   = "dispatch_error"
	synthesisTurnClosedOnReply   = "closed_on_reply"
	synthesisTurnClosedOnTimeout = "closed_on_timeout"
)

// pendingSynthesisClose is one armed close-on-reply: the bound has fired, the
// synthesis turn is on the wire, and the interaction closes when the chair's
// claimed reply lands (or the timer fires). Rides the resolver entry
// ([openInteraction.pendingSynthesis], guarded by interactionMu) so it dies
// with the interaction generation, the chairEscalated/roundCount lifetime.
type pendingSynthesisClose struct {
	interactionID string
	trigger       string // structural | cost — the bound that fired
	chairID       string
	ct            ChannelType
	// stimulus is the bounding message, stashed for the timeout net's 4b-i
	// teardown (the reply path closes with the reply instead).
	stimulus ChannelMessage
	// stimulusDelivered records whether the bounding stimulus reached members
	// live (the floor path ran its round before the tail; the concurrent path
	// withholds) — it becomes the fallback notification's redelivery marker.
	stimulusDelivered bool
	timer             *time.Timer
}

// synthesisArmOutcome is [ChannelRouter.maybeArmSynthesisClose]'s verdict.
type synthesisArmOutcome uint8

const (
	// synthesisArmed — the turn is dispatched and the close now lands on the
	// chair's reply or the timeout. The caller withholds its dispatch.
	synthesisArmed synthesisArmOutcome = iota
	// synthesisAlreadyArmed — a sibling bound-crossing fanout armed first
	// (both advanced the tally before either armed). Withhold, dispatch
	// nothing, close nothing: the winner's reply/timer owns the close.
	synthesisAlreadyArmed
	// synthesisEntryMovedOn — the resolver entry rotated or was closed between
	// the bound's tally advance and this arm (a racing end-vote close, or a
	// benign idle rotation). The caller falls THROUGH to the immediate
	// boundedClose, whose tombstone CAS separates the two: it LOSES to a
	// deliberate close (the caller then reports stale) and WINS a benign
	// rotation (delivering `msg` as the close). Reporting synthesisAlreadyArmed
	// here instead would map to the deliberate-close withhold and silently
	// swallow a live committed message on a benign rotation — the rotated id
	// never entered the no-reopen ledger, so nothing else would ever account
	// for it (PR 4b-i review finding 8; PR #718 review).
	synthesisEntryMovedOn
	// synthesisUnavailable — no viable chair or the dispatch failed; the
	// caller falls back to the 4b-i immediate artifact-bearing close.
	synthesisUnavailable
)

// composeSynthesisDirective assembles the operator goal/topic into the
// directive the chair synthesizes against — [composeConveneDirective]'s close
// sibling, same trust story (plain assembly; the RFC 0009 envelope is applied
// receiver-side, agents/persona_runtime/synthesis_turn.py). The goal leads:
// §D judges the synthesis against it. Never empty — a channel armed under the
// PR 1 contract can carry agenda-only config, and the chair's turn is the one
// mandatory §D artifact, so a bare config degrades to a generic
// synthesize-the-outcome instruction rather than an empty envelope.
func composeSynthesisDirective(a AutonomousConfig) string {
	var b strings.Builder
	if goal := strings.TrimSpace(a.Goal); goal != "" {
		fmt.Fprintf(&b, "Goal: %s\n", goal)
	}
	if topic := strings.TrimSpace(a.Topic); topic != "" {
		fmt.Fprintf(&b, "\nTopic: %s\n", topic)
	}
	out := strings.TrimSpace(b.String())
	if out == "" {
		out = "No goal was configured for this discussion; synthesize the outcome of the conversation so far."
	}
	// The convene wire ceiling, shared rune-safe clamp ([clampDirectiveBytes]).
	return clampDirectiveBytes(out)
}

// maybeArmSynthesisClose is the bound-crossing branch of
// [ChannelRouter.maybeBoundedClose] on a chaired channel: resolve the chair,
// arm the [pendingSynthesisClose] on the resolver entry, dispatch the directed
// synthesis turn, and start the timeout net. Every degraded branch returns
// [synthesisUnavailable] so the caller's 4b-i immediate close keeps
// termination deterministic — this function can delay a close, never lose one.
func (r *ChannelRouter) maybeArmSynthesisClose(
	ctx context.Context,
	msg ChannelMessage,
	ct ChannelType,
	members []Member,
	channelSize int,
	interactionID, trigger string,
	stimulusDelivered bool,
	a AutonomousConfig,
) synthesisArmOutcome {
	chairID := r.escalationChairFor(msg.ChannelID)
	if chairID == "" {
		// PR 4a's validate gate makes a chair mandatory on an armed channel,
		// so this is runtime drift (the knob cleared after arming) or a
		// pre-gate stored blob — close immediately rather than wait on a turn
		// nobody will take.
		r.recordSynthesisTurn(ctx, ct, synthesisTurnChairMissing)
		return synthesisUnavailable
	}
	chair := memberByID(members, chairID)
	if chair == nil || chair.RespondPolicy.Normalize() == RespondNever {
		// Membership drift (the chair left, or turned observer) — the
		// stall-escalation chair-gone posture: the directive would land in a
		// gate suppression, so the §D artifact is unreachable and the
		// immediate close stands.
		r.logger.Warn("channels: synthesis chair is not a dispatchable member; closing without the synthesis turn",
			zap.String("channel_id", msg.ChannelID),
			zap.String("escalation_chair_id", chairID))
		r.recordSynthesisTurn(ctx, ct, synthesisTurnChairMissing)
		return synthesisUnavailable
	}

	pending := &pendingSynthesisClose{
		interactionID:     interactionID,
		trigger:           trigger,
		chairID:           chairID,
		ct:                ct,
		stimulus:          msg,
		stimulusDelivered: stimulusDelivered,
	}
	// Arm under interactionMu — the CAS half: two sibling bound-crossing
	// fanouts can both pass the tally advance before either arms; exactly one
	// may dispatch the turn (the once-per-interaction contract), the loser
	// just withholds.
	r.interactionMu.Lock()
	entry := r.openInteractions[msg.ChannelID]
	if entry == nil || entry.id != interactionID {
		// The interaction moved on between the tally advance and this arm (a
		// racing end-vote close, or a benign idle rotation). Do NOT report
		// already-armed — that withholds the message as deliberately-closed
		// traffic and loses it on a benign rotation. Fall through to the
		// caller's immediate close so the tombstone CAS decides (see
		// [synthesisEntryMovedOn]).
		r.interactionMu.Unlock()
		return synthesisEntryMovedOn
	}
	if entry.pendingSynthesis != nil {
		r.interactionMu.Unlock()
		return synthesisAlreadyArmed
	}
	entry.pendingSynthesis = pending
	r.interactionMu.Unlock()

	// The directive: the convene dispatch shape — synthetic sender (see
	// [SynthesisDispatchSenderID]), operator goal in the content, and the
	// CLOSING interaction id stamped as the wire claim so the chair's reply
	// echoes it back (the 4b-i origin pair) and the head claim recognises the
	// closing artifact. Dispatched outside the lock, like every dispatch.
	directive := ChannelMessage{
		ID:        uuid.NewString(),
		ChannelID: msg.ChannelID,
		SenderID:  SynthesisDispatchSenderID,
		Content:   composeSynthesisDirective(a),
		Metadata:  map[string]any{interactionIDMetadataKey: interactionID},
	}
	r.markActivity(msg.ChannelID, []string{chairID})
	if err := r.dispatchTo(ctx, directive, ct, "", *chair, channelSize, nil, dispatchControl{marker: markerSynthesisTurn}); err != nil {
		r.clearActivity(msg.ChannelID, chairID)
		r.disarmSynthesis(msg.ChannelID, pending)
		r.logger.Warn("channels: synthesis turn dispatch failed; closing without it",
			zap.String("channel_id", msg.ChannelID),
			zap.String("escalation_chair_id", chairID),
			zap.Error(err))
		r.recordSynthesisTurn(ctx, ct, synthesisTurnDispatchError)
		return synthesisUnavailable
	}
	// Start the timeout net only while the arm still stands — a racing close
	// (end-vote quorum landing mid-dispatch) already disarmed via
	// [ChannelRouter.markInteractionClosed], and a timer for a dead arm would
	// fire into its identity check for nothing.
	r.interactionMu.Lock()
	if entry := r.openInteractions[msg.ChannelID]; entry != nil && entry.pendingSynthesis == pending {
		pending.timer = time.AfterFunc(r.synthesisTimeout, func() { r.onSynthesisTimeout(pending) })
	}
	r.interactionMu.Unlock()
	r.logger.Info("channels: bound crossed; synthesis turn dispatched to chair",
		zap.String("channel_id", msg.ChannelID),
		zap.String("interaction_id", interactionID),
		zap.String("trigger", trigger),
		zap.String("escalation_chair_id", chairID))
	r.recordSynthesisTurn(ctx, ct, synthesisTurnDispatched)
	return synthesisArmed
}

// claimSynthesisReply is the fanout-HEAD intercept: when `msg` is the chair's
// reply claiming the armed interaction, consume the arm (stop the timer) and
// return it — the caller closes with `msg` as the closing artifact and skips
// the entire fanout (no round, no concurrent dispatch, no revival tails: a
// fanned synthesis would draw replies into the closed discussion, the reopen
// §D forbids). Anything else — a straggler responder claiming the armed id, an
// unstamped operator publish, a non-chair sender — returns nil and the armed
// withhold in [ChannelRouter.advanceBoundedCloseRound] /
// [ChannelRouter.stimulusOutlivedClose] owns it. Runs before the head
// staleness check; nil for every publish on an unarmed channel.
func (r *ChannelRouter) claimSynthesisReply(msg ChannelMessage, a AutonomousConfig) *pendingSynthesisClose {
	if !a.Enabled {
		return nil // OQ #2: human channels never arm; skip the mutex entirely.
	}
	claim := readInteractionID(msg.Metadata)
	if claim == "" {
		return nil
	}
	r.interactionMu.Lock()
	defer r.interactionMu.Unlock()
	entry := r.openInteractions[msg.ChannelID]
	if entry == nil || entry.pendingSynthesis == nil {
		return nil
	}
	pending := entry.pendingSynthesis
	if msg.SenderID != pending.chairID || claim != pending.interactionID {
		return nil
	}
	if pending.timer != nil {
		pending.timer.Stop()
	}
	entry.pendingSynthesis = nil
	return pending
}

// onSynthesisTimeout is the timeout net: the chair's reply never arrived, so
// run the 4b-i immediate teardown with the stashed bounding stimulus as the
// closing message. The identity CAS under interactionMu makes it a no-op when
// the reply claimed first, a racing close disarmed the pending synthesis, or
// the entry rotated; [ChannelRouter.boundedClose]'s tombstone CAS backstops
// the residual race with a closer that has not yet reached
// markInteractionClosed. Runs on the timer goroutine — recovered like every
// detached channel worker (an unrecovered panic would down the orchestrator).
func (r *ChannelRouter) onSynthesisTimeout(pending *pendingSynthesisClose) {
	defer r.recoverFanout("synthesis_timeout", pending.stimulus.ChannelID, pending.stimulus.ID)
	r.interactionMu.Lock()
	entry := r.openInteractions[pending.stimulus.ChannelID]
	if entry == nil || entry.pendingSynthesis != pending {
		r.interactionMu.Unlock()
		return
	}
	entry.pendingSynthesis = nil
	r.interactionMu.Unlock()

	// This timeout won the identity CAS, so it owns the teardown — and the
	// chair's synthesis reply never arrived (that is WHY the net fired), so
	// nothing re-enters publishCommit to clear the "thinking" mark
	// [ChannelRouter.maybeArmSynthesisClose] set on the chair. Clear it here,
	// mirroring the failed-dispatch unwind's clearActivity at the OTHER no-reply
	// abandon terminal, so a timed-out close does not strand the chair as
	// composing for the activity TTL. A no-op when the mark already aged out (a
	// default timeout past the TTL) — it earns its keep under an OQ #5 timeout
	// calibrated below activityTTL.
	r.clearActivity(pending.stimulus.ChannelID, pending.chairID)

	ctx := context.Background()
	r.logger.Warn("channels: synthesis reply timed out; closing without the synthesis artifact",
		zap.String("channel_id", pending.stimulus.ChannelID),
		zap.String("interaction_id", pending.interactionID),
		zap.String("trigger", pending.trigger),
		zap.String("escalation_chair_id", pending.chairID))
	if r.boundedClose(ctx, pending.stimulus, pending.ct, pending.interactionID, pending.trigger, pending.stimulusDelivered) {
		r.recordSynthesisTurn(ctx, pending.ct, synthesisTurnClosedOnTimeout)
	}
}

// disarmSynthesis clears `pending` off the channel's resolver entry iff it is
// still the armed one — the failed-dispatch unwind (the timer does its own
// inline CAS, and markInteractionClosed owns the racing-close disarm).
func (r *ChannelRouter) disarmSynthesis(channelID string, pending *pendingSynthesisClose) {
	r.interactionMu.Lock()
	if entry := r.openInteractions[channelID]; entry != nil && entry.pendingSynthesis == pending {
		entry.pendingSynthesis = nil
	}
	r.interactionMu.Unlock()
}

// armedSynthesisChair returns the chair id of the channel's armed synthesis
// close, or "" if none is armed. The fanout withhold seam uses it to spare the
// chair's in-flight "thinking" mark when it clears the withheld responders'
// presence: while a synthesis is armed a directed turn IS genuinely in flight
// on the chair, so clearing its mark would blank the console for the whole
// armed window (PR #718 review finding 8). Cheap read under interactionMu, only
// on the withhold path.
func (r *ChannelRouter) armedSynthesisChair(channelID string) string {
	r.interactionMu.Lock()
	defer r.interactionMu.Unlock()
	if entry := r.openInteractions[channelID]; entry != nil && entry.pendingSynthesis != nil {
		return entry.pendingSynthesis.chairID
	}
	return ""
}

// disarmChannelSynthesis drops WHATEVER synthesis close is armed on the
// channel's resolver entry (stopping its timer), independent of any particular
// pending pointer — the RFC 0050 disable path ([ChannelRouter.SetAutonomous])
// uses it so a block disabled mid-arm leaves no orphaned timeout net to close
// the now-ordinary interaction a window later. Nil-tolerant like
// [openInteraction.disarmPendingSynthesisLocked], which it wraps under the lock.
func (r *ChannelRouter) disarmChannelSynthesis(channelID string) {
	r.interactionMu.Lock()
	entry := r.openInteractions[channelID]
	var chairID string
	if entry != nil && entry.pendingSynthesis != nil {
		chairID = entry.pendingSynthesis.chairID
	}
	entry.disarmPendingSynthesisLocked() // nil-tolerant receiver.
	r.interactionMu.Unlock()
	// The arm marked the chair "thinking" ([ChannelRouter.maybeArmSynthesisClose]);
	// disabling the block abandons the arm and kills its timer, so no reply and
	// no timeout net will re-enter to clear that mark. Clear it here — the same
	// no-reply abandon posture as [ChannelRouter.onSynthesisTimeout] — so the
	// operator who just took manual control is not shown the chair composing a
	// turn that will never come for the activity TTL.
	if chairID != "" {
		r.clearActivity(channelID, chairID)
	}
}

// disarmPendingSynthesisLocked drops any armed synthesis close off this entry,
// stopping its timer — the unconditional disarm the resolver's fresh-mint
// reset and [ChannelRouter.markInteractionClosed] share (a fire after the
// clear is an identity-CAS no-op regardless; stopping just saves the spin).
// Nil-tolerant like [openInteraction.openCommitted]. Caller holds
// interactionMu.
func (e *openInteraction) disarmPendingSynthesisLocked() {
	if e == nil || e.pendingSynthesis == nil {
		return
	}
	if e.pendingSynthesis.timer != nil {
		e.pendingSynthesis.timer.Stop()
	}
	e.pendingSynthesis = nil
}

// recordSynthesisTurn emits `channel.conversation.synthesis_turn{channel_type,
// outcome}` — see the outcome constants for the lifecycle contract. Nil-safe
// like every other channel instrument.
func (r *ChannelRouter) recordSynthesisTurn(ctx context.Context, ct ChannelType, outcome string) {
	if r.metrics == nil || r.metrics.SynthesisTurn == nil {
		return
	}
	r.metrics.SynthesisTurn.Add(ctx, 1, metric.WithAttributes(
		attribute.String("channel_type", string(ct)),
		attribute.String("outcome", outcome),
	))
}
