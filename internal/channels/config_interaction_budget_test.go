package channels

import (
	"errors"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// RFC 0030 Layer 1 (v0.3.8) — the per-channel `interaction_budget_tokens`
// cost-ceiling knob and the top-level `default_interaction_budget_tokens`
// fleet default. Both are opt-in: absent / zero means uncapped, so existing
// channels are unaffected. Negative is a loud loader error (belt-and-
// suspenders behind the schema's `minimum: 0`).

func TestLoadConfig_InteractionBudget_AbsentIsUncapped(t *testing.T) {
	body := `
channels:
  - name: design
    members: [iron-fox, ada]
`
	cfg, err := LoadConfig(writeYAML(t, body))
	require.NoError(t, err)
	assert.Equal(t, int64(0), cfg.DefaultInteractionBudgetTokens,
		"absent default_interaction_budget_tokens is 0 (uncapped)")
	assert.Equal(t, int64(0), cfg.Channels[0].InteractionBudgetTokens,
		"absent per-channel interaction_budget_tokens is 0 (uncapped)")
}

func TestLoadConfig_InteractionBudget_ParsesChannelAndDefault(t *testing.T) {
	body := `
default_interaction_budget_tokens: 50000
channels:
  - name: design
    interaction_budget_tokens: 8000
    members: [iron-fox, ada]
`
	cfg, err := LoadConfig(writeYAML(t, body))
	require.NoError(t, err)
	assert.Equal(t, int64(50000), cfg.DefaultInteractionBudgetTokens)
	assert.Equal(t, int64(8000), cfg.Channels[0].InteractionBudgetTokens)
}

// TestChannelConfig_ResolveInteractionBudgetTokens pins the channel-over-
// default precedence: an explicit per-channel value wins; a channel that
// omits the knob (value 0) inherits the fleet default; both zero stays
// uncapped.
func TestChannelConfig_ResolveInteractionBudgetTokens(t *testing.T) {
	cases := []struct {
		name    string
		channel int64
		fleet   int64
		want    int64
	}{
		{"channel wins over default", 8000, 50000, 8000},
		{"channel omitted inherits default", 0, 50000, 50000},
		{"both omitted stays uncapped", 0, 0, 0},
		{"channel set, no fleet default", 8000, 0, 8000},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			ch := ChannelConfig{InteractionBudgetTokens: tc.channel}
			assert.Equal(t, tc.want, ch.ResolveInteractionBudgetTokens(tc.fleet))
		})
	}
}

func TestLoadConfig_InteractionBudget_RejectsNegativeChannel(t *testing.T) {
	body := `
channels:
  - name: design
    interaction_budget_tokens: -1
    members: [iron-fox, ada]
`
	_, err := LoadConfig(writeYAML(t, body))
	require.Error(t, err)
	assert.True(t, errors.Is(err, ErrInvalidInteractionBudgetTokens),
		"a negative per-channel interaction_budget_tokens must be rejected")
}

func TestLoadConfig_InteractionBudget_RejectsNegativeDefault(t *testing.T) {
	body := `
default_interaction_budget_tokens: -5
channels:
  - name: design
    members: [iron-fox, ada]
`
	_, err := LoadConfig(writeYAML(t, body))
	require.Error(t, err)
	assert.True(t, errors.Is(err, ErrInvalidInteractionBudgetTokens),
		"a negative default_interaction_budget_tokens must be rejected")
}
