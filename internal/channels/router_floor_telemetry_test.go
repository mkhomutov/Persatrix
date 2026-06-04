package channels

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

// TestFloorRound_Telemetry pins RFC 0030 PR 4: a serialized floor round emits
// the floor-control instruments so the latency cost (D4) and timeout rate (D2)
// are observable. The round below has one responder that replies and one that
// stays silent past a short per-turn timeout, so a single round exercises both
// `outcome` labels and produces exactly one round-duration observation.
func TestFloorRound_Telemetry(t *testing.T) {
	reader := sdkmetric.NewManualReader()
	mp := sdkmetric.NewMeterProvider(sdkmetric.WithReader(reader))
	t.Cleanup(func() { _ = mp.Shutdown(context.Background()) })
	meter := mp.Meter("test")
	floorTurn, err := meter.Int64Counter("channel.conversation.floor_turn")
	require.NoError(t, err)
	roundDur, err := meter.Float64Histogram("channel.conversation.floor_round_duration")
	require.NoError(t, err)

	store := newTestStore(t, SQLiteOptions{})
	// `a` auto-replies; `b` is absent from the reply set, so its turn advances
	// on the per-turn timeout.
	disp := newFloorDispatcher(store, "a")
	router := NewChannelRouter(store, disp, zap.NewNop(), &RouterMetrics{
		FloorTurn:          floorTurn,
		FloorRoundDuration: roundDur,
	})
	disp.router = router

	id := mustCreateGroupWithPolicies(t, store, "planning", map[string]RespondPolicy{
		"user": RespondNever, "a": RespondAlways, "b": RespondAlways,
	}, "user", "a", "b")
	router.SetFloorControl(id, true, 200*time.Millisecond)

	require.NoError(t, router.Publish(context.Background(), ChannelMessage{
		ID: uuid.NewString(), ChannelID: id, SenderID: "user", Content: "kickoff",
	}, ""))

	var rm metricdata.ResourceMetrics
	require.NoError(t, reader.Collect(context.Background(), &rm))

	assert.Equal(t, int64(1), floorTurnCount(t, rm, "group", "replied"),
		"the replying responder records one floor_turn{outcome=replied}")
	assert.Equal(t, int64(1), floorTurnCount(t, rm, "group", "timeout"),
		"the silent responder records one floor_turn{outcome=timeout}")
	assert.Equal(t, uint64(1), floorRoundCount(t, rm, "group"),
		"the round records exactly one round-duration observation")

	// The single observation's value must be plausible: the silent responder
	// alone holds the floor for its full 200ms turn timeout, so the round
	// (timed from floor acquisition) is necessarily >= that, yet far below a
	// minute. The paired bounds pin the millisecond unit conversion — a
	// nanoseconds-as-ms regression would read ~2e8, a seconds-as-ms one ~0.2.
	roundMillis := floorRoundSumMillis(t, rm, "group")
	assert.GreaterOrEqual(t, roundMillis, 150.0,
		"round duration spans at least the silent speaker's turn timeout (~200ms)")
	assert.Less(t, roundMillis, 60000.0,
		"round duration is recorded in milliseconds, not a smaller time unit")
}

// TestFloorRound_Telemetry_NilMetricsSafe pins that a router with no metrics
// handle runs the floor round without panicking — the nil-safe contract every
// other channel instrument honours (NewChannelRouter accepts a nil
// RouterMetrics for unit tests and minimal deployments).
func TestFloorRound_Telemetry_NilMetricsSafe(t *testing.T) {
	store := newTestStore(t, SQLiteOptions{})
	disp := newFloorDispatcher(store, "a", "b")
	router := NewChannelRouter(store, disp, zap.NewNop(), nil)
	disp.router = router

	id := mustCreateGroupWithPolicies(t, store, "planning", map[string]RespondPolicy{
		"user": RespondNever, "a": RespondAlways, "b": RespondAlways,
	}, "user", "a", "b")
	router.SetFloorControl(id, true, 2*time.Second)

	require.NoError(t, router.Publish(context.Background(), ChannelMessage{
		ID: uuid.NewString(), ChannelID: id, SenderID: "user", Content: "kickoff",
	}, ""))
}

// floorTurnCount returns the floor_turn counter value for the given
// channel_type + outcome attribute pair, or 0 if no matching data point exists.
func floorTurnCount(t *testing.T, rm metricdata.ResourceMetrics, channelType, outcome string) int64 {
	t.Helper()
	for _, sm := range rm.ScopeMetrics {
		for _, m := range sm.Metrics {
			if m.Name != "channel.conversation.floor_turn" {
				continue
			}
			sum, ok := m.Data.(metricdata.Sum[int64])
			require.Truef(t, ok, "floor_turn: expected Sum[int64], got %T", m.Data)
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

// floorRoundCount returns the floor_round_duration histogram observation count
// for the given channel_type, or 0 if no matching data point exists.
func floorRoundCount(t *testing.T, rm metricdata.ResourceMetrics, channelType string) uint64 {
	t.Helper()
	for _, sm := range rm.ScopeMetrics {
		for _, m := range sm.Metrics {
			if m.Name != "channel.conversation.floor_round_duration" {
				continue
			}
			hist, ok := m.Data.(metricdata.Histogram[float64])
			require.Truef(t, ok, "floor_round_duration: expected Histogram[float64], got %T", m.Data)
			for _, dp := range hist.DataPoints {
				if ct, _ := dp.Attributes.Value("channel_type"); ct.AsString() == channelType {
					return dp.Count
				}
			}
		}
	}
	return 0
}

// floorRoundSumMillis returns the floor_round_duration histogram Sum for the
// given channel_type, or 0 if no matching data point exists. With a single
// observation per round, the Sum equals that round's recorded duration in ms.
func floorRoundSumMillis(t *testing.T, rm metricdata.ResourceMetrics, channelType string) float64 {
	t.Helper()
	for _, sm := range rm.ScopeMetrics {
		for _, m := range sm.Metrics {
			if m.Name != "channel.conversation.floor_round_duration" {
				continue
			}
			hist, ok := m.Data.(metricdata.Histogram[float64])
			require.Truef(t, ok, "floor_round_duration: expected Histogram[float64], got %T", m.Data)
			for _, dp := range hist.DataPoints {
				if ct, _ := dp.Attributes.Value("channel_type"); ct.AsString() == channelType {
					return dp.Sum
				}
			}
		}
	}
	return 0
}
