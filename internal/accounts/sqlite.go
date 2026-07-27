package accounts

import (
	"context"
	"database/sql"
	"fmt"
	"net/url"
	"strings"
	"time"

	"github.com/google/uuid"

	_ "modernc.org/sqlite" // pure-Go SQLite driver; matches CGO_ENABLED=0 build (Dockerfile.orchestrator)
)

// Store is the durable account store over `accounts.db`. It is safe for
// concurrent use; like the channel store, the pool is pinned to one
// connection so every read-then-write CRUD path is serialised without a
// busy-retry loop. Authentication traffic is a per-request primary-key
// lookup — nowhere near SQLite's single-writer ceiling.
type Store struct {
	db *sql.DB
}

// Open opens (or creates) the account database at `path`, applying the
// versioned schema migration. WAL mode and foreign-key enforcement are
// set at connection time — the latter makes `sessions.account_id →
// accounts(id)` enforced rather than decorative (RFC 0039 §B).
//
// Pass a filesystem path for production, or a per-test temp path in
// tests (`:memory:` also works — with MaxOpenConns(1) there is exactly
// one connection, so the in-memory database is stable).
func Open(path string) (*Store, error) {
	db, err := sql.Open("sqlite", buildDSN(path))
	if err != nil {
		return nil, fmt.Errorf("accounts: open %s: %w", path, err)
	}
	// One connection, same rationale as the channel store: every write
	// path below is a read-then-write whose correctness is simplest
	// under a single serialised connection, and nothing on the auth
	// path needs read concurrency at v0.3.x scale.
	db.SetMaxOpenConns(1)

	if err := applySchema(db); err != nil {
		_ = db.Close()
		return nil, err
	}
	s := &Store{db: db}
	// §D: sweep expired/revoked sessions on open, so the table tracks
	// live sessions across restarts even if no periodic prune ever runs.
	if _, err := s.PruneSessions(context.Background()); err != nil {
		_ = db.Close()
		return nil, err
	}
	return s, nil
}

// Close releases the underlying database handle.
func (s *Store) Close() error {
	return s.db.Close()
}

// buildDSN appends the store's connection pragmas to `path`, preserving
// any caller-supplied query parameters — the channels buildDSN shape.
func buildDSN(path string) string {
	base, existing := path, ""
	if i := strings.Index(path, "?"); i >= 0 {
		base, existing = path[:i], path[i+1:]
	}
	q, _ := url.ParseQuery(existing)
	q.Add("_pragma", "foreign_keys(1)")
	q.Add("_pragma", "journal_mode(WAL)")
	q.Add("_pragma", "busy_timeout(5000)")
	return base + "?" + q.Encode()
}

// NewAccount is the CreateAccount input. Zero-value convenience:
// AuthMethod defaults to `password`, Role to `user`; Status always
// starts `active`.
type NewAccount struct {
	Username      string // case-folded on write
	PasswordHash  string // argon2id PHC string; required for the password method, forbidden otherwise
	Role          string
	ParticipantID string
	AuthMethod    string
}

// CreateAccount validates the §B write-boundary rules, mints the
// account UUID, and inserts the row. The username is case-folded before
// the write; the returned Account carries the stored (folded) form.
func (s *Store) CreateAccount(ctx context.Context, in NewAccount) (*Account, error) {
	if in.AuthMethod == "" {
		in.AuthMethod = AuthMethodPassword
	}
	if in.Role == "" {
		in.Role = RoleUser
	}
	folded := FoldUsername(in.Username)
	if err := validateUsername(folded); err != nil {
		return nil, err
	}
	if !validAuthMethods[in.AuthMethod] {
		return nil, fmt.Errorf("accounts: unknown auth_method %q", in.AuthMethod)
	}
	if !validRoles[in.Role] {
		return nil, fmt.Errorf("accounts: unknown role %q", in.Role)
	}
	if err := validateParticipantID(in.ParticipantID); err != nil {
		return nil, err
	}
	// §B: password_hash is the credential for the password method and
	// NULL for every other — enforce both directions so a methodless
	// hash (or a hashless password account) cannot become a row.
	if in.AuthMethod == AuthMethodPassword && in.PasswordHash == "" {
		return nil, fmt.Errorf("accounts: password auth_method requires a password hash")
	}
	if in.AuthMethod != AuthMethodPassword && in.PasswordHash != "" {
		return nil, fmt.Errorf("accounts: auth_method %q must not carry a password hash", in.AuthMethod)
	}

	now := time.Now().UTC().Truncate(time.Second)
	a := &Account{
		ID:            uuid.NewString(),
		Username:      folded,
		AuthMethod:    in.AuthMethod,
		PasswordHash:  in.PasswordHash,
		Role:          in.Role,
		ParticipantID: in.ParticipantID,
		Status:        StatusActive,
		CreatedAt:     now,
		UpdatedAt:     now,
	}
	_, err := s.db.ExecContext(ctx, `
		INSERT INTO accounts (id, username, auth_method, password_hash, role, participant_id, status, created_at, updated_at)
		VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)`,
		a.ID, a.Username, a.AuthMethod, nullableString(a.PasswordHash), a.Role,
		a.ParticipantID, a.Status, a.CreatedAt.Unix(), a.UpdatedAt.Unix())
	if err != nil {
		return nil, mapUniqueViolation(err)
	}
	return a, nil
}

// GetAccount fetches one account by UUID.
func (s *Store) GetAccount(ctx context.Context, id string) (*Account, error) {
	return s.getWhere(ctx, `id = ?`, id)
}

// GetAccountByUsername fetches one account by login name, applying the
// same case-fold as the write path so lookups match writes.
func (s *Store) GetAccountByUsername(ctx context.Context, username string) (*Account, error) {
	return s.getWhere(ctx, `username = ?`, FoldUsername(username))
}

