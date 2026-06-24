// RFC 0050 Phase 1 PR 3 — revision-gated YAML reconciliation + drift detection.
//
// PR 2 made the channel store the single source of truth and seeded the router
// from it at boot ([ChannelRouter.ResolveFromStore]). PR 3 closes the loop in
// the other direction: it turns the `config/channels.yaml` loader into a
// revision-gated WRITER into the store, so config-as-code and live edits coexist
// under one ordering rule — the higher per-channel revision wins. These tests
// pin the decision table (YAML newer / equal+same-hash / equal+diff-hash /
// older / absent), the absent-revision migration case (existing channel left
// untouched), and the store-side adopt primitive that SETS (rather than bumps)
// the revision to the YAML-declared value.
package channels

import (
	"context"
	"errors"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.uber.org/zap"
	"go.uber.org/zap/zaptest/observer"
)

// newReconcileRouter is newApplyRouter with an observed logger so the drift
// WARN (mechanic 4) is assertable.
func newReconcileRouter(t *testing.T) (*ChannelRouter, ChannelStore, *observer.ObservedLogs, context.Context) {
	t.Helper()
	store := newTestStore(t, SQLiteOptions{})
	core, logs := observer.New(zap.WarnLevel)
	router := NewChannelRouter(store, NoopDispatcher{}, zap.New(core), nil)
	return router, store, logs, context.Background()
}

// reconcileCfg builds a one-channel [Config] (the `planning` group with two
// members) at the given declared revision and floor-control state — the minimal
// shape the decision-table tests need. A nil floorControl omits the knob
// (resolved default ON).
func reconcileCfg(t *testing.T, revision int64, floorControl *bool) *Config {
	t.Helper()
	cfg, err := LoadConfig(writeYAML(t, planningYAML(revision, floorControl)))
	require.NoError(t, err)
	return cfg
}

func planningYAML(revision int64, floorControl *bool) string {
	body := "channels:\n  - name: planning\n    members:\n      - ada\n      - iron-fox\n"
	if revision != 0 {
		body += "    revision: " + itoa(revision) + "\n"
	}
	if floorControl != nil {
		if *floorControl {
			body += "    floor_control: true\n"
		} else {
			body += "    floor_control: false\n"
		}
	}
	return body
}

func itoa(v int64) string {
	// Local tiny helper so the YAML builder reads cleanly; strconv would do but
	// keeps the import surface of this test file minimal.
	if v == 0 {
		return "0"
	}
	neg := v < 0
	if neg {
		v = -v
	}
	var buf [20]byte
	i := len(buf)
	for v > 0 {
		i--
		buf[i] = byte('0' + v%10)
		v /= 10
	}
	if neg {
		i--
		buf[i] = '-'
	}
	return string(buf[i:])
}

// --- YAML `revision:` field parsing + validation ----------------------------

func TestLoadConfig_ParsesRevision(t *testing.T) {
	one := true
	cfg := reconcileCfg(t, 7, &one)
	require.Len(t, cfg.Channels, 1)
	assert.Equal(t, int64(7), cfg.Channels[0].Revision)
}

func TestLoadConfig_AbsentRevisionIsZero(t *testing.T) {
	cfg := reconcileCfg(t, 0, nil)
	require.Len(t, cfg.Channels, 1)
	assert.Equal(t, int64(0), cfg.Channels[0].Revision, "absent revision reads as 0 (seed-only)")
}

func TestLoadConfig_RejectsNegativeRevision(t *testing.T) {
	body := "channels:\n  - name: planning\n    revision: -1\n    members:\n      - ada\n"
	_, err := LoadConfig(writeYAML(t, body))
	require.Error(t, err)
	assert.ErrorIs(t, err, ErrInvalidConfigRevision)
}

// --- store adopt primitive --------------------------------------------------

// TestReconcileChannelConfig_SetsRevisionExactly verifies the boot-loader write
// path SETS the revision to the YAML-declared value rather than bumping by one
// (the [ChannelStore.PutChannelConfig] CAS semantics) — so a hand-authored
// skip-ahead revision converges in one boot and is idempotent thereafter.
func TestReconcileChannelConfig_SetsRevisionExactly(t *testing.T) {
	_, store, _, ctx := newReconcileRouter(t)
	mustCreateGroup(t, store, "planning", "ada", "iron-fox")

	fc := false
	overrides := ChannelConfigOverrides{FloorControl: &fc}
	require.NoError(t, store.ReconcileChannelConfig(ctx, "group:planning", overrides, 5))

	got, revision, err := store.GetChannelConfig(ctx, "group:planning")
	require.NoError(t, err)
	assert.Equal(t, int64(5), revision, "revision SET to the declared value, not bumped")
	require.NotNil(t, got.FloorControl)
	assert.False(t, *got.FloorControl)

	// Idempotent: re-writing the same revision/content does not drift the row.
	require.NoError(t, store.ReconcileChannelConfig(ctx, "group:planning", overrides, 5))
	_, revision, err = store.GetChannelConfig(ctx, "group:planning")
	require.NoError(t, err)
	assert.Equal(t, int64(5), revision)
}

