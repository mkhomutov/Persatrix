package channels

// convene_test.go — RFC 0052 §B self-convening, orchestrator half (PR 3).
// TDD-first: pins that [ChannelRouter.ConveneChannel] dispatches exactly one
// directed convene forced turn to the configured convener, carrying the
// `markerConvene` marker and the operator topic/agenda/goal directive, and
// that it fails loudly (no dispatch) on an unarmed channel or a
// drifted/observer convener — the runaway-class failures the safety contract
// exists to catch.

import (
	"context"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.uber.org/zap"
)

// conveneHarness builds an armed group channel with a recording dispatcher so
// the test can assert both the envelope marker and the directive content.
func conveneHarness(t *testing.T, a AutonomousConfig) (*ChannelRouter, *messageRecordingDispatcher, string) {
	t.Helper()
	store := newTestStore(t, SQLiteOptions{})
	disp := &messageRecordingDispatcher{}
	router := NewChannelRouter(store, disp, zap.NewNop(), nil)
	ch := mustCreateGroupWithPolicies(t, store, "planning",
		map[string]RespondPolicy{
			"nova-sparrow": RespondAlways, // the convener
			"ember-owl":    RespondAlways,
			"iron-fox":     RespondAlways,
		}, "nova-sparrow", "ember-owl", "iron-fox")
	router.SetAutonomous(ch, a)
	return router, disp, ch
}

func conveneEnvelopes(disp *messageRecordingDispatcher) []DispatchEnvelope {
	var out []DispatchEnvelope
	for _, env := range disp.envelopes {
		if env.Convene {
			out = append(out, env)
		}
	}
	return out
}

// TestConvene_DispatchesSingleForcedTurnToConvener — the happy path: convening
// an armed idle channel dispatches exactly one convene-marked forced turn to
// the convener, and to no one else.
func TestConvene_DispatchesSingleForcedTurnToConvener(t *testing.T) {
	router, disp, ch := conveneHarness(t, AutonomousConfig{
		Enabled:  true,
		Convener: "nova-sparrow",
		Topic:    "Should we adopt a monorepo?",
		Agenda:   []string{"Build tooling cost", "Cross-team coupling"},
		Goal:     "A synthesized recommendation.",
	})

	convener, err := router.ConveneChannel(context.Background(), ch)
	require.NoError(t, err)
	assert.Equal(t, "nova-sparrow", convener)

	env := conveneEnvelopes(disp)
	require.Len(t, env, 1, "convening dispatches exactly one convene forced turn")
	assert.Equal(t, "nova-sparrow", env[0].Recipient.ParticipantID,
		"the convene forced turn goes to the configured convener")
	// The convene lane never aliases the other directed markers (the
	// dispatchMarker never-alias invariant).
	assert.False(t, env[0].ChairEscalation)
	assert.False(t, env[0].InteractionCloseNotification)
}

// TestConvene_DirectiveCarriesTopicAgendaGoal — the seed directive carries the
// operator topic/agenda/goal so the convener has something to open on; the
// sender is the synthetic orchestrator id (NOT the convener, which the
// receiver self-sender defence would suppress).
func TestConvene_DirectiveCarriesTopicAgendaGoal(t *testing.T) {
	router, disp, ch := conveneHarness(t, AutonomousConfig{
		Enabled:  true,
		Convener: "nova-sparrow",
		Topic:    "Should we adopt a monorepo?",
		Agenda:   []string{"Build tooling cost"},
		Goal:     "A recommendation.",
	})

	_, err := router.ConveneChannel(context.Background(), ch)
	require.NoError(t, err)

	require.Len(t, disp.messages, 1)
	msg := disp.messages[0]
	assert.Equal(t, ConveneDispatchSenderID, msg.SenderID)
	assert.NotEqual(t, "nova-sparrow", msg.SenderID,
		"the directive sender must not be the convener (self-sender suppression)")
	assert.Contains(t, msg.Content, "Should we adopt a monorepo?")
	assert.Contains(t, msg.Content, "Build tooling cost")
	assert.Contains(t, msg.Content, "A recommendation.")
	assert.NotEmpty(t, msg.ID, "the directive carries an event id")
}

// TestConvene_RejectsUnarmedChannel — a channel that is not autonomous-enabled
// cannot be convened, and nothing is dispatched.
func TestConvene_RejectsUnarmedChannel(t *testing.T) {
	router, disp, ch := conveneHarness(t, AutonomousConfig{Enabled: false})

	_, err := router.ConveneChannel(context.Background(), ch)
	require.Error(t, err)
	assert.ErrorIs(t, err, ErrChannelNotArmed)
	assert.Empty(t, conveneEnvelopes(disp), "an unarmed channel dispatches nothing")
}

// TestConvene_RejectsDriftedConvener — a convener who left the roster between
// arming and convening fails the convene loudly rather than dispatching into a
// silent gate suppression (RemoveMember does not touch the resolved block).
func TestConvene_RejectsDriftedConvener(t *testing.T) {
	router, disp, ch := conveneHarness(t, AutonomousConfig{
		Enabled:  true,
		Convener: "ghost-agent", // armed for a member who is not in the roster
	})

	_, err := router.ConveneChannel(context.Background(), ch)
	require.Error(t, err)
	assert.ErrorIs(t, err, ErrInvalidAutonomousConvener)
	assert.Empty(t, conveneEnvelopes(disp))
}

// TestConvene_RejectsObserverConvener — an observer (respond: never) convener
// can never author the opening turn (its receiver gate suppresses it), so the
// convene is rejected — defence-in-depth mirroring the config-time gate.
func TestConvene_RejectsObserverConvener(t *testing.T) {
	store := newTestStore(t, SQLiteOptions{})
	disp := &messageRecordingDispatcher{}
	router := NewChannelRouter(store, disp, zap.NewNop(), nil)
	ch := mustCreateGroupWithPolicies(t, store, "planning",
		map[string]RespondPolicy{
			"watcher":   RespondNever, // an observer
			"ember-owl": RespondAlways,
		}, "watcher", "ember-owl")
	router.SetAutonomous(ch, AutonomousConfig{Enabled: true, Convener: "watcher"})

	_, err := router.ConveneChannel(context.Background(), ch)
	require.Error(t, err)
	assert.ErrorIs(t, err, ErrInvalidAutonomousConvener)
	assert.Empty(t, conveneEnvelopes(disp))
}

// TestComposeConveneDirective_OmitsEmptySections — a topic-only block yields a
// directive with no agenda/goal scaffolding.
func TestComposeConveneDirective_OmitsEmptySections(t *testing.T) {
	got := composeConveneDirective(AutonomousConfig{Topic: "Just a topic"})
	assert.Equal(t, "Topic: Just a topic", got)
	assert.NotContains(t, got, "Agenda:")
	assert.NotContains(t, got, "Goal:")
}