// ListAccounts returns every account, oldest first (id tie-break for a
// stable order at equal second-precision timestamps).
func (s *Store) ListAccounts(ctx context.Context) ([]Account, error) {
	rows, err := s.db.QueryContext(ctx, selectAccounts+` ORDER BY created_at, id`)
	if err != nil {
		return nil, fmt.Errorf("accounts: list: %w", err)
	}
	defer rows.Close()
	var out []Account
	for rows.Next() {
		a, err := scanAccount(rows)
		if err != nil {
			return nil, err
		}
		out = append(out, *a)
	}
	return out, rows.Err()
}

// CountAccounts reports the total number of accounts — the §G bootstrap
// zero-accounts precondition reads this inside its transaction.
func (s *Store) CountAccounts(ctx context.Context) (int, error) {
	var n int
	if err := s.db.QueryRowContext(ctx, `SELECT COUNT(*) FROM accounts`).Scan(&n); err != nil {
		return 0, fmt.Errorf("accounts: count: %w", err)
	}
	return n, nil
}

// SetAccountStatus flips an account between `active` and `disabled`
// (§K: accounts are disabled, never deleted).
func (s *Store) SetAccountStatus(ctx context.Context, id, status string) error {
	if !validStatuses[status] {
		return fmt.Errorf("accounts: unknown status %q", status)
	}
	return s.updateField(ctx, id, `status`, status)
}

// SetPasswordHash re-stores the credential — the §C verify-then-rehash
// write path, and later the Phase 3 password change/reset. The §B write
// boundary holds on update as it does on create: the hash must be a
// well-formed argon2id PHC string, and only a password-method account
// may carry one — the method guard rides in the UPDATE's WHERE clause
// so it cannot race a concurrent write.
func (s *Store) SetPasswordHash(ctx context.Context, id, phcHash string) error {
	if _, _, _, err := decodePHC(phcHash); err != nil {
		return err
	}
	now := time.Now().UTC().Truncate(time.Second)
	res, err := s.db.ExecContext(ctx,
		`UPDATE accounts SET password_hash = ?, updated_at = ? WHERE id = ? AND auth_method = ?`,
		phcHash, now.Unix(), id, AuthMethodPassword)
	if err != nil {
		return fmt.Errorf("accounts: update password_hash: %w", err)
	}
	n, err := res.RowsAffected()
	if err != nil {
		return fmt.Errorf("accounts: update password_hash: %w", err)
	}
	if n == 0 {
		// Distinguish an unknown id from a non-password account.
		if _, getErr := s.GetAccount(ctx, id); getErr != nil {
			return getErr
		}
		return fmt.Errorf("accounts: auth_method must be %q to carry a password hash (§B)", AuthMethodPassword)
	}
	return nil
}

// selectAccounts is the shared projection; scanAccount is its inverse.
const selectAccounts = `
	SELECT id, username, auth_method, password_hash, role, participant_id, status, created_at, updated_at
	FROM accounts`

func (s *Store) getWhere(ctx context.Context, where string, arg any) (*Account, error) {
	row := s.db.QueryRowContext(ctx, selectAccounts+` WHERE `+where, arg)
	a, err := scanAccount(row)
	if err == sql.ErrNoRows {
		return nil, ErrAccountNotFound
	}
	return a, err
}

func (s *Store) updateField(ctx context.Context, id, column, value string) error {
	now := time.Now().UTC().Truncate(time.Second)
	res, err := s.db.ExecContext(ctx,
		`UPDATE accounts SET `+column+` = ?, updated_at = ? WHERE id = ?`,
		value, now.Unix(), id)
	if err != nil {
		return fmt.Errorf("accounts: update %s: %w", column, err)
	}
	n, err := res.RowsAffected()
	if err != nil {
		return fmt.Errorf("accounts: update %s: %w", column, err)
	}
	if n == 0 {
		return ErrAccountNotFound
	}
	return nil
}

// scanner covers both *sql.Row and *sql.Rows.
type scanner interface{ Scan(dest ...any) error }

func scanAccount(row scanner) (*Account, error) {
	var (
		a       Account
		hash    sql.NullString
		created int64
		updated int64
	)
	err := row.Scan(&a.ID, &a.Username, &a.AuthMethod, &hash, &a.Role,
		&a.ParticipantID, &a.Status, &created, &updated)
	if err != nil {
		if err == sql.ErrNoRows {
			return nil, err
		}
		return nil, fmt.Errorf("accounts: scan: %w", err)
	}
	a.PasswordHash = hash.String
	a.CreatedAt = time.Unix(created, 0).UTC()
	a.UpdatedAt = time.Unix(updated, 0).UTC()
	return &a, nil
}

// nullableString maps "" to SQL NULL — password_hash is NULL, not
// empty-string, for non-password methods (§B).
func nullableString(s string) any {
	if s == "" {
		return nil
	}
	return s
}

// mapUniqueViolation translates the driver's UNIQUE failures into the
// package sentinels so callers branch on errors.Is instead of parsing
// driver strings themselves. modernc.org/sqlite reports the violated
// index as "UNIQUE constraint failed: <table>.<column>"; anything
// unrecognised passes through wrapped.
func mapUniqueViolation(err error) error {
	msg := err.Error()
	switch {
	case strings.Contains(msg, "UNIQUE constraint failed: accounts.username"):
		return fmt.Errorf("%w: %v", ErrUsernameTaken, err)
	case strings.Contains(msg, "UNIQUE constraint failed: accounts.participant_id"):
		return fmt.Errorf("%w: %v", ErrParticipantBound, err)
	default:
		return fmt.Errorf("accounts: insert: %w", err)
	}
}
