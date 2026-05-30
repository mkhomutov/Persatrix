package channels

// RFC 0031 Phase 3 PR 1 — session registry accessor.
//
// SessionRegistry is the operator-facing CRUD surface over the `sessions`
// table — the registry of named, archivable session rows. It is the read/
// write side the `/api/v1/sessions` REST handlers sit on so the thin Rust
// CLI (`persatrix session new / list / archive`) never touches SQLite.
//
// It is deliberately separate from [SessionResolver]:
//
//   - SessionResolver owns the per-request `(agent, channel, user) →
//     session_id` *binding* and mints a registry row as a side effect.
//   - SessionRegistry owns the `sessions` *registry* rows directly — naming,
//     listing, resolving, and archiving them.
//
// Both write through the same `sessions` table over the same DB, so an
// operator-created session and an auto-minted one are indistinguishable rows
// in one registry. The binding table is untouched here: operators create
// *registry* rows, not bindings.
//
// There is no FOREIGN KEY from a binding's `session_id` to `sessions(id)`
// (RFC 0031 §G code-enforced integrity); archive therefore never cascades
// and a binding keeps resolving to an archived session's id — its tagged
// memory rows stay readable, exactly as the §B one-way-archive contract
// requires.

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"time"

	"github.com/google/uuid"
)

// Session is one row in the `sessions` registry.
//
// `CreatedAt` is materialised from the table's REAL unix-seconds column into
// a [time.Time] so callers (and the REST wire shape) get a self-describing
// timestamp rather than a bare float. `Archived` is derived from whether the
// nullable `archived_at` column is set — archive is one-way, so the boolean
// is the only state the operator surface needs.
type Session struct {
	ID        string
	Label     string
	CreatedAt time.Time
	Archived  bool
}

// Session-registry sentinels. Callers compare with [errors.Is], matching the
// package's convention (see [ErrChannelNotFound] et al.).
var (
	// ErrSessionNotFound — GetSession/ArchiveSession against an id-or-label
	// with no row in `sessions`. The synthetic `legacy` carve-out has no
	// row, so it resolves to this too.
	ErrSessionNotFound = errors.New("channels: session not found")

	// ErrReservedSessionID — CreateSession was handed a reserved sentinel
	// (today only [DefaultSessionID]) as a label. Minting it would collide
	// with the always-visible §D `session_id = 'legacy'` carve-out, silently
	// merging the operator's session into the pre-RFC namespace (RFC 0031
	// OQ #2a). Rejected at the store boundary — the server-authoritative
	// guard a direct REST caller cannot bypass.
	ErrReservedSessionID = errors.New("channels: reserved session id")
)

// reservedSessionIDs is the set of labels/ids CreateSession rejects. Only
// the `legacy` carve-out is reserved today; the set keeps the guard a single
// source of truth if §D grows future sentinels.
var reservedSessionIDs = map[string]struct{}{
	DefaultSessionID: {},
}

// isReservedSessionID reports whether s collides with a reserved §D sentinel.
func isReservedSessionID(s string) bool {
	_, ok := reservedSessionIDs[s]
	return ok
}

// SessionRegistry reads and writes the `sessions` registry over the channel
// store's database. Safe for concurrent use without holding a transaction:
// the reads are single statements, and ArchiveSession's read-then-update is
// made race-safe by the `archived_at IS NULL` guard on the UPDATE — a
// concurrent archive in the window between its read and its write turns the
// UPDATE into a 0-row no-op that preserves the original stamp, so no two
// callers can clobber each other.
type SessionRegistry struct {
	db *sql.DB
}

// NewSessionRegistry builds a registry over the channel store's database. It
// requires the SQLite-backed [ChannelStore] (the only production
// implementation); a different implementation is a programming error and
// returns an error rather than silently disabling the operator surface —
// mirroring [NewSessionResolver].
func NewSessionRegistry(store ChannelStore) (*SessionRegistry, error) {
	s, ok := store.(*sqliteStore)
	if !ok {
		return nil, fmt.Errorf("channels: SessionRegistry requires the SQLite store, got %T", store)
	}
	return &SessionRegistry{db: s.db}, nil
}

// CreateSession mints a UUIDv7 id, registers it in `sessions` with `label`,
// and returns the created row. The id is minted the same way the §B mint and
// the ISSUE-0082 resolver mint theirs (UUIDv7) so all three sort
// lexicographically by creation time — the default `list` order.
//
// `label` is rejected when it is a reserved §D sentinel ([isReservedSessionID]).
// The minted id is a fresh UUIDv7 and so can never itself be reserved, but the
// guard is expressed against the value the operator controls — the label.
func (r *SessionRegistry) CreateSession(ctx context.Context, label string) (Session, error) {
	if isReservedSessionID(label) {
		return Session{}, fmt.Errorf("%w: %q is reserved for the legacy carve-out", ErrReservedSessionID, label)
	}
	newID, err := uuid.NewV7()
	if err != nil {
		return Session{}, fmt.Errorf("channels: mint session id: %w", err)
	}
	// created_at is unix seconds as a float, matching the REAL column type and
	// the sibling SessionResolver mint (sub-second fraction preserved).
	now := time.Now().UTC()
	createdAt := float64(now.UnixNano()) / float64(time.Second)

	var labelArg any
	if label != "" {
		labelArg = label
	}
	if _, err := r.db.ExecContext(ctx,
		`INSERT INTO sessions (id, label, created_at) VALUES (?, ?, ?)`,
		newID.String(), labelArg, createdAt); err != nil {
		return Session{}, fmt.Errorf("channels: register session: %w", err)
	}
	return Session{
		ID:        newID.String(),
		Label:     label,
		CreatedAt: secondsToTime(createdAt),
		Archived:  false,
	}, nil
}

