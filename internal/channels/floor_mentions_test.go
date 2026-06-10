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

// ---------------------------------------------------------------------------
// orderResponders — the directedness basis change
// ---------------------------------------------------------------------------

// TestOrderResponders_MentionOfFloorIncapableIsOpenFloor — the trigger defect:
// a persona reply that politely @-mentions the human ("@alex, here's our
// recommendation…") must not close the floor. Pre-amendment, the raw
// `Mentions` non-empty check marked the message directed and dropped every
// unnamed `always` member to the ingestion-only set; with the floor-capable
// basis the message is open floor and both participants stay candidates.
func TestOrderResponders_MentionOfFloorIncapableIsOpenFloor(t *testing.T) {
	members := []Member{
		member("alex", RespondNever), // the human
		member("ember-owl", RespondAlways),
		member("iron-fox", RespondAlways),
	}
	msg := ChannelMessage{SenderID: "nova-sparrow", Mentions: []string{"alex"}}

	responders, nonResponders := orderResponders(members, msg, "")

	assert.Equal(t, []string{"ember-owl", "iron-fox"}, ids(responders),
		"a mention of the floor-incapable human leaves the floor open to every participant")
	assert.Empty(t, nonResponders)
}

// TestOrderResponders_MentionOfNonMemberIsOpenFloor — a mention that resolves
// to no membership row (a typo, an external name) is a conversational anchor,
// not a floor allocation.
func TestOrderResponders_MentionOfNonMemberIsOpenFloor(t *testing.T) {
	members := []Member{
		member("ember-owl", RespondAlways),
		member("iron-fox", RespondAlways),
	}
	msg := ChannelMessage{SenderID: "user", Mentions: []string{"stranger"}}

	responders, nonResponders := orderResponders(members, msg, "")

	assert.Equal(t, []string{"ember-owl", "iron-fox"}, ids(responders))
	assert.Empty(t, nonResponders)
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

// TestOrderResponders_SelfMentionAloneIsOpenFloor — §E matrix: a message
// whose only mention is its own sender is open floor. The sender never
// replies to itself (it is filtered from both sets), so without the §C
// sender exclusion the self-mention would mark the message directed and
// suppress every other participant for an addressee that cannot take the
// turn.
func TestOrderResponders_SelfMentionAloneIsOpenFloor(t *testing.T) {
	members := []Member{
		member("ember-owl", RespondAlways),
		member("iron-fox", RespondAlways),
	}
	msg := ChannelMessage{SenderID: "ember-owl", Mentions: []string{"ember-owl"}}

	responders, nonResponders := orderResponders(members, msg, "")

	assert.Equal(t, []string{"iron-fox"}, ids(responders),
		"a self-mention does not direct the floor; the other participant stays a candidate")
	assert.Empty(t, nonResponders)
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
