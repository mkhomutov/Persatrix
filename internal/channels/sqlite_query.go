// sqlite_query.go — sqliteStore read/query methods for channels, members, and
// DMs (RFC 0011 §B).
//
// Split from sqlite.go to keep sqlite.go (constructor + write operations)
// under the 500-line review-friendly cap (ISSUE-0008 extraction pattern).
// All methods share the same `sqliteStore` receiver defined in sqlite.go.
package channels

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"time"

	"go.uber.org/zap"
)

// GetChannel implements [ChannelStore.GetChannel].
func (s *sqliteStore) GetChannel(ctx context.Context, id string) (Channel, error) {
	row := s.db.QueryRowContext(ctx,
		`SELECT id, name, channel_type, description, created_at, session_id, classification
		   FROM channels WHERE id = ?`, id)
	var ch Channel
	var typ, classification string
	// SF-4: name is nullable post-v2 — DM/thread rows hold NULL.
	var name sql.NullString
	if err := row.Scan(&ch.ID, &name, &typ, &ch.Description, &ch.CreatedAt, &ch.SessionID, &classification); err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			return Channel{}, fmt.Errorf("%w: %s", ErrChannelNotFound, id)
		}
		return Channel{}, fmt.Errorf("channels: scan channel: %w", err)
	}
	ch.Type = ChannelType(typ)
	// RFC 0037 (v0.3.12 PR 2): scanned verbatim, no normalization — the v11
	// column is NOT NULL DEFAULT 'internal' and every writer stamps through
	// [NormalizeForStamp], so an out-of-lattice value here is store
	// corruption the §A rule-(b)/(c) resolvers own downstream, not this
	// scan's to paper over (PR 1's one-resolver-per-rule discipline).
	ch.Classification = Classification(classification)
	if name.Valid {
		ch.Name = name.String
	}
	return ch, nil
}

// ListChannels implements [ChannelStore.ListChannels].
//
// ISSUE-0015: keyset pagination. ORDER BY id ASC is total (id is the
// PK) so no tiebreaker column is needed and the cursor stays a single
// opaque string the handler can echo back without decoding. LIMIT is
// pushed into SQL so a deployment that grows past the soft cap does
// not load the whole table per request the way the previous
// implementation did. `limit <= 0` keeps the historical "every row"
// shape for non-paginated callers — the handler always passes a
// positive limit (and over-cap silently capped at parse time).
func (s *sqliteStore) ListChannels(ctx context.Context, limit int, afterID string) ([]Channel, error) {
	const baseQuery = `SELECT id, name, channel_type, description, created_at, session_id, classification
		   FROM channels`

	var (
		rows *sql.Rows
		err  error
	)
	switch {
	case limit > 0 && afterID != "":
		rows, err = s.db.QueryContext(ctx,
			baseQuery+` WHERE id > ? ORDER BY id ASC LIMIT ?`,
			afterID, limit)
	case limit > 0:
		rows, err = s.db.QueryContext(ctx,
			baseQuery+` ORDER BY id ASC LIMIT ?`, limit)
	case afterID != "":
		rows, err = s.db.QueryContext(ctx,
			baseQuery+` WHERE id > ? ORDER BY id ASC`, afterID)
	default:
		rows, err = s.db.QueryContext(ctx,
			baseQuery+` ORDER BY id ASC`)
	}
	if err != nil {
		return nil, fmt.Errorf("channels: list: %w", err)
	}
	defer func() { _ = rows.Close() }()

	out := make([]Channel, 0)
	for rows.Next() {
		var ch Channel
		var typ, classification string
		var name sql.NullString
		if err := rows.Scan(&ch.ID, &name, &typ, &ch.Description, &ch.CreatedAt, &ch.SessionID, &classification); err != nil {
			return nil, fmt.Errorf("channels: scan list: %w", err)
		}
		ch.Type = ChannelType(typ)
		// Verbatim scan — see the GetChannel note (RFC 0037 v0.3.12 PR 2).
		ch.Classification = Classification(classification)
		if name.Valid {
			ch.Name = name.String
		}
		out = append(out, ch)
	}
	return out, rows.Err()
}

// DeleteChannel implements [ChannelStore.DeleteChannel].
func (s *sqliteStore) DeleteChannel(ctx context.Context, id string) error {
	res, err := s.db.ExecContext(ctx, `DELETE FROM channels WHERE id = ?`, id)
	if err != nil {
		return fmt.Errorf("channels: delete: %w", err)
	}
	n, err := res.RowsAffected()
	if err != nil {
		return fmt.Errorf("channels: delete rowsaffected: %w", err)
	}
	if n == 0 {
		return fmt.Errorf("%w: %s", ErrChannelNotFound, id)
	}
	s.logger.Info("channels: channel deleted", zap.String("channel_id", id))
	return nil
}

