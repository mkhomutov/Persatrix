package server

import (
	"net/http"

	"github.com/mkhomutov/persatrix/internal/channels"
)

// handleUpdateChannelMember edits an existing member's disposition + salience
// threshold (RFC 0050 member-config edit) via
// PATCH /api/v1/channels/{id}/members/{participant_id}. It is a full REPLACE of
// the member's editable config: the store re-resolves the Tier B signals and
// enforces the same threshold rules as config load, so an out-of-range or
// wrong-disposition threshold is a 400 and a missing member is a 404. Like
// add/remove it returns 204 on success. Carved into its own file so
// channel_handlers.go stays under the repo's 500-line review cap.
//
// `respond` is REQUIRED (empty/absent → 400), deliberately NOT defaulted to
// when_mentioned the way add-member's bare-id shorthand is. The edit re-derives
// `salience_gated` from the *declared* disposition, which collapses to the
// legacy `always` wire value and is unrecoverable from persisted state (see
// [channels.Member.SalienceGated]); a body that omits the disposition therefore
// could not re-derive the bid correctly, and silently defaulting it would
// quietly demote a salience-gated participant to bias-to-silence. Requiring the
// caller to name the disposition on every edit keeps the replace sound.
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
	if req.Respond == "" {
		writeError(w, "BAD_REQUEST", "respond is required", http.StatusBadRequest)
		return
	}
	if err := s.channelStore.UpdateMemberConfig(r.Context(), id, participantID, channels.RespondPolicy(req.Respond), req.Threshold); err != nil {
		s.writeChannelError(w, err)
		return
	}
	w.WriteHeader(http.StatusNoContent)
}
