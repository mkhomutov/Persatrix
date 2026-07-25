// classification_wire_test.go — RFC 0037 §B (v0.3.12 PR 2): classification
// on the wire. Pins the four legs of the dispatch-side contract:
//
//  1. the proto field round-trips (and stays proto3-implicit — an old
//     producer's silence is representable);
//  2. the store persists, scans, updates, and default-fills the `Channel`
//     plumbing the PR 1 note deferred here;
//  3. the reconcile threads a declared level at create AND adopts it onto a
//     pre-existing row (the 0037-plan "PR 2 note — existing rows" debt);
//  4. every dispatch envelope carries the row's level, served through the
//     router's read-through cache — including after a reconcile adoption
//     refreshed it mid-run.
//
// The tests declare levels ABOVE the item-8 dark-window ceiling on purpose:
// the ceiling is Config.Validate's operator-boundary guard (pinned in
// config_classification_test.go), while these exercise the plumbing beneath
// it — the same rows PR 4's gate will read once the guard is deleted.
package channels

import (
	"context"
	"testing"

	"github.com/google/uuid"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.uber.org/zap"
	"google.golang.org/protobuf/proto"

	"github.com/mkhomutov/persatrix/internal/generated/taskpb"
)

// TestChannelMessageEvent_ClassificationRoundTrips pins field 31: a set
// value survives marshal/unmarshal, and an unset value marshals to nothing —
// proto3 implicit presence, so a pre-v0.3.12 producer's event is
// indistinguishable from an explicit empty and resolves to the §A rule-(b)
// `public` floor at the receiver, never `internal`.
func TestChannelMessageEvent_ClassificationRoundTrips(t *testing.T) {
	original := &taskpb.ChannelMessageEvent{
		MessageId:      "msg-cls-1",
		ChannelId:      "group:leadership",
		Classification: "restricted",
	}
	blob, err := proto.Marshal(original)
	require.NoError(t, err)
	decoded := &taskpb.ChannelMessageEvent{}
	require.NoError(t, proto.Unmarshal(blob, decoded))
	assert.Equal(t, "restricted", decoded.Classification)

	unset := &taskpb.ChannelMessageEvent{MessageId: "msg-cls-2", ChannelId: "group:eng"}
	blob, err = proto.Marshal(unset)
	require.NoError(t, err)
	decoded = &taskpb.ChannelMessageEvent{}
	require.NoError(t, proto.Unmarshal(blob, decoded))
	assert.Empty(t, decoded.Classification,
		"an unset classification must decode to the empty string (the legacy-producer shape)")
}

// TestChannelMessageToProto_LiftsClassification pins the dispatcher
// translation: the envelope's stamp rides the typed field verbatim,
// including the empty resolve-failure value (fail-closed by omission).
func TestChannelMessageToProto_LiftsClassification(t *testing.T) {
	d := &GRPCMessageDispatcher{logger: zap.NewNop()}

	ev := d.channelMessageToProto(
		ChannelMessage{ID: "m-1", ChannelID: "group:leadership", SenderID: "a"},
		DispatchEnvelope{
			Recipient:      Member{ParticipantID: "b", RespondPolicy: RespondAlways},
			Classification: "restricted",
		})
	assert.Equal(t, "restricted", ev.Classification)

	ev = d.channelMessageToProto(
		ChannelMessage{ID: "m-2", ChannelID: "group:leadership", SenderID: "a"},
		DispatchEnvelope{Recipient: Member{ParticipantID: "b", RespondPolicy: RespondAlways}})
	assert.Empty(t, ev.Classification,
		"a failed row resolve must ride the wire as empty — the receiver floors to public")
}

