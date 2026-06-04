package main

import (
	"context"
	"errors"
	"flag"
	"io/fs"
	"os"
	"path/filepath"

	"go.uber.org/zap"

	"github.com/mkhomutov/persatrix/internal/channels"
	obsmetrics "github.com/mkhomutov/persatrix/internal/observability/metrics"
	"github.com/mkhomutov/persatrix/internal/registry"
	"github.com/mkhomutov/persatrix/internal/server"
)

// channelsDB is the SQLite path backing the RFC 0011 channels subsystem.
// Defined here (next to [initChannels]) rather than in main.go's flag
// block to keep all channels-specific wiring co-located.
var channelsDB = flag.String("channels-db", "data/channels.db", "SQLite path for RFC 0011 channels")

// initChannels brings the RFC 0011 channels subsystem online.
//
// Loads `<configDir>/channels.yaml`, opens the SQLite-backed store at
// `dbPath`, builds the router with the registry-aware
// [channels.GRPCMessageDispatcher] (PR 4a-ii-β-1; replaced the PR-2
// [channels.NoopDispatcher] placeholder), and reconciles config-vs-store
// membership at startup. A loud reconcile failure (per RFC 0011 §B
// coexistence rules) is reported via the returned error so the caller
// can `Fatal` — operators must reconcile by editing channels.yaml or
// `DELETE /api/v1/channels/{id}` (deferred to PR 4b).
//
// `reg` is the orchestrator's agent registry, consulted per-recipient by
// the dispatcher to translate participant IDs into dialable gRPC
// addresses. Nil disables cross-process delivery (the router falls back
// to [channels.NoopDispatcher] so member lookups + persistence still
// work for tests / channels-disabled deployments).
//
// Returns:
//
//   - opts: zero or one `server.WithChannels(...)` options to append to
//     the orchestrator's option slice. Empty when channels.yaml is
//     absent or the store cannot open (channel REST endpoints then
//     return 503; the orchestrator's other surfaces are unaffected).
//   - cleanup: always non-nil; a no-op on the disabled path so callers
//     can `defer cleanup()` unconditionally.
//   - err: non-nil only on a hard reconcile failure (operator action
//     required); nil on the soft "channels disabled" path.
func initChannels(
	cfgDir, dbPath, sessionID, epochID string,
	orchMetrics *obsmetrics.Instruments,
	reg registry.Registry,
	logger *zap.Logger,
) (opts []server.ServerOption, cleanup func(), err error) {
	noop := func() {}
	channelsCfgPath := filepath.Join(cfgDir, "channels.yaml")
	chanCfg, cfgErr := channels.LoadConfig(channelsCfgPath)
	if cfgErr != nil {
		// PR #245 review (Low): distinguish "config absent" (a perfectly
		// valid disabled-channels deployment) from "config malformed"
		// (an operator bug we want to be loud about). The previous
		// implementation logged both at Warn with the same message, so
		// an operator who fat-fingered channels.yaml saw the same line
		// as one who simply hadn't created the file.
		if errors.Is(cfgErr, fs.ErrNotExist) {
			logger.Info("channels: channels.yaml not present; channel endpoints will return 503",
				zap.String("path", channelsCfgPath))
		} else {
			logger.Warn("channels: config load failed; channel endpoints will return 503",
				zap.String("path", channelsCfgPath),
				zap.Error(cfgErr))
		}
		return nil, noop, nil
	}

	maxCh := chanCfg.MaxChannels
	if maxCh <= 0 {
		// PR #245 review (Nice): use the package const rather than the
		// magic 50 — keeps the default in one place if it ever moves.
		maxCh = channels.DefaultMaxChannels
	}
	// ISSUE-0012 (PR #245 review Low; PR #246 finding L1): ensure the parent
	// directory exists before handing the path to SQLite. On a fresh checkout,
	// data/ is gitignored and may not exist; without this, sqlite.Open returns
	// "unable to open database file" and all seven channel REST endpoints
	// silently degrade to 503. Skip MkdirAll for ":memory:" (in-process store
	// used in tests) to avoid creating a literal directory named ".".
	if dbPath != ":memory:" {
		if mkErr := os.MkdirAll(filepath.Dir(dbPath), 0o755); mkErr != nil {
			logger.Warn("channels: cannot create db directory; channel endpoints will return 503",
				zap.String("path", dbPath), zap.Error(mkErr))
			return nil, noop, nil
		}
	}
	var sessionMetrics *channels.SessionMetrics
	if orchMetrics != nil {
		sessionMetrics = &channels.SessionMetrics{Writes: orchMetrics.SessionsWrites}
	}
	chanStore, sErr := channels.NewSQLiteStore(dbPath, channels.SQLiteOptions{
		MaxChannels:    maxCh,
		Logger:         logger,
		SessionMetrics: sessionMetrics,
	})
	if sErr != nil {
		logger.Warn("channels: store open failed; channel endpoints will return 503",
			zap.String("path", dbPath), zap.Error(sErr))
		return nil, noop, nil
	}

	cleanup = func() {
		if cErr := chanStore.Close(); cErr != nil {
			logger.Warn("channels: store close failed", zap.Error(cErr))
		}
	}

	var routerMetrics *channels.RouterMetrics
	if orchMetrics != nil {
		routerMetrics = &channels.RouterMetrics{
			MessagesDelivered:     orchMetrics.ChannelMessagesDelivered,
			MessagesPublished:     orchMetrics.ChannelMessagesPublished,
			MessagesCascadeCapped: orchMetrics.ChannelMessagesCascadeCapped,
		}
	}
	// ISSUE-0082 PR 2: build the per-request session resolver over the
	// channels store so the dispatcher can emit `persatrix-session` per
	// request. Construction only fails on a programming error (a non-SQLite
	// store); degrade to no session emission rather than failing channels
	// startup — delivery must not depend on the session rail being wired.
	sessionResolver, srErr := channels.NewSessionResolver(chanStore)
	if srErr != nil {
		logger.Warn("channels: session resolver unavailable; per-request persatrix-session emission disabled (personas fall back to legacy snapshot)",
			zap.Error(srErr))
		sessionResolver = nil
	}
	dispatcher := selectChannelDispatcher(reg, sessionResolver, epochID, logger)
	router := channels.NewChannelRouter(chanStore, dispatcher, logger, routerMetrics)
	// `channels.yaml` may override the default cascade-depth cap. Apply
	// after construction so the router's [defaults.DefaultMaxCascadeDepth]
	// default stays the canonical "no config" value; a zero or negative
	// row in the YAML is ignored by SetMaxCascadeDepth (the backstop
	// cannot be silently disabled — see the [RFC 0011 amendment]).
	router.SetMaxCascadeDepth(chanCfg.MaxCascadeDepth)
	// RFC 0031 Phase 1: stamp the per-process session id on router-
	// internal writes (today only ReconcileConfig-created channels).
	// Empty falls through to the store's `legacy` default.
	router.SetDefaultSessionID(sessionID)
	if rErr := router.ReconcileConfig(context.Background(), chanCfg); rErr != nil {
		// Loud-fail per RFC 0011 §B; the caller (main) will `Fatal`.
		// Run the cleanup ourselves first so the half-opened store does
		// not leak its DB handle.
		cleanup()
		return nil, noop, rErr
	}
	// RFC 0030 Layer 2.5 (floor control / speaker serialization) — PR 3
	// behaviour flip. Resolve the per-channel flag for every group channel
	// known at startup: config-declared channels use their resolved value (an
	// explicit `floor_control: false` opts back out), and any group channel
	// only present in the store — a runtime-created channel persisted by a
	// prior process — defaults ON so it does not silently revert to the
	// pre-amendment concurrent "shout" after a restart. Runs after
	// ReconcileConfig so the config channels exist in the store. Non-fatal:
	// a store-enumeration failure leaves the already-resolved config channels
	// (the shipped `planning` demo) in place; channels startup must not hinge
	// on the floor-resolution scan.
	if fErr := router.ResolveFloorControl(context.Background(), chanCfg); fErr != nil {
		logger.Warn("channels: floor-control resolution incomplete; config channels resolved, store-resident channels may default off until next create/restart",
			zap.Error(fErr))
	}

	logger.Info("channels: subsystem ready",
		zap.String("db", dbPath),
		zap.Int("declared_channels", len(chanCfg.Channels)),
		zap.Int("max_channels", maxCh),
	)
	// PR #245 re-review (Must-Fix #1): v0.3.0 ships the channels REST
	// surface unauthenticated — `sender_id` is body-trusted, and any
	// HTTP-reachable client can publish as any registered participant
	// or add themselves to any channel. Token-based auth lands in
	// RFC 0009 Phase 4. Until then operators MUST front the
	// orchestrator with an authenticating reverse proxy, bind the
	// listener to 127.0.0.1, or firewall the port. This Warn fires
	// once at startup so the trust boundary is impossible to miss in
	// the operator's first log scrape — it is intentionally not
	// suppressible from config (an opt-out would defeat the warning's
	// purpose). Removed when auth lands.
	logger.Warn("channels: REST surface is UNAUTHENTICATED in v0.3.0 — sender_id is body-trusted; firewall the port or front with an authenticating reverse proxy. Auth lands in RFC 0009 Phase 4.",
		zap.String("rfc", "0011"),
		zap.String("auth_eta", "RFC 0009 Phase 4"),
	)
	// The chat-as-DM façade (RFC 0011 PR 4a-ii-β-2) parks
	// `replyWaiter` entries in an **in-process** correlation table.
	// If the orchestrator is ever horizontally scaled and the
	// agent's REST publish lands on a different replica than the
	// one that called `PublishAndAwait`, the waiter on the origin
	// replica never fires and the chat times out. v0.3.0 ships
	// single-replica so this is not a release blocker, but a
	// startup-time WARN gives operators a fighting chance to spot
	// the limitation BEFORE topology changes start dropping chats
	// silently — far cheaper than discovering it from a flood of
	// 504s after a deployment.
	logger.Warn("chat: in-process reply waiter is single-replica only — horizontal scale will time out chats until a cross-process correlation primitive lands",
		zap.String("rfc", "0011"),
		zap.String("scope", "PublishAndAwait"),
	)
	return []server.ServerOption{
		server.WithChannels(chanStore, router),
		server.WithChannelSessionID(sessionID),
	}, cleanup, nil
}

