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
	"go.uber.org/zap/zapcore"
	"go.uber.org/zap/zaptest/observer"

	"github.com/mkhomutov/persatrix/internal/defaults"
)

// TestChannelRouter_Publish_CascadeDepth_PropagatesUnderCap pins the
// primary enforcement contract from the [RFC 0011 amendment].
// Inbound depths strictly below the cap MUST fanout, and the per-recipient
// dispatch event MUST carry the inbound depth unchanged (no orchestrator
// double-increment — the +1 stays agent-side on outbound).
//
// [RFC 0011 amendment]: ../../docs/rfcs/0011-amendment-cascade-depth-wire-propagation.md
func TestChannelRouter_Publish_CascadeDepth_PropagatesUnderCap(t *testing.T) {
	for _, depth := range []int{0, 1, 4} {
		depth := depth
		t.Run(formatDepthName(depth), func(t *testing.T) {
			router, disp, store := newRouterTest(t)
			ctx := context.Background()
			id := mustCreateGroup(t, store, "planning", "alice", "bob")

			require.NoError(t, router.Publish(ctx, ChannelMessage{
				ID:        uuid.NewString(),
				ChannelID: id,
				SenderID:  "alice",
				Content:   "hi",
				Metadata:  map[string]any{"cascade_depth": depth},
			}, ""))

			calls := disp.snapshot()
			require.Len(t, calls, 1, "fanout to bob, sender filtered")
			assert.Equal(t, depth, calls[0].cascadeDepth,
				"child dispatch must carry inbound depth unchanged (+1 lives agent-side)")
		})
	}
}

// TestChannelRouter_Publish_CascadeDepth_DropsAtCap pins the drop-side of
// the contract: inbound depths at-or-above the cap MUST suppress fanout.
// The publish itself remains successful — only the cascade is capped.
func TestChannelRouter_Publish_CascadeDepth_DropsAtCap(t *testing.T) {
	for _, depth := range []int{defaults.DefaultMaxCascadeDepth, defaults.DefaultMaxCascadeDepth + 1} {
		depth := depth
		t.Run(formatDepthName(depth), func(t *testing.T) {
			router, disp, store := newRouterTest(t)
			ctx := context.Background()
			id := mustCreateGroup(t, store, "planning", "alice", "bob")

			err := router.Publish(ctx, ChannelMessage{
				ID:        uuid.NewString(),
				ChannelID: id,
				SenderID:  "alice",
				Content:   "hi",
				Metadata:  map[string]any{"cascade_depth": depth},
			}, "")
			require.NoError(t, err, "cascade-cap drops the cascade but not the publish")
			assert.Empty(t, disp.snapshot(),
				"fanout MUST be suppressed at depth >= cap (no per-recipient dispatch)")
		})
	}
}

// TestChannelRouter_Publish_CascadeDepth_ClampedOverCap pins the clamp on
// over-cap inbound: a publisher claiming cascade_depth=99 is clamped down
// to max_cascade_depth and then the drop check fires. This is the defense
// against over-cap poisoning where a misbehaving publisher tries to make
// the orchestrator skip the drop check or push a downstream branch past
// the cap on a fanout it dislikes.
func TestChannelRouter_Publish_CascadeDepth_ClampedOverCap(t *testing.T) {
	router, disp, store := newRouterTest(t)
	ctx := context.Background()
	id := mustCreateGroup(t, store, "planning", "alice", "bob")

	err := router.Publish(ctx, ChannelMessage{
		ID:        uuid.NewString(),
		ChannelID: id,
		SenderID:  "alice",
		Content:   "hi",
		Metadata:  map[string]any{"cascade_depth": 99},
	}, "")
	require.NoError(t, err)
	assert.Empty(t, disp.snapshot(),
		"99 clamped to cap; drop check then fires — fanout suppressed")
}

// TestChannelRouter_Publish_CascadeDepth_MissingDefaultsToZero pins the
// implicit-zero contract: a publish that omits the metadata key (or
// supplies no metadata at all) is treated as depth=0, the chain's origin.
func TestChannelRouter_Publish_CascadeDepth_MissingDefaultsToZero(t *testing.T) {
	router, disp, store := newRouterTest(t)
	ctx := context.Background()
	id := mustCreateGroup(t, store, "planning", "alice", "bob")

	require.NoError(t, router.Publish(ctx, ChannelMessage{
		ID: uuid.NewString(), ChannelID: id, SenderID: "alice", Content: "hi",
	}, ""))

	calls := disp.snapshot()
	require.Len(t, calls, 1)
	assert.Equal(t, 0, calls[0].cascadeDepth, "absent metadata key MUST default to 0")
}

