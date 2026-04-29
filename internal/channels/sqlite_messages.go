package channels

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"time"
)

// PublishMessage implements [ChannelStore.PublishMessage].
//
// The transaction holds:
//  1. membership check (sender must be in the channel) — ErrNotMember on miss
//  2. INSERT against `messages`
//  3. cap enforcement: if the post-insert row count exceeds
//     `maxMessagesPerChannel`, delete the oldest excess rows (`thread_id`
//     cascade prunes their replies in the same transaction)
//
// The channel-existence check is implicit in step 1 — a missing channel has
// no membership rows, so the same `ErrNotMember` surfaces. PublishMessage
// distinguishes the two by re-checking the channel row when the membership
// query returns false.
func (s *sqliteStore) PublishMessage(ctx context.Context, msg ChannelMessage) error {
	if msg.ID == "" {
		return errors.New("channels: message id is required")
	}
	if msg.ChannelID == "" {
		return errors.New("channels: message channel_id is required")
	}
	if err := validateParticipantID(msg.SenderID); err != nil {
		return err
	}
	if msg.Timestamp.IsZero() {
		msg.Timestamp = time.Now().UTC()
	}
	mentions, err := encodeMentions(msg.Mentions)
	if err != nil {
		return err
	}
	metadata, err := encodeMetadata(msg.Metadata)
	if err != nil {
		return err
	}
	threadID := sql.NullString{String: msg.ThreadID, Valid: msg.ThreadID != ""}

	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	defer func() { _ = tx.Rollback() }()

	// Membership check (also catches missing-channel implicitly).
	var ok int
	err = tx.QueryRowContext(ctx,
		`SELECT 1 FROM memberships WHERE channel_id = ? AND participant_id = ?`,
		msg.ChannelID, msg.SenderID).Scan(&ok)
	if errors.Is(err, sql.ErrNoRows) {
		// Disambiguate "channel missing" vs. "sender not a member".
		var chCount int
		if err := tx.QueryRowContext(ctx,
			`SELECT COUNT(1) FROM channels WHERE id = ?`, msg.ChannelID).Scan(&chCount); err != nil {
			return fmt.Errorf("channels: lookup channel: %w", err)
		}
		if chCount == 0 {
			return fmt.Errorf("%w: %s", ErrChannelNotFound, msg.ChannelID)
		}
		return fmt.Errorf("%w: channel=%s sender=%s",
			ErrNotMember, msg.ChannelID, msg.SenderID)
	}
	if err != nil {
		return fmt.Errorf("channels: membership probe: %w", err)
	}

	if _, err := tx.ExecContext(ctx,
		`INSERT INTO messages (id, channel_id, sender_id, content, timestamp, thread_id, mentions, metadata)
		 VALUES (?, ?, ?, ?, ?, ?, ?, ?)`,
		msg.ID, msg.ChannelID, msg.SenderID, msg.Content, msg.Timestamp,
		threadID, mentions, metadata); err != nil {
		if isUniqueViolation(err) {
			return fmt.Errorf("channels: duplicate message id %s", msg.ID)
		}
		if isForeignKeyViolation(err) {
			// The INSERT touches two FK columns: `channel_id` and (when set)
			// `thread_id`. modernc.org/sqlite reports both as the same
			// generic FOREIGN KEY constraint failure, so we disambiguate by
			// what the caller supplied. When `ThreadID` is empty, the only
			// possible FK target is `channel_id` — the channel was deleted
			// between the membership probe and this INSERT (memberships
			// cascade-delete with the channel, so the probe could still have
			// read its own snapshot). Surface that as ErrChannelNotFound
			// instead of the misleading "invalid thread_id <empty>" reported
			// pre-PR-#231-review.
			if msg.ThreadID == "" {
				return fmt.Errorf("%w: %s (deleted concurrently)",
					ErrChannelNotFound, msg.ChannelID)
			}
			return fmt.Errorf("channels: invalid thread_id %s: %w", msg.ThreadID, err)
		}
		return fmt.Errorf("channels: insert message: %w", err)
	}

	if err := s.pruneExcess(ctx, tx, msg.ChannelID); err != nil {
		return err
	}

	return tx.Commit()
}

// pruneExcess deletes the oldest rows from `channelID` until the row count
// is at or under the configured cap. The `thread_id` self-cascade prunes
// reply chains rooted in any deleted message during the same statement.
//
// Pruning runs on every publish: if the cap shrinks at runtime (it cannot
// today, but a future config-reload feature wants this behavior), the next
// publish brings the channel back into bounds. The expected hot-path cost
// is one COUNT and zero DELETEs in steady state.
func (s *sqliteStore) pruneExcess(ctx context.Context, tx *sql.Tx, channelID string) error {
	var count int
	if err := tx.QueryRowContext(ctx,
		`SELECT COUNT(1) FROM messages WHERE channel_id = ?`, channelID).Scan(&count); err != nil {
		return fmt.Errorf("channels: count messages: %w", err)
	}
	excess := count - s.maxMessagesPerChannel
	if excess <= 0 {
		return nil
	}
	// Delete the `excess` oldest messages; cascade handles thread replies.
	// Using a subquery against the `(channel_id, timestamp DESC)` index lets
	// SQLite walk the tail in order.
	if _, err := tx.ExecContext(ctx,
		`DELETE FROM messages
		   WHERE id IN (
		     SELECT id FROM messages
		      WHERE channel_id = ?
		      ORDER BY timestamp ASC
		      LIMIT ?
		   )`, channelID, excess); err != nil {
		return fmt.Errorf("channels: prune oldest %d: %w", excess, err)
	}
	return nil
}

