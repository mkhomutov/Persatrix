package channels

// synthesis_claim.go — the RFC 0052 §D synthesis-reply RECOGNITION seam
// (v0.3.11 PR 4b-ii), split out of synthesis_close.go when the PR #718
// follow-up review's consumed-arm machinery pushed it past the 500-line review
// cap (the synthesis_disarm.go precedent). synthesis_close.go keeps the
// arm/timeout LIFECYCLE; this file holds how the chair's closing reply is
// recognised on the wire — the marker key, its tolerant reader, the
// commit-path claim that consumes the arm, and the close that runs on the
// claimed reply.

import (
	"context"

	"go.uber.org/zap"
)

// synthesisReplyMetadataKey is the wire-level publish-metadata flag the
// persona runtime stamps on every same-channel publish authored IN REPLY TO
// the synthesis directive: `DispatchContext.for_event` derives it from the
// dispatched `synthesis_turn` marker and `same_channel_claim` stamps it
// beside the interaction-id claim (agents/channel_wire_metadata.py; the key
// literal is pinned by the cross-language drift test). Centralised like
// [endVoteMetadataKey], its metadata-bag vehicle sibling.
const synthesisReplyMetadataKey = "synthesis_reply"

// readSynthesisReply reports whether a publish metadata bag flags the message
// as a reply to the synthesis directive. Tolerant like the other wire readers
// ([readEndInteractionVote] / [readInteractionID]): absent or non-bool reads
// false — an unmarked publish is ordinary traffic, never a closing artifact.
func readSynthesisReply(metadata map[string]any) bool {
	if metadata == nil {
		return false
	}
	v, ok := metadata[synthesisReplyMetadataKey].(bool)
	return ok && v
}

// claimSynthesisReply is the COMMIT-path intercept ([ChannelRouter.publishCommit],
// before the end-vote hook — PR #718 review, moved from the fanout head): when
// `msg` is the chair's reply claiming the armed interaction, consume the arm
// (stop the timer) and return it — the caller closes with `msg` as the closing
// artifact and suppresses the fanout entirely (no round, no concurrent
// dispatch, no revival tails: a fanned synthesis would draw replies into the
// closed discussion, the reopen §D forbids). Running BEFORE processEndVote is
// load-bearing: the §D directive explicitly blesses a vote as the synthesis
// vehicle (end_vote_action.py), and the end-vote hook consumed such a reply
// first — an in-window duplicate was suppressed as spam (the arm burned the
// full timeout and the artifact was lost), while a quorum-completing vote
// closed as `end_votes` (the unmetered wire shape, silently skipping the
// OQ #6 lease on a bound-crossed arc).
//
// The claim requires THREE conjuncts: sender == chair, the INBOUND claim ==
// the armed id, AND the [synthesisReplyMetadataKey] marker (PR #718 review).
// Sender+claim alone cannot discriminate BY CONSTRUCTION: the interaction id
// spans every round of the discussion and every agent reply echoes its
// dispatched-under id (the 4b-i rail), so an ORDINARY chair reply from an
// earlier round still in flight when the bound crossed (concurrent-path
// replies re-enter Publish on detached goroutines) matches both — and would
// be fanned to every member as the goal-directed synthesis while the REAL
// synthesis reply lands post-close in the no-reopen latch, silently
// discarded. Only the persona knows which of its publishes replies to the
// synthesis directive, so it says so on the wire; the marker rides BESIDE the
// interaction-id claim rather than replacing it with a synthesis nonce,
// because the id claim is load-bearing for the orphan posture (a disarmed
// arm's late reply must latch on the closed id — a nonce the ledger never
// held would mint fresh and REOPEN). The marker is a correlation
// discriminator, not an auth token: sender identity is the publish boundary's
// concern, and a forged marker without sender == chair still fails the claim.
//
// `inboundClaim` is the PRE-RESOLVE wire claim [ChannelRouter.publishCommit]
// captured before it stamped the resolver's verdict over the metadata bag
// (PR #718 follow-up review). Re-reading the bag here compared the RESOLVED
// id — which, while armed, always equals the armed id for every non-latched
// publish (idle rotation is arm-gated, interaction_resolver.go), so the id
// conjunct could never reject a stale inbound claim: a marked chair publish
// echoing a PREDECESSOR generation's id was consumed as the closing artifact,
// with only the 8-generation no-reopen ledger (which idle rotations and
// disable-disarms bypass) standing between it and the close. An EMPTY inbound
// claim rejects too — a genuine synthesis reply always carries the claim
// `same_channel_claim` stamped beside the marker.
//
// Anything else — an unmarked chair reply from an earlier round, a straggler
// responder claiming the armed id, an unstamped operator publish, a non-chair
// sender — returns nil and the armed withhold in
// [ChannelRouter.advanceBoundedCloseRound] /
// [ChannelRouter.stimulusOutlivedClose] owns it. A producer that never stamps
// the marker (a pre-4b-ii agent, gate drift) degrades to the timeout net —
// the documented no-reply branch, sized for exactly this. nil for every
// publish on an unarmed channel.
func (r *ChannelRouter) claimSynthesisReply(msg ChannelMessage, inboundClaim string) *pendingSynthesisClose {
	// Marker/claim first — two pure map reads that reject virtually every
	// publish before ANY lock or config copy is paid (PR #718 review: the
	// caller used to evaluate AutonomousFor eagerly per commit — an
	// autonomousMu read + config struct copy on the hottest path, the third
	// per-publish config read beside the fanout's and the resolver's — to
	// guard a branch only the chair's one closing reply per interaction ever
	// takes). Only a marked, claiming publish reads the config below.
	if inboundClaim == "" || !readSynthesisReply(msg.Metadata) {
		return nil
	}
	if !r.AutonomousFor(msg.ChannelID).Enabled {
		return nil // OQ #2: human channels never arm; skip interactionMu entirely.
	}
	r.interactionMu.Lock()
	defer r.interactionMu.Unlock()
	entry := r.openInteractions[msg.ChannelID]
	if entry == nil || entry.pendingSynthesis == nil {
		return nil
	}
	pending := entry.pendingSynthesis
	if pending.consumed || msg.SenderID != pending.chairID || inboundClaim != pending.interactionID {
		// A consumed arm's close is already in flight (this claim, an earlier
		// one, or the timeout fire won) — a second marked reply is post-close
		// traffic the still-standing withhold owns, never a second close.
		return nil
	}
	// Stop the timeout net — and TRANSFER the arm's synthesisWG count to the
	// caller instead of releasing it here (PR #718 follow-up review). The
	// caller's close (boundedClose → notifyInteractionClose) runs its
	// fanoutWG.Add(1)s on the publishing goroutine, which holds no fanoutWG
	// count of its own, and [ChannelRouter.DrainPendingFanout]'s ordering
	// proof needs every arm-originated Add registered before synthesisWG.Wait
	// returns; a Done() here — before the close — let those Adds race the
	// drain's fanoutWG.Wait from zero, the documented WaitGroup misuse the
	// timeout path avoids by deferring its Done past its close.
	// [ChannelRouter.closeOnSynthesisReply] releases the count after the
	// teardown. Two shapes, one count held on return:
	//   - Stop()==true — we PREVENTED the fire, inheriting the timer's count
	//     (the fire path never runs, so it never Done()s).
	//   - Otherwise (already fired, or not yet armed) — Add(1) under this
	//     mutex, which is never an Add-from-zero racing the drain's Wait: a
	//     fired timer's onSynthesisTimeout cannot pass its consumed check
	//     until this mutex releases, so its deferred Done still holds a
	//     count; and a pre-timer claim serializes against the drain's disarm
	//     sweep on this same mutex, so its Add happens-before the sweep
	//     completes and the Wait begins (a claim serialized after the sweep
	//     finds the arm gone above and never reaches here).
	if pending.timer == nil || !pending.timer.Stop() {
		r.synthesisWG.Add(1)
	}
	// Consume WITHOUT clearing the pointer (PR #718 review): see the field doc —
	// the armed withhold and the arm CAS must hold through the claim→tombstone
	// teardown gap, or a straggler re-crosses the bound and dispatches a
	// duplicate directive; markInteractionClosed clears it beside the ledger
	// write, handing the withhold to the latch atomically.
	pending.consumed = true
	return pending
}

