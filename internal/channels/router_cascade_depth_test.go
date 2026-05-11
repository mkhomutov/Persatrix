package channels

import (
	"context"
	"errors"
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

// failingMembersStore wraps a real ChannelStore and injects a synthetic
// error on `GetMembers` while leaving every other method intact. Used
// by [TestChannelRouter_Publish_CascadeDepth_CappedWarn_MemberLookupFailure]
// to exercise the cap-drop observability path when the recipient-count
// lookup fails — a path that a real SQLite store cannot reach in unit
// tests without injecting fault from outside the package.
type failingMembersStore struct {
	ChannelStore
	membersErr error
}

func (s *failingMembersStore) GetMembers(ctx context.Context, channelID string) ([]Member, error) {
	if s.membersErr != nil {
		return nil, s.membersErr
	}
	return s.ChannelStore.GetMembers(ctx, channelID)
}

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
	// Happy path MUST carry `suppressed_recipients` so the dashboard's
	// cap-rate-by-channel derivation has a numerator. The failure-branch
	// shape (member lookup failed) is pinned by the sibling test below.
	_, hasSuppressed := fields["suppressed_recipients"]
	assert.Truef(t, hasSuppressed,
		"happy-path cap-drop Warn MUST include suppressed_recipients (got fields: %v)", fields)
}

// TestChannelRouter_Publish_CascadeDepth_CappedWarn_MemberLookupFailure
// pins the observability accuracy contract on the cap-drop path when
// the recipient-count lookup itself fails. PR #319 deep review M2: the
// previous implementation initialised `suppressed := 0` and emitted
// the Warn line with `zap.Int("suppressed_recipients", 0)` regardless
// of whether the lookup succeeded — a fabricated zero that lied to
// operators investigating a cascade-cap event. The fix branches the
// Warn shape so the failure path omits the field (and surfaces the
// underlying error on the same Warn for correlation) rather than
// reporting a zero that is indistinguishable from "every recipient
// was filtered upstream".
//
// "Lie of omission > lie of commission" — operators reading a Warn
// with no `suppressed_recipients` immediately know to dig deeper;
// operators reading `suppressed_recipients=0` reasonably conclude
// the cap-drop affected nobody and move on.
func TestChannelRouter_Publish_CascadeDepth_CappedWarn_MemberLookupFailure(t *testing.T) {
	core, recorded := observer.New(zapcore.WarnLevel)
	logger := zap.New(core)

	base := newTestStore(t, SQLiteOptions{})
	ctx := context.Background()
	id := mustCreateGroup(t, base, "planning", "alice", "bob")

	// Wrap the store AFTER channel setup so the membership rows are
	// committed; the wrapper then injects a synthetic GetMembers error
	// for the duration of the publish that follows. This mirrors the
	// real-world race window the M2 finding called out: PublishMessage
	// just succeeded (so membership existed at commit time), but a
	// transient store fault between commit and recordCascadeCap means
	// the recipient count cannot be retrieved.
	wantErr := errors.New("synthetic store fault for member lookup")
	store := &failingMembersStore{ChannelStore: base, membersErr: wantErr}
	router := NewChannelRouter(store, &recordingDispatcher{}, logger, nil)

	require.NoError(t, router.Publish(ctx, ChannelMessage{
		ID:        uuid.NewString(),
		ChannelID: id,
		SenderID:  "alice",
		Content:   "hi",
		Metadata:  map[string]any{"cascade_depth": 5},
	}, ""))

	logs := recorded.FilterMessageSnippet("cascade limit reached").All()
	require.Len(t, logs, 1,
		"cap-drop MUST still produce exactly one Warn even when member lookup fails")
	fields := logs[0].ContextMap()
	assert.Equal(t, id, fields["channel_id"])
	assert.Equal(t, "alice", fields["sender_id"])
	assert.EqualValues(t, 5, fields["depth"])

	// The Warn line MUST NOT carry suppressed_recipients on the
	// lookup-failure branch — a fabricated zero would be
	// indistinguishable from "every recipient was filtered upstream",
	// which is exactly the wrong signal for triage.
	_, hasSuppressed := fields["suppressed_recipients"]
	assert.Falsef(t, hasSuppressed,
		"failure-branch Warn MUST omit suppressed_recipients (got fields: %v)", fields)

	// Failure context MUST be present on the same Warn for correlation.
	// Previously this lived only in a Debug log that production
	// operators would not see; the cap-rate dashboard would show a
	// drop with no explanation. Promoting the error into the cap-drop
	// Warn (under a distinct field name so the happy-path log shape
	// stays clean) is the smallest change that gives operators
	// actionable signal.
	errField, hasErrField := fields["recipient_lookup_error"]
	require.Truef(t, hasErrField,
		"failure-branch Warn MUST carry recipient_lookup_error for correlation (got fields: %v)", fields)
	assert.Contains(t, errField, "synthetic store fault for member lookup",
		"recipient_lookup_error MUST surface the underlying store error")
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

// TestChannelRouter_SetMaxCascadeDepth_IgnoresNonPositive pins the
// operator-load-bearing guard on [ChannelRouter.SetMaxCascadeDepth]:
// a zero or negative override is silently ignored so the backstop
// cannot be disabled from config. PR #319 deep review L1 noted this
// contract was untested directly — the existing default-cap and
// override paths exercised positive values only, so a future refactor
// that dropped the `if d > 0` guard ("simplifying" to
// `r.maxCascadeDepth = d`) would regress without flipping a single
// test red and would silently un-cap every deployment whose
// `channels.yaml` happens to leave the row at the placeholder zero.
//
// The CHANGELOG entry and `docs/guides/channels.md` both make the
// "non-positive is ignored" claim explicit; this test pins the claim
// in code so it stops being inference from the README.
func TestChannelRouter_SetMaxCascadeDepth_IgnoresNonPositive(t *testing.T) {
	router, _, _ := newRouterTest(t)
	require.Equal(t, defaults.DefaultMaxCascadeDepth, router.MaxCascadeDepth(),
		"NewChannelRouter MUST initialise to the orchestrator default")

	router.SetMaxCascadeDepth(0)
	assert.Equal(t, defaults.DefaultMaxCascadeDepth, router.MaxCascadeDepth(),
		"zero override MUST be ignored — backstop cannot be silently disabled")

	router.SetMaxCascadeDepth(-3)
	assert.Equal(t, defaults.DefaultMaxCascadeDepth, router.MaxCascadeDepth(),
		"negative override MUST be ignored — backstop cannot be silently disabled")

	router.SetMaxCascadeDepth(7)
	assert.Equal(t, 7, router.MaxCascadeDepth(),
		"positive override MUST apply — operator-tightening path must remain functional")

	// Pin the asymmetry: a positive override followed by a non-positive
	// MUST keep the positive value, not revert. A future
	// "if d != current { r.maxCascadeDepth = d }" refactor would pass
	// every other assertion above but fail this one.
	router.SetMaxCascadeDepth(0)
	assert.Equal(t, 7, router.MaxCascadeDepth(),
		"non-positive override after a positive one MUST NOT clobber the previously applied cap")
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
