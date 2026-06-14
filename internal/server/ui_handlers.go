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
// `Create` is the optional nested structural-write capability (RFC 0048
// channel-creation amendment §A): present only for a panel that exposes a create
// affordance (today, channel_timeline) and omitted (omitempty) everywhere else,
// so an older client that does not know the key simply shows no create
// affordance — the §C graceful-degradation contract, unchanged.
type uiPanelStatus struct {
	Enabled    bool            `json:"enabled"`
	Available  bool            `json:"available"`
	Create     *uiCreateStatus `json:"create,omitempty"`
	ConfigEdit *uiCreateStatus `json:"config_edit,omitempty"`
}

// uiCreateStatus mirrors the panel `{enabled, available}` shape one level down
// for a per-panel capability affordance. It backs both the create affordance
// (RFC 0048 channel-creation amendment §A) and the RFC 0050 config-edit
// affordance: in each case `Enabled` echoes the operator's toggle
// (create_enabled / config_edit_enabled) and `Available` is runtime-derived
// (the console renders the affordance only when both are true), never authored.
type uiCreateStatus struct {
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
			Enabled:    toggle.Enabled,
			Available:  s.panelAvailable(name),
			Create:     s.panelCreate(name, toggle),
			ConfigEdit: s.panelConfigEdit(name, toggle),
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
	case "channel_timeline":
		// The consolidated conversation panel (RFC 0048 chat-panel-retirement
		// amendment — it hosts both group channels and DMs). Mirrors the channel
		// endpoints' 503 degradation: available exactly when the channel store is
		// wired (WithChannels).
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

// panelCreate reports a panel's structural-write (create) capability, or nil for
// panels that expose none (RFC 0048 channel-creation amendment §A). Today only
// channel_timeline carries one — group-channel creation over the already-exposed
// POST /api/v1/channels. `enabled` echoes the operator's create_enabled toggle;
// `available` is runtime-derived, mirroring channel_timeline's own availability:
// true exactly when the channel store is wired (WithChannels). That derivation
// is the forward-compat hook the amendment §D calls out — pre-auth it rides the
// localhost surface, and once /ui/context reports authenticated:true (RFC 0039)
// it becomes the seam where create.available is driven by a capability hint, so
// the toggle alone can never re-open creation to an unprivileged principal.
func (s *Server) panelCreate(name string, toggle PanelToggle) *uiCreateStatus {
	if name != "channel_timeline" {
		return nil
	}
	return &uiCreateStatus{
		Enabled:   toggle.CreateEnabled,
		Available: s.channelStore != nil,
	}
}

// panelConfigEdit reports a panel's governance-config edit capability (RFC 0050
// Phase 1), or nil for panels that expose none. Like [Server.panelCreate], only
// channel_timeline carries one — the PATCH/GET /api/v1/channels/{id}/config
// surface over the store-canonical apply path. `enabled` echoes the operator's
// config_edit_enabled toggle (ships OFF — the surface lands dark); `available`
// is runtime-derived and true only when the router is wired, because editing a
// live knob needs the apply path ([ChannelRouter.ApplyChannelConfig]), not just
// the store. So the affordance renders only when an operator opted in AND the
// channels subsystem can actually serve the edit — and the server-side gate
// ([Server.configEditEnabled]) enforces the same toggle on the endpoints, so a
// client that ignores this hint still cannot reach a dark surface.
func (s *Server) panelConfigEdit(name string, toggle PanelToggle) *uiCreateStatus {
	if name != "channel_timeline" {
		return nil
	}
	return &uiCreateStatus{
		Enabled:   toggle.ConfigEditEnabled,
		Available: s.channelRouter != nil,
	}
}

// defaultServiceVersion is the orchestrator binary's compiled-in service
// version, used by [uiBuildVersion] when neither the PERSATRIX_SERVICE_VERSION
// env var nor a `go install`/release module version is present (a plain
// `go build`/Docker image, the common deployment case). It mirrors the Python
// runtimes' _DEFAULT_SERVICE_VERSION so the console's reported version agrees
// with the observability stack's service.version. Like that constant it is not
// a build input, so a stale value never fails `make all` — scripts/bump_version.py
// bumps it each release (VERSION_FILES) or it silently drifts.
const defaultServiceVersion = "0.3.7"

// uiBuildVersion resolves the version string surfaced in /api/v1/ui/config. It
// prefers the PERSATRIX_SERVICE_VERSION env var (the same source the
// observability runtimes read), then the module version stamped by
// `go install`/release builds, and finally the compiled-in
// [defaultServiceVersion] — never an empty or "dev" placeholder, so an operator
// always sees a real version and it matches the Python runtimes' default.
func uiBuildVersion() string {
	if v := os.Getenv("PERSATRIX_SERVICE_VERSION"); v != "" {
		return v
	}
	if info, ok := debug.ReadBuildInfo(); ok {
		if v := info.Main.Version; v != "" && v != "(devel)" {
			return v
		}
	}
	return defaultServiceVersion
}
