package society

// The SQLite society store, on the channels store's connection (RFC 0029
// Phase 3 plan S-2 and S-6). Each Store call opens a transaction of its own
// and runs the same code a Tx runs inside the caller's, so the two forms
// cannot drift. The graph lives in graph.go and the approval rows in
// approvals.go; the key is here.

import (
	"context"
	"crypto/rand"
	"database/sql"
	"errors"
	"fmt"
	"time"
)

// sqliteStore is the [Store] on the channels store's connection.
type sqliteStore struct {
	db  *sql.DB
	now func() time.Time
}

// sqliteTx is the [Tx] InTx hands its function, and the handle every Store
// call runs on.
type sqliteTx struct {
	tx  *sql.Tx
	now func() time.Time
}

// NewSQLiteStore builds the society store on src's connection, which it
// shares and never closes: the channels store owns it. It fails if the
// connection does not hold the society tables (channels-store v15).
func NewSQLiteStore(src ConnSource) (Store, error) {
	db := src.SocietyConn()
	if db == nil {
		return nil, errors.New("society: the channels store handed over no connection")
	}
	var version int64
	if err := db.QueryRow(`SELECT graph_version FROM society_meta WHERE id = 1`).Scan(&version); err != nil {
		return nil, fmt.Errorf("society: read society_meta: %w", err)
	}
	return &sqliteStore{db: db, now: time.Now}, nil
}

// InTx implements [Store].
func (s *sqliteStore) InTx(ctx context.Context, fn func(Tx) error) error {
	return s.run(ctx, func(tx *sqliteTx) error { return fn(tx) })
}

// run is InTx on the concrete handle. The deferred rollback also ends the
// transaction when fn panics; after a commit it does nothing.
func (s *sqliteStore) run(ctx context.Context, fn func(*sqliteTx) error) error {
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return fmt.Errorf("society: begin: %w", err)
	}
	defer func() { _ = tx.Rollback() }()
	if err := fn(&sqliteTx{tx: tx, now: s.now}); err != nil {
		return err
	}
	if err := tx.Commit(); err != nil {
		return fmt.Errorf("society: commit: %w", err)
	}
	return nil
}

// inTx runs one call in a transaction of its own and returns its result.
func inTx[T any](ctx context.Context, s *sqliteStore, call func(*sqliteTx) (T, error)) (T, error) {
	var out T
	err := s.run(ctx, func(tx *sqliteTx) error {
		var err error
		out, err = call(tx)
		return err
	})
	if err != nil {
		var zero T
		return zero, err
	}
	return out, nil
}

func (s *sqliteStore) Graph(ctx context.Context) (Graph, error) {
	return inTx(ctx, s, func(tx *sqliteTx) (Graph, error) { return tx.Graph(ctx) })
}

func (s *sqliteStore) ReplaceGraph(ctx context.Context, g Graph) error {
	return s.run(ctx, func(tx *sqliteTx) error { return tx.ReplaceGraph(ctx, g) })
}

func (s *sqliteStore) OpenApproval(ctx context.Context, a Approval) (Approval, error) {
	return inTx(ctx, s, func(tx *sqliteTx) (Approval, error) { return tx.OpenApproval(ctx, a) })
}

func (s *sqliteStore) Approval(ctx context.Context, id string) (Approval, error) {
	return inTx(ctx, s, func(tx *sqliteTx) (Approval, error) { return tx.Approval(ctx, id) })
}

func (s *sqliteStore) ListApprovals(ctx context.Context, q ApprovalQuery) ([]Approval, error) {
	return inTx(ctx, s, func(tx *sqliteTx) ([]Approval, error) { return tx.ListApprovals(ctx, q) })
}

func (s *sqliteStore) MoveApproval(ctx context.Context, id string, m Move) (Approval, error) {
	return inTx(ctx, s, func(tx *sqliteTx) (Approval, error) { return tx.MoveApproval(ctx, id, m) })
}

func (s *sqliteStore) Key(ctx context.Context, purpose string) (Key, error) {
	return inTx(ctx, s, func(tx *sqliteTx) (Key, error) { return tx.Key(ctx, purpose) })
}

// Key implements [Ops]. The insert names its conflict, so even two first
// calls on two connections keep one key; the one stored is the one returned.
func (t *sqliteTx) Key(ctx context.Context, purpose string) (Key, error) {
	if purpose == "" {
		return nil, fmt.Errorf("%w: a key needs a purpose", ErrInvalid)
	}
	key, err := t.readKey(ctx, purpose)
	if err == nil || !errors.Is(err, sql.ErrNoRows) {
		return key, err
	}
	fresh := make([]byte, KeySize)
	if _, err := rand.Read(fresh); err != nil {
		return nil, fmt.Errorf("society: make key: %w", err)
	}
	if _, err := t.tx.ExecContext(ctx,
		`INSERT INTO society_keys (purpose, key, created_at) VALUES (?, ?, ?)
		 ON CONFLICT (purpose) DO NOTHING`, purpose, fresh, t.now().Unix()); err != nil {
		return nil, fmt.Errorf("society: store key: %w", err)
	}
	return t.readKey(ctx, purpose)
}

func (t *sqliteTx) readKey(ctx context.Context, purpose string) (Key, error) {
	var key []byte
	err := t.tx.QueryRowContext(ctx, `SELECT key FROM society_keys WHERE purpose = ?`, purpose).Scan(&key)
	if err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			return nil, err
		}
		return nil, fmt.Errorf("society: read key: %w", err)
	}
	return Key(key), nil
}

// unixOrNull stores a time as unix seconds, and a zero time as NULL.
func unixOrNull(t time.Time) any {
	if t.IsZero() {
		return nil
	}
	return t.Unix()
}

// fromUnix reads unix seconds back as a UTC time, and NULL as a zero time.
func fromUnix(n sql.NullInt64) time.Time {
	if !n.Valid {
		return time.Time{}
	}
	return time.Unix(n.Int64, 0).UTC()
}
