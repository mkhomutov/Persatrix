package channels

// grpc_dispatcher_empty_fleet_test.go — ISSUE-0125's observability half. An
// orchestrator that holds ZERO registered agents while channels still have
// members has lost its whole fleet, and until now the only signal was one
// `dispatch target not registered` WARN per dropped message — logged at warn
// precisely because a single unregistered member is normally benign, so the
// catastrophic case was indistinguishable from a typo in channels.yaml.

import (
	"context"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.uber.org/zap"
	"go.uber.org/zap/zapcore"
	"go.uber.org/zap/zaptest/observer"

	"github.com/mkhomutov/persatrix/internal/registry"
)

// listingResolver is a stubResolver that also satisfies [fleetLister] — the
// shape the production *registry.InMemoryRegistry has.
type listingResolver struct {
	stubResolver
	listErr error
}

func (l *listingResolver) List(_ context.Context) ([]registry.AgentInfo, error) {
	if l.listErr != nil {
		return nil, l.listErr
	}
	out := make([]registry.AgentInfo, 0, len(l.agents))
	for _, a := range l.agents {
		out = append(out, *a)
	}
	return out, nil
}

func dispatchToGhost(t *testing.T, d *GRPCMessageDispatcher) {
	t.Helper()
	err := d.Dispatch(context.Background(), DispatchEnvelope{
		Recipient: Member{ParticipantID: "ghost", RespondPolicy: RespondAlways},
	}, ChannelMessage{ID: "m-1", ChannelID: "group:planning", SenderID: "alice"})
	require.Error(t, err)
}

func TestDispatch_EmptyRegistryLogsFleetLossOnce(t *testing.T) {
	core, recorded := observer.New(zapcore.ErrorLevel)
	resolver := &listingResolver{stubResolver: stubResolver{agents: map[string]*registry.AgentInfo{}}}
	d := NewGRPCMessageDispatcher(resolver, zap.New(core))

	dispatchToGhost(t, d)
	dispatchToGhost(t, d)
	dispatchToGhost(t, d)

	entries := recorded.FilterMessageSnippet("zero registered agents").All()
	require.Len(t, entries, 1,
		"once per outage — a per-dropped-message ERROR is the log spam the WARN was demoted to avoid")
	assert.Equal(t, zapcore.ErrorLevel, entries[0].Level)
}

// A populated registry that simply does not hold THIS participant is the
// benign case the dispatch WARN was written for (one mistyped membership), so
// it must stay at warn and emit no fleet-loss ERROR.
func TestDispatch_PopulatedRegistryLogsNoFleetLoss(t *testing.T) {
	core, recorded := observer.New(zapcore.ErrorLevel)
	resolver := &listingResolver{stubResolver: stubResolver{agents: map[string]*registry.AgentInfo{
		"agent-b": {ID: "agent-b", Address: "10.0.0.7:50051", Status: registry.StatusHealthy},
	}}}
	d := NewGRPCMessageDispatcher(resolver, zap.New(core))

	dispatchToGhost(t, d)

	assert.Empty(t, recorded.FilterMessageSnippet("zero registered agents").All(),
		"a missing member is not a missing fleet")
}

// The signal re-arms: an outage that recovers and recurs must be reported
// again, or the ERROR is useful exactly once in the lifetime of the process
// and silent for every later restart.
func TestDispatch_FleetLossSignalReArmsAfterRecovery(t *testing.T) {
	core, recorded := observer.New(zapcore.ErrorLevel)
	resolver := &listingResolver{stubResolver: stubResolver{agents: map[string]*registry.AgentInfo{}}}
	d := NewGRPCMessageDispatcher(resolver, zap.New(core))

	dispatchToGhost(t, d)

	// The fleet re-registers…
	resolver.agents["agent-b"] = &registry.AgentInfo{ID: "agent-b", Status: registry.StatusHealthy}
	dispatchToGhost(t, d)

	// …and is lost again.
	delete(resolver.agents, "agent-b")
	dispatchToGhost(t, d)

	assert.Len(t, recorded.FilterMessageSnippet("zero registered agents").All(), 2,
		"a second outage is a second signal")
}

// A resolver that cannot answer List (or does not implement it at all) must
// not turn a delivery miss into a fleet-loss claim.
func TestDispatch_ListFailureSuppressesFleetLossClaim(t *testing.T) {
	core, recorded := observer.New(zapcore.ErrorLevel)
	resolver := &listingResolver{
		stubResolver: stubResolver{agents: map[string]*registry.AgentInfo{}},
		listErr:      assert.AnError,
	}
	d := NewGRPCMessageDispatcher(resolver, zap.New(core))

	dispatchToGhost(t, d)

	assert.Empty(t, recorded.FilterMessageSnippet("zero registered agents").All())
}

func TestDispatch_ResolverWithoutListEmitsNoFleetLoss(t *testing.T) {
	core, recorded := observer.New(zapcore.ErrorLevel)
	// Bare stubResolver — Get only, no List.
	d := NewGRPCMessageDispatcher(&stubResolver{agents: map[string]*registry.AgentInfo{}}, zap.New(core))

	dispatchToGhost(t, d)

	assert.Empty(t, recorded.All(),
		"the fleet-loss probe is opt-in on the resolver's capability, not a hard dependency")
}
