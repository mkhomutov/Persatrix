package accounts

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"crypto/subtle"
	"database/sql"
	"encoding/base64"
	"encoding/hex"
	"errors"
	"fmt"
	"time"
)

// Session store (RFC 0039 §D): opaque server-side sessions, persisted
// token-hash-only. Every login path — whatever the §I authentication
// method — ends here: Authenticate yields an account, IssueSession
// turns it into a bearer credential, ResolveSession is the per-request
// gate the auth middleware (next PR) calls.

const (
	// sessionTokenBytes sizes the opaque token: 256 bits from
	// crypto/rand, base64url on the wire (§D).
	sessionTokenBytes = 32

	// lastUsedRefreshInterval is the §D lazy-refresh threshold:
	// last_used_at is rewritten only once it has gone this stale.
	// SQLite is single-writer, so refreshing per request would serialise
	// an UPDATE onto the hot authentication path for a column that only
	// feeds session-list display and idle reporting.
	lastUsedRefreshInterval = 5 * time.Minute
)

// Session-resolution sentinels. The REST layer collapses all of them to
// the identical 401 (§D — no probe may distinguish why a token died);
// they stay distinct here for audit detail.
var (
	// ErrSessionNotFound — no session row matches the supplied token.
	ErrSessionNotFound = errors.New("accounts: session not found")
	// ErrSessionExpired — the session's expires_at has passed.
	ErrSessionExpired = errors.New("accounts: session expired")
	// ErrSessionRevoked — the session was logged out or revoked.
	ErrSessionRevoked = errors.New("accounts: session revoked")
	// ErrAccountDisabled — the bound account is not `active`; also
	// returned by the §I authenticator when a disabled account presents
	// a correct credential.
	ErrAccountDisabled = errors.New("accounts: account disabled")
)

// mintSessionToken draws a fresh 256-bit token from crypto/rand,
// base64url-encoded (§D).
func mintSessionToken() (string, error) {
	raw := make([]byte, sessionTokenBytes)
	if _, err := rand.Read(raw); err != nil {
		return "", fmt.Errorf("accounts: mint session token: %w", err)
	}
	return base64.RawURLEncoding.EncodeToString(raw), nil
}

// hashSessionToken reduces a supplied token to the stored form:
// hex(sha256(token)). The reduction is the ISSUE-0004 discipline
// generalized — whatever the length or content of the input, the
// database only ever sees a fixed-size digest, so neither property of a
// stored token can leak through a lookup's timing.
func hashSessionToken(token string) string {
	digest := sha256.Sum256([]byte(token))
	return hex.EncodeToString(digest[:])
}

// IssueSession mints an opaque token for `accountID` and persists its
// hash with the supplied TTL. The raw token is returned exactly once,
// here — it is never stored (§D). The account must exist and be active:
// a disabled account must not acquire a live credential.
func (s *Store) IssueSession(ctx context.Context, accountID string, ttl time.Duration) (string, *Session, error) {
	if ttl < time.Second {
		// Sub-second TTLs would truncate to a born-expired row (storage
		// is second-precision).
		return "", nil, fmt.Errorf("accounts: session ttl must be at least 1s (got %s)", ttl)
	}
	// Check-then-insert, not a transaction: a disable racing this issue
	// could still land a row, but ResolveSession re-checks account
	// status on every request, so such a session is dead at first use.
	acct, err := s.GetAccount(ctx, accountID)
	if err != nil {
		return "", nil, err
	}
	if acct.Status != StatusActive {
		return "", nil, fmt.Errorf("%w: cannot issue session", ErrAccountDisabled)
	}

	token, err := mintSessionToken()
	if err != nil {
		return "", nil, err
	}
	now := time.Now().UTC().Truncate(time.Second)
	sess := &Session{
		TokenHash:  hashSessionToken(token),
		AccountID:  accountID,
		IssuedAt:   now,
		ExpiresAt:  now.Add(ttl).Truncate(time.Second),
		LastUsedAt: now,
	}
	_, err = s.db.ExecContext(ctx, `
		INSERT INTO sessions (token_hash, account_id, issued_at, expires_at, last_used_at, revoked_at)
		VALUES (?, ?, ?, ?, ?, NULL)`,
		sess.TokenHash, sess.AccountID, sess.IssuedAt.Unix(), sess.ExpiresAt.Unix(), sess.LastUsedAt.Unix())
	if err != nil {
		return "", nil, fmt.Errorf("accounts: insert session: %w", err)
	}
	return token, sess, nil
}

