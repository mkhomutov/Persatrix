package channels

// ISSUE-0082 PR 1 — per-request session binding store.
//
// SessionResolver makes the orchestrator the authoritative, persisted
// source of a per-request session id keyed on the `(agent, channel)` unit —
// room continuity, per the RFC 0031 §A scope-axes amendment. ISSUE-0083
// dropped the sender axis the original §B PR-2 amendment carried: it only
// ever changed the multi-party-room case, and changed it wrongly (one session
// per speaker fragmented an agent's memory of one room). DMs are unaffected —
// distinct DM threads are already distinct channel ids. It is the source the
// dispatch path feeds into the `persatrix-session` gRPC header (PR 2).
//
// The binding table is the `(agent, channel) → session_id` map; the
// `sessions` table stays the session registry (id, label, created_at, …).
// A pure deterministic hash of the pair would survive a restart without
// persistence, but §B deliberately chose *authoritative + persisted* over
// *derived* so the operator surface (`persatrix session …`) can label,
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
// session id for an `(agent, channel)` pair. It is safe for concurrent use:
// minting runs in a single transaction guarded by `INSERT … ON CONFLICT DO
// NOTHING` + re-read, so a concurrent first-sight of the same pair resolves
// to one id with no orphan session row.
type SessionResolver struct {
	db *sql.DB
}

// ErrEmptySessionAxis is returned by [SessionResolver.Resolve] when either of
// the (agent, channel) axes is empty. The session unit is defined on both
// axes (RFC 0031 §A scope-axes amendment); an empty component is never a valid
// unit, so the resolver fails loud rather than minting a session keyed on an
// empty axis — a junk row would pollute the `sessions` registry the Phase 3
// CLI surfaces and mis-group the per-room recall this issue isolates.
// Callers compare with [errors.Is], matching the package's sentinel
// convention.
var ErrEmptySessionAxis = errors.New("channels: session resolve requires non-empty (agent, channel)")

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

// Resolve returns the session id bound to `(agentID, channelID)`, minting
// and persisting a new one on first sight. The returned id is never empty
// and never the `legacy` carve-out — it is always a concrete UUIDv7
// registered in the `sessions` table.
//
// Both axes are load-bearing (RFC 0031 §A): an empty agent or channel is a
// caller bug and returns [ErrEmptySessionAxis] without touching the store,
// never a session minted on a partial pair.
func (r *SessionResolver) Resolve(ctx context.Context, agentID, channelID string) (string, error) {
	if agentID == "" || channelID == "" {
		return "", fmt.Errorf("%w: agent=%q channel=%q",
			ErrEmptySessionAxis, agentID, channelID)
	}
	// Fast path: an existing binding is the overwhelmingly common case —
	// one mint per room, every subsequent message reuses it. No transaction
	// needed for the read.
	if sid, ok, err := r.lookup(ctx, r.db, agentID, channelID); err != nil {
		return "", err
	} else if ok {
		return sid, nil
	}
	return r.mint(ctx, agentID, channelID)
}

// lookup reads the bound session id for the pair. `ok` is false (with a
// nil error) when no binding exists yet.
func (r *SessionResolver) lookup(ctx context.Context, q rowQuerier, agentID, channelID string) (sid string, ok bool, err error) {
	err = q.QueryRowContext(ctx,
		`SELECT session_id FROM session_bindings
		   WHERE agent_id = ? AND channel_id = ?`,
		agentID, channelID).Scan(&sid)
	switch {
	case err == nil:
		return sid, true, nil
	case errors.Is(err, sql.ErrNoRows):
		return "", false, nil
	default:
		return "", false, fmt.Errorf("channels: lookup session binding: %w", err)
	}
}

// mint creates a new session for an unseen pair. It inserts the binding
// with `ON CONFLICT DO NOTHING` and only registers the `sessions` row when
// that insert won the race, so a concurrent first-sight that lost cannot
// leave an orphan session row. The canonical id is then re-read inside the
// same transaction — the winner's id, whether or not this call won.
func (r *SessionResolver) mint(ctx context.Context, agentID, channelID string) (string, error) {
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
		return "", fmt.Errorf("channels: begin session mint tx: %w", err)
	}
	defer func() { _ = tx.Rollback() }()

	res, err := tx.ExecContext(ctx,
		`INSERT INTO session_bindings (agent_id, channel_id, session_id, created_at)
		 VALUES (?, ?, ?, ?)
		 ON CONFLICT(agent_id, channel_id) DO NOTHING`,
		agentID, channelID, newID.String(), now)
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
	//
	// Only id + created_at are written; `label` and `metadata_json` stay
	// NULL. Minting registers the session so it is discoverable (Phase 3
	// `persatrix session list`); naming it is a separate operator action,
	// not the mint path's job. This is the first writer of the `sessions`
	// table, so it sets that precedent.
	if won == 1 {
		if _, err := tx.ExecContext(ctx,
			`INSERT INTO sessions (id, created_at) VALUES (?, ?)
			 ON CONFLICT(id) DO NOTHING`,
			newID.String(), now); err != nil {
			return "", fmt.Errorf("channels: register session: %w", err)
		}
	}

	sid, ok, err := r.lookup(ctx, tx, agentID, channelID)
	if err != nil {
		return "", err
	}
	if !ok {
		// Unreachable: we either inserted the binding above or it already
		// existed. Treat a vanished row as a hard error rather than risk a
		// second mint outside this transaction.
		return "", fmt.Errorf("channels: session binding missing after insert for (%q,%q)",
			agentID, channelID)
	}
	if err := tx.Commit(); err != nil {
		return "", fmt.Errorf("channels: commit session mint tx: %w", err)
	}
	return sid, nil
}