// ListSessions returns the registry rows ordered by id ascending — UUIDv7
// lexicographic order, i.e. creation order. Active rows only by default;
// `includeArchived` widens it to every row.
func (r *SessionRegistry) ListSessions(ctx context.Context, includeArchived bool) ([]Session, error) {
	query := `SELECT id, label, created_at, archived_at FROM sessions`
	if !includeArchived {
		query += ` WHERE archived_at IS NULL`
	}
	query += ` ORDER BY id ASC`

	rows, err := r.db.QueryContext(ctx, query)
	if err != nil {
		return nil, fmt.Errorf("channels: list sessions: %w", err)
	}
	defer func() { _ = rows.Close() }()

	out := make([]Session, 0)
	for rows.Next() {
		s, err := scanSession(rows)
		if err != nil {
			return nil, err
		}
		out = append(out, s)
	}
	return out, rows.Err()
}

// GetSession resolves a session by id *or* label and returns the row, or
// [ErrSessionNotFound] when neither matches.
//
// The id is tried first: it is the primary key, so the common path is an index
// hit and the unambiguous form. Only on a miss does it fall back to a label
// lookup — the label column is unindexed, so keeping it off the id path avoids
// a full-table scan on the registry (which grows one row per auto-minted
// binding). The two-query split also makes the id-wins-over-label precedence
// explicit rather than leaning on an `OR` whose plan SQLite cannot index. The
// label fallback is what lets `session use <label>` / `current` render the
// human name; when duplicate labels exist, the lowest id (earliest-created)
// wins deterministically.
func (r *SessionRegistry) GetSession(ctx context.Context, idOrLabel string) (Session, error) {
	const cols = `SELECT id, label, created_at, archived_at FROM sessions`

	s, err := scanSession(r.db.QueryRowContext(ctx, cols+` WHERE id = ?`, idOrLabel))
	if err == nil {
		return s, nil
	}
	if !errors.Is(err, sql.ErrNoRows) {
		return Session{}, err
	}

	// No id match — fall back to the label, lowest id first for a deterministic
	// resolution when labels collide.
	s, err = scanSession(r.db.QueryRowContext(ctx,
		cols+` WHERE label = ? ORDER BY id ASC LIMIT 1`, idOrLabel))
	if errors.Is(err, sql.ErrNoRows) {
		return Session{}, fmt.Errorf("%w: %s", ErrSessionNotFound, idOrLabel)
	}
	if err != nil {
		return Session{}, err
	}
	return s, nil
}

// ArchiveSession marks the session identified by id-or-label archived by
// stamping `archived_at`. Archive is one-way (RFC 0031 §B): no `unarchive`
// verb exists, and the row (and its tagged memory) is never deleted. Returns
// [ErrSessionNotFound] when no row matches. Re-archiving an already-archived
// session is idempotent — `archived_at` keeps its original stamp.
func (r *SessionRegistry) ArchiveSession(ctx context.Context, idOrLabel string) error {
	s, err := r.GetSession(ctx, idOrLabel)
	if err != nil {
		return err
	}
	if s.Archived {
		// Already archived: idempotent no-op, original stamp preserved.
		return nil
	}
	now := float64(time.Now().UTC().UnixNano()) / float64(time.Second)
	if _, err := r.db.ExecContext(ctx,
		`UPDATE sessions SET archived_at = ? WHERE id = ? AND archived_at IS NULL`,
		now, s.ID); err != nil {
		return fmt.Errorf("channels: archive session: %w", err)
	}
	return nil
}

// rowScanner is the subset of *sql.Row / *sql.Rows scanSession needs, so the
// single-row and multi-row reads share one decode path.
type rowScanner interface {
	Scan(dest ...any) error
}

// scanSession decodes one registry row, translating the nullable label and
// the REAL created_at / archived_at columns into the typed [Session] shape.
func scanSession(sc rowScanner) (Session, error) {
	var (
		s          Session
		label      sql.NullString
		createdAt  float64
		archivedAt sql.NullFloat64
	)
	if err := sc.Scan(&s.ID, &label, &createdAt, &archivedAt); err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			return Session{}, err
		}
		return Session{}, fmt.Errorf("channels: scan session: %w", err)
	}
	if label.Valid {
		s.Label = label.String
	}
	s.CreatedAt = secondsToTime(createdAt)
	s.Archived = archivedAt.Valid
	return s, nil
}

// secondsToTime converts the table's REAL unix-seconds stamp back to a UTC
// [time.Time], preserving the sub-second fraction the mint wrote.
func secondsToTime(sec float64) time.Time {
	return time.Unix(0, int64(sec*float64(time.Second))).UTC()
}
