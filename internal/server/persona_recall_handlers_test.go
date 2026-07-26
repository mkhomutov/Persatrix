package server

// RFC 0036 PR 3 — the audited REST surface over the membership-scoped,
// epoch-filtered verbatim search (PR 2's [channels.ChannelStore.RecallMessages]).
//
// PR 2 proved the scope/epoch/narrowing/ranking SQL at the store level. These
// tests pin only what the HANDLER adds on top:
//
//   - the PATH participant id is the scope subject (never a body field), so a
//     join → leave → rejoin recalls both stints and excludes the removal gap
//     through the HTTP layer;
//   - recall always binds the "live" epoch (the `epoch_id` body override was
//     removed by ISSUE-0106(b)) — a synthetically cross-epoch row is unreachable;
//   - the persona-facing response shape (message_id / sender);
//   - every executed recall emits exactly one server-side `channel.recall` audit
//     event recording the persona, query, narrowing params, and result COUNT —
//     and never the recalled content;
//   - the store-side `limit` clamp holds through the pass-through handler;
//   - a malformed body is a 400 and an unconfigured store is a 503.
//
// Fixtures seed `membership_intervals` + `messages` via direct SQL with
// `?`-bound `time.Time` boundaries (mirroring sqlite_search_test.go) so the
// stint/epoch geometry is deterministic and free of wall-clock flake; the HTTP
// request and the audit assertions are the genuine end-to-end surface.

import (
	"context"
	"database/sql"
	"encoding/json"
	"net/http"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
	"time"

	_ "modernc.org/sqlite"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.uber.org/zap"

	"github.com/mkhomutov/persatrix/internal/channels"
	"github.com/mkhomutov/persatrix/internal/planner"
	"github.com/mkhomutov/persatrix/internal/registry"
	"github.com/mkhomutov/persatrix/internal/security"
	"github.com/mkhomutov/persatrix/internal/state"
)

// recallTestServer wires a real on-disk SQLite channel store + router AND a
// file-backed audit logger onto a fresh test Server. It returns the store (for
// channel/member setup through the real API), the db path (so fixtures can seed
// intervals + messages with controlled timestamps), and the audit logger (so
// tests can Flush + read the emitted `channel.recall` trail). WithBatchSize(1)
// flushes the telemetry-class recall events eagerly so the trail is on disk
// without waiting on the batch ticker.
func recallTestServer(t *testing.T) (*Server, channels.ChannelStore, string, security.AuditLogger) {
	t.Helper()
	dir := t.TempDir()
	dbPath := filepath.Join(dir, "channels.db")
	store, err := channels.NewSQLiteStore(dbPath, channels.SQLiteOptions{MaxChannels: 50, Logger: zap.NewNop()})
	require.NoError(t, err)
	t.Cleanup(func() { _ = store.Close() })
	router := channels.NewChannelRouter(store, channels.NoopDispatcher{}, zap.NewNop(), nil)

	auditor, err := security.NewFileAuditLogger(filepath.Join(dir, "audit.jsonl"), security.WithBatchSize(1))
	require.NoError(t, err)
	t.Cleanup(func() { _ = auditor.Close() })

	logger := zap.NewNop()
	srv, err := New("127.0.0.1:0", t.TempDir(),
		state.NewInMemoryStore(logger),
		registry.NewInMemoryRegistry(logger),
		planner.NewYAMLPlanner(logger),
		logger,
		WithChannels(store, router),
		WithAuditLogger(auditor),
	)
	require.NoError(t, err)
	return srv, store, dbPath, auditor
}

const recallPath = "/api/v1/personas/alice/recall"

