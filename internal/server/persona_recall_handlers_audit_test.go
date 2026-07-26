package server

// RFC 0036 PR 3 — audit-trail tests for the recall endpoint, carved out of
// persona_recall_handlers_test.go so both stay under the repo's 500-line code cap
// (the same split rationale that put the handler in its own file). These pin the
// two PR #677 review findings that changed or fixed audit behaviour; the scope /
// epoch / shape / content-exclusion tests stay in the sibling file. Fixtures and
// helpers (recallTestServer, withRecallDB, recallSeed*, readAuditEvents,
// filterRecallEvents, recallPath) are shared from that file in the same package.

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

// TestRecallEndpoint_AuditRecordsEffectiveLimit pins PR #677 review finding #2:
// the audit's `limit` is the EFFECTIVE cap the store actually applied — the value
// that bounds `result_count` — not the raw request, so an auditor reading
// `result_count == limit` can tell the set was truncated. Two cases the prior
// "record the request only when > 0" behaviour got wrong, asserted through the
// endpoint:
//
//   - unset limit: the store applies [channels.DefaultRecallLimit], but the audit
//     recorded no `limit` at all, leaving `result_count` uninterpretable;
//   - over-max limit: the store clamps to [channels.MaxRecallLimit], but the audit
//     recorded the larger request, so a clamped result read as un-truncated.
func TestRecallEndpoint_AuditRecordsEffectiveLimit(t *testing.T) {
	srv, store, dbPath, auditor := recallTestServer(t)
	ch := "group:planning"
	require.NoError(t, store.CreateChannel(context.Background(),
		channels.Channel{ID: ch, Name: "planning", Type: channels.ChannelTypeGroup}))
	base := time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)
	withRecallDB(t, dbPath, func(db *sql.DB) {
		recallSeedInterval(t, db, ch, "alice", base, nil)
		recallSeedMsg(t, db, "m1", ch, "bob", "budget one", base.Add(time.Minute), "")
		recallSeedMsg(t, db, "m2", ch, "bob", "budget two", base.Add(2*time.Minute), "")
	})

	// Unset limit → the store applies DefaultRecallLimit; the audit must name it,
	// not omit it.
	body, _ := json.Marshal(recallRequest{ActingClassification: "internal", Query: "budget"})
	rec := doRequest(srv.Handler(), http.MethodPost, recallPath, body)
	require.Equal(t, http.StatusOK, rec.Code, "body=%s", rec.Body.String())
	require.NoError(t, auditor.Flush())
	recalls := filterRecallEvents(readAuditEvents(t, auditor.Path()))
	require.Len(t, recalls, 1)
	assert.EqualValues(t, channels.DefaultRecallLimit, recalls[0].Detail["limit"],
		"an unset limit is audited as the effective DefaultRecallLimit, never omitted")

	// Over-max limit → the store clamps to MaxRecallLimit; the audit must name the
	// clamped value, not the larger request.
	body, _ = json.Marshal(recallRequest{ActingClassification: "internal", Query: "budget", Limit: 10_000})
	rec = doRequest(srv.Handler(), http.MethodPost, recallPath, body)
	require.Equal(t, http.StatusOK, rec.Code, "body=%s", rec.Body.String())
	require.NoError(t, auditor.Flush())
	recalls = filterRecallEvents(readAuditEvents(t, auditor.Path()))
	require.Len(t, recalls, 2)
	assert.EqualValues(t, channels.MaxRecallLimit, recalls[1].Detail["limit"],
		"an over-max limit is audited as the clamped MaxRecallLimit, not the request")
}

// TestRecallEndpoint_FailedAttemptNotAudited pins PR #677 review finding #1 as a
// DELIBERATE boundary: the `channel.recall` trail records executed reads, not
// attempts. A request that fails before the store query runs (here a malformed
// body → 400) emits NO audit event — even though the auditor is fully wired and
// flushes eagerly. This is intentional: a failed call read nothing, and auditing
// attacker-controlled malformed input would let an unauthenticated caller inflate
// the trail at will. The day attempts SHOULD be audited (with real attribution,
// once RFC 0009 lands), this test flips red so the change is made consciously
// rather than by drift.
func TestRecallEndpoint_FailedAttemptNotAudited(t *testing.T) {
	srv, _, _, auditor := recallTestServer(t) // auditor wired and able to emit
	rec := doRequest(srv.Handler(), http.MethodPost, recallPath, []byte("{not valid json"))
	require.Equal(t, http.StatusBadRequest, rec.Code)

	require.NoError(t, auditor.Flush())
	recalls := filterRecallEvents(readAuditEvents(t, auditor.Path()))
	assert.Empty(t, recalls,
		"a recall that fails before the store query emits no channel.recall event (executed reads only)")
}
