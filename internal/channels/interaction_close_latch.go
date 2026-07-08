package channels

// interaction_close_latch.go — the RFC 0052 no-reopen latch's resolver-side
// state (PR 4b-i review rounds 5–6): the per-channel ledger of deliberately
// CLOSED interaction ids, plus [ChannelRouter.markInteractionClosed], the
// close-side resolver writer that maintains it. Split out of
// interaction_resolver.go, which sits at the 500-line review cap (the
// interaction_escalation_state.go precedent).
//
// WHY A LEDGER (review round 6). The latch suppresses post-close traffic that
// still CLAIMS a closed id — a floor straggler whose reply lands after its
// bounding round ended, or a reply drawn by a sub-bound sibling fanout that
// raced the close on the concurrent path. Minting fresh for such a claim
// re-fans the reply and REOPENS the terminated discussion (the §D runaway).
// The round-5 latch keyed on `entry.retired == claim && tombstoned(claim)`,
// read on the publish path OUTSIDE the resolver's critical section, and that
// shape had two confirmed holes:
//
//   - TOCTOU: the predicate and [ChannelRouter.resolveInteractionID] took
//     interactionMu in two separate acquisitions, so a bounded close landing
//     between them (predicate: "not latched"; resolve: `entry.id == ""` →
//     mint) re-fanned the very straggler the latch exists to suppress — and
//     the freshly-stamped id sailed past processEndVote's tombstone branch
//     too. The ledger is read by resolveInteractionID INSIDE the same
//     critical section that would mint, so no close can slip between the
//     decision and the mint.
//   - One-generation coverage: the retiree slot holds a single id and the
//     tombstone dies at the NEXT close's deferred discard, so a straggler of
//     generation A arriving after close(A) → re-convene → close(B) escaped
//     both conjuncts and minted fresh. A re-convene during one slow in-flight
//     LLM reply compresses that window to seconds. The ledger spans
//     [postCloseLatchGenerations] generations instead.
//
// SCOPE. Only DELIBERATE closes (end-vote quorum, RFC 0052 structural/cost)
// enter the ledger — markInteractionClosed is their one notification seam.
// Idle rotations never do (that path is inline in resolveInteractionID), so a
// reply claiming an idle-rotated predecessor keeps minting fresh — the IP2
// posture a discussion surviving an idle rotation depends on. The ledger is
// per-entry, so it is channel-scoped by construction: a forged claim naming a
// FOREIGN channel's closed id never latches and keeps the IP2 override.
// Whether the ledger is CONSULTED is the resolver's own autonomous-only scope
// gate (resolveInteractionID, PR #716 review) — human channels never latch,
// byte-for-byte unchanged.
//
// LIFETIME. In-memory, dies with the process like the rest of the resolver
// table (IP5): a straggler arriving after a restart mints fresh, the accepted
// inheritance posture. Bounded residue: at most postCloseLatchGenerations
// uuid strings per channel that ever deliberately closed — the same
// "bounded by real channels" argument as the retiree slot.

import "slices"

// postCloseLatchGenerations bounds the per-channel no-reopen ledger. Sized
// for the realistic escape window — a straggler in flight across MULTIPLE
// fast successor generations (a `max_rounds`-floor re-convene can close in
// seconds) — while keeping the residue a handful of uuid strings per channel.
// A straggler older than the whole ledger falls back to the IP2 mint, the
// pre-latch posture.
const postCloseLatchGenerations = 8

// rememberClosed appends a deliberately closed interaction id to the
// channel's no-reopen ledger (newest last), evicting the oldest past
// [postCloseLatchGenerations]. The Contains guard is DEFENSIVE ONLY: no
// production path closes the same id twice today — both close causes sit
// behind the single-shot tombstone CAS under endVoteMu, and ids are minted
// fresh per generation, so a double-remember has no live producer (PR #716
// review). It stays as cheap insurance for a future second close path (the
// ledger outlives the tombstone, so the CAS alone would not cover one).
// Caller holds interactionMu.
func (e *openInteraction) rememberClosed(interactionID string) {
	if slices.Contains(e.recentlyClosed, interactionID) {
		return
	}
	e.recentlyClosed = append(e.recentlyClosed, interactionID)
	if len(e.recentlyClosed) > postCloseLatchGenerations {
		e.recentlyClosed = e.recentlyClosed[len(e.recentlyClosed)-postCloseLatchGenerations:]
	}
}

// latchedClaim reports whether `claim` names one of this channel's
// deliberately closed interactions — the RFC 0052 no-reopen latch read,
// consulted by [ChannelRouter.resolveInteractionID] inside the same critical
// section that would otherwise mint (see the file header for why atomicity is
// load-bearing). Caller holds interactionMu.
func (e *openInteraction) latchedClaim(claim string) bool {
	return slices.Contains(e.recentlyClosed, claim)
}

