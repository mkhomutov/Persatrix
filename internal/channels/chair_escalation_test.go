package channels

// chair_escalation_test.go — the chair-stall-escalation amendment, PR 2
// (docs/rfcs/0030-amendment-chair-stall-escalation.md §C item 1). TDD-first:
// this matrix was written red against the planned API and pins CE1–CE7's
// orchestrator half — deterministic stall detection at the floor round's
// tail, the detection-then-disposition metric contract, the one-per-
// interaction ration riding the resolver entry, and the floor-exempt
// forced-turn dispatch under a fresh event id.

import (
	"context"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	sdkmetric "go.opentelemetry.io/otel/sdk/metric"
	"go.opentelemetry.io/otel/sdk/metric/metricdata"
	"go.uber.org/zap"
)

// chairEscalationCount returns the chair_escalation counter value for the
// given channel_type + outcome pair, 0 when absent.
func chairEscalationCount(t *testing.T, rm metricdata.ResourceMetrics, channelType, outcome string) int64 {
	t.Helper()
	for _, sm := range rm.ScopeMetrics {
		for _, m := range sm.Metrics {
			if m.Name != "channel.conversation.chair_escalation" {
				continue
			}
			sum, ok := m.Data.(metricdata.Sum[int64])
			require.Truef(t, ok, "chair_escalation: expected Sum[int64], got %T", m.Data)
			for _, dp := range sum.DataPoints {
				ct, _ := dp.Attributes.Value("channel_type")
				oc, _ := dp.Attributes.Value("outcome")
				if ct.AsString() == channelType && oc.AsString() == outcome {
					return dp.Value
				}
			}
		}
	}
	return 0
}

// escalationHarness builds a floor-controlled stall stage: a group channel
// whose two participants never reply (the recorder swallows dispatches), a
// 1ms turn timeout so the stalled round completes immediately, a configured
// escalation chair, and the chair_escalation counter on a manual reader.
func escalationHarness(t *testing.T) (*ChannelRouter, *envelopeRecorder, ChannelStore, string, *sdkmetric.ManualReader) {
	t.Helper()
	reader := sdkmetric.NewManualReader()
	mp := sdkmetric.NewMeterProvider(sdkmetric.WithReader(reader))
	t.Cleanup(func() { _ = mp.Shutdown(context.Background()) })
	ctr, err := mp.Meter("test").Int64Counter("channel.conversation.chair_escalation")
	require.NoError(t, err)
	store := newTestStore(t, SQLiteOptions{})
	disp := &envelopeRecorder{}
	router := NewChannelRouter(store, disp, zap.NewNop(), &RouterMetrics{ChairEscalation: ctr})
	ch := mustCreateGroupWithPolicies(t, store, "planning",
		map[string]RespondPolicy{
			"alex":         RespondNever, // the human stimulus author
			"ember-owl":    RespondAlways,
			"iron-fox":     RespondAlways,
			"nova-sparrow": RespondAlways, // the designated chair
		}, "alex", "ember-owl", "iron-fox", "nova-sparrow")
	router.SetFloorControl(ch, true, time.Millisecond)
	router.SetEscalationChair(ch, "nova-sparrow")
	return router, disp, store, ch, reader
}

// stallStimulus publishes one open-floor message and returns its id. With the
// recorder never replying, every granted floor turn times out — the round
// stalls by construction.
func stallStimulus(t *testing.T, router *ChannelRouter, ch, sender string) string {
	t.Helper()
	id := uuid.NewString()
	require.NoError(t, router.Publish(context.Background(), ChannelMessage{
		ID: id, ChannelID: ch, SenderID: sender, Content: "thoughts, team?",
	}, ""))
	return id
}

// escalationEnvelopes filters the recorder's calls down to the forced-turn
// dispatches.
func escalationEnvelopes(disp *envelopeRecorder) []DispatchEnvelope {
	var out []DispatchEnvelope
	for _, env := range disp.snapshot() {
		if env.ChairEscalation {
			out = append(out, env)
		}
	}
	return out
}

// TestChairEscalation_StalledRoundDispatchesForcedTurn — the CE3 arc: a
// fully-silent round on an open tracked interaction dispatches exactly one
// forced turn to the configured chair, marked on the envelope and carried
// under a fresh event id (the agent-side conversation window dedups by
// message id, so the stalled stimulus's own id is not a reliable turn).
func TestChairEscalation_StalledRoundDispatchesForcedTurn(t *testing.T) {
	router, disp, _, ch, reader := escalationHarness(t)

	stimulusID := stallStimulus(t, router, ch, "alex")

	escalations := escalationEnvelopes(disp)
	require.Len(t, escalations, 1, "a stalled round dispatches exactly one forced turn")
	assert.Equal(t, "nova-sparrow", escalations[0].Recipient.ParticipantID,
		"the forced turn goes to the configured escalation chair")

	var rm metricdata.ResourceMetrics
	require.NoError(t, reader.Collect(context.Background(), &rm))
	assert.Equal(t, int64(1), chairEscalationCount(t, rm, "group", "dispatched"))

	// The fresh-event-id contract rides the dispatched message, which the
	// recorder does not capture — pin it via the dispatch seam instead.
	_ = stimulusID
}