// RemoveMember implements [ChannelStore.RemoveMember].
//
// Disambiguates the two 404 causes (channel-not-found vs
// membership-not-found) so REST callers see the right cause string,
// while preserving the participant's prior messages per RFC 0011 §C
// (`messages.sender_id` carries the historical value after removal).
//
// The function runs in a single transaction with a DELETE-then-check
// ordering. The DELETE upgrades the deferred tx to a writer lock,
// which serializes the subsequent existence check against any
// concurrent `DeleteChannel`. The earlier
// `GetChannel`-then-`DELETE` shape left a TOCTOU window where a
// concurrent channel deletion would surface as `ErrNotMember`
// ("membership not found") instead of the more accurate
// `ErrChannelNotFound` — both surface as 404 to REST, but the error
// string is what an operator reads first when triaging. PR #252
// review N-1.
func (s *sqliteStore) RemoveMember(ctx context.Context, channelID, participantID string) error {
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return fmt.Errorf("channels: remove member begin tx: %w", err)
	}
	defer func() { _ = tx.Rollback() }()

	res, err := tx.ExecContext(ctx,
		`DELETE FROM memberships WHERE channel_id = ? AND participant_id = ?`,
		channelID, participantID,
	)
	if err != nil {
		return fmt.Errorf("channels: remove member: %w", err)
	}
	n, err := res.RowsAffected()
	if err != nil {
		return fmt.Errorf("channels: remove member rowsaffected: %w", err)
	}

	if n == 0 {
		// Disambiguate inside the same tx so the writer lock acquired
		// by the DELETE blocks any concurrent DeleteChannel from
		// racing the existence check.
		var present int
		err := tx.QueryRowContext(ctx,
			`SELECT 1 FROM channels WHERE id = ?`, channelID,
		).Scan(&present)
		if errors.Is(err, sql.ErrNoRows) {
			return fmt.Errorf("%w: %s", ErrChannelNotFound, channelID)
		}
		if err != nil {
			return fmt.Errorf("channels: remove member existence check: %w", err)
		}
		return fmt.Errorf("%w: channel=%s participant=%s",
			ErrNotMember, channelID, participantID)
	}

	// RFC 0035 §C: the `memberships` row was deleted (success path), so close
	// the participant's OPEN interval in the same tx. The §D backfill and the
	// AddMember / GetOrCreateDM / CreateChannelWithMembers open hooks guarantee a
	// present member has exactly one open interval, so this closes exactly one
	// row. A ZERO-row close means a `memberships` row existed with no matching
	// open interval — the projection and the ledger diverged (Goal 6). Roll back
	// loudly rather than commit a never-closing interval (an RFC 0036 exposure
	// bug); the deferred Rollback fires on the early return.
	closed, err := closeOpenMembershipInterval(ctx, tx, channelID, participantID, time.Now().UTC())
	if err != nil {
		return err
	}
	if closed == 0 {
		// Log at the store, not only at the REST boundary's default-case
		// handler: internal callers (config reconcile, future non-REST callers)
		// never pass through writeChannelError, and a silent never-closing
		// interval is an RFC 0036 data-exposure bug — the breach must leave an
		// operator breadcrumb regardless of who called RemoveMember.
		s.logger.Error("channels: membership ledger divergence",
			zap.String("channel_id", channelID),
			zap.String("participant_id", participantID),
		)
		return fmt.Errorf("%w: channel=%s participant=%s",
			errMembershipLedgerDivergence, channelID, participantID)
	}

	if err := tx.Commit(); err != nil {
		return fmt.Errorf("channels: remove member commit: %w", err)
	}
	s.logger.Info("channels: member removed",
		zap.String("channel_id", channelID),
		zap.String("participant_id", participantID),
	)
	return nil
}

