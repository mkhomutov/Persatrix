package channels

// bounded_close.go — RFC 0052 §D deterministic bounded close (v0.3.11 PR 4b-i),
// orchestrator half. An autonomous channel MUST terminate, but the shipped
// terminators are the quorum end-vote (end_vote.go) and idle rotation
// (interaction_resolver.go) — and neither is guaranteed on an unattended channel
// that converges to silence before any quorum forms. This file adds the THIRD,
// deterministic terminator that fires at the floor round's tail — the
// [ChannelRouter.maybeEscalateStall] sibling — when the discussion crosses a hard
// bound:
//
//   - `autonomous.max_rounds` — the shipped knob had NO enforcement until now
//     (config_autonomous.go calls it "a second independent terminator alongside
//     the cost cap"); this is that enforcement (trigger=structural); OR
//   - the wallet SOFT budget threshold — running interaction spend reaches
//     `interaction_budget_tokens` minus the PR 4a synthesis reserve
//     ([wallet.SynthesisSoftBudgetTokens]), so the close fires BEFORE the hard cap
//     would deny the close-path leases (trigger=cost).
//
// On fire it runs the same artifact-bearing teardown the quorum end-vote path
// produces ([ChannelRouter.processEndVote]'s close branch): it retires the
// interaction id (so the channel is re-convenable and the next publish mints
// fresh — IP8), discards the per-interaction governance state, records
// `interaction_closed{trigger=structural|cost}`, and fans the marked close
// NOTIFICATION so every member's agent-local tracker closes its scope NOW and
// produces its RFC 0020 interaction summary — the readable artifact §D requires —
// instead of burying the converged discussion as "went idle" a window later.
//
// CE4 is intact: the chair still cannot close itself; this is an ORCHESTRATOR
// trigger, not a chair turn.
//
// SCOPE (RFC 0052 OQ #2) — the load-bearing safety invariant: the trigger is
// gated on `autonomous.enabled`. On an ordinary (human) channel
// [ChannelRouter.AutonomousFor] resolves the disabled default and the hook
// returns before touching any state, so human channels are byte-for-byte
// unchanged (pinned by TestBoundedClose_HumanChannelUntouched).
//
// DEFERRED (this is PR 4b-i — the Go trigger; the maintainer-chosen slice):
//   - The goal-directed CHAIR SYNTHESIS TURN against `autonomous.goal` (RFC 0052
//     §D artifact #1) is PR 4b-ii. Dispatching a re-fanning synthesis turn around
//     the close needs claim/correlation machinery (the ISSUE-0099 resynthesize
//     shape) so the chair's synthesis reply is not mistaken for a normal round
//     reply and does not mint a FRESH interaction that REOPENS the discussion — a
//     runaway on an unattended channel. That machinery lands with the Python
//     authoring + OQ #6 close-summary metering in 4b-ii; 4b-i delivers the
//     deterministic close + the per-agent RFC 0020 summary artifact (via the
//     shipped close-notification path).
//   - The wallet interaction-closed EVICTION ([wallet.WalletService.EvictInteraction],
//     PR 4a, shipped dark) is PR 7 (standing channels), where the residue leak
//     bites and the schedule timer gives a natural settle point for its
//     cross-process, fire-and-forget-close-summary precondition. The teardown here
//     therefore does NOT evict.

import (
	"context"

	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/metric"
	"go.uber.org/zap"

	"github.com/mkhomutov/persatrix/internal/wallet"
)

// interactionSpender is the narrow read the bounded-close soft-budget trigger
// needs from the wallet: the running per-interaction token total
// ([wallet.WalletService.InteractionSpend]). Injected via
// [ChannelRouter.SetInteractionSpender] at server startup — the router→wallet
// direction, the mirror of the wallet's own
// [wallet.WalletService.SetInteractionBudgetResolver] read of the router. nil
// when no wallet is wired (a $0/mock fleet, or a unit test), in which case the
// soft-budget trigger is inert and `max_rounds` alone bounds the close.
// *wallet.WalletService satisfies it.
type interactionSpender interface {
	InteractionSpend(interactionID string) int64
}

