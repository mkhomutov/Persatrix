package channels

// chair_escalation_guard_test.go — the chair-stall-escalation amendment's
// guard matrix, from the PR #609 deep review. chair_escalation_test.go pins
// the happy arc (CE1–CE7); this file pins the dispositions the first cut
// missed: the chair-authored stimulus (a forced turn every dispatch-path
// sender filter — and the receiver gate's self_sender defence — guarantees
// suppressed, so it must be withheld WITHOUT spending the ration), the round
// that outlived its interaction (escalating would spend the successor's
// ration on the predecessor's silence), the two dispatch_error producers
// (runtime membership drift, dispatcher failure) and their deliberate
// ration spend, the dispatchTo contract the forced turn must honour
// (`channel.messages.delivered` + the presence re-mark), and the
// observer-chair config rejection (CE2 names a PARTICIPANT member).

import (
	"context"
	"errors"
	"testing"

	"github.com/google/uuid"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.opentelemetry.io/otel/sdk/metric/metricdata"
	"go.uber.org/zap"
	"go.uber.org/zap/zaptest/observer"
)

// commitOpenInteraction mints + commits an open interaction for `ch`, exactly
// as a persisted publish would (resolve, then settle persisted=true), and
// returns its id — the direct-drive seam for tests that need a tracked
// interaction without a stalling setup publish.
func commitOpenInteraction(t *testing.T, router *ChannelRouter, ch string) string {
	t.Helper()
	id, _, settle := router.resolveInteractionID(context.Background(), ch, ChannelTypeGroup, "")
	settle(true)
	return id
}

// stalledMsg builds the stimulus message a stalled round would hand the tail:
// the router-stamped interaction_id rides the metadata bag, as publishCommit
// leaves it on every routed publish.
func stalledMsg(ch, sender, interactionID string) ChannelMessage {
	return ChannelMessage{
		ID: uuid.NewString(), ChannelID: ch, SenderID: sender, Content: "thoughts, team?",
		Metadata: map[string]any{interactionIDMetadataKey: interactionID},
	}
}

// deliveredCount returns the `channel.messages.delivered` counter value for
// the given status, 0 when absent.
func deliveredCount(t *testing.T, rm metricdata.ResourceMetrics, status string) int64 {
	t.Helper()
	for _, sm := range rm.ScopeMetrics {
		for _, m := range sm.Metrics {
			if m.Name != "channel.messages.delivered" {
				continue
			}
			sum, ok := m.Data.(metricdata.Sum[int64])
			require.Truef(t, ok, "messages.delivered: expected Sum[int64], got %T", m.Data)
			for _, dp := range sum.DataPoints {
				st, _ := dp.Attributes.Value("status")
				if st.AsString() == status {
					return dp.Value
				}
			}
		}
	}
	return 0
}

// TestChairEscalation_ChairAuthoredStimulus_WithholdsForcedTurn — the
// self-stimulus guard: when the chair itself authored the stalled stimulus,
// the forced turn would re-deliver the chair's own message to it. Every
// dispatch path filters the sender (orderResponders' never-reply-to-self,
// dispatchConcurrent's sender skip) and the receiver gate's self_sender
// defence suppresses a self-delivery before any LLM regardless — dispatching
// would burn the interaction's one ration on a turn that cannot happen while
// recording `dispatched`. Withheld as `self_stimulus`, ration UNSPENT: a
// later stall on another member's stimulus in the same interaction still
// escalates.
func TestChairEscalation_ChairAuthoredStimulus_WithholdsForcedTurn(t *testing.T) {
	router, disp, _, ch, reader := escalationHarness(t)

	stallStimulus(t, router, ch, "nova-sparrow") // the chair asks; ember-owl + iron-fox pass

	assert.Empty(t, escalationEnvelopes(disp),
		"the chair must not receive its own stimulus as a forced turn")
	var rm metricdata.ResourceMetrics
	require.NoError(t, reader.Collect(context.Background(), &rm))
	assert.Equal(t, int64(1), chairEscalationCount(t, rm, "group", "self_stimulus"))
	assert.Zero(t, chairEscalationCount(t, rm, "group", "dispatched"))

	// The ration was not spent: a second stall in the SAME interaction, on a
	// stimulus the chair did not author, still escalates.
	stallStimulus(t, router, ch, "alex")
	require.Len(t, escalationEnvelopes(disp), 1,
		"a withheld self-stimulus escalation must not consume the interaction's ration")
	require.NoError(t, reader.Collect(context.Background(), &rm))
	assert.Equal(t, int64(1), chairEscalationCount(t, rm, "group", "dispatched"))
}