// TestRecallEndpoint_JoinLeaveRejoin_BothStintsGapExcluded is the structural
// half of MT-PERSONA-RECALL-001: against a two-stint fixture, the endpoint
// scoped by the PATH participant returns messages inside either stint and
// excludes the pre-join prefix and the removal gap — proving the path id (not a
// body field) is the access-control subject flowing into the store's EXISTS join.
func TestRecallEndpoint_JoinLeaveRejoin_BothStintsGapExcluded(t *testing.T) {
	srv, store, dbPath, _ := recallTestServer(t)
	ch := "group:planning"
	require.NoError(t, store.CreateChannel(context.Background(),
		channels.Channel{ID: ch, Name: "planning", Type: channels.ChannelTypeGroup}))

	base := time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)
	at := func(m int) time.Time { return base.Add(time.Duration(m) * time.Minute) }

	withRecallDB(t, dbPath, func(db *sql.DB) {
		// alice: closed stint [60,120), open stint [240, ∞) — the half-open ledger.
		recallSeedInterval(t, db, ch, "alice", at(60), at(120))
		recallSeedInterval(t, db, ch, "alice", at(240), nil)
		recallSeedMsg(t, db, "m-before", ch, "bob", "budget before", at(30), "")
		recallSeedMsg(t, db, "m-stint1", ch, "bob", "budget stint one", at(90), "")
		recallSeedMsg(t, db, "m-gap", ch, "bob", "budget in gap", at(180), "")
		recallSeedMsg(t, db, "m-stint2", ch, "bob", "budget stint two", at(300), "")
	})

	body, _ := json.Marshal(recallRequest{ActingClassification: "internal", Query: "budget"})
	rec := doRequest(srv.Handler(), http.MethodPost, recallPath, body)
	require.Equal(t, http.StatusOK, rec.Code, "body=%s", rec.Body.String())

	var resp recallResponse
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &resp))
	assert.ElementsMatch(t, []string{"m-stint1", "m-stint2"}, recallRespIDs(resp),
		"both stints recallable via REST; pre-join prefix and removal gap excluded")

	// The persona-facing wire shape is populated (message_id / sender, not the
	// internal id / sender_id field names).
	for _, m := range resp.Messages {
		assert.NotEmpty(t, m.MessageID, "message_id populated")
		assert.Equal(t, ch, m.ChannelID)
		assert.Equal(t, "bob", m.Sender, "sender populated from sender_id")
		assert.NotEmpty(t, m.Content)
		assert.False(t, m.Timestamp.IsZero())
	}
}

// TestRecallEndpoint_CrossEpochExcluded pins the store's vestigial epoch guard
// through the endpoint, using a SYNTHETICALLY-seeded row — the only way to get
// a non-"live" epoch into the store, since the real publish path cannot (it
// never stamps a non-"live" epoch). Recall always binds "live" now: the
// `epoch_id` body override was removed by ISSUE-0106(b) — see
// TestRecallEndpoint_EpochOverrideRemoved_PointedRejection for the 400 — so
// the one property left is that a row seeded under a different epoch stays
// unreachable through the endpoint (the amended RFC 0036 §OQ-6: the store is
// not epoch-partitioned, and the strict-equality "live" filter is a
// defensive leftover, not an isolation axis).
func TestRecallEndpoint_CrossEpochExcluded(t *testing.T) {
	srv, store, dbPath, _ := recallTestServer(t)
	ch := "group:planning"
	require.NoError(t, store.CreateChannel(context.Background(),
		channels.Channel{ID: ch, Name: "planning", Type: channels.ChannelTypeGroup}))
	base := time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)

	withRecallDB(t, dbPath, func(db *sql.DB) {
		recallSeedInterval(t, db, ch, "alice", base, nil) // open from the start
		recallSeedMsg(t, db, "m-live", ch, "bob", "budget live", base.Add(10*time.Minute), "live")
		recallSeedMsg(t, db, "m-ci", ch, "bob", "budget ci", base.Add(10*time.Minute), "ci-run-7")
	})

	// Recall always binds "live" (no override exists) → only the live row.
	body, _ := json.Marshal(recallRequest{ActingClassification: "internal", Query: "budget"})
	rec := doRequest(srv.Handler(), http.MethodPost, recallPath, body)
	require.Equal(t, http.StatusOK, rec.Code, "body=%s", rec.Body.String())
	var resp recallResponse
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &resp))
	assert.Equal(t, []string{"m-live"}, recallRespIDs(resp),
		"the store's strict-equality 'live' filter keeps a synthetic cross-epoch row unreachable")
}

