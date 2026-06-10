package channels

import (
	"errors"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.uber.org/zap"
)

// The RFC 0030 interaction-id producer's idle window (IP3) — the per-channel
// `interaction_idle_timeout_seconds` knob and the top-level
// `default_interaction_idle_timeout_seconds` fleet default. Pointer tri-state,
// unlike the sibling integer knobs: an explicit 0 (idle rotation off) is a
// meaningful value distinct from absent (inherit — ultimately 600s, the RFC
// 0020 §B idle default). Negative is a loud loader error (belt-and-suspenders
// behind the schema's `minimum: 0`).

func TestLoadConfig_InteractionIdle_AbsentInheritsDefault(t *testing.T) {
	body := `
channels:
  - name: design
    members: [iron-fox, ada]
`
	cfg, err := LoadConfig(writeYAML(t, body))
	require.NoError(t, err)
	assert.Nil(t, cfg.DefaultInteractionIdleTimeoutSeconds,
		"absent fleet default stays nil (inherit 600 at resolution)")
	assert.Nil(t, cfg.Channels[0].InteractionIdleTimeoutSeconds,
		"absent per-channel knob stays nil (inherit)")
	assert.Equal(t, DefaultInteractionIdleTimeoutSeconds,
		cfg.Channels[0].ResolveInteractionIdleTimeoutSeconds(cfg.DefaultInteractionIdleTimeoutSeconds),
		"double-absent resolves to the 600s default")
}

func TestLoadConfig_InteractionIdle_ParsesTriState(t *testing.T) {
	body := `
default_interaction_idle_timeout_seconds: 900
channels:
  - name: design
    interaction_idle_timeout_seconds: 0
    members: [iron-fox, ada]
  - name: planning
    members: [iron-fox, ada]
`
	cfg, err := LoadConfig(writeYAML(t, body))
	require.NoError(t, err)
	require.NotNil(t, cfg.DefaultInteractionIdleTimeoutSeconds)
	assert.Equal(t, 900, *cfg.DefaultInteractionIdleTimeoutSeconds)
	assert.Equal(t, 0,
		cfg.Channels[0].ResolveInteractionIdleTimeoutSeconds(cfg.DefaultInteractionIdleTimeoutSeconds),
		"an explicit per-channel 0 (rotation off) wins over the fleet default")
	assert.Equal(t, 900,
		cfg.Channels[1].ResolveInteractionIdleTimeoutSeconds(cfg.DefaultInteractionIdleTimeoutSeconds),
		"an absent per-channel knob inherits the fleet default")
}

func TestLoadConfig_InteractionIdle_NegativeRejected(t *testing.T) {
	fleet := `
default_interaction_idle_timeout_seconds: -1
channels:
  - name: design
    members: [iron-fox, ada]
`
	_, err := LoadConfig(writeYAML(t, fleet))
	require.Error(t, err)
	assert.True(t, errors.Is(err, ErrInvalidInteractionIdleTimeout),
		"a negative fleet default is rejected at load")

	perChannel := `
channels:
  - name: design
    interaction_idle_timeout_seconds: -600
    members: [iron-fox, ada]
`
	_, err = LoadConfig(writeYAML(t, perChannel))
	require.Error(t, err)
	assert.True(t, errors.Is(err, ErrInvalidInteractionIdleTimeout),
		"a negative per-channel value is rejected at load")
}

// TestResolveInteractionIdleTimeouts_StampsRouter pins the startup applier:
// declared channels get their resolved window; the fleet default lands on the
// router for the store-resident fallback ([ChannelRouter.idleWindowLocked]).
func TestResolveInteractionIdleTimeouts_StampsRouter(t *testing.T) {
	store := newTestStore(t, SQLiteOptions{})
	router := NewChannelRouter(store, NoopDispatcher{}, zap.NewNop(), nil)
	ninety, zero := 90, 0
	cfg := &Config{
		DefaultInteractionIdleTimeoutSeconds: &ninety,
		Channels: []ChannelConfig{
			{Name: "design", InteractionIdleTimeoutSeconds: &zero},
			{Name: "planning"}, // inherits the fleet 90
		},
	}
	require.NoError(t, router.ResolveInteractionIdleTimeouts(t.Context(), cfg))

	router.interactionMu.Lock()
	defer router.interactionMu.Unlock()
	assert.Equal(t, 90*time.Second, router.defaultInteractionIdleTimeout,
		"the fleet default covers store-resident channels at read time")
	assert.Equal(t, time.Duration(0), router.interactionIdleTimeouts["group:design"],
		"an explicit per-channel 0 is stamped as rotation-off")
	assert.Equal(t, 90*time.Second, router.interactionIdleTimeouts["group:planning"],
		"a declared channel with no knob is stamped with the fleet default")
}
