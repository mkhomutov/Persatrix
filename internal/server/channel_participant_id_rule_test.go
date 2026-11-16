package server

import (
	"context"
	"database/sql"
	"encoding/json"
	"net/http"
	"path/filepath"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.uber.org/zap"

	"github.com/mkhomutov/persatrix/internal/channels"
	"github.com/mkhomutov/persatrix/internal/planner"
	"github.com/mkhomutov/persatrix/internal/registry"
	"github.com/mkhomutov/persatrix/internal/state"
)

// ISSUE-0209 at the REST door: an id that does not fit the participant id
// rule is refused with a 400 whose message names the rule, where it used to
// be stored and then refused by every persona without a word to the caller.
// What an earlier orchestrator stored stays readable.

const participantIDRuleText = "^[a-z0-9][a-z0-9-]*[a-z0-9]$"

// looseIDs are the five ids of the issue.
var looseIDs = []string{"User_1", "alice_b", "a", "Bob", "ops-"}

// requireRefusedByTheRule asserts a 400 whose message names the id and the rule.
func requireRefusedByTheRule(t *testing.T, code int, body, id string) {
	t.Helper()
	require.Equal(t, http.StatusBadRequest, code, "body=%s", body)
	var envelope errorResponse
	require.NoError(t, json.Unmarshal([]byte(body), &envelope))
	assert.Equal(t, "BAD_REQUEST", envelope.Code)
	assert.Contains(t, envelope.Error, `"`+id+`"`, "the message names the id")
	assert.Contains(t, envelope.Error, participantIDRuleText, "the message names the rule")
	assert.Contains(t, envelope.Error, "lowercase letters, digits and hyphens", "and says it in words")
}

func TestChannels_ALooseParticipantIDIsRefusedAtTheDoor(t *testing.T) {
	for _, loose := range looseIDs {
		t.Run(loose, func(t *testing.T) {
			srv, store := channelTestServer(t)
			ctx := context.Background()
			require.NoError(t, store.CreateChannelWithMembers(ctx,
				channels.Channel{ID: "group:planning", Name: "planning", Type: channels.ChannelTypeGroup},
				[]channels.Member{{ParticipantID: "alice"}, {ParticipantID: "ember-owl"}}))
			h := srv.Handler()

			post := func(path string, body any) (int, string) {
				raw, err := json.Marshal(body)
				require.NoError(t, err)
				rec := doRequest(h, http.MethodPost, path, raw)
				return rec.Code, rec.Body.String()
			}

			code, body := post("/api/v1/channels/group:planning/messages",
				publishMessageRequest{SenderID: loose, Content: "hi"})
			requireRefusedByTheRule(t, code, body, loose)

			code, body = post("/api/v1/channels/group:planning/messages",
				publishMessageRequest{SenderID: "alice", Content: "hi", Mentions: []string{loose}})
			requireRefusedByTheRule(t, code, body, loose)

			code, body = post("/api/v1/channels/group:planning/members", addMemberRequest{ID: loose})
			requireRefusedByTheRule(t, code, body, loose)

			code, body = post("/api/v1/channels", createChannelRequest{
				Name: "other", Members: []channelMemberRequest{{ID: "alice"}, {ID: loose}},
			})
			requireRefusedByTheRule(t, code, body, loose)

			history, err := store.GetHistory(ctx, "group:planning", 10, time.Time{})
			require.NoError(t, err)
			assert.Empty(t, history, "nothing was stored")
		})
	}
}

// earlierStoreServer builds a server on a store as an orchestrator before
// ISSUE-0209 left it: group:ops with the members User_1, alice and ember-owl
// and one message from User_1, and the DM of User_1 with ember-owl with one
// message. The loose rows are written with plain SQL: no route takes them.
func earlierStoreServer(t *testing.T) (*Server, *registry.InMemoryRegistry, channels.ChannelStore) {
	t.Helper()
	path := filepath.Join(t.TempDir(), "channels.db")
	first, err := channels.NewSQLiteStore(path, channels.SQLiteOptions{MaxChannels: 50})
	require.NoError(t, err)
	require.NoError(t, first.Close())

	db, err := sql.Open("sqlite", path)
	require.NoError(t, err)
	at := time.Date(2026, 11, 1, 9, 0, 0, 0, time.UTC)
	for _, stmt := range []struct {
		sql  string
		args []any
	}{
		{`INSERT INTO channels (id, name, channel_type, description, created_at) VALUES (?, ?, 'group', '', ?)`,
			[]any{"group:ops", "ops", at}},
		{`INSERT INTO channels (id, name, channel_type, description, created_at) VALUES (?, ?, 'dm', '', ?)`,
			[]any{"dm:User_1:ember-owl", "dm:User_1:ember-owl", at}},
		{`INSERT INTO memberships (channel_id, participant_id, respond_policy, joined_at) VALUES ('group:ops', 'User_1', 'never', ?)`, []any{at}},
		{`INSERT INTO memberships (channel_id, participant_id, respond_policy, joined_at) VALUES ('group:ops', 'alice', 'never', ?)`, []any{at}},
		{`INSERT INTO memberships (channel_id, participant_id, respond_policy, joined_at) VALUES ('group:ops', 'ember-owl', 'always', ?)`, []any{at}},
		{`INSERT INTO memberships (channel_id, participant_id, respond_policy, joined_at) VALUES ('dm:User_1:ember-owl', 'User_1', 'always', ?)`, []any{at}},
		{`INSERT INTO memberships (channel_id, participant_id, respond_policy, joined_at) VALUES ('dm:User_1:ember-owl', 'ember-owl', 'always', ?)`, []any{at}},
		{`INSERT INTO membership_intervals (channel_id, participant_id, joined_at, left_at)
			SELECT channel_id, participant_id, joined_at, NULL FROM memberships`, nil},
		{`INSERT INTO messages (id, channel_id, sender_id, content, timestamp) VALUES ('old-1', 'group:ops', 'User_1', 'can anyone hear me?', ?)`,
			[]any{at.Add(time.Minute)}},
		{`INSERT INTO messages (id, channel_id, sender_id, content, timestamp) VALUES ('old-2', 'dm:User_1:ember-owl', 'User_1', 'hello', ?)`,
			[]any{at.Add(2 * time.Minute)}},
	} {
		_, err := db.Exec(stmt.sql, stmt.args...)
		require.NoError(t, err, stmt.sql)
	}
	require.NoError(t, db.Close())

	store, err := channels.NewSQLiteStore(path, channels.SQLiteOptions{MaxChannels: 50})
	require.NoError(t, err)
	t.Cleanup(func() { _ = store.Close() })
	logger := zap.NewNop()
	reg := registry.NewInMemoryRegistry(logger)
	srv, err := New("127.0.0.1:0", t.TempDir(), state.NewInMemoryStore(logger), reg,
		planner.NewYAMLPlanner(logger), logger,
		WithChannels(store, channels.NewChannelRouter(store, channels.NoopDispatcher{}, logger, nil)))
	require.NoError(t, err)
	return srv, reg, store
}

