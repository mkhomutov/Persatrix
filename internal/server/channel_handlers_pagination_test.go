// channel_handlers_pagination_test.go — handler-level pagination tests
// for GET /api/v1/channels (ISSUE-0015). The endpoint used to load every
// row and truncate client-side; clients had no signal that more pages
// existed once a deployment exceeded the 50-channel soft cap. These
// tests pin the `next_cursor` envelope contract.
package server

import (
	"encoding/json"
	"fmt"
	"net/http"
	"net/url"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// TestChannels_ListChannels_NextCursor_PresentWhenMoreRows pins the
// regression target from ISSUE-0015's acceptance: create `limit + 1`
// channels and assert the response surfaces a non-empty `next_cursor`
// pointing at the last row of the returned page.
func TestChannels_ListChannels_NextCursor_PresentWhenMoreRows(t *testing.T) {
	srv, _ := channelTestServer(t)
	// Six channels with deterministic lex-ordered names; with limit=5
	// the sixth must remain unreturned and surface as a cursor.
	for _, n := range []string{"a01", "a02", "a03", "a04", "a05", "a06"} {
		body, _ := json.Marshal(createChannelRequest{
			Name:    n,
			Members: []channelMemberRequest{{ID: "alice"}},
		})
		require.Equal(t, http.StatusCreated,
			doRequest(srv.Handler(), http.MethodPost, "/api/v1/channels", body).Code,
			"create %s", n)
	}

	rec := doRequest(srv.Handler(), http.MethodGet, "/api/v1/channels?limit=5", nil)
	require.Equal(t, http.StatusOK, rec.Code)
	var resp listChannelsResponse
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &resp))
	require.Len(t, resp.Channels, 5)
	require.NotEmpty(t, resp.NextCursor,
		"next_cursor must be set when more rows exist after the page")
	assert.Equal(t, resp.Channels[len(resp.Channels)-1].ID, resp.NextCursor,
		"cursor must address the last returned row so a follow-up `?cursor=` "+
			"query starts strictly past it")
}

// TestChannels_ListChannels_NextCursor_AbsentOnLastPage pins the inverse:
// when fewer rows remain than the requested limit, `next_cursor` must
// be omitted (or empty) so clients stop paginating.
func TestChannels_ListChannels_NextCursor_AbsentOnLastPage(t *testing.T) {
	srv, _ := channelTestServer(t)
	for _, n := range []string{"a01", "a02"} {
		body, _ := json.Marshal(createChannelRequest{
			Name:    n,
			Members: []channelMemberRequest{{ID: "alice"}},
		})
		require.Equal(t, http.StatusCreated,
			doRequest(srv.Handler(), http.MethodPost, "/api/v1/channels", body).Code)
	}

	rec := doRequest(srv.Handler(), http.MethodGet, "/api/v1/channels?limit=10", nil)
	require.Equal(t, http.StatusOK, rec.Code)
	var resp listChannelsResponse
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &resp))
	require.Len(t, resp.Channels, 2)
	assert.Empty(t, resp.NextCursor,
		"next_cursor must be empty when the page returns the trailing rows")
}

// TestChannels_ListChannels_CursorWalk pins the round-trip: take the
// `next_cursor` from page 1, pass it as `?cursor=`, receive the next
// page with no rows duplicated and no rows skipped.
func TestChannels_ListChannels_CursorWalk(t *testing.T) {
	srv, _ := channelTestServer(t)
	names := []string{"a01", "a02", "a03", "a04", "a05"}
	for _, n := range names {
		body, _ := json.Marshal(createChannelRequest{
			Name:    n,
			Members: []channelMemberRequest{{ID: "alice"}},
		})
		require.Equal(t, http.StatusCreated,
			doRequest(srv.Handler(), http.MethodPost, "/api/v1/channels", body).Code)
	}

	page1Rec := doRequest(srv.Handler(), http.MethodGet, "/api/v1/channels?limit=2", nil)
	require.Equal(t, http.StatusOK, page1Rec.Code)
	var page1 listChannelsResponse
	require.NoError(t, json.Unmarshal(page1Rec.Body.Bytes(), &page1))
	require.Len(t, page1.Channels, 2)
	require.NotEmpty(t, page1.NextCursor)

	page2Rec := doRequest(srv.Handler(), http.MethodGet,
		fmt.Sprintf("/api/v1/channels?limit=2&cursor=%s", url.QueryEscape(page1.NextCursor)), nil)
	require.Equal(t, http.StatusOK, page2Rec.Code)
	var page2 listChannelsResponse
	require.NoError(t, json.Unmarshal(page2Rec.Body.Bytes(), &page2))
	require.Len(t, page2.Channels, 2)
	require.NotEmpty(t, page2.NextCursor)

	page3Rec := doRequest(srv.Handler(), http.MethodGet,
		fmt.Sprintf("/api/v1/channels?limit=2&cursor=%s", url.QueryEscape(page2.NextCursor)), nil)
	require.Equal(t, http.StatusOK, page3Rec.Code)
	var page3 listChannelsResponse
	require.NoError(t, json.Unmarshal(page3Rec.Body.Bytes(), &page3))
	require.Len(t, page3.Channels, 1, "last page contains the trailing row")
	assert.Empty(t, page3.NextCursor, "trailing page must not advertise a next cursor")

	// Union of all pages == every channel created, no duplicates.
	collected := make(map[string]struct{})
	for _, p := range []listChannelsResponse{page1, page2, page3} {
		for _, c := range p.Channels {
			_, dup := collected[c.ID]
			require.False(t, dup, "row %s observed across two pages", c.ID)
			collected[c.ID] = struct{}{}
		}
	}
	require.Len(t, collected, len(names))
}
