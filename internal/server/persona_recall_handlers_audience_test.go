package server

// ISSUE-0158 — the recall endpoint accepts the acting channel id the persona
// tool binds from its turn and applies the §F audience condition server-side.

import (
	"context"
	"database/sql"
	"encoding/json"
	"net/http"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"

	"github.com/mkhomutov/persatrix/internal/channels"
)

func TestRecallEndpoint_ActingChannelAudience(t *testing.T) {
	srv, store, dbPath, auditor := recallTestServer(t)
	ctx := context.Background()
	dm, planning := "dm:alice:alice-bot", "group:planning"
	require.NoError(t, store.CreateChannel(ctx, channels.Channel{
		ID: dm, Name: dm, Type: channels.ChannelTypeDM, Classification: channels.ClassificationInternal}))
	require.NoError(t, store.CreateChannel(ctx, channels.Channel{
		ID: planning, Name: "planning", Type: channels.ChannelTypeGroup, Classification: channels.ClassificationInternal}))
	base := time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)
	withRecallDB(t, dbPath, func(db *sql.DB) {
		for _, row := range [][2]string{{dm, "alice-bot"}, {dm, "alice"}, {planning, "alice"}, {planning, "bob"}, {planning, "alice-bot"}} {
			_, err := db.Exec(`INSERT INTO memberships (channel_id, participant_id, respond_policy, joined_at)
			                   VALUES (?, ?, 'when_mentioned', ?)`, row[0], row[1], base)
			require.NoError(t, err)
		}
		// recallPath scopes the read to participant "alice".
		recallSeedInterval(t, db, dm, "alice", base, nil)
		recallSeedInterval(t, db, planning, "alice", base, nil)
		recallSeedMsg(t, db, "m-dm", dm, "alice", "helix paused", base.Add(time.Minute), "")
		recallSeedMsg(t, db, "m-planning", planning, "alice", "helix board", base.Add(2*time.Minute), "")
	})

	post := func(body map[string]any) (int, []string) {
		raw, _ := json.Marshal(body)
		rec := doRequest(srv.Handler(), http.MethodPost, recallPath, raw)
		var resp recallResponse
		_ = json.Unmarshal(rec.Body.Bytes(), &resp)
		out := make([]string, 0, len(resp.Messages))
		for _, m := range resp.Messages {
			out = append(out, m.MessageID)
		}
		return rec.Code, out
	}

	// A bare group name canonicalises like `channel_id` does (ISSUE-0107).
	code, got := post(map[string]any{"query": "helix", "acting_classification": "internal", "acting_channel_id": "planning"})
	require.Equal(t, http.StatusOK, code)
	assert.ElementsMatch(t, []string{"m-planning"}, got, "the DM's transcript stays out of a room that adds bob")

	code, got = post(map[string]any{"query": "helix", "acting_classification": "internal", "acting_channel_id": dm})
	require.Equal(t, http.StatusOK, code)
	assert.ElementsMatch(t, []string{"m-dm", "m-planning"}, got, "acting in the DM admits both")

	code, got = post(map[string]any{"query": "helix", "acting_classification": "internal"})
	require.Equal(t, http.StatusOK, code)
	assert.ElementsMatch(t, []string{"m-dm", "m-planning"}, got, "no acting channel: unchanged")

	require.NoError(t, auditor.Flush())
	recalls := filterRecallEvents(readAuditEvents(t, auditor.Path()))
	require.Len(t, recalls, 3)
	assert.Equal(t, "group:planning", recalls[0].Detail["acting_channel_id"],
		"the audit names the acting room the read was scoped to")
	_, absent := recalls[2].Detail["acting_channel_id"]
	assert.False(t, absent, "an unscoped read records no acting channel")
}
