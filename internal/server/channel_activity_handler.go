package server

import "net/http"

// handleChannelActivity handles GET /api/v1/channels/{id}/activity (RFC 0048
// console presence Tier 1). It returns the participants the orchestrator
// currently has an in-flight turn for in the channel — those it dispatched to
// and is awaiting a reply from — so the web console can show an accurate
// "… is thinking" for every trigger, not just the turns it fired itself.
//
// The in-flight set lives on the ChannelRouter (it owns the dispatch seams), so
// the route 503s when no router is wired — distinct from the store-only
// read paths, which can serve history without a router. The channel is looked
// up first so an unknown id 404s consistently with the other channel reads
// rather than returning an empty set for a channel that does not exist.
func (s *Server) handleChannelActivity(w http.ResponseWriter, r *http.Request) {
	if s.channelStore == nil || s.channelRouter == nil {
		writeError(w, "UNAVAILABLE", "channels not configured", http.StatusServiceUnavailable)
		return
	}
	id := r.PathValue("id")
	if _, err := s.channelStore.GetChannel(r.Context(), id); err != nil {
		s.writeChannelError(w, err)
		return
	}
	thinking := s.channelRouter.ChannelActivity(id)
	if thinking == nil {
		thinking = []string{} // marshal an idle channel as [], never null
	}
	writeJSON(w, channelActivityResponse{Thinking: thinking}, http.StatusOK)
}
