// RFC 0036 PR 2 — the membership-scoped, epoch-filtered verbatim search query
// ([sqliteStore.RecallMessages]). PR 1 landed the `messages_fts` index dormant;
// this is the load-bearing access-control PR — the `membership_intervals`
// `EXISTS` join *is* the recall authorization decision, and the `epoch_id`
// filter (§OQ-6 lock) is the run-isolation boundary.
//
// These tests pin the store-level contract with no endpoint or persona in the
// loop:
//
//   - Scope: against a join → leave → rejoin fixture, both stints are
//     recallable and the pre-join prefix + removal gap are not — and the SQL
//     `EXISTS` encoding agrees with the Go [InScope] predicate on every message,
//     closing the no-drift TODO the RFC 0035 read surface left for this PR.
//   - Epoch (load-bearing): a different / post-`reset` epoch is never returned.
//   - Session-span: two messages in the same channel + epoch but different
//     sessions are both recallable — recall is NOT session-scoped.
//   - Narrowing: channel_id / sender / after / before each filter and compose.
//   - Ranking: the more relevant FTS hit ranks first.
//   - Unicode: a non-Latin (Cyrillic) term filters, not sanitizes to a match-all.
//   - MATCH safety: a query carrying FTS5 operator syntax neither errors the
//     statement nor escapes the membership scope.
//   - Limit: clamped to the server-side maximum regardless of the request.
//   - Retention: a hard-deleted message leaves the index and is unrecallable.
//
// The FTS5-UNAVAILABLE `LIKE` fallback contract (same scope, divergent per-token
// substring match) lives in the sibling sqlite_search_fallback_test.go, which
// reuses this file's fixtures.
//
// Fixtures seed `membership_intervals` and `messages` via direct SQL with
// `?`-bound `time.Time` values (not string literals) so the join/leave
// boundaries compare against message timestamps in exactly the format the
// modernc driver also writes through [sqliteStore.PublishMessage] — the same
// storage shape the real write paths produce.
package channels

import (
	"context"
	"database/sql"
	"path/filepath"
	"testing"
	"time"

	_ "modernc.org/sqlite"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// recallFixtureBase is the deterministic clock all fixtures derive from; mins()
// offsets keep boundary arithmetic readable.
var recallFixtureBase = time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)

func mins(m int) time.Time { return recallFixtureBase.Add(time.Duration(m) * time.Minute) }

// TestRecallMessages_Scope_JoinLeaveRejoin pins the access-control core: against
// the RFC 0035 two-stint fixture, recall returns messages inside either stint
// and excludes the pre-join prefix and the removal gap. The half-open
// `[joined_at, left_at)` boundary is exercised at the exact join / leave /
// rejoin instants, and the recalled set is asserted to equal the Go [InScope]
// verdict for every message — so the SQL `EXISTS` encoding and the Go predicate
// cannot drift (the no-drift obligation membership_intervals.go records).
func TestRecallMessages_Scope_JoinLeaveRejoin(t *testing.T) {
	path := filepath.Join(t.TempDir(), "channels.db")
	store, err := NewSQLiteStore(path, SQLiteOptions{})
	require.NoError(t, err)
	t.Cleanup(func() { _ = store.Close() })
	ctx := context.Background()
	ch := "group:planning"
	require.NoError(t, store.CreateChannel(ctx, Channel{ID: ch, Name: "planning", Type: ChannelTypeGroup}))

	// alice: closed stint [60m,120m), open stint [240m, ∞). The half-open ledger.
	aliceIvs := []MembershipInterval{
		{ChannelID: ch, ParticipantID: "alice", JoinedAt: mins(60), LeftAt: mins(120)},
		{ChannelID: ch, ParticipantID: "alice", JoinedAt: mins(240)}, // open
	}

	seeds := []msgSeed{
		{id: "m-before", channelID: ch, sender: "bob", content: "budget before", ts: mins(30)},
		{id: "m-atjoin1", channelID: ch, sender: "bob", content: "budget at join", ts: mins(60)},     // inclusive lower → in
		{id: "m-stint1", channelID: ch, sender: "bob", content: "budget stint one", ts: mins(90)},    // in
		{id: "m-atleave1", channelID: ch, sender: "bob", content: "budget at leave", ts: mins(120)},  // exclusive upper → out
		{id: "m-gap", channelID: ch, sender: "bob", content: "budget in gap", ts: mins(180)},         // out
		{id: "m-atrejoin", channelID: ch, sender: "bob", content: "budget at rejoin", ts: mins(240)}, // in
		{id: "m-stint2", channelID: ch, sender: "bob", content: "budget stint two", ts: mins(300)},   // in
	}

	withDB(t, path, func(db *sql.DB) {
		seedInterval(t, db, ch, "alice", mins(60), mins(120)) // stint 1 closed
		seedInterval(t, db, ch, "alice", mins(240), nil)      // stint 2 open
		for _, m := range seeds {
			seedMsg(t, db, m)
		}
	})

	got, err := store.RecallMessages(ctx, RecallParams{ParticipantID: "alice", Query: "budget"})
	require.NoError(t, err)
	recalled := idSet(got)

	// The recalled set equals exactly the in-scope set...
	assert.ElementsMatch(t,
		[]string{"m-atjoin1", "m-stint1", "m-atrejoin", "m-stint2"},
		idSlice(got),
		"both stints recallable; pre-join prefix and removal gap excluded")

	// ...and equals the Go InScope verdict on every message (no-drift guard).
	for _, m := range seeds {
		want := InScope(aliceIvs, m.ts)
		assert.Equalf(t, want, recalled[m.id],
			"message %s at +%s: InScope(Go)=%v, recalled(SQL)=%v — encodings must agree",
			m.id, m.ts.Sub(recallFixtureBase), want, recalled[m.id])
	}
}

