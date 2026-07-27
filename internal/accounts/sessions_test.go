package accounts

import (
	"context"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"fmt"
	"path/filepath"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// issueFor creates an active account and issues a session for it,
// returning the raw token and the parsed session.
func issueFor(t *testing.T, s *Store, ttl time.Duration) (string, *Session, *Account) {
	t.Helper()
	ctx := context.Background()
	acct, err := s.CreateAccount(ctx, validNew())
	require.NoError(t, err)
	token, sess, err := s.IssueSession(ctx, acct.ID, ttl)
	require.NoError(t, err)
	return token, sess, acct
}

func TestIssueSession_RoundTrip(t *testing.T) {
	s := openStore(t)
	ctx := context.Background()

	token, sess, acct := issueFor(t, s, time.Hour)

	// §D: a 256-bit opaque token, base64url, returned raw exactly once.
	raw, err := base64.RawURLEncoding.DecodeString(token)
	require.NoError(t, err, "the token is base64url")
	assert.Len(t, raw, sessionTokenBytes, "256-bit token (§D)")

	assert.Equal(t, acct.ID, sess.AccountID)
	assert.Equal(t, sess.IssuedAt.Add(time.Hour), sess.ExpiresAt)
	assert.Equal(t, sess.IssuedAt, sess.LastUsedAt)
	assert.Nil(t, sess.RevokedAt)
	assert.Zero(t, sess.IssuedAt.Nanosecond(), "INTEGER-at-rest timestamps are second precision")

	got, gotAcct, err := s.ResolveSession(ctx, token)
	require.NoError(t, err)
	assert.Equal(t, sess, got)
	assert.Equal(t, acct, gotAcct)
}

func TestIssueSession_TokensAreHashedAtRest(t *testing.T) {
	s := openStore(t)

	token, sess, _ := issueFor(t, s, time.Hour)

	// §D: the server persists only sha256(token) — a read of accounts.db
	// yields no usable live credential.
	digest := sha256.Sum256([]byte(token))
	assert.Equal(t, hex.EncodeToString(digest[:]), sess.TokenHash)

	var n int
	require.NoError(t, s.db.QueryRow(
		`SELECT COUNT(*) FROM sessions WHERE token_hash = ?`, token).Scan(&n))
	assert.Zero(t, n, "the raw token must never be stored")
	require.NoError(t, s.db.QueryRow(
		`SELECT COUNT(*) FROM sessions WHERE token_hash = ?`, sess.TokenHash).Scan(&n))
	assert.Equal(t, 1, n, "the row is keyed by the hash")
}

func TestIssueSession_TokensAreUnique(t *testing.T) {
	s := openStore(t)
	ctx := context.Background()

	acct, err := s.CreateAccount(ctx, validNew())
	require.NoError(t, err)

	seen := map[string]bool{}
	for i := 0; i < 8; i++ {
		token, _, err := s.IssueSession(ctx, acct.ID, time.Hour)
		require.NoError(t, err)
		assert.False(t, seen[token], "every mint draws fresh crypto/rand bytes")
		seen[token] = true
	}
}

func TestIssueSession_InvalidTTL(t *testing.T) {
	s := openStore(t)
	ctx := context.Background()
	acct, err := s.CreateAccount(ctx, validNew())
	require.NoError(t, err)

	for _, ttl := range []time.Duration{0, -time.Minute, 500 * time.Millisecond} {
		_, _, err := s.IssueSession(ctx, acct.ID, ttl)
		assert.Error(t, err,
			"a session must expire in the future — sub-second TTLs would truncate to a born-expired row")
	}
}

func TestIssueSession_UnknownAccount(t *testing.T) {
	s := openStore(t)
	_, _, err := s.IssueSession(context.Background(), "no-such-id", time.Hour)
	assert.ErrorIs(t, err, ErrAccountNotFound)
}

func TestIssueSession_DisabledAccount(t *testing.T) {
	s := openStore(t)
	ctx := context.Background()
	acct, err := s.CreateAccount(ctx, validNew())
	require.NoError(t, err)
	require.NoError(t, s.SetAccountStatus(ctx, acct.ID, StatusDisabled))

	_, _, err = s.IssueSession(ctx, acct.ID, time.Hour)
	assert.ErrorIs(t, err, ErrAccountDisabled,
		"a disabled account must not acquire a live session (§D)")
}

func TestResolveSession_Unknown(t *testing.T) {
	s := openStore(t)
	_, _, err := s.ResolveSession(context.Background(), "never-issued")
	assert.ErrorIs(t, err, ErrSessionNotFound)
}

func TestResolveSession_AnySuppliedLengthIsAccepted(t *testing.T) {
	s := openStore(t)
	ctx := context.Background()

	// ISSUE-0004 discipline: the supplied token is reduced to a
	// fixed-size digest before the lookup, so length never takes a
	// distinct code path — a 1-byte and a 1 MiB probe fail identically.
	for _, probe := range []string{"", "x", string(make([]byte, 1<<20))} {
		_, _, err := s.ResolveSession(ctx, probe)
		assert.ErrorIs(t, err, ErrSessionNotFound)
	}
}

func TestResolveSession_Expired(t *testing.T) {
	s := openStore(t)
	ctx := context.Background()

	token, sess, _ := issueFor(t, s, time.Hour)
	// Age the row directly — the store has no clock seam by design.
	_, err := s.db.Exec(`UPDATE sessions SET expires_at = ? WHERE token_hash = ?`,
		time.Now().UTC().Add(-time.Second).Unix(), sess.TokenHash)
	require.NoError(t, err)

	_, _, err = s.ResolveSession(ctx, token)
	assert.ErrorIs(t, err, ErrSessionExpired)
}

func TestResolveSession_Revoked(t *testing.T) {
	s := openStore(t)
	ctx := context.Background()

	token, _, _ := issueFor(t, s, time.Hour)
	require.NoError(t, s.RevokeSession(ctx, token))

	_, _, err := s.ResolveSession(ctx, token)
	assert.ErrorIs(t, err, ErrSessionRevoked)
}

func TestResolveSession_DisabledAccount(t *testing.T) {
	s := openStore(t)
	ctx := context.Background()

	token, _, acct := issueFor(t, s, time.Hour)
	require.NoError(t, s.SetAccountStatus(ctx, acct.ID, StatusDisabled))

	_, _, err := s.ResolveSession(ctx, token)
	assert.ErrorIs(t, err, ErrAccountDisabled,
		"disabling an account kills its live sessions at the next lookup (§D)")
}

func TestResolveSession_LazyLastUsedRefresh(t *testing.T) {
	s := openStore(t)
	ctx := context.Background()

	token, sess, _ := issueFor(t, s, time.Hour)

	// Within the threshold: no write (§D — SQLite is single-writer; a
	// per-request UPDATE would serialise onto the hot auth path). The
	// row is aged to a value distinct from now so an eager-write
	// regression cannot hide inside the same truncated second.
	slightlyStale := time.Now().UTC().Add(-2 * time.Minute).Truncate(time.Second)
	_, err := s.db.Exec(`UPDATE sessions SET last_used_at = ? WHERE token_hash = ?`,
		slightlyStale.Unix(), sess.TokenHash)
	require.NoError(t, err)

	got, _, err := s.ResolveSession(ctx, token)
	require.NoError(t, err)
	assert.True(t, got.LastUsedAt.Equal(slightlyStale), "a below-threshold session is not refreshed")
	var atRest int64
	require.NoError(t, s.db.QueryRow(
		`SELECT last_used_at FROM sessions WHERE token_hash = ?`, sess.TokenHash).Scan(&atRest))
	assert.Equal(t, slightlyStale.Unix(), atRest, "no write happened below the threshold")

	// Age last_used_at past the threshold: the next resolve refreshes.
	stale := time.Now().UTC().Add(-lastUsedRefreshInterval - time.Minute).Truncate(time.Second)
	_, err = s.db.Exec(`UPDATE sessions SET last_used_at = ? WHERE token_hash = ?`,
		stale.Unix(), sess.TokenHash)
	require.NoError(t, err)

	got, _, err = s.ResolveSession(ctx, token)
	require.NoError(t, err)
	assert.True(t, got.LastUsedAt.After(stale), "a stale last_used_at is refreshed")

	require.NoError(t, s.db.QueryRow(
		`SELECT last_used_at FROM sessions WHERE token_hash = ?`, sess.TokenHash).Scan(&atRest))
	assert.Equal(t, got.LastUsedAt.Unix(), atRest, "the refresh is durable, not view-only")
}

func TestRevokeSession(t *testing.T) {
	s := openStore(t)
	ctx := context.Background()

	token, sess, _ := issueFor(t, s, time.Hour)

	require.NoError(t, s.RevokeSession(ctx, token))
	var revoked *int64
	require.NoError(t, s.db.QueryRow(
		`SELECT revoked_at FROM sessions WHERE token_hash = ?`, sess.TokenHash).Scan(&revoked))
	require.NotNil(t, revoked)

	// Logout is idempotent: a replay is a no-op success (unless a prune
	// already deleted the row) and must not advance the original
	// revocation instant.
	require.NoError(t, s.RevokeSession(ctx, token))
	var again *int64
	require.NoError(t, s.db.QueryRow(
		`SELECT revoked_at FROM sessions WHERE token_hash = ?`, sess.TokenHash).Scan(&again))
	assert.Equal(t, *revoked, *again)

	assert.ErrorIs(t, s.RevokeSession(ctx, "never-issued"), ErrSessionNotFound)
}

func TestPruneSessions(t *testing.T) {
	s := openStore(t)
	ctx := context.Background()

	acct, err := s.CreateAccount(ctx, validNew())
	require.NoError(t, err)

	live, _, err := s.IssueSession(ctx, acct.ID, time.Hour)
	require.NoError(t, err)
	_, expired, err := s.IssueSession(ctx, acct.ID, time.Hour)
	require.NoError(t, err)
	revokedTok, _, err := s.IssueSession(ctx, acct.ID, time.Hour)
	require.NoError(t, err)

	_, err = s.db.Exec(`UPDATE sessions SET expires_at = ? WHERE token_hash = ?`,
		time.Now().UTC().Add(-time.Second).Unix(), expired.TokenHash)
	require.NoError(t, err)
	require.NoError(t, s.RevokeSession(ctx, revokedTok))

	n, err := s.PruneSessions(ctx)
	require.NoError(t, err)
	assert.Equal(t, int64(2), n, "expired and revoked rows are swept (§D)")

	var remaining int
	require.NoError(t, s.db.QueryRow(`SELECT COUNT(*) FROM sessions`).Scan(&remaining))
	assert.Equal(t, 1, remaining)
	_, _, err = s.ResolveSession(ctx, live)
	assert.NoError(t, err, "the live session survives the sweep")
}

func TestOpen_PrunesOnOpen(t *testing.T) {
	path := filepath.Join(t.TempDir(), "accounts.db")
	s, err := Open(path)
	require.NoError(t, err)
	ctx := context.Background()

	live, _, err := issueForPath(ctx, t, s)
	require.NoError(t, err)
	_, stale, err2 := issueForPath(ctx, t, s)
	require.NoError(t, err2)
	_, err = s.db.Exec(`UPDATE sessions SET expires_at = ? WHERE token_hash = ?`,
		time.Now().UTC().Add(-time.Second).Unix(), stale.TokenHash)
	require.NoError(t, err)
	require.NoError(t, s.Close())

	// §D: a sweep on store open keeps the table tracking live sessions.
	reopened, err := Open(path)
	require.NoError(t, err)
	t.Cleanup(func() { _ = reopened.Close() })

	var n int
	require.NoError(t, reopened.db.QueryRow(`SELECT COUNT(*) FROM sessions`).Scan(&n))
	assert.Equal(t, 1, n, "the open sweep removed the expired row")
	_, _, err = reopened.ResolveSession(ctx, live)
	assert.NoError(t, err)
}

// issueForPath issues a session for a fresh uniquely-named account —
// TestOpen_PrunesOnOpen issues twice into one file-backed store.
var issueSeq int

func issueForPath(ctx context.Context, t *testing.T, s *Store) (string, *Session, error) {
	t.Helper()
	issueSeq++
	in := validNew()
	in.Username = fmt.Sprintf("user-%d", issueSeq)
	in.ParticipantID = fmt.Sprintf("user-%d-h", issueSeq)
	acct, err := s.CreateAccount(ctx, in)
	require.NoError(t, err)
	token, sess, err := s.IssueSession(ctx, acct.ID, time.Hour)
	return token, sess, err
}
