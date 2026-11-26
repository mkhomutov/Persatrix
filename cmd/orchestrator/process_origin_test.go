package main

// Real-process tests of the two checks of where a request came from
// (ISSUE-0212). The checks live in internal/server and are tested there; what
// only a real process shows is that main() switches them on, with the names
// the shipped config and the shipped compose file give it.

import (
	"io"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.uber.org/zap"
	"gopkg.in/yaml.v3"
)

// extraHostsFlag is the flag the compose file passes to add the name its
// agents call the orchestrator by.
const extraHostsFlag = "--http-extra-allowed-hosts"

const (
	hostRefusalFor     = `host name \"%s\" is not expected: add it to auth.allowed_hosts in config/security.yaml`
	crossOriginRefusal = "cross-origin write refused: the browser reports that this request comes from another origin"
)

// crossSiteForm is what a browser sends when a page on another site submits
// a plain HTML form to the orchestrator.
var crossSiteForm = map[string]string{
	"Content-Type":   "application/x-www-form-urlencoded",
	"Origin":         "http://evil.example",
	"Sec-Fetch-Site": "cross-site",
}

// call sends one request to the process's REST port under the given Host and
// returns the status and the body. The wait is bounded by the client.
func (p *orchestratorProcess) call(method, path, host string, headers map[string]string, body string) (int, string) {
	p.t.Helper()
	req, err := http.NewRequest(method, "http://"+p.httpAddr+path, strings.NewReader(body))
	require.NoError(p.t, err)
	req.Host = host
	for k, v := range headers {
		req.Header.Set(k, v)
	}
	client := &http.Client{Timeout: processLimit}
	resp, err := client.Do(req)
	require.NoError(p.t, err, "%s", p.output)
	defer resp.Body.Close() //nolint:errcheck
	got, err := io.ReadAll(resp.Body)
	require.NoError(p.t, err)
	return resp.StatusCode, string(got)
}

// composeFile reads one compose file of the repository.
func composeFile(t *testing.T, name string) map[string]any {
	t.Helper()
	data, err := os.ReadFile(filepath.Join(repoRoot(t), name)) //nolint:gosec // the repository's own file
	require.NoError(t, err)
	var doc map[string]any
	require.NoError(t, yaml.Unmarshal(data, &doc))
	return doc
}

// serviceCommands returns each service's command line in a compose file.
func serviceCommands(t *testing.T, doc map[string]any) map[string][]string {
	t.Helper()
	out := map[string][]string{}
	services, _ := doc["services"].(map[string]any)
	for name, raw := range services {
		svc, _ := raw.(map[string]any)
		items, ok := svc["command"].([]any)
		if !ok {
			continue
		}
		for _, item := range items {
			word, isString := item.(string)
			require.True(t, isString, "service %s: a command word that is not a string", name)
			out[name] = append(out[name], word)
		}
	}
	return out
}

// flagValue returns the word after flag in a command line, "" when absent.
func flagValue(command []string, flag string) string {
	for i, word := range command {
		if word == flag && i+1 < len(command) {
			return command[i+1]
		}
	}
	return ""
}

// composeExtraHosts is what the shipped compose file passes for
// extraHostsFlag.
func composeExtraHosts(t *testing.T) string {
	t.Helper()
	command := serviceCommands(t, composeFile(t, "docker-compose.yaml"))["orchestrator"]
	require.NotEmpty(t, command, "docker-compose.yaml gives the orchestrator a command")
	return flagValue(command, extraHostsFlag)
}

