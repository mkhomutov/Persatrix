package channels

// standing_schedule_test.go — RFC 0052 §E standing/scheduled discussions, the
// config-round-trip timer PRODUCER (v0.3.11 PR 7c-i). TDD-first: pins the pure
// derivation that turns an armed STANDING channel's `autonomous.schedule_interval_seconds`
// into the RFC 0024 convener timer entry the round-trip seam must register in the
// convener's `agents.yaml` `autonomy.timers` set.
//
// This slice is DARK: nothing FIRES these specs yet. It lands the producer half
// (deriving + encoding the timer entry) first, so the PR 7c-ii consumer — the
// `agents.yaml` writer + the convener-side ScheduledWake→POST /convene handler —
// has a tested, drift-guarded contract to build against, exactly as PR 7b-i
// landed the convening-count ceiling before the timer that fires into it.
//
// The two load-bearing invariants under test:
//   - the derived timer id is BOTH agent.schema-valid AND reversible (the fired
//     wake carries only `timer_id`/`callback_kind`, so the channel to convene must
//     be recoverable from the id alone — ScheduledWake has no channel_id field);
//   - the enumerator yields ONLY armed standing channels (a one-shot / disarmed /
//     unconfigured channel gets no timer), so the round-trip never arms a schedule
//     the operator did not declare.

import (
	"os"
	"regexp"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.uber.org/zap"
)

// TestDeriveConveneTimer_ArmedStandingChannel — the happy path: an armed group
// channel with a positive schedule interval + a convener derives the full timer
// spec, keyed on the convener and reversibly encoding the channel id.
func TestDeriveConveneTimer_ArmedStandingChannel(t *testing.T) {
	spec, ok := deriveConveneTimer("group:planning", standingArmed(0))
	require.True(t, ok, "an armed standing channel yields a timer")
	assert.Equal(t, "group:planning", spec.ChannelID)
	assert.Equal(t, "nova-sparrow", spec.ConvenerID, "the timer rides the convener's timer set")
	assert.Equal(t, "convene-planning", spec.TimerID, "the id reversibly encodes the group name")
	assert.Equal(t, StandingConveneKind, spec.Kind)
	assert.Equal(t, "convene", spec.Kind, "the callback_kind the convener-side wake handler branches on")
	assert.Equal(t, 3600, spec.IntervalSeconds, "the schedule interval carries through")
}

// TestDeriveConveneTimer_SkipsNonStanding — the producer yields NO timer for a
// channel that is not an armed standing channel: a one-shot channel (interval 0),
// a disarmed channel, a convener-less block, and a non-group address each derive
// nothing, so the round-trip never registers a schedule the config did not arm.
func TestDeriveConveneTimer_SkipsNonStanding(t *testing.T) {
	oneShot := standingArmed(0)
	oneShot.ScheduleIntervalSeconds = 0 // a one-shot channel, convened manually only.

	disarmed := standingArmed(0)
	disarmed.Enabled = false

	noConvener := standingArmed(0)
	noConvener.Convener = "" // a drifted/forced block that never passed validation.

	cases := []struct {
		name      string
		channelID string
		cfg       AutonomousConfig
	}{
		{"one-shot interval", "group:planning", oneShot},
		{"disarmed", "group:planning", disarmed},
		{"no convener", "group:planning", noConvener},
		{"non-group address", "dm:nova-sparrow:iron-fox", standingArmed(0)},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			_, ok := deriveConveneTimer(tc.channelID, tc.cfg)
			assert.False(t, ok, "%s derives no convener timer", tc.name)
		})
	}
}

// timerIDSchemaPattern mirrors the `autonomy.timers[].id` pattern in
// schemas/agent.schema.json. The producer's encoding MUST stay inside it — a
// timer id the schema rejects would fail agent-config validation the moment the
// round-trip writes it. TestStandingConveneTimerID_SatisfiesSchemaPattern pins
// this copy against the schema file so a schema-side change breaks loudly here.
const timerIDSchemaPattern = `^[a-z0-9][a-z0-9_-]*[a-z0-9]$`

