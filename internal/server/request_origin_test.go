package server

// request_origin_test.go: the two checks of where a request came from
// (ISSUE-0212). A request whose Host is not an expected name is refused, and
// so is a write that the browser marks as coming from another origin. Both
// hold whether sign-in is on or off.

import (
	"encoding/json"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"testing/fstest"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.uber.org/zap"
	"go.uber.org/zap/zapcore"
	"go.uber.org/zap/zaptest/observer"
)

const (
	crossSite  = "cross-site"
	sameOrigin = "same-origin"
	evilOrigin = "http://evil.example"
)

// withOriginChecks turns both checks on for a server a helper has already
// built, with the default names and any extra ones, and returns its handler.
func withOriginChecks(srv *Server, extra ...string) http.Handler {
	WithAllowedHosts(append(DefaultAllowedHosts(), extra...))(srv)
	return srv.Handler()
}

// sendFrom sends one request with no body, under the given Host and headers.
func sendFrom(h http.Handler, method, path, host string, headers map[string]string) *httptest.ResponseRecorder {
	req := httptest.NewRequest(method, path, nil)
	req.Host = host
	for k, v := range headers {
		req.Header.Set(k, v)
	}
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	return rec
}

// refusalOf decodes the error body of a refused request; a body that is not
// the JSON error envelope gives the zero value.
func refusalOf(rec *httptest.ResponseRecorder) errorResponse {
	var body errorResponse
	_ = json.Unmarshal(rec.Body.Bytes(), &body)
	return body
}

// originModes are the three ways a server can be set up for sign-in. The
// checks must not depend on which one it is.
func originModes(t *testing.T) map[string]func(extra ...string) http.Handler {
	t.Helper()
	withAuth := func(mode string) func(extra ...string) http.Handler {
		return func(extra ...string) http.Handler {
			cfg := DefaultAuthConfig()
			cfg.Mode = mode
			srv, _ := newAuthServer(t, cfg)
			return withOriginChecks(srv, extra...)
		}
	}
	return map[string]func(extra ...string) http.Handler{
		"no sign-in wired": func(extra ...string) http.Handler {
			srv, _ := testServer(t)
			return withOriginChecks(srv, extra...)
		},
		"sign-in off": withAuth(AuthModeDisabled),
		"sign-in on":  withAuth(AuthModeEnabled),
	}
}

func TestCanonicalHost(t *testing.T) {
	tests := []struct{ in, want string }{
		{"localhost", "localhost"},
		{"localhost:8080", "localhost"},
		{"localhost:", "localhost"},
		{"LocalHost:8080", "localhost"},
		{"orchestrator:8080", "orchestrator"},
		{"127.0.0.1", "127.0.0.1"},
		{"127.0.0.1:8080", "127.0.0.1"},
		{"[::1]", "[::1]"},
		{"[::1]:8080", "[::1]"},
		{"[0:0:0:0:0:0:0:1]:8080", "[::1]"},
		{"[2001:DB8::A]:443", "[2001:db8::a]"},
		{"::1", "[::1]"},
		{"", ""},
		// Spellings that are no name and no address stay as they came, so
		// they match nothing on the list.
		{"localhost.", "localhost."},
		{"localhost:80:80", "localhost:80:80"},
		{"localhost:http", "localhost:http"},
		{"localhost:123456", "localhost:123456"},
		{"[::1", "[::1"},
		{"[::1]x", "[::1]x"},
		{"[localhost]:8080", "[localhost]:8080"},
		{"127.1", "127.1"},
	}
	for _, tt := range tests {
		t.Run(tt.in, func(t *testing.T) {
			assert.Equal(t, tt.want, canonicalHost(tt.in))
		})
	}
}