// TestRecallMessages_Epoch_HardFilter pins the §OQ-6 load-bearing lock: recall
// is strict-equality on epoch with no carve-out, so a message in a different (or
// post-`reset`) epoch is never returned even when it matches the query and falls
// inside the membership window. The default epoch resolves to "live".
func TestRecallMessages_Epoch_HardFilter(t *testing.T) {
	path := filepath.Join(t.TempDir(), "channels.db")
	store, err := NewSQLiteStore(path, SQLiteOptions{})
	require.NoError(t, err)
	t.Cleanup(func() { _ = store.Close() })
	ctx := context.Background()
	ch := "group:planning"
	require.NoError(t, store.CreateChannel(ctx, Channel{ID: ch, Name: "planning", Type: ChannelTypeGroup}))

	withDB(t, path, func(db *sql.DB) {
		seedInterval(t, db, ch, "alice", mins(0), nil) // open from the start
		seedMsg(t, db, msgSeed{id: "m-live", channelID: ch, sender: "bob", content: "budget live", ts: mins(10), epoch: "live"})
		seedMsg(t, db, msgSeed{id: "m-ci", channelID: ch, sender: "bob", content: "budget ci", ts: mins(10), epoch: "ci-run-7"})
		seedMsg(t, db, msgSeed{id: "m-reset", channelID: ch, sender: "bob", content: "budget reset", ts: mins(10), epoch: "live-2"})
	})

	// Default epoch → only the "live" row, never the other-world rows.
	got, err := store.RecallMessages(ctx, RecallParams{ParticipantID: "alice", Query: "budget"})
	require.NoError(t, err)
	assert.Equal(t, []string{"m-live"}, idSlice(got),
		"default epoch is strict-equality 'live'; cross-epoch / post-reset rows excluded")

	// An explicit epoch selects its own world and nothing else.
	got, err = store.RecallMessages(ctx, RecallParams{ParticipantID: "alice", Query: "budget", EpochID: "ci-run-7"})
	require.NoError(t, err)
	assert.Equal(t, []string{"m-ci"}, idSlice(got), "explicit epoch is also strict-equality")
}

// TestRecallMessages_SpansSessions pins §OQ-6's other half: recall is NOT
// session-scoped. Two messages in the same channel + epoch but different
// sessions are both recallable — verbatim recall's value is cross-conversation,
// and the membership interval (not the session) is the access boundary.
func TestRecallMessages_SpansSessions(t *testing.T) {
	path := filepath.Join(t.TempDir(), "channels.db")
	store, err := NewSQLiteStore(path, SQLiteOptions{})
	require.NoError(t, err)
	t.Cleanup(func() { _ = store.Close() })
	ctx := context.Background()
	ch := "group:planning"
	require.NoError(t, store.CreateChannel(ctx, Channel{ID: ch, Name: "planning", Type: ChannelTypeGroup}))

	withDB(t, path, func(db *sql.DB) {
		seedInterval(t, db, ch, "alice", mins(0), nil)
		seedMsg(t, db, msgSeed{id: "m-sessA", channelID: ch, sender: "bob", content: "budget alpha", ts: mins(10), session: "sess-A"})
		seedMsg(t, db, msgSeed{id: "m-sessB", channelID: ch, sender: "bob", content: "budget beta", ts: mins(20), session: "sess-B"})
	})

	got, err := store.RecallMessages(ctx, RecallParams{ParticipantID: "alice", Query: "budget"})
	require.NoError(t, err)
	assert.ElementsMatch(t, []string{"m-sessA", "m-sessB"}, idSlice(got),
		"recall spans sessions within the epoch — not session-scoped")
}

