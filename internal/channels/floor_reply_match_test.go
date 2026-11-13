package channels

import (
	"context"
	"sync"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.uber.org/zap"
)

// A floor turn ends on the speaker's reply to this round's message, not on a
// reply it sends late to an earlier round (ISSUE-0200).
//
// A floor round gives each speaker a turn and waits for its reply before the
// next speaker is dispatched, so the next one reads it. The wait is the chat
// request's waiter, and it was matched by channel and sender only: a reply
// the speaker published late, to the round before, ended its turn in this
// round at once, and the next speaker was dispatched without having read what
// this speaker goes on to say now.

// staleReplyDispatcher plays two agents over two rounds. In round 1 speaker a
// stays silent and its turn times out. In round 2 a first publishes its late
// answer to round 1, and only then its answer to round 2.
type staleReplyDispatcher struct {
	router *ChannelRouter
	store  ChannelStore

	mu       sync.Mutex
	round1ID string // the id of round 1's message
	// lateEchoesRound1 is false for the "agent that echoes nothing" case.
	lateEchoesRound1 bool
	// bSawInRound2 is what speaker b could read when it got round 2's turn.
	bSawInRound2 []string
}

func (d *staleReplyDispatcher) Dispatch(ctx context.Context, env DispatchEnvelope, msg ChannelMessage) error {
	rid := env.Recipient.ParticipantID
	d.mu.Lock()
	if d.round1ID == "" {
		d.round1ID = msg.ID
	}
	round1 := msg.ID == d.round1ID
	round1ID := d.round1ID
	d.mu.Unlock()

	say := func(content, inReplyTo string) {
		reply := ChannelMessage{ID: uuid.NewString(), ChannelID: msg.ChannelID, SenderID: rid, Content: content}
		if inReplyTo != "" {
			reply.Metadata = map[string]any{replyToMetadataKey: inReplyTo}
		}
		_ = d.router.Publish(context.Background(), reply, "")
	}

	switch {
	case rid == "a" && round1:
		// Silent: the turn times out.
	case rid == "a":
		late := ""
		if d.lateEchoesRound1 {
			late = round1ID
		}
		say("a: late answer to round 1", late)
		go func() {
			time.Sleep(60 * time.Millisecond)
			say("a: answer to round 2", msg.ID)
		}()
	case rid == "b" && !round1:
		hist, _ := d.store.GetHistory(ctx, msg.ChannelID, 100, time.Time{})
		d.mu.Lock()
		for _, m := range hist {
			d.bSawInRound2 = append(d.bSawInRound2, m.Content)
		}
		d.mu.Unlock()
		say("b: answer to round 2", msg.ID)
	default:
		say("b: answer to round 1", msg.ID)
	}
	return nil
}

func TestFloorRound_ALateReplyToAnEarlierRoundDoesNotEndTheTurn(t *testing.T) {
	tests := []struct {
		name string
		// lateEchoesRound1: the late reply names round 1's message, as an
		// agent's does. Without it the reply answers nothing.
		lateEchoesRound1 bool
	}{
		{name: "a late reply that names the earlier round's message", lateEchoesRound1: true},
		{name: "a message that names no message at all", lateEchoesRound1: false},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			store := newTestStore(t, SQLiteOptions{})
			disp := &staleReplyDispatcher{store: store, lateEchoesRound1: tt.lateEchoesRound1}
			router := NewChannelRouter(store, disp, zap.NewNop(), nil)
			disp.router = router
			id := mustCreateGroupWithPolicies(t, store, "planning", map[string]RespondPolicy{
				"user": RespondNever, "a": RespondAlways, "b": RespondAlways,
			}, "user", "a", "b")
			// Round 1: a is silent, so its turn runs to this timeout.
			router.SetFloorControl(id, true, 150*time.Millisecond)
			ask := func(text string) {
				require.NoError(t, router.Publish(context.Background(), ChannelMessage{
					ID: uuid.NewString(), ChannelID: id, SenderID: "user", Content: text,
				}, ""))
				router.WaitForPendingFanout()
			}

			ask("round 1")
			// Round 2: a answers 60 ms after its late reply. The timeout is
			// wide so that only the late reply can end the turn early.
			router.SetFloorControl(id, true, 10*time.Second)
			ask("round 2")

			disp.mu.Lock()
			saw := append([]string(nil), disp.bSawInRound2...)
			disp.mu.Unlock()
			require.NotEmpty(t, saw, "speaker b had its turn in round 2")
			assert.Contains(t, saw, "a: late answer to round 1")
			assert.Contains(t, saw, "a: answer to round 2",
				"b was given the floor before a's answer to this round was in the channel")
		})
	}
}