// TestSQLiteStore_ChannelClassificationPlumbing pins the PR 1 note's
// deferred `Channel` plumbing: a declared level persists through both create
// paths and scans back on both read paths; an absent level default-fills
// `internal` (§A rule (a), the store-boundary rewrite).
func TestSQLiteStore_ChannelClassificationPlumbing(t *testing.T) {
	store := newTestStore(t, SQLiteOptions{})
	ctx := context.Background()

	require.NoError(t, store.CreateChannel(ctx, Channel{
		ID: "group:leadership", Name: "leadership", Type: ChannelTypeGroup,
		Classification: ClassificationRestricted,
	}))
	require.NoError(t, store.CreateChannelWithMembers(ctx, Channel{
		ID: "group:announce", Name: "announce", Type: ChannelTypeGroup,
		Classification: ClassificationPublic,
	}, []Member{{ParticipantID: "alice", RespondPolicy: RespondAlways}}))
	require.NoError(t, store.CreateChannel(ctx, Channel{
		ID: "group:adhoc", Name: "adhoc", Type: ChannelTypeGroup,
		// Classification deliberately absent — the REST-create shape.
	}))

	ch, err := store.GetChannel(ctx, "group:leadership")
	require.NoError(t, err)
	assert.Equal(t, ClassificationRestricted, ch.Classification)

	ch, err = store.GetChannel(ctx, "group:announce")
	require.NoError(t, err)
	assert.Equal(t, ClassificationPublic, ch.Classification)

	ch, err = store.GetChannel(ctx, "group:adhoc")
	require.NoError(t, err)
	assert.Equal(t, DefaultClassification, ch.Classification,
		"a classification-unaware create must stamp `internal` (§A rule (a)), never empty or public")

	byID := map[string]Classification{}
	list, err := store.ListChannels(ctx, 0, "")
	require.NoError(t, err)
	for _, c := range list {
		byID[c.ID] = c.Classification
	}
	assert.Equal(t, map[string]Classification{
		"group:leadership": ClassificationRestricted,
		"group:announce":   ClassificationPublic,
		"group:adhoc":      ClassificationInternal,
	}, byID, "ListChannels must scan the same column GetChannel does")
}

// TestSQLiteStore_SetChannelClassification pins the adoption/reclassification
// primitive: the row updates in place, an out-of-lattice level is rejected
// (no silent rule-(a) rewrite on the UPDATE path), and a missing channel is
// a not-found.
func TestSQLiteStore_SetChannelClassification(t *testing.T) {
	store := newTestStore(t, SQLiteOptions{})
	ctx := context.Background()
	mustCreateGroup(t, store, "planning")

	require.NoError(t, store.SetChannelClassification(ctx, "group:planning", ClassificationSecret))
	ch, err := store.GetChannel(ctx, "group:planning")
	require.NoError(t, err)
	assert.Equal(t, ClassificationSecret, ch.Classification)

	err = store.SetChannelClassification(ctx, "group:planning", Classification("cosmic"))
	assert.ErrorIs(t, err, ErrInvalidClassification,
		"an UPDATE holds an explicit operator level — a typo must be rejected, not normalized")

	err = store.SetChannelClassification(ctx, "group:ghost", ClassificationPublic)
	assert.ErrorIs(t, err, ErrChannelNotFound)
}

// TestGetOrCreateDM_StructCarriesStamp pins that the returned struct agrees
// with the row GetOrCreateDM just inserted (the row-side stamp is pinned in
// sqlite_dm_classification_test.go; this closes the struct half so a caller
// holding the fresh value never sees an unclassified DM).
func TestGetOrCreateDM_StructCarriesStamp(t *testing.T) {
	store := newTestStore(t, SQLiteOptions{DMDefaultClassification: ClassificationRestricted})
	ch, err := store.GetOrCreateDM(context.Background(), "alice", "bob")
	require.NoError(t, err)
	assert.Equal(t, ClassificationRestricted, ch.Classification)
}

