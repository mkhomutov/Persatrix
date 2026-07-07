package channels

// convener_cadence.go — RFC 0052 §C anti-collapse cadence (v0.3.11 PR 6),
// orchestrator half. The v0.3.x realism arc installed bias-to-silence +
// converge-then-terminate pressure (RFC 0051 semantic silence, the RFC 0030
// chair-stall escalation). With a human in the loop that is right; with NO human
// the same forces compose into premature death — every persona reasons "the
// others can cover this", all stay silent, the single chair escalation fires and
// its hand-off also draws silence (the CE5 one-shot ration is now spent), and an
// unattended channel converges to a near-empty transcript before idle rotation
// buries it with no synthesis.
//
// The counter-pressure is a CONVENER cadence (RFC 0052 OQ #1 — a role DISTINCT
// from the chair): on a stall with the agenda not yet exhausted, the convener
// advances to the next agenda item (poses the next sub-topic), giving the room
// something concrete to react to. This GENERALIZES the shipped CE5
// one-escalation-per-INTERACTION ration to one turn per AGENDA ITEM
// (chair_escalation.go keeps its own untouched one-shot ration — the two
// mechanisms are separate), while PRESERVING the CE5 loop guard: the agenda cursor
// is monotonic, so an item is never re-posed once advanced past and the convener
// re-invites any one item at most once. Total convener turns are therefore LINEAR
// in agenda length — one introduction plus at most one re-invite per item, so at
// most ~2×len (a hard ceiling, not an open loop) — and the deterministic bounded
// close (bounded_close.go) backstops termination regardless.
//
// PRECEDENCE (fanout.go): on a stalled autonomous floor round the convener cadence
// runs BEFORE the chair escalation and, while the agenda has items, SUPPRESSES it
// — anti-collapse (keep the discussion alive) takes precedence over convergence
// until the agenda is worked through. Only an agenda-exhausted (or absent) stall
// falls through to the shipped chair escalation, which then proposes
// synthesis-and-close (§D). Silence stays SEMANTIC: the cadence does not lower the
// RFC 0051 silence threshold (that would bring back the pile-on the arc removed) —
// it raises salience honestly by giving the convener a concrete next thing to ask.
//
// SCOPE (RFC 0052 OQ #2) — the load-bearing safety invariant: the cadence is gated
// on `autonomous.enabled`. A human channel keeps the shipped CE5 one-shot ration
// and bias-to-silence defaults byte-for-byte (pinned by
// TestConvenerCadence_HumanChannelUnchanged).
//
// WIRE REUSE: the advance/re-invite forced turn reuses the §B convene lane end to
// end — the `convene` dispatch marker (dispatch_control.go) → the gate's
// forced-turn admit (agents/response_gate.py) → the convener framing
// (format_convener_opening) — so it needs NO new proto field, gate rule, or prompt
// snippet. The directive names the item to (re-)pose; the convener already carries
// the prior discussion in its conversation window, so the shared opening framing
// reads as "turn the room to this item". A dedicated advance framing (a distinct
// wire marker + snippet) is a deferred refinement, tracked in the PR plan.

import (
	"context"
	"fmt"
	"strings"

	"github.com/google/uuid"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/metric"
	"go.uber.org/zap"
)

// convener_advance{outcome} attribute values — one per DISPATCHED convener
// cadence turn (an exhausted/absent-agenda stall dispatches none). `dispatch_error`
// is the drifted-convener/send failure that leaves the stall standing; unlike the
// chair escalation it has no self_stimulus disposition (the forced turn always
// carries the synthetic convene sender, never self-suppressed).
const (
	convenerAdvanceAdvance       = "advance"
	convenerAdvanceReinvite      = "reinvite"
	convenerAdvanceDispatchError = "dispatch_error"
)

// convenerCadenceAction is the per-item decision [ChannelRouter.claimConvenerCadence]
// returns when it spends a ration: either re-invite the current item in place (the
// liveness second chance) or advance to the next one.
type convenerCadenceAction int

const (
	// cadenceReinvite re-poses the CURRENT agenda item — the item reached its stall
	// without drawing a substantive round, so it earns one re-invite before the
	// cursor moves on (the RFC §C liveness target, best-effort).
	cadenceReinvite convenerCadenceAction = iota
	// cadenceAdvance poses the NEXT agenda item, with its own fresh ration.
	cadenceAdvance
)

