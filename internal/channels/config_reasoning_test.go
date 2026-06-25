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

// TestLoadConfig_ReasoningGovernedDefaultsToBid: PR 6 go-live — an absent
// reasoning block on a GOVERNED channel (a salience-gated `participant`) resolves
// to the `bid` default, the rest of the rung filling from the package defaults
// (fast / shallow / 0).
func TestLoadConfig_ReasoningGovernedDefaultsToBid(t *testing.T) {
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
	assert.Equal(t, ReasoningModeBid, rc.Mode, "governed channel flips to the bid default (PR 6)")
	assert.Equal(t, ReasoningModelFast, rc.Model)
	assert.Equal(t, ReasoningDepthShallow, rc.Depth)
	assert.Equal(t, 0, rc.Revise)
}

// TestLoadConfig_ReasoningUngovernedDefaultsToOff: an absent reasoning block on an
// UNgoverned channel (a non-salience-gated `addressed` member) keeps the package
// `off` default — the flip takes effect only at the moment a channel is governed.
func TestLoadConfig_ReasoningUngovernedDefaultsToOff(t *testing.T) {
	body := `
channels:
  - name: planning
    members:
      - id: ada
        respond: addressed
`
	cfg, err := LoadConfig(writeYAML(t, body))
	require.NoError(t, err)
	rc := cfg.Channels[0].Reasoning
	assert.Equal(t, ReasoningModeOff, rc.Mode, "ungoverned channel stays off")
	assert.Equal(t, ReasoningModelFast, rc.Model)
	assert.Equal(t, ReasoningDepthShallow, rc.Depth)
	assert.Equal(t, 0, rc.Revise)
}

