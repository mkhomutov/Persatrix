package channels

// RFC 0035 — SQLite reads over the `membership_intervals` ledger. Kept in its
// own file (not sqlite_query.go, which is near the 500-line cap) so the ledger
// read sits beside PR 3's ledger write helpers rather than scattered across the
// general query file.

import (
	"context"
	"database/sql"
	"fmt"
)

// GetMembershipIntervals returns every interval for `(channelID, participantID)`
// ordered by `joined_at` ascending — the data form of the ledger for the
// [InScope] predicate, the tests, and the Phase 2 inspection endpoint. An
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
		  ORDER BY joined_at ASC`, channelID, participantID)
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
