package channels

import (
	"context"
	"testing"

	"github.com/google/uuid"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	sdkmetric "go.opentelemetry.io/otel/sdk/metric"
	"go.opentelemetry.io/otel/sdk/metric/metricdata"
	"go.uber.org/zap"
)

// TestChannelRouter_Publish_PublishedCounterTicks pins ISSUE-0013: the
// publish-side counter increments once per accepted publish, regardless
// of fanout outcome — including the all-RespondNever case where no
// `delivered` increment fires. The delivered/published ratio dashboard
// depends on this asymmetry being observable.
func TestChannelRouter_Publish_PublishedCounterTicks(t *testing.T) {
	reader := sdkmetric.NewManualReader()
	mp := sdkmetric.NewMeterProvider(sdkmetric.WithReader(reader))
	t.Cleanup(func() { _ = mp.Shutdown(context.Background()) })
	meter := mp.Meter("test")
	publishedCtr, err := meter.Int64Counter("channel.messages.published")
	require.NoError(t, err)
	deliveredCtr, err := meter.Int64Counter("channel.messages.delivered")
	require.NoError(t, err)

	store := newTestStore(t, SQLiteOptions{})
	disp := &recordingDispatcher{}
	router := NewChannelRouter(store, disp, zap.NewNop(), &RouterMetrics{
		MessagesDelivered: deliveredCtr,
		MessagesPublished: publishedCtr,
	})
	ctx := context.Background()

	// Channel where the only non-sender member is RespondNever — fanout
	// short-circuits, so the delivered counter never ticks. The published
	// counter MUST still tick once.
	id := "group:silent"
	require.NoError(t, store.CreateChannel(ctx, Channel{
		ID: id, Name: "silent", Type: ChannelTypeGroup,
	}))
	require.NoError(t, store.AddMember(ctx, id, "alice", RespondAlways))
	require.NoError(t, store.AddMember(ctx, id, "bob", RespondNever))

	require.NoError(t, router.Publish(ctx, ChannelMessage{
		ID: uuid.NewString(), ChannelID: id, SenderID: "alice", Content: "hi",
	}, ""))
	require.Empty(t, disp.snapshot(), "RespondNever-only fanout produces no dispatch")

	var rm metricdata.ResourceMetrics
	require.NoError(t, reader.Collect(ctx, &rm))
	assertCounterValue(t, rm, "channel.messages.published", "group", 1)
	assertCounterAbsent(t, rm, "channel.messages.delivered")
}

// TestChannelRouter_Publish_PublishedCounter_PerType pins that the label
// set is `channel_type` only — repeated publishes in the same channel
// accumulate on a single data point.
func TestChannelRouter_Publish_PublishedCounter_PerType(t *testing.T) {
	reader := sdkmetric.NewManualReader()
	mp := sdkmetric.NewMeterProvider(sdkmetric.WithReader(reader))
	t.Cleanup(func() { _ = mp.Shutdown(context.Background()) })
	meter := mp.Meter("test")
	publishedCtr, err := meter.Int64Counter("channel.messages.published")
	require.NoError(t, err)

	store := newTestStore(t, SQLiteOptions{})
	router := NewChannelRouter(store, NoopDispatcher{}, zap.NewNop(), &RouterMetrics{
		MessagesPublished: publishedCtr,
	})
	ctx := context.Background()
	id := mustCreateGroup(t, store, "planning", "alice", "bob")

	for i := 0; i < 3; i++ {
		require.NoError(t, router.Publish(ctx, ChannelMessage{
			ID: uuid.NewString(), ChannelID: id, SenderID: "alice", Content: "hi",
		}, ""))
	}

	var rm metricdata.ResourceMetrics
	require.NoError(t, reader.Collect(ctx, &rm))
	assertCounterValue(t, rm, "channel.messages.published", "group", 3)
}

func assertCounterValue(t *testing.T, rm metricdata.ResourceMetrics, name, channelType string, want int64) {
	t.Helper()
	for _, sm := range rm.ScopeMetrics {
		for _, m := range sm.Metrics {
			if m.Name != name {
				continue
			}
			sum, ok := m.Data.(metricdata.Sum[int64])
			require.Truef(t, ok, "metric %s: expected Sum[int64], got %T", name, m.Data)
			for _, dp := range sum.DataPoints {
				v, hasAttr := dp.Attributes.Value("channel_type")
				if hasAttr && v.AsString() == channelType {
					assert.Equal(t, want, dp.Value, "metric %s channel_type=%s", name, channelType)
					return
				}
			}
			t.Fatalf("metric %s: no data point with channel_type=%s", name, channelType)
		}
	}
	t.Fatalf("metric %s not found", name)
}

func assertCounterAbsent(t *testing.T, rm metricdata.ResourceMetrics, name string) {
	t.Helper()
	for _, sm := range rm.ScopeMetrics {
		for _, m := range sm.Metrics {
			if m.Name == name {
				sum, ok := m.Data.(metricdata.Sum[int64])
				if !ok {
					return
				}
				for _, dp := range sum.DataPoints {
					assert.Equalf(t, int64(0), dp.Value, "metric %s expected absent/zero, got %d", name, dp.Value)
				}
				return
			}
		}
	}
}
