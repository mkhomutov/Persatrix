// RFC 0050 Phase 1 PR 2 — the single validated apply path
// ([ChannelRouter.ApplyChannelConfig]) and the boot repoint
// ([ChannelRouter.ResolveFromStore]) that seed the router's in-memory
// governance maps from the canonical channel store.
//
// PR 1 landed storage only (overrides written/read but never consulted at
// runtime). PR 2 wires that storage to the live router: an apply persists +
// bumps the revision (PR 1's optimistic-concurrency primitive) AND reflects the
// six router-held knobs through the existing setters, and at boot the router is
// seeded from the store for any channel the operator has edited. These tests
// pin that round-trip, the validate-before-write contract, and the
// restart-preservation property (RFC 0050 goal G1).
package channels

import (
	"context"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.uber.org/zap"
)

// newApplyRouter opens a fresh on-disk store and a router over it — the minimal
// rig for exercising the apply path and the boot repoint.
func newApplyRouter(t *testing.T) (*ChannelRouter, ChannelStore, context.Context) {
	t.Helper()
	store := newTestStore(t, SQLiteOptions{})
	router := NewChannelRouter(store, NoopDispatcher{}, zap.NewNop(), nil)
	return router, store, context.Background()
}

// idleWindowFor reads the resolved idle window under the router's lock — the
// router has no exported idle getter (the hot path reads idleWindowLocked), so
// the test borrows the same locked read.
func idleWindowFor(r *ChannelRouter, channelID string) time.Duration {
	r.interactionMu.Lock()
	defer r.interactionMu.Unlock()
	return r.idleWindowLocked(channelID)
}

// TestApplyChannelConfig_PersistsAndReflectedByRouter is the PR-2 happy path: a
// sparse patch across every router-held knob persists to the store AND is
// reflected by the router getters without a restart.
func TestApplyChannelConfig_PersistsAndReflectedByRouter(t *testing.T) {
	router, store, ctx := newApplyRouter(t)
	mustCreateGroup(t, store, "planning", "ada", "iron-fox")

	fc := false
	maxMembers := 12
	replyK := 3
	endK, endW := 4, 5
	budget := int64(50_000)
	idle := 30
	patch := ChannelConfigOverrides{
		FloorControl:                           &fc,
		SalienceMaxChannelMembers:              &maxMembers,
		MaxRepliesPerParticipantPerInteraction: &replyK,
		EndVoteThreshold:                       &endK,
		EndVoteWindow:                          &endW,
		InteractionBudgetTokens:                &budget,
		InteractionIdleTimeoutSeconds:          &idle,
	}
	require.NoError(t, router.ApplyChannelConfig(ctx, "group:planning", patch, 0, ""))

	// Persisted: the store round-trips the patch and bumped the revision.
	got, revision, err := store.GetChannelConfig(ctx, "group:planning")
	require.NoError(t, err)
	assert.Equal(t, int64(1), revision, "first apply bumps revision 0 → 1")
	require.NotNil(t, got.SalienceMaxChannelMembers)
	assert.Equal(t, 12, *got.SalienceMaxChannelMembers)

	// Reflected: every router-held knob honours the apply live.
	enabled, _, set := router.FloorControlFor("group:planning")
	assert.True(t, set, "floor control resolved")
	assert.False(t, enabled, "explicit floor_control:false applied live")

	maxOut, setMax := router.SalienceMaxChannelMembersFor("group:planning")
	assert.True(t, setMax)
	assert.Equal(t, 12, maxOut)

	assert.Equal(t, 3, router.ReplyBudgetFor("group:planning"))

	k, w := router.EndVoteParamsFor("group:planning")
	assert.Equal(t, 4, k)
	assert.Equal(t, 5, w)

	assert.Equal(t, 30*time.Second, idleWindowFor(router, "group:planning"))
}