// metricOutcome maps a spent cadence action to its convener_advance label.
func (a convenerCadenceAction) metricOutcome() string {
	if a == cadenceReinvite {
		return convenerAdvanceReinvite
	}
	return convenerAdvanceAdvance
}

// recordAgendaProgress marks the CURRENT agenda item as having drawn a substantive
// round — the best-effort liveness target's only input. Called at the fanout tail
// on a WORKING autonomous floor round (`replied > 0`), so a subsequent stall on the
// same item advances without spending a wasted re-invite. Rides the resolver entry
// under interactionMu, the CE5-ration pattern; the divergence guard mirrors
// [ChannelRouter.claimConvenerCadence] so a working round that outlived its
// interaction does not credit the successor's current item. A miss (no open
// committed interaction, or a diverged stamp) is a silent no-op — nothing to
// record.
func (r *ChannelRouter) recordAgendaProgress(channelID, stampedID string) {
	r.interactionMu.Lock()
	defer r.interactionMu.Unlock()
	entry := r.openInteractions[channelID]
	if !entry.openCommitted() {
		return
	}
	if stampedID != "" && stampedID != entry.id {
		return
	}
	entry.agendaItemDiscussed = true
}

// claimConvenerCadence is the per-agenda-item ration's compare-and-set half,
// deciding — atomically under interactionMu — what the convener does on a stall,
// and mutating the cursor/liveness state to record it. Unlike the chair's
// markChairEscalated CAS (exactly one escalation per interaction), the per-item
// ration is NOT collective exactly-once: two concurrently-stalled rounds can each
// spend a ration (e.g. one re-invites the current item, the next advances). The
// lock only guarantees each claim is a clean read-modify-write; the MONOTONIC
// cursor + one-re-invite-per-item state is what keeps the lifetime total bounded
// (≤ 2·len−1) no matter how many rounds stall concurrently.
//
// `agendaLen` is the resolved agenda's length (the caller's config snapshot);
// `stampedID` is the id stamped on the stalling stimulus ([readInteractionID]),
// "" (unstamped) tolerantly falling through like [ChannelRouter.maybeEscalateStall].
//
// Returns `ok == false` — the caller falls through to the shipped chair escalation
// — for every non-cadence stall: no open committed interaction (a resolver bypass),
// a stamped divergence (the stall outlived its interaction, so spending would hit
// the successor's ration), no agenda (a single-topic discussion has no cadence —
// the chair converges), or the agenda EXHAUSTED (on the last item with its ration
// spent, the cursor cannot advance, so the convener yields to the chair's
// synthesis-and-close, §D). On `ok == true` it returns the open id, the agenda item
// to (re-)pose, and which action was spent.
func (r *ChannelRouter) claimConvenerCadence(channelID, stampedID string, agendaLen int) (interactionID string, item int, action convenerCadenceAction, ok bool) {
	r.interactionMu.Lock()
	defer r.interactionMu.Unlock()
	entry := r.openInteractions[channelID]
	if !entry.openCommitted() {
		return "", 0, 0, false
	}
	// Divergence guard (mirrors maybeEscalateStall / advanceBoundedCloseRound): a
	// stall that outlived its interaction must not spend the successor's ration.
	if stampedID != "" && stampedID != entry.id {
		return "", 0, 0, false
	}
	if agendaLen == 0 {
		// Single-topic discussion: no agenda to advance, so no convener cadence —
		// the shipped chair escalation handles the stall (RFC §C agenda scope).
		return "", 0, 0, false
	}
	cur := entry.agendaCursor
	if cur >= agendaLen {
		// The agenda SHRANK below the cursor mid-discussion (a live RFC 0050 apply
		// can replace `autonomous.agenda` while the interaction runs, but the cursor
		// rides the resolver entry across rounds). Everything at/after the cursor is
		// gone, so the remaining agenda is exhausted from here — yield to the chair
		// rather than re-pose a now-nonexistent item (which would index the fresh
		// agenda out of range in composeAgendaAdvanceDirective).
		return "", 0, 0, false
	}
	// Liveness target (best-effort, shipped at one substantive turn per item): an
	// item that reached its stall having drawn NO substantive round earns one
	// re-invite before it is skipped — the "re-invite rather than skip on the first
	// quiet round" second chance. The re-invite IS the item's per-item ration used
	// in place, so a second stall (below) advances instead of re-inviting again.
	if !entry.agendaItemDiscussed && !entry.agendaItemReinvited {
		entry.agendaItemReinvited = true
		return entry.id, cur, cadenceReinvite, true
	}
	// Advance: pose the next item with its own fresh ration. The cursor is
	// MONOTONIC — the loop guard — so an item is never re-posed once advanced past,
	// keeping total convener turns linear in agenda length (one advance per item
	// transition + at most one re-invite per item).
	if cur+1 < agendaLen {
		entry.agendaCursor = cur + 1
		entry.agendaItemDiscussed = false
		entry.agendaItemReinvited = false
		return entry.id, cur + 1, cadenceAdvance, true
	}
	// The last item, ration spent: the agenda is exhausted. Yield to the chair.
	return "", 0, 0, false
}

