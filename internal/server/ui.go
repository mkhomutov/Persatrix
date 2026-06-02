package server

import (
	"io/fs"
	"net/http"
	"strings"
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

// WithUIConfig injects the parsed config/ui.yaml feature toggles (RFC 0048 §C)
// that /api/v1/ui/config reports to the SPA. Separate from WithUI so the asset
// tree and the toggle config stay independently injectable (a test can supply
// toggles without an FS, or an FS without toggles). When absent — including
// whenever --enable-ui is off — handleUIConfig falls back to the Slice-1
// defaults (see [DefaultUIConfig]).
func WithUIConfig(cfg *UIConfig) ServerOption {
	return func(s *Server) {
		s.uiConfig = cfg
	}
}

// registerUIRoutes serves the embedded web console on mux when WithUI wired the
// asset tree (orchestrator --enable-ui, default off). Absent it, no route is
// registered, so the console surface is a clean 404 and the rest is untouched.
//
// It is registered on the security-bypass root mux (alongside /healthz) rather
// than on s.mux behind RESTRateLimitMiddleware: the console is operator/tester
// traffic, not agent API traffic, so it must NOT be governed by the agent
// rate-limiter / circuit-breaker. The browser calls the boot endpoints
// anonymously (no X-Agent-ID), and the PR #244 H-01 anonymous-deny fires
// whenever *any* agent is quarantined — routing the console through that layer
// would 403 it and make the console unbootable exactly when an operator needs
// it to investigate or clear the quarantine. See [Server.Handler]. (Pinned by
// ui_quarantine_test.go; the H-01 deny stays in force for genuine /api/v1/*.)
//
// http.FileServer (over hash-mode routing, RFC 0048 PR plan D1) is correct
// because every real-file request maps to an embedded asset and client routes
// live under '#', so no index.html SPA-fallback shim is needed. GET /ui (no
// trailing slash) is redirected to /ui/ by net/http's subtree pattern, so no
// explicit /ui route is needed.
//
// The FS is wrapped in noListFS so a directory without an index.html 404s
// instead of rendering http.FileServer's auto-generated listing — the embedded
// console is a deliberately-unauthenticated surface (RFC 0048 §Security) and
// PR 3's Svelte/Vite bundle ships subdirectories (e.g. _app/, .vite/) whose
// internal filenames must not be browsable. Hashed assets stay reachable by
// their direct path.
func (s *Server) registerUIRoutes(mux *http.ServeMux) {
	if s.uiFS == nil {
		return
	}
	fileServer := http.FileServer(noListFS{http.FS(s.uiFS)})
	mux.Handle("GET /ui/", http.StripPrefix("/ui/", fileServer))

	// The two read-only endpoints the SPA boots off (RFC 0048 PR 2). Registered
	// in the same uiFS-gated block as the static handler so they share the
	// console's enablement: with --enable-ui off neither is registered and both
	// are a clean 404.
	mux.HandleFunc("GET /api/v1/ui/config", s.handleUIConfig)
	mux.HandleFunc("GET /api/v1/ui/context", s.handleUIContext)
}

// noListFS wraps an http.FileSystem so http.FileServer never renders a
// directory listing. Opening a directory succeeds only when it contains an
// index.html (which http.FileServer then serves); otherwise Open returns the
// not-found error, yielding a clean 404 in place of a browsable listing. Files
// are served unchanged, so hashed assets remain reachable by direct path.
type noListFS struct {
	fs http.FileSystem
}

func (n noListFS) Open(name string) (http.File, error) {
	f, err := n.fs.Open(name)
	if err != nil {
		return nil, err
	}
	info, err := f.Stat()
	if err != nil {
		_ = f.Close()
		return nil, err
	}
	if info.IsDir() {
		index := strings.TrimSuffix(name, "/") + "/index.html"
		idx, err := n.fs.Open(index)
		if err != nil {
			_ = f.Close()
			return nil, err // no index.html in this dir → 404, not a listing
		}
		_ = idx.Close()
	}
	return f, nil
}