// SetMemberPolicy implements [ChannelStore.SetMemberPolicy].
//
// Uses the same DELETE-style "act first, disambiguate inside the same tx"
// shape as [RemoveMember] so a concurrent `DeleteChannel` cannot race the
// existence check between zero-rows-affected and the channel lookup.
func (s *sqliteStore) SetMemberPolicy(ctx context.Context, channelID, participantID string, policy RespondPolicy) error {
	// Resolve the declared disposition into the persisted triple
	// ([ResolveMemberPolicy]): the store is the second back-compat
	// boundary (mirroring the config loader) so the REST write path and
	// the membership-table CHECK constraint see only legacy values, and
	// re-resolving on every policy change keeps the salience-bid signals
	// in lock-step with `respond_policy` (e.g. `addressed` →
	// `participant` turns the bid on and resets the chair threshold) —
	// otherwise a re-disposition would leave a stale `salience_gated`.
	mp, err := ResolveMemberPolicy(policy)
	if err != nil {
		return err
	}
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return fmt.Errorf("channels: set member policy begin tx: %w", err)
	}
	defer func() { _ = tx.Rollback() }()

	res, err := tx.ExecContext(ctx,
		`UPDATE memberships SET respond_policy = ?, threshold = ?, salience_gated = ?
		   WHERE channel_id = ? AND participant_id = ?`,
		string(mp.Policy), mp.Threshold, boolToInt(mp.SalienceGated), channelID, participantID,
	)
	if err != nil {
		return fmt.Errorf("channels: set member policy: %w", err)
	}
	n, err := res.RowsAffected()
	if err != nil {
		return fmt.Errorf("channels: set member policy rowsaffected: %w", err)
	}
	if n == 0 {
		var present int
		err := tx.QueryRowContext(ctx,
			`SELECT 1 FROM channels WHERE id = ?`, channelID,
		).Scan(&present)
		if errors.Is(err, sql.ErrNoRows) {
			return fmt.Errorf("%w: %s", ErrChannelNotFound, channelID)
		}
		if err != nil {
			return fmt.Errorf("channels: set member policy existence check: %w", err)
		}
		return fmt.Errorf("%w: channel=%s participant=%s",
			ErrNotMember, channelID, participantID)
	}
	if err := tx.Commit(); err != nil {
		return fmt.Errorf("channels: set member policy commit: %w", err)
	}
	return nil
}

// UpdateMemberConfig implements [ChannelStore.UpdateMemberConfig].
func (s *sqliteStore) UpdateMemberConfig(ctx context.Context, channelID, participantID string, policy RespondPolicy, threshold *float64) error {
	// Resolve the declared disposition + explicit threshold into the persisted
	// triple, enforcing the same threshold rules config load applies
	// ([ResolveMemberPolicyWithThreshold]) so the REST member-config PATCH cannot
	// persist a pair the YAML path would reject. Re-resolving keeps the
	// salience-bid signals in lock-step with respond_policy, exactly like
	// SetMemberPolicy.
	mp, err := ResolveMemberPolicyWithThreshold(policy, threshold)
	if err != nil {
		return err
	}
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return fmt.Errorf("channels: update member config begin tx: %w", err)
	}
	defer func() { _ = tx.Rollback() }()

	res, err := tx.ExecContext(ctx,
		`UPDATE memberships SET respond_policy = ?, threshold = ?, salience_gated = ?
		   WHERE channel_id = ? AND participant_id = ?`,
		string(mp.Policy), mp.Threshold, boolToInt(mp.SalienceGated), channelID, participantID,
	)
	if err != nil {
		return fmt.Errorf("channels: update member config: %w", err)
	}
	n, err := res.RowsAffected()
	if err != nil {
		return fmt.Errorf("channels: update member config rowsaffected: %w", err)
	}
	if n == 0 {
		// Disambiguate channel-missing (404 channel) from member-missing (404
		// member) for operator triage, mirroring SetMemberPolicy.
		var present int
		err := tx.QueryRowContext(ctx, `SELECT 1 FROM channels WHERE id = ?`, channelID).Scan(&present)
		if errors.Is(err, sql.ErrNoRows) {
			return fmt.Errorf("%w: %s", ErrChannelNotFound, channelID)
		}
		if err != nil {
			return fmt.Errorf("channels: update member config existence check: %w", err)
		}
		return fmt.Errorf("%w: channel=%s participant=%s", ErrMemberNotFound, channelID, participantID)
	}
	if err := tx.Commit(); err != nil {
		return fmt.Errorf("channels: update member config commit: %w", err)
	}
	return nil
}

