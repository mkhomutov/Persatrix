package channels

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"net/url"
	"strings"
	"sync"
	"time"

	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/metric"
	"go.uber.org/zap"

	_ "modernc.org/sqlite" // pure-Go SQLite driver; matches CGO_ENABLED=0 build (Dockerfile.orchestrator)
)

// DefaultSessionID is the synthetic carve-out applied to every channel /
// message write that arrives without an explicit session_id (RFC 0031
// Phase 1). Phase 2 makes this carve-out a tested invariant of the recall
// path; Phase 1 only pins the storage shape.
//
// Exported so callers outside this package (notably the orchestrator's
// boot-time `resolveSessionID` fallback) reference a single source of
// truth — PR #335 review L2.
const DefaultSessionID = "legacy"

// DefaultEpochID is the run/test-isolation epoch applied to every channel /
// message row that arrives without an explicit epoch (ISSUE-0085 PR 2 —
// channel-store schema v6). Unlike [DefaultSessionID] it is NOT a carve-out:
// the epoch recall predicate (forthcoming) is strict equality with no
// `legacy`-style "always visible" escape, so a fresh epoch sees nothing. PR 2
// only lands the column (backfilled here via the SQL DEFAULT); no writer sets
// a non-default epoch until the gRPC rail (PR 4) lights up the producer.
//
// Cross-language contract: mirrors `agents.epoch_id.DEFAULT_EPOCH_ID` — the
// `'live'` literal the persona-memory migration v12 backfills onto its five
// tiers. A rename here is a conscious break that must move in lock-step with
// the Python leaf.
const DefaultEpochID = "live"

// SessionMetrics is the subset of orchestrator OTEL handles the channel
// store needs for the RFC 0031 `sessions.writes` counter. Defined locally
// so the channels package does not take a dependency on the orchestrator-
// wide instrument struct.
//
// Nil-safe: a nil SessionMetrics value disables emission so unit tests and
// minimal deployments run without OTEL wiring.
type SessionMetrics struct {
	// Writes counts each `CreateChannel` / `CreateChannelWithMembers` /
	// `GetOrCreateDM` / `PublishMessage` write attributed to a
	// session_id. Phase 1 inserts the env-var value verbatim, so
	// cardinality is bounded by the operator-controlled session count.
	Writes metric.Int64Counter
}

// DefaultMaxMessagesPerChannel is the per-channel oldest-first prune cap when
// a config value is not supplied (RFC 0011 §B).
const DefaultMaxMessagesPerChannel = 10_000

// DefaultMaxChannels is the global named-group-channel cap when a config
// value is not supplied (RFC 0011 §B).
const DefaultMaxChannels = 50

// SQLiteOptions configures [NewSQLiteStore].
//
// `MaxChannels` is checked against the named-group population by
// `CreateChannel`. `MaxMessagesPerChannel` is checked inside `PublishMessage`
// after the insert; oldest-first pruning runs in the same transaction so the
// thread-FK cascade resolves atomically with the new write.
//
// `Logger` is optional. When nil the store uses `zap.NewNop()` so callers in
// tests can omit it without losing the orchestrator's structured-logging
// convention (PR #231 review Should-Fix #4: previously every cap-enforcement
// and channel-lifecycle event was silent in production).
type SQLiteOptions struct {
	MaxChannels           int
	MaxMessagesPerChannel int
	Logger                *zap.Logger
	// SessionMetrics is optional; nil disables `sessions.writes`
	// emission. RFC 0031 Phase 1.
	SessionMetrics *SessionMetrics
}

