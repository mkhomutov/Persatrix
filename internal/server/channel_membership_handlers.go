package server

import (
	"net/http"

	"go.uber.org/zap"

	"github.com/mkhomutov/persatrix/internal/channels"
)

// handleGetMembershipHistory handles
// GET /api/v1/channels/{id}/members/{participant_id}/history — the RFC 0035
// Phase 2 read-only operator-inspection endpoint. It surfaces a participant's
// membership stints in a channel from the append-only `membership_intervals`
// ledger, for operator debugging and audit reconstruction: a join → leave →
// rejoin reads back as two intervals (oldest first), the closed one carrying
// `left_at`, the open one omitting it.
//
// Auth posture (RFC 0035 OQ #2): this endpoint adds NO new auth model. It matches
// the surrounding channel REST surface's trust level — currently unauthenticated,
// single-tenant — and inherits RFC 0009's auth model when that lands. It MUST NOT
// ship more permissively than its neighbours; exposing who-was-where-when is no
// more sensitive than handleGetChannel's member list already is. Read-only: there
// is no ledger-mutation surface over REST — the append-only invariant stays owned
// by the four Go write hooks (RFC 0035 §C).
//
// A non-existent channel is a 404 (matching the channel-handler convention); a
// KNOWN channel the participant was never in is a clean empty history at 200, not
// a 404 — the existence check is on the channel, not the membership. Carved into
// its own file so channel_handlers.go stays under the repo's 500-line review cap.
func (s *Server) handleGetMembershipHistory(w http.ResponseWriter, r *http.Request) {
	if s.channelStore == nil {
		writeError(w, "UNAVAILABLE", "channel store not configured", http.StatusServiceUnavailable)
		return
	}
	channelID := r.PathValue("id")
	participantID := r.PathValue("participant_id")

	// 404 a non-existent channel up front (the channel-handler convention); a
	// present channel with no membership for the participant falls through to an
	// empty 200, consistent with the read-only "history" framing.
	if _, err := s.channelStore.GetChannel(r.Context(), channelID); err != nil {
		s.writeChannelError(w, err)
		return
	}
	intervals, err := s.channelStore.GetMembershipIntervals(r.Context(), channelID, participantID)
	if err != nil {
		s.logger.Error("channels: membership history failed",
			zap.String("channel_id", channelID),
			zap.String("participant_id", participantID),
			zap.Error(err))
		writeError(w, "INTERNAL", "failed to load membership history", http.StatusInternalServerError)
		return
	}
	writeJSON(w, membershipIntervalsToResponse(intervals), http.StatusOK)
}

// membershipIntervalsToResponse maps the store's [channels.MembershipInterval]
// slice to the wire shape, translating the zero-`LeftAt` open-stint convention
// to an omitted `left_at`. Always returns a non-nil `Intervals` slice so the
// payload is `{"intervals": []}` (never null) for an empty history.
func membershipIntervalsToResponse(intervals []channels.MembershipInterval) membershipHistoryResponse {
	out := membershipHistoryResponse{
		Intervals: make([]membershipIntervalResponse, 0, len(intervals)),
	}
	for _, iv := range intervals {
		entry := membershipIntervalResponse{JoinedAt: iv.JoinedAt}
		if !iv.LeftAt.IsZero() {
			left := iv.LeftAt
			entry.LeftAt = &left
		}
		out.Intervals = append(out.Intervals, entry)
	}
	return out
}