// AddMember implements [ChannelStore.AddMember].
//
// RFC 0035 §C: AddMember is now transactional. It opens a membership interval
// in the same tx as the `memberships` insert, but ONLY when it actually
// inserted a row (RowsAffected == 1 — a genuine new join or a post-removal
// re-add). A redundant add on a present member fires the `ON CONFLICT` no-op
// (RowsAffected == 0) and opens no second interval: the existing stint's open
// interval is still correct, and `ux_membership_intervals_open` would reject a
// duplicate anyway. Idempotency is preserved on both tables, and the
// foreign-key → [ErrChannelNotFound] mapping is unchanged, now inside the tx.
func (s *sqliteStore) AddMember(ctx context.Context, channelID, participantID string, policy RespondPolicy) error {
	if err := ValidateParticipantID(participantID); err != nil {
		return err
	}
	// Resolve the declared disposition into the persisted triple
	// ([ResolveMemberPolicy]). This is the REST single-add path, which
	// (unlike the config reconcile path) still carries the disposition, so
	// resolving here is correct: `participant`/`chair` opt into the bid, a
	// `chair` picks up the low default threshold, and the CHECK constraint
	// sees only the legacy values.
	mp, err := ResolveMemberPolicy(policy)
	if err != nil {
		return err
	}

	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return fmt.Errorf("channels: add member begin tx: %w", err)
	}
	defer func() { _ = tx.Rollback() }()

	// One `now` for both the projection and the ledger so they agree (§C).
	now := time.Now().UTC()
	// Idempotent re-add: keep the existing joined_at and respond_policy.
	res, err := tx.ExecContext(ctx,
		`INSERT INTO memberships (channel_id, participant_id, respond_policy, joined_at, threshold, salience_gated)
		 VALUES (?, ?, ?, ?, ?, ?)
		 ON CONFLICT(channel_id, participant_id) DO NOTHING`,
		channelID, participantID, string(mp.Policy), now, mp.Threshold, boolToInt(mp.SalienceGated),
	)
	if err != nil {
		if isForeignKeyViolation(err) {
			return fmt.Errorf("%w: %s", ErrChannelNotFound, channelID)
		}
		return fmt.Errorf("channels: add member: %w", err)
	}
	n, err := res.RowsAffected()
	if err != nil {
		return fmt.Errorf("channels: add member rowsaffected: %w", err)
	}
	if n == 1 {
		if err := openMembershipInterval(ctx, tx, channelID, participantID, now); err != nil {
			return err
		}
	}
	if err := tx.Commit(); err != nil {
		return fmt.Errorf("channels: add member commit: %w", err)
	}
	return nil
}

// GetMembers implements [ChannelStore.GetMembers].
func (s *sqliteStore) GetMembers(ctx context.Context, channelID string) ([]Member, error) {
	rows, err := s.db.QueryContext(ctx,
		`SELECT participant_id, respond_policy, joined_at, threshold, salience_gated
		   FROM memberships
		  WHERE channel_id = ?
		  ORDER BY joined_at ASC, participant_id ASC`, channelID)
	if err != nil {
		return nil, fmt.Errorf("channels: get members: %w", err)
	}
	defer func() { _ = rows.Close() }()

	out := make([]Member, 0)
	for rows.Next() {
		var m Member
		var policy string
		var threshold sql.NullFloat64
		var salienceGated int
		if err := rows.Scan(&m.ParticipantID, &policy, &m.JoinedAt, &threshold, &salienceGated); err != nil {
			return nil, fmt.Errorf("channels: scan member: %w", err)
		}
		m.RespondPolicy = RespondPolicy(policy)
		m.Threshold, m.SalienceGated = readSalienceColumns(threshold, salienceGated)
		out = append(out, m)
	}
	return out, rows.Err()
}

// readSalienceColumns maps the raw `memberships` Tier B columns to the typed
// [Member] fields: a NULL `threshold` → nil (*float64) → unset; the 0/1
// `salience_gated` INTEGER → bool. Shared by [GetMembers]/[GetMember] so the
// nullable-decode lives in one place.
func readSalienceColumns(threshold sql.NullFloat64, salienceGated int) (*float64, bool) {
	var t *float64
	if threshold.Valid {
		v := threshold.Float64
		t = &v
	}
	return t, salienceGated != 0
}

// GetMember implements [ChannelStore.GetMember].
func (s *sqliteStore) GetMember(ctx context.Context, channelID, participantID string) (Member, error) {
	row := s.db.QueryRowContext(ctx,
		`SELECT participant_id, respond_policy, joined_at, threshold, salience_gated
		   FROM memberships WHERE channel_id = ? AND participant_id = ?`,
		channelID, participantID)
	var m Member
	var policy string
	var threshold sql.NullFloat64
	var salienceGated int
	if err := row.Scan(&m.ParticipantID, &policy, &m.JoinedAt, &threshold, &salienceGated); err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			return Member{}, fmt.Errorf("%w: channel=%s participant=%s",
				ErrNotMember, channelID, participantID)
		}
		return Member{}, fmt.Errorf("channels: get member: %w", err)
	}
	m.RespondPolicy = RespondPolicy(policy)
	m.Threshold, m.SalienceGated = readSalienceColumns(threshold, salienceGated)
	return m, nil
}

// IsMember implements [ChannelStore.IsMember].
func (s *sqliteStore) IsMember(ctx context.Context, channelID, participantID string) (bool, error) {
	var exists int
	err := s.db.QueryRowContext(ctx,
		`SELECT 1 FROM memberships WHERE channel_id = ? AND participant_id = ?`,
		channelID, participantID).Scan(&exists)
	if errors.Is(err, sql.ErrNoRows) {
		return false, nil
	}
	if err != nil {
		return false, fmt.Errorf("channels: is member: %w", err)
	}
	return true, nil
}

// DM resolution (LookupDM, GetOrCreateDM) lives in sqlite_dm.go to keep this
// file under the 500-line cap.
