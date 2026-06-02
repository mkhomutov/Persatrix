package main

import (
	"flag"

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

// initUI returns the server option that serves the embedded web console, or
// nil when the console is disabled.
//
// When enableUI is false (the default), it returns no option, so the /ui/
// route is never registered (server.WithUI is never called) and no default
// runtime behaviour changes. When true, it wires the committed embedded asset
// tree — a placeholder until `make ui` (RFC 0048 Phase 1 PR 3) overwrites it
// with the real Svelte bundle, so a flag-on binary built without the JS
// toolchain serves a clear "run make ui" message instead of failing.
//
// Returns a slice (zero or one option) so the caller can append unconditionally,
// matching initChannels' shape.
//
// On the enabled path it emits a startup WARN — co-located with the wiring like
// channels.go's unauthenticated-surface warning — so the security posture is
// impossible to miss in an operator's first log scrape.
func initUI(enableUI bool, logger *zap.Logger) []server.ServerOption {
	if !enableUI {
		return nil
	}
	if logger != nil {
		logger.Warn("ui: web console ENABLED at /ui — the unauthenticated REST surface is now browser-discoverable; keep the listener on 127.0.0.1 and front with an authenticating reverse proxy before exposing beyond localhost. Auth lands in RFC 0039.",
			zap.String("rfc", "0048"),
			zap.String("auth_eta", "RFC 0039"),
		)
	}
	return []server.ServerOption{server.WithUI(ui.Assets())}
}