func TestReconcileChannelConfig_EmptyOverridesPersistAsNull(t *testing.T) {
	_, store, _, ctx := newReconcileRouter(t)
	mustCreateGroup(t, store, "planning", "ada")

	require.NoError(t, store.ReconcileChannelConfig(ctx, "group:planning", ChannelConfigOverrides{}, 3))
	got, revision, err := store.GetChannelConfig(ctx, "group:planning")
	require.NoError(t, err)
	assert.Equal(t, int64(3), revision)
	assert.True(t, got.IsEmpty(), "all-unset overrides read back as inherit-all")
}

func TestReconcileChannelConfig_UnknownChannel(t *testing.T) {
	_, store, _, ctx := newReconcileRouter(t)
	err := store.ReconcileChannelConfig(ctx, "group:ghost", ChannelConfigOverrides{}, 1)
	assert.ErrorIs(t, err, ErrChannelNotFound)
}

// --- toConfigOverrides ------------------------------------------------------

// TestToConfigOverrides_FullResolvedSnapshot pins that the YAML→store snapshot
// captures the COMPLETE resolved governance set (every router-held knob plus the
// persisted interaction budget), so the store row is a faithful image of the
// YAML and the drift hash is computed over a stable canonical form.
func TestToConfigOverrides_FullResolvedSnapshot(t *testing.T) {
	fc := false
	cfg := reconcileCfg(t, 1, &fc)
	o := cfg.Channels[0].toConfigOverrides(cfg)

	require.NotNil(t, o.FloorControl)
	assert.False(t, *o.FloorControl)
	require.NotNil(t, o.SalienceMaxChannelMembers)
	assert.Equal(t, DefaultSalienceMaxChannelMembers, *o.SalienceMaxChannelMembers)
	require.NotNil(t, o.EndVoteThreshold)
	assert.Equal(t, DefaultEndVoteThreshold, *o.EndVoteThreshold)
	require.NotNil(t, o.EndVoteWindow)
	assert.Equal(t, DefaultEndVoteWindow, *o.EndVoteWindow)
	require.NotNil(t, o.InteractionIdleTimeoutSeconds)
	assert.Equal(t, DefaultInteractionIdleTimeoutSeconds, *o.InteractionIdleTimeoutSeconds)
	// Opt-in knobs default to 0 (uncapped) but are still snapshotted explicitly.
	require.NotNil(t, o.MaxRepliesPerParticipantPerInteraction)
	assert.Equal(t, 0, *o.MaxRepliesPerParticipantPerInteraction)
	require.NotNil(t, o.InteractionBudgetTokens)
	assert.Equal(t, int64(0), *o.InteractionBudgetTokens)
	// An absent escalation chair stays nil (no escalation), not &"".
	assert.Nil(t, o.EscalationChairID)
	// RFC 0051: a default-off reasoning rung snapshots as inherit (nil), like the
	// chair — so a never-deliberated channel stays responsive to the PR 6 flip and
	// boots byte-identically (no `reasoning` key in the store blob).
	assert.Nil(t, o.Reasoning, "default-off reasoning rung stays inherit in the snapshot")
}

// --- the decision table -----------------------------------------------------

// absent revision (= 0): seed-only. The store row is left untouched (revision
// stays 0, config-as-code still owns it) — the existing per-knob resolvers seed
// the router. This is the migration case: an existing channel with no revision.
func TestReconcileFromYAML_AbsentRevision_StoreUntouched(t *testing.T) {
	router, store, logs, ctx := newReconcileRouter(t)
	mustCreateGroup(t, store, "planning", "ada", "iron-fox")

	fc := false
	require.NoError(t, router.ReconcileFromYAML(ctx, reconcileCfg(t, 0, &fc)))

	overrides, revision, err := store.GetChannelConfig(ctx, "group:planning")
	require.NoError(t, err)
	assert.Equal(t, int64(0), revision, "absent revision never writes the store")
	assert.True(t, overrides.IsEmpty(), "store stays inherit-all (config-as-code owns it)")
	assert.Zero(t, logs.Len(), "seed-only is silent — no drift warning")
}