// TestRecallMessages_Narrowing pins the optional filters: channel_id, sender,
// after (inclusive lower), and before (exclusive upper) each filter on their own
// and compose. alice holds an open interval in BOTH channels so scope is never
// the thing trimming these rows.
func TestRecallMessages_Narrowing(t *testing.T) {
	path := filepath.Join(t.TempDir(), "channels.db")
	store, err := NewSQLiteStore(path, SQLiteOptions{})
	require.NoError(t, err)
	t.Cleanup(func() { _ = store.Close() })
	ctx := context.Background()
	require.NoError(t, store.CreateChannel(ctx, Channel{ID: "group:planning", Name: "planning", Type: ChannelTypeGroup}))
	require.NoError(t, store.CreateChannel(ctx, Channel{ID: "group:design", Name: "design", Type: ChannelTypeGroup}))

	withDB(t, path, func(db *sql.DB) {
		seedInterval(t, db, "group:planning", "alice", mins(0), nil)
		seedInterval(t, db, "group:design", "alice", mins(0), nil)
		seedMsg(t, db, msgSeed{id: "m1", channelID: "group:planning", sender: "alice", content: "budget one", ts: mins(60)})
		seedMsg(t, db, msgSeed{id: "m2", channelID: "group:planning", sender: "bob", content: "budget two", ts: mins(120)})
		seedMsg(t, db, msgSeed{id: "m3", channelID: "group:design", sender: "alice", content: "budget three", ts: mins(180)})
	})

	base := RecallParams{ParticipantID: "alice", Query: "budget"}

	got, err := store.RecallMessages(ctx, withChannel(base, "group:planning"))
	require.NoError(t, err)
	assert.ElementsMatch(t, []string{"m1", "m2"}, idSlice(got), "channel_id narrows to one channel")

	got, err = store.RecallMessages(ctx, withSender(base, "alice"))
	require.NoError(t, err)
	assert.ElementsMatch(t, []string{"m1", "m3"}, idSlice(got), "sender narrows to one author")

	got, err = store.RecallMessages(ctx, withSender(withChannel(base, "group:planning"), "alice"))
	require.NoError(t, err)
	assert.ElementsMatch(t, []string{"m1"}, idSlice(got), "channel_id + sender compose")

	got, err = store.RecallMessages(ctx, withAfter(base, mins(120)))
	require.NoError(t, err)
	assert.ElementsMatch(t, []string{"m2", "m3"}, idSlice(got), "after is an inclusive lower bound")

	got, err = store.RecallMessages(ctx, withBefore(base, mins(180)))
	require.NoError(t, err)
	assert.ElementsMatch(t, []string{"m1", "m2"}, idSlice(got), "before is an exclusive upper bound")

	got, err = store.RecallMessages(ctx, withBefore(withAfter(base, mins(120)), mins(180)))
	require.NoError(t, err)
	assert.ElementsMatch(t, []string{"m2"}, idSlice(got), "after + before compose to a half-open window")
}

// TestRecallMessages_Ranking pins OQ #3: BM25-dominant, recency a mild tiebreak.
// A short, term-dense document outranks a long document that mentions the term
// once, regardless of which was written more recently.
func TestRecallMessages_Ranking(t *testing.T) {
	path := filepath.Join(t.TempDir(), "channels.db")
	store, err := NewSQLiteStore(path, SQLiteOptions{})
	require.NoError(t, err)
	t.Cleanup(func() { _ = store.Close() })
	ctx := context.Background()
	ch := "group:planning"
	require.NoError(t, store.CreateChannel(ctx, Channel{ID: ch, Name: "planning", Type: ChannelTypeGroup}))

	withDB(t, path, func(db *sql.DB) {
		seedInterval(t, db, ch, "alice", mins(0), nil)
		// Term-dense + short → strong BM25. Written EARLIER, so recency alone
		// would rank it last; BM25 dominance must still float it to the top.
		seedMsg(t, db, msgSeed{id: "m-dense", channelID: ch, sender: "bob",
			content: "budget budget budget", ts: mins(10)})
		// One mention buried in a long document → weak BM25. Written LATER.
		seedMsg(t, db, msgSeed{id: "m-sparse", channelID: ch, sender: "bob",
			content: "the quarterly budget was mentioned once among many other unrelated planning words here", ts: mins(20)})
	})

	got, err := store.RecallMessages(ctx, RecallParams{ParticipantID: "alice", Query: "budget"})
	require.NoError(t, err)
	require.Len(t, got, 2)
	assert.Equal(t, "m-dense", got[0].ID, "the term-dense hit ranks first (BM25-dominant, not recency-dominant)")
}

