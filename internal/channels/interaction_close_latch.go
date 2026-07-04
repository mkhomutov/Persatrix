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
// the cheap unstamped check runs first so human-typed traffic touches no
// mutex), and DELIBERATE closes only — a divergence without a close (orphan
// park, idle rotation) reads false and the round runs exactly as before.
func (r *ChannelRouter) stimulusOutlivedClose(msg ChannelMessage) bool {
	stamped := readInteractionID(msg.Metadata)
	if stamped == "" || !r.AutonomousFor(msg.ChannelID).Enabled {
		return false
	}
	r.interactionMu.Lock()
	defer r.interactionMu.Unlock()
	entry := r.openInteractions[msg.ChannelID]
	return entry != nil && entry.latchedClaim(stamped)
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
	var discard string
	if entry != nil {
		entry.rememberClosed(interactionID)
		if entry.id == interactionID {
			discard = entry.retired
			entry.retired = interactionID
			entry.id = ""
			entry.prev = previousClose{id: interactionID, trigger: trigger}
		}
	}
	r.interactionMu.Unlock()

	if discard != "" {
		r.DiscardInteractionReplyBudget(discard)
		r.DiscardInteractionEndVotes(discard)
		r.DiscardInteractionBudget(discard)
	}
}