// TestStandingConveneTimerID_SatisfiesSchemaPattern — every derived timer id
// matches the agent.schema timer-id pattern (so the round-trip writes valid
// config), and the pattern this test asserts against is the one the schema
// actually ships (a cross-artifact drift guard, the sibling of the Python
// wire-drift tests).
func TestStandingConveneTimerID_SatisfiesSchemaPattern(t *testing.T) {
	schema, err := os.ReadFile("../../schemas/agent.schema.json")
	require.NoError(t, err)
	assert.Contains(t, string(schema), timerIDSchemaPattern,
		"agent.schema timer-id pattern drifted from the producer's assumption — re-check the convene-timer encoding")

	re := regexp.MustCompile(timerIDSchemaPattern)
	for _, name := range []string{"planning", "weekly-arch-review", "ab", "x9", "a-b-c-2"} {
		id, ok := standingConveneTimerID("group:" + name)
		require.Truef(t, ok, "group:%s is a group address", name)
		assert.Truef(t, re.MatchString(id), "derived timer id %q must satisfy the schema pattern", id)
	}
}

// TestStandingConveneTimerID_RoundTrips — encode then parse is the identity on
// group channel ids (so a fired wake recovers exactly the channel to convene),
// and ParseStandingConveneTimerID rejects a timer id that is not a convene timer
// (the legacy tick, a bare prefix) rather than mis-decoding it into a channel.
func TestStandingConveneTimerID_RoundTrips(t *testing.T) {
	for _, id := range []string{"group:planning", "group:convene-foo", "group:ab"} {
		enc, ok := standingConveneTimerID(id)
		require.Truef(t, ok, "%s encodes", id)
		dec, ok := ParseStandingConveneTimerID(enc)
		require.Truef(t, ok, "%s decodes", enc)
		assert.Equalf(t, id, dec, "encode∘parse is the identity for %s", id)
	}

	for _, notConvene := range []string{"legacy_tick", "reflection", "convene-", "convene", "planning"} {
		_, ok := ParseStandingConveneTimerID(notConvene)
		assert.Falsef(t, ok, "%q is not a convene timer id", notConvene)
	}
}

// TestStandingConveneTimers_EnumeratesArmedStandingOnly — the router-level
// enumerator returns exactly the armed standing channels in the resolved
// autonomous registry, in a deterministic (timer-id sorted) order, skipping the
// one-shot, disarmed, and unconfigured channels.
func TestStandingConveneTimers_EnumeratesArmedStandingOnly(t *testing.T) {
	store := newTestStore(t, SQLiteOptions{})
	router := NewChannelRouter(store, &messageRecordingDispatcher{}, zap.NewNop(), nil)

	standingB := standingArmed(0) // ScheduleIntervalSeconds=3600, convener nova-sparrow.
	standingA := standingArmed(0)

	oneShot := standingArmed(0)
	oneShot.ScheduleIntervalSeconds = 0

	disarmed := standingArmed(0)
	disarmed.Enabled = false

	// Stamped out of timer-id order to prove the enumerator sorts.
	router.SetAutonomous("group:zeta", standingB)
	router.SetAutonomous("group:alpha", standingA)
	router.SetAutonomous("group:oneshot", oneShot)
	router.SetAutonomous("group:disarmed", disarmed)
	// group:unconfigured is never stamped — it resolves to the disabled default.

	specs := router.StandingConveneTimers()

	require.Len(t, specs, 2, "only the two armed standing channels yield timers")
	assert.Equal(t, "convene-alpha", specs[0].TimerID, "sorted by timer id")
	assert.Equal(t, "convene-zeta", specs[1].TimerID)
	assert.Equal(t, "group:alpha", specs[0].ChannelID)
	assert.Equal(t, 3600, specs[0].IntervalSeconds)
}

// TestStandingConveneTimers_EmptyWhenNoStandingChannels — a fleet with no armed
// standing channel derives no timers (the round-trip writes nothing), so the
// dark producer is inert on an ordinary deployment.
func TestStandingConveneTimers_EmptyWhenNoStandingChannels(t *testing.T) {
	router, _ := conveningHarness(t, &messageRecordingDispatcher{}, func() AutonomousConfig {
		a := standingArmed(0)
		a.ScheduleIntervalSeconds = 0 // the harness channel is a one-shot.
		return a
	}())
	assert.Empty(t, router.StandingConveneTimers())
}
