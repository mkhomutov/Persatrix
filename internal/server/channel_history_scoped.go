package server

// RFC 0036 PR 5 (Phase 3) — the `?as_participant=` membership filter on the
// channel-history GET. Split from channel_handlers.go (at the 500-line review
// cap) so handleGetChannelHistory's host file takes only a one-line call-site
// swap, the rest of the routing logic living here.

import (
	"context"
	"net/http"
	"strings"
	"time"

	"github.com/mkhomutov/persatrix/internal/channels"
)

// loadChannelHistory selects between the unscoped and the membership-scoped
// history query for [Server.handleGetChannelHistory], based on the optional
// `?as_participant=<id>` query parameter (RFC 0036 §G).
//
// Absent or blank — the human / CLI path — routes to the unchanged
// [channels.ChannelStore.GetHistory], so those callers see the full channel
// history byte-for-byte as before (OQ #4: no operator opt-in, the param is the
// only switch). Present — the persona-runtime path — routes to
// [channels.ChannelStore.GetHistoryScoped], which trims the result to the
// participant's `membership_intervals` stints in the persisted "live" epoch, so
// a re-added persona's live window excludes its removal-gap messages.
//
// The scope subject is the query-param VALUE, never a body field, and the
// store's `EXISTS` join is the access decision — the same predicate recall
// applies (§OQ-6). A blank value (`?as_participant=`) is treated as absent, not
// as a participant whose id is the empty string: GetHistoryScoped would reject
// the empty id, but more to the point a stray empty param must not silently
// switch a human caller onto the scoped path.
func (s *Server) loadChannelHistory(
	ctx context.Context, r *http.Request, channelID string, limit int, before time.Time,
) ([]channels.ChannelMessage, error) {
	if as := strings.TrimSpace(r.URL.Query().Get("as_participant")); as != "" {
		return s.channelStore.GetHistoryScoped(ctx, channelID, as, limit, before)
	}
	return s.channelStore.GetHistory(ctx, channelID, limit, before)
}