// YAML newer (revision > store): adopt. The store takes the YAML overrides at
// the declared revision.
func TestReconcileFromYAML_YamlNewer_Adopts(t *testing.T) {
	router, store, _, ctx := newReconcileRouter(t)
	mustCreateGroup(t, store, "planning", "ada", "iron-fox")

	fc := false
	require.NoError(t, router.ReconcileFromYAML(ctx, reconcileCfg(t, 1, &fc)))

	overrides, revision, err := store.GetChannelConfig(ctx, "group:planning")
	require.NoError(t, err)
	assert.Equal(t, int64(1), revision, "store adopts the YAML-declared revision")
	require.NotNil(t, overrides.FloorControl)
	assert.False(t, *overrides.FloorControl, "YAML floor_control:false adopted into the store")
}

// YAML newer than a prior LIVE edit: a committed GitOps push (higher revision)
// wins over the store, and re-running converges (idempotent), not re-applying
// every boot — the reason the adopt path SETS rather than bumps.
func TestReconcileFromYAML_YamlNewerThanLiveEdit_AdoptsAndIsIdempotent(t *testing.T) {
	router, store, _, ctx := newReconcileRouter(t)
	mustCreateGroup(t, store, "planning", "ada", "iron-fox")

	// A live edit lands at revision 1.
	live := true
	require.NoError(t, router.ApplyChannelConfig(ctx, "group:planning",
		ChannelConfigOverrides{FloorControl: &live}, 0, ""))

	// GitOps commits a skip-ahead revision 5 that opts floor control back out.
	fc := false
	cfg := reconcileCfg(t, 5, &fc)
	require.NoError(t, router.ReconcileFromYAML(ctx, cfg))

	overrides, revision, err := store.GetChannelConfig(ctx, "group:planning")
	require.NoError(t, err)
	assert.Equal(t, int64(5), revision, "adopts the declared revision in one boot")
	require.NotNil(t, overrides.FloorControl)
	assert.False(t, *overrides.FloorControl)

	// Re-running the same reconcile must not bump again (idempotent at equal rev).
	require.NoError(t, router.ReconcileFromYAML(ctx, cfg))
	_, revision, err = store.GetChannelConfig(ctx, "group:planning")
	require.NoError(t, err)
	assert.Equal(t, int64(5), revision, "equal revision + same content is a no-op")
}

// equal revision + same content hash: in sync, silent no-op.
func TestReconcileFromYAML_EqualRevisionSameContent_NoOp(t *testing.T) {
	router, store, logs, ctx := newReconcileRouter(t)
	mustCreateGroup(t, store, "planning", "ada", "iron-fox")

	fc := false
	cfg := reconcileCfg(t, 2, &fc)
	require.NoError(t, router.ReconcileFromYAML(ctx, cfg)) // adopt → rev 2
	require.NoError(t, router.ReconcileFromYAML(ctx, cfg)) // equal rev, same content

	_, revision, err := store.GetChannelConfig(ctx, "group:planning")
	require.NoError(t, err)
	assert.Equal(t, int64(2), revision)
	assert.Zero(t, logs.Len(), "in-sync equal revision emits no drift warning")
}

// equal revision + differing content hash: DRIFT. Warn loudly; the store stays
// authoritative (no write).
func TestReconcileFromYAML_EqualRevisionDiffContent_DriftWarn(t *testing.T) {
	router, store, logs, ctx := newReconcileRouter(t)
	mustCreateGroup(t, store, "planning", "ada", "iron-fox")

	off := false
	require.NoError(t, router.ReconcileFromYAML(ctx, reconcileCfg(t, 2, &off))) // adopt rev 2, floor off

	// Same revision 2 but content changed (floor control back on) — someone
	// edited YAML without re-exporting / bumping.
	on := true
	require.NoError(t, router.ReconcileFromYAML(ctx, reconcileCfg(t, 2, &on)))

	overrides, revision, err := store.GetChannelConfig(ctx, "group:planning")
	require.NoError(t, err)
	assert.Equal(t, int64(2), revision, "store stays authoritative on drift")
	require.NotNil(t, overrides.FloorControl)
	assert.False(t, *overrides.FloorControl, "drift does NOT silently apply the YAML")

	require.Equal(t, 1, logs.Len(), "drift emits exactly one warning")
	entry := logs.All()[0]
	assert.Contains(t, entry.Message, "drift")
	fields := entry.ContextMap()
	assert.Equal(t, "group:planning", fields["channel_id"])
}

