package server

// RFC 0036 PR 5 (Phase 3) — the `?as_participant=` membership filter on
// `GET /api/v1/channels/{id}/messages`.
//
// PR 5's store half ([channels.ChannelStore.GetHistoryScoped]) is proven at the
// store level in internal/channels/sqlite_history_scoped_test.go. These tests pin
// only what the HANDLER adds: routing the GET to the scoped query when (and only
// when) `?as_participant=` is present, so a persona's live window obeys the same
// membership scope recall does, while human/CLI callers that omit the param see
// the unchanged full history.
//
// Fixtures reuse the recall handler suite's synthetic seeding (`withRecallDB` /
// `recallSeedInterval` / `recallSeedMsg`) so the join → leave → rejoin geometry is
// deterministic — the real AddMember/RemoveMember path stamps wall-clock
// boundaries, which cannot place a message precisely in the removal gap.
import (
	"database/sql"
	"encoding/json"
	"net/http"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"

	"github.com/mkhomutov/persatrix/internal/channels"
)

func historyRespIDs(t *testing.T, rec interface{ String() string }) []string {
	t.Helper()
	var resp historyResponse
	require.NoError(t, json.Unmarshal([]byte(rec.String()), &resp))
	out := make([]string, len(resp.Messages))
	for i, m := range resp.Messages {
		out[i] = m.ID
	}
	return out
}

// seedScopedHistoryFixture creates `group:planning` and seeds alice with a closed
// stint [60,120) + an open stint [240,∞) plus one message in each region. Returns
// the channel id and the time helper.
func seedScopedHistoryFixture(t *testing.T, store channels.ChannelStore, dbPath string) (string, func(int) time.Time) {
	t.Helper()
	ch := "group:planning"
	require.NoError(t, store.CreateChannel(t.Context(),
		channels.Channel{ID: ch, Name: "planning", Type: channels.ChannelTypeGroup}))
	base := time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)
	at := func(m int) time.Time { return base.Add(time.Duration(m) * time.Minute) }

	withRecallDB(t, dbPath, func(db *sql.DB) {
		recallSeedInterval(t, db, ch, "alice", at(60), at(120)) // stint 1 closed
		recallSeedInterval(t, db, ch, "alice", at(240), nil)    // stint 2 open
		recallSeedMsg(t, db, "m-before", ch, "bob", "before join", at(30), "")
		recallSeedMsg(t, db, "m-stint1", ch, "bob", "stint one", at(90), "")
		recallSeedMsg(t, db, "m-gap", ch, "bob", "in the gap", at(180), "")
		recallSeedMsg(t, db, "m-stint2", ch, "bob", "stint two", at(300), "")
	})
	return ch, at
}

// TestHistoryEndpoint_AsParticipant_ScopesToMembership pins the headline: with
// `?as_participant=alice` the window is scoped to alice's stints — both stints'
// messages are returned and the pre-join prefix + removal gap are excluded. This
// is the live-prompt half of the "scoped to where you were present" promise.
func TestHistoryEndpoint_AsParticipant_ScopesToMembership(t *testing.T) {
	srv, store, dbPath, _ := recallTestServer(t)
	ch, _ := seedScopedHistoryFixture(t, store, dbPath)

	rec := doRequest(srv.Handler(), http.MethodGet,
		"/api/v1/channels/"+ch+"/messages?as_participant=alice", nil)
	require.Equal(t, http.StatusOK, rec.Code, "body=%s", rec.Body.String())

	assert.ElementsMatch(t, []string{"m-stint1", "m-stint2"}, historyRespIDs(t, rec.Body),
		"scoped window shows both stints; pre-join prefix and removal gap excluded")
}

// TestHistoryEndpoint_NoAsParticipant_ReturnsFullHistory pins the human/CLI
// path: omitting `as_participant` is byte-identical to today — the full channel
// history, including the gap and pre-join rows a scoped persona never sees.
func TestHistoryEndpoint_NoAsParticipant_ReturnsFullHistory(t *testing.T) {
	srv, store, dbPath, _ := recallTestServer(t)
	ch, _ := seedScopedHistoryFixture(t, store, dbPath)

	rec := doRequest(srv.Handler(), http.MethodGet,
		"/api/v1/channels/"+ch+"/messages", nil)
	require.Equal(t, http.StatusOK, rec.Code, "body=%s", rec.Body.String())

	assert.ElementsMatch(t,
		[]string{"m-before", "m-stint1", "m-gap", "m-stint2"}, historyRespIDs(t, rec.Body),
		"unscoped history is unchanged — every row, no membership filter")
}

// TestHistoryEndpoint_AsParticipant_NonMemberEmpty pins that the scope is real,
// not advisory: a participant with no membership interval in the channel gets an
// empty window even though the channel has messages. The path id is the
// access-control subject, exactly as on the recall endpoint.
func TestHistoryEndpoint_AsParticipant_NonMemberEmpty(t *testing.T) {
	srv, store, dbPath, _ := recallTestServer(t)
	ch, _ := seedScopedHistoryFixture(t, store, dbPath)

	rec := doRequest(srv.Handler(), http.MethodGet,
		"/api/v1/channels/"+ch+"/messages?as_participant=stranger", nil)
	require.Equal(t, http.StatusOK, rec.Code, "body=%s", rec.Body.String())

	assert.Empty(t, historyRespIDs(t, rec.Body),
		"a non-member as_participant sees nothing — scope is enforced, not optional")
}

// TestHistoryEndpoint_AsParticipant_RespectsLimit pins that the scoped path still
// honours `?limit=`, newest-first — the param composes with the membership scope.
func TestHistoryEndpoint_AsParticipant_RespectsLimit(t *testing.T) {
	srv, store, dbPath, _ := recallTestServer(t)
	ch, _ := seedScopedHistoryFixture(t, store, dbPath)

	rec := doRequest(srv.Handler(), http.MethodGet,
		"/api/v1/channels/"+ch+"/messages?as_participant=alice&limit=1", nil)
	require.Equal(t, http.StatusOK, rec.Code, "body=%s", rec.Body.String())

	assert.Equal(t, []string{"m-stint2"}, historyRespIDs(t, rec.Body),
		"limit applies to the scoped window, newest-first")
}