// TestChannelRouter_Publish_CascadeDepth_AcceptsFloat64Repr pins the
// JSON-decoded shape: `json.Unmarshal` into `map[string]any` yields
// `float64` for every numeric, so the router MUST accept `float64(3)`
// equivalently to `int(3)`. Without this, REST publishes through the
// orchestrator handler — which decodes via `map[string]any` — would
// silently fall back to depth=0 and the cap would never fire.
func TestChannelRouter_Publish_CascadeDepth_AcceptsFloat64Repr(t *testing.T) {
	router, disp, store := newRouterTest(t)
	ctx := context.Background()
	id := mustCreateGroup(t, store, "planning", "alice", "bob")

	require.NoError(t, router.Publish(ctx, ChannelMessage{
		ID:        uuid.NewString(),
		ChannelID: id,
		SenderID:  "alice",
		Content:   "hi",
		Metadata:  map[string]any{"cascade_depth": float64(3)},
	}, ""))

	calls := disp.snapshot()
	require.Len(t, calls, 1)
	assert.Equal(t, 3, calls[0].cascadeDepth,
		"float64 cascade_depth (JSON-decoded shape) MUST be accepted as the int value")
}

// TestChannelRouter_Publish_CascadeDepth_CappedEmitsWarnLog pins the
// observability contract: a cap-drop MUST emit a structured Warn line
// with channel_id, sender_id, and depth so operators can correlate a
// cascade-cap event back to its source publish.
func TestChannelRouter_Publish_CascadeDepth_CappedEmitsWarnLog(t *testing.T) {
	core, recorded := observer.New(zapcore.WarnLevel)
	logger := zap.New(core)

	store := newTestStore(t, SQLiteOptions{})
	router := NewChannelRouter(store, &recordingDispatcher{}, logger, nil)
	ctx := context.Background()
	id := mustCreateGroup(t, store, "planning", "alice", "bob")

	require.NoError(t, router.Publish(ctx, ChannelMessage{
		ID:        uuid.NewString(),
		ChannelID: id,
		SenderID:  "alice",
		Content:   "hi",
		Metadata:  map[string]any{"cascade_depth": 5},
	}, ""))

	logs := recorded.FilterMessageSnippet("cascade limit reached").All()
	require.Len(t, logs, 1,
		"cap-drop MUST produce exactly one Warn (saw %d entries)", len(logs))
	fields := logs[0].ContextMap()
	assert.Equal(t, id, fields["channel_id"])
	assert.Equal(t, "alice", fields["sender_id"])
	assert.EqualValues(t, 5, fields["depth"])
}

// TestChannelRouter_Publish_CascadeDepth_CappedTicksCounter pins the
// metric contract: `channel.messages.cascade_capped{channel_type}`
// increments by the number of suppressed per-recipient dispatches.
// "Number of suppressed fanouts" is what makes this counter directly
// comparable to the `delivered` counter; if it were a simple per-publish
// boolean a fully populated channel and a 2-member channel would emit
// the same signal.
func TestChannelRouter_Publish_CascadeDepth_CappedTicksCounter(t *testing.T) {
	reader := sdkmetric.NewManualReader()
	mp := sdkmetric.NewMeterProvider(sdkmetric.WithReader(reader))
	t.Cleanup(func() { _ = mp.Shutdown(context.Background()) })
	meter := mp.Meter("test")
	cappedCtr, err := meter.Int64Counter("channel.messages.cascade_capped")
	require.NoError(t, err)
	deliveredCtr, err := meter.Int64Counter("channel.messages.delivered")
	require.NoError(t, err)

	store := newTestStore(t, SQLiteOptions{})
	router := NewChannelRouter(store, &recordingDispatcher{}, zap.NewNop(), &RouterMetrics{
		MessagesCascadeCapped: cappedCtr,
		MessagesDelivered:     deliveredCtr,
	})
	ctx := context.Background()

	// 1 sender + 2 eligible (always) + 1 RespondNever (filtered). The
	// suppressed count MUST be 2 — the two recipients that would have
	// received a dispatch had the publish not been capped.
	id := "group:planning"
	require.NoError(t, store.CreateChannel(ctx, Channel{
		ID: id, Name: "planning", Type: ChannelTypeGroup,
	}))
	require.NoError(t, store.AddMember(ctx, id, "alice", RespondAlways))
	require.NoError(t, store.AddMember(ctx, id, "bob", RespondAlways))
	require.NoError(t, store.AddMember(ctx, id, "carol", RespondAlways))
	require.NoError(t, store.AddMember(ctx, id, "silent", RespondNever))

	require.NoError(t, router.Publish(ctx, ChannelMessage{
		ID:        uuid.NewString(),
		ChannelID: id,
		SenderID:  "alice",
		Content:   "hi",
		Metadata:  map[string]any{"cascade_depth": 5},
	}, ""))

	var rm metricdata.ResourceMetrics
	require.NoError(t, reader.Collect(ctx, &rm))
	assertCounterValue(t, rm, "channel.messages.cascade_capped", "group", 2)
	// Dispatch was suppressed entirely, so the delivered counter does
	// NOT tick. The two counters are complementary on a cap-drop.
	assertCounterAbsent(t, rm, "channel.messages.delivered")
}

