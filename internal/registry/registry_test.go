package registry

import (
	"context"
	"fmt"
	"sync"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.uber.org/zap"
)

// Compile-time check: InMemoryRegistry implements Registry.
var _ Registry = (*InMemoryRegistry)(nil)

func newTestRegistry() *InMemoryRegistry {
	return NewInMemoryRegistry(zap.NewNop())
}

func sampleAgent(id string) AgentInfo {
	return AgentInfo{
		ID:           id,
		Name:         "Test Agent " + id,
		Role:         "coder",
		Capabilities: []string{"code", "test"},
		Address:      "localhost:9090",
		NodeID:       "",
		Status:       StatusHealthy,
	}
}

func TestRegisterAndGet(t *testing.T) {
	r := newTestRegistry()
	ctx := context.Background()

	agent := sampleAgent("agent-01")
	err := r.Register(ctx, agent)
	require.NoError(t, err)

	got, err := r.Get(ctx, "agent-01")
	require.NoError(t, err)
	assert.Equal(t, "agent-01", got.ID)
	assert.Equal(t, "Test Agent agent-01", got.Name)
	assert.Equal(t, "coder", got.Role)
	assert.Equal(t, []string{"code", "test"}, got.Capabilities)
	assert.Equal(t, "localhost:9090", got.Address)
	assert.Equal(t, StatusHealthy, got.Status)
}

func TestRegisterDuplicate(t *testing.T) {
	r := newTestRegistry()
	ctx := context.Background()

	agent := sampleAgent("agent-01")
	err := r.Register(ctx, agent)
	require.NoError(t, err)

	err = r.Register(ctx, agent)
	assert.ErrorIs(t, err, ErrAgentAlreadyRegistered)
}

func TestGetNotFound(t *testing.T) {
	r := newTestRegistry()
	ctx := context.Background()

	_, err := r.Get(ctx, "nonexistent")
	assert.ErrorIs(t, err, ErrAgentNotFound)
}

func TestUnregister(t *testing.T) {
	r := newTestRegistry()
	ctx := context.Background()

	agent := sampleAgent("agent-01")
	require.NoError(t, r.Register(ctx, agent))

	err := r.Unregister(ctx, "agent-01")
	require.NoError(t, err)

	_, err = r.Get(ctx, "agent-01")
	assert.ErrorIs(t, err, ErrAgentNotFound)
}

func TestUnregisterNotFound(t *testing.T) {
	r := newTestRegistry()
	ctx := context.Background()

	err := r.Unregister(ctx, "nonexistent")
	assert.ErrorIs(t, err, ErrAgentNotFound)
}

func TestUpdateStatus(t *testing.T) {
	r := newTestRegistry()
	ctx := context.Background()

	agent := sampleAgent("agent-01")
	require.NoError(t, r.Register(ctx, agent))

	err := r.UpdateStatus(ctx, "agent-01", StatusDegraded)
	require.NoError(t, err)

	got, err := r.Get(ctx, "agent-01")
	require.NoError(t, err)
	assert.Equal(t, StatusDegraded, got.Status)
}

func TestUpdateStatusNotFound(t *testing.T) {
	r := newTestRegistry()
	ctx := context.Background()

	err := r.UpdateStatus(ctx, "nonexistent", StatusHealthy)
	assert.ErrorIs(t, err, ErrAgentNotFound)
}

func TestList(t *testing.T) {
	r := newTestRegistry()
	ctx := context.Background()

	require.NoError(t, r.Register(ctx, sampleAgent("agent-01")))
	require.NoError(t, r.Register(ctx, sampleAgent("agent-02")))

	list, err := r.List(ctx)
	require.NoError(t, err)
	assert.Len(t, list, 2)

	ids := make(map[string]bool)
	for _, a := range list {
		ids[a.ID] = true
	}
	assert.True(t, ids["agent-01"])
	assert.True(t, ids["agent-02"])
}

func TestListEmpty(t *testing.T) {
	r := newTestRegistry()
	ctx := context.Background()

	list, err := r.List(ctx)
	require.NoError(t, err)
	assert.Empty(t, list)
}

func TestFindByCapability(t *testing.T) {
	r := newTestRegistry()
	ctx := context.Background()

	a1 := AgentInfo{ID: "agent-01", Capabilities: []string{"code", "test"}}
	a2 := AgentInfo{ID: "agent-02", Capabilities: []string{"review", "test"}}
	a3 := AgentInfo{ID: "agent-03", Capabilities: []string{"review"}}

	require.NoError(t, r.Register(ctx, a1))
	require.NoError(t, r.Register(ctx, a2))
	require.NoError(t, r.Register(ctx, a3))

	// Both agent-01 and agent-02 have "test".
	results, err := r.FindByCapability(ctx, "test")
	require.NoError(t, err)
	assert.Len(t, results, 2)

	ids := make(map[string]bool)
	for _, a := range results {
		ids[a.ID] = true
	}
	assert.True(t, ids["agent-01"])
	assert.True(t, ids["agent-02"])
}

