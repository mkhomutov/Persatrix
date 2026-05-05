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
		`SELECT id, name, channel_type, description, created_at
		   FROM channels WHERE id = ?`, id)
	var ch Channel
	var typ string
	// SF-4: name is nullable post-v2 — DM/thread rows hold NULL.
	var name sql.NullString
	if err := row.Scan(&ch.ID, &name, &typ, &ch.Description, &ch.CreatedAt); err != nil {
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
func (s *sqliteStore) ListChannels(ctx context.Context) ([]Channel, error) {
	rows, err := s.db.QueryContext(ctx,
		`SELECT id, name, channel_type, description, created_at
		   FROM channels ORDER BY created_at ASC`)
	if err != nil {
		return nil, fmt.Errorf("channels: list: %w", err)
	}
	defer func() { _ = rows.Close() }()

	out := make([]Channel, 0)
	for rows.Next() {
		var ch Channel
		var typ string
		var name sql.NullString
		if err := rows.Scan(&ch.ID, &name, &typ, &ch.Description, &ch.CreatedAt); err != nil {
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
// First verifies the channel exists so callers can distinguish a
// missing channel from a missing membership (404 vs 404 with the
// REST handler's message reflecting the actual cause). Then removes
// the membership row; the participant's prior messages are preserved
// per RFC 0011 §C — `messages.sender_id` carries the historical value
// after removal.
func (s *sqliteStore) RemoveMember(ctx context.Context, channelID, participantID string) error {
	if _, err := s.GetChannel(ctx, channelID); err != nil {
		return err
	}
	res, err := s.db.ExecContext(ctx,
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
		return fmt.Errorf("%w: channel=%s participant=%s",
			ErrNotMember, channelID, participantID)
	}
	s.logger.Info("channels: member removed",
		zap.String("channel_id", channelID),
		zap.String("participant_id", participantID),
	)
	return nil
}

// AddMember implements [ChannelStore.AddMember].
func (s *sqliteStore) AddMember(ctx context.Context, channelID, participantID string, policy RespondPolicy) error {
	if err := validateParticipantID(participantID); err != nil {
		return err
	}
	if !policy.Valid() {
		return fmt.Errorf("%w: %q", ErrInvalidRespondPolicy, policy)
	}
	// Idempotent re-add: keep the existing joined_at and respond_policy.
	_, err := s.db.ExecContext(ctx,
		`INSERT INTO memberships (channel_id, participant_id, respond_policy, joined_at)
		 VALUES (?, ?, ?, ?)
		 ON CONFLICT(channel_id, participant_id) DO NOTHING`,
		channelID, participantID, string(policy), time.Now().UTC(),
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
		`SELECT participant_id, respond_policy, joined_at
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
		if err := rows.Scan(&m.ParticipantID, &policy, &m.JoinedAt); err != nil {
			return nil, fmt.Errorf("channels: scan member: %w", err)
		}
		m.RespondPolicy = RespondPolicy(policy)
		out = append(out, m)
	}
	return out, rows.Err()
}

// GetMember implements [ChannelStore.GetMember].
func (s *sqliteStore) GetMember(ctx context.Context, channelID, participantID string) (Member, error) {
	row := s.db.QueryRowContext(ctx,
		`SELECT participant_id, respond_policy, joined_at
		   FROM memberships WHERE channel_id = ? AND participant_id = ?`,
		channelID, participantID)
	var m Member
	var policy string
	if err := row.Scan(&m.ParticipantID, &policy, &m.JoinedAt); err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			return Member{}, fmt.Errorf("%w: channel=%s participant=%s",
				ErrNotMember, channelID, participantID)
		}
		return Member{}, fmt.Errorf("channels: get member: %w", err)
	}
	m.RespondPolicy = RespondPolicy(policy)
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

	if _, err := tx.ExecContext(ctx,
		`INSERT INTO channels (id, name, channel_type, description, created_at)
		 VALUES (?, NULL, ?, '', ?)`,
		id, string(ChannelTypeDM), now); err != nil {
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

	return Channel{
		ID:        id,
		Type:      ChannelTypeDM,
		CreatedAt: now,
	}, nil
}