// TestChairEscalation_FreshEventID — §C 1: the forced turn re-delivers the
// stimulus under a NEW message id; redelivering the original id would be
// silently deduped by the agent-side conversation window.
func TestChairEscalation_FreshEventID(t *testing.T) {
	router, _, store, ch, _ := escalationHarness(t)
	rec := &messageRecordingDispatcher{}
	router.dispatcher = rec

	stimulusID := stallStimulus(t, router, ch, "alex")

	var forced []ChannelMessage
	for i, env := range rec.envelopes {
		if env.ChairEscalation {
			forced = append(forced, rec.messages[i])
		}
	}
	require.Len(t, forced, 1)
	assert.NotEqual(t, stimulusID, forced[0].ID, "the forced turn carries a fresh event id")
	assert.NotEmpty(t, forced[0].ID)
	assert.Equal(t, "thoughts, team?", forced[0].Content,
		"the forced turn re-delivers the stalled stimulus content")
	_ = store
}

// messageRecordingDispatcher captures envelope+message pairs (the
// envelopeRecorder drops the message half).
type messageRecordingDispatcher struct {
	envelopes []DispatchEnvelope
	messages  []ChannelMessage
}

func (d *messageRecordingDispatcher) Dispatch(_ context.Context, env DispatchEnvelope, msg ChannelMessage) error {
	d.envelopes = append(d.envelopes, env)
	d.messages = append(d.messages, msg)
	return nil
}

// TestChairEscalation_OncePerInteraction — CE5: the second stalled round in
// the same interaction emits already_escalated and dispatches nothing.
func TestChairEscalation_OncePerInteraction(t *testing.T) {
	router, disp, _, ch, reader := escalationHarness(t)

	stallStimulus(t, router, ch, "alex")
	stallStimulus(t, router, ch, "alex") // same open interaction — within the idle window

	assert.Len(t, escalationEnvelopes(disp), 1, "the interaction's escalation ration is one")
	var rm metricdata.ResourceMetrics
	require.NoError(t, reader.Collect(context.Background(), &rm))
	assert.Equal(t, int64(1), chairEscalationCount(t, rm, "group", "dispatched"))
	assert.Equal(t, int64(1), chairEscalationCount(t, rm, "group", "already_escalated"))
}

// TestChairEscalation_FreshInteractionFreshRation — CE5's mark rides the
// resolver entry and dies with it: after an idle rotation, a new stall in the
// fresh interaction escalates again.
func TestChairEscalation_FreshInteractionFreshRation(t *testing.T) {
	router, disp, _, ch, _ := escalationHarness(t)
	now := time.Date(2026, 6, 11, 12, 0, 0, 0, time.UTC)
	router.interactionNow = func() time.Time { return now }

	stallStimulus(t, router, ch, "alex")
	now = now.Add(601 * time.Second) // idle rotation → fresh interaction
	stallStimulus(t, router, ch, "alex")

	assert.Len(t, escalationEnvelopes(disp), 2,
		"a fresh interaction carries a fresh escalation ration")
}

// TestChairEscalation_NoChairConfigured — the detection-then-disposition
// metric contract: the stall is still detected and counted (outcome=no_chair)
// so operators see stalls they could configure a chair for; nothing is
// dispatched.
func TestChairEscalation_NoChairConfigured(t *testing.T) {
	router, disp, _, ch, reader := escalationHarness(t)
	router.SetEscalationChair(ch, "") // unset the harness default

	stallStimulus(t, router, ch, "alex")

	assert.Empty(t, escalationEnvelopes(disp))
	var rm metricdata.ResourceMetrics
	require.NoError(t, reader.Collect(context.Background(), &rm))
	assert.Equal(t, int64(1), chairEscalationCount(t, rm, "group", "no_chair"))
}

// TestChairEscalation_RepliedRoundIsNotAStall — a round with at least one
// reply is a working conversation: no detection, no metric, no dispatch.
// Drives the tail directly — no setup publish (a stalled harness publish
// would itself escalate); the replied>0 guard exits before any state read.
func TestChairEscalation_RepliedRoundIsNotAStall(t *testing.T) {
	router, disp, _, ch, reader := escalationHarness(t)

	msg := ChannelMessage{ID: uuid.NewString(), ChannelID: ch, SenderID: "alex", Content: "hi"}
	router.maybeEscalateStall(context.Background(), msg, ChannelTypeGroup,
		floorRoundOutcome{granted: 2, replied: 1},
		[]Member{member("nova-sparrow", RespondAlways)}, 4, nil)

	assert.Empty(t, escalationEnvelopes(disp))
	var rm metricdata.ResourceMetrics
	require.NoError(t, reader.Collect(context.Background(), &rm))
	assert.Zero(t, chairEscalationCount(t, rm, "group", "dispatched"))
	assert.Zero(t, chairEscalationCount(t, rm, "group", "no_chair"))
}