// Bounded-close `interaction_closed{trigger}` values — the `structural` / `cost`
// labels the governance-layers plan reserved on the same instrument as
// [idleTrigger] / [endVotesTrigger] (see end_vote.go).
const (
	// structuralTrigger — the interaction hit `autonomous.max_rounds`.
	structuralTrigger = "structural"
	// costTrigger — running spend crossed the wallet SOFT budget threshold.
	costTrigger = "cost"
)

// SetInteractionSpender wires the wallet's per-interaction spend read for the
// RFC 0052 bounded-close soft-budget trigger. MUST run at startup before any
// [ChannelRouter.Publish] — the field is unsynchronised, like maxCascadeDepth. A
// nil `s` leaves the soft-budget trigger inert (max_rounds still bounds the
// close), the posture for a fleet with no wallet.
func (r *ChannelRouter) SetInteractionSpender(s interactionSpender) {
	r.spend = s
}

// advanceInteractionRound increments and returns the bounded-close round tally
// for the channel's open interaction, but only while it still matches
// interactionID and is committed (a rotation/close between the fanout tail's
// read and this advance drops it). Returns (0, false) when the interaction moved
// on. Rides the resolver entry under interactionMu, the CE5-ration pattern.
//
// UNIT: one tick per fanout cycle — a full floor round under floor control (the
// expected autonomous posture), a single message without it. See
// [DefaultAutonomousMaxRounds] for why the two differ.
func (r *ChannelRouter) advanceInteractionRound(channelID, interactionID string) (int, bool) {
	r.interactionMu.Lock()
	defer r.interactionMu.Unlock()
	entry := r.openInteractions[channelID]
	if entry == nil || entry.id != interactionID || !entry.idCommitted {
		return 0, false
	}
	entry.roundCount++
	return entry.roundCount, true
}

// maybeBoundedClose is the RFC 0052 §D deterministic bounded-close trigger, run
// at the floor round's tail (the [ChannelRouter.maybeEscalateStall] sibling) for
// autonomous channels only. It advances the interaction's floor-round tally and,
// when the round bound (`autonomous.max_rounds`) or the wallet SOFT budget
// threshold is crossed, closes the interaction. A send-side teardown, never an
// await; every degraded branch nets to "no close" (the status quo).
//
// A no-op on human channels (autonomous disabled) and on untracked/diverged
// traffic, so ordinary channels are byte-for-byte unchanged.
//
// `channelSize` (the fanout's member count) is a conservative upper bound on the
// roster size N the reserve is sized for (`1 + N` close-path calls): it includes
// observer / operator seats that author no summary, so it can hold back a
// slightly larger reserve than the true roster, tripping the soft close a touch
// earlier — the safe direction (more hard-cap headroom for the close path), not
// less.
func (r *ChannelRouter) maybeBoundedClose(ctx context.Context, msg ChannelMessage, ct ChannelType, channelSize int) {
	a := r.AutonomousFor(msg.ChannelID)
	if !a.Enabled {
		return // OQ #2 scope gate: human channels are untouched.
	}
	interactionID, _, tracked := r.openInteractionEscalationState(msg.ChannelID)
	if !tracked {
		return // no open committed interaction to bound.
	}
	// Divergence guard (mirrors maybeEscalateStall): a fanout that outlived its
	// interaction — a concurrent rotation/close moved the open id on between the
	// stimulus commit and this tail — must not advance or close the successor.
	if stamped := readInteractionID(msg.Metadata); stamped != "" && stamped != interactionID {
		return
	}
	round, ok := r.advanceInteractionRound(msg.ChannelID, interactionID)
	if !ok {
		return
	}

	maxRounds := a.MaxRounds
	if maxRounds <= 0 {
		maxRounds = DefaultAutonomousMaxRounds
	}
	roundExceeded := round >= maxRounds

	budgetExceeded := false
	if r.spend != nil {
		if budget, capped := r.ResolveInteractionBudgetForInteraction(interactionID); capped && budget > 0 {
			// 4b-ii consistency (deep review): this soft threshold is derived from
			// `channelSize`; PR 4a's reserve is sized from a persona roster N. While
			// the reserve is dark (AcquireLease enforces only the hard cap) any basis
			// is safe, but once the reserve is ENFORCED the two roster bases MUST
			// match — a soft threshold computed from a larger roster than the reserve
			// was carved for would let the close fire at a spend the reserve cannot
			// actually cover, re-opening the "close leases denied" hole the reserve
			// closes. Lock the two bases together when 4b-ii wires enforcement.
			soft := wallet.SynthesisSoftBudgetTokens(budget, channelSize)
			if soft > 0 && r.spend.InteractionSpend(interactionID) >= soft {
				budgetExceeded = true
			}
		}
	}

	if !roundExceeded && !budgetExceeded {
		return
	}
	// Prefer the cost label when spend crossed the soft budget (the reserve
	// earned its keep); otherwise it is the structural (max_rounds) bound.
	trigger := structuralTrigger
	if budgetExceeded {
		trigger = costTrigger
	}
	r.boundedClose(ctx, msg, ct, interactionID, trigger)
}