// ResolveSession is the per-request bearer gate (§D): it reduces the
// supplied token to its digest, looks the session up by that primary
// key, and returns the session plus its bound account only when the
// session is unexpired, unrevoked, and the account is active.
//
// As a side effect it refreshes last_used_at, lazily (see
// lastUsedRefreshInterval).
func (s *Store) ResolveSession(ctx context.Context, token string) (*Session, *Account, error) {
	supplied := hashSessionToken(token)
	sess, err := s.getSession(ctx, supplied)
	if err != nil {
		return nil, nil, err
	}
	// Defensive re-verification of what the primary-key lookup already
	// matched, in the fixed-size-digest ConstantTimeCompare shape
	// ISSUE-0004 established. Both inputs are SHA-256 digests, so the
	// comparison cost is independent of anything an attacker controls.
	if subtle.ConstantTimeCompare([]byte(supplied), []byte(sess.TokenHash)) != 1 {
		return nil, nil, ErrSessionNotFound
	}

	now := time.Now().UTC().Truncate(time.Second)
	if sess.RevokedAt != nil {
		return nil, nil, ErrSessionRevoked
	}
	if !sess.ExpiresAt.After(now) {
		return nil, nil, ErrSessionExpired
	}
	acct, err := s.GetAccount(ctx, sess.AccountID)
	if err != nil {
		return nil, nil, err
	}
	if acct.Status != StatusActive {
		return nil, nil, ErrAccountDisabled
	}

	if now.Sub(sess.LastUsedAt) > lastUsedRefreshInterval {
		// Best-effort: the column is display-only (§D — "no functional
		// gain"), so a failed refresh must not turn a session this call
		// just validated as live into an auth failure. On failure the
		// returned struct keeps the stale at-rest value.
		if _, err := s.db.ExecContext(ctx,
			`UPDATE sessions SET last_used_at = ? WHERE token_hash = ?`,
			now.Unix(), sess.TokenHash); err == nil {
			sess.LastUsedAt = now
		}
	}
	return sess, acct, nil
}

// RevokeSession sets revoked_at on the session matching `token` — the
// logout path (§D). Idempotent: revoking an already-revoked session is
// a no-op success and never advances the original revocation instant;
// only a token that was never issued (or already pruned) errors.
func (s *Store) RevokeSession(ctx context.Context, token string) error {
	hash := hashSessionToken(token)
	now := time.Now().UTC().Truncate(time.Second)
	res, err := s.db.ExecContext(ctx,
		`UPDATE sessions SET revoked_at = ? WHERE token_hash = ? AND revoked_at IS NULL`,
		now.Unix(), hash)
	if err != nil {
		return fmt.Errorf("accounts: revoke session: %w", err)
	}
	n, err := res.RowsAffected()
	if err != nil {
		return fmt.Errorf("accounts: revoke session: %w", err)
	}
	if n == 0 {
		if _, err := s.getSession(ctx, hash); err != nil {
			return err
		}
	}
	return nil
}

// PruneSessions deletes every expired or revoked row (§D): sessions are
// server-side, so a deleted row *is* the revocation record — the table
// tracks live sessions rather than growing without bound. Runs on store
// open; a deployment may also call it periodically. The per-request
// lookup stays a single primary-key hit regardless.
func (s *Store) PruneSessions(ctx context.Context) (int64, error) {
	// `<=` matches the resolve gate exactly: a row resolve would reject
	// as expired is already prunable in the same second.
	res, err := s.db.ExecContext(ctx,
		`DELETE FROM sessions WHERE expires_at <= ? OR revoked_at IS NOT NULL`,
		time.Now().UTC().Unix())
	if err != nil {
		return 0, fmt.Errorf("accounts: prune sessions: %w", err)
	}
	n, err := res.RowsAffected()
	if err != nil {
		return 0, fmt.Errorf("accounts: prune sessions: %w", err)
	}
	return n, nil
}

// getSession fetches one session row by token hash.
func (s *Store) getSession(ctx context.Context, tokenHash string) (*Session, error) {
	row := s.db.QueryRowContext(ctx, `
		SELECT token_hash, account_id, issued_at, expires_at, last_used_at, revoked_at
		FROM sessions WHERE token_hash = ?`, tokenHash)
	var (
		sess     Session
		issued   int64
		expires  int64
		lastUsed int64
		revoked  sql.NullInt64
	)
	err := row.Scan(&sess.TokenHash, &sess.AccountID, &issued, &expires, &lastUsed, &revoked)
	if err == sql.ErrNoRows {
		return nil, ErrSessionNotFound
	}
	if err != nil {
		return nil, fmt.Errorf("accounts: scan session: %w", err)
	}
	sess.IssuedAt = time.Unix(issued, 0).UTC()
	sess.ExpiresAt = time.Unix(expires, 0).UTC()
	sess.LastUsedAt = time.Unix(lastUsed, 0).UTC()
	if revoked.Valid {
		at := time.Unix(revoked.Int64, 0).UTC()
		sess.RevokedAt = &at
	}
	return &sess, nil
}
