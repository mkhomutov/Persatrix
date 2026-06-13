package server

// channel_mention_lift_test.go — unit coverage for the publish-seam wiring
// ([Server.liftContentMentions]), complementing the HTTP-level acceptance in
// channel_display_name_mention_lift_test.go. These tests pin the *cost* contract
// the acceptance suite cannot see: the lift must not touch the channel store or
// the agent registry for content that carries no `@` at all — the dominant case
// on a chat publish path — so the enrichment never taxes ordinary prose.

import (
	"context"
	"testing"

	"github.com/stretchr/testify/assert"
	"go.uber.org/zap"

	"github.com/mkhomutov/persatrix/internal/channels"
	"github.com/mkhomutov/persatrix/internal/registry"
)

// countingMemberStore embeds the full ChannelStore interface (nil — any method
// the lift does not use panics if called, which is the point) and counts only
// GetMembers, the one lookup liftContentMentions makes against the store.
type countingMemberStore struct {
	channels.ChannelStore
	members         []channels.Member
	getMembersCalls int
}

func (s *countingMemberStore) GetMembers(_ context.Context, _ string) ([]channels.Member, error) {
	s.getMembersCalls++
	return s.members, nil
}

// countingRegistry embeds the full Registry interface and counts only List,
// the one call liftContentMentions makes against the registry.
type countingRegistry struct {
	registry.Registry
	agents    []registry.AgentInfo
	listCalls int
}

func (r *countingRegistry) List(_ context.Context) ([]registry.AgentInfo, error) {
	r.listCalls++
	return r.agents, nil
}

// liftUnitServer builds the minimal Server the lift reads: the two spies plus a
// nop logger. No HTTP, no router — just the dependency seam under test.
func liftUnitServer(store *countingMemberStore, reg *countingRegistry) *Server {
	return &Server{
		channelStore: store,
		registry:     reg,
		logger:       zap.NewNop(),
	}
}

// TestLiftContentMentions_NoAtSignSkipsLookups pins the cost contract: content
// with no `@` cannot name anyone, so the lift must short-circuit BEFORE the
// store query and the full registry scan and hand the producer's array straight
// back. Most publishes are exactly this shape, so paying a DB round-trip plus a
// whole-directory snapshot on each one is pure waste.
func TestLiftContentMentions_NoAtSignSkipsLookups(t *testing.T) {
	store := &countingMemberStore{members: []channels.Member{{ParticipantID: "iron-fox"}}}
	reg := &countingRegistry{agents: []registry.AgentInfo{{ID: "iron-fox", Name: "Iron Fox"}}}
	s := liftUnitServer(store, reg)

	got := s.liftContentMentions(context.Background(), "group:planning", "nova-sparrow",
		"no addressing here, just plain prose", []string{"alex"})

	assert.Equal(t, []string{"alex"}, got, "an @-less publish returns the producer's array untouched")
	assert.Zero(t, store.getMembersCalls, "no `@` in content: the store is never queried")
	assert.Zero(t, reg.listCalls, "no `@` in content: the registry is never scanned")
}

// TestLiftContentMentions_AtSignTriggersLift is the guard against an
// over-aggressive short-circuit: an `@`-bearing publish must still run the
// lookups and lift the in-text mention into the structured union.
func TestLiftContentMentions_AtSignTriggersLift(t *testing.T) {
	store := &countingMemberStore{members: []channels.Member{
		{ParticipantID: "nova-sparrow"},
		{ParticipantID: "iron-fox"},
	}}
	reg := &countingRegistry{agents: []registry.AgentInfo{
		{ID: "nova-sparrow", Name: "Nova Sparrow"},
		{ID: "iron-fox", Name: "Iron Fox"},
	}}
	s := liftUnitServer(store, reg)

	got := s.liftContentMentions(context.Background(), "group:planning", "nova-sparrow",
		"@Iron Fox take this one", []string{"alex"})

	assert.Equal(t, []string{"alex", "iron-fox"}, got,
		"an @-mention is lifted and unioned after the structured prefix")
	assert.Equal(t, 1, store.getMembersCalls, "an `@` in content: the store is queried once")
	assert.Equal(t, 1, reg.listCalls, "an `@` in content: the registry is scanned once")
}
