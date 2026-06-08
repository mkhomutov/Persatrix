package channels

import (
	"errors"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// RFC 0030 Layer 2 (v0.3.8) — the per-channel
// `max_replies_per_participant_per_interaction` reply-budget knob, the top-level
// `default_max_replies_per_participant` fleet default, and the
// `governance.exempt_principals` list. All opt-in: absent / zero means
// uncapped, so existing channels are unaffected. Negative is a loud loader
// error (belt-and-suspenders behind the schema's `minimum: 0`).

func TestLoadConfig_ReplyBudget_AbsentIsUncapped(t *testing.T) {
	body := `
channels:
  - name: design
    members: [iron-fox, ada]
`
	cfg, err := LoadConfig(writeYAML(t, body))
	require.NoError(t, err)
	assert.Equal(t, 0, cfg.DefaultMaxRepliesPerParticipant,
		"absent default_max_replies_per_participant is 0 (uncapped)")
	assert.Equal(t, 0, cfg.Channels[0].MaxRepliesPerParticipantPerInteraction,
		"absent per-channel max_replies_per_participant_per_interaction is 0 (uncapped)")
	assert.Empty(t, cfg.Governance.ExemptPrincipals, "absent governance block is empty")
}

func TestLoadConfig_ReplyBudget_ParsesChannelDefaultAndGovernance(t *testing.T) {
	body := `
default_max_replies_per_participant: 5
governance:
  exempt_principals: [human]
channels:
  - name: design
    max_replies_per_participant_per_interaction: 2
    members: [iron-fox, ada]
`
	cfg, err := LoadConfig(writeYAML(t, body))
	require.NoError(t, err)
	assert.Equal(t, 5, cfg.DefaultMaxRepliesPerParticipant)
	assert.Equal(t, 2, cfg.Channels[0].MaxRepliesPerParticipantPerInteraction)
	assert.Equal(t, []string{"human"}, cfg.Governance.ExemptPrincipals)
}

// TestChannelConfig_ResolveMaxRepliesPerParticipant pins the channel-over-
// default precedence: an explicit per-channel value wins; a channel that omits
// the knob (value 0) inherits the fleet default; both zero stays uncapped.
func TestChannelConfig_ResolveMaxRepliesPerParticipant(t *testing.T) {
	cases := []struct {
		name    string
		channel int
		fleet   int
		want    int
	}{
		{"channel wins over default", 2, 5, 2},
		{"channel omitted inherits default", 0, 5, 5},
		{"both omitted stays uncapped", 0, 0, 0},
		{"channel set, no fleet default", 2, 0, 2},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			ch := ChannelConfig{MaxRepliesPerParticipantPerInteraction: tc.channel}
			assert.Equal(t, tc.want, ch.ResolveMaxRepliesPerParticipant(tc.fleet))
		})
	}
}

func TestLoadConfig_ReplyBudget_RejectsNegativeChannel(t *testing.T) {
	body := `
channels:
  - name: design
    max_replies_per_participant_per_interaction: -1
    members: [iron-fox, ada]
`
	_, err := LoadConfig(writeYAML(t, body))
	require.Error(t, err)
	assert.True(t, errors.Is(err, ErrInvalidMaxRepliesPerParticipant),
		"a negative per-channel max_replies_per_participant_per_interaction must be rejected")
}

func TestLoadConfig_ReplyBudget_RejectsNegativeDefault(t *testing.T) {
	body := `
default_max_replies_per_participant: -5
channels:
  - name: design
    members: [iron-fox, ada]
`
	_, err := LoadConfig(writeYAML(t, body))
	require.Error(t, err)
	assert.True(t, errors.Is(err, ErrInvalidMaxRepliesPerParticipant),
		"a negative default_max_replies_per_participant must be rejected")
}

// TestExemptPrincipalParticipantType pins the principal→participant-type
// mapping: only `human` maps (to the wire `user` type); anything else has no
// mapping and silently fails open to "not exempt".
func TestExemptPrincipalParticipantType(t *testing.T) {
	pt, ok := exemptPrincipalParticipantType("human")
	assert.True(t, ok)
	assert.Equal(t, "user", pt)

	_, ok = exemptPrincipalParticipantType("agent")
	assert.False(t, ok, "agent is never an exempt principal")
	_, ok = exemptPrincipalParticipantType("robot")
	assert.False(t, ok, "an unrecognised principal has no mapping")
}
