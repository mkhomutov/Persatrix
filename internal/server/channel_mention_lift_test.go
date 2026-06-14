package server

// channel_mention_lift_test.go — unit coverage for the publish-seam wiring
// ([Server.liftContentMentions]), complementing the HTTP-level acceptance in
// channel_display_name_mention_lift_test.go. These tests pin the *cost* contract
// the acceptance suite cannot see: the lift must not touch the channel store or
// the agent registry for content that carries no `@` at all — the dominant case
// on a chat publish path — so the enrichment never taxes ordinary prose.

import (
	"context"
	"errors"
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

// countingRegistry embeds the full Registry interface and spies on the lift's
// name lookup: it counts NamesFor (the membership-scoped read the lift makes —
// ISSUE-0100) and records the ids it was handed, so a test can assert the lift
// never over-reads the whole directory. List is also overridden and counted so a
// regression back to the whole-directory snapshot is caught (listCalls must stay
// 0). The embedded nil Registry makes any other method panic, which is the point.
type countingRegistry struct {
	registry.Registry
	agents        []registry.AgentInfo
	listCalls     int
	namesForCalls int
	namesForIDs   []string
	namesForErr   bool // simulate a registry miss: NamesFor returns an error
}

func (r *countingRegistry) List(_ context.Context) ([]registry.AgentInfo, error) {
	r.listCalls++
	return r.agents, nil
}

func (r *countingRegistry) NamesFor(_ context.Context, ids []string) (map[string]string, error) {
	r.namesForCalls++
	r.namesForIDs = ids
	if r.namesForErr {
		return nil, errors.New("simulated registry miss")
	}
	names := make(map[string]string, len(ids))
	want := make(map[string]struct{}, len(ids))
	for _, id := range ids {
		want[id] = struct{}{}
	}
	for _, a := range r.agents {
		if _, ok := want[a.ID]; ok {
			names[a.ID] = a.Name
		}
	}
	return names, nil
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
	assert.Zero(t, reg.namesForCalls, "no `@` in content: the registry is never read")
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
	assert.Equal(t, 1, reg.namesForCalls, "an `@` in content: the registry is read once, scoped to members")
	assert.Zero(t, reg.listCalls, "the lift must not snapshot the whole directory (ISSUE-0100)")
	assert.ElementsMatch(t, []string{"nova-sparrow", "iron-fox"}, reg.namesForIDs,
		"the name lookup is scoped to exactly the channel's members, not the whole directory")
}

// TestLiftContentMentions_NamesForErrorDegradesToIDOnly pins the fail-open
// contract on the name-lookup leg: a NamesFor error must never fail the publish.
// The lift still builds candidates from the membership rows with empty names, so
// an in-text *id* ("@iron-fox") lifts while a display *name* ("@Iron Fox") quietly
// falls back to today's no-lift behaviour — exactly the registry-miss degradation
// the old List-based code had, preserved across the ISSUE-0100 scoping change.
func TestLiftContentMentions_NamesForErrorDegradesToIDOnly(t *testing.T) {
	store := &countingMemberStore{members: []channels.Member{{ParticipantID: "iron-fox"}}}
	reg := &countingRegistry{
		agents:      []registry.AgentInfo{{ID: "iron-fox", Name: "Iron Fox"}},
		namesForErr: true,
	}
	s := liftUnitServer(store, reg)

	// In-text id still lifts despite the registry miss...
	gotID := s.liftContentMentions(context.Background(), "group:planning", "nova-sparrow",
		"@iron-fox take this one", nil)
	assert.Equal(t, []string{"iron-fox"}, gotID, "an in-text id lifts even when names are unavailable")

	// ...but the display name cannot resolve without the registry, so it no-lifts.
	gotName := s.liftContentMentions(context.Background(), "group:planning", "nova-sparrow",
		"@Iron Fox take this one", nil)
	assert.Empty(t, gotName, "a display-name mention degrades to no-lift on a registry miss")
}