func TestFindByCapabilityNoMatch(t *testing.T) {
	r := newTestRegistry()
	ctx := context.Background()

	require.NoError(t, r.Register(ctx, sampleAgent("agent-01")))

	results, err := r.FindByCapability(ctx, "nonexistent-capability")
	require.NoError(t, err)
	assert.Empty(t, results)
}

func TestFindByCapabilityMultipleMatches(t *testing.T) {
	r := newTestRegistry()
	ctx := context.Background()

	for i := 0; i < 5; i++ {
		a := AgentInfo{
			ID:           fmt.Sprintf("agent-%02d", i),
			Capabilities: []string{"shared-cap"},
		}
		require.NoError(t, r.Register(ctx, a))
	}

	results, err := r.FindByCapability(ctx, "shared-cap")
	require.NoError(t, err)
	assert.Len(t, results, 5)
}

func TestReRegistrationFlow(t *testing.T) {
	r := newTestRegistry()
	ctx := context.Background()

	agent := sampleAgent("agent-01")
	require.NoError(t, r.Register(ctx, agent))

	// Unregister, then re-register with new address.
	require.NoError(t, r.Unregister(ctx, "agent-01"))
	agent.Address = "localhost:9091"
	require.NoError(t, r.Register(ctx, agent))

	got, err := r.Get(ctx, "agent-01")
	require.NoError(t, err)
	assert.Equal(t, "localhost:9091", got.Address)
}

func TestDeepCopyGetCapabilities(t *testing.T) {
	r := newTestRegistry()
	ctx := context.Background()

	agent := AgentInfo{
		ID:           "agent-01",
		Capabilities: []string{"code", "test"},
	}
	require.NoError(t, r.Register(ctx, agent))

	// Mutate the returned copy's Capabilities.
	got, err := r.Get(ctx, "agent-01")
	require.NoError(t, err)
	got.Capabilities[0] = "MUTATED"

	// Internal state must be unaffected.
	got2, err := r.Get(ctx, "agent-01")
	require.NoError(t, err)
	assert.Equal(t, "code", got2.Capabilities[0])
}

func TestDeepCopyRegisterCapabilities(t *testing.T) {
	r := newTestRegistry()
	ctx := context.Background()

	caps := []string{"code", "test"}
	agent := AgentInfo{ID: "agent-01", Capabilities: caps}
	require.NoError(t, r.Register(ctx, agent))

	// Mutate the original slice after registering.
	caps[0] = "MUTATED"

	got, err := r.Get(ctx, "agent-01")
	require.NoError(t, err)
	assert.Equal(t, "code", got.Capabilities[0])
}

func TestDeepCopyListCapabilities(t *testing.T) {
	r := newTestRegistry()
	ctx := context.Background()

	agent := AgentInfo{
		ID:           "agent-01",
		Capabilities: []string{"code", "test"},
	}
	require.NoError(t, r.Register(ctx, agent))

	list, err := r.List(ctx)
	require.NoError(t, err)
	require.Len(t, list, 1)
	list[0].Capabilities[0] = "MUTATED"

	// Internal state must be unaffected.
	got, err := r.Get(ctx, "agent-01")
	require.NoError(t, err)
	assert.Equal(t, "code", got.Capabilities[0])
}

func TestDeepCopyFindByCapability(t *testing.T) {
	r := newTestRegistry()
	ctx := context.Background()

	agent := AgentInfo{
		ID:           "agent-01",
		Capabilities: []string{"code", "test"},
	}
	require.NoError(t, r.Register(ctx, agent))

	results, err := r.FindByCapability(ctx, "code")
	require.NoError(t, err)
	require.Len(t, results, 1)
	results[0].Capabilities[0] = "MUTATED"

	// Internal state must be unaffected.
	got, err := r.Get(ctx, "agent-01")
	require.NoError(t, err)
	assert.Equal(t, "code", got.Capabilities[0])
}

func TestNilCapabilities(t *testing.T) {
	r := newTestRegistry()
	ctx := context.Background()

	agent := AgentInfo{ID: "agent-01", Capabilities: nil}
	require.NoError(t, r.Register(ctx, agent))

	got, err := r.Get(ctx, "agent-01")
	require.NoError(t, err)
	assert.Empty(t, got.Capabilities)
}

func TestNewInMemoryRegistryNilLogger(t *testing.T) {
	// Nil logger guard: should not panic.
	r := NewInMemoryRegistry(nil)
	ctx := context.Background()

	agent := sampleAgent("agent-01")
	require.NoError(t, r.Register(ctx, agent))

	got, err := r.Get(ctx, "agent-01")
	require.NoError(t, err)
	assert.Equal(t, "agent-01", got.ID)
}

func TestAgentStatusConstants(t *testing.T) {
	// Verify existing iota-based constants have expected values.
	assert.Equal(t, AgentStatus(0), StatusUnknown)
	assert.Equal(t, AgentStatus(1), StatusHealthy)
	assert.Equal(t, AgentStatus(2), StatusDegraded)
	assert.Equal(t, AgentStatus(3), StatusOffline)
}

