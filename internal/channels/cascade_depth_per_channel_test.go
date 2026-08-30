package channels

import (
	"context"
	"testing"

	"github.com/google/uuid"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.uber.org/zap"
	"go.uber.org/zap/zapcore"
	"go.uber.org/zap/zaptest/observer"

	"github.com/mkhomutov/persatrix/internal/defaults"
)

// cascade_depth_per_channel_test.go — ISSUE-0114 (v0.3.13): the per-channel
// `max_cascade_depth` override. Split from router_cascade_depth_test.go (the
// fleet-cap suite) so that file stays under the 500-line review cap; the
// fleet-cap contracts pinned there are unchanged by the override and stay
// where they are.

// TestLoadConfig_PerChannelMaxCascadeDepth pins the load + precedence
// contract: a declared per-channel `max_cascade_depth` loads verbatim and
// wins over the fleet value in [ChannelConfig.ResolveMaxCascadeDepth]; an
// undeclared channel resolves the fleet value.
func TestLoadConfig_PerChannelMaxCascadeDepth(t *testing.T) {
	body := `
max_cascade_depth: 5
channels:
  - name: deliberative
    max_cascade_depth: 3
    members:
      - {id: a, respond: participant}
  - name: ordinary
    members:
      - {id: b, respond: participant}
`
	cfg, err := LoadConfig(writeYAML(t, body))
	require.NoError(t, err)
	require.Len(t, cfg.Channels, 2)

	fleet := cfg.ResolvedMaxCascadeDepth()
	assert.Equal(t, 5, fleet)
	assert.Equal(t, 3, cfg.Channels[0].ResolveMaxCascadeDepth(fleet),
		"a declared per-channel cap wins over the fleet value")
	assert.Equal(t, 5, cfg.Channels[1].ResolveMaxCascadeDepth(fleet),
		"an undeclared (zero) per-channel cap inherits the fleet value")
}

// TestConfig_ResolvedMaxCascadeDepth pins the fleet-side resolution the
// per-channel validation compares against: an unset (zero) top-level knob
// resolves to [defaults.DefaultMaxCascadeDepth], mirroring the router's
// zero-sentinel treatment in SetMaxCascadeDepth.
func TestConfig_ResolvedMaxCascadeDepth(t *testing.T) {
	assert.Equal(t, defaults.DefaultMaxCascadeDepth, (&Config{}).ResolvedMaxCascadeDepth())
	assert.Equal(t, 8, (&Config{MaxCascadeDepth: 8}).ResolvedMaxCascadeDepth())
}

// TestLoadConfig_PerChannelMaxCascadeDepth_RejectsNegative mirrors the fleet
// knob's PR #319 guard on the per-channel field: a negative value is a loader
// error, never a silent fall-through to the inherit sentinel.
func TestLoadConfig_PerChannelMaxCascadeDepth_RejectsNegative(t *testing.T) {
	body := `
channels:
  - name: planning
    max_cascade_depth: -1
    members:
      - {id: a, respond: participant}
`
	_, err := LoadConfig(writeYAML(t, body))
	require.Error(t, err)
	assert.ErrorIs(t, err, ErrInvalidMaxCascadeDepth)
}

// TestLoadConfig_PerChannelMaxCascadeDepth_RejectsAboveFleet pins the
// ISSUE-0114 option (c) alignment rule at the config-as-code boundary: a
// per-channel cap above the RESOLVED fleet cap is rejected, whether the fleet
// value is explicit or the default — the Python dispatcher backstop is a
// per-process global aligned with the fleet value, so the extra depth would
// be silently unreachable. At-fleet is legal (it is the no-op override).
func TestLoadConfig_PerChannelMaxCascadeDepth_RejectsAboveFleet(t *testing.T) {
	overDefault := `
channels:
  - name: planning
    max_cascade_depth: 6
    members:
      - {id: a, respond: participant}
`
	_, err := LoadConfig(writeYAML(t, overDefault))
	require.Error(t, err, "6 > the defaulted fleet cap 5 must be rejected")
	assert.ErrorIs(t, err, ErrInvalidMaxCascadeDepth)

	overExplicit := `
max_cascade_depth: 8
channels:
  - name: planning
    max_cascade_depth: 9
    members:
      - {id: a, respond: participant}
`
	_, err = LoadConfig(writeYAML(t, overExplicit))
	require.Error(t, err, "9 > the explicit fleet cap 8 must be rejected")
	assert.ErrorIs(t, err, ErrInvalidMaxCascadeDepth)

	atFleet := `
max_cascade_depth: 8
channels:
  - name: planning
    max_cascade_depth: 8
    members:
      - {id: a, respond: participant}
`
	_, err = LoadConfig(writeYAML(t, atFleet))
	assert.NoError(t, err, "a per-channel cap equal to the fleet cap is legal")
}

