package registry

import (
	"context"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// TestNamesFor pins the membership-scoped name lookup the channel mention lift
// needs (ISSUE-0100): given a set of ids, return only those ids' id→name pairs
// in one pass, instead of snapshotting + sorting the whole directory just to
// read a handful of names. Ids absent from the directory are simply omitted —
// the caller treats a missing name as "id-only", the same fail-open the lift
// already applies on a registry miss. (Split into its own file to keep
// registry_test.go under the review-size cap; newTestRegistry/sampleAgent are
// package-level test helpers.)
func TestNamesFor(t *testing.T) {
	r := newTestRegistry()
	ctx := context.Background()

	for _, id := range []string{"iron-fox", "nova-sparrow", "gray-owl"} {
		require.NoError(t, r.Register(ctx, sampleAgent(id)))
	}

	t.Run("returns names only for the requested ids", func(t *testing.T) {
		got, err := r.NamesFor(ctx, []string{"iron-fox", "nova-sparrow"})
		require.NoError(t, err)
		assert.Equal(t, map[string]string{
			"iron-fox":     "Test Agent iron-fox",
			"nova-sparrow": "Test Agent nova-sparrow",
		}, got, "only the requested ids are returned, gray-owl is never read")
	})

	t.Run("omits ids with no registry row", func(t *testing.T) {
		// alex — a human — has no registry row by design; an unknown id must be
		// absent from the map (not an error), so the lift sees an empty name and
		// falls back to id-only matching for it.
		got, err := r.NamesFor(ctx, []string{"iron-fox", "alex"})
		require.NoError(t, err)
		assert.Equal(t, map[string]string{"iron-fox": "Test Agent iron-fox"}, got,
			"an id with no row is omitted, not errored")
	})

	t.Run("empty input yields a non-nil empty map", func(t *testing.T) {
		got, err := r.NamesFor(ctx, nil)
		require.NoError(t, err)
		require.NotNil(t, got, "empty input returns {} not nil, matching List/FindByCapability")
		assert.Empty(t, got)
	})
}
