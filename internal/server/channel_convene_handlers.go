// Channel convene REST handler — RFC 0052 §B self-convening (v0.3.11 PR 3).
//
// Exposes POST /api/v1/channels/{id}/convene — the operator action that opens
// an autonomous agent-only channel with no human message. It dispatches a
// directed convene forced turn to the channel's configured `autonomous.convener`
// ([channels.ChannelRouter.ConveneChannel]); the convener persona authors the
// opening turn from which the existing InboundEventWake chain carries the
// discussion. This is the single endpoint the CLI `persatrix channel convene`
// verb and the web "Convene" button both call.
//
// Gated behind the same `config_edit_enabled` toggle as the RFC 0050
// config surface: the whole RFC 0050/0052 operator surface ships dark behind
// one server-side opt-in, and convening triggers real LLM spend on an
// unattended channel, so an explicit operator opt-in is the more defensible
// posture, not less. Until an operator turns the toggle on, convene 403s
// exactly as GET/PATCH …/config do.
package server

import "net/http"

// conveneResponse is the 202 body the convene endpoint returns: the channel and
// the convener the opening turn was dispatched to. The opening turn itself is
// authored asynchronously by the convener persona, so the ack is "accepted +
// dispatched", not "discussion complete".
type conveneResponse struct {
	ChannelID string `json:"channel_id"`
	Convener  string `json:"convener"`
	Status    string `json:"status"`
}

// handleConveneChannel handles POST /api/v1/channels/{id}/convene. Order
// mirrors the other channel writers: availability (503) → toggle (403) →
// convene (the router validates armed-state + convener membership, mapping to
// 409 unarmed / 400 invalid convener / 404 missing channel / 500 store error).
// On success it returns 202 Accepted — the convener was woken; the opening turn
// is being authored.
func (s *Server) handleConveneChannel(w http.ResponseWriter, r *http.Request) {
	if s.channelStore == nil || s.channelRouter == nil {
		writeError(w, "UNAVAILABLE", "channel config surface not configured", http.StatusServiceUnavailable)
		return
	}
	if !s.configEditEnabled() {
		writeConfigEditDisabled(w)
		return
	}
	id := r.PathValue("id")
	convener, err := s.channelRouter.ConveneChannel(r.Context(), id)
	if err != nil {
		s.writeChannelError(w, err)
		return
	}
	writeJSON(w, conveneResponse{
		ChannelID: id,
		Convener:  convener,
		Status:    "convening",
	}, http.StatusAccepted)
}
