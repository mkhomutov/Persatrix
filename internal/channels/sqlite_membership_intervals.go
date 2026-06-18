package channels

// RFC 0035 — SQLite reads over the `membership_intervals` ledger. Kept in its
// own file (not sqlite_query.go, which is near the 500-line cap) so the ledger
// read sits beside PR 3's ledger write helpers rather than scattered across the
// general query file.

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"time"
)

// errMembershipLedgerDivergence is returned by [sqliteStore.RemoveMember] when
// closing a removed participant's open interval affects zero rows — a
// `memberships` row existed with no matching OPEN interval, so the current-state
// projection and the append-only ledger have diverged (RFC 0035 Goal 6 breach).
// It is deliberately loud: a silent commit would leave a removed participant
// with an interval that never closes, a data-*exposure* bug for RFC 0036
// verbatim recall (whose `EXISTS` join *is* the access decision). It surfaces as
// a 500 to REST — an invariant breach, not a client error — and is unexported
// because it is an internal correctness signal, not part of the store's error
// contract.
var errMembershipLedgerDivergence = errors.New(
	"channels: membership ledger divergence: no open interval to close")

// openMembershipInterval inserts a new OPEN interval (NULL left_at) for the pair
// inside tx — the ledger half of a membership admit (RFC 0035 §C). `now` is the
// SAME instant the caller writes to `memberships.joined_at`, so the projection
// and the ledger agree on the join boundary the half-open §F predicate compares
// against. The partial unique index `ux_membership_intervals_open` rejects a
// second open interval for the pair, so a caller that opens without first
// gating on a genuine insert will fail loudly rather than corrupt history.
func openMembershipInterval(ctx context.Context, tx *sql.Tx, channelID, participantID string, now time.Time) error {
	if _, err := tx.ExecContext(ctx,
		`INSERT INTO membership_intervals (channel_id, participant_id, joined_at, left_at)
		 VALUES (?, ?, ?, NULL)`,
		channelID, participantID, now,
	); err != nil {
		return fmt.Errorf("channels: open membership interval: %w", err)
	}
	return nil
}

// closeOpenMembershipInterval stamps `left_at = now` on the pair's OPEN interval
// inside tx and returns the number of rows closed. On a [sqliteStore.RemoveMember]
// success path the caller REQUIRES exactly one (Goal 6: every present member has
// one open interval); a zero-row close means the projection and the ledger
// diverged, and the caller MUST roll back with [errMembershipLedgerDivergence]
// rather than commit a never-closing interval.
func closeOpenMembershipInterval(ctx context.Context, tx *sql.Tx, channelID, participantID string, now time.Time) (int64, error) {
	res, err := tx.ExecContext(ctx,
		`UPDATE membership_intervals SET left_at = ?
		  WHERE channel_id = ? AND participant_id = ? AND left_at IS NULL`,
		now, channelID, participantID,
	)
	if err != nil {
		return 0, fmt.Errorf("channels: close membership interval: %w", err)
	}
	n, err := res.RowsAffected()
	if err != nil {
		return 0, fmt.Errorf("channels: close membership interval rowsaffected: %w", err)
	}
	return n, nil
}

// GetMembershipIntervals returns every interval for `(channelID, participantID)`
// ordered by `joined_at` then `id` ascending — the data form of the ledger for
// the [InScope] predicate, the tests, and the Phase 2 inspection endpoint. The
// `id` tiebreaker keeps the order deterministic when two stints share a
// `joined_at` (so a closed stint always sorts before a later open one). An
// unknown pair reads back as an empty slice, not an error: this is a lookup,
// not a membership assertion.
//
// A SQL NULL `left_at` (an open stint) maps to the zero [time.Time] on
// [MembershipInterval.LeftAt], per the type's open-stint convention. RFC 0036's
// recall query does NOT route through this method — it joins
// `membership_intervals` directly in SQL (RFC 0035 §E) — so this read carries no
// epoch/session filtering of its own; it is the raw interval list for a pair.
func (s *sqliteStore) GetMembershipIntervals(
	ctx context.Context, channelID, participantID string,
) ([]MembershipInterval, error) {
	rows, err := s.db.QueryContext(ctx,
		`SELECT channel_id, participant_id, joined_at, left_at
		   FROM membership_intervals
		  WHERE channel_id = ? AND participant_id = ?
		  ORDER BY joined_at ASC, id ASC`, channelID, participantID)
	if err != nil {
		return nil, fmt.Errorf("channels: get membership intervals: %w", err)
	}
	defer func() { _ = rows.Close() }()

	out := make([]MembershipInterval, 0)
	for rows.Next() {
		var iv MembershipInterval
		var leftAt sql.NullTime
		if err := rows.Scan(&iv.ChannelID, &iv.ParticipantID, &iv.JoinedAt, &leftAt); err != nil {
			return nil, fmt.Errorf("channels: scan membership interval: %w", err)
		}
		if leftAt.Valid {
			iv.LeftAt = leftAt.Time
		}
		out = append(out, iv)
	}
	return out, rows.Err()
}

// GetAccessibleChannels returns the distinct set of channel ids `participantID`
// has EVER held a membership interval in — across both open and closed stints —
// ordered by channel id ascending. An unknown participant returns an empty
// slice, not an error.
//
// This is the RFC 0035 Phase 2 operator-inspection convenience ("what channels
// was X ever in", for audit reconstruction). It is deliberately "ever a member"
// rather than "currently a member" (which `memberships` already answers via
// GetMembers): a closed-only stint still grants a recall scope over that
// channel's messages for the stint's window, so an audit view must surface it.
// RFC 0036 recall does NOT route through this method — its default-all-channels
// search joins `membership_intervals` directly in SQL (RFC 0035 §E).
//
// NOTE: it has no in-tree caller yet — the Phase 2 history endpoint serves
// per-channel intervals from GetMembershipIntervals, and recall bypasses this
// read. It ships as the RFC 0035 Phase-2 optional convenience (the RFC marks it
// cut-tolerant); revisit at the RFC 0035 closeout (PR 5) if still unconsumed.
func (s *sqliteStore) GetAccessibleChannels(ctx context.Context, participantID string) ([]string, error) {
	rows, err := s.db.QueryContext(ctx,
		`SELECT DISTINCT channel_id
		   FROM membership_intervals
		  WHERE participant_id = ?
		  ORDER BY channel_id ASC`, participantID)
	if err != nil {
		return nil, fmt.Errorf("channels: get accessible channels: %w", err)
	}
	defer func() { _ = rows.Close() }()

	out := make([]string, 0)
	for rows.Next() {
		var channelID string
		if err := rows.Scan(&channelID); err != nil {
			return nil, fmt.Errorf("channels: scan accessible channel: %w", err)
		}
		out = append(out, channelID)
	}
	return out, rows.Err()
}
