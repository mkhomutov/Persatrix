package channels

// RFC 0051 PR 6 (v0.3.10) — the reconcile/freeze half of the go-live default
// flip. Split out of config_reconcile_test.go (at the 500-line review cap) so the
// reasoning-specific snapshot / boot-round-trip / drift-hash regressions live with
// their own siblings. These pin that the governance-aware FreezeOverrides captures
// the right sub-knobs: an above-default rung (`plan`) and an explicit `off` kill
// switch are frozen and survive boot, while the governed default (`bid`) stays
// inherit.

import (
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// TestToConfigOverrides_CapturesAboveDefaultReasoning is the F1 regression: a
// YAML-declared reasoning rung ABOVE the governed default (`plan`) MUST be
// captured in the snapshot the reconcile persists, or the boot round-trip
// (adopt → ResolveFromStore) silently clobbers it back to the governed default
// (bid) and the drift hash goes blind to it. Mirrors the per-sub-knob freeze:
// only the committed sub-knob(s) are explicit; the rest stay inherit. Uses `plan`
// because post the PR 6 flip `bid` IS the governed default and so stays inherit
// (see TestToConfigOverrides_GovernedBidStaysInherit).
func TestToConfigOverrides_CapturesAboveDefaultReasoning(t *testing.T) {
	body := `
channels:
  - name: planning
    revision: 1
    reasoning:
      mode: plan
    members:
      - id: ada
        respond: participant
`
	cfg, err := LoadConfig(writeYAML(t, body))
	require.NoError(t, err)

	o := cfg.Channels[0].toConfigOverrides(cfg)
	require.NotNil(t, o.Reasoning, "an above-default YAML rung must be captured in the snapshot")
	require.NotNil(t, o.Reasoning.Mode)
	assert.Equal(t, ReasoningModePlan, *o.Reasoning.Mode)
	// Default sub-knobs stay inherit (per-sub-knob freeze), not frozen to explicit.
	assert.Nil(t, o.Reasoning.Model, "a default model stays inherit, not frozen")
	assert.Nil(t, o.Reasoning.Depth)
	assert.Nil(t, o.Reasoning.Revise)
}

// TestToConfigOverrides_GovernedBidStaysInherit: post the PR 6 flip, `mode: bid`
// on a governed channel IS the governed default, so it stays inherit (nil) in the
// snapshot — the channel resolves to bid at boot either way, and the drift hash
// treats an explicit `bid` and an absent block identically (both → bid).
func TestToConfigOverrides_GovernedBidStaysInherit(t *testing.T) {
	body := `
channels:
  - name: planning
    revision: 1
    reasoning:
      mode: bid
    members:
      - id: ada
        respond: participant
`
	cfg, err := LoadConfig(writeYAML(t, body))
	require.NoError(t, err)
	o := cfg.Channels[0].toConfigOverrides(cfg)
	assert.Nil(t, o.Reasoning, "bid equals the governed default and stays inherit in the snapshot")
}

// TestToConfigOverrides_CapturesGovernedKillSwitchOff is the PR 6 kill-switch
// regression: an explicit `mode: off` on a GOVERNED channel differs from the
// governed default (bid), so FreezeOverrides MUST capture it — otherwise the boot
// round-trip re-resolves the channel to bid and the operator's kill switch is lost.
func TestToConfigOverrides_CapturesGovernedKillSwitchOff(t *testing.T) {
	body := `
channels:
  - name: planning
    revision: 1
    reasoning:
      mode: "off"
    members:
      - id: ada
        respond: participant
`
	cfg, err := LoadConfig(writeYAML(t, body))
	require.NoError(t, err)
	o := cfg.Channels[0].toConfigOverrides(cfg)
	require.NotNil(t, o.Reasoning, "an explicit off kill switch on a governed channel must be captured")
	require.NotNil(t, o.Reasoning.Mode)
	assert.Equal(t, ReasoningModeOff, *o.Reasoning.Mode)
}

// TestReconcileRoundTrip_ReasoningSurvivesBoot is the F1 end-to-end: a revisioned
// YAML channel declaring an above-default rung (mode=plan) must still resolve to
// plan after the full boot sequence (ResolveReasoning → ReconcileFromYAML →
// ResolveFromStore), not be reset to the governed default (bid) by the
// store-overlay step. Uses `plan` rather than `bid` so the freeze is load-bearing
// — bid would survive even with a broken freeze because it IS the governed default.
func TestReconcileRoundTrip_ReasoningSurvivesBoot(t *testing.T) {
	router, store, _, ctx := newReconcileRouter(t)
	require.NoError(t, store.CreateChannelWithMembers(ctx,
		Channel{ID: "group:planning", Name: "planning", Type: ChannelTypeGroup},
		[]Member{{ParticipantID: "ada", RespondPolicy: RespondAlways, SalienceGated: true}}))

	body := `
channels:
  - name: planning
    revision: 1
    reasoning:
      mode: plan
    members:
      - id: ada
        respond: participant
`
	cfg, err := LoadConfig(writeYAML(t, body))
	require.NoError(t, err)

	require.NoError(t, router.ResolveReasoning(ctx, cfg))
	require.NoError(t, router.ReconcileFromYAML(ctx, cfg))
	require.NoError(t, router.ResolveFromStore(ctx))

	assert.Equal(t, ReasoningModePlan, router.ReasoningFor("group:planning").Mode,
		"a YAML-declared plan rung must survive the reconcile→overlay boot round-trip")
}

// TestReconcileRoundTrip_GovernedKillSwitchOffSurvivesBoot is the PR 6 kill-switch
// end-to-end: a revisioned YAML channel declaring an explicit `mode: off` on a
// governed channel must still resolve to off after the full boot sequence — the
// go-live default flip must not re-flip an explicit kill switch back to bid.
func TestReconcileRoundTrip_GovernedKillSwitchOffSurvivesBoot(t *testing.T) {
	router, store, _, ctx := newReconcileRouter(t)
	require.NoError(t, store.CreateChannelWithMembers(ctx,
		Channel{ID: "group:planning", Name: "planning", Type: ChannelTypeGroup},
		[]Member{{ParticipantID: "ada", RespondPolicy: RespondAlways, SalienceGated: true}}))

	body := `
channels:
  - name: planning
    revision: 1
    reasoning:
      mode: "off"
    members:
      - id: ada
        respond: participant
`
	cfg, err := LoadConfig(writeYAML(t, body))
	require.NoError(t, err)

	require.NoError(t, router.ResolveReasoning(ctx, cfg))
	require.NoError(t, router.ReconcileFromYAML(ctx, cfg))
	require.NoError(t, router.ResolveFromStore(ctx))

	assert.Equal(t, ReasoningModeOff, router.ReasoningFor("group:planning").Mode,
		"an explicit off kill switch survives the boot round-trip, not re-flipped to bid")
}

// TestChannelConfigContentHash_DistinguishesReasoning is the F1 drift half: two
// otherwise-identical snapshots differing only in reasoning.mode MUST hash
// differently, so a hand-edit to the reasoning block is caught as drift. `off`
// (the captured kill switch) and `bid` (inherit) hash differently post the flip.
func TestChannelConfigContentHash_DistinguishesReasoning(t *testing.T) {
	load := func(mode string) ChannelConfigOverrides {
		body := `
channels:
  - name: planning
    revision: 1
    reasoning:
      mode: ` + mode + `
    members:
      - id: ada
        respond: participant
`
		cfg, err := LoadConfig(writeYAML(t, body))
		require.NoError(t, err)
		return cfg.Channels[0].toConfigOverrides(cfg)
	}
	hOff, err := channelConfigContentHash(load(`"off"`))
	require.NoError(t, err)
	hBid, err := channelConfigContentHash(load("bid"))
	require.NoError(t, err)
	assert.NotEqual(t, hOff, hBid, "drift detection must see a reasoning.mode change")
}