// NewSQLiteStore opens (or creates) the channel database at `path` and
// applies the schema migration. WAL mode and foreign-key enforcement are
// turned on at connection time — the latter is required for both the
// `messages.thread_id` self-cascade and the `memberships`/`messages` →
// `channels(id)` cascades to fire.
//
// Pass `:memory:` (or `file::memory:?cache=shared`) for unit tests; pass an
// absolute filesystem path for production.
func NewSQLiteStore(path string, opts SQLiteOptions) (ChannelStore, error) {
	if opts.MaxChannels <= 0 {
		opts.MaxChannels = DefaultMaxChannels
	}
	if opts.MaxMessagesPerChannel <= 0 {
		opts.MaxMessagesPerChannel = DefaultMaxMessagesPerChannel
	}
	logger := opts.Logger
	if logger == nil {
		logger = zap.NewNop()
	}

	dsn := buildDSN(path)
	db, err := sql.Open("sqlite", dsn)
	if err != nil {
		return nil, fmt.Errorf("channels: open %s: %w", path, err)
	}
	// MaxOpenConns is intentionally pinned to 1 for v0.3.0.
	//
	// Rationale (per PR #231 review): WAL mode permits concurrent readers and
	// modernc.org/sqlite is connection-safe, so the file is technically
	// capable of more. We pin to 1 anyway because:
	//
	//  1. Every write path in this PR (CreateChannel cap check, PublishMessage
	//     publish+prune, GetOrCreateDM existence check + insert) wraps a
	//     read-then-write transaction whose correctness depends on the lack
	//     of an interleaved writer. A pool of >1 + WAL would still serialise
	//     writers via SQLITE_BUSY, but the cap-check TOCTOU window in
	//     CreateChannel widens.
	//  2. PR 1 ships persistence only — no REST traffic is layered on top yet,
	//     so the read-throughput penalty is not measurable.
	//
	// TODO(rfc0011-pr2): when the REST router lands and concurrent reads
	// matter, lift the cap to a small bounded value (4–8), gate writers via
	// busy_timeout/sqlite-side serialisation, and replace the cap-check
	// SELECT+INSERT with an idempotent INSERT-or-fail.
	db.SetMaxOpenConns(1)

	if err := applySchema(db); err != nil {
		_ = db.Close()
		return nil, err
	}

	return &sqliteStore{
		db:                    db,
		maxChannels:           opts.MaxChannels,
		maxMessagesPerChannel: opts.MaxMessagesPerChannel,
		logger:                logger,
		sessionMetrics:        opts.SessionMetrics,
	}, nil
}

// buildDSN attaches the PRAGMA flags every channel-store connection needs:
// foreign keys ON (cascade enforcement), WAL mode (consistent with the rest
// of the project), and a generous busy timeout so concurrent tests don't
// flake under SQLite's writer-exclusion contract.
//
// `path` may itself carry a query string — the doc-comment on
// [NewSQLiteStore] advertises `file::memory:?cache=shared` as a supported
// form. We split the existing query off, merge the caller's params into the
// `_pragma` Values, and emit a single `?`-separated DSN. Concatenating
// `path + "?" + q.Encode()` (the pre-ISSUE-0049 form) produced two `?`
// separators on a `file:` URI, which the SQLite driver parsed as a single
// malformed `cache` value and rejected with "no such cache mode" — every
// PRAGMA was dropped on the floor (or the open failed outright).
func buildDSN(path string) string {
	base, existing := path, ""
	if i := strings.Index(path, "?"); i >= 0 {
		base, existing = path[:i], path[i+1:]
	}
	// url.ParseQuery on an empty string returns an empty Values + nil error;
	// on a non-empty malformed string it returns a partial Values + a non-nil
	// error. We accept partials silently because the SQLite driver itself is
	// the authoritative parser of the resulting DSN — surfacing a parse error
	// here would block paths the driver would have accepted.
	q, _ := url.ParseQuery(existing)
	q.Add("_pragma", "foreign_keys(1)")
	q.Add("_pragma", "journal_mode(WAL)")
	q.Add("_pragma", "busy_timeout(5000)")
	return base + "?" + q.Encode()
}

// sqliteStore is the concrete [ChannelStore] backed by SQLite.
//
// `dmMu` serialises [GetOrCreateDM] for a given canonical id so a publish-
// against-new-DM race cannot insert the channel row twice. The lock is
// process-local — multi-process deployments are out of scope for v0.3.0
// (Non-Goal: cross-node federation).
type sqliteStore struct {
	db                    *sql.DB
	maxChannels           int
	maxMessagesPerChannel int
	logger                *zap.Logger
	sessionMetrics        *SessionMetrics

	dmMu sync.Mutex
}

// recordSessionWrite increments the `sessions.writes` counter with the
// `session_id` attribute set. No-op when SessionMetrics is unset (test
// fixtures / channels-disabled deployments).
func (s *sqliteStore) recordSessionWrite(ctx context.Context, sessionID string) {
	if s.sessionMetrics == nil || s.sessionMetrics.Writes == nil {
		return
	}
	s.sessionMetrics.Writes.Add(ctx, 1, metric.WithAttributes(attribute.String("session_id", sessionID)))
}

// Close implements [ChannelStore.Close].
func (s *sqliteStore) Close() error { return s.db.Close() }

