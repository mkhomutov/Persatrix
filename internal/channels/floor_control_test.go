package channels

import (
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// member is a tiny constructor so the ordering tests read as a list of
// `(id, policy)` pairs without the JoinedAt noise — JoinedAt is the
// store-side tiebreak already reflected in the slice order the helper
// receives, so the tests pass members in the order GetMembers would
// return them and assert the helper preserves it.
func member(id string, policy RespondPolicy) Member {
	return Member{ParticipantID: id, RespondPolicy: policy}
}

// ---------------------------------------------------------------------------
// floorRegistry — per-channel mutual exclusion (D5: keyed by channel_id)
// ---------------------------------------------------------------------------

// TestFloorRegistry_AcquireBlocksUntilRelease pins the core contract: a
// second acquire on the same channel parks until the first release, so at
// most one floor round runs per channel at a time.
func TestFloorRegistry_AcquireBlocksUntilRelease(t *testing.T) {
	reg := newFloorRegistry()
	reg.acquire("group:planning")

	acquired := make(chan struct{})
	go func() {
		reg.acquire("group:planning")
		close(acquired)
	}()

	// The second acquire must still be parked while we hold the floor.
	select {
	case <-acquired:
		t.Fatal("second acquire returned while the floor was held")
	case <-time.After(50 * time.Millisecond):
		// expected: still blocked
	}

	reg.release("group:planning")

	select {
	case <-acquired:
		// expected: release handed the floor to the waiter
	case <-time.After(time.Second):
		t.Fatal("second acquire did not unblock after release")
	}
	reg.release("group:planning")
}

// TestFloorRegistry_DistinctChannelsIndependent — a held floor on one
// channel must not block acquiring a different channel's floor.
func TestFloorRegistry_DistinctChannelsIndependent(t *testing.T) {
	reg := newFloorRegistry()
	reg.acquire("group:planning")

	done := make(chan struct{})
	go func() {
		reg.acquire("group:design") // different channel — must not block
		reg.release("group:design")
		close(done)
	}()

	select {
	case <-done:
		// expected: independent channel acquired immediately
	case <-time.After(time.Second):
		t.Fatal("acquire on a distinct channel blocked behind an unrelated floor")
	}
	reg.release("group:planning")
}

// TestFloorRegistry_ReleaseIdempotent — releasing a floor that is already
// free is a safe no-op (the loop's deferred release must not panic if the
// round already released on a prior path).
func TestFloorRegistry_ReleaseIdempotent(t *testing.T) {
	reg := newFloorRegistry()
	reg.acquire("group:planning")
	reg.release("group:planning")
	assert.NotPanics(t, func() { reg.release("group:planning") },
		"double release must be a safe no-op")

	// The floor is still acquirable after the redundant release.
	acquired := make(chan struct{})
	go func() {
		reg.acquire("group:planning")
		close(acquired)
	}()
	select {
	case <-acquired:
	case <-time.After(time.Second):
		t.Fatal("floor not acquirable after idempotent release")
	}
	reg.release("group:planning")
}

// ---------------------------------------------------------------------------
// orderResponders — candidate split + mentioned-first ordering (D3)
// ---------------------------------------------------------------------------

func ids(members []Member) []string {
	out := make([]string, len(members))
	for i, m := range members {
		out[i] = m.ParticipantID
	}
	return out
}

// TestOrderResponders_AlwaysAreResponders — `always` members are candidate
// responders; the sender is filtered; `never` is excluded from both sets.
func TestOrderResponders_AlwaysAreResponders(t *testing.T) {
	members := []Member{
		member("alice", RespondAlways),
		member("bob", RespondAlways),
		member("muted", RespondNever),
		member("user", RespondAlways), // the sender
	}
	msg := ChannelMessage{SenderID: "user"}

	responders, nonResponders := orderResponders(members, msg, "")

	assert.Equal(t, []string{"alice", "bob"}, ids(responders),
		"always members (minus sender) are responders in member order")
	assert.Empty(t, nonResponders,
		"never is excluded entirely; no when_mentioned-not-mentioned members here")
}

// TestOrderResponders_WhenMentionedSplit — a `when_mentioned` member that is
// mentioned becomes a responder; one that is not becomes a non-responder
// (delivered for ingestion only, off the floor queue).
func TestOrderResponders_WhenMentionedSplit(t *testing.T) {
	members := []Member{
		member("alice", RespondWhenMentioned),
		member("bob", RespondWhenMentioned),
	}
	msg := ChannelMessage{SenderID: "user", Mentions: []string{"bob"}}

	responders, nonResponders := orderResponders(members, msg, "")

	assert.Equal(t, []string{"bob"}, ids(responders))
	assert.Equal(t, []string{"alice"}, ids(nonResponders))
}

// TestOrderResponders_MentionedFirst — among responders, mentioned members
// take the floor before unmentioned `always` members; within each group the
// original member order is preserved (D3 stable tie-break).
func TestOrderResponders_MentionedFirst(t *testing.T) {
	members := []Member{
		member("alice", RespondAlways),        // always, not mentioned
		member("bob", RespondAlways),          // always, mentioned
		member("carol", RespondWhenMentioned), // when_mentioned, mentioned
		member("dave", RespondAlways),         // always, not mentioned
	}
	msg := ChannelMessage{SenderID: "user", Mentions: []string{"bob", "carol"}}

	responders, _ := orderResponders(members, msg, "")

	// mentioned-first (bob, carol in member order), then the rest in
	// member order (alice, dave).
	assert.Equal(t, []string{"bob", "carol", "alice", "dave"}, ids(responders))
}

// TestOrderResponders_ThreadReplyToSelf — a `when_mentioned` member who is
// not mentioned is still a responder when the stimulus is a thread reply to
// a message they sent (mirrors the receiver gate's thread-reply-to-self
// branch). They are unmentioned, so they order after mentioned responders.
func TestOrderResponders_ThreadReplyToSelf(t *testing.T) {
	members := []Member{
		member("alice", RespondWhenMentioned),
		member("bob", RespondWhenMentioned),
	}
	// Reply in a thread whose parent was sent by alice; bob is mentioned.
	msg := ChannelMessage{
		SenderID: "user",
		ThreadID: "m-parent",
		Mentions: []string{"bob"},
	}

	responders, nonResponders := orderResponders(members, msg, "alice")

	// bob mentioned → first; alice thread-reply-to-self → responder, after.
	assert.Equal(t, []string{"bob", "alice"}, ids(responders))
	assert.Empty(t, nonResponders)
}

// TestOrderResponders_SingleResponderAndEmpty — degenerate inputs the loop
// must tolerate: a lone responder and a members slice that yields none.
func TestOrderResponders_SingleResponderAndEmpty(t *testing.T) {
	single := []Member{member("alice", RespondAlways), member("user", RespondAlways)}
	responders, nonResponders := orderResponders(single, ChannelMessage{SenderID: "user"}, "")
	assert.Equal(t, []string{"alice"}, ids(responders))
	assert.Empty(t, nonResponders)

	// Only the sender and a muted member → no responders, no non-responders.
	none := []Member{member("user", RespondAlways), member("muted", RespondNever)}
	responders, nonResponders = orderResponders(none, ChannelMessage{SenderID: "user"}, "")
	assert.Empty(t, responders)
	assert.Empty(t, nonResponders)

	// Empty membership slice.
	responders, nonResponders = orderResponders(nil, ChannelMessage{SenderID: "user"}, "")
	assert.Empty(t, responders)
	assert.Empty(t, nonResponders)
}

func TestNewFloorRegistry_NotNil(t *testing.T) {
	require.NotNil(t, newFloorRegistry())
}