// Nothing stored becomes unreadable through the REST API.
func TestChannels_AStoredLooseParticipantIDStaysReadable(t *testing.T) {
	srv, reg, _ := earlierStoreServer(t)
	registerHealthyAgent(t, reg, "ember-owl", "Ember Owl")
	h := srv.Handler()

	rec := doRequest(h, http.MethodGet, "/api/v1/channels/group:ops", nil)
	require.Equal(t, http.StatusOK, rec.Code, "body=%s", rec.Body.String())
	assert.Contains(t, rec.Body.String(), `"id":"User_1"`, "the member is still listed")

	rec = doRequest(h, http.MethodGet, "/api/v1/channels/group:ops/messages", nil)
	require.Equal(t, http.StatusOK, rec.Code, "body=%s", rec.Body.String())
	assert.Contains(t, rec.Body.String(), `"sender_id":"User_1"`, "its message is still in the history")

	rec = doRequest(h, http.MethodGet, "/api/v1/agents/ember-owl/chat/history?user_id=User_1", nil)
	require.Equal(t, http.StatusOK, rec.Code, "body=%s", rec.Body.String())
	var history historyResponse
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &history))
	require.Len(t, history.Messages, 1, "the DM's history is still read")
	assert.Equal(t, "hello", history.Messages[0].Content)

	rec = doRequest(h, http.MethodDelete, "/api/v1/channels/group:ops/members/User_1", nil)
	assert.Equal(t, http.StatusNoContent, rec.Code, "and the member can be removed, to be added under a new id: %s", rec.Body.String())
}

// A stored member with a loose id cannot write, and the caller is told why.
func TestChannels_AStoredLooseParticipantIDCannotWrite(t *testing.T) {
	srv, reg, _ := earlierStoreServer(t)
	registerHealthyAgent(t, reg, "ember-owl", "Ember Owl")
	h := srv.Handler()
	post := func(path string, body any) (int, string) {
		raw, err := json.Marshal(body)
		require.NoError(t, err)
		rec := doRequest(h, http.MethodPost, path, raw)
		return rec.Code, rec.Body.String()
	}

	code, body := post("/api/v1/channels/group:ops/messages", publishMessageRequest{SenderID: "User_1", Content: "still here"})
	requireRefusedByTheRule(t, code, body, "User_1")

	// The DM exists, so the chat route finds it; the new message is refused.
	// This used to fall to the route's catch-all and answer 500.
	code, body = post("/api/v1/agents/ember-owl/chat", chatRequest{Message: "hello again", UserID: "User_1"})
	requireRefusedByTheRule(t, code, body, "User_1")

	// No DM yet: creating one is the write that is refused.
	code, body = post("/api/v1/agents/ember-owl/chat", chatRequest{Message: "hello", UserID: "New_User"})
	requireRefusedByTheRule(t, code, body, "New_User")
}

// A message that names a stored loose id in its text is not refused for it:
// the name is left as text and not turned into a mention.
func TestChannels_TextThatNamesAStoredLooseIDIsNotLiftedIntoAMention(t *testing.T) {
	srv, _, _ := earlierStoreServer(t)
	h := srv.Handler()

	raw, err := json.Marshal(publishMessageRequest{SenderID: "alice", Content: "thanks @User_1 and @ember-owl, noted"})
	require.NoError(t, err)
	rec := doRequest(h, http.MethodPost, "/api/v1/channels/group:ops/messages", raw)

	require.Equal(t, http.StatusCreated, rec.Code, "body=%s", rec.Body.String())
	var stored channelMessageResponse
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &stored))
	assert.Equal(t, []string{"ember-owl"}, stored.Mentions,
		"the member whose id fits the rule is lifted as before; the loose one stays text")
	assert.Equal(t, "thanks @User_1 and @ember-owl, noted", stored.Content)
}