// CreateChannel implements [ChannelStore.CreateChannel].
func (s *sqliteStore) CreateChannel(ctx context.Context, ch Channel) error {
	if !ch.Type.Valid() {
		return fmt.Errorf("%w: %q", ErrInvalidChannelType, ch.Type)
	}
	if ch.ID == "" {
		return errors.New("channels: channel id is required")
	}
	// PR #245 re-review (Low/Med): the name-required and name-pattern
	// errors used to be plain (un-wrapped) errors. writeChannelError in
	// internal/server falls through to 500 INTERNAL for any error that
	// does not match a known sentinel, so an operator typing
	// `Name: "Sprint 1"` got a 500 instead of the correct 400. Wrapping
	// with ErrInvalidChannelType (the closest existing 400-class
	// sentinel — these ARE invalid channel attributes) reclassifies the
	// status code without changing the human-readable message.
	if ch.Type == ChannelTypeGroup && ch.Name == "" {
		return fmt.Errorf("%w: group channel requires a name", ErrInvalidChannelType)
	}
	// PR #231 review-2 Should-Fix #1: enforce channelNamePattern at the
	// store boundary too, not only in the loader (Config.Validate). The REST
	// surface in PR 2 will route through CreateChannel and would otherwise
	// accept group names that the next restart's LoadConfig would reject,
	// breaking the config-vs-store parity invariant from RFC 0011 §B.
	// DM and thread channels store their canonical id in the `name` column
	// as a placeholder (see storedName below) and are exempt — only
	// user-declared group names are user-visible.
	if ch.Type == ChannelTypeGroup && !channelNamePattern.MatchString(ch.Name) {
		return fmt.Errorf("%w: group channel name %q does not match %s",
			ErrInvalidChannelType, ch.Name, channelNamePattern.String())
	}
	// PR #231 review SF-2: make CreateChannel the canonical-id authority for
	// group channels. The previous implementation accepted any
	// `(ID, Name)` pair so a REST handler could insert a row whose PK
	// disagreed with its display name (e.g. ID=`group:foo`, Name=`bar`).
	// Future memory and observability rows reference the canonical id; a
	// drift here would mis-route every downstream lookup. We require the
	// caller to supply the exact PK they intend to use rather than mutate
	// the input — the wrapper signature stays value-receiver so a
	// surprising in-place rewrite cannot happen at higher layers.
	if ch.Type == ChannelTypeGroup && ch.ID != "group:"+ch.Name {
		return fmt.Errorf("%w: group channel id %q must be \"group:\"+name (name=%q)",
			ErrInvalidChannelType, ch.ID, ch.Name)
	}
	if ch.CreatedAt.IsZero() {
		ch.CreatedAt = time.Now().UTC()
	}
	// RFC 0031 Phase 1: rewrite the empty default at the store boundary
	// so session-unaware callers persist a queryable row.
	if ch.SessionID == "" {
		ch.SessionID = DefaultSessionID
	}

	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	defer func() { _ = tx.Rollback() }()

	if ch.Type == ChannelTypeGroup {
		var groupCount int
		if err := tx.QueryRowContext(ctx,
			`SELECT COUNT(1) FROM channels WHERE channel_type = 'group'`).
			Scan(&groupCount); err != nil {
			return fmt.Errorf("channels: count groups: %w", err)
		}
		if groupCount >= s.maxChannels {
			return fmt.Errorf("%w: cap=%d", ErrChannelCapExceeded, s.maxChannels)
		}
	}

	// SF-4 (PR 2 of RFC 0011): channels.name is now nullable. Group
	// channels store their declared display name; DM and thread channels
	// store NULL. Uniqueness is enforced by the partial index
	// `ux_channels_name_group` (group rows only) — see migrateV1ToV2.
	var nameArg any
	if ch.Type == ChannelTypeGroup {
		nameArg = ch.Name
	} else {
		nameArg = nil
	}

	_, err = tx.ExecContext(ctx,
		`INSERT INTO channels (id, name, channel_type, description, created_at, session_id)
		 VALUES (?, ?, ?, ?, ?, ?)`,
		ch.ID, nameArg, string(ch.Type), ch.Description, ch.CreatedAt, ch.SessionID,
	)
	if err != nil {
		if isUniqueViolation(err) {
			return fmt.Errorf("%w: %s", ErrChannelExists, ch.ID)
		}
		return fmt.Errorf("channels: insert channel: %w", err)
	}

	if err := tx.Commit(); err != nil {
		return err
	}
	// PR #231 review Should-Fix #4: surface lifecycle events through the
	// orchestrator's structured logger. Info-level matches the cardinality
	// (one event per declared channel + one per implicit DM/thread).
	s.logger.Info("channels: channel created",
		zap.String("channel_id", ch.ID),
		zap.String("channel_type", string(ch.Type)),
		zap.String("session_id", ch.SessionID))
	s.recordSessionWrite(ctx, ch.SessionID)
	return nil
}

