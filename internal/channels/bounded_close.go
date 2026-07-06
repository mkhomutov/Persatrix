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
// SINCE PR 4b-ii the goal-directed CHAIR SYNTHESIS TURN against
// `autonomous.goal` (RFC 0052 §D artifact #1) rides this trigger: on a chaired
// channel the bound ARMS a close-on-reply instead of closing inline — the
// claim/correlation machinery that keeps the chair's synthesis reply from
// minting a fresh interaction and REOPENING the discussion lives in
// synthesis_close.go (this file only branches to it). A missing chair, a
// failed dispatch, or a reply lost to the timeout net all fall back to the
// immediate close below, so termination stays deterministic.
//
// DEFERRED: the wallet interaction-closed EVICTION
// ([wallet.WalletService.EvictInteraction], PR 4a, shipped dark) is PR 7
// (standing channels), where the residue leak bites and the schedule timer
// gives a natural settle point for its cross-process,
// fire-and-forget-close-summary precondition. The teardown here therefore
// does NOT evict.

import (
	"context"

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

// advanceBoundedCloseRound reads the channel's open committed interaction and,
// unless a stamped divergence shows the fanout outlived it, increments and
// returns the bounded-close round tally — all in ONE interactionMu acquisition.
// It folds what used to be three locked reads on the fanout tail (an
// openInteractionEscalationState id read, the divergence guard, and a separate
// round advance) into a single critical section, closing the TOCTOU window
// between them (the entry could rotate mid-read, forcing an extra guard) and one
// lock+lookup on the autonomous hot path.
//
// `stampedID` is the interaction id stamped on the triggering message
// ([readInteractionID] of msg.Metadata); "" (unstamped) tolerantly falls through,
// the [ChannelRouter.maybeEscalateStall] posture. Returns (interactionID, round,
// true, false) on a live advance. A no-advance is one of two distinct cases the
// caller must treat differently (PR #716 review — the round-7 shape withheld
// BOTH, silently losing a live committed message to a benign interleaving):
//
//   - ("", 0, false, true) — the stamped stimulus belongs to a deliberately
//     CLOSED interaction (the no-reopen ledger, interaction_close_latch.go):
//     a sibling fanout's bounded close or an end-vote quorum landed between
//     this publish's commit and its tail. Fanning it would draw LLM replies
//     into a terminated discussion, so the dispatch must be withheld.
//   - ("", 0, false, false) — there is no open committed interaction to bound,
//     or the fanout diverged from the one it was stamped for WITHOUT a
//     deliberate close: the resolver's orphan-park interleaving (its doc calls
//     it "an interleaving artefact, not a close") or an idle rotation, neither
//     of which terminates the discussion — the message is live and the caller
//     dispatches it exactly as the pre-bounded-close router did. (Idle-rotated
//     ids never enter the ledger — the latch's own IP2 posture — so rotation
//     always lands here.)
//
// Rides the resolver entry under interactionMu, the CE5-ration pattern; the
// ledger read shares the same acquisition, so a close cannot slip between the
// staleness verdict and the tally advance.
//
// UNIT: one tick per fanout cycle — a full floor round under floor control (the
// expected autonomous posture), a single message without it. See
// [DefaultAutonomousMaxRounds] for why the two differ.
func (r *ChannelRouter) advanceBoundedCloseRound(channelID, stampedID string) (interactionID string, round int, ok, closedStale bool) {
	r.interactionMu.Lock()
	defer r.interactionMu.Unlock()
	entry := r.openInteractions[channelID]
	// Deliberate-close staleness first: it holds whether the successor is
	// already open (divergence) or not yet minted (entry.id == "" until the
	// next resolve), and in both shapes the withhold is the same.
	if entry != nil && stampedID != "" && entry.latchedClaim(stampedID) {
		return "", 0, false, true
	}
	// Armed synthesis window (PR 4b-ii, synthesis_close.go): the bound has
	// fired and the chair's synthesis turn is outstanding — the discussion is
	// TERMINATING, so no further stimulus advances the tally, dispatches, or
	// races a second close; the withhold is the deliberate-close shape (the
	// chair's reply never reaches this tail — the fanout head claims it).
	if entry != nil && entry.pendingSynthesis != nil {
		return "", 0, false, true
	}
	if !entry.openCommitted() {
		return "", 0, false, false // no open committed interaction to bound.
	}
	// Divergence guard (mirrors maybeEscalateStall): a fanout that outlived its
	// interaction must not advance or close the successor. Reaching here means
	// the stamped id is NOT in the close ledger, so the divergence is an
	// interleaving artefact and the message itself is still deliverable.
	if stampedID != "" && stampedID != entry.id {
		return "", 0, false, false
	}
	entry.roundCount++
	return entry.id, entry.roundCount, true, false
}

// maybeBoundedClose is the RFC 0052 §D deterministic bounded-close trigger, run
// at the fanout tail (the [ChannelRouter.maybeEscalateStall] sibling) for
// autonomous channels only. It advances the interaction's round tally and, when
// the round bound (`autonomous.max_rounds`) or the wallet SOFT budget threshold
// is crossed, closes the interaction. A send-side teardown, never an await;
// every degraded branch nets to "no close" (the status quo).
//
// Returns (closed, stale). `closed` — the bound was crossed and THIS fanout's
// close fired: the interaction is retired, so the caller MUST skip any
// follow-on work that would re-open it — a forced chair turn
// (maybeEscalateStall) or, on the concurrent path, the stimulus dispatch
// itself (fanout.go). `stale` — no close fired here, but the stamped stimulus
// belongs to a discussion that already terminated: its id is in the no-reopen
// ledger (a sibling close or end-vote landed between its commit and this
// tail), or it crossed the bound and lost the closing race to a concurrent
// closer (the tombstone CAS). The concurrent dispatch must ALSO be skipped,
// since fanning it would draw LLM replies into that terminated discussion —
// replies the publish-path no-reopen latch then absorbs with the spend
// already spent (PR #716 review; the resynthesize dispatch's openness
// re-check is this same rule at its own seam, and the floor path runs the
// same ledger read at the fanout HEAD — [ChannelRouter.stimulusOutlivedClose]
// — because its round dispatches before this tail). A divergence WITHOUT a
// deliberate close — the resolver's orphan-park artefact, an idle rotation —
// is NOT stale: the message is live and returns (false, false) so it
// dispatches exactly as before this trigger existed (see
// [ChannelRouter.advanceBoundedCloseRound]). (false, false) is also always
// the pair on human channels (autonomous disabled), so ordinary channels are
// byte-for-byte unchanged.
//
// `channelSize` (the fanout's member count) is a conservative upper bound on the
// roster size N the reserve is sized for (`1 + N` close-path calls): it includes
// observer / operator seats that author no summary, so it can hold back a
// slightly larger reserve than the true roster, tripping the soft close a touch
// earlier — the safe direction (more hard-cap headroom for the close path), not
// less.
//
// `dispatchPending` says the caller's dispatch for THIS cycle has not happened
// yet and a close would withhold it (the concurrent path's close-before-dispatch
// ordering; false on the floor path, whose round has already run).
//
// `a` is the fanout's single per-publish [ChannelRouter.AutonomousFor]
// snapshot (PR #716 review), shared with the floor head check so the two
// reads cannot be torn by a concurrent RFC 0050 apply; the OQ #2 scope gate
// itself stays HERE, not at the call site, so a caller cannot silently opt
// out of it (the resolver's latch-gate posture). A head-stale floor stimulus
// never reaches this trigger at all — fanout branches on the head verdict
// first, since the tail's ledger read would only re-derive it.
func (r *ChannelRouter) maybeBoundedClose(ctx context.Context, msg ChannelMessage, ct ChannelType, members []Member, channelSize int, dispatchPending bool, undelivered map[string]struct{}, a AutonomousConfig) (closed, stale bool) {
	if !a.Enabled {
		return false, false // OQ #2 scope gate: human channels are untouched.
	}
	// One locked read of the resolver entry: the open committed id, the
	// close-ledger staleness verdict, the stamped-divergence guard, and the
	// round advance fold into a single acquisition. Only a DELIBERATE close
	// makes the stimulus stale; every other no-advance is a live message the
	// caller dispatches unchanged (PR #716 review).
	interactionID, round, ok, closedStale := r.advanceBoundedCloseRound(msg.ChannelID, readInteractionID(msg.Metadata))
	if !ok {
		return false, closedStale
	}
	// §D artifact guarantee (PR #716 review): never close before the
	// interaction's FIRST live dispatch. With the dispatch still pending, a
	// close at round 1 would withhold the OPENING turn itself: no member ever
	// opened a scope, the close notification no-ops agent-side
	// (close_notification.py's no-open-scope posture), and the channel would
	// terminate having delivered nothing and produced no artifact — reachable
	// at the legal `max_rounds = 1` (or a tiny budget) on any single-responder
	// roster. Deferring to the next tail costs at most one live round past the
	// bound (the wallet hard cap still backstops the budget trigger) and gives
	// `max_rounds = 1` the same meaning on both paths: one live exchange, then
	// close — the floor path's bounding round has already run when it counts.
	if dispatchPending && round == 1 {
		return false, false
	}

	// AutonomousFor returns a NORMALIZED config on every path (SetAutonomous
	// normalizes on write; the miss fallback DefaultAutonomousConfig is
	// pre-filled), so MaxRounds is always positive here — re-filling a zero
	// locally would re-implement the one normalization rule and drift from it.
	roundExceeded := round >= a.MaxRounds

	budgetExceeded := false
	if r.spend != nil {
		if budget, capped := r.ResolveInteractionBudgetForInteraction(interactionID); capped && budget > 0 {
			// 4b-ii consistency (deep review): this soft threshold is derived from
			// `channelSize`; PR 4a's reserve is sized from a persona roster N. While
			// the reserve is dark (AcquireLease enforces only the hard cap) the basis
			// does not matter, but it stays load-bearing once the reserve is ENFORCED.
			// The safe requirement is ONE-DIRECTIONAL, not equality: the threshold
			// basis must never be SMALLER than the roster the reserve was carved for.
			// `channelSize` is always >= that persona roster (it counts every member,
			// including the observer/operator seats that author no summary — see this
			// method's doc), so `SynthesisReserveTokens` (monotonic in the roster) holds
			// back a reserve at least as large as the true close cost and trips the
			// close no LATER than a roster-exact threshold would: remaining budget at
			// close is >= the close-path cost, so the reserve covers it. The hazard is
			// the OPPOSITE mismatch — a threshold from a SMALLER roster than the reserve
			// — which would fire the close at a spend the reserve cannot cover,
			// re-opening the "close leases denied" hole. So when 4b-ii wires
			// enforcement, keep the threshold basis >= the reserve basis; matching them
			// exactly only trades the slightly-early close (this method's doc) for a
			// tighter bound and is not required for safety.
			soft := wallet.SynthesisSoftBudgetTokens(budget, channelSize)
			if soft > 0 && r.spend.InteractionSpend(interactionID) >= soft {
				budgetExceeded = true
			}
		}
	}

	if !roundExceeded && !budgetExceeded {
		return false, false
	}
	// Fresh config re-check at the ACTION point (PR #718 review + follow-up):
	// `a` is the fanout-HEAD snapshot, and the floor round between that read
	// and this tail can span minutes. An RFC 0050 disable landing inside it
	// already ran its disarm — a no-op, nothing was armed yet — so acting on
	// the stale snapshot would arm a synthesis close (or close inline) on a
	// channel the operator just took manual control of, and the timeout net
	// would force-close the live manual discussion ~2 minutes later. The bound
	// is only ever ACTED on against the CURRENT config — Enabled AND
	// MaxRounds: a mid-round `max_rounds` RAISE (config_apply → SetAutonomous)
	// must extend the discussion, not close it against the old bound. The
	// crossed tally survives on the entry, so a re-enable (or a later
	// lowering) resumes exactly where the discussion stood. Two deliberate
	// asymmetries: a mid-round LOWERING is still not caught before this tail
	// (the head-snapshot early-out above already returned; the next round's
	// tail closes against it — the one-round lag every knob write has always
	// had), and the BUDGET half stays on its per-interaction snapshot — its
	// mid-interaction immutability is the documented wallet-consistent design
	// (interaction_budget.go's snapshot-at-open). The residual
	// read-then-disable sliver (microseconds, no floor round inside it) is
	// backstopped by [ChannelRouter.onSynthesisTimeout]'s own enabled re-check.
	fresh := r.AutonomousFor(msg.ChannelID)
	if !fresh.Enabled {
		return false, false
	}
	if roundExceeded && round < fresh.MaxRounds {
		roundExceeded = false
		if !budgetExceeded {
			return false, false
		}
	}
	// Prefer the cost label when spend crossed the soft budget (the reserve
	// earned its keep); otherwise it is the structural (max_rounds) bound.
	trigger := structuralTrigger
	if budgetExceeded {
		trigger = costTrigger
	}
	// PR 4b-ii close-on-reply (RFC 0052 §D artifact #1, synthesis_close.go):
	// on a chaired channel the bound does not close YET — it dispatches the
	// goal-directed chair synthesis turn and arms the close, which then lands
	// on the chair's claimed reply (the closing artifact) or the timeout net.
	// Either armed verdict reports `stale` so the caller withholds this
	// cycle's dispatch and revival tails exactly like post-close traffic; the
	// unavailable verdict (no viable chair / dispatch failure) falls through
	// to the 4b-i immediate artifact-bearing close, keeping termination
	// deterministic.
	switch r.maybeArmSynthesisClose(ctx, msg, ct, members, channelSize, interactionID, trigger, !dispatchPending, undelivered, fresh) {
	case synthesisArmed, synthesisAlreadyArmed:
		return false, true
	case synthesisEntryMovedOn, synthesisUnavailable:
		// Fall through to the immediate close below. synthesisUnavailable — no
		// viable chair / failed dispatch — takes the 4b-i artifact-bearing
		// close. synthesisEntryMovedOn — the interaction rotated or closed under
		// this arm — relies on boundedClose's tombstone CAS: a benign rotation
		// wins and delivers `msg` as the close, a racing deliberate close loses
		// and is reported stale below (never withhold-swallowed, PR #718 review).
	}
	// The floor path's bounding stimulus was already delivered live inside
	// its round (`dispatchPending` false), so the close notification carrying
	// it is a RE-delivery — the typed marker receivers key the ingest skip on
	// (PR 4b-ii). The concurrent path withholds the dispatch, so its
	// notification is the sole delivery.
	if !r.boundedClose(ctx, msg, ct, interactionID, trigger, !dispatchPending, undelivered) {
		// The tombstone CAS lost to a racing closer (a sibling bound-crossing
		// fanout, or an end-vote quorum): the interaction IS closing, but not
		// by this fanout's hand, and this fanout's message is not the one the
		// winner's close notification carries. Report stale, not closed, so
		// the caller's withhold is LOGGED as an outlived sibling instead of
		// silently masquerading as the close itself (PR #716 review — the
		// round-7 shape returned closed=true here, and the loser's committed
		// message vanished from every member's record with no trace).
		return false, true
	}
	return true, false
}

// boundedClose runs the artifact-bearing close teardown for an autonomous
// interaction that crossed a hard bound — the deterministic-terminator mirror of
// [ChannelRouter.processEndVote]'s close branch, minus the eviction (deferred to
// PR 7; see the file header). The shared close-write
// ([ChannelRouter.tombstoneInteractionLocked]) makes it single-shot: a second
// bound-crossing fanout, or a racing end-vote quorum, finds the tombstone
// standing and returns false — the caller reports that fanout's stimulus as a
// stale sibling, not as the close (PR #716 review). Returns true when THIS
// call won the tombstone and ran the teardown.
// `redelivery` says the closing message `msg` was already delivered live via
// ordinary fanout (the floor path's bounding stimulus; false for the withheld
// concurrent-path stimulus and for the sole-delivery synthesis reply), and is
// stamped onto the close-notification fan so receivers skip the duplicate
// final-turn ingest (PR 4b-ii, `close_notification.py`). `undelivered` names
// the members that live delivery MISSED (a per-recipient dispatch error inside
// the round — see [liveDeliveryFailures]); the fan downgrades exactly those to
// sole delivery so the ingest-skip never drops a turn that never landed
// (PR #718 review). nil whenever `redelivery` is false.
func (r *ChannelRouter) boundedClose(ctx context.Context, msg ChannelMessage, ct ChannelType, interactionID, trigger string, redelivery bool, undelivered map[string]struct{}) bool {
	r.endVoteMu.Lock()
	won := r.tombstoneInteractionLocked(interactionID)
	r.endVoteMu.Unlock()
	if !won {
		return false
	}

	r.recordInteractionClosedBounded(ctx, msg, ct, interactionID, trigger)
	// The shared close-teardown tail ([ChannelRouter.finalizeInteractionClose]):
	// reply-budget snapshot → discard → retire the id (truthful bounded-close
	// cause, not end_votes) → fan the RFC 0020 summary notification. excludeSender
	// is FALSE — unlike the end-vote close (where the voter's own vote closed its
	// tracker), `msg` here is only the round-triggering stimulus (or the chair's
	// synthesis reply: its PROSE shape closed nothing agent-side, while the
	// vote-cast shape the directive invites DID close the chair's own record
	// through its end-vote discharge (vote_close.py, where the Python side
	// also meters it) — the self-echo notification then no-ops on the
	// already-closed scope by design), so the sender needs the notification
	// too or it strands on "went idle" and authors no summary.
	r.finalizeInteractionClose(ctx, msg, ct, interactionID, trigger, closeNotify{redelivery: redelivery, undelivered: undelivered})
	// NOTE: no wallet EvictInteraction here — deferred to PR 7 (file header).
	return true
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
	r.recordInteractionClosedMetric(ctx, ct, trigger)
}