// TestChannelRouter_SetChannelMaxCascadeDepth pins the setter/getter
// contract: a positive value resolves for that channel only, a non-positive
// value is the inherit sentinel that DELETES the entry (the channel reads the
// fleet cap again — deliberately unlike SetMaxCascadeDepth, whose
// non-positive is ignored), and MaxCascadeDepthFor reports the explicit-set
// flag the RFC 0050 first-edit baseline keys on.
func TestChannelRouter_SetChannelMaxCascadeDepth(t *testing.T) {
	router, _, _ := newRouterTest(t)

	depth, set := router.MaxCascadeDepthFor("group:planning")
	assert.Equal(t, defaults.DefaultMaxCascadeDepth, depth)
	assert.False(t, set, "no override resolved yet — fleet cap applies")

	router.SetChannelMaxCascadeDepth("group:planning", 3)
	depth, set = router.MaxCascadeDepthFor("group:planning")
	assert.Equal(t, 3, depth)
	assert.True(t, set)
	other, otherSet := router.MaxCascadeDepthFor("group:other")
	assert.Equal(t, defaults.DefaultMaxCascadeDepth, other,
		"the override is per-channel — a sibling channel keeps the fleet cap")
	assert.False(t, otherSet)

	router.SetChannelMaxCascadeDepth("group:planning", 0)
	depth, set = router.MaxCascadeDepthFor("group:planning")
	assert.Equal(t, defaults.DefaultMaxCascadeDepth, depth,
		"the zero sentinel deletes the entry — back to inheriting the fleet cap")
	assert.False(t, set)
}

// TestChannelRouter_SetChannelMaxCascadeDepth_WarnsAboveFleet pins the
// warn-don't-reject posture on the live path (the SetEndVoteParams k>w
// precedent): an above-fleet per-channel cap applies but emits a loud Warn
// naming the fleet cap and the remedy, because the Python backstop (aligned
// with the fleet value) makes the extra depth unreachable. At-or-below-fleet
// values must stay silent so the warning keeps its signal.
func TestChannelRouter_SetChannelMaxCascadeDepth_WarnsAboveFleet(t *testing.T) {
	core, recorded := observer.New(zapcore.WarnLevel)
	store := newTestStore(t, SQLiteOptions{})
	router := NewChannelRouter(store, &recordingDispatcher{}, zap.New(core), nil)

	router.SetChannelMaxCascadeDepth("group:planning", defaults.DefaultMaxCascadeDepth)
	router.SetChannelMaxCascadeDepth("group:planning", 3)
	assert.Empty(t, recorded.All(), "at-or-below-fleet overrides must not warn")

	router.SetChannelMaxCascadeDepth("group:planning", defaults.DefaultMaxCascadeDepth+2)
	logs := recorded.FilterMessageSnippet("exceeds the fleet cap").All()
	require.Len(t, logs, 1, "an above-fleet override must warn exactly once")
	fields := logs[0].ContextMap()
	assert.Equal(t, "group:planning", fields["channel_id"])
	assert.EqualValues(t, defaults.DefaultMaxCascadeDepth+2, fields["max_cascade_depth"])
	assert.EqualValues(t, defaults.DefaultMaxCascadeDepth, fields["fleet_max_cascade_depth"])

	depth, _ := router.MaxCascadeDepthFor("group:planning")
	assert.Equal(t, defaults.DefaultMaxCascadeDepth+2, depth,
		"the warned value still applies — warn, not reject")
}

