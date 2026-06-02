package server

import (
	"io/fs"
	"net/http"
)

// WithUI enables the embedded operator/tester web console (RFC 0048 Phase 1 /
// Slice 1) by injecting its static asset tree. When wired, registerUIRoutes
// serves the assets under /ui/ (same-origin, alongside the REST API).
//
// Nil-safe and off by default: absent this option — including whenever the
// orchestrator's --enable-ui flag is off — the /ui/ route is never registered,
// so /ui/ returns a clean 404 and no default runtime behaviour changes. Mirrors
// the nil-safe gating used by WithChannels / WithLogBuffer.
//
// The console makes the unauthenticated REST surface browser-discoverable, so
// it ships off by default and the orchestrator binds 127.0.0.1; exposing it
// beyond localhost requires an authenticating reverse proxy until RFC 0039
// (auth) lands. See RFC 0048 §Security.
func WithUI(uiFS fs.FS) ServerOption {
	return func(s *Server) {
		s.uiFS = uiFS
	}
}

// registerUIRoutes serves the embedded web console under /ui/ when WithUI wired
// the asset tree (orchestrator --enable-ui, default off). Absent it, the route
// is never registered, so /ui/ is a clean 404 and the rest of the surface is
// untouched.
//
// A plain http.FileServer is correct because the SPA uses hash-mode routing
// (RFC 0048 PR plan D1) — every real-file request maps to an embedded asset and
// client routes live under '#', so no index.html SPA-fallback shim is needed.
// GET /ui (no trailing slash) is redirected to /ui/ by net/http's subtree
// pattern, so no explicit /ui route is needed.
func (s *Server) registerUIRoutes() {
	if s.uiFS == nil {
		return
	}
	s.mux.Handle("GET /ui/", http.StripPrefix("/ui/", http.FileServer(http.FS(s.uiFS))))
}
