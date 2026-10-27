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
// connection does not hold the society tables (channels-store v15), or does
// not enforce foreign keys: replacing the graph relies on deleting an
// organization deleting its roles, members and edges.
func NewSQLiteStore(src ConnSource) (Store, error) {
	db := src.SocietyConn()
	if db == nil {
		return nil, errors.New("society: the channels store handed over no connection")
	}
	var version int64
	if err := db.QueryRow(`SELECT graph_version FROM society_meta WHERE id = 1`).Scan(&version); err != nil {
		return nil, fmt.Errorf("society: read society_meta: %w", err)
	}
	var foreignKeys bool
	if err := db.QueryRow(`PRAGMA foreign_keys`).Scan(&foreignKeys); err != nil {
		return nil, fmt.Errorf("society: read foreign_keys: %w", err)
	}
	if !foreignKeys {
		return nil, errors.New("society: the connection does not enforce foreign keys")
	}
	return &sqliteStore{db: db, now: time.Now}, nil
}

// readOnly begins a transaction that only reads. Under the driver's
// `_txlock=immediate` every other transaction takes the write lock as it
// begins, so a read that did not say so would queue behind every writer.
var readOnly = &sql.TxOptions{ReadOnly: true}

// inTxMark keys the mark InTx puts on its function's context: the
// connection that function's transaction holds.
type inTxMark struct{}

// InTx implements [Store].
func (s *sqliteStore) InTx(ctx context.Context, fn func(context.Context, Tx) error) error {
	return s.run(ctx, nil, func(tx *sqliteTx) error {
		return fn(context.WithValue(ctx, inTxMark{}, s.db), tx)
	})
}

// run is InTx on the concrete handle, in a transaction begun with opts. A
// context InTx marked for this connection is refused before it waits for
// the connection its own transaction holds. The deferred rollback also ends
// the transaction when fn panics; after a commit it does nothing.
func (s *sqliteStore) run(ctx context.Context, opts *sql.TxOptions, fn func(*sqliteTx) error) error {
	if ctx.Value(inTxMark{}) == s.db {
		return fmt.Errorf("%w: a call inside InTx goes through its Tx, not the Store", ErrInvalid)
	}
	tx, err := s.db.BeginTx(ctx, opts)
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

// inTx runs one call in a transaction of its own, begun with opts, and
// returns its result.
func inTx[T any](ctx context.Context, s *sqliteStore, opts *sql.TxOptions, call func(*sqliteTx) (T, error)) (T, error) {
	var out T
	err := s.run(ctx, opts, func(tx *sqliteTx) error {
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
	return inTx(ctx, s, readOnly, func(tx *sqliteTx) (Graph, error) { return tx.Graph(ctx) })
}

func (s *sqliteStore) ReplaceGraph(ctx context.Context, g Graph) error {
	return s.run(ctx, nil, func(tx *sqliteTx) error { return tx.ReplaceGraph(ctx, g) })
}

func (s *sqliteStore) OpenApproval(ctx context.Context, a Approval) (Approval, error) {
	return inTx(ctx, s, nil, func(tx *sqliteTx) (Approval, error) { return tx.OpenApproval(ctx, a) })
}

func (s *sqliteStore) Approval(ctx context.Context, id string) (Approval, error) {
	return inTx(ctx, s, readOnly, func(tx *sqliteTx) (Approval, error) { return tx.Approval(ctx, id) })
}

func (s *sqliteStore) ListApprovals(ctx context.Context, q ApprovalQuery) ([]Approval, error) {
	return inTx(ctx, s, readOnly, func(tx *sqliteTx) ([]Approval, error) { return tx.ListApprovals(ctx, q) })
}

func (s *sqliteStore) MoveApproval(ctx context.Context, id string, m Move) (Approval, error) {
	return inTx(ctx, s, nil, func(tx *sqliteTx) (Approval, error) { return tx.MoveApproval(ctx, id, m) })
}

func (s *sqliteStore) MoveApprovals(ctx context.Context, q ApprovalQuery, to State) ([]Approval, error) {
	return inTx(ctx, s, nil, func(tx *sqliteTx) ([]Approval, error) { return tx.MoveApprovals(ctx, q, to) })
}

func (s *sqliteStore) Key(ctx context.Context, purpose string) (Key, error) {
	return inTx(ctx, s, nil, func(tx *sqliteTx) (Key, error) { return tx.Key(ctx, purpose) })
}

// Key implements [Ops]. It stores a fresh key unless one is kept, then
// reads the one kept. The insert comes first, so in a transaction of its own
// the call takes the write lock before it reads anything, and two first
// calls, even on two connections, store one key and both return it. Inside
// a caller's transaction that has already read, on a second connection, the
// insert can fail with SQLITE_BUSY as any write after a read can; on the
// channels store's own connection it cannot (S-2).
func (t *sqliteTx) Key(ctx context.Context, purpose string) (Key, error) {
	if purpose == "" {
		return Key{}, fmt.Errorf("%w: a key needs a purpose", ErrInvalid)
	}
	fresh := make([]byte, KeySize)
	if _, err := rand.Read(fresh); err != nil {
		return Key{}, fmt.Errorf("society: make key: %w", err)
	}
	if _, err := t.tx.ExecContext(ctx,
		`INSERT INTO society_keys (purpose, key, created_at) VALUES (?, ?, ?)
		 ON CONFLICT (purpose) DO NOTHING`, purpose, fresh, t.now().Unix()); err != nil {
		return Key{}, fmt.Errorf("society: store key: %w", err)
	}
	var kept []byte
	if err := t.tx.QueryRowContext(ctx,
		`SELECT key FROM society_keys WHERE purpose = ?`, purpose).Scan(&kept); err != nil {
		return Key{}, fmt.Errorf("society: read key: %w", err)
	}
	return Key{secret: &kept}, nil
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
