package channels

// interaction_escalation_state.go — the chair-stall-escalation amendment's
// half of the resolver state (RFC 0030 §C, ISSUE-0099). The interaction-id
// producer in interaction_resolver.go owns the open-interaction table; these
// accessors are its escalation-ration readers and writers, split out to keep
// that file under the 500-line review cap (the floor_mentions.go precedent).
// All of them take interactionMu and key off the same openInteraction entry,
// so the ration, the stashed stimulus, and the resynthesize bound stay
// generation-scoped: a rotation that replaces the entry's id drops them, and a
// fresh mint clears them (see resolveInteractionID).

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
// was built from (ISSUE-0099), so a later provable misfire can re-force the
// chair WITHOUT re-sending the chair's own reply (which self-suppresses at the
// gate). Called after that dispatch succeeds. The message is cloned — its
// metadata/mentions are shared with the live publish path, and the stash
// outlives the round — and only stored while the open id still matches the one
// that was escalated (a rotation between dispatch and store drops it, matching
// the ration's own generation-scoping).
func (r *ChannelRouter) storeEscalatedStimulus(channelID, interactionID string, stimulus ChannelMessage) {
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
}

// claimResynthesize is the ISSUE-0099 bound's compare-and-set: it hands back
// the stashed stalled stimulus and marks the one re-dispatch spent, atomically,
// so two concurrent misfire observations resolve to exactly one resynthesize.
// Returns (nil, false) unless the open id still matches, the ration was spent
// (a first escalation happened), the re-dispatch has not already fired, and a
// stimulus is stashed. The returned message is a fresh clone — the caller
// re-stamps it for dispatch and must not alias the stash.
func (r *ChannelRouter) claimResynthesize(channelID, interactionID string) (ChannelMessage, bool) {
	r.interactionMu.Lock()
	defer r.interactionMu.Unlock()
	entry := r.openInteractions[channelID]
	if entry == nil || entry.id != interactionID || !entry.chairEscalated ||
		entry.escalationResynthesized || entry.escalatedStimulus == nil {
		return ChannelMessage{}, false
	}
	entry.escalationResynthesized = true
	clone := *entry.escalatedStimulus
	clone.Mentions = slices.Clone(entry.escalatedStimulus.Mentions)
	clone.Metadata = maps.Clone(entry.escalatedStimulus.Metadata)
	return clone, true
}