// TestRecallMessages_EmptyQuery_RecencyListing pins the documented degrade: a
// query with no searchable terms (empty, or pure punctuation that sanitizes
// away) is not an error — it lists the in-scope set newest-first, still bounded
// by membership + epoch. On the prod build (modernc ships FTS5) a real search
// takes the FTS path, so this term-less branch is the one production actually
// reaches here — and it exercises recallViaLike's ZERO-pattern shape: a WHERE
// carrying the scope predicate and no `content LIKE` clause at all.
func TestRecallMessages_EmptyQuery_RecencyListing(t *testing.T) {
	path := filepath.Join(t.TempDir(), "channels.db")
	store, err := NewSQLiteStore(path, SQLiteOptions{})
	require.NoError(t, err)
	t.Cleanup(func() { _ = store.Close() })
	ctx := context.Background()
	require.NoError(t, store.CreateChannel(ctx, Channel{ID: "group:planning", Name: "planning", Type: ChannelTypeGroup}))
	require.NoError(t, store.CreateChannel(ctx, Channel{ID: "group:secret", Name: "secret", Type: ChannelTypeGroup}))

	withDB(t, path, func(db *sql.DB) {
		seedInterval(t, db, "group:planning", "alice", mins(0), nil)
		seedMsg(t, db, msgSeed{id: "m-old", channelID: "group:planning", sender: "bob", content: "first", ts: mins(10)})
		seedMsg(t, db, msgSeed{id: "m-new", channelID: "group:planning", sender: "bob", content: "second", ts: mins(20)})
		// Out of scope (alice was never in group:secret) AND newest — proves the
		// term-less listing still enforces scope, not just recency.
		seedMsg(t, db, msgSeed{id: "m-out", channelID: "group:secret", sender: "carol", content: "third", ts: mins(30)})
	})

	// Empty, whitespace-only, and pure-punctuation queries all sanitize to no terms.
	for _, q := range []string{"", "   ", "!!! …"} {
		got, err := store.RecallMessages(ctx, RecallParams{ParticipantID: "alice", Query: q})
		require.NoErrorf(t, err, "term-less query %q lists rather than errors", q)
		assert.Equalf(t, []string{"m-new", "m-old"}, idSlice(got),
			"term-less query %q lists the in-scope set newest-first, excluding the out-of-scope row", q)
	}
}

// TestRecallMessages_MatchSafety pins the §F injection property: a query
// carrying FTS5 operator syntax and metacharacters neither errors the statement
// nor escapes the membership scope. The `EXISTS` clause is a separate AND-ed
// predicate the MATCH text cannot reach, so an out-of-scope message with the
// same content is never surfaced however the query is crafted.
func TestRecallMessages_MatchSafety(t *testing.T) {
	path := filepath.Join(t.TempDir(), "channels.db")
	store, err := NewSQLiteStore(path, SQLiteOptions{})
	require.NoError(t, err)
	t.Cleanup(func() { _ = store.Close() })
	ctx := context.Background()
	require.NoError(t, store.CreateChannel(ctx, Channel{ID: "group:planning", Name: "planning", Type: ChannelTypeGroup}))
	require.NoError(t, store.CreateChannel(ctx, Channel{ID: "group:secret", Name: "secret", Type: ChannelTypeGroup}))

	withDB(t, path, func(db *sql.DB) {
		// alice is only ever a member of planning.
		seedInterval(t, db, "group:planning", "alice", mins(0), nil)
		seedMsg(t, db, msgSeed{id: "m-in", channelID: "group:planning", sender: "bob", content: "quarterly budget discussion", ts: mins(10)})
		// Same content, but in a channel alice was never in.
		seedMsg(t, db, msgSeed{id: "m-out", channelID: "group:secret", sender: "carol", content: "quarterly budget discussion", ts: mins(10)})
	})

	for _, q := range []string{
		`budget`,                         // baseline control
		`budget*()":`,                    // wildcards, parens, quote, colon — strip to "budget"
		`budget OR secret`,               // operator keywords as bare words
		`"budget NEAR/0 secret -- ;`,     // unbalanced quote, NEAR op, SQL-ish comment
		`budget AND (secret OR planning`, // unbalanced paren + operators
	} {
		got, err := store.RecallMessages(ctx, RecallParams{ParticipantID: "alice", Query: q})
		require.NoErrorf(t, err, "query %q must not error the statement", q)
		assert.NotContainsf(t, idSlice(got), "m-out",
			"query %q must not escape the membership scope", q)
	}

	// The baseline still functions: the metacharacter-laden form that strips to
	// "budget" surfaces the in-scope row.
	got, err := store.RecallMessages(ctx, RecallParams{ParticipantID: "alice", Query: `budget*()":`})
	require.NoError(t, err)
	assert.Equal(t, []string{"m-in"}, idSlice(got), "sanitized query still matches the in-scope row")
}