// TestApplyChannelConfig_AbsentKnobsResolveToDefaults asserts the apply path is
// store-canonical: a knob absent from the patch resolves to the inherited
// default in the router, not to whatever was there before. This is the
// shadow-the-whole-YAML-block semantics the revision gate turns on (RFC 0050).
func TestApplyChannelConfig_AbsentKnobsResolveToDefaults(t *testing.T) {
	router, store, ctx := newApplyRouter(t)
	mustCreateGroup(t, store, "planning", "ada")

	// Pre-seed a non-default salience cap as a stand-in for a YAML-resolved
	// value already in the router.
	router.SetSalienceMaxChannelMembers("group:planning", 7)

	// An apply that touches only floor control must reset the un-mentioned
	// salience cap to the package default (the store now says "inherit").
	fc := true
	require.NoError(t, router.ApplyChannelConfig(ctx, "group:planning",
		ChannelConfigOverrides{FloorControl: &fc}, 0, ""))

	maxOut, set := router.SalienceMaxChannelMembersFor("group:planning")
	assert.True(t, set, "the apply re-stamped the cap (to the default)")
	assert.Equal(t, DefaultSalienceMaxChannelMembers, maxOut,
		"an absent knob falls back to the package default, not the prior value")
}

// TestApplyChannelConfig_InvalidPatchRejectedBeforeWrite pins the
// validate-before-persist contract: a malformed patch is rejected and leaves
// both the store and the router untouched.
func TestApplyChannelConfig_InvalidPatchRejectedBeforeWrite(t *testing.T) {
	router, store, ctx := newApplyRouter(t)
	mustCreateGroup(t, store, "planning", "ada")

	bad := -5
	err := router.ApplyChannelConfig(ctx, "group:planning",
		ChannelConfigOverrides{SalienceMaxChannelMembers: &bad}, 0, "")
	require.Error(t, err)
	assert.ErrorIs(t, err, ErrInvalidSalienceMaxChannelMembers)

	// Nothing persisted.
	_, revision, err := store.GetChannelConfig(ctx, "group:planning")
	require.NoError(t, err)
	assert.Equal(t, int64(0), revision, "a rejected patch does not bump the revision")

	// Router untouched — no entry was stamped.
	_, set := router.SalienceMaxChannelMembersFor("group:planning")
	assert.False(t, set, "a rejected patch leaves the router map untouched")
}

// TestApplyChannelConfig_StaleRevisionConflict asserts the apply path surfaces
// the PR-1 optimistic-concurrency conflict verbatim (the REST layer maps it to
// 409 in PR 4).
func TestApplyChannelConfig_StaleRevisionConflict(t *testing.T) {
	router, store, ctx := newApplyRouter(t)
	mustCreateGroup(t, store, "planning", "ada")

	k := 9
	require.NoError(t, router.ApplyChannelConfig(ctx, "group:planning",
		ChannelConfigOverrides{SalienceMaxChannelMembers: &k}, 0, ""))
	// Store is at revision 1; a writer that still believes it is 0 loses.
	other := 99
	err := router.ApplyChannelConfig(ctx, "group:planning",
		ChannelConfigOverrides{SalienceMaxChannelMembers: &other}, 0, "")
	require.Error(t, err)
	assert.ErrorIs(t, err, ErrConfigRevisionConflict)

	// The losing apply did not touch the router.
	maxOut, _ := router.SalienceMaxChannelMembersFor("group:planning")
	assert.Equal(t, 9, maxOut, "the winning apply's value stands")
}