// TestChairEscalation_RoundOutlivedInteraction_NotDetected — the stale-round
// guard: a serialized round runs up to N×turnTimeout while rotation/close
// happen lazily on concurrent publishes, so by round end the open id can be a
// different generation than the one the round stalled on (the stimulus's
// stamped interaction_id). Escalating then would spend the successor's ration
// on the predecessor's silence and dispatch a forced turn whose stamped id
// disagrees with the ration's — and the divergence itself proves new traffic
// arrived mid-round, so the channel is not stalled. Fails CE1 detection like
// the untracked branch: no dispatch, no metric.
func TestChairEscalation_RoundOutlivedInteraction_NotDetected(t *testing.T) {
	router, disp, _, ch, reader := escalationHarness(t)
	open := commitOpenInteraction(t, router, ch)
	members := []Member{member("nova-sparrow", RespondAlways)}

	stale := stalledMsg(ch, "alex", uuid.NewString()) // a retired generation's id
	router.maybeEscalateStall(context.Background(), stale, ChannelTypeGroup, "",
		floorRoundOutcome{granted: 2}, members, 4, nil)

	assert.Empty(t, escalationEnvelopes(disp))
	var rm metricdata.ResourceMetrics
	require.NoError(t, reader.Collect(context.Background(), &rm))
	for _, outcome := range []string{"dispatched", "no_chair", "already_escalated", "dispatch_error", "self_stimulus"} {
		assert.Zerof(t, chairEscalationCount(t, rm, "group", outcome),
			"a round that outlived its interaction is not a detected stall (outcome %s)", outcome)
	}

	// Control: the same round under the OPEN interaction's stamped id is the
	// real stall and dispatches.
	router.maybeEscalateStall(context.Background(), stalledMsg(ch, "alex", open), ChannelTypeGroup, "",
		floorRoundOutcome{granted: 2}, members, 4, nil)
	require.Len(t, escalationEnvelopes(disp), 1)
	require.NoError(t, reader.Collect(context.Background(), &rm))
	assert.Equal(t, int64(1), chairEscalationCount(t, rm, "group", "dispatched"))
}

// TestChairEscalation_RuntimeDrift_SpendsRationAsDispatchError — the chair
// left the channel after startup (config load validated membership, so this
// is drift): dispatch_error, no envelope, and the ration is DELIBERATELY
// spent — one attempt per interaction keeps the loop guard simple; idle
// rotation nets it. The second stall pins the spend as already_escalated.
func TestChairEscalation_RuntimeDrift_SpendsRationAsDispatchError(t *testing.T) {
	router, disp, _, ch, reader := escalationHarness(t)
	open := commitOpenInteraction(t, router, ch)
	withoutChair := []Member{member("ember-owl", RespondAlways), member("iron-fox", RespondAlways)}

	router.maybeEscalateStall(context.Background(), stalledMsg(ch, "alex", open), ChannelTypeGroup, "",
		floorRoundOutcome{granted: 2}, withoutChair, 3, nil)
	router.maybeEscalateStall(context.Background(), stalledMsg(ch, "alex", open), ChannelTypeGroup, "",
		floorRoundOutcome{granted: 2}, withoutChair, 3, nil)

	assert.Empty(t, escalationEnvelopes(disp))
	var rm metricdata.ResourceMetrics
	require.NoError(t, reader.Collect(context.Background(), &rm))
	assert.Equal(t, int64(1), chairEscalationCount(t, rm, "group", "dispatch_error"))
	assert.Equal(t, int64(1), chairEscalationCount(t, rm, "group", "already_escalated"),
		"a drift dispatch_error spends the ration — one attempt per interaction")
}

// chairEscalationFailingDispatcher records every envelope but fails exactly
// the forced-turn dispatches, so the floor turns of a full publish proceed
// (and time out) normally while the escalation send errors.
type chairEscalationFailingDispatcher struct {
	envelopeRecorder
}

func (d *chairEscalationFailingDispatcher) Dispatch(ctx context.Context, env DispatchEnvelope, msg ChannelMessage) error {
	_ = d.envelopeRecorder.Dispatch(ctx, env, msg)
	if env.ChairEscalation {
		return errors.New("chair endpoint unreachable")
	}
	return nil
}

