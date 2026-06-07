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
		`SELECT id, name, channel_type, description, created_at, session_id
		   FROM channels WHERE id = ?`, id)
	var ch Channel
	var typ string
	// SF-4: name is nullable post-v2 — DM/thread rows hold NULL.
	var name sql.NullString
	if err := row.Scan(&ch.ID, &name, &typ, &ch.Description, &ch.CreatedAt, &ch.SessionID); err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			return Channel{}, fmt.Errorf("%w: %s", ErrChannelNotFound, id)
		}
		return Channel{}, fmt.Errorf("channels: scan channel: %w", err)
	}
	ch.Type = ChannelType(typ)
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
	const baseQuery = `SELECT id, name, channel_type, description, created_at, session_id
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
		var typ string
		var name sql.NullString
		if err := rows.Scan(&ch.ID, &name, &typ, &ch.Description, &ch.CreatedAt, &ch.SessionID); err != nil {
			return nil, fmt.Errorf("channels: scan list: %w", err)
		}
		ch.Type = ChannelType(typ)
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
	// Normalize the RFC 0030 disposition vocabulary to the legacy triple
	// before validating/persisting: the store is the second back-compat
	// boundary (mirroring the config loader) so the REST write path and
	// the membership-table CHECK constraint see only legacy values. An
	// unknown value is returned unchanged by Normalize and rejected here
	// (see [canonicalRespondPolicy]).
	//
	// RFC 0030 Tier B (v0.3.8): re-derive the salience-bid signals from the
	// raw disposition before normalizing, so changing a member's disposition
	// through this path (e.g. `addressed` → `participant`) turns the bid on or
	// off and resets the chair threshold in lock-step with `respond_policy` —
	// otherwise a re-disposition would leave a stale `tier_b_active`.
	tierBActive, threshold := ResolveTierBSignal(policy, nil)
	policy, err := canonicalRespondPolicy(policy)
	if err != nil {
		return err
	}
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return fmt.Errorf("channels: set member policy begin tx: %w", err)
	}
	defer func() { _ = tx.Rollback() }()

	res, err := tx.ExecContext(ctx,
		`UPDATE memberships SET respond_policy = ?, threshold = ?, tier_b_active = ?
		   WHERE channel_id = ? AND participant_id = ?`,
		string(policy), threshold, boolToInt(tierBActive), channelID, participantID,
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

// AddMember implements [ChannelStore.AddMember].
func (s *sqliteStore) AddMember(ctx context.Context, channelID, participantID string, policy RespondPolicy) error {
	if err := validateParticipantID(participantID); err != nil {
		return err
	}
	// RFC 0030 Tier B (v0.3.8): derive the per-member salience-bid signals from
	// the *raw* disposition before it is normalized — `participant`/`chair`
	// opt into the bid, a `chair` picks up the low default threshold. This is
	// the REST single-add path, which (unlike the config reconcile path) still
	// carries the disposition, so deriving here is correct; see
	// [ResolveTierBSignal].
	tierBActive, threshold := ResolveTierBSignal(policy, nil)
	// Normalize the disposition vocabulary to the legacy triple before
	// validating/persisting (see [canonicalRespondPolicy]). Keeps the REST
	// write path and the membership CHECK constraint on the legacy values.
	policy, err := canonicalRespondPolicy(policy)
	if err != nil {
		return err
	}
	// Idempotent re-add: keep the existing joined_at and respond_policy.
	_, err = s.db.ExecContext(ctx,
		`INSERT INTO memberships (channel_id, participant_id, respond_policy, joined_at, threshold, tier_b_active)
		 VALUES (?, ?, ?, ?, ?, ?)
		 ON CONFLICT(channel_id, participant_id) DO NOTHING`,
		channelID, participantID, string(policy), time.Now().UTC(), threshold, boolToInt(tierBActive),
	)
	if err != nil {
		if isForeignKeyViolation(err) {
			return fmt.Errorf("%w: %s", ErrChannelNotFound, channelID)
		}
		return fmt.Errorf("channels: add member: %w", err)
	}
	return nil
}

// GetMembers implements [ChannelStore.GetMembers].
func (s *sqliteStore) GetMembers(ctx context.Context, channelID string) ([]Member, error) {
	rows, err := s.db.QueryContext(ctx,
		`SELECT participant_id, respond_policy, joined_at, threshold, tier_b_active
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
		var tierB int
		if err := rows.Scan(&m.ParticipantID, &policy, &m.JoinedAt, &threshold, &tierB); err != nil {
			return nil, fmt.Errorf("channels: scan member: %w", err)
		}
		m.RespondPolicy = RespondPolicy(policy)
		m.Threshold, m.TierBActive = readTierBColumns(threshold, tierB)
		out = append(out, m)
	}
	return out, rows.Err()
}

