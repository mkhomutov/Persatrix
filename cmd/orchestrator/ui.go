package main

import (
	"flag"
	"path/filepath"

	"go.uber.org/zap"

	"github.com/mkhomutov/persatrix/internal/server"
	"github.com/mkhomutov/persatrix/internal/ui"
)

// enableUI gates the embedded operator/tester web console (RFC 0048 Phase 1 /
// Slice 1). Defined here next to [initUI] rather than in main.go's flag block
// to keep all web-console wiring co-located, mirroring channels.go.
//
// Defaults OFF per RFC 0048 §Security: the console makes the unauthenticated
// REST surface browser-discoverable, so an operator must opt in. The
// orchestrator still binds 127.0.0.1; exposing the console beyond localhost
// requires an authenticating reverse proxy until RFC 0039 (auth) lands.
var enableUI = flag.Bool("enable-ui", false,
	"Serve the embedded web console at /ui (RFC 0048; localhost-only until RFC 0039 auth)")

// initUI returns the server options for the web console + its feature toggles.
//
// The two are deliberately split by their enablement:
//
//   - The config/ui.yaml feature toggles (server.WithUIConfig) are loaded
//     UNCONDITIONALLY, even with --enable-ui off. Those toggles gate non-web
//     surfaces too: the RFC 0050 `config_edit_enabled` knob gates the PATCH/GET
//     /api/v1/channels/{id}/config endpoints (and the PR 5 CLI that rides them),
//     which registerChannelRoutes mounts regardless of the console. Loading the
//     toggles only under --enable-ui would leave server.uiConfig nil on a
//     console-off deployment, so [server.Server.configEditEnabled] would fall
//     back to the default-OFF and pin the config-edit endpoints at 403 — an
//     operator could never opt the surface on without also exposing the browser
//     console. So the toggles ride along always; only the asset tree is gated.
//   - The embedded console (server.WithUI: static assets + the /api/v1/ui/*
//     boot endpoints) ships OFF by default (RFC 0048 §Security) — it makes the
//     unauthenticated REST surface browser-discoverable, so it is wired only
//     under --enable-ui. The assets are a placeholder until `make ui` (RFC 0048
//     Phase 1 PR 3) overwrites them with the real Svelte bundle, so a flag-on
//     binary built without the JS toolchain serves a clear "run make ui"
//     message instead of failing.
//
// config/ui.yaml is loaded from cfgDir with the channels.yaml-consistent
// posture: an absent file is the expected zero-config case (defaults apply); a
// malformed file is logged at WARN and soft-degrades to defaults so a config
// typo never blocks startup.
//
// Returns a slice (one option when the console is off — the toggles; two when
// on — toggles + assets) so the caller can append unconditionally, matching
// initChannels' shape.
//
// On the enabled path it emits a startup WARN — co-located with the wiring like
// channels.go's unauthenticated-surface warning — so the security posture is
// impossible to miss in an operator's first log scrape.
func initUI(enableUI bool, cfgDir string, logger *zap.Logger) []server.ServerOption {
	uiCfgPath := filepath.Join(cfgDir, "ui.yaml")
	uiCfg, err := server.LoadUIConfig(uiCfgPath)
	if err != nil {
		if logger != nil {
			logger.Warn("ui: config/ui.yaml load failed; falling back to Slice-1 panel defaults",
				zap.String("path", uiCfgPath),
				zap.Error(err),
			)
		}
		uiCfg = server.DefaultUIConfig()
	}
	// The toggles are always wired; the asset tree only when the console is on.
	opts := []server.ServerOption{server.WithUIConfig(uiCfg)}

	if !enableUI {
		return opts
	}
	if logger != nil {
		logger.Warn("ui: web console ENABLED at /ui — the unauthenticated REST surface is now browser-discoverable; keep the listener on 127.0.0.1 and front with an authenticating reverse proxy before exposing beyond localhost. Auth lands in RFC 0039.",
			zap.String("rfc", "0048"),
			zap.String("auth_eta", "RFC 0039"),
		)
	}
	return append(opts, server.WithUI(ui.Assets()))
}
