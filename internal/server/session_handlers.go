// Session registry REST handlers — RFC 0031 Phase 3 §E operator surface.
//
// These four endpoints expose the orchestrator-owned `sessions` registry
// (in channels.db) over REST so the thin Rust CLI can create, list, resolve,
// and archive sessions without touching SQLite. They are an enabler: PR 2
// adds the `persatrix session …` verbs that call them. No operator-visible
// behaviour changes when no CLI calls them, and the routes return 503 when
// the channels subsystem (and therefore the registry) is not wired.
package server

import (
	"errors"
	"net/http"
	"strings"

	"go.uber.org/zap"

	"github.com/mkhomutov/persatrix/internal/channels"
)

// handleCreateSession handles POST /api/v1/sessions.
//
// Mints a UUIDv7-id session registered under the requested `label`. The
// reserved `legacy` label is rejected here (OQ #2a) — the server is the guard
// of record, so a direct REST caller cannot mint a row that would collide
// with the always-visible §D carve-out.
func (s *Server) handleCreateSession(w http.ResponseWriter, r *http.Request) {
	if s.sessionRegistry == nil {
		writeError(w, "UNAVAILABLE", "session registry not configured", http.StatusServiceUnavailable)
		return
	}
	if !requireJSON(w, r) {
		return
	}
	var req createSessionRequest
	if !decodeJSON(w, r, &req) {
		return
	}
	// Normalise the operator-supplied label at the boundary: trim surrounding
	// whitespace and reject an empty-or-whitespace-only value (the auto-mint
	// path is the only legitimate source of label-less rows). Trimming also
	// funnels a padded " legacy " into the store's reserved-id guard, so the
	// §D carve-out cannot be skirted with a whitespace-padded variant.
	req.Label = strings.TrimSpace(req.Label)
	if req.Label == "" {
		writeError(w, "BAD_REQUEST", "label is required", http.StatusBadRequest)
		return
	}
	sess, err := s.sessionRegistry.CreateSession(r.Context(), req.Label)
	if err != nil {
		s.writeSessionError(w, err)
		return
	}
	writeJSON(w, sessionToResponse(sess), http.StatusCreated)
}

// handleListSessions handles GET /api/v1/sessions?include_archived=.
//
// Returns active sessions only by default; `?include_archived=true` widens it
// to every row. Order is id-ascending (UUIDv7 lexicographic = creation order).
func (s *Server) handleListSessions(w http.ResponseWriter, r *http.Request) {
	if s.sessionRegistry == nil {
		writeError(w, "UNAVAILABLE", "session registry not configured", http.StatusServiceUnavailable)
		return
	}
	includeArchived := r.URL.Query().Get("include_archived") == "true"
	sessions, err := s.sessionRegistry.ListSessions(r.Context(), includeArchived)
	if err != nil {
		s.logger.Error("sessions: list failed", zap.Error(err))
		writeError(w, "INTERNAL", "failed to list sessions", http.StatusInternalServerError)
		return
	}
	out := make([]sessionResponse, 0, len(sessions))
	for _, sess := range sessions {
		out = append(out, sessionToResponse(sess))
	}
	writeJSON(w, listSessionsResponse{Sessions: out}, http.StatusOK)
}

// handleGetSession handles GET /api/v1/sessions/{id}.
//
// Resolves the path value as an id *or* a label (the §E `use <id-or-label>`
// contract) so `session use` / `current` can render the human name. An
// archived session still resolves — archive is one-way, not deletion.
func (s *Server) handleGetSession(w http.ResponseWriter, r *http.Request) {
	if s.sessionRegistry == nil {
		writeError(w, "UNAVAILABLE", "session registry not configured", http.StatusServiceUnavailable)
		return
	}
	idOrLabel := r.PathValue("id")
	sess, err := s.sessionRegistry.GetSession(r.Context(), idOrLabel)
	if err != nil {
		s.writeSessionError(w, err)
		return
	}
	writeJSON(w, sessionToResponse(sess), http.StatusOK)
}

// handleArchiveSession handles POST /api/v1/sessions/{id}/archive.
//
// Archive is one-way (RFC 0031 §B): there is no unarchive/activate verb. The
// row and its tagged memory are preserved; only the `archived_at` flag flips.
// Re-archiving an already-archived session is idempotent.
func (s *Server) handleArchiveSession(w http.ResponseWriter, r *http.Request) {
	if s.sessionRegistry == nil {
		writeError(w, "UNAVAILABLE", "session registry not configured", http.StatusServiceUnavailable)
		return
	}
	idOrLabel := r.PathValue("id")
	if err := s.sessionRegistry.ArchiveSession(r.Context(), idOrLabel); err != nil {
		s.writeSessionError(w, err)
		return
	}
	w.WriteHeader(http.StatusNoContent)
}

// writeSessionError maps a session-registry sentinel to the standard JSON
// envelope and HTTP status. Centralised so every session handler reports the
// same code/message for the same store sentinel (mirrors writeChannelError).
func (s *Server) writeSessionError(w http.ResponseWriter, err error) {
	switch {
	case errors.Is(err, channels.ErrSessionNotFound):
		writeError(w, "NOT_FOUND", "session not found", http.StatusNotFound)
	case errors.Is(err, channels.ErrReservedSessionID):
		writeError(w, "BAD_REQUEST", "label is reserved", http.StatusBadRequest)
	default:
		s.logger.Error("sessions: unexpected error", zap.Error(err))
		writeError(w, "INTERNAL", "session registry error", http.StatusInternalServerError)
	}
}

// sessionToResponse converts a [channels.Session] to the wire shape.
func sessionToResponse(sess channels.Session) sessionResponse {
	return sessionResponse{
		ID:        sess.ID,
		Label:     sess.Label,
		CreatedAt: sess.CreatedAt,
		Archived:  sess.Archived,
	}
}