// CreateChannelWithMembers implements [ChannelStore.CreateChannelWithMembers].
//
// PR #245 review (High): the REST `POST /api/v1/channels` handler used
// to call CreateChannel followed by an N-call AddMember loop, all
// outside any transaction. A failure mid-loop left the channel row
// committed but with only a prefix of the requested membership; the
// client's natural retry then hit ErrChannelExists \u2192 409 and the
// remaining members were never added. This helper makes the bundle
// atomic at the store boundary so handlers no longer need to compose a
// rollback path of their own.
//
// Member validation runs inside the transaction so the same
// `ErrInvalidParticipantID` / `ErrInvalidRespondPolicy` sentinels surface
// as before \u2014 only the persistence side becomes all-or-nothing.
func (s *sqliteStore) CreateChannelWithMembers(ctx context.Context, ch Channel, members []Member) error {
	// Pre-flight validation duplicates the checks in [CreateChannel] /
	// [AddMember] so we can fail fast before touching the database. Any
	// future change to those rules MUST be mirrored here \u2014 the unit test
	// TestSQLiteStore_CreateChannelWithMembers_AtomicOnPartialFailure
	// pins the rollback contract for one concrete invalid-member case.
	if !ch.Type.Valid() {
		return fmt.Errorf("%w: %q", ErrInvalidChannelType, ch.Type)
	}
	if ch.ID == "" {
		return errors.New("channels: channel id is required")
	}
	// PR #245 re-review (Low/Med): mirror the wrapping fix from
	// CreateChannel above so REST callers see 400 BAD_REQUEST instead of
	// 500 INTERNAL when they submit an invalid `name`.
	if ch.Type == ChannelTypeGroup && ch.Name == "" {
		return fmt.Errorf("%w: group channel requires a name", ErrInvalidChannelType)
	}
	if ch.Type == ChannelTypeGroup && !channelNamePattern.MatchString(ch.Name) {
		return fmt.Errorf("%w: group channel name %q does not match %s",
			ErrInvalidChannelType, ch.Name, channelNamePattern.String())
	}
	if ch.Type == ChannelTypeGroup && ch.ID != "group:"+ch.Name {
		return fmt.Errorf("%w: group channel id %q must be \"group:\"+name (name=%q)",
			ErrInvalidChannelType, ch.ID, ch.Name)
	}
	if ch.CreatedAt.IsZero() {
		ch.CreatedAt = time.Now().UTC()
	}
	if ch.SessionID == "" {
		ch.SessionID = DefaultSessionID
	}

	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	defer func() { _ = tx.Rollback() }()

	if ch.Type == ChannelTypeGroup {
		var groupCount int
		if err := tx.QueryRowContext(ctx,
			`SELECT COUNT(1) FROM channels WHERE channel_type = 'group'`).
			Scan(&groupCount); err != nil {
			return fmt.Errorf("channels: count groups: %w", err)
		}
		if groupCount >= s.maxChannels {
			return fmt.Errorf("%w: cap=%d", ErrChannelCapExceeded, s.maxChannels)
		}
	}

	var nameArg any
	if ch.Type == ChannelTypeGroup {
		nameArg = ch.Name
	} else {
		nameArg = nil
	}

	if _, err := tx.ExecContext(ctx,
		`INSERT INTO channels (id, name, channel_type, description, created_at, session_id)
		 VALUES (?, ?, ?, ?, ?, ?)`,
		ch.ID, nameArg, string(ch.Type), ch.Description, ch.CreatedAt, ch.SessionID,
	); err != nil {
		if isUniqueViolation(err) {
			return fmt.Errorf("%w: %s", ErrChannelExists, ch.ID)
		}
		return fmt.Errorf("channels: insert channel: %w", err)
	}

	now := time.Now().UTC()
	for _, m := range members {
		if err := validateParticipantID(m.ParticipantID); err != nil {
			return err
		}
		policy := m.RespondPolicy
		if policy == "" {
			policy = RespondWhenMentioned
		}
		if !policy.Valid() {
			return fmt.Errorf("%w: %q", ErrInvalidRespondPolicy, policy)
		}
		joinedAt := m.JoinedAt
		if joinedAt.IsZero() {
			joinedAt = now
		}
		if _, err := tx.ExecContext(ctx,
			`INSERT INTO memberships (channel_id, participant_id, respond_policy, joined_at)
			 VALUES (?, ?, ?, ?)
			 ON CONFLICT(channel_id, participant_id) DO NOTHING`,
			ch.ID, m.ParticipantID, string(policy), joinedAt,
		); err != nil {
			// FK violations are unexpected here \u2014 we just inserted the
			// parent row in the same transaction \u2014 but classify them the
			// same way [AddMember] does for symmetry.
			if isForeignKeyViolation(err) {
				return fmt.Errorf("%w: %s", ErrChannelNotFound, ch.ID)
			}
			return fmt.Errorf("channels: add member %s: %w", m.ParticipantID, err)
		}
	}

	if err := tx.Commit(); err != nil {
		return err
	}
	s.logger.Info("channels: channel created with members",
		zap.String("channel_id", ch.ID),
		zap.String("channel_type", string(ch.Type)),
		zap.String("session_id", ch.SessionID),
		zap.Int("member_count", len(members)))
	s.recordSessionWrite(ctx, ch.SessionID)
	return nil
}
