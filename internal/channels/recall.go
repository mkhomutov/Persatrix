package channels

// RFC 0036 PR 2 — the backend-agnostic surface of verbatim message recall: the
// [RecallParams] query shape and its server-side limit bounds. The SQLite
// implementation (the scoped FTS5 / LIKE query and [sqliteStore.RecallMessages])
// lives in the sibling sqlite_search.go — the same type/impl split RFC 0035 uses
// for [MembershipInterval] (membership_intervals.go) vs its SQLite reads
// (sqlite_membership_intervals.go). It lives here rather than beside its sibling
// ChannelMessage / Member types in channels.go because that file sits at the
// repo's 500-line code cap.

import "time"

const (
	// DefaultRecallLimit is the result count when a caller leaves
	// [RecallParams.Limit] at zero. Sized small because each recalled row is
	// verbatim text destined for a persona's prompt (RFC 0036 §F); the persona
	// tool (PR 4) passes its own small explicit limit, this is only the
	// unset-value floor.
	DefaultRecallLimit = 20

	// MaxRecallLimit is the hard server-side ceiling. The store clamps to it
	// regardless of the requested value so the prompt-cost bound holds even if a
	// future caller bypasses the persona tool and hits the endpoint directly
	// (RFC 0036 PR plan §PR 2 — "limit clamped server-side to a hard maximum").
	MaxRecallLimit = 100
)

// RecallParams is the input to [ChannelStore.RecallMessages]: a free-text search
// scoped to one participant's membership stints within one epoch, with optional
// narrowing. The membership + epoch fields are the access-control decision and
// are bound from trusted request context (the endpoint path segment + resolved
// epoch in PR 3), never from LLM-supplied query text.
type RecallParams struct {
	// ParticipantID scopes the search to this participant's
	// `membership_intervals` stints — a message is recallable only if its
	// timestamp falls inside one of them (RFC 0035 §F as a SQL `EXISTS` join).
	// It is the access-control subject, bound from the endpoint path in PR 3.
	ParticipantID string

	// Query is the free-text search. It is sanitized before reaching FTS5
	// MATCH; an all-punctuation query that sanitizes to empty degrades to a
	// recency-ordered scoped listing.
	Query string

	// EpochID hard-filters to one run/test world (§OQ-6 lock) with strict
	// equality and no carve-out. Empty resolves to [DefaultEpochID] ("live") so
	// a single-world caller is fail-safe; PR 3 threads the request's resolved
	// epoch exactly as the publish handler does.
	EpochID string

	// ChannelID, when non-empty, narrows to a single channel.
	ChannelID string

	// Sender, when non-empty, narrows to one author (`messages.sender_id`).
	Sender string

	// After, when non-zero, keeps messages at or after this instant (inclusive
	// lower bound).
	After time.Time

	// Before, when non-zero, keeps messages strictly before this instant
	// (exclusive upper bound — matching [ChannelStore.GetHistory]'s `before`).
	Before time.Time

	// Limit is the maximum result count. A value <= 0 resolves to
	// [DefaultRecallLimit]; any value above [MaxRecallLimit] is clamped down.
	Limit int
}

// effectiveLimit resolves [RecallParams.Limit] against the default floor and the
// hard ceiling. Centralised here (not in the SQL site) so the bound is a single
// line of truth a future non-SQLite store reuses.
func (p RecallParams) effectiveLimit() int {
	if p.Limit <= 0 {
		return DefaultRecallLimit
	}
	if p.Limit > MaxRecallLimit {
		return MaxRecallLimit
	}
	return p.Limit
}

// epochOrDefault resolves [RecallParams.EpochID] to the strict-equality epoch the
// query binds, defaulting an empty value to [DefaultEpochID].
func (p RecallParams) epochOrDefault() string {
	if p.EpochID == "" {
		return DefaultEpochID
	}
	return p.EpochID
}