// TestChannelRouter_ResolveChannelCascadeCaps pins the startup resolver: only
// channels declaring the knob get an entry (the ResolveEndVotes posture — no
// store enumeration; everyone else falls back at read time).
func TestChannelRouter_ResolveChannelCascadeCaps(t *testing.T) {
	router, _, _ := newRouterTest(t)
	cfg := &Config{MaxCascadeDepth: 5, Channels: []ChannelConfig{
		{Name: "deliberative", MaxCascadeDepth: 3},
		{Name: "ordinary"},
	}}

	require.NoError(t, router.ResolveChannelCascadeCaps(context.Background(), cfg))

	depth, set := router.MaxCascadeDepthFor("group:deliberative")
	assert.Equal(t, 3, depth)
	assert.True(t, set)
	_, set = router.MaxCascadeDepthFor("group:ordinary")
	assert.False(t, set, "a channel without the knob keeps reading the fleet cap")
}

// TestChannelRouter_Publish_PerChannelCascadeCap_Binds pins the hot-path
// contract the knob exists for: on the overridden channel the LOWER
// per-channel cap suppresses fanout at its own bound, while a sibling channel
// with no override still fans the same depth under the fleet cap — one
// channel's discussion length tuned without touching the fleet.
func TestChannelRouter_Publish_PerChannelCascadeCap_Binds(t *testing.T) {
	router, disp, store := newRouterTest(t)
	ctx := context.Background()
	short := mustCreateGroup(t, store, "short", "alice", "bob")
	long := mustCreateGroup(t, store, "long", "alice", "bob")
	router.SetChannelMaxCascadeDepth(short, 3)

	require.NoError(t, router.Publish(ctx, ChannelMessage{
		ID: uuid.NewString(), ChannelID: short, SenderID: "alice", Content: "hi",
		Metadata: map[string]any{cascadeDepthMetadataKey: 2},
	}, ""))
	require.Len(t, disp.snapshot(), 1, "depth 2 < the channel cap 3 must fan out")

	require.NoError(t, router.Publish(ctx, ChannelMessage{
		ID: uuid.NewString(), ChannelID: short, SenderID: "alice", Content: "hi",
		Metadata: map[string]any{cascadeDepthMetadataKey: 3},
	}, ""))
	require.Len(t, disp.snapshot(), 1,
		"depth 3 >= the channel cap 3 must suppress fanout, below the fleet cap 5")

	require.NoError(t, router.Publish(ctx, ChannelMessage{
		ID: uuid.NewString(), ChannelID: long, SenderID: "alice", Content: "hi",
		Metadata: map[string]any{cascadeDepthMetadataKey: 3},
	}, ""))
	require.Len(t, disp.snapshot(), 2,
		"the sibling channel has no override — depth 3 < fleet cap 5 still fans out")
}

// TestChannelRouter_Publish_PerChannelCascadeCap_PersistsClampedValue pins
// the clamp side: an over-cap inbound claim persists at the CHANNEL's cap,
// not the fleet cap — `GET /messages` reports what this channel enforced.
func TestChannelRouter_Publish_PerChannelCascadeCap_PersistsClampedValue(t *testing.T) {
	router, _, store := newRouterTest(t)
	ctx := context.Background()
	id := mustCreateGroup(t, store, "short", "alice", "bob")
	router.SetChannelMaxCascadeDepth(id, 3)

	msgID := uuid.NewString()
	require.NoError(t, router.Publish(ctx, ChannelMessage{
		ID: msgID, ChannelID: id, SenderID: "alice", Content: "hi",
		Metadata: map[string]any{cascadeDepthMetadataKey: 99},
	}, ""))

	stored, err := store.GetMessage(ctx, msgID)
	require.NoError(t, err)
	require.NotNil(t, stored.Metadata)
	assert.EqualValues(t, 3, asInt(stored.Metadata[cascadeDepthMetadataKey]),
		"the stored depth must reflect the per-channel clamp, not the fleet cap")
}

