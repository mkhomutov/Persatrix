package channels

// floor_mentions_test.go — RFC 0030 floor-capable-directedness amendment
// (docs/rfcs/0030-amendment-floor-capable-directedness.md). Pins the three
// layers of the Go half in one sibling file (the salience-test pattern):
// the [resolveFloorMentions] subset rule, the [orderResponders] directedness
// basis change, and the fanout-envelope → proto wire stamping.

import (
	"context"
	"testing"

	"github.com/google/uuid"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.uber.org/zap"
	"go.uber.org/zap/zaptest/observer"
)

// ---------------------------------------------------------------------------
// resolveFloorMentions — the floor-capable subset rule
// ---------------------------------------------------------------------------

// TestResolveFloorMentions_SubsetRule pins the core rule: the result is the
// subset of mentions naming members whose normalized policy is not `never`,
// in mention order. The human operator (a `never` member by the documented
// join convention), a non-member, and the `@everyone` sentinel (never a
// member id) all drop out; `always` and `when_mentioned` members survive.
func TestResolveFloorMentions_SubsetRule(t *testing.T) {
	members := []Member{
		member("alex", RespondNever), // the human, joined `--respond never`
		member("ember-owl", RespondWhenMentioned),
		member("iron-fox", RespondAlways),
	}

	got := resolveFloorMentions(members,
		[]string{MentionEveryone, "alex", "iron-fox", "stranger", "ember-owl"}, "user")

	assert.Equal(t, []string{"iron-fox", "ember-owl"}, got,
		"floor-capable members survive in mention order; the human, the non-member, and the sentinel drop out")
}

// TestResolveFloorMentions_ExcludesSender pins the §C sender exclusion: a
// self-mention cannot direct the floor — the sender never replies to itself,
// so counting it would suppress everyone else for an addressee that cannot
// take the turn.
func TestResolveFloorMentions_ExcludesSender(t *testing.T) {
	members := []Member{
		member("ember-owl", RespondAlways),
		member("iron-fox", RespondAlways),
	}

	got := resolveFloorMentions(members, []string{"ember-owl", "iron-fox"}, "ember-owl")

	assert.Equal(t, []string{"iron-fox"}, got,
		"the sender drops out of the floor-capable subset")
}

// TestResolveFloorMentions_EmptyAndAllIncapable pins the two empty shapes: no
// mentions at all, and mentions naming only floor-incapable parties — both
// yield an empty subset (the open-floor basis), which the wire pairs with the
// unconditional `floor_mentions_resolved` flag precisely because this empty
// value is load-bearing.
func TestResolveFloorMentions_EmptyAndAllIncapable(t *testing.T) {
	members := []Member{
		member("alex", RespondNever),
		member("iron-fox", RespondAlways),
	}

	assert.Nil(t, resolveFloorMentions(members, nil, "user"), "no mentions → nil")
	assert.Empty(t, resolveFloorMentions(members, []string{"alex", "stranger"}, "user"),
		"mentions naming only the human and a non-member resolve to the empty subset")
}

// TestResolveFloorMentions_NormalizesDispositions pins the defence-in-depth
// Normalize: a membership row carrying an un-normalized disposition spelling
// (a hand-edited row — the store normalizes on write) still classifies
// correctly, because the subset is the cross-language wire suppression basis
// and must not silently widen or narrow on a spelling.
func TestResolveFloorMentions_NormalizesDispositions(t *testing.T) {
	members := []Member{
		member("watcher", RespondObserver),   // → never: floor-incapable
		member("helper", RespondParticipant), // → always: floor-capable
	}

	got := resolveFloorMentions(members, []string{"watcher", "helper"}, "user")

	assert.Equal(t, []string{"helper"}, got,
		"observer normalizes to never (incapable); participant normalizes to always (capable)")
}

