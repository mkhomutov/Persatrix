package main

import (
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.uber.org/zap"
	"go.uber.org/zap/zaptest/observer"

	"github.com/mkhomutov/persatrix/internal/planner"
	"github.com/mkhomutov/persatrix/internal/registry"
	"github.com/mkhomutov/persatrix/internal/server"
	"github.com/mkhomutov/persatrix/internal/state"
)

// RFC 0039 PR 3 — initAuth wiring (auth.go): config load posture, store
// creation, and the §H / §B3 startup WARNs.

func TestInitAuthZeroConfigDefaults(t *testing.T) {
	cfgDir := t.TempDir() // no security.yaml — the zero-config default
	dbPath := filepath.Join(t.TempDir(), "nested", "accounts.db")

	opts, cleanup, err := initAuth(cfgDir, dbPath, "127.0.0.1", "", zap.NewNop())
	require.NoError(t, err)
	defer cleanup()
	assert.Len(t, opts, 2, "sign-in, and the checks of where a request came from")
	assert.FileExists(t, dbPath, "the store opens (and creates parents) under both modes — Phase 1 ships the mechanism inert, not absent")
}

func TestInitAuthMalformedConfigFailsLoud(t *testing.T) {
	cfgDir := t.TempDir()
	require.NoError(t, os.WriteFile(filepath.Join(cfgDir, "security.yaml"),
		[]byte("auth:\n  mode: enbaled\n"), 0o600)) // the typo the loud-fail posture exists for

	_, _, err := initAuth(cfgDir, filepath.Join(t.TempDir(), "accounts.db"), "127.0.0.1", "", zap.NewNop())
	assert.Error(t, err, "a typo'd mode must fail startup, not silently boot unauthenticated")
}

// bareServer is an orchestrator REST server with nothing behind it but the
// options a test passes.
func bareServer(t *testing.T, opts ...server.ServerOption) *server.Server {
	t.Helper()
	logger := zap.NewNop()
	srv, err := server.New("127.0.0.1:0", t.TempDir(), state.NewInMemoryStore(logger),
		registry.NewInMemoryRegistry(logger), planner.NewYAMLPlanner(logger), logger, opts...)
	require.NoError(t, err)
	return srv
}

// statusUnderHost is how the handler answers a read of the agent list sent
// under the given Host.
func statusUnderHost(h http.Handler, host string) int {
	req := httptest.NewRequest(http.MethodGet, "/api/v1/agents", nil)
	req.Host = host
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	return rec.Code
}

func TestExpectedHosts(t *testing.T) {
	loopback := []string{"localhost", "127.0.0.1", "[::1]"}
	tests := []struct {
		name       string
		configured []string
		extra      string
		want       []string
		wantErr    string
	}{
		{name: "no flag", configured: loopback, want: loopback},
		{name: "one name", configured: loopback, extra: "orchestrator", want: []string{"localhost", "127.0.0.1", "[::1]", "orchestrator"}},
		{name: "several names, with spaces and an empty item", configured: loopback, extra: " orchestrator , persatrix.example,,",
			want: []string{"localhost", "127.0.0.1", "[::1]", "orchestrator", "persatrix.example"}},
		{name: "a name the config already lists, in another spelling", configured: loopback, extra: "LOCALHOST,::1", want: loopback},
		{name: "the flag adds to a configured list, it does not replace it", configured: []string{"persatrix.example"}, extra: "orchestrator",
			want: []string{"persatrix.example", "orchestrator"}},
		{name: "a name with a port", configured: loopback, extra: "orchestrator:8080",
			wantErr: `--http-extra-allowed-hosts: "orchestrator:8080" is not a host name or an IP address on its own (no scheme, port, path or wildcard)`},
		{name: "a URL", configured: loopback, extra: "orchestrator,http://orchestrator",
			wantErr: `--http-extra-allowed-hosts: "http://orchestrator" is not a host name`},
		{name: "a wildcard", configured: loopback, extra: "*", wantErr: `--http-extra-allowed-hosts: "*" is not a host name`},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got, err := expectedHosts(tt.configured, tt.extra)
			if tt.wantErr != "" {
				require.Error(t, err)
				assert.Contains(t, err.Error(), tt.wantErr)
				return
			}
			require.NoError(t, err)
			assert.Equal(t, tt.want, got)
		})
	}
}

