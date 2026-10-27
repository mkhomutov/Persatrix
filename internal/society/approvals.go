package society

// Approval rows on the society tables (RFC 0029 Phase 3 plan S-3 and S-6;
// RFC 0009 G-9 to G-11; RFC 0028 E-8 and E-11). The table's constraints hold
// the row's shape, and the store checks the same shape first, so a caller is
// told ErrInvalid rather than a driver's error. It adds what a constraint
// cannot: the nonce and the creation time are its own, a payload is JSON, a
// row moves only from the state its caller read, along the queue's
// lifecycle, and no row is granted or redeemed past its expiry.

import (
	"context"
	"crypto/rand"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"slices"
	"strings"
	"time"

	"modernc.org/sqlite"
)

// approvalColumns is every column, in the order scanApproval reads them.
const approvalColumns = `id, kind, actor_id, action_hash, nonce, state, expires_at,
	protection_level, cascade_depth, origin, tie_group, payload,
	decided_by, decided_at, delivered_at, created_at`

// openCondition is the open-row index's own condition, repeated as written:
// SQLite uses a partial index only for a query that states it (PR 2 F-16).
const openCondition = `state IN ('pending', 'approved')`

// nonceSize is a nonce's random bytes: unique, not secret.
const nonceSize = 16

// sqliteConstraintUnique is SQLITE_CONSTRAINT_UNIQUE, the extended code of a
// unique index refusing a row (https://www.sqlite.org/rescode.html), defined
// here as internal/channels defines it rather than import the driver's lib.
const sqliteConstraintUnique = 2067

// OpenApproval implements [Ops].
func (t *sqliteTx) OpenApproval(ctx context.Context, a Approval) (Approval, error) {
	if err := checkNew(a); err != nil {
		return Approval{}, err
	}
	raw := make([]byte, nonceSize)
	if _, err := rand.Read(raw); err != nil {
		return Approval{}, fmt.Errorf("society: make nonce: %w", err)
	}
	var protection, origin, payload, tieGroup any
	var depth any
	if a.Held != nil {
		protection, depth, origin = a.Held.ProtectionLevel, a.Held.CascadeDepth, a.Held.Origin
	}
	if a.TieGroup != "" {
		tieGroup = a.TieGroup
	}
	if len(a.Payload) > 0 {
		payload = string(a.Payload)
	}
	opened, err := scanApproval(t.tx.QueryRowContext(ctx, `INSERT INTO society_approvals
		(id, kind, actor_id, action_hash, nonce, state, expires_at,
		 protection_level, cascade_depth, origin, tie_group, payload, created_at)
		VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?)
		RETURNING `+approvalColumns,
		a.ID, string(a.Kind), a.ActorID, a.ActionHash, hex.EncodeToString(raw), a.ExpiresAt.Unix(),
		protection, depth, origin, tieGroup, payload, t.now().Unix()))
	if err != nil {
		if t.openRowTaken(ctx, err, a) {
			return Approval{}, fmt.Errorf("%w: actor %q, action %q", ErrOpenApprovalExists, a.ActorID, a.ActionHash)
		}
		return Approval{}, fmt.Errorf("society: open approval %q: %w", a.ID, err)
	}
	return opened, nil
}

// openRowTaken reports whether err is the open-row index refusing a: a
// unique violation, told by the driver's typed code rather than its text,
// while the actor holds an open row for the action. The lookup tells it from
// the nonce's own unique index; the failed insert leaves the transaction
// open for it.
func (t *sqliteTx) openRowTaken(ctx context.Context, err error, a Approval) bool {
	var sqliteErr *sqlite.Error
	if !errors.As(err, &sqliteErr) || sqliteErr.Code() != sqliteConstraintUnique {
		return false
	}
	var open int
	return t.tx.QueryRowContext(ctx, `SELECT 1 FROM society_approvals
		WHERE actor_id = ? AND action_hash = ? AND `+openCondition, a.ActorID, a.ActionHash).Scan(&open) == nil
}