// TestOrchestratorProcess_RefusesAnUnexpectedHost: started on the shipped
// config and nothing else, as `make run` starts it, the orchestrator answers
// to the three loopback names and refuses every other Host with a message
// that names the setting. The health route answers a probe under any Host.
//
// Before ISSUE-0212's fix every row was served: a page whose own name had
// been pointed at 127.0.0.1 read the API as if it were the operator.
func TestOrchestratorProcess_RefusesAnUnexpectedHost(t *testing.T) {
	p := startOrchestrator(t, shippedConfig(t), filepath.Join(t.TempDir(), "logs"))
	p.healthz()
	_, port, found := strings.Cut(p.httpAddr, ":")
	require.True(t, found)

	tests := []struct {
		name, path, host string
		want             int
		refusedName      string
	}{
		{name: "localhost", path: "/api/v1/agents", host: "localhost:" + port, want: http.StatusOK},
		{name: "the IPv4 loopback address", path: "/api/v1/agents", host: "127.0.0.1:" + port, want: http.StatusOK},
		{name: "the IPv6 loopback address", path: "/api/v1/agents", host: "[::1]:" + port, want: http.StatusOK},
		{name: "a rebound page's own name", path: "/api/v1/agents", host: "evil.example:" + port, want: http.StatusForbidden, refusedName: "evil.example"},
		{name: "the channels of a rebound page", path: "/api/v1/channels", host: "evil.example:" + port, want: http.StatusForbidden, refusedName: "evil.example"},
		{name: "the compose name, outside compose", path: "/api/v1/agents", host: "orchestrator:8080", want: http.StatusForbidden, refusedName: "orchestrator"},
		{name: "a probe by address", path: "/healthz", host: "10.42.0.17:8080", want: http.StatusOK},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			status, body := p.call(http.MethodGet, tt.path, tt.host, nil, "")
			assert.Equal(t, tt.want, status, "body=%s", body)
			if tt.refusedName != "" {
				assert.Contains(t, body, strings.Replace(hostRefusalFor, "%s", tt.refusedName, 1))
			}
		})
	}

	assert.Equal(t, 0, p.stop(), "%s", p.output)
	assert.Contains(t, p.output.String(),
		"auth: the REST API and the web console answer to these host names only, and a request under any other is refused "+
			"(auth.allowed_hosts in config/security.yaml, plus --http-extra-allowed-hosts)")
	assert.Contains(t, p.output.String(), `"allowed_hosts":["localhost","127.0.0.1","[::1]"]`)
}

// TestOrchestratorProcess_RefusesACrossSiteFormPost: the three routes that
// take no body are refused to a form on another site, and the session such a
// form aimed at stays as it was. The same calls from a client that is not a
// browser go through to their routes.
//
// Before ISSUE-0212's fix each form post reached its route: the session was
// archived, and the other two answered as they answer the operator.
func TestOrchestratorProcess_RefusesACrossSiteFormPost(t *testing.T) {
	p := startOrchestrator(t, shippedConfig(t), filepath.Join(t.TempDir(), "logs"))
	p.healthz()
	host := "localhost:8080"

	status, body := p.call(http.MethodPost, "/api/v1/sessions", host,
		map[string]string{"Content-Type": "application/json"}, `{"label":"kept"}`)
	require.Equal(t, http.StatusCreated, status, "body=%s", body)
	archivedNow := func() bool {
		status, body := p.call(http.MethodGet, "/api/v1/sessions/kept", host, nil, "")
		require.Equal(t, http.StatusOK, status, "body=%s", body)
		return strings.Contains(body, `"archived":true`)
	}

	routes := []struct{ name, path string }{
		{"session archive", "/api/v1/sessions/kept/archive"},
		{"unquarantine", "/api/v1/agents/planner/unquarantine"},
		{"convene", "/api/v1/channels/group:roundtable/convene"},
	}
	for _, route := range routes {
		t.Run(route.name+", from a form on another site", func(t *testing.T) {
			status, body := p.call(http.MethodPost, route.path, host, crossSiteForm, "")
			assert.Equal(t, http.StatusForbidden, status, "body=%s", body)
			assert.Contains(t, body, crossOriginRefusal)
		})
	}
	assert.False(t, archivedNow(), "the refused form archived nothing")

	for _, route := range routes {
		t.Run(route.name+", from a client that is not a browser", func(t *testing.T) {
			status, body := p.call(http.MethodPost, route.path, host, nil, "")
			assert.NotEqual(t, http.StatusForbidden, status, "body=%s", body)
			assert.NotContains(t, body, crossOriginRefusal)
		})
	}
	assert.True(t, archivedNow(), "control: the plain call archived the session")
}