// TestReconcileConfig_ThreadsDeclaredClassification pins the create arm: a
// config-declared channel lands in the store with its declared level, not
// the migration default.
func TestReconcileConfig_ThreadsDeclaredClassification(t *testing.T) {
	router, _, store := newRouterTest(t)
	ctx := context.Background()

	require.NoError(t, router.ReconcileConfig(ctx, &Config{
		MaxChannels: 50,
		Channels: []ChannelConfig{{
			Name:           "leadership",
			Classification: ClassificationRestricted,
			Members:        []MemberConfig{{ID: "alice", RespondPolicy: RespondAlways}},
		}},
	}))

	ch, err := store.GetChannel(ctx, "group:leadership")
	require.NoError(t, err)
	assert.Equal(t, ClassificationRestricted, ch.Classification)
}

// TestReconcileConfig_AdoptsDeclaredClassificationOnExistingRow pins the
// 0037-plan "PR 2 note — existing rows" debt: a row created before the
// declaration (holding the v11 `internal` backfill) adopts the declared
// level at the next reconcile, and an UNDECLARED store channel is left
// untouched (the §B coexistence rule).
func TestReconcileConfig_AdoptsDeclaredClassificationOnExistingRow(t *testing.T) {
	router, _, store := newRouterTest(t)
	ctx := context.Background()

	// The pre-declaration store: created without a classification (the v11
	// backfill shape) with the membership the config will declare.
	mustCreateGroup(t, store, "leadership", "alice")
	mustCreateGroup(t, store, "adhoc", "bob")

	require.NoError(t, router.ReconcileConfig(ctx, &Config{
		MaxChannels: 50,
		Channels: []ChannelConfig{{
			Name:           "leadership",
			Classification: ClassificationRestricted,
			Members:        []MemberConfig{{ID: "alice", RespondPolicy: RespondWhenMentioned}},
		}},
	}))

	ch, err := store.GetChannel(ctx, "group:leadership")
	require.NoError(t, err)
	assert.Equal(t, ClassificationRestricted, ch.Classification,
		"the declared level must reach a pre-existing row, or an upgraded deployment silently under-classifies")

	ch, err = store.GetChannel(ctx, "group:adhoc")
	require.NoError(t, err)
	assert.Equal(t, ClassificationInternal, ch.Classification,
		"a config-undeclared channel is preserved untouched (§B coexistence)")
}

// TestPublish_DispatchEnvelopeCarriesClassification pins the dispatch leg
// end to end at the envelope: an ordinary fanout on a classified channel
// stamps the row's level on every recipient's envelope, and a reconcile
// adoption AFTER a dispatch refreshes the router's read-through cache — the
// [classificationCache] coherence contract for router-side writes.
func TestPublish_DispatchEnvelopeCarriesClassification(t *testing.T) {
	router, disp, store := newRouterTest(t)
	ctx := context.Background()
	id := mustCreateGroup(t, store, "planning", "alice", "bob")
	require.NoError(t, store.SetChannelClassification(ctx, id, ClassificationRestricted))

	require.NoError(t, router.Publish(ctx, ChannelMessage{
		ID: uuid.NewString(), ChannelID: id, SenderID: "alice", Content: "x",
	}, ""))
	calls := disp.snapshot()
	require.Len(t, calls, 1)
	assert.Equal(t, "restricted", calls[0].classification,
		"the dispatch envelope must carry the channels row's §A level")

	// A reconcile adoption mid-run must reach later dispatches through the
	// cache refresh, not wait for a restart.
	require.NoError(t, router.ReconcileConfig(ctx, &Config{
		MaxChannels: 50,
		Channels: []ChannelConfig{{
			Name:           "planning",
			Classification: ClassificationSecret,
			Members: []MemberConfig{
				{ID: "alice", RespondPolicy: RespondWhenMentioned},
				{ID: "bob", RespondPolicy: RespondWhenMentioned},
			},
		}},
	}))
	require.NoError(t, router.Publish(ctx, ChannelMessage{
		ID: uuid.NewString(), ChannelID: id, SenderID: "alice", Content: "y",
	}, ""))
	calls = disp.snapshot()
	require.Len(t, calls, 2)
	assert.Equal(t, "secret", calls[1].classification,
		"a router-side adoption must refresh the dispatch cache")
}