// TestRecallEndpoint_EmitsAuditEvent_CountNotContent pins the RFC 0036
// §Security — Audit contract: every executed recall emits exactly one
// server-side `channel.recall` event recording the persona, the query, the
// narrowing params, and the result COUNT — and the recalled CONTENT never
// reaches the audit log.
func TestRecallEndpoint_EmitsAuditEvent_CountNotContent(t *testing.T) {
	srv, store, dbPath, auditor := recallTestServer(t)
	ch := "group:planning"
	require.NoError(t, store.CreateChannel(context.Background(),
		channels.Channel{ID: ch, Name: "planning", Type: channels.ChannelTypeGroup}))
	base := time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)

	// The query token appears in both content rows; the unique per-row markers
	// appear ONLY in content, so a clean "content never logged" assertion can
	// distinguish the (logged) query from the (never-logged) recalled text.
	withRecallDB(t, dbPath, func(db *sql.DB) {
		recallSeedInterval(t, db, ch, "alice", base, nil)
		recallSeedMsg(t, db, "m1", ch, "bob", "budgetzorp uniquecontentmarkeralpha", base.Add(time.Minute), "")
		recallSeedMsg(t, db, "m2", ch, "bob", "budgetzorp uniquecontentmarkerbeta", base.Add(2*time.Minute), "")
	})

	body, _ := json.Marshal(recallRequest{ActingClassification: "internal", Query: "budgetzorp", ChannelID: ch, Sender: "bob", Limit: 25})
	rec := doRequest(srv.Handler(), http.MethodPost, recallPath, body)
	require.Equal(t, http.StatusOK, rec.Code, "body=%s", rec.Body.String())
	var resp recallResponse
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &resp))
	require.Len(t, resp.Messages, 2, "positive control: both in-scope rows returned")

	require.NoError(t, auditor.Flush())
	recalls := filterRecallEvents(readAuditEvents(t, auditor.Path()))
	require.Len(t, recalls, 1, "exactly one channel.recall event per executed call")

	ev := recalls[0]
	assert.Equal(t, "alice", ev.AgentID, "the calling persona is the path participant")
	assert.Equal(t, "recall", ev.Action)
	assert.Equal(t, "budgetzorp", ev.Detail["query"], "the query string is recorded")
	assert.EqualValues(t, 2, ev.Detail["result_count"], "the result COUNT is recorded")
	assert.Equal(t, ch, ev.Detail["channel_id"], "narrowing channel_id recorded")
	assert.Equal(t, "bob", ev.Detail["sender"], "narrowing sender recorded")
	assert.EqualValues(t, 25, ev.Detail["limit"], "effective limit recorded (25 is within bounds, so equals the request)")
	assert.Equal(t, "live", ev.Detail["epoch_id"], "resolved epoch recorded")

	// The recalled content must never be written anywhere in the audit trail.
	raw, err := os.ReadFile(auditor.Path())
	require.NoError(t, err)
	assert.NotContains(t, string(raw), "uniquecontentmarkeralpha", "recalled content must never reach the audit log")
	assert.NotContains(t, string(raw), "uniquecontentmarkerbeta", "recalled content must never reach the audit log")
}

