package server

import (
	"context"
	"encoding/json"
	"net/http"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"

	"github.com/mkhomutov/persatrix/internal/channels"
)

// The thread route and the publish route keep a thread inside the channel
// in their path (ISSUE-0203).
//
// Two channels with different RFC 0037 levels make the harm visible: the
// thread response is labelled with the path channel's level, so a `secret`
// channel's reply read through a `public` channel's route came back
// labelled `public`. A third channel at the lobby's own level shows the
// rule is about the channel, not about the level.

type threadFixture struct {
	srv         *Server
	store       channels.ChannelStore
	lobbyParent string // a message in group:lobby (public)
	annexParent string // a message in group:annex (public, like the lobby)
	boardParent string // a message in group:board (secret)
	boardReply  string // a reply to boardParent, in group:board
}

func newThreadFixture(t *testing.T, srv *Server, store channels.ChannelStore) threadFixture {
	t.Helper()
	ctx := context.Background()
	for id, level := range map[string]channels.Classification{
		"group:lobby": channels.ClassificationPublic,
		"group:annex": channels.ClassificationPublic,
		"group:board": channels.ClassificationSecret,
	} {
		require.NoError(t, store.CreateChannel(ctx, channels.Channel{
			ID: id, Name: id[len("group:"):], Type: channels.ChannelTypeGroup, Classification: level,
		}))
		for _, member := range []string{"alice", "bob"} {
			require.NoError(t, store.AddMember(ctx, id, member, channels.RespondWhenMentioned))
		}
	}
	f := threadFixture{srv: srv, store: store}
	f.lobbyParent = f.publish(t, "group:lobby", "alice", "lobby parent", "")
	f.annexParent = f.publish(t, "group:annex", "alice", "annex parent", "")
	f.boardParent = f.publish(t, "group:board", "alice", "board parent", "")
	f.boardReply = f.publish(t, "group:board", "bob", "board reply", f.boardParent)
	return f
}

// publish posts through the REST route and returns the stored id.
func (f threadFixture) publish(t *testing.T, channelID, sender, content, threadID string) string {
	t.Helper()
	body, _ := json.Marshal(publishMessageRequest{SenderID: sender, Content: content, ThreadID: threadID})
	rec := doRequest(f.srv.Handler(), http.MethodPost, "/api/v1/channels/"+channelID+"/messages", body)
	require.Equal(t, http.StatusCreated, rec.Code, "body=%s", rec.Body.String())
	var stored channelMessageResponse
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &stored))
	return stored.ID
}

func (f threadFixture) historyIDs(t *testing.T, channelID string) []string {
	t.Helper()
	msgs, err := f.store.GetHistory(context.Background(), channelID, 100, time.Time{})
	require.NoError(t, err)
	ids := make([]string, 0, len(msgs))
	for _, m := range msgs {
		ids = append(ids, m.ID)
	}
	return ids
}

