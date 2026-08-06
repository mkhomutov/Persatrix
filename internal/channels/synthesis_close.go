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
//  3. The chair's claimed reply — the publish that echoes the armed id AND
//     carries the `synthesis_reply` marker the persona stamps off the
//     dispatched `synthesis_turn` marker (see [synthesisReplyMetadataKey]) —
//     is intercepted on the COMMIT path ([ChannelRouter.publishCommit], before
//     the end-vote hook, so a reply cast AS a vote — the shape the directive
//     invites — cannot be spam-suppressed or relabelled `end_votes` first;
//     PR #718 review) via [ChannelRouter.claimSynthesisReply] and handed to
//     the 4b-i teardown as
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
// the synthesis supersedes it as the record's final turn). A mid-arm ABANDON
// (the RFC 0050 disable, or the timeout net's max_rounds-raise re-check)
// strands that same withheld stimulus with NO superseding close at all — the
// interaction stays open and no notification ever carries a final turn; the
// same accepted loss, reached without a closing message (the operator's
// deliberate intervention owns the trade).

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
// branches (the shutdown-drain refusal degrades the same way but is
// deliberately unmetered: nothing was dispatched, and the close still counts
// on interaction_closed{…}); exactly one of
// `closed_on_reply`/`closed_on_timeout` follows a `dispatched` (a racing
// end-vote close can orphan the arm — its close is counted on
// interaction_closed{end_votes} — and a mid-arm abandon (the RFC 0050
// disable, the timeout's max_rounds-raise re-check) leaves the interaction
// open and counts nothing; in both shapes neither closed_on_* fires).
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
	// stimulusNotify carries the bounding stimulus's close-notification
	// choices as the NAMED [closeNotify] fields, stashed verbatim from the
	// arming tail so the timeout net's fallback close fans exactly what the
	// immediate close would have (PR #718 review — the redelivery bool and
	// the undelivered-member set used to ride here as a bare field pair that
	// restated closeNotify's `redelivery=false ⇒ undelivered=nil` invariant):
	// `redelivery` records whether the stimulus reached members live (the
	// floor path ran its round before the tail; the concurrent path
	// withholds), `undelivered` the members that round MISSED (see
	// [liveDeliveryFailures]), downgraded to sole delivery by the fan.
	stimulusNotify closeNotify
	// principal is the arming request's verified principal, stashed because
	// the timeout net runs on a TIMER goroutine with no request context and
	// would otherwise reach [ChannelRouter.boundedClose] on a bare
	// `context.Background()` (ISSUE-0082 Part 2, v0.3.14 PR 2). Principal is
	// the ONLY axis such a reset exposes — session re-resolves, epoch falls
	// back to the boot value — so left unfixed the close fan lands in
	// `'local'` while the arming person's own turns are partitioned. The
	// STRING, not the detached ctx: it is the entire delta a fresh context
	// loses, and holding one for the ~2-minute window would pin it for
	// nothing. Empty on every agent origin. Deliberately NOT mirrored on the
	// reply / end-vote closes (they descend from the chair's unauthenticated
	// publish): ISSUE-0082 R-1 — the close summary aggregates every speaker,
	// so no one principal is right for it until tracker scope is per-speaker.
	principal string
	// consumed flips when the arm's close is DECIDED — the reply claim or the
	// timeout fire won the identity CAS — but the teardown has not yet reached
	// [ChannelRouter.markInteractionClosed]. The pointer deliberately STAYS on
	// the entry through that window: clearing it at claim time reopened the
	// armed withhold and the arm CAS for the claim→tombstone gap, where a
	// straggler stamped with the still-open id could advance the tally past
	// the bound AGAIN and dispatch a duplicate synthesis directive (PR #718
	// review). markInteractionClosed clears the pointer in the same critical
	// section that writes the no-reopen ledger, so the withhold hands over to
	// the latch atomically. Guarded by interactionMu.
	consumed bool
	timer    *time.Timer
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
	stimulusNotify closeNotify,
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
		interactionID:  interactionID,
		trigger:        trigger,
		chairID:        chairID,
		ct:             ct,
		stimulus:       msg,
		stimulusNotify: stimulusNotify,
		// Arm time, not fire time: this ctx descends from the publish that
		// crossed the bound (fanout's `context.WithoutCancel`), so it still
		// carries the publisher's principal. See the field's doc.
		principal: PrincipalFromContext(ctx),
	}
	// Arm under interactionMu — the CAS half: two sibling bound-crossing
	// fanouts can both pass the tally advance before either arms; exactly one
	// may dispatch the turn (the once-per-interaction contract), the loser
	// just withholds.
	r.interactionMu.Lock()
	if r.draining {
		// The shutdown drain's gate (PR #718 follow-up review; ordering story
		// in router_publish_async.go): the drain's disarm sweep is final only
		// because no arm can start behind it, and the flag shares this lock
		// with the CAS. Refusing degrades to the caller's immediate close, so
		// a bound crossed mid-drain still terminates deterministically — and
		// its close-notification Adds run on this fanout goroutine, which
		// holds its own fanoutWG count, so the drain's fanoutWG.Wait captures
		// them legally. No synthesis_turn outcome is recorded: nothing was
		// dispatched, and the close itself lands on interaction_closed{…}.
		r.interactionMu.Unlock()
		r.logger.Info("channels: bound crossed during shutdown drain; closing immediately without the synthesis turn",
			zap.String("channel_id", msg.ChannelID),
			zap.String("interaction_id", interactionID))
		return synthesisUnavailable
	}
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
		// Since the PR #718 delivery-miss returns, a chair MISSING FROM THE
		// REGISTRY lands here too (Dispatch no longer nil-swallows
		// [registry.ErrAgentNotFound]) and takes the immediate close instead of
		// burning the full timeout net on a turn that never reached anyone.
		// Deliberately `dispatch_error`, not `chair_missing`: the roster said
		// the chair exists (the resolve-time label above owns that drift), the
		// WIRE could not deliver.
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
	armStands := false
	if entry := r.openInteractions[msg.ChannelID]; entry != nil && entry.pendingSynthesis == pending {
		// Register the timeout net on synthesisWG BEFORE arming the timer (PR #718
		// review finding 1) so the shutdown drain can bound the otherwise-invisible
		// timer goroutine: without it a graceful shutdown returns past an
		// armed-but-unreplied close (abandoning the §D artifact) and the timer's
		// later onSynthesisTimeout → notifyInteractionClose Add(1) races the drain's
		// fanoutWG.Wait. synthesisWG (NOT fanoutWG) so WaitForPendingFanout — which
		// the tests call while an arm is deliberately still pending — does not block
		// on the whole timeout window. The paired Done() is owned by whoever RESOLVES
		// the arm: onSynthesisTimeout on fire, or the disarm that stops the timer in
		// time (Stop()==true) — mutually exclusive, so the count balances once.
		r.synthesisWG.Add(1)
		pending.timer = time.AfterFunc(r.synthesisTimeout, func() { r.onSynthesisTimeout(pending) })
		armStands = true
	}
	r.interactionMu.Unlock()
	if !armStands {
		// A racing deliberate close (or an RFC 0050 disable) disarmed the arm
		// while the dispatch above was in flight. Its releaseSynthesisArm ran
		// BEFORE markActivity set the chair's "thinking" mark, so that clear
		// was a no-op — and with no timer armed and the reply not ours to
		// claim, nothing else re-enters to clear it: the chair would strand as
		// composing for the whole activity TTL (PR #718 review). Clear it here,
		// the mirror of the failed-dispatch unwind above. The close itself is
		// the racer's (or, for a disable, deliberately not owed), so this
		// still reports armed — the turn IS on the wire and the caller's
		// withhold is the correct posture for this cycle either way — but the
		// orphaned reply's fate differs by variant (PR #718 follow-up review):
		// after a racing DELIBERATE close it latches on the ledgered id (the
		// no-reopen shape, pinned by the races test); after a DISABLE the id
		// was never ledgered and the reply re-fans as an ordinary stimulus
		// into the now-manual channel — SetAutonomous's documented posture,
		// not a reopen (the operator holds the floor).
		r.clearActivity(msg.ChannelID, chairID)
	}
	r.logger.Info("channels: bound crossed; synthesis turn dispatched to chair",
		zap.String("channel_id", msg.ChannelID),
		zap.String("interaction_id", interactionID),
		zap.String("trigger", trigger),
		zap.String("escalation_chair_id", chairID))
	r.recordSynthesisTurn(ctx, ct, synthesisTurnDispatched)
	return synthesisArmed
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
	// The fire path owns the arm's synthesisWG count (Added at
	// [ChannelRouter.maybeArmSynthesisClose], PR #718 review finding 1): release
	// it unconditionally — this runs iff the timer fired, and the disarm sites
	// only Done() when they Stop()ped the fire (Stop()==true), so the two never
	// both release. Deferred FIRST so it runs LAST — after recover, and crucially
	// after the boundedClose → notifyInteractionClose fanoutWG.Add(1)s below, so
	// synthesisWG hits zero only once those fanoutWG counts are already held (the
	// ordering DrainPendingFanout's synthesisWG-then-fanoutWG wait relies on).
	defer r.synthesisWG.Done()
	defer r.recoverFanout("synthesis_timeout", pending.stimulus.ChannelID, pending.stimulus.ID)
	// Fresh config read BEFORE interactionMu (the resolver's one-governance-
	// mutex-at-a-time posture), consumed inside the CAS below (PR #718 review):
	// SetAutonomous's disable disarms armed closes, but an arm CREATED after
	// that disarm swept — the stale-snapshot window maybeBoundedClose's fresh
	// re-check narrows to microseconds — still reaches this fire, and the
	// operator has taken manual control: closing now would terminate a live
	// human-steered discussion, the exact failure the disable disarm exists to
	// prevent.
	fresh := r.AutonomousFor(pending.stimulus.ChannelID)
	r.interactionMu.Lock()
	entry := r.openInteractions[pending.stimulus.ChannelID]
	if entry == nil || entry.pendingSynthesis != pending || pending.consumed {
		// The reply claimed first (consumed), a racing close disarmed, or the
		// entry rotated — the arm is not this fire's to close.
		r.interactionMu.Unlock()
		return
	}
	// This fire is the OTHER action point on a crossed bound: consult the same
	// [boundStandsAgainst] verdict as maybeBoundedClose's tail, against the
	// tally frozen at the crossed round (the armed withhold blocks advances).
	// A raise does NOT disarm (only a disable does), so `boundExtended` here
	// means the operator extended the discussion mid-arm: abandon, the
	// withhold lifts, the tally survives, and the next round's tail
	// re-crosses against the raised bound (PR #718 follow-up review). Both
	// abandons route through the shared disarm pair even though the timer has
	// already fired (Stop()==false, so the release owes no synthesisWG
	// Done() — the defer above owns this fire's count).
	if verdict := boundStandsAgainst(fresh, pending.trigger, entry.roundCount); verdict != boundStands {
		crossedRound := entry.roundCount
		chairID, timerStopped := entry.disarmPendingSynthesisChairLocked()
		r.interactionMu.Unlock()
		r.releaseSynthesisArm(pending.stimulus.ChannelID, chairID, timerStopped)
		switch verdict {
		case boundDisabled:
			// Full disarm, not just consumed: the channel is manual now, so
			// the withhold must not outlive this fire — SetAutonomous's own
			// posture, "leaves the interaction open under the operator's
			// manual control".
			r.logger.Warn("channels: synthesis timeout fired on a disabled channel; leaving the interaction open under manual control",
				zap.String("channel_id", pending.stimulus.ChannelID),
				zap.String("interaction_id", pending.interactionID))
		case boundExtended:
			r.logger.Warn("channels: synthesis timeout fired after a max_rounds raise; leaving the discussion open under the raised bound",
				zap.String("channel_id", pending.stimulus.ChannelID),
				zap.String("interaction_id", pending.interactionID),
				zap.Int("crossed_round", crossedRound),
				zap.Int("raised_max_rounds", fresh.MaxRounds))
		}
		return
	}
	// Consume WITHOUT clearing (the claim path's posture, PR #718 review): the
	// withhold holds through this teardown too; markInteractionClosed clears.
	pending.consumed = true
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

	// Background, then re-stamped with the arming request's principal: the
	// timer goroutine owns no request, but the close-notification fan below
	// must land in the same tenant as the rest of the interaction. A no-op
	// when the arm had no principal (agent/autonomous origin, or `auth.mode:
	// disabled`), which keeps that path byte-identical to a bare Background.
	ctx := WithPrincipal(context.Background(), pending.principal)
	r.logger.Warn("channels: synthesis reply timed out; closing without the synthesis artifact",
		zap.String("channel_id", pending.stimulus.ChannelID),
		zap.String("interaction_id", pending.interactionID),
		zap.String("trigger", pending.trigger),
		zap.String("escalation_chair_id", pending.chairID))
	if r.boundedClose(ctx, pending.stimulus, pending.ct, pending.interactionID, pending.trigger, pending.stimulusNotify) {
		r.recordSynthesisTurn(ctx, pending.ct, synthesisTurnClosedOnTimeout)
	}
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
