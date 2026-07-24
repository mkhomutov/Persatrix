package channels

// interaction_cap_utilization_test.go — ISSUE-0109 (RFC 0052 OQ #5) cap-
// utilization telemetry. Pins the `interaction_cap_utilization` half of the
// centralized close funnel ([ChannelRouter.recordInteractionClosedMetric]):
// a CAPPED interaction's close — any trigger — records spend-at-close as a
// fraction of `interaction_budget_tokens`, labelled by `channel_type` and
// `trigger`; an uncapped close, or a router with no wallet wired, records
// nothing (no denominator / no numerator — never a fabricated sample). The
// harness mirrors bounded_close_test.go's: a floor-controlled autonomous
// group whose personas never reply, so each operator publish is exactly one
// stalled round and the bounded close fires deterministically.

import (
	"context"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.opentelemetry.io/otel/attribute"
	sdkmetric "go.opentelemetry.io/otel/sdk/metric"
	"go.opentelemetry.io/otel/sdk/metric/metricdata"
	"go.uber.org/zap"
)

// capUtilizationHarness is boundedCloseHarness with the ISSUE-0109 histogram
// wired alongside the close counter — no escalation chair, so the bounded
// close takes the immediate artifact-bearing path and the funnel fires inside
// the closing publish.
func capUtilizationHarness(t *testing.T, maxRounds int) (*ChannelRouter, string, *sdkmetric.ManualReader) {
	t.Helper()
	reader := sdkmetric.NewManualReader()
	mp := sdkmetric.NewMeterProvider(sdkmetric.WithReader(reader))
	t.Cleanup(func() { _ = mp.Shutdown(context.Background()) })
	ctr, err := mp.Meter("test").Int64Counter("channel.conversation.interaction_closed")
	require.NoError(t, err)
	hist, err := mp.Meter("test").Float64Histogram("channel.conversation.interaction_cap_utilization")
	require.NoError(t, err)
	store := newTestStore(t, SQLiteOptions{})
	router := NewChannelRouter(store, &envelopeRecorder{}, zap.NewNop(), &RouterMetrics{
		InteractionClosed:         ctr,
		InteractionCapUtilization: hist,
	})
	ch := mustCreateGroupWithPolicies(t, store, "brainstorm",
		map[string]RespondPolicy{
			"operator":  RespondNever, // the stimulus author (no seat in the discussion)
			"ember-owl": RespondAlways,
			"iron-fox":  RespondAlways,
		}, "operator", "ember-owl", "iron-fox")
	router.SetFloorControl(ch, true, time.Millisecond)
	router.SetAutonomous(ch, AutonomousConfig{Enabled: true, MaxRounds: maxRounds, Convener: "ember-owl"})
	return router, ch, reader
}

// capUtilizationPoints collects the histogram's data points (nil when the
// instrument never recorded).
func capUtilizationPoints(t *testing.T, reader *sdkmetric.ManualReader) []metricdata.HistogramDataPoint[float64] {
	t.Helper()
	var rm metricdata.ResourceMetrics
	require.NoError(t, reader.Collect(context.Background(), &rm))
	for _, sm := range rm.ScopeMetrics {
		for _, m := range sm.Metrics {
			if m.Name != "channel.conversation.interaction_cap_utilization" {
				continue
			}
			h, ok := m.Data.(metricdata.Histogram[float64])
			require.Truef(t, ok, "expected Histogram[float64], got %T", m.Data)
			return h.DataPoints
		}
	}
	return nil
}