// GetMessage implements [ChannelStore.GetMessage].
func (s *sqliteStore) GetMessage(ctx context.Context, messageID string) (ChannelMessage, error) {
	row := s.db.QueryRowContext(ctx,
		`SELECT id, channel_id, sender_id, content, timestamp, thread_id, mentions, metadata
		   FROM messages WHERE id = ?`, messageID)
	msg, err := scanMessage(row)
	if err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			return ChannelMessage{}, fmt.Errorf("%w: %s", ErrMessageNotFound, messageID)
		}
		return ChannelMessage{}, err
	}
	return msg, nil
}

// GetHistory implements [ChannelStore.GetHistory].
func (s *sqliteStore) GetHistory(ctx context.Context, channelID string, limit int, before time.Time) ([]ChannelMessage, error) {
	if limit <= 0 {
		limit = 50
	}
	// Branch on `before.IsZero()` rather than substituting a future-dated
	// sentinel: a synthetic "now+1h" upper bound would skew if the system
	// clock jumped backwards mid-pagination and is harder to read at the
	// SQL site. Two query strings keep the predicate honest.
	var (
		rows *sql.Rows
		qErr error
	)
	if before.IsZero() {
		rows, qErr = s.db.QueryContext(ctx,
			`SELECT id, channel_id, sender_id, content, timestamp, thread_id, mentions, metadata
			   FROM messages
			  WHERE channel_id = ?
			  ORDER BY timestamp DESC
			  LIMIT ?`, channelID, limit)
	} else {
		rows, qErr = s.db.QueryContext(ctx,
			`SELECT id, channel_id, sender_id, content, timestamp, thread_id, mentions, metadata
			   FROM messages
			  WHERE channel_id = ? AND timestamp < ?
			  ORDER BY timestamp DESC
			  LIMIT ?`, channelID, before, limit)
	}
	if qErr != nil {
		return nil, fmt.Errorf("channels: history query: %w", qErr)
	}
	defer func() { _ = rows.Close() }()
	return scanMessageRows(rows)
}

// GetThread implements [ChannelStore.GetThread].
func (s *sqliteStore) GetThread(ctx context.Context, threadID string, limit int) ([]ChannelMessage, error) {
	q := `SELECT id, channel_id, sender_id, content, timestamp, thread_id, mentions, metadata
	        FROM messages
	       WHERE thread_id = ?
	       ORDER BY timestamp ASC`
	args := []any{threadID}
	if limit > 0 {
		q += " LIMIT ?"
		args = append(args, limit)
	}
	rows, err := s.db.QueryContext(ctx, q, args...)
	if err != nil {
		return nil, fmt.Errorf("channels: thread query: %w", err)
	}
	defer func() { _ = rows.Close() }()
	return scanMessageRows(rows)
}

// scanner abstracts *sql.Row vs *sql.Rows for [scanMessage].
type scanner interface {
	Scan(dest ...any) error
}

func scanMessage(s scanner) (ChannelMessage, error) {
	var (
		msg          ChannelMessage
		threadID     sql.NullString
		mentionsJSON string
		metadataJSON string
	)
	if err := s.Scan(
		&msg.ID, &msg.ChannelID, &msg.SenderID, &msg.Content, &msg.Timestamp,
		&threadID, &mentionsJSON, &metadataJSON,
	); err != nil {
		return ChannelMessage{}, err
	}
	if threadID.Valid {
		msg.ThreadID = threadID.String
	}
	if err := json.Unmarshal([]byte(mentionsJSON), &msg.Mentions); err != nil {
		return ChannelMessage{}, fmt.Errorf("channels: decode mentions: %w", err)
	}
	if err := json.Unmarshal([]byte(metadataJSON), &msg.Metadata); err != nil {
		return ChannelMessage{}, fmt.Errorf("channels: decode metadata: %w", err)
	}
	return msg, nil
}

func scanMessageRows(rows *sql.Rows) ([]ChannelMessage, error) {
	out := make([]ChannelMessage, 0)
	for rows.Next() {
		msg, err := scanMessage(rows)
		if err != nil {
			return nil, err
		}
		out = append(out, msg)
	}
	if err := rows.Err(); err != nil {
		return nil, fmt.Errorf("channels: row error: %w", err)
	}
	return out, nil
}

func encodeMentions(m []string) (string, error) {
	if len(m) == 0 {
		return "[]", nil
	}
	b, err := json.Marshal(m)
	if err != nil {
		return "", fmt.Errorf("channels: encode mentions: %w", err)
	}
	return string(b), nil
}

func encodeMetadata(m map[string]any) (string, error) {
	if len(m) == 0 {
		return "{}", nil
	}
	b, err := json.Marshal(m)
	if err != nil {
		return "", fmt.Errorf("channels: encode metadata: %w", err)
	}
	return string(b), nil
}

// isUniqueViolation reports whether err signals a SQLite UNIQUE constraint
// failure. modernc.org/sqlite returns errors with messages like
// "constraint failed: UNIQUE ...". A typed-error path via the driver's
// `*sqlite.Error` would be cleaner, but matching on the message text keeps
// this package free of driver-specific imports beyond the `_` registration.
func isUniqueViolation(err error) bool {
	return err != nil && strings.Contains(err.Error(), "UNIQUE constraint")
}

// isForeignKeyViolation reports whether err signals a SQLite FOREIGN KEY
// constraint failure (e.g., AddMember against a missing channel id).
func isForeignKeyViolation(err error) bool {
	return err != nil && strings.Contains(err.Error(), "FOREIGN KEY constraint")
}