// TestLoadConfig_ReasoningExplicitOffKillSwitch: an EXPLICIT `mode: off` on a
// governed channel is the kill switch — it is NOT flipped to bid (only an absent
// mode picks up the governed default).
func TestLoadConfig_ReasoningExplicitOffKillSwitch(t *testing.T) {
	body := `
channels:
  - name: planning
    members:
      - id: ada
        respond: participant
    reasoning:
      mode: "off"
`
	cfg, err := LoadConfig(writeYAML(t, body))
	require.NoError(t, err)
	assert.Equal(t, ReasoningModeOff, cfg.Channels[0].Reasoning.Mode,
		"explicit off is the kill switch, not flipped to bid")
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
// TestLoadConfig_ReasoningReviseRejected: out-of-range revise (negative, or above
// MaxReasoningRevise) is rejected at load even under `mode: plan`.
func TestLoadConfig_ReasoningReviseRejected(t *testing.T) {
	for _, revise := range []int{-1, MaxReasoningRevise + 1} {
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

// TestLoadConfig_ReasoningReviseRequiresPlan: a `revise >= 1` on any rung but
// `plan` is rejected — the reflexion critic re-reads the draft against the plan,
// so a revise without one is inert (RFC 0051 Phase 5). PR 8 lifts the old
// blanket Phase-5 capability gate; this mode gate replaces it.
func TestLoadConfig_ReasoningReviseRequiresPlan(t *testing.T) {
	body := `
channels:
  - name: planning
    members:
      - id: ada
        respond: participant
    reasoning:
      mode: bid
      revise: 1
`
	_, err := LoadConfig(writeYAML(t, body))
	require.Error(t, err)
	assert.ErrorIs(t, err, ErrInvalidReasoningRevise)
}

// TestLoadConfig_ReasoningReviseAcceptedUnderPlan: `mode: plan` + `revise: 1|2`
// now LOADS (the Phase-5 deployment) and the count is preserved.
func TestLoadConfig_ReasoningReviseAcceptedUnderPlan(t *testing.T) {
	for _, revise := range []int{1, MaxReasoningRevise} {
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
		cfg, err := LoadConfig(writeYAML(t, body))
		require.NoError(t, err, "revise=%d", revise)
		assert.Equal(t, revise, cfg.Channels[0].Reasoning.Revise, "revise=%d", revise)
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
	overCap := MaxReasoningRevise + 1
	bad := ChannelConfigOverrides{Reasoning: &ReasoningOverrides{Depth: &deep}}
	assert.ErrorIs(t, bad.Validate(), ErrInvalidReasoningDepth)

	// Per-field: an out-of-range revise is rejected here; the `revise >= 1 ⇒ plan`
	// cross-field rule needs the merged mode and is checked at apply
	// (TestApplyChannelConfig_ReasoningReviseRequiresPlan), not in this per-field
	// path — so a lone in-range `revise: 1` passes Validate() now.
	bad = ChannelConfigOverrides{Reasoning: &ReasoningOverrides{Revise: &overCap}}
	assert.ErrorIs(t, bad.Validate(), ErrInvalidReasoningRevise)
	inRange := 1
	ok := ChannelConfigOverrides{Reasoning: &ReasoningOverrides{Revise: &inRange}}
	assert.NoError(t, ok.Validate(), "in-range revise passes the per-field validate")

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

	// Default (no override) on a governed channel: bid (the PR 6 go-live flip).
	// Seeded through the governance-aware boot resolver (the channel is revision 0,
	// so it is a store-only group channel ResolveReasoning stamps with the governed
	// default rather than ResolveFromStore, which skips never-edited channels).
	require.NoError(t, router.ResolveReasoning(ctx, &Config{}))
	assert.Equal(t, ReasoningModeBid, router.ReasoningFor(id).Mode)

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

// TestApplyChannelConfig_ReasoningReviseRequiresPlan: the Phase-5 cross-field rule
// at apply — `revise >= 1` is rejected unless the merged effective mode is `plan`
// (inherit/bid both reject), and accepted + stamped onto the router under `plan`.
func TestApplyChannelConfig_ReasoningReviseRequiresPlan(t *testing.T) {
	router, store, ctx := newApplyRouter(t)
	id := mustCreateGovernedGroup(t, store, "planning")
	require.NoError(t, router.ResolveReasoning(ctx, &Config{}))

	one := 1
	bid := ReasoningModeBid
	plan := ReasoningModePlan
	two := MaxReasoningRevise

	// revise alone (mode inherits → not plan) → rejected, no write.
	err := router.ApplyChannelConfig(ctx, id,
		ChannelConfigOverrides{Reasoning: &ReasoningOverrides{Revise: &one}}, 0, "")
	require.Error(t, err)
	assert.ErrorIs(t, err, ErrInvalidReasoningRevise)

	// revise with an explicit non-plan mode → rejected.
	err = router.ApplyChannelConfig(ctx, id,
		ChannelConfigOverrides{Reasoning: &ReasoningOverrides{Mode: &bid, Revise: &one}}, 0, "")
	require.Error(t, err)
	assert.ErrorIs(t, err, ErrInvalidReasoningRevise)

	_, revision, err := store.GetChannelConfig(ctx, id)
	require.NoError(t, err)
	assert.Equal(t, int64(0), revision, "rejected applies never write")

	// mode: plan + revise: 2 → accepted, persisted, and stamped onto the router.
	err = router.ApplyChannelConfig(ctx, id,
		ChannelConfigOverrides{Reasoning: &ReasoningOverrides{Mode: &plan, Revise: &two}}, 0, "")
	require.NoError(t, err)
	got, revision, err := store.GetChannelConfig(ctx, id)
	require.NoError(t, err)
	assert.Equal(t, int64(1), revision)
	require.NotNil(t, got.Reasoning)
	require.NotNil(t, got.Reasoning.Revise)
	assert.Equal(t, two, *got.Reasoning.Revise)
	assert.Equal(t, two, router.ReasoningFor(id).Revise, "revise reaches the router")
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

// TestApplyChannelConfig_ReasoningAbsentResolvesToGovernedDefault: an apply that
// omits the reasoning block resolves it to the GOVERNED default on the router —
// `bid` on this governed channel (the PR 6 flip), via the store-canonical
// shadow-the-whole-block semantics every knob uses. (A prior explicit `plan` is
// shadowed away because the patch omits reasoning.)
func TestApplyChannelConfig_ReasoningAbsentResolvesToGovernedDefault(t *testing.T) {
	router, store, ctx := newApplyRouter(t)
	id := mustCreateGovernedGroup(t, store, "planning")
	router.SetReasoning(id, ReasoningConfig{Mode: ReasoningModePlan})

	fc := true
	require.NoError(t, router.ApplyChannelConfig(ctx, id,
		ChannelConfigOverrides{FloorControl: &fc}, 0, ""))

	assert.Equal(t, ReasoningModeBid, router.ReasoningFor(id).Mode,
		"an absent reasoning override falls back to the governed default (bid)")
}

// TestApplyChannelConfig_ReasoningExplicitOffKillSwitchPreserved: an explicit
// `mode: off` override on a governed channel is preserved across the persist →
// boot-replay round-trip — the one-flip kill switch survives the go-live default.
func TestApplyChannelConfig_ReasoningExplicitOffKillSwitchPreserved(t *testing.T) {
	router, store, ctx := newApplyRouter(t)
	id := mustCreateGovernedGroup(t, store, "planning")

	off := ReasoningModeOff
	patch := ChannelConfigOverrides{Reasoning: &ReasoningOverrides{Mode: &off}}
	require.NoError(t, router.ApplyChannelConfig(ctx, id, patch, 0, ""))
	assert.Equal(t, ReasoningModeOff, router.ReasoningFor(id).Mode, "explicit off applied")

	// Simulate a fresh boot: drop the live router state and re-overlay from the
	// store. The explicit off must survive rather than re-resolving to bid.
	router.SetReasoning(id, ReasoningConfig{Mode: ReasoningModeBid}) // poison the map
	require.NoError(t, router.ResolveFromStore(ctx))
	assert.Equal(t, ReasoningModeOff, router.ReasoningFor(id).Mode,
		"the explicit off kill switch survives boot replay (not re-flipped to bid)")
}