// TestApplyChannelConfig_EscalationChair pins the cross-field chair validation:
// a non-member chair is rejected before any write, while a declared member is
// accepted and applied live.
func TestApplyChannelConfig_EscalationChair(t *testing.T) {
	router, store, ctx := newApplyRouter(t)
	mustCreateGroup(t, store, "planning", "ada", "iron-fox")

	ghost := "ghost"
	err := router.ApplyChannelConfig(ctx, "group:planning",
		ChannelConfigOverrides{EscalationChairID: &ghost}, 0, "")
	require.Error(t, err)
	assert.ErrorIs(t, err, ErrInvalidEscalationChair)
	_, revision, _ := store.GetChannelConfig(ctx, "group:planning")
	assert.Equal(t, int64(0), revision, "a bad-chair patch does not persist")

	// A declared member is accepted and applied to the router.
	chair := "ada"
	require.NoError(t, router.ApplyChannelConfig(ctx, "group:planning",
		ChannelConfigOverrides{EscalationChairID: &chair}, 0, ""))
	assert.Equal(t, "ada", router.escalationChairFor("group:planning"))
}

// TestApplyChannelConfig_EscalationChairRequiresFloorControl asserts a chair set
// alongside an explicit floor_control:false is rejected — stall detection runs
// only at the floor round's tail, so the knob would be silently inert (mirrors
// the config-load rule).
func TestApplyChannelConfig_EscalationChairRequiresFloorControl(t *testing.T) {
	router, store, ctx := newApplyRouter(t)
	mustCreateGroup(t, store, "planning", "ada")

	chair := "ada"
	off := false
	err := router.ApplyChannelConfig(ctx, "group:planning",
		ChannelConfigOverrides{EscalationChairID: &chair, FloorControl: &off}, 0, "")
	require.Error(t, err)
	assert.ErrorIs(t, err, ErrInvalidEscalationChair)
}

// TestResolveFromStore_RestartPreservesApply is RFC 0050 goal G1: an apply
// survives a process restart. A second router built over the same store, seeded
// via ResolveFromStore at boot, reflects the prior apply.
func TestResolveFromStore_RestartPreservesApply(t *testing.T) {
	store := newTestStore(t, SQLiteOptions{})
	ctx := context.Background()
	mustCreateGroup(t, store, "planning", "ada", "iron-fox")

	r1 := NewChannelRouter(store, NoopDispatcher{}, zap.NewNop(), nil)
	fc := false
	maxMembers := 15
	require.NoError(t, r1.ApplyChannelConfig(ctx, "group:planning",
		ChannelConfigOverrides{FloorControl: &fc, SalienceMaxChannelMembers: &maxMembers}, 0, ""))

	// Restart: a brand-new router over the same store, seeded from the store.
	r2 := NewChannelRouter(store, NoopDispatcher{}, zap.NewNop(), nil)
	require.NoError(t, r2.ResolveFromStore(ctx))

	enabled, _, set := r2.FloorControlFor("group:planning")
	assert.True(t, set, "restart re-seeded floor control from the store")
	assert.False(t, enabled, "the prior floor_control:false survives restart")
	maxOut, setMax := r2.SalienceMaxChannelMembersFor("group:planning")
	assert.True(t, setMax)
	assert.Equal(t, 15, maxOut, "the prior salience cap survives restart")
}

// TestResolveFromStore_UneditedChannelLeavesSeedingIntact asserts the
// "empty overrides → identical to today" property: a channel the store has
// never had edited (revision 0) is skipped by ResolveFromStore, so its
// YAML/default seeding (modelled here by a prior SetSalienceMaxChannelMembers)
// stands rather than being clobbered to the package default.
func TestResolveFromStore_UneditedChannelLeavesSeedingIntact(t *testing.T) {
	store := newTestStore(t, SQLiteOptions{})
	ctx := context.Background()
	mustCreateGroup(t, store, "design", "ada")

	router := NewChannelRouter(store, NoopDispatcher{}, zap.NewNop(), nil)
	// Model the YAML resolver having seeded a non-default cap at boot.
	router.SetSalienceMaxChannelMembers("group:design", 7)

	require.NoError(t, router.ResolveFromStore(ctx))

	maxOut, _ := router.SalienceMaxChannelMembersFor("group:design")
	assert.Equal(t, 7, maxOut,
		"an un-edited channel (revision 0) is skipped — its prior seeding is untouched")
}
