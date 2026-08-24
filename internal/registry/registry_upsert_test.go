package registry

// registry_upsert_test.go — ISSUE-0125. Register used to reject a duplicate id
// with ErrAgentAlreadyRegistered ("re-registration requires calling Unregister
// first"), which the REST layer surfaced as 409 CONFLICT. That made a
// re-register a no-op against a POPULATED registry, so an agent that came back
// on a new address could never correct it — the precondition every shape of
// fleet re-registration inherits. Split out of registry_test.go to keep that
// file under the 500-line review cap, on the registry_list_test.go precedent.

import (
	"context"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// TestRegisterIsUpsert pins the ISSUE-0125 precondition: a second Register for
// the same id REPLACES the stored row instead of failing. Before this, Register
// returned ErrAgentAlreadyRegistered ("re-registration requires calling
// Unregister first"), which the REST layer surfaced as 409 — so an agent that
// moved to a new address could never correct it, and a re-register aimed at an
// orchestrator that had NOT lost its registry was a no-op.
func TestRegisterIsUpsert(t *testing.T) {
	r := newTestRegistry()
	ctx := context.Background()

	require.NoError(t, r.Register(ctx, sampleAgent("agent-01")))

	moved := sampleAgent("agent-01")
	moved.Address = "10.0.0.7:50051"
	moved.Name = "Renamed agent-01"
	moved.Capabilities = []string{"review"}
	require.NoError(t, r.Register(ctx, moved))

	got, err := r.Get(ctx, "agent-01")
	require.NoError(t, err)
	assert.Equal(t, "10.0.0.7:50051", got.Address, "a re-register corrects a stale address")
	assert.Equal(t, "Renamed agent-01", got.Name)
	assert.Equal(t, []string{"review"}, got.Capabilities)

	all, err := r.List(ctx)
	require.NoError(t, err)
	assert.Len(t, all, 1, "an upsert replaces the row, it does not add one")
}

// TestRegisterUpsertDeepCopiesCapabilities pins that the upsert path keeps the
// deep-copy contract the insert path has always had — the caller must not be
// able to mutate registry state through the slice it handed in.
func TestRegisterUpsertDeepCopiesCapabilities(t *testing.T) {
	r := newTestRegistry()
	ctx := context.Background()

	require.NoError(t, r.Register(ctx, sampleAgent("agent-01")))

	caps := []string{"code"}
	moved := sampleAgent("agent-01")
	moved.Capabilities = caps
	require.NoError(t, r.Register(ctx, moved))

	caps[0] = "mutated"

	got, err := r.Get(ctx, "agent-01")
	require.NoError(t, err)
	assert.Equal(t, []string{"code"}, got.Capabilities)
}

// TestRegisterAfterRestartRepopulatesEmptyRegistry is the restart case itself:
// the orchestrator comes back with an EMPTY map (InMemoryRegistry has no load
// path), and the agent's re-register makes the fleet dispatchable again — the
// row is present, healthy, and carries a dialable address, which is exactly
// what GRPCMessageDispatcher.Dispatch resolves before it dials.
//
// Pinned SEPARATELY from the upsert above on purpose: the upsert passes even
// when nothing ever re-registers, so it must not stand in as evidence that a
// restart self-heals.
func TestRegisterAfterRestartRepopulatesEmptyRegistry(t *testing.T) {
	ctx := context.Background()

	before := newTestRegistry()
	require.NoError(t, before.Register(ctx, sampleAgent("agent-01")))

	// The orchestrator process restarts: a brand-new registry, no load path.
	after := newTestRegistry()
	_, err := after.Get(ctx, "agent-01")
	require.ErrorIs(t, err, ErrAgentNotFound, "a restarted orchestrator starts empty")

	require.NoError(t, after.Register(ctx, sampleAgent("agent-01")))

	got, err := after.Get(ctx, "agent-01")
	require.NoError(t, err)
	assert.Equal(t, StatusHealthy, got.Status)
	assert.NotEmpty(t, got.Address)
}