func TestOriginChecks_Host(t *testing.T) {
	tests := []struct {
		name    string
		host    string
		listed  []string // names added to the default three
		refused bool
		quoted  string // the name the refusal shows
	}{
		{name: "localhost with a port", host: "localhost:8080"},
		{name: "localhost without a port", host: "localhost"},
		{name: "localhost on another port", host: "localhost:5173"},
		{name: "localhost in capitals", host: "LOCALHOST:8080"},
		{name: "the IPv4 loopback address", host: "127.0.0.1:8080"},
		{name: "the IPv6 loopback address", host: "[::1]:8080"},
		{name: "the IPv6 loopback address written out", host: "[0:0:0:0:0:0:0:1]:8080"},
		{name: "a listed name", host: "orchestrator:8080", listed: []string{"orchestrator"}},
		{name: "a listed name in capitals", host: "Orchestrator:8080", listed: []string{"orchestrator"}},
		{name: "a foreign name", host: "evil.example:8080", refused: true, quoted: "evil.example"},
		{name: "a foreign name without a port", host: "evil.example", refused: true, quoted: "evil.example"},
		{name: "a foreign name in capitals", host: "EVIL.example", refused: true, quoted: "evil.example"},
		{name: "the compose name when it is not listed", host: "orchestrator:8080", refused: true, quoted: "orchestrator"},
		{name: "a name that starts with an expected one", host: "localhost.evil.example", refused: true, quoted: "localhost.evil.example"},
		{name: "a name that ends with an expected one", host: "evil.localhost:8080", refused: true, quoted: "evil.localhost"},
		{name: "another loopback address", host: "127.0.0.2:8080", refused: true, quoted: "127.0.0.2"},
		{name: "another IPv6 address", host: "[::2]:8080", refused: true, quoted: "[::2]"},
		{name: "the IPv4 loopback address written as an IPv6 one", host: "[::ffff:127.0.0.1]:8080", refused: true, quoted: "[::ffff:127.0.0.1]"},
		{name: "an expected name with a trailing dot", host: "localhost.:8080", refused: true, quoted: "localhost."},
		{name: "an expected name with two ports", host: "localhost:80:80", refused: true, quoted: "localhost:80:80"},
		{name: "no Host", host: "", refused: true, quoted: ""},
	}
	for mode, build := range originModes(t) {
		served := http.StatusOK
		if mode == "sign-in on" {
			served = http.StatusUnauthorized // the route asks for a session; the Host was accepted
		}
		handlers := map[bool]http.Handler{false: build(), true: build("orchestrator")}
		for _, tt := range tests {
			t.Run(mode+"/"+tt.name, func(t *testing.T) {
				rec := sendFrom(handlers[len(tt.listed) > 0], http.MethodGet, "/api/v1/agents", tt.host, nil)
				if !tt.refused {
					assert.Equal(t, served, rec.Code, "body=%s", rec.Body.String())
					return
				}
				require.Equal(t, http.StatusForbidden, rec.Code, "body=%s", rec.Body.String())
				assert.Equal(t, errorResponse{
					Code:  "FORBIDDEN",
					Error: `host name "` + tt.quoted + `" is not expected: add it to auth.allowed_hosts in config/security.yaml`,
				}, refusalOf(rec))
			})
		}
	}
}

// TestOriginChecks_HostRefusalShowsAShortName: the refusal and the log line
// repeat what the client sent, so a long Host is cut.
func TestOriginChecks_HostRefusalShowsAShortName(t *testing.T) {
	core, logs := observer.New(zapcore.WarnLevel)
	srv, _ := testServer(t)
	srv.logger = zap.New(core)
	long := ""
	for len(long) < 300 {
		long += "a"
	}

	rec := sendFrom(withOriginChecks(srv), http.MethodGet, "/api/v1/agents", long+".example:8080", nil)

	require.Equal(t, http.StatusForbidden, rec.Code)
	shown := long[:64] + "…"
	assert.Equal(t, `host name "`+shown+`" is not expected: add it to auth.allowed_hosts in config/security.yaml`,
		refusalOf(rec).Error)
	require.Equal(t, 1, logs.Len(), "one warning for one refused request")
	entry := logs.All()[0]
	assert.Equal(t, "request refused: its Host is not an expected name (auth.allowed_hosts in config/security.yaml)", entry.Message)
	assert.Equal(t, shown, entry.ContextMap()["host"])
	assert.Equal(t, "/api/v1/agents", entry.ContextMap()["path"])
}