// TestRecallMessages_NonLatinQuery pins that a non-Latin search term actually
// filters rather than sanitizing to empty and dumping the whole in-scope corpus.
// The ASCII-only `[^a-zA-Z0-9\s]+` sanitizer stripped every Cyrillic / CJK /
// accented character, so such a query degraded to a recency-ordered match-all —
// silently discarding the term. The `\p{L}\p{N}` form preserves the term, so the
// search excludes a message that does NOT contain it (the load-bearing
// assertion) and still surfaces one that does.
func TestRecallMessages_NonLatinQuery(t *testing.T) {
	path := filepath.Join(t.TempDir(), "channels.db")
	store, err := NewSQLiteStore(path, SQLiteOptions{})
	require.NoError(t, err)
	t.Cleanup(func() { _ = store.Close() })
	ctx := context.Background()
	ch := "group:planning"
	require.NoError(t, store.CreateChannel(ctx, Channel{ID: ch, Name: "planning", Type: ChannelTypeGroup}))

	withDB(t, path, func(db *sql.DB) {
		seedInterval(t, db, ch, "alice", mins(0), nil)
		seedMsg(t, db, msgSeed{id: "m-hit", channelID: ch, sender: "bob", content: "обсудили бюджет на квартал", ts: mins(10)})
		seedMsg(t, db, msgSeed{id: "m-miss", channelID: ch, sender: "bob", content: "lunch logistics thread", ts: mins(20)})
	})

	got, err := store.RecallMessages(ctx, RecallParams{ParticipantID: "alice", Query: "бюджет"})
	require.NoError(t, err)
	// Load-bearing: the term must filter — the non-matching row must not appear
	// (it would, if the Cyrillic query sanitized away into a match-all listing).
	assert.NotContains(t, idSlice(got), "m-miss",
		"a non-Latin query must search, not dump the whole in-scope corpus")
	assert.Equal(t, []string{"m-hit"}, idSlice(got),
		"the message containing the Cyrillic term is the only hit")
}

// TestRecallMessages_LimitClamp pins the server-side bound: a request above the
// hard maximum is clamped to MaxRecallLimit, a zero/absent limit resolves to
// DefaultRecallLimit, and an explicit in-range limit is honoured. The clamp lives
// in the store so the bound holds even if a future caller bypasses the tool.
func TestRecallMessages_LimitClamp(t *testing.T) {
	path := filepath.Join(t.TempDir(), "channels.db")
	store, err := NewSQLiteStore(path, SQLiteOptions{})
	require.NoError(t, err)
	t.Cleanup(func() { _ = store.Close() })
	ctx := context.Background()
	ch := "group:planning"
	require.NoError(t, store.CreateChannel(ctx, Channel{ID: ch, Name: "planning", Type: ChannelTypeGroup}))

	total := MaxRecallLimit + 5
	withDB(t, path, func(db *sql.DB) {
		seedInterval(t, db, ch, "alice", mins(0), nil)
		for i := 0; i < total; i++ {
			seedMsg(t, db, msgSeed{
				id: "m-" + itoa(int64(i)), channelID: ch, sender: "bob",
				content: "budget item", ts: mins(i + 1),
			})
		}
	})

	got, err := store.RecallMessages(ctx, RecallParams{ParticipantID: "alice", Query: "budget", Limit: 10_000})
	require.NoError(t, err)
	assert.Len(t, got, MaxRecallLimit, "an over-large request is clamped to MaxRecallLimit")

	got, err = store.RecallMessages(ctx, RecallParams{ParticipantID: "alice", Query: "budget", Limit: 0})
	require.NoError(t, err)
	assert.Len(t, got, DefaultRecallLimit, "a zero limit resolves to DefaultRecallLimit")

	got, err = store.RecallMessages(ctx, RecallParams{ParticipantID: "alice", Query: "budget", Limit: 5})
	require.NoError(t, err)
	assert.Len(t, got, 5, "an explicit in-range limit is honoured")
}

