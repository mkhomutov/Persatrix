package channels

import (
	"context"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// TestUpdateMemberConfig_SetsThresholdAndDisposition pins the happy path: a
// participant disposition + an explicit threshold persist, normalized to the
// legacy triple with the salience bid on.
func TestUpdateMemberConfig_SetsThresholdAndDisposition(t *testing.T) {
	store := newTestStore(t, SQLiteOptions{})
	id := mustCreateGroup(t, store, "planning", "alice", "bob")
	ctx := context.Background()

	th := 0.42
	require.NoError(t, store.UpdateMemberConfig(ctx, id, "alice", RespondPolicy("participant"), &th))

	m, err := store.GetMember(ctx, id, "alice")
	require.NoError(t, err)
	assert.Equal(t, RespondAlways, m.RespondPolicy, "participant normalizes to the legacy always triple")
	assert.True(t, m.SalienceGated, "participant runs the salience bid")
	require.NotNil(t, m.Threshold)
	assert.InDelta(t, 0.42, *m.Threshold, 1e-9)
}

// TestUpdateMemberConfig_NilThresholdUnsets pins that a nil threshold clears a
// previously-set bar (bias-to-silence).
func TestUpdateMemberConfig_NilThresholdUnsets(t *testing.T) {
	store := newTestStore(t, SQLiteOptions{})
	id := mustCreateGroup(t, store, "planning", "alice", "bob")
	ctx := context.Background()

	th := 0.5
	require.NoError(t, store.UpdateMemberConfig(ctx, id, "alice", "participant", &th))
	require.NoError(t, store.UpdateMemberConfig(ctx, id, "alice", "participant", nil))

	m, err := store.GetMember(ctx, id, "alice")
	require.NoError(t, err)
	assert.Nil(t, m.Threshold, "a nil threshold unsets the bar")
}

// TestUpdateMemberConfig_RejectsBadThreshold pins the two threshold rules: range
// and disposition applicability — the same invariants config load enforces, so
// the REST PATCH cannot persist a pair the YAML path would reject.
func TestUpdateMemberConfig_RejectsBadThreshold(t *testing.T) {
	store := newTestStore(t, SQLiteOptions{})
	id := mustCreateGroup(t, store, "planning", "alice", "bob")
	ctx := context.Background()

	over := 1.5
	assert.ErrorIs(t, store.UpdateMemberConfig(ctx, id, "alice", "participant", &over), ErrInvalidThreshold)

	th := 0.3
	assert.ErrorIs(t, store.UpdateMemberConfig(ctx, id, "alice", "observer", &th), ErrThresholdNotApplicable,
		"a threshold on a non-open-floor disposition is rejected")
}

// TestUpdateMemberConfig_NotFound pins the 404-bound sentinels: a missing member
// is ErrMemberNotFound (distinct from publish-time ErrNotMember), a missing
// channel is ErrChannelNotFound.
func TestUpdateMemberConfig_NotFound(t *testing.T) {
	store := newTestStore(t, SQLiteOptions{})
	id := mustCreateGroup(t, store, "planning", "alice", "bob")
	ctx := context.Background()

	assert.ErrorIs(t, store.UpdateMemberConfig(ctx, id, "carol", "participant", nil), ErrMemberNotFound)
	assert.ErrorIs(t, store.UpdateMemberConfig(ctx, "group:ghost", "alice", "participant", nil), ErrChannelNotFound)
}
