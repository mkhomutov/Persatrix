package channels

// ISSUE-0082 PR 1 — per-request session binding store.
//
// SessionResolver makes the orchestrator the authoritative, persisted
// source of a per-request session id keyed on the `(agent, channel, user)`
// unit decided in RFC 0031 §B (the PR 2 amendment). It is the source the
// dispatch path will feed into the `persatrix-session` gRPC header in PR 2;
// this PR ships the store only — there is no production caller yet, so
// behaviour is unchanged for every live deployment.
//
// The binding table is the `(agent, channel, user) → session_id` map; the
// `sessions` table stays the session registry (id, label, created_at, …).
// A pure deterministic hash of the triple would survive a restart without
// persistence, but §B deliberately chose *authoritative + persisted* over
// *derived* so a future operator surface (`persatrix session …`) can label,
// list, and archive the session as a first-class registered row.

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"time"

	"github.com/google/uuid"
)

// SessionResolver resolves (and on first sight mints + persists) the
// session id for an `(agent, channel, user)` triple. It is safe for
// concurrent use: minting runs in a single transaction guarded by
// `INSERT … ON CONFLICT DO NOTHING` + re-read, so a concurrent first-sight
// of the same triple resolves to one id with no orphan session row.
type SessionResolver struct {
	db *sql.DB
}

// ErrEmptySessionAxis is returned by [SessionResolver.Resolve] when any of
// the (agent, channel, user) axes is empty. The session unit is defined on
// all three axes (RFC 0031 §B); an empty component is never a valid unit, so
// the resolver fails loud rather than minting a session keyed on an empty
// axis — a junk row would pollute the `sessions` registry the Phase 3 CLI
// surfaces and mis-group the per-conversation recall this issue isolates.
// Callers compare with [errors.Is], matching the package's sentinel
// convention.
var ErrEmptySessionAxis = errors.New("channels: session resolve requires non-empty (agent, channel, user)")

// NewSessionResolver builds a resolver over the channel store's database.
// It requires the SQLite-backed [ChannelStore] (the only production
// implementation); a different implementation is a programming error and
// returns an error rather than silently disabling session resolution.
func NewSessionResolver(store ChannelStore) (*SessionResolver, error) {
	s, ok := store.(*sqliteStore)
	if !ok {
		return nil, fmt.Errorf("channels: SessionResolver requires the SQLite store, got %T", store)
	}
	return &SessionResolver{db: s.db}, nil
}

// rowQuerier is the subset of *sql.DB / *sql.Tx the binding lookup needs,
// so the fast path (no tx) and the mint path (inside a tx) share one query.
type rowQuerier interface {
	QueryRowContext(ctx context.Context, query string, args ...any) *sql.Row
}

// Resolve returns the session id bound to `(agentID, channelID, userID)`,
// minting and persisting a new one on first sight. The returned id is
// never empty and never the `legacy` carve-out — it is always a concrete
// UUIDv7 registered in the `sessions` table.
//
// Every axis is load-bearing (RFC 0031 §B): an empty agent, channel, or
// user is a caller bug and returns [ErrEmptySessionAxis] without touching
// the store, never a session minted on a partial triple.
func (r *SessionResolver) Resolve(ctx context.Context, agentID, channelID, userID string) (string, error) {
	if agentID == "" || channelID == "" || userID == "" {
		return "", fmt.Errorf("%w: agent=%q channel=%q user=%q",
			ErrEmptySessionAxis, agentID, channelID, userID)
	}
	// Fast path: an existing binding is the overwhelmingly common case —
	// one mint per conversation, every subsequent message reuses it. No
	// transaction needed for the read.
	if sid, ok, err := r.lookup(ctx, r.db, agentID, channelID, userID); err != nil {
		return "", err
	} else if ok {
		return sid, nil
	}
	return r.mint(ctx, agentID, channelID, userID)
}

// lookup reads the bound session id for the triple. `ok` is false (with a
// nil error) when no binding exists yet.
func (r *SessionResolver) lookup(ctx context.Context, q rowQuerier, agentID, channelID, userID string) (sid string, ok bool, err error) {
	err = q.QueryRowContext(ctx,
		`SELECT session_id FROM session_bindings
		   WHERE agent_id = ? AND channel_id = ? AND user_id = ?`,
		agentID, channelID, userID).Scan(&sid)
	switch {
	case err == nil:
		return sid, true, nil
	case errors.Is(err, sql.ErrNoRows):
		return "", false, nil
	default:
		return "", false, fmt.Errorf("channels: lookup session binding: %w", err)
	}
}

// mint creates a new session for an unseen triple. It inserts the binding
// with `ON CONFLICT DO NOTHING` and only registers the `sessions` row when
// that insert won the race, so a concurrent first-sight that lost cannot
// leave an orphan session row. The canonical id is then re-read inside the
// same transaction — the winner's id, whether or not this call won.
func (r *SessionResolver) mint(ctx context.Context, agentID, channelID, userID string) (string, error) {
	newID, err := uuid.NewV7()
	if err != nil {
		return "", fmt.Errorf("channels: mint session id: %w", err)
	}
	// created_at is unix seconds as a float, matching the REAL column type
	// the sibling `sessions` table uses (not the DATETIME encoding on
	// channels/messages). The sub-second fraction is preserved rather than
	// truncated to whole seconds, so the stamp keeps sub-millisecond
	// resolution.
	now := float64(time.Now().UTC().UnixNano()) / float64(time.Second)

	tx, err := r.db.BeginTx(ctx, nil)
	if err != nil {
		return "", err
	}
	defer func() { _ = tx.Rollback() }()

	res, err := tx.ExecContext(ctx,
		`INSERT INTO session_bindings (agent_id, channel_id, user_id, session_id, created_at)
		 VALUES (?, ?, ?, ?, ?)
		 ON CONFLICT(agent_id, channel_id, user_id) DO NOTHING`,
		agentID, channelID, userID, newID.String(), now)
	if err != nil {
		return "", fmt.Errorf("channels: insert session binding: %w", err)
	}
	won, err := res.RowsAffected()
	if err != nil {
		return "", fmt.Errorf("channels: session binding rows affected: %w", err)
	}
	// Register the session row only when our binding insert won. A loser
	// (DO NOTHING → 0 rows) must not write a `sessions` row no binding
	// references.
	if won == 1 {
		if _, err := tx.ExecContext(ctx,
			`INSERT INTO sessions (id, created_at) VALUES (?, ?)
			 ON CONFLICT(id) DO NOTHING`,
			newID.String(), now); err != nil {
			return "", fmt.Errorf("channels: register session: %w", err)
		}
	}

	sid, ok, err := r.lookup(ctx, tx, agentID, channelID, userID)
	if err != nil {
		return "", err
	}
	if !ok {
		// Unreachable: we either inserted the binding above or it already
		// existed. Treat a vanished row as a hard error rather than risk a
		// second mint outside this transaction.
		return "", fmt.Errorf("channels: session binding missing after insert for (%q,%q,%q)",
			agentID, channelID, userID)
	}
	if err := tx.Commit(); err != nil {
		return "", err
	}
	return sid, nil
}