// TestResolveFloorMentions_DedupesMentions pins that a duplicated mention id
// appears once in the resolved subset. The publish path caps `mentions` at 10
// but never dedupes, and `floor_mentions` is a contract-bearing wire field —
// receivers reason about it as a set (membership/emptiness), so duplicates
// would be noise at best and a drift hazard for any future consumer that
// counts entries.
func TestResolveFloorMentions_DedupesMentions(t *testing.T) {
	members := []Member{
		member("iron-fox", RespondAlways),
	}

	got := resolveFloorMentions(members, []string{"iron-fox", "iron-fox"}, "user")

	assert.Equal(t, []string{"iron-fox"}, got,
		"a duplicated mention resolves to one subset entry")
}

// ---------------------------------------------------------------------------
// orderResponders — the directedness basis change
// ---------------------------------------------------------------------------

// TestOrderResponders_FloorIncapableMentionsAreOpenFloor — the §C item 3
// basis flip, landed in the same change as the Python gate's (the §E "PR 2"
// matrix; preserves candidate-set/gate parity at every commit). These three
// rows are the inverted PR-1 interim parity pins: a message mentioning only
// a floor-incapable party (the human, a non-member, the sender itself) is
// open floor — every `always` member stays a candidate instead of dropping
// to the ingestion-only set. The first row is the trigger defect ("@alex,
// here's our recommendation…") that silenced the room pre-amendment.
func TestOrderResponders_FloorIncapableMentionsAreOpenFloor(t *testing.T) {
	cases := []struct {
		name     string
		members  []Member
		msg      ChannelMessage
		wantResp []string
		wantNon  []string
	}{
		{
			name: "human-only mention is open floor",
			members: []Member{
				member("alex", RespondNever), // the human
				member("ember-owl", RespondAlways),
				member("iron-fox", RespondAlways),
			},
			msg:      ChannelMessage{SenderID: "nova-sparrow", Mentions: []string{"alex"}},
			wantResp: []string{"ember-owl", "iron-fox"},
			wantNon:  []string{},
		},
		{
			name: "non-member mention is open floor",
			members: []Member{
				member("ember-owl", RespondAlways),
				member("iron-fox", RespondAlways),
			},
			msg:      ChannelMessage{SenderID: "user", Mentions: []string{"stranger"}},
			wantResp: []string{"ember-owl", "iron-fox"},
			wantNon:  []string{},
		},
		{
			name: "sole self-mention is open floor",
			members: []Member{
				member("ember-owl", RespondAlways),
				member("iron-fox", RespondAlways),
			},
			msg:      ChannelMessage{SenderID: "ember-owl", Mentions: []string{"ember-owl"}},
			wantResp: []string{"iron-fox"},
			wantNon:  []string{},
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			responders, nonResponders := orderResponders(tc.members, tc.msg, "")
			assert.Equal(t, tc.wantResp, ids(responders))
			assert.Equal(t, tc.wantNon, ids(nonResponders))
		})
	}
}

// TestOrderResponders_NormalizesStoredPolicies pins read-seam normalization:
// the candidate split classifies a member by its *normalized* policy, exactly
// as the Python gate's `_DISPOSITION_ALIASES` defence-in-depth does for the
// same row arriving on the wire. Store rows are canonical by construction
// (the membership CHECK constraint admits only the legacy triple), so this is
// unreachable through the store today — but a non-canonical spelling that
// ever does reach a [Member] must classify identically here, in
// [resolveFloorMentions] (whose Normalize is the wire-basis defence), and in
// the gate, or the same row directs the floor on one basis and is refused
// candidacy on another — the guaranteed-silence defect class.
func TestOrderResponders_NormalizesStoredPolicies(t *testing.T) {
	members := []Member{
		member("watcher", RespondObserver),   // → never: off both sets
		member("helper", RespondParticipant), // → always: candidate
	}
	msg := ChannelMessage{SenderID: "user"}

	responders, nonResponders := orderResponders(members, msg, "")

	assert.Equal(t, []string{"helper"}, ids(responders),
		"a participant-spelled member normalizes to always and stays a candidate")
	assert.Empty(t, nonResponders,
		"an observer-spelled member normalizes to never: no dispatch, off both sets")
}

