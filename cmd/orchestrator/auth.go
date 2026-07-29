package main

import (
	"flag"
	"net"
	"os"
	"path/filepath"

	"go.uber.org/zap"

	"github.com/mkhomutov/persatrix/internal/accounts"
	"github.com/mkhomutov/persatrix/internal/server"
)

// accountsDB locates the RFC 0039 accounts & sessions store — the
// second orchestrator-owned SQLite database, beside channels.db.
// Defined here next to initAuth to keep the auth wiring co-located,
// mirroring channels.go / ui.go.
var accountsDB = flag.String("accounts-db", "data/accounts.db",
	"SQLite path for RFC 0039 accounts & sessions")

// initAuth wires the RFC 0039 auth subsystem (PR 3 — Phase 1, inert):
// loads the config/security.yaml `auth:` block, opens the accounts
// store, and builds the password authenticator at the configured
// Argon2id cost.
//
// Failure posture is asymmetric on purpose:
//   - A MISSING security.yaml is the zero-config default (auth
//     disabled, nothing enforced).
//   - A PRESENT-but-malformed file, or an unopenable store, is
//     returned as an error and main Fatals — an operator who authored
//     `mode: enabled` with a typo must not silently boot an
//     unauthenticated deployment (the channels.yaml loud-fail posture,
//     opposite of ui.yaml's soft-degrade).
//
// The store opens under BOTH auth modes: the login/logout/whoami
// endpoints function under `disabled` too (Phase 1 ships the complete
// mechanism inert — new routes only, no existing behaviour changes).
func initAuth(cfgDir, accountsPath, httpBind string, logger *zap.Logger) ([]server.ServerOption, func(), error) {
	authCfg, err := server.LoadSecurityConfig(filepath.Join(cfgDir, "security.yaml"))
	if err != nil {
		return nil, nil, err
	}
	// channels.db's data/ dir may not exist on a fresh checkout when the
	// operator pointed --accounts-db elsewhere; creating the parent is
	// cheaper than a new startup failure mode.
	if dir := filepath.Dir(accountsPath); dir != "." && dir != "" {
		if err := os.MkdirAll(dir, 0o755); err != nil {
			return nil, nil, err
		}
	}
	store, err := accounts.Open(accountsPath)
	if err != nil {
		return nil, nil, err
	}
	warnAuthPosture(logger, authCfg, httpBind)

	authenticator := accounts.NewPasswordAuthenticator(store, authCfg.Argon)
	opts := []server.ServerOption{server.WithAuth(store, authenticator, authCfg)}
	return opts, func() { _ = store.Close() }, nil
}

// warnAuthPosture emits the RFC 0039 §H / amendment §B3 startup WARNs —
// the RFC 0009/0011 trust-boundary pattern: the posture must be
// impossible to miss in an operator's first log scrape.
func warnAuthPosture(logger *zap.Logger, cfg *server.AuthConfig, httpBind string) {
	if logger == nil {
		return
	}
	loopback := bindIsLoopback(httpBind)
	if cfg.Mode == server.AuthModeDisabled && !loopback {
		logger.Warn("auth: mode is DISABLED on a non-loopback bind — the REST surface is unauthenticated beyond localhost; set auth.mode: enabled in config/security.yaml (RFC 0039 §H)",
			zap.String("http_bind", httpBind),
		)
	}
	if cfg.Mode == server.AuthModeEnabled && !loopback && len(cfg.TrustedProxies) == 0 {
		logger.Warn("auth: enabled on a non-loopback bind with no auth.trusted_proxies — behind a reverse proxy the per-source login limiter degrades to a global one (amendment §B3); configure trusted_proxies for the proxy's address",
			zap.String("http_bind", httpBind),
		)
	}
	if cfg.Mode == server.AuthModeEnabled && !loopback {
		logger.Warn("auth: the agent-attributable REST ingress (agent register/deregister, channel list/history/publish, convene) stays UNGATED under enabled — the persona fleet holds no accounts (RFC 0039 §Non-Goals); its authorization story is RFC 0009 agent tokens. Keep that surface network-restricted to the agent fleet",
			zap.String("http_bind", httpBind),
		)
	}
}

// bindIsLoopback reports whether a bind address string names a loopback
// interface. An unparsable value is treated as non-loopback so the WARN
// fires on the side of caution.
func bindIsLoopback(bind string) bool {
	host := bind
	if h, _, err := net.SplitHostPort(bind); err == nil {
		host = h
	}
	if host == "localhost" {
		return true
	}
	ip := net.ParseIP(host)
	return ip != nil && ip.IsLoopback()
}