// withholdsStimulusLocked reports whether a stimulus stamped `stamped` must be
// withheld from dispatch because the discussion it belongs to is TERMINATING:
// its id is in the no-reopen ledger (a deliberate close landed between its
// commit and its fanout), or a synthesis close is armed on the entry (PR 4b-ii
// — the bound has fired, only the closing artifact is outstanding, and a
// dispatched round would fan LLM turns into the terminated discussion; the
// armed half applies regardless of the stamp). THE one definition of the
// terminating-state verdict, shared by the fanout head
// ([ChannelRouter.stimulusOutlivedClose]) and the fanout tail
// ([ChannelRouter.advanceBoundedCloseRound]) — the two sites must stay
// semantically identical, and a future terminating condition landing in one
// hand-spelled copy but not the other would give the head and tail divergent
// verdicts on the same stimulus (PR #718 review; the
// disarmPendingSynthesisChairLocked precedent, one drift class over).
// Nil-tolerant like [openInteraction.openCommitted]; caller holds
// interactionMu.
func (e *openInteraction) withholdsStimulusLocked(stamped string) bool {
	if e == nil {
		return false
	}
	if stamped != "" && e.latchedClaim(stamped) {
		return true
	}
	return e.pendingSynthesis != nil
}

// stimulusOutlivedClose reports whether `msg` is stamped with an interaction id
// the no-reopen ledger holds — the floor path's fanout-HEAD staleness check
// (PR #716 review). [ChannelRouter.advanceBoundedCloseRound] reads the same
// ledger at the fanout TAIL, which suffices for the concurrent path (its
// dispatch follows the tail) but sat AFTER the floor path's round: a deliberate
// close landing between a publish's commit and its detached fanout still
// dispatched a full multi-speaker floor round of LLM turns into the terminated
// discussion — replies the publish-path latch then absorbed with the spend
// already spent, the exact cost the concurrent path's close-before-dispatch
// ordering exists to avoid. Same scope and semantics as the tail read: gated on
// `autonomous.enabled` (human channels never latch, byte-for-byte unchanged —
// the `!a.Enabled` early-out keeps human CHANNELS off the mutex; on an enabled
// channel even UNSTAMPED traffic must take it, because the armed-synthesis
// withhold below applies regardless of the stamp — PR #718 review retired the
// old unstamped early-out for exactly that reason), and DELIBERATE closes only
// — a divergence without a close (orphan park, idle rotation) reads false and
// the round runs exactly as before.
// `a` is the fanout's single per-publish [ChannelRouter.AutonomousFor]
// snapshot (PR #716 review), shared with the tail trigger so the two reads
// cannot be torn by a concurrent RFC 0050 apply; the scope gate itself stays
// HERE, not at the call site, so a caller cannot silently opt out of it (the
// resolver's latch-gate posture).
// Since PR 4b-ii the head verdict also covers the ARMED synthesis window
// (synthesis_close.go): once a bound-crossing round dispatched the chair
// synthesis turn, the discussion has terminated and only the closing artifact
// is outstanding — running a floor round on any further stimulus would fan
// LLM turns into it exactly like the post-close case, so an armed channel
// withholds at the head regardless of the stamp (the chair's reply itself
// never reaches this check — [ChannelRouter.claimSynthesisReply] runs first).
func (r *ChannelRouter) stimulusOutlivedClose(msg ChannelMessage, a AutonomousConfig) bool {
	if !a.Enabled {
		return false
	}
	r.interactionMu.Lock()
	defer r.interactionMu.Unlock()
	// ONE shared predicate with the tail read (see
	// [openInteraction.withholdsStimulusLocked]) so the two stimulus action
	// points cannot drift (PR #718 review).
	return r.openInteractions[msg.ChannelID].withholdsStimulusLocked(readInteractionID(msg.Metadata))
}

