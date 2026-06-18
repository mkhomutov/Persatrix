package server

import (
	"context"
	"encoding/json"
	"net/http"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"

	"github.com/mkhomutov/persatrix/internal/channels"
)

// TestChannels_MembershipHistory_TwoStints pins the Phase 2 inspection happy
// path: a participant added → removed → re-added reads back two intervals in
// joined_at order — the first closed (carries `left_at`), the second open (omits
// `left_at`). GET /api/v1/channels/{id}/members/{participant_id}/history.
func TestChannels_MembershipHistory_TwoStints(t *testing.T) {
	srv, store := channelTestServer(t)
	ctx := context.Background()
	require.NoError(t, store.CreateChannel(ctx, channels.Channel{
		ID: "group:planning", Name: "planning", Type: channels.ChannelTypeGroup,
	}))
	require.NoError(t, store.AddMember(ctx, "group:planning", "alice", channels.RespondWhenMentioned))
	require.NoError(t, store.RemoveMember(ctx, "group:planning", "alice"))
	require.NoError(t, store.AddMember(ctx, "group:planning", "alice", channels.RespondWhenMentioned))

	rec := doRequest(srv.Handler(), http.MethodGet,
		"/api/v1/channels/group:planning/members/alice/history", nil)
	require.Equal(t, http.StatusOK, rec.Code, "body=%s", rec.Body.String())

	var resp membershipHistoryResponse
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &resp))
	require.Len(t, resp.Intervals, 2)

	// [0] is the earlier, closed stint — left_at present and after the join.
	require.NotNil(t, resp.Intervals[0].LeftAt, "the first stint is closed")
	assert.False(t, resp.Intervals[0].JoinedAt.IsZero())
	assert.True(t, resp.Intervals[0].LeftAt.After(resp.Intervals[0].JoinedAt))

	// [1] is the later, open stint — left_at omitted (nil after unmarshal).
	assert.Nil(t, resp.Intervals[1].LeftAt, "the second stint is open")
	assert.True(t, resp.Intervals[1].JoinedAt.After(*resp.Intervals[0].LeftAt),
		"the re-add join is after the first leave")
}

// TestChannels_MembershipHistory_NoIntervalsIsEmpty200 pins that a KNOWN channel
// the participant was never in returns an empty list at 200 — not 404. The
// existence check is on the channel; an absent membership is a clean empty
// history, consistent with the read-only "history" framing.
func TestChannels_MembershipHistory_NoIntervalsIsEmpty200(t *testing.T) {
	srv, store := channelTestServer(t)
	require.NoError(t, store.CreateChannel(context.Background(), channels.Channel{
		ID: "group:planning", Name: "planning", Type: channels.ChannelTypeGroup,
	}))

	rec := doRequest(srv.Handler(), http.MethodGet,
		"/api/v1/channels/group:planning/members/ghost/history", nil)
	require.Equal(t, http.StatusOK, rec.Code, "body=%s", rec.Body.String())

	var resp membershipHistoryResponse
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &resp))
	assert.Empty(t, resp.Intervals)
	assert.NotNil(t, resp.Intervals, "intervals is always an array, never null")
}

// TestChannels_MembershipHistory_UnknownChannel404 pins ErrChannelNotFound → 404,
// matching the existing channel-handler convention (a missing channel is a 404,
// a present channel with no membership is an empty 200).
func TestChannels_MembershipHistory_UnknownChannel404(t *testing.T) {
	srv, _ := channelTestServer(t)
	rec := doRequest(srv.Handler(), http.MethodGet,
		"/api/v1/channels/group:nope/members/alice/history", nil)
	assert.Equal(t, http.StatusNotFound, rec.Code)
}
