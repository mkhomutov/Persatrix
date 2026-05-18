package main

import (
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.uber.org/zap"

	"github.com/mkhomutov/persatrix/internal/observability/logbuffer"
)

// TestNewAgentGRPCServer confirms the extracted agent-facing gRPC server
// builder returns a usable server with both agent-facing services
// registered. The recovery + rate-limit interceptor behaviour itself is
// covered in internal/security; this test pins the wiring extracted from
// main() (ISSUE-0059 / ISSUE-0008).
func TestNewAgentGRPCServer(t *testing.T) {
	buf, err := logbuffer.New(logbuffer.Config{Dir: t.TempDir()}, zap.NewNop())
	require.NoError(t, err)
	t.Cleanup(func() { _ = buf.Close() })

	// nil rate limiter + breaker: GRPCRateLimitInterceptor is nil-safe,
	// so the interceptor chain still composes.
	srv := newAgentGRPCServer(buf, nil, nil, zap.NewNop())
	require.NotNil(t, srv)
	t.Cleanup(srv.Stop)

	// LogService + WalletService are both registered on the listener.
	assert.Len(t, srv.GetServiceInfo(), 2,
		"both LogService and WalletService must be registered on the agent-facing server")
}