// YAML older (revision < store): ignored — a live edit raised the store past
// the committed YAML. Store stays authoritative.
func TestReconcileFromYAML_YamlOlder_Ignored(t *testing.T) {
	router, store, logs, ctx := newReconcileRouter(t)
	mustCreateGroup(t, store, "planning", "ada", "iron-fox")

	// Adopt revision 3 first.
	off := false
	require.NoError(t, router.ReconcileFromYAML(ctx, reconcileCfg(t, 3, &off)))

	// A committed YAML at the older revision 1 (e.g. a stale branch) is ignored.
	on := true
	require.NoError(t, router.ReconcileFromYAML(ctx, reconcileCfg(t, 1, &on)))

	overrides, revision, err := store.GetChannelConfig(ctx, "group:planning")
	require.NoError(t, err)
	assert.Equal(t, int64(3), revision, "older YAML never lowers the store revision")
	require.NotNil(t, overrides.FloorControl)
	assert.False(t, *overrides.FloorControl, "older YAML is ignored, not applied")
	assert.Zero(t, logs.Len(), "an ignored older revision is not drift")
}

// A channel declared in YAML but absent from the store (reconcile bypass /
// partial create) is skipped gracefully, not a hard error.
func TestReconcileFromYAML_ChannelNotInStore_Skipped(t *testing.T) {
	router, _, _, ctx := newReconcileRouter(t)
	// No store row created for `planning`.
	err := router.ReconcileFromYAML(ctx, reconcileCfg(t, 1, nil))
	require.NoError(t, err, "a YAML channel missing from the store is skipped, not fatal")
}

func TestReconcileFromYAML_NilConfig(t *testing.T) {
	router, _, _, ctx := newReconcileRouter(t)
	assert.NoError(t, router.ReconcileFromYAML(ctx, nil))
}

// failingGetStore wraps a ChannelStore and injects a HARD (non-ErrChannelNotFound)
// error from GetChannelConfig for one channel id — the transient-store-fault case
// the per-channel resilience guard exists for. Every other method delegates to the
// embedded store.
type failingGetStore struct {
	ChannelStore
	failID string
}

func (f *failingGetStore) GetChannelConfig(ctx context.Context, id string) (ChannelConfigOverrides, int64, error) {
	if id == f.failID {
		return ChannelConfigOverrides{}, 0, errors.New("channels: injected store fault")
	}
	return f.ChannelStore.GetChannelConfig(ctx, id)
}

// A hard store error on ONE channel must not abort the whole reconcile pass: the
// channels declared after the failing one must still be adopted. Otherwise a
// transient fault on an early channel silently defers every later channel's
// adoption to the next restart — exactly the "log-and-continue, per-channel"
// posture the method's doc and the boot wiring promise. The fault is still
// surfaced to the caller (a non-nil return) so the orchestrator logs the incident.
func TestReconcileFromYAML_PerChannelErrorDoesNotAbortPass(t *testing.T) {
	store := newTestStore(t, SQLiteOptions{})
	core, logs := observer.New(zap.WarnLevel)
	mustCreateGroup(t, store, "planning", "ada", "iron-fox")
	mustCreateGroup(t, store, "design", "ada", "iron-fox")

	// planning is declared first and is wired to fault; design is declared after.
	wrapped := &failingGetStore{ChannelStore: store, failID: "group:planning"}
	router := NewChannelRouter(wrapped, NoopDispatcher{}, zap.New(core), nil)
	ctx := context.Background()

	body := "channels:\n" +
		"  - name: planning\n    revision: 1\n    members:\n      - ada\n      - iron-fox\n" +
		"  - name: design\n    revision: 1\n    members:\n      - ada\n      - iron-fox\n"
	cfg, err := LoadConfig(writeYAML(t, body))
	require.NoError(t, err)

	rErr := router.ReconcileFromYAML(ctx, cfg)
	require.Error(t, rErr, "a per-channel fault is surfaced to the caller (orchestrator logs it)")

	// The channel declared AFTER the failing one is still adopted — the pass did
	// not abort at the first fault.
	_, rev, err := store.GetChannelConfig(ctx, "group:design")
	require.NoError(t, err)
	assert.Equal(t, int64(1), rev, "a later channel is adopted despite an earlier channel's fault")

	assert.NotZero(t, logs.Len(), "the per-channel fault is logged, not swallowed")
}