// TestCapUtilization_CostCloseRecordsFraction — a soft-budget (trigger=cost)
// close on a capped interaction records spend/cap with the cost label.
func TestCapUtilization_CostCloseRecordsFraction(t *testing.T) {
	router, ch, reader := capUtilizationHarness(t, 100) // round bound out of reach
	router.SetInteractionBudgetTokens(ch, 100_000)
	router.SetInteractionSpender(fakeSpender{v: 90_000}) // >= soft (cap − reserve)

	tick(t, router, ch) // first round: spend already over the soft budget

	points := capUtilizationPoints(t, reader)
	require.Len(t, points, 1, "one capped close, one sample")
	dp := points[0]
	assert.Equal(t, uint64(1), dp.Count)
	assert.InDelta(t, 0.9, dp.Sum, 1e-9, "90k spend of a 100k cap")
	trigger, ok := dp.Attributes.Value(attribute.Key("trigger"))
	require.True(t, ok)
	assert.Equal(t, costTrigger, trigger.AsString())
	ct, ok := dp.Attributes.Value(attribute.Key("channel_type"))
	require.True(t, ok)
	assert.Equal(t, "group", ct.AsString())
}

// TestCapUtilization_StructuralCloseRecordsFraction — a max_rounds
// (trigger=structural) close records the same series: the calibration reads
// how much of the cap a converged arc used, not only the cost-bound ones.
func TestCapUtilization_StructuralCloseRecordsFraction(t *testing.T) {
	router, ch, reader := capUtilizationHarness(t, 2)
	router.SetInteractionBudgetTokens(ch, 200_000)
	router.SetInteractionSpender(fakeSpender{v: 50_000}) // well under the soft budget

	tick(t, router, ch)
	tick(t, router, ch) // the 2nd round hits max_rounds

	points := capUtilizationPoints(t, reader)
	require.Len(t, points, 1)
	dp := points[0]
	assert.InDelta(t, 0.25, dp.Sum, 1e-9, "50k spend of a 200k cap")
	trigger, ok := dp.Attributes.Value(attribute.Key("trigger"))
	require.True(t, ok)
	assert.Equal(t, structuralTrigger, trigger.AsString())
}

// TestCapUtilization_UncappedCloseRecordsNothing — no cap, no denominator: the
// close counter still ticks, the utilization series stays silent.
func TestCapUtilization_UncappedCloseRecordsNothing(t *testing.T) {
	router, ch, reader := capUtilizationHarness(t, 2)
	router.SetInteractionSpender(fakeSpender{v: 50_000}) // wallet wired, channel uncapped

	tick(t, router, ch)
	tick(t, router, ch)

	var rm metricdata.ResourceMetrics
	require.NoError(t, reader.Collect(context.Background(), &rm))
	assert.Equal(t, int64(1), interactionClosedCount(t, rm, "group", structuralTrigger),
		"the structural close itself fired")
	assert.Empty(t, capUtilizationPoints(t, reader), "no cap → no utilization sample")
}

// capUtilizationPlainRouter builds a router with BOTH close instruments over an
// ORDINARY (non-autonomous) group, so the end-vote and idle close paths can be
// driven directly — the two triggers [capUtilizationHarness]'s floor-controlled
// autonomous roster cannot reach. The funnel is shared, but each trigger takes a
// different route to it, and only these two run outside the bounded-close path.
func capUtilizationPlainRouter(t *testing.T) (*ChannelRouter, ChannelStore, *sdkmetric.ManualReader) {
	t.Helper()
	reader := sdkmetric.NewManualReader()
	mp := sdkmetric.NewMeterProvider(sdkmetric.WithReader(reader))
	t.Cleanup(func() { _ = mp.Shutdown(context.Background()) })
	ctr, err := mp.Meter("test").Int64Counter("channel.conversation.interaction_closed")
	require.NoError(t, err)
	hist, err := mp.Meter("test").Float64Histogram("channel.conversation.interaction_cap_utilization")
	require.NoError(t, err)
	store := newTestStore(t, SQLiteOptions{})
	router := NewChannelRouter(store, &recordingDispatcher{}, zap.NewNop(), &RouterMetrics{
		InteractionClosed:         ctr,
		InteractionCapUtilization: hist,
	})
	return router, store, reader
}