// TestChairEscalation_DispatcherFailure_DispatchError — the second
// dispatch_error producer: the chair is a member but the send itself fails.
// Same deliberate ration spend as drift; the second stalled round in the
// interaction emits already_escalated, not a retry.
func TestChairEscalation_DispatcherFailure_DispatchError(t *testing.T) {
	router, _, _, ch, reader := escalationHarness(t)
	failing := &chairEscalationFailingDispatcher{}
	router.dispatcher = failing

	stallStimulus(t, router, ch, "alex")
	stallStimulus(t, router, ch, "alex") // same open interaction

	var rm metricdata.ResourceMetrics
	require.NoError(t, reader.Collect(context.Background(), &rm))
	assert.Equal(t, int64(1), chairEscalationCount(t, rm, "group", "dispatch_error"))
	assert.Equal(t, int64(1), chairEscalationCount(t, rm, "group", "already_escalated"),
		"a failed dispatch spends the ration — one attempt per interaction")
	assert.Zero(t, chairEscalationCount(t, rm, "group", "dispatched"))
}

// TestChairEscalation_ForcedTurnHonoursDispatchContract — the forced turn
// rides [ChannelRouter.dispatchTo], the single deadline + telemetry contract
// point every other recipient dispatch uses: it increments
// `channel.messages.delivered` (delivered/error dashboards must see forced
// turns), and re-stamps the chair's presence mark (the round-start mark has
// typically aged out across the silent round — the same TTL decay
// runFloorTurn's re-mark exists for).
func TestChairEscalation_ForcedTurnHonoursDispatchContract(t *testing.T) {
	router, disp, _, ch, reader := escalationHarness(t)
	open := commitOpenInteraction(t, router, ch)

	router.maybeEscalateStall(context.Background(), stalledMsg(ch, "alex", open), ChannelTypeGroup, "",
		floorRoundOutcome{granted: 2}, []Member{member("nova-sparrow", RespondAlways)}, 4, nil)

	require.Len(t, escalationEnvelopes(disp), 1)
	var rm metricdata.ResourceMetrics
	require.NoError(t, reader.Collect(context.Background(), &rm))
	assert.Equal(t, int64(1), deliveredCount(t, rm, "ok"),
		"the forced turn must count on channel.messages.delivered like every dispatch")
	assert.Equal(t, []string{"nova-sparrow"}, router.ChannelActivity(ch),
		"the forced turn is an expected-reply dispatch: the chair shows as thinking")
}

// ─── Config: the observer-chair rejection (CE2 names a participant) ─────

// TestLoadConfig_EscalationChair_RejectsObserver — an `observer`/`never`
// chair is as guaranteed-futile as a non-member: the receiver gate suppresses
// an observer before any LLM, forever, so every stall would burn its ration
// as `dispatched` with no turn possible. Same loud-at-load rationale as the
// membership check.
func TestLoadConfig_EscalationChair_RejectsObserver(t *testing.T) {
	for _, spelling := range []string{"observer", "never"} {
		t.Run(spelling, func(t *testing.T) {
			body := `
channels:
  - name: design
    escalation_chair_id: watcher
    members:
      - id: watcher
        respond: ` + spelling + `
      - id: ada
`
			_, err := LoadConfig(writeYAML(t, body))
			require.Error(t, err)
			assert.ErrorIs(t, err, ErrInvalidEscalationChair,
				"an observer chair can never take the forced turn and must fail at load")
		})
	}
}

// TestLoadConfig_EscalationChair_AcceptsAddressed — the boundary pin: only
// the never-class disposition is rejected. An `addressed` chair is legal —
// the forced-turn marker is the directed admit that lets it speak (CE3's
// gate lift), so addressed-ness is not futility.
func TestLoadConfig_EscalationChair_AcceptsAddressed(t *testing.T) {
	body := `
channels:
  - name: design
    escalation_chair_id: advisor
    members:
      - id: advisor
        respond: addressed
      - id: ada
`
	cfg, err := LoadConfig(writeYAML(t, body))
	require.NoError(t, err)
	assert.Equal(t, "advisor", cfg.Channels[0].EscalationChairID)
}

// ─── PR #609 review follow-up: the four remaining findings ──────────────

// TestChairEscalation_RoundOutlivedInteraction_LogsDivergence — the stale-
// round guard is the one silently-dropped branch in an otherwise every-
// branch-counted design (deliberately: per the RFC it fails CE1 *detection*,
// so counting it would pollute the stall counter with non-stalls). A debug
// line keeps the mid-round-rotation race observable for whoever eventually
// chases a "stall that didn't escalate".
func TestChairEscalation_RoundOutlivedInteraction_LogsDivergence(t *testing.T) {
	router, _, _, ch, _ := escalationHarness(t)
	core, logs := observer.New(zap.DebugLevel)
	router.logger = zap.New(core)
	open := commitOpenInteraction(t, router, ch)

	router.maybeEscalateStall(context.Background(), stalledMsg(ch, "alex", uuid.NewString()),
		ChannelTypeGroup, "", floorRoundOutcome{granted: 2},
		[]Member{member("nova-sparrow", RespondAlways)}, 4, nil)

	entries := logs.FilterMessage("channels: stalled round outlived its interaction; not escalating").All()
	require.Len(t, entries, 1, "the stale-round guard must leave a debug trace")
	fields := entries[0].ContextMap()
	assert.Equal(t, ch, fields["channel_id"])
	assert.Equal(t, open, fields["open_interaction_id"])
	assert.NotEqual(t, open, fields["stamped_interaction_id"],
		"the divergence itself is the evidence the line exists to carry")
}

