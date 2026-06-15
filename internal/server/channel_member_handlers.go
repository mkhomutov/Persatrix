package server

import "net/http"

// handleUpdateChannelMember edits an existing member's disposition + salience
// threshold (RFC 0050 member-config edit) via
// PATCH /api/v1/channels/{id}/members/{participant_id}. It replaces the member's
// editable config: the store re-resolves the Tier B signals and enforces the same
// threshold rules as config load, so an out-of-range or wrong-disposition
// threshold is a 400 and a missing member is a 404. Like add/remove it returns
// 204 on success. Carved into its own file so channel_handlers.go stays under the
// repo's 500-line review cap.
func (s *Server) handleUpdateChannelMember(w http.ResponseWriter, r *http.Request) {
	if s.channelStore == nil {
		writeError(w, "UNAVAILABLE", "channel store not configured", http.StatusServiceUnavailable)
		return
	}
	if !requireJSON(w, r) {
		return
	}
	id := r.PathValue("id")
	participantID := r.PathValue("participant_id")
	var req updateMemberRequest
	if !decodeJSON(w, r, &req) {
		return
	}
	if err := s.channelStore.UpdateMemberConfig(r.Context(), id, participantID, wireRespondPolicy(req.Respond), req.Threshold); err != nil {
		s.writeChannelError(w, err)
		return
	}
	w.WriteHeader(http.StatusNoContent)
}