// TestRecallEndpoint_BareChannelNarrowMatchesCanonical pins the ISSUE-0107 fix:
// the body `channel_id` narrower is canonicalized to the store's prefixed id form
// before it reaches the membership-scoped query, so a bare, human-facing channel
// name (`mt-recall-001` — the form a persona/tool sees in context and the form
// ember-owl actually sent in MT-PERSONA-RECALL-001 Step 5, getting count=0)
// narrows identically to the canonical `group:mt-recall-001`. Both must equal the
// un-narrowed result for the single-channel fixture; the pre-fix bare path
// returned the empty set. It also pins the audit side effect (ISSUE-0107).
func TestRecallEndpoint_BareChannelNarrowMatchesCanonical(t *testing.T) {
	srv, store, dbPath, auditor := recallTestServer(t)
	const name = "mt-recall-001"
	ch := "group:" + name // the store-canonical id
	require.NoError(t, store.CreateChannel(context.Background(),
		channels.Channel{ID: ch, Name: name, Type: channels.ChannelTypeGroup}))
	base := time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)

	withRecallDB(t, dbPath, func(db *sql.DB) {
		recallSeedInterval(t, db, ch, "alice", base, nil) // open from the start
		recallSeedMsg(t, db, "m1", ch, "bob", "deploy window decision", base.Add(time.Minute), "")
		recallSeedMsg(t, db, "m2", ch, "bob", "deploy window confirmed", base.Add(2*time.Minute), "")
	})

	recall := func(channelID string) []string {
		body, _ := json.Marshal(recallRequest{ActingClassification: "internal", Query: "deploy", ChannelID: channelID})
		rec := doRequest(srv.Handler(), http.MethodPost, recallPath, body)
		require.Equal(t, http.StatusOK, rec.Code, "body=%s", rec.Body.String())
		var resp recallResponse
		require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &resp))
		return recallRespIDs(resp)
	}

	canonical := recall(ch)
	require.ElementsMatch(t, []string{"m1", "m2"}, canonical, "positive control: canonical id narrows to the in-scope set")

	bare := recall(name)     // the persona's form — pre-fix this returned 0
	unnarrowed := recall("") // span every accessible channel

	assert.ElementsMatch(t, canonical, bare, "a bare channel name narrows identically to the canonical id (ISSUE-0107)")
	assert.ElementsMatch(t, canonical, unnarrowed, "single-channel fixture: the narrowed set equals the un-narrowed set")

	// Every narrowed recall is audited as the canonical id, never the raw bare form.
	require.NoError(t, auditor.Flush())
	for _, ev := range filterRecallEvents(readAuditEvents(t, auditor.Path())) {
		if cid, ok := ev.Detail["channel_id"]; ok {
			assert.Equal(t, ch, cid, "narrowed recall is audited as the canonical channel_id, never the bare form (ISSUE-0107)")
		}
	}
}

// TestRecallEndpoint_RealPublishPath_Recallable proves the endpoint works
// against data written through the REAL RFC 0035 membership write hook
// (store.AddMember) and the real publish path (store.PublishMessage populates
// messages_fts via the trigger) — not just SQL-seeded fixtures — and that the
// audit count reflects the actual result set.
func TestRecallEndpoint_RealPublishPath_Recallable(t *testing.T) {
	srv, store, _, auditor := recallTestServer(t)
	ctx := context.Background()
	ch := "group:planning"
	require.NoError(t, store.CreateChannel(ctx, channels.Channel{ID: ch, Name: "planning", Type: channels.ChannelTypeGroup}))
	require.NoError(t, store.AddMember(ctx, ch, "alice", channels.RespondWhenMentioned)) // real open interval
	require.NoError(t, store.PublishMessage(ctx, channels.ChannelMessage{
		ID: "m-real", ChannelID: ch, SenderID: "alice",
		Content: "recallzorptoken via the real publish path", Timestamp: time.Now().UTC(),
	}))

	body, _ := json.Marshal(recallRequest{ActingClassification: "internal", Query: "recallzorptoken"})
	rec := doRequest(srv.Handler(), http.MethodPost, recallPath, body)
	require.Equal(t, http.StatusOK, rec.Code, "body=%s", rec.Body.String())
	var resp recallResponse
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &resp))
	require.Equal(t, []string{"m-real"}, recallRespIDs(resp), "a message published through the real path is recallable via REST")

	require.NoError(t, auditor.Flush())
	recalls := filterRecallEvents(readAuditEvents(t, auditor.Path()))
	require.Len(t, recalls, 1)
	assert.EqualValues(t, 1, recalls[0].Detail["result_count"])
}