// TestInitAuthSwitchesTheOriginChecksOn: the options initAuth returns make a
// server refuse a Host that is not expected, in both auth modes, and the
// start-up log names the list. The names come from the config and the flag.
func TestInitAuthSwitchesTheOriginChecksOn(t *testing.T) {
	tests := []struct {
		name     string
		security string // config/security.yaml; "" leaves the file out
		extra    string
		served   []string
		refused  []string
		logged   []any
	}{
		{
			name:    "no config file",
			served:  []string{"localhost:8080", "127.0.0.1:8080", "[::1]:8080"},
			refused: []string{"evil.example:8080", "orchestrator:8080"},
			logged:  []any{"localhost", "127.0.0.1", "[::1]"},
		},
		{
			name:     "sign-in on, no list",
			security: "auth:\n  mode: enabled\n",
			refused:  []string{"evil.example:8080", "orchestrator:8080"},
			logged:   []any{"localhost", "127.0.0.1", "[::1]"},
		},
		{
			name:    "the flag the compose file passes",
			extra:   "orchestrator",
			served:  []string{"localhost:8080", "orchestrator:8080"},
			refused: []string{"evil.example:8080"},
			logged:  []any{"localhost", "127.0.0.1", "[::1]", "orchestrator"},
		},
		{
			name:     "a configured list replaces the default",
			security: "auth:\n  allowed_hosts: [persatrix.example]\n",
			served:   []string{"persatrix.example", "persatrix.example:443"},
			refused:  []string{"localhost:8080", "127.0.0.1:8080", "evil.example"},
			logged:   []any{"persatrix.example"},
		},
		{
			name:     "a configured list and the flag",
			security: "auth:\n  allowed_hosts: [persatrix.example]\n",
			extra:    "orchestrator",
			served:   []string{"persatrix.example", "orchestrator:8080"},
			refused:  []string{"localhost:8080"},
			logged:   []any{"persatrix.example", "orchestrator"},
		},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			cfgDir := t.TempDir()
			if tt.security != "" {
				require.NoError(t, os.WriteFile(filepath.Join(cfgDir, "security.yaml"), []byte(tt.security), 0o600))
			}
			core, logs := observer.New(zap.InfoLevel)

			opts, cleanup, err := initAuth(cfgDir, filepath.Join(t.TempDir(), "accounts.db"), "127.0.0.1", tt.extra, zap.New(core))
			require.NoError(t, err)
			defer cleanup()
			handler := bareServer(t, opts...).Handler()

			for _, host := range tt.served {
				assert.NotEqual(t, http.StatusForbidden, statusUnderHost(handler, host), "Host %s", host)
			}
			for _, host := range tt.refused {
				assert.Equal(t, http.StatusForbidden, statusUnderHost(handler, host), "Host %s", host)
			}
			lines := logs.FilterMessage("auth: the REST API and the web console answer to these host names only, and a request under any other is refused " +
				"(auth.allowed_hosts in config/security.yaml, plus --http-extra-allowed-hosts)").All()
			require.Len(t, lines, 1)
			assert.Equal(t, tt.logged, lines[0].ContextMap()["allowed_hosts"])
		})
	}
}

// TestInitAuthRefusesABadExtraHost: a fault in the flag ends the start before
// the accounts store is opened, so nothing is left behind.
func TestInitAuthRefusesABadExtraHost(t *testing.T) {
	dbPath := filepath.Join(t.TempDir(), "accounts.db")

	_, _, err := initAuth(t.TempDir(), dbPath, "127.0.0.1", "orchestrator:8080", zap.NewNop())

	require.Error(t, err)
	assert.Contains(t, err.Error(), `--http-extra-allowed-hosts: "orchestrator:8080" is not a host name`)
	assert.NoFileExists(t, dbPath)
}

func TestBindIsLoopback(t *testing.T) {
	assert.True(t, bindIsLoopback("127.0.0.1"))
	assert.True(t, bindIsLoopback("127.0.0.1:8080"))
	assert.True(t, bindIsLoopback("localhost"))
	assert.True(t, bindIsLoopback("::1"))
	assert.False(t, bindIsLoopback("0.0.0.0"))
	assert.False(t, bindIsLoopback("192.168.1.5"))
	assert.False(t, bindIsLoopback(""), "unparsable → non-loopback so the WARN errs on caution")
}

func TestWarnAuthPosture(t *testing.T) {
	warns := func(cfg *server.AuthConfig, bind string) []string {
		core, logs := observer.New(zap.WarnLevel)
		warnAuthPosture(zap.New(core), cfg, bind)
		var msgs []string
		for _, e := range logs.All() {
			msgs = append(msgs, e.Message)
		}
		return msgs
	}

	disabled := server.DefaultAuthConfig()
	assert.Empty(t, warns(disabled, "127.0.0.1"), "loopback + disabled is the quiet default")
	assert.Len(t, warns(disabled, "0.0.0.0"), 1, "§H: disabled on a non-loopback bind WARNs")

	enabled := server.DefaultAuthConfig()
	enabled.Mode = server.AuthModeEnabled
	assert.Len(t, warns(enabled, "0.0.0.0"), 2,
		"enabled + non-loopback WARNs twice: §B3 no trusted_proxies + the PR 5 agent-ingress residual")
	assert.Empty(t, warns(enabled, "127.0.0.1"), "loopback + enabled needs no WARN")

	proxied := server.DefaultAuthConfig()
	proxied.Mode = server.AuthModeEnabled
	_, cidr, err := net.ParseCIDR("10.0.0.0/8")
	require.NoError(t, err)
	proxied.TrustedProxies = []*net.IPNet{cidr}
	got := warns(proxied, "0.0.0.0")
	require.Len(t, got, 1, "with trusted_proxies configured only the agent-ingress residual WARNs")
	assert.Contains(t, got[0], "agent-attributable")
}