// checkNew refuses a row the store would have to make up or drop part of:
// anything missing that every row needs, a payload that is not JSON, the
// fields the store or a later move sets, and a shape the table's checks
// refuse, so the caller hears ErrInvalid before the table sees the row.
func checkNew(a Approval) error {
	var missing []string
	for field, v := range map[string]string{"id": a.ID, "actor": a.ActorID, "action hash": a.ActionHash} {
		if v == "" {
			missing = append(missing, field)
		}
	}
	if a.ExpiresAt.IsZero() {
		missing = append(missing, "expiry")
	}
	switch {
	case len(missing) > 0:
		slices.Sort(missing)
		return fmt.Errorf("%w: approval %q has no %s", ErrInvalid, a.ID, strings.Join(missing, ", "))
	case a.Kind != KindAction && a.Kind != KindGraphChange && a.Kind != KindTie:
		return fmt.Errorf("%w: approval %q has unknown kind %q", ErrInvalid, a.ID, a.Kind)
	case len(a.Payload) > 0 && !json.Valid(a.Payload):
		return fmt.Errorf("%w: approval %q payload is not JSON", ErrInvalid, a.ID)
	case a.Nonce != "" || !a.CreatedAt.IsZero():
		return fmt.Errorf("%w: approval %q: the store makes the nonce and the creation time", ErrInvalid, a.ID)
	case a.State != "" && a.State != StatePending:
		return fmt.Errorf("%w: approval %q opens pending, not %s", ErrInvalid, a.ID, a.State)
	case a.DecidedBy != "" || !a.DecidedAt.IsZero() || !a.DeliveredAt.IsZero():
		return fmt.Errorf("%w: approval %q opens undecided and undelivered", ErrInvalid, a.ID)
	case a.Held != nil && a.Kind != KindAction:
		return fmt.Errorf("%w: approval %q: only an action row keeps a held turn", ErrInvalid, a.ID)
	case a.Held != nil && a.Held.CascadeDepth < 0:
		return fmt.Errorf("%w: approval %q has cascade depth %d", ErrInvalid, a.ID, a.Held.CascadeDepth)
	case (a.Kind == KindTie) != (a.TieGroup != ""):
		return fmt.Errorf("%w: approval %q: a tie row, and no other, has a tie group", ErrInvalid, a.ID)
	}
	return nil
}

// Approval implements [Ops].
func (t *sqliteTx) Approval(ctx context.Context, id string) (Approval, error) {
	a, err := scanApproval(t.tx.QueryRowContext(ctx,
		`SELECT `+approvalColumns+` FROM society_approvals WHERE id = ?`, id))
	if errors.Is(err, sql.ErrNoRows) {
		return Approval{}, fmt.Errorf("%w: %q", ErrNotFound, id)
	}
	if err != nil {
		return Approval{}, fmt.Errorf("society: read approval %q: %w", id, err)
	}
	return a, nil
}

// ListApprovals implements [Ops].
func (t *sqliteTx) ListApprovals(ctx context.Context, q ApprovalQuery) ([]Approval, error) {
	query, args, err := approvalQuerySQL(q)
	if err != nil {
		return nil, err
	}
	var out []Approval
	err = t.each(ctx, query, func(rows *sql.Rows) error {
		a, err := scanApproval(rows)
		if err == nil {
			out = append(out, a)
		}
		return err
	}, args...)
	return out, err
}

// approvalQuerySQL builds the statement ListApprovals runs for q. Rows come
// in the order they were opened: by creation time, which is whole seconds,
// then by rowid, which goes up with every row inserted.
func approvalQuerySQL(q ApprovalQuery) (string, []any, error) {
	if q.Limit < 0 {
		return "", nil, fmt.Errorf("%w: limit %d", ErrInvalid, q.Limit)
	}
	where, args, err := approvalWhere(q)
	if err != nil {
		return "", nil, err
	}
	query := `SELECT ` + approvalColumns + ` FROM society_approvals`
	if len(where) > 0 {
		query += ` WHERE ` + strings.Join(where, ` AND `)
	}
	query += ` ORDER BY created_at, rowid`
	if q.Limit > 0 {
		query += ` LIMIT ?`
		args = append(args, q.Limit)
	}
	return query, args, nil
}

// approvalWhere builds the conditions q selects by, each with its argument.
func approvalWhere(q ApprovalQuery) ([]string, []any, error) {
	if q.State != "" && q.Open {
		return nil, nil, fmt.Errorf("%w: a query selects one state or the open ones, not both", ErrInvalid)
	}
	var where []string
	var args []any
	match := func(cond string, arg any) {
		where = append(where, cond)
		args = append(args, arg)
	}
	if q.Open {
		where = append(where, openCondition)
	}
	if q.State != "" {
		match("state = ?", string(q.State))
	}
	if q.ActorID != "" {
		match("actor_id = ?", q.ActorID)
	}
	if q.ActionHash != "" {
		match("action_hash = ?", q.ActionHash)
	}
	if q.TieGroup != "" {
		match("tie_group = ?", q.TieGroup)
	}
	if !q.ExpiredBy.IsZero() {
		match("expires_at <= ?", q.ExpiredBy.Unix())
	}
	return where, args, nil
}