// markInteractionClosed is the resolver close notification (IP8): a close site
// calls it so the channel's next publish mints fresh instead of stamping the
// closed id into post-close suppression for the rest of the idle window. The
// closed id parks as the pending retiree — its `closedInteractions` tombstone
// (left by the close) survives until the deferred discard, long enough to
// suppress and self-heal any commit racing the close — and is recorded as the
// channel's OQ 5 close cause with `trigger` (`end_votes` for the Layer 4 quorum
// close; `structural`/`cost` for the RFC 0052 bounded close, bounded_close.go)
// so the successor interaction's publishes carry the truthful close attribution.
// A stale call (the open id moved on already) is a no-op for the retiree and
// cause records — but the close DID happen, so the id still enters the
// no-reopen ledger: its stragglers must latch regardless of which generation
// the slot machinery was pointing at.
func (r *ChannelRouter) markInteractionClosed(channelID, interactionID, trigger string) {
	r.interactionMu.Lock()
	entry := r.openInteractions[channelID]
	var discard, disarmedChair string
	var disarmedTimer, foldSpend bool
	if entry != nil {
		entry.rememberClosed(interactionID)
		// PR 4b-ii: a deliberate close disarms any pending synthesis for the
		// SAME interaction — the racing end-vote quorum keeps its supremacy
		// (CE4), and the orphaned arm's reply then lands in the ledger above
		// as post-close traffic instead of double-closing. Capture the chair id
		// so its in-flight "thinking" mark is cleared below (PR #718 review):
		// this disarm kills the timeout net, so — exactly like
		// [ChannelRouter.onSynthesisTimeout] and
		// [ChannelRouter.disarmChannelSynthesis], the other two no-reply abandon
		// terminals — nothing else re-enters to clear the mark
		// [ChannelRouter.maybeArmSynthesisClose] set on the chair if its
		// synthesis reply never lands (the chair is now latch-suppressed), which
		// would strand it as composing for the whole activity TTL.
		if p := entry.pendingSynthesis; p != nil && p.interactionID == interactionID {
			disarmedChair, disarmedTimer = entry.disarmPendingSynthesisChairLocked()
		}
		if entry.id == interactionID {
			discard = entry.retired
			entry.retired = interactionID
			entry.id = ""
			entry.prev = previousClose{id: interactionID, trigger: trigger}
			// The open→retired transition fires exactly once per closed id, so it
			// is the fold point for the RFC 0052 §E standing SPEND total below: a
			// stale re-close (the losing side of a two-closers race) leaves
			// entry.id == "" and never re-folds.
			foldSpend = true
		}
	}
	r.interactionMu.Unlock()

	// This close disarmed the timeout net before it fired, so it owns the arm's
	// synthesisWG Done() (PR #718 review finding 1) — the racing end-vote quorum
	// (or any other deliberate closer) that beat the chair's reply must release
	// the count the arm registered, or the shutdown drain never settles. A
	// no-op when the chair already re-published its reply (publishCommit
	// cleared it) or nothing was armed.
	r.releaseSynthesisArm(channelID, disarmedChair, disarmedTimer)
	r.discardInteractionGovernance(discard)
	// RFC 0052 §E: fold the just-closed interaction's settled discussion spend into
	// the channel's aggregate standing total (standing_budget.go) — the SPEND twin
	// of the convening COUNT. On the open→retired transition only (exactly once per
	// closed id), OUTSIDE interactionMu so the wallet InteractionSpend read never
	// inverts the router→wallet lock order. A stale close folds nothing — its spend
	// was folded on the winning close that first retired the id.
	if foldSpend {
		r.foldStandingSpendOnClose(channelID, interactionID)
	}
}

// discardInteractionGovernance drops every per-interaction governance ledger
// for one retired interaction id: the RFC 0052 reply counters, the Layer 4
// end-vote tally + tombstone, and the budget snapshot. THE canonical list —
// the two one-generation-deferred discharge seams (the close above, the
// resolver's idle rotation) and the channel-delete purge all route through
// here, so a fourth per-interaction ledger added to one seam cannot silently
// stay resident on the others and re-open the resident-forever leak the purge
// was added to close (PR #718 review). Deliberately NOT the whole discharge
// story: the two partial seams that drop only the reply budget (the end-vote
// close and the close-notification tail) stay on the single call — their
// tallies are consumed, not leaked. Tolerates "" (each discard no-ops before
// taking its leaf mutex); runs outside interactionMu at every call site.
func (r *ChannelRouter) discardInteractionGovernance(interactionID string) {
	r.DiscardInteractionReplyBudget(interactionID)
	r.DiscardInteractionEndVotes(interactionID)
	r.DiscardInteractionBudget(interactionID)
}

// previousClose is the resolver's OQ 5 close-cause attribution for one
// channel, returned by [ChannelRouter.resolveInteractionID] from the same
// critical section that resolved the open id — reading it later would race a
// concurrent rotation into stamping a cause from a different generation than
// the resolved id's. A zero value means no retiree is known (fresh channel
// or post-restart re-mint). (Moved here from interaction_resolver.go when
// PR 4b-ii pushed that file past the 500-line cap — this file already owns
// the close-side resolver state the record belongs to.)
type previousClose struct {
	id      string
	trigger string
}
