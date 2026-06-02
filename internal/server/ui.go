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

// registerUIRoutes serves the embedded web console under /ui/ when WithUI wired
// the asset tree (orchestrator --enable-ui, default off). Absent it, the route
// is never registered, so /ui/ is a clean 404 and the rest of the surface is
// untouched.
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
func (s *Server) registerUIRoutes() {
	if s.uiFS == nil {
		return
	}
	fileServer := http.FileServer(noListFS{http.FS(s.uiFS)})
	s.mux.Handle("GET /ui/", http.StripPrefix("/ui/", fileServer))
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
