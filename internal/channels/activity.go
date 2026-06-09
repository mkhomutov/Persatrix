package channels

import (
	"sort"
	"time"
)

// Channel-activity tracking (RFC 0048 console presence Tier 1). The router keeps
// a per-channel set of the participants it has dispatched a turn to and is
// expecting a reply from — its in-flight "thinking" set — so the web console can
// read it (`GET /channels/{id}/activity`) and show an accurate "… is thinking"
// for EVERY trigger, not just the turns the console fired itself (the Tier 0
// optimism in web/src/lib/presence.js).
//
// Lifecycle, wired at the router's existing dispatch seams:
//   - mark   — [ChannelRouter.fanout] marks the message's responders (the
//     members [orderResponders] expects to reply, NOT the ingestion-only
//     recipients — marking those would strand a "thinking" line on a member
//     that will never answer).
//   - clear  — [ChannelRouter.publishCommit] clears a participant the moment any
//     message it sent re-enters (the reply), keyed on sender id. This runs on
//     every publish and so covers the chat, floor, and fire-and-forget paths
//     uniformly — wherever the reply lands, it lands through publishCommit.
//   - TTL    — a read-time prune drops entries older than [activityTTL]. The
//     fire-and-forget fanout has no server-side await, so a responder that
//     declines or never answers has no reply to clear it; the ceiling is its
//     only backstop. Sized above the chat handler's 30s default and the floor
//     turn budget so a slow-but-real reply still clears via its own publish.
//
// Correlation is in-process (a map on the router), matching the replyWaiter's
// single-process constraint; a horizontal-scale rollout needs the same
// cross-process replacement called out in the replyWaiter doc.

const activityTTL = 90 * time.Second

// markActivity records that each of agentIDs has an in-flight turn in channelID,
// stamping the current time for the TTL prune. A no-op for an empty list.
func (r *ChannelRouter) markActivity(channelID string, agentIDs []string) {
	if len(agentIDs) == 0 {
		return
	}
	now := r.activityNow()
	r.activityMu.Lock()
	defer r.activityMu.Unlock()
	set := r.channelActivity[channelID]
	if set == nil {
		set = make(map[string]time.Time)
		r.channelActivity[channelID] = set
	}
	for _, id := range agentIDs {
		if id != "" {
			set[id] = now
		}
	}
}

// clearActivity drops a single participant from a channel's in-flight set (its
// reply arrived), and the channel entry entirely once empty so the map does not
// accumulate idle conversations.
func (r *ChannelRouter) clearActivity(channelID, agentID string) {
	r.activityMu.Lock()
	defer r.activityMu.Unlock()
	set := r.channelActivity[channelID]
	if set == nil {
		return
	}
	delete(set, agentID)
	if len(set) == 0 {
		delete(r.channelActivity, channelID)
	}
}

// ChannelActivity returns the participant ids with an in-flight turn in the
// channel, sorted for a stable response. It prunes entries past the TTL as it
// reads (and drops a fully-expired channel), so a silent/declined responder
// falls off without a background sweeper.
func (r *ChannelRouter) ChannelActivity(channelID string) []string {
	cutoff := r.activityNow().Add(-activityTTL)
	r.activityMu.Lock()
	defer r.activityMu.Unlock()
	set := r.channelActivity[channelID]
	if set == nil {
		return nil
	}
	out := make([]string, 0, len(set))
	for id, at := range set {
		if at.Before(cutoff) {
			delete(set, id)
			continue
		}
		out = append(out, id)
	}
	if len(set) == 0 {
		delete(r.channelActivity, channelID)
		return nil
	}
	sort.Strings(out)
	return out
}

// responderIDs projects a responder member slice to its participant ids — the
// argument shape markActivity wants from [orderResponders].
func responderIDs(responders []Member) []string {
	if len(responders) == 0 {
		return nil
	}
	ids := make([]string, 0, len(responders))
	for _, m := range responders {
		ids = append(ids, m.ParticipantID)
	}
	return ids
}
