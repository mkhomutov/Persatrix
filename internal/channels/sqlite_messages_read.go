package channels

// The `messages` READ paths for the SQLite channel store — the projection
// constant, the four queries that use it, and the scanner they share. Split
// out of sqlite_messages.go (which keeps [sqliteStore.PublishMessage] and the
// cap-prune) when ISSUE-0130's `principal_id` column took that file to 494 of
// its 500 lines: a file parked at the cap turns the next fix into deleted
// rationale, so the split happens at the seam rather than one comment at a
// time. A PURE MOVE — no query, scan, or signature changed by it.
//
// The seam is write-vs-read: everything here is invoked against already
// committed rows and answers "what is in the table", while the sibling file
// owns validation, tenant stamping, the INSERT and the retention cap.

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"time"
)

// GetMessage implements [ChannelStore.GetMessage].
func (s *sqliteStore) GetMessage(ctx context.Context, messageID string) (ChannelMessage, error) {
	row := s.db.QueryRowContext(ctx,
		`SELECT `+messageColumns+`
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
			`SELECT `+messageColumns+`
			   FROM messages
			  WHERE channel_id = ?
			  ORDER BY timestamp DESC
			  LIMIT ?`, channelID, limit)
	} else {
		rows, qErr = s.db.QueryContext(ctx,
			`SELECT `+messageColumns+`
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

// GetHistoryScoped implements [ChannelStore.GetHistoryScoped] — the §G membership
// filter behind the conversation-window / catch-up `?as_participant=` param.
//
// It is [GetHistory] with the [membershipEpochScope] fragment AND-ed in: the SAME
// `membership_intervals` `EXISTS` + `epoch_id` predicate PR 2's recall query (§C)
// uses, so the live persona prompt and verbatim recall obey one access rule and
// cannot drift. The epoch is bound to [DefaultEpochID] ("live"), not a request
// override: the window reads the persisted `messages` rows, which always carry
// the "live" column default (persona-side epoch overrides ride the gRPC rail and
// are never persisted — see channel_epoch_override.go), so "live" is the only
// world the live transcript has. Ordering (newest-first), the `before` exclusive
// upper bound, and the `limit<=0 → 50` default all mirror [GetHistory].
//
// `messages` is aliased `m` so the shared fragment and the [recallMessageColumns]
// projection (m-aliased, in [scanMessage] order) both apply unchanged.
func (s *sqliteStore) GetHistoryScoped(ctx context.Context, channelID, participantID string, limit int, before time.Time) ([]ChannelMessage, error) {
	if participantID == "" {
		// Reject, don't run: an empty subject query would return an empty set
		// that reads as "no in-scope history" rather than "no scope subject".
		// Mirrors [sqliteStore.RecallMessages] so §C and §G share the input
		// guard, not just the EXISTS predicate. The handler treats a blank
		// `?as_participant=` as absent before reaching here.
		return nil, errors.New("channels: scoped history requires a participant id")
	}
	if limit <= 0 {
		limit = 50
	}
	scope, scopeArgs := membershipEpochScope(participantID, DefaultEpochID)

	q := `SELECT ` + recallMessageColumns + `
	        FROM messages m
	       WHERE m.channel_id = ?
	         AND ` + scope
	// args order tracks the `?` order: channel_id, then the scope fragment's
	// [epochID, participantID], then the optional `before`, then `limit`.
	args := make([]any, 0, 4+len(scopeArgs))
	args = append(args, channelID)
	args = append(args, scopeArgs...)
	if !before.IsZero() {
		q += ` AND m.timestamp < ?`
		args = append(args, before)
	}
	q += ` ORDER BY m.timestamp DESC LIMIT ?`
	args = append(args, limit)

	rows, err := s.db.QueryContext(ctx, q, args...)
	if err != nil {
		return nil, fmt.Errorf("channels: scoped history query: %w", err)
	}
	defer func() { _ = rows.Close() }()
	return scanMessageRows(rows)
}

// GetThread implements [ChannelStore.GetThread].
func (s *sqliteStore) GetThread(ctx context.Context, threadID string, limit int) ([]ChannelMessage, error) {
	q := `SELECT ` + messageColumns + `
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

// messageColumns is the `messages` projection every unaliased read in this
// file selects, in the column order [scanMessage] expects. The m-aliased twin
// is [recallMessageColumns] (sqlite_search.go); `TestMessageColumns_...`
// pins the two lists identical modulo the alias, so a column added to one
// cannot silently rot the other's scan.
//
// One const, not four inline lists: v12's `principal_id` had to be added to
// four copies of the same string, each feeding the same scanner — the shape
// where one missed copy is a runtime "expected 10 destination arguments"
// against whichever endpoint the tests happen not to cover.
const messageColumns = `id, channel_id, sender_id, content, timestamp, ` +
	`thread_id, mentions, metadata, session_id, principal_id`

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
		&threadID, &mentionsJSON, &metadataJSON, &msg.SessionID, &msg.PrincipalID,
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
