package server

import (
	"net/http"
	"os"
	"runtime/debug"
)

// uiConfigResponse is the /api/v1/ui/config payload (RFC 0048 §C): which panels
// the console should render and what binary is serving them. The SPA boots off
// this — it renders only panels that are both enabled (operator toggle) and
// available (subsystem wired), and ignores panels it does not recognise.
type uiConfigResponse struct {
	Panels map[string]uiPanelStatus `json:"panels"`
	Build  uiBuildInfo              `json:"build"`
}

// uiPanelStatus pairs the operator-authored `enabled` toggle with the
// server-derived `available` flag. `available` is never read from YAML — see
// [Server.panelAvailable] and the config/ui.yaml schema's additionalProperties:false.
type uiPanelStatus struct {
	Enabled   bool `json:"enabled"`
	Available bool `json:"available"`
}

type uiBuildInfo struct {
	Version string `json:"version"`
}

// uiContextResponse is the /api/v1/ui/context payload (RFC 0048 §F): the single
// source of the console's identity. Today's no-auth localhost mode is the
// degenerate single-tenant case (principal=tenant=local, authenticated=false);
// PR 4's chat panel derives its user_id from `principal` here rather than
// prompting for or hard-coding a user, so the contract composes cleanly with
// RFC 0039 auth later.
type uiContextResponse struct {
	Principal     string `json:"principal"`
	Tenant        string `json:"tenant"`
	Authenticated bool   `json:"authenticated"`
}

// handleUIConfig serves the feature-toggle + build payload the SPA boots off.
// Registered only when the console is wired (WithUI), so it is a clean 404 when
// --enable-ui is off. Read-only; rides the existing /api/v1/* middleware stack.
func (s *Server) handleUIConfig(w http.ResponseWriter, _ *http.Request) {
	cfg := s.uiConfig
	if cfg == nil {
		cfg = DefaultUIConfig()
	}

	panels := make(map[string]uiPanelStatus, len(cfg.Panels))
	for name, toggle := range cfg.Panels {
		panels[name] = uiPanelStatus{
			Enabled:   toggle.Enabled,
			Available: s.panelAvailable(name),
		}
	}

	writeJSON(w, uiConfigResponse{
		Panels: panels,
		Build:  uiBuildInfo{Version: uiBuildVersion()},
	}, http.StatusOK)
}

// handleUIContext serves the console's identity (RFC 0048 §F). Constant today;
// the shape is fixed now so PR 4 and the eventual RFC 0039 auth layer slot in
// without a client change.
func (s *Server) handleUIContext(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, uiContextResponse{
		Principal:     "local",
		Tenant:        "local",
		Authenticated: false,
	}, http.StatusOK)
}

// panelAvailable reports whether a panel's backing subsystem is wired — the
// runtime-derived half of the toggle contract (RFC 0048 §C). A panel ships
// "dark" in YAML (enabled:false) and is flipped on per deployment; availability
// is orthogonal — it reflects whether the server *can* serve the panel's data
// regardless of the toggle. An unknown panel is unavailable (a server that does
// not know a panel cannot vouch for its backing subsystem).
func (s *Server) panelAvailable(name string) bool {
	switch name {
	case "chat":
		// The chat/agents surface is always registered (registry + planner are
		// required New args), so chat is always serveable.
		return true
	case "channel_timeline":
		// Mirrors the channel endpoints' 503 degradation: available exactly when
		// the channel store is wired (WithChannels).
		return s.channelStore != nil
	case "cost":
		return s.costReporter != nil
	case "memory_strip":
		// Deferred to Slice 2: needs a new Go↔Python persona-memory read method
		// that does not exist yet, so the panel can never be available in Slice 1
		// even if an operator flips its toggle on.
		return false
	default:
		return false
	}
}

// uiBuildVersion resolves the version string surfaced in /api/v1/ui/config.
// The orchestrator binary carries no compiled-in version constant, so it
// prefers the PERSATRIX_SERVICE_VERSION env var (the same source the
// observability runtimes read) and falls back to the module version stamped by
// `go install`/release builds, then to "dev" for a plain `go build`/`go test`.
func uiBuildVersion() string {
	if v := os.Getenv("PERSATRIX_SERVICE_VERSION"); v != "" {
		return v
	}
	if info, ok := debug.ReadBuildInfo(); ok {
		if v := info.Main.Version; v != "" && v != "(devel)" {
			return v
		}
	}
	return "dev"
}
