// sqlite_pagination_test.go — store-level pagination tests for ListChannels
// (ISSUE-0015). The handler used to fetch every row and truncate
// client-side, leaving no signal that more pages existed and silently
// loading the entire table once a deployment exceeds the soft cap. These
// tests pin the LIMIT-pushed-into-SQL contract and the cursor-after-id
// keyset paging shape.
package channels

import (
	"context"
	"sort"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// TestListChannels_LimitPushesIntoSQL verifies that the store honors a
// caller-supplied limit rather than returning every row. Pre-fix
// behaviour: the handler truncated client-side after the store loaded
// the whole table; the regression target is "ListChannels respects
// limit" so client-side truncation can be retired.
func TestListChannels_LimitPushesIntoSQL(t *testing.T) {
	store := newTestStore(t, SQLiteOptions{MaxChannels: 100})
	ctx := context.Background()

	// Five group channels, lex-sorted ids.
	for _, n := range []string{"ch01", "ch02", "ch03", "ch04", "ch05"} {
		mustCreateGroup(t, store, n)
	}

	got, err := store.ListChannels(ctx, 2, "")
	require.NoError(t, err)
	require.Len(t, got, 2, "limit must cap result count at the store boundary")
}

// TestListChannels_ZeroLimitReturnsAllRows pins the back-compat default:
// callers that pass `limit <= 0` (e.g. router reconcile or sanity tests
// that need every row) keep getting every row. The handler always
// passes a positive limit; this contract exists for non-handler
// callers that legitimately need the whole table.
func TestListChannels_ZeroLimitReturnsAllRows(t *testing.T) {
	store := newTestStore(t, SQLiteOptions{MaxChannels: 100})
	ctx := context.Background()
	for _, n := range []string{"ch01", "ch02", "ch03"} {
		mustCreateGroup(t, store, n)
	}

	got, err := store.ListChannels(ctx, 0, "")
	require.NoError(t, err)
	require.Len(t, got, 3, "limit<=0 must return every row")
}

// TestListChannels_KeysetPaging walks two pages with the after-id
// cursor and verifies every row is observed exactly once and in
// id-ascending order. Keyset (`WHERE id > ?`) rather than OFFSET so
// concurrent inserts cannot duplicate or skip rows between pages.
func TestListChannels_KeysetPaging(t *testing.T) {
	store := newTestStore(t, SQLiteOptions{MaxChannels: 100})
	ctx := context.Background()
	names := []string{"alpha", "beta", "gamma", "delta", "epsilon"}
	for _, n := range names {
		mustCreateGroup(t, store, n)
	}

	page1, err := store.ListChannels(ctx, 2, "")
	require.NoError(t, err)
	require.Len(t, page1, 2)

	cursor := page1[len(page1)-1].ID
	page2, err := store.ListChannels(ctx, 2, cursor)
	require.NoError(t, err)
	require.Len(t, page2, 2)

	cursor2 := page2[len(page2)-1].ID
	page3, err := store.ListChannels(ctx, 2, cursor2)
	require.NoError(t, err)
	require.Len(t, page3, 1, "last page returns the trailing row only")

	// Every row observed exactly once, ordering is total.
	seen := append(append([]Channel{}, page1...), page2...)
	seen = append(seen, page3...)
	require.Len(t, seen, len(names))

	ids := make([]string, len(seen))
	for i, c := range seen {
		ids[i] = c.ID
	}
	sortedIDs := append([]string{}, ids...)
	sort.Strings(sortedIDs)
	assert.Equal(t, sortedIDs, ids, "rows must arrive in id-ascending order")
}

// TestListChannels_AfterIDExcludesCursorRow pins that the cursor row
// itself is NOT returned on the next page (`WHERE id > ?`, strict
// inequality). A `>=` regression would duplicate the boundary row.
func TestListChannels_AfterIDExcludesCursorRow(t *testing.T) {
	store := newTestStore(t, SQLiteOptions{MaxChannels: 100})
	ctx := context.Background()
	for _, n := range []string{"ch01", "ch02", "ch03"} {
		mustCreateGroup(t, store, n)
	}

	got, err := store.ListChannels(ctx, 10, "group:ch02")
	require.NoError(t, err)
	require.Len(t, got, 1, "only rows strictly after cursor must surface")
	assert.Equal(t, "group:ch03", got[0].ID)
}