// TestDispatchConcurrent_NormalizesNeverCheck pins the same read-seam
// normalization on the concurrent path's `never` short-circuit: an
// observer-spelled member is a `never` member and must not receive a
// dispatch, exactly as a canonically-spelled one would not.
func TestDispatchConcurrent_NormalizesNeverCheck(t *testing.T) {
	store := newTestStore(t, SQLiteOptions{})
	disp := &envelopeRecorder{}
	router := NewChannelRouter(store, disp, zap.NewNop(), nil)

	members := []Member{
		member("watcher", RespondObserver), // → never: no dispatch
		member("iron-fox", RespondAlways),
	}
	msg := ChannelMessage{ID: uuid.NewString(), ChannelID: "group:planning", SenderID: "user"}

	router.dispatchConcurrent(context.Background(), msg, ChannelTypeGroup, "", members, len(members), nil)

	calls := disp.snapshot()
	require.Len(t, calls, 1, "only the floor-capable member is dispatched")
	assert.Equal(t, "iron-fox", calls[0].Recipient.ParticipantID)
}

// TestOrderResponders_MixedMentionStaysDirected — naming a floor-capable
// member alongside the human keeps the message directed: the pile-on
// protection is untouched whenever a real addressee exists. The named
// `when_mentioned` member takes the floor; the unnamed `always` member drops
// to the ingestion-only set exactly as on a pre-amendment directed message.
func TestOrderResponders_MixedMentionStaysDirected(t *testing.T) {
	members := []Member{
		member("alex", RespondNever),              // the human
		member("ember-owl", RespondWhenMentioned), // named → floor
		member("iron-fox", RespondAlways),         // unnamed → off-floor
	}
	msg := ChannelMessage{SenderID: "user", Mentions: []string{"alex", "ember-owl"}}

	responders, nonResponders := orderResponders(members, msg, "")

	assert.Equal(t, []string{"ember-owl"}, ids(responders),
		"the floor-capable addressee keeps the message directed")
	assert.Equal(t, []string{"iron-fox"}, ids(nonResponders),
		"the unnamed participant stays suppressed — pile-on protection intact")
}

// TestOrderResponders_BroadcastWithFloorCapableMentionNotDirected — §E
// matrix: `@everyone` alongside a floor-capable mention is still a broadcast
// (not directed). The sentinel is a non-member and silently falls out of the
// resolved subset, so this row pins that the broadcast guard keeps reading
// the raw mentions list — a guard on the resolved subset would be vacuously
// true and re-suppress the unnamed participant (§C item 1).
func TestOrderResponders_BroadcastWithFloorCapableMentionNotDirected(t *testing.T) {
	members := []Member{
		member("ember-owl", RespondAlways),
		member("iron-fox", RespondAlways),
	}
	msg := ChannelMessage{
		SenderID: "user",
		Mentions: []string{MentionEveryone, "iron-fox"},
	}

	responders, nonResponders := orderResponders(members, msg, "")

	assert.Equal(t, []string{"iron-fox", "ember-owl"}, ids(responders),
		"the broadcast admits everyone; the named member orders first")
	assert.Empty(t, nonResponders)
}

// ---------------------------------------------------------------------------
// fanout envelope + proto stamping — the wire half
// ---------------------------------------------------------------------------

// TestFanout_StampsFloorMentionsOnEnvelope pins that fanout resolves the
// floor-capable subset once per publish and stamps it identically on every
// recipient's envelope (the ChannelSize pattern): the human and the
// non-member drop out of the basis while the raw mentions ride the message
// untouched.
func TestFanout_StampsFloorMentionsOnEnvelope(t *testing.T) {
	store := newTestStore(t, SQLiteOptions{})
	disp := &envelopeRecorder{}
	router := NewChannelRouter(store, disp, zap.NewNop(), nil)
	ctx := context.Background()

	id := mustCreateGroupWithPolicies(t, store, "planning",
		map[string]RespondPolicy{
			"alex":      RespondNever, // the human
			"ember-owl": RespondAlways,
			"iron-fox":  RespondAlways,
		}, "alex", "ember-owl", "iron-fox")

	require.NoError(t, router.Publish(ctx, ChannelMessage{
		ID: uuid.NewString(), ChannelID: id, SenderID: "ember-owl",
		Content:  "@alex here is our recommendation; @iron-fox covered the rest",
		Mentions: []string{"alex", "iron-fox", "stranger"},
	}, ""))

	calls := disp.snapshot()
	require.NotEmpty(t, calls, "fanout dispatched at least iron-fox")
	for _, c := range calls {
		assert.Equal(t, []string{"iron-fox"}, c.FloorMentions,
			"the envelope carries only the floor-capable subset, identical across recipients")
	}
}