// maybeAdvanceAgenda is the RFC 0052 §C cadence's fanout-tail hook, the
// [ChannelRouter.maybeEscalateStall] sibling run for autonomous channels only.
// It returns `true` when it HANDLED the round — either recorded a working round's
// progress or dispatched a convener forced turn — so the caller SUPPRESSES the
// chair escalation this round; `false` falls through to the shipped chair
// escalation (a human channel, a non-stall, or an exhausted/absent agenda). A
// send-side dispatch, never an await (CE7), like the chair escalation it defers to.
//
// `outcome` is the floor round's tally (granted/replied); `members` the roster the
// fanout already loaded (the convener's row + the audience/drift check); `a` the
// fanout's single per-publish [ChannelRouter.AutonomousFor] snapshot, shared with
// the bounded close so the OQ #2 scope reads cannot be torn by a concurrent RFC
// 0050 apply.
func (r *ChannelRouter) maybeAdvanceAgenda(ctx context.Context, msg ChannelMessage, ct ChannelType, outcome floorRoundOutcome, members []Member, channelSize int, a AutonomousConfig) bool {
	if !a.Enabled {
		return false // OQ #2 scope gate: a human channel keeps the shipped CE5 ration.
	}
	if outcome.granted == 0 {
		return false // nothing was asked — vacuously silent, not a stall (CE1).
	}
	stampedID := readInteractionID(msg.Metadata)
	if outcome.replied > 0 {
		// A working round: credit the current agenda item toward its liveness target
		// and defer — the chair escalation this would suppress also no-ops on a
		// replied round, so returning false is a harmless double no-op.
		r.recordAgendaProgress(msg.ChannelID, stampedID)
		return false
	}
	if len(a.Agenda) == 0 {
		return false // single-topic discussion: no cadence; the chair converges.
	}
	// The convener must be a dispatchable roster member to advance the agenda. A
	// drifted (non-member) or observer (respond: never) convener falls through to
	// the chair escalation rather than burning a ration on a guaranteed-suppressed
	// dispatch — the receiver gate would silence an observer convener before any LLM,
	// exactly the case the convene path also refuses ([ChannelRouter.ConveneChannel]).
	// Checked BEFORE the claim so a broken convener neither spends a ration nor moves
	// the cursor; the discussion still terminates on the bounded close.
	convener := memberByID(members, a.Convener)
	if convener == nil || convener.RespondPolicy.Normalize() == RespondNever {
		return false
	}
	interactionID, item, action, ok := r.claimConvenerCadence(msg.ChannelID, stampedID, len(a.Agenda))
	if !ok {
		return false // agenda exhausted / diverged / untracked → the chair converges.
	}

	// The forced turn: the item to (re-)pose as a fresh convene-lane dispatch. It
	// carries the synthetic convene sender (never self-suppressed at the gate, even
	// when the convener authored the stalling stimulus — so unlike the chair
	// escalation the cadence needs no self_stimulus guard) and is stamped with the
	// OPEN interaction id, so its lease bills to the discussion's per-interaction cap
	// (metering) and the convener's reply resolves back into the same interaction.
	forced := ChannelMessage{
		ID:        uuid.NewString(),
		ChannelID: msg.ChannelID,
		SenderID:  ConveneDispatchSenderID,
		Content:   composeAgendaAdvanceDirective(a, item),
		Metadata:  map[string]any{interactionIDMetadataKey: interactionID},
	}
	// Mark the convener thinking so the RFC 0048 console shows the turn being
	// composed; a failed dispatch clears it (no reply can ever clear a mark whose
	// dispatch never landed), mirroring the chair escalation.
	r.markActivity(msg.ChannelID, []string{a.Convener})
	// nil floor mentions: the convener is admitted by the convene-marker lift, not
	// by directedness — the §B convene / ISSUE-0099 resynthesize posture.
	if err := r.dispatchTo(ctx, forced, ct, "", *convener, channelSize, nil, dispatchControl{marker: markerConvene}); err != nil {
		r.clearActivity(msg.ChannelID, a.Convener)
		r.logger.Warn("channels: convener agenda advance dispatch failed; stall stands",
			zap.String("channel_id", msg.ChannelID),
			zap.String("interaction_id", interactionID),
			zap.String("convener", a.Convener),
			zap.Error(err))
		r.recordConvenerAdvance(ctx, ct, convenerAdvanceDispatchError)
		// The ration is spent (claimed above) — like the chair's chair-gone branch
		// the cadence does not refund a failed dispatch (one attempt per item keeps
		// the loop guard simple). STILL report handled: the chair must not ALSO fire
		// into the same silence while the agenda has items (its one-shot ration is
		// precious for the eventual §D converge); the next stall advances or exhausts.
		return true
	}
	r.recordConvenerAdvance(ctx, ct, action.metricOutcome())
	r.logger.Info("channels: convener advanced the agenda on a stall",
		zap.String("channel_id", msg.ChannelID),
		zap.String("interaction_id", interactionID),
		zap.String("convener", a.Convener),
		zap.Int("agenda_item", item),
		zap.String("action", action.metricOutcome()),
	)
	return true
}