// TestRecallEndpoint_RealPublishPath_ExplicitEpochUnreachable lived here from
// PR #677 (added in 9ce3a26) until RFC 0037 PR 5: it pinned the ISSUE-0106
// decoupling — a message "published into ci-run-7" landed under the "live"
// column default and was unreachable when recalled under ci-run-7 — as the
// tripwire that would flip red the day publish began persisting a per-run
// epoch. ISSUE-0106 was resolved in direction (b) instead: the deployment
// model is physical isolation (separate runs never share a channel-store DB),
// the recall `epoch_id` body override was REMOVED, and the axis it guarded is
// gone — so the tripwire retired with it. The rejection of the removed field
// is pinned by TestRecallEndpoint_EpochOverrideRemoved_PointedRejection.

// TestRecallEndpoint_TimeWindowNarrowing_AuditedAsRFC3339 pins the after
// (inclusive) / before (exclusive) body params: they narrow the result through
// the endpoint and are recorded in the audit detail as RFC3339 strings (not raw
// time structs).
func TestRecallEndpoint_TimeWindowNarrowing_AuditedAsRFC3339(t *testing.T) {
	srv, store, dbPath, auditor := recallTestServer(t)
	ch := "group:planning"
	require.NoError(t, store.CreateChannel(context.Background(),
		channels.Channel{ID: ch, Name: "planning", Type: channels.ChannelTypeGroup}))
	base := time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)
	at := func(m int) time.Time { return base.Add(time.Duration(m) * time.Minute) }

	withRecallDB(t, dbPath, func(db *sql.DB) {
		recallSeedInterval(t, db, ch, "alice", base, nil)
		recallSeedMsg(t, db, "m-early", ch, "bob", "budget early", at(60), "")
		recallSeedMsg(t, db, "m-mid", ch, "bob", "budget mid", at(120), "")
		recallSeedMsg(t, db, "m-late", ch, "bob", "budget late", at(180), "")
	})

	after, before := at(120), at(180) // half-open [120,180) → only m-mid
	body, _ := json.Marshal(recallRequest{ActingClassification: "internal", Query: "budget", After: after, Before: before})
	rec := doRequest(srv.Handler(), http.MethodPost, recallPath, body)
	require.Equal(t, http.StatusOK, rec.Code, "body=%s", rec.Body.String())
	var resp recallResponse
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &resp))
	assert.Equal(t, []string{"m-mid"}, recallRespIDs(resp),
		"after is inclusive, before exclusive — the half-open window narrows to the middle row")

	require.NoError(t, auditor.Flush())
	recalls := filterRecallEvents(readAuditEvents(t, auditor.Path()))
	require.Len(t, recalls, 1)
	assert.Equal(t, after.Format(time.RFC3339), recalls[0].Detail["after"], "after recorded as an RFC3339 string")
	assert.Equal(t, before.Format(time.RFC3339), recalls[0].Detail["before"], "before recorded as an RFC3339 string")
}

// TestRecallEndpoint_LimitClampedServerSide pins that the store-side clamp holds
// through the pass-through handler: the handler forwards the requested limit
// unmodified and the store clamps it to MaxRecallLimit, so the bound survives a
// caller that bypasses the persona tool and posts a huge limit directly.
func TestRecallEndpoint_LimitClampedServerSide(t *testing.T) {
	srv, store, dbPath, _ := recallTestServer(t)
	ch := "group:planning"
	require.NoError(t, store.CreateChannel(context.Background(),
		channels.Channel{ID: ch, Name: "planning", Type: channels.ChannelTypeGroup}))
	base := time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)

	total := channels.MaxRecallLimit + 5
	withRecallDB(t, dbPath, func(db *sql.DB) {
		recallSeedInterval(t, db, ch, "alice", base, nil)
		for i := 0; i < total; i++ {
			recallSeedMsg(t, db, "m-"+strconv.Itoa(i), ch, "bob", "budget item", base.Add(time.Duration(i+1)*time.Minute), "")
		}
	})

	body, _ := json.Marshal(recallRequest{ActingClassification: "internal", Query: "budget", Limit: 10_000})
	rec := doRequest(srv.Handler(), http.MethodPost, recallPath, body)
	require.Equal(t, http.StatusOK, rec.Code, "body=%s", rec.Body.String())
	var resp recallResponse
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &resp))
	assert.Len(t, resp.Messages, channels.MaxRecallLimit,
		"an over-large limit is clamped server-side even though the handler passes it through")
}

