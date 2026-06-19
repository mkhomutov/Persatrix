package channels

// RFC 0036 PR 2 — the membership-scoped, epoch-filtered verbatim search query.
// This is the load-bearing access-control PR: the `membership_intervals`
// `EXISTS` join *is* the recall authorization decision, and the `epoch_id`
// strict-equality filter is the run-isolation boundary (§OQ-6 lock). Both are
// server-side, in SQL, and bound from trusted request context — the FTS `MATCH`
// text is a separate AND-ed predicate that can never reach them.
//
// The query has two paths, the same degradation the episodic tier ships
// (agents/memory/episodic_queries.py): an FTS5 MATCH over `messages_fts` when
// the index is present and the query carries searchable terms, and a `LIKE`
// substring fallback when FTS5 is unavailable (or the query sanitizes to no
// terms). Both apply the identical scope + epoch + narrowing predicates, so the
// fallback returns the same row set — only ranking differs.

import (
	"context"
	"errors"
	"fmt"
	"regexp"
	"strings"
)

// fts5SanitizeRecall collapses every run of non-alphanumeric, non-space
// characters to a single space — the Go twin of the episodic tier's
// `_FTS5_SANITIZE` (`re.compile(r'[^a-zA-Z0-9\s]+')`). After this pass a query
// holds only `[A-Za-z0-9]` runs and whitespace, so each token can be wrapped in
// FTS5 double quotes with no inner escaping (there are no quotes left to
// escape). Quoting makes every token a literal term — FTS5 operator keywords
// (AND/OR/NOT/NEAR) and metacharacters become inert search text rather than
// syntax, so a crafted query cannot error the statement or alter the scope join.
var fts5SanitizeRecall = regexp.MustCompile(`[^a-zA-Z0-9\s]+`)

// recallMessageColumns is the `messages` projection, m-aliased and in the column
// order [scanMessage] expects, so recall reuses [scanMessageRows] verbatim.
const recallMessageColumns = `m.id, m.channel_id, m.sender_id, m.content, m.timestamp, ` +
	`m.thread_id, m.mentions, m.metadata, m.session_id`

// membershipEpochScope returns the RFC 0035 §F membership predicate as a
// correlated `EXISTS`, AND the non-optional `epoch_id` equality (§OQ-6), as one
// SQL fragment plus its bound args in placeholder order ([epochID,
// participantID]). It assumes the outer query aliases `messages` as `m`.
//
// This is the single encoding of "was participant P a member of this channel
// when this message was sent, in this epoch?" — the half-open `[joined_at,
// left_at)` interval test that [InScope] expresses in Go. PR 5's scoped history
// query reuses this exact fragment so the §C recall predicate and the §G window
// clause are provably identical and cannot drift.
func membershipEpochScope(participantID, epochID string) (string, []any) {
	const clause = `m.epoch_id = ?
          AND EXISTS (
              SELECT 1 FROM membership_intervals mi
               WHERE mi.channel_id = m.channel_id
                 AND mi.participant_id = ?
                 AND mi.joined_at <= m.timestamp
                 AND (mi.left_at IS NULL OR m.timestamp < mi.left_at)
          )`
	return clause, []any{epochID, participantID}
}

// recallNarrowing builds the optional `channel_id` / `sender` / `after` /
// `before` predicates, each emitted only when supplied, plus their bound args in
// the same order. `after` is an inclusive lower bound and `before` an exclusive
// upper bound (matching [ChannelStore.GetHistory]'s `before`).
func recallNarrowing(p RecallParams) (string, []any) {
	var b strings.Builder
	var args []any
	if p.ChannelID != "" {
		b.WriteString(" AND m.channel_id = ?")
		args = append(args, p.ChannelID)
	}
	if p.Sender != "" {
		b.WriteString(" AND m.sender_id = ?")
		args = append(args, p.Sender)
	}
	if !p.After.IsZero() {
		b.WriteString(" AND m.timestamp >= ?")
		args = append(args, p.After)
	}
	if !p.Before.IsZero() {
		b.WriteString(" AND m.timestamp < ?")
		args = append(args, p.Before)
	}
	return b.String(), args
}

// RecallMessages implements [ChannelStore.RecallMessages].
//
// It routes to the FTS5 path when the index exists and the query carries
// searchable terms, and to the `LIKE` fallback otherwise (FTS5 unavailable, or a
// query that sanitizes to no terms — which lists the in-scope set by recency).
// The scope + epoch + narrowing predicates and the server-side `limit` clamp are
// identical on both paths.
func (s *sqliteStore) RecallMessages(ctx context.Context, params RecallParams) ([]ChannelMessage, error) {
	if params.ParticipantID == "" {
		return nil, errors.New("channels: recall requires a participant id")
	}

	limit := params.effectiveLimit()
	scope, scopeArgs := membershipEpochScope(params.ParticipantID, params.epochOrDefault())
	narrow, narrowArgs := recallNarrowing(params)
	match := buildFTS5Match(params.Query)

	if match != "" && s.messagesFTSAvailable(ctx) {
		return s.recallViaFTS(ctx, match, scope, scopeArgs, narrow, narrowArgs, limit)
	}

	// LIKE fallback. A query with no searchable terms (empty / pure punctuation)
	// becomes a match-all so the caller still gets a recency-ordered scoped
	// listing; a query with terms on an FTS5-less build is a substring search.
	pattern := "%%"
	if match != "" {
		pattern = escapeLikePattern(params.Query)
	}
	return s.recallViaLike(ctx, pattern, scope, scopeArgs, narrow, narrowArgs, limit)
}

