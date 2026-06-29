package channels

// RFC 0052 PR 1 (v0.3.11) — the reconcile/freeze half of the autonomous block,
// the sibling of config_reconcile_reasoning_test.go. These pin the deep-review
// finding that [ChannelConfig.toConfigOverrides] MUST snapshot the autonomous rung:
// without it a revisioned, armed YAML channel is silently DISARMED by the
// reconcile→[ChannelRouter.ResolveFromStore] boot round-trip and the drift hash
// goes blind to an autonomous edit. Mirrors the per-sub-knob freeze: only the
// committed sub-knobs are explicit; a disabled default rung stays inherit.

import (
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// armedAutonomousYAML is a revisioned, capped, armed channel — the config-as-code
// shape the reconcile path adopts. `topic` is a parameter so the drift test can
// vary one sub-knob while holding the rest (and the cap) constant.
func armedAutonomousYAML(topic string) string {
	return `
channels:
  - name: roundtable
    revision: 1
    interaction_budget_tokens: 200000
    autonomous:
      enabled: true
      topic: "` + topic + `"
      convener: nova
    members:
      - id: nova
        respond: participant
      - id: ada
        respond: participant
`
}

// TestToConfigOverrides_CapturesArmedAutonomous is the finding's snapshot half: a
// YAML-declared ARMED rung MUST be captured in the override set the reconcile
// persists, or the boot round-trip clobbers it back to the disabled default. Only
// the committed sub-knobs are explicit; max_rounds at the default stays inherit.
func TestToConfigOverrides_CapturesArmedAutonomous(t *testing.T) {
	cfg, err := LoadConfig(writeYAML(t, armedAutonomousYAML("monorepo?")))
	require.NoError(t, err)

	o := cfg.Channels[0].toConfigOverrides(cfg)
	require.NotNil(t, o.Autonomous, "an armed YAML rung must be captured in the snapshot")
	require.NotNil(t, o.Autonomous.Enabled)
	assert.True(t, *o.Autonomous.Enabled)
	require.NotNil(t, o.Autonomous.Convener)
	assert.Equal(t, "nova", *o.Autonomous.Convener)
	// A default max_rounds stays inherit (per-sub-knob freeze), not frozen explicit.
	assert.Nil(t, o.Autonomous.MaxRounds, "a default max_rounds stays inherit, not frozen")
	// The mandatory cap that the armed block depends on is in the same snapshot.
	require.NotNil(t, o.InteractionBudgetTokens)
	assert.Equal(t, int64(200000), *o.InteractionBudgetTokens)
}

// TestToConfigOverrides_DisabledAutonomousStaysInherit: a disabled (default) block
// equals never-set, so it stays inherit (nil) in the snapshot — an ordinary channel
// does not hash differently from one that explicitly left autonomy off.
func TestToConfigOverrides_DisabledAutonomousStaysInherit(t *testing.T) {
	body := `
channels:
  - name: planning
    revision: 1
    members:
      - id: ada
        respond: participant
`
	cfg, err := LoadConfig(writeYAML(t, body))
	require.NoError(t, err)
	o := cfg.Channels[0].toConfigOverrides(cfg)
	assert.Nil(t, o.Autonomous, "a disabled autonomous rung stays inherit in the snapshot")
}

// TestReconcileRoundTrip_ArmedAutonomousSurvivesBoot is the finding's end-to-end: a
// revisioned YAML channel armed for autonomy must STILL resolve armed after the full
// boot sequence (ResolveAutonomous → ReconcileFromYAML → ResolveFromStore), not be
// reset to the disabled default by the store-overlay step. This is the regression
// that the missing snapshot line silently broke.
func TestReconcileRoundTrip_ArmedAutonomousSurvivesBoot(t *testing.T) {
	router, store, _, ctx := newReconcileRouter(t)
	require.NoError(t, store.CreateChannelWithMembers(ctx,
		Channel{ID: "group:roundtable", Name: "roundtable", Type: ChannelTypeGroup},
		[]Member{
			{ParticipantID: "nova", RespondPolicy: RespondAlways},
			{ParticipantID: "ada", RespondPolicy: RespondAlways},
		}))

	cfg, err := LoadConfig(writeYAML(t, armedAutonomousYAML("monorepo?")))
	require.NoError(t, err)

	require.NoError(t, router.ResolveAutonomous(ctx, cfg))
	require.NoError(t, router.ReconcileFromYAML(ctx, cfg))
	require.NoError(t, router.ResolveFromStore(ctx))

	got := router.AutonomousFor("group:roundtable")
	assert.True(t, got.Enabled,
		"a YAML-armed channel must survive the reconcile→overlay boot round-trip, not be disarmed")
	assert.Equal(t, "nova", got.Convener, "the convener survives the round-trip too")
}

// TestChannelConfigContentHash_DistinguishesAutonomous is the finding's drift half:
// two snapshots differing only in an autonomous sub-knob (topic) MUST hash
// differently, so a hand-edit to the autonomous block is caught as drift rather than
// silently ignored at equal revision.
func TestChannelConfigContentHash_DistinguishesAutonomous(t *testing.T) {
	hash := func(topic string) string {
		cfg, err := LoadConfig(writeYAML(t, armedAutonomousYAML(topic)))
		require.NoError(t, err)
		h, err := channelConfigContentHash(cfg.Channels[0].toConfigOverrides(cfg))
		require.NoError(t, err)
		return h
	}
	assert.NotEqual(t, hash("monorepo?"), hash("polyrepo?"),
		"drift detection must see an autonomous.topic change")
}