// MoveApproval implements [Ops]. One statement moves the row and returns
// it; only when it moves nothing does a read say why.
func (t *sqliteTx) MoveApproval(ctx context.Context, id string, m Move) (Approval, error) {
	if !slices.Contains(moves[m.From], m.To) {
		return Approval{}, fmt.Errorf("%w: the queue never moves a row from %s to %s", ErrInvalid, m.From, m.To)
	}
	if (m.DecidedBy == "") != m.DecidedAt.IsZero() {
		return Approval{}, fmt.Errorf("%w: a decision records who and when, both or neither", ErrInvalid)
	}
	decision := m.To == StateApproved || m.To == StateDenied
	if decision && m.DecidedBy == "" {
		return Approval{}, fmt.Errorf("%w: a move to %s is a person's decision and names who made it", ErrInvalid, m.To)
	}
	var decidedBy any
	if m.DecidedBy != "" {
		decidedBy = m.DecidedBy
	}
	settles := m.To != StateApproved
	// A decision or a spend needs a live row; the sweep's expiry and a
	// withdrawal end a row whenever they come.
	live := decision || m.To == StateSpent
	now := t.now()
	moved, err := scanApproval(t.tx.QueryRowContext(ctx, `UPDATE society_approvals
		SET state = ?,
		    payload = CASE WHEN ? THEN NULL ELSE payload END,
		    decided_by = COALESCE(?, decided_by),
		    decided_at = COALESCE(?, decided_at),
		    delivered_at = NULL
		WHERE id = ? AND state = ? AND (NOT ? OR expires_at > ?)
		RETURNING `+approvalColumns,
		string(m.To), settles, decidedBy, unixOrNull(m.DecidedAt), id, string(m.From), live, now.Unix()))
	if err == nil {
		return moved, nil
	}
	if !errors.Is(err, sql.ErrNoRows) {
		return Approval{}, fmt.Errorf("society: move approval %q: %w", id, err)
	}
	stored, err := t.Approval(ctx, id)
	if err != nil {
		return Approval{}, err
	}
	if stored.State != m.From {
		return Approval{}, &WrongStateError{ID: id, Want: m.From, Got: stored.State}
	}
	return Approval{}, fmt.Errorf("%w: %q expired at %s", ErrExpired, id, stored.ExpiresAt.Format(time.RFC3339))
}

// MoveApprovals implements [Ops].
func (t *sqliteTx) MoveApprovals(ctx context.Context, q ApprovalQuery, to State) ([]Approval, error) {
	switch {
	case to != StateExpired && to != StateWithdrawn:
		return nil, fmt.Errorf("%w: the queue moves rows to %s one at a time", ErrInvalid, to)
	case q.Limit != 0:
		return nil, fmt.Errorf("%w: a bulk move takes every row it selects, not %d", ErrInvalid, q.Limit)
	case q.State != "" && len(moves[q.State]) == 0:
		return nil, fmt.Errorf("%w: a %s row is settled and moves no further", ErrInvalid, q.State)
	}
	if q.State == "" {
		q.Open = true
	}
	where, args, err := approvalWhere(q)
	if err != nil {
		return nil, err
	}
	var out []Approval
	err = t.each(ctx, `UPDATE society_approvals SET state = ?, payload = NULL, delivered_at = NULL
		WHERE `+strings.Join(where, ` AND `)+` RETURNING `+approvalColumns,
		func(rows *sql.Rows) error {
			a, err := scanApproval(rows)
			if err == nil {
				out = append(out, a)
			}
			return err
		}, append([]any{string(to)}, args...)...)
	return out, err
}

// scanApproval reads one row of approvalColumns.
func scanApproval(row interface{ Scan(...any) error }) (Approval, error) {
	var a Approval
	var kind, state string
	var protection, origin, tieGroup, payload, decidedBy sql.NullString
	var depth, expires, decidedAt, deliveredAt, created sql.NullInt64
	if err := row.Scan(&a.ID, &kind, &a.ActorID, &a.ActionHash, &a.Nonce, &state, &expires,
		&protection, &depth, &origin, &tieGroup, &payload,
		&decidedBy, &decidedAt, &deliveredAt, &created); err != nil {
		return Approval{}, err
	}
	a.Kind, a.State = Kind(kind), State(state)
	if depth.Valid {
		a.Held = &HeldTurn{ProtectionLevel: protection.String, CascadeDepth: int(depth.Int64), Origin: origin.String}
	}
	a.TieGroup, a.DecidedBy = tieGroup.String, decidedBy.String
	if payload.Valid {
		a.Payload = json.RawMessage(payload.String)
	}
	a.ExpiresAt, a.CreatedAt = fromUnix(expires), fromUnix(created)
	a.DecidedAt, a.DeliveredAt = fromUnix(decidedAt), fromUnix(deliveredAt)
	return a, nil
}
