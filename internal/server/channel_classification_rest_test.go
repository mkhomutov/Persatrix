// channel_classification_rest_test.go — RFC 0037 §B (v0.3.12 PR 2): the REST
// leg of the two-path wire contract. The history and thread envelopes carry
// the channel's §A level (the value on-startup catch-up replay stamps onto
// rebuilt events — `agents/channel_catchup.py`), and the channel object
// carries it on the list/create surface the catch-up channel walk reads.
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

// TestChannels_HistoryEnvelopeCarriesClassification pins the history +
// thread envelopes: channel-level (one value per envelope, never per
// message — a message's classification IS its channel's, §H), read from the
// row the handlers already fetch.
func TestChannels_HistoryEnvelopeCarriesClassification(t *testing.T) {
	srv, store := channelTestServer(t)
	createBody, _ := json.Marshal(createChannelRequest{
		Name:    "leadership",
		Members: []channelMemberRequest{{ID: "alice"}, {ID: "bob"}},
	})
	require.Equal(t, http.StatusCreated,
		doRequest(srv.Handler(), http.MethodPost, "/api/v1/channels", createBody).Code)
	require.NoError(t, store.SetChannelClassification(
		context.Background(), "group:leadership", channels.ClassificationRestricted))

	pubBody, _ := json.Marshal(publishMessageRequest{SenderID: "alice", Content: "quarterly plan"})
	pubRec := doRequest(srv.Handler(), http.MethodPost, "/api/v1/channels/group:leadership/messages", pubBody)
	require.Equal(t, http.StatusCreated, pubRec.Code)
	var msg channelMessageResponse
	require.NoError(t, json.Unmarshal(pubRec.Body.Bytes(), &msg))

	histRec := doRequest(srv.Handler(), http.MethodGet, "/api/v1/channels/group:leadership/messages", nil)
	require.Equal(t, http.StatusOK, histRec.Code)
	var hist historyResponse
	require.NoError(t, json.Unmarshal(histRec.Body.Bytes(), &hist))
	assert.Equal(t, "restricted", hist.Classification,
		"the history envelope must carry the channel's §A level for catch-up stamping")

	threadRec := doRequest(srv.Handler(), http.MethodGet,
		"/api/v1/channels/group:leadership/messages/"+msg.ID+"/thread", nil)
	require.Equal(t, http.StatusOK, threadRec.Code)
	var thread historyResponse
	require.NoError(t, json.Unmarshal(threadRec.Body.Bytes(), &thread))
	assert.Equal(t, "restricted", thread.Classification,
		"a thread is never more or less confidential than the channel it forks from (§B/§H)")
}

// TestChannels_ChannelObjectCarriesClassification pins the create/list
// surface: a fresh channel reads back `internal` (the §A rule-(a) stamp) —
// the catch-up channel walk and the operator opt-in path both read this
// field.
func TestChannels_ChannelObjectCarriesClassification(t *testing.T) {
	srv, _ := channelTestServer(t)
	createBody, _ := json.Marshal(createChannelRequest{
		Name:    "planning",
		Members: []channelMemberRequest{{ID: "alice"}},
	})
	rec := doRequest(srv.Handler(), http.MethodPost, "/api/v1/channels", createBody)
	require.Equal(t, http.StatusCreated, rec.Code)
	var created channelResponse
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &created))
	assert.Equal(t, "internal", created.Classification,
		"a classification-unaware REST create stamps `internal`, and the response reads it back")

	listRec := doRequest(srv.Handler(), http.MethodGet, "/api/v1/channels", nil)
	require.Equal(t, http.StatusOK, listRec.Code)
	var list listChannelsResponse
	require.NoError(t, json.Unmarshal(listRec.Body.Bytes(), &list))
	require.Len(t, list.Channels, 1)
	assert.Equal(t, "internal", list.Channels[0].Classification)
}
