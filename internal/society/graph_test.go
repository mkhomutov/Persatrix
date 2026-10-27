package society

// The organization graph and its version (RFC 0029 Phase 3 plan S-4 and S-6):
// a graph is replaced whole and read back in one canonical order; the writer
// raises the version, and the store only checks that it went up.

import (
	"context"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// lab is a small hierarchy: one lead managing two members, one of them only
// in a scope.
func lab() Graph {
	return Graph{Version: 1, Organizations: []Organization{{
		ID: "lab", Name: "Research lab", Topology: "hierarchy",
		Roles: []Role{{ID: "lead", Authority: "directive"}, {ID: "member", Authority: "operational"}},
		Members: []Member{
			{AgentID: "ember-owl", RoleID: "lead"},
			{AgentID: "moss-fox", RoleID: "member"},
			{AgentID: "tide-wren", RoleID: "member"},
		},
		Manages: []Manages{
			{ManagerID: "ember-owl", ReportID: "moss-fox"},
			{ManagerID: "ember-owl", ReportID: "tide-wren", Scope: "budget"},
		},
	}}}
}

func TestGraph_AnEmptyStoreHasNoGraphAtVersionZero(t *testing.T) {
	s, _ := newTestStore(t)
	g, err := s.Graph(context.Background())
	require.NoError(t, err)
	assert.Equal(t, Graph{}, g)
}

func TestGraph_RefusesARowOfAnOrganizationItDoesNotHold(t *testing.T) {
	s, _ := newTestStore(t)
	ctx := context.Background()
	require.NoError(t, s.ReplaceGraph(ctx, lab()))
	// Only a connection with foreign keys off can leave such a row, and the
	// constructor refuses one, so the test turns them off on the store's own.
	_, err := s.db.Exec(`PRAGMA foreign_keys = OFF`)
	require.NoError(t, err)
	_, err = s.db.Exec(`INSERT INTO society_roles (organization_id, id, authority) VALUES ('gone', 'lead', 'directive')`)
	require.NoError(t, err)

	_, err = s.Graph(ctx)
	assert.ErrorContains(t, err, `"gone"`, "a row of no organization fails the read, never joins another")
}

func TestReplaceGraph_ReadsBackInCanonicalOrder(t *testing.T) {
	s, _ := newTestStore(t)
	ctx := context.Background()
	in := Graph{Version: 3, Organizations: []Organization{
		{ID: "studio", Name: "Studio", Topology: "flat",
			Roles:   []Role{{ID: "peer", Authority: "advisory"}},
			Members: []Member{{AgentID: "sage-lynx", RoleID: "peer"}, {AgentID: "fern-kite", RoleID: "peer"}}},
		lab().Organizations[0],
	}}
	// Shuffle what the canonical order sorts.
	in.Organizations[1].Members[0], in.Organizations[1].Members[2] =
		in.Organizations[1].Members[2], in.Organizations[1].Members[0]
	in.Organizations[1].Manages[0], in.Organizations[1].Manages[1] =
		in.Organizations[1].Manages[1], in.Organizations[1].Manages[0]
	require.NoError(t, s.ReplaceGraph(ctx, in))

	g, err := s.Graph(ctx)
	require.NoError(t, err)
	want := Graph{Version: 3, Organizations: []Organization{
		lab().Organizations[0],
		{ID: "studio", Name: "Studio", Topology: "flat",
			Roles:   []Role{{ID: "peer", Authority: "advisory"}},
			Members: []Member{{AgentID: "fern-kite", RoleID: "peer"}, {AgentID: "sage-lynx", RoleID: "peer"}}},
	}}
	assert.Equal(t, want, g, "organizations, roles, members and edges come back sorted by their keys")
}

func TestReplaceGraph_ReplacesTheWholeGraph(t *testing.T) {
	s, _ := newTestStore(t)
	ctx := context.Background()
	require.NoError(t, s.ReplaceGraph(ctx, lab()))

	next := lab()
	next.Version = 2
	next.Organizations[0].Members = next.Organizations[0].Members[:2]
	next.Organizations[0].Members[1].RoleID = "lead"
	next.Organizations[0].Manages = nil
	require.NoError(t, s.ReplaceGraph(ctx, next))

	g, err := s.Graph(ctx)
	require.NoError(t, err)
	assert.Equal(t, next, g, "a removed member, a changed role and dropped edges must all show")

	empty := Graph{Version: 3}
	require.NoError(t, s.ReplaceGraph(ctx, empty))
	g, err = s.Graph(ctx)
	require.NoError(t, err)
	assert.Equal(t, empty, g, "an empty graph clears the organizations and keeps the version")
}

func TestReplaceGraph_TheVersionMustGoUp(t *testing.T) {
	s, _ := newTestStore(t)
	ctx := context.Background()
	require.NoError(t, s.ReplaceGraph(ctx, lab()))

	for _, version := range []int64{1, 0, -1} {
		g := lab()
		g.Version = version
		g.Organizations[0].Manages = nil
		err := s.ReplaceGraph(ctx, g)
		require.ErrorIs(t, err, ErrGraphVersion, "version %d", version)
	}
	g, err := s.Graph(ctx)
	require.NoError(t, err)
	assert.Equal(t, lab(), g, "a refused write must leave the graph as it was")
}

func TestReplaceGraph_AFailureHalfWayLeavesTheOldGraph(t *testing.T) {
	s, _ := newTestStore(t)
	ctx := context.Background()
	require.NoError(t, s.ReplaceGraph(ctx, lab()))

	for name, broken := range map[string]func(*Organization){
		"member with an unknown role": func(o *Organization) {
			o.Members = append(o.Members, Member{AgentID: "sage-lynx", RoleID: "chair"})
		},
		"edge to a non-member": func(o *Organization) {
			o.Manages = append(o.Manages, Manages{ManagerID: "ember-owl", ReportID: "sage-lynx"})
		},
		"agent twice in one organization": func(o *Organization) {
			o.Members = append(o.Members, Member{AgentID: "moss-fox", RoleID: "lead"})
		},
	} {
		t.Run(name, func(t *testing.T) {
			g := lab()
			g.Version = 2
			broken(&g.Organizations[0])
			require.Error(t, s.ReplaceGraph(ctx, g))

			got, err := s.Graph(ctx)
			require.NoError(t, err)
			assert.Equal(t, lab(), got, "the old graph and its version must survive")
		})
	}
}

// ReplaceGraph runs under a savepoint, so a caller whose transaction carries
// on past the failure, and commits, still keeps the old graph whole.
func TestReplaceGraph_AFailureInsideACommittedTransactionLeavesTheOldGraph(t *testing.T) {
	s, _ := newTestStore(t)
	ctx := context.Background()
	require.NoError(t, s.ReplaceGraph(ctx, lab()))

	require.NoError(t, s.InTx(ctx, func(ctx context.Context, tx Tx) error {
		g := lab()
		g.Version = 2
		g.Organizations[0].Members = append(g.Organizations[0].Members, Member{AgentID: "sage-lynx", RoleID: "chair"})
		assert.Error(t, tx.ReplaceGraph(ctx, g))
		_, err := tx.OpenApproval(ctx, graphChange("change-1"))
		return err
	}))

	got, err := s.Graph(ctx)
	require.NoError(t, err)
	assert.Equal(t, lab(), got, "neither half of the failed replacement may commit")
	_, err = s.Approval(ctx, "change-1")
	assert.NoError(t, err, "the transaction's other write still commits")
}
