package server

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// RFC 0039 enabled-mode exposure amendment §A3 — the console security
// headers ui.go sets from this PR on (it set none before).

func TestUISecurityHeadersOnConsoleSurface(t *testing.T) {
	srv := uiTestServer(t, WithUI(uiAssetFS()))
	h := srv.Handler()

	for _, path := range []string{"/ui/", "/api/v1/ui/config", "/api/v1/ui/context"} {
		t.Run(path, func(t *testing.T) {
			rec := httptest.NewRecorder()
			h.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, path, nil))
			require.Equal(t, http.StatusOK, rec.Code)

			csp := rec.Header().Get("Content-Security-Policy")
			assert.Equal(t, uiCSP, csp)
			// The two load-bearing directives, pinned individually so a
			// future CSP edit cannot silently drop them: no inline
			// scripts (XSS probability), no framing (clickjacking).
			assert.Contains(t, csp, "frame-ancestors 'none'")
			var scriptSrc string
			for _, directive := range strings.Split(csp, ";") {
				if d := strings.TrimSpace(directive); strings.HasPrefix(d, "script-src") {
					scriptSrc = d
				}
			}
			assert.Equal(t, "script-src 'self'", scriptSrc,
				"script-src must never gain 'unsafe-inline' — that is a build fix, not a CSP relaxation (§A3)")

			assert.Equal(t, "nosniff", rec.Header().Get("X-Content-Type-Options"))
			assert.Equal(t, "same-origin", rec.Header().Get("Referrer-Policy"))
		})
	}
}

func TestUISecurityHeadersScopedToConsole(t *testing.T) {
	// The §A3 headers are the console's, not the API's: a JSON route
	// outside ui.go stays untouched (Phase 1 inertness — existing
	// response bytes unchanged).
	srv := uiTestServer(t, WithUI(uiAssetFS()))
	rec := httptest.NewRecorder()
	srv.Handler().ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/healthz", nil))
	require.Equal(t, http.StatusOK, rec.Code)
	assert.Empty(t, rec.Header().Get("Content-Security-Policy"))
}