// TestOriginChecks_HealthRouteAnswersAnyHost: a liveness probe calls the
// health route with an address as Host (a Kubernetes probe sends the pod's
// address), and a refused probe is a restart loop. The route says only that
// the orchestrator is up, so it is the one route the Host check leaves out.
func TestOriginChecks_HealthRouteAnswersAnyHost(t *testing.T) {
	for mode, build := range originModes(t) {
		h := build()
		tests := []struct {
			name, method, path, host string
			want                     int
		}{
			{"a pod address", http.MethodGet, "/healthz", "10.42.0.17:8080", http.StatusOK},
			{"a foreign name", http.MethodGet, "/healthz", "evil.example", http.StatusOK},
			{"no Host", http.MethodGet, "/healthz", "", http.StatusOK},
			{"a HEAD probe", http.MethodHead, "/healthz", "10.42.0.17:8080", http.StatusOK},
			{"a path under it is not the health route", http.MethodGet, "/healthz/x", "evil.example", http.StatusForbidden},
			{"a path that starts like it is not the health route", http.MethodGet, "/healthzz", "evil.example", http.StatusForbidden},
			{"a path that ends like it is not the health route", http.MethodGet, "/api/v1/agents/healthz", "evil.example", http.StatusForbidden},
			{"the API root is not the health route", http.MethodGet, "/api/v1/agents", "10.42.0.17:8080", http.StatusForbidden},
		}
		for _, tt := range tests {
			t.Run(mode+"/"+tt.name, func(t *testing.T) {
				rec := sendFrom(h, tt.method, tt.path, tt.host, nil)
				assert.Equal(t, tt.want, rec.Code, "body=%s", rec.Body.String())
			})
		}
	}
}

// TestOriginChecks_NothingAPageAddsExcusesAForeignHost: a rebound page's
// script chooses the method and may add any header a script is allowed to
// set, and the browser adds its own marks. A write from such a page carries
// an Origin that names the page's own Host. None of that gets a foreign Host
// past the check, and none of it is read in place of the Host.
func TestOriginChecks_NothingAPageAddsExcusesAForeignHost(t *testing.T) {
	const evil = "evil.example:8080"
	browserMarks := map[string]string{"Sec-Fetch-Site": sameOrigin, "Sec-Fetch-Mode": "cors", "Sec-Fetch-Dest": "empty"}
	tests := []struct {
		name    string
		method  string
		headers map[string]string
	}{
		{"a write whose Origin names the page's own Host", http.MethodPost, map[string]string{"Origin": "http://" + evil}},
		{"a write the browser marks same-origin", http.MethodPost, map[string]string{"Origin": "http://" + evil, "Sec-Fetch-Site": sameOrigin}},
		{"a read with the marks a browser adds", http.MethodGet, browserMarks},
		{"X-Forwarded-Host names an expected host", http.MethodGet, map[string]string{"X-Forwarded-Host": "localhost"}},
		{"Forwarded names an expected host", http.MethodGet, map[string]string{"Forwarded": "host=localhost"}},
		{"an Authorization header", http.MethodGet, map[string]string{"Authorization": "Bearer anything"}},
		{"a cookie", http.MethodGet, map[string]string{"Cookie": sessionCookieName + "=anything"}},
		{"a JSON content type", http.MethodPost, map[string]string{"Content-Type": "application/json", "Origin": "http://" + evil}},
		{"an OPTIONS request", http.MethodOptions, nil},
		{"a DELETE", http.MethodDelete, map[string]string{"Origin": "http://" + evil}},
	}
	for mode, build := range originModes(t) {
		h := build()
		for _, tt := range tests {
			t.Run(mode+"/"+tt.name, func(t *testing.T) {
				rec := sendFrom(h, tt.method, "/api/v1/agents", evil, tt.headers)
				require.Equal(t, http.StatusForbidden, rec.Code, "body=%s", rec.Body.String())
				assert.Equal(t, `host name "evil.example" is not expected: add it to auth.allowed_hosts in config/security.yaml`,
					refusalOf(rec).Error)
			})
		}

		// Control, and the other half of "not read in place of the Host":
		// a forwarded-host header that names a foreign host does not get a
		// request under an expected Host refused.
		for name, headers := range map[string]map[string]string{
			"X-Forwarded-Host names a foreign host": {"X-Forwarded-Host": "evil.example"},
			"Forwarded names a foreign host":        {"Forwarded": "host=evil.example"},
		} {
			t.Run(mode+"/under an expected Host, "+name, func(t *testing.T) {
				rec := sendFrom(h, http.MethodGet, "/api/v1/agents", "localhost:8080", headers)
				assert.NotEqual(t, http.StatusForbidden, rec.Code, "body=%s", rec.Body.String())
			})
		}
	}
}

