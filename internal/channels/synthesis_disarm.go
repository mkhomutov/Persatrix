package channels

// synthesis_disarm.go — the RFC 0052 §D armed synthesis close's DISARM
// terminals (v0.3.11 PR 4b-ii), split out of synthesis_close.go when the
// PR #718 review's reply-echo claim conjunct pushed it past the 500-line
// review cap (the interaction_close_latch.go precedent). synthesis_close.go
// keeps the arm/claim/timeout LIFECYCLE — the close-on-reply story; this file
// holds every path that ABANDONS an arm without running its close (the
// failed-dispatch unwind, the RFC 0050 disable, the channel delete, the
// shutdown drain) plus the shared release tail and the locked primitive they
// all route through. The synthesisWG ownership rule threads through both
// files: the paired Done() is owned by whoever RESOLVES the arm — the timer
// on fire (onSynthesisTimeout, synthesis_close.go), or the disarm that
// Stop()s it in time ([openInteraction.disarmPendingSynthesisLocked]'s
// timerStopped return, released by [ChannelRouter.releaseSynthesisArm]).

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
// and the exported [ChannelRouter.DisarmChannelSynthesis] both use it so an
// interaction abandoned mid-arm leaves no orphaned timeout net behind. Nil-
// tolerant like [openInteraction.disarmPendingSynthesisLocked], which it wraps.
func (r *ChannelRouter) disarmChannelSynthesis(channelID string) {
	r.interactionMu.Lock()
	entry := r.openInteractions[channelID]
	var chairID string
	if entry != nil && entry.pendingSynthesis != nil {
		chairID = entry.pendingSynthesis.chairID
	}
	timerStopped := entry.disarmPendingSynthesisLocked() // nil-tolerant receiver.
	r.interactionMu.Unlock()
	// No reply/timeout will re-enter to clear the chair's "thinking" mark once
	// abandoned here — same no-reply posture as [ChannelRouter.onSynthesisTimeout].
	r.releaseSynthesisArm(channelID, chairID, timerStopped)
}

// releaseSynthesisArm is the disarm tail shared by every terminal path (PR #718
// review): release synthesisWG if THIS call stopped the live timer (owning it —
// see [openInteraction.disarmPendingSynthesisLocked]), and clear the chair's
// stranded "thinking" mark. Both args no-op when empty/false. Runs OUTSIDE
// interactionMu — clearActivity takes its own leaf mutex.
func (r *ChannelRouter) releaseSynthesisArm(channelID, chairID string, timerStopped bool) {
	if timerStopped {
		r.synthesisWG.Done()
	}
	if chairID != "" {
		r.clearActivity(channelID, chairID)
	}
}

// disarmAllPendingSyntheses stops every channel's armed synthesis timeout net —
// the shutdown-drain precondition ([ChannelRouter.DrainPendingFanout], PR #718
// review finding 1). The timers run on detached runtime goroutines whose close
// work Add(1)s to fanoutWG, so an undisarmed timer could fire into (and race)
// the drain's fanoutWG.Wait. Abandoning an armed-but-unreplied close is the
// deliberate shutdown trade — the §D artifact is best-effort across process
// exit, and holding the drain budget open for a reply that may never come would
// starve the real in-flight fanout deliveries. Each stopped timer releases its
// synthesisWG count here; a timer already mid-fire (Stop()==false) is left to
// its onSynthesisTimeout, which releases its own count and whose tracked close
// work the subsequent fanoutWG.Wait still bounds.
func (r *ChannelRouter) disarmAllPendingSyntheses() {
	var stopped int
	r.interactionMu.Lock()
	for _, entry := range r.openInteractions {
		if entry.disarmPendingSynthesisLocked() {
			stopped++
		}
	}
	r.interactionMu.Unlock()
	for i := 0; i < stopped; i++ {
		r.synthesisWG.Done()
	}
}

// disarmPendingSynthesisLocked drops any armed synthesis close off this entry,
// stopping its timer — the unconditional disarm the resolver's fresh-mint
// reset and [ChannelRouter.markInteractionClosed] share (a fire after the
// clear is an identity-CAS no-op regardless; stopping just saves the spin).
// Nil-tolerant like [openInteraction.openCommitted]. Caller holds
// interactionMu.
//
// Returns whether it STOPPED a live timer before it fired (Stop()==true): the
// caller then owns that timer's synthesisWG Done() (PR #718 review finding 1). A
// false return — no timer yet, or the timer already fired — means the caller
// owes nothing: an already-fired onSynthesisTimeout releases the count itself.
func (e *openInteraction) disarmPendingSynthesisLocked() (timerStopped bool) {
	if e == nil || e.pendingSynthesis == nil {
		return false
	}
	if e.pendingSynthesis.timer != nil {
		timerStopped = e.pendingSynthesis.timer.Stop()
	}
	e.pendingSynthesis = nil
	return timerStopped
}