// TestAutonomousContinuation_PerChannelCascadeCapCloses is the ISSUE-0114
// headline: the fleet-cap composition test
// (TestAutonomousContinuation_CascadeCapCloses) re-run with the fleet cap
// UNTOUCHED and only the per-channel override at 3 — the knob must bind
// exactly where the fleet value used to, closing the armed discussion at the
// channel's own bound (the Phase 3 live arc verifies the same shape on a
// live roster).
func TestAutonomousContinuation_PerChannelCascadeCapCloses(t *testing.T) {
	router, disp, ch := continuationHarness(t, true, 10)
	router.SetChannelMaxCascadeDepth(ch, 3)
	require.Equal(t, defaults.DefaultMaxCascadeDepth, router.MaxCascadeDepth(),
		"precondition: the fleet cap stays at the default — only the channel is tuned")

	require.NoError(t, router.Publish(context.Background(), ChannelMessage{
		ID: uuid.NewString(), ChannelID: ch, SenderID: "nova",
		Content:  "Opening: should we adopt a monorepo?",
		Metadata: map[string]any{cascadeDepthMetadataKey: 1},
	}, ""))
	router.WaitForPendingFanout()

	assert.Equal(t, []string{"ember", "iron", "nova", "ember"}, disp.snapshot(),
		"exactly two rounds: the per-channel cap must close where the fleet cap used to")
	_, _, tracked := router.openInteractionEscalationState(ch)
	assert.False(t, tracked,
		"the discussion at the per-channel cap must take the structural close")
}

// TestApplyOverridesToRouter_CascadeDepth pins the RFC 0050 stamp seam: a
// present override resolves for the channel; a later override set WITHOUT the
// knob restores inherit (the apply path re-seeds all nine knobs from the
// stored state, so an unset knob must fall back rather than linger).
func TestApplyOverridesToRouter_CascadeDepth(t *testing.T) {
	router, _, _ := newRouterTest(t)
	three := 3

	router.applyOverridesToRouter("group:planning", ChannelConfigOverrides{MaxCascadeDepth: &three}, false)
	depth, set := router.MaxCascadeDepthFor("group:planning")
	assert.Equal(t, 3, depth)
	assert.True(t, set)

	router.applyOverridesToRouter("group:planning", ChannelConfigOverrides{}, false)
	depth, set = router.MaxCascadeDepthFor("group:planning")
	assert.Equal(t, defaults.DefaultMaxCascadeDepth, depth,
		"an override set without the knob must restore fleet inheritance")
	assert.False(t, set)
}

// TestChannelConfigOverrides_Validate_MaxCascadeDepth pins the per-field
// range on the RFC 0050 patch path: below 1 is rejected (the salience-cap
// posture — the setter treats non-positive as the inherit sentinel, so a
// persisted explicit 0 would be indistinguishable from nil), while nil and
// >= 1 pass. The option (c) above-fleet rule is deliberately NOT here (warn
// at the setter instead — see that test).
func TestChannelConfigOverrides_Validate_MaxCascadeDepth(t *testing.T) {
	zero, minusOne, one := 0, -1, 1

	err := ChannelConfigOverrides{MaxCascadeDepth: &zero}.Validate()
	require.Error(t, err, "explicit 0 must be rejected — it would persist as a lying no-op")
	assert.ErrorIs(t, err, ErrInvalidMaxCascadeDepth)
	err = ChannelConfigOverrides{MaxCascadeDepth: &minusOne}.Validate()
	require.Error(t, err)
	assert.ErrorIs(t, err, ErrInvalidMaxCascadeDepth)

	assert.NoError(t, ChannelConfigOverrides{MaxCascadeDepth: &one}.Validate())
	assert.NoError(t, ChannelConfigOverrides{}.Validate(), "nil is inherit, never an error")
}

// TestToConfigOverrides_CascadeDepth_Conditional pins the adopt-freeze
// capture: a DECLARED per-channel cap is snapshotted, an undeclared one
// stays nil — both for fleet-cap tracking after adoption and so pre-v0.3.13
// store rows keep hashing identically to their re-resolved YAML (no spurious
// equal-revision drift warning at the first post-upgrade boot).
func TestToConfigOverrides_CascadeDepth_Conditional(t *testing.T) {
	cfg := &Config{}

	declared := ChannelConfig{Name: "deliberative", MaxCascadeDepth: 3}
	o := declared.toConfigOverrides(cfg)
	require.NotNil(t, o.MaxCascadeDepth, "a declared cap must survive the adopt freeze")
	assert.Equal(t, 3, *o.MaxCascadeDepth)

	undeclared := ChannelConfig{Name: "ordinary"}
	assert.Nil(t, undeclared.toConfigOverrides(cfg).MaxCascadeDepth,
		"an undeclared cap must stay nil — inherit-tracking + pre-v0.3.13 hash stability")
}
