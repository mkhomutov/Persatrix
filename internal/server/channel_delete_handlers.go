// Channel REST DELETE handlers — RFC 0011 §C, PR 4b.
//
// Split from `channel_handlers.go` to keep both files under the
// 500-line review-friendly cap. The DELETE pair landed in PR 4b
// alongside the response gate; the create/list/get/publish/history
// surface stays in `channel_handlers.go`.
package server

import (
	"errors"
	"net/http"

	"github.com/mkhomutov/persatrix/internal/channels"
)

// handleDeleteChannel handles DELETE /api/v1/channels/{id}.
//
// Cascades to memberships and messages via the schema's ON DELETE
// CASCADE rules (RFC 0011 §B "Channel-deletion cascade"). 404 on
// unknown id. 409 is reserved for a future "channel pinned by config"
// guard if §B coexistence rules grow one (RFC 0011 §C endpoint table).
//
// Unauthenticated in v0.3.0 per the channels REST surface trust
// boundary (PR #245 startup-WARN); token auth lands in RFC 0009 Phase 4.
func (s *Server) handleDeleteChannel(w http.ResponseWriter, r *http.Request) {
	if s.channelStore == nil {
		writeError(w, "UNAVAILABLE", "channel store not configured", http.StatusServiceUnavailable)
		return
	}
	id := r.PathValue("id")
	if err := s.channelStore.DeleteChannel(r.Context(), id); err != nil {
		s.writeChannelError(w, err)
		return
	}
	w.WriteHeader(http.StatusNoContent)
}

// handleDeleteChannelMember handles DELETE /api/v1/channels/{id}/members/{participant_id}.
//
// Removes the membership row but preserves the participant's prior
// messages — `messages.sender_id` retains the historical value per
// RFC 0011 §C endpoint table. 404 on unknown channel or membership.
//
// The ErrNotMember branch overrides the package-default 403 mapping
// (which encodes "publish from non-member") because in this DELETE
// context the same sentinel means "no row to remove" — RFC 0011 §C
// pins both 404 paths (unknown channel, unknown membership) for this
// endpoint.
func (s *Server) handleDeleteChannelMember(w http.ResponseWriter, r *http.Request) {
	if s.channelStore == nil {
		writeError(w, "UNAVAILABLE", "channel store not configured", http.StatusServiceUnavailable)
		return
	}
	id := r.PathValue("id")
	participantID := r.PathValue("participant_id")
	if err := s.channelStore.RemoveMember(r.Context(), id, participantID); err != nil {
		if errors.Is(err, channels.ErrNotMember) {
			writeError(w, "NOT_FOUND", "membership not found", http.StatusNotFound)
			return
		}
		s.writeChannelError(w, err)
		return
	}
	w.WriteHeader(http.StatusNoContent)
}