// TestRecallEndpoint_MalformedBody_BadRequest pins that an unparseable body is a
// 400 (decodeJSON), not a 500.
func TestRecallEndpoint_MalformedBody_BadRequest(t *testing.T) {
	srv, _, _, _ := recallTestServer(t)
	rec := doRequest(srv.Handler(), http.MethodPost, recallPath, []byte("{not valid json"))
	assert.Equal(t, http.StatusBadRequest, rec.Code)
}

// TestRecallEndpoint_503WhenStoreUnset pins the channel-surface convention: with
// no channel store wired the endpoint is 503, matching its neighbours.
func TestRecallEndpoint_503WhenStoreUnset(t *testing.T) {
	srv, _ := testServer(t) // no WithChannels
	body, _ := json.Marshal(recallRequest{ActingClassification: "internal", Query: "budget"})
	rec := doRequest(srv.Handler(), http.MethodPost, recallPath, body)
	assert.Equal(t, http.StatusServiceUnavailable, rec.Code)
}

// ─── fixture helpers ─────────────────────────────────────────

func withRecallDB(t *testing.T, dbPath string, fn func(*sql.DB)) {
	t.Helper()
	db, err := sql.Open("sqlite", dbPath)
	require.NoError(t, err)
	defer func() { _ = db.Close() }()
	fn(db)
}

func recallSeedInterval(t *testing.T, db *sql.DB, channelID, participantID string, joinedAt time.Time, leftAt any) {
	t.Helper()
	_, err := db.Exec(
		`INSERT INTO membership_intervals (channel_id, participant_id, joined_at, left_at) VALUES (?, ?, ?, ?)`,
		channelID, participantID, joinedAt, leftAt)
	require.NoError(t, err)
}

func recallSeedMsg(t *testing.T, db *sql.DB, id, channelID, sender, content string, ts time.Time, epoch string) {
	t.Helper()
	if epoch == "" {
		epoch = channels.DefaultEpochID
	}
	_, err := db.Exec(
		`INSERT INTO messages (id, channel_id, sender_id, content, timestamp, mentions, metadata, session_id, epoch_id)
		 VALUES (?, ?, ?, ?, ?, '[]', '{}', ?, ?)`,
		id, channelID, sender, content, ts, channels.DefaultSessionID, epoch)
	require.NoError(t, err)
}

func recallRespIDs(resp recallResponse) []string {
	out := make([]string, len(resp.Messages))
	for i, m := range resp.Messages {
		out[i] = m.MessageID
	}
	return out
}

func readAuditEvents(t *testing.T, path string) []security.AuditEvent {
	t.Helper()
	b, err := os.ReadFile(path)
	require.NoError(t, err)
	var out []security.AuditEvent
	for _, line := range strings.Split(strings.TrimSpace(string(b)), "\n") {
		if line == "" {
			continue
		}
		var ev security.AuditEvent
		require.NoError(t, json.Unmarshal([]byte(line), &ev))
		out = append(out, ev)
	}
	return out
}

func filterRecallEvents(events []security.AuditEvent) []security.AuditEvent {
	var out []security.AuditEvent
	for _, e := range events {
		if e.EventType == security.AuditChannelRecall {
			out = append(out, e)
		}
	}
	return out
}
