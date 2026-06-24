// RFC 0051 Phase 3a (PR 4) — the per-channel `reasoning` config block on the RFC
// 0050 surface. These tests pin (a) the YAML load/normalize/validate path for the
// `reasoning.{mode,model,depth,revise}` knob, including the capability gate that
// rejects unbacked values (`depth: deep`, `revise >= 1`) and the cross-field rule
// that `mode != off` requires a salience-gated channel, and (b) the runtime
// override apply path (validate → persist → bump revision → stamp router), with
// the governed-channel default held at `off` (the flip is PR 6).
package channels

import (
	"context"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// mustCreateGovernedGroup creates a group channel with one salience-gated
// open-floor participant — the shape `reasoning.mode != off` requires (the
// deliberation rides the Tier B salience seam, so it is inert without a
// salience-gated member; [ChannelRouter.validateReasoningGoverned] rejects a
// non-off mode on an ungoverned channel).
func mustCreateGovernedGroup(t *testing.T, store ChannelStore, name string) string {
	t.Helper()
	id := "group:" + name
	require.NoError(t, store.CreateChannelWithMembers(context.Background(),
		Channel{ID: id, Name: name, Type: ChannelTypeGroup},
		[]Member{{ParticipantID: "ada", RespondPolicy: RespondAlways, SalienceGated: true}},
	))
	return id
}

// --- YAML load + validate -------------------------------------------------

// TestLoadConfig_ReasoningDefaults: an absent reasoning block normalizes to the
// shipped default rung (off / fast / shallow / 0).
func TestLoadConfig_ReasoningDefaults(t *testing.T) {
	body := `
channels:
  - name: planning
    members:
      - id: ada
        respond: participant
`
	cfg, err := LoadConfig(writeYAML(t, body))
	require.NoError(t, err)
	rc := cfg.Channels[0].Reasoning
	assert.Equal(t, ReasoningModeOff, rc.Mode)
	assert.Equal(t, ReasoningModelFast, rc.Model)
	assert.Equal(t, ReasoningDepthShallow, rc.Depth)
	assert.Equal(t, 0, rc.Revise)
}

// TestLoadConfig_ReasoningPartialNormalizes: a block that sets only `mode` fills
// the rest from the defaults (per-field normalization, not all-or-nothing).
func TestLoadConfig_ReasoningPartialNormalizes(t *testing.T) {
	body := `
channels:
  - name: planning
    members:
      - id: ada
        respond: participant
    reasoning:
      mode: bid
`
	cfg, err := LoadConfig(writeYAML(t, body))
	require.NoError(t, err)
	rc := cfg.Channels[0].Reasoning
	assert.Equal(t, ReasoningModeBid, rc.Mode)
	assert.Equal(t, ReasoningModelFast, rc.Model, "model fills from default")
	assert.Equal(t, ReasoningDepthShallow, rc.Depth, "depth fills from default")
}

// TestLoadConfig_ReasoningModeBidGoverned: `mode: bid` is accepted on a channel
// with a salience-gated member.
func TestLoadConfig_ReasoningModeBidGoverned(t *testing.T) {
	body := `
channels:
  - name: planning
    members:
      - id: ada
        respond: participant
    reasoning:
      mode: bid
`
	_, err := LoadConfig(writeYAML(t, body))
	require.NoError(t, err)
}

// TestLoadConfig_ReasoningModeRejectedUngoverned: `mode != off` on a channel
// without a salience-gated member is rejected — the knob does not by itself arm
// the gate (RFC 0051 §G; the deliberation rides the Tier B seam).
func TestLoadConfig_ReasoningModeRejectedUngoverned(t *testing.T) {
	body := `
channels:
  - name: planning
    members:
      - id: ada
        respond: addressed
    reasoning:
      mode: bid
`
	_, err := LoadConfig(writeYAML(t, body))
	require.Error(t, err)
	assert.ErrorIs(t, err, ErrInvalidReasoningMode)
}

// TestLoadConfig_ReasoningUnknownModeRejected: an out-of-vocabulary mode is a
// loud load error, not a silent fall-back.
func TestLoadConfig_ReasoningUnknownModeRejected(t *testing.T) {
	body := `
channels:
  - name: planning
    members:
      - id: ada
        respond: participant
    reasoning:
      mode: ponder
`
	_, err := LoadConfig(writeYAML(t, body))
	require.Error(t, err)
	assert.ErrorIs(t, err, ErrInvalidReasoningMode)
}

// TestLoadConfig_ReasoningDepthDeepRejected: `depth: deep` is capability-rejected
// in v0.3.10 (Phase 4 unbuilt) rather than silently degraded to shallow.
func TestLoadConfig_ReasoningDepthDeepRejected(t *testing.T) {
	body := `
channels:
  - name: planning
    members:
      - id: ada
        respond: participant
    reasoning:
      mode: plan
      depth: deep
`
	_, err := LoadConfig(writeYAML(t, body))
	require.Error(t, err)
	assert.ErrorIs(t, err, ErrInvalidReasoningDepth)
}

// TestLoadConfig_ReasoningReviseRejected: `revise >= 1` is capability-rejected
// (Phase 5 not yet deployed); negative is an outright invalid value.
func TestLoadConfig_ReasoningReviseRejected(t *testing.T) {
	for _, revise := range []int{1, 2, -1} {
		body := `
channels:
  - name: planning
    members:
      - id: ada
        respond: participant
    reasoning:
      mode: plan
      revise: ` + itoa(int64(revise)) + `
`
		_, err := LoadConfig(writeYAML(t, body))
		require.Error(t, err, "revise=%d", revise)
		assert.ErrorIs(t, err, ErrInvalidReasoningRevise, "revise=%d", revise)
	}
}

// TestLoadConfig_ReasoningModelQualityWarnsButLoads: `model: quality` is a
// soft-discouraged (warn) value, not a rejection — the cheap-pass economics are a
// recommendation, so the config still loads.
func TestLoadConfig_ReasoningModelQualityWarnsButLoads(t *testing.T) {
	body := `
channels:
  - name: planning
    members:
      - id: ada
        respond: participant
    reasoning:
      mode: bid
      model: quality
`
	cfg, err := LoadConfig(writeYAML(t, body))
	require.NoError(t, err)
	assert.Equal(t, ReasoningModelQuality, cfg.Channels[0].Reasoning.Model)
}

// --- override Validate ----------------------------------------------------

// TestReasoningOverridesValidate_CapabilityGate: the runtime override Validate
// rejects the same unbacked values the loader does (the REST PATCH path runs this,
// not the JSON schema).
func TestReasoningOverridesValidate_CapabilityGate(t *testing.T) {
	deep := ReasoningDepthDeep
	revise := 1
	bad := ChannelConfigOverrides{Reasoning: &ReasoningOverrides{Depth: &deep}}
	assert.ErrorIs(t, bad.Validate(), ErrInvalidReasoningDepth)

	bad = ChannelConfigOverrides{Reasoning: &ReasoningOverrides{Revise: &revise}}
	assert.ErrorIs(t, bad.Validate(), ErrInvalidReasoningRevise)

	junk := "ponder"
	bad = ChannelConfigOverrides{Reasoning: &ReasoningOverrides{Mode: &junk}}
	assert.ErrorIs(t, bad.Validate(), ErrInvalidReasoningMode)
}

// --- apply path -----------------------------------------------------------

// TestApplyChannelConfig_ReasoningModeRoundTrips: a `mode: bid` then `mode: plan`
// override persists, bumps the revision, and is stamped onto the router — while a
// channel with no override resolves to the default `off`.
func TestApplyChannelConfig_ReasoningModeRoundTrips(t *testing.T) {
	router, store, ctx := newApplyRouter(t)
	id := mustCreateGovernedGroup(t, store, "planning")

	// Default (no override): off.
	assert.Equal(t, ReasoningModeOff, router.ReasoningFor(id).Mode)

	for i, mode := range []string{ReasoningModeBid, ReasoningModePlan} {
		m := mode
		patch := ChannelConfigOverrides{Reasoning: &ReasoningOverrides{Mode: &m}}
		require.NoError(t, router.ApplyChannelConfig(ctx, id, patch, int64(i), ""))

		got, revision, err := store.GetChannelConfig(ctx, id)
		require.NoError(t, err)
		assert.Equal(t, int64(i+1), revision, "apply bumps revision")
		require.NotNil(t, got.Reasoning)
		require.NotNil(t, got.Reasoning.Mode)
		assert.Equal(t, mode, *got.Reasoning.Mode)

		// Reflected live, with the unset sub-fields resolving to the default.
		rc := router.ReasoningFor(id)
		assert.Equal(t, mode, rc.Mode)
		assert.Equal(t, ReasoningModelFast, rc.Model)
		assert.Equal(t, ReasoningDepthShallow, rc.Depth)
	}
}

// TestApplyChannelConfig_ReasoningRejectsUngoverned: a `mode != off` override on a
// channel with no salience-gated member is rejected before any write (the
// cross-field governance rule, mirroring the loader).
func TestApplyChannelConfig_ReasoningRejectsUngoverned(t *testing.T) {
	router, store, ctx := newApplyRouter(t)
	id := mustCreateGroup(t, store, "planning", "ada") // when_mentioned, not gated

	m := ReasoningModeBid
	patch := ChannelConfigOverrides{Reasoning: &ReasoningOverrides{Mode: &m}}
	err := router.ApplyChannelConfig(ctx, id, patch, 0, "")
	require.Error(t, err)
	assert.ErrorIs(t, err, ErrInvalidReasoningMode)

	// No write happened — still at revision 0.
	_, revision, gerr := store.GetChannelConfig(ctx, id)
	require.NoError(t, gerr)
	assert.Equal(t, int64(0), revision)
}

// TestApplyChannelConfig_ReasoningAbsentResolvesToDefault: an apply that omits the
// reasoning block resolves it to the default `off` on the router (store-canonical
// shadow-the-whole-block semantics, like every other knob).
func TestApplyChannelConfig_ReasoningAbsentResolvesToDefault(t *testing.T) {
	router, store, ctx := newApplyRouter(t)
	id := mustCreateGovernedGroup(t, store, "planning")
	router.SetReasoning(id, ReasoningConfig{Mode: ReasoningModePlan})

	fc := true
	require.NoError(t, router.ApplyChannelConfig(ctx, id,
		ChannelConfigOverrides{FloorControl: &fc}, 0, ""))

	assert.Equal(t, ReasoningModeOff, router.ReasoningFor(id).Mode,
		"an absent reasoning override falls back to the package default (off)")
}
