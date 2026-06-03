package channels

import (
	"context"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// RFC 0048 amendment §B — LookupDM resolves an existing DM read-only and reports
// a clean not-found for a pair that has never chatted, without creating it.

func TestSQLiteStore_LookupDM_NotFoundBeforeCreate(t *testing.T) {
	store := newTestStore(t, SQLiteOptions{})
	ctx := context.Background()

	_, err := store.LookupDM(ctx, "alice", "ember-owl")
	assert.ErrorIs(t, err, ErrChannelNotFound)

	// The failed lookup must not have materialised the DM — a subsequent lookup
	// is still not-found (read-only, no side effect).
	_, err = store.LookupDM(ctx, "alice", "ember-owl")
	assert.ErrorIs(t, err, ErrChannelNotFound)
}

func TestSQLiteStore_LookupDM_ResolvesExistingCanonicalDM(t *testing.T) {
	store := newTestStore(t, SQLiteOptions{})
	ctx := context.Background()

	created, err := store.GetOrCreateDM(ctx, "ember-owl", "alice")
	require.NoError(t, err)

	// Lookup resolves the same canonical id regardless of argument order.
	got, err := store.LookupDM(ctx, "alice", "ember-owl")
	require.NoError(t, err)
	assert.Equal(t, created.ID, got.ID)
	assert.Equal(t, "dm:alice:ember-owl", got.ID)
	assert.Equal(t, ChannelTypeDM, got.Type)
}

func TestSQLiteStore_LookupDM_RejectsInvalidPair(t *testing.T) {
	store := newTestStore(t, SQLiteOptions{})
	ctx := context.Background()

	// Same participant twice is not a DM — the canonical-id derivation rejects
	// it, exactly as GetOrCreateDM would. This is the access boundary: the id is
	// derived from both parties, so a caller can only resolve a DM it is in.
	_, err := store.LookupDM(ctx, "alice", "alice")
	assert.ErrorIs(t, err, ErrInvalidParticipantID)
}