func TestChannels_GetThread_OnlyForAMessageInThePathsChannel(t *testing.T) {
	srv, store := channelTestServer(t)
	f := newThreadFixture(t, srv, store)

	tests := []struct {
		name        string
		channel     string
		message     string
		wantStatus  int
		wantMessage string   // the error envelope's message, on a refusal
		wantLevel   string   // the response's classification, on 200
		wantReplies []string // reply ids, on 200
	}{
		{
			name: "a message in the path's channel", channel: "group:board", message: f.boardParent,
			wantStatus: http.StatusOK, wantLevel: "secret", wantReplies: []string{f.boardReply},
		},
		{
			name: "a message with no replies, in the path's channel", channel: "group:lobby", message: f.lobbyParent,
			wantStatus: http.StatusOK, wantLevel: "public", wantReplies: []string{},
		},
		{
			name: "another channel's message", channel: "group:lobby", message: f.boardParent,
			wantStatus: http.StatusNotFound, wantMessage: "message not found",
		},
		{
			name: "another channel's reply", channel: "group:lobby", message: f.boardReply,
			wantStatus: http.StatusNotFound, wantMessage: "message not found",
		},
		{
			name: "a message of another channel at the same level", channel: "group:lobby", message: f.annexParent,
			wantStatus: http.StatusNotFound, wantMessage: "message not found",
		},
		{
			name: "the other direction", channel: "group:board", message: f.lobbyParent,
			wantStatus: http.StatusNotFound, wantMessage: "message not found",
		},
		{
			name: "a message that does not exist", channel: "group:lobby", message: "no-such-message",
			wantStatus: http.StatusNotFound, wantMessage: "message not found",
		},
		{
			name: "a channel that does not exist", channel: "group:nowhere", message: f.boardParent,
			wantStatus: http.StatusNotFound, wantMessage: "channel not found",
		},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			rec := doRequest(srv.Handler(), http.MethodGet,
				"/api/v1/channels/"+tt.channel+"/messages/"+tt.message+"/thread", nil)

			require.Equal(t, tt.wantStatus, rec.Code, "body=%s", rec.Body.String())
			if tt.wantStatus != http.StatusOK {
				assert.Contains(t, rec.Body.String(), `"code":"NOT_FOUND"`)
				assert.Contains(t, rec.Body.String(), tt.wantMessage)
				assert.NotContains(t, rec.Body.String(), "board reply", "no message text in a refusal")
				return
			}
			var hist historyResponse
			require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &hist))
			assert.Equal(t, tt.wantLevel, hist.Classification)
			ids := make([]string, 0, len(hist.Messages))
			for _, m := range hist.Messages {
				ids = append(ids, m.ID)
				assert.Equal(t, tt.channel, m.ChannelID)
			}
			assert.Equal(t, tt.wantReplies, ids)
		})
	}
}

func TestChannels_PublishMessage_RefusesAThreadParentOutsideTheChannel(t *testing.T) {
	servers := []struct {
		name string
		new  func(t *testing.T) (*Server, channels.ChannelStore)
	}{
		{name: "through the router", new: channelTestServer},
		{name: "with no router wired", new: func(t *testing.T) (*Server, channels.ChannelStore) {
			srv, _ := channelTestServerNoRouter(t)
			return srv, srv.channelStore
		}},
	}
	for _, s := range servers {
		t.Run(s.name, func(t *testing.T) {
			srv, store := s.new(t)
			f := newThreadFixture(t, srv, store)

			tests := []struct {
				name   string
				parent string
			}{
				{name: "another channel's message", parent: f.boardParent},
				{name: "another channel's reply", parent: f.boardReply},
				{name: "a message of another channel at the same level", parent: f.annexParent},
				{name: "a message that does not exist", parent: "no-such-message"},
			}
			for _, tt := range tests {
				t.Run(tt.name, func(t *testing.T) {
					lobbyBefore := f.historyIDs(t, "group:lobby")
					boardBefore := f.historyIDs(t, "group:board")
					body, _ := json.Marshal(publishMessageRequest{
						SenderID: "bob", Content: "cross-channel reply", ThreadID: tt.parent,
					})

					rec := doRequest(srv.Handler(), http.MethodPost,
						"/api/v1/channels/group:lobby/messages", body)

					require.Equal(t, http.StatusBadRequest, rec.Code, "body=%s", rec.Body.String())
					assert.Contains(t, rec.Body.String(), `"code":"BAD_REQUEST"`)
					assert.Contains(t, rec.Body.String(), "invalid thread_id "+tt.parent)
					assert.Equal(t, lobbyBefore, f.historyIDs(t, "group:lobby"))
					assert.Equal(t, boardBefore, f.historyIDs(t, "group:board"))
				})
			}

			// A reply to a message of the same channel is still stored.
			reply := f.publish(t, "group:lobby", "bob", "lobby reply", f.lobbyParent)
			assert.Contains(t, f.historyIDs(t, "group:lobby"), reply)
		})
	}
}