// selectChannelDispatcher picks the per-recipient [channels.MessageDispatcher]
// based on whether the orchestrator has a live agent registry.
//
// Extracted from [initChannels] (PR #250 review Should-Fix #2) so the
// branch is independently testable without standing up a full router +
// store + membership scenario just to verify the type swap.
//
//   - reg == nil → [channels.NoopDispatcher]: channels-disabled
//     deployments and the existing tests that exercise the router-only
//     paths (member lookups + persistence) without spinning up agents.
//     ISSUE-0031: emit a startup Info line so operators can distinguish
//     "registry intentionally absent" from "init-order regression
//     dropped the registry on the floor" in the first log scrape.
//   - reg != nil → [*channels.GRPCMessageDispatcher]: production wiring
//     (PR 4a-ii-β-1) that turns each per-recipient `Dispatch` into an
//     `AgentService.ReceiveChannelMessage` gRPC call against the
//     address the recipient registered under.
//   - sessionResolver != nil → emitted as the `persatrix-session` gRPC
//     header per dispatch (ISSUE-0082 PR 2). Nil disables session emission
//     (the dispatcher ships no header; personas fall back to the legacy
//     construction snapshot).
//   - epochID != "" → emitted as the `persatrix-epoch` gRPC header on every
//     dispatch (ISSUE-0085 PR 4). The orchestrator resolves it once at boot
//     (`live` in production, a per-job id in CI); empty disables epoch
//     emission (personas fall back to their construction-time "live"
//     snapshot, byte-identical to the pre-epoch dispatch).
func selectChannelDispatcher(reg registry.Registry, sessionResolver channels.SessionBinder, epochID string, logger *zap.Logger) channels.MessageDispatcher {
	if reg == nil {
		logger.Info("channels: registry not available; cross-process dispatch disabled (NoopDispatcher in use)")
		return channels.NoopDispatcher{}
	}
	var opts []channels.DispatcherOption
	if sessionResolver != nil {
		opts = append(opts, channels.WithSessionResolver(sessionResolver))
	}
	if epochID != "" {
		opts = append(opts, channels.WithEpoch(epochID))
	}
	return channels.NewGRPCMessageDispatcher(reg, logger, opts...)
}
