package main

import (
	"context"
	"errors"
	"flag"
	"io/fs"
	"path/filepath"

	"go.uber.org/zap"

	"github.com/mkhomutov/persatrix/internal/channels"
	obsmetrics "github.com/mkhomutov/persatrix/internal/observability/metrics"
	"github.com/mkhomutov/persatrix/internal/server"
)

// channelsDB is the SQLite path backing the RFC 0011 channels subsystem.
// Defined here (next to [initChannels]) rather than in main.go's flag
// block to keep all channels-specific wiring co-located.
var channelsDB = flag.String("channels-db", "data/channels.db", "SQLite path for RFC 0011 channels")

// initChannels brings the RFC 0011 channels subsystem online.
//
// Loads `<configDir>/channels.yaml`, opens the SQLite-backed store at
// `dbPath`, builds the router with a [channels.NoopDispatcher] (the
// gRPC-backed dispatcher lands in PR 4), and reconciles config-vs-store
// membership at startup. A loud reconcile failure (per RFC 0011 §B
// coexistence rules) is reported via the returned error so the caller
// can `Fatal` — operators must reconcile by editing channels.yaml or
// `DELETE /api/v1/channels/{id}` (deferred to PR 4).
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
	cfgDir, dbPath string,
	orchMetrics *obsmetrics.Instruments,
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
	chanStore, sErr := channels.NewSQLiteStore(dbPath, channels.SQLiteOptions{
		MaxChannels: maxCh,
		Logger:      logger,
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
			MessagesDelivered: orchMetrics.ChannelMessagesDelivered,
		}
	}
	router := channels.NewChannelRouter(chanStore, channels.NoopDispatcher{}, logger, routerMetrics)
	if rErr := router.ReconcileConfig(context.Background(), chanCfg); rErr != nil {
		// Loud-fail per RFC 0011 §B; the caller (main) will `Fatal`.
		// Run the cleanup ourselves first so the half-opened store does
		// not leak its DB handle.
		cleanup()
		return nil, noop, rErr
	}

	logger.Info("channels: subsystem ready",
		zap.String("db", dbPath),
		zap.Int("declared_channels", len(chanCfg.Channels)),
		zap.Int("max_channels", maxCh),
	)
	return []server.ServerOption{server.WithChannels(chanStore, router)}, cleanup, nil
}