// readTierBColumns maps the raw `memberships` Tier B columns to the typed
// [Member] fields: a NULL `threshold` → nil (*float64) → unset; the 0/1
// `tier_b_active` INTEGER → bool. Shared by [GetMembers]/[GetMember] so the
// nullable-decode lives in one place.
func readTierBColumns(threshold sql.NullFloat64, tierB int) (*float64, bool) {
	var t *float64
	if threshold.Valid {
		v := threshold.Float64
		t = &v
	}
	return t, tierB != 0
}

// GetMember implements [ChannelStore.GetMember].
func (s *sqliteStore) GetMember(ctx context.Context, channelID, participantID string) (Member, error) {
	row := s.db.QueryRowContext(ctx,
		`SELECT participant_id, respond_policy, joined_at, threshold, tier_b_active
		   FROM memberships WHERE channel_id = ? AND participant_id = ?`,
		channelID, participantID)
	var m Member
	var policy string
	var threshold sql.NullFloat64
	var tierB int
	if err := row.Scan(&m.ParticipantID, &policy, &m.JoinedAt, &threshold, &tierB); err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			return Member{}, fmt.Errorf("%w: channel=%s participant=%s",
				ErrNotMember, channelID, participantID)
		}
		return Member{}, fmt.Errorf("channels: get member: %w", err)
	}
	m.RespondPolicy = RespondPolicy(policy)
	m.Threshold, m.TierBActive = readTierBColumns(threshold, tierB)
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

// LookupDM implements [ChannelStore.LookupDM]: a read-only resolve of the
// canonical DM between `a` and `b`. It derives the canonical id (which validates
// the pair and is the same access boundary GetOrCreateDM uses) and returns the
// existing channel, or [ErrChannelNotFound] when the DM has never been created.
// No mutation, no membership insert — the fresh-start case is a clean not-found,
// not a side-effecting create.
func (s *sqliteStore) LookupDM(ctx context.Context, a, b string) (Channel, error) {
	id, err := CanonicalDMID(a, b)
	if err != nil {
		return Channel{}, err
	}
	return s.GetChannel(ctx, id)
}

// GetOrCreateDM implements [ChannelStore.GetOrCreateDM].
func (s *sqliteStore) GetOrCreateDM(ctx context.Context, a, b string) (Channel, error) {
	id, err := CanonicalDMID(a, b)
	if err != nil {
		return Channel{}, err
	}

	s.dmMu.Lock()
	defer s.dmMu.Unlock()

	ch, err := s.GetChannel(ctx, id)
	if err == nil {
		return ch, nil
	}
	if !errors.Is(err, ErrChannelNotFound) {
		return Channel{}, err
	}

	// Lexicographically sort once to mirror CanonicalDMID's ordering for the
	// membership inserts.
	pa, pb := a, b
	if pa > pb {
		pa, pb = pb, pa
	}

	now := time.Now().UTC()
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return Channel{}, err
	}
	defer func() { _ = tx.Rollback() }()

	// RFC 0031 Phase 1: DM rows created implicitly carry the legacy
	// carve-out. Operators can promote a DM into a named session via
	// Phase 3 CLI's `persatrix session use <id>` after the fact.
	if _, err := tx.ExecContext(ctx,
		`INSERT INTO channels (id, name, channel_type, description, created_at, session_id)
		 VALUES (?, NULL, ?, '', ?, ?)`,
		id, string(ChannelTypeDM), now, DefaultSessionID); err != nil {
		return Channel{}, fmt.Errorf("channels: create dm: %w", err)
	}
	for _, p := range []string{pa, pb} {
		if _, err := tx.ExecContext(ctx,
			`INSERT INTO memberships (channel_id, participant_id, respond_policy, joined_at)
			 VALUES (?, ?, 'always', ?)`,
			id, p, now); err != nil {
			return Channel{}, fmt.Errorf("channels: add dm member %s: %w", p, err)
		}
	}
	if err := tx.Commit(); err != nil {
		return Channel{}, err
	}
	s.recordSessionWrite(ctx, DefaultSessionID)

	return Channel{
		ID:        id,
		Type:      ChannelTypeDM,
		CreatedAt: now,
		SessionID: DefaultSessionID,
	}, nil
}