// recallViaFTS runs the FTS5 MATCH path, ordered BM25-dominant with recency as a
// mild tiebreak (OQ #3). Ordering is on the bare FTS5 `rank` column: FTS5 bm25
// scores are negative with more-negative meaning more relevant, so plain
// ascending `rank` is the canonical "best match first" sort, with `m.timestamp
// DESC` breaking exact ties toward the most recent message.
//
// We deliberately do NOT order by the episodic tier's `1.0/(1.0+ABS(rank))`
// `_normalize_bm25` shape: that maps a stronger match (larger `ABS(rank)`) to a
// *smaller* value, which inverts relevance order under `DESC`. There it is a
// [0,1] min-score *filter*, not an ordering key — the episodic ORDER BY itself
// uses `rank * -1`. Bare `rank` is both correct and the idiomatic FTS5 form.
func (s *sqliteStore) recallViaFTS(
	ctx context.Context, match, scope string, scopeArgs []any, narrow string, narrowArgs []any, limit int,
) ([]ChannelMessage, error) {
	q := `SELECT ` + recallMessageColumns + `
        FROM messages_fts
        JOIN messages m ON m.rowid = messages_fts.rowid
        WHERE messages_fts MATCH ?
          AND ` + scope + narrow + `
        ORDER BY messages_fts.rank, m.timestamp DESC
        LIMIT ?`

	args := make([]any, 0, 2+len(scopeArgs)+len(narrowArgs))
	args = append(args, match)
	args = append(args, scopeArgs...)
	args = append(args, narrowArgs...)
	args = append(args, limit)

	rows, err := s.db.QueryContext(ctx, q, args...)
	if err != nil {
		return nil, fmt.Errorf("channels: recall fts query: %w", err)
	}
	defer func() { _ = rows.Close() }()
	return scanMessageRows(rows)
}

// recallViaLike runs the substring fallback, ordered newest-first (LIKE matches
// are binary, so there is no relevance signal to rank on). Scope, epoch, and
// narrowing are byte-identical to the FTS path.
func (s *sqliteStore) recallViaLike(
	ctx context.Context, pattern, scope string, scopeArgs []any, narrow string, narrowArgs []any, limit int,
) ([]ChannelMessage, error) {
	q := `SELECT ` + recallMessageColumns + `
        FROM messages m
        WHERE m.content LIKE ? ESCAPE '\'
          AND ` + scope + narrow + `
        ORDER BY m.timestamp DESC
        LIMIT ?`

	args := make([]any, 0, 2+len(scopeArgs)+len(narrowArgs))
	args = append(args, pattern)
	args = append(args, scopeArgs...)
	args = append(args, narrowArgs...)
	args = append(args, limit)

	rows, err := s.db.QueryContext(ctx, q, args...)
	if err != nil {
		return nil, fmt.Errorf("channels: recall like query: %w", err)
	}
	defer func() { _ = rows.Close() }()
	return scanMessageRows(rows)
}

// messagesFTSAvailable reports whether the `messages_fts` index exists. It is
// absent on an FTS5-less build (migration v10 skipped the virtual table) — the
// degradation PR 1 left for this query to detect. The lookup is a single indexed
// read of `sqlite_master`; recall is not a hot path, so it runs per call rather
// than caching a flag on the store.
func (s *sqliteStore) messagesFTSAvailable(ctx context.Context) bool {
	var n int
	err := s.db.QueryRowContext(ctx,
		`SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = 'messages_fts'`).Scan(&n)
	return err == nil && n > 0
}

// buildFTS5Match turns a free-text query into a safe FTS5 MATCH expression:
// sanitize to alphanumeric tokens, then double-quote each so it is a literal
// term (neutralising operator keywords and metacharacters), joined by spaces
// (an implicit AND of the terms). Returns "" when the query holds no searchable
// characters — the caller then takes the recency-listing fallback.
func buildFTS5Match(query string) string {
	sanitized := strings.TrimSpace(fts5SanitizeRecall.ReplaceAllString(query, " "))
	if sanitized == "" {
		return ""
	}
	tokens := strings.Fields(sanitized)
	quoted := make([]string, len(tokens))
	for i, tok := range tokens {
		quoted[i] = `"` + tok + `"`
	}
	return strings.Join(quoted, " ")
}

// escapeLikePattern escapes the LIKE metacharacters (`\`, `%`, `_`) and wraps the
// query in `%…%` for a substring match — the Go twin of the episodic tier's LIKE
// escaping, paired with `ESCAPE '\'` at the query site.
func escapeLikePattern(query string) string {
	r := strings.NewReplacer(`\`, `\\`, `%`, `\%`, `_`, `\_`)
	return "%" + r.Replace(query) + "%"
}