// TestCapUtilization_EndVoteCloseRecordsFraction — the quorum (trigger=end_votes)
// close records the same series. [ChannelRouter.recordInteractionClosed] fires
// ahead of the [ChannelRouter.finalizeInteractionClose] discard tail, so the
// budget/spend read still resolves.
func TestCapUtilization_EndVoteCloseRecordsFraction(t *testing.T) {
	router, store, reader := capUtilizationPlainRouter(t)
	ch := mustCreateGroup(t, store, "planning", "alice", "bob", "carol")
	router.SetEndVoteParams(ch, 2, 3)
	router.SetInteractionBudgetTokens(ch, 100_000)
	router.SetInteractionSpender(fakeSpender{v: 40_000})

	require.NoError(t, endVote(t, router, ch, "alice", "int-1"))
	require.NoError(t, endVote(t, router, ch, "bob", "int-1")) // K=2 reached

	points := capUtilizationPoints(t, reader)
	require.Len(t, points, 1, "the end-vote close records exactly one sample")
	dp := points[0]
	assert.InDelta(t, 0.4, dp.Sum, 1e-9, "40k spend of a 100k cap")
	trigger, ok := dp.Attributes.Value(attribute.Key("trigger"))
	require.True(t, ok)
	assert.Equal(t, endVotesTrigger, trigger.AsString())
}

// TestCapUtilization_IdleCloseRecordsFraction — the lazy idle rotation
// (trigger=idle) records too. This is the funnel's subtlest caller: the close is
// emitted one publish AFTER the semantic close, so the sample depends on IP4's
// one-generation grace (rotating away from an id does not discard its governance
// state). If that grace ever regresses the resolve would miss and the sample
// would be SKIPPED SILENTLY — an idle-shaped hole in the calibration series with
// no error — which is precisely why it is pinned here.
func TestCapUtilization_IdleCloseRecordsFraction(t *testing.T) {
	router, store, reader := capUtilizationPlainRouter(t)
	now := time.Date(2026, 6, 10, 12, 0, 0, 0, time.UTC)
	router.interactionNow = func() time.Time { return now }
	ch := mustCreateGroupWithPolicies(t, store, "planning",
		map[string]RespondPolicy{"ember-owl": RespondAlways, "iron-fox": RespondAlways},
		"ember-owl", "iron-fox")
	router.SetInteractionBudgetTokens(ch, 200_000)
	router.SetInteractionSpender(fakeSpender{v: 60_000})

	first := publishAndGetInteractionID(t, router, store, ch, "ember-owl", nil)
	now = now.Add(601 * time.Second) // past the 600s idle window
	second := publishAndGetInteractionID(t, router, store, ch, "iron-fox", nil)
	require.NotEqual(t, first, second, "the publish past the window rotated the interaction")

	points := capUtilizationPoints(t, reader)
	require.Len(t, points, 1,
		"the idle rotation still resolves the retiring interaction's budget (IP4 one-generation grace)")
	dp := points[0]
	assert.InDelta(t, 0.3, dp.Sum, 1e-9, "60k spend of a 200k cap")
	trigger, ok := dp.Attributes.Value(attribute.Key("trigger"))
	require.True(t, ok)
	assert.Equal(t, idleTrigger, trigger.AsString())
}

// TestCapUtilization_NoWalletRecordsNothing — a capped channel on a router
// with no wallet wired (r.spend nil) has no numerator; the sample is skipped
// rather than fabricated as zero.
func TestCapUtilization_NoWalletRecordsNothing(t *testing.T) {
	router, ch, reader := capUtilizationHarness(t, 2)
	router.SetInteractionBudgetTokens(ch, 200_000)
	// no SetInteractionSpender — r.spend is nil.

	tick(t, router, ch)
	tick(t, router, ch)

	var rm metricdata.ResourceMetrics
	require.NoError(t, reader.Collect(context.Background(), &rm))
	assert.Equal(t, int64(1), interactionClosedCount(t, rm, "group", structuralTrigger),
		"the structural close itself fired")
	assert.Empty(t, capUtilizationPoints(t, reader), "no wallet → no utilization sample")
}
