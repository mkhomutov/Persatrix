package registry

import (
	"context"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// TestListSortedByID pins a stable List ordering. The agents map has a
// randomized Go iteration order, so List returned a different sequence on every
// call — the web console re-fetches the persona list on each tab switch (RFC
// 0048), so the dropdown reshuffled each time. List must sort by ID so every
// consumer (web picker, channel decoration, CLI) sees one deterministic order.
// (Split into its own file to keep registry_test.go under the review-size cap;
// newTestRegistry/sampleAgent are package-level test helpers.)
func TestListSortedByID(t *testing.T) {
	r := newTestRegistry()
	ctx := context.Background()

	// Register out of sorted order, with enough entries that a randomized map
	// walk would essentially never return them sorted by chance (8! permutations).
	insertion := []string{"delta", "alpha", "hotel", "charlie", "bravo", "golf", "echo", "foxtrot"}
	for _, id := range insertion {
		require.NoError(t, r.Register(ctx, sampleAgent(id)))
	}

	list, err := r.List(ctx)
	require.NoError(t, err)

	ids := make([]string, len(list))
	for i, a := range list {
		ids[i] = a.ID
	}
	want := []string{"alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf", "hotel"}
	assert.Equal(t, want, ids, "List must return agents sorted by ID for a stable dropdown order")
}