// TestFanout_ReclassificationDebugLog pins the §D observability contract on
// both edges: the resolved-empty debug line fires when mentions named no
// floor-capable member (the previously-silent "addressed a non-responder"
// case), and does NOT fire for an explicit `@everyone` broadcast — the
// sentinel always falls out of the intersection, but a broadcast is
// open-floor by the D3 contract, not a reclassification, so logging it would
// mislabel every broadcast and drown the signal §D exists to surface.
func TestFanout_ReclassificationDebugLog(t *testing.T) {
	run := func(t *testing.T, mentions []string) *observer.ObservedLogs {
		store := newTestStore(t, SQLiteOptions{})
		core, logs := observer.New(zap.DebugLevel)
		router := NewChannelRouter(store, &envelopeRecorder{}, zap.New(core), nil)

		id := mustCreateGroupWithPolicies(t, store, "planning",
			map[string]RespondPolicy{
				"alex":      RespondNever, // the human
				"ember-owl": RespondAlways,
				"iron-fox":  RespondAlways,
			}, "alex", "ember-owl", "iron-fox")

		require.NoError(t, router.Publish(context.Background(), ChannelMessage{
			ID: uuid.NewString(), ChannelID: id, SenderID: "ember-owl",
			Content:  "hello",
			Mentions: mentions,
		}, ""))
		return logs
	}

	t.Run("human-only mention logs the resolution", func(t *testing.T) {
		logs := run(t, []string{"alex"})
		assert.Equal(t, 1,
			logs.FilterMessageSnippet("no floor-capable member").Len(),
			"the resolved-empty case is surfaced while the change beds in (§D)")
	})

	t.Run("broadcast does not log", func(t *testing.T) {
		logs := run(t, []string{MentionEveryone})
		assert.Equal(t, 0,
			logs.FilterMessageSnippet("no floor-capable member").Len(),
			"an explicit broadcast is open floor by contract (D3), not a reclassification")
	})
}

// TestChannelMessageToProto_PopulatesFloorMentions pins the dispatcher
// translation: the envelope subset rides `floor_mentions`, and
// `floor_mentions_resolved` is unconditionally true — including when the
// subset is empty, the load-bearing "reclassified to open floor" value an old
// producer could not have expressed (proto3 repeated fields have no
// presence; the flag is the presence).
func TestChannelMessageToProto_PopulatesFloorMentions(t *testing.T) {
	d := &GRPCMessageDispatcher{logger: zap.NewNop()}

	ev := d.channelMessageToProto(
		ChannelMessage{ID: "m-1", ChannelID: "group:planning", SenderID: "a"},
		DispatchEnvelope{
			Recipient:     Member{ParticipantID: "b", RespondPolicy: RespondAlways},
			FloorMentions: []string{"iron-fox"},
		})
	assert.Equal(t, []string{"iron-fox"}, ev.FloorMentions)
	assert.True(t, ev.FloorMentionsResolved)

	ev = d.channelMessageToProto(
		ChannelMessage{ID: "m-2", ChannelID: "group:planning", SenderID: "a"},
		DispatchEnvelope{
			Recipient: Member{ParticipantID: "b", RespondPolicy: RespondAlways},
		})
	assert.Empty(t, ev.FloorMentions,
		"no floor-capable mention → empty subset on the wire")
	assert.True(t, ev.FloorMentionsResolved,
		"the flag is producer-presence, not data: true even when the subset is empty")
}