// TestOriginChecks_HostCoversEveryOtherSurface: the console's files, the
// sign-in routes and the event streams sit on the root mux beside the API,
// and the Host check stands in front of all of them.
func TestOriginChecks_HostCoversEveryOtherSurface(t *testing.T) {
	cfg := DefaultAuthConfig()
	srv, _ := newAuthServer(t, cfg, WithUI(fstest.MapFS{"index.html": {Data: []byte("<html>console</html>")}}))
	h := withOriginChecks(srv)

	tests := []struct {
		name, method, path string
		servedAs           int
	}{
		{"the console", http.MethodGet, "/ui/", http.StatusOK},
		{"the console's settings", http.MethodGet, "/api/v1/ui/config", http.StatusOK},
		{"who am I", http.MethodGet, "/api/v1/auth/whoami", http.StatusOK},
		{"sign-in", http.MethodPost, "/api/v1/auth/login", http.StatusBadRequest},
		{"a log stream", http.MethodGet, "/api/v1/executions/exec-1/logs/stream", http.StatusNotImplemented},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			refused := sendFrom(h, tt.method, tt.path, "evil.example:8080", nil)
			assert.Equal(t, http.StatusForbidden, refused.Code)
			assert.Contains(t, refusalOf(refused).Error, "auth.allowed_hosts")

			served := sendFrom(h, tt.method, tt.path, "localhost:8080", nil)
			assert.Equal(t, tt.servedAs, served.Code, "the same request under an expected name reaches its route: %s", served.Body.String())
		})
	}
}

// TestOriginChecks_OverARealConnection sends raw requests, because what Go's
// server makes of the request line and the headers decides which Host the
// check sees. Each read is bounded.
func TestOriginChecks_OverARealConnection(t *testing.T) {
	srv, _ := testServer(t)
	hs := httptest.NewServer(withOriginChecks(srv))
	defer hs.Close()
	addr := hs.Listener.Addr().String() // 127.0.0.1:<port>

	tests := []struct {
		name string
		raw  string
		want string // how the answer starts
	}{
		{
			name: "the server's own address as Host",
			raw:  "GET /api/v1/agents HTTP/1.1\r\nHost: " + addr + "\r\nConnection: close\r\n\r\n",
			want: "HTTP/1.1 200 ",
		},
		{
			name: "a foreign Host",
			raw:  "GET /api/v1/agents HTTP/1.1\r\nHost: evil.example\r\nConnection: close\r\n\r\n",
			want: "HTTP/1.1 403 ",
		},
		{
			name: "HTTP/1.0 with no Host header",
			raw:  "GET /api/v1/agents HTTP/1.0\r\n\r\n",
			want: "HTTP/1.0 403 ",
		},
		{
			name: "HTTP/1.0 with no Host header, the health route",
			raw:  "GET /healthz HTTP/1.0\r\n\r\n",
			want: "HTTP/1.0 200 ",
		},
		{
			// Go answers this itself; no handler runs.
			name: "HTTP/1.1 with no Host header",
			raw:  "GET /api/v1/agents HTTP/1.1\r\nConnection: close\r\n\r\n",
			want: "HTTP/1.1 400 ",
		},
		{
			// The name in a full request URL is the Host a handler sees,
			// whatever the Host header says (RFC 9112 section 3.2.2).
			name: "a full URL with a foreign name, and an expected Host header",
			raw:  "GET http://evil.example/api/v1/agents HTTP/1.1\r\nHost: " + addr + "\r\nConnection: close\r\n\r\n",
			want: "HTTP/1.1 403 ",
		},
		{
			name: "a full URL with an expected name, and a foreign Host header",
			raw:  "GET http://" + addr + "/api/v1/agents HTTP/1.1\r\nHost: evil.example\r\nConnection: close\r\n\r\n",
			want: "HTTP/1.1 200 ",
		},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			conn, err := net.DialTimeout("tcp", addr, 5*time.Second)
			require.NoError(t, err)
			defer conn.Close() //nolint:errcheck
			require.NoError(t, conn.SetDeadline(time.Now().Add(5*time.Second)))
			_, err = conn.Write([]byte(tt.raw))
			require.NoError(t, err)
			answer, err := io.ReadAll(conn)
			require.NoError(t, err)
			assert.True(t, strings.HasPrefix(string(answer), tt.want), "want %q, got:\n%s", tt.want, answer)
		})
	}
}