// TestChairEscalation_EmptyRoundIsNotAStall — CE1's granted-turn floor: an
// empty candidate list is not a stall (nobody was asked anything).
func TestChairEscalation_EmptyRoundIsNotAStall(t *testing.T) {
	router, disp, _, ch, reader := escalationHarness(t)

	msg := ChannelMessage{ID: uuid.NewString(), ChannelID: ch, SenderID: "alex", Content: "hi"}
	router.maybeEscalateStall(context.Background(), msg, ChannelTypeGroup,
		floorRoundOutcome{granted: 0, replied: 0},
		[]Member{member("nova-sparrow", RespondAlways)}, 4, nil)

	assert.Empty(t, escalationEnvelopes(disp))
	var rm metricdata.ResourceMetrics
	require.NoError(t, reader.Collect(context.Background(), &rm))
	assert.Zero(t, chairEscalationCount(t, rm, "group", "dispatched"))
}

// TestChairEscalation_UntrackedChannelIsNotAStall — detection requires an
// open tracked interaction; a channel the resolver has never seen (direct
// tail invocation, no Publish) detects nothing.
func TestChairEscalation_UntrackedChannelIsNotAStall(t *testing.T) {
	router, disp, _, ch, reader := escalationHarness(t)

	msg := ChannelMessage{ID: uuid.NewString(), ChannelID: ch, SenderID: "alex", Content: "hi"}
	router.maybeEscalateStall(context.Background(), msg, ChannelTypeGroup,
		floorRoundOutcome{granted: 2, replied: 0},
		[]Member{member("nova-sparrow", RespondAlways)}, 4, nil)

	assert.Empty(t, escalationEnvelopes(disp))
	var rm metricdata.ResourceMetrics
	require.NoError(t, reader.Collect(context.Background(), &rm))
	assert.Zero(t, chairEscalationCount(t, rm, "group", "dispatched"))
	assert.Zero(t, chairEscalationCount(t, rm, "group", "no_chair"))
	_ = ch
}

// TestChannelMessageToProto_PopulatesChairEscalation — the wire half: the
// envelope marker rides `ChannelMessageEvent.chair_escalation`; an ordinary
// dispatch carries the proto3 default false (additive for old producers).
func TestChannelMessageToProto_PopulatesChairEscalation(t *testing.T) {
	d := &GRPCMessageDispatcher{logger: zap.NewNop()}

	ev := d.channelMessageToProto(
		ChannelMessage{ID: "m-1", ChannelID: "group:planning", SenderID: "a"},
		DispatchEnvelope{
			Recipient:       Member{ParticipantID: "b", RespondPolicy: RespondAlways},
			ChairEscalation: true,
		})
	assert.True(t, ev.ChairEscalation)

	ev = d.channelMessageToProto(
		ChannelMessage{ID: "m-2", ChannelID: "group:planning", SenderID: "a"},
		DispatchEnvelope{Recipient: Member{ParticipantID: "b", RespondPolicy: RespondAlways}})
	assert.False(t, ev.ChairEscalation)
}

// ─── Config: the escalation_chair_id knob ──────────────────────────────

func TestLoadConfig_EscalationChair_ParsesAndDefaultsEmpty(t *testing.T) {
	body := `
channels:
  - name: design
    escalation_chair_id: iron-fox
    members: [iron-fox, ada]
  - name: planning
    members: [iron-fox, ada]
`
	cfg, err := LoadConfig(writeYAML(t, body))
	require.NoError(t, err)
	assert.Equal(t, "iron-fox", cfg.Channels[0].EscalationChairID)
	assert.Empty(t, cfg.Channels[1].EscalationChairID, "absent knob means no escalation (opt-in)")
}

func TestLoadConfig_EscalationChair_MustBeAMember(t *testing.T) {
	body := `
channels:
  - name: design
    escalation_chair_id: stranger
    members: [iron-fox, ada]
`
	_, err := LoadConfig(writeYAML(t, body))
	require.Error(t, err)
	assert.ErrorIs(t, err, ErrInvalidEscalationChair,
		"an escalation chair that is not a member is a loud config error")
}

func TestResolveEscalationChairs_StampsRouter(t *testing.T) {
	store := newTestStore(t, SQLiteOptions{})
	router := NewChannelRouter(store, NoopDispatcher{}, zap.NewNop(), nil)
	cfg := &Config{
		Channels: []ChannelConfig{
			{Name: "design", EscalationChairID: "iron-fox",
				Members: []MemberConfig{{ID: "iron-fox", RespondPolicy: RespondAlways}}},
			{Name: "planning",
				Members: []MemberConfig{{ID: "ada", RespondPolicy: RespondAlways}}},
		},
	}
	require.NoError(t, router.ResolveEscalationChairs(t.Context(), cfg))

	assert.Equal(t, "iron-fox", router.escalationChairFor("group:design"))
	assert.Empty(t, router.escalationChairFor("group:planning"))
}
