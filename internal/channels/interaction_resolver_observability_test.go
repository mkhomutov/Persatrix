package channels

// interaction_resolver_observability_test.go — ISSUE-0095. The idle-rotation
// path logged only when it FIRED; a no-fire (the unreproduced 2026-06-12
// 700s-gap-vs-600s-window case) left no trace, so the next occurrence could
// only be caught by an operator watching a live MT. These tests pin the two
// observability seams that make a rotation decision self-diagnosing without a
// repro: a per-publish decision debug log (channel, window, now, lastActivity,
// gap, fired?) and the once-at-startup resolved window map.

import (
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.uber.org/zap"
	"go.uber.org/zap/zaptest/observer"
)

const rotationDecisionMsg = "channels: interaction idle-rotation decision"

// TestInteractionResolver_LogsRotationDecision_NoFire is the ISSUE-0095 core:
// a committed publish that stays WITHIN the window leaves a decision trace with
// rotated=false and the computed gap, so a future no-fire is diagnosable from
// logs alone. The opening mint is NOT a decision (the entry has no committed id
// yet to rotate), so the second publish is the first and only decision logged.
func TestInteractionResolver_LogsRotationDecision_NoFire(t *testing.T) {
	router, store, ch, now := resolverHarness(t)
	core, logs := observer.New(zap.DebugLevel)
	router.logger = zap.New(core)

	publishAndGetInteractionID(t, router, store, ch, "ember-owl", nil) // mint: not a decision
	*now = now.Add(599 * time.Second)
	publishAndGetInteractionID(t, router, store, ch, "iron-fox", nil) // within window: no-fire

	entries := logs.FilterMessage(rotationDecisionMsg).All()
	require.Len(t, entries, 1, "the committed within-window publish logs exactly one rotation decision")
	f := entries[0].ContextMap()
	assert.Equal(t, ch, f["channel_id"])
	assert.Equal(t, false, f["rotated"], "a within-window gap does not rotate")
	assert.Equal(t, 600*time.Second, f["window"])
	assert.Equal(t, 599*time.Second, f["gap"], "the gap is the diagnostic signal a no-fire needs")
}

// TestInteractionResolver_LogsRotationDecision_Fire — the fired side of the
// same seam: a past-window gap logs rotated=true with the gap that crossed the
// window, the companion to the existing trigger=idle close log/counter.
func TestInteractionResolver_LogsRotationDecision_Fire(t *testing.T) {
	router, store, ch, now := resolverHarness(t)
	core, logs := observer.New(zap.DebugLevel)
	router.logger = zap.New(core)

	publishAndGetInteractionID(t, router, store, ch, "ember-owl", nil)
	*now = now.Add(601 * time.Second)
	publishAndGetInteractionID(t, router, store, ch, "iron-fox", nil)

	entries := logs.FilterMessage(rotationDecisionMsg).All()
	require.Len(t, entries, 1, "the past-window publish logs one rotation decision")
	f := entries[0].ContextMap()
	assert.Equal(t, true, f["rotated"], "a gap past the window rotates")
	assert.Equal(t, 601*time.Second, f["gap"])
}

// TestInteractionResolver_RotationDecision_UncommittedMintNotLogged — a
// minted-but-uncommitted id is not eligible for rotation (no persisted
// messages), so it must not produce a decision line. Otherwise a channel whose
// first publish was rejected would emit a spurious "no-fire" the operator would
// chase.
func TestInteractionResolver_RotationDecision_UncommittedMintNotLogged(t *testing.T) {
	router, _, ch, _ := resolverHarness(t)
	core, logs := observer.New(zap.DebugLevel)
	router.logger = zap.New(core)

	// resolve without settling persisted: the mint stays tentative.
	_, _, settle := router.resolveInteractionID(t.Context(), ch, ChannelTypeGroup, "")
	settle(false)

	assert.Empty(t, logs.FilterMessage(rotationDecisionMsg).All(),
		"an uncommitted mint is not a rotation decision")
}

// TestResolveInteractionIdleTimeouts_LogsWindowMap — the startup applier emits
// the resolved per-channel window map once, so a wrong resolved window (the
// other ISSUE-0095 suspect) is visible at boot without a repro.
func TestResolveInteractionIdleTimeouts_LogsWindowMap(t *testing.T) {
	store := newTestStore(t, SQLiteOptions{})
	core, logs := observer.New(zap.InfoLevel)
	router := NewChannelRouter(store, NoopDispatcher{}, zap.New(core), nil)
	ninety, zero := 90, 0
	cfg := &Config{
		DefaultInteractionIdleTimeoutSeconds: &ninety,
		Channels: []ChannelConfig{
			{Name: "design", InteractionIdleTimeoutSeconds: &zero},
			{Name: "planning"},
		},
	}
	require.NoError(t, router.ResolveInteractionIdleTimeouts(t.Context(), cfg))

	entries := logs.FilterMessage("channels: interaction idle windows resolved").All()
	require.Len(t, entries, 1, "the startup applier logs the resolved window map exactly once")
	f := entries[0].ContextMap()
	assert.Equal(t, 90*time.Second, f["default_window"])
	windows, ok := f["windows"].(map[string]string)
	require.True(t, ok, "the per-channel windows ride as a map")
	assert.Equal(t, (0 * time.Second).String(), windows["group:design"], "an explicit 0 is rotation-off")
	assert.Equal(t, (90 * time.Second).String(), windows["group:planning"], "an absent knob inherits the fleet default")
}