// boundedClose runs the artifact-bearing close teardown for an autonomous
// interaction that crossed a hard bound — the deterministic-terminator mirror of
// [ChannelRouter.processEndVote]'s close branch, minus the eviction (deferred to
// PR 7; see the file header). A CAS on `closedInteractions` makes it single-shot:
// a second bound-crossing fanout, or a racing end-vote quorum, finds the
// interaction already closed and returns.
func (r *ChannelRouter) boundedClose(ctx context.Context, msg ChannelMessage, ct ChannelType, interactionID, trigger string) {
	r.endVoteMu.Lock()
	if _, done := r.closedInteractions[interactionID]; done {
		r.endVoteMu.Unlock()
		return
	}
	r.closedInteractions[interactionID] = struct{}{}
	delete(r.endVotes, interactionID)
	r.endVoteMu.Unlock()

	r.recordInteractionClosedBounded(ctx, msg, ct, interactionID, trigger)
	// Layer 4 → Layer 2 composition seam (mirrors processEndVote): record the
	// leftover reply allowances BEFORE discarding the counters, so the
	// reply_budget_remaining histogram observes the interaction's final state.
	r.recordReplyBudgetRemainingAtClose(ctx, msg.ChannelID, interactionID, ct)
	r.DiscardInteractionReplyBudget(interactionID)
	// Retire the id (IP8) so the channel is re-convenable and the next publish
	// mints fresh — carrying the truthful bounded-close cause, not end_votes.
	r.markInteractionClosed(msg.ChannelID, interactionID, trigger)
	// Deliver the close so each agent-local tracker closes its scope NOW and
	// produces its RFC 0020 summary (the §D artifact). Fire-and-forget, off-path.
	// excludeSender=false — unlike the end-vote close (where the voter's own vote
	// closed its tracker), `msg` here is only the round-triggering stimulus, so
	// its sender (routinely the convener/chair whose reply drove the round) needs
	// the notification too or it strands on "went idle" and authors no summary.
	r.notifyInteractionClose(ctx, msg, ct, false)
	// NOTE: no wallet EvictInteraction here — deferred to PR 7 (file header).
}

// recordInteractionClosedBounded fires the structured close log + the
// `interaction_closed{trigger=structural|cost}` counter for a deterministic
// bounded close — the sibling of [ChannelRouter.recordInteractionClosed]
// (end_votes) and [ChannelRouter.recordInteractionClosedIdle] (idle). Nil-safe
// like every other channel instrument.
func (r *ChannelRouter) recordInteractionClosedBounded(ctx context.Context, msg ChannelMessage, ct ChannelType, interactionID, trigger string) {
	r.logger.Info("channels: interaction closed by RFC 0052 bounded close",
		zap.String("channel_id", msg.ChannelID),
		zap.String("interaction_id", interactionID),
		zap.String("trigger", trigger),
	)
	if r.metrics != nil && r.metrics.InteractionClosed != nil {
		r.metrics.InteractionClosed.Add(ctx, 1, metric.WithAttributes(
			attribute.String("channel_type", string(ct)),
			attribute.String("trigger", trigger),
		))
	}
}
