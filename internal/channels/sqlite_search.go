package channels

// RFC 0036 PR 2 — the membership-scoped, epoch-filtered verbatim search query.
// This is the load-bearing access-control PR: the `membership_intervals`
// `EXISTS` join *is* the recall authorization decision, and the `epoch_id`
// strict-equality filter is the run-isolation boundary (§OQ-6 lock). Both are
// server-side, in SQL, and bound from trusted request context — the FTS `MATCH`
// text is a separate AND-ed predicate that can never reach them.
//
// The query has two paths, mirroring the episodic tier's FTS5/LIKE split
// (agents/memory/episodic_queries.py) though gated differently: an FTS5 MATCH
// over `messages_fts` when the index is present and the query carries searchable
// terms, and a `LIKE` fallback otherwise — FTS5 unavailable, or the query
// sanitizes to no terms. (The episodic tier ALSO drops to LIKE on a
// malformed-MATCH runtime error; this query instead quotes every token so MATCH
// cannot error, so its only fallback triggers are absent-index and empty-query.)
// Both paths apply the identical scope + epoch + narrowing predicates AND
// tokenize the query through the same [recallTokens] pass, so the fallback can
// never widen ACCESS and narrows on the same term set. Their per-token match
// rule still differs deliberately: FTS5 does a BM25-ranked whole-token AND
// (matches anywhere, in any order), while LIKE ANDs one `%token%` SUBSTRING per
// token (recency-ordered — a substring hit is binary, so there is no relevance
// signal). So the two diverge even for a SINGLE term, in both directions — LIKE
// matches a superstring token (`budget` hits `budgets`), FTS5 matches a case/
// diacritic-folded token (`café` hits `cafe`) — and the row sets are NOT
// interchangeable, only the scope is. (An earlier revision substring-matched the
// RAW query as one blob, so a multi-term query ALSO demanded the terms be
// adjacent — `budget report` missed `budget … report`; sharing the tokenizer
// removed that extra, non-FTS-like divergence.) modernc.org/sqlite always ships
// FTS5, so the term-carrying LIKE branch is reached only on an FTS5-less build
// (or a test that drops the index); the only branch prod hits is the empty-query
// one, a recency listing.

import (
	"context"
	"errors"
	"fmt"
	"regexp"
	"strings"
)

// fts5SanitizeRecall collapses every run of characters that are neither Unicode
// letters, Unicode digits, nor whitespace down to a single space. It is the Go
// cousin of the episodic tier's `_FTS5_SANITIZE` (`re.compile(r'[^a-zA-Z0-9\s]+')`)
// but DELIBERATELY widened from ASCII to Unicode (`\p{L}\p{N}`): the ASCII regex
// strips every non-Latin letter, so a Cyrillic / CJK / accented query sanitizes
// to empty (or a truncated stem) and silently degrades to the match-all recency
// listing — the search term is discarded with no error. That is wrong for a
// verbatim recall surface whose stored text is arbitrary persona/human language
// in any script; the episodic tier tolerates it because its corpus is the
// agent's own (English-shaped) memory. After this pass a query still holds only
// letter/digit runs and whitespace — no quote or FTS5 metacharacter survives (a
// `"` is punctuation, not `\p{L}`/`\p{N}`) — so each token can be wrapped in FTS5
// double quotes with no inner escaping. Quoting makes every token a literal
// term — FTS5 operator keywords (AND/OR/NOT/NEAR) and metacharacters become
// inert search text rather than syntax, so a crafted query cannot error the
// statement or alter the scope join.
var fts5SanitizeRecall = regexp.MustCompile(`[^\p{L}\p{N}\s]+`)

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

	limit := params.EffectiveLimit()
	scope, scopeArgs := membershipEpochScope(params.ParticipantID, params.epochOrDefault())
	narrow, narrowArgs := recallNarrowing(params)
	match := buildFTS5Match(params.Query)

	if match != "" && s.ftsAvailable {
		return s.recallViaFTS(ctx, match, scope, scopeArgs, narrow, narrowArgs, limit)
	}

	// LIKE fallback (FTS5 unavailable, or a query that sanitized to no terms).
	// Each searchable token becomes one `%token%` substring predicate, AND-ed —
	// the closest a substring scan gets to FTS5's order-independent token AND. A
	// query with no terms yields no patterns, so the caller still gets a
	// recency-ordered scoped listing of the whole in-scope set.
	return s.recallViaLike(ctx, buildLikePatterns(params.Query), scope, scopeArgs, narrow, narrowArgs, limit)
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
// are binary, so there is no relevance signal to rank on). It ANDs one
// `m.content LIKE ?` predicate per pattern in `patterns`; an empty slice emits
// no text predicate, so the result is a recency listing of the whole in-scope
// set. Scope, epoch, and narrowing are byte-identical to the FTS path — only the
// TEXT match differs.
func (s *sqliteStore) recallViaLike(
	ctx context.Context, patterns []string, scope string, scopeArgs []any, narrow string, narrowArgs []any, limit int,
) ([]ChannelMessage, error) {
	var text strings.Builder
	for range patterns {
		text.WriteString(` AND m.content LIKE ? ESCAPE '\'`)
	}

	q := `SELECT ` + recallMessageColumns + `
        FROM messages m
        WHERE ` + scope + narrow + text.String() + `
        ORDER BY m.timestamp DESC
        LIMIT ?`

	args := make([]any, 0, 1+len(scopeArgs)+len(narrowArgs)+len(patterns))
	args = append(args, scopeArgs...)
	args = append(args, narrowArgs...)
	for _, p := range patterns {
		args = append(args, p)
	}
	args = append(args, limit)

	rows, err := s.db.QueryContext(ctx, q, args...)
	if err != nil {
		return nil, fmt.Errorf("channels: recall like query: %w", err)
	}
	defer func() { _ = rows.Close() }()
	return scanMessageRows(rows)
}

