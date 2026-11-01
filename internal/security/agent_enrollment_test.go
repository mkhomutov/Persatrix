package security

// RFC 0009 Phases 3–4 plan PR 6: the enrollment registers an agent and
// issues its token as one step, and deregisters it and revokes its tokens
// as one step, for every server that registers agents.

import (
	"context"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.uber.org/zap"

	"github.com/mkhomutov/persatrix/internal/registry"
)

func newTestEnrollment(t *testing.T) (*AgentEnrollment, registry.Registry) {
	t.Helper()
	reg := registry.NewInMemoryRegistry(zap.NewNop())
	return NewAgentEnrollment(reg, NewAgentTokenIssuer()), reg
}

func agentAt(id, address string) registry.AgentInfo {
	return registry.AgentInfo{ID: id, Address: address, Status: registry.StatusHealthy}
}

// From PR 6's review: a registration whose token cannot be issued must not
// leave the agent registered without one.
func TestAgentEnrollment_UndoesARegistrationNoTokenCanBeIssuedFor(t *testing.T) {
	ctx := context.Background()
	e, reg := newTestEnrollment(t)

	_, err := e.Enroll(ctx, agentAt(ConsoleAgentID, "localhost:50051"), "192.0.2.1")
	require.ErrorIs(t, err, ErrAgentTokenAgentID)
	_, err = reg.Get(ctx, ConsoleAgentID)
	require.ErrorIs(t, err, registry.ErrAgentNotFound, "a first registration is removed again")

	// An agent registered some other way keeps its row as it was.
	require.NoError(t, reg.Register(ctx, agentAt(DefaultUnauthenticatedID, "old:1")))
	_, err = e.Enroll(ctx, agentAt(DefaultUnauthenticatedID, "new:2"), "192.0.2.1")
	require.ErrorIs(t, err, ErrAgentTokenAgentID)
	got, err := reg.Get(ctx, DefaultUnauthenticatedID)
	require.NoError(t, err)
	assert.Equal(t, "old:1", got.Address, "a re-registration is put back as it was")
}

func TestAgentEnrollment_RemembersWhereEachAgentRegisteredFrom(t *testing.T) {
	ctx := context.Background()
	e, _ := newTestEnrollment(t)

	first, err := e.Enroll(ctx, agentAt("code-writer", "localhost:50051"), "192.0.2.1")
	require.NoError(t, err)
	assert.Nil(t, first.Previous)
	assert.Empty(t, first.PreviousSource)

	second, err := e.Enroll(ctx, agentAt("code-writer", "localhost:50051"), "198.51.100.7")
	require.NoError(t, err)
	require.NotNil(t, second.Previous)
	assert.Equal(t, "192.0.2.1", second.PreviousSource)
	_, err = e.Issuer().Check(first.Token)
	require.ErrorIs(t, err, ErrAgentTokenRevoked, "registering again retires the earlier token")

	require.NoError(t, e.Deregister(ctx, "code-writer"))
	_, err = e.Issuer().Check(second.Token)
	require.ErrorIs(t, err, ErrAgentTokenRevoked, "deregistering revokes the agent's tokens")

	third, err := e.Enroll(ctx, agentAt("code-writer", "localhost:50051"), "203.0.113.9")
	require.NoError(t, err)
	assert.Nil(t, third.Previous)
	assert.Empty(t, third.PreviousSource, "a deregistered agent's earlier source is forgotten")
}

func TestAgentEnrollment_DeregisteringAnUnknownAgentRevokesNothing(t *testing.T) {
	e, _ := newTestEnrollment(t)
	require.ErrorIs(t, e.Deregister(context.Background(), "nobody"), registry.ErrAgentNotFound)
}
