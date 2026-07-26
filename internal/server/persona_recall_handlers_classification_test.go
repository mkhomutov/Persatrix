package server

// RFC 0037 PR 5 — the recall endpoint's reworked request surface: the REQUIRED
// §F `acting_classification` parameter and the ISSUE-0106(b) removal of the
// `epoch_id` body override. Carved out of persona_recall_handlers_test.go
// (which sits at the repo's 500-line cap) per the established split; fixtures
// (recallTestServer, withRecallDB, recallSeed*, recallRespIDs, readAuditEvents,
// filterRecallEvents, recallPath) are shared from that file in the same
// package. The store-level §F matrix (all four levels, rule (b)/(c) fail
// directions, the LIKE fallback) is pinned in
// internal/channels/sqlite_search_classification_test.go; here only what the
// HANDLER adds is asserted: validation, the pointed rejections, the cap
// working end-to-end through REST, and the audit detail.

import (
	"context"
	"database/sql"
	"encoding/json"
	"net/http"
	"strings"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"

	"github.com/mkhomutov/persatrix/internal/channels"
)

// TestRecallEndpoint_ActingClassificationRequired pins the fail-loud contract:
// a request with no `acting_classification` is a 400 naming the requirement —
// never a silent rule-(b) public-floor (which would return empty everywhere on
// an `internal`-default store with no signal to the caller) — and a value
// outside the §A vocabulary is a 400 naming the vocabulary. Neither failure
// emits an audit event (executed reads only).
func TestRecallEndpoint_ActingClassificationRequired(t *testing.T) {
	srv, store, dbPath, auditor := recallTestServer(t)
	ch := "group:planning"
	require.NoError(t, store.CreateChannel(context.Background(),
		channels.Channel{ID: ch, Name: "planning", Type: channels.ChannelTypeGroup}))
	base := time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)
	withRecallDB(t, dbPath, func(db *sql.DB) {
		recallSeedInterval(t, db, ch, "alice", base, nil)
		recallSeedMsg(t, db, "m1", ch, "bob", "budget one", base.Add(time.Minute), "")
	})

	// Absent → 400 with the pointed requirement.
	body, _ := json.Marshal(recallRequest{Query: "budget"})
	rec := doRequest(srv.Handler(), http.MethodPost, recallPath, body)
	assert.Equal(t, http.StatusBadRequest, rec.Code)
	assert.Contains(t, rec.Body.String(), "acting_classification is required",
		"the 400 names the missing required parameter")

	// Outside the §A vocabulary → 400 naming the vocabulary. "sekrit" is a
	// plausible typo; the §A levels are exact and case-sensitive.
	for _, bad := range []string{"sekrit", "SECRET", "confidential"} {
		body, _ = json.Marshal(recallRequest{ActingClassification: bad, Query: "budget"})
		rec = doRequest(srv.Handler(), http.MethodPost, recallPath, body)
		assert.Equalf(t, http.StatusBadRequest, rec.Code, "level %q", bad)
		assert.Containsf(t, rec.Body.String(), "invalid acting_classification",
			"the 400 names the invalid level (%q)", bad)
	}

	// Positive control: a valid level on the same fixture succeeds.
	body, _ = json.Marshal(recallRequest{ActingClassification: "internal", Query: "budget"})
	rec = doRequest(srv.Handler(), http.MethodPost, recallPath, body)
	require.Equal(t, http.StatusOK, rec.Code, "body=%s", rec.Body.String())

	// Only the executed recall is audited — none of the 400s above.
	require.NoError(t, auditor.Flush())
	recalls := filterRecallEvents(readAuditEvents(t, auditor.Path()))
	assert.Len(t, recalls, 1, "validation failures emit no channel.recall event (executed reads only)")
}