// probeMessagesFTS reports whether the `messages_fts` index exists in this
// database. It is absent on an FTS5-less build (migration v10 skipped the
// virtual table) — the degradation PR 1 left for recall to detect.
//
// Called ONCE at construction ([NewSQLiteStore]) and cached on the store
// ([sqliteStore.ftsAvailable]): the table is created (or skipped) by migration
// v10 during applySchema and never changes for a handle's lifetime, so a
// per-call catalog lookup would only re-confirm an immutable fact. It would also
// not be free — the store pins `MaxOpenConns(1)`, so every recall would
// serialise an extra `sqlite_master` read against that single connection. A lone
// indexed read at open settles it. Uses [context.Background]: it runs before the
// store is handed out, with no caller context to honour, and a transient error
// fails safe to the LIKE fallback (which keeps the identical scope).
func (s *sqliteStore) probeMessagesFTS() bool {
	var n int
	err := s.db.QueryRowContext(context.Background(),
		`SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = 'messages_fts'`).Scan(&n)
	return err == nil && n > 0
}

// recallTokens sanitizes a free-text query to the alphanumeric token list both
// search paths share — [buildFTS5Match] quotes each into a literal MATCH term,
// [buildLikePatterns] wraps each in `%…%`. Centralising the tokenization is what
// keeps the two paths narrowing on the SAME terms; only their per-token match
// rule (whole-token vs substring) is allowed to differ. Returns nil when nothing
// searchable survives the sanitizer (an empty or pure-punctuation query).
func recallTokens(query string) []string {
	sanitized := strings.TrimSpace(fts5SanitizeRecall.ReplaceAllString(query, " "))
	if sanitized == "" {
		return nil
	}
	return strings.Fields(sanitized)
}

// buildFTS5Match turns a free-text query into a safe FTS5 MATCH expression:
// double-quote each [recallTokens] token so it is a literal term (neutralising
// operator keywords and metacharacters), joined by spaces (an implicit AND of
// the terms). Returns "" when the query holds no searchable characters — the
// caller then takes the recency-listing fallback.
func buildFTS5Match(query string) string {
	tokens := recallTokens(query)
	if len(tokens) == 0 {
		return ""
	}
	quoted := make([]string, len(tokens))
	for i, tok := range tokens {
		quoted[i] = `"` + tok + `"`
	}
	return strings.Join(quoted, " ")
}

// buildLikePatterns turns a free-text query into one `%token%` LIKE pattern per
// [recallTokens] token, for the FTS5-unavailable fallback to AND together. Using
// the same tokenizer as [buildFTS5Match] keeps the fallback narrowing on the
// identical term set, so a multi-term query is an order-independent per-token AND
// rather than a contiguous-substring match of the raw phrase. Returns nil when
// the query holds no searchable terms — the caller then lists the in-scope set
// by recency.
//
// recallTokens already stripped every LIKE metacharacter (`%` / `_` / `\` are
// neither `\p{L}` nor `\p{N}`), so the per-token escape is defense-in-depth: it
// keeps the pattern safe should that sanitizer ever be loosened, mirroring the
// FTS path's belt-and-suspenders double-quoting. Paired with `ESCAPE '\'` at the
// query site.
func buildLikePatterns(query string) []string {
	tokens := recallTokens(query)
	if len(tokens) == 0 {
		return nil
	}
	esc := strings.NewReplacer(`\`, `\\`, `%`, `\%`, `_`, `\_`)
	patterns := make([]string, len(tokens))
	for i, tok := range tokens {
		patterns[i] = "%" + esc.Replace(tok) + "%"
	}
	return patterns
}
