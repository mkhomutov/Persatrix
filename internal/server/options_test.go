package server

import (
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.uber.org/zap"

	"github.com/mkhomutov/persatrix/internal/planner"
	"github.com/mkhomutov/persatrix/internal/registry"
	"github.com/mkhomutov/persatrix/internal/society"
	"github.com/mkhomutov/persatrix/internal/state"
)

// fakeSociety is a society store no call reaches: the option only hands it
// over.
type fakeSociety struct{ society.Store }

// TestWithSociety_HandsTheStoreToTheServer pins the hand-over RFC 0029 Phase
// 3's startup wiring makes: the server keeps the society store it is given,
// and a server built without one keeps none, so a society feature can tell
// that it is off.
func TestWithSociety_HandsTheStoreToTheServer(t *testing.T) {
	logger := zap.NewNop()
	st := &fakeSociety{}
	srv, err := New("127.0.0.1:0", t.TempDir(), state.NewInMemoryStore(logger),
		registry.NewInMemoryRegistry(logger), planner.NewYAMLPlanner(logger), logger,
		WithSociety(st))
	require.NoError(t, err)
	assert.Same(t, st, srv.societyStore)

	bare, _ := testServer(t)
	assert.Nil(t, bare.societyStore)
}