// closeOnSynthesisReply runs the commit-path close for a claimed synthesis
// reply — the body of [ChannelRouter.publishCommit]'s claim branch: notify any
// parked reply waiter (a closing reply must never starve a floor turn), run
// the bounded teardown with the reply as the closing message (sole delivery —
// redelivery=false, so no per-recipient miss ledger applies), and release the
// arm's synthesisWG count LAST — after the teardown's notifyInteractionClose
// fanoutWG.Add(1)s — so [ChannelRouter.DrainPendingFanout]'s
// synthesisWG-then-fanoutWG ordering holds for this path exactly as it does
// for the timeout fire, whose Done is deferred first for the same reason
// ([ChannelRouter.onSynthesisTimeout]). The deferred release also covers a
// panicking teardown: a leaked count would hang every later drain. A lost
// tombstone CAS means a racing closer beat the reply — the synthesis stays
// committed history, the 4b-i degraded artifact shape.
func (r *ChannelRouter) closeOnSynthesisReply(ctx context.Context, msg ChannelMessage, ct ChannelType, pending *pendingSynthesisClose) {
	defer r.synthesisWG.Done()
	r.waiter.Notify(msg)
	// The zero-value closeNotify IS this path's contract: notify the sender
	// too, sole delivery (redelivery=false — no per-recipient miss ledger
	// applies to a reply nobody was dispatched).
	if r.boundedClose(ctx, msg, ct, pending.channelSize, pending.interactionID, pending.trigger, closeNotify{}) {
		r.recordSynthesisTurn(ctx, ct, synthesisTurnClosedOnReply)
		return
	}
	r.logger.Debug("channels: synthesis reply lost the closing race; close stands by the winner",
		zap.String("channel_id", msg.ChannelID),
		zap.String("interaction_id", pending.interactionID))
}