// TestRecallEndpoint_EpochOverrideRemoved_PointedRejection pins ISSUE-0106(b):
// ANY `epoch_id` in the body — the "live" default included, and even an empty
// string — is a 400 with a message naming the issue, not silently ignored.
// Silent acceptance would imply an isolation axis that does not exist (the
// store is not epoch-partitioned; separate runs never share a DB), so an old
// caller breaks loudly with directions instead of getting subtly re-scoped
// results. The rejection happens before the store query, so no audit event.
func TestRecallEndpoint_EpochOverrideRemoved_PointedRejection(t *testing.T) {
	srv, _, _, auditor := recallTestServer(t)

	for _, epoch := range []string{"ci-run-7", "live", ""} {
		// Hand-built JSON: the request struct's *string field is how the handler
		// DETECTS presence; the wire form an old caller actually sends is a plain
		// string field, present-but-empty included.
		body := []byte(`{"query":"budget","acting_classification":"internal","epoch_id":"` + epoch + `"}`)
		rec := doRequest(srv.Handler(), http.MethodPost, recallPath, body)
		assert.Equalf(t, http.StatusBadRequest, rec.Code, "epoch_id=%q", epoch)
		assert.Containsf(t, rec.Body.String(), "ISSUE-0106",
			"the 400 points the old caller at the removal rationale (epoch_id=%q)", epoch)
	}

	require.NoError(t, auditor.Flush())
	assert.Empty(t, filterRecallEvents(readAuditEvents(t, auditor.Path())),
		"rejected epoch_id requests emit no channel.recall event")
}

// TestRecallEndpoint_ClassificationCap_EndToEnd pins the §F cap through REST:
// with a `secret` and an `internal` channel both inside alice's membership,
// acting `internal` serves only the internal channel's message and acting
// `secret` serves both — and every executed recall's audit detail records the
// acting level the search ran at, so the trail shows not just that a verbatim
// read happened but at what confidentiality cap.
func TestRecallEndpoint_ClassificationCap_EndToEnd(t *testing.T) {
	srv, store, dbPath, auditor := recallTestServer(t)
	ctx := context.Background()
	require.NoError(t, store.CreateChannel(ctx, channels.Channel{
		ID: "group:planning", Name: "planning", Type: channels.ChannelTypeGroup,
		Classification: channels.ClassificationInternal,
	}))
	require.NoError(t, store.CreateChannel(ctx, channels.Channel{
		ID: "group:warroom", Name: "warroom", Type: channels.ChannelTypeGroup,
		Classification: channels.ClassificationSecret,
	}))
	base := time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)
	withRecallDB(t, dbPath, func(db *sql.DB) {
		recallSeedInterval(t, db, "group:planning", "alice", base, nil)
		recallSeedInterval(t, db, "group:warroom", "alice", base, nil)
		recallSeedMsg(t, db, "m-int", "group:planning", "bob", "budget planning", base.Add(time.Minute), "")
		recallSeedMsg(t, db, "m-sec", "group:warroom", "bob", "budget warroom", base.Add(2*time.Minute), "")
	})

	recallActing := func(level string) []string {
		body, _ := json.Marshal(recallRequest{ActingClassification: level, Query: "budget"})
		rec := doRequest(srv.Handler(), http.MethodPost, recallPath, body)
		require.Equal(t, http.StatusOK, rec.Code, "body=%s", rec.Body.String())
		var resp recallResponse
		require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &resp))
		return recallRespIDs(resp)
	}

	assert.ElementsMatch(t, []string{"m-int"}, recallActing("internal"),
		"acting internal: the secret-channel message is withheld through REST")
	assert.ElementsMatch(t, []string{"m-int", "m-sec"}, recallActing("secret"),
		"acting secret: both member channels are served")

	require.NoError(t, auditor.Flush())
	recalls := filterRecallEvents(readAuditEvents(t, auditor.Path()))
	require.Len(t, recalls, 2)
	assert.Equal(t, "internal", recalls[0].Detail["acting_classification"],
		"the audit records the acting level the search ran at")
	assert.Equal(t, "secret", recalls[1].Detail["acting_classification"])

	// The withheld content never appears in the audit trail either — the §F
	// exclusion composes with the existing count-not-content contract.
	for _, ev := range recalls {
		for _, v := range ev.Detail {
			if s, ok := v.(string); ok {
				assert.False(t, strings.Contains(s, "budget warroom"),
					"withheld secret-channel content must not surface in audit detail")
			}
		}
	}
}