// TestWithAllowedHosts: the option takes names as an operator writes them,
// leaves out what is not a name, and with nothing left refuses everything but
// a health probe. It never falls back to serving every Host: given no names
// at all it refuses in the same way, so a caller that builds its list wrongly
// cannot switch the checks off.
func TestWithAllowedHosts(t *testing.T) {
	serve := func(names ...string) http.Handler {
		srv, _ := testServer(t)
		WithAllowedHosts(names)(srv)
		return srv.Handler()
	}
	status := func(h http.Handler, path, host string) int {
		return sendFrom(h, http.MethodGet, path, host, nil).Code
	}

	h := serve("Persatrix.Example", "::1", "not a name", "")
	assert.Equal(t, http.StatusOK, status(h, "/api/v1/agents", "persatrix.example:8080"))
	assert.Equal(t, http.StatusOK, status(h, "/api/v1/agents", "[::1]:8080"))
	assert.Equal(t, http.StatusForbidden, status(h, "/api/v1/agents", "localhost:8080"), "a list replaces the default names")
	assert.Equal(t, http.StatusForbidden, status(h, "/api/v1/agents", ""), "an empty entry allows no empty Host")
	assert.Equal(t, http.StatusForbidden, status(h, "/api/v1/agents", "not a name"))

	nothingLeft := map[string]http.Handler{
		"no entry is a name": serve("", "localhost:8080"),
		"no names":           serve(),
		"an empty list": func() http.Handler {
			srv, _ := testServer(t)
			WithAllowedHosts([]string{})(srv)
			return srv.Handler()
		}(),
	}
	for name, h := range nothingLeft {
		t.Run(name, func(t *testing.T) {
			assert.Equal(t, http.StatusForbidden, status(h, "/api/v1/agents", "localhost:8080"))
			assert.Equal(t, http.StatusForbidden, status(h, "/api/v1/agents", "localhost"))
			assert.Equal(t, http.StatusForbidden, status(h, "/api/v1/agents", "evil.example"))
			assert.Equal(t, http.StatusForbidden, status(h, "/api/v1/agents", ""))
			assert.Equal(t, http.StatusOK, status(h, "/healthz", "localhost"))
			rec := sendFrom(h, http.MethodPost, "/api/v1/agents/a/unquarantine", "localhost", nil)
			assert.Contains(t, refusalOf(rec).Error, "auth.allowed_hosts", "the refusal still names the setting")
		})
	}
}

// TestOriginChecks_OffWithoutTheOption: a server built without the option
// runs neither check, as a server built without WithAuth runs no sign-in.
// That is the only way to have none: the option switches both on whatever it
// is given (TestWithAllowedHosts). The orchestrator always passes the option
// (cmd/orchestrator, initAuth).
func TestOriginChecks_OffWithoutTheOption(t *testing.T) {
	srv, _ := testServer(t)
	h := srv.Handler()
	assert.Equal(t, http.StatusOK, sendFrom(h, http.MethodGet, "/api/v1/agents", "evil.example", nil).Code)
	rec := sendFrom(h, http.MethodPost, "/api/v1/agents/a/unquarantine", "localhost", formPost)
	assert.NotEqual(t, crossOriginRefusalText, refusalOf(rec).Error)
}