// TestRecallMessages_Retention_DeletedMessageGone pins the RFC 0036 §H retention
// horizon through the REAL write path: a published, in-scope, MATCH-findable
// message becomes unrecallable once hard-deleted (the messages_ad trigger drops
// it from messages_fts). This also exercises recall against the driver-written
// timestamp/interval formats the production publish + AddMember paths produce.
func TestRecallMessages_Retention_DeletedMessageGone(t *testing.T) {
	path := filepath.Join(t.TempDir(), "channels.db")
	store, err := NewSQLiteStore(path, SQLiteOptions{})
	require.NoError(t, err)
	t.Cleanup(func() { _ = store.Close() })
	ctx := context.Background()
	ch := "group:planning"
	require.NoError(t, store.CreateChannel(ctx, Channel{ID: ch, Name: "planning", Type: ChannelTypeGroup}))
	require.NoError(t, store.AddMember(ctx, ch, "alice", RespondWhenMentioned)) // real open interval

	require.NoError(t, store.PublishMessage(ctx, ChannelMessage{
		ID: "m-keep", ChannelID: ch, SenderID: "alice", Content: "recallzorptoken stays",
		Timestamp: time.Now().UTC(),
	}))

	got, err := store.RecallMessages(ctx, RecallParams{ParticipantID: "alice", Query: "recallzorptoken"})
	require.NoError(t, err)
	require.Equal(t, []string{"m-keep"}, idSlice(got), "positive control: the live message is recallable")

	withDB(t, path, func(db *sql.DB) {
		_, err := db.Exec(`DELETE FROM messages WHERE id = 'm-keep'`)
		require.NoError(t, err)
	})

	got, err = store.RecallMessages(ctx, RecallParams{ParticipantID: "alice", Query: "recallzorptoken"})
	require.NoError(t, err)
	assert.Empty(t, got, "a hard-deleted message is gone from messages_fts and unrecallable")
}

// ─── fixture helpers ─────────────────────────────────────────

// msgSeed is one direct-SQL message row. epoch/session default to the store's
// defaults ('live'/'legacy') when left blank.
type msgSeed struct {
	id, channelID, sender, content, epoch, session string
	ts                                             time.Time
}

func seedMsg(t *testing.T, db *sql.DB, m msgSeed) {
	t.Helper()
	if m.epoch == "" {
		m.epoch = DefaultEpochID
	}
	if m.session == "" {
		m.session = DefaultSessionID
	}
	_, err := db.Exec(
		`INSERT INTO messages (id, channel_id, sender_id, content, timestamp, mentions, metadata, session_id, epoch_id)
		 VALUES (?, ?, ?, ?, ?, '[]', '{}', ?, ?)`,
		m.id, m.channelID, m.sender, m.content, m.ts, m.session, m.epoch)
	require.NoError(t, err)
}

// seedInterval inserts one membership_intervals row. Pass leftAt == nil for an
// OPEN stint (SQL NULL); a time.Time for a closed one.
func seedInterval(t *testing.T, db *sql.DB, channelID, participantID string, joinedAt time.Time, leftAt any) {
	t.Helper()
	_, err := db.Exec(
		`INSERT INTO membership_intervals (channel_id, participant_id, joined_at, left_at)
		 VALUES (?, ?, ?, ?)`,
		channelID, participantID, joinedAt, leftAt)
	require.NoError(t, err)
}

func withChannel(p RecallParams, id string) RecallParams  { p.ChannelID = id; return p }
func withSender(p RecallParams, s string) RecallParams    { p.Sender = s; return p }
func withAfter(p RecallParams, t time.Time) RecallParams  { p.After = t; return p }
func withBefore(p RecallParams, t time.Time) RecallParams { p.Before = t; return p }

func idSlice(msgs []ChannelMessage) []string {
	out := make([]string, len(msgs))
	for i, m := range msgs {
		out[i] = m.ID
	}
	return out
}

func idSet(msgs []ChannelMessage) map[string]bool {
	out := make(map[string]bool, len(msgs))
	for _, m := range msgs {
		out[m.ID] = true
	}
	return out
}
