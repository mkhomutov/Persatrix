package channels

// interaction_escalation_state.go — the chair-stall-escalation amendment's
// half of the resolver state (RFC 0030 §C, ISSUE-0099). The interaction-id
// producer in interaction_resolver.go owns the open-interaction table; these
// accessors are its escalation-ration readers and writers, split out to keep
// that file under the 500-line review cap (the floor_mentions.go precedent).
// All of them take interactionMu and key off the same openInteraction entry,
// so the ration and the stashed stimulus (which doubles as the resynthesize
// arm + once-bound) stay generation-scoped: a rotation that replaces the
// entry's id drops them, and a fresh mint clears them (see
// resolveInteractionID).

import (
	"maps"
	"slices"
)

// openInteractionEscalationState is the chair-stall-escalation amendment's
// read half (CE1's "open tracked interaction" detection input): the channel's
// open interaction id, whether its CE5 ration is spent, and whether a
// tracked, committed interaction exists at all. Only a COMMITTED id counts —
// an uncommitted mint has no persisted messages, so there is no discussion to
// have stalled.
func (r *ChannelRouter) openInteractionEscalationState(channelID string) (interactionID string, escalated, tracked bool) {
	r.interactionMu.Lock()
	defer r.interactionMu.Unlock()
	entry := r.openInteractions[channelID]
	if entry == nil || entry.id == "" || !entry.idCommitted {
		return "", false, false
	}
	return entry.id, entry.chairEscalated, true
}

// retiredInteractionFor returns the channel's pending retiree id — the most
// recently rotated/closed interaction whose deferred discard has not fired —
// or "" when none. The RFC 0052 no-reopen latch (publishCommit,
// router_publish_async.go) uses it to scope its tombstone check to the
// channel's OWN closed interaction: the tombstone map is keyed by id alone,
// and without this scoping a member could stamp a FOREIGN channel's tombstoned
// id onto its publish and have it persist under an interaction that never
// existed on this channel (self-harm only — its own fanout is what gets
// suppressed — but polluted attribution all the same). The retiree slot and
// the tombstone move in lockstep (a displaced retiree's tombstone is discarded
// in the same critical path), so `claim == retiree && tombstoned(claim)` holds
// exactly for the latch's intended window. Lives here rather than
// interaction_resolver.go for the same reason as its siblings above — the
// resolver file sits at the 500-line review cap.
func (r *ChannelRouter) retiredInteractionFor(channelID string) string {
	r.interactionMu.Lock()
	defer r.interactionMu.Unlock()
	if entry := r.openInteractions[channelID]; entry != nil {
		return entry.retired
	}
	return ""
}

// markChairEscalated spends the interaction's CE5 ration — compare-and-set
// under interactionMu so two concurrently-stalled rounds racing the same
// ration resolve to exactly one dispatched escalation. Returns false when the
// open id moved on (rotation/close between read and mark) or the ration is
// already spent.
func (r *ChannelRouter) markChairEscalated(channelID, interactionID string) bool {
	r.interactionMu.Lock()
	defer r.interactionMu.Unlock()
	entry := r.openInteractions[channelID]
	if entry == nil || entry.id != interactionID || entry.chairEscalated {
		return false
	}
	entry.chairEscalated = true
	return true
}

// storeEscalatedStimulus stashes the stalled stimulus the first forced turn
// was built from plus the thread-parent it carried (ISSUE-0099), so a later
// provable misfire can re-force the chair WITHOUT re-sending the chair's own
// reply (which self-suppresses at the gate) and in the ORIGINAL stimulus's
// thread context. Called after that dispatch succeeds. The message is cloned —
// its metadata/mentions are shared with the live publish path, and the stash
// outlives the round — and only stored while the open id still matches the one
// that was escalated (a rotation between dispatch and store drops it, matching
// the ration's own generation-scoping).
func (r *ChannelRouter) storeEscalatedStimulus(channelID, interactionID, threadParentSenderID string, stimulus ChannelMessage) {
	clone := stimulus
	clone.Mentions = slices.Clone(stimulus.Mentions)
	clone.Metadata = maps.Clone(stimulus.Metadata)
	r.interactionMu.Lock()
	defer r.interactionMu.Unlock()
	entry := r.openInteractions[channelID]
	if entry == nil || entry.id != interactionID {
		return
	}
	entry.escalatedStimulus = &clone
	entry.escalatedThreadParent = threadParentSenderID
}

// claimChairReply is the ISSUE-0099 once-bound, expressed as the consumption of
// the stash by the chair's FIRST publish after the forced turn — its
// forced-turn reply. Compare-and-set under interactionMu: it hands back the
// stashed stimulus + thread parent and clears the stash (back to the unarmed
// state) atomically, so exactly one reply consumes the arm and a second
// observation — a concurrent publish, the resynthesize turn's own reply, or a
// later misfire — finds nothing.
//
// Crucially it fires for the forced-turn reply whether or not it misfired: the
// CALLER decides, from the publish's floor-mention outcome, whether to re-force
// or simply discard. That is the whole defence against the false-positive a
// "first empty-floor-mention chair publish" proxy invited — a clean hand-off
// consumes the arm here and disarms the trigger, so a later innocuous chair
// message can never be mistaken for the reply's misfire.
//
// Returns ("", false) unless the open id still matches, the ration was spent (a
// first escalation happened), and a stimulus is stashed. The returned message
// is a fresh clone — the caller re-stamps it for dispatch and must not alias
// the stash.
func (r *ChannelRouter) claimChairReply(channelID, interactionID string) (stimulus ChannelMessage, threadParentSenderID string, ok bool) {
	r.interactionMu.Lock()
	defer r.interactionMu.Unlock()
	entry := r.openInteractions[channelID]
	if entry == nil || entry.id != interactionID || !entry.chairEscalated ||
		entry.escalatedStimulus == nil {
		return ChannelMessage{}, "", false
	}
	clone := *entry.escalatedStimulus
	clone.Mentions = slices.Clone(entry.escalatedStimulus.Mentions)
	clone.Metadata = maps.Clone(entry.escalatedStimulus.Metadata)
	parent := entry.escalatedThreadParent
	entry.escalatedStimulus = nil
	entry.escalatedThreadParent = ""
	return clone, parent, true
}
