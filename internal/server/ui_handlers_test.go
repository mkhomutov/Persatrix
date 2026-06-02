package server

import (
	"net/http"
	"testing"
	"testing/fstest"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.uber.org/zap"

	"github.com/mkhomutov/persatrix/internal/planner"
	"github.com/mkhomutov/persatrix/internal/registry"
	"github.com/mkhomutov/persatrix/internal/state"
)

// uiTestMarker is the sentinel body the in-test embed FS serves at /ui/, so
// the assertions confirm the static handler reaches the asset tree rather than
// any other route. (The real placeholder body — "run `make ui`" — is asserted
// by the internal/ui package's own compile-time presence, not here.)
const uiTestMarker = "PERSATRIX-UI-TEST-MARKER"

// uiTestServer builds a Server with WithUI wired over an in-memory asset FS
// carrying a known marker at index.html (RFC 0048 Phase 1 PR 1). Mirrors the
// other *TestServer helpers in this package.
func uiTestServer(t *testing.T, opts ...ServerOption) *Server {
	t.Helper()
	dir := t.TempDir()
	logger := zap.NewNop()
	store := state.NewInMemoryStore(logger)
	reg := registry.NewInMemoryRegistry(logger)
	pl := planner.NewYAMLPlanner(logger)
	srv, err := New("127.0.0.1:0", dir, store, reg, pl, logger, opts...)
	require.NoError(t, err)
	return srv
}

// uiAssetFS is the in-memory asset tree the WithUI tests serve. A real bundle
// would carry hashed JS/CSS too; the marker on index.html is all the static
// handler needs to prove the wiring.
func uiAssetFS() fstest.MapFS {
	return fstest.MapFS{
		"index.html": &fstest.MapFile{
			Data: []byte("<!doctype html><title>" + uiTestMarker + "</title>"),
		},
	}
}

// TestWithUI_ServesShell is the load-bearing Red assertion for PR 1: with
// WithUI wired, GET /ui/ serves the embedded index.html (200 + marker body).
func TestWithUI_ServesShell(t *testing.T) {
	srv := uiTestServer(t, WithUI(uiAssetFS()))

	rec := doRequest(srv.Handler(), http.MethodGet, "/ui/", nil)

	require.Equal(t, http.StatusOK, rec.Code,
		"GET /ui/ must serve the embedded shell when WithUI is wired")
	assert.Contains(t, rec.Body.String(), uiTestMarker,
		"the served body must come from the injected asset FS (index.html)")
}

// TestWithUI_RedirectsBareUITrailingSlash verifies net/http's subtree-pattern
// redirect: GET /ui (no trailing slash) redirects to /ui/, so no explicit /ui
// route is needed. The exact 3xx code is net/http's choice (307 on this Go
// version); the contract this asserts is "bare /ui lands at the /ui/ subtree".
func TestWithUI_RedirectsBareUITrailingSlash(t *testing.T) {
	srv := uiTestServer(t, WithUI(uiAssetFS()))

	rec := doRequest(srv.Handler(), http.MethodGet, "/ui", nil)

	assert.GreaterOrEqual(t, rec.Code, 300, "GET /ui must redirect (3xx)")
	assert.Less(t, rec.Code, 400, "GET /ui must redirect (3xx)")
	assert.Equal(t, "/ui/", rec.Header().Get("Location"),
		"the redirect target must be the /ui/ subtree")
}

// TestWithUI_NoDirectoryListing hardens the static surface: a bare
// http.FileServer renders an auto-generated listing for any directory lacking
// an index.html, which on this deliberately-unauthenticated console surface
// (RFC 0048 §Security) would expose the bundle's internal file names. PR 3's
// Svelte/Vite output ships exactly such subdirectories (e.g. _app/, .vite/), so
// the listing must be a clean 404 while individual hashed assets stay reachable
// by their direct path. The scaffold today has no subdirs, so this test
// supplies one via the injected FS to lock the contract before PR 3 relies on
// it.
func TestWithUI_NoDirectoryListing(t *testing.T) {
	fsys := fstest.MapFS{
		"index.html": &fstest.MapFile{
			Data: []byte("<!doctype html><title>" + uiTestMarker + "</title>"),
		},
		"_app/immutable/chunk.js": &fstest.MapFile{Data: []byte("export const x = 1")},
	}
	srv := uiTestServer(t, WithUI(fsys))

	listing := doRequest(srv.Handler(), http.MethodGet, "/ui/_app/immutable/", nil)
	assert.Equal(t, http.StatusNotFound, listing.Code,
		"a directory without index.html must 404, not serve a file listing")

	asset := doRequest(srv.Handler(), http.MethodGet, "/ui/_app/immutable/chunk.js", nil)
	assert.Equal(t, http.StatusOK, asset.Code,
		"hardening must not break serving hashed assets by their direct path")
}

// TestWithoutUI_NotFound is the nil-safe gate: with no WithUI option the /ui/
// route is never registered, so it is a clean 404 and the rest of the surface
// is untouched.
func TestWithoutUI_NotFound(t *testing.T) {
	srv := uiTestServer(t) // no WithUI

	rec := doRequest(srv.Handler(), http.MethodGet, "/ui/", nil)

	assert.Equal(t, http.StatusNotFound, rec.Code,
		"without WithUI, /ui/ must be a clean 404 (route never registered)")
}

// TestWithoutUI_ExistingRoutesUnaffected guards that wiring (or not wiring) the
// UI scaffold never perturbs the existing surface — /healthz and the agents
// list keep responding exactly as before.
func TestWithoutUI_ExistingRoutesUnaffected(t *testing.T) {
	srv := uiTestServer(t) // no WithUI

	health := doRequest(srv.Handler(), http.MethodGet, "/healthz", nil)
	assert.Equal(t, http.StatusOK, health.Code, "/healthz must be unaffected by the UI scaffold")

	agents := doRequest(srv.Handler(), http.MethodGet, "/api/v1/agents", nil)
	assert.Equal(t, http.StatusOK, agents.Code, "GET /api/v1/agents must be unaffected by the UI scaffold")
}