// TestChannelRouter_Publish_CascadeDepth_NotCapped_NoCappedCounter pins
// that the cascade_capped counter does NOT tick on a normal publish.
// Under-cap traffic must read clean on the cap-drop dashboard.
func TestChannelRouter_Publish_CascadeDepth_NotCapped_NoCappedCounter(t *testing.T) {
	reader := sdkmetric.NewManualReader()
	mp := sdkmetric.NewMeterProvider(sdkmetric.WithReader(reader))
	t.Cleanup(func() { _ = mp.Shutdown(context.Background()) })
	meter := mp.Meter("test")
	cappedCtr, err := meter.Int64Counter("channel.messages.cascade_capped")
	require.NoError(t, err)

	store := newTestStore(t, SQLiteOptions{})
	router := NewChannelRouter(store, &recordingDispatcher{}, zap.NewNop(), &RouterMetrics{
		MessagesCascadeCapped: cappedCtr,
	})
	ctx := context.Background()
	id := mustCreateGroup(t, store, "planning", "alice", "bob")

	require.NoError(t, router.Publish(ctx, ChannelMessage{
		ID:        uuid.NewString(),
		ChannelID: id,
		SenderID:  "alice",
		Content:   "hi",
		Metadata:  map[string]any{"cascade_depth": 2},
	}, ""))

	var rm metricdata.ResourceMetrics
	require.NoError(t, reader.Collect(ctx, &rm))
	assertCounterAbsent(t, rm, "channel.messages.cascade_capped")
}

// TestChannelRouter_Publish_CascadeDepth_PersistsClampedValue pins that
// the stored message reflects the clamped depth, not the original
// (poisoned) inbound. A future operator reading channel history via
// `GET /api/v1/channels/{id}/messages` MUST see cascade_depth=5 rather
// than 99 on a capped publish — otherwise the history surface lies
// about what the orchestrator actually enforced.
func TestChannelRouter_Publish_CascadeDepth_PersistsClampedValue(t *testing.T) {
	router, _, store := newRouterTest(t)
	ctx := context.Background()
	id := mustCreateGroup(t, store, "planning", "alice", "bob")

	msgID := uuid.NewString()
	require.NoError(t, router.Publish(ctx, ChannelMessage{
		ID:        msgID,
		ChannelID: id,
		SenderID:  "alice",
		Content:   "hi",
		Metadata:  map[string]any{"cascade_depth": 99},
	}, ""))

	stored, err := store.GetMessage(ctx, msgID)
	require.NoError(t, err)
	require.NotNil(t, stored.Metadata)
	assert.EqualValues(t, defaults.DefaultMaxCascadeDepth, asInt(stored.Metadata["cascade_depth"]),
		"stored cascade_depth MUST reflect the clamped value, not the publisher's claim")
}

// asInt collects the int representation from any JSON-decoded numeric
// value (int / int64 / float64). Returns 0 for nil so a `map[string]any`
// lookup miss reads cleanly without each callsite checking for absence
// first — mirrors the production path's "absent → 0" semantic from
// [readCascadeDepth].
func asInt(v any) int {
	switch n := v.(type) {
	case nil:
		return 0
	case int:
		return n
	case int32:
		return int(n)
	case int64:
		return int(n)
	case float64:
		return int(n)
	case float32:
		return int(n)
	}
	return 0
}

func formatDepthName(d int) string {
	switch d {
	case 0:
		return "depth_0"
	case 1:
		return "depth_1"
	case 4:
		return "depth_4_just_under_cap"
	case defaults.DefaultMaxCascadeDepth:
		return "depth_at_cap"
	default:
		if d > defaults.DefaultMaxCascadeDepth {
			return "depth_over_cap"
		}
		return "depth_under_cap"
	}
}