// composeAgendaAdvanceDirective assembles the operator topic + the single agenda
// item to (re-)pose + goal into the directive the convener turn carries. Reuses the
// §B convene framing (format_convener_opening receiver-side), so the item rides a
// single-item "Next agenda item" block the shared snippet's "pose the first agenda
// item (if an agenda is given)" instruction binds to; the item is renumbered `1.`
// so the persona poses the one item present regardless of its position in the full
// agenda. NOTE the shared snippet still frames this as an OPENING turn ("open the
// discussion"), so a mid-run advance/re-invite reads as a re-opening rather than a
// "move on to the next item" — a dedicated advance framing (its own wire marker +
// snippet) is the deferred refinement tracked in the PR plan. Plain assembly, no
// escaping — the trust boundary is the RFC 0009 envelope the receiver wraps it in,
// not this string ([composeConveneDirective]'s posture). Hard-trimmed to the
// shared wire ceiling
// ([clampDirectiveBytes]) so an unbounded operator agenda item cannot dispatch a
// multi-MB directive.
func composeAgendaAdvanceDirective(a AutonomousConfig, item int) string {
	var b strings.Builder
	if topic := strings.TrimSpace(a.Topic); topic != "" {
		fmt.Fprintf(&b, "Topic: %s\n", topic)
	}
	b.WriteString("\nNext agenda item:\n")
	fmt.Fprintf(&b, "1. %s\n", a.Agenda[item])
	if goal := strings.TrimSpace(a.Goal); goal != "" {
		fmt.Fprintf(&b, "\nGoal: %s\n", goal)
	}
	return clampDirectiveBytes(strings.TrimSpace(b.String()))
}

// recordConvenerAdvance emits `channel.conversation.convener_advance
// {channel_type, outcome}` — one increment per DISPATCHED cadence turn, labelled by
// its action. Nil-safe like every other channel instrument.
func (r *ChannelRouter) recordConvenerAdvance(ctx context.Context, ct ChannelType, outcome string) {
	if r.metrics == nil || r.metrics.ConvenerAdvance == nil {
		return
	}
	r.metrics.ConvenerAdvance.Add(ctx, 1, metric.WithAttributes(
		attribute.String("channel_type", string(ct)),
		attribute.String("outcome", outcome),
	))
}
