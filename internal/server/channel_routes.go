package server

// channel_routes.go centralises the channel REST route table. Split out of both
// server.go (the global route registry) and channel_handlers.go (which sits at
// the file-size review cap) so the channel surface mounts from one place that
// stays under the cap as endpoints are added — RFC 0050 Phase 1 PR 4 added the
// governance-config pair here rather than growing either neighbour.

// registerChannelRoutes mounts the channel REST surface on the server mux:
// RFC 0011 §C (create/list/get/publish/history/thread/members + delete),
// RFC 0048 console presence (/activity), and RFC 0050 Phase 1 PR 4 governance
// config (GET/PATCH …/config, gated server-side on the config_edit_enabled
// toggle). Called once from [Server.registerRoutes].
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
	s.mux.HandleFunc("DELETE /api/v1/channels/{id}/members/{participant_id}", s.handleDeleteChannelMember)
	// RFC 0050 Phase 1 PR 4 — per-channel governance config over the store-canonical
	// apply path, gated server-side on the config_edit_enabled toggle (config/ui.yaml).
	s.mux.HandleFunc("GET /api/v1/channels/{id}/config", s.handleGetChannelConfig)
	s.mux.HandleFunc("PATCH /api/v1/channels/{id}/config", s.handlePatchChannelConfig)
}
