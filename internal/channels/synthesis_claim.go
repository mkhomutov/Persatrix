package channels

// synthesis_claim.go — the RFC 0052 §D synthesis-reply RECOGNITION seam
// (v0.3.11 PR 4b-ii), split out of synthesis_close.go when the PR #718
// follow-up review's consumed-arm machinery pushed it past the 500-line review
// cap (the synthesis_disarm.go precedent). synthesis_close.go keeps the
// arm/timeout LIFECYCLE; this file holds how the chair's closing reply is
// recognised on the wire — the marker key, its tolerant reader, and the
// commit-path claim that consumes the arm.

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
func (r *ChannelRouter) claimSynthesisReply(msg ChannelMessage, inboundClaim string, a AutonomousConfig) *pendingSynthesisClose {
	if !a.Enabled {
		return nil // OQ #2: human channels never arm; skip the mutex entirely.
	}
	if inboundClaim == "" || !readSynthesisReply(msg.Metadata) {
		return nil
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
	// Stop the timeout net; when Stop()==true we PREVENTED its fire, so this
	// path owns the timer's synthesisWG Done() (PR #718 review finding 1). A false
	// return means onSynthesisTimeout already fired and will Done() itself —
	// releasing here too would double-Done (its own consumed check under this
	// mutex then makes it a close no-op, so the reply still owns the close).
	if pending.timer != nil && pending.timer.Stop() {
		r.synthesisWG.Done()
	}
	// Consume WITHOUT clearing the pointer (PR #718 review): see the field doc —
	// the armed withhold and the arm CAS must hold through the claim→tombstone
	// teardown gap, or a straggler re-crosses the bound and dispatches a
	// duplicate directive; markInteractionClosed clears it beside the ledger
	// write, handing the withhold to the latch atomically.
	pending.consumed = true
	return pending
}
