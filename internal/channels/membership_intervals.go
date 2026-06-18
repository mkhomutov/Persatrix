package channels

// RFC 0035 — the read surface over the `membership_intervals` ledger. The type
// and the in-scope predicate live here rather than in channels.go, which sits
// at the repo's 500-line code cap; the SQLite read method is in the sibling
// sqlite_membership_intervals.go, co-located with PR 3's ledger write helpers.

import "time"

// MembershipInterval is one row of the `membership_intervals` ledger (RFC 0035
// §B): a single membership stint for a `(ChannelID, ParticipantID)` pair. The
// interval is half-open `[JoinedAt, LeftAt)`.
//
// LeftAt is the zero [time.Time] while the stint is open — the read method maps
// a SQL NULL `left_at` to the zero value rather than threading a `*time.Time`
// or `sql.NullTime` through callers. Use [time.Time.IsZero] to test for an open
// stint; [InScope] does exactly that.
type MembershipInterval struct {
	ChannelID     string
	ParticipantID string
	JoinedAt      time.Time
	LeftAt        time.Time // zero ⇒ open
}

// InScope reports whether a message at time `t` falls inside any of `intervals`
// — the single membership-access predicate every consumer applies (RFC 0035
// §F). An interval contains `t` iff:
//
//	JoinedAt <= t  AND  (LeftAt is zero/open  OR  t < LeftAt)
//
// The interval is half-open `[JoinedAt, LeftAt)`: a message at the exact join
// instant is in scope, one at the exact leave instant is not. That makes a
// back-to-back leave-then-rejoin unambiguous — the closing `LeftAt` of one
// stint and the opening `JoinedAt` of the next can be equal without a message
// falling into both or neither.
//
// This Go helper pins the predicate in one place for the [GetMembershipIntervals]
// callers, the tests, and the Phase 2 inspection endpoint. RFC 0036's recall
// query expresses the *same* predicate as a SQL `EXISTS` join and does not call
// this helper; both are tested against the same join/leave/rejoin fixtures so
// the two encodings cannot drift.
func InScope(intervals []MembershipInterval, t time.Time) bool {
	for _, iv := range intervals {
		// JoinedAt <= t, i.e. t is not strictly before the join.
		if t.Before(iv.JoinedAt) {
			continue
		}
		// Open stint (zero LeftAt) extends to +∞; a closed stint excludes its
		// LeftAt instant (t < LeftAt).
		if iv.LeftAt.IsZero() || t.Before(iv.LeftAt) {
			return true
		}
	}
	return false
}
