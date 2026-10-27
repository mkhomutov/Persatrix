package main

// The society store at startup (RFC 0029 Phase 3 plan PR 4). The store keeps
// the state a society shares rather than one persona holds — the
// organization graph, the pending approvals and the key that signs them — in
// the channels store's file, on that store's one connection (plan S-2). So it
// comes up with the channels store or not at all: [initChannels] calls
// [initSociety] once the channels store has opened, and on each path where it
// has not. A reconcile failure is the one exception, since it stops startup.

import (
	"fmt"

	"go.uber.org/zap"

	"github.com/mkhomutov/persatrix/internal/channels"
	"github.com/mkhomutov/persatrix/internal/server"
	"github.com/mkhomutov/persatrix/internal/society"
)

// initSociety builds the society store on chanStore's connection and returns
// the option that hands it to the server. A nil chanStore means channels are
// off, so there is no society store. Either way it logs one line saying
// whether society features are on; neither failure stops startup, as a
// channels store that cannot open does not.
func initSociety(chanStore channels.ChannelStore, logger *zap.Logger) []server.ServerOption {
	if chanStore == nil {
		logger.Info("society: channels are off, so there is no society store; society features are off")
		return nil
	}
	st, err := openSociety(chanStore)
	if err != nil {
		logger.Warn("society: store unavailable; society features are off", zap.Error(err))
		return nil
	}
	logger.Info("society: store ready on the channels store's connection")
	return []server.ServerOption{server.WithSociety(st)}
}

// openSociety builds the society store on chanStore's own connection. Only
// the SQLite channels store hands one over.
func openSociety(chanStore channels.ChannelStore) (society.Store, error) {
	src, ok := chanStore.(society.ConnSource)
	if !ok {
		return nil, fmt.Errorf("society: the channels store (%T) hands over no connection", chanStore)
	}
	return society.NewSQLiteStore(src)
}
