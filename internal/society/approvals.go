package society

// Approval rows on the society tables (RFC 0029 Phase 3 plan S-3 and S-6;
// RFC 0009 G-9 to G-11; RFC 0028 E-8 and E-11). The table's constraints hold
// the row's shape; the store adds what a constraint cannot: the nonce and the
// creation time are its own, a payload is JSON, and a row moves only from the
// state its caller read, along the queue's lifecycle.

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
	_, err := t.tx.ExecContext(ctx, `INSERT INTO society_approvals
		(id, kind, actor_id, action_hash, nonce, state, expires_at,
		 protection_level, cascade_depth, origin, tie_group, payload, created_at)
		VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?)`,
		a.ID, string(a.Kind), a.ActorID, a.ActionHash, hex.EncodeToString(raw), a.ExpiresAt.Unix(),
		protection, depth, origin, tieGroup, payload, t.now().Unix())
	if err != nil {
		if strings.Contains(err.Error(), "society_approvals.actor_id, society_approvals.action_hash") {
			return Approval{}, fmt.Errorf("%w: actor %q, action %q", ErrOpenApprovalExists, a.ActorID, a.ActionHash)
		}
		return Approval{}, fmt.Errorf("society: open approval %q: %w", a.ID, err)
	}
	return t.Approval(ctx, a.ID)
}

// checkNew refuses a row the store would have to make up or drop part of:
// anything missing that every row needs, a payload that is not JSON, and the
// fields the store or a later move sets.
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

// approvalQuerySQL builds the statement ListApprovals runs for q.
func approvalQuerySQL(q ApprovalQuery) (string, []any, error) {
	if q.State != "" && q.Open {
		return "", nil, fmt.Errorf("%w: a query selects one state or the open ones, not both", ErrInvalid)
	}
	if q.Limit < 0 {
		return "", nil, fmt.Errorf("%w: limit %d", ErrInvalid, q.Limit)
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
	query := `SELECT ` + approvalColumns + ` FROM society_approvals`
	if len(where) > 0 {
		query += ` WHERE ` + strings.Join(where, ` AND `)
	}
	query += ` ORDER BY created_at, id`
	if q.Limit > 0 {
		query += ` LIMIT ?`
		args = append(args, q.Limit)
	}
	return query, args, nil
}

// MoveApproval implements [Ops].
func (t *sqliteTx) MoveApproval(ctx context.Context, id string, m Move) (Approval, error) {
	if !slices.Contains(moves[m.From], m.To) {
		return Approval{}, fmt.Errorf("%w: the queue never moves a row from %s to %s", ErrInvalid, m.From, m.To)
	}
	if (m.DecidedBy == "") != m.DecidedAt.IsZero() {
		return Approval{}, fmt.Errorf("%w: a decision records who and when, both or neither", ErrInvalid)
	}
	var decidedBy any
	if m.DecidedBy != "" {
		decidedBy = m.DecidedBy
	}
	settles := m.To != StateApproved
	res, err := t.tx.ExecContext(ctx, `UPDATE society_approvals
		SET state = ?,
		    payload = CASE WHEN ? THEN NULL ELSE payload END,
		    decided_by = COALESCE(?, decided_by),
		    decided_at = COALESCE(?, decided_at),
		    delivered_at = NULL
		WHERE id = ? AND state = ?`,
		string(m.To), settles, decidedBy, unixOrNull(m.DecidedAt), id, string(m.From))
	if err != nil {
		return Approval{}, fmt.Errorf("society: move approval %q: %w", id, err)
	}
	if n, err := res.RowsAffected(); err != nil {
		return Approval{}, fmt.Errorf("society: move approval %q: %w", id, err)
	} else if n == 0 {
		now, err := t.Approval(ctx, id)
		if err != nil {
			return Approval{}, err
		}
		return Approval{}, &WrongStateError{ID: id, Want: m.From, Got: now.State}
	}
	return t.Approval(ctx, id)
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
