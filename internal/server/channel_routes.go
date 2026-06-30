package server

// channel_routes.go centralises the channel REST route table. Split out of both
// server.go (the global route registry) and channel_handlers.go (which sits at
// the file-size review cap) so the channel surface mounts from one place that
// stays under the cap as endpoints are added — RFC 0050 Phase 1 PR 4 added the
// governance-config pair here rather than growing either neighbour.

// registerChannelRoutes mounts the channel REST surface on the server mux:
// RFC 0011 §C (create/list/get/publish/history/thread/members + delete),
// RFC 0048 console presence (/activity), RFC 0050 Phase 1 PR 4 governance
// config (GET/PATCH …/config, gated server-side on the config_edit_enabled
// toggle), and RFC 0035 Phase 2 membership-history inspection
// (GET …/members/{participant_id}/history). Called once from [Server.registerRoutes].
func (s *Server) registerChannelRoutes() {
	s.mux.HandleFunc("POST /api/v1/channels", s.handleCreateChannel)
	s.mux.HandleFunc("GET /api/v1/channels", s.handleListChannels)
	s.mux.HandleFunc("GET /api/v1/channels/{id}", s.handleGetChannel)
	s.mux.HandleFunc("DELETE /api/v1/channels/{id}", s.handleDeleteChannel)
	s.mux.HandleFunc("POST /api/v1/channels/{id}/messages", s.handlePublishMessage)
	s.mux.HandleFunc("GET /api/v1/channels/{id}/messages", s.handleGetChannelHistory)
	s.mux.HandleFunc("GET /api/v1/channels/{id}/activity", s.handleChannelActivity)
	s.mux.HandleFunc("GET /api/v1/channels/{id}/messages/{msg_id}/thread", s.handleGetThread)
	s.mux.HandleFunc("POST /api/v1/channels/{id}/members", s.handleAddChannelMember)
	s.mux.HandleFunc("PATCH /api/v1/channels/{id}/members/{participant_id}", s.handleUpdateChannelMember) // RFC 0050 member-config edit
	s.mux.HandleFunc("DELETE /api/v1/channels/{id}/members/{participant_id}", s.handleDeleteChannelMember)
	// RFC 0035 Phase 2 — read-only membership-interval history for one participant
	// in one channel (operator debugging / audit reconstruction). Auth inherits the
	// surrounding channel-surface trust level (OQ #2); no bespoke auth.
	s.mux.HandleFunc("GET /api/v1/channels/{id}/members/{participant_id}/history", s.handleGetMembershipHistory)
	// RFC 0050 Phase 1 PR 4 — per-channel governance config over the store-canonical
	// apply path, gated server-side on the config_edit_enabled toggle (config/ui.yaml).
	s.mux.HandleFunc("GET /api/v1/channels/{id}/config", s.handleGetChannelConfig)
	s.mux.HandleFunc("PATCH /api/v1/channels/{id}/config", s.handlePatchChannelConfig)
	// RFC 0052 §B PR 3 — convene an autonomous channel: the operator action that
	// opens a human-free discussion by dispatching the convene forced turn to the
	// configured convener. Gated server-side on the same config_edit_enabled
	// toggle as the config pair above (the whole RFC 0050/0052 operator surface
	// ships dark behind one opt-in).
	s.mux.HandleFunc("POST /api/v1/channels/{id}/convene", s.handleConveneChannel)
	// RFC 0036 PR 3 — membership-scoped, epoch-filtered verbatim message recall.
	// Channel-store-backed (RecallMessages joins messages × membership_intervals),
	// so it mounts with the channel surface even though its path is persona-scoped.
	// The scope participant is the path segment; every executed call is audited
	// server-side (channel.recall). Auth inherits the surrounding trust level (OQ #1).
	s.mux.HandleFunc("POST /api/v1/personas/{participant_id}/recall", s.handleRecallMessages)
}