func TestConcurrentRegisterGetList(t *testing.T) {
	r := newTestRegistry()
	ctx := context.Background()

	const numGoroutines = 30
	var wg sync.WaitGroup

	// Phase 1: Concurrent registrations.
	wg.Add(numGoroutines)
	for i := 0; i < numGoroutines; i++ {
		go func(idx int) {
			defer wg.Done()
			agent := AgentInfo{
				ID:           fmt.Sprintf("agent-%03d", idx),
				Name:         fmt.Sprintf("Agent %d", idx),
				Capabilities: []string{"cap-a", "cap-b"},
				Address:      fmt.Sprintf("localhost:%d", 9000+idx),
			}
			_ = r.Register(ctx, agent)
		}(i)
	}
	wg.Wait()

	// Phase 2: Concurrent reads (Get + List + FindByCapability).
	wg.Add(numGoroutines * 3)
	for i := 0; i < numGoroutines; i++ {
		go func(idx int) {
			defer wg.Done()
			_, _ = r.Get(ctx, fmt.Sprintf("agent-%03d", idx))
		}(i)
		go func() {
			defer wg.Done()
			_, _ = r.List(ctx)
		}()
		go func() {
			defer wg.Done()
			_, _ = r.FindByCapability(ctx, "cap-a")
		}()
	}
	wg.Wait()

	// Verify all agents were registered.
	list, err := r.List(ctx)
	require.NoError(t, err)
	assert.Len(t, list, numGoroutines)
}

func TestConcurrentRegisterAndUnregister(t *testing.T) {
	r := newTestRegistry()
	ctx := context.Background()

	const numGoroutines = 20

	// Pre-register agents.
	for i := 0; i < numGoroutines; i++ {
		agent := AgentInfo{
			ID:           fmt.Sprintf("agent-%03d", i),
			Capabilities: []string{"cap"},
		}
		require.NoError(t, r.Register(ctx, agent))
	}

	var wg sync.WaitGroup

	// Concurrently unregister even-indexed, update status of odd-indexed.
	wg.Add(numGoroutines)
	for i := 0; i < numGoroutines; i++ {
		go func(idx int) {
			defer wg.Done()
			id := fmt.Sprintf("agent-%03d", idx)
			if idx%2 == 0 {
				_ = r.Unregister(ctx, id)
			} else {
				_ = r.UpdateStatus(ctx, id, StatusDegraded)
			}
		}(i)
	}
	wg.Wait()

	// Verify: even-indexed are gone, odd-indexed are degraded.
	for i := 0; i < numGoroutines; i++ {
		id := fmt.Sprintf("agent-%03d", i)
		got, err := r.Get(ctx, id)
		if i%2 == 0 {
			assert.ErrorIs(t, err, ErrAgentNotFound, "agent %s should be unregistered", id)
		} else {
			require.NoError(t, err, "agent %s should exist", id)
			assert.Equal(t, StatusDegraded, got.Status)
		}
	}
}

func TestConcurrentUpdateStatus(t *testing.T) {
	r := newTestRegistry()
	ctx := context.Background()

	agent := sampleAgent("agent-01")
	require.NoError(t, r.Register(ctx, agent))

	const numGoroutines = 30
	var wg sync.WaitGroup
	wg.Add(numGoroutines)

	statuses := []AgentStatus{StatusHealthy, StatusDegraded, StatusOffline, StatusUnknown}
	for i := 0; i < numGoroutines; i++ {
		go func(idx int) {
			defer wg.Done()
			_ = r.UpdateStatus(ctx, "agent-01", statuses[idx%len(statuses)])
		}(i)
	}
	wg.Wait()

	// Agent should still be retrievable (no corruption).
	got, err := r.Get(ctx, "agent-01")
	require.NoError(t, err)
	assert.Contains(t, statuses, got.Status)
}

func TestFullLifecycle(t *testing.T) {
	r := newTestRegistry()
	ctx := context.Background()

	// Register.
	agent := sampleAgent("agent-01")
	require.NoError(t, r.Register(ctx, agent))

	// Get and verify.
	got, err := r.Get(ctx, "agent-01")
	require.NoError(t, err)
	assert.Equal(t, StatusHealthy, got.Status)

	// Update status.
	require.NoError(t, r.UpdateStatus(ctx, "agent-01", StatusDegraded))
	got, err = r.Get(ctx, "agent-01")
	require.NoError(t, err)
	assert.Equal(t, StatusDegraded, got.Status)

	// List.
	list, err := r.List(ctx)
	require.NoError(t, err)
	assert.Len(t, list, 1)

	// FindByCapability.
	results, err := r.FindByCapability(ctx, "code")
	require.NoError(t, err)
	assert.Len(t, results, 1)

	// Unregister.
	require.NoError(t, r.Unregister(ctx, "agent-01"))

	// Verify gone.
	_, err = r.Get(ctx, "agent-01")
	assert.ErrorIs(t, err, ErrAgentNotFound)

	list, err = r.List(ctx)
	require.NoError(t, err)
	assert.Empty(t, list)
}