// TestChairEscalation_DispatcherFailure_ClearsThinkingMark — the chair is
// re-stamped as thinking BEFORE the dispatch (the runFloorTurn ordering: mark
// first, or a fast reply could race the mark and strand it the other way
// round). When the dispatch itself fails, no reply can ever clear that mark —
// clear it on the error branch instead of leaving a "thinking" line dangling
// until the TTL prune.
func TestChairEscalation_DispatcherFailure_ClearsThinkingMark(t *testing.T) {
	router, _, _, ch, _ := escalationHarness(t)
	router.dispatcher = &chairEscalationFailingDispatcher{}
	open := commitOpenInteraction(t, router, ch)

	router.maybeEscalateStall(context.Background(), stalledMsg(ch, "alex", open),
		ChannelTypeGroup, "", floorRoundOutcome{granted: 2},
		[]Member{member("nova-sparrow", RespondAlways)}, 4, nil)

	assert.NotContains(t, router.ChannelActivity(ch), "nova-sparrow",
		"a failed forced-turn dispatch must not strand the chair's thinking mark")
}

// TestChairEscalation_ForcedTurnMetadataDetached — `forced := msg` copies the
// struct but aliases the Metadata map. Nothing downstream mutates it today,
// but the forced turn outlives the stimulus's round and crosses the
// dispatcher seam; pin the clone so a future metadata write on either side
// cannot silently corrupt the other.
func TestChairEscalation_ForcedTurnMetadataDetached(t *testing.T) {
	router, _, _, ch, _ := escalationHarness(t)
	rec := &messageRecordingDispatcher{}
	router.dispatcher = rec
	open := commitOpenInteraction(t, router, ch)

	stimulus := stalledMsg(ch, "alex", open)
	router.maybeEscalateStall(context.Background(), stimulus, ChannelTypeGroup, "",
		floorRoundOutcome{granted: 2}, []Member{member("nova-sparrow", RespondAlways)}, 4, nil)

	var forced []ChannelMessage
	for i, env := range rec.envelopes {
		if env.ChairEscalation {
			forced = append(forced, rec.messages[i])
		}
	}
	require.Len(t, forced, 1)
	assert.Equal(t, open, forced[0].Metadata[interactionIDMetadataKey],
		"the stamped interaction_id still rides the forced turn (CE6 lease attribution)")
	forced[0].Metadata["probe"] = true
	assert.NotContains(t, stimulus.Metadata, "probe",
		"the forced turn's metadata must be a detached copy, not an alias of the stimulus's map")
}

// TestLoadConfig_EscalationChair_RequiresFloorControl — stall detection runs
// ONLY at the floor round's tail, so `escalation_chair_id` on a channel with
// an explicit `floor_control: false` is a knob that can never act — and
// unlike the runtime dispositions it is also invisible: no round, no
// detection, no metric, so the operator reads "no stalls" where the truth is
// "knob inert". Same loud-at-load rationale as the non-member and observer
// rejections.
func TestLoadConfig_EscalationChair_RequiresFloorControl(t *testing.T) {
	body := `
channels:
  - name: design
    floor_control: false
    escalation_chair_id: ada
    members: [ada, iron-fox]
`
	_, err := LoadConfig(writeYAML(t, body))
	require.Error(t, err)
	assert.ErrorIs(t, err, ErrInvalidEscalationChair,
		"an escalation chair on a floor-control-disabled channel can never act and must fail at load")
}

// TestLoadConfig_EscalationChair_FloorControlBoundary — explicit true and
// absent (the group default is ON) both stay legal: only the explicit
// opt-out contradicts the knob.
func TestLoadConfig_EscalationChair_FloorControlBoundary(t *testing.T) {
	for name, line := range map[string]string{"explicit_true": "    floor_control: true\n", "absent_default_on": ""} {
		t.Run(name, func(t *testing.T) {
			body := `
channels:
  - name: design
` + line + `    escalation_chair_id: ada
    members: [ada, iron-fox]
`
			cfg, err := LoadConfig(writeYAML(t, body))
			require.NoError(t, err)
			assert.Equal(t, "ada", cfg.Channels[0].EscalationChairID)
		})
	}
}
