package channels

import (
	"strconv"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// RFC 0030 Tier B PR 2b (v0.3.8): the config-load derivation of the per-member
// `TierBActive` signal (only the open-floor participant vocabulary opts into
// the salience bid) and the channel-level `tier_b_max_channel_members` cap.

// TestLoadConfig_ParticipantIsTierBActive pins that a `participant` member is
// marked salience-gated at load with no explicit threshold (nil → unset →
// bias-to-silence).
func TestLoadConfig_ParticipantIsTierBActive(t *testing.T) {
	body := `
channels:
  - name: planning
    members:
      - id: alice
        respond: participant
`
	cfg, err := LoadConfig(writeYAML(t, body))
	require.NoError(t, err)
	m := cfg.Channels[0].Members[0]
	assert.True(t, m.TierBActive, "participant opts into the Tier B bid")
	assert.Nil(t, m.Threshold, "a plain participant carries no explicit threshold")
}

// TestLoadConfig_ChairIsTierBActive pins that a `chair` is salience-gated and
// carries the low default threshold.
func TestLoadConfig_ChairIsTierBActive(t *testing.T) {
	body := `
channels:
  - name: planning
    members:
      - id: facilitator
        respond: chair
`
	cfg, err := LoadConfig(writeYAML(t, body))
	require.NoError(t, err)
	m := cfg.Channels[0].Members[0]
	assert.True(t, m.TierBActive, "chair opts into the Tier B bid")
	require.NotNil(t, m.Threshold)
	assert.InDelta(t, DefaultChairThreshold, *m.Threshold, 1e-9)
}

// TestLoadConfig_BareLegacyAlwaysNotTierBActive pins the back-compat rule: a
// legacy `always` member with no threshold keeps replying unconditionally — it
// is NOT salience-gated, so a v0.3.7 channel behaves identically under v0.3.8.
func TestLoadConfig_BareLegacyAlwaysNotTierBActive(t *testing.T) {
	body := `
channels:
  - name: planning
    members:
      - id: alice
        respond: always
`
	cfg, err := LoadConfig(writeYAML(t, body))
	require.NoError(t, err)
	m := cfg.Channels[0].Members[0]
	assert.False(t, m.TierBActive, "a bare legacy always member is not salience-gated")
	assert.Nil(t, m.Threshold)
}

// TestLoadConfig_LegacyAlwaysWithThresholdIsTierBActive pins that setting an
// explicit threshold on a legacy `always` member is itself an opt-in: the only
// reason to put a salience bar on a member is to gate it.
func TestLoadConfig_LegacyAlwaysWithThresholdIsTierBActive(t *testing.T) {
	body := `
channels:
  - name: planning
    members:
      - id: alice
        respond: always
        threshold: 0.5
`
	cfg, err := LoadConfig(writeYAML(t, body))
	require.NoError(t, err)
	m := cfg.Channels[0].Members[0]
	assert.True(t, m.TierBActive, "an explicit threshold on `always` opts the member into the bid")
	require.NotNil(t, m.Threshold)
	assert.InDelta(t, 0.5, *m.Threshold, 1e-9)
}

// TestLoadConfig_NonOpenFloorNotTierBActive pins that the non-open-floor
// dispositions are never salience-gated (the bid never reaches them).
func TestLoadConfig_NonOpenFloorNotTierBActive(t *testing.T) {
	for _, disp := range []string{"addressed", "observer", "when_mentioned", "never"} {
		t.Run(disp, func(t *testing.T) {
			body := `
channels:
  - name: planning
    members:
      - id: alice
        respond: ` + disp + `
`
			cfg, err := LoadConfig(writeYAML(t, body))
			require.NoError(t, err)
			assert.False(t, cfg.Channels[0].Members[0].TierBActive,
				"%s is not an open-floor disposition", disp)
		})
	}
}

// TestLoadConfig_DefaultTierBMaxChannelMembers pins that an absent
// `tier_b_max_channel_members` normalizes to the default at load — so the
// channel-size cap is always populated for the dispatcher to carry on the wire.
func TestLoadConfig_DefaultTierBMaxChannelMembers(t *testing.T) {
	body := `
channels:
  - name: planning
    members:
      - id: alice
        respond: participant
`
	cfg, err := LoadConfig(writeYAML(t, body))
	require.NoError(t, err)
	assert.Equal(t, DefaultTierBMaxChannelMembers, cfg.Channels[0].TierBMaxChannelMembers,
		"absent tier_b_max_channel_members normalizes to the default")
}

// TestLoadConfig_AcceptsTierBMaxChannelMembers pins that an explicit cap loads.
func TestLoadConfig_AcceptsTierBMaxChannelMembers(t *testing.T) {
	body := `
channels:
  - name: planning
    tier_b_max_channel_members: 4
    members:
      - id: alice
        respond: participant
`
	cfg, err := LoadConfig(writeYAML(t, body))
	require.NoError(t, err)
	assert.Equal(t, 4, cfg.Channels[0].TierBMaxChannelMembers)
}

// TestLoadConfig_RejectsNegativeTierBMaxChannelMembers pins the loud-failure
// belt-and-suspenders for an operator who skipped `make validate`.
func TestLoadConfig_RejectsNegativeTierBMaxChannelMembers(t *testing.T) {
	body := `
channels:
  - name: planning
    tier_b_max_channel_members: ` + strconv.Itoa(-1) + `
    members:
      - id: alice
        respond: participant
`
	_, err := LoadConfig(writeYAML(t, body))
	require.Error(t, err)
	assert.ErrorIs(t, err, ErrInvalidTierBMaxChannelMembers)
}
