package channels

// convene_test.go — RFC 0052 §B self-convening, orchestrator half (PR 3).
// TDD-first: pins that [ChannelRouter.ConveneChannel] dispatches exactly one
// directed convene forced turn to the configured convener, carrying the
// `markerConvene` marker and the operator topic/agenda/goal directive, and
// that it fails loudly (no dispatch) on a missing channel, an unarmed channel,
// a channel with a live interaction, a drifted/observer convener, a roster with
// no open-floor responder (an observer- or when_mentioned-only audience), or a
// channel with no subject to convene on — the runaway-class failures the safety
// contract exists to catch — plus the invariant that the synthetic convene
// sender can never be a legal participant id (so a convener can never collide
// with it and self-suppress its own opener).

import (
	"context"
	"strings"
	"testing"
	"unicode/utf8"

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

// TestConveneDispatchSenderID_IsNotAValidParticipantID — the deep-review fix:
// the synthetic convene sender MUST be a value no roster member can ever hold,
// or a convener whose agent id equals it would have its opening turn silently
// suppressed by the receiver gate's self-sender defence (a 202-then-nothing on
// an unattended channel). The `:` makes the collision impossible by
// construction; this locks the invariant so a future edit cannot reintroduce
// the hazard by picking a participant-id-shaped sentinel.
func TestConveneDispatchSenderID_IsNotAValidParticipantID(t *testing.T) {
	assert.Error(t, ValidateParticipantID(ConveneDispatchSenderID),
		"the convene dispatch sender must never be a legal participant id, else a "+
			"convener sharing it would self-suppress its own opening turn")
}

// TestConvene_RejectsMissingChannel — convening a channel that does not exist
// reports 404 (ErrChannelNotFound), not 409 not-armed: the existence check runs
// before the AutonomousFor read, which would otherwise resolve the disabled
// default for an unknown channel and mis-report it as "not armed".
func TestConvene_RejectsMissingChannel(t *testing.T) {
	router, disp, _ := conveneHarness(t, AutonomousConfig{Enabled: true, Convener: "nova-sparrow"})

	_, err := router.ConveneChannel(context.Background(), "group:does-not-exist")
	require.Error(t, err)
	assert.ErrorIs(t, err, ErrChannelNotFound)
	assert.NotErrorIs(t, err, ErrChannelNotArmed, "a missing channel must not masquerade as unarmed")
	assert.Empty(t, conveneEnvelopes(disp))
}

// TestConvene_RejectsWhenInteractionOpen — convening a channel that already has
// an open committed interaction is refused loudly (ErrChannelAlreadyConvening)
// rather than silently joining the live discussion. Convening is "open an idle
// channel"; the live case is PR 7's force-fresh territory.
func TestConvene_RejectsWhenInteractionOpen(t *testing.T) {
	router, disp, ch := conveneHarness(t, AutonomousConfig{
		Enabled: true, Convener: "nova-sparrow", Topic: "Live already",
	})

	// Open + commit an interaction so the channel is no longer idle.
	_, _, commit := router.resolveInteractionID(context.Background(), ch, ChannelTypeGroup, "")
	commit(true)

	_, err := router.ConveneChannel(context.Background(), ch)
	require.Error(t, err)
	assert.ErrorIs(t, err, ErrChannelAlreadyConvening)
	assert.Empty(t, conveneEnvelopes(disp), "a live channel dispatches no second opener")
}

// TestConvene_RejectsNoAudience — an armed channel whose only floor-capable
// member is the convener (everyone else is an observer) is refused: the opening
// turn would land in an empty room, a guaranteed-futile convene that burns an
// uncapped opener for nothing.
func TestConvene_RejectsNoAudience(t *testing.T) {
	store := newTestStore(t, SQLiteOptions{})
	disp := &messageRecordingDispatcher{}
	router := NewChannelRouter(store, disp, zap.NewNop(), nil)
	ch := mustCreateGroupWithPolicies(t, store, "planning",
		map[string]RespondPolicy{
			"nova-sparrow": RespondAlways, // the convener — the lone floor-capable member
			"watcher":      RespondNever,  // an observer, never an audience
		}, "nova-sparrow", "watcher")
	router.SetAutonomous(ch, AutonomousConfig{Enabled: true, Convener: "nova-sparrow", Topic: "x"})

	_, err := router.ConveneChannel(context.Background(), ch)
	require.Error(t, err)
	assert.ErrorIs(t, err, ErrAutonomousNoAudience)
	assert.Empty(t, conveneEnvelopes(disp))
}

// TestConvene_RejectsWhenMentionedOnlyAudience — the deep-review fix: a
// `when_mentioned` member is NOT an audience for the convener's open-floor
// opener (the gate suppresses it `not_mentioned`, and the opener names no one),
// so a roster of {convener=always, member=when_mentioned} is the same
// dead-on-arrival convene as an observer-only one. The earlier `!= RespondNever`
// audience test let this through, dispatching an opener nobody answers.
func TestConvene_RejectsWhenMentionedOnlyAudience(t *testing.T) {
	store := newTestStore(t, SQLiteOptions{})
	disp := &messageRecordingDispatcher{}
	router := NewChannelRouter(store, disp, zap.NewNop(), nil)
	ch := mustCreateGroupWithPolicies(t, store, "planning",
		map[string]RespondPolicy{
			"nova-sparrow": RespondAlways,        // the convener
			"quiet-quokka": RespondWhenMentioned, // only replies when @-mentioned — never to the open-floor opener
		}, "nova-sparrow", "quiet-quokka")
	router.SetAutonomous(ch, AutonomousConfig{Enabled: true, Convener: "nova-sparrow", Topic: "x"})

	_, err := router.ConveneChannel(context.Background(), ch)
	require.Error(t, err)
	assert.ErrorIs(t, err, ErrAutonomousNoAudience)
	assert.Empty(t, conveneEnvelopes(disp), "an opener must not be dispatched into a room nobody answers")
}

// TestConvene_RejectsEmptyDirective — an armed channel with a valid convener and
// a real audience but NO topic/agenda/goal is refused: the directive composes
// empty, so the convener would open on nothing (an empty `<external_data>`
// envelope) — a degenerate, uncapped opener. PR 1 validation requires a convener
// but not a subject, so this is the convene-time guard for that gap.
func TestConvene_RejectsEmptyDirective(t *testing.T) {
	// conveneHarness gives nova/ember/iron all RespondAlways — a real audience —
	// so only the no-subject guard can fire here.
	router, disp, ch := conveneHarness(t, AutonomousConfig{Enabled: true, Convener: "nova-sparrow"})

	_, err := router.ConveneChannel(context.Background(), ch)
	require.Error(t, err)
	assert.ErrorIs(t, err, ErrAutonomousNoTopic)
	assert.Empty(t, conveneEnvelopes(disp), "a subject-less channel dispatches no opener")
}

// TestComposeConveneDirective_EmptyWhenAllSectionsBlank — the empty-directive
// case the convene guard keys on (returns "" so the caller can refuse).
func TestComposeConveneDirective_EmptyWhenAllSectionsBlank(t *testing.T) {
	assert.Empty(t, composeConveneDirective(AutonomousConfig{}))
	// Whitespace-only topic trims to empty, too.
	assert.Empty(t, composeConveneDirective(AutonomousConfig{Topic: "   "}))
}

// TestComposeConveneDirective_BoundsHugeDirective — a pathological multi-MB
// operator free-text (the topic/goal fields carry no maxLength) is hard-trimmed
// at the wire-safety ceiling so the gRPC dispatch can never carry a multi-MB
// directive; the result stays valid UTF-8.
func TestComposeConveneDirective_BoundsHugeDirective(t *testing.T) {
	got := composeConveneDirective(AutonomousConfig{Topic: strings.Repeat("x", maxConveneDirectiveBytes*2)})
	assert.LessOrEqual(t, len(got), maxConveneDirectiveBytes)
	assert.True(t, utf8.ValidString(got), "the dispatched directive must stay valid UTF-8")
}