// TestOrchestratorProcess_ServesTheComposeFleet: started with the flag value
// the shipped compose file passes, the orchestrator serves the calls an agent
// makes under the name the compose file gives every agent, and still refuses
// a rebound page.
func TestOrchestratorProcess_ServesTheComposeFleet(t *testing.T) {
	extra := composeExtraHosts(t)
	require.Equal(t, "orchestrator", extra, "docker-compose.yaml passes %s", extraHostsFlag)
	p := startOrchestratorOn(t, shippedConfig(t), filepath.Join(t.TempDir(), "logs"), freeLoopbackPort(t),
		extraHostsFlag, extra)
	p.healthz()
	const fleetHost = "orchestrator:8080"
	jsonBody := map[string]string{"Content-Type": "application/json"}

	// What agents/server.py posts when an agent starts.
	status, body := p.call(http.MethodPost, "/api/v1/agents/register", fleetHost, jsonBody,
		`{"id":"probe-agent","name":"Probe","type":"task","role":"","address":"agent-probe:50051","capabilities":["probe"]}`)
	assert.Equal(t, http.StatusCreated, status, "an agent registers under the compose name: %s", body)

	status, body = p.call(http.MethodGet, "/api/v1/agents/probe-agent", fleetHost, nil, "")
	assert.Equal(t, http.StatusOK, status, "body=%s", body)
	status, body = p.call(http.MethodGet, "/api/v1/channels", fleetHost, nil, "")
	assert.Equal(t, http.StatusOK, status, "an agent reads its channels under the compose name: %s", body)
	status, body = p.call(http.MethodDelete, "/api/v1/agents/probe-agent", fleetHost, nil, "")
	assert.Equal(t, http.StatusNoContent, status, "an agent deregisters under the compose name: %s", body)

	// The host's own names still work, as the health check inside the
	// container and a browser on the host use them.
	status, _ = p.call(http.MethodGet, "/api/v1/agents", "localhost:8080", nil, "")
	assert.Equal(t, http.StatusOK, status)
	status, body = p.call(http.MethodGet, "/api/v1/agents", "evil.example:8080", nil, "")
	assert.Equal(t, http.StatusForbidden, status, "body=%s", body)

	assert.Equal(t, 0, p.stop(), "%s", p.output)
	assert.Contains(t, p.output.String(), `"allowed_hosts":["localhost","127.0.0.1","[::1]","orchestrator"]`)
}

// TestOrchestratorProcess_RefusesToStartOnABadExtraHost: a value of the flag
// that is not a list of names ends the start with exit status 1 and a line
// that names the flag and the entry.
func TestOrchestratorProcess_RefusesToStartOnABadExtraHost(t *testing.T) {
	p := startOrchestratorOn(t, shippedConfig(t), filepath.Join(t.TempDir(), "logs"), freeLoopbackPort(t),
		extraHostsFlag, "orchestrator:8080")

	assert.Equal(t, 1, p.waitExit(), "%s", p.output)
	assert.Contains(t, p.output.String(), "auth: init failed")
	assert.Contains(t, p.output.String(),
		`--http-extra-allowed-hosts: \"orchestrator:8080\" is not a host name or an IP address on its own`)
}

// TestComposeFiles_EveryAgentCallsTheOrchestratorByAnExpectedName reads every
// compose file of the repository. Each `--orchestrator-url` an agent is given
// must name a host the orchestrator answers to with the shipped config and
// the shipped orchestrator command, and no overlay may replace that command,
// which would drop the flag.
func TestComposeFiles_EveryAgentCallsTheOrchestratorByAnExpectedName(t *testing.T) {
	files, err := filepath.Glob(filepath.Join(repoRoot(t), "docker-compose*.yaml"))
	require.NoError(t, err)
	require.GreaterOrEqual(t, len(files), 8, "the base file and its seven overlays")

	opts, cleanup, err := initAuth(shippedConfig(t), filepath.Join(t.TempDir(), "accounts.db"),
		"0.0.0.0", composeExtraHosts(t), zap.NewNop())
	require.NoError(t, err)
	defer cleanup()
	handler := bareServer(t, opts...).Handler()

	urls := 0
	for _, file := range files {
		name := filepath.Base(file)
		for service, command := range serviceCommands(t, composeFile(t, name)) {
			if service == "orchestrator" {
				assert.Equal(t, "docker-compose.yaml", name,
					"only the base file may set the orchestrator's command: an overlay that sets it drops %s", extraHostsFlag)
				continue
			}
			url := flagValue(command, "--orchestrator-url")
			if url == "" {
				continue
			}
			urls++
			host, isHTTP := strings.CutPrefix(url, "http://")
			require.True(t, isHTTP, "%s, %s: %s", name, service, url)
			assert.Equal(t, http.StatusOK, statusUnderHost(handler, host),
				"%s, service %s: the orchestrator refuses the Host of %s", name, service, url)
		}
	}
	assert.GreaterOrEqual(t, urls, 7, "the six agents of the base file and the one the multivendor overlay adds")

	// Positive control: the same handler refuses a name nobody listed, so
	// the 200s above are not a check that is switched off.
	assert.Equal(t, http.StatusForbidden, statusUnderHost(handler, "agent-planner:8080"))
}
